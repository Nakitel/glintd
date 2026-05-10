"""Shared helpers for the read-only RPC handlers.

Each `glintd-rpc <method>` invocation is a fresh Python process -
it can't reach into the daemon's in-memory state. So handlers open
a sqlite connection to the same DB the daemon writes to. WAL mode
(set in schema.sql) lets multiple readers run concurrently with
the writer without locking, which is exactly what we want here.
"""
from __future__ import annotations
from glintd.storage.store import Store

import os

# Production paths. Overridable via env so the CLI / tests work
# from a source checkout without an /etc/glintd install.
DEFAULT_DB = os.environ.get("GLINTD_DB", "/tmp/glintd.db")
SCHEMA_PATH = os.environ.get(
    "GLINTD_SCHEMA",
    "/etc/glintd/storage/schema.sql",
)


def open_store() -> Store:
    return Store(DEFAULT_DB, SCHEMA_PATH)
