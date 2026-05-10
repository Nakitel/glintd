"""Metric collectors. Each module implements:

    NAME            : str            - short id, also used in DB
    INTERVAL_SEC    : int            - how often to sample
    requires(caps)  : (Capabilities) → bool
    collect(caps)   : (Capabilities) → dict[str, float]

`collect` returns a flat dict mapping metric-name → value. The
daemon batch-writes those into the SQLite hot tier under their
metric names. A collector may emit multiple series per call (e.g.
`battery.pct` + `battery.voltage` + `battery.temp_c` from the
same battery sample), reducing process spawn overhead.

Each module is small and self-contained: capabilities-aware, no
shared state. Adding a new metric is a new file in this folder
with the four contract symbols above - `daemon.py` discovers
them via `pkgutil.iter_modules`.
"""
from __future__ import annotations
import importlib
import pkgutil
from typing import Iterable, Protocol, Any


class Collector(Protocol):
    NAME: str
    INTERVAL_SEC: int
    @staticmethod
    def requires(caps: Any) -> bool: ...
    @staticmethod
    def collect(caps: Any) -> dict[str, float]: ...


def discover() -> list[Any]:
    """Return all collector modules under this package. Order is
    deterministic (alphabetical by module name) - keeps the
    daemon's startup log tidy and tests reproducible."""
    out = []
    for info in sorted(pkgutil.iter_modules(__path__),
                       key=lambda m: m.name):
        mod = importlib.import_module(f"{__name__}.{info.name}")
        # Soft contract - log + skip modules that don't expose
        # the four required symbols, instead of crashing the
        # whole daemon for one bad file.
        if all(hasattr(mod, s)
               for s in ("NAME", "INTERVAL_SEC", "requires", "collect")):
            out.append(mod)
    return out
