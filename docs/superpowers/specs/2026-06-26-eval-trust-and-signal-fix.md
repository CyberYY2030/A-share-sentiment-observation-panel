# 选股系统下一步规格：评估可信化 + trend_embryo 信号修正 + 全区间校准

状态：待实现（实现交给 Codex）
作者：规划 by Claude
前置：`2026-06-26-scanner-validation-and-separation-upgrade.md` 已由 Codex 完成（Phase A/B/C，65 测试通过）。
触发：30 日真实评估（2026-05-13 → 2026-06-24）暴露三个问题——评估跑不完全区间、对照口径被肥尾误导、`trend_embryo` 打分反向预测。必须先把证据做可信、修掉信号缺陷，再用满区间校准。

---

## 0. 背景与实证发现（本规格的出发点）

30 日评估报告（`output/scanner_eval_2026-05-13_2026-06-24.md`）：

| strategy | n_eval | win_rate | avg_r5 | median_r5 | score 高/中/低桶 avg_r5 | 单调 |
|---|---|---|---|---|---|---|
| baseline（全市场） | 129661 | — | +1.07% | −2.88% | — | — |
| momentum_breakout | 228 | 37.7% | +2.30% | −0.89% | — | — |
| rps_stock_top20 | 40 | 25.0% | −9.86% | −8.74% | −20.1/+0.2/−8.2 | False |
| trend_embryo | 749 | 33.4% | −0.41% | −2.26% | **−2.04/−0.57/+1.38** | False（倒挂） |
| true_leader | 500 | 37.2% | +0.26% | −2.60% | **+3.89/−2.22/−0.91** | False（头部有效） |

**三个必须处理的问题**：
1. **评估跑不完全区间**：全区间（2025-11-25 → 2026-06-17，~140 交易日）运行 >3 分钟超时被中止，校准只用了 30 日单一牛市样本 → 锁参数有过拟合风险。
2. **对照口径被肥尾误导**：基线 `avg_r5=+1.07%` 但 `median_r5=−2.88%`，均值被少数暴涨股拉高。按均值两个新扫描器都"没跑赢"，按中位数 `true_leader(−2.60%)`/`trend_embryo(−2.26%)` 反而都略胜基线。只看 `avg_r5_vs_baseline` 会把决策带偏。
3. **`trend_embryo` 打分反向预测**：score 高分桶 avg_r5=−2.04% < 低分桶 +1.38%，打分公式选错方向。
4. （非缺陷，但影响用法）`true_leader` 可用 edge 集中在最高分桶（+3.89%），top_n=20 整体被拉平 → 实战看榜首、校准应优化头部。

**用户已定取舍**：
- 范围 = D + E + F 全做（评估可信 → 修信号 → 全区间校准）。
- `trend_embryo` 信号倒挂 = **先量化诊断再定修正方案**，不盲目改公式。

**边界**：不改 `momentum_breakout`/`rps_stock`/`universe.py` 算法；不依赖实时行情/龙虎榜；纯本地 DB。`true_leader` v1.1 算法本轮不动（只在 F 阶段校准默认值）。

---

## 1. Phase D：评估可信化（`mining/evaluate.py`）

目标：让全区间（~140 交易日）能在合理时间跑完，并把对照口径补成决策友好。

### D1 性能：全区间可跑完
- **主瓶颈 = `compute_baseline()` 逐日两次全市场 SQL**（~140 日 × 2 查询 × 13 万行）。改为**一次性**加载区间内全部 stock 收盘到一个 `trade_date × sec_code` pivot，再向量化算每只票的 t+5 比例：
  ```
  close_pivot 全区间一次读入 → 对每个 entry 日 d，t5 = calendar[idx+5]
  day_return = close_pivot.loc[t5] / close_pivot.loc[d] - 1   # 向量化，无逐日 SQL
  baseline = 所有 (d, code) day_return 的均值/中位数
  ```
