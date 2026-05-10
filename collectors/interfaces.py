"""Per-ethernet-port link state + speed. Number of ports varies by
device - GL-E5800 has one (eth0/WAN), Slate AX has one each WAN+LAN
shared on the same kernel iface, GL-MT6000 Flint has 1 WAN + 4 LAN,
GL-BE3600 Slate 7 has dual 2.5G WAN. The capability probe tells us
which interfaces to look at; the format is uniform across them.

We sample three things per port: link state (up/down as 1/0),
negotiated speed in Mbps (when the kernel knows), and duplex
(0=half, 1=full). The values come from sysfs:

    /sys/class/net/<iface>/operstate
    /sys/class/net/<iface>/speed
    /sys/class/net/<iface>/duplex

`speed` and `duplex` only exist on devices the driver flagged as
ETHTOOL-capable. We tolerate their absence (read returns ENOTSUP /
empty file) - link state alone is enough for the "WAN dropped"
push trigger.
"""
from __future__ import annotations
import os

NAME = "interfaces"
INTERVAL_SEC = 30


def requires(caps) -> bool:
    return bool(caps.ethernet_ports)


def collect(caps) -> dict[str, float]:
    out: dict[str, float] = {}
    for port in caps.ethernet_ports:
        base = f"/sys/class/net/{port.name}"
        operstate = _read(os.path.join(base, "operstate"))
        # operstate strings: "up", "down", "unknown", "lowerlayerdown".
        # We collapse to 1 only on literal "up" so `lowerlayerdown`
        # (carrier present, link disabled) reads as down - the
        # user's "WAN dropped" mental model.
        out[f"interfaces.{port.name}.up"] = 1.0 if operstate == "up" else 0.0
        speed = _read_int(os.path.join(base, "speed"))
        if speed is not None and speed > 0:
            # ethtool reports -1 on no-link; clamp to 0/positive.
            out[f"interfaces.{port.name}.speed_mbps"] = float(speed)
        duplex = _read(os.path.join(base, "duplex"))
        if duplex == "full":
            out[f"interfaces.{port.name}.duplex"] = 1.0
        elif duplex == "half":
            out[f"interfaces.{port.name}.duplex"] = 0.0
    return out


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _read_int(path: str) -> int | None:
    s = _read(path)
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None
