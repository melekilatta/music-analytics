"""Прогнозный контур Music Analytics: обучение GBM и прогноз популярности треков.

Запуск:
    python src/run_forecast.py                      # прогноз от последней даты Gold на 7 дней
    python src/run_forecast.py --horizon 7          # явно задать горизонт прогноза (дней)
    python src/run_forecast.py --date 2026-10-27    # построить прогноз от указанной даты
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values
from sklearn.ensemble import HistGradientBoostingRegressor

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
SQL_SCHEMA = ROOT / "sql" / "gold" / "30_forecast_tables.sql"

MODEL_NAME = "gbm_mae"
# Отсекаем треки с техническим нулём (холодный старт API) и альбомные интро/скиты
MIN_TRACK_POPULARITY = 10

FEATURE_COLS: list[str] = [
    "spotify_popularity",
    "delta_1d",
    "delta_3d",
    "rolling_mean_3d",
    "artist_popularity",
    "pop_vs_artist",
    "log_artist_followers",
    "release_age_days_capped",
    "is_new_release_num",
    "in_deezer_chart",
    "best_chart_pos_inv",
    "log_deezer_rank",
    "log_deezer_fans",
    "horizon_days",
]


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"Не задана обязательная переменная окружения {name} (см. .env.example)")
    return value


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"ожидается дата YYYY-MM-DD, получено: {value}") from exc


def print_table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    text_rows = [["" if v is None else str(v) for v in row] for row in rows]
    widths = [
        max(len(h), *(len(r[i]) for r in text_rows)) if text_rows else len(h)
        for i, h in enumerate(headers)
    ]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for row in text_rows:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)))


def load_history(conn, max_date: date | None) -> pd.DataFrame:
    where_clause = "WHERE t.snapshot_date <= %s" if max_date else ""
    params = (max_date,) if max_date else ()
    sql = f"""
        WITH chart_agg AS (
            SELECT snapshot_date,
                   spotify_track_id,
                   min(chart_position) AS best_chart_position,
                   count(DISTINCT genre_id) AS chart_genres_count
            FROM gold.fact_chart_position_daily
            WHERE spotify_track_id IS NOT NULL
            GROUP BY snapshot_date, spotify_track_id
        )
        SELECT t.snapshot_date,
               t.spotify_track_id,
               t.track_name,
               t.artist_name,
               t.spotify_popularity,
               t.popularity_delta_1d,
               t.artist_popularity,
               t.artist_followers,
               t.release_age_days,
               t.is_new_release_track,
               t.deezer_match_count,
               t.deezer_rank_max,
               t.deezer_fans_max,
               c.best_chart_position,
               COALESCE(c.chart_genres_count, 0) AS chart_genres_count
        FROM gold.fact_track_daily t
        LEFT JOIN chart_agg c
          ON c.snapshot_date = t.snapshot_date
         AND c.spotify_track_id = t.spotify_track_id
        {where_clause}
        ORDER BY t.spotify_track_id, t.snapshot_date
    """
    return pd.read_sql_query(sql, conn, params=params)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    df = df.sort_values(["spotify_track_id", "snapshot_date"]).reset_index(drop=True)

    grp = df.groupby("spotify_track_id", sort=False)
    df["prev_pop_1"] = grp["spotify_popularity"].shift(1)
    df["prev_pop_2"] = grp["spotify_popularity"].shift(2)

    raw_delta_1 = df["spotify_popularity"] - df["prev_pop_1"]
    df["delta_1d"] = df["popularity_delta_1d"].fillna(raw_delta_1).fillna(0.0)
    df["delta_3d"] = (df["spotify_popularity"] - df["prev_pop_2"]).fillna(df["delta_1d"])
    df["rolling_mean_3d"] = (
        grp["spotify_popularity"]
        .transform(lambda s: s.rolling(3, min_periods=1).mean())
        .fillna(df["spotify_popularity"])
    )

    df["artist_popularity"] = df["artist_popularity"].fillna(df["spotify_popularity"])
    df["pop_vs_artist"] = df["spotify_popularity"] - df["artist_popularity"]
    df["log_artist_followers"] = np.log1p(df["artist_followers"].clip(lower=0).fillna(0))
    df["release_age_days_capped"] = df["release_age_days"].clip(lower=0, upper=365).fillna(180)
    df["is_new_release_num"] = df["is_new_release_track"].astype(float)
    df["in_deezer_chart"] = (df["best_chart_position"].notna()).astype(float)
    df["best_chart_pos_inv"] = np.where(
        df["best_chart_position"].notna(),
        1.0 / df["best_chart_position"].clip(lower=1),
        0.0,
    )
    df["log_deezer_rank"] = np.log1p(df["deezer_rank_max"].clip(lower=0).fillna(0))
    df["log_deezer_fans"] = np.log1p(df["deezer_fans_max"].clip(lower=0).fillna(0))
    return df


def build_horizon_pairs(df: pd.DataFrame, max_horizon: int) -> pd.DataFrame:
    # Исключаем наблюдения с техническим нулём на старте (pop < MIN_TRACK_POPULARITY),
    # чтобы модель не заучивала искусственные скачки индексации API с 0 до 18.
    valid_left = df[df["spotify_popularity"] >= MIN_TRACK_POPULARITY][
        [
            "snapshot_date",
            "spotify_track_id",
            "track_name",
            "artist_name",
            "is_new_release_track",
            *[c for c in FEATURE_COLS if c != "horizon_days"],
        ]
    ].copy()
    right = df[["snapshot_date", "spotify_track_id", "spotify_popularity"]].rename(
        columns={
            "snapshot_date": "target_date",
            "spotify_popularity": "target_popularity",
        }
    )
    pairs = valid_left.merge(right, on="spotify_track_id", how="inner")
    pairs["horizon_days"] = (pairs["target_date"] - pairs["snapshot_date"]).dt.days
    pairs = pairs[
        (pairs["horizon_days"] >= 1)
        & (pairs["horizon_days"] <= max_horizon)
        & pairs["spotify_popularity"].notna()
        & pairs["target_popularity"].notna()
    ].copy()
    pairs["target_delta"] = pairs["target_popularity"] - pairs["spotify_popularity"]
    return pairs.sort_values(["snapshot_date", "spotify_track_id", "horizon_days"]).reset_index(drop=True)


def make_ml_model() -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(
        loss="absolute_error",
        max_iter=80,
        max_depth=4,
        min_samples_leaf=25,
        learning_rate=0.06,
        l2_regularization=2.0,
        random_state=42,
    )


def compute_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> tuple[float, float, float]:
    err = y_true - y_pred
    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err ** 2)))
    denom = (np.abs(y_true) + np.abs(y_pred)) / 2.0
    smape = float(np.mean(np.where(denom > 0, np.abs(err) / denom, 0.0)) * 100.0)
    return mae, rmse, smape


def evaluate_gbm(pairs: pd.DataFrame) -> pd.DataFrame:
    """Ретроспективная проверка (walk-forward) модели GBM по горизонтам."""
    if pairs.empty:
        return pd.DataFrame()

    eval_records: list[dict[str, object]] = []
    for t_date in sorted(pairs["target_date"].unique()):
        test_fold = pairs[pairs["target_date"] == t_date].copy()
        train_fold = pairs[pairs["target_date"] < t_date].copy()
        if len(train_fold) < 30:
            continue

        ml_model = make_ml_model()
        ml_model.fit(train_fold[FEATURE_COLS], train_fold["target_delta"])
        pred_delta = ml_model.predict(test_fold[FEATURE_COLS])
        y_pred = np.clip(test_fold["spotify_popularity"].to_numpy() + pred_delta, 0.0, 100.0)

        for row_idx, (_, row) in enumerate(test_fold.iterrows()):
            eval_records.append(
                {
                    "horizon_days": int(row["horizon_days"]),
                    "y_true": float(row["target_popularity"]),
                    "y_pred": float(y_pred[row_idx]),
                }
            )

    if not eval_records:
        return pd.DataFrame()

    eval_df = pd.DataFrame(eval_records)
    metrics_rows: list[dict[str, object]] = []
    for h, grp in eval_df.groupby("horizon_days"):
        mae, rmse, smape = compute_metrics(grp["y_true"].to_numpy(), grp["y_pred"].to_numpy())
        metrics_rows.append(
            {
                "model_name": MODEL_NAME,
                "horizon_days": int(h),
                "n_samples": int(len(grp)),
                "mae": round(mae, 4),
                "rmse": round(rmse, 4),
                "smape": round(smape, 4),
                "is_champion": True,
            }
        )
    return pd.DataFrame(metrics_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Прогноз популярности треков (Gold)")
    parser.add_argument("--date", dest="origin_date", type=parse_date, default=None,
                        help="дата среза, от которой строится прогноз (по умолчанию — последняя в Gold)")
    parser.add_argument("--horizon", dest="horizon", type=int, default=7,
                        help="горизонт прогноза в днях (по умолчанию 7)")
    args = parser.parse_args()

    conn = psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", "15432")),
        user=require_env("PG_USER"),
        password=require_env("PG_PASSWORD"),
        dbname=require_env("PG_DB"),
    )

    try:
        with conn.cursor() as cur:
            cur.execute(SQL_SCHEMA.read_text(encoding="utf-8"))
            conn.commit()

        history = load_history(conn, args.origin_date)
        if history.empty:
            print("В gold.fact_track_daily нет данных. Сначала выполните python src/run_gold.py.")
            return 1

        feat_df = build_features(history)
        origin_ts = feat_df["snapshot_date"].max()
        origin_date: date = origin_ts.date()
        n_dates = feat_df["snapshot_date"].nunique()
        print(f"=== Прогнозный модуль (GBM): срез {origin_date} (дней: {n_dates}, горизонт: {args.horizon} дн.) ===")

        pairs = build_horizon_pairs(feat_df, args.horizon)
        metrics_df = evaluate_gbm(pairs)

        ml_model = make_ml_model()
        ml_model.fit(pairs[FEATURE_COLS], pairs["target_delta"])

        # Прогнозируем только активные проиндексированные треки (popularity >= MIN_TRACK_POPULARITY)
        latest_slice = feat_df[
            (feat_df["snapshot_date"] == origin_ts)
            & (feat_df["spotify_popularity"] >= MIN_TRACK_POPULARITY)
        ].copy()

        forecast_records: list[tuple] = []
        max_observed_h = int(pairs["horizon_days"].max()) if not pairs.empty else 1

        for h in range(1, args.horizon + 1):
            grid = latest_slice.copy()
            grid["horizon_days"] = h
            target_d = origin_date + timedelta(days=h)

            raw_delta = ml_model.predict(grid[FEATURE_COLS])
            if h > max_observed_h:
                grid_max = grid.copy()
                grid_max["horizon_days"] = max_observed_h
                base_delta = ml_model.predict(grid_max[FEATURE_COLS])
                raw_delta = base_delta * np.sqrt(h / max(1, max_observed_h))

            # Добавляем лёгкий вклад недавнего импульса (15%) для гладкого ранжирования
            momentum_step = 0.20 * grid["delta_1d"].to_numpy() * np.sqrt(h)
            combined_delta = np.clip(0.85 * raw_delta + 0.15 * momentum_step, -20.0, 20.0)

            cur_pop = grid["spotify_popularity"].to_numpy(dtype=float)
            y_hat = np.clip(cur_pop + combined_delta, 0.0, 100.0)
            delta_hat = y_hat - cur_pop

            for idx_row, (_, tr) in enumerate(grid.iterrows()):
                forecast_records.append(
                    (
                        origin_date,
                        target_d,
                        h,
                        tr["spotify_track_id"],
                        tr["track_name"],
                        tr["artist_name"],
                        bool(tr["is_new_release_track"]),
                        MODEL_NAME,
                        int(tr["spotify_popularity"]),
                        round(float(y_hat[idx_row]), 2),
                        round(float(delta_hat[idx_row]), 2),
                        round(float(y_hat[idx_row]), 2),
                        round(float(y_hat[idx_row]), 2),
                    )
                )

        with conn.cursor() as cur:
            cur.execute("DELETE FROM gold.fact_track_forecast WHERE origin_date = %s;", (origin_date,))
            execute_values(
                cur,
                """
                INSERT INTO gold.fact_track_forecast (
                    origin_date, target_date, horizon_days, spotify_track_id,
                    track_name, artist_name, is_new_release_track, model_name,
                    current_popularity, predicted_popularity, predicted_delta,
                    prediction_lower, prediction_upper
                ) VALUES %s
                """,
                forecast_records,
            )

            if not metrics_df.empty:
                cur.execute("DELETE FROM gold.forecast_model_metrics WHERE eval_date = %s;", (origin_date,))
                metric_tuples = [
                    (
                        origin_date,
                        r["model_name"],
                        int(r["horizon_days"]),
                        int(r["n_samples"]),
                        float(r["mae"]),
                        float(r["rmse"]),
                        float(r["smape"]),
                        bool(r["is_champion"]),
                    )
                    for _, r in metrics_df.iterrows()
                ]
                execute_values(
                    cur,
                    """
                    INSERT INTO gold.forecast_model_metrics (
                        eval_date, model_name, horizon_days, n_samples,
                        mae, rmse, smape, is_champion
                    ) VALUES %s
                    """,
                    metric_tuples,
                )
        conn.commit()

        if not metrics_df.empty:
            w_mae = float(np.average(metrics_df["mae"], weights=metrics_df["n_samples"]))
            w_rmse = float(np.average(metrics_df["rmse"], weights=metrics_df["n_samples"]))
            print(f"\nКачество модели GBM (взвешенный MAE={w_mae:.3f}, RMSE={w_rmse:.3f}):")
            print_table(
                ["горизонт", "примеров", "MAE", "RMSE", "sMAPE %"],
                [
                    (
                        f"+{int(r['horizon_days'])} дн.",
                        int(r["n_samples"]),
                        f"{r['mae']:.3f}",
                        f"{r['rmse']:.3f}",
                        f"{r['smape']:.2f}%",
                    )
                    for _, r in metrics_df.sort_values("horizon_days").iterrows()
                ],
            )

        print(
            f"\nЗаписано прогнозов: {len(forecast_records)} строк "
            f"(отфильтровано треков с popularity >= {MIN_TRACK_POPULARITY}: {len(latest_slice)})."
        )
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())