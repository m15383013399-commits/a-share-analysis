from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .analyze import AnalysisResult
from .models import MarketGateResult, QualityResult
from .utils import now_text


INDEX_ORDER: tuple[tuple[str, str], ...] = (
    ("000001", "上证指数"),
    ("399001", "深证成指"),
    ("399006", "创业板指"),
    ("000688", "科创50"),
    ("000905", "中证500"),
    ("000300", "沪深300"),
)
SOURCE_URLS: dict[str, str] = {
    "tencent": "https://qt.gtimg.cn/q=",
    "sina": "https://vip.stock.finance.sina.com.cn/",
    "eastmoney": "https://push2.eastmoney.com/",
    "akshare": "https://akshare.akfamily.xyz/",
}


def render_market_status(
    gate: MarketGateResult, report_name: str = "A股市场日报"
) -> str:
    """Render a non-close gate result without leaking market analysis."""

    lines = [f"# {report_name} - {_text(gate.trade_date)}", ""]
    lines.append(f"- 状态：{_text(gate.status)}")
    lines.append(f"- 原因：{_text(gate.reason)}")
    if gate.next_trade_date:
        lines.append(f"- 下一交易日：{_text(gate.next_trade_date)}")
    lines.extend(["", "> 不构成投资建议。", ""])
    return "\n".join(lines)


def render_quality_failure(
    snapshot: Mapping[str, Any],
    quality: QualityResult,
    report_name: str = "A股市场日报",
) -> str:
    """Render a fail-closed quality result and only its audit evidence."""

    lines = [
        f"# {report_name} - {_text(snapshot.get('trade_date'))}",
        "",
        f"> 数据质量：{_text(quality.status)}",
        "",
        "## 必需数据错误",
        "",
    ]
    if quality.required_errors:
        lines.extend(f"- {_text(item)}" for item in quality.required_errors)
    else:
        lines.append("- -")
    if quality.optional_warnings:
        lines.extend(["", "## 可选数据警告", ""])
        lines.extend(f"- {_text(item)}" for item in quality.optional_warnings)
    lines.extend(_sources_section(snapshot.get("sources"), "已获取来源事实"))
    lines.extend(
        [
            "已停止生成正式收盘结论与累计成绩更新。",
            "",
            "> 不构成投资建议。",
            "",
        ]
    )
    return "\n".join(lines)


def render_close_report(
    result: AnalysisResult,
    *,
    evaluation_summary: Mapping[str, Any] | None = None,
    report_name: str = "A股市场日报",
) -> str:
    """Render formal close facts, explicitly separated judgement and audit metrics."""

    lines = [
        f"# {report_name} - {_text(result.trade_date)}",
        "",
        f"> 生成时间：{now_text()}  ",
        "> 报告类型：正式收盘事实与评价摘要  ",
        f"> 数据标识：{_text(result.source)}  ",
        "> 用途：复盘和研究，不构成投资建议。",
        "",
        "## 正式收盘事实",
        "",
    ]
    lines.extend(_indices_section(result))
    lines.extend(_breadth_section(result))
    lines.extend(_limit_ladder_section(result))
    lines.extend(_top_stocks_section(result))
    lines.extend(_news_section(result))

    lines.extend(["## 代理口径与数据说明", ""])
    if result.hot_boards or result.industry_boards or result.concept_boards:
        lines.append("- 行业与概念分类采用数据源的板块口径，作为市场主线的代理口径。")
    else:
        lines.append("- 板块代理数据未提供，本报告不补零或补写结论。")
    lines.append("")
    lines.extend(_boards_section("主线强度榜（代理口径）", result.hot_boards))
    lines.extend(_boards_section("行业板块（代理口径）", result.industry_boards))
    lines.extend(_boards_section("概念板块（代理口径）", result.concept_boards))

    lines.extend(["## Codex推断", "", "> 以下是基于上述已获取事实的定性判断，不是行情原始事实。", ""])
    lines.extend(_market_temperature_section(result))
    lines.extend(_text_section("短期推演", result.inferences, inference=True))
    lines.extend(_text_section("风险提示", result.risks, inference=True))
    lines.extend(_text_section("明日观察信号", result.watch_signals, inference=True))

    lines.extend(_evaluation_section(evaluation_summary))
    warnings = _optional_warnings(result)
    if warnings:
        lines.extend(["## 可选数据警告", ""])
        lines.extend(f"- {_text(item)}" for item in warnings)
        lines.append("")
    lines.extend(_sources_section(result.sources, "数据来源"))
    lines.extend(["> 不构成投资建议。", ""])
    return "\n".join(lines)


def render_markdown(result: AnalysisResult, report_name: str = "A股市场日报") -> str:
    """Compatibility wrapper for legacy callers."""

    return render_close_report(result, report_name=report_name)


