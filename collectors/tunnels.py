"""Per-tunnel handshake age + transfer counters.

WireGuard is the common case across GL.iNet (every model with the
"VPN client" feature has wgN interfaces). OpenVPN, Tailscale,
ZeroTier are present on a subset; the capability probe records
which binaries are installed and we only run their respective
collectors when the capability is present.

For each *active* WireGuard peer we emit:

    tunnels.wg.<peer-id>.handshake_age_s   - seconds since last
    tunnels.wg.<peer-id>.rx_bytes
    tunnels.wg.<peer-id>.tx_bytes

A tunnel that has never connected (no handshake) doesn't get
sampled - `wg show` doesn't list it. The app's "tunnel down" UI
state already handles that.
"""
from __future__ import annotations
import subprocess
import time

NAME = "tunnels"
INTERVAL_SEC = 30


def requires(caps) -> bool:
    # OpenVPN / Tailscale / ZeroTier are stubbed for now;
    # WireGuard is the only fully-collected tunnel kind today.
    # When their collectors land, expand this gate to `or` them in.
    return caps.has_wireguard


def collect(caps) -> dict[str, float]:
    out: dict[str, float] = {}
    if not caps.has_wireguard:
        return out

    # `wg show all dump` is whitespace-separated, machine-readable.
    # Format per peer line:
    #   <iface>  <pubkey>  <preshared>  <endpoint>  <allowed-ips>
    #     <last-handshake>  <rx-bytes>  <tx-bytes>  <keepalive>
    try:
        r = subprocess.run(
            ["wg", "show", "all", "dump"],
            capture_output=True, text=True, timeout=2.0)
        if r.returncode != 0:
            return out
    except (OSError, subprocess.TimeoutExpired):
        return out

    now = int(time.time())
    seen_iface: dict[str, int] = {}
    for line in r.stdout.splitlines():
        cols = line.split("\t")
        # `wg show all dump` prepends iface to every line:
        # interface rows have 5 cols (iface privkey pubkey
        # listen-port fwmark); peer rows have 9 cols (iface
        # pubkey psk endpoint allowed-ips last-hs rx tx keepalive).
        # We want peers; skip iface rows + any malformed lines.
        if len(cols) < 9:
            continue
        iface = cols[0]
        # Multiple peers on a single iface: index them by order
        # of appearance so the metric name is stable across
        # samples (we don't want it to depend on pubkey, which is
        # a 44-char base64 string and noisy in graph legends).
        idx = seen_iface.get(iface, 0)
        seen_iface[iface] = idx + 1
        peer_id = f"{iface}-{idx}"
        try:
            last_hs = int(cols[5])  # unix seconds, 0 = never
            rx = int(cols[6])
            tx = int(cols[7])
        except ValueError:
            continue
        if last_hs > 0:
            out[f"tunnels.wg.{peer_id}.handshake_age_s"] = float(now - last_hs)
        out[f"tunnels.wg.{peer_id}.rx_bytes"] = float(rx)
        out[f"tunnels.wg.{peer_id}.tx_bytes"] = float(tx)
    return out
