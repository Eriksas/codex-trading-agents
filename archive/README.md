# archive/ - 弃用入口与 legacy 策略

本目录存放不再使用、但因回滚或历史对照需要保留的代码。**不要在新代码中导入本目录**（唯一例外：`research/main_strategy_upgrade_v3.py` 以 legacy 策略为对照基准）。

| 文件 | 说明 |
|---|---|
| `market_scanner_legacy_v1.py` | `legacy_momentum_v1` 旧主策略扫描器。回滚步骤见 `docs/rollback_guide.md`；对应配置 `strategy_legacy_v1.json`。 |
| `main.py` | 阶段 1 的 subprocess CLI 编排入口，已被 `main_v2.py` 取代（见 `docs/sop.md`）。 |
| `main_native_task.py` | 原生 Agent Task 编排实验入口，已弃用（见 `docs/sop.md`）。 |
