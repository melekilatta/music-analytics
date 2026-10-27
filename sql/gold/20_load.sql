-- Идемпотентная загрузка Gold из Silver и Meta.
-- p_from/p_to ограничивают пересчёт включительно. При NULL пересчитывается весь доступный период.
-- Вызов: SELECT * FROM gold.load_all();

CREATE OR REPLACE FUNCTION gold.load_all(
  p_from date DEFAULT NULL,
  p_to   date DEFAULT NULL
)
RETURNS TABLE (o_target text, o_new bigint, o_total bigint)
LANGUAGE plpgsql AS $$
DECLARE
  v_new bigint;
  v_total bigint;
BEGIN
  IF p_from IS NOT NULL AND p_to IS NOT NULL AND p_from > p_to THEN
    RAISE EXCEPTION 'p_from (%) не может быть позже p_to (%)', p_from, p_to;
  END IF;

  -- Снимок каталожного сопоставления. DO NOTHING намерен: историческое
  -- сопоставление не меняется задним числом при повторном запуске.
  WITH ins AS (
    INSERT INTO gold.bridge_track_mapping_snapshot (
      snapshot_date, deezer_track_id, isrc, spotify_track_id,
      deezer_artist_id, spotify_artist_id, spotify_album_id,
      mapping_status, is_active
    )
    SELECT d.snapshot_date,
           d.deezer_track_id,
           COALESCE(d.isrc, c.isrc),
           c.spotify_track_id,
           COALESCE(d.deezer_artist_id, c.deezer_artist_id),
           c.spotify_artist_id,
           c.spotify_album_id,
           c.spotify_match_status,
           COALESCE(c.is_active, false)
    FROM silver.dz_track_daily d
    LEFT JOIN meta.tracked_tracks c
      ON c.deezer_track_id = d.deezer_track_id
    WHERE (p_from IS NULL OR d.snapshot_date >= p_from)
      AND (p_to IS NULL OR d.snapshot_date <= p_to)
    ON CONFLICT (snapshot_date, deezer_track_id) DO NOTHING
    RETURNING 1
  )
  SELECT count(*), count(*) INTO v_new, v_total FROM ins;
  o_target := 'gold.bridge_track_mapping_snapshot'; o_new := v_new; o_total := v_total; RETURN NEXT;

  -- Основная трековая витрина. dz_agg намеренно агрегирует many-to-one
  -- сопоставления до соединения со Spotify, чтобы не размножить popularity.
  WITH spotify_series AS (
    SELECT s.*,
           lag(s.snapshot_date) OVER w AS previous_snapshot_date,
           lag(s.popularity) OVER w AS previous_popularity
    FROM silver.sp_track_daily s
    WINDOW w AS (PARTITION BY s.spotify_track_id ORDER BY s.snapshot_date)
  ),
  deezer_aggregate AS (
    SELECT b.snapshot_date,
           b.spotify_track_id,
           count(*)::integer AS deezer_match_count,
           array_agg(DISTINCT b.deezer_track_id ORDER BY b.deezer_track_id) AS deezer_track_ids,
           max(d.deezer_rank) AS deezer_rank_max,
           max(a.nb_fan) AS deezer_fans_max
    FROM gold.bridge_track_mapping_snapshot b
    JOIN silver.dz_track_daily d
      ON d.snapshot_date = b.snapshot_date
     AND d.deezer_track_id = b.deezer_track_id
    LEFT JOIN silver.dz_artist_daily a
      ON a.snapshot_date = d.snapshot_date
     AND a.deezer_artist_id = d.deezer_artist_id
    WHERE b.spotify_track_id IS NOT NULL
    GROUP BY b.snapshot_date, b.spotify_track_id
  ),
  release_tracks AS (
    SELECT DISTINCT unnest(r.spotify_track_ids) AS spotify_track_id
    FROM meta.tracked_releases r
    WHERE r.spotify_track_ids IS NOT NULL
  ),
  ins AS (
    INSERT INTO gold.fact_track_daily (
      snapshot_date, spotify_track_id, isrc, track_name,
      spotify_popularity, popularity_delta_1d,
      spotify_artist_id, artist_name, artist_popularity, artist_followers, artist_genres,
      spotify_album_id, album_name, album_type, album_release_date, release_age_days,
      is_new_release_track, deezer_match_count, deezer_track_ids, deezer_rank_max, deezer_fans_max,
      spotify_track_bronze_run_id, spotify_artist_bronze_run_id, spotify_album_bronze_run_id
    )
    SELECT s.snapshot_date,
           s.spotify_track_id,
           s.isrc,
           s.name,
           s.popularity,
           CASE WHEN s.previous_snapshot_date = s.snapshot_date - 1
                THEN s.popularity - s.previous_popularity END,
           s.artist_id,
           a.name,
           a.popularity,
           a.followers,
           a.genres,
           s.album_id,
           al.name,
           al.album_type,
           al.release_date,
           CASE WHEN al.release_date IS NOT NULL AND al.release_date <= s.snapshot_date
                THEN s.snapshot_date - al.release_date END,
           EXISTS (SELECT 1 FROM release_tracks r WHERE r.spotify_track_id = s.spotify_track_id),
           COALESCE(dz.deezer_match_count, 0),
           COALESCE(dz.deezer_track_ids, ARRAY[]::text[]),
           dz.deezer_rank_max,
           dz.deezer_fans_max,
           s.bronze_run_id,
           a.bronze_run_id,
           al.bronze_run_id
    FROM spotify_series s
    LEFT JOIN silver.sp_artist_daily a
      ON a.snapshot_date = s.snapshot_date
     AND a.spotify_artist_id = s.artist_id
    LEFT JOIN silver.sp_album_daily al
      ON al.snapshot_date = s.snapshot_date
     AND al.spotify_album_id = s.album_id
    LEFT JOIN deezer_aggregate dz
      ON dz.snapshot_date = s.snapshot_date
     AND dz.spotify_track_id = s.spotify_track_id
    WHERE (p_from IS NULL OR s.snapshot_date >= p_from)
      AND (p_to IS NULL OR s.snapshot_date <= p_to)
    ON CONFLICT (snapshot_date, spotify_track_id) DO UPDATE SET
      isrc = EXCLUDED.isrc,
      track_name = EXCLUDED.track_name,
      spotify_popularity = EXCLUDED.spotify_popularity,
      popularity_delta_1d = EXCLUDED.popularity_delta_1d,
      spotify_artist_id = EXCLUDED.spotify_artist_id,
      artist_name = EXCLUDED.artist_name,
      artist_popularity = EXCLUDED.artist_popularity,
      artist_followers = EXCLUDED.artist_followers,
      artist_genres = EXCLUDED.artist_genres,
      spotify_album_id = EXCLUDED.spotify_album_id,
      album_name = EXCLUDED.album_name,
      album_type = EXCLUDED.album_type,
      album_release_date = EXCLUDED.album_release_date,
      release_age_days = EXCLUDED.release_age_days,
      is_new_release_track = EXCLUDED.is_new_release_track,
      deezer_match_count = EXCLUDED.deezer_match_count,
      deezer_track_ids = EXCLUDED.deezer_track_ids,
      deezer_rank_max = EXCLUDED.deezer_rank_max,
      deezer_fans_max = EXCLUDED.deezer_fans_max,
      spotify_track_bronze_run_id = EXCLUDED.spotify_track_bronze_run_id,
      spotify_artist_bronze_run_id = EXCLUDED.spotify_artist_bronze_run_id,
      spotify_album_bronze_run_id = EXCLUDED.spotify_album_bronze_run_id,
      gold_loaded_at = now()
    RETURNING (xmax = 0) AS inserted
  )
  SELECT count(*) FILTER (WHERE inserted), count(*) INTO v_new, v_total FROM ins;
  o_target := 'gold.fact_track_daily'; o_new := v_new; o_total := v_total; RETURN NEXT;

  -- Чартовая витрина. Она не соединяется с fact_track_daily, чтобы не потерять
  -- жанровую гранулярность и не исказить агрегаты при нескольких чартах.
  WITH chart_series AS (
    SELECT c.*,
           lag(c.snapshot_date) OVER w AS previous_snapshot_date,
           lag(c.chart_position) OVER w AS previous_chart_position
    FROM silver.dz_chart_position c
    WINDOW w AS (PARTITION BY c.genre_id, c.deezer_track_id ORDER BY c.snapshot_date)
  ),
  ins AS (
    INSERT INTO gold.fact_chart_position_daily (
      snapshot_date, genre_id, deezer_track_id, deezer_artist_id, track_name,
      chart_position, chart_position_delta_1d, deezer_rank, bpm, deezer_fans,
      spotify_track_id, spotify_popularity, is_spotify_matched,
      chart_bronze_run_id, track_bronze_run_id
    )
    SELECT c.snapshot_date,
           c.genre_id,
           c.deezer_track_id,
           COALESCE(c.deezer_artist_id, d.deezer_artist_id),
           COALESCE(c.title, d.title),
           c.chart_position,
           CASE WHEN c.previous_snapshot_date = c.snapshot_date - 1
                THEN c.previous_chart_position - c.chart_position END,
           d.deezer_rank,
           d.bpm,
           a.nb_fan,
           b.spotify_track_id,
           s.popularity,
           b.spotify_track_id IS NOT NULL,
           c.bronze_run_id,
           d.bronze_run_id
    FROM chart_series c
    LEFT JOIN silver.dz_track_daily d
      ON d.snapshot_date = c.snapshot_date
     AND d.deezer_track_id = c.deezer_track_id
    LEFT JOIN silver.dz_artist_daily a
      ON a.snapshot_date = c.snapshot_date
     AND a.deezer_artist_id = COALESCE(c.deezer_artist_id, d.deezer_artist_id)
    LEFT JOIN gold.bridge_track_mapping_snapshot b
      ON b.snapshot_date = c.snapshot_date
     AND b.deezer_track_id = c.deezer_track_id
    LEFT JOIN silver.sp_track_daily s
      ON s.snapshot_date = c.snapshot_date
     AND s.spotify_track_id = b.spotify_track_id
    WHERE (p_from IS NULL OR c.snapshot_date >= p_from)
      AND (p_to IS NULL OR c.snapshot_date <= p_to)
    ON CONFLICT (snapshot_date, genre_id, deezer_track_id) DO UPDATE SET
      deezer_artist_id = EXCLUDED.deezer_artist_id,
      track_name = EXCLUDED.track_name,
      chart_position = EXCLUDED.chart_position,
      chart_position_delta_1d = EXCLUDED.chart_position_delta_1d,
      deezer_rank = EXCLUDED.deezer_rank,
      bpm = EXCLUDED.bpm,
      deezer_fans = EXCLUDED.deezer_fans,
      spotify_track_id = EXCLUDED.spotify_track_id,
      spotify_popularity = EXCLUDED.spotify_popularity,
      is_spotify_matched = EXCLUDED.is_spotify_matched,
      chart_bronze_run_id = EXCLUDED.chart_bronze_run_id,
      track_bronze_run_id = EXCLUDED.track_bronze_run_id,
      gold_loaded_at = now()
    RETURNING (xmax = 0) AS inserted
  )
  SELECT count(*) FILTER (WHERE inserted), count(*) INTO v_new, v_total FROM ins;
  o_target := 'gold.fact_chart_position_daily'; o_new := v_new; o_total := v_total; RETURN NEXT;

  -- Контрольная группа новых релизов: все треки релиза, а не только чартовые.
  WITH spotify_series AS (
    SELECT s.*,
           lag(s.snapshot_date) OVER w AS previous_snapshot_date,
           lag(s.popularity) OVER w AS previous_popularity
    FROM silver.sp_track_daily s
    WINDOW w AS (PARTITION BY s.spotify_track_id ORDER BY s.snapshot_date)
  ),
  release_tracks AS (
    SELECT r.spotify_album_id,
           r.spotify_artist_id,
           r.name AS album_name,
           r.album_group,
           r.release_date,
           r.is_active,
           unnest(r.spotify_track_ids) AS spotify_track_id
    FROM meta.tracked_releases r
    WHERE r.spotify_track_ids IS NOT NULL
  ),
  ins AS (
    INSERT INTO gold.fact_release_track_daily (
      snapshot_date, spotify_album_id, spotify_track_id, spotify_artist_id,
      album_name, album_group, release_date, release_age_days, track_name,
      track_popularity, track_popularity_delta_1d, album_popularity,
      is_active_release, track_bronze_run_id, album_bronze_run_id
    )
    SELECT s.snapshot_date,
           r.spotify_album_id,
           r.spotify_track_id,
           r.spotify_artist_id,
           COALESCE(al.name, r.album_name),
           r.album_group,
           r.release_date,
           s.snapshot_date - r.release_date,
           s.name,
           s.popularity,
           CASE WHEN s.previous_snapshot_date = s.snapshot_date - 1
                THEN s.popularity - s.previous_popularity END,
           al.popularity,
           r.is_active,
           s.bronze_run_id,
           al.bronze_run_id
    FROM release_tracks r
    JOIN spotify_series s
      ON s.spotify_track_id = r.spotify_track_id
    LEFT JOIN silver.sp_album_daily al
      ON al.snapshot_date = s.snapshot_date
     AND al.spotify_album_id = r.spotify_album_id
    WHERE s.snapshot_date >= r.release_date
      AND (p_from IS NULL OR s.snapshot_date >= p_from)
      AND (p_to IS NULL OR s.snapshot_date <= p_to)
    ON CONFLICT (snapshot_date, spotify_album_id, spotify_track_id) DO UPDATE SET
      spotify_artist_id = EXCLUDED.spotify_artist_id,
      album_name = EXCLUDED.album_name,
      album_group = EXCLUDED.album_group,
      release_date = EXCLUDED.release_date,
      release_age_days = EXCLUDED.release_age_days,
      track_name = EXCLUDED.track_name,
      track_popularity = EXCLUDED.track_popularity,
      track_popularity_delta_1d = EXCLUDED.track_popularity_delta_1d,
      album_popularity = EXCLUDED.album_popularity,
      is_active_release = EXCLUDED.is_active_release,
      track_bronze_run_id = EXCLUDED.track_bronze_run_id,
      album_bronze_run_id = EXCLUDED.album_bronze_run_id,
      gold_loaded_at = now()
    RETURNING (xmax = 0) AS inserted
  )
  SELECT count(*) FILTER (WHERE inserted), count(*) INTO v_new, v_total FROM ins;
  o_target := 'gold.fact_release_track_daily'; o_new := v_new; o_total := v_total; RETURN NEXT;

  -- Предметные KPI. Дашборд получает готовые метрики без повторной агрегации
  -- больших фактов и может фильтровать детальные fact_* таблицы отдельно.
  WITH days AS (
    SELECT snapshot_date FROM gold.fact_track_daily
    UNION
    SELECT snapshot_date FROM gold.fact_chart_position_daily
  ),
  track_stats AS (
    SELECT snapshot_date,
           count(*)::integer AS spotify_tracks_observed,
           count(DISTINCT spotify_artist_id)::integer AS unique_artists_observed,
           avg(spotify_popularity)::numeric(8,3) AS avg_track_popularity,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY spotify_popularity)::numeric(8,3) AS median_track_popularity
    FROM gold.fact_track_daily
    GROUP BY snapshot_date
  ),
  top_track AS (
    SELECT DISTINCT ON (snapshot_date)
           snapshot_date, spotify_track_id, track_name, spotify_popularity
    FROM gold.fact_track_daily
    ORDER BY snapshot_date, spotify_popularity DESC NULLS LAST, spotify_track_id
  ),
  chart_stats AS (
    SELECT snapshot_date,
           count(*)::integer AS chart_entries,
           count(DISTINCT deezer_track_id)::integer AS chart_unique_tracks,
           count(DISTINCT genre_id)::integer AS chart_genres_covered,
           (count(*) FILTER (WHERE is_spotify_matched))::numeric
             / NULLIF(count(*), 0) AS chart_spotify_match_share
    FROM gold.fact_chart_position_daily
    GROUP BY snapshot_date
  ),
  release_stats AS (
    SELECT snapshot_date, count(*)::integer AS new_release_tracks_observed
    FROM gold.fact_release_track_daily
    GROUP BY snapshot_date
  ),
  ins AS (
    INSERT INTO gold.mart_music_daily (
      snapshot_date, spotify_tracks_observed, unique_artists_observed,
      avg_track_popularity, median_track_popularity,
      top_spotify_track_id, top_spotify_track_name, top_spotify_track_popularity,
      chart_entries, chart_unique_tracks, chart_genres_covered, chart_spotify_match_share,
      new_release_tracks_observed
    )
    SELECT d.snapshot_date,
           COALESCE(t.spotify_tracks_observed, 0),
           COALESCE(t.unique_artists_observed, 0),
           t.avg_track_popularity,
           t.median_track_popularity,
           top.spotify_track_id,
           top.track_name,
           top.spotify_popularity,
           COALESCE(c.chart_entries, 0),
           COALESCE(c.chart_unique_tracks, 0),
           COALESCE(c.chart_genres_covered, 0),
           c.chart_spotify_match_share,
           COALESCE(r.new_release_tracks_observed, 0)
    FROM days d
    LEFT JOIN track_stats t ON t.snapshot_date = d.snapshot_date
    LEFT JOIN top_track top ON top.snapshot_date = d.snapshot_date
    LEFT JOIN chart_stats c ON c.snapshot_date = d.snapshot_date
    LEFT JOIN release_stats r ON r.snapshot_date = d.snapshot_date
    WHERE (p_from IS NULL OR d.snapshot_date >= p_from)
      AND (p_to IS NULL OR d.snapshot_date <= p_to)
    ON CONFLICT (snapshot_date) DO UPDATE SET
      spotify_tracks_observed = EXCLUDED.spotify_tracks_observed,
      unique_artists_observed = EXCLUDED.unique_artists_observed,
      avg_track_popularity = EXCLUDED.avg_track_popularity,
      median_track_popularity = EXCLUDED.median_track_popularity,
      top_spotify_track_id = EXCLUDED.top_spotify_track_id,
      top_spotify_track_name = EXCLUDED.top_spotify_track_name,
      top_spotify_track_popularity = EXCLUDED.top_spotify_track_popularity,
      chart_entries = EXCLUDED.chart_entries,
      chart_unique_tracks = EXCLUDED.chart_unique_tracks,
      chart_genres_covered = EXCLUDED.chart_genres_covered,
      chart_spotify_match_share = EXCLUDED.chart_spotify_match_share,
      new_release_tracks_observed = EXCLUDED.new_release_tracks_observed,
      gold_loaded_at = now()
    RETURNING (xmax = 0) AS inserted
  )
  SELECT count(*) FILTER (WHERE inserted), count(*) INTO v_new, v_total FROM ins;
  o_target := 'gold.mart_music_daily'; o_new := v_new; o_total := v_total; RETURN NEXT;

  -- Операционные KPI. Запуски берутся непосредственно из ingestion_log,
  -- поэтому requests_total из одного run_id не суммируется по каждой raw-строке
  -- и полностью неудачные запуски также попадают в витрину.
  WITH days AS (
    SELECT snapshot_date FROM bronze.raw_response
    UNION
    SELECT started_at::date FROM meta.ingestion_log
    UNION
    SELECT snapshot_date FROM gold.fact_track_daily
    UNION
    SELECT snapshot_date FROM gold.fact_chart_position_daily
    UNION
    SELECT snapshot_date FROM meta.dq_results
  ),
  run_snapshot AS (
    SELECT run_id, min(snapshot_date) AS snapshot_date
    FROM bronze.raw_response
    WHERE run_id IS NOT NULL
    GROUP BY run_id
  ),
  run_stats AS (
    SELECT COALESCE(s.snapshot_date, l.started_at::date) AS snapshot_date,
           count(*)::integer AS ingestion_runs,
           count(*) FILTER (WHERE l.status = 'success')::integer AS successful_runs,
           count(*) FILTER (WHERE l.status <> 'success')::integer AS partial_or_failed_runs,
           COALESCE(sum(l.requests_total), 0)::integer AS requests_total,
           COALESCE(sum(l.requests_failed), 0)::integer AS requests_failed,
           max(l.finished_at) AS latest_ingestion_finished_at
    FROM meta.ingestion_log l
    LEFT JOIN run_snapshot s ON s.run_id = l.run_id
    GROUP BY COALESCE(s.snapshot_date, l.started_at::date)
  ),
  bronze_stats AS (
    SELECT snapshot_date, count(*)::integer AS bronze_rows
    FROM bronze.raw_response
    GROUP BY snapshot_date
  ),
  silver_stats AS (
    SELECT snapshot_date, count(*)::integer AS silver_rows
    FROM (
      SELECT snapshot_date FROM silver.dz_chart_position
      UNION ALL SELECT snapshot_date FROM silver.dz_track_daily
      UNION ALL SELECT snapshot_date FROM silver.dz_artist_daily
      UNION ALL SELECT snapshot_date FROM silver.sp_track_daily
      UNION ALL SELECT snapshot_date FROM silver.sp_artist_daily
      UNION ALL SELECT snapshot_date FROM silver.sp_album_daily
    ) x
    GROUP BY snapshot_date
  ),
  gold_stats AS (
    SELECT snapshot_date, count(*)::integer AS gold_fact_rows
    FROM (
      SELECT snapshot_date FROM gold.fact_track_daily
      UNION ALL SELECT snapshot_date FROM gold.fact_chart_position_daily
      UNION ALL SELECT snapshot_date FROM gold.fact_release_track_daily
    ) x
    GROUP BY snapshot_date
  ),
  dq_stats AS (
    SELECT snapshot_date,
           count(*) FILTER (WHERE severity = 'error' AND status = 'fail')::integer AS dq_error_failures,
           count(*) FILTER (WHERE status = 'warn')::integer AS dq_warnings
    FROM meta.dq_results
    GROUP BY snapshot_date
  ),
  ins AS (
    INSERT INTO gold.mart_pipeline_daily (
      snapshot_date, ingestion_runs, successful_runs, partial_or_failed_runs,
      requests_total, requests_failed, bronze_rows, silver_rows, gold_fact_rows,
      dq_error_failures, dq_warnings, latest_ingestion_finished_at
    )
    SELECT d.snapshot_date,
           COALESCE(r.ingestion_runs, 0),
           COALESCE(r.successful_runs, 0),
           COALESCE(r.partial_or_failed_runs, 0),
           COALESCE(r.requests_total, 0),
           COALESCE(r.requests_failed, 0),
           COALESCE(b.bronze_rows, 0),
           COALESCE(s.silver_rows, 0),
           COALESCE(g.gold_fact_rows, 0),
           COALESCE(q.dq_error_failures, 0),
           COALESCE(q.dq_warnings, 0),
           r.latest_ingestion_finished_at
    FROM days d
    LEFT JOIN run_stats r ON r.snapshot_date = d.snapshot_date
    LEFT JOIN bronze_stats b ON b.snapshot_date = d.snapshot_date
    LEFT JOIN silver_stats s ON s.snapshot_date = d.snapshot_date
    LEFT JOIN gold_stats g ON g.snapshot_date = d.snapshot_date
    LEFT JOIN dq_stats q ON q.snapshot_date = d.snapshot_date
    WHERE (p_from IS NULL OR d.snapshot_date >= p_from)
      AND (p_to IS NULL OR d.snapshot_date <= p_to)
    ON CONFLICT (snapshot_date) DO UPDATE SET
      ingestion_runs = EXCLUDED.ingestion_runs,
      successful_runs = EXCLUDED.successful_runs,
      partial_or_failed_runs = EXCLUDED.partial_or_failed_runs,
      requests_total = EXCLUDED.requests_total,
      requests_failed = EXCLUDED.requests_failed,
      bronze_rows = EXCLUDED.bronze_rows,
      silver_rows = EXCLUDED.silver_rows,
      gold_fact_rows = EXCLUDED.gold_fact_rows,
      dq_error_failures = EXCLUDED.dq_error_failures,
      dq_warnings = EXCLUDED.dq_warnings,
      latest_ingestion_finished_at = EXCLUDED.latest_ingestion_finished_at,
      gold_loaded_at = now()
    RETURNING (xmax = 0) AS inserted
  )
  SELECT count(*) FILTER (WHERE inserted), count(*) INTO v_new, v_total FROM ins;
  o_target := 'gold.mart_pipeline_daily'; o_new := v_new; o_total := v_total; RETURN NEXT;
END $$;