- `run_scanners_for_dates()`：新增 `--skip-existing`（默认开），对 `(strategy_id, version, trade_date)` 已有 candidates 的日期**跳过重跑**（upsert 幂等，重跑是纯浪费）。这样增量评估只跑新日期。
- 加**进度输出**：每处理 N（默认 10）个交易日 print 一行 `已处理 d / 总数，累计 candidates`。

### D2 对照口径：补公平基线（决策友好）
聚合表/报告在现有列基础上**新增**：
- `median_r5_vs_baseline = strategy.median_r5 − baseline.median_r5`（中位数相对基线，抗肥尾）。
- `win_rate_vs_baseline = strategy.win_rate − baseline_win_rate`，其中 baseline_win_rate 用与 `is_win` 同口径（`r2>0.02 且 r3>−0.03`）在全市场样本上算；若实现成本高，退而求其次用"基线 t+5 为正的占比"，并在列名/文档注明口径差异。
- **按日 top-k 桶**：参数 `--top-k`（默认 5）。对每个策略，取每个交易日 score 最高的 k 只候选，单独聚合 `topk_n / topk_win_rate / topk_avg_r5 / topk_median_r5`。这是用户实战看榜首的真实用法，比 top_n=20 均值更贴近决策。
- 保留现有 `avg_r5_vs_baseline`，但报告把均值口径与中位数口径**并列展示**，并在表头注释一句"均值受肥尾影响，参考中位数/topk"。

### D3 default 评估区间
- `--start`/`--end` 缺省时，自动取"留足 lookback(20) 与 t+5 后"的可评估区间（约 2025-11-25 → 2026-06-17，按本地数据动态算，不写死）。

### D4 测试（更新 `tests/test_mining_evaluate.py`）
- `compute_baseline` 向量化版与原逐日版在同一合成数据上结果一致（防重构改坏）。
- `--skip-existing`：已存在 candidates 的日期不重复 save（用 mock 或计数断言）。
- D2 新列：合成数据断言 `median_r5_vs_baseline`、`win_rate_vs_baseline`、top-k 桶聚合数值正确；空区间/无完整 outcome 不抛错。

---

## 2. Phase E：`trend_embryo` 信号倒挂诊断与修正

**先诊断、后改公式**（用户决定）。诊断结论与最终改动都回填本规格"实现记录"。

### E1 诊断（量化拆解，不改算法）
新增一次性诊断（可写在 `evaluate.py` 增加 `--diagnose trend_embryo` 子路径，或独立 `mining/diagnostics.py`，择简）：
- 对 30 日 + 全区间两段，分别看 `trend_embryo` 候选的单因子与 t+5 的关系：
  - `limit_up_count_5d`（0 vs 1）分组 avg_r5/median_r5 —— 验证 `w_path` 偏好"0 涨停"是否反而选了弱票。
  - `small_yang_count` 分桶 avg_r5 —— 验证小阳堆积加分方向。
  - `single_day_max_change`（spike）分桶 —— 验证 spike 扣分方向。
  - `ret_5d` 分桶（已知 30 日 25-32 桶 +1.84%、32-40 桶 −4.76%）全区间复看。
- 产出：每个因子与前向收益的方向性结论（正相关/无关/反向）。

### E2 修正（依据 E1 结论）
- 仅对**方向被证伪**的项调整：例如若"0 涨停"反向，则降低或反转 `w_path`；若 `ret_5d` 上沿确为负，则收窄 `embryo_ret_hi`。
- 若诊断显示**整体打分对 t+5 无区分度**（embryo 本质是"加速前"、edge 可能在更长 horizon 或需独立性维度），则**不强行造分**：保留硬门槛筛选、把 score 退化为"可读排序"并在实现记录注明"embryo 的 edge 不在 t+5 打分，建议作为候选池观察而非直接打分排序"。诚实记录优于硬凑单调。
- 版本号升 `v1.1`，`strategy_id` 不变（白名单无需改）。
- 测试：更新 `tests/test_mining_pipeline.py` 覆盖新打分/退化逻辑。

---

## 3. Phase F：全区间校准（基于 D 的可信评估）

