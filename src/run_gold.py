"""Построение Gold-витрин и проверка их качества.

Запуск:
    python src/run_gold.py                      # построить Gold за последнюю дату Silver
    python src/run_gold.py --date 2026-10-24    # пересчитать ровно одну дату
    python src/run_gold.py --from 2026-10-01    # пересчитать диапазон до последней даты

Перед загрузкой для каждой целевой даты запускаются базовые DQ-проверки Bronze/Silver.
Gold не строится, если есть хотя бы одна error-проверка со статусом fail.
Код выхода: 0 — успешная загрузка, 1 — сработал quality gate или Gold-DQ.
"""
import argparse
import os
import sys
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

import psycopg2
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
# Базовые DQ нужны и при изолированном запуске Gold: они создают meta.dq_put
# и обеспечивают gate перед использованием Silver-данных.
SQL_FILES = [
    ROOT / "sql" / "dq" / "10_dq.sql",
    ROOT / "sql" / "gold" / "10_tables.sql",
    ROOT / "sql" / "gold" / "20_load.sql",
    ROOT / "sql" / "dq" / "20_gold_dq.sql",
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
    text_rows = [["" if value is None else str(value) for value in row] for row in rows]
    widths = [
        max(len(header), *(len(row[index]) for row in text_rows)) if text_rows else len(header)
        for index, header in enumerate(headers)
    ]
    line = "  ".join(header.ljust(width) for header, width in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for row in text_rows:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)))


def get_target_dates(cur, exact_date: date | None, from_date: date | None) -> list[date]:
    """Возвращает даты, в которых есть хотя бы один Silver-факт для Gold."""
    if exact_date is not None:
        cur.execute(
            """
            SELECT snapshot_date
            FROM (
              SELECT snapshot_date FROM silver.sp_track_daily WHERE snapshot_date = %s
              UNION
              SELECT snapshot_date FROM silver.dz_chart_position WHERE snapshot_date = %s
            ) d
            ORDER BY snapshot_date
            """,
            (exact_date, exact_date),
        )
    elif from_date is not None:
        cur.execute(
            """
            SELECT snapshot_date
            FROM (
              SELECT snapshot_date FROM silver.sp_track_daily WHERE snapshot_date >= %s
              UNION
              SELECT snapshot_date FROM silver.dz_chart_position WHERE snapshot_date >= %s
            ) d
            ORDER BY snapshot_date
            """,
            (from_date, from_date),
        )
    else:
        cur.execute(
            """
            SELECT max(snapshot_date)
            FROM (
              SELECT snapshot_date FROM silver.sp_track_daily
              UNION ALL
              SELECT snapshot_date FROM silver.dz_chart_position
            ) d
            """
        )
    return [row[0] for row in cur.fetchall() if row[0] is not None]


def upstream_failures(cur, snapshot_date: date) -> list[tuple]:
    """Error DQ Bronze/Silver/Meta, не включая Gold правила прошлого запуска."""
    cur.execute(
        """
        SELECT rule_name, layer, table_name, details
        FROM meta.dq_results
        WHERE snapshot_date = %s
          AND severity = 'error'
          AND status = 'fail'
          AND layer IS DISTINCT FROM 'gold'
        ORDER BY rule_name
        """,
        (snapshot_date,),
    )
    return cur.fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description="Gold-витрины + DQ")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--date", dest="exact_date", type=parse_date,
                       help="пересчитать Gold только за указанную дату")
    scope.add_argument("--from", dest="from_date", type=parse_date,
                       help="пересчитать Gold от указанной даты до последней")
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
            for path in SQL_FILES:
                cur.execute(path.read_text(encoding="utf-8"))
            conn.commit()
            print("Схема Gold, функции загрузки и DQ созданы/обновлены.\n")

            target_dates = get_target_dates(cur, args.exact_date, args.from_date)
            if not target_dates:
                requested = args.exact_date or args.from_date or "последней даты"
                print(f"Нет Silver-данных для {requested}. Сначала выполните src/run_silver.py.")
                return 1

            # Базовые DQ выполняются на всех датах выбранного диапазона, чтобы
            # backfill не обходил quality gate только из-за отсутствия записи DQ.
            blocked: list[tuple[date, tuple]] = []
            for target_date in target_dates:
                cur.execute(
                    "SELECT o_rule, o_status, o_total, o_failed, o_share, o_severity "
                    "FROM meta.run_dq(%s)",
                    (target_date,),
                )
                cur.fetchall()
                for failure in upstream_failures(cur, target_date):
                    blocked.append((target_date, failure))
            conn.commit()

            if blocked:
                print("Gold не построен: не пройден quality gate Bronze/Silver.")
                print_table(
                    ["дата", "правило", "слой", "таблица", "детали"],
                    [(day, rule, layer, table_name, details)
                     for day, (rule, layer, table_name, details) in blocked],
                )
                return 1

            date_from = args.exact_date or args.from_date or target_dates[0]
            date_to = args.exact_date or target_dates[-1]
            cur.execute(
                "SELECT o_target, o_new, o_total FROM gold.load_all(%s, %s)",
                (date_from, date_to),
            )
            load_rows = cur.fetchall()
            conn.commit()

            print("Загрузка Gold (новых строк / всего затронуто):")
            print_table(["таблица", "новых", "всего"], load_rows)

            gold_dq_rows: list[tuple] = []
            for target_date in target_dates:
                cur.execute(
                    "SELECT o_rule, o_status, o_total, o_failed, o_share, o_severity "
                    "FROM meta.run_gold_dq(%s)",
                    (target_date,),
                )
                gold_dq_rows.extend([(target_date, *row) for row in cur.fetchall()])
            cur.execute("SELECT gold.refresh_pipeline_dq(%s, %s)", (date_from, date_to))
            conn.commit()

        print("\nПроверки качества Gold:")
        print_table(
            ["дата", "правило", "статус", "проверено", "не прошло", "доля", "важность"],
            gold_dq_rows,
        )
        failures = [row for row in gold_dq_rows if row[2] == "fail"]
        warnings = [row for row in gold_dq_rows if row[2] == "warn"]
        print(f"\nИтого Gold-DQ: fail={len(failures)}, warn={len(warnings)}, "
              f"pass={len(gold_dq_rows) - len(failures) - len(warnings)}")
        return 1 if failures else 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
