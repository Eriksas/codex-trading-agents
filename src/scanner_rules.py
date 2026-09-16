"""扫描评分、候选过滤、市场环境与模拟计划；沿用冻结规则。"""

from statistics import median, stdev
from typing import Any, Optional

if __package__:
    from . import scanner_config as _config
    from .scanner_config import (
        _cfg,
        _is_alpha040_v3_active,
        _strategy_cfg,
    )
    from .scanner_data import (
        _daily_returns,
        _moving_average,
        _series_return,
    )
    from .scanner_utils import (
        _fmt_pct,
        _fmt_yi,
        _symbol_cn_suffix,
        _to_float,
    )
else:
    import scanner_config as _config
    from scanner_config import (
        _cfg,
        _is_alpha040_v3_active,
        _strategy_cfg,
    )
    from scanner_data import (
        _daily_returns,
        _moving_average,
        _series_return,
    )
    from scanner_utils import (
        _fmt_pct,
        _fmt_yi,
        _symbol_cn_suffix,
        _to_float,
    )


def _score_stock(row: dict) -> dict:
    """对单只股票计算研究观察分数。"""
    latest = _to_float(row.get("latest"))
    change_rate = _to_float(row.get("change_rate"))
    change_rate_5d = _to_float(row.get("change_rate_5d"))
    change_rate_20d = _to_float(row.get("change_rate_20d"))
    change_rate_60d = _to_float(row.get("change_rate_60d"))
    turnover = _to_float(row.get("turnover"))
    turnover_rate = _to_float(row.get("turnover_rate"))
    amplitude = _to_float(row.get("amplitude"))
    market_cap = _to_float(row.get("market_cap_total"))
    ma5 = _to_float(row.get("ma5"))
    ma10 = _to_float(row.get("ma10"))
    ma20 = _to_float(row.get("ma20"))
    volume_ratio = _to_float(row.get("volume_ratio"))
    volatility_20d = _to_float(row.get("volatility_20d"))
    close_to_20d_high = _to_float(row.get("close_to_20d_high"))
    status = row.get("trading_status")

    filter_reasons: list[str] = []
    if status != "NORMAL":
        filter_reasons.append(f"交易状态={status}")
    if row.get("data_source") == "fuyao" and not row.get("history_enriched"):
        filter_reasons.append("历史K线未补齐")
    if latest is None or latest <= _cfg("filters", "min_price", 3):
        filter_reasons.append("价格过低或缺失")
    if turnover is None or turnover < _cfg("filters", "min_turnover", 1e9):
        filter_reasons.append("成交额不足 10 亿")
    if market_cap is not None and market_cap < _cfg("filters", "min_market_cap", 5e10):
        filter_reasons.append("总市值不足 500 亿")
    if change_rate is None or change_rate <= 0:
        filter_reasons.append("当日未转强")
    if change_rate is not None and change_rate >= _cfg("filters", "max_change_rate", 0.095):
        filter_reasons.append("接近涨停或当日涨幅过高")
    if amplitude is not None and amplitude >= _cfg("filters", "max_amplitude", 0.12):
        filter_reasons.append("振幅过大")
    if change_rate_5d is not None and change_rate_5d >= _cfg("filters", "max_5d_return", 0.28):
        filter_reasons.append("5 日涨幅过高")
    if change_rate_60d is not None and change_rate_60d >= _cfg("filters", "max_60d_return", 1.5):
        filter_reasons.append("60 日涨幅过高")
    if latest is not None and ma20 is not None and latest < ma20:
        filter_reasons.append("未站上20日均线")
    if volatility_20d is not None and volatility_20d >= _cfg("filters", "max_volatility_20d", 0.09):
        filter_reasons.append("20日波动率过高")
    if volume_ratio is not None and volume_ratio >= _cfg("filters", "max_volume_ratio", 5):
        filter_reasons.append("当日放量过急")
    if close_to_20d_high is not None and close_to_20d_high < _cfg("filters", "min_close_to_20d_high", -0.12):
        filter_reasons.append("距离20日高点过远")

    score = 0.0
    if turnover is not None:
        score += min(turnover / 1.5e10, 1.0) * 22
    if market_cap is not None:
        score += min(market_cap / 3e11, 1.0) * 12
    else:
        score += 6
    if change_rate is not None:
        if 0.005 <= change_rate <= 0.055:
            score += 24
        elif 0.055 < change_rate < 0.085:
            score += 16
        elif 0 < change_rate < 0.005:
            score += 8
    if change_rate_5d is not None:
        if 0.015 <= change_rate_5d <= 0.14:
            score += 20
        elif -0.02 <= change_rate_5d < 0.015:
            score += 8
    else:
        score += 8
    if change_rate_20d is not None:
        if 0 <= change_rate_20d <= 0.35:
            score += 10
        elif change_rate_20d > 0.35:
            score += 3
    else:
        score += 4
    if amplitude is not None:
        if amplitude <= 0.06:
            score += 8
        elif amplitude <= 0.09:
            score += 4
    if turnover_rate is not None:
        if 0.01 <= turnover_rate <= 0.08:
            score += 4
        elif turnover_rate > 0.12:
            score -= 6
    if latest is not None and ma20 is not None:
        if latest >= ma20:
            score += 6
        if ma5 is not None and ma10 is not None and ma5 >= ma10 >= ma20:
            score += 8
        elif ma5 is not None and ma5 >= ma20:
            score += 4
    if volume_ratio is not None:
        if 0.8 <= volume_ratio <= 2.8:
            score += 7
        elif 2.8 < volume_ratio < 5:
            score += 3
    if volatility_20d is not None:
        if volatility_20d <= 0.04:
            score += 5
        elif volatility_20d <= 0.07:
            score += 2
    if close_to_20d_high is not None:
        if -0.04 <= close_to_20d_high <= 0.015:
            score += 7
        elif -0.08 <= close_to_20d_high < -0.04:
            score += 3

    reasons: list[str] = []
    if turnover is not None:
        reasons.append(f"成交额 {_fmt_yi(turnover)}")
    if change_rate is not None:
        reasons.append(f"日涨跌 {_fmt_pct(change_rate)}")
    if change_rate_5d is not None:
        reasons.append(f"5日 {_fmt_pct(change_rate_5d)}")
    if change_rate_20d is not None:
        reasons.append(f"20日 {_fmt_pct(change_rate_20d)}")
    if amplitude is not None:
        reasons.append(f"振幅 {_fmt_pct(amplitude)}")
    if volume_ratio is not None:
        reasons.append(f"量比20日均额 {volume_ratio:.1f}x")
    if close_to_20d_high is not None:
        reasons.append(f"距20日高点 {_fmt_pct(close_to_20d_high)}")

    risk_notes: list[str] = []
    if change_rate is not None and change_rate > 0.07:
        risk_notes.append("当日涨幅偏高，需观察后续承接")
    if amplitude is not None and amplitude > 0.09:
        risk_notes.append("振幅偏大")
    if change_rate_5d is not None and change_rate_5d > 0.16:
        risk_notes.append("短期涨幅已较高")
    if change_rate_60d is not None and change_rate_60d > 0.8:
        risk_notes.append("中期涨幅较大")
    if volume_ratio is not None and volume_ratio > 3:
        risk_notes.append("量能短期放大，次日承接需复核")
    if volatility_20d is not None and volatility_20d > 0.06:
        risk_notes.append("20日波动偏高")
    if close_to_20d_high is not None and close_to_20d_high < -0.08:
        risk_notes.append("尚未接近20日强势区间")
    if not risk_notes:
        risk_notes.append("未触发主要波动风险项")

    sector = row.get("industry_sector") or {}
    return {
        "symbol": _symbol_cn_suffix(str(row.get("symkey") or "")),
        "name": row.get("name"),
        "sector": sector.get("name"),
        "latest": latest,
        "change_rate": change_rate,
        "change_rate_5d": change_rate_5d,
        "change_rate_20d": change_rate_20d,
        "change_rate_60d": change_rate_60d,
        "turnover": turnover,
        "turnover_rate": turnover_rate,
        "amplitude": amplitude,
        "market_cap": market_cap,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "volume_ratio": volume_ratio,
        "volatility_20d": volatility_20d,
        "close_to_20d_high": close_to_20d_high,
        "history_days": row.get("history_days"),
        "history_enriched": row.get("history_enriched"),
        "score": round(score, 1),
        "passed": len(filter_reasons) == 0,
        "filter_reasons": "；".join(filter_reasons),
        "reasons": "；".join(reasons),
        "risk_notes": "；".join(risk_notes),
    }


