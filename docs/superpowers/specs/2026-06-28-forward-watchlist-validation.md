# 规格：给漏斗做"前向体检" —— 每天留痕，事后看它说得准不准（Phase J）

状态：待实现（实现交给 Codex）
作者：规划 by Claude
前置：Phase I（漏斗双清单）、Phase K（复盘笔记决策助手）已完成。本轮不改漏斗算法，只在旁边加一层"留痕 + 事后对账"。

---

## 0. 一句话：这个体检在帮你确认什么

漏斗现在每天给你「回踩到位·预备」「再启动·触发」这些判断，**但这些判断准不准，谁都没验证过**——里面的阈值（回撤多深算"到位"、缩量到多少、跌多少算"破位"）全是我拍脑袋设的默认值；`triage` 排序也只是号称"复核优先级"，到底和后续走势有没有关系，没人知道。

这个体检解决的就是这件事，办法是**"前向留痕、事后对账"**：

> - **今天**：漏斗一跑完，系统就把当天每只票的判断（什么状态、triage 多少、当时的缩量/回撤/价格）**原样拍照存下来**。
> - **几天后**：等这些票真实走出来了，系统**回头记录它们实际涨跌**。
> - **攒够样本后**：系统告诉你——"再启动"是不是真的比"回踩到位"更快上涨？triage 排在前面的票是不是真的走得更好？可动作的票是不是真的比"破位失效"的票强？

**为什么这样最诚实**：判断是今天存的、结果是以后才填的，**时间上不可能偷看未来**，所以天生没有"马后炮"。这是在现有数据下唯一能真验证漏斗的办法（历史回测此路不通，原因见下）。

**为什么排在 K 后面**：这是**后台体检**，不改变你每天看盘的体验。先让系统每天真能帮到你（K），再回头确认它靠不靠谱（J）。先解渴，后体检。

---

## 1. 为什么只能前向、不能历史回测（写死防反复）

两个硬阻断（都有证据）：
1. **复盘笔记是方法论散文，不是案例数据库**：母本 D0034 通篇讲逻辑、正文 0 股票代码，专栏复盘篇同样 0 代码，没有可机械抽取的"代码+日期+结果"。
2. **本地没有历史行情**：kline 只覆盖 2025-10~2026-06，笔记是 2017–2023，历史案例没有 K 线可回放。

→ "回测漏斗能否捞到历史票"在数据上根本跑不了，硬做就是马后炮。**前向留痕是唯一诚实可跑的路径。**

---

## 2. 三条纪律（防止体检结果被人为做漂亮）

1. **先写死"什么算好"，再攒数据**（见 §3 的三个问题），事后**不许回头改阈值/分桶**去凑好看的结论。
2. **样本不够就老实说"样本不足"**，不提前下"有效/无效"的结论。
3. **样本量如实报告**，区分"已经走完、可对账"的和"还没走完"的。

---

## 3. 攒够数据后，要回答的三个问题（预先写死，冻结）

1. **"再启动"是不是比"回踩到位"更值得动手？** 带放量触发的"再启动"，后续 5 日内冲高比例、收益中位数，是否高于只是"回踩到位"的？（假设：放量触发 → 跟随更快。）
2. **triage 排序到底有没有用？** 同一状态里，triage 排前 1/4 的票，后续收益是否真的高于排后 1/4 的？（现在只敢说它是"复核优先级"，这一问检验它是否**碰巧**真和走势相关——无论结果好坏都如实记，不回头调权重。）
3. **漏斗的状态切分有没有区分度？** 可动作状态（回踩到位/再启动）后续冲高比例，是否既高于全市场同类票平均、又高于"破位失效"的票？（验证状态机不是白分的。）

每个状态攒够约 150 个"已走完"样本前，一律标"样本不足"，不出结论。

---

## 4. 给 Codex 的实现细节（与上面分开，按需读）

**J1 每日留痕 —— 扩 `mining/watchlist.py` + 新表 `watchlist_snapshots`（建在 `mining_mvp.db`）**

| 列 | 含义 |
|---|---|
| snapshot_date | 决策日（= build_watchlist 的 trade_date）|
| sec_code / sec_name | 标的 |
| state / triage | 漏斗状态 / 分诊分 |
| entry_price | **决策日收盘价**（前向收益基准）|
| run_up_pct / shrink_ratio / ma_proximity / pullback_pct | 决策上下文快照 |
| reclaim_ma10 / vol_expand_up | 触发上下文（0/1）|
| flag_count / days_since_flag / flag_strategies | 来源上下文 |
| created_at | 写入时间 |

