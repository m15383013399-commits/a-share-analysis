#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_diary.stock_list import (  # noqa: E402
    StockListError,
    export_stock_lists,
    fetch_a_share_stocks,
)


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须使用 YYYY-MM-DD 格式") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("日期必须使用 YYYY-MM-DD 格式")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从东方财富抓取沪深京全部 A 股并生成四分类名单。"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "stock_lists",
        help="输出目录（默认: data/stock_lists）",
    )
    parser.add_argument(
        "--date",
        type=parse_date,
        default=date.today(),
        help="输出文件日期，格式 YYYY-MM-DD（默认: 今天）",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    fetcher: Callable[[], list[dict[str, str]]] = fetch_a_share_stocks,
    exporter: Callable[
        [list[dict[str, str]], Path, date], tuple[Path, Path]
    ] = export_stock_lists,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        stocks = fetcher()
        csv_path, xlsx_path = exporter(stocks, args.output_dir, args.date)
    except StockListError as exc:
        print(f"抓取失败: {exc}", file=stderr)
        return 1

    counts = Counter(stock["分类"] for stock in stocks)
    print(f"共抓取 {len(stocks)} 只 A 股", file=stdout)
    for category in ("一般企业", "银行", "证券", "其他金融"):
        print(f"{category}: {counts[category]}", file=stdout)
    print(f"CSV: {csv_path.resolve()}", file=stdout)
    print(f"Excel: {xlsx_path.resolve()}", file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
