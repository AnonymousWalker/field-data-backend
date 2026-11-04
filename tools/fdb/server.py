"""fdb HTTP API: the same operations as the CLI, as JSON over HTTP. Local use only (no auth).

  GET  /healthz
  GET  /repos
  POST /import                                   {"url": "...", "as": "owner/repo"?, "wait": secs?}
  PUT  /repos/{owner}/{repo}/contents/{path}     raw file body; ?message=&branch=&wait=
  GET  /repos/{owner}/{repo}/pipeline            ?commit=&since=&wait=
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import core
from .clients import HTTPError
from .config import Config, NotLocalError

MAX_WAIT = 600
MAX_BODY = 50 * 1024 * 1024
_REPO = r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
ROUTES = [
    ("GET", re.compile(r"^/healthz$"), "health"),
    ("GET", re.compile(r"^/repos$"), "list"),
    ("POST", re.compile(r"^/import$"), "import_"),
    ("PUT", re.compile(rf"^/repos/{_REPO}/contents/(?P<path>.+)$"), "put"),
    ("GET", re.compile(rf"^/repos/{_REPO}/pipeline$"), "pipeline"),
]


def _wait(value) -> float:
    try:
        return max(0.0, min(float(value or 0), MAX_WAIT))
    except ValueError:
        raise core.UserError("wait must be a number of seconds")


class Handler(BaseHTTPRequestHandler):
    server_version = "fdb-api/1"

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise core.UserError("request body too large")
        return self.rfile.read(length) if length else b""

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        for m, pattern, name in ROUTES:
            match = pattern.match(url.path)
            if m == method and match:
                break
        else:
            return self._send(404, {"error": f"no route for {method} {url.path}"})
        try:
            cfg = Config.from_env()
            status, body = getattr(self, name)(cfg, match.groupdict(), query)
            self._send(status, body)
        except (core.UserError, NotLocalError, json.JSONDecodeError) as e:
            self._send(400, {"error": str(e)})
        except HTTPError as e:
            self._send(502, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 - report anything else as a 500 with context
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self):  # noqa: N802
        self._dispatch("PUT")

    # ---- handlers: return (http_status, json_body)

    def health(self, cfg, params, query):
        return 200, {"ok": True}

    def list(self, cfg, params, query):
        return 200, core.list_repos(cfg)

    def import_(self, cfg, params, query):
        req = json.loads(self._body() or b"{}")
        if not req.get("url"):
            raise core.UserError('body must be JSON like {"url": "https://..."}')
        res = core.import_repo(cfg, req["url"], req.get("as"))
        wait = _wait(req.get("wait"))
        if wait:
            res["pipeline"] = core.pipeline_status(cfg, res["repo"], res["head"], wait, res["pushed_at"])
        return 200, res

    def put(self, cfg, params, query):
        full = f"{params['owner']}/{params['repo']}"
        res = core.put_file(cfg, full, unquote(params["path"]), self._body(),
                            query.get("message"), query.get("branch"))
        wait = _wait(query.get("wait"))
        if wait:
            res["pipeline"] = core.pipeline_status(cfg, full, res["commit"], wait)
        return 200, res

    def pipeline(self, cfg, params, query):
        res = core.pipeline_status(cfg, f"{params['owner']}/{params['repo']}", query.get("commit"),
                                   _wait(query.get("wait")), query.get("since"))
        # 200 = done ok, 202 = still pending, 409 = a stage failed.
        return {"ok": 200, "pending": 202, "failed": 409}[res["status"]], res

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")


def main() -> None:
    host = os.environ.get("FDB_API_HOST", "127.0.0.1")
    port = int(os.environ.get("FDB_API_PORT", "7080"))
    Config.from_env()  # fail fast on a non-local WACS URL
    print(f"fdb-api listening on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
