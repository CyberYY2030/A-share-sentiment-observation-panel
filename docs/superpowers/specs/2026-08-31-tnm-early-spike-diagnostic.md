# TNM V2.4R D+1 首小时冲高诊断冻结规格

## 目标与边界

研究 V2.3 尾盘候选在 D 日 `[14:51,14:56)` VWAP 买入后，D+1 `09:30–10:30` 是否出现可执行的相对买价 +3% 冲高。研究只读 2023–2024 分钟源和冻结 V2.3 checkpoint，不改变正式持有、退出、选股或账户规则，不读取 2025/2026 分钟容器或含这些年份的日 K 工作簿。

候选总体固定为冻结 run `v23-development-4dc58bf0106b-adfb3b853979` 的 `all_candidate_rows` 中 `short_phase_only=false && liquidity_pass=true` 的 126,443 行。所有特征必须来自 D 14:50 前的 checkpoint 字段。

## 冻结标签和经济口径

- `buy_vwap`：D `[14:51,14:56)` 的正成交量 VWAP。
- `gap_hit3`：D+1 首分钟 `open >= 1.03 * buy_vwap`。
- `high_touch3`：D+1 `[09:30,10:30)` 任一分钟 `high >= 1.03 * buy_vwap`。
- `vwap5_hit3`：十二个完整五分钟块中任一块 VWAP `>= 1.03 * buy_vwap`。
- `intraday_hit3`：`high_touch3 && !gap_hit3`。
- `causal_next5_exit`：触发所在五分钟块完成后，仅用下一完整五分钟块 VWAP 成交；最后一块触发用 `[10:30,10:35)`。
- 未触发退出：D+1 `[10:30,10:35)` VWAP。所有经济结果 `net30 = gross - 0.003`。
- D+1 缺失、窗口无量或非法时保留 D 特征并输出明确缺失原因，不用未来日期替补。

## 固定实证设计

固定三个基础特征块：

- daily：`prior10_runup, drawdown, range_expansion, prev_ma5_dist, prev_ma10_dist`
- intraday：`ret1450, tail_return, vwap_dist, activity, position1450, tail_location`
- candidate_market：当日候选数、`ret1450/tail_return` 正比例、中位数和 IQR

固定三个交互：`breadth_x_vwap_dist, prior10_x_vwap_dist, tail_return_x_tail_location`。禁止临时扩展特征、阈值、聚类或行业网格。

按交易日滚动：首个测试日距首个训练样本必须至少完整六个日历月，两个月测试、移动两个月，训练/测试间 purge 两个交易日。每折仅用训练段估计缺失填充、标准化和线性方向；分别报告 `gap_hit3` 与 `intraday_hit3`，分位单调性、标准化差异、每折方向、Top-N 命中与完整 net30。方向先逐交易日计算横截面 Spearman/IC，再逐日等权；net30 跨所有测试日逐日等权，不按折等权。对照固定为同日 same-N seeded random、`activity`、`ret1450`。

模型和全部对照只在共同完整日期比较经济结果：任一选择器当日 Top-N 不足或任一入选行 net30 缺失，该日从所有选择器的经济比较共同排除，并单列缺失日数。禁止依赖 `groupby.mean` 跳过个别缺失行。

## Go/No-Go 与完整性门

只有基础块在至少 70% 测试折方向一致、每日 net30 大于 0 且胜最强对照，才允许指数或 soft-peer 增量。否则结论 `no_predictable_edge` 并停止扩展。

公司行为当前没有严格限于 2023–2024 的可靠调整因子合同，状态冻结为 `unproven`。这会阻断正式经济 Go；标签和可预测性研究仍可完成。最终结论只允许 `diagnostic_signal_go_research | no_predictable_edge | blocked_data_integrity | blocked_corporate_action_unproven`。

正成交量 VWAP 仅作为诊断价格口径，不代表订单一定可成交。研究不模拟涨跌停排队、微量成交可获得数量或参与率，统一 30bp 成本不覆盖无法成交风险。

## 验收

```powershell
python -m unittest tests.test_tnm_early_spike_diagnostic tests.test_tail_next_morning_v2
python -m py_compile mining/tnm_early_spike_diagnostic.py tests/test_tnm_early_spike_diagnostic.py
git diff --check -- mining/tnm_early_spike_diagnostic.py tests/test_tnm_early_spike_diagnostic.py docs/superpowers/specs/2026-08-31-tnm-early-spike-diagnostic.md
```

输出必须包含 `label_panel.parquet`、每日漏斗/异常、`research_report.md`、`summary.json`、`fold_results.csv`、`feature_results.csv`、`manifest.json` 和未来泄漏红灯后恢复绿灯证据。
