"""fdb operations. Every function returns a plain dict, used as-is by the CLI (--json) and HTTP API."""
from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .clients import Blob, Gitea, Hasura, basic_auth, git
from .config import Config, assert_local_wacs

POLL_SECONDS = 3
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class UserError(ValueError):
    """Bad input (maps to HTTP 400 / CLI exit 2)."""


# --------------------------------------------------------------------------------------- helpers

def _clients(cfg: Config) -> tuple[Gitea, Blob, Hasura]:
    return (Gitea(cfg.wacs_url, cfg.admin_user, cfg.admin_password), Blob(cfg.blob_url), Hasura(cfg.hasura_url))


def split_repo(full: str) -> tuple[str, str]:
    parts = full.strip("/").split("/")
    if len(parts) != 2 or not all(_NAME_RE.match(p) for p in parts):
        raise UserError(f"expected <owner>/<repo>, got {full!r}")
    return parts[0], parts[1]


def owner_repo_from_url(url: str) -> tuple[str, str]:
    path = urlparse(url).path if "://" in url else url
    parts = [p for p in path.rstrip("/").split("/") if p]
    if len(parts) < 2:
        raise UserError(f"can't infer <owner>/<repo> from {url!r}; pass --as owner/repo")
    owner, repo = parts[-2], re.sub(r"\.git$", "", parts[-1])
    return split_repo(f"{owner}/{repo}")


def parse_time(value: str | None) -> datetime | None:
    """ISO timestamps from .NET (7 fractional digits, offset) and Postgres (naive UTC)."""
    if not value or value.startswith("0001-"):
        return None
    v = value.strip().replace("Z", "+00:00")
    v = re.sub(r"(\.\d{6})\d+", r"\1", v)
    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _default_branch(gitea: Gitea, owner: str, repo: str) -> str:
    info = gitea.call("GET", f"/repos/{Gitea.q(owner, repo)}", ok404=True)
    if info is None:
        raise UserError(f"{owner}/{repo} does not exist in the local WACS")
    return info.get("default_branch") or "master"


# ---------------------------------------------------------------------------------------- import

def import_repo(cfg: Config, url: str, as_repo: str | None = None) -> dict:
    """Mirror-clone any git repo locally, then push its default branch into the local WACS only."""
    gitea, _, _ = _clients(cfg)
    owner, repo = split_repo(as_repo) if as_repo else owner_repo_from_url(url)
    local = cfg.imports_dir / owner / f"{repo}.git"

    if (local / "HEAD").exists():
        git("fetch", "--prune", "origin", cwd=str(local))
        cloned = False
    else:
        local.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--mirror", url, str(local))
        cloned = True
    # The source is read-only from here on: any push to it fails.
    git("remote", "set-url", "--push", "origin", "DISABLED", cwd=str(local))

    branch = git("symbolic-ref", "--short", "HEAD", cwd=str(local)).strip()
    head = git("rev-parse", f"refs/heads/{branch}", cwd=str(local)).strip()

    created_user = False
    if gitea.call("GET", f"/users/{Gitea.q(owner)}", ok404=True) is None:
        gitea.call("POST", "/admin/users", {
            "username": owner, "email": f"{owner}@example.com", "password": secrets.token_urlsafe(18),
            "must_change_password": False, "visibility": "public"})
        created_user = True
    created_repo = False
    if gitea.call("GET", f"/repos/{Gitea.q(owner, repo)}", ok404=True) is None:
        gitea.call("POST", f"/admin/users/{Gitea.q(owner)}/repos", {
            "name": repo, "default_branch": branch, "auto_init": False, "private": False})
        created_repo = True

    push_url = f"{cfg.wacs_url}/{owner}/{repo}.git"
    assert_local_wacs(push_url)
    pushed_at = datetime.now(timezone.utc)
    git("push", push_url, f"refs/heads/{branch}:refs/heads/{branch}", cwd=str(local),
        auth_header=basic_auth(cfg.admin_user, cfg.admin_password))

    return {
        "repo": f"{owner}/{repo}", "source": url, "branch": branch, "head": head,
        "pushed_at": _iso(pushed_at), "local_clone": str(local),
        "cloned": cloned, "created_user": created_user, "created_repo": created_repo,
        "wacs_url": f"{cfg.public_wacs_url}/{owner}/{repo}",
    }


# ------------------------------------------------------------------------------------------- put

def put_file(cfg: Config, full_repo: str, path: str, content: bytes, message: str | None = None,
             branch: str | None = None) -> dict:
    """Create or replace one file in a single commit (fires the push webhook)."""
    gitea, _, _ = _clients(cfg)
    owner, repo = split_repo(full_repo)
    path = path.strip("/")
    if not path or ".." in path.split("/"):
        raise UserError(f"invalid file path {path!r}")
    branch = branch or _default_branch(gitea, owner, repo)
    resp = gitea.change_files(
        owner, repo, branch, message or f"fdb put {path}", {path: content})
    commit = (resp or {}).get("commit") or {}
    return {"repo": f"{owner}/{repo}", "branch": branch, "path": path, "bytes": len(content),
            "commit": commit.get("sha"), "commit_url": commit.get("html_url")}


