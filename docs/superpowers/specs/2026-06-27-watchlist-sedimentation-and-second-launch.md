# 选股系统下一步规格：候选池沉淀 + 二次启动扫描器

状态：✅ 已完成（Codex 实现，78 测试通过；second_launch 终判 no_edge）
作者：规划 by Claude
前置：`2026-06-27-measurement-fairness-and-edge-verdict.md`（Phase G）已由 Codex 完成，71 测试通过。终判：`trend_embryo`/`true_leader` 均为 `discovery_only`（机械持有无 t+5 edge，但发现命中率显著高于同票池基线，且发现价值与高波动绑定，需人工择时兑现）。
触发：按 Phase G §4 预先登记分支——`discovery_only` → 转候选池呈现 + 人工复核动线。本轮把"一次性发现"沉淀成可复用观察清单，并落地 D0034「二次启动」低吸 setup。

---

## 0. 背景与目标

**为什么是这步（接终判）**：两个扫描器是"发现工具"不是"机械 alpha"——价值在于把强势/真龙票**捞进视野**，由人工择时。但目前每天的候选是**孤立快照**，强势名单跨日就散了。沉淀 = 把历史强势标记聚成一张滚动观察清单；二次启动 = 在清单里捕捉「强趋势票缩量回踩均线止跌 → 低吸候选」（D0034 原话），这是比追突破**胜率更高、不追高**的入场点。

**本轮目标**：
1. **候选池沉淀**：可查询的滚动观察清单（近 N 日被 `trend_embryo`/`true_leader` 标记过的强势名单 + 其后的回踩状态）。
2. **二次启动扫描器**：在沉淀清单上识别「缩量回踩均线止跌」低吸候选。
3. **同口径评估 + 面板呈现**：用现成 `evaluate.py` 对新扫描器下**同样写死门槛**的诚实结论；面板登记新扫描器。

**关键纪律（沿用上轮）**：新扫描器**不豁免评估**。低吸 setup 理论上中位数应优于追突破，但**仍按 Phase G 写死门槛判定**，不预设它有 edge。校准继续冻结。

**边界**：不改 `trend_embryo`/`true_leader`/`momentum`/`rps`/`universe.py` 算法与默认参数；不依赖实时行情/龙虎榜；纯本地 DB。

---

## 1. Phase H1：候选池沉淀（`mining/watchlist.py`，新建）

**定位**：纯读层，聚合已持久化的 `candidates` + `ash.kline_daily`，不写新表。

- `build_watchlist(conn, trade_date, lookback=40, source_strategies=("trend_embryo","true_leader"), ...) -> pd.DataFrame`：
  - 取近 `lookback` 交易日内、被 `source_strategies` 标记过的去重 `sec_code`，记录每只的 `first_flag_date`/`last_flag_date`/`flag_count`/`flag_strategies`。
  - 对每只票用 `kline_daily` 计算回踩状态字段：
    - `peak_close_since_flag`（首次标记后的最高收盘）、`peak_date`。
    - `pullback_pct = close_today / peak_close_since_flag − 1`（当前相对峰值回撤，≤0）。
    - `ma10`/`ma20`/`ma_proximity`（`min(|close/ma−1|)`，是否贴近某条均线）。
    - `days_since_flag`、`days_since_peak`。
  - 返回按 `last_flag_date desc, pullback_pct asc` 排序，供面板与二次启动扫描器共用。
- 复用一次性向量化加载，禁止逐票循环 SQL。

## 2. Phase H2：二次启动扫描器（`mining/scanners/second_launch.py`，新建）

**input universe = H1 沉淀清单**（不是 `build_universe` 全市场——只在"曾强势"的票里找低吸点）。

- 新增 `mining/features.py::moving_average(series, window)`（简单均线，复用）。
- 硬门槛（缩量回踩止跌，参数占位、待后续校准）：
  1. **曾强势**：在清单内（近 `lookback` 被标记过）。
  2. **回踩到位**：`pullback_pct <= -pullback_min`（默认 `pullback_min=0.08`，自峰值回撤≥8%）**且** `ma_proximity <= ma_tol`（默认 `ma_tol=0.03`，贴近 MA10/MA20 三个点内）。
  3. **缩量**：回踩段量能收缩——`vol_recent_avg / vol_run_avg <= shrink_ratio`（默认 `shrink_ratio=0.7`；`vol_recent`=近 `vol_recent_n=5` 日均量，`vol_run`=标记日附近 `vol_run_n=5` 日均量）。
  4. **止跌**：近 `stop_n=3` 日不创新低 **或** 当日收回 MA10 上方（`close >= ma10`）。
