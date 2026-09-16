"""扫描与影子对照的 Markdown 展示；不执行数据请求或回测。"""

from datetime import datetime, timedelta
from statistics import median, stdev
from typing import Any, Optional

if __package__:
    from .scanner_config import (
        _active_strategy_key,
        _strategy_display_name,
    )
    from .scanner_utils import (
        _fmt_pct,
        _fmt_source,
        _to_float,
    )
else:
    from scanner_config import (
        _active_strategy_key,
        _strategy_display_name,
    )
    from scanner_utils import (
        _fmt_pct,
        _fmt_source,
        _to_float,
    )


def _market_comment(indexes: list[dict], scored: list[dict], market_profile: Optional[dict] = None) -> str:
    """生成大盘研究性描述。"""
    idx_parts = []
    for item in indexes:
        idx_parts.append(f"{item.get('label')}{_fmt_pct(_to_float(item.get('change_rate')))}")
    passed = [x for x in scored if x["passed"]]
    changes = [x["change_rate"] for x in scored if x.get("change_rate") is not None]
    up_ratio = sum(1 for x in changes if x > 0) / len(changes) if changes else 0
    median_change = median(changes) if changes else None
    hot_sectors: dict[str, int] = {}
    for item in passed[:60]:
        sector = item.get("sector") or "unknown"
        hot_sectors[sector] = hot_sectors.get(sector, 0) + 1
    top_sectors = "、".join([k for k, _ in sorted(hot_sectors.items(), key=lambda x: x[1], reverse=True)[:3]])
    comment = (
        f"指数表现：{ '，'.join(idx_parts) if idx_parts else '指数数据缺失' }。"
        f"高成交额/强势样本中上涨占比约 {up_ratio * 100:.0f}%，中位涨跌为 {_fmt_pct(median_change)}。"
        f"通过初筛的方向集中在 {top_sectors or '若干板块'}，整体更像结构性活跃，而非全面扩散。"
    )
    if market_profile:
        comment += (
            f" 市场环境评级为 {market_profile.get('regime_label')}，"
            f"候选上限 {market_profile.get('candidate_limit')} 只，"
            f"仓位系数 {market_profile.get('position_multiplier')}。"
        )
    return comment


def _render_shadow_experiment_report(summary: dict) -> str:
    """渲染影子实验 Markdown 报告。"""
    current = summary.get("current_trigger") or {}
    next_open = summary.get("next_day_open_buy") or {}
    risk = summary.get("risk_budget_position") or {}
    current_account = risk.get("current_fixed_position_account") or {}
    risk_account = risk.get("risk_budget_account") or {}
    stop = summary.get("stop_execution_pressure") or {}
    defensive = summary.get("defensive_skip") or {}
    return "\n".join(
        [
            "# 影子实验评估",
            "",
            "免责声明：本报告仅用于个人模拟复盘和策略实验设计，不构成投资建议。",
            "",
            "## 样本口径",
            "",
            f"- {summary.get('sample_scope')}",
            f"- 晋级规则：{summary.get('promotion_rule')}",
            "",
            "## 实验对照",
            "",
            "| 实验 | 样本 | 胜率 | 平均单笔 | 中位单笔 | 最差单笔 | 备注 |",
            "|---|---:|---:|---:|---:|---:|---|",
            (
                f"| 当前触发区间 | {current.get('trade_count', 0)} | {_fmt_pct(current.get('win_rate'))} | "
                f"{_fmt_pct(current.get('average_return'))} | {_fmt_pct(current.get('median_return'))} | "
                f"{_fmt_pct(current.get('worst_trade_return'))} | 主策略回测口径 |"
            ),
            (
                f"| 次日开盘直接买 | {next_open.get('trade_count', 0)} | {_fmt_pct(next_open.get('win_rate'))} | "
                f"{_fmt_pct(next_open.get('average_return'))} | {_fmt_pct(next_open.get('median_return'))} | "
                f"{_fmt_pct(next_open.get('worst_trade_return'))} | "
                f"平均差值 {_fmt_pct(next_open.get('average_return_delta_vs_current'))} |"
            ),
            "",
            "## 防守环境跳过",
            "",
            f"- 当前回测中的防守环境交易数：{defensive.get('current_defensive_trade_count', 0)}",
            f"- 非防守环境交易数：{defensive.get('non_defensive_trade_count', 0)}",
            f"- 说明：{defensive.get('note')}",
            "",
            "## 风险预算仓位",
            "",
            f"- 单笔账户风险预算：{_fmt_pct(risk.get('account_risk_pct'))}",
            f"- 原固定仓位折算收益：{_fmt_pct(current_account.get('total_return'))}，最大回撤 {_fmt_pct(current_account.get('max_drawdown'))}，平均仓位 {current_account.get('average_position_pct')}%",
            f"- 风险预算仓位折算收益：{_fmt_pct(risk_account.get('total_return'))}，最大回撤 {_fmt_pct(risk_account.get('max_drawdown'))}，平均仓位 {risk_account.get('average_position_pct')}%",
            f"- 平均仓位变化：{risk.get('average_position_delta_pct')} 个百分点",
            "",
            "## 止损执行压力",
            "",
            f"- 止损样本：{stop.get('stop_loss_count', 0)} 笔",
            f"- 理想止损平均：{_fmt_pct(stop.get('ideal_stop_average'))}",
            f"- 滑点止损平均：{_fmt_pct(stop.get('slippage_stop_average'))}",
            f"- 日内最低压力平均：{_fmt_pct(stop.get('worst_intraday_average'))}",
            f"- 说明：{stop.get('note')}",
            "",
        ]
    )


