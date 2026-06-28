# 选股系统下一步规格：测量公平化 + edge 终判

状态：待实现（实现交给 Codex）
作者：规划 by Claude
前置：`2026-06-26-eval-trust-and-signal-fix.md`（Phase D/E/F）已由 Codex 完成，68 测试通过；全区间报告 `output/scanner_eval_2025-11-20_2026-06-17.md` 已产出。
触发：全区间（138 日）评估显示两个新扫描器无 t+5 edge，但当前测量有两处可能**系统性冤枉扫描器**——基线用全市场（含一字板暴涨票）而非候选同票池；机械 t+5 持有不匹配"只负责发现、买卖点人工"的设计。先修测量，再对 edge 下最终结论。

---

## 0. 背景与本轮唯一目标

**全区间实证（不利但要正视）**：
- 基线（全市场）avg_r5=+2.17%、median=−0.31%、win_rate=33.20%。
- 按**中位数相对基线**，4 个扫描器全为负（true_leader −2.50pct、trend_embryo −1.68pct）。
- `true_leader` score 高分桶在全区间**翻成 −0.98% < 低分桶 +2.87%**（30 日的 +3.89% 头部优势是样本运气）。
- top-k 仅 `momentum_breakout` +0.60% 为正。

**两处测量疑点（本轮要消除）**：
1. **基线票池不对等**：基线用 `ash.kline_daily` 全市场，但候选来自 `build_universe`（已剔一字板/ST/北交所 8·4·9/总市值>5000亿/上市<87天）。基线 +2.17% 均值正被一字板暴涨票抬高，而这些票不在候选池 → 对扫描器不公平。
2. **机械 t+5 持有不匹配"发现工具"定位**：用户明确"选股只解决发现、买卖点人工"。固定 5 日持有可能测不出发现价值（真实用法是人工择时进出）。

**本轮唯一目标**：把测量改公平，用预先写死的判定门槛对两个新扫描器的 edge 下**最终结论**。

**用户已定取舍**：先修测量再下结论。

**严格边界（本轮不做）**：不改任何 scanner 算法（`true_leader`/`trend_embryo`/`momentum`/`rps`/`universe.py` 全不动）；不调任何默认参数（Phase F 校准继续冻结，直到测量可信且确认有 edge）。**纯测量层改动，集中在 `mining/evaluate.py`。**

---

## 1. Phase G1：同票池基线（对等比较）

- 新增 `compute_baseline_universe(conn, start, end, ...)`：基线只统计**候选同口径票池**——对区间内每个 entry 日 d，`build_universe(conn, d)` 得到当日合格票池，仅对这些 `sec_code` 算 t+5 `r5` 与 t+1 胜率口径（与 `is_win` 同口径 `r2>0.02 且 r3>−0.03`）。
- 性能：复用已加载的 `close/high/low` pivot，按日过滤到票池 code 后向量化，**不得逐日全市场 SQL**；`build_universe` 每日一次可接受（与 run_scanners 同量级）。
- 报告**同时展示两条基线**并标注：`baseline_market`（旧全市场，参考）与 `baseline_universe`（同票池，**决策主用**）。相对基线列默认改用 `baseline_universe`。
- 不删 `compute_baseline`/`compute_baseline_daily_sql`（保留为对照与回归防护）。

## 2. Phase G2：发现友好的前向指标（不靠固定持有）

在 evaluate 聚合层，对每个候选用已加载的 `high/low/close` pivot 算 t+1..t+5 窗口指标（**不改 outcomes 表 schema**，纯测量计算）：
- `mfe` 最大有利波动 = `max(high_{t1..t5}) / entry_price − 1`（人工最优出场可达的上行）。
- `mae` 最大不利波动 = `min(low_{t1..t5}) / entry_price − 1`（窗口内最大回撤）。
- `best_close` = `max(close_{t1..t5}) / entry_price − 1`。
- **发现命中率** `discovery_hit = mean(mfe >= hit_threshold)`，`--hit-threshold` 默认 `0.05`（5 个交易日内出现过 ≥+5% 的可交易机会即记一次命中）。
- 同口径对 `baseline_universe` 也算 `discovery_hit`，得到 `discovery_hit_vs_baseline`。
- 聚合表新增列：`mfe_avg/mfe_median`、`mae_median`、`discovery_hit`、`discovery_hit_vs_baseline`（top-k 桶也给 `topk_discovery_hit`）。