def _market_temperature_section(result: AnalysisResult) -> list[str]:
    breadth = result.breadth if isinstance(result.breadth, Mapping) else {}
    temp = result.market_temperature if isinstance(result.market_temperature, Mapping) else {}
    if not temp:
        return []
    lines = [
        "## 市场温度",
        "",
        "> 类型：Codex推断",
        "",
        f"- 温度：{_text(temp.get('label'))}（{_number(temp.get('score'), 1)}/100）",
        f"- 涨跌家数：上涨 {_integer(breadth.get('up'))} / 下跌 {_integer(breadth.get('down'))} / 平盘 {_integer(breadth.get('flat'))}，上涨占比 {_ratio(breadth.get('adv_ratio'))}",
        f"- 平均涨跌幅：{_pct(breadth.get('avg_pct'))}，中位涨跌幅：{_pct(breadth.get('median_pct'))}",
        f"- 两市成交额：{_money(breadth.get('turnover'))}",
        f"- 涨停数量：{_integer(breadth.get('limit_up'))}，跌停代理估算：{_integer(breadth.get('limit_down_est'))}",
        "",
    ]
    components = temp.get("components")
    if isinstance(components, Mapping) and components:
        lines.extend(["温度拆分：", "", "| 项目 | 分数 |", "| --- | ---: |"])
        for key, value in components.items():
            lines.append(f"| {_cell(key)} | {_number(value, 1)} |")
        lines.append("")
    return lines


def _indices_section(result: AnalysisResult) -> list[str]:
    rows = _ordered_indices(result.indices)
    lines = ["六指数收盘事实：", "", "| 指数 | 收盘 | 涨跌幅 | 成交额 |", "| --- | ---: | ---: | ---: |"]
    for row in rows:
        lines.append(
            f"| {_cell(row.get('name'))} | {_number(row.get('price'), 2)} | "
            f"{_pct(row.get('pct'))} | {_money(row.get('amount'))} |"
        )
    lines.append("")
    return lines


def _ordered_indices(rows: Any) -> list[Mapping[str, Any]]:
    source_rows = [row for row in rows or [] if isinstance(row, Mapping)]
    by_code = {str(row.get("code") or ""): row for row in source_rows if row.get("code")}
    ordered: list[Mapping[str, Any]] = []
    for code, name in INDEX_ORDER:
        row = by_code.get(code)
        if row is None:
            ordered.append({"code": code, "name": name, "price": None, "pct": None, "amount": None})
        else:
            ordered.append({**dict(row), "code": code, "name": name})
    return ordered


def _breadth_section(result: AnalysisResult) -> list[str]:
    breadth = result.breadth if isinstance(result.breadth, Mapping) else {}
    sh_amount = breadth.get("sh_amount")
    sz_amount = breadth.get("sz_amount")
    total_amount = breadth.get("turnover")
    if total_amount is None and _finite(sh_amount) is not None and _finite(sz_amount) is not None:
        total_amount = _finite(sh_amount) + _finite(sz_amount)
    return [
        "市场宽度与成交事实：",
        "",
        "| 有效样本 | 上涨 | 下跌 | 平盘 | 上涨占比 |",
        "| ---: | ---: | ---: | ---: | ---: |",
        f"| {_integer(breadth.get('total'))} | {_integer(breadth.get('up'))} | {_integer(breadth.get('down'))} | {_integer(breadth.get('flat'))} | {_ratio(breadth.get('adv_ratio'))} |",
        "",
        f"- 沪市成交额：{_money(sh_amount)}",
        f"- 深市成交额：{_money(sz_amount)}",
        f"- 两市总成交额（沪+深）：{_money(total_amount)}",
        "",
    ]


