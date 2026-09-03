-- glintd storage schema. One database, three tiered tables.
-- Hot tier holds raw samples for fast inserts; warm/cool hold
-- min/avg/max aggregates that the rollup populates from the
-- previous tier. The app's get_history call picks the narrowest
-- tier that covers the requested window.
--
-- Schema is deliberately simple: no per-metric tables. A single
-- (metric, ts) compound key keeps cardinality bounded - even a
-- well-equipped router (battery + 2 SIMs + 5 ifaces + 4 pings)
-- emits ~30 series at 15 s, which is 7 200 rows/h hot, 3 600/h
-- warm, 720/h cool. Trivially queryable.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;

CREATE TABLE IF NOT EXISTS samples_hot (
    metric  TEXT    NOT NULL,
    ts      INTEGER NOT NULL,   -- unix seconds (15 s grid)
    value   REAL    NOT NULL,
    PRIMARY KEY (metric, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS samples_warm (
    metric  TEXT    NOT NULL,
    ts      INTEGER NOT NULL,   -- unix seconds (60 s grid)
    min_v   REAL    NOT NULL,
    avg_v   REAL    NOT NULL,
    max_v   REAL    NOT NULL,
    PRIMARY KEY (metric, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS samples_cool (
    metric  TEXT    NOT NULL,
    ts      INTEGER NOT NULL,   -- unix seconds (300 s grid)
    min_v   REAL    NOT NULL,
    avg_v   REAL    NOT NULL,
    max_v   REAL    NOT NULL,
    PRIMARY KEY (metric, ts)
) WITHOUT ROWID;

-- Capability cache. Populated once at daemon start; the JSON
-- payload is the same shape `Capabilities.to_json()` produces.
-- Used by RPC.list_metrics + by get_history's tier-auto math.
CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

-- Device tokens for APNs. Stored here so the daemon survives
-- restart with the registered tokens intact. router_id stays
-- stable across token rotations - clients re-register their token
-- on every cold start, idempotently.
CREATE TABLE IF NOT EXISTS push_tokens (
    token       TEXT PRIMARY KEY,
    platform    TEXT NOT NULL,    -- "ios" / "macos"
    bundle_id   TEXT NOT NULL,    -- com.nakitel.glint / .glintlite
    registered  INTEGER NOT NULL, -- unix seconds
    -- Comma-separated event ids the user has muted (e.g.
    -- "battery.low,sim.switched"). NULL/empty = receive all.
    -- Updated via set_push_preferences without re-registering
    -- the token.
    disabled_events TEXT,
    -- APNs environment this token belongs to: "production" /
    -- "development". NULL is treated as "production" by every
    -- reader (COALESCE). store.py also ADD COLUMNs this for DBs
    -- created before the column existed.
    environment TEXT
);

-- Existing-installs migration: adds the column if the table was
-- created by an older schema. Older sqlite returns "duplicate
-- column" if it's already there; we wrap in a no-op pragma so
-- the executescript path doesn't bail.
CREATE TABLE IF NOT EXISTS _schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at INTEGER NOT NULL
);