def _build_legacy_trade_plan(item: dict) -> dict:
    """
    由 legacy 动量策略固定规则生成模拟交易参数。

    触发区间、止损、第一止盈和仓位来自振幅、短期涨幅和最新价，不做主观预测。
    """
    latest = item.get("latest") or 0.0
    amplitude = item.get("amplitude") or 0.05
    change_rate = item.get("change_rate") or 0.0
    change_rate_5d = item.get("change_rate_5d") or 0.0

    pullback_pct = min(
        max(amplitude * _cfg("trade_plan", "pullback_factor", 0.35), _cfg("trade_plan", "pullback_min", 0.008)),
        _cfg("trade_plan", "pullback_max", 0.025),
    )
    chase_pct = min(
        max(amplitude * _cfg("trade_plan", "chase_factor", 0.12), _cfg("trade_plan", "chase_min", 0.003)),
        _cfg("trade_plan", "chase_max", 0.012),
    )
    stop_pct = min(
        max(amplitude * _cfg("trade_plan", "stop_factor", 0.75), _cfg("trade_plan", "stop_min", 0.035)),
        _cfg("trade_plan", "stop_max", 0.08),
    )

    trigger_low = latest * (1 - pullback_pct)
    trigger_high = latest * (1 + chase_pct)
    stop_loss = latest * (1 - stop_pct)
    risk_per_share = max(latest - stop_loss, latest * 0.02)
    reward_risk = _cfg("trade_plan", "reward_risk", 1.6)
    first_take_profit = latest + risk_per_share * reward_risk

    position_pct = int(_cfg("trade_plan", "base_position_pct", 8))
    if amplitude >= _cfg("trade_plan", "high_amplitude_threshold", 0.09):
        position_pct = int(_cfg("trade_plan", "high_amplitude_position_pct", 5))
    if change_rate_5d >= _cfg("trade_plan", "hot_5d_threshold", 0.14):
        position_pct = min(position_pct, int(_cfg("trade_plan", "hot_5d_position_pct", 5)))
    if change_rate >= _cfg("trade_plan", "hot_day_threshold", 0.07):
        position_pct = min(position_pct, int(_cfg("trade_plan", "hot_day_position_pct", 4)))

    if amplitude >= _cfg("trade_plan", "high_amplitude_threshold", 0.09):
        plan = "只看回踩承接，不追高；若次日振幅继续扩大则降级观察"
    elif change_rate_5d >= _cfg("trade_plan", "hot_5d_threshold", 0.14):
        plan = "短期涨幅较高，只在回踩区间企稳时试探；高开不追"
    else:
        plan = "优先观察回踩区间承接；若放量突破触发上沿再复核"

    return {
        **item,
        "signal_price": round(latest, 2),
        "trigger_zone": f"{trigger_low:.2f}-{trigger_high:.2f}",
        "stop_loss": round(stop_loss, 2),
        "first_take_profit": round(first_take_profit, 2),
        "position_pct": position_pct,
        "plan": plan,
        "risk_per_share": round(risk_per_share, 2),
        "reward_risk": reward_risk,
    }