def _render_report(
    date: str,
    indexes: list[dict],
    scored: list[dict],
    candidates: list[dict],
    excluded: list[dict],
    source: str,
    market_profile: Optional[dict] = None,
    backtest_summary: Optional[dict] = None,
    portfolio_summary: Optional[dict] = None,
    shadow_summary: Optional[dict] = None,
    ledger_summary: Optional[dict] = None,
    review_summary: Optional[dict] = None,
    health_summary: Optional[dict] = None,
    steward_summary: Optional[dict] = None,
    learning_summary: Optional[dict] = None,
) -> str:
    """渲染仿图片风格的研究汇报。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"使用市场数据接口完成了 {now} 收盘扫描，并已维护观察台账：",
        "",
        f"- 当前启用策略：{_strategy_display_name()}（`{_active_strategy_key()}`）",
        "- 已写入 `daily_scans.csv`",
        f"- 已写入 {len(candidates)} 只研究候选到 `research_candidates.csv`",
        f"- 已写入 {len(candidates)} 笔 pending 到 `simulated_trades.csv`",
        "- 已写入市场环境过滤到 `market_profile.json`",
        "- 已写入规则回测到 `backtest_trades.csv` / `backtest_summary.json`",
        "- 已写入组合级回测到 `portfolio_backtest_trades.csv` / `portfolio_backtest_summary.json`",
        "- 已写入影子实验到 `shadow_experiments.csv` / `shadow_experiments_summary.json`",
        "- 已更新 pending/持仓台账到 `data/ledger/`",
        "- 已写入计划复盘到 `review_summary.json` / `review_report.md`",
        "- 已写入策略健康监控到 `strategy_health_summary.json` / `strategy_health_report.md`",
        "- 自动化记忆已更新到 `memory.md`",
        f"- 数据来源：{_fmt_source(source)}",
    ]
    if steward_summary:
        if steward_summary.get("status") == "success":
            lines.append(f"- Hermes 策略管家已写入 `{steward_summary.get('report_path')}`")
        else:
            lines.append(f"- Hermes 策略管家运行状态：{steward_summary.get('status')}")
    if learning_summary:
        lines.append(f"- 策略学习记忆已更新 `{learning_summary.get('memory_markdown_path')}`")
    if review_summary:
        lines.extend(
            [
                "",
                "**昨日计划复盘**",
                "",
                (
                f"- 复盘对象：{review_summary.get('reviewed_count')} 笔，"
                f"今日新 pending：{review_summary.get('new_pending_count')} 笔"
            ),
            (
                f"- 今日新触发：{review_summary.get('newly_triggered_count', review_summary.get('triggered_count'))}，"
                f"历史已入场/退出：{review_summary.get('entry_confirmed_count', '-')}，"
                f"未触发：{review_summary.get('not_triggered_count')}，"
                f"继续观察：{review_summary.get('active_open_count')}，"
                f"本次归档：{review_summary.get('closed_count')}，"
                f"同标的跳过：{review_summary.get('skipped_same_symbol_count', 0)}"
            ),
                (
                    f"- 平均收盘相对信号价：{_fmt_pct(review_summary.get('average_close_vs_signal_pct'))}，"
                    f"最大顺向波动：{_fmt_pct(review_summary.get('best_max_favorable_pct'))}，"
                    f"最大逆向波动：{_fmt_pct(review_summary.get('worst_max_adverse_pct'))}"
                ),
                f"- 复盘详情：`{review_summary.get('report_path')}`",
            ]
        )
        report_items = review_summary.get("report_items") or []
        if report_items:
            lines.extend(
                [
                    "",
                    "| 标的 | 信号日 | 复盘状态 | 收盘偏离 | 最大顺向 | 最大逆向 | 观察 |",
                    "|---|---:|---|---:|---:|---:|---|",
                ]
            )
            for item in report_items[:5]:
                observation = str(item.get("observation") or "-").replace("|", "/")
                lines.append(
                    "| {name} {symbol} | {signal_date} | {status} | {close_dev} | {mfe} | {mae} | {observation} |".format(
                        name=item.get("name") or "",
                        symbol=item.get("symbol") or "",
                        signal_date=item.get("signal_date") or "-",
                        status=item.get("status_label") or item.get("status") or "-",
                        close_dev=_fmt_pct(item.get("close_vs_signal_pct")),
                        mfe=_fmt_pct(item.get("max_favorable_pct")),
                        mae=_fmt_pct(item.get("max_adverse_pct")),
                        observation=observation,
                    )
                )
    lines.extend(["", "**大盘判断**", _market_comment(indexes, scored, market_profile)])
    if health_summary:
        primary = health_summary.get("primary_window") or {}
        lines.extend(
            [
                "",
                "**策略健康监控**",
                "",
                (
                    f"- 主观察窗口：{primary.get('window_days', '-')} 日，"
                    f"复盘样本 {primary.get('reviewed_count', 0)} 笔，"
                    f"触发率 {_fmt_pct(primary.get('trigger_rate'))}，"
                    f"第一止盈率 {_fmt_pct(primary.get('take_profit_rate'))}，"
                    f"止损率 {_fmt_pct(primary.get('stop_loss_rate'))}"
                ),
                (
                    f"- 已归档平均净收益：{_fmt_pct(primary.get('avg_net_return'))}，"
                    f"平均顺向 {_fmt_pct(primary.get('avg_max_favorable_pct'))}，"
                    f"平均逆向 {_fmt_pct(primary.get('avg_max_adverse_pct'))}"
                ),
                f"- 体检详情：`{health_summary.get('report_path')}`",
            ]
        )
        for note in (primary.get("diagnostics") or [])[:3]:
            lines.append(f"- 健康提示：{note}")
        for experiment in (primary.get("experiment_hypotheses") or [])[:2]:
            lines.append(f"- 候选实验：{experiment}")
    if steward_summary:
        lines.extend(
            [
                "",
                "**Hermes 策略管家**",
                "",
                f"- 模式：{steward_summary.get('mode')}，状态：{steward_summary.get('status')}",
                f"- 报告/草案路径：`{steward_summary.get('report_path')}`",
            ]
        )
        digest_lines = steward_summary.get("digest_lines") or []
        if digest_lines:
            for line in digest_lines[:3]:
                lines.append(f"- 核心诊断：{line}")
        elif steward_summary.get("status") == "failed":
            error = steward_summary.get("error") or steward_summary.get("stderr_tail") or "未生成诊断"
            lines.append(f"- 运行提示：{error}")
        else:
            lines.append("- 运行提示：已生成完整文件，摘要提取为空，请查看报告路径")
    if learning_summary:
        lines.extend(
            [
                "",
                "**策略学习记忆**",
                "",
                f"- 经验条目：{learning_summary.get('lesson_count')} 条",
                f"- 本次更新：{', '.join(learning_summary.get('updated_lessons') or []) or '无新增'}",
                f"- 记忆路径：`{learning_summary.get('memory_markdown_path')}`",
            ]
        )
    if market_profile:
        lines.extend(
            [
                "",
                "**市场环境过滤**",
                "",
                f"- 环境评级：{market_profile.get('regime_label')}（分数 {market_profile.get('score')}）",
                (
                    f"- 执行参数：候选上限 {market_profile.get('candidate_limit')}，"
                    f"最低综合分 {market_profile.get('min_final_score')}，"
                    f"仓位系数 {market_profile.get('position_multiplier')}，"
                    f"单票上限 {market_profile.get('max_position_pct')}%"
                ),
                "- 仓位口径：`market_position_multiplier` 为环境目标系数，"
                "`effective_position_multiplier` 为四舍五入和仓位上限后的实际生效系数。",
                (
                    f"- 市场宽度：上涨占比 {_fmt_pct(market_profile.get('up_ratio'))}，"
                    f"中位涨跌 {_fmt_pct(market_profile.get('median_change'))}，"
                    f"强势样本 {market_profile.get('strong_count')} 只"
                ),
                (
                    f"- 指数趋势：站上 MA20 比例 {_fmt_pct(market_profile.get('above_ma20_ratio'))}，"
                    f"站上 MA60 比例 {_fmt_pct(market_profile.get('above_ma60_ratio'))}，"
                    f"指数平均涨跌 {_fmt_pct(market_profile.get('avg_index_change'))}"
                ),
            ]
        )
        for item in market_profile.get("index_details") or []:
            lines.append(
                f"- {item.get('label')}：{_fmt_pct(item.get('change_rate'))}，"
                f"20日 {_fmt_pct(item.get('change_rate_20d'))}，"
                f"{'站上' if item.get('above_ma20') else '未站上'}MA20"
            )
    lines.extend(
        [
        "",
        "**今日候选，不超过 5 只**",
        "",
        "| 标的 | 综合分 | 策略标签 | 当前/信号价 | 触发区间 | 止损 | 第一止盈 | 仓位 | 计划 |",
        "|---|---:|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in candidates:
        lines.append(
            "| {name} {symbol} | {final_score:.1f} | {tags} | {signal:.2f} | {zone} | {stop:.2f} | {take:.2f} | {position}% | {plan} |".format(
                name=item.get("name"),
                symbol=item.get("symbol"),
                final_score=item.get("final_score") or item.get("score") or 0,
                tags=item.get("strategy_tags") or "-",
                signal=item.get("signal_price") or item.get("latest") or 0,
                zone=item.get("trigger_zone") or "-",
                stop=item.get("stop_loss") or 0,
                take=item.get("first_take_profit") or 0,
                position=item.get("position_pct") or 0,
                plan=item.get("plan") or "-",
            )
        )

    lines.extend(
        [
            "",
            "**本轮策略来源与落地**",
            "",
            "- qstock 思路：借鉴 RPS、趋势和资金/量能模型，但只落地为可由 OHLCV 复现的横截面动量与量能过滤。",
            "- Momentum-Investing 思路：采用市场内横截面动量、趋势过滤和波动率约束，不引入黑箱预测。",
            "- tw_stocker 思路：采用“20 日动量 × 60 日趋势”的隔夜筛选框架，并用流动性样本先缩小股票池。",
            "- A-Share Volatility Strategy 思路：把历史波动率和成交活跃度作为风险过滤，而不是只按涨幅排序。",
            "- QuantsPlaybook 思路：保留多因子/择时可复现原则，本轮仅使用透明技术因子，不接入深度学习或复杂基本面模型。",
        ]
    )

    if backtest_summary:
        win_rate = backtest_summary.get("win_rate")
        avg_ret = backtest_summary.get("average_return")
        median_ret = backtest_summary.get("median_return")
        event_dd = backtest_summary.get("event_sequence_max_drawdown")
        worst_trade = backtest_summary.get("worst_trade_return")
        reason_counts = backtest_summary.get("exit_reason_counts") or {}
        lines.extend(
            [
                "",
                "**规则回测摘要（历史 K 样本）**",
                "",
                f"- 覆盖标的：{backtest_summary.get('symbol_count')} 只，回测笔数：{backtest_summary.get('trade_count')} 笔",
                f"- 胜率：{_fmt_pct(win_rate)}，平均单笔：{_fmt_pct(avg_ret)}，中位单笔：{_fmt_pct(median_ret)}",
                f"- 最差单笔：{_fmt_pct(worst_trade)}，事件序列回撤：{_fmt_pct(event_dd)}，扣除成本：{backtest_summary.get('cost_bps')}bp/笔",
                (
                    "- 退出分布："
                    f"止盈 {reason_counts.get('take_profit', 0)}，"
                    f"止损 {reason_counts.get('stop_loss', 0)}，"
                    f"到期 {reason_counts.get('timeout', 0)}"
                ),
                (
                    f"- 止损执行压力：下穿止损 {backtest_summary.get('stop_loss_breach_count', 0)} 笔，"
                    f"下穿率 {_fmt_pct(backtest_summary.get('stop_loss_breach_rate'))}，"
                    f"平均下穿 {_fmt_pct(backtest_summary.get('average_stop_breach_pct'))}，"
                    f"最深下穿 {_fmt_pct(backtest_summary.get('worst_stop_breach_pct'))}，"
                    f"止损滑点版平均 {_fmt_pct(backtest_summary.get('average_slippage_return'))}"
                ),
                f"- 规则：{backtest_summary.get('rule')}",
                f"- 注：{backtest_summary.get('note')}",
            ]
        )

    if portfolio_summary:
        lines.extend(
            [
                "",
                "**组合级回测摘要**",
                "",
                (
                    f"- 初始资金：{portfolio_summary.get('initial_cash'):.0f}，"
                    f"期末权益：{portfolio_summary.get('ending_equity'):.0f}，"
                    f"组合收益：{_fmt_pct(portfolio_summary.get('total_return'))}"
                ),
                (
                    f"- 接受交易：{portfolio_summary.get('accepted_trades')}，"
                    f"因持仓/敞口跳过：{portfolio_summary.get('skipped_trades')}，"
                    f"最大同时持仓：{portfolio_summary.get('max_positions')}，"
                    f"总敞口上限：{_fmt_pct(portfolio_summary.get('max_total_exposure_pct'))}"
                ),
                (
                    f"- 组合胜率：{_fmt_pct(portfolio_summary.get('win_rate'))}，"
                    f"平均单笔：{_fmt_pct(portfolio_summary.get('average_trade_return'))}，"
                    f"最大回撤：{_fmt_pct(portfolio_summary.get('max_drawdown'))}"
                ),
                f"- 注：{portfolio_summary.get('note')}",
            ]
        )

    if shadow_summary:
        current = shadow_summary.get("current_trigger") or {}
        next_open = shadow_summary.get("next_day_open_buy") or {}
        risk = shadow_summary.get("risk_budget_position") or {}
        current_account = risk.get("current_fixed_position_account") or {}
        risk_account = risk.get("risk_budget_account") or {}
        stop = shadow_summary.get("stop_execution_pressure") or {}
        lines.extend(
            [
                "",
                "**影子实验评估**",
                "",
                (
                    f"- 当前触发区间：{current.get('trade_count', 0)} 笔，"
                    f"胜率 {_fmt_pct(current.get('win_rate'))}，"
                    f"平均 {_fmt_pct(current.get('average_return'))}，"
                    f"中位 {_fmt_pct(current.get('median_return'))}"
                ),
                (
                    f"- 次日开盘直接买：{next_open.get('trade_count', 0)} 笔，"
                    f"胜率 {_fmt_pct(next_open.get('win_rate'))}，"
                    f"平均 {_fmt_pct(next_open.get('average_return'))}，"
                    f"相对当前 {_fmt_pct(next_open.get('average_return_delta_vs_current'))}"
                ),
                (
                    f"- 风险预算仓位：单笔账户风险 {_fmt_pct(risk.get('account_risk_pct'))}，"
                    f"平均仓位 {risk_account.get('average_position_pct')}%，"
                    f"折算收益 {_fmt_pct(risk_account.get('total_return'))}，"
                    f"最大回撤 {_fmt_pct(risk_account.get('max_drawdown'))}"
                ),
                (
                    f"- 固定仓位折算：平均仓位 {current_account.get('average_position_pct')}%，"
                    f"折算收益 {_fmt_pct(current_account.get('total_return'))}，"
                    f"最大回撤 {_fmt_pct(current_account.get('max_drawdown'))}"
                ),
                (
                    f"- 止损压力：理想止损平均 {_fmt_pct(stop.get('ideal_stop_average'))}，"
                    f"滑点止损平均 {_fmt_pct(stop.get('slippage_stop_average'))}，"
                    f"日内最低压力平均 {_fmt_pct(stop.get('worst_intraday_average'))}"
                ),
                "- 说明：影子实验只提供证据，不改主策略、不写入台账；样本不足时仍优先不调参。",
                "- 详情：`shadow_experiments_report.md`",
            ]
        )

    if ledger_summary:
        lines.extend(
            [
                "",
                "**Pending 台账**",
                "",
                (
                    f"- 当前 active：{ledger_summary.get('active_count')}，"
                    f"新 pending：{ledger_summary.get('new_pending_count')}，"
                    f"本次归档：{ledger_summary.get('closed_count')}，"
                    f"同标的跳过：{ledger_summary.get('skipped_same_symbol_count', 0)}"
                ),
                f"- 台账路径：`{ledger_summary.get('pending_path')}`",
            ]
        )

    lines.extend(["", "**容易误判但今天不列入**", ""])
    for item in excluded[:5]:
        lines.append(
            f"- {item.get('name')} {item.get('symbol')}：{item.get('filter_reasons') or item.get('risk_notes')}"
        )

    lines.extend(
        [
            "",
            "**明日复核要点**",
            "早盘运行时，优先复核今日 pending 的成交额延续性、触发区间承接、以及同板块是否继续扩散。若弱开后无法收复触发区间，取消；若高开过大且换手急升，不追。",
            "",
            "---",
            "免责声明：本扫描为个人模拟交易计划，所有参数由规则自动生成，仅供复盘与风控演练。真实交易风险自担。",
            "",
        ]
    )
    return "\n".join(lines)
