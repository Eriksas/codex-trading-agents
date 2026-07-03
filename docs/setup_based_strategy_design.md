# Setup-Based Strategy Design

## 1. 为什么从 ranking-based 转向 setup-based

2021 起长样本显示，`alpha040_v3_risk_controlled` 和多组短线 ranking shadow 版本收益端均未通过验证。继续在同一横截面排序框架里调 alpha040、RPS 或持有期，容易把问题误判为参数问题。新的研究方向改为事件型 setup：没有明确形态、市场和板块共振时允许不交易。

## 2. 三类 setup 的定义

- `pullback_reclaim_v1`：过去 20 日相对强，3-5 日回踩不破 MA20，量能收缩，当日重新站回 MA5/MA10。
- `volatility_contraction_breakout_v1`：10-20 日低波动平台，靠近 20 日高点，温和放量突破平台上沿。
- `panic_repair_v1`：3-10 日恐慌下跌后，不抢第一根修复，等待重新站上前高或 MA5 的确认。

## 3. 市场层和板块层的作用

市场层把行情分为 `strong_active`、`weak_active`、`neutral`、`defensive`。只有 strong/weak active 允许开仓。板块层使用 BaoStock 行业映射，要求行业短期收益、上涨比例、MA20 宽度和同步转强数量相对全市场占优，避免个股孤立强势。

## 4. 为什么 no-trade 是正确结果

短线 setup 的目标不是每天选出几只股票，而是在市场、板块和个股形态同时满足时才生成计划。没有 setup 时不交易，比强行交易更符合风险控制。

## 5. 为什么当前仍不能实盘

本模块只使用日线数据，且当前扩展数据为 efinance 单源口径。它无法验证盘中触发顺序、买卖盘口、涨停附近成交概率，也没有经过 forward paper trading 积累。

## 6. 为什么短线 setup 后续需要分钟数据验证

短线胜负往往取决于次日触发区间、冲高回落、跳空低开破位、接近涨停和失败点的先后顺序。日线只能做保守代理，分钟数据用于验证执行路径，不应被用来无限调参。

## 7. 为什么当前只做 shadow research

当前主策略和 legacy 回滚机制保持不变。setup-based 方向必须先通过长样本、压力测试和 forward paper trading，至少累计 30-50 笔后再讨论是否升级。