def _boards_section(title: str, rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    lines = [
        f"### {title}",
        "",
        "| 排名 | 类型 | 板块 | 涨跌幅 | 成交额 | 上涨/下跌 | 领涨 | 强度分 |",
        "| ---: | --- | --- | ---: | ---: | ---: | --- | ---: |",
    ]
    for index, row in enumerate(rows, start=1):
        up = row.get("up_count")
        down = row.get("down_count")
        up_down = f"{_integer(up)}/{_integer(down)}" if up is not None or down is not None else "-"
        leader = _text(row.get("leader"))
        if row.get("leader_pct") is not None:
            leader = f"{leader} {_pct(row.get('leader_pct'))}"
        lines.append(
            f"| {index} | {_cell(row.get('kind'))} | {_cell(row.get('name'))} | "
            f"{_pct(row.get('pct'))} | {_money(row.get('amount'))} | {up_down} | "
            f"{_cell(leader)} | {_number(row.get('score'), 2)} |"
        )
    lines.append("")
    return lines


def _limit_ladder_section(result: AnalysisResult) -> list[str]:
    if not result.limit_ladder:
        return []
    lines = ["涨停梯队事实：", "", "| 连板高度 | 数量 | 代表个股 |", "| ---: | ---: | --- |"]
    for row in result.limit_ladder:
        names = row.get("names")
        names_text = "、".join(str(item) for item in names) if isinstance(names, Sequence) and not isinstance(names, str) else "-"
        height = f"{_integer(row.get('height'))}板" if row.get("height") is not None else "-"
        lines.append(f"| {height} | {_integer(row.get('count'))} | {_cell(names_text)} |")
    lines.append("")
    return lines


def _top_stocks_section(result: AnalysisResult) -> list[str]:
    if not result.top_stocks:
        return []
    lines = [
        "强势个股样本事实：",
        "",
        "| 排名 | 代码 | 名称 | 涨跌幅 | 收盘 | 成交额 |",
        "| ---: | --- | --- | ---: | ---: | ---: |",
    ]
    for index, row in enumerate(result.top_stocks[:10], start=1):
        lines.append(
            f"| {index} | {_cell(row.get('code'))} | {_cell(row.get('name'))} | "
            f"{_pct(row.get('pct'))} | {_number(row.get('price'), 2)} | {_money(row.get('amount'))} |"
        )
    lines.append("")
    return lines


def _news_section(result: AnalysisResult) -> list[str]:
    if not result.news:
        return []
    lines = ["消息来源事实：", ""]
    for item in result.news:
        source = f"（{_text(item.get('source'))}）" if item.get("source") else ""
        when = f"{_text(item.get('time'))} " if item.get("time") else ""
        title = _text(item.get("title"))
        url = item.get("url")
        lines.append(f"- {when}[{title}]({url}){source}" if url else f"- {when}{title}{source}")
    lines.append("")
    return lines


def _text_section(title: str, items: list[str], *, inference: bool = False) -> list[str]:
    if not items:
        return []
    lines = [f"## {title}", ""]
    if inference:
        lines.extend(["> 类型：Codex推断", ""])
    lines.extend(f"- {_text(item)}" for item in items)
    lines.append("")
    return lines


def _evaluation_section(summary: Mapping[str, Any] | None) -> list[str]:
    summary = summary if isinstance(summary, Mapping) else {}
    labels = (("indices", "指数"), ("sectors", "板块代理"), ("stocks", "个股"), ("overall", "合计"))
    lines = [
        "## 累计评价摘要",
        "",
        "| 类别 | 样本数 | 方向命中率 | 区间命中率 | MAE（百分点） |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for key, label in labels:
        metrics = summary.get(key)
        metrics = metrics if isinstance(metrics, Mapping) else {}
        lines.append(
            f"| {label} | {_integer(metrics.get('sample_count'))} | "
            f"{_ratio(metrics.get('direction_hit_rate'))} | {_ratio(metrics.get('range_hit_rate'))} | "
            f"{_number(metrics.get('mean_absolute_error'), 2)} |"
        )
    lines.append("")
    return lines


def _sources_section(value: Any, title: str) -> list[str]:
    lines = ["", f"## {title}", ""]
    if not isinstance(value, Mapping) or not value:
        lines.extend(["- 未提供可展示的来源元数据。", ""])
        return lines
    found = False
    for dataset, raw in value.items():
        if not isinstance(raw, Mapping):
            continue
        source = raw.get("source")
        url = raw.get("url") or raw.get("endpoint")
        if not url and isinstance(source, str):
            url = SOURCE_URLS.get(source.lower())
        fetched_at = raw.get("fetched_at")
        if source is None and url is None and fetched_at is None:
            continue
        found = True
        source_text = _text(source)
        if url:
            source_text = f"[{source_text}]({url})"
        suffix = f"，获取时间 {_text(fetched_at)}" if fetched_at else ""
        lines.append(f"- {_text(dataset)}：{source_text}{suffix}")
    if not found:
        lines.append("- 未提供可展示的来源元数据。")
    lines.append("")
    return lines


def _optional_warnings(result: AnalysisResult) -> list[str]:
    warnings = list(result.optional_warnings)
    status_warnings = result.status.get("warnings") if isinstance(result.status, Mapping) else None
    if isinstance(status_warnings, (list, tuple)):
        warnings.extend(str(item) for item in status_warnings if item)
    return list(dict.fromkeys(warnings))


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _number(value: Any, places: int) -> str:
    number = _finite(value)
    return "-" if number is None else f"{number:.{places}f}"


def _integer(value: Any) -> str:
    number = _finite(value)
    return "-" if number is None else str(int(number))


def _pct(value: Any) -> str:
    number = _finite(value)
    return "-" if number is None else f"{number:.2f}%"


def _ratio(value: Any) -> str:
    number = _finite(value)
    return "-" if number is None else f"{number:.2%}"


def _money(value: Any) -> str:
    amount = _finite(value)
    if amount is None:
        return "-"
    if amount >= 1_000_000_000_000:
        return f"{amount / 1_000_000_000_000:.2f}万亿"
    if amount >= 100_000_000:
        return f"{amount / 100_000_000:.1f}亿"
    if amount >= 10_000:
        return f"{amount / 10_000:.1f}万"
    return f"{amount:.0f}"


def _text(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("\n", " ")


def _cell(value: Any) -> str:
    return _text(value).replace("|", "\\|")
