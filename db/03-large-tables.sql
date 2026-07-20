-- ---------------------------------------------------------------------------
-- Large generated tables, layered on top of Pagila.
--
-- WHY: Pagila gives realistic relational structure (FKs, views, 22 tables) but
-- its tables are tiny. Blast-radius simulation ("this UPDATE would hit 2.3M
-- rows") and latency benchmarks are only meaningful at real volume. These two
-- tables supply that volume.
--
-- This runs once, at first container init (empty data dir), after Pagila
-- schema (01) and data (02). Expect ~1-2 min to generate + index.
-- ---------------------------------------------------------------------------

\echo 'Generating large tables (this runs once on first init)...'

-- ~3M rows, with a REAL foreign key into Pagila's customer table (599 rows).
-- Exercises FK-aware classification, cascade reasoning, and large UPDATE/DELETE
-- blast radius scoped by customer.
CREATE TABLE app_event (
    event_id    bigserial PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES customer (customer_id),
    event_type  text NOT NULL,
    amount      numeric(10, 2) NOT NULL,
    created_at  timestamptz NOT NULL,
    metadata    jsonb NOT NULL DEFAULT '{}'::jsonb
);

INSERT INTO app_event (customer_id, event_type, amount, created_at, metadata)
SELECT
    (random() * 598 + 1)::int AS customer_id,
    (ARRAY['login', 'purchase', 'refund', 'view', 'logout'])[(random() * 4 + 1)::int],
    (random() * 1000)::numeric(10, 2),
    now() - (random() * 365 * 24 * 3600) * interval '1 second',
    jsonb_build_object('score', (random() * 100)::int)
FROM generate_series(1, 3000000);

-- ~2M rows, independent (no FK). A high-volume "telemetry" table: the kind of
-- table where a careless unbounded UPDATE/DELETE is catastrophic.
CREATE TABLE metric_sample (
    id          bigserial PRIMARY KEY,
    sensor_id   integer NOT NULL,
    value       double precision NOT NULL,
    recorded_at timestamptz NOT NULL
);

INSERT INTO metric_sample (sensor_id, value, recorded_at)
SELECT
    (random() * 5000 + 1)::int AS sensor_id,
    random() * 1000.0,
    now() - (random() * 90 * 24 * 3600) * interval '1 second'
FROM generate_series(1, 2000000);

-- Indexes so EXPLAIN/blast-radius simulation produces realistic query plans
-- and row estimates rather than always seq-scanning.
CREATE INDEX idx_app_event_customer ON app_event (customer_id);
CREATE INDEX idx_app_event_created ON app_event (created_at);
CREATE INDEX idx_metric_sample_sensor ON metric_sample (sensor_id);

-- Refresh planner statistics so EXPLAIN estimates are accurate immediately.
ANALYZE app_event;
ANALYZE metric_sample;

\echo 'Large tables ready: app_event (~3M rows), metric_sample (~2M rows).'
