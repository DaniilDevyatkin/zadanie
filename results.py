# -*- coding: utf-8 -*-
"""
Ведомость результатов для преподавателя.

    python results.py                 # таблица по всем попыткам
    python results.py --csv out.csv   # выгрузка в CSV
    python results.py --test 3is-1-2026-10-17
    python results.py --best          # только лучшая попытка каждого студента

Файл ничего не публикует в веб: студентов в него не пускают, разбора ошибок
здесь тоже нет — только баллы, процент, вердикт и время.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

from paths import data_dir

DB_PATH = data_dir() / "attempts.sqlite3"


def load(test_id: str | None, best_only: bool) -> list[sqlite3.Row]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT id, test_id, test_title, student, score, total, percent,
               passing_percent, passed, answered, duration_sec, flags, created_at
        FROM attempts
        WHERE (? IS NULL OR test_id = ?)
        ORDER BY test_id, student COLLATE NOCASE, id
    """
    rows = conn.execute(sql, (test_id, test_id)).fetchall()
    conn.close()

    if not best_only:
        return rows

    best: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        key = (row["test_id"], row["student"].lower().replace("ё", "е"))
        current = best.get(key)
        if current is None or row["score"] > current["score"]:
            best[key] = row
    return sorted(best.values(), key=lambda r: (r["test_id"], r["student"].lower()))


def fmt_duration(seconds: int | None) -> str:
    total = max(0, int(seconds or 0))
    return f"{total // 60:02d}:{total % 60:02d}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Ведомость результатов кабинета заданий")
    parser.add_argument("--test", default=None, help="фильтр по идентификатору задания")
    parser.add_argument("--csv", default=None, help="сохранить в CSV")
    parser.add_argument("--best", action="store_true", help="только лучшая попытка студента")
    args = parser.parse_args()

    rows = load(args.test, args.best)
    if not rows:
        print("Попыток пока нет.")
        return 0

    headers = ["Фамилия", "Задание", "Баллы", "%", "Порог", "Итог", "Отмечено", "Время", "Завершено", "Флаги"]
    printed: list[list[str]] = []
    for row in rows:
        printed.append([
            row["student"],
            row["test_id"],
            f"{row['score']}/{row['total']}",
            f"{row['percent']:.1f}",
            f"{row['passing_percent']:.0f}",
            "зачёт" if row["passed"] else "не зачёт",
            f"{row['answered']}/{row['total']}",
            fmt_duration(row["duration_sec"]),
            (row["created_at"] or "").replace("T", " ")[:16],
            row["flags"] or "—",
        ])

    widths = [max(len(headers[i]), *(len(line[i]) for line in printed)) for i in range(len(headers))]
    line = "  ".join(headers[i].ljust(widths[i]) for i in range(len(headers)))
    print(line)
    print("-" * len(line))
    for item in printed:
        print("  ".join(item[i].ljust(widths[i]) for i in range(len(headers))))

    passed = sum(1 for row in rows if row["passed"])
    print(f"\nВсего попыток: {len(rows)} · зачёт: {passed} · не зачёт: {len(rows) - passed}")

    if args.csv:
        target = Path(args.csv)
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(headers)
            writer.writerows(printed)
        print(f"CSV сохранён: {target.resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
