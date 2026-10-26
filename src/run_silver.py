"""Построение слоя Silver и проверки качества данных одной командой.

Запуск:
    python src/run_silver.py                      # пересчитать всё, проверить последнюю дату
    python src/run_silver.py --from 2026-10-23    # пересчитать Silver только с этой даты
    python src/run_silver.py --date 2026-10-24    # проверки качества за конкретную дату

Код выхода: 0 - все проверки пройдены или есть только предупреждения,
            1 - есть проверки со статусом fail.
"""
import argparse
import os
import sys
from datetime import date
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv()  # не перезаписывает переменные, заданные в окружении

ROOT = Path(__file__).resolve().parent.parent
# Порядок важен: сначала функции и таблицы, затем загрузка, затем DQ.
# Все файлы идемпотентны (IF NOT EXISTS / OR REPLACE), поэтому выполняются при каждом запуске.
SQL_FILES = [
    ROOT / "sql" / "silver" / "10_tables.sql",
    ROOT / "sql" / "silver" / "20_load.sql",
    ROOT / "sql" / "dq" / "10_dq.sql",
]


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"Не задана обязательная переменная окружения {name} (см. .env.example)")
    return value


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидается дата YYYY-MM-DD, получено: {value}")


def print_table(headers, rows) -> None:
    rows = [["" if v is None else str(v) for v in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Silver + DQ")
    parser.add_argument("--from", dest="from_date", type=parse_date, default=None,
                        help="пересчитать Silver только начиная с этой даты (по умолчанию - всё)")
    parser.add_argument("--date", dest="dq_date", type=parse_date, default=None,
                        help="дата для проверок качества (по умолчанию - последняя в Bronze)")
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
            print("Схема Silver и функции созданы/обновлены.\n")

            cur.execute("SELECT o_target, o_new, o_total FROM silver.load_all(%s)", (args.from_date,))
            print("Загрузка Silver (новых строк / всего затронуто):")
            print_table(["таблица", "новых", "всего"], cur.fetchall())
            conn.commit()

            print()
            cur.execute(
                "SELECT o_rule, o_status, o_total, o_failed, o_share, o_severity FROM meta.run_dq(%s)",
                (args.dq_date,),
            )
            dq_rows = cur.fetchall()
            conn.commit()

        print("Проверки качества:")
        print_table(["правило", "статус", "проверено", "не прошло", "доля", "важность"], dq_rows)

        n_fail = sum(1 for r in dq_rows if r[1] == "fail")
        n_warn = sum(1 for r in dq_rows if r[1] == "warn")
        print(f"\nИтого: fail={n_fail}, warn={n_warn}, pass={len(dq_rows) - n_fail - n_warn}")
        return 1 if n_fail else 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
