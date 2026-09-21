from __future__ import annotations

import re
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import openpyxl
import pandas as pd
import requests


CATEGORIES = {"一般企业", "银行", "证券", "其他金融"}
STATUSES = {"正常", "退市"}
BANK_INDUSTRIES = {"银行", "银行Ⅱ"}
SECURITIES_INDUSTRIES = {"证券", "证券Ⅱ"}
OTHER_FINANCE_INDUSTRIES = {"保险", "保险Ⅱ", "多元金融"}
OUTPUT_COLUMNS = [
    "股票代码",
    "股票名称",
    "交易所",
    "东方财富行业",
    "分类",
    "原始状态码",
    "状态",
]
EASTMONEY_URLS = (
    "https://82.push2.eastmoney.com/api/qt/clist/get",
    "https://81.push2.eastmoney.com/api/qt/clist/get",
    "https://28.push2.eastmoney.com/api/qt/clist/get",
    "https://7.push2.eastmoney.com/api/qt/clist/get",
)
MARKET_FILTER = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"
FIELDS = "f12,f13,f14,f100,f292"


class StockListError(RuntimeError):
    """Raised when the stock list is incomplete or internally inconsistent."""


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def classify_industry(industry: object) -> str:
    cleaned = _clean_text(industry)
    if cleaned in BANK_INDUSTRIES:
        return "银行"
    if cleaned in SECURITIES_INDUSTRIES:
        return "证券"
    if cleaned in OTHER_FINANCE_INDUSTRIES:
        return "其他金融"
    return "一般企业"


def classify_status(name: object, raw_status: object) -> str:
    cleaned_name = _clean_text(name)
    cleaned_status = _clean_text(raw_status)
    if (
        cleaned_status == "7"
        or cleaned_name.startswith("退市")
        or cleaned_name.endswith("退")
    ):
        return "退市"
    return "正常"


def identify_exchange(code: str, market: object) -> str:
    if not re.fullmatch(r"\d{6}", code):
        raise StockListError(f"股票代码必须是六位数字: {code!r}")

    try:
        market_number = int(str(market).strip())
    except (TypeError, ValueError) as exc:
        raise StockListError(f"无效的东方财富市场标识: {market!r}") from exc

    if code.startswith(("60", "68")):
        expected_market, exchange = 1, "上海证券交易所"
    elif code.startswith(("00", "30")):
        expected_market, exchange = 0, "深圳证券交易所"
    elif code.startswith(("4", "8", "92")):
        expected_market, exchange = 0, "北京证券交易所"
    else:
        raise StockListError(f"无法根据股票代码识别交易所: {code}")

    if market_number != expected_market:
        raise StockListError(
            f"股票 {code} 的市场标识 {market_number} 与代码所属交易所冲突"
        )
    return exchange


def normalize_stock(raw: dict[str, Any]) -> dict[str, str]:
    required = ("f12", "f13", "f14", "f100", "f292")
    missing = [field for field in required if field not in raw]
    if missing:
        raise StockListError(f"东方财富记录缺少必需字段: {', '.join(missing)}")

    code = _clean_text(raw["f12"])
    name = _clean_text(raw["f14"])
    industry = _clean_text(raw["f100"])
    raw_status = _clean_text(raw["f292"])
    if not name:
        raise StockListError(f"股票名称不能为空，代码: {code or '<空>'}")

    return {
        "股票代码": code,
        "股票名称": name,
        "交易所": identify_exchange(code, raw["f13"]),
        "东方财富行业": industry,
        "分类": classify_industry(industry),
        "原始状态码": raw_status,
        "状态": classify_status(name, raw_status),
    }


def validate_stocks(stocks: list[dict[str, str]], expected_total: int) -> None:
    if expected_total <= 0 or not stocks:
        raise StockListError("东方财富返回的 A 股列表为空")
    if len(stocks) != expected_total:
        raise StockListError(
            f"抓取数量与接口总数不一致: 抓取 {len(stocks)}，接口总数 {expected_total}"
        )

    codes: list[str] = []
    for index, stock in enumerate(stocks, start=1):
        missing = [column for column in OUTPUT_COLUMNS if column not in stock]
        if missing:
            raise StockListError(f"第 {index} 条规范化记录缺少字段: {', '.join(missing)}")
        code = stock["股票代码"]
        if not re.fullmatch(r"\d{6}", code):
            raise StockListError(f"股票代码必须是六位数字: {code!r}")
        if not stock["股票名称"].strip():
            raise StockListError(f"股票名称不能为空，代码: {code}")
        if stock["分类"] not in CATEGORIES:
            raise StockListError(f"股票 {code} 的分类无效: {stock['分类']!r}")
        if stock["状态"] not in STATUSES:
            raise StockListError(f"股票 {code} 的状态无效: {stock['状态']!r}")
        codes.append(code)

    duplicate_codes = sorted(code for code, count in Counter(codes).items() if count > 1)
    if duplicate_codes:
        raise StockListError(f"发现重复股票代码: {', '.join(duplicate_codes[:10])}")


