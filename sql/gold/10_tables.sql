-- Gold: бизнес-витрины для предметного, операционного и прогнозного контуров.
-- Все витрины имеют ежедневную гранулярность и строятся только из Silver/Meta.
-- Скрипт идемпотентен и может выполняться при каждом запуске run_gold.py.

CREATE SCHEMA IF NOT EXISTS gold;

-- Исторический снимок сопоставления Deezer -> Spotify.
-- Однажды записанное сопоставление не переписывается: это сохраняет происхождение
-- ранее рассчитанных показателей, даже если каталог будет уточнён позднее.
CREATE TABLE IF NOT EXISTS gold.bridge_track_mapping_snapshot (
  snapshot_date       date        NOT NULL,
  deezer_track_id     text        NOT NULL,
  isrc                text,
  spotify_track_id    text,
  deezer_artist_id    text,
  spotify_artist_id   text,
  spotify_album_id    text,
  mapping_status      text,
  is_active           boolean     NOT NULL DEFAULT true,
  captured_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, deezer_track_id)
);

CREATE INDEX IF NOT EXISTS ix_gold_bridge_spotify_date
  ON gold.bridge_track_mapping_snapshot (spotify_track_id, snapshot_date)
  WHERE spotify_track_id IS NOT NULL;

-- Каноническая витрина треков Spotify. Гранулярность: один Spotify-трек в день.
-- Несколько Deezer-версий одного трека предварительно агрегируются в поля deezer_*;
-- поэтому популярность Spotify не размножается из-за many-to-one сопоставления.
CREATE TABLE IF NOT EXISTS gold.fact_track_daily (
  snapshot_date              date        NOT NULL,
  spotify_track_id           text        NOT NULL,
  isrc                       text,
  track_name                 text,
  spotify_popularity         integer     CHECK (spotify_popularity BETWEEN 0 AND 100),
  popularity_delta_1d        integer,
  spotify_artist_id          text,
  artist_name                text,
  artist_popularity          integer     CHECK (artist_popularity BETWEEN 0 AND 100),
  artist_followers           bigint      CHECK (artist_followers >= 0),
  artist_genres              text[],
  spotify_album_id           text,
  album_name                 text,
  album_type                 text,
  album_release_date         date,
  release_age_days           integer     CHECK (release_age_days >= 0),
  is_new_release_track       boolean     NOT NULL DEFAULT false,
  deezer_match_count         integer     NOT NULL DEFAULT 0 CHECK (deezer_match_count >= 0),
  deezer_track_ids           text[]      NOT NULL DEFAULT ARRAY[]::text[],
  deezer_rank_max            integer,
  deezer_fans_max            bigint,
  spotify_track_bronze_run_id bigint,
  spotify_artist_bronze_run_id bigint,
  spotify_album_bronze_run_id bigint,
  gold_loaded_at             timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, spotify_track_id)
);