- 主键 `(snapshot_date, sec_code, state)`，重跑同日用 `INSERT OR REPLACE` 幂等。
- 新函数 `persist_watchlist_snapshot(conn, trade_date, params=None) -> int`：调 `build_watchlist(conn, trade_date, params=params)`，**持久化全部五状态**（不只可动作两栏——为验证"可动作状态是否真优于破位失效"留全样本），只存上表精简列，返回写入行数；缺收盘的行跳过并计数。

**J2 事后对账 —— 扩 `mining/backtest.py`，复用现有前向收益逻辑**

- 现有 `backfill_outcomes` 里算 t1/t2/t5 收益、判 `status`（complete/partial/halted/delisted）的那段，**抽成共享助手** `_forward_bars(conn, sec_code, trade_date)`（或等价），`backfill_outcomes` 与新函数共用，**不复制粘贴**。
- 新函数 `backfill_snapshot_outcomes(conn, ...)` + 新表 `watchlist_outcomes`，主键 `(snapshot_date, sec_code, state)`，列 `r1..r5, is_win, backfilled_at, status`，口径与现有 `outcomes` **完全一致**（r1=open_t1…r5=close_t5；is_win=(r2>0.02 and r3>−0.03)）。
- 只对账**已走完**的快照（status=complete，即 t1/t2/t5 齐全）；未走完留 partial、下次再补。
- mfe/mae/冲高命中率**不落库**，与 `evaluate.py` 一致在读出时算（避免口径分叉）。

**J3 接入日更 + 读出 —— `run_daily.py` + `mining/evaluate.py`**

- `run_daily.py`：在 `execute_daily_pipeline` 既有 `backfill_outcomes` 之后，追加 `persist_watchlist_snapshot(conn, resolved_trade_date)` 与 `backfill_snapshot_outcomes(conn)`；**包 try、失败只记日志不阻断主流程**（留痕是旁路）。
- `mining/evaluate.py`：新增 `evaluate_watchlist_snapshots(conn, ...) -> DataFrame` + CLI `--snapshots`：
  - 只统计**已走完**快照；**复用 `compute_baseline_universe`** 作同类票基线。
  - 按 `state` 分组、并在每个 state 内按 `triage` 四分位分桶，输出 n、胜率、收益中位数、冲高命中率(mfe≥5%)、及各项 vs 基线的差。
  - **最小样本门槛 `min_sample`（默认150）**：某 state 已走完样本不足 → 该 state 判 `insufficient_sample`，不出结论。
  - 报告写 `output/`，表头注明"前向样本外、累积中"。

**测试**
- 留痕幂等：同日重跑不增行；entry_price=决策日收盘；五状态都进表；缺收盘行被跳过计数。
- 对账口径平价：合成 fixture 上 `backfill_snapshot_outcomes` 的 r1..r5/is_win 与 `backfill_outcomes` 同票同日逐值一致。
- 不偷看未来：outcome 只用 snapshot_date **之后**的 bar；未走完快照标 partial、不误填。
- 读出：按 state/triage 分桶聚合正确；样本 < min_sample 返回 `insufficient_sample`；空快照表安全返回。
- 现有 85 测试保持全绿。

**验证命令**
```powershell
python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui tests.test_mining_evaluate
python -m py_compile mining/watchlist.py mining/backtest.py mining/evaluate.py run_daily.py
```
冒烟（首日预期"样本不足"，因还没有走完的样本）：
```powershell
python run_daily.py --trade-date 2026-06-25
python -c "from mining.db import connect; from mining.evaluate import evaluate_watchlist_snapshots; print(evaluate_watchlist_snapshots(connect(base_dir='.')))"
```

**约束**
- 不改 scanner 算法/默认参数/`universe.py`/漏斗状态机逻辑。
- 复用 `_forward_bars`/前向收益与 `compute_baseline_universe`，不复制。
- 比较口径预注册、冻结，样本不足判 `insufficient_sample`，不事后回调。
- 向量化优先；旁路失败不阻断日更主流程。
- 编辑前 `git status --short`，勿回退无关改动；不提交 `knowledge/_raw/`。

