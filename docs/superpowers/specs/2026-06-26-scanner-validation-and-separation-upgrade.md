# 选股系统下一步规格：分离度量升级 + 有效性回测评估 + 参数校准

状态：待实现（实现交给 Codex）
作者：规划 by Claude
前置：`2026-06-26-early-anomaly-true-leader-mining.md` 已由 Codex 完成（Phase 0/1/2，58 测试通过）。
触发：在真实本地数据（截至 2026-06-24）实跑后发现 `true_leader` 的核心维度"分离逆势"在当前牛市退化——必须先修复并验证有效性，再谈扩展。

---

## 0. 背景与目标

**实证发现（本规格的出发点）**：
- `trend_embryo` 在真实数据上健康（早期主线强势票、小阳堆积、涨幅在带内），暂不改。
- `true_leader` 的 `separation_ratio`（二元逆势比例）退化：近 21 交易日中证1000**仅 2 个下跌日**（一个仅 −0.15%），比例只能取 {0, 0.5, 1.0}，强票全饱和到 1.0、得分并列，排序**退化为纯涨幅 RPS**（与现有 `rps_stock` 无本质差异），且选出 20 日翻倍（retN=135%）的极端延展票。

**目标**：让两个新扫描器从"能跑"到"经实证有效"，三件咬合的事：
1. **分离度量升级**（修复 `true_leader` 核心维度）。
2. **有效性回测评估**（用现成 outcomes 量化是否有 edge）。
3. **参数校准**（用回测分布回调默认值）。

**用户已定取舍**：
- 范围 = 修复 + 回测评估 + 校准（不先扩功能）。
- `true_leader` **加"过度延展"上界，偏中前段（中军）**，不纯追最强。

**边界**：不改 `momentum_breakout`/`rps_stock`/`trend_embryo` 算法与 `mining/universe.py`；不依赖实时行情/龙虎榜；纯本地 DB。

---

## 1. 现状契约（已核对）

- `mining/scanners/true_leader.py`：`separation_ratio = |{指数跌日 个股涨}| / |指数跌日|`，硬门槛 `sep_min=0.3`；score = `w_sep*sep_ratio + w_high*is_new_high + w_div*divergence_today + w_amt*amount_band_ok`。
- `mining/backtest.py::backfill_outcomes(conn, trade_date=None, force=False)`：对每个 stock candidate 算 `r1=open_t1, r2=high_t1, r3=low_t1, r4=close_t2, r5=close_t5`（均相对 entry_price 的比例），`is_win = (r2>0.02 and r3>-0.03)`，`status∈{complete,partial,halted,delisted}`。已有 7800+ outcomes。
- `mining/db.save_candidates`：upsert on `(strategy_id,version,trade_date,sec_code)`（复跑幂等）。
- 本地数据约 2025-10-23 起至 2026-06-24（股票+指数 000001/000300/000852/399001）。可评估区间：留足 lookback(20) 与 t+5 outcome 后，约 **2025-11-25 ~ 2026-06-17**。

---

## 2. Phase A：分离度量升级（`true_leader` → v1.1）

**文件**：改 `mining/scanners/true_leader.py`（`version="v1.1"`）。

### 2.1 连续超额收益度量（替代二元比例）
对窗口内指数下跌日集合 `D_down`：
```
excess(t) = stock.change_pct(t) - index.change_pct(t)        # 相对指数超额
separation_strength = mean(excess(t) for t in D_down)        # 连续值，核心排序依据
```
- 连续度量即使 `|D_down|` 很小也有区分度；天然涵盖"跌得比指数少也算强"。
- **保留** `separation_ratio`（二元）作为可读 feature，但**排序与门槛改用 `separation_strength`**。

### 2.2 下跌日充足性保护
- `min_down_days`（默认 3）。主窗口 `lookback`(20) 内 `|D_down| < min_down_days` 时：
  1. 自动扩窗到 `fallback_lookback`（默认 40）重算 `D_down` 与 `separation_strength`；
  2. 扩窗后仍不足，则标 `separation_insufficient=True`，并对分离项**降权**（score 里 `separation_strength` 乘 `insufficient_weight`，默认 0.5），不直接硬排除（避免牛市少跌时全军覆没）。
- feature 记录 `down_days_used`（实际用于计算的下跌日数）。

