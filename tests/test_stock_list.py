from __future__ import annotations

import unittest
import argparse
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import openpyxl
import pandas as pd
import requests

from scripts.fetch_a_share_list import main, parse_date

from market_diary.stock_list import (
    StockListError,
    classify_industry,
    classify_status,
    export_stock_lists,
    fetch_a_share_stocks,
    identify_exchange,
    normalize_stock,
    validate_stocks,
)


class StockNormalizationTest(unittest.TestCase):
    def test_classifies_four_categories(self) -> None:
        cases = {
            "银行": "银行",
            "银行Ⅱ": "银行",
            " 证券 ": "证券",
            "证券Ⅱ": "证券",
            "保险": "其他金融",
            "保险Ⅱ": "其他金融",
            "多元金融": "其他金融",
            "软件开发": "一般企业",
            "": "一般企业",
            None: "一般企业",
        }

        for industry, expected in cases.items():
            with self.subTest(industry=industry):
                self.assertEqual(classify_industry(industry), expected)

    def test_identifies_all_three_exchanges(self) -> None:
        cases = [
            ("600000", 1, "上海证券交易所"),
            ("688001", "1", "上海证券交易所"),
            ("000001", 0, "深圳证券交易所"),
            ("300001", "0", "深圳证券交易所"),
            ("430001", 0, "北京证券交易所"),
            ("830001", 0, "北京证券交易所"),
            ("920001", 0, "北京证券交易所"),
        ]

        for code, market, expected in cases:
            with self.subTest(code=code):
                self.assertEqual(identify_exchange(code, market), expected)

    def test_normalizes_eastmoney_record(self) -> None:
        record = normalize_stock(
            {
                "f12": "000001",
                "f13": 0,
                "f14": " 平安银行 ",
                "f100": " 银行 ",
                "f292": 2,
            }
        )

        self.assertEqual(
            record,
            {
                "股票代码": "000001",
                "股票名称": "平安银行",
                "交易所": "深圳证券交易所",
                "东方财富行业": "银行",
                "分类": "银行",
                "原始状态码": "2",
                "状态": "正常",
            },
        )

    def test_classifies_normal_and_delisted_statuses(self) -> None:
        cases = [
            ("平安银行", 2, "正常"),
            ("ST三木", 2, "正常"),
            ("*ST美丽", 2, "正常"),
            ("普通公司", 7, "退市"),
            ("退市银鸽", 2, "退市"),
            ("国华退", 2, "退市"),
            ("退休产业", 2, "正常"),
        ]

        for name, raw_status, expected in cases:
            with self.subTest(name=name, raw_status=raw_status):
                self.assertEqual(classify_status(name, raw_status), expected)

    def test_rejects_market_code_conflict(self) -> None:
        with self.assertRaisesRegex(StockListError, "市场标识"):
            identify_exchange("600000", 0)

    def test_rejects_invalid_code(self) -> None:
        with self.assertRaisesRegex(StockListError, "六位数字"):
            identify_exchange("ABC", 0)

    def test_rejects_empty_name(self) -> None:
        with self.assertRaisesRegex(StockListError, "股票名称"):
            normalize_stock(
                {"f12": "000001", "f13": 0, "f14": "  ", "f100": "银行", "f292": 2}
            )

    def test_rejects_missing_required_field(self) -> None:
        with self.assertRaisesRegex(StockListError, "f100"):
            normalize_stock({"f12": "000001", "f13": 0, "f14": "平安银行", "f292": 2})

    def test_rejects_missing_status_field(self) -> None:
        with self.assertRaisesRegex(StockListError, "f292"):
            normalize_stock({"f12": "000001", "f13": 0, "f14": "平安银行", "f100": "银行"})


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self.payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self) -> object:
        return self.payload