# ---------------------------------------------------------------------------------------- status

_RENDERED_QUERY = """
query ($name: String!) {
  rendered_content(where: {content: {name: {_eq: $name}}}) { modified_on }
}"""


def _status_once(cfg: Config, owner: str, repo: str, commit: str, since: datetime | None) -> dict:
    gitea, blob, hasura = _clients(cfg)
    stages: dict[str, dict] = {}

    info = gitea.call("GET", f"/repos/{Gitea.q(owner, repo)}/git/commits/{commit}", ok404=True)
    if info is None:
        stages["wacs"] = {"state": "failed", "detail": f"commit {commit} not found in local WACS"}
        return stages
    stages["wacs"] = {"state": "ok", "commit": info.get("sha"),
                      "message": ((info.get("commit") or {}).get("message") or "").strip()}

    log = blob.get_json(f"u/{owner}/{repo}/build_log.json")
    ended = parse_time(log.get("ended_at")) if log else None
    if not log:
        stages["render"] = {"state": "pending", "detail": "no build_log.json yet"}
    else:
        logged = log.get("commit_id")
        # A branch-creation push renders with no commit info; accept it if it finished after `since`.
        matches = logged == info.get("sha") or (logged is None and since is not None and ended and ended >= since)
        if not matches:
            stages["render"] = {"state": "pending", "detail": f"latest render is for {logged or 'an unknown commit'}",
                                "ended_at": _iso(ended)}
        elif not log.get("success"):
            stages["render"] = {"state": "failed", "detail": log.get("message"), "ended_at": _iso(ended)}
        else:
            stages["render"] = {"state": "ok", "ended_at": _iso(ended), "converter": log.get("convert_module"),
                                "output": blob.url(f"u/{owner}/{repo}/index.json")}

    if stages["render"]["state"] == "failed":
        stages["languageapi"] = {"state": "skipped", "detail": "languageapi ignores unsuccessful renders"}
    else:
        rows = hasura.query(_RENDERED_QUERY, {"name": f"{owner}/{repo}".lower()})["rendered_content"]
        latest = max((parse_time(r["modified_on"]) for r in rows), default=None)
        if stages["render"]["state"] == "ok" and latest and ended and latest >= ended - timedelta(seconds=2):
            stages["languageapi"] = {"state": "ok", "rendered_files": len(rows), "updated_at": _iso(latest)}
        else:
            stages["languageapi"] = {"state": "pending", "rendered_files": len(rows), "updated_at": _iso(latest)}
    return stages


def pipeline_status(cfg: Config, full_repo: str, commit: str | None = None, wait: float = 0,
                    since: str | None = None) -> dict:
    """Where a commit is in WACS -> SRP render -> languageapi. With wait>0, poll until done."""
    gitea, _, _ = _clients(cfg)
    owner, repo = split_repo(full_repo)
    branch = _default_branch(gitea, owner, repo)
    if not commit:
        b = gitea.call("GET", f"/repos/{Gitea.q(owner, repo)}/branches/{Gitea.q(branch)}", ok404=True)
        if not b:
            raise UserError(f"{owner}/{repo} has no {branch} branch yet")
        commit = b["commit"]["id"]
    since_dt = parse_time(since) if since else None

    start = time.monotonic()
    while True:
        stages = _status_once(cfg, owner, repo, commit, since_dt)
        states = [s["state"] for s in stages.values()]
        done = all(s in ("ok", "skipped") for s in states) or "failed" in states
        if done or time.monotonic() - start >= wait:
            break
        time.sleep(POLL_SECONDS)

    if "failed" in states:
        overall = "failed"
    elif all(s == "ok" for s in states):
        overall = "ok"
    else:
        overall = "pending"
    return {"repo": f"{owner}/{repo}", "commit": commit, "status": overall,
            "elapsed_s": round(time.monotonic() - start, 1), "stages": stages}


# ------------------------------------------------------------------------------------------ list

def list_repos(cfg: Config) -> dict:
    gitea, blob, hasura = _clients(cfg)
    found = gitea.call("GET", "/repos/search?limit=50&sort=updated&order=desc")
    out = []
    for r in (found or {}).get("data", []):
        owner, name = r["owner"]["login"], r["name"]
        log = blob.get_json(f"u/{owner}/{name}/build_log.json")
        rows = hasura.query(_RENDERED_QUERY, {"name": f"{owner}/{name}".lower()})["rendered_content"]
        out.append({"repo": f"{owner}/{name}", "updated_at": r.get("updated_at"),
                    "rendered": bool(log and log.get("success")), "languageapi_files": len(rows)})
    return {"repos": out}