def _build_trade_plan(item: dict) -> dict:
    """
    由当前启用策略生成模拟交易参数。

    触发区间、止损、第一止盈和仓位来自固定规则，不做主观预测。
    """
    latest = item.get("latest") or 0.0
    amplitude = item.get("amplitude") or 0.05
    if not _is_alpha040_v3_active():
        return _build_legacy_trade_plan(item)

    pullback_pct = min(
        max(amplitude * _cfg("trade_plan", "pullback_factor", 0.35), _cfg("trade_plan", "pullback_min", 0.008)),
        _cfg("trade_plan", "pullback_max", 0.025),
    )
    chase_pct = min(
        max(amplitude * _cfg("trade_plan", "chase_factor", 0.12), _cfg("trade_plan", "chase_min", 0.003)),
        _cfg("trade_plan", "chase_max", 0.012),
    )
    trigger_low = latest * (1 - pullback_pct)
    trigger_high = latest * (1 + chase_pct)
    atr_value = _to_float(item.get("atr14"))
    atr_multiplier = float(_strategy_cfg("risk_control", "atr_multiplier", 2.0))
    fallback_stop_pct = min(
        max(amplitude * _cfg("trade_plan", "stop_factor", 0.75), _cfg("trade_plan", "stop_min", 0.035)),
        _cfg("trade_plan", "stop_max", 0.08),
    )
    stop_loss = latest - atr_value * atr_multiplier if atr_value is not None else latest * (1 - fallback_stop_pct)
    stop_loss = max(0.01, stop_loss)
    reward_risk = float(_strategy_cfg("risk_control", "reward_risk", _cfg("trade_plan", "reward_risk", 1.6)))
    risk_per_share = max(latest - stop_loss, latest * 0.02)
    first_take_profit = latest + risk_per_share * reward_risk
    max_position = float(_strategy_cfg("risk_control", "risk_budget_max_position_pct", 8))
    risk_budget = float(_strategy_cfg("risk_control", "risk_budget_account_pct", 0.002))
    min_stop_pct = float(_strategy_cfg("risk_control", "risk_budget_min_stop_pct", 0.005))
    stop_distance_pct = max((latest - stop_loss) / latest, min_stop_pct) if latest else min_stop_pct
    position_pct = round(max(0.0, min(max_position, risk_budget / stop_distance_pct * 100)), 2)
    plan = "冻结 Alpha040 V3：仅用于个人模拟；次日按触发区间观察，ATR 止损，风险预算仓位；不自动下单"
    return {
        **item,
        "signal_price": round(latest, 2),
        "trigger_zone": f"{trigger_low:.2f}-{trigger_high:.2f}",
        "stop_loss": round(stop_loss, 2),
        "first_take_profit": round(first_take_profit, 2),
        "position_pct": position_pct,
        "plan": plan,
        "risk_per_share": round(risk_per_share, 2),
        "reward_risk": reward_risk,
        "atr14": round(atr_value, 4) if atr_value is not None else None,
        "risk_budget_account_pct": risk_budget,
        "stop_distance_pct": round(stop_distance_pct, 6),
    }


def _select_candidates(scored: list[dict], limit: int = 5, market_profile: Optional[dict] = None) -> list[dict]:
    """选择候选，限制单行业过度集中。"""
    selected: list[dict] = []
    sector_counts: dict[str, int] = {}
    effective_limit = min(limit, int(market_profile.get("candidate_limit", limit))) if market_profile else limit
    if _is_alpha040_v3_active() and market_profile:
        allow_label = _strategy_cfg("filters", "open_only_market_regime_label", "积极")
        if market_profile.get("regime_label") != allow_label:
            return []
    min_final_score = _to_float(market_profile.get("min_final_score")) if market_profile else None
    for item in sorted(scored, key=lambda x: x.get("final_score", x["score"]), reverse=True):
        if not item["passed"]:
            continue
        if not _is_alpha040_v3_active() and min_final_score is not None and (item.get("final_score") or item["score"]) < min_final_score:
            continue
        sector = item.get("sector") or "unknown"
        if sector_counts.get(sector, 0) >= 2:
            continue
        selected.append(item)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if len(selected) >= effective_limit:
            break
    candidates = [_build_trade_plan(item) for item in selected]
    return _apply_market_risk_controls(candidates, market_profile) if market_profile else candidates


