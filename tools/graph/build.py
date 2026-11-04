#!/usr/bin/env python3
"""Build the interactive architecture graph for the local stack (docs/architecture/).

The graph is extracted from the code wherever possible, so it stays honest as things change:

  compose/*.yaml                 services, depends_on, who-talks-to-whom (env URLs), volumes, mounts
  config/servicebus/Config.json  topics, subscriptions and their filters
  repos/srp                      Azure Functions, their triggers, the topics they publish to, renderers
  repos/languageapi              controller routes, bus/timer functions, Drizzle tables, Hasura metadata
  tools/fdb                      fdb modules, imports and HTTP endpoints
  Makefile, seed/, scripts/      make targets, scripts and the services they touch

tools/graph/knowledge.json adds what code can't say: plain-language descriptions, a few edges that
only exist at runtime, known gaps, and the guided tours. Every node it names must exist, so a rename
in the code fails the build instead of silently leaving stale docs.

Usage:
  python3 tools/graph/build.py           write docs/architecture/{index.html,graph.json,REPORT.md}
  python3 tools/graph/build.py --check   exit 1 if those files are out of date

Needs PyYAML (`pip install pyyaml`) and checked-out submodules (`make init`).
"""
from __future__ import annotations

import argparse
import configparser
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment dependent
    sys.exit("tools/graph/build.py needs PyYAML: pip install pyyaml")

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "architecture"

# compose/<file>.yaml -> system that owns the services defined there.
COMPOSE_SYSTEMS = {"infra": "infra", "wacs": "wacs", "languageapi": "languageapi", "srp": "srp", "tools": "fdb"}
SUBMODULE_SYSTEMS = {"wacs": "wacs", "srp": "srp", "languageapi": "languageapi"}


class BuildError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------- model

class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple, dict] = {}

    def node(self, nid: str, label: str, kind: str, *, system: str | None = None, parent: str | None = None,
             desc: str | None = None, source: tuple[str, int] | None = None, **attrs) -> str:
        n = self.nodes.setdefault(nid, {"id": nid, "label": label, "kind": kind, "sources": [], "attrs": {}})
        n["label"] = label or n["label"]
        n["kind"] = kind
        if system:
            n["system"] = system
        if parent:
            n["parent"] = parent
        if desc and not n.get("desc"):
            n["desc"] = desc
        if source and list(source) not in n["sources"]:
            n["sources"].append(list(source))
        for k, v in attrs.items():
            if v not in (None, "", [], {}):
                n["attrs"][k] = v
        return nid

    def edge(self, src: str, dst: str, kind: str, label: str | None = None, *, evidence: str | None = None,
             origin: str = "extracted") -> None:
        if src == dst:
            return
        key = (src, dst, kind)
        e = self.edges.setdefault(key, {"source": src, "target": dst, "kind": kind, "labels": [],
                                        "evidence": [], "origin": origin})
        if label and label not in e["labels"]:
            e["labels"].append(label)
        if evidence and evidence not in e["evidence"]:
            e["evidence"].append(evidence)


def rel(p: Path) -> str:
    return p.resolve().relative_to(ROOT).as_posix()


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def default_env(value: str) -> str:
    """'${WACS_HTTP_PORT:-3000}:3000' -> '3000:3000'."""
    return re.sub(r"\$\{[A-Z0-9_]+:-([^}]*)\}", r"\1", str(value)).replace("$$", "$")


def comment_above(lines: list[str], idx: int) -> str | None:
    """Contiguous '#' comment lines directly above lines[idx], joined."""
    out = []
    i = idx - 1
    while i >= 0 and lines[i].strip().startswith("#"):
        out.append(lines[i].strip().lstrip("#").strip())
        i -= 1
    text = " ".join(reversed(out)).strip()
    return text or None


class _Loader(yaml.SafeLoader):
    pass


# Compose's `!reset` / Hasura's `!include`: keep the value, drop the tag.
_Loader.add_multi_constructor("!", lambda loader, suffix, node: (
    loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode) else
    loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else
    loader.construct_mapping(node)))


def load_yaml(path: Path):
    return yaml.load(path.read_text(), Loader=_Loader)  # noqa: S506 - SafeLoader subclass


# ------------------------------------------------------------------------------------------ compose

URL_HOST = re.compile(r"(?:https?|sb|postgres|redis)://(?:[^@/\s;]+@)?([a-z0-9][a-z0-9-]*)(?::\d+)?", re.I)


