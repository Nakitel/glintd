"""ubus-side RPC. Registers the `mudi.glintd` namespace via
`ubus-cli` (or, when present, `ubusd` direct socket) and dispatches
incoming method calls to handlers.

We keep this dead simple: a long-running `ubus listen`-style
subprocess isn't viable from Python without binding to libubus.
Instead we use the file-protocol pattern - each method handler
is also exposed as `glintd-rpc <method> <json-args>` on the CLI,
and ubus is wired up via shell-out registrations in the procd
init script. Slower than libubus by milliseconds, fast enough.

The app's existing SSH-tunnelled ubus call therefore works
unchanged: `ubus call mudi.glintd ping` lands here, the script
prints JSON, ubus relays it back. No new ports, no new auth.
"""
from __future__ import annotations
import json
import sys
import threading
from typing import Any

from glintd.storage.store import Store
from glintd.capabilities import Capabilities
from glintd.rpc import credentials, history, ping, test_push, tokens

# Single dispatch table. Adding a new method = adding a row.
HANDLERS = {
    "ping":                   ping.handle,
    "get_history":            history.handle,
    "list_metrics":           history.list_metrics_handle,
    "register_device_token":  tokens.register_handle,
    "unregister_device_token": tokens.unregister_handle,
    "set_push_preferences":   tokens.set_preferences_handle,
    "get_router_credentials": credentials.handle,
    "test_push":              test_push.handle,
}


class RpcServer:
    """Thread-resident dispatcher. The actual ubus binding is in
    `/etc/glintd/glintd-rpc.sh` (the install script writes it),
    which calls `python3 -m glintd.rpc.cli <method> <args-json>`
    per request. This class is only here to hold a reference to
    the Store + Capabilities so the CLI can `from glintd.daemon
    import RPC_SHARED` without separate state objects."""
    def __init__(self, store: Store, caps: Capabilities):
        self.store = store
        self.caps = caps
        self._thread: threading.Thread | None = None
        # Shared state for the CLI dispatcher to pick up. The CLI
        # process is *separate* from the daemon - it inherits no
        # memory. Instead it opens a read-only sqlite handle and
        # serves the request without touching daemon state.

    def start(self) -> None:
        # Nothing async to do at start - the install script's
        # ubus registration already wires `mudi.glintd.<method>`
        # → `glintd-rpc <method>`. Keeping the no-op start/stop
        # contract in case a future build needs an in-process listener.
        pass

    def stop(self) -> None:
        pass


def dispatch(method: str, args: dict[str, Any]) -> dict[str, Any]:
    """Top-level CLI entry point. Opens a read-only Store handle,
    looks up the handler, returns a JSON-able dict. Errors are
    surfaced as `{ "error": "<message>" }` rather than raised -
    ubus then passes that back to the app, which renders it
    inline instead of crashing."""
    handler = HANDLERS.get(method)
    if handler is None:
        return {"error": f"unknown method: {method}"}
    try:
        return handler(args)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
