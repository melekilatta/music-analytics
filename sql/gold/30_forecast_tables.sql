-- Таблицы прогнозного контура в слое Gold.
-- Заполняются скриптом src/run_forecast.py.

CREATE SCHEMA IF NOT EXISTS gold;

-- Прогнозы популярности треков по горизонтам (1..7 дней вперёд)
CREATE TABLE IF NOT EXISTS gold.fact_track_forecast (
  origin_date          date        NOT NULL,
  target_date          date        NOT NULL,
  horizon_days         integer     NOT NULL CHECK (horizon_days BETWEEN 1 AND 30),
  spotify_track_id     text        NOT NULL,
  track_name           text,
  artist_name          text,
  is_new_release_track boolean     NOT NULL DEFAULT false,
  model_name           text        NOT NULL,
  current_popularity   integer     CHECK (current_popularity BETWEEN 0 AND 100),
  predicted_popularity numeric(6,2) NOT NULL CHECK (predicted_popularity BETWEEN 0 AND 100),
  predicted_delta      numeric(6,2) NOT NULL,
  prediction_lower     numeric(6,2) CHECK (prediction_lower BETWEEN 0 AND 100),
  prediction_upper     numeric(6,2) CHECK (prediction_upper BETWEEN 0 AND 100),
  created_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (origin_date, horizon_days, spotify_track_id, model_name)
);

CREATE INDEX IF NOT EXISTS ix_gold_forecast_origin_model
  ON gold.fact_track_forecast (origin_date, model_name, horizon_days);

-- Метрики качества моделей на ретроспективной проверке (walk-forward backtest)
CREATE TABLE IF NOT EXISTS gold.forecast_model_metrics (
  eval_date      date         NOT NULL,
  model_name     text         NOT NULL,
  horizon_days   integer      NOT NULL CHECK (horizon_days BETWEEN 1 AND 30),
  n_samples      integer      NOT NULL CHECK (n_samples >= 0),
  mae            numeric(8,4) NOT NULL,
  rmse           numeric(8,4) NOT NULL,
  smape          numeric(8,4) NOT NULL,
  is_champion    boolean      NOT NULL DEFAULT false,
  created_at     timestamptz  NOT NULL DEFAULT now(),
  PRIMARY KEY (eval_date, model_name, horizon_days)
);

-- Важность признаков для интерпретации ML-модели на дашборде
CREATE TABLE IF NOT EXISTS gold.forecast_feature_importance (
  eval_date      date         NOT NULL,
  model_name     text         NOT NULL,
  feature_name   text         NOT NULL,
  feature_label  text         NOT NULL,
  coefficient    numeric(10,5) NOT NULL,
  abs_importance numeric(10,5) NOT NULL,
  created_at     timestamptz  NOT NULL DEFAULT now(),
  PRIMARY KEY (eval_date, model_name, feature_name)
);