def extract_compose(g: Graph) -> dict:
    """Services and their wiring. Returns facts later extractors need (e.g. which SRP projects run)."""
    facts = {"srp_projects": {}, "services": set(), "env": {}}
    root = load_yaml(ROOT / "compose.yaml")
    files = [ROOT / f for f in root.get("include", [])]
    parsed = []
    for f in files:
        data = load_yaml(f) or {}
        parsed.append((f, data))
        facts["services"].update((data.get("services") or {}).keys())

    for f, data in parsed:
        system = COMPOSE_SYSTEMS.get(f.stem, "infra")
        text = f.read_text()
        lines = text.splitlines()
        named_volumes = set((data.get("volumes") or {}).keys())
        for name, svc in (data.get("services") or {}).items():
            svc = svc or {}
            idx = next(i for i, ln in enumerate(lines) if re.match(rf"^  {re.escape(name)}:\s*$", ln))
            build = svc.get("build") or {}
            attrs = {
                "image": svc.get("image"),
                "ports": [default_env(p) for p in svc.get("ports", [])],
                "profiles": svc.get("profiles"),
                "command": " ".join(svc["command"]) if isinstance(svc.get("command"), list) else
                (default_env(svc["command"]).strip() if svc.get("command") else None),
            }
            if isinstance(build, dict) and build:
                attrs["build_context"] = rel((f.parent / build["context"]))
                if build.get("args", {}).get("PROJECT"):
                    attrs["project"] = build["args"]["PROJECT"]
                    facts["srp_projects"][build["args"]["PROJECT"]] = f"svc:{name}"
            sid = g.node(f"svc:{name}", name, "service", system=system, parent=f"sys:{system}",
                         desc=comment_above(lines, idx), source=(rel(f), idx + 1), **attrs)

            # Startup ordering.
            deps = svc.get("depends_on") or {}
            if isinstance(deps, list):
                deps = {d: {} for d in deps}
            for dep, cond in deps.items():
                condition = (cond or {}).get("condition", "service_started")
                g.edge(sid, f"svc:{dep}", "depends_on", condition.replace("service_", "").replace("_", " "),
                       evidence=f"{rel(f)}: depends_on")

            # Runtime connections: any env value that points at another service.
            env = svc.get("environment") or {}
            if isinstance(env, list):
                env = dict(e.split("=", 1) for e in env)
            facts["env"][name] = {k: str(v) for k, v in env.items()}
            for key, value in sorted(env.items()):
                value = str(value)
                for host in URL_HOST.findall(value):
                    if host in facts["services"] and host != name:
                        g.edge(sid, f"svc:{host}", "uses", None, evidence=f"{key}={default_env(value)[:120]}")
                if re.search(r"Container$", key) or "devstoreaccount1/$$web" in value:
                    container = "$web" if "$$web" in value else value
                    g.node(f"blob:{container}", container, "blob", system="infra", parent="svc:azurite")
                    g.edge(sid, f"blob:{container}", "uses", None, evidence=f"{key}={default_env(value)}")

            # Data volumes and bind mounts.
            for vol in svc.get("volumes", []):
                src, _, target = str(vol).partition(":")
                target = target.split(":")[0]
                if src in named_volumes:
                    vid = g.node(f"vol:{src}", src, "volume", system=system, parent=f"sys:{system}",
                                 source=(rel(f), idx + 1), mount=target)
                    g.edge(sid, vid, "stores", target)
                elif src.startswith("."):
                    path = (f.parent / src).resolve()
                    if path.is_relative_to(ROOT / ".local"):
                        continue  # build-time scratch (extra CA bundle)
                    fid = file_node(g, path, system)
                    g.edge(sid, fid, "mounts", target)
            for env_file in svc.get("env_file", []):
                fid = file_node(g, (f.parent / env_file).resolve(), system)
                g.edge(sid, fid, "mounts", "env_file")

            if isinstance(build, dict) and build:
                dockerfile = (f.parent / build["context"] / build.get("dockerfile", "Dockerfile")).resolve()
                if dockerfile.exists():
                    fid = file_node(g, dockerfile, system)
                    g.edge(sid, fid, "built_by", attrs.get("project"))
    return facts


def file_node(g: Graph, path: Path, system: str) -> str:
    r = rel(path)
    if r.split("/")[0] in ("seed", "scripts"):
        system = "umbrella"
    desc = None
    if path.is_file():
        head = path.read_text(errors="replace").splitlines()[:12]
        comments = [ln.lstrip("#").strip() for ln in head if ln.startswith("#") and not ln.startswith("#!")
                    and "syntax=" not in ln]
        desc = " ".join(comments).strip() or None
    label = r if path.is_dir() else path.name
    if path.name in ("Dockerfile",) or label.startswith("."):
        label = r
    return g.node(f"file:{r}", label, "file", system=system, parent=f"sys:{system}", desc=desc, source=(r, 1),
                  is_dir=path.is_dir() or None)


# --------------------------------------------------------------------------------------- servicebus

def extract_servicebus(g: Graph) -> None:
    path = ROOT / "config/servicebus/Config.json"
    text = path.read_text()
    ns = json.loads(text)["UserConfig"]["Namespaces"][0]
    file_node(g, path, "infra")
    for topic in ns.get("Topics", []):
        tname = topic["Name"]
        tid = g.node(f"topic:{tname.lower()}", tname, "topic", system="infra", parent="svc:servicebus",
                     source=(rel(path), line_of(text, text.index(f'"Name": "{tname}"'))))
        for sub in topic.get("Subscriptions", []):
            filters = []
            for rule in sub.get("Rules", []):
                props = rule.get("Properties", {})
                if props.get("FilterType") == "Sql":
                    expr = props.get("SqlFilter", {}).get("SqlExpression", "")
                    if expr.strip() != "1=1":
                        filters.append(expr)
                elif props.get("FilterType") == "Correlation":
                    cf = props.get("CorrelationFilter", {}).get("Properties", {})
                    filters.append(" and ".join(f"{k}='{v}'" for k, v in sorted(cf.items())))
            sname = sub["Name"]
            sid = g.node(f"sub:{tname.lower()}/{sname}", sname, "subscription", system="infra",
                         parent="svc:servicebus", topic=tname, filter=" OR ".join(filters) or "all messages")
            g.edge(tid, sid, "delivers", " OR ".join(filters) or None)


def sub_ids(g: Graph, topic: str | None, sub: str) -> list[str]:
    return [n for n in g.nodes if n.startswith("sub:") and n.split("/", 1)[1].lower() == sub.lower()
            and (topic is None or n.split(":", 1)[1].split("/")[0] == topic.lower())]


def ensure_topic(g: Graph, name: str, evidence: str) -> str:
    tid = f"topic:{name.lower()}"
    if tid not in g.nodes:
        g.node(tid, name, "topic", system="infra", parent="svc:servicebus", missing_in_emulator=True)
        g.nodes[tid].setdefault("notes", []).append(
            f"Used in code ({evidence}) but not declared in config/servicebus/Config.json.")
    return tid


# ---------------------------------------------------------------------------------------------- srp

FUNC_ATTR = re.compile(r'\[Function\("([^"]+)"\)\]')