1. 用 D 优化后的 `evaluate.py` 跑**满区间**（~140 日），产出含中位数/topk/胜率相对基线的对照表。
2. 对关键参数粗网格（每参数 3–5 值），看**全区间** topk_avg_r5 / median_r5_vs_baseline / win_rate：
   - `true_leader`：`sep_excess_min`（30 日证据指向 3–5，需全区间确认）、`top_n`（头部有效→是否收紧到 8–12）、`ret_cap_soft/hard`、`strength_min`、`amt_band_*`。
   - `trend_embryo`：E2 修正后的关键参数（`embryo_ret_hi` 30 日指向 32–35）。
3. 选**方向性稳健**值（不追极值），**建议值 + 区间证据回填本规格实现记录，最终落定前回到用户/Claude 复核**——Codex 不擅自改默认值。

> 防过拟合：用满 ~140 日跨 regime；注明换行情需复测；优先用中位数/topk 口径而非均值。

---

## 4. 交付顺序

```
Phase D：evaluate.py 性能 + 公平口径 + 测试        ← 先让全区间能跑、口径公平
Phase E：trend_embryo 诊断 → 修正 v1.1 + 测试       ← 修掉反向打分
Phase F：全区间重跑 → 校准建议值 + 证据回填（待复核）
```

---

## 5. 验证命令

```powershell
python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui tests.test_mining_evaluate
python -m py_compile mining/evaluate.py mining/scanners/trend_embryo.py
```
真实数据：先小区间冒烟，再全区间：
```powershell
python -m mining.evaluate --last-n-days 30
python -m mining.evaluate            # 缺省=全可评估区间，应能跑完并打印进度
```

验收：
1. 测试全绿、py_compile 通过。
2. 全区间评估**能跑完**（有进度输出、不超时）；报告含中位数相对基线、胜率相对基线、按日 top-k 桶。
3. `compute_baseline` 重构后与原逻辑同结果（测试保证）。
4. `trend_embryo` 诊断结论 + 修正（或退化为候选池的明确说明）回填实现记录。
5. Phase F 给出全区间校准建议值与证据（待复核，不擅自改默认值）。

---

## 6. 约束

- 不改 `momentum_breakout`/`rps_stock`/`universe.py` 算法；`true_leader` v1.1 算法本轮不动（仅 F 校准默认值）。
- 历史复跑写 candidates（幂等 upsert）；`--skip-existing` 默认开，先小区间冒烟再全跑。
- 评估只统计 outcome 完整（t+2/t+5 非空）的候选；基线同口径。
- 向量化优先；`compute_baseline` 严禁逐日全市场 SQL。
- 编辑前 `git status --short`，勿回退无关改动；不提交 `knowledge/_raw/`。

---

## 7. 实现记录（Codex 填写）

### Phase D：evaluate.py
- `compute_baseline()` 已改为一次性读取 `ash.kline_daily` 区间内 stock 的 `close/high/low`，构造 `trade_date × sec_code` pivot，按交易日向量化计算 t+5 `r5` 与 t+1 胜率口径；保留 `compute_baseline_daily_sql()` 作为测试对照，防止向量化重构改坏结果。
- `resolve_eval_dates()` 默认动态取“留足 lookback(20) 与 t+5”的可评估区间。本地当前默认区间为 `2025-11-20 -> 2026-06-17`，共 138 个交易日。
- `run_scanners_for_dates()` 新增 `skip_existing=True` 默认行为，按 `(strategy_id, version, trade_date)` 已有候选跳过重跑；新增 `progress_every` 进度输出。本轮全区间复跑有进度输出并在 92 秒内完成，随后基于 skip-existing 的当前版本报告 19 秒完成。
- 聚合新增公平口径：`median_r5_vs_baseline`、`win_rate_vs_baseline`、`topk_n/topk_win_rate/topk_avg_r5/topk_median_r5`，默认 `top_k=5`。报告注明均值受肥尾影响，应并看中位数与 top-k。
- 额外修正：评估聚合默认只统计当前注册 scanner 版本，避免同一 `strategy_id` 的 v1.0/v1.1 混算污染结果；未知/测试策略仍可按 `strategy_id` 聚合。
- 新增 `--diagnose trend_embryo`，输出 `output/trend_embryo_diagnostics_<start>_<end>.md`。

