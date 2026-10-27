-- Mono Edge Signal Analytics: medallion-style schema for Postgres / Supabase.
--
-- bronze_readings : raw 20 ms PLC samples, append-only, partitioned by day
-- silver_signal_1s: 1-second rollups per mill/signal (avg/min/max/std/n)
-- gold_alerts     : detected incidents with reason and affected signals
-- gold_alert_signals: per-signal evidence for each alert

-- Supabase already has the `authenticated` role; create it on plain Postgres (local dev).
do $$ begin
    if not exists (select from pg_roles where rolname = 'authenticated') then
        create role authenticated nologin;
    end if;
end $$;

create schema if not exists mill;

-- ---------- BRONZE: raw readings -------------------------------------------
create table if not exists mill.bronze_readings (
    mill_id   text             not null,
    signal    text             not null,
    ts        timestamptz      not null,
    value     double precision not null,
    unit      text,
    quality   smallint         not null default 0,   -- 0 = good
    ingested_at timestamptz    not null default now()
) partition by range (ts);

-- Helper to create daily partitions (call from the ingest job).
create or replace function mill.ensure_partition(day date) returns void as $$
declare
    part_name text := format('bronze_readings_%s', to_char(day, 'YYYYMMDD'));
begin
    if to_regclass(format('mill.%I', part_name)) is null then
        execute format(
            'create table mill.%I partition of mill.bronze_readings
             for values from (%L) to (%L)',
            part_name, day, day + 1);
        execute format(
            'create index on mill.%I (mill_id, signal, ts)', part_name);
    end if;
end;
$$ language plpgsql;

-- ---------- SILVER: 1-second rollups ---------------------------------------
create table if not exists mill.silver_signal_1s (
    mill_id text             not null,
    signal  text             not null,
    bucket  timestamptz      not null,
    avg     double precision not null,
    min     double precision not null,
    max     double precision not null,
    std     double precision not null,
    n       integer          not null,
    primary key (mill_id, signal, bucket)
);
create index if not exists silver_bucket_idx
    on mill.silver_signal_1s (mill_id, bucket);

-- ---------- GOLD: alerts ----------------------------------------------------
create table if not exists mill.gold_alerts (
    alert_id    bigint generated always as identity primary key,
    mill_id     text        not null,
    rule        text        not null,             -- e.g. cobble_signature
    severity    text        not null,             -- warning | critical
    started_at  timestamptz not null,
    ended_at    timestamptz,
    reason      text        not null,
    created_at  timestamptz not null default now(),
    unique (mill_id, rule, started_at)
);

create table if not exists mill.gold_alert_signals (
    alert_id bigint not null references mill.gold_alerts(alert_id) on delete cascade,
    signal   text   not null,
    peak_z   double precision,
    peak_value double precision,
    baseline_value double precision,
    primary key (alert_id, signal)
);

-- ---------- Row-level security (Supabase): authenticated users read only ----
alter table mill.bronze_readings    enable row level security;
alter table mill.silver_signal_1s   enable row level security;
alter table mill.gold_alerts        enable row level security;
alter table mill.gold_alert_signals enable row level security;

drop policy if exists read_bronze on mill.bronze_readings;
create policy read_bronze on mill.bronze_readings for select to authenticated using (true);
drop policy if exists read_silver on mill.silver_signal_1s;
create policy read_silver on mill.silver_signal_1s for select to authenticated using (true);
drop policy if exists read_alerts on mill.gold_alerts;
create policy read_alerts on mill.gold_alerts for select to authenticated using (true);
drop policy if exists read_alertsg on mill.gold_alert_signals;
create policy read_alertsg on mill.gold_alert_signals for select to authenticated using (true);
