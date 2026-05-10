"""Per-SIM cellular signal. Skipped on Wi-Fi-only routers.

Uses the no-arg form of `cellular.network info` because it's the
only one that returns RSSI for the *inactive* slot - same trick
the app's SimProbe uses. Covers GL-X3000 (1 SIM), GL-E5800 (2), and
any future DSDS Quectel-based GL.iNet box that exposes the same
ubus method.

Emits one series per slot per metric:

    signal.sim1.rsrp_dbm   = -98
    signal.sim1.sinr_db    = 12
    signal.sim2.rssi_dbm   = -101
    ...

We don't try to canonicalise carrier name or band into a numeric
value - those are surfaced by the live-state path, not the history
graph. History is for the four numeric quality indicators.
"""
from __future__ import annotations
import json
import subprocess

NAME = "signal"
INTERVAL_SEC = 30  # signal moves slower than throughput; 2× the system rate


def requires(caps) -> bool:
    return caps.has_modem and caps.sim_slots > 0


def collect(caps) -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        r = subprocess.run(
            ["ubus", "call", "cellular.network", "info", "{}"],
            capture_output=True, text=True, timeout=3.0)
        if r.returncode != 0 or not r.stdout.strip():
            return out
        data = json.loads(r.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return out

    if not isinstance(data, dict):
        return out

    # GL.iNet 4.x: {"networks":[{"slot":"1", "cell_info":{...}}, ...]}
    # Older flavour:        {"sim1":{...}, "sim2":{...}}
    iter_slots: list[tuple[str, dict]] = []
    if isinstance(data.get("networks"), list):
        for n in data["networks"]:
            if isinstance(n, dict) and n.get("slot"):
                iter_slots.append((f"sim{n['slot']}", n))
    else:
        for k, v in data.items():
            if k.startswith("sim") and isinstance(v, dict):
                iter_slots.append((k, v))

    for key, slot in iter_slots:
        cell = slot.get("cell_info") if isinstance(slot.get("cell_info"), dict) else {}
        # Quectel reports the four indicators inside cell_info on
        # GL.iNet firmware. They might appear at the top level on
        # other vendors - we check both.
        for src_key, dest_suffix in (
            ("rsrp", "rsrp_dbm"),
            ("rsrq", "rsrq_db"),
            ("sinr", "sinr_db"),
            ("rssi", "rssi_dbm"),
        ):
            v = cell.get(src_key) if cell else None
            if v is None:
                v = slot.get(src_key)
            if v is None:
                continue
            try:
                out[f"signal.{key}.{dest_suffix}"] = float(v)
            except (TypeError, ValueError):
                pass
    return out
