"""Выгрузка статистики Яндекс.Директа в CSV.

    python -m src.fetch --from 2026-01-01 --to 2026-08-04
    python -m src.fetch --all-time
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

from .direct import DirectClient, DirectError

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

REPORTS = (
    {
        "title": "Статистика по дням",
        "file": "days.csv",
        "report_type": "CUSTOM_REPORT",
        "fields": [
            "Date",
            "CampaignName",
            "AdNetworkType",
            "Impressions",
            "Clicks",
            "Ctr",
            "Cost",
            "AvgCpc",
            "Conversions",
            "CostPerConversion",
            "BounceRate",
        ],
    },
    {
        "title": "Поисковые запросы",
        "file": "queries.csv",
        # Поле Query доступно только в этом типе отчёта, в CUSTOM_REPORT запрещено.
        "report_type": "SEARCH_QUERY_PERFORMANCE_REPORT",
        "fields": [
            "Date",
            "CampaignName",
            "Query",
            "Impressions",
            "Clicks",
            "Ctr",
            "Cost",
            "Conversions",
        ],
    },
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.fetch",
        description="Выгружает статистику Яндекс.Директа в data/*.csv",
    )
    parser.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="начало периода")
    parser.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="конец периода")
    parser.add_argument("--all-time", action="store_true", help="за всё время")
    parser.add_argument(
        "--out",
        type=Path,
        default=DATA_DIR,
        metavar="DIR",
        help=f"куда сложить CSV (по умолчанию {DATA_DIR})",
    )
    parser.add_argument(
        "--no-vat",
        action="store_true",
        help="суммы без НДС (по умолчанию с НДС — как в счёте)",
    )
    args = parser.parse_args(argv)

    if args.all_time:
        if args.date_from or args.date_to:
            parser.error("--all-time нельзя сочетать с --from/--to")
    elif not (args.date_from and args.date_to):
        parser.error("укажи период: --from ... --to ... либо --all-time")
    else:
        start = _parse_date(parser, args.date_from, "--from")
        end = _parse_date(parser, args.date_to, "--to")
        if start > end:
            parser.error("--from позже, чем --to")

    return args


def _parse_date(parser: argparse.ArgumentParser, value: str, flag: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        parser.error(f"{flag}: ожидается дата в формате YYYY-MM-DD, получено {value!r}")


def write_csv(path: Path, fields: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        # Заголовок берём из списка полей: мы просили skipColumnHeader,
        # поэтому в ответе Директа названий колонок нет.
        writer.writerow(fields)
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    date_range_type = "ALL_TIME" if args.all_time else "CUSTOM_DATE"
    period = "за всё время" if args.all_time else f"{args.date_from} — {args.date_to}"

    client = DirectClient(include_vat=not args.no_vat)
    failed = []

    print(f"Период: {period}\n")
    for report in REPORTS:
        print(f"{report['title']} ({report['report_type']})...")
        try:
            rows = client.get_report(
                report_type=report["report_type"],
                field_names=report["fields"],
                date_range_type=date_range_type,
                date_from=args.date_from,
                date_to=args.date_to,
            )
        except DirectError as error:
            # Один упавший отчёт не должен уносить второй: у них разные
            # ограничения по глубине данных, и поисковый падает чаще.
            print(f"  ОШИБКА: {error}\n", file=sys.stderr)
            failed.append(report["file"])
            continue

        path = args.out / report["file"]
        write_csv(path, report["fields"], rows)
        print(f"  {len(rows)} строк -> {path}\n")

    if failed:
        print(f"Не выгружено: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