## 3. Phase G3：edge 终判（门槛**预先写死**，不事后挪动）

跑全区间 + 同票池基线后，按下表机械判定（避免自我安慰）：

| 维度（全区间，对 `baseline_universe`） | 判"有 edge"门槛 |
|---|---|
| top-k 中位数相对基线 `topk_median_r5 − baseline_universe.median` | > 0 |
| 发现命中率相对基线 `topk_discovery_hit − baseline_universe.discovery_hit` | > +3pct |
| 胜率相对基线 | > 0（辅证，不单独成立） |

判定规则：
- **三项主门槛（中位数 + 发现命中率）同时满足 → 该扫描器"有 edge"**，进入后续 Phase F 校准。
- 仅命中率满足、中位数不满足 → 标"**仅发现价值**"（适合人工候选池，不适合机械排序）。
- 都不满足 → 标"**无 edge**"。
- 结论按扫描器分别下，回填实现记录，**不许调参数去凑门槛**。

> 预先写死门槛的意义：edge 判定不能等看到数字再定标准。命中率门槛设 +3pct（相对基线的发现增量）是"扫描器是否比随便从票池里挑更容易撞上机会"的最低可交易增量。

## 4. 实现记录预案（依据 G3 结论的下一步，本轮不执行，仅登记）

- 若某扫描器判"有 edge"：解冻 Phase F，对它做全区间粗网格校准。
- 若判"仅发现价值"：转 dashboard 候选池呈现 + 人工复核动线（原方向选项 B）。
- 若判"无 edge"：回到特征工程，补质性维度（独立性/分离确认时机/概念龙头，原方向选项 C）。
本轮只产出结论 + 推荐分支，不直接执行分支。

---

## 5. 测试（更新 `tests/test_mining_evaluate.py`）

- `compute_baseline_universe`：合成数据断言只统计票池内 code、t+5 与胜率口径正确；与全市场基线在构造数据上差异符合预期。
- `mfe/mae/best_close/discovery_hit`：合成 high/low/close 序列断言数值正确（含无完整 t+5 窗口的边界返回 NaN、不抛错）。
- top-k `discovery_hit` 聚合正确；空区间不抛错。
- 回归：`compute_baseline`（全市场）保持原结果不变。

## 6. 验证命令

```powershell
python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui tests.test_mining_evaluate
python -m py_compile mining/evaluate.py
```
真实数据：
```powershell
python -m mining.evaluate --no-rerun        # 复用已有 candidates，只重算公平口径基线与发现指标
```

验收：
1. 测试全绿、py_compile 通过。
2. 全区间报告含 `baseline_universe`、`mfe/mae`、`discovery_hit`、`discovery_hit_vs_baseline` 及 top-k 版本。
3. 按 §3 写死门槛对 `true_leader`/`trend_embryo` 各下一个明确结论（有 edge / 仅发现价值 / 无 edge）+ 证据，回填实现记录。
4. 全程未改 scanner 算法与默认参数。

---

## 7. 约束

- 纯测量层；不改 scanner 算法、`universe.py`、默认参数、outcomes schema。
- 向量化优先；同票池基线复用已加载 pivot，禁止逐日全市场 SQL。
- 评估只统计 outcome/窗口完整的候选；基线同口径同票池。
- `--no-rerun` 默认可复用现有 candidates（本轮不必重跑扫描器）。
- 编辑前 `git status --short`，勿回退无关改动；不提交 `knowledge/_raw/`。

---