### Phase E：trend_embryo 诊断与修正
- 30 日诊断（`2026-05-06 -> 2026-06-16`）：`limit_up_count=0` 优于 1；`small_yang_count` 不单调；`single_day_max_change` 方向不稳定；`ret_5d 25-32` 桶均值较好但 `32-40` 明显转差。
- 全区间诊断（`2025-11-20 -> 2026-06-17`）：`limit_up_count=0/1` 差异很小且中位数都为负；`small_yang_count` 均值高计数更好但中位数不稳定；`single_day_max_change <=5` 最好、`>12` 最差；`ret_5d 32-40` 均值为正但样本少，中位数仍为负。
- 结论：旧 score 的三项加减分没有稳定、可解释的 t+5 单调预测力；按规格选择不硬凑单调公式。
- 修正：`trend_embryo` 升 `version="v1.1"`，保留硬门槛筛选，`score=0.0`，features 增加 `score_mode="candidate_pool"`，排序退为稳定 `sec_code`。该 scanner 定位为“早期胚子候选池”，不再把 score 当作短期收益排序信号。

### Phase F：全区间校准建议（待复核，不改默认值）
- 全区间当前版本报告：`output/scanner_eval_2025-11-20_2026-06-17.md`。
- baseline：`n=714875`，`avg_r5=+2.17%`，`median_r5=-0.31%`，`win_rate=33.20%`。均值明显受肥尾影响，校准以中位数、胜率和 top-k 为主。
- `trend_embryo v1.1`：`n_candidates=4134`，`n_evaluated=4131`，`win_rate=38.61%`（相对 baseline +5.41pct），`avg_r5=-0.30%`，`median_r5=-1.99%`（相对 baseline -1.68pct），`top5/day avg_r5=-1.55%`，`top5/day median_r5=-2.75%`。建议：暂不把 score 用于排序；默认参数暂不改，后续若要校准，优先复核 `single_day_max_change <=5` 与 `ret_5d 32-40` 是否在更长窗口仍有可交易含义。
- `true_leader v1.1`：`n_candidates=2760`，`n_evaluated=2759`，`win_rate=34.90%`（相对 baseline +1.70pct），`avg_r5=-0.09%`，`median_r5=-2.81%`，`top5/day avg_r5=-0.84%`。建议：不要仅因 30 日头部表现上调权重；全区间 top-k 没有确认 edge，`sep_excess_min=3-5` 仍可作为待复核区间，但本轮不建议直接改默认。
- `momentum_breakout`：`top5/day avg_r5=+0.60%`，`win_rate=35.72%`，整体中位数仍低于 baseline；可作为对照中较稳的短线扫描器，但本规格未要求改算法。
- `rps_stock_top20`：`avg_r5=-0.38%`，`median_r5=-2.27%`，`top5/day avg_r5=-2.22%`，维持弱对照。
- 校准建议总表：`trend_embryo` 保持候选池模式；`true_leader` 默认值暂不改，先复核 `sep_excess_min`、`top_n`、`w_ext` 的全区间网格；所有建议待用户/Claude 复核，不擅自改默认参数。

### 测试覆盖
- `tests.test_mining_evaluate`：覆盖向量化 baseline 与逐日 SQL 对齐、`skip_existing`、默认可评估区间、当前注册版本过滤、公平口径列、top-k 聚合、报告写出。
- `tests.test_mining_pipeline`：覆盖 `trend_embryo v1.1` 候选池 features（`score_mode="candidate_pool"`）与通用分支持久化。
- 验证已通过：`python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui tests.test_mining_evaluate`（68 tests OK）；`python -m py_compile mining/evaluate.py mining/scanners/trend_embryo.py`。