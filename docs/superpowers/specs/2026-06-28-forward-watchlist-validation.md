# 选股系统下一步规格：前向漏斗留痕 + 兑现追踪（Phase J）

状态：待实现（实现交给 Codex）
作者：规划 by Claude
前置：`2026-06-28-watchlist-actionable-funnel.md`（Phase I）已完成，83 测试通过。漏斗已出 `state`（延伸中/回踩中/回踩到位/再启动/破位失效）+ `triage`。
触发：用户选定方向「2a 前向漏斗留痕+兑现追踪」。

---

## 0. 第一性原理：为什么是前向，不是回测

**已排除的死路（带证据，写死防反复）**：
- 笔记是**方法论散文，非案例库**：母本 D0034 通篇讲逻辑（5日涨>13%/涨停≤1/同花顺指标），正文 0 股票代码；专栏复盘篇同样 0 代码。无可机械抽取的「代码+日期+结果」。
- **本地无历史行情**：kline 仅覆盖 2025-10-23~2026-06-25；笔记是 2017–2023。历史案例即便抽出也无 K 线回放。
- → "回测漏斗能否捞到哥点过的历史票"**数据上不可跑**，硬做即马后炮。

**本轮要解决的真问题**：漏斗的 `state`/`triage` 现在是**未经检验的假设**——阈值（broken_pullback=−0.30、ready 带 [−0.25,−0.08]、shrink_max=0.7 等）全是拍脑袋默认值，`triage` 仅声称"复核优先级"、未验证是否真和走势相关。

**唯一诚实且可跑的验证 = 前向、样本外**：从今天起每日把漏斗的判断**在决策时刻快照**（含当时的 state/triage/特征/进场参考价），未来交易日到位后**再**回填真实走势。因为标签时刻早于结果时刻，**结构上不可能前视**，天然零马后炮。

**纪律护栏（沿用 Phase G edge 终判同款）**：
- 比较口径**预先写死**（见 §4），样本未达门槛前一律判 `insufficient_sample`，不提前下结论。
- 阈值/口径**不得事后回调**去凑好看。
- 样本量如实报告，区分"已成熟/未成熟"快照。

**边界**：不改任何 scanner 算法、默认参数、`universe.py`、`watchlist.py` 的状态机逻辑。本轮只**加一层留痕+兑现+读出**，复用既有前向收益与同票池基线，不造重复轮子。

---

## 1. Phase J1：每日快照落盘（扩 `mining/watchlist.py` + 新表）

新表 `watchlist_snapshots`（建在 `mining_mvp.db`，幂等）：

| 列 | 含义 |
|---|---|
| snapshot_date TEXT | 决策日（= build_watchlist 的 trade_date）|
| sec_code / sec_name TEXT | 标的 |
| state TEXT | 五状态之一 |
| triage REAL | 分诊分 |
| entry_price REAL | **决策日收盘价**（前向收益基准）|
| run_up_pct / shrink_ratio / ma_proximity / pullback_pct REAL | 决策上下文快照 |
| reclaim_ma10 / vol_expand_up INTEGER | 触发上下文 |
| flag_count / days_since_flag INTEGER, flag_strategies TEXT | 来源上下文 |
| created_at TEXT | 写入时间 |

主键 `(snapshot_date, sec_code, state)`，重跑同日用 `INSERT OR REPLACE` 幂等。

新函数 `persist_watchlist_snapshot(conn, trade_date, params=None) -> int`：
- 调 `build_watchlist(conn, trade_date, params=params)`，**持久化全部五状态**（不只可动作两栏）——为验证"状态机是否单调"（可动作状态是否真优于破位失效）留全样本。
- 只存上表精简决策列，返回写入行数。
- `entry_price` 取决策日该票收盘；缺收盘的行跳过并计数。

## 2. Phase J2：兑现回填（扩 `mining/backtest.py`，复用前向收益）

新函数 `backfill_snapshot_outcomes(conn, ...)`，**复用** `_compute_return` 与 t1/t2/t5 取 bar 逻辑（如有重复，抽 `_forward_bars(conn, sec_code, trade_date)` 共享助手，`backfill_outcomes` 与本函数共用，不复制粘贴）：
- 新表 `watchlist_outcomes`，主键 `(snapshot_date, sec_code, state)`，列 `r1..r5, is_win, backfilled_at, status`，口径与现有 `outcomes` 完全一致（r1=open_t1…r5=close_t5；is_win=(r2>0.02 and r3>−0.03)）。
- 只回填**已成熟**快照（snapshot_date 后已有 ≥5 个交易日 bar）；未成熟标 `status='pending'`，下次再补。
- mfe/mae/discovery_hit **不落库**，与 evaluate.py 一致在读出时算（避免口径分叉）。