def _request_page(
    request_get: Callable[..., Any],
    params: dict[str, object],
    timeout: tuple[float, float],
    max_retries: int,
    sleep: Callable[[float], None],
) -> Any:
    for attempt in range(max_retries):
        endpoint = EASTMONEY_URLS[attempt % len(EASTMONEY_URLS)]
        try:
            response = request_get(endpoint, params=params, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            status_code = getattr(exc.response, "status_code", None)
            if status_code is not None and status_code < 500:
                raise StockListError(f"东方财富请求失败: HTTP {status_code}") from exc
            error = exc
        except requests.RequestException as exc:
            error = exc

        if attempt + 1 < max_retries:
            sleep(float(min(2**attempt, 15)))

    raise StockListError(f"东方财富请求重试 {max_retries} 次后仍失败: {error}") from error


def _parse_page(response: Any, page: int) -> tuple[int, list[dict[str, Any]]]:
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise StockListError(f"东方财富第 {page} 页不是有效 JSON") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise StockListError(f"东方财富第 {page} 页缺少 data 对象")
    data = payload["data"]
    if "total" not in data:
        raise StockListError(f"东方财富第 {page} 页缺少 data.total")
    if "diff" not in data:
        raise StockListError(f"东方财富第 {page} 页缺少 data.diff")

    try:
        total = int(data["total"])
    except (TypeError, ValueError) as exc:
        raise StockListError(f"东方财富第 {page} 页的 total 无效: {data['total']!r}") from exc

    diff = data["diff"]
    if isinstance(diff, dict):
        rows = list(diff.values())
    elif isinstance(diff, list):
        rows = diff
    else:
        raise StockListError(f"东方财富第 {page} 页的 diff 必须是列表或对象")
    if not all(isinstance(row, dict) for row in rows):
        raise StockListError(f"东方财富第 {page} 页包含非对象记录")
    return total, rows


def fetch_a_share_stocks(
    request_get: Callable[..., Any] = requests.get,
    page_size: int = 500,
    max_retries: int = 8,
    timeout: tuple[float, float] = (5.0, 20.0),
    page_delay_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, str]]:
    if page_size <= 0:
        raise StockListError("page_size 必须大于 0")
    if max_retries <= 0:
        raise StockListError("max_retries 必须大于 0")
    if page_delay_seconds < 0:
        raise StockListError("page_delay_seconds 不能小于 0")

    base_params: dict[str, object] = {
        "pz": page_size,
        "po": 1,
        "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2,
        "invt": 2,
        "fid": "f12",
        "fs": MARKET_FILTER,
        "fields": FIELDS,
    }
    raw_rows: list[dict[str, Any]] = []
    expected_total: int | None = None
    page = 1

    while expected_total is None or len(raw_rows) < expected_total:
        params = {**base_params, "pn": page}
        response = _request_page(request_get, params, timeout, max_retries, sleep)
        page_total, page_rows = _parse_page(response, page)
        if expected_total is None:
            expected_total = page_total
        elif page_total != expected_total:
            raise StockListError(
                f"东方财富分页总数发生变化: 首页面 {expected_total}，第 {page} 页 {page_total}"
            )
        if not page_rows and len(raw_rows) < expected_total:
            raise StockListError(f"东方财富第 {page} 页为空页，但尚未达到接口总数")
        raw_rows.extend(page_rows)
        if len(raw_rows) < expected_total and page_delay_seconds > 0:
            sleep(page_delay_seconds)
        page += 1

    assert expected_total is not None
    stocks = [normalize_stock(row) for row in raw_rows]
    validate_stocks(stocks, expected_total)
    return stocks


def export_stock_lists(
    stocks: list[dict[str, str]], output_dir: Path, file_date: date
) -> tuple[Path, Path]:
    validate_stocks(stocks, len(stocks))
    sorted_stocks = sorted(stocks, key=lambda stock: stock["股票代码"])
    frame = pd.DataFrame(sorted_stocks, columns=OUTPUT_COLUMNS)

    output_dir = Path(output_dir)
    csv_path = output_dir / f"a_share_list_{file_date.isoformat()}.csv"
    xlsx_path = output_dir / f"a_share_list_{file_date.isoformat()}.xlsx"
    csv_temp: Path | None = None
    xlsx_temp: Path | None = None

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=output_dir, prefix=".a_share_list_", suffix=".csv", delete=False
        ) as temp_file:
            csv_temp = Path(temp_file.name)
        with tempfile.NamedTemporaryFile(
            dir=output_dir, prefix=".a_share_list_", suffix=".xlsx", delete=False
        ) as temp_file:
            xlsx_temp = Path(temp_file.name)

        frame.to_csv(csv_temp, index=False, encoding="utf-8-sig")
        frame.to_excel(xlsx_temp, index=False, engine="openpyxl")

        workbook = openpyxl.load_workbook(xlsx_temp)
        worksheet = workbook.active
        for cell in worksheet["A"][1:]:
            cell.number_format = "@"
        workbook.save(xlsx_temp)

        csv_temp.replace(csv_path)
        csv_temp = None
        xlsx_temp.replace(xlsx_path)
        xlsx_temp = None
    except Exception as exc:
        raise StockListError(f"导出股票名单失败: {exc}") from exc
    finally:
        for temp_path in (csv_temp, xlsx_temp):
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    return csv_path, xlsx_path
