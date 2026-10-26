-- Контроль качества данных. Вызов: SELECT * FROM meta.run_dq();  (или meta.run_dq('2026-10-24'))
-- Результаты хранятся в meta.dq_results: по одной строке на (дата, правило), повторный запуск обновляет строку.
--
-- Политика при обнаружении некорректных данных:
--   * строки не удаляются; недопустимые значения (popularity вне 0..100, не число) превращаются в NULL в Silver,
--     а их число фиксируется правилами типа type / range;
--   * severity = error и превышен порог  -> status = fail  (сборка Gold за эту дату не должна запускаться,
--     скрипт run_silver.py завершается с кодом 1);
--   * severity = warn и превышен порог   -> status = warn  (данные используются, проблема видна на дашборде).

CREATE TABLE IF NOT EXISTS meta.dq_results (
  dq_id         bigserial PRIMARY KEY,
  checked_at    timestamptz NOT NULL DEFAULT now(),
  snapshot_date date NOT NULL,
  rule_name     text NOT NULL,
  dimension     text NOT NULL,   -- completeness / uniqueness / validity / type / range / referential / freshness
  layer         text,
  table_name    text,
  severity      text NOT NULL CHECK (severity IN ('error', 'warn')),
  total_rows    bigint,
  failed_rows   bigint,
  fail_share    numeric(8,5),
  threshold     numeric(8,5) NOT NULL,
  status        text NOT NULL CHECK (status IN ('pass', 'warn', 'fail')),
  details       text,
  UNIQUE (snapshot_date, rule_name)
);