class StockFetcherTest(unittest.TestCase):
    @staticmethod
    def row(
        code: str, market: int, name: str, industry: object, status: object = 2
    ) -> dict[str, object]:
        return {"f12": code, "f13": market, "f14": name, "f100": industry, "f292": status}

    def test_fetches_and_merges_multiple_pages(self) -> None:
        calls: list[int] = []
        pages = {
            1: {
                "data": {
                    "total": 3,
                    "diff": {
                        "0": self.row("000001", 0, "平安银行", "银行"),
                        "1": self.row("600000", 1, "浦发银行", "银行"),
                    },
                }
            },
            2: {
                "data": {
                    "total": 3,
                    "diff": [self.row("430001", 0, "北交样例", "软件开发")],
                }
            },
        }

        def get(url: str, *, params: dict[str, object], timeout: object) -> FakeResponse:
            del url, timeout
            page = int(params["pn"])
            calls.append(page)
            return FakeResponse(200, pages[page])

        result = fetch_a_share_stocks(request_get=get, page_size=2, sleep=lambda _: None)

        self.assertEqual(calls, [1, 2])
        self.assertEqual([item["股票代码"] for item in result], ["000001", "600000", "430001"])

    def test_waits_between_successful_pages(self) -> None:
        sleeps: list[float] = []

        def get(url: str, *, params: dict[str, object], timeout: object) -> FakeResponse:
            del url, timeout
            page = int(params["pn"])
            if page == 1:
                rows = [self.row("000001", 0, "平安银行", "银行")]
            else:
                rows = [self.row("600000", 1, "浦发银行", "银行")]
            return FakeResponse(200, {"data": {"total": 2, "diff": rows}})

        result = fetch_a_share_stocks(
            request_get=get,
            page_size=1,
            page_delay_seconds=1.0,
            sleep=sleeps.append,
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(sleeps, [1.0])

    def test_retries_http_500_then_succeeds(self) -> None:
        attempts = 0

        def get(url: str, *, params: dict[str, object], timeout: object) -> FakeResponse:
            nonlocal attempts
            del url, params, timeout
            attempts += 1
            if attempts == 1:
                return FakeResponse(500, {})
            return FakeResponse(
                200,
                {"data": {"total": 1, "diff": [self.row("000001", 0, "平安银行", "银行")] }},
            )

        result = fetch_a_share_stocks(request_get=get, sleep=lambda _: None)

        self.assertEqual(attempts, 2)
        self.assertEqual(len(result), 1)

    def test_switches_endpoint_after_connection_error(self) -> None:
        urls: list[str] = []

        def get(url: str, *, params: dict[str, object], timeout: object) -> FakeResponse:
            del params, timeout
            urls.append(url)
            if len(urls) == 1:
                raise requests.ConnectionError("remote disconnected")
            return FakeResponse(
                200,
                {"data": {"total": 1, "diff": [self.row("000001", 0, "平安银行", "银行")] }},
            )

        result = fetch_a_share_stocks(request_get=get, sleep=lambda _: None)

        self.assertEqual(len(result), 1)
        self.assertEqual(len(urls), 2)
        self.assertNotEqual(urls[0], urls[1])
        for url in urls:
            self.assertRegex(url, r"https://\d+\.push2\.eastmoney\.com/")

    def test_uses_exponential_backoff_and_cycles_all_endpoints(self) -> None:
        urls: list[str] = []
        sleeps: list[float] = []

        def get(url: str, *, params: dict[str, object], timeout: object) -> FakeResponse:
            del params, timeout
            urls.append(url)
            if len(urls) <= 4:
                raise requests.ConnectionError("temporarily blocked")
            return FakeResponse(
                200,
                {"data": {"total": 1, "diff": [self.row("000001", 0, "平安银行", "银行")] }},
            )

        result = fetch_a_share_stocks(
            request_get=get,
            max_retries=5,
            page_delay_seconds=0,
            sleep=sleeps.append,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(len(set(urls[:4])), 4)
        self.assertEqual(urls[0], urls[4])
        self.assertEqual(sleeps, [1.0, 2.0, 4.0, 8.0])

    def test_does_not_retry_http_400(self) -> None:
        attempts = 0

        def get(url: str, *, params: dict[str, object], timeout: object) -> FakeResponse:
            nonlocal attempts
            del url, params, timeout
            attempts += 1
            return FakeResponse(400, {})

        with self.assertRaisesRegex(StockListError, "HTTP 400"):
            fetch_a_share_stocks(request_get=get, sleep=lambda _: None)
        self.assertEqual(attempts, 1)

    def test_rejects_missing_response_structure(self) -> None:
        def get(url: str, *, params: dict[str, object], timeout: object) -> FakeResponse:
            del url, params, timeout
            return FakeResponse(200, {"data": {"total": 1}})

        with self.assertRaisesRegex(StockListError, "diff"):
            fetch_a_share_stocks(request_get=get, sleep=lambda _: None)

    def test_rejects_empty_page_before_total_is_reached(self) -> None:
        def get(url: str, *, params: dict[str, object], timeout: object) -> FakeResponse:
            del url, timeout
            if int(params["pn"]) == 1:
                return FakeResponse(
                    200,
                    {"data": {"total": 2, "diff": [self.row("000001", 0, "平安银行", "银行")] }},
                )
            return FakeResponse(200, {"data": {"total": 2, "diff": []}})

        with self.assertRaisesRegex(StockListError, "空页"):
            fetch_a_share_stocks(request_get=get, page_size=1, sleep=lambda _: None)

    def test_rejects_count_different_from_reported_total(self) -> None:
        def get(url: str, *, params: dict[str, object], timeout: object) -> FakeResponse:
            del url, params, timeout
            return FakeResponse(
                200,
                {
                    "data": {
                        "total": 1,
                        "diff": [
                            self.row("000001", 0, "平安银行", "银行"),
                            self.row("600000", 1, "浦发银行", "银行"),
                        ],
                    }
                },
            )

        with self.assertRaisesRegex(StockListError, "总数"):
            fetch_a_share_stocks(request_get=get, sleep=lambda _: None)

    def test_validate_rejects_duplicate_codes(self) -> None:
        stock = normalize_stock(self.row("000001", 0, "平安银行", "银行"))
        with self.assertRaisesRegex(StockListError, "重复"):
            validate_stocks([stock, stock.copy()], expected_total=2)


class StockExportTest(unittest.TestCase):
    def test_exports_matching_csv_and_excel_with_text_codes(self) -> None:
        stocks = [
            normalize_stock(
                {"f12": "600000", "f13": 1, "f14": "浦发银行", "f100": "银行", "f292": 2}
            ),
            normalize_stock(
                {"f12": "000001", "f13": 0, "f14": "平安银行", "f100": "银行", "f292": 2}
            ),
        ]

        with TemporaryDirectory() as temp_dir:
            csv_path, xlsx_path = export_stock_lists(
                stocks, Path(temp_dir), date(2026, 7, 3)
            )

            self.assertEqual(csv_path.name, "a_share_list_2026-07-03.csv")
            self.assertEqual(xlsx_path.name, "a_share_list_2026-07-03.xlsx")
            self.assertEqual(csv_path.read_bytes()[:3], b"\xef\xbb\xbf")

            csv_frame = pd.read_csv(csv_path, dtype={"股票代码": str})
            xlsx_frame = pd.read_excel(xlsx_path, dtype={"股票代码": str})
            self.assertEqual(csv_frame.to_dict("records"), xlsx_frame.to_dict("records"))
            self.assertEqual(csv_frame["股票代码"].tolist(), ["000001", "600000"])
            self.assertEqual(csv_frame.columns[-2:].tolist(), ["原始状态码", "状态"])

            workbook = openpyxl.load_workbook(xlsx_path)
            worksheet = workbook.active
            self.assertEqual(worksheet["A2"].value, "000001")
            self.assertEqual(worksheet["A2"].number_format, "@")
            self.assertEqual(worksheet["A3"].number_format, "@")


class StockListCliTest(unittest.TestCase):
    def test_parse_date_accepts_iso_date(self) -> None:
        self.assertEqual(parse_date("2026-07-03"), date(2026, 7, 3))

    def test_parse_date_rejects_invalid_value(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_date("20260703")

    def test_main_prints_counts_and_output_paths(self) -> None:
        stocks = [
            normalize_stock(
                {"f12": "000001", "f13": 0, "f14": "平安银行", "f100": "银行", "f292": 2}
            ),
            normalize_stock(
                {"f12": "600030", "f13": 1, "f14": "中信证券", "f100": "证券", "f292": 2}
            ),
            normalize_stock(
                {"f12": "601318", "f13": 1, "f14": "中国平安", "f100": "保险", "f292": 2}
            ),
            normalize_stock(
                {"f12": "300001", "f13": 0, "f14": "特锐德", "f100": "电网设备", "f292": 2}
            ),
        ]
        stdout = StringIO()

        def fetcher() -> list[dict[str, str]]:
            return stocks

        def exporter(
            fetched: list[dict[str, str]], output_dir: Path, file_date: date
        ) -> tuple[Path, Path]:
            self.assertIs(fetched, stocks)
            self.assertEqual(output_dir, Path("custom-output"))
            self.assertEqual(file_date, date(2026, 7, 3))
            return Path("/tmp/list.csv"), Path("/tmp/list.xlsx")

        exit_code = main(
            ["--date", "2026-07-03", "--output-dir", "custom-output"],
            fetcher=fetcher,
            exporter=exporter,
            stdout=stdout,
        )

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("共抓取 4 只 A 股", output)
        for text in ("一般企业: 1", "银行: 1", "证券: 1", "其他金融: 1"):
            self.assertIn(text, output)
        self.assertIn("/tmp/list.csv", output)
        self.assertIn("/tmp/list.xlsx", output)

    def test_main_returns_one_when_fetch_fails(self) -> None:
        stderr = StringIO()

        def fetcher() -> list[dict[str, str]]:
            raise StockListError("网络不可用")

        exit_code = main([], fetcher=fetcher, stderr=stderr)

        self.assertEqual(exit_code, 1)
        self.assertIn("抓取失败: 网络不可用", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
