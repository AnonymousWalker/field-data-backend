"""Turn the extracted graph into a GitDiagram-style Mermaid flowchart.

The full graph (graph.json) has every function, subscription and table. A readable system diagram
needs fewer, bigger boxes, so this module:

  * keeps the nodes worth drawing (containers, functions, topics, stores, the main tooling),
  * folds the rest into a drawn node: a table into its database, a subscription into the arrow from
    its topic to its consumer, look-alike functions into one group node (knowledge.json "diagram"),
  * lifts every edge onto the drawn nodes, drops duplicates and plumbing, and labels what flows,
  * draws each system as a subgraph, colours nodes by type, and links every node to its source.

The result is plain Mermaid text, so it also renders on GitHub (docs/architecture/README.md).
"""
from __future__ import annotations

import re
from collections import defaultdict

# Node type -> (classDef name, Mermaid shape template).
SHAPES = {
    "service": ("svc", '("{}")'),
    "function": ("fn", '["{}"]'),
    "endpoint": ("fn", '["{}"]'),
    "topic": ("topic", '{{{{"{}"}}}}'),
    "store": ("store", '[("{}")]'),
    "module": ("code", '["{}"]'),
    "script": ("tool", '[["{}"]]'),
    "make": ("tool", '[["{}"]]'),
    "file": ("tool", '>"{}"]'),
    "group": ("fn", '["{}"]'),
}

# Fill / stroke / text per class, for light and dark pages.
PALETTES = {
    "light": {
        "svc": ("#dbeafe", "#1d4ed8", "#0b1b3a"),
        "fn": ("#ede9fe", "#6d28d9", "#1e1033"),
        "topic": ("#ffedd5", "#c2410c", "#3a1606"),
        "store": ("#dcfce7", "#15803d", "#062612"),
        "code": ("#e0f2fe", "#0369a1", "#082032"),
        "tool": ("#f1f5f9", "#475569", "#0f172a"),
        "inactive": ("#f8fafc", "#94a3b8", "#64748b"),
        "subgraph": ("#fafaf9", "#d6d3d1", "#44403c"),
        "box": ("#f5f9ff", "#93c5fd", "#1e3a8a"),
        "line": "#57534e",
    },
    "dark": {
        "svc": ("#1e3a8a", "#93c5fd", "#eff6ff"),
        "fn": ("#3b2a6b", "#c4b5fd", "#f5f3ff"),
        "topic": ("#5a260c", "#fdba74", "#fff7ed"),
        "store": ("#14532d", "#86efac", "#f0fdf4"),
        "code": ("#0c3b55", "#7dd3fc", "#f0f9ff"),
        "tool": ("#27272a", "#a1a1aa", "#fafafa"),
        "inactive": ("#1c1c1e", "#71717a", "#a1a1aa"),
        "subgraph": ("#1f1f1d", "#44403c", "#d6d3d1"),
        "box": ("#18202e", "#3f5f9f", "#bfdbfe"),
        "line": "#a8a29e",
    },
}

DRAWN_KINDS = {"service", "function", "endpoint", "topic", "blob"}
STORE_IMAGES = re.compile(r"^(postgres|mariadb|valkey|redis|mcr\.microsoft\.com/mssql)")
EDGE_KINDS = {"publishes", "triggers", "calls", "uses", "reads_writes", "runs"}


def mid(graph_id: str) -> str:
    """Graph id -> Mermaid-safe id."""
    return re.sub(r"\W", "_", graph_id).strip("_")


def text(s: str) -> str:
    return (s or "").replace('"', "#quot;").replace("\n", " ")


def short_filter(expr: str | None) -> str | None:
    if not expr or expr == "all messages":
        return None
    values = re.findall(r"'([^']+)'", expr)
    if "Action='deleted'" in expr:
        return "repo deleted"
    return "/".join(v for v in values if v != "repository") if values else expr


