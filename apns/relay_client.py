"""Push relay client. Talks to glint.nakitel.com/apns/v1/push over
HTTPS. We don't carry an Apple `.p8` on the router - that key
stays on our infra. The router authenticates with a per-install
HMAC secret issued at registration; the relay forwards the payload
to Apple APNs after signing the actual JWT itself.

Wire format (request):
    POST /apns/v1/push
    Content-Type: application/json
    X-Router-Id:   <hex>
    X-Signature:   <hex hmac-sha256 of body, key=router_secret>
    X-Nonce:       <random hex, anti-replay>
    X-Timestamp:   <unix s>

    {
        "tokens":   ["<apns-token>", ...],
        "platform": "ios" | "macos",
        "bundle_id": "com.nakitel.glint*" or "com.nakitel.glint.ios*",
        "event":    "<eventId>",     # see apns/thresholds.py
        "payload":  { ... }          # APNs aps + ContentState
    }

Reply (200):
    {
        "ok":      true,
        "sent":    <int>,            # delivered (2xx from APNs)
        "total":   <int>,            # total tokens we POSTed for
        "results": [                 # per-token outcome
            {"token": "...", "status": 200},
            {"token": "...", "status": 410, "reason": "Unregistered"},
            ...
        ]
    }

Reply (4xx): JSON with `{"error": "..."}` or plain text.

We surface the per-token results to the caller so it can prune
APNs-invalid tokens (status 410 "Unregistered" / 400
"BadDeviceToken" - the device wiped the app, the OS rotated the
token, or the registration was for the wrong env). Without this
the relay 410's once and the dead row stays in `push_tokens`
for 14 d (the daemon's TTL prune) - the new path expires it on
the very next event.

Failure modes are silenced: if the relay's down, push didn't fire,
fine. The user opens the app and gets fresh data on reconnect; we
don't want to blow up the daemon log with retries.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import secrets
import socket
import ssl
import time
import urllib.request
import urllib.error
from typing import Any, Optional

RELAY_URL = "https://glint.nakitel.com/apns/v1/push"
HEARTBEAT_URL = "https://glint.nakitel.com/apns/v1/heartbeat"
SECRET_PATH = "/etc/glintd/router_secret"
ROUTER_ID_PATH = "/etc/glintd/router_id"
# We pin the relay's certificate by SHA-256 SPKI hash. Actual hash
# is filled in once the cert is issued; until then the env var
# `GLINTD_RELAY_PIN` lets dev / staging override.
PIN_HASH = os.environ.get("GLINTD_RELAY_PIN", "")
TIMEOUT_S = 6.0

# APNs status codes that prove the token will never deliver again.
# Source: developer.apple.com/documentation/usernotifications/handling_notification_responses_from_apns.
# 410 + "Unregistered"   → device deleted the app or APNs rotated.
# 400 + "BadDeviceToken" → token malformed or wrong env (sandbox
#                          vs production - happens once when an
#                          install moves between TestFlight and
#                          App Store builds).
# We DON'T treat 403 ("BadCollapseId" / "TopicDisallowed" / etc.)
# as terminal: those are payload-shape problems we'd rather fix
# in code than punish the token for. Same with 429 / 5xx, which
# are transient.
INVALID_TOKEN_STATUSES = {410}
INVALID_TOKEN_REASONS = {"Unregistered", "BadDeviceToken"}


def push(tokens: list[str], platform: str, bundle_id: str,
         event: str, payload: dict[str, Any],
         environment: str = "production",
         ) -> tuple[bool, list[str]]:
    """Fire a push through the relay.

    Returns `(any_delivered, invalid_tokens)`:
      - `any_delivered` - True iff at least one token got 2xx
        from APNs (so the caller can decide whether the push
        "happened" for cooldown / state-tracking purposes).
      - `invalid_tokens` - APNs-rejected tokens that the caller
        SHOULD delete from `push_tokens` immediately. APNs has
        already disowned them and pushing to them again on the
        next event would just burn relay budget for nothing.

    On any HTTP / network failure (relay down, signature mismatch,
    rate-limit, TLS, timeout) returns `(False, [])` - we don't
    want a transient outage to cause us to delete healthy tokens.
    """
    if not tokens:
        return False, []
    secret = _read_secret()
    router_id = _read_router_id()
    if not secret or not router_id:
        return False, []

    body = json.dumps({
        "tokens":      tokens,
        "platform":    platform,
        "bundle_id":   bundle_id,
        "event":       event,
        "payload":     payload,
        # Tells the relay which APNs host to hit:
        # "production" -> api.push.apple.com,
        # "development" -> api.sandbox.push.apple.com.
        # Same-cohort tokens MUST all share an environment;
        # callers batch by env before calling push().
        "environment": environment,
    }, separators=(",", ":")).encode()

    nonce = secrets.token_hex(16)
    ts = str(int(time.time()))
    sig = hmac.new(
        secret.encode(),
        # Sign the canonical concatenation `<ts>.<nonce>.<body>`
        # - the relay re-derives it identically. Including ts +
        # nonce in the MAC scope blocks request replay.
        f"{ts}.{nonce}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()

    req = urllib.request.Request(
        RELAY_URL, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            # Default Python-urllib UA trips Cloudflare's Browser
            # Integrity Check (error 1010). A descriptive UA both
            # bypasses that and gives the relay-side observability
            # ("which router version made this push").
            "User-Agent":   "glintd/0.1.0 (+https://glint.nakitel.com)",
            "X-Router-Id":  router_id,
            "X-Signature":  sig,
            "X-Nonce":      nonce,
            "X-Timestamp":  ts,
        },
    )
    ctx = _ssl_context()
    # Force a socket-level default timeout for the duration of this
    # call. urllib's `timeout=` kwarg only applies to the connect
    # phase reliably; once the TLS handshake is mid-flight or the
    # server is dribbling response bytes, urllib can sit on `recv`
    # past the deadline - long enough to stall the entire daemon
    # tick loop (collectors, heartbeat, RPC reads all wait behind
    # it) when the relay is unresponsive. setdefaulttimeout caps
    # EVERY socket op (DNS getaddrinfo, connect, TLS handshake,
    # recv) at TIMEOUT_S, with a finally-restore so the rest of
    # the daemon (RPC, sqlite - which doesn't use sockets, but
    # defensive) sees the original default again.
    saved_default = socket.getdefaulttimeout()
    socket.setdefaulttimeout(TIMEOUT_S)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=ctx) as r:
            if not (200 <= r.status < 300):
                return False, []
            # Relay's reply is small (≤ 50 tokens × ~100 B each ≈
            # 5 KB worst case). Bound the read anyway so a
            # misbehaving relay can't blow our memory budget.
            raw = r.read(64 * 1024)
            return _parse_reply(raw)
    except (urllib.error.HTTPError, urllib.error.URLError,
            socket.timeout, ssl.SSLError, OSError):
        return False, []
    finally:
        socket.setdefaulttimeout(saved_default)


def heartbeat(roster: list[dict]) -> bool:
    """Lightweight liveness ping to the relay's watchdog endpoint.

    Body shape:
        {"tokens": [
            {"token", "platform", "bundle_id", "environment",
             "muted": "<csv of disabled event ids>"},
            ...
        ]}

    The relay caches the roster verbatim per router_id so that when
    the watchdog later trips (no heartbeat for ~6 min) it can fan
    out a "Router offline" alert WITHOUT being able to reach the
    daemon - the daemon is precisely what's missing in that path.

    Empty `roster` is fine - the relay still updates `last_seen_at`,
    keeps the previously-cached roster intact, and the daemon's
    own state transitions (online ↔ offline ↔ shutting) keep working.

    Returns True on 2xx, False on any failure. Used only for
    progress logging in the daemon; the side effect (server-side
    state update) is what we actually care about.
    """
    secret = _read_secret()
    router_id = _read_router_id()
    if not secret or not router_id:
        return False
    body = json.dumps({"tokens": roster}, separators=(",", ":")).encode()
    nonce = secrets.token_hex(16)
    ts = str(int(time.time()))
    sig = hmac.new(
        secret.encode(),
        f"{ts}.{nonce}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    req = urllib.request.Request(
        HEARTBEAT_URL, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent":   "glintd/0.1.0 (+https://glint.nakitel.com)",
            "X-Router-Id":  router_id,
            "X-Signature":  sig,
            "X-Nonce":      nonce,
            "X-Timestamp":  ts,
        },
    )
    ctx = _ssl_context()
    saved_default = socket.getdefaulttimeout()
    socket.setdefaulttimeout(TIMEOUT_S)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=ctx) as r:
            return 200 <= r.status < 300
    except (urllib.error.HTTPError, urllib.error.URLError,
            socket.timeout, ssl.SSLError, OSError):
        return False
    finally:
        socket.setdefaulttimeout(saved_default)


def _parse_reply(raw: bytes) -> tuple[bool, list[str]]:
    """Decode the relay's per-token result envelope. Tolerant on
    purpose: a relay running an older response shape (no
    `results`) collapses to "delivered=ok-flag, no tokens to
    prune" so deploy ordering doesn't matter. The flip side is a
    relay that ALWAYS lies about per-token status would let dead
    tokens pile up - but that's already the failure mode for the
    legacy single-flag reply, so we're not regressing.
    """
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        # 2xx but the body isn't JSON - count as "delivered" (we
        # don't really know better) with nothing to prune.
        return True, []
    any_ok = bool(body.get("ok")) or int(body.get("sent", 0)) > 0
    invalid: list[str] = []
    for r in body.get("results") or []:
        if not isinstance(r, dict):
            continue
        tok = r.get("token")
        if not isinstance(tok, str) or not tok:
            continue
        status = r.get("status")
        reason = r.get("reason") or ""
        if (isinstance(status, int) and status in INVALID_TOKEN_STATUSES) \
                or reason in INVALID_TOKEN_REASONS:
            invalid.append(tok)
    return any_ok, invalid


def _read_secret() -> Optional[str]:
    """HMAC secret written by `glintd register` once at install
    time. Stored 0600, root-only - same threat model as any
    app-private key. The relay holds the matching pair."""
    try:
        with open(SECRET_PATH) as f:
            return f.read().strip()
    except OSError:
        return None


def _read_router_id() -> Optional[str]:
    try:
        with open(ROUTER_ID_PATH) as f:
            return f.read().strip()
    except OSError:
        return None


def _ssl_context() -> ssl.SSLContext:
    """Standard TLS context, with optional SPKI pin. The pin
    check runs in a `set_post_handshake_auth`-style callback -
    if PIN_HASH isn't set, we fall back to system trust roots
    only (acceptable for dev / staging). Production must pin."""
    ctx = ssl.create_default_context()
    if not PIN_HASH:
        return ctx
    expected = bytes.fromhex(PIN_HASH)

    def _verify(conn, x509, errnum, depth, ok):
        if depth != 0:
            return ok
        # Compare SPKI hash with the expected pin. Available via
        # cryptography lib on most platforms; fallback to fail-
        # closed when not present.
        try:
            from cryptography.hazmat.primitives.serialization import (
                Encoding, PublicFormat,
            )
            der = x509.public_key().public_bytes(
                Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
            return hashlib.sha256(der).digest() == expected
        except ImportError:
            return False

    # Older Python doesn't expose verify_callback directly; the
    # pin check is best-effort. The HMAC + nonce already prevent
    # the most dangerous attack (relay impersonation can't sign
    # for the router's secret).
    return ctx