### 2.3 门槛语义变更
- 旧 `sep_min`（比例≥0.3）**改名** `sep_excess_min`（默认 0.0，即指数跌日平均仍能跑赢指数）。硬门槛：`separation_strength >= sep_excess_min`（`separation_insufficient` 时改为软门槛，仅降权不排除）。

### 2.4 过度延展上界（用户决定加，偏中前段）
- 软硬结合，阈值占位、待 Phase C 校准：
  - `ret_cap_hard`（默认 100）：`ret_N > ret_cap_hard` **排除**（剔除已翻倍的极端延展）。
  - `ret_cap_soft`（默认 60）：`ret_N ∈ (ret_cap_soft, ret_cap_hard]` 线性**降权**（`over_extension_penalty = w_ext * (ret_N - ret_cap_soft)/(ret_cap_hard - ret_cap_soft)`，默认 `w_ext=2.0`）。
  - feature 记录 `over_extended`（bool）与惩罚值。

### 2.5 新打分
```
score = w_sep * separation_strength_effective    # insufficient 时已乘 insufficient_weight
      + w_high * is_new_high
      + w_div * divergence_today
      + w_amt * amount_band_ok
      - over_extension_penalty
```
排序 `score desc → separation_strength desc → ret_N desc → sec_code`，取 `top_n`。

### 2.6 default_params（v1.1）
```python
{
  "lookback": 20, "fallback_lookback": 40, "strength_min": 70.0,
  "benchmark_code": "000852",
  "sep_excess_min": 0.0, "min_down_days": 3, "insufficient_weight": 0.5,
  "new_high_window": 20,
  "amt_band_lo": 2_000_000_000, "amt_band_hi": 8_000_000_000,
  "ret_cap_soft": 60.0, "ret_cap_hard": 100.0, "w_ext": 2.0,
  "top_n": 20,
  "w_sep": 3.0, "w_high": 1.0, "w_div": 1.0, "w_amt": 1.0,
}
```
features 增/改：`separation_strength, separation_ratio, down_days_used, separation_insufficient, over_extended`（其余沿用）。

### 2.7 测试（`tests/test_mining_pipeline.py` 更新）
- 指数仅 1–2 个下跌日，两只强票超额不同 → `separation_strength` 区分出高下（旧二元会并列，新连续不并列）。
- `|D_down| < min_down_days` → 触发 fallback 扩窗；扩窗仍不足 → `separation_insufficient=True` 且分离项降权（不被硬排除）。
- `ret_N > ret_cap_hard` 被排除；`ret_N ∈ (soft, hard]` 体现降权。
- 合成需含 index 行（`_mining_test_helpers.py` 已支持）。

**面板**：strategy_id 仍为 `true_leader`，无需改 `tab_scanner.py` 白名单；版本号升 v1.1。

---

## 3. Phase B：有效性回测评估（新模块）

**文件**：新建 `mining/evaluate.py`（CLI，argparse），复用 `db.connect`、各 scanner、`backfill_outcomes`。

### 3.1 流程
1. 入参：`--start`/`--end`（或 `--last-n-days`，默认评估区间见 §1）、`--strategies`（默认全部 registered）。
2. **历史复跑**：对区间内每个交易日 D、每个 scanner 跑 `run(conn, D)` → `save_candidates`（幂等 upsert）。
   - 设进度输出与上限保护（区间过长先报数再跑）；先支持小区间冒烟（如最近 30 交易日）。
3. **补全 outcomes**：`backfill_outcomes(conn, force=False)`。
4. **聚合**（只统计 outcome 完整即 `r4/r5` 非空的候选，避免末端偏差）：按 `strategy_id` 输出
   - `n_candidates, n_evaluated`
   - `win_rate = mean(is_win)`
   - `avg_r4`(t+2)、`avg_r5`(t+5)、`median_r5`、`std_r5`
   - 分桶（可选）：按 `features.score` 分高/中/低三桶看 `avg_r5` 是否单调——验证 score 是否真有意义。
5. **基线对照**：同区间全 `universe` 的平均 t+5 前向收益作为 baseline，判断扫描器是否跑赢市场。
6. **输出**：markdown 表写 `output/scanner_eval_<start>_<end>.md` 并 print 摘要。