CREATE INDEX IF NOT EXISTS ix_gold_fact_track_id_date
  ON gold.fact_track_daily (spotify_track_id, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_gold_fact_track_date_popularity
  ON gold.fact_track_daily (snapshot_date, spotify_popularity DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS ix_gold_fact_track_artist_date
  ON gold.fact_track_daily (spotify_artist_id, snapshot_date);

-- Витрина чартов. Гранулярность намеренно отличается от fact_track_daily:
-- дата x жанр Deezer x Deezer-трек. Это позволяет не потерять попадания
-- одного трека в общий и жанровые чарты.
CREATE TABLE IF NOT EXISTS gold.fact_chart_position_daily (
  snapshot_date              date        NOT NULL,
  genre_id                   integer     NOT NULL,
  deezer_track_id            text        NOT NULL,
  deezer_artist_id           text,
  track_name                 text,
  chart_position             integer     CHECK (chart_position > 0),
  chart_position_delta_1d    integer,
  deezer_rank                integer,
  bpm                        numeric,
  deezer_fans                bigint      CHECK (deezer_fans >= 0),
  spotify_track_id           text,
  spotify_popularity         integer     CHECK (spotify_popularity BETWEEN 0 AND 100),
  is_spotify_matched         boolean     NOT NULL DEFAULT false,
  chart_bronze_run_id        bigint,
  track_bronze_run_id        bigint,
  gold_loaded_at             timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, genre_id, deezer_track_id)
);

CREATE INDEX IF NOT EXISTS ix_gold_chart_track_date
  ON gold.fact_chart_position_daily (deezer_track_id, genre_id, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_gold_chart_date_position
  ON gold.fact_chart_position_daily (snapshot_date, genre_id, chart_position);

-- Витрина контрольной группы новых релизов. Гранулярность:
-- дата x альбом Spotify x трек Spotify. Треки необязательно есть в Deezer-чартах.
CREATE TABLE IF NOT EXISTS gold.fact_release_track_daily (
  snapshot_date             date        NOT NULL,
  spotify_album_id          text        NOT NULL,
  spotify_track_id          text        NOT NULL,
  spotify_artist_id         text,
  album_name                text,
  album_group               text,
  release_date              date        NOT NULL,
  release_age_days          integer     NOT NULL CHECK (release_age_days >= 0),
  track_name                text,
  track_popularity          integer     CHECK (track_popularity BETWEEN 0 AND 100),
  track_popularity_delta_1d integer,
  album_popularity          integer     CHECK (album_popularity BETWEEN 0 AND 100),
  is_active_release         boolean     NOT NULL,
  track_bronze_run_id       bigint,
  album_bronze_run_id       bigint,
  gold_loaded_at            timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, spotify_album_id, spotify_track_id)
);

CREATE INDEX IF NOT EXISTS ix_gold_release_track_date
  ON gold.fact_release_track_daily (spotify_track_id, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_gold_release_album_date
  ON gold.fact_release_track_daily (spotify_album_id, snapshot_date);

-- Ежедневные KPI для предметного дашборда.
CREATE TABLE IF NOT EXISTS gold.mart_music_daily (
  snapshot_date                 date        PRIMARY KEY,
  spotify_tracks_observed       integer     NOT NULL CHECK (spotify_tracks_observed >= 0),
  unique_artists_observed       integer     NOT NULL CHECK (unique_artists_observed >= 0),
  avg_track_popularity          numeric(8,3),
  median_track_popularity       numeric(8,3),
  top_spotify_track_id          text,
  top_spotify_track_name        text,
  top_spotify_track_popularity  integer     CHECK (top_spotify_track_popularity BETWEEN 0 AND 100),
  chart_entries                 integer     NOT NULL CHECK (chart_entries >= 0),
  chart_unique_tracks           integer     NOT NULL CHECK (chart_unique_tracks >= 0),
  chart_genres_covered          integer     NOT NULL CHECK (chart_genres_covered >= 0),
  chart_spotify_match_share     numeric(8,5),
  new_release_tracks_observed   integer     NOT NULL CHECK (new_release_tracks_observed >= 0),
  gold_loaded_at                timestamptz NOT NULL DEFAULT now()
);

-- Ежедневная операционная витрина для второго (операционного) дашборда.
CREATE TABLE IF NOT EXISTS gold.mart_pipeline_daily (
  snapshot_date               date        PRIMARY KEY,
  ingestion_runs              integer     NOT NULL CHECK (ingestion_runs >= 0),
  successful_runs             integer     NOT NULL CHECK (successful_runs >= 0),
  partial_or_failed_runs      integer     NOT NULL CHECK (partial_or_failed_runs >= 0),
  requests_total              integer     NOT NULL CHECK (requests_total >= 0),
  requests_failed             integer     NOT NULL CHECK (requests_failed >= 0),
  bronze_rows                 integer     NOT NULL CHECK (bronze_rows >= 0),
  silver_rows                 integer     NOT NULL CHECK (silver_rows >= 0),
  gold_fact_rows              integer     NOT NULL CHECK (gold_fact_rows >= 0),
  dq_error_failures           integer     NOT NULL CHECK (dq_error_failures >= 0),
  dq_warnings                 integer     NOT NULL CHECK (dq_warnings >= 0),
  latest_ingestion_finished_at timestamptz,
  gold_loaded_at              timestamptz NOT NULL DEFAULT now()
);
