"""APNs device-token registry. The threshold engine reads here
when firing pushes through the relay.

`register_device_token` is idempotent - same (token, bundle_id)
just bumps the registered timestamp. Tokens are kept in a sqlite
table that survives daemon restart, so the push path doesn't
care whether the daemon was rebooted between registration and the
first event."""
from __future__ import annotations
import time
from typing import Any
from glintd.rpc.context import open_store


def register_handle(args: dict[str, Any]) -> dict[str, Any]:
    token = args.get("token")
    platform = args.get("platform")
    bundle_id = args.get("bundle_id")
    for k, v in (("token", token), ("platform", platform),
                 ("bundle_id", bundle_id)):
        if not isinstance(v, str) or not v:
            return {"error": f"{k} is required"}
    # Optional `disabled_events`: list of event ids the client wants
    # silenced. Stored as CSV. Re-registers preserve the previous
    # value when this arg is absent so iOS doesn't have to send
    # preferences with every token rotation.
    disabled = args.get("disabled_events")
    disabled_csv: str | None = None
    if isinstance(disabled, list):
        disabled_csv = ",".join(str(e) for e in disabled if isinstance(e, str))
    # Optional `environment`: "production" | "development". Tells
    # the relay which APNs host to route this token through - prod
    # vs sandbox. Critical for Debug-build / TestFlight tokens
    # which only deliver via api.sandbox.push.apple.com; sending
    # them to api.push.apple.com returns BadDeviceToken (400) and
    # the silent prune path then deletes the row, looping forever.
    # Re-registers without the arg preserve the previous value so
    # a quick mute-toggle from the iOS app doesn't accidentally
    # downgrade env back to NULL → default-production.
    env_arg = args.get("environment")
    environment: str | None = None
    if isinstance(env_arg, str) and env_arg in ("production", "development"):
        environment = env_arg
    store = open_store()
    now = int(time.time())
    # Server-side downgrade guard. If the existing row's env is
    # "development" but the incoming call says "production", REFUSE
    # the change and keep development. Reason: a device that was
    # ever bound to sandbox APNs (development entitlement) can't
    # silently flip to production - that path only exists if the
    # binary itself is replaced, which on iOS gives a NEW token
    # (different hex). Same hex + downgrade env = race condition
    # in the iOS app's env detection. Surfaces in testing as a
    # cycle: register dev → some observer re-fires with bad env
    # → row becomes production → APNs prod returns BadDeviceToken
    # → daemon prunes → repeat. Pinning env to "development" on
    # the storage side breaks that loop without needing to fully
    # debug iOS-side flakiness.
    if environment == "production":
        cur = store.conn.execute(
            "SELECT environment FROM push_tokens WHERE token = ?",
            (token,)).fetchone()
        if cur is not None and cur["environment"] == "development":
            environment = "development"
    if disabled_csv is None:
        # Preserve existing prefs: read current row, keep its CSV.
        cur = store.conn.execute(
            "SELECT disabled_events FROM push_tokens WHERE token = ?",
            (token,)).fetchone()
        disabled_csv = (cur["disabled_events"] if cur else None) or ""
    if environment is None:
        # Same preserve-on-omit semantics as disabled_csv: read
        # the existing row's env so a partial re-register can't
        # blank a previously-set field. Falls back to "production"
        # only on first-time insert with no env arg (matches the
        # daemon's COALESCE(environment, 'production') reader).
        cur = store.conn.execute(
            "SELECT environment FROM push_tokens WHERE token = ?",
            (token,)).fetchone()
        environment = (cur["environment"] if cur else None) or "production"
    store.conn.execute(
        "INSERT OR REPLACE INTO push_tokens(token, platform, bundle_id, "
        "registered, disabled_events, environment) VALUES (?, ?, ?, ?, ?, ?)",
        (token, platform, bundle_id, now, disabled_csv, environment))
    # Opportunistic single-token-per-device prune: when the app
    # re-registers, drop any *other* tokens with the same
    # (platform, bundle_id) that haven't been refreshed in 7 days.
    # A device that recently registered is almost certainly the
    # current owner of the install, and APNs would 410 the older
    # tokens anyway. The 7-day window is shorter than the daemon's
    # global TOKEN_TTL prune (14 d) so a real second device that
    # only opens the app every couple of weeks isn't stomped on
    # by a more-active first device. Without this step the table
    # accumulates one stale row per app launch.
    store.conn.execute(
        "DELETE FROM push_tokens "
        "WHERE platform = ? AND bundle_id = ? AND token != ? "
        "AND registered < ?",
        (platform, bundle_id, token, now - 7 * 86400))
    # router_id is the stable per-install id used as the relay's
    # auth subject. Derived from the daemon's persistent secret in
    # /etc/glintd/router_id; the install script writes that file once.
    router_id = ""
    try:
        with open("/etc/glintd/router_id") as f:
            router_id = f.read().strip()
    except OSError:
        pass
    return {"ok": True, "router_id": router_id, "registered": now}


def unregister_handle(args: dict[str, Any]) -> dict[str, Any]:
    token = args.get("token")
    if not isinstance(token, str) or not token:
        return {"error": "token is required"}
    store = open_store()
    store.conn.execute(
        "DELETE FROM push_tokens WHERE token = ?", (token,))
    return {"ok": True}


def set_preferences_handle(args: dict[str, Any]) -> dict[str, Any]:
    """Update the `disabled_events` mute list for a registered
    token without re-registering. iOS Settings calls this when the
    user toggles individual event types. Unknown tokens are a
    no-op (the next register_device_token will pick up fresh prefs)."""
    token = args.get("token")
    if not isinstance(token, str) or not token:
        return {"error": "token is required"}
    disabled = args.get("disabled_events")
    if not isinstance(disabled, list):
        return {"error": "disabled_events must be a list"}
    csv = ",".join(str(e) for e in disabled if isinstance(e, str))
    store = open_store()
    cur = store.conn.execute(
        "UPDATE push_tokens SET disabled_events = ? WHERE token = ?",
        (csv, token))
    return {"ok": True, "matched": cur.rowcount}
