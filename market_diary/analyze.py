from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .utils import pick, to_float, top_n


@dataclass
class AnalysisResult:
    trade_date: str
    source: str
    status: dict[str, Any]
    market_temperature: dict[str, Any]
    breadth: dict[str, Any]
    indices: list[dict[str, Any]]
    hot_boards: list[dict[str, Any]]
    industry_boards: list[dict[str, Any]]
    concept_boards: list[dict[str, Any]]
    limit_ladder: list[dict[str, Any]]
    top_stocks: list[dict[str, Any]]
    news: list[dict[str, Any]]
    inferences: list[str]
    risks: list[str]
    watch_signals: list[str]
    sources: dict[str, Any] = field(default_factory=dict)
    optional_warnings: list[str] = field(default_factory=list)


class IncompleteMarketDataError(RuntimeError):
    """Raised when analysis is attempted without a passing quality gate."""


def analyze_payload(payload: dict[str, Any], config: dict[str, Any] | None = None) -> AnalysisResult:
    quality = payload.get("quality_status")
    quality_status = quality.get("status") if isinstance(quality, Mapping) else quality
    raw_sources = payload.get("sources")
    sources = dict(raw_sources) if isinstance(raw_sources, Mapping) else {}
    source = payload.get("source") or ("multi-source" if sources else "unknown")
    valid_sample = source == "sample" and quality_status == "sample_only"
    valid_market_data = source != "sample" and quality_status == "passed"
    if not (valid_sample or valid_market_data):
        errors = quality.get("required_errors", []) if isinstance(quality, Mapping) else []
        detail = f": {'; '.join(str(error) for error in errors)}" if errors else ""
        raise IncompleteMarketDataError(
            f"market data quality status is {quality_status or 'missing'}{detail}"
        )

    config = config or {}
    stocks = payload.get("stocks") or []
    sectors = payload.get("sectors")
    sectors = sectors if isinstance(sectors, Mapping) else {}
    industries = payload.get("industry_boards") or sectors.get("industries") or sectors.get("industry") or []
    concepts = payload.get("concept_boards") or sectors.get("concepts") or sectors.get("concept") or []
    raw_limit_pool = payload.get("limit_up_pool")
    limit_pool = raw_limit_pool if isinstance(raw_limit_pool, list) else None
    news = payload.get("news") or []

    breadth = _merge_snapshot_breadth(
        analyze_breadth(stocks, limit_pool), payload.get("breadth")
    )
    industry_boards = rank_boards(industries, "行业")
    concept_boards = rank_boards(concepts, "概念")
    hot_boards = sorted(industry_boards + concept_boards, key=lambda row: row["score"], reverse=True)
    market_temperature = (
        score_market_temperature(breadth, hot_boards)
        if hot_boards and _has_temperature_facts(breadth)
        else {}
    )

    warnings: list[str] = []
    if isinstance(quality, Mapping):
        raw_optional = quality.get("optional_warnings")
        if isinstance(raw_optional, (list, tuple)):
            warnings.extend(str(item) for item in raw_optional if item)
    raw_warnings = payload.get("warnings")
    if isinstance(raw_warnings, (list, tuple)):
        warnings.extend(str(item) for item in raw_warnings if item)
    warnings = list(dict.fromkeys(warnings))
    raw_status = payload.get("status")
    status = dict(raw_status) if isinstance(raw_status, Mapping) else {}
    if warnings:
        status["warnings"] = warnings

    result = AnalysisResult(
        trade_date=payload.get("trade_date", ""),
        source=str(source),
        status=status,
        market_temperature=market_temperature,
        breadth=breadth,
        indices=pick_key_indices(payload.get("indices") or []),
        hot_boards=top_n(hot_boards, int(config.get("board_top_n", 10))),
        industry_boards=top_n(industry_boards, int(config.get("board_top_n", 10))),
        concept_boards=top_n(concept_boards, int(config.get("board_top_n", 10))),
        limit_ladder=build_limit_ladder(limit_pool or []),
        top_stocks=top_stocks_by_pct(stocks, int(config.get("stock_top_n", 20))),
        news=filter_news(news, config),
        inferences=[],
        risks=[],
        watch_signals=[],
        sources=sources,
        optional_warnings=warnings,
    )
    result.inferences = build_inferences(result)
    result.risks = build_risks(result)
    result.watch_signals = build_watch_signals(result)
    return result