def csharp_summary(lines: list[str], idx: int) -> str | None:
    i = idx - 1
    out = []
    while i >= 0 and (lines[i].strip().startswith("///") or lines[i].strip().startswith("[")):
        s = lines[i].strip()
        if s.startswith("///"):
            out.append(s[3:].strip())
        i -= 1
    text = " ".join(reversed(out))
    m = re.search(r"<summary>(.*?)</summary>", text)
    text = m.group(1) if m else ""
    text = re.sub(r"<[^>]+>", "", text).strip()
    return text or None


def extract_srp(g: Graph, facts: dict) -> None:
    base = ROOT / "repos/srp"
    projects = sorted(p.parent for p in base.glob("*/*.csproj") if "Test" not in p.stem)
    running = facts["srp_projects"]
    for proj in projects:
        name = proj.name
        if name == "PipelineCommon":
            continue  # shared library, not a deployable
        owner = running.get(name)
        if owner is None:
            # Deployed in production, not part of the local stack. Console utilities are skipped.
            sources = "".join(cs.read_text(errors="replace") for cs in proj.rglob("*.cs"))
            if not FUNC_ATTR.search(sources) and not re.search(r'Subscription(?:Name)?\s*=\s*"', sources):
                continue
            owner = g.node(f"svc:{name}", name, "service", system="srp", parent="sys:srp",
                           source=(rel(proj), 1), inactive=True, project=name)
            for cs in sorted(proj.rglob("*.cs")):
                text = cs.read_text(errors="replace")
                for m in re.finditer(r'(?:Subscription|SubscriptionName)\s*=\s*"([^"]+)"', text):
                    for sid in sub_ids(g, None, m.group(1)):
                        g.edge(sid, owner, "triggers", None, evidence=f"{rel(cs)}:{line_of(text, m.start())}")
            continue
        g.nodes[owner]["attrs"]["source_dir"] = rel(proj)
        for cs in sorted(proj.glob("*.cs")):
            text = cs.read_text(errors="replace")
            lines = text.splitlines()
            funcs = [(line_of(text, m.start()), m.group(1)) for m in FUNC_ATTR.finditer(text)]
            for i, (ln, fname) in enumerate(funcs):
                end_line = funcs[i + 1][0] - 1 if i + 1 < len(funcs) else len(lines)
                window = "\n".join(lines[ln - 1:min(ln + 14, end_line)])
                body = "\n".join(lines[ln - 1:end_line])
                trig = {}
                if m := re.search(r'HttpTrigger\(([^)]*)\)', window):
                    args = m.group(1)
                    methods = re.findall(r'"(get|post|put|delete|patch)"', args, re.I)
                    route = re.search(r'Route\s*=\s*"([^"]+)"', args)
                    trig = {"trigger": "http", "methods": [x.upper() for x in methods],
                            "route": "/" + (route.group(1) if route else f"api/{fname}")}
                elif m := re.search(r'ServiceBusTrigger\(\s*"([^"]+)"\s*,\s*"([^"]+)"', window):
                    trig = {"trigger": "servicebus", "topic": m.group(1), "subscription": m.group(2)}
                elif m := re.search(r'TimerTrigger\("([^"]+)"', window):
                    trig = {"trigger": "timer", "schedule": m.group(1)}
                fid = g.node(f"fn:srp/{fname}", fname, "function", system="srp", parent=owner,
                             desc=csharp_summary(lines, ln - 1), source=(rel(cs), ln), **trig)
                ev = f"{rel(cs)}:{ln}"
                if trig.get("trigger") == "servicebus":
                    tid = ensure_topic(g, trig["topic"], ev)
                    subs = sub_ids(g, trig["topic"], trig["subscription"])
                    if not subs:
                        sid = g.node(f"sub:{trig['topic'].lower()}/{trig['subscription']}", trig["subscription"],
                                     "subscription", system="infra", parent="svc:servicebus", topic=trig["topic"])
                        g.nodes[sid].setdefault("notes", []).append("Not declared in the emulator config.")
                        g.edge(tid, sid, "delivers")
                        subs = [sid]
                    for sid in subs:
                        g.edge(sid, fid, "triggers", None, evidence=ev)
                for m in re.finditer(r'(?:ServiceBusOutput|CreateSender)\("([^"]+)"', body):
                    out_ln = ln + body.count("\n", 0, m.start())
                    tid = ensure_topic(g, m.group(1), f"{rel(cs)}:{out_ln}")
                    g.edge(fid, tid, "publishes", None, evidence=f"{rel(cs)}:{out_ln}")
                for m in re.finditer(r'new (\w+Renderer)\(\)', body):
                    cls = m.group(1)
                    defs = sorted(base.rglob(f"{cls}.cs"))
                    src = (rel(defs[0]), 1) if defs else None
                    rid = f"renderer:{cls}"
                    if rid in g.nodes and g.nodes[rid].get("parent") != owner:
                        owner_for_renderer = "sys:srp"
                    else:
                        owner_for_renderer = owner
                    g.node(rid, cls, "module", system="srp", source=src)
                    g.nodes[rid]["parent"] = owner_for_renderer
                    g.edge(fid, rid, "calls", "renders with",
                           evidence=f"{rel(cs)}:{ln + body.count(chr(10), 0, m.start())}")


# -------------------------------------------------------------------------------------- languageapi