- 打分（占位，本轮**不**承诺机械 edge）：`score = w_stop*止跌 + w_ma*贴均线度 + w_shrink*缩量度`，仅作可读排序；features 写全（`pullback_pct/ma_proximity/shrink_ratio/days_since_flag/stop_signal/flag_strategies`）。
- `@register`，`strategy_id="second_launch"`，`version="v1.0"`，`kind="stock"`，`top_n=20`。注册后自动进 `run_daily.py` 与 `evaluate.py`。

## 3. Phase H3：面板登记 + 同口径评估

### 面板（`mining/streamlit_tabs/tab_scanner.py`）
- 4 处加 `second_launch`：白名单 SQL（`_persisted_candidate_dates` ~L345、`_load_persisted_candidates` ~L358），中文标签两处（~L662/L701）映射 `"second_launch": "二次启动低吸"`。
- 沉淀清单呈现：新增一个轻量视图/区块展示 `build_watchlist()` 结果（曾强势 + 当前回踩状态），让用户能看"哪些强势票正在回踩等二次启动"。实现从简，复用现有 tab 渲染风格。

### 评估（沿用 `evaluate.py`，不改其算法）
- 跑 `python -m mining.evaluate`（含历史复跑 `second_launch`），对它产出**同样写死门槛**的 `edge_verdict`。
- 诚实记录结论：`has_edge` / `discovery_only` / `no_edge` + 证据（topk 中位数、发现命中率、胜率相对同票池基线）。
- 若 `second_launch` 判 `has_edge` → 登记"可解冻 Phase F 校准它"；否则同样转候选池/人工动线，不调参凑门槛。

---

## 4. 交付顺序

```
H1 watchlist.py 沉淀（读层）+ 测试
H2 second_launch 扫描器 + features.moving_average + 测试
H3 面板登记 + 沉淀清单视图 + 全区间评估 → second_launch edge 结论回填
```

---

## 5. 测试

- `tests/test_mining_*`（新增/更新）：
  - `build_watchlist`：合成 candidates + kline，断言去重名单、`peak/pullback_pct/ma_proximity/days_since_flag` 计算正确；空清单不抛错。
  - `second_launch`：构造"标记后回踩贴 MA + 缩量 + 止跌"的票应入选；不满足任一硬门槛应剔除；缺历史窗口边界返回空、不抛错。
  - `moving_average`：数值正确、窗口不足返回 NaN。
- 现有 71 测试保持全绿。

## 6. 验证命令

```powershell
python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui tests.test_mining_evaluate
python -m py_compile mining/watchlist.py mining/scanners/second_launch.py mining/evaluate.py mining/streamlit_tabs/tab_scanner.py
```
真实数据：
```powershell
python run_daily.py                 # 确认 second_launch 进日更、出候选
python -m mining.evaluate           # 全区间评估含 second_launch + edge verdict
```

验收：
1. 测试全绿、py_compile 通过。
2. `build_watchlist` 在真实数据上产出非空滚动清单（近 40 日强势 + 回踩状态）。
3. `second_launch` 进 run_daily/evaluate，面板可见「二次启动低吸」与沉淀清单视图。
4. `evaluate.py` 对 `second_launch` 给出写死门槛的 edge 结论 + 证据，回填实现记录。
5. 未改其他 scanner 算法与默认参数。

---

## 7. 约束

- 新扫描器不豁免评估，按 Phase G 写死门槛判定，不预设有 edge、不调参凑门槛。
- `watchlist.py` 纯读层，不新增表；向量化优先，禁止逐票循环 SQL。
- 不改 `trend_embryo`/`true_leader`/`momentum`/`rps`/`universe.py` 算法与默认参数。
- 编辑前 `git status --short`，勿回退无关改动；不提交 `knowledge/_raw/`。

---

## 8. 实现记录（Codex 填写）

