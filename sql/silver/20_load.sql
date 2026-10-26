-- Загрузка Silver из Bronze. Вызов: SELECT * FROM silver.load_all();
-- Параметр p_from ограничивает пересчёт датами >= p_from (NULL = всё).
-- Идемпотентность: ON CONFLICT DO UPDATE. Повторный запуск даёт то же состояние,
-- а колонка o_new показывает, сколько строк реально вставлено (при повторе = 0).

CREATE OR REPLACE FUNCTION silver.load_all(p_from date DEFAULT NULL)
RETURNS TABLE (o_target text, o_new bigint, o_total bigint)
LANGUAGE plpgsql AS $$
DECLARE
  v_new bigint;
  v_all bigint;
BEGIN
  -- ---------------- Deezer: позиции в чартах ----------------
  WITH ins AS (
    INSERT INTO silver.dz_chart_position
      (snapshot_date, genre_id, deezer_track_id, chart_position, deezer_artist_id, title, bronze_run_id)
    SELECT DISTINCT ON (b.snapshot_date, g.genre_id, t->>'id')
           b.snapshot_date,
           g.genre_id,
           t->>'id',
           silver.safe_int(t->>'position')::integer,
           t->'artist'->>'id',
           t->>'title',
           b.run_id
    FROM bronze.raw_response b
    CROSS JOIN LATERAL (
      SELECT silver.safe_int(replace(b.entity_id, 'genre_', ''))::integer AS genre_id
    ) g
    CROSS JOIN LATERAL jsonb_array_elements(
      CASE WHEN jsonb_typeof(b.payload->'data') = 'array' THEN b.payload->'data' ELSE '[]'::jsonb END
    ) AS t
    WHERE b.source = 'deezer' AND b.entity_type = 'chart' AND b.http_status = 200
      AND g.genre_id IS NOT NULL
      AND t->>'id' IS NOT NULL
      AND (p_from IS NULL OR b.snapshot_date >= p_from)
    ORDER BY b.snapshot_date, g.genre_id, t->>'id', silver.safe_int(t->>'position')
    ON CONFLICT (snapshot_date, genre_id, deezer_track_id) DO UPDATE SET
      chart_position   = EXCLUDED.chart_position,
      deezer_artist_id = EXCLUDED.deezer_artist_id,
      title            = EXCLUDED.title,
      bronze_run_id    = EXCLUDED.bronze_run_id,
      silver_loaded_at = now()
    RETURNING (xmax = 0) AS inserted
  )
  SELECT count(*) FILTER (WHERE inserted), count(*) INTO v_new, v_all FROM ins;
  o_target := 'silver.dz_chart_position'; o_new := v_new; o_total := v_all; RETURN NEXT;

  -- ---------------- Deezer: треки ----------------
  WITH ins AS (
    INSERT INTO silver.dz_track_daily
      (snapshot_date, deezer_track_id, isrc, title, deezer_rank, release_date, bpm,
       explicit, duration_sec, deezer_artist_id, deezer_album_id, bronze_run_id)
    SELECT b.snapshot_date,
           b.entity_id,
           NULLIF(b.payload->>'isrc', ''),
           b.payload->>'title',
           silver.safe_int(b.payload->>'rank')::integer,
           silver.safe_date(b.payload->>'release_date'),
           NULLIF(silver.safe_numeric(b.payload->>'bpm'), 0),
           silver.safe_bool(b.payload->>'explicit_lyrics'),
           silver.safe_int(b.payload->>'duration')::integer,
           b.payload->'artist'->>'id',
           b.payload->'album'->>'id',
           b.run_id
    FROM bronze.raw_response b
    WHERE b.source = 'deezer' AND b.entity_type = 'track' AND b.http_status = 200
      AND (p_from IS NULL OR b.snapshot_date >= p_from)
    ON CONFLICT (snapshot_date, deezer_track_id) DO UPDATE SET
      isrc             = EXCLUDED.isrc,
      title            = EXCLUDED.title,
      deezer_rank      = EXCLUDED.deezer_rank,
      release_date     = EXCLUDED.release_date,
      bpm              = EXCLUDED.bpm,
      explicit         = EXCLUDED.explicit,
      duration_sec     = EXCLUDED.duration_sec,
      deezer_artist_id = EXCLUDED.deezer_artist_id,
      deezer_album_id  = EXCLUDED.deezer_album_id,
      bronze_run_id    = EXCLUDED.bronze_run_id,
      silver_loaded_at = now()
    RETURNING (xmax = 0) AS inserted
  )
  SELECT count(*) FILTER (WHERE inserted), count(*) INTO v_new, v_all FROM ins;
  o_target := 'silver.dz_track_daily'; o_new := v_new; o_total := v_all; RETURN NEXT;

  -- ---------------- Deezer: артисты ----------------
  WITH ins AS (
    INSERT INTO silver.dz_artist_daily
      (snapshot_date, deezer_artist_id, name, nb_fan, nb_album, bronze_run_id)
    SELECT b.snapshot_date,
           b.entity_id,
           b.payload->>'name',
           silver.safe_int(b.payload->>'nb_fan'),
           silver.safe_int(b.payload->>'nb_album')::integer,
           b.run_id
    FROM bronze.raw_response b
    WHERE b.source = 'deezer' AND b.entity_type = 'artist' AND b.http_status = 200
      AND (p_from IS NULL OR b.snapshot_date >= p_from)
    ON CONFLICT (snapshot_date, deezer_artist_id) DO UPDATE SET
      name             = EXCLUDED.name,
      nb_fan           = EXCLUDED.nb_fan,
      nb_album         = EXCLUDED.nb_album,
      bronze_run_id    = EXCLUDED.bronze_run_id,
      silver_loaded_at = now()
    RETURNING (xmax = 0) AS inserted
  )
  SELECT count(*) FILTER (WHERE inserted), count(*) INTO v_new, v_all FROM ins;
  o_target := 'silver.dz_artist_daily'; o_new := v_new; o_total := v_all; RETURN NEXT;

  -- ---------------- Spotify: треки ----------------
  WITH ins AS (
    INSERT INTO silver.sp_track_daily
      (snapshot_date, spotify_track_id, isrc, name, popularity, album_id, artist_id,
       duration_ms, explicit, bronze_run_id)
    SELECT b.snapshot_date,
           b.entity_id,
           NULLIF(b.payload->'external_ids'->>'isrc', ''),
           b.payload->>'name',
           silver.safe_popularity(b.payload->>'popularity'),
           b.payload->'album'->>'id',
           b.payload->'artists'->0->>'id',
           silver.safe_int(b.payload->>'duration_ms')::integer,
           silver.safe_bool(b.payload->>'explicit'),
           b.run_id
    FROM bronze.raw_response b
    WHERE b.source = 'spotify' AND b.entity_type = 'track' AND b.http_status = 200
      AND (p_from IS NULL OR b.snapshot_date >= p_from)
    ON CONFLICT (snapshot_date, spotify_track_id) DO UPDATE SET
      isrc             = EXCLUDED.isrc,
      name             = EXCLUDED.name,
      popularity       = EXCLUDED.popularity,
      album_id         = EXCLUDED.album_id,
      artist_id        = EXCLUDED.artist_id,
      duration_ms      = EXCLUDED.duration_ms,
      explicit         = EXCLUDED.explicit,
      bronze_run_id    = EXCLUDED.bronze_run_id,
      silver_loaded_at = now()
    RETURNING (xmax = 0) AS inserted
  )
  SELECT count(*) FILTER (WHERE inserted), count(*) INTO v_new, v_all FROM ins;
  o_target := 'silver.sp_track_daily'; o_new := v_new; o_total := v_all; RETURN NEXT;

  -- ---------------- Spotify: артисты ----------------
  WITH ins AS (
    INSERT INTO silver.sp_artist_daily
      (snapshot_date, spotify_artist_id, name, popularity, followers, genres, bronze_run_id)
    SELECT b.snapshot_date,
           b.entity_id,
           b.payload->>'name',
           silver.safe_popularity(b.payload->>'popularity'),
           silver.safe_int(b.payload->'followers'->>'total'),
           CASE WHEN jsonb_typeof(b.payload->'genres') = 'array'
                THEN ARRAY(SELECT jsonb_array_elements_text(b.payload->'genres'))
                ELSE ARRAY[]::text[] END,
           b.run_id
    FROM bronze.raw_response b
    WHERE b.source = 'spotify' AND b.entity_type = 'artist' AND b.http_status = 200
      AND (p_from IS NULL OR b.snapshot_date >= p_from)
    ON CONFLICT (snapshot_date, spotify_artist_id) DO UPDATE SET
      name             = EXCLUDED.name,
      popularity       = EXCLUDED.popularity,
      followers        = EXCLUDED.followers,
      genres           = EXCLUDED.genres,
      bronze_run_id    = EXCLUDED.bronze_run_id,
      silver_loaded_at = now()
    RETURNING (xmax = 0) AS inserted
  )
  SELECT count(*) FILTER (WHERE inserted), count(*) INTO v_new, v_all FROM ins;
  o_target := 'silver.sp_artist_daily'; o_new := v_new; o_total := v_all; RETURN NEXT;

  -- ---------------- Spotify: альбомы ----------------
  WITH ins AS (
    INSERT INTO silver.sp_album_daily
      (snapshot_date, spotify_album_id, name, album_type, popularity,
       release_date_raw, release_date, total_tracks, artist_id, bronze_run_id)
    SELECT b.snapshot_date,
           b.entity_id,
           b.payload->>'name',
           b.payload->>'album_type',
           silver.safe_popularity(b.payload->>'popularity'),
           b.payload->>'release_date',
           silver.safe_date(b.payload->>'release_date'),
           silver.safe_int(b.payload->>'total_tracks')::integer,
           b.payload->'artists'->0->>'id',
           b.run_id
    FROM bronze.raw_response b
    WHERE b.source = 'spotify' AND b.entity_type = 'album' AND b.http_status = 200
      AND (p_from IS NULL OR b.snapshot_date >= p_from)
    ON CONFLICT (snapshot_date, spotify_album_id) DO UPDATE SET
      name             = EXCLUDED.name,
      album_type       = EXCLUDED.album_type,
      popularity       = EXCLUDED.popularity,
      release_date_raw = EXCLUDED.release_date_raw,
      release_date     = EXCLUDED.release_date,
      total_tracks     = EXCLUDED.total_tracks,
      artist_id        = EXCLUDED.artist_id,
      bronze_run_id    = EXCLUDED.bronze_run_id,
      silver_loaded_at = now()
    RETURNING (xmax = 0) AS inserted
  )
  SELECT count(*) FILTER (WHERE inserted), count(*) INTO v_new, v_all FROM ins;
  o_target := 'silver.sp_album_daily'; o_new := v_new; o_total := v_all; RETURN NEXT;
END $$;
