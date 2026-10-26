-- Silver: типизированные ежедневные снимки из bronze.raw_response.
-- Ответственность слоя: типы, чистка значений, одна строка на (дата, сущность).
-- Скрипт безопасен для повторного запуска (IF NOT EXISTS / OR REPLACE).

CREATE SCHEMA IF NOT EXISTS silver;

-- ---------------------------------------------------------------------------
-- Безопасные приведения типов: при неверном значении возвращают NULL,
-- а не прерывают загрузку. Количество таких значений считают DQ-правила.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION silver.safe_int(t text) RETURNS bigint
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
  RETURN t::bigint;
EXCEPTION WHEN others THEN
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION silver.safe_numeric(t text) RETURNS numeric
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
  RETURN t::numeric;
EXCEPTION WHEN others THEN
  RETURN NULL;
END $$;

-- Только полная дата YYYY-MM-DD. Значения вида '2021', '2021-05', '0000-00-00' дают NULL.
CREATE OR REPLACE FUNCTION silver.safe_date(t text) RETURNS date
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
  IF t IS NULL OR t !~ '^\d{4}-\d{2}-\d{2}$' OR t = '0000-00-00' THEN
    RETURN NULL;
  END IF;
  RETURN t::date;
EXCEPTION WHEN others THEN
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION silver.safe_bool(t text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE lower(t) WHEN 'true' THEN TRUE WHEN 'false' THEN FALSE END
$$;

-- popularity допустима только в диапазоне 0..100, иначе NULL
CREATE OR REPLACE FUNCTION silver.safe_popularity(t text) RETURNS integer
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN silver.safe_int(t) BETWEEN 0 AND 100 THEN silver.safe_int(t)::integer END
$$;

-- ---------------------------------------------------------------------------
-- Deezer
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.dz_chart_position (
  snapshot_date    date    NOT NULL,
  genre_id         integer NOT NULL,       -- 0 = общий чарт
  deezer_track_id  text    NOT NULL,
  chart_position   integer,
  deezer_artist_id text,
  title            text,
  bronze_run_id    bigint,                 -- ссылка на запуск загрузки (lineage)
  silver_loaded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, genre_id, deezer_track_id)
);

CREATE TABLE IF NOT EXISTS silver.dz_track_daily (
  snapshot_date    date NOT NULL,
  deezer_track_id  text NOT NULL,
  isrc             text,
  title            text,
  deezer_rank      integer,
  release_date     date,
  bpm              numeric,                -- 0 заменяется на NULL (нет данных)
  explicit         boolean,
  duration_sec     integer,
  deezer_artist_id text,
  deezer_album_id  text,
  bronze_run_id    bigint,
  silver_loaded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, deezer_track_id)
);

CREATE TABLE IF NOT EXISTS silver.dz_artist_daily (
  snapshot_date    date NOT NULL,
  deezer_artist_id text NOT NULL,
  name             text,
  nb_fan           bigint,
  nb_album         integer,
  bronze_run_id    bigint,
  silver_loaded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, deezer_artist_id)
);

-- ---------------------------------------------------------------------------
-- Spotify
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.sp_track_daily (
  snapshot_date    date NOT NULL,
  spotify_track_id text NOT NULL,
  isrc             text,
  name             text,
  popularity       integer,                -- 0..100, иначе NULL
  album_id         text,
  artist_id        text,                   -- первый артист трека
  duration_ms      integer,
  explicit         boolean,
  bronze_run_id    bigint,
  silver_loaded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, spotify_track_id)
);

CREATE TABLE IF NOT EXISTS silver.sp_artist_daily (
  snapshot_date     date NOT NULL,
  spotify_artist_id text NOT NULL,
  name              text,
  popularity        integer,
  followers         bigint,
  genres            text[],
  bronze_run_id     bigint,
  silver_loaded_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, spotify_artist_id)
);

CREATE TABLE IF NOT EXISTS silver.sp_album_daily (
  snapshot_date      date NOT NULL,
  spotify_album_id   text NOT NULL,
  name               text,
  album_type         text,
  popularity         integer,
  release_date_raw   text,                 -- как пришло: '2021', '2021-05', '2021-05-17'
  release_date       date,                 -- заполняется только при полной дате
  total_tracks       integer,
  artist_id          text,
  bronze_run_id      bigint,
  silver_loaded_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, spotify_album_id)
);

-- Индексы под выборку рядов по одной сущности (PK оптимизирован под выборку по дате)
CREATE INDEX IF NOT EXISTS ix_dz_track_id_date   ON silver.dz_track_daily   (deezer_track_id, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_dz_artist_id_date  ON silver.dz_artist_daily  (deezer_artist_id, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_sp_track_id_date   ON silver.sp_track_daily   (spotify_track_id, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_sp_artist_id_date  ON silver.sp_artist_daily  (spotify_artist_id, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_sp_album_id_date   ON silver.sp_album_daily   (spotify_album_id, snapshot_date);