def analyze_breadth(
    stocks: list[dict[str, Any]], limit_pool: list[dict[str, Any]] | None
) -> dict[str, Any]:
    pct_values = [
        value
        for row in stocks
        if (value := _optional_float(pick(row, ["涨跌幅", "涨跌幅%", "change_pct", "pct_chg", "pct"])))
        is not None
    ]
    total = len(pct_values)
    up = sum(1 for value in pct_values if value > 0)
    down = sum(1 for value in pct_values if value < 0)
    flat = max(total - up - down, 0)
    avg_pct = sum(pct_values) / total if total else None
    median_pct = sorted(pct_values)[total // 2] if total else None

    amounts = [
        value
        for row in stocks
        if (value := _optional_float(pick(row, ["成交额", "amount", "成交金额"])))
        is not None
    ]
    turnover = sum(amounts) if amounts else None
    limit_up = len(limit_pool) if limit_pool is not None else None
    limit_down = sum(1 for value in pct_values if value <= -9.8) if pct_values else None
    adv_ratio = up / total if total else None

    return {
        "total": total,
        "up": up,
        "down": down,
        "flat": flat,
        "adv_ratio": adv_ratio,
        "avg_pct": avg_pct,
        "median_pct": median_pct,
        "turnover": turnover,
        "limit_up": limit_up,
        "limit_down_est": limit_down,
    }


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _merge_snapshot_breadth(
    computed: dict[str, Any], raw_breadth: Any
) -> dict[str, Any]:
    if not isinstance(raw_breadth, Mapping):
        return computed
    merged = dict(computed)
    raw_counts = raw_breadth.get("counts")
    counts = raw_counts if isinstance(raw_counts, Mapping) else raw_breadth
    for key in ("up", "down", "flat", "total"):
        if key in counts:
            merged[key] = counts.get(key)
    total = _optional_float(merged.get("total"))
    up = _optional_float(merged.get("up"))
    merged["adv_ratio"] = up / total if up is not None and total and total > 0 else None

    for key in ("sh_amount", "sz_amount"):
        if key in raw_breadth:
            merged[key] = _optional_float(raw_breadth.get(key))
    sh_amount = _optional_float(merged.get("sh_amount"))
    sz_amount = _optional_float(merged.get("sz_amount"))
    if sh_amount is not None and sz_amount is not None:
        merged["turnover"] = sh_amount + sz_amount
    return merged


def _has_temperature_facts(breadth: Mapping[str, Any]) -> bool:
    required = ("adv_ratio", "turnover", "limit_up", "avg_pct")
    return all(_optional_float(breadth.get(key)) is not None for key in required)


def score_market_temperature(breadth: dict[str, Any], hot_boards: list[dict[str, Any]]) -> dict[str, Any]:
    adv_ratio = to_float(breadth.get("adv_ratio"))
    turnover = to_float(breadth.get("turnover"))
    limit_up = int(to_float(breadth.get("limit_up")))
    avg_pct = to_float(breadth.get("avg_pct"))

    breadth_score = min(max(adv_ratio, 0.0), 1.0) * 35
    turnover_score = _turnover_score(turnover)
    limit_score = min(limit_up / 90, 1.0) * 20
    board_score = _board_heat_score(hot_boards)
    avg_score = min(max((avg_pct + 2.0) / 4.0, 0.0), 1.0) * 10

    score = round(breadth_score + turnover_score + limit_score + board_score + avg_score, 1)
    if score >= 78:
        label = "偏热/高潮"
    elif score >= 62:
        label = "偏热"
    elif score >= 45:
        label = "均衡"
    elif score >= 30:
        label = "偏冷"
    else:
        label = "冰点"

    return {
        "score": score,
        "label": label,
        "components": {
            "breadth": round(breadth_score, 1),
            "turnover": round(turnover_score, 1),
            "limit_up": round(limit_score, 1),
            "board_heat": round(board_score, 1),
            "avg_pct": round(avg_score, 1),
        },
    }


def _turnover_score(turnover: float) -> float:
    if turnover >= 1_500_000_000_000:
        return 20
    if turnover >= 1_200_000_000_000:
        return 18
    if turnover >= 1_000_000_000_000:
        return 15
    if turnover >= 800_000_000_000:
        return 12
    if turnover >= 600_000_000_000:
        return 8
    if turnover > 0:
        return 5
    return 0


def _board_heat_score(boards: list[dict[str, Any]]) -> float:
    if not boards:
        return 0
    top = boards[:5]
    avg_pct = sum(to_float(row.get("pct")) for row in top) / len(top)
    hot_count = sum(1 for row in top if to_float(row.get("pct")) >= 3)
    return min(max(avg_pct, 0) * 2 + hot_count * 1.5, 15)


def rank_boards(rows: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for row in rows:
        name = pick(row, ["板块名称", "名称", "行业名称", "概念名称", "name"], "")
        if not name:
            continue
        pct = _optional_float(pick(row, ["涨跌幅", "涨跌幅%", "change_pct", "pct_chg", "pct"]))
        amount = _optional_float(pick(row, ["成交额", "成交金额", "amount"]))
        up_count = _optional_float(pick(row, ["上涨家数", "上涨数", "up_count"]))
        down_count = _optional_float(pick(row, ["下跌家数", "下跌数", "down_count"]))
        leader = pick(row, ["领涨股票", "领涨股", "leader", "龙头股"], "")
        leader_pct = _optional_float(pick(row, ["领涨股票-涨跌幅", "领涨股涨跌幅", "leader_pct"]))
        if pct is None or amount is None:
            continue
        score = pct * 2.2 + math.log10(max(amount, 1)) * 1.7
        if up_count is not None and down_count is not None and up_count + down_count > 0:
            score += up_count / (up_count + down_count) * 8
        if leader_pct is not None:
            score += leader_pct * 0.45
        ranked.append(
            {
                "kind": kind,
                "name": str(name),
                "pct": pct,
                "amount": amount,
                "up_count": int(up_count) if up_count is not None else None,
                "down_count": int(down_count) if down_count is not None else None,
                "leader": str(leader) if leader else "",
                "leader_pct": leader_pct,
                "score": round(score, 2),
            }
        )
    return sorted(ranked, key=lambda item: item["score"], reverse=True)


def pick_key_indices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wanted = ["上证指数", "深证成指", "创业板指", "科创50", "中证500", "沪深300"]
    wanted_codes = {"000001", "399001", "399006", "000688", "000905", "000300"}
    picked: list[dict[str, Any]] = []
    seen = set()
    for row in rows:
        name = str(pick(row, ["名称", "指数名称", "name"], ""))
        code = str(pick(row, ["代码", "指数代码", "code"], ""))
        if not name:
            continue
        identity = code or name
        if (any(key in name for key in wanted) or code in wanted_codes) and identity not in seen:
            picked.append(
                {
                    "code": code,
                    "name": name,
                    "price": _optional_float(pick(row, ["最新价", "收盘", "最新", "close"])),
                    "pct": _optional_float(pick(row, ["涨跌幅", "涨跌幅%", "pct_chg", "change_pct", "pct"])),
                    "amount": _optional_float(pick(row, ["成交额", "成交金额", "amount"])),
                }
            )
            seen.add(identity)
    return picked[:6]


def build_limit_ladder(limit_pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[int, list[str]] = defaultdict(list)
    for row in limit_pool:
        name = pick(row, ["名称", "股票简称", "name"], "")
        if not name:
            continue
        height = int(to_float(pick(row, ["连板数", "连续涨停", "连板", "height"], 1), 1))
        buckets[height].append(str(name))

    ladder = []
    for height in sorted(buckets.keys(), reverse=True):
        names = buckets[height]
        ladder.append({"height": height, "count": len(names), "names": names[:8]})
    return ladder


def top_stocks_by_pct(stocks: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    normalized = []
    for row in stocks:
        name = pick(row, ["名称", "股票简称", "name"], "")
        code = pick(row, ["代码", "股票代码", "code"], "")
        if not name:
            continue
        pct = _optional_float(pick(row, ["涨跌幅", "涨跌幅%", "pct_chg", "change_pct", "pct"]))
        if pct is None:
            continue
        normalized.append(
            {
                "code": str(code),
                "name": str(name),
                "pct": pct,
                "price": _optional_float(pick(row, ["最新价", "现价", "close", "price"])),
                "amount": _optional_float(pick(row, ["成交额", "成交金额", "amount"])),
            }
        )
    normalized.sort(
        key=lambda item: (
            item["pct"],
            item["amount"] if item["amount"] is not None else float("-inf"),
        ),
        reverse=True,
    )
    return top_n(normalized, n)


def filter_news(news: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    top_n_value = int(config.get("news_top_n", 12))
    keywords = [str(item) for item in config.get("news_keywords", [])]
    normalized = []
    for row in news:
        title = str(pick(row, ["标题", "新闻标题", "title", "内容", "摘要"], ""))
        if not title:
            continue
        if keywords and not any(keyword in title for keyword in keywords):
            continue
        normalized.append(
            {
                "title": title,
                "time": str(pick(row, ["发布时间", "时间", "datetime", "time"], "")),
                "source": str(pick(row, ["来源", "文章来源", "source"], "")),
                "url": str(pick(row, ["链接", "新闻链接", "url"], "")),
            }
        )
    return normalized[:top_n_value]


def build_inferences(result: AnalysisResult) -> list[str]:
    breadth = result.breadth
    hot = result.hot_boards[:3]
    lines: list[str] = []
    if hot:
        names = "、".join(row["name"] for row in hot)
        lines.append(f"热点集中在 {names}，需要观察这些方向明日是否继续维持成交额和涨幅排名。")

    adv_ratio = _optional_float(breadth.get("adv_ratio"))
    if adv_ratio is not None and breadth.get("total"):
        if adv_ratio >= 0.6:
            lines.append("涨跌家数显示赚钱效应偏扩散，短线资金更容易从龙头向补涨扩展。")
        elif adv_ratio <= 0.4:
            lines.append("涨跌家数偏弱，市场更像局部抱团或防守轮动，追高容错率下降。")
        else:
            lines.append("涨跌家数处在中性区域，热点持续性比指数涨跌更重要。")

    limit_up = _optional_float(breadth.get("limit_up"))
    if limit_up is not None:
        if int(limit_up) >= 60:
            lines.append("涨停数量较多，情绪处于活跃区，重点看连板高度能否继续打开。")
        elif int(limit_up) <= 25:
            lines.append("涨停数量不高，题材进攻力度一般，热点更可能以轮动形式呈现。")
    return lines


def build_risks(result: AnalysisResult) -> list[str]:
    risks: list[str] = []
    breadth = result.breadth
    adv_ratio = _optional_float(breadth.get("adv_ratio"))
    turnover = _optional_float(breadth.get("turnover"))
    hot = result.hot_boards

    if turnover is not None and adv_ratio is not None and turnover >= 1_000_000_000_000 and adv_ratio < 0.45:
        risks.append("成交额较大但上涨家数不足，可能存在资金集中抱团或高低切换风险。")
    if hot and hot[0]["pct"] >= 5 and len([row for row in hot[:5] if row["pct"] >= 3]) <= 2:
        risks.append("热点强度集中在少数方向，若龙头分歧，板块容易冲高回落。")
    limit_up = _optional_float(breadth.get("limit_up"))
    if limit_up is not None and int(limit_up) <= 20 and result.source != "sample":
        risks.append("涨停数量偏低，短线情绪不足，题材持续性需要降低预期。")
    if result.source == "sample":
        risks.append("当前报告使用样例数据，只能验证模板，不能用于判断真实市场。")
    return risks


def build_watch_signals(result: AnalysisResult) -> list[str]:
    hot_names = [row["name"] for row in result.hot_boards[:5]]
    signals = []
    if hot_names:
        signals.append(f"观察 { '、'.join(hot_names) } 是否继续留在涨幅和成交额前列。")
    if _optional_float(result.breadth.get("turnover")) is not None:
        signals.append("观察两市成交额能否维持或放大，缩量上涨容易降低持续性。")
    if _optional_float(result.breadth.get("limit_up")) is not None:
        signals.append("观察涨停池连板高度和炸板后的回封情况，用来判断情绪是否升温。")
    if hot_names:
        signals.append("若强势板块龙头高开低走，同时低位补涨不接力，短线应偏谨慎。")
    return signals