## 8. 实现记录（Codex 填写）

### Phase G1：同票池基线
- 已在 `mining/evaluate.py` 新增 `compute_baseline_universe(conn, start, end, hit_threshold=0.05)`：对区间内每个 entry 日只调用一次 `build_universe(conn, d)` 取得同口径票池，再复用一次性加载的 `close/high/low` pivot 计算 t+5、t+1 胜率和发现指标；未修改 `mining/universe.py`。
- 保留 `compute_baseline` / `compute_baseline_daily_sql` 作为全市场参考与回归防护；报告同时展示 `baseline_market (reference)` 与 `baseline_universe (decision)`，相对基线列默认使用 `baseline_universe`。
- 真实全区间 `2025-11-20 -> 2026-06-17`：`baseline_market n=714875 avg_r5=2.17% median_r5=-0.31% win_rate=33.20%`；`baseline_universe n=682926 avg_r5=2.30% median_r5=-0.28% win_rate=33.29% discovery_hit=36.40%`。

### Phase G2：发现友好前向指标
- 已新增 `compute_forward_window_metrics` 和聚合层前向指标：`mfe=max(high_t1..t5)/entry-1`、`mae=min(low_t1..t5)/entry-1`、`best_close=max(close_t1..t5)/entry-1`、`discovery_hit=(mfe>=--hit-threshold)`，默认 `--hit-threshold=0.05`。
- 未改 `outcomes` schema；候选聚合仍要求 outcome 与 t+1..t+5 价格窗口完整。输出新增 `mfe_avg/mfe_median`、`mae_median`、`discovery_hit`、`discovery_hit_vs_baseline`、`topk_discovery_hit`、`topk_median_r5_vs_baseline`、`topk_win_rate_vs_baseline`、`topk_discovery_hit_vs_baseline`。
- 真实报告已生成：`output/scanner_eval_2025-11-20_2026-06-17.md`，含双基线、mfe/mae、discovery_hit 与 top-k 版本。

### Phase G3：edge 终判
- 已新增固定门槛判定 `edge_verdicts`：`topk_median_r5_vs_baseline > 0`、`topk_discovery_hit_vs_baseline > +3pct`、`topk_win_rate_vs_baseline > 0` 同时满足才判 `has_edge`；若仅发现命中率满足且中位数不满足，判 `discovery_only`；否则 `no_edge`。
- `trend_embryo` 结论：`discovery_only`。证据：`topk_median_r5_vs_baseline=-2.47%` 未过中位数门槛；`topk_discovery_hit_vs_baseline=+18.82%` 过发现门槛；`topk_win_rate_vs_baseline=+3.95%` 为正。推荐分支：候选池呈现 + 人工复核动线，不进入默认参数校准。
- `true_leader` 结论：`discovery_only`。证据：`topk_median_r5_vs_baseline=-3.76%` 未过中位数门槛；`topk_discovery_hit_vs_baseline=+23.89%` 过发现门槛；`topk_win_rate_vs_baseline=-0.40%` 未过辅证。推荐分支：候选池呈现 + 人工复核动线，不进入默认参数校准。
- 本轮未修改任何 scanner 算法、scanner 默认参数或 `mining/universe.py`。

### 测试覆盖
- 新增/更新 `tests/test_mining_evaluate.py` 覆盖：`compute_baseline_universe` 只统计同票池 code 且与全市场基线在构造数据上拉开；`compute_forward_window_metrics` 的 `mfe/mae/best_close/discovery_hit` 与无完整 t+5 窗口 NaN 边界；top-k `discovery_hit` 聚合；固定 edge verdict 门槛；空区间新列；`compute_baseline` 全市场回归保持。
- 已通过：`python -m py_compile mining/evaluate.py`。
- 已通过：`python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui tests.test_mining_evaluate`，共 71 tests OK。
- 已通过真实数据：`python -m mining.evaluate --no-rerun`，复用已有 candidates 生成公平口径报告与 edge verdict。