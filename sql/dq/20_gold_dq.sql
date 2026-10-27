-- Контроль качества Gold. Требует meta.dq_put из sql/dq/10_dq.sql.
-- Вызов: SELECT * FROM meta.run_gold_dq('2026-10-24');

CREATE OR REPLACE FUNCTION meta.run_gold_dq(p_date date DEFAULT NULL)
RETURNS TABLE (o_rule text, o_status text, o_total bigint, o_failed bigint, o_share numeric, o_severity text)
LANGUAGE plpgsql AS $$
DECLARE
  v_date date := COALESCE(
    p_date,
    (SELECT max(snapshot_date) FROM gold.fact_track_daily),
    (SELECT max(snapshot_date) FROM gold.fact_chart_position_daily)
  );
  v_total bigint;
  v_failed bigint;
BEGIN
  IF v_date IS NULL THEN
    RAISE EXCEPTION 'В Gold нет данных для проверки';
  END IF;

  -- Полнота: витрина треков должна содержать по одной строке на каждый
  -- типизированный Spotify-снимок за дату.
  SELECT count(*) INTO v_total
  FROM silver.sp_track_daily
  WHERE snapshot_date = v_date;
  SELECT count(*) INTO v_failed
  FROM (
    (SELECT spotify_track_id FROM silver.sp_track_daily WHERE snapshot_date = v_date)
    EXCEPT
    (SELECT spotify_track_id FROM gold.fact_track_daily WHERE snapshot_date = v_date)
  ) q;
  PERFORM meta.dq_put(v_date, 'gold.reconcile.fact_track_daily', 'completeness', 'gold',
    'gold.fact_track_daily', 'error', v_total, v_failed, 0,
    'каждый Spotify-трек из Silver должен иметь строку в Gold');

  -- Полнота: чартовая витрина сохраняет все позиции из Silver.
  SELECT count(*) INTO v_total
  FROM silver.dz_chart_position
  WHERE snapshot_date = v_date;
  SELECT count(*) INTO v_failed
  FROM (
    (SELECT genre_id, deezer_track_id FROM silver.dz_chart_position WHERE snapshot_date = v_date)
    EXCEPT
    (SELECT genre_id, deezer_track_id FROM gold.fact_chart_position_daily WHERE snapshot_date = v_date)
  ) q;
  PERFORM meta.dq_put(v_date, 'gold.reconcile.fact_chart_position_daily', 'completeness', 'gold',
    'gold.fact_chart_position_daily', 'error', v_total, v_failed, 0,
    'каждая позиция Deezer-чарта из Silver должна иметь строку в Gold');

  -- Допустимость значений, несмотря на CHECK-ограничения: результат остаётся
  -- видимым в meta.dq_results и на операционном дашборде.
  SELECT count(*),
         count(*) FILTER (WHERE spotify_popularity IS NOT NULL
                            AND spotify_popularity NOT BETWEEN 0 AND 100)
    INTO v_total, v_failed
  FROM gold.fact_track_daily
  WHERE snapshot_date = v_date;
  PERFORM meta.dq_put(v_date, 'gold.fact_track.popularity_in_0_100', 'range', 'gold',
    'gold.fact_track_daily', 'error', v_total, v_failed, 0, NULL);

  SELECT count(*),
         count(*) FILTER (WHERE chart_position IS NULL OR chart_position <= 0)
    INTO v_total, v_failed
  FROM gold.fact_chart_position_daily
  WHERE snapshot_date = v_date;
  PERFORM meta.dq_put(v_date, 'gold.fact_chart.chart_position_positive', 'validity', 'gold',
    'gold.fact_chart_position_daily', 'error', v_total, v_failed, 0, NULL);

  -- Ссылочная целостность: связанный Spotify-трек должен существовать в том же
  -- ежедневном снимке. Несовпадения возможны из-за лимита/сбоя API, поэтому warn.
  SELECT count(*),
         count(*) FILTER (
           WHERE is_spotify_matched
             AND NOT EXISTS (
               SELECT 1
               FROM silver.sp_track_daily s
               WHERE s.snapshot_date = c.snapshot_date
                 AND s.spotify_track_id = c.spotify_track_id
             )
         )
    INTO v_total, v_failed
  FROM gold.fact_chart_position_daily c
  WHERE snapshot_date = v_date;
  PERFORM meta.dq_put(v_date, 'gold.ref.chart_spotify_snapshot_present', 'referential', 'gold',
    'gold.fact_chart_position_daily', 'warn', v_total, v_failed, 0.05,
    'сопоставленный Deezer-трек не имеет Spotify-снимка за ту же дату');

  -- Уникальность: один Deezer ID не должен встречаться в bridge дважды за дату.
  SELECT count(*), count(*) FILTER (WHERE cnt > 1)
    INTO v_total, v_failed
  FROM (
    SELECT deezer_track_id, count(*) OVER (PARTITION BY deezer_track_id) AS cnt
    FROM gold.bridge_track_mapping_snapshot
    WHERE snapshot_date = v_date
  ) q;
  PERFORM meta.dq_put(v_date, 'gold.unique.bridge_deezer_track', 'uniqueness', 'gold',
    'gold.bridge_track_mapping_snapshot', 'warn', v_total, v_failed, 0,
    'отсутствие bridge допустимо для ещё не сопоставленных Deezer-треков; дубликаты недопустимы');

  -- Согласованность агрегированных KPI с фактами.
  SELECT 1,
         CASE WHEN EXISTS (
           SELECT 1
           FROM gold.mart_music_daily m
           WHERE m.snapshot_date = v_date
             AND m.spotify_tracks_observed = (
               SELECT count(*) FROM gold.fact_track_daily f WHERE f.snapshot_date = v_date
             )
         ) THEN 0 ELSE 1 END
    INTO v_total, v_failed;
  PERFORM meta.dq_put(v_date, 'gold.reconcile.mart_music_track_count', 'completeness', 'gold',
    'gold.mart_music_daily', 'error', v_total, v_failed, 0,
    'KPI spotify_tracks_observed должен совпадать с числом строк fact_track_daily');

  RETURN QUERY
  SELECT q.rule_name, q.status, q.total_rows, q.failed_rows, q.fail_share::numeric, q.severity
  FROM meta.dq_results q
  WHERE q.snapshot_date = v_date
    AND q.layer = 'gold'
  ORDER BY (q.status = 'fail') DESC, (q.status = 'warn') DESC, q.rule_name;
END $$;

-- После Gold-DQ обновляется уже построенная операционная витрина: она получает
-- актуальное число проверок и ошибок, включая собственные правила Gold.
CREATE OR REPLACE FUNCTION gold.refresh_pipeline_dq(
  p_from date DEFAULT NULL,
  p_to date DEFAULT NULL
)
RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
  v_updated bigint;
BEGIN
  UPDATE gold.mart_pipeline_daily m
  SET dq_error_failures = q.dq_error_failures,
      dq_warnings = q.dq_warnings,
      gold_loaded_at = now()
  FROM (
    SELECT snapshot_date,
           count(*) FILTER (WHERE severity = 'error' AND status = 'fail')::integer AS dq_error_failures,
           count(*) FILTER (WHERE status = 'warn')::integer AS dq_warnings
    FROM meta.dq_results
    GROUP BY snapshot_date
  ) q
  WHERE m.snapshot_date = q.snapshot_date
    AND (p_from IS NULL OR m.snapshot_date >= p_from)
    AND (p_to IS NULL OR m.snapshot_date <= p_to);
  GET DIAGNOSTICS v_updated = ROW_COUNT;
  RETURN v_updated;
END $$;