---

## 5. 交付顺序
```
J1 watchlist.py persist_watchlist_snapshot + 建表 + 测试
J2 backtest.py 抽 _forward_bars 共享助手 + backfill_snapshot_outcomes + 测试
J3 run_daily 接入 + evaluate.py --snapshots 读出 + 测试
```

## 6. 实现记录（Codex 填写）

完成时间：2026-06-29

### J1 每日留痕
- 新增表 `watchlist_snapshots`，建在 `mining_mvp.db`，主键为 `(snapshot_date, sec_code, state)`。
- 新增函数 `persist_watchlist_snapshot(conn, trade_date, params=None) -> int`，调用 `build_watchlist(conn, trade_date, params=params)` 后持久化完整 watchlist 漏斗样本，不只保存双清单中的可动作状态。
- 入库字段包含：`snapshot_date`、`sec_code`、`sec_name`、`state`、`triage`、`entry_price`、`run_up_pct`、`shrink_ratio`、`ma_proximity`、`pullback_pct`、`reclaim_ma10`、`vol_expand_up`、`flag_count`、`days_since_flag`、`flag_strategies`、`created_at`。
- `entry_price` 使用决策日 `build_watchlist` 输出的 `close`，即决策日收盘价；无有效收盘价的行跳过。
- `INSERT OR REPLACE` 保证同日重复运行幂等。

### J2 事后对账
- `mining/backtest.py` 抽出共享 `_forward_bars(conn, sec_code, trade_date)` 和 `_forward_outcome_values(entry_price, bars)`。
- 旧 `backfill_outcomes` 与新 `backfill_snapshot_outcomes(conn, snapshot_date=None, force=False)` 共用同一套 t+1/t+2/t+5 前向口径。
- 新增表 `watchlist_outcomes`，主键为 `(snapshot_date, sec_code, state)`，字段包含 `open_t1/high_t1/low_t1/close_t1/close_t2/close_t5`、`r1..r5`、`is_win`、`backfilled_at`、`status`。
- `is_win` 口径与旧 outcomes 一致：`r2 > 0.02 and r3 > -0.03`。
- 未走完 t+5 的快照写入 `partial`，下次可继续补；evaluate 只统计 `status='complete'` 的样本。

### J3 日更接入与读出
- `run_daily.py` 在原 `backfill_outcomes` 后旁路执行 `persist_watchlist_snapshot(conn, resolved_trade_date)` 与 `backfill_snapshot_outcomes(conn)`。
- 旁路用 `try` 包裹，失败只写入返回值 `watchlist_validation.error`，不阻断主日更流程。
- `mining/evaluate.py` 新增 `evaluate_watchlist_snapshots(conn, start=None, end=None, min_sample=50, hit_threshold=DEFAULT_HIT_THRESHOLD) -> DataFrame`。
- 新增 CLI：`python -m mining.evaluate --snapshots ...`，并新增 `--min-sample`。
- 新增报告函数 `write_watchlist_snapshot_report(...)`，输出 `output/watchlist_snapshot_eval_<start>_<end>.md`。
- 读出按 `state` 输出 `all/top_q1/middle/bottom_q4`，统计 `n_snapshots`、`n_evaluated`、`win_rate`、`avg_r5`、`median_r5`、`mfe_median`、`mae_median`、`discovery_hit` 及相对 `compute_baseline_universe` 的差值。
- 样本不足时标记 `insufficient_sample`，不调整任何漏斗阈值或 scanner 默认参数。

### 测试与冒烟
- `python -m py_compile mining/watchlist.py mining/backtest.py mining/evaluate.py run_daily.py`：通过。
- `python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui tests.test_mining_evaluate`：90 tests OK。
- 合成 SQLite 冒烟：`execute_daily_pipeline(... refresh=False, emit_reports=False)` 后写入 `watchlist_snapshots=1`、`watchlist_outcomes=1`；`evaluate_watchlist_snapshots(...)` 返回 2 行分组结果，`sample_status=['insufficient_sample']`。

### 约束确认
- 未修改 scanner 算法、默认参数、`mining/universe.py`、watchlist 状态机逻辑。
- 前向结果只使用 `snapshot_date` 之后的 bar；未完成样本保留为 `partial`。
- 本轮只新增测量层留痕与读出，不对任何阈值做事后校准。