def _excluded_trade_examples(scored: list[dict], limit: int = 30) -> list[dict]:
    """选择容易误判但暂不纳入的样例。"""
    return [
        item for item in scored
        if not item["passed"] and (
            "涨幅过高" in item["filter_reasons"]
            or "振幅过大" in item["filter_reasons"]
            or "5 日涨幅过高" in item["filter_reasons"]
            or "60 日涨幅过高" in item["filter_reasons"]
        )
    ][:limit]


def _classify_market_regime(score: float) -> dict:
    """把市场环境分数映射为候选和仓位规则。"""
    regimes = _config.ACTIVE_SCANNER_CONFIG.get("market_regimes") or {}
    risk_on = regimes.get("risk_on") or {}
    neutral = regimes.get("neutral") or {}
    cautious = regimes.get("cautious") or {}
    defensive = regimes.get("defensive") or {}
    if score >= risk_on.get("min_score", 70):
        return {
            "regime": "risk_on",
            "regime_label": "积极",
            "candidate_limit": risk_on.get("candidate_limit", 5),
            "position_multiplier": risk_on.get("position_multiplier", 1.0),
            "max_position_pct": risk_on.get("max_position_pct", 8),
            "min_final_score": risk_on.get("min_final_score", 88),
        }
    if score >= neutral.get("min_score", 50):
        return {
            "regime": "neutral",
            "regime_label": "中性",
            "candidate_limit": neutral.get("candidate_limit", 4),
            "position_multiplier": neutral.get("position_multiplier", 0.8),
            "max_position_pct": neutral.get("max_position_pct", 6),
            "min_final_score": neutral.get("min_final_score", 90),
        }
    if score >= cautious.get("min_score", 35):
        return {
            "regime": "cautious",
            "regime_label": "谨慎",
            "candidate_limit": cautious.get("candidate_limit", 3),
            "position_multiplier": cautious.get("position_multiplier", 0.6),
            "max_position_pct": cautious.get("max_position_pct", 5),
            "min_final_score": cautious.get("min_final_score", 92),
        }
    return {
        "regime": "defensive",
        "regime_label": "防守",
        "candidate_limit": defensive.get("candidate_limit", 1),
        "position_multiplier": defensive.get("position_multiplier", 0.35),
        "max_position_pct": defensive.get("max_position_pct", 3),
        "min_final_score": defensive.get("min_final_score", 95),
    }


def _build_market_profile(indexes: list[dict], scored: list[dict]) -> dict:
    """根据指数趋势与全市场样本状态生成市场环境过滤规则。"""
    changes = [x["change_rate"] for x in scored if x.get("change_rate") is not None]
    up_ratio = sum(1 for x in changes if x > 0) / len(changes) if changes else None
    median_change = median(changes) if changes else None
    enriched = [x for x in scored if x.get("history_enriched")]
    passed = [x for x in scored if x.get("passed")]
    strong_count = sum(1 for x in passed if (x.get("final_score") or x.get("score") or 0) >= 92)
    strong_ratio = strong_count / len(enriched) if enriched else 0

    above_ma20_values = [x.get("above_ma20") for x in indexes if x.get("above_ma20") is not None]
    above_ma60_values = [x.get("above_ma60") for x in indexes if x.get("above_ma60") is not None]
    index_changes = [_to_float(x.get("change_rate")) for x in indexes if _to_float(x.get("change_rate")) is not None]
    index_vols = [_to_float(x.get("volatility_20d")) for x in indexes if _to_float(x.get("volatility_20d")) is not None]
    above_ma20_ratio = sum(1 for x in above_ma20_values if x) / len(above_ma20_values) if above_ma20_values else None
    above_ma60_ratio = sum(1 for x in above_ma60_values if x) / len(above_ma60_values) if above_ma60_values else None
    avg_index_change = sum(index_changes) / len(index_changes) if index_changes else None
    avg_index_volatility = sum(index_vols) / len(index_vols) if index_vols else None

    score = 0.0
    score += (above_ma20_ratio * 30) if above_ma20_ratio is not None else 12
    score += (above_ma60_ratio * 15) if above_ma60_ratio is not None else 6
    if avg_index_change is not None:
        if avg_index_change >= 0.008:
            score += 15
        elif avg_index_change > 0:
            score += 8
        elif avg_index_change <= -0.01:
            score -= 10
    if up_ratio is not None:
        if up_ratio >= 0.55:
            score += 25
        elif up_ratio >= 0.40:
            score += 12
        elif up_ratio >= 0.28:
            score += 4
        else:
            score -= 8
    if median_change is not None:
        if median_change >= 0:
            score += 10
        elif median_change > -0.01:
            score += 2
        else:
            score -= 6
    if strong_ratio >= 0.06:
        score += 10
    elif strong_count >= 2:
        score += 4
    if avg_index_volatility is not None:
        if avg_index_volatility <= 0.015:
            score += 5
        elif avg_index_volatility >= 0.03:
            score -= 8

    profile = _classify_market_regime(score)
    profile.update(
        {
            "score": round(score, 1),
            "up_ratio": round(up_ratio, 4) if up_ratio is not None else None,
            "median_change": round(median_change, 5) if median_change is not None else None,
            "strong_count": strong_count,
            "strong_ratio": round(strong_ratio, 4),
            "above_ma20_ratio": round(above_ma20_ratio, 4) if above_ma20_ratio is not None else None,
            "above_ma60_ratio": round(above_ma60_ratio, 4) if above_ma60_ratio is not None else None,
            "avg_index_change": round(avg_index_change, 5) if avg_index_change is not None else None,
            "avg_index_volatility": round(avg_index_volatility, 5) if avg_index_volatility is not None else None,
            "index_details": [
                {
                    "label": item.get("label"),
                    "change_rate": item.get("change_rate"),
                    "change_rate_20d": item.get("change_rate_20d"),
                    "above_ma20": item.get("above_ma20"),
                    "above_ma60": item.get("above_ma60"),
                    "volatility_20d": item.get("volatility_20d"),
                }
                for item in indexes
            ],
            "rule": "指数趋势 + 全市场上涨占比 + 强势样本密度 + 指数波动率共同决定候选上限、最低综合分和仓位系数。",
        }
    )
    return profile


