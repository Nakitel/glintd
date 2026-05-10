"""get_router_credentials - return the router's id + HMAC secret
to a trusted local caller (the app, over the same SSH session
that runs every other ubus call). The pair lets the app:

    1. POST to /apns/v1/enroll on glint.nakitel.com so the relay
       knows this router's secret. Idempotent.
    2. POST any further authenticated requests it ever needs to
       sign on the daemon's behalf - currently none, but having
       the secret on the app side keeps the option open.

Threat model: anything that can run `ubus call mudi.glintd
get_router_credentials` is already root on the router (the SSH
key that authenticates the session is the same one used for
every other read), so we're not gating on anything beyond ubus
ACL - which already restricts the namespace to authenticated
users via rpcd."""
from __future__ import annotations
from typing import Any


def handle(_: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        with open("/etc/glintd/router_id") as f:
            out["router_id"] = f.read().strip()
    except OSError:
        return {"error": "router_id missing - daemon not fully provisioned"}
    try:
        with open("/etc/glintd/router_secret") as f:
            out["router_secret"] = f.read().strip()
    except OSError:
        return {"error": "router_secret missing - re-run install.sh"}
    out["ok"] = True
    return out
