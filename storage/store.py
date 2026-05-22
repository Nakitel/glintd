"""Storage facade. Single SQLite file, tiered ring tables.

The daemon writes only to the hot tier. A `roll()` task -
scheduled by the main loop every 60 s - promotes hot rows older
than 1 h into warm, and warm rows older than 6 h into cool. After
each roll, expired rows are trimmed:

    hot:  samples older than 1 h        → deleted (already in warm)
    warm: samples older than 6 h        → deleted (already in cool)
    cool: samples older than 24 h       → deleted (retention floor)

Aggregation is plain SQL. Each warm row holds (min, avg, max) over
the four 15 s hot samples it covers; each cool row holds (min,
avg, max) over the five 1 m warm rows it covers. Min/max preserve
spike events that a pure mean would smooth out.
"""
from __future__ import annotations
import os
import sqlite3
import time
from typing import Optional

# Tier definitions. (resolution_seconds, retention_seconds).
# The daemon's roll task uses these to decide what to promote/expire.
HOT_RES_S, HOT_KEEP_S   = 15,  60 * 60       # 1 h
WARM_RES_S, WARM_KEEP_S = 60,  6 * 3600       # 6 h
COOL_RES_S, COOL_KEEP_S = 300, 24 * 3600      # 24 h


class Store:
    def __init__(self, path: str, schema_path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with open(schema_path) as f:
            self.conn.executescript(f.read())
        # The DB lives on tmpfs (/tmp), so the WAL is RAM too. The
        # default autocheckpoint (1000 pages ≈ 4 MB) let the WAL grow
        # larger than the main DB itself (observed 4.3 MB WAL vs 3 MB
        # DB), doubling the daemon's RAM footprint for the same data.
        # Checkpoint more eagerly - 256 pages ≈ 1 MB - to fold the WAL
        # back into the main file. `journal_size_limit` then truncates
        # the WAL file back down after each checkpoint (without it the
        # file stays allocated at its high-water mark and the RAM is
        # never reclaimed). Negligible cost on tmpfs (no real fsync).
        self.conn.execute("PRAGMA wal_autocheckpoint=256")
        self.conn.execute("PRAGMA journal_size_limit=1048576")
        # Idempotent column adds for tables that pre-date this
        # field. Older `push_tokens` rows lack `disabled_events`;
        # ALTER TABLE ADD COLUMN is the canonical sqlite migration.
        self._add_column_if_missing("push_tokens", "disabled_events", "TEXT")

    def _add_column_if_missing(self, table: str, column: str,
                               type_decl: str) -> None:
        cur = self.conn.execute(f"PRAGMA table_info({table})")
        existing = {r["name"] for r in cur}
        if column not in existing:
            self.conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {type_decl}")

    # ---- writes ----

    def write_hot(self, samples: dict[str, float], ts: Optional[int] = None) -> None:
        """Insert a batch of samples into the hot tier.

        Uses the raw second-resolution wall-clock ts. We used to
        quantise to a 15 s grid here, but that lost samples when a
        slow probe (pings, 2 s) shifted the throughput collector's
        firing times into a 13 s / 17 s alternation - pairs of
        consecutive ticks landed in the same 15 s bucket and
        INSERT OR REPLACE merged them, halving effective cadence.
        Storing raw ts keeps every tick. Rollup math only needs
        integer arithmetic on `ts // bucket_s * bucket_s` and
        works the same on un-quantised values."""
        if not samples:
            return
        if ts is None:
            ts = int(time.time())
        rows = [(name, ts, float(v)) for name, v in samples.items()]
        self.conn.executemany(
            "INSERT OR REPLACE INTO samples_hot(metric, ts, value) "
            "VALUES (?, ?, ?)",
            rows,
        )

    # ---- rollup ----

    def roll(self) -> None:
        """Promote eligible rows hot→warm and warm→cool, then
        expire stale rows. Idempotent: running it twice in the
        same minute produces the same end state because the
        target tables use INSERT OR REPLACE on (metric, ts)."""
        now = int(time.time())
        # Hot → warm. We aggregate hot rows that are at least
        # `WARM_RES_S` old (i.e. their warm bucket is closed and
        # can't receive more samples).
        warm_cutoff = ((now - WARM_RES_S) // WARM_RES_S) * WARM_RES_S
        self.conn.execute(f"""
            INSERT OR REPLACE INTO samples_warm(metric, ts, min_v, avg_v, max_v)
            SELECT metric,
                   (ts / {WARM_RES_S}) * {WARM_RES_S} AS bucket_ts,
                   MIN(value), AVG(value), MAX(value)
            FROM samples_hot
            WHERE ts < ?
            GROUP BY metric, bucket_ts
        """, (warm_cutoff,))

        # Warm → cool.
        cool_cutoff = ((now - COOL_RES_S) // COOL_RES_S) * COOL_RES_S
        self.conn.execute(f"""
            INSERT OR REPLACE INTO samples_cool(metric, ts, min_v, avg_v, max_v)
            SELECT metric,
                   (ts / {COOL_RES_S}) * {COOL_RES_S} AS bucket_ts,
                   MIN(min_v), AVG(avg_v), MAX(max_v)
            FROM samples_warm
            WHERE ts < ?
            GROUP BY metric, bucket_ts
        """, (cool_cutoff,))

        # Trim expired rows. We keep one bucket past the retention
        # window so the app's "request last N seconds" queries
        # never see the boundary tear off a fresh sample.
        self.conn.execute(
            "DELETE FROM samples_hot  WHERE ts < ?", (now - HOT_KEEP_S,))
        self.conn.execute(
            "DELETE FROM samples_warm WHERE ts < ?", (now - WARM_KEEP_S,))
        self.conn.execute(
            "DELETE FROM samples_cool WHERE ts < ?", (now - COOL_KEEP_S,))

    # ---- reads ----

    def query(self, metric: str, since: int,
              tier: str = "auto") -> list[dict]:
        """Return rows for (metric, ts ≥ since), oldest first.

        tier: "hot" / "warm" / "cool" / "auto". Auto picks the
        narrowest tier that *can* cover the window. If the picked
        tier is empty (fresh daemon - rollup hasn't moved any rows
        yet), falls back finer-and-finer until something hits, so
        a `since=0` call right after install still returns the
        hot-tier samples instead of an empty result.
        """
        if tier == "auto":
            for candidate in self._auto_tier_chain(since):
                rows = self._query_tier(metric, since, candidate)
                if rows:
                    return rows
            return []
        if tier == "hot":
            cur = self.conn.execute(
                "SELECT ts, value FROM samples_hot "
                "WHERE metric = ? AND ts >= ? ORDER BY ts",
                (metric, since))
            return [{"ts": r["ts"], "value": r["value"]} for r in cur]
        else:
            table = f"samples_{tier}"
            cur = self.conn.execute(
                f"SELECT ts, min_v, avg_v, max_v FROM {table} "
                f"WHERE metric = ? AND ts >= ? ORDER BY ts",
                (metric, since))
            return [{"ts": r["ts"],
                     "min": r["min_v"],
                     "avg": r["avg_v"],
                     "max": r["max_v"]} for r in cur]

    def _auto_tier(self, since: int) -> str:
        # Kept for symmetry / explicit-tier callers; the auto path
        # uses _auto_tier_chain below for the empty-fallback case.
        now = int(time.time())
        window = now - since
        if window <= HOT_KEEP_S:
            return "hot"
        if window <= WARM_KEEP_S:
            return "warm"
        return "cool"

    def _auto_tier_chain(self, since: int) -> list[str]:
        """Tiers to try in order of preference. Picks the
        narrowest tier that fits the window first, then falls
        finer-finer for empty-tier robustness."""
        now = int(time.time())
        window = now - since
        if window <= HOT_KEEP_S:
            return ["hot"]
        if window <= WARM_KEEP_S:
            return ["warm", "hot"]
        return ["cool", "warm", "hot"]

    def _query_tier(self, metric: str, since: int, tier: str) -> list[dict]:
        if tier == "hot":
            cur = self.conn.execute(
                "SELECT ts, value FROM samples_hot "
                "WHERE metric = ? AND ts >= ? ORDER BY ts",
                (metric, since))
            return [{"ts": r["ts"], "value": r["value"]} for r in cur]
        table = f"samples_{tier}"
        cur = self.conn.execute(
            f"SELECT ts, min_v, avg_v, max_v FROM {table} "
            f"WHERE metric = ? AND ts >= ? ORDER BY ts",
            (metric, since))
        return [{"ts": r["ts"],
                 "min": r["min_v"],
                 "avg": r["avg_v"],
                 "max": r["max_v"]} for r in cur]

    def latest_per_metric(self) -> dict[str, tuple[int, float]]:
        """Return the most recent (ts, value) for every metric the
        daemon has hot data for. One query, indexed by the
        (metric, ts) primary key - the engine resolves it as a
        per-group MAX in one scan. Powers `get_snapshot` so the
        app can populate every chart's "current" reading from
        a single round-trip instead of N per-metric calls.
        """
        cur = self.conn.execute(
            "SELECT metric, ts, value FROM samples_hot "
            "WHERE (metric, ts) IN "
            "  (SELECT metric, MAX(ts) FROM samples_hot GROUP BY metric)"
        )
        return {r["metric"]: (int(r["ts"]), float(r["value"])) for r in cur}

    def list_metrics(self) -> list[str]:
        """Distinct metric names that have any data right now.
        Source of truth for the RPC list_metrics. Hot tier alone
        is enough - anything dormant for >1 h has fallen out of
        the live data anyway."""
        cur = self.conn.execute(
            "SELECT DISTINCT metric FROM samples_hot ORDER BY metric")
        return [r["metric"] for r in cur]

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (key, value))

    def get_meta(self, key: str) -> Optional[str]:
        cur = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None