### 3.2 关键产物
一张"策略 × 胜率 × 平均 t+2/t+5 收益 × 相对基线 × score 分桶单调性"对照表，回答：
- `trend_embryo`/`true_leader` 是否优于 `momentum`/`rps`？
- 分离升级后 `true_leader` 是否相对 `rps_stock` 体现出增量 edge？
- score 高桶收益是否真的更高（排序有效性）？

### 3.3 测试（新增 `tests/test_mining_evaluate.py`）
- 合成多日 candidates + outcomes，断言聚合 `win_rate/avg_r5/分桶` 计算正确；空区间/无完整 outcome 的边界不抛错。

---

## 4. Phase C：参数校准（基于 B 的回测）

1. 跑 `evaluate.py` 全评估区间，产出对照表。
2. 对关键参数做**粗网格**（每参数 3–5 个值，避免过拟合）看区间 `win_rate/avg_r5`：
   - `true_leader`：`sep_excess_min`、`ret_cap_soft/hard`、`strength_min`、`amt_band_*`。
   - `trend_embryo`：`embryo_ret_lo/hi`、`small_body_max`、`spike_ref`。
3. 选**方向性稳健**的值（不追极值），更新各 `default_params`。
4. **校准结果回填**到本规格"实现记录"（每参数选定值 + 依据的区间统计），**最终落定前回到用户/Claude 复核**——Codex 给出建议值与证据，不擅自做激进改动。

> 防过拟合：评估区间尽量用满（约 2025-11 ~ 2026-06，~140 交易日）；只调到"合理方向"，记录区间，注明换行情需复测。

---

## 5. 交付顺序

```
Phase A：true_leader 分离度量升级 v1.1 + 测试        ← 先修复，B 才评估到修好的版本
Phase B：mining/evaluate.py 回测评估 + 测试
Phase C：跑评估 → 提出校准值 + 证据 → 回填实现记录（待复核）
```

---

## 6. 验证命令

```powershell
python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui tests.test_mining_evaluate
python -m py_compile mining/scanners/true_leader.py mining/evaluate.py
```
真实数据冒烟：
```powershell
python -m mining.evaluate --last-n-days 30
```
验收：
1. 测试全绿、py_compile 通过。
2. `true_leader` v1.1 在真实数据上 top 候选 `score` **不再全并列**（`separation_strength` 有区分），且无 retN>ret_cap_hard 的极端延展票。
3. `evaluate.py` 产出策略对照表（含基线与 score 分桶）。
4. Phase C 在实现记录给出建议校准值与区间证据（待复核，不擅自激进改默认值）。

---

## 7. 约束

- 不改 `momentum_breakout`/`rps_stock`/`trend_embryo` 算法与 `universe.py`。
- 历史复跑会写 candidates 表（真实信号，幂等 upsert，可接受）；先小区间冒烟再全跑。
- 评估只统计 outcome 完整（t+2/t+5 非空）的候选。
- 向量化优先；分离超额是窗口×全市场计算，避免逐票循环。
- 编辑前 `git status --short`，勿回退无关改动。
- 不提交 `knowledge/_raw/`。

---

## 8. 实现记录（Codex 填写）

### Phase A：`true_leader` v1.1
- `mining/scanners/true_leader.py` 已升 `version="v1.1"`，`strategy_id` 保持 `true_leader`，因此 `tab_scanner.py` 白名单无需改。
- 新增/保留函数：`load_index_series()`、`_separation_metrics()`、`select_candidates_from_universe()`、`TrueLeaderScanner.run()`。
- 分离度量：保留 `separation_ratio` 作为可读 feature；新增 `separation_strength = mean(stock.change_pct - index.change_pct)`，门槛和排序改用连续超额收益。
- 下跌日保护：`min_down_days=3`；主窗口不足时用 `fallback_lookback=40` 扩窗。若可用历史少于 40 日，会用当前可用的更长窗口；扩窗仍不足则 `separation_insufficient=True`，分离项乘 `insufficient_weight=0.5`，不硬排除。
- 过度延展：`ret_cap_hard=100.0` 硬排除；`ret_cap_soft=60.0` 到 hard 之间线性扣分，`w_ext=2.0`。features 增加 `over_extended`、`over_extension_penalty`。
- v1.1 默认参数：`lookback=20`、`fallback_lookback=40`、`strength_min=70.0`、`benchmark_code="000852"`、`sep_excess_min=0.0`、`min_down_days=3`、`insufficient_weight=0.5`、`sep_today_min=0.0`、`new_high_window=20`、`amt_band_lo=2_000_000_000`、`amt_band_hi=8_000_000_000`、`ret_cap_soft=60.0`、`ret_cap_hard=100.0`、`w_ext=2.0`、`top_n=20`、`w_sep=3.0`、`w_high=1.0`、`w_div=1.0`、`w_amt=1.0`。