def ts_block(text: str, start: int) -> str:
    """Text from `start` to the matching ')' of the first '(' after it."""
    i = text.index("(", start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    return text[start:]


def prod_string(expr: str) -> str | None:
    """`process.env.X == "DEV" ? "a" : "b"` -> 'b'; `"a"` -> 'a'."""
    lits = re.findall(r'"([^"]*)"', expr)
    return lits[-1] if lits else None


def route_name(text: str) -> str | None:
    """The `name` of a languageapi route module, which is either a literal or the file's tableName const."""
    m = re.search(r'externalRouteType = \{\s*name: (?:"([^"]+)"|(\w+))', text)
    if not m:
        return None
    if m.group(1):
        return m.group(1)
    const = re.search(rf'const {m.group(2)} = "([^"]+)"', text)
    return const.group(1) if const else None


def env_edges(g: Graph, fid: str, text: str, env: dict, evidence: str) -> None:
    """Edges from a function to services it reaches through `process.env.X` URLs."""
    for key in sorted(set(re.findall(r'process\.env\.(\w+)', text))):
        for host in URL_HOST.findall(env.get(key, "")):
            g.edge(fid, f"svc:{host}", "uses", None, evidence=f"{evidence} reads {key}={env[key]}")


def extract_languageapi(g: Graph, facts: dict) -> None:
    base = ROOT / "repos/languageapi"
    src = base / "controller/src"
    ctrl = "svc:lapi-controller"
    g.nodes[ctrl]["attrs"]["source_dir"] = rel(base / "controller")

    # Drizzle schema: export name -> table, plus FKs.
    schema_path = src / "db/schema/schema.ts"
    schema = schema_path.read_text()
    exports: dict[str, str] = {}
    matches = [m for m in re.finditer(r'^export const (\w+) = pgTable\(\s*"(\w+)"', schema, re.M)]
    for i, m in enumerate(matches):
        export, table = m.groups()
        exports[export] = table
        g.node(f"table:{table}", table, "table", system="languageapi", parent="svc:lapi-postgres",
               source=(rel(schema_path), line_of(schema, m.start())), drizzle_export=export)
    for i, m in enumerate(matches):
        block = schema[m.start():matches[i + 1].start() if i + 1 < len(matches) else len(schema)]
        for ref in sorted(set(re.findall(r'references\(\(\)\s*=>\s*(\w+)\.', block))):
            if ref in exports and exports[ref] != m.group(2):
                g.edge(f"table:{m.group(2)}", f"table:{exports[ref]}", "fk", "foreign key",
                       evidence=rel(schema_path))

    def table_refs(text: str) -> set[str]:
        refs = set(re.findall(r'\b(?:schema|dbSchema)\.(\w+)', text))
        refs |= set(re.findall(r'insertSchemas\.(\w+)', text))
        refs |= {m for m in re.findall(r'\{\s*([\w\s,]+)\s*\}\s*=\s*dbSchema', text) for m in m.split(",")}
        return {exports[r.strip()] for r in refs if r.strip() in exports}

    # HTTP routes (registered in functions/app.ts from routes/index.ts).
    for ts in sorted((src / "routes").glob("*.ts")):
        text = ts.read_text()
        m = re.search(r'export const (\w+): externalRouteType = \{\s*name: (?:"([^"]+)"|(\w+))', text)
        if not m:
            continue
        name = m.group(2) or route_name(text)
        methods = re.search(r'methods:\s*\[([^\]]*)\]', text[m.start():])
        rid = g.node(f"route:lapi/{name}", f"/api/{name}", "function", system="languageapi", parent=ctrl,
                     source=(rel(ts), line_of(text, m.start())), trigger="http", route=f"/api/{name}",
                     methods=re.findall(r'"(\w+)"', methods.group(1)) if methods else None)
        for table in sorted(table_refs(text)):
            g.edge(rid, f"table:{table}", "reads_writes", None, evidence=rel(ts))

    # Service Bus and timer functions.
    for ts in sorted((src / "functions").glob("*.ts")):
        text = ts.read_text()
        for m in re.finditer(r'app\.(serviceBusTopic|timer)\(\s*"([^"]+)"', text):
            kind, name = m.groups()
            block = ts_block(text, m.start())
            ln = line_of(text, m.start())
            ev = f"{rel(ts)}:{ln}"
            if kind == "serviceBusTopic":
                topic = prod_string(re.search(r'topicName:\s*([^,\n]+)', block).group(1))
                sub = prod_string(re.search(r'subscriptionName:\s*([\s\S]+?),\n\s*\w+:', block).group(1))
                fid = g.node(f"fn:lapi/{name}", name, "function", system="languageapi", parent=ctrl,
                             source=(rel(ts), ln), trigger="servicebus", topic=topic, subscription=sub)
                tid = ensure_topic(g, topic, ev)
                subs = sub_ids(g, topic, sub)
                if not subs:
                    sid = g.node(f"sub:{topic.lower()}/{sub}", sub, "subscription", system="infra",
                                 parent="svc:servicebus", topic=topic)
                    g.nodes[sid].setdefault("notes", []).append("Not declared in the emulator config.")
                    g.edge(tid, sid, "delivers")
                    subs = [sid]
                for sid in subs:
                    g.edge(sid, fid, "triggers", None, evidence=ev)
            else:
                sched = re.search(r'schedule:\s*"([^"]+)"', block)
                fid = g.node(f"fn:lapi/{name}", name, "function", system="languageapi", parent=ctrl,
                             source=(rel(ts), ln), trigger="timer", schedule=sched.group(1).strip() if sched else None)
            for table in sorted(table_refs(text)):
                g.edge(fid, f"table:{table}", "reads_writes", None, evidence=rel(ts))
            env_edges(g, fid, text, facts["env"].get("lapi-controller", {}), rel(ts))
            for route in sorted(set(re.findall(r'from "\.\./routes/(\w+)"', text))):
                route_file = src / "routes" / f"{route}.ts"
                if route_file.exists() and (rt := route_name(route_file.read_text())):
                    g.edge(fid, f"route:lapi/{rt}", "calls", "reuses handler", evidence=rel(ts))

    # Migrations: which views actually get created (Hasura tracks some that aren't).
    created_views = set()
    for sql in (base / "controller/drizzle").glob("*.sql"):
        created_views |= {v.lower() for v in re.findall(r'CREATE (?:OR REPLACE )?(?:MATERIALIZED )?VIEW\s+'
                                                         r'(?:IF NOT EXISTS\s+)?"?(?:public"?\."?)?(\w+)',
                                                         sql.read_text(), re.I)}

    # Hasura: tracked tables/views and REST endpoints.
    meta = base / "hasura/metadata"
    hasura = "svc:lapi-hasura"
    tables_dir = meta / "databases/languageapi/tables"
    for inc in load_yaml(tables_dir / "tables.yaml"):
        tf = tables_dir / inc.split()[-1]
        t = load_yaml(tf)["table"]["name"]
        tid = f"table:{t}"
        if tid not in g.nodes:
            is_view = t.startswith("vw_")
            g.node(tid, t, "table", system="languageapi", parent="svc:lapi-postgres", source=(rel(tf), 1),
                   view=is_view or None, drizzle=False)
            note = ("View is tracked by Hasura but no migration creates it, so Hasura marks it inconsistent."
                    if is_view and t not in created_views else
                    "Created by a migration (raw SQL), not declared in the Drizzle schema."
                    if is_view else "Tracked by Hasura; not declared in the Drizzle schema (created by raw SQL "
                                    "migrations or a later rename).")
            g.nodes[tid].setdefault("notes", []).append(note)
            if is_view and t not in created_views:
                g.nodes[tid]["attrs"]["known_gap"] = True
        g.edge(hasura, tid, "exposes", "GraphQL", evidence=rel(tf))
    rest = meta / "rest_endpoints.yaml"
    for ep in load_yaml(rest) or []:
        q = ep["definition"]["query"]["query_name"]
        eid = g.node(f"rest:hasura/{ep['url']}", f"/api/rest/{ep['url']}", "endpoint", system="languageapi",
                     parent=hasura, source=(rel(rest), 1), methods=ep.get("methods"), query=q)
        g.edge(hasura, eid, "exposes", "REST")
        if q == "Consolidated Repos":
            g.edge(eid, "table:vw_consolidated_repos", "reads_writes", "reads", evidence=rel(rest))
        elif q == "langnames":
            g.edge(eid, "table:vw_langnames", "reads_writes", "reads", evidence=rel(rest))


# --------------------------------------------------------------------------------------------- fdb

def extract_fdb(g: Graph) -> None:
    pkg = ROOT / "tools/fdb"
    for py in sorted(pkg.glob("*.py")):
        if py.name == "__init__.py":
            continue
        text = py.read_text()
        doc = re.match(r'\s*"""(.*?)(?:\n\n|""")', text, re.S)
        mod = py.stem
        mid = g.node(f"py:fdb.{mod}", f"fdb.{mod}", "module", system="fdb", parent="sys:fdb",
                     desc=" ".join(doc.group(1).split()) if doc else None, source=(rel(py), 1))
        for m in re.finditer(r'^from \.(\w*) import ([\w, ]+)', text, re.M):
            target = m.group(1) or None
            names = [n.strip() for n in m.group(2).split(",")]
            for t in ([target] if target else names):
                if (pkg / f"{t}.py").exists():
                    g.edge(mid, f"py:fdb.{t}", "imports", None if target is None else ", ".join(names),
                           evidence=f"{rel(py)}:{line_of(text, m.start())}")
        if mod == "server":
            handlers = {}
            for hm in re.finditer(r'^    def (\w+)\(self, cfg[^)]*\):\n((?:        .*\n|\n)+)', text, re.M):
                handlers[hm.group(1)] = sorted(set(re.findall(r'core\.([a-z_]\w*)\(', hm.group(2))))
            for m in re.finditer(r'\("(GET|POST|PUT|DELETE)", re\.compile\(r?f?"\^(.+?)\$"\), "(\w+)"\)', text):
                method, pattern, handler = m.groups()
                path = re.sub(r'\{_REPO\}', '{owner}/{repo}', pattern)
                path = re.sub(r'\(\?P<(\w+)>[^)]*\)', r'{\1}', path)
                eid = g.node(f"ep:fdb/{method} {path}", f"{method} {path}", "endpoint", system="fdb",
                             parent="svc:fdb-api", source=(rel(py), line_of(text, m.start())))
                g.edge("svc:fdb-api", eid, "exposes", "HTTP")
                for fn in handlers.get(handler, []):
                    g.edge(eid, "py:fdb.core", "calls", f"core.{fn}()", evidence=rel(py))
    # Entry points.
    cli = ROOT / "tools/cli/fdb"
    cid = g.node("script:tools/cli/fdb", "tools/cli/fdb", "script", system="fdb", parent="sys:fdb",
                 desc=re.search(r'"""(.*?)"""', cli.read_text(), re.S).group(1).strip(), source=(rel(cli), 1))
    if m := re.search(r'from fdb\.(\w+) import', cli.read_text()):
        g.edge(cid, f"py:fdb.{m.group(1)}", "calls", "main()", evidence=rel(cli))
    dockerfile = ROOT / "tools/http/Dockerfile"
    if m := re.search(r'"-m",\s*"fdb\.(\w+)"', dockerfile.read_text()):
        g.edge("svc:fdb-api", f"py:fdb.{m.group(1)}", "calls", f"python -m fdb.{m.group(1)}",
               evidence=rel(dockerfile))
    test = ROOT / "tools/test.sh"
    script_node(g, test, "fdb")


# ---------------------------------------------------------------------------------------- umbrella

# What a script reaches when it uses one of these: shell variables holding a service URL/port (used as $X or
# ${X}), and commands that talk to a service.
SCRIPT_VARS = {"WACS_URL": "svc:wacs", "LAPI_URL": "svc:lapi-controller", "HASURA_URL": "svc:lapi-hasura",
               "BLOB_URL": "svc:azurite", "BLOB": "svc:azurite", "SRP_WEBHOOK_PORT": "svc:srp-webhook",
               "SERVICEBUS_MGMT_PORT": "svc:servicebus", "API": "svc:fdb-api"}
SCRIPT_COMMANDS = {"gitea_api": "svc:wacs", "tools/cli/fdb": "script:tools/cli/fdb"}


def script_node(g: Graph, path: Path, system: str = "umbrella") -> str:
    text = path.read_text()
    lines = text.splitlines()
    head = []
    for ln in lines[1:]:
        if not ln.startswith("#"):
            break
        head.append(ln.lstrip("#").strip())
    sid = g.node(f"script:{rel(path)}", rel(path), "script", system=system, parent=f"sys:{system}",
                 desc=" ".join(head) or None, source=(rel(path), 1))
    for m in re.finditer(r'^\s*source (\S+)', text, re.M):
        lib = ROOT / m.group(1)
        if lib.exists():
            g.edge(sid, script_node(g, lib), "calls", "source", evidence=f"{rel(path)}:{line_of(text, m.start())}")
    for m in re.finditer(r'compose (?:exec|run)(?: -\S+| --\S+)* ([a-z][\w-]*)', text):
        g.edge(sid, f"svc:{m.group(1)}", "calls", "docker compose exec/run",
               evidence=f"{rel(path)}:{line_of(text, m.start())}")
    uses = [(rf'\$\{{?{v}\b', t) for v, t in SCRIPT_VARS.items()]
    uses += [(rf'(?<![\w/]){re.escape(c)}(?![\w(=])', t) for c, t in SCRIPT_COMMANDS.items()]
    for pattern, target in uses:
        for i, line in enumerate(lines, 1):
            code = line.split(" #")[0]
            if line.lstrip().startswith("#") or not re.search(pattern, code):
                continue
            g.edge(sid, target, "calls", None, evidence=f"{rel(path)}:{i}")
            break
    return sid


def extract_umbrella(g: Graph) -> None:
    for p in sorted((ROOT / "scripts").glob("*.sh")) + [ROOT / "seed/seed.sh"]:
        script_node(g, p)
    seed = (ROOT / "seed/seed.sh").read_text()
    if m := re.search(r'for c in ([^;]+); do az storage container create', seed):
        for c in re.findall(r'"?\\?(\$?\w+)"?', m.group(1)):
            bid = g.node(f"blob:{c}", c, "blob", system="infra", parent="svc:azurite")
            g.edge("script:seed/seed.sh", bid, "calls", "creates container", evidence="seed/seed.sh")
    sql = ROOT / "seed/sql/wacs-token-trigger.sql"
    if sql.exists() and sql.name in seed:
        fid = file_node(g, sql, "wacs")
        g.edge("script:seed/seed.sh", fid, "calls", "applies to wacs-db")
    for d in ("seed/fixtures", "seed/languageapi", "seed/templates"):
        if (ROOT / d).name in seed or d in seed:
            g.edge("script:seed/seed.sh", file_node(g, ROOT / d, "umbrella"), "calls", "reads")

    mk = ROOT / "Makefile"
    text = mk.read_text()
    lines = text.splitlines()
    targets = {}
    for i, ln in enumerate(lines):
        m = re.match(r'^([a-z][\w-]*):(?!=)\s*([^#]*?)\s*(?:## (.*))?$', ln)
        if not m:
            continue
        name, deps, help_ = m.groups()
        recipe = []
        for body in lines[i + 1:]:
            if not body.startswith("\t"):
                break
            recipe.append(body.strip())
        targets[name] = (i + 1, deps.split(), help_, recipe)
    compose_id = g.node("file:compose.yaml", "compose.yaml", "file", system="umbrella", parent="sys:umbrella",
                        source=("compose.yaml", 1),
                        desc="Root compose file. It `include:`s compose/{infra,wacs,languageapi,srp,tools}.yaml, "
                             "so every service in the graph comes from here.")
    for name, (ln, deps, help_, recipe) in targets.items():
        if help_ is None and name != "prepare":
            continue
        tid = g.node(f"make:{name}", f"make {name}", "make", system="umbrella", parent="sys:umbrella",
                     desc=help_, source=("Makefile", ln), recipe=recipe or None)
        for d in deps:
            if d in targets and (targets[d][2] is not None or d == "prepare"):
                g.edge(tid, f"make:{d}", "runs", f"step {deps.index(d) + 1}" if len(deps) > 1 else "first")
        joined = "\n".join(recipe)
        for m in re.finditer(r'\./(\S+\.sh|tools/cli/fdb)', joined):
            target = f"script:{m.group(1)}"
            if target in g.nodes:
                g.edge(tid, target, "runs")
        if m := re.search(r'\$\(COMPOSE\)(?: --profile \w+)? (\w+)', joined):
            g.edge(tid, compose_id, "runs", f"docker compose {m.group(1)}")
        if "git submodule" in joined:
            g.edge(tid, file_node(g, ROOT / ".gitmodules", "umbrella"), "runs", "git submodule")


# ----------------------------------------------------------------------------------------- curated

def apply_knowledge(g: Graph, k: dict) -> list[dict]:
    errors = []
    for sid, s in k["systems"].items():
        g.node(f"sys:{sid}", s["label"], "system", system=sid, desc=s.get("desc"))
        g.nodes[f"sys:{sid}"]["attrs"].update({k2: v for k2, v in s.items() if k2 not in ("label", "desc")})
    for nid, info in k.get("nodes", {}).items():
        if nid not in g.nodes:
            errors.append(f"knowledge.json: node '{nid}' does not exist in the extracted graph")
            continue
        n = g.nodes[nid]
        if "desc" in info:
            n["desc"] = info["desc"]
        for key in ("links", "tips"):
            if key in info:
                n[key] = info[key]
        if "notes" in info:
            n.setdefault("notes", []).extend(info["notes"])
        if "label" in info:
            n["label"] = info["label"]
    for e in k.get("edges", []):
        for end in (e["from"], e["to"]):
            if end not in g.nodes:
                errors.append(f"knowledge.json: edge endpoint '{end}' does not exist")
        if e["from"] in g.nodes and e["to"] in g.nodes:
            g.edge(e["from"], e["to"], e["kind"], e.get("label"), evidence=e.get("why"), origin="curated")
    for tour in k.get("tours", []):
        for kind in tour.get("edge_kinds", []):
            if kind not in k["edge_kinds"]:
                errors.append(f"knowledge.json: tour '{tour['id']}' names unknown edge kind '{kind}'")
        for step in tour["steps"]:
            for nid in step.get("nodes", []) + ([step["focus"]] if "focus" in step else []):
                if nid not in g.nodes:
                    errors.append(f"knowledge.json: tour '{tour['id']}' names unknown node '{nid}'")
    if errors:
        raise BuildError("\n".join(errors))
    return k.get("tours", [])


# ------------------------------------------------------------------------------------------ output

def submodules() -> list[dict]:
    cp = configparser.ConfigParser()
    cp.read(ROOT / ".gitmodules")
    out = []
    for section in cp.sections():
        path = cp[section]["path"]
        url = cp[section]["url"].removesuffix(".git")
        try:
            sha = subprocess.run(["git", "-C", str(ROOT / path), "rev-parse", "HEAD"], capture_output=True,
                                 text=True, check=True).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            sha = None
        out.append({"path": path, "url": url, "sha": sha, "branch": cp[section].get("branch")})
    return out


def source_url(path: str, line: int, subs: list[dict], repo_url: str) -> str:
    for s in subs:
        if path == s["path"] or path.startswith(s["path"] + "/"):
            inner = path[len(s["path"]):].lstrip("/")
            kind = "tree" if not inner or (ROOT / path).is_dir() else "blob"
            return f"{s['url']}/{kind}/{s['sha']}/{inner}" + (f"#L{line}" if line > 1 and kind == "blob" else "")
    kind = "tree" if (ROOT / path).is_dir() else "blob"
    return f"{repo_url}/{kind}/main/{path}" + (f"#L{line}" if line > 1 and kind == "blob" else "")


def auto_desc(g: Graph, n: dict) -> str | None:
    """A plain sentence from extracted facts, for nodes knowledge.json doesn't describe."""
    a = n["attrs"]
    label = lambda nid: g.nodes[nid]["label"]  # noqa: E731
    outs = defaultdict(list)
    ins = defaultdict(list)
    for e in g.edges.values():
        if e["source"] == n["id"]:
            outs[e["kind"]].append(label(e["target"]))
        if e["target"] == n["id"]:
            ins[e["kind"]].append(label(e["source"]))
    join = lambda xs: ", ".join(f"`{x}`" for x in sorted(set(xs)))  # noqa: E731
    kind = n["kind"]
    if kind == "subscription":
        who = join(outs["triggers"]) or "nothing that runs locally"
        return f"Copy of the `{a.get('topic')}` topic. Receives {a.get('filter')}; consumed by {who}."
    if kind == "topic":
        pub = join(ins["publishes"]) or "nothing that runs locally"
        return f"Service Bus topic. Published by {pub}. Subscriptions: {join(outs['delivers']) or 'none'}."
    if kind == "table":
        what = "view" if a.get("view") else "table"
        users = join(ins["reads_writes"])
        return f"Postgres {what} `{n['label']}`." + (f" Touched by {users}." if users else "") + \
            (" Exposed through Hasura." if ins["exposes"] else "")
    if kind == "function":
        t = a.get("trigger")
        if t == "http":
            return f"HTTP {'/'.join(a.get('methods') or ['ANY'])} {a.get('route')}."
        if t == "servicebus":
            return f"Runs for each message on `{a.get('topic')}` (subscription `{a.get('subscription')}`)."
        if t == "timer":
            return f"Timer function, schedule `{a.get('schedule')}` (NCRONTAB, seconds first)."
    if kind == "volume":
        return f"Named docker volume, mounted at `{a.get('mount')}` by {join(ins['stores'])}. `make nuke` deletes it."
    if kind == "endpoint":
        return f"`{n['label']}` served by {join(ins['exposes'])}." + (f" Calls {', '.join(outs['calls'])}." if outs["calls"] else "")
    if kind == "blob":
        return "Blob container in Azurite."
    if kind == "module" and n["id"].startswith("renderer:"):
        return f"Renderer used by {join(ins['calls'])}."
    if kind == "file" and a.get("is_dir"):
        return f"Directory `{n['label']}`" + (f", used by {join(ins['mounts'] + ins['calls'])}." if ins else ".")
    if kind == "service":
        return f"Compose service from image `{a['image']}`." if a.get("image") else None
    return None


def finalize(g: Graph, tours: list[dict], k: dict) -> dict:
    subs = submodules()
    for n in g.nodes.values():
        if not n.get("desc") and (d := auto_desc(g, n)):
            n["desc"] = d
            n["desc_auto"] = True
    for n in g.nodes.values():
        n["sources"] = [{"path": p, "line": ln, "url": source_url(p, ln, subs, k["repo_url"])}
                        for p, ln in n["sources"]]
        if n["kind"] != "system" and "system" not in n:
            n["system"] = "umbrella"
    # Drop edges whose endpoints never materialised (e.g. a service a script mentions that isn't defined).
    edges = []
    dangling = []
    for e in g.edges.values():
        if e["source"] in g.nodes and e["target"] in g.nodes:
            edges.append(e)
        else:
            dangling.append(f"{e['source']} -> {e['target']} ({e['kind']})")
    if dangling:
        raise BuildError("edges point at unknown nodes:\n  " + "\n  ".join(sorted(dangling)))
    nodes = sorted(g.nodes.values(), key=lambda n: n["id"])
    edges.sort(key=lambda e: (e["source"], e["target"], e["kind"]))
    counts = defaultdict(int)
    for n in nodes:
        counts[n["kind"]] += 1
    return {
        "meta": {
            "title": k.get("title", "Architecture graph"),
            "repo_url": k["repo_url"],
            "generator": "tools/graph/build.py",
            "submodules": subs,
            "counts": {"nodes": len(nodes), "edges": len(edges), **dict(sorted(counts.items()))},
        },
        "edge_kinds": k["edge_kinds"],
        "node_kinds": k["node_kinds"],
        "nodes": nodes,
        "edges": edges,
        "tours": tours,
    }


def render_report(data: dict) -> str:
    nodes = {n["id"]: n for n in data["nodes"]}
    degree = defaultdict(int)
    for e in data["edges"]:
        if e["kind"] in ("depends_on",):
            continue
        degree[e["source"]] += 1
        degree[e["target"]] += 1
    sysname = {n["system"]: n["label"] for n in data["nodes"] if n["kind"] == "system"}
    m = data["meta"]
    out = [
        "# Architecture graph report",
        "",
        "Generated by `make graph` (`tools/graph/build.py`); do not edit by hand. Open "
        "[`index.html`](index.html) in a browser for the interactive version.",
        "",
        f"**{m['counts']['nodes']} nodes, {m['counts']['edges']} edges.** "
        + ", ".join(f"{v} {k}" for k, v in m["counts"].items() if k not in ("nodes", "edges")) + ".",
        "",
        "Pinned submodules:",
        "",
    ]
    for s in m["submodules"]:
        out.append(f"- `{s['path']}` @ [`{(s['sha'] or 'not checked out')[:10]}`]({s['url']}/tree/{s['sha']}) "
                   f"(tracks `{s['branch']}`)")
    out += ["", "## Most connected", "",
            "Hubs: start reading here. Startup-order (`depends_on`) edges are not counted.", "",
            "| Node | Kind | System | Connections |", "|---|---|---|---|"]
    for nid, d in sorted(degree.items(), key=lambda x: (-x[1], x[0]))[:12]:
        n = nodes[nid]
        out.append(f"| `{n['label']}` | {n['kind']} | {sysname.get(n['system'], n['system'])} | {d} |")
    out += ["", "## Seams between systems", "",
            "Edges that cross from one system to another. These are the contracts a change can break.", "",
            "| From | | To | Evidence |", "|---|---|---|---|"]
    seams = []
    for e in data["edges"]:
        a, b = nodes[e["source"]], nodes[e["target"]]
        if a["system"] != b["system"] and e["kind"] not in ("depends_on", "mounts", "built_by", "stores") \
                and "umbrella" not in (a["system"], b["system"]) and a["kind"] != "system":
            label = e["kind"].replace("_", " ") + (f" ({', '.join(e['labels'])})" if e["labels"] else "")
            ev = (e["evidence"] or [""])[0].replace("|", "\\|")
            seams.append(f"| `{a['label']}` ({sysname[a['system']]}) | {label} | `{b['label']}` "
                         f"({sysname[b['system']]}) | {ev[:90]} |")
    out += sorted(set(seams))
    out += ["", "## Known gaps and notes", ""]
    for n in data["nodes"]:
        for note in n.get("notes", []):
            out.append(f"- **`{n['label']}`** ({n['kind']}): {note}")
    inactive = [n for n in data["nodes"] if n["attrs"].get("inactive")]
    if inactive:
        out += ["", "## Deployed in production, not run locally", ""]
        out += [f"- `{n['label']}`: {n.get('desc') or ''}".rstrip(": ") for n in inactive]
    out += ["", "## Tours", ""]
    for t in data["tours"]:
        out.append(f"### {t['title']}")
        out.append("")
        out.append(t.get("intro", ""))
        out.append("")
        for i, s in enumerate(t["steps"], 1):
            out.append(f"{i}. **{s['title']}** {s['text']}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def build() -> dict[Path, str]:
    missing = [p for p in ("repos/wacs/.env", "repos/srp/PipelineCommon", "repos/languageapi/controller")
               if not (ROOT / p).exists()]
    if missing:
        raise BuildError("submodules are not checked out (missing " + ", ".join(missing) + "); run `make init`")
    k = json.loads((HERE / "knowledge.json").read_text())
    g = Graph()
    for sid, s in k["systems"].items():
        g.node(f"sys:{sid}", s["label"], "system", system=sid)
    facts = extract_compose(g)
    extract_servicebus(g)
    extract_srp(g, facts)
    extract_languageapi(g, facts)
    extract_fdb(g)
    extract_umbrella(g)
    tours = apply_knowledge(g, k)
    data = finalize(g, tours, k)
    template = (HERE / "viewer.html").read_text()
    payload = json.dumps(data, indent=None, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    html = template.replace("/*__GRAPH_DATA__*/null", payload)
    return {
        OUT / "graph.json": json.dumps(data, indent=1, ensure_ascii=False) + "\n",
        OUT / "index.html": html,
        OUT / "REPORT.md": render_report(data),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the generated files are out of date")
    args = ap.parse_args(argv)
    try:
        outputs = build()
    except BuildError as e:
        print(f"graph build failed: {e}", file=sys.stderr)
        return 2
    stale = [p for p, content in outputs.items() if not p.exists() or p.read_text() != content]
    if args.check:
        if stale:
            print("architecture graph is out of date; run `make graph`:\n  "
                  + "\n  ".join(rel(p) for p in stale), file=sys.stderr)
            return 1
        print("architecture graph is up to date")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for p, content in outputs.items():
        p.write_text(content)
    counts = json.loads(outputs[OUT / "graph.json"])["meta"]["counts"]
    print(f"wrote {rel(OUT)}/ ({counts['nodes']} nodes, {counts['edges']} edges)"
          + ("" if stale else "; no changes"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