### Phase H1：watchlist.py 沉淀
- 已新增 `mining/watchlist.py::build_watchlist(conn, trade_date, lookback=40, source_strategies=("trend_embryo","true_leader"))`，纯读 `candidates` 与 `ash.kline_daily`，不新增表、不写入状态。
- 字段口径：按近 `lookback` 个交易日内源策略标记去重，输出 `first_flag_date/last_flag_date/flag_count/flag_strategies`；从首次标记日至 `trade_date` 计算 `peak_close_since_flag/peak_date/pullback_pct`；用一次性加载的收盘 pivot 计算 `ma10/ma20/ma_proximity`；`days_since_flag` 使用最近标记日至当前日的交易日差，`days_since_peak` 使用峰值日至当前日的交易日差。
- 性能：候选标记一次 SQL、行情一次 SQL；后续用 pivot/rolling 和内存分组计算，未逐票循环 SQL。
- 真实数据验证：`build_watchlist(conn, "2026-06-25")` 产出 908 条滚动观察清单。

### Phase H2：second_launch 扫描器
- 已新增 `mining/scanners/second_launch.py`，`strategy_id="second_launch"`、`version="v1.0"`、`kind="stock"`、`top_n=20`，并在 `mining/scanners/__init__.py` 注册为 builtin scanner，因此自动进入 `run_daily.py` 与 `evaluate.py`。
- 已新增 `mining/features.py::moving_average(series, window)`，窗口不足保持 NaN；用于 watchlist 的 MA10/MA20。
- 硬门槛实现：输入只来自 H1 watchlist；`pullback_pct <= -pullback_min`（默认 8% 回撤）、`ma_proximity <= ma_tol`（默认 3% 贴 MA10/MA20）、`vol_recent_avg / vol_run_avg <= shrink_ratio`（默认 0.7；近 5 日量对首次标记日前后 5 日量）、近 `stop_n=3` 日不创新低或收回 MA10 上方。
- 打分仅作排序：`w_stop*止跌 + w_ma*贴均线度 + w_shrink*缩量度`；features 写入 `pullback_pct/ma_proximity/shrink_ratio/days_since_flag/stop_signal/flag_strategies` 等可解释字段。本轮未调任何既有扫描器算法或默认参数。

### Phase H3：面板登记 + 评估
- 已在 `mining/streamlit_tabs/tab_scanner.py` 持久化候选白名单两处加入 `second_launch`，中文映射两处加入 `"second_launch": "二次启动低吸"`。
- 面板新增沉淀清单视图：`load_watchlist_snapshot` + `_prepare_watchlist_display`，在挖掘面板展示近 40 日强势沉淀观察清单及回撤/贴均线/距标记日状态。
- 日更验证：`python run_daily.py --date 2026-06-25` 成功，`second_launch` 通过通用分支运行，状态 `ok`，候选数 20。
- 全区间评估：`python -m mining.evaluate` 完成 `2025-11-20 -> 2026-06-17` 138 日，历史补跑 `second_launch` 后候选数 2484、可评估 2484；报告 `output/scanner_eval_2025-11-20_2026-06-17.md` 已包含 `second_launch` verdict。
- `second_launch` edge 结论：`no_edge`。写死门槛证据：`topk_median_r5_vs_baseline=-0.58%`，`topk_discovery_hit_vs_baseline=-2.15%`，`topk_win_rate_vs_baseline=-4.35%`，三项均未过门槛；后续不进入参数校准，仍作为候选池/人工复核动线或回到特征工程。

### 测试覆盖
- 新增/更新测试覆盖：`moving_average` 窗口与 NaN；`build_watchlist` 去重、峰值、回撤、MA 贴近、交易日差、空清单；`second_launch` 入选路径、缩量硬门槛剔除、缺历史边界；`run_daily` 通用分支持久化 `second_launch`；UI persisted loader 与中文标签。
- 已通过：`python -m py_compile mining/watchlist.py mining/scanners/second_launch.py mining/evaluate.py mining/streamlit_tabs/tab_scanner.py`。
- 已通过：`python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui tests.test_mining_evaluate`，共 78 tests OK。
- 已通过真实 harness：`python run_daily.py --date 2026-06-25`；`python -m mining.evaluate`；`python -m mining.evaluate --no-rerun` 复核报告包含 `second_launch no_edge`。