def build(data: dict, spec: dict) -> dict:
    nodes = {n["id"]: n for n in data["nodes"]}
    children = defaultdict(list)
    for n in data["nodes"]:
        if n.get("parent"):
            children[n["parent"]].append(n["id"])

    # ---- which graph nodes are drawn, and as what
    drawn: dict[str, dict] = {}       # diagram id -> {label, sub, kind, parent, url, tip, inactive}
    owner: dict[str, str] = {}        # graph id -> diagram id it is drawn as / folded into
    groups = spec.get("groups", {})
    exclude = set(spec.get("exclude", []))
    include = set(spec.get("include", []))

    def first_url(n):
        return n["sources"][0]["url"] if n["sources"] else None

    for gid, g in groups.items():
        members = [i for i in nodes if re.search(g["match"], i)]
        if not members:
            raise ValueError(f"knowledge.json diagram group '{gid}' matches no nodes")
        did = mid("grp:" + gid)
        url = g.get("url") or (first_url(nodes[members[0]]) if len(members) == 1 else None)
        if not url:
            srcs = sorted((s for m in members for s in nodes[m]["sources"]), key=lambda s: (s["path"], s["line"]))
            files = {s["path"] for s in srcs}
            dirs = {s["url"].rsplit("/", 1)[0].replace("/blob/", "/tree/") for s in srcs}
            url = (srcs[0]["url"] if len(files) == 1 else sorted(dirs)[0] if len(dirs) == 1
                   else first_url(nodes[members[0]]))
        drawn[did] = {"label": g["label"], "sub": g.get("sub"), "kind": g.get("kind", "group"), "parent": g["parent"],
                      "url": url, "tip": g.get("desc") or ", ".join(nodes[m]["label"] for m in members),
                      "system": nodes[members[0]]["system"]}
        for m in members:
            owner[m] = did

    for n in data["nodes"]:
        gid = n["id"]
        if gid in owner or gid in exclude or n["kind"] == "system":
            continue
        if not (n["kind"] in DRAWN_KINDS or gid in include):
            continue
        if n["kind"] in ("function", "endpoint") and n.get("parent", "").startswith("svc:") and \
                nodes[n["parent"]]["attrs"].get("inactive"):
            continue
        kind = n["kind"]
        if kind == "blob" or (kind == "service" and STORE_IMAGES.match(n["attrs"].get("image") or "")):
            kind = "store"
        drawn[mid(gid)] = {"label": n["label"], "sub": subtitle(n), "kind": kind, "parent": n.get("parent"),
                           "url": first_url(n), "tip": n.get("desc"), "system": n["system"],
                           "inactive": bool(n["attrs"].get("inactive"))}
        owner[gid] = mid(gid)

    # Everything else folds into its nearest drawn ancestor (a table into its database, ...).
    # Subscriptions fold into their topic; the arrow topic -> consumer carries the subscription name.
    for gid, n in nodes.items():
        if gid in owner or n["kind"] == "system":
            continue
        if n["kind"] == "subscription":
            topic = next((e["source"] for e in data["edges"] if e["target"] == gid and e["kind"] == "delivers"), None)
            if topic in owner:
                owner[gid] = owner[topic]
            continue
        p = n.get("parent")
        while p and p not in owner:
            p = nodes[p].get("parent")
        if p and n["kind"] not in ("volume", "file"):
            owner[gid] = owner[p]

    # Drawn parents of drawn nodes become subgraphs; parents are only services (systems are subgraphs too).
    def drawn_parent(d):
        p = d["parent"]
        while p and not p.startswith("sys:") and p not in owner:
            p = nodes[p].get("parent")
        if p and not p.startswith("sys:") and owner.get(p) and owner[p] != d.get("_self"):
            return owner[p]
        return None

    for did, d in drawn.items():
        d["_self"] = did
        d["box"] = drawn_parent(d)
    containers = {d["box"] for d in drawn.values() if d["box"]}

    # ---- edges
    subs = {n["id"]: n for n in data["nodes"] if n["kind"] == "subscription"}
    raw = []
    for e in data["edges"]:
        if e["kind"] == "delivers":
            continue
        if e["kind"] == "triggers" and e["source"] in subs:
            sub = subs[e["source"]]
            filt = short_filter(sub["attrs"].get("filter"))
            label = sub["label"] + (f": {filt}" if filt else "")
            raw.append((e["source"], e["target"], "triggers", label, e))
            continue
        if e["kind"] not in EDGE_KINDS:
            continue
        if e["kind"] == "uses" and e["evidence"] and all(
                ev.startswith(("AzureWebJobsStorage=", "WebhookStorageConnectionString=")) for ev in e["evidence"]):
            continue  # Functions host plumbing, not part of the story
        label = e["labels"][0] if e["labels"] else data["edge_kinds"][e["kind"]]["label"]
        raw.append((e["source"], e["target"], e["kind"], label, e))

    def ancestors(did):
        out = []
        while did:
            out.append(did)
            did = drawn[did]["box"] if did in drawn else None
        return out

    lifted: dict[tuple, dict] = {}
    for s, t, kind, label, e in raw:
        a, b = owner.get(s), owner.get(t)
        if not a or not b or a == b or a in ancestors(b) or b in ancestors(a):
            continue
        key = (a, b)
        cur = lifted.setdefault(key, {"labels": [], "kinds": set(), "dashed": True})
        if label not in cur["labels"]:
            cur["labels"].append(label)
        cur["kinds"].add(kind)
        if kind != "uses":
            cur["dashed"] = False
    # Drop arrows that say less than another arrow already drawn:
    #  * a -> Box when a -> (something inside Box) exists,
    #  * a generic "connects to" between two boxes whose contents are already linked (either direction).
    hide_from = {owner[g] for g in spec.get("hide_edges_from", []) if g in owner}
    for (a, b), v in list(lifted.items()):
        others = [k for k in lifted if k != (a, b)]
        if a in hide_from and v["kinds"] == {"uses"}:
            del lifted[(a, b)]
        elif any(k[0] == a and b in ancestors(k[1]) for k in others):
            del lifted[(a, b)]
        elif v["kinds"] == {"uses"} and any(
                (a in ancestors(k[0]) and b in ancestors(k[1])) or
                (lifted[k]["kinds"] != {"uses"} and b in ancestors(k[0]) and a in ancestors(k[1])) for k in others):
            del lifted[(a, b)]
        elif v["kinds"] == {"uses"}:
            v["labels"] = ["connects to"]
    for v in lifted.values():
        if v["kinds"] != {"uses"}:
            v["labels"] = [lab for lab in v["labels"] if lab != "connects to"]
        if len(v["labels"]) > 2 or any(lab.endswith("()") for lab in v["labels"]):
            kinds = sorted(v["kinds"] - {"uses"})
            v["labels"] = [data["edge_kinds"][kinds[0]]["label"]] if len(kinds) == 1 else v["labels"][:1]
    # Some arrows read better in the direction the data flows ("wacs -> worker: repo zip" rather than
    # "worker -> wacs: downloads"), and it keeps the layout flowing downstream.
    for key, label in spec.get("flip", {}).items():
        a, b = key.split("->")
        if (a, b) not in lifted:
            raise ValueError(f"knowledge.json diagram flip: no arrow {key}")
        v = lifted.pop((a, b))
        v["labels"] = [label]
        lifted[(b, a)] = v
    for key, label in spec.get("edge_labels", {}).items():
        a, b = key.split("->")
        if (a, b) not in lifted:
            raise ValueError(f"knowledge.json diagram edge_labels: no arrow {key}")
        lifted[(a, b)]["labels"] = [label]

    # ---- emit
    def node_line(did, d, indent, small=True):
        cls, shape = SHAPES[d["kind"]]
        sub = text(d.get("sub") or "")
        label = text(d["label"]) + ((f"<br/><small>{sub}</small>" if small else f"<br/>{sub}") if sub else "")
        return f"{indent}{did}{shape.format(label)}:::{'inactive' if d.get('inactive') else cls}"

    systems = [n for n in data["nodes"] if n["kind"] == "system"]
    order = spec.get("system_order", [s["system"] for s in systems])
    sys_label = {s["system"]: s["label"] for s in systems}

    def emit(palette: str, clicks: bool, small: bool = True) -> str:
        pal = PALETTES[palette]
        lines = [f"flowchart {spec.get('direction', 'TD')}"]

        def emit_box(did, indent):
            d = drawn[did]
            lines.append(f'{indent}subgraph {did}["{text(d["label"])}"]')
            lines.append(f"{indent}  direction {spec.get('inner_direction', 'TB')}")
            for cid, c in sorted(drawn.items()):
                if c["box"] == did:
                    emit_box(cid, indent + "  ") if cid in containers else lines.append(node_line(cid, c, indent + "  ", small))
            lines.append(f"{indent}end")

        for sysname in order:
            members = sorted(did for did, d in drawn.items() if d["system"] == sysname and not d["box"])
            if not members:
                continue
            lines.append(f'  subgraph sys_{sysname}["{text(sys_label[sysname])}"]')
            for did in members:
                emit_box(did, "    ") if did in containers else lines.append(node_line(did, drawn[did], "    ", small))
            lines.append("  end")
        lines.append("")
        for (a, b), v in sorted(lifted.items()):
            label = "; ".join(v["labels"])
            if len(label) > 48:
                label = label[:46] + "…"
            arrow = "-.->" if v["dashed"] else "-->"
            lines.append(f'  {a} {arrow}|"{text(label)}"| {b}')
        lines.append("")
        if clicks:
            for did, d in sorted(drawn.items()):
                if d.get("url") and did not in containers:
                    tip = d.get("tip") or d["label"]
                    if len(tip) > 260:
                        tip = tip[:260].rsplit(" ", 1)[0] + "…"
                    tip = text(tip)
                    lines.append(f'  click {did} "{d["url"]}" "{tip}" _blank')
            lines.append("")
        for cls, val in pal.items():
            if cls in ("line", "subgraph", "box"):
                continue
            fill, stroke, color = val
            extra = ",stroke-dasharray:5 4" if cls == "inactive" else ""
            lines.append(f"  classDef {cls} fill:{fill},stroke:{stroke},color:{color},stroke-width:1.5px{extra}")
        sfill, sstroke, scolor = pal["subgraph"]
        for sysname in order:
            if any(d["system"] == sysname for d in drawn.values()):
                lines.append(f"  style sys_{sysname} fill:{sfill},stroke:{sstroke},color:{scolor}")
        for did in sorted(containers):
            fill, stroke, color = pal["box"]
            lines.append(f"  style {did} fill:{fill},stroke:{stroke},color:{color},stroke-width:1.5px")
        return "\n".join(lines) + "\n"

    # Which drawn element each graph node is shown as (used to highlight walkthrough steps).
    shown_as = {gid: did for gid, did in owner.items()}
    for gid, target in spec.get("aliases", {}).items():
        if target not in owner:
            raise ValueError(f"knowledge.json diagram alias target '{target}' is not drawn")
        shown_as.setdefault(gid, owner[target])
    return {
        "light": emit("light", True),
        "dark": emit("dark", True),
        "github": emit("light", False, small=False),
        "edges": [[a, b] for a, b in sorted(lifted)],
        "shown_as": shown_as,
        "containers": sorted(containers),
        "counts": {"nodes": len(drawn), "edges": len(lifted)},
    }


def subtitle(n: dict) -> str | None:
    a = n["attrs"]
    if n["kind"] == "function":
        if a.get("trigger") == "http":
            return f"HTTP {a.get('route')}"
        if a.get("trigger") == "servicebus":
            return "Service Bus trigger"
        if a.get("trigger") == "timer":
            return "timer"
    if n["kind"] == "service":
        if a.get("inactive"):
            return "production only"
        if a.get("project"):
            return ".NET Functions"
        img = a.get("image") or ""
        if img.startswith("fdb/"):
            return "built here"
        return img.split("/")[-1][:28] or None
    if n["kind"] == "topic":
        return "topic"
    if n["kind"] == "blob":
        return "blob container"
    return None