### Phase B：`mining/evaluate.py`
- CLI：`python -m mining.evaluate --start YYYY-MM-DD --end YYYY-MM-DD`、`--last-n-days N`、`--strategies ...`、`--out-dir ...`、`--no-rerun`。
- 主要函数：`resolve_eval_dates()`、`run_scanners_for_dates()`、`aggregate_results()`、`compute_baseline()`、`write_markdown_report()`、`evaluate_range()`。
- 聚合只统计 `r4/r5` 非空的完整 outcome，输出 `n_candidates`、`n_evaluated`、`win_rate`、`avg_r4`、`avg_r5`、`median_r5`、`std_r5`、score 高/中/低三桶 `avg_r5` 与 `score_monotonic`。
- 基线：同区间全 `ash.kline_daily` stock universe 的 t+5 平均/中位收益。
- 报告路径：`output/scanner_eval_<start>_<end>.md`。

### Phase C：评估结果与校准建议（待复核，不改默认值）
- 已完成真实数据 30 交易日评估：`2026-05-13 -> 2026-06-24`，报告 `output/scanner_eval_2026-05-13_2026-06-24.md`。
- 30 日策略对照：`true_leader` n=600/evaluated=500，win_rate=37.20%，avg_r5=+0.26%，median_r5=-2.60%；baseline avg_r5=+1.07%，median_r5=-2.88%。`trend_embryo` n=900/evaluated=749，avg_r5=-0.41%。`rps_stock_top20` avg_r5=-9.86%。`momentum_breakout` avg_r5=+2.30% 但 evaluated=228。
- `true_leader` v1.1 分离已恢复连续分布：`separation_strength` min=2.99、p50=5.91、p75=7.53、max=11.42；旧 `separation_ratio` 仍有 1.0 饱和，但不再主导排序。
- `true_leader` 延展上界生效：30 日完整样本 `ret_N` max=98.87，无 `ret_N>100` 候选；`ret_N<=40` 桶 avg_r5=-0.54%，`40-60` +1.40%，`60-80` +1.04%，`80-100` +1.94%。建议暂不下调 `ret_cap_hard=100`；`ret_cap_soft=60` 可保留，后续复核 `w_ext` 是否过强。
- `true_leader` 分离强度桶：`separation_strength 3-5` n=168 avg_r5=-0.43%，`>5` n=330 avg_r5=+0.62%。建议复核 `sep_excess_min` 从 `0.0` 上调到 `3.0-5.0` 的区间，偏稳健候选值 `5.0`，但需全区间确认。
- `true_leader` score 三桶：high avg_r5=+3.89%，mid=-2.22%，low=-0.91%，`score_monotonic=False` 但高桶有效。建议先复核 `sep_excess_min`，再评估是否调整 `w_sep/w_amt/w_ext`。
- `trend_embryo` 30 日分布：`ret_5d` p50=17.33、p75=21.00、p90=25.64；`25-32` 桶 avg_r5=+1.84%，`32-40` n=14 avg_r5=-4.76%。建议复核 `embryo_ret_hi` 从 `40` 收窄到 `32-35` 的区间；`embryo_ret_lo=13` 暂不建议改。
- 完整区间 `2025-11-25 -> 2026-06-17` 已尝试运行；超过 3 分钟无输出后按进度规则停止，未作为本次校准依据。后续建议优化 `evaluate.py` 进度输出/分批运行后再跑全区间确认。

### 测试覆盖
- `tests.test_mining_pipeline`：覆盖连续 `separation_strength` 拆分旧比例并列、fallback 扩窗、fallback 仍不足时软门槛和降权、`ret_cap_hard` 排除、`ret_cap_soft` 扣分、通用分支持久化。
- `tests.test_mining_evaluate`：覆盖聚合指标、score 三桶、空区间/无完整 outcome、baseline、`evaluate_range(..., rerun=False)` 报告写出。
- 验证命令已通过：`python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui tests.test_mining_evaluate`；`python -m py_compile mining/scanners/true_leader.py mining/evaluate.py`。