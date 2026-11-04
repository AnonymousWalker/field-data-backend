"""Endpoints and credentials, from the environment.

Defaults target the host (published ports on localhost). The fdb-api container overrides the
FDB_*_URL variables with in-network addresses.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]

# Push targets the tool will accept. Anything else is refused, so imports can only ever land
# in the local WACS.
LOCAL_WACS_HOSTS = {"localhost", "127.0.0.1", "wacs"}


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "")
    return value if value else default


@dataclass(frozen=True)
class Config:
    wacs_url: str
    blob_url: str
    hasura_url: str
    admin_user: str
    admin_password: str
    imports_dir: Path
    # Host:port that WACS writes into html_url / clone URLs (its ROOT_URL), used to show URLs.
    public_wacs_url: str

    @classmethod
    def from_env(cls) -> "Config":
        wacs_port = _env("WACS_HTTP_PORT", "3000")
        blob_port = _env("AZURITE_BLOB_PORT", "10000")
        hasura_port = _env("HASURA_PORT", "8080")
        public_wacs = f"http://localhost:{wacs_port}"
        cfg = cls(
            wacs_url=_env("FDB_WACS_URL", public_wacs).rstrip("/"),
            blob_url=_env("FDB_BLOB_URL", f"http://localhost:{blob_port}/devstoreaccount1/$web").rstrip("/"),
            hasura_url=_env("FDB_HASURA_URL", f"http://localhost:{hasura_port}").rstrip("/"),
            admin_user=_env("WACS_ADMIN_USER", "fdbadmin"),
            admin_password=_env("WACS_ADMIN_PASSWORD", "fdbadmin123"),
            imports_dir=Path(_env("FDB_IMPORTS_DIR", str(ROOT / ".local" / "imports"))),
            public_wacs_url=public_wacs,
        )
        assert_local_wacs(cfg.wacs_url)
        return cfg


class NotLocalError(RuntimeError):
    pass


def assert_local_wacs(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host not in LOCAL_WACS_HOSTS:
        raise NotLocalError(
            f"refusing to use {url!r}: fdb only pushes to the local WACS "
            f"(allowed hosts: {', '.join(sorted(LOCAL_WACS_HOSTS))})"
        )