## 3. Phase J3：接入日更 + 读出报告

**接入 `run_daily.py`**（在 `execute_daily_pipeline` 既有 `backfill_outcomes` 之后）：
- 追加 `persist_watchlist_snapshot(conn, resolved_trade_date)` 与 `backfill_snapshot_outcomes(conn)`。
- 失败不阻断主流程（漏斗留痕是旁路，包 try 记日志）。

**读出 `mining/evaluate.py`** 新增 `evaluate_watchlist_snapshots(conn, ...) -> DataFrame` + CLI `--snapshots`：
- 只统计**已成熟**快照（有 outcome）。
- **复用 `compute_baseline_universe`** 作同票池基线。
- 按 `state` 分组、并在每个 state 内按 `triage` 四分位分桶，输出：n、win_rate、median_r5、mfe_median、mae_median、discovery_hit，及各项 vs baseline_universe 的差。
- **最小样本门槛**：每 state 已成熟样本 < `min_sample(默认150)` → 该 state 判 `insufficient_sample`，不出"有效/无效"结论。
- 报告写 `output/`，文件名含区间；表头注明"前向样本外、当前为累积中"。

## 4. 预先写死的校准问题（冻结，达样本量前不下结论）

1. **再启动 vs 回踩到位**：再启动（带放量触发）的 t+5 discovery_hit 与 median_r5 是否高于回踩到位？（假设：放量触发→更近的跟随。）
2. **triage 是否真有信息**：各 state 内 triage 顶 1/4 桶的 median_r5 是否高于底 1/4 桶？（当前只声称"复核排序"，本问检验它是否**意外地**与走势相关；无论结果都如实记，不回调权重。）
3. **状态机是否单调**：可动作状态（回踩到位/再启动）的 discovery_hit 是否高于 baseline_universe，且高于破位失效？（验证状态切分有无区分度。）

达 `min_sample` 前全判 `insufficient_sample`。结论出来后**不得事后改阈值/分桶**去凑。

---

## 5. 交付顺序

```
J1 watchlist.py persist_watchlist_snapshot + 建表 + 测试
J2 backtest.py backfill_snapshot_outcomes + 共享前向取 bar 助手 + 测试
J3 run_daily 接入 + evaluate.py --snapshots 读出 + 预注册问题 + 测试
```

## 6. 测试

- 落盘幂等：同日重跑不增行；entry_price=决策日收盘；全五状态都进表；缺收盘行被跳过计数。
- 兑现口径平价：在合成 fixture 上 `backfill_snapshot_outcomes` 的 r1..r5/is_win 与 `backfill_outcomes` 同票同日逐值一致。
- 不前视：outcome 只用 snapshot_date **之后**的 bar；未成熟快照标 pending、不误填。
- 读出：按 state/triage 分桶聚合正确；样本 < min_sample 返回 `insufficient_sample`；空快照表安全返回。
- 现有 83 测试保持全绿。

## 7. 验证命令

```powershell
python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui tests.test_mining_evaluate
python -m py_compile mining/watchlist.py mining/backtest.py mining/evaluate.py run_daily.py
```
冒烟（首日预期 insufficient_sample，因无成熟样本）：
```powershell
python run_daily.py --trade-date 2026-06-25
python -c "from mining.db import connect; from mining.evaluate import evaluate_watchlist_snapshots; print(evaluate_watchlist_snapshots(connect(base_dir='.')))"
```

## 8. 约束

- 不改 scanner 算法/默认参数/`universe.py`/状态机逻辑。
- 复用 `_compute_return`/前向取 bar 与 `compute_baseline_universe`，不复制。
- 比较口径预注册、冻结，样本不足判 `insufficient_sample`，不事后回调。
- 向量化优先；旁路失败不阻断日更主流程。
- 编辑前 `git status --short`，勿回退无关改动；不提交 `knowledge/_raw/`。

---

## 9. 实现记录（Codex 填写）

（待 Codex 实现后回填：建表/函数签名、测试数、首日冒烟输出、样本累积说明。）