def _apply_market_risk_controls(candidates: list[dict], market_profile: dict) -> list[dict]:
    """按市场环境调整模拟仓位并标注原因。"""
    multiplier = _to_float(market_profile.get("position_multiplier")) or 1.0
    max_position = int(market_profile.get("max_position_pct") or 8)
    regime_label = market_profile.get("regime_label") or "未知"
    for item in candidates:
        base_position = int(item.get("position_pct") or 0)
        raw_position = base_position * multiplier
        capped_position = min(max_position, raw_position)
        adjusted = min(max_position, max(1, int(round(capped_position))))
        effective_multiplier = adjusted / base_position if base_position else None
        item["base_position_pct"] = base_position
        item["raw_position_pct"] = round(raw_position, 2)
        item["position_cap_pct"] = max_position
        item["position_pct"] = adjusted
        item["market_regime"] = market_profile.get("regime")
        item["market_regime_label"] = regime_label
        item["market_position_multiplier"] = multiplier
        item["effective_position_multiplier"] = round(effective_multiplier, 4) if effective_multiplier is not None else None
        item["position_adjustment_note"] = (
            f"基础{base_position}% × 环境系数{multiplier} = {raw_position:.2f}%，"
            f"上限{max_position}%，实际{adjusted}%"
        )
        item["market_filter_note"] = (
            f"市场环境{regime_label}，候选上限{market_profile.get('candidate_limit')}，"
            f"仓位系数{multiplier}，实际系数{item['effective_position_multiplier']}"
        )
        if multiplier < 1:
            item["plan"] = f"{item.get('plan') or ''}；市场环境{regime_label}，仓位降级"
    return candidates


def _build_market_timeline(indexes: list[dict]) -> dict[str, dict]:
    """基于指数历史 K 线生成每日市场环境，用于回测信号过滤。"""
    daily_metrics: dict[str, list[dict]] = {}
    for index in indexes:
        bars = index.get("_history_bars") or []
        if len(bars) < 60:
            continue
        for idx in range(60, len(bars)):
            date = bars[idx].get("date")
            if not date:
                continue
            closes = [x["close"] for x in bars[: idx + 1]]
            returns = _daily_returns(closes)
            ma20 = _moving_average(closes, 20)
            ma60 = _moving_average(closes, 60)
            prev_close = bars[idx - 1]["close"] if idx > 0 else None
            change_rate = bars[idx]["close"] / prev_close - 1 if prev_close else None
            volatility_20d = stdev(returns[-20:]) if len(returns[-20:]) >= 2 else None
            daily_metrics.setdefault(date, []).append(
                {
                    "above_ma20": bars[idx]["close"] >= ma20 if ma20 is not None else None,
                    "above_ma60": bars[idx]["close"] >= ma60 if ma60 is not None else None,
                    "change_rate": change_rate,
                    "volatility_20d": volatility_20d,
                }
            )

    timeline: dict[str, dict] = {}
    for date, metrics in daily_metrics.items():
        above20 = [x["above_ma20"] for x in metrics if x.get("above_ma20") is not None]
        above60 = [x["above_ma60"] for x in metrics if x.get("above_ma60") is not None]
        changes = [x["change_rate"] for x in metrics if x.get("change_rate") is not None]
        vols = [x["volatility_20d"] for x in metrics if x.get("volatility_20d") is not None]
        above20_ratio = sum(1 for x in above20 if x) / len(above20) if above20 else None
        above60_ratio = sum(1 for x in above60 if x) / len(above60) if above60 else None
        avg_change = sum(changes) / len(changes) if changes else None
        avg_vol = sum(vols) / len(vols) if vols else None

        score = 0.0
        score += (above20_ratio * 40) if above20_ratio is not None else 15
        score += (above60_ratio * 25) if above60_ratio is not None else 8
        if avg_change is not None:
            if avg_change >= 0.008:
                score += 20
            elif avg_change > 0:
                score += 10
            elif avg_change <= -0.01:
                score -= 12
        if avg_vol is not None:
            if avg_vol <= 0.015:
                score += 8
            elif avg_vol >= 0.03:
                score -= 8
        profile = _classify_market_regime(score)
        profile.update(
            {
                "score": round(score, 1),
                "above_ma20_ratio": round(above20_ratio, 4) if above20_ratio is not None else None,
                "above_ma60_ratio": round(above60_ratio, 4) if above60_ratio is not None else None,
                "avg_index_change": round(avg_change, 5) if avg_change is not None else None,
                "avg_index_volatility": round(avg_vol, 5) if avg_vol is not None else None,
            }
        )
        timeline[date] = profile
    return timeline


