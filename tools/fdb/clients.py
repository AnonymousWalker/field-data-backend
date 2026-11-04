"""Minimal stdlib clients for Gitea (WACS), Azurite blob, Hasura, and git."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class HTTPError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} from {url}: {body[:300]}")
        self.status = status
        self.body = body


def _request(method: str, url: str, *, headers: dict | None = None, data: bytes | None = None,
             timeout: float = 60) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class Gitea:
    def __init__(self, base_url: str, user: str, password: str):
        self.base = base_url.rstrip("/")
        self.auth = basic_auth(user, password)

    def call(self, method: str, path: str, body: Any = None, *, ok404: bool = False) -> Any:
        url = f"{self.base}/api/v1{path}"
        data = json.dumps(body).encode() if body is not None else None
        status, raw = _request(method, url, data=data, headers={
            "Authorization": self.auth, "Content-Type": "application/json", "Accept": "application/json"})
        if ok404 and status == 404:
            return None
        if status >= 300:
            raise HTTPError(status, url, raw.decode(errors="replace"))
        return json.loads(raw) if raw else None

    @staticmethod
    def q(*parts: str) -> str:
        return "/".join(urllib.parse.quote(p, safe="") for p in parts)

    def get_file_sha(self, owner: str, repo: str, path: str, ref: str) -> str | None:
        p = urllib.parse.quote(path, safe="/")
        info = self.call("GET", f"/repos/{self.q(owner, repo)}/contents/{p}?ref={urllib.parse.quote(ref)}", ok404=True)
        return info.get("sha") if isinstance(info, dict) else None

    def change_files(self, owner: str, repo: str, branch: str, message: str,
                     files: dict[str, bytes]) -> dict:
        """One commit creating/updating each path. Fires a normal push webhook."""
        entries = []
        for path, content in files.items():
            sha = self.get_file_sha(owner, repo, path, branch)
            entry = {"operation": "update" if sha else "create", "path": path,
                     "content": base64.b64encode(content).decode()}
            if sha:
                entry["sha"] = sha
            entries.append(entry)
        return self.call("POST", f"/repos/{self.q(owner, repo)}/contents",
                         {"branch": branch, "message": message, "files": entries})


class Blob:
    """Anonymous reads of the public $web container in Azurite."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def get_json(self, path: str) -> Any | None:
        status, raw = _request("GET", f"{self.base}/{path.lstrip('/')}", timeout=15)
        if status == 404:
            return None
        if status >= 300:
            raise HTTPError(status, path, raw.decode(errors="replace"))
        return json.loads(raw.decode("utf-8-sig"))

    def url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"


class Hasura:
    """Queries run as the `anonymous` role, i.e. with the public API's read permissions."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def query(self, query: str, variables: dict | None = None) -> dict:
        url = f"{self.base}/v1/graphql"
        status, raw = _request("POST", url, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                               headers={"Content-Type": "application/json", "x-hasura-role": "anonymous"}, timeout=15)
        body = json.loads(raw) if raw else {}
        if status >= 300 or "errors" in body:
            raise HTTPError(status, url, json.dumps(body.get("errors", body)))
        return body["data"]


def git(*args: str, cwd: str | None = None, auth_header: str | None = None) -> str:
    """Run git. Credentials go in a per-command header, never into the repo's config."""
    cmd = ["git"]
    if auth_header:
        cmd += ["-c", f"http.extraHeader=Authorization: {auth_header}"]
    cmd += list(args)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        safe = [a if not a.startswith("http.extraHeader") else "http.extraHeader=<redacted>" for a in cmd]
        raise RuntimeError(f"{' '.join(safe)} failed: {proc.stderr.strip()[-800:]}")
    return proc.stdout