-- Запись результата одной проверки и вычисление статуса
CREATE OR REPLACE FUNCTION meta.dq_put(
  p_date date, p_rule text, p_dim text, p_layer text, p_table text, p_sev text,
  p_total bigint, p_failed bigint, p_thr numeric, p_details text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  v_share  numeric;
  v_status text;
BEGIN
  v_share := CASE WHEN p_total > 0 THEN p_failed::numeric / p_total ELSE NULL END;
  v_status := CASE
    WHEN p_total = 0 THEN CASE WHEN p_sev = 'error' THEN 'fail' ELSE 'warn' END   -- нет данных для проверки
    WHEN v_share > p_thr THEN CASE WHEN p_sev = 'error' THEN 'fail' ELSE 'warn' END
    ELSE 'pass'
  END;

  INSERT INTO meta.dq_results
    (snapshot_date, rule_name, dimension, layer, table_name, severity,
     total_rows, failed_rows, fail_share, threshold, status, details)
  VALUES
    (p_date, p_rule, p_dim, p_layer, p_table, p_sev,
     p_total, p_failed, v_share, p_thr, v_status, p_details)
  ON CONFLICT (snapshot_date, rule_name) DO UPDATE SET
    checked_at  = now(),
    dimension   = EXCLUDED.dimension,
    layer       = EXCLUDED.layer,
    table_name  = EXCLUDED.table_name,
    severity    = EXCLUDED.severity,
    total_rows  = EXCLUDED.total_rows,
    failed_rows = EXCLUDED.failed_rows,
    fail_share  = EXCLUDED.fail_share,
    threshold   = EXCLUDED.threshold,
    status      = EXCLUDED.status,
    details     = EXCLUDED.details;
END $$;

CREATE OR REPLACE FUNCTION meta.run_dq(p_date date DEFAULT NULL)
RETURNS TABLE (o_rule text, o_status text, o_total bigint, o_failed bigint, o_share numeric, o_severity text)
LANGUAGE plpgsql AS $$
DECLARE
  v_date   date := COALESCE(p_date, (SELECT max(snapshot_date) FROM bronze.raw_response));
  v_total  bigint;
  v_failed bigint;
  v_silver bigint;
  v_min    date;
  r        record;
BEGIN
  IF v_date IS NULL THEN
    RAISE EXCEPTION 'В bronze.raw_response нет данных';
  END IF;

  -- ===== Полнота: обязательные поля в ответах API =====
  SELECT count(*), count(*) FILTER (WHERE payload->>'popularity' IS NULL)
    INTO v_total, v_failed
  FROM bronze.raw_response
  WHERE source = 'spotify' AND entity_type = 'track' AND snapshot_date = v_date AND http_status = 200;
  PERFORM meta.dq_put(v_date, 'spotify_track.popularity_present', 'completeness', 'bronze',
    'bronze.raw_response', 'error', v_total, v_failed, 0.01,
    'поле popularity отсутствует в ответе Spotify (возможное изменение API)');

  SELECT count(*), count(*) FILTER (WHERE payload->>'popularity' IS NULL)
    INTO v_total, v_failed
  FROM bronze.raw_response
  WHERE source = 'spotify' AND entity_type = 'artist' AND snapshot_date = v_date AND http_status = 200;
  PERFORM meta.dq_put(v_date, 'spotify_artist.popularity_present', 'completeness', 'bronze',
    'bronze.raw_response', 'error', v_total, v_failed, 0.01, NULL);

  SELECT count(*), count(*) FILTER (WHERE payload->'followers'->>'total' IS NULL)
    INTO v_total, v_failed
  FROM bronze.raw_response
  WHERE source = 'spotify' AND entity_type = 'artist' AND snapshot_date = v_date AND http_status = 200;
  PERFORM meta.dq_put(v_date, 'spotify_artist.followers_present', 'completeness', 'bronze',
    'bronze.raw_response', 'error', v_total, v_failed, 0.01, NULL);

  SELECT count(*), count(*) FILTER (WHERE payload->>'nb_fan' IS NULL)
    INTO v_total, v_failed
  FROM bronze.raw_response
  WHERE source = 'deezer' AND entity_type = 'artist' AND snapshot_date = v_date AND http_status = 200;
  PERFORM meta.dq_put(v_date, 'deezer_artist.nb_fan_present', 'completeness', 'bronze',
    'bronze.raw_response', 'error', v_total, v_failed, 0.01, NULL);

  -- ===== Соответствие типов =====
  SELECT count(*),
         count(*) FILTER (WHERE payload->>'popularity' IS NOT NULL
                            AND silver.safe_int(payload->>'popularity') IS NULL)
    INTO v_total, v_failed
  FROM bronze.raw_response
  WHERE source = 'spotify' AND entity_type = 'track' AND snapshot_date = v_date AND http_status = 200;
  PERFORM meta.dq_put(v_date, 'spotify_track.popularity_is_integer', 'type', 'bronze',
    'bronze.raw_response', 'error', v_total, v_failed, 0, 'popularity должно быть целым числом');

  -- ===== Допустимость диапазона =====
  SELECT count(*),
         count(*) FILTER (WHERE silver.safe_int(payload->>'popularity') IS NOT NULL
                            AND silver.safe_int(payload->>'popularity') NOT BETWEEN 0 AND 100)
    INTO v_total, v_failed
  FROM bronze.raw_response
  WHERE source = 'spotify' AND entity_type = 'track' AND snapshot_date = v_date AND http_status = 200;
  PERFORM meta.dq_put(v_date, 'spotify_track.popularity_in_0_100', 'range', 'bronze',
    'bronze.raw_response', 'error', v_total, v_failed, 0, 'popularity вне диапазона 0..100 заменяется на NULL в Silver');

  -- ===== Допустимость значений =====
  SELECT count(*),
         count(*) FILTER (WHERE silver.safe_int(payload->>'rank') IS NULL
                             OR silver.safe_int(payload->>'rank') <= 0)
    INTO v_total, v_failed
  FROM bronze.raw_response
  WHERE source = 'deezer' AND entity_type = 'track' AND snapshot_date = v_date AND http_status = 200;
  PERFORM meta.dq_put(v_date, 'deezer_track.rank_positive', 'validity', 'bronze',
    'bronze.raw_response', 'warn', v_total, v_failed, 0.01, 'rank трека должен быть положительным числом');

  SELECT count(*),
         count(*) FILTER (WHERE COALESCE(silver.safe_numeric(payload->>'bpm'), 0) = 0)
    INTO v_total, v_failed
  FROM bronze.raw_response
  WHERE source = 'deezer' AND entity_type = 'track' AND snapshot_date = v_date AND http_status = 200;
  PERFORM meta.dq_put(v_date, 'deezer_track.bpm_zero_share', 'validity', 'bronze',
    'bronze.raw_response', 'warn', v_total, v_failed, 0.9,
    'bpm = 0 означает отсутствие данных и заменяется на NULL в Silver');

  -- ===== Сверка Bronze -> Silver (все строки загружены) =====
  FOR r IN
    SELECT * FROM (VALUES
      ('sp_track_daily',  'spotify', 'track'),
      ('sp_artist_daily', 'spotify', 'artist'),
      ('sp_album_daily',  'spotify', 'album'),
      ('dz_track_daily',  'deezer',  'track'),
      ('dz_artist_daily', 'deezer',  'artist')
    ) AS x(tbl, src, ent)
  LOOP
    EXECUTE format(
      'SELECT (SELECT count(*) FROM bronze.raw_response
                WHERE source = %L AND entity_type = %L AND snapshot_date = $1 AND http_status = 200),
              (SELECT count(*) FROM silver.%I WHERE snapshot_date = $1)',
      r.src, r.ent, r.tbl)
    INTO v_total, v_silver USING v_date;
    PERFORM meta.dq_put(v_date, 'reconcile.' || r.tbl, 'completeness', 'silver',
      'silver.' || r.tbl, 'error', v_total, abs(v_total - v_silver), 0,
      'число записей Bronze и Silver за дату должно совпадать');
  END LOOP;

  -- ===== Ссылочная целостность =====
  SELECT count(*),
         count(*) FILTER (WHERE NOT EXISTS (
           SELECT 1 FROM silver.dz_track_daily t
           WHERE t.snapshot_date = c.snapshot_date AND t.deezer_track_id = c.deezer_track_id))
    INTO v_total, v_failed
  FROM silver.dz_chart_position c
  WHERE c.snapshot_date = v_date;
  PERFORM meta.dq_put(v_date, 'ref.dz_chart_track_has_snapshot', 'referential', 'silver',
    'silver.dz_chart_position', 'warn', v_total, v_failed, 0.05,
    'трек из чарта без снимка: не попал в справочник из-за лимита MAX_TRACKED_TRACKS');

  SELECT count(*),
         count(*) FILTER (WHERE s.artist_id IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM silver.sp_artist_daily a
           WHERE a.snapshot_date = s.snapshot_date AND a.spotify_artist_id = s.artist_id))
    INTO v_total, v_failed
  FROM silver.sp_track_daily s
  WHERE s.snapshot_date = v_date;
  PERFORM meta.dq_put(v_date, 'ref.sp_track_artist_has_snapshot', 'referential', 'silver',
    'silver.sp_track_daily', 'warn', v_total, v_failed, 0.05, NULL);

  SELECT count(*),
         count(*) FILTER (WHERE s.album_id IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM silver.sp_album_daily a
           WHERE a.snapshot_date = s.snapshot_date AND a.spotify_album_id = s.album_id))
    INTO v_total, v_failed
  FROM silver.sp_track_daily s
  WHERE s.snapshot_date = v_date;
  PERFORM meta.dq_put(v_date, 'ref.sp_track_album_has_snapshot', 'referential', 'silver',
    'silver.sp_track_daily', 'warn', v_total, v_failed, 0.05, NULL);

  -- ===== Уникальность и сопоставление сущностей =====
  SELECT count(*), count(*) FILTER (WHERE cnt > 1)
    INTO v_total, v_failed
  FROM (
    SELECT spotify_track_id, count(*) OVER (PARTITION BY spotify_track_id) AS cnt
    FROM meta.tracked_tracks
    WHERE is_active AND spotify_track_id IS NOT NULL
  ) q;
  PERFORM meta.dq_put(v_date, 'unique.catalog_spotify_track_id', 'uniqueness', 'meta',
    'meta.tracked_tracks', 'warn', v_total, v_failed, 0.05,
    'один трек Spotify сопоставлен нескольким версиям трека Deezer (ремастеры, сборники); решается агрегацией в Gold');

  SELECT count(*), count(*) FILTER (WHERE spotify_track_id IS NULL)
    INTO v_total, v_failed
  FROM meta.tracked_tracks
  WHERE is_active AND isrc IS NOT NULL;
  PERFORM meta.dq_put(v_date, 'catalog.spotify_match_rate', 'completeness', 'meta',
    'meta.tracked_tracks', 'warn', v_total, v_failed, 0.05,
    'доля треков Deezer, не найденных в Spotify по ISRC');

  -- ===== Актуальность =====
  FOR r IN SELECT * FROM (VALUES ('deezer'), ('spotify')) AS x(src) LOOP
    PERFORM meta.dq_put(v_date, 'freshness.bronze_' || r.src, 'freshness', 'bronze',
      'bronze.raw_response', 'error', 1,
      CASE WHEN current_date - COALESCE(
             (SELECT max(snapshot_date) FROM bronze.raw_response WHERE source = r.src),
             date '1900-01-01') > 1 THEN 1 ELSE 0 END,
      0, 'последний снимок источника старше одних суток');
  END LOOP;

  -- ===== Непрерывность ряда (пропущенные дни за последние 14 суток) =====
  SELECT min(snapshot_date) INTO v_min FROM bronze.raw_response;
  FOR r IN SELECT * FROM (VALUES ('deezer'), ('spotify')) AS x(src) LOOP
    SELECT count(*),
           count(*) FILTER (WHERE NOT EXISTS (
             SELECT 1 FROM bronze.raw_response b
             WHERE b.source = r.src AND b.snapshot_date = d::date))
      INTO v_total, v_failed
    FROM generate_series(GREATEST(v_min, v_date - 13)::timestamp, v_date::timestamp, interval '1 day') AS d;
    PERFORM meta.dq_put(v_date, 'continuity.bronze_' || r.src || '_14d', 'completeness', 'bronze',
      'bronze.raw_response', 'warn', v_total, v_failed, 0,
      'дни без снимков за последние 14 суток (пропуски в ряде)');
  END LOOP;

  RETURN QUERY
  SELECT q.rule_name, q.status, q.total_rows, q.failed_rows, q.fail_share::numeric, q.severity
  FROM meta.dq_results q
  WHERE q.snapshot_date = v_date
  ORDER BY (q.status = 'fail') DESC, (q.status = 'warn') DESC, q.rule_name;
END $$;