def _market_allows_backtest_signal(market_profile: Optional[dict], score: float) -> bool:
    """回测中按历史市场环境过滤新信号。"""
    if not market_profile:
        return True
    regime = market_profile.get("regime")
    if regime == "defensive":
        return False
    if regime == "cautious":
        return score >= 85
    if regime == "neutral":
        return score >= 75
    return True


def _percentile_ranks(items: list[dict], field: str) -> dict[str, float]:
    """按字段计算横截面百分位排名，值越大排名越靠前。"""
    valid = [
        (item.get("symbol"), _to_float(item.get(field)))
        for item in items
        if item.get("symbol") and _to_float(item.get(field)) is not None
    ]
    valid.sort(key=lambda x: x[1], reverse=True)
    if not valid:
        return {}
    if len(valid) == 1:
        return {valid[0][0]: 1.0}
    return {symbol: 1 - idx / (len(valid) - 1) for idx, (symbol, _value) in enumerate(valid)}


def _standardize_cross_section(items: list[dict], field: str) -> dict[str, float]:
    """按字段做简单横截面 z-score。"""
    valid = [
        (str(item.get("symbol")), _to_float(item.get(field)))
        for item in items
        if item.get("symbol") and _to_float(item.get(field)) is not None
    ]
    if not valid:
        return {}
    values = [value for _symbol, value in valid if value is not None]
    mean_value = sum(values) / len(values)
    variance = sum((value - mean_value) ** 2 for value in values) / len(values)
    std_value = variance ** 0.5
    if std_value == 0:
        return {symbol: 0.0 for symbol, _value in valid}
    return {symbol: (float(value) - mean_value) / std_value for symbol, value in valid if value is not None}


def _alpha040_from_bars(bars: list[dict], window: int = 26) -> Optional[float]:
    """计算 Alpha040：26 日上涨量 / 下跌量 * 100。"""
    if len(bars) < window + 1:
        return None
    up_volume = 0.0
    down_volume = 0.0
    for prev, curr in zip(bars[-window - 1: -1], bars[-window:]):
        prev_close = _to_float(prev.get("close"))
        close = _to_float(curr.get("close"))
        volume = _to_float(curr.get("volume")) or 0.0
        if prev_close is None or close is None:
            continue
        if close > prev_close:
            up_volume += volume
        else:
            down_volume += volume
    if down_volume <= 0:
        return None
    return up_volume / down_volume * 100


