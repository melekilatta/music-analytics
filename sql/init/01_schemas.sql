CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;
CREATE SCHEMA IF NOT EXISTS meta;

CREATE TABLE IF NOT EXISTS meta.ingestion_log (
  run_id          BIGSERIAL PRIMARY KEY,
  source          TEXT NOT NULL,
  endpoint        TEXT NOT NULL,
  started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at     TIMESTAMPTZ,
  status          TEXT NOT NULL DEFAULT 'running',  -- running / success / partial / failed
  requests_total  INT NOT NULL DEFAULT 0,
  requests_failed INT NOT NULL DEFAULT 0,
  rows_inserted   INT NOT NULL DEFAULT 0,
  error           TEXT
);

CREATE TABLE IF NOT EXISTS bronze.raw_response (
  source        TEXT NOT NULL,
  entity_type   TEXT NOT NULL,
  entity_id     TEXT NOT NULL,
  snapshot_date DATE NOT NULL,
  run_id        BIGINT REFERENCES meta.ingestion_log(run_id),
  http_status   INT,
  payload       JSONB,
  loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source, entity_type, entity_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS meta.tracked_tracks (
  deezer_track_id   TEXT PRIMARY KEY,
  isrc              TEXT,
  spotify_track_id  TEXT,
  deezer_artist_id  TEXT,
  spotify_artist_id TEXT,
  spotify_album_id  TEXT,
  title             TEXT,
  first_seen_date   DATE NOT NULL DEFAULT CURRENT_DATE,
  last_chart_date   DATE NOT NULL DEFAULT CURRENT_DATE,
  is_active         BOOLEAN NOT NULL DEFAULT TRUE,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);