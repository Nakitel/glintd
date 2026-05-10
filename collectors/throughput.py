"""Per-interface RX/TX throughput. Universal - `/proc/net/dev` is on
every Linux box.

Stores absolute byte counters; the daemon's downsampler converts
adjacent samples into a Mbps rate when it rolls hot→warm. The app
also computes deltas client-side for the live tick, so two
collectors writing the same data is fine - they're idempotent.

Emits one series per interface that has changing counters. Static
interfaces (loopback, never-active wan when no cellular up) still
get sampled - the tier rollup will collapse a flat line to ~0 KB.

Capability gating is implicit: every interface listed in
`caps.ethernet_ports` is sampled, plus every Wi-Fi radio's
master interface (so `phy0-ap0` etc. on GL.iNet's bridge layout).
"""
from __future__ import annotations

NAME = "throughput"
INTERVAL_SEC = 15


def requires(caps) -> bool:
    return True  # /proc/net/dev is always present


def collect(caps) -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()
    except OSError:
        return out

    # First two lines are header. Each remaining line is:
    #   <iface>: rx_bytes rx_packets ... tx_bytes tx_packets ...
    # We track every iface whose name we recognise from caps; that
    # keeps the database clean on boxes with a wild bridge layout.
    wanted = set(p.name for p in caps.ethernet_ports)
    for radio in caps.wifi_radios:
        wanted.add(radio.name)
    # Bridges + wlan masters are common upstream-traffic carriers
    # on GL.iNet; opt them in by prefix even if the cap probe
    # didn't enumerate them explicitly.
    for line in lines[2:]:
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        # `rmnet_*` is Qualcomm's modem-side iface (Mudi 7's
        # cellular WAN appears as `rmnet_ipa0`); without it the
        # WAN-cellular chart stays empty after history backfill.
        # `wwan*` covers older modem families where the kernel
        # uses generic naming.
        if (name not in wanted and
            not name.startswith(("br-", "wlan", "wlp", "phy0-", "phy1-",
                                 "wwan", "rmnet_"))):
            continue
        cols = rest.split()
        if len(cols) < 16:
            continue
        try:
            rx_bytes = int(cols[0])
            tx_bytes = int(cols[8])
            out[f"throughput.{name}.rx_bytes"] = float(rx_bytes)
            out[f"throughput.{name}.tx_bytes"] = float(tx_bytes)
        except (ValueError, IndexError):
            pass
    return out