def _atr_from_bars(bars: list[dict], window: int) -> Optional[float]:
    """计算 ATR。"""
    if len(bars) < max(6, window + 1):
        return None
    values: list[float] = []
    for idx in range(max(1, len(bars) - window), len(bars)):
        high = _to_float(bars[idx].get("high"))
        low = _to_float(bars[idx].get("low"))
        prev_close = _to_float(bars[idx - 1].get("close"))
        if high is None or low is None or prev_close is None:
            continue
        values.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(values) < max(5, window // 2):
        return None
    return sum(values) / len(values)


def _upper_shadow_ratio(item: dict) -> Optional[float]:
    """上影线比例，仅作为标签。"""
    high = _to_float(item.get("high"))
    low = _to_float(item.get("low"))
    close = _to_float(item.get("latest") or item.get("close"))
    if high is None or low is None or close is None or high <= low:
        return None
    return (high - close) / (high - low)


def _alpha040_v3_rank_score(item: dict) -> Optional[float]:
    """配置化 Alpha040 V3 排序分。"""
    weights = _strategy_cfg("ranking", "weights", {})
    alpha = _to_float(item.get("alpha040_z"))
    rps60 = _to_float(item.get("rps60_z"))
    high = _to_float(item.get("close_to_20d_high_z"))
    latest = _to_float(item.get("latest"))
    ma5 = _to_float(item.get("ma5"))
    ma10 = _to_float(item.get("ma10"))
    ma20 = _to_float(item.get("ma20"))
    if alpha is None or rps60 is None or high is None:
        return None
    if bool(_strategy_cfg("filters", "require_close_above_ma20", True)) and (latest is None or ma20 is None or latest < ma20):
        return None
    score = (
        float(weights.get("alpha040_z", 0.55)) * alpha
        + float(weights.get("rps60_z", 0.30)) * rps60
        + float(weights.get("close_to_20d_high_z", 0.15)) * high
    )
    if ma5 is not None and ma10 is not None and ma20 is not None and ma5 >= ma10 >= ma20:
        score += float(_strategy_cfg("ranking", "ma_alignment_bonus", 0.08))
    elif ma5 is not None and ma20 is not None and ma5 >= ma20:
        score += float(_strategy_cfg("ranking", "ma5_above_ma20_bonus", 0.04))
    if latest is not None and ma20:
        distance = latest / ma20 - 1
        start = float(_strategy_cfg("ranking", "ma20_distance_penalty_start", 0.12))
        if distance > start:
            multiplier = float(_strategy_cfg("ranking", "ma20_distance_penalty_multiplier", 2.0))
            cap = float(_strategy_cfg("ranking", "ma20_distance_penalty_cap", 0.35))
            score -= min(cap, (distance - start) * multiplier)
    return round(score, 6)


def _apply_alpha040_v3_strategy(scored: list[dict], histories: Optional[dict[str, list[dict]]] = None) -> list[dict]:
    """应用冻结 Alpha040 V3 风控主策略。"""
    histories = histories or {}
    enriched = [item for item in scored if item.get("history_enriched")]
    rps20_map = _percentile_ranks(enriched, "change_rate_20d")
    rps60_map = _percentile_ranks(enriched, "change_rate_60d")
    atr_window = int(_strategy_cfg("risk_control", "atr_window", 14))
    for item in scored:
        symbol = str(item.get("symbol") or "")
        bars = histories.get(symbol) or []
        item["rps20"] = round(rps20_map[symbol], 4) if symbol in rps20_map else None
        item["rps60"] = round(rps60_map[symbol], 4) if symbol in rps60_map else None
        if item.get("alpha040") is None and bars:
            item["alpha040"] = _alpha040_from_bars(bars)
        if item.get("atr14") is None and bars:
            item["atr14"] = _atr_from_bars(bars, atr_window)
        item["upper_shadow_ratio"] = _upper_shadow_ratio(item)

    for field in ["alpha040", "rps60", "close_to_20d_high"]:
        z_map = _standardize_cross_section(scored, field)
        for item in scored:
            symbol = str(item.get("symbol") or "")
            item[f"{field}_z"] = round(z_map[symbol], 6) if symbol in z_map else None

    max_5d = _to_float(_strategy_cfg("filters", "max_change_rate_5d", 0.2039))
    max_vol = _to_float(_strategy_cfg("filters", "max_volatility_20d", 0.049826))
    inherited_hard_reasons = (
        "交易状态=",
        "历史K线未补齐",
        "价格过低或缺失",
        "成交额不足",
        "接近涨停",
    )
    for item in scored:
        original_reasons = [x for x in str(item.get("filter_reasons") or "").split("；") if x]
        filter_reasons = [
            reason
            for reason in original_reasons
            if any(token in reason for token in inherited_hard_reasons)
        ]
        rank_score = _alpha040_v3_rank_score(item)
        if rank_score is None:
            filter_reasons.append("Alpha040 V3 排序条件不足或未通过趋势弱过滤")
        change_5d = _to_float(item.get("change_rate_5d"))
        volatility = _to_float(item.get("volatility_20d"))
        if max_5d is not None and change_5d is not None and change_5d > max_5d:
            filter_reasons.append(f"5日涨幅超过冻结阈值 {_fmt_pct(max_5d)}")
        if max_vol is not None and volatility is not None and volatility > max_vol:
            filter_reasons.append(f"20日波动率超过冻结阈值 {_fmt_pct(max_vol)}")
        if item.get("alpha040") is None:
            filter_reasons.append("alpha040 缺失")
        if item.get("atr14") is None:
            filter_reasons.append("ATR 数据不足")
        item.update(
            {
                "strategy_score": round(rank_score or 0.0, 6),
                "final_score": round(rank_score or -999.0, 6),
                "alpha040_core_score": rank_score,
                "strategy_tags": "Alpha040 V3" if rank_score is not None else "Alpha040 V3 未通过",
                "strategy_notes": "alpha040 主排序 + rps60/20日高点辅助 + 积极环境 + 过热/波动过滤 + ATR风控",
                "passed": rank_score is not None and not filter_reasons,
                "filter_reasons": "；".join(filter_reasons),
            }
        )
    return scored


def _apply_strategy_overlays(scored: list[dict], histories: Optional[dict[str, list[dict]]] = None) -> list[dict]:
    """加入从开源策略中抽象出的透明规则标签和综合分。"""
    if _is_alpha040_v3_active():
        return _apply_alpha040_v3_strategy(scored, histories=histories)
    enriched = [item for item in scored if item.get("history_enriched")]
    rps20_map = _percentile_ranks(enriched, "change_rate_20d")
    rps60_map = _percentile_ranks(enriched, "change_rate_60d")

    for item in scored:
        symbol = item.get("symbol")
        rps20 = rps20_map.get(symbol)
        rps60 = rps60_map.get(symbol)
        latest = _to_float(item.get("latest"))
        ma5 = _to_float(item.get("ma5"))
        ma10 = _to_float(item.get("ma10"))
        ma20 = _to_float(item.get("ma20"))
        volume_ratio = _to_float(item.get("volume_ratio"))
        volatility_20d = _to_float(item.get("volatility_20d"))
        close_to_high = _to_float(item.get("close_to_20d_high"))
        change_rate_5d = _to_float(item.get("change_rate_5d"))
        change_rate_60d = _to_float(item.get("change_rate_60d"))

        trend_ok = latest is not None and ma5 is not None and ma10 is not None and ma20 is not None and latest >= ma20 and ma5 >= ma10 >= ma20
        volume_ok = volume_ratio is not None and 0.8 <= volume_ratio <= 2.8
        volatility_ok = volatility_20d is not None and volatility_20d <= 0.055
        near_breakout = close_to_high is not None and -0.04 <= close_to_high <= 0.015
        rps_ok = rps20 is not None and rps60 is not None and rps20 >= 0.65 and rps60 >= 0.45
        overheated = (change_rate_5d is not None and change_rate_5d > 0.16) or (change_rate_60d is not None and change_rate_60d > 1.2)

        strategy_score = 0.0
        tags: list[str] = []
        if rps20 is not None:
            strategy_score += rps20 * 30
        if rps60 is not None:
            strategy_score += rps60 * 18
        if rps_ok:
            tags.append("RPS动量")
        if trend_ok:
            strategy_score += 18
            tags.append("均线趋势")
        if volume_ok:
            strategy_score += 12
            tags.append("温和放量")
        if volatility_ok:
            strategy_score += 10
            tags.append("波动可控")
        if near_breakout:
            strategy_score += 12
            tags.append("接近20日强势区")
        if overheated:
            strategy_score -= 12
            tags.append("过热扣分")

        if item.get("history_enriched"):
            final_score = item["score"] * 0.5 + strategy_score * 0.5
        else:
            final_score = item["score"] * 0.7

        item.update(
            {
                "rps20": round(rps20, 4) if rps20 is not None else None,
                "rps60": round(rps60, 4) if rps60 is not None else None,
                "strategy_score": round(strategy_score, 1),
                "final_score": round(final_score, 1),
                "strategy_tags": "、".join(tags) if tags else "无增强标签",
                "strategy_notes": (
                    "横截面动量 + 均线趋势 + 温和放量 + 波动过滤 + 近20日强势区"
                    if tags else "仅保留基础量价打分"
                ),
            }
        )
    return scored


def _passes_enhanced_strategy(item: dict) -> bool:
    """回测用增强过滤，避免只凭基础分触发。"""
    latest = _to_float(item.get("latest"))
    ma20 = _to_float(item.get("ma20"))
    change_rate_20d = _to_float(item.get("change_rate_20d"))
    volume_ratio = _to_float(item.get("volume_ratio"))
    volatility_20d = _to_float(item.get("volatility_20d"))
    close_to_high = _to_float(item.get("close_to_20d_high"))

    return all(
        [
            latest is not None and ma20 is not None and latest >= ma20,
            change_rate_20d is not None and change_rate_20d >= 0,
            volume_ratio is not None and 0.6 <= volume_ratio <= 3.5,
            volatility_20d is not None and volatility_20d <= 0.075,
            close_to_high is not None and close_to_high >= -0.08,
        ]
    )


def _signal_row_from_bar(base_row: dict, bars: list[dict], idx: int) -> dict:
    """将历史某一天转换成扫描器可评分的伪快照。"""
    bar = bars[idx]
    prev_close = bars[idx - 1]["close"] if idx > 0 else None
    closes = [x["close"] for x in bars[: idx + 1]]
    highs = [x["high"] for x in bars[: idx + 1]]
    lows = [x["low"] for x in bars[: idx + 1]]
    turnovers = [x["turnover"] for x in bars[: idx + 1] if x.get("turnover") is not None]
    change_rate = bar["close"] / prev_close - 1 if prev_close else None
    amplitude = (bar["high"] - bar["low"]) / prev_close if prev_close else None
    avg_turnover_20d = _moving_average(turnovers, 20)
    volume_ratio = None
    if bar.get("turnover") is not None and avg_turnover_20d:
        volume_ratio = bar["turnover"] / avg_turnover_20d
    returns = _daily_returns(closes)
    volatility_20d = stdev(returns[-20:]) if len(returns[-20:]) >= 2 else None
    high_20d = max(highs[-20:]) if len(highs) >= 20 else None
    low_20d = min(lows[-20:]) if len(lows) >= 20 else None
    close_to_20d_high = bar["close"] / high_20d - 1 if high_20d else None
    return {
        "name": base_row.get("name"),
        "symkey": base_row.get("symkey"),
        "latest": bar["close"],
        "close": bar["close"],
        "open": bar["open"],
        "high": bar["high"],
        "low": bar["low"],
        "prev_close": prev_close,
        "change_rate": change_rate,
        "change_rate_5d": _series_return(closes, 5),
        "change_rate_20d": _series_return(closes, 20),
        "change_rate_60d": _series_return(closes, 60),
        "turnover": bar.get("turnover"),
        "volume": bar.get("volume"),
        "amplitude": amplitude,
        "ma5": _moving_average(closes, 5),
        "ma10": _moving_average(closes, 10),
        "ma20": _moving_average(closes, 20),
        "avg_turnover_20d": avg_turnover_20d,
        "volume_ratio": volume_ratio,
        "volatility_20d": volatility_20d,
        "high_20d": high_20d,
        "low_20d": low_20d,
        "close_to_20d_high": close_to_20d_high,
        "history_days": idx + 1,
        "history_enriched": True,
        "trading_status": "NORMAL",
        "industry_sector": base_row.get("industry_sector") or {"name": "未分类"},
        "data_source": base_row.get("data_source") or "fuyao",
    }
