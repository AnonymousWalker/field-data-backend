"""fdb command line. Run `bin/fdb --help`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import core
from .clients import HTTPError
from .config import Config, NotLocalError

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_PENDING = 0, 1, 2, 3
_MARK = {"ok": "\033[32mok\033[0m", "pending": "\033[33mpending\033[0m",
         "failed": "\033[31mfailed\033[0m", "skipped": "skipped"}


def _print_status(res: dict) -> None:
    print(f"{res['repo']} @ {res['commit'][:10]}: {res['status']} ({res['elapsed_s']}s)")
    for name, st in res["stages"].items():
        extra = {k: v for k, v in st.items() if k != "state"}
        detail = ", ".join(f"{k}={str(v).splitlines()[0] if v else v}" for k, v in extra.items())
        print(f"  {name:<12} {_MARK.get(st['state'], st['state']):<18} {detail}")


def _status_exit(res: dict) -> int:
    return {"ok": EXIT_OK, "failed": EXIT_FAILED}.get(res["status"], EXIT_PENDING)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fdb", description=(
        "Import, edit and follow WACS repos through the local field-data stack. "
        "Everything is pushed to the local WACS only."))
    p.add_argument("--json", action="store_true", help="print machine-readable JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import", help="mirror a git repo locally and push it into the local WACS")
    imp.add_argument("url", help="source git URL (https://, file://, or a local path)")
    imp.add_argument("--as", dest="as_repo", metavar="OWNER/REPO", help="local name (default: from the URL)")
    imp.add_argument("--wait", nargs="?", type=float, const=300, default=0, metavar="SECS",
                     help="then wait for render + languageapi (default 300s)")

    put = sub.add_parser("put", help="create or replace one file in a single commit")
    put.add_argument("repo", metavar="OWNER/REPO")
    put.add_argument("path", help="path inside the repo, e.g. 66-JUD.usfm")
    put.add_argument("src", help="local file with the new content ('-' for stdin)")
    put.add_argument("-m", "--message", help="commit message")
    put.add_argument("--branch", help="default: the repo's default branch")
    put.add_argument("--wait", nargs="?", type=float, const=300, default=0, metavar="SECS",
                     help="then wait for render + languageapi (default 300s)")

    st = sub.add_parser("status", help="where a commit is in WACS -> render -> languageapi")
    st.add_argument("repo", metavar="OWNER/REPO")
    st.add_argument("--commit", help="default: head of the default branch")
    st.add_argument("--since", help="ISO time; accept a commit-less render that finished after it")
    st.add_argument("--wait", nargs="?", type=float, const=300, default=0, metavar="SECS",
                    help="poll until done (default 300s)")

    sub.add_parser("list", help="local WACS repos and their pipeline state")

    args = p.parse_args(argv)
    out = (lambda r: print(json.dumps(r, indent=2))) if args.json else None

    try:
        cfg = Config.from_env()
        if args.cmd == "import":
            res = core.import_repo(cfg, args.url, args.as_repo)
            if out:
                if args.wait:
                    res["pipeline"] = core.pipeline_status(cfg, res["repo"], res["head"], args.wait, res["pushed_at"])
                out(res)
                return _status_exit(res["pipeline"]) if args.wait else EXIT_OK
            print(f"imported {res['source']} -> {res['wacs_url']} ({res['branch']} @ {res['head'][:10]})")
            print(f"  local mirror: {res['local_clone']} (push to source disabled)")
            if args.wait:
                status = core.pipeline_status(cfg, res["repo"], res["head"], args.wait, res["pushed_at"])
                _print_status(status)
                return _status_exit(status)
            return EXIT_OK

        if args.cmd == "put":
            content = sys.stdin.buffer.read() if args.src == "-" else Path(args.src).read_bytes()
            res = core.put_file(cfg, args.repo, args.path, content, args.message, args.branch)
            if args.wait:
                res["pipeline"] = core.pipeline_status(cfg, args.repo, res["commit"], args.wait)
            if out:
                out(res)
            else:
                print(f"committed {res['path']} to {res['repo']}@{res['branch']}: {res['commit']}")
                if args.wait:
                    _print_status(res["pipeline"])
            return _status_exit(res["pipeline"]) if args.wait else EXIT_OK

        if args.cmd == "status":
            res = core.pipeline_status(cfg, args.repo, args.commit, args.wait, args.since)
            out(res) if out else _print_status(res)
            return _status_exit(res)

        if args.cmd == "list":
            res = core.list_repos(cfg)
            if out:
                out(res)
            else:
                for r in res["repos"]:
                    print(f"  {r['repo']:<40} rendered={str(r['rendered']).lower():<5} "
                          f"languageapi_files={r['languageapi_files']}")
            return EXIT_OK
    except (core.UserError, NotLocalError, FileNotFoundError) as e:
        print(f"fdb: {e}", file=sys.stderr)
        return EXIT_USAGE
    except (HTTPError, RuntimeError, OSError) as e:
        print(f"fdb: {e}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
