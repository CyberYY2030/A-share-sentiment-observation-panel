# 任务卡：launch_burst 审查后续（排序修复 / 前向报告分组 / 盘中扫描）

日期：2026-07-17
来源：Phase M 对抗式审查裁决（红-1 排序反向、黄-1 入场口径、黄-2 无市场状态）。用户已批准前两项开卡，并新增盘中筛选需求（使用当日快照数据）。
派发顺序：**串行 M.1 → M.2 → N**，一卡一 commit，后卡以前卡 commit 为 parent。三卡文件域互斥，禁止并行改同一文件。

**全局约束（每张卡都适用）**
- 编辑前 `git status --short`；工作树中 `daily_job.ps1`、`mining/notify.py`、`tests/test_notify.py`、`offline_daily_update.py`、`runtime_paths.py`、`.gitignore`、`AGENTS.md`、`CLAUDE.md`、`backfill_*.py`、`repair_market_day_akshare.py`、`run_daily.py`(未提交改动部分)、`mining/backtest.py`(未提交改动部分)、`tests/test_backfill_rules.py`、`tests/test_mining_pipeline.py`(未提交改动部分) 属于另一条 pipeline-reliability 工作流，**不得回退、不得混入本卡 commit**。交付后逐 commit 检查 diff 纯度。
- 禁止提交 `knowledge/_raw/`、`*.db`、行情缓存、`output/` 产物。
- 计算涨幅一律 `close/pre_close-1`，`pre_close` 无效(NULL/≤0)时回退同股票前一交易日 `close`；**禁用 `change_pct` 列**（库中大量 NULL，已实证）。
- Lessons 投递（抄自 `~/.claude/LESSONS.md`，卡即接口）：
  - **L-5**：选股/形态类规则的验收标准是"命中用户标注的图形样本"，不是"数据门槛通过"。触发：本组全部三卡。样本集 = `tests/fixtures/launch_samples/` fixture + 2026-06-18 实跑必含 002821。
  - **L-1**：改共享定义/排序规则必须全仓搜旧字面量本身（注释、测试名、spec 文档都是消费者）。本仓库无 `scripts/check_stale_literal.py`，用等价 grep 命令（见 M.1 验收）。
  - **L-4**：新增可再生产物目录必须已有忽略规则。卡 N 的盘中报告写入 `output/intraday/`，确认 `.gitignore` 已覆盖 `output/`（已在另一工作流增补，勿重复改 `.gitignore`）。

---

## 卡 M.1 — launch_burst 日内截断排序反向（红-1）

### 仍存在的问题

`launch_burst` 的日内截断按 `score=(pct/σ20)×量比` 降序取 top-10。日内配对检验（只比同一天内部，剔除市况混淆）显示该排序是**反向指标**：cap 生效的 21 天里，被保留的 top-10 信号 t+10（次日开盘入）均值 +1.06%/中位 -0.19%，被截掉的低分票 +3.92%/+2.20%。机制：单日打满 3σ 以上且巨量的爆发多为情绪透支一日游；温和过线的启动更有延续性。2026-06-18 实跑中后来 +34% 的 002821 恰好排最后一名。

### 根因

- `mining/scanners/launch_burst.py:158` — `"score": sigma_multiple * volume_ratio`；
- `mining/scanners/launch_burst.py:210-214` — `sort_values(["score", "sec_code"], ascending=[False, True]).head(daily_cap)`。

### 改法

排序键改为：**`sigma_multiple` 升序（温和启动优先），tie-break `cluster_days` 降序（横盘越久越好），再 `sec_code` 升序**。即：

```python
selected = (
    pd.DataFrame(rows)
    .sort_values(["sigma_multiple", "cluster_days", "sec_code"], ascending=[True, False, True])
    .head(int(params["daily_cap"]))
)
```

依据（主审已在本地全市场 2025-10~2026-07 数据上验证，locally-verified）：cap 生效日 top-10 的 t+10（次日开盘入）——

| 排序方案 | t+5 均值 | t+10 均值 | t+10 中位 | win | >10% |
| --- | --- | --- | --- | --- | --- |
| A 现行 score 降序 | +0.62% | +1.06% | -0.19% | 49% | 16% |
| B cluster_days 降序 | +1.00% | +1.47% | +0.07% | 50% | 19% |
| C 缩量比升序 | +1.13% | +1.15% | -0.35% | 48% | 18% |
| **D σ倍数升序（采用，tie-break B）** | **+1.42%** | **+1.78%** | **+0.48%** | **52%** | **20%** |

- `features` 字段全部保留（含 `score`，作为展示/复盘特征，不再决定排序）；`rank` 按新顺序编号。
- 同步更新 spec `docs/superpowers/specs/2026-07-17-main-wave-launch-scanner.md` 第 4 节 M1 的排序描述（原文"按 (pct_t/sigma20)×量比 排序取 top"），并在其实现记录追加一行本次变更。
- 更新 `tests/test_launch_burst.py:204` `test_index_rows_are_excluded_and_daily_cap_keeps_highest_scores`：改名并改断言为"cap 保留 σ倍数最低的候选、同值时 cluster_days 长者优先"。

### 不改什么

八项入选条件、全部阈值、`daily_cap=10`、`features_json` 字段集、fixture、面板列、watchlist 接入——全部不动。本卡只改排序与其消费者（测试名/断言、spec 文本）。

### 验收（最低完成标签：locally-verified）

```powershell
# 1. 旧字面量清扫（L-1 等价检查，应只剩本卡文件命中）
git grep -n "keeps_highest_scores"
git grep -n "sigma20)×量比" -- docs
# 2. 单测
python -m unittest tests.test_launch_burst tests.test_mining_pipeline tests.test_mining_ui
# 3. 真实数据回放（L-5：002821 必须入选且排名第 1）
python -c "from mining.db import connect; from mining.scanners.launch_burst import LaunchBurstScanner; c=connect(); s=LaunchBurstScanner(); [print(x.rank, x.sec_code, x.features['sigma_multiple'], x.features['cluster_days']) for x in s.run(c,'2026-06-18')]"
```

断言：`2026-06-18` 输出 4 只，顺序为 `002821, 688306, 603358, 688131`（σ倍数 3.02/3.34/3.55/4.73 升序）。

---

## 卡 M.2 — 前向验证报告：次日开盘口径 + 市场状态分组（黄-1 / 黄-2）

### 仍存在的问题

1. 报告统计口径偏乐观：`mining/reports.py:73-104` `get_review_summary` 只汇总 `r1/r2/r3`（信号日收盘入场对 t+1 开/高/低），没有"次日开盘价入场、持有到 t+2/t+5"的真实可执行口径——而 run_daily 收盘后才跑，用户实际只能拿到 t+1 开盘价。
2. 无市场状态维度：弱市日信号 t+10 均值 -2.31%（中位 -3.91%、win 33%），强市日 +1.62%/win 50%（本地全市场验证，locally-verified），但日报与累计统计完全不区分市况。面板情绪灯是 Streamlit 内嵌逻辑，root 下 `market_light_api.py` 是带 `...` 占位符的坏 stub（缺 `import time`），均不可作为离线数据源。
3. 日报 `generate_markdown_report`（`mining/reports.py:107`）没有 launch_burst 区块，用户每天最关心的清单不在日报里。

### 根因

- `mining/reports.py:73-104`：SQL 只聚合 `o.r1/r2/r3`；`outcomes` 表其实已存 `open_t1/close_t2/close_t5`（`mining/backtest.py:141-165`），开盘口径可直接推导，无需改 schema。
- 市况指标从未被计算：`ash.kline_daily` 全市场行数据可随时重算（无重启失忆问题），但没有任何函数做这件事。

### 改法（只改 `mining/reports.py` + 测试）

1. 新增市场状态函数（放在 `mining/reports.py` 内，不新建模块）：

```python
def compute_market_regime(conn, trade_date) -> dict:
    # 从 ash.kline_daily 取当日全部 sec_type='stock' 行,
    # pct = close/有效pre_close - 1 (pre_close<=0 或 NULL 时回退该股前一交易日 close)
    # advancers_ratio = (pct > 0).mean()
    # light: advancers_ratio < 0.40 -> "RED"; > 0.60 -> "GREEN"; 否则 "YELLOW"
    # 返回 {"light", "advancers_ratio", "median_pct", "n"}
```

阈值 40%/60% 依据：按上涨家数占比分组，RED 组信号 t+10 均值 -2.96%/win 30%，GREEN 组 +2.02%/win 51%（locally-verified）。

2. 扩展 `get_review_summary`（或并列新函数 `get_forward_summary`）：对 lookback 窗口内每个 strategy_id，按信号日 `compute_market_regime(...)["light"]` 分组，输出——`n`、`开盘入t2收益 = close_t2/open_t1-1` 与 `开盘入t5收益 = close_t5/open_t1-1` 的均值/中位数、win(>0) 占比。对 `watchlist_outcomes`（Phase J 快照，同列结构）做同样分组汇总，复用同一 helper。
3. `generate_markdown_report` 增加：
   - 开头一行"市场状态：GREEN/YELLOW/RED（上涨家数占比 xx%，全场中位涨幅 xx%）"；
   - `## 今日主升启动` 区块（`_load_candidates(conn, trade_date, "launch_burst")`，列：rank、sec_code、sec_name、涨幅、量比、σ倍数、横盘天数）；
   - `## 前向验证（次日开盘口径 × 市场状态）` 区块，输出第 2 点的分组表。
4. 测试：在现有报告相关测试文件中（synthetic SQLite fixture）覆盖：RED/GREEN 日分组正确、open_t1 为 NULL（停牌）时该行不进开盘口径统计、launch_burst 区块出现在 markdown 中。

### 不改什么

- 不改 `outcomes`/`watchlist_outcomes` schema，不改 `mining/backtest.py` 回填逻辑（该文件有另一工作流的未提交改动，禁止触碰）；
- **市场灯只分组展示，不做候选过滤/硬闸门**（用户尚未拍板闸门方案）；
- 不修 `market_light_api.py`（遗留坏 stub，另行处置）；不动 Streamlit 面板。

### 验收（最低完成标签：locally-verified）

```powershell
python -m unittest tests.test_mining_pipeline tests.test_mining_ui tests.test_launch_burst
# 真实库生成一次日报,人工检查三个新区块存在且数值合理
python -c "from mining.db import connect; from mining.reports import generate_markdown_report; c=connect(); print(generate_markdown_report(c, '2026-06-18', 'output'))"
```

断言：`output/report_20260618.md` 含市场状态行、`## 今日主升启动`（4 只、002821 在列）、前向验证分组表；RED 组与 GREEN 组统计值方向与上表证据一致（同数据源应完全可复现）。

---

## 卡 N — 盘中主升启动扫描（当日快照数据，Phase N v1）

### 业务定义

交易时段内（设计窗口 14:00-15:00，参数化）用 AkShare 实时快照构造"当日合成 K 线"，对**同一套 launch_burst 八项条件**跑全市场扫描，把命中清单即时输出给用户——把信号从"收盘后知道"提前到"盘中可下单"，消除 +0.3pp 平均隔夜溢价与 17~19% 的高开>2% 追价成本（黄-1）。

- 输入：本地 `ash.kline_daily` 最近 `history_days` 根历史 + 实时快照（open/high/latest→close/volume/amount/昨收→pre_close）。
- 输出：控制台表格 + `output/intraday/launch_intraday_<YYYYMMDD>_<HHMM>.md`。
- **不做什么（v1 边界）**：不写 `candidates`/`outcomes`（盘中部分日数据会污染 Phase J 前向统计）；不接 `mining/notify.py`（属另一工作流未提交文件）；不加 Streamlit UI（后续卡）；不改 EOD 扫描器——收盘后的 `run_daily.py` 仍是唯一记录口径。

### 恒等式 / 硬约束

- **同一套规则**：复用 `mining/scanners/launch_burst.py:162` `select_candidates_from_history(history, trade_date, params, ...)`——它只吃 DataFrame，天然支持注入合成 bar；禁止复制粘贴条件逻辑形成第二份实现。
- 若 DB 中已存在当日行（收盘快照已同步），以注入的实时 bar **替换**该行后再扫，避免重复行；非交易时段运行时明确报错退出（fail-closed），不得静默输出空表。
- 快照字段与 `kline_daily` 的**单位一致性**是硬前提：AkShare 东财 spot 的成交量单位为手、`kline_daily` 为股（或相反）时条件⑥⑧全错。列映射以 `app_panel.py:3027` `normalize_a_spot_df_full` 为权威参照（只参照，不 import app_panel——它是 Streamlit 单体）。验收含实测比对（见下）。

### 改法

1. 新模块 `mining/intraday.py`（新文件的理由：盘中取数与 EOD 管线是不同运行时依赖面，混进 db.py/scanners 会让单测拖上网络依赖）：
   - `fetch_spot_frame()`：`ak.stock_zh_a_spot_em()` 失败回退 `ak.stock_zh_a_spot()`（参照 `app_panel.py:3564` `ak_fetch_a_spot_any` 的回退顺序），每接口最多 3 次、显式 timeout，两个接口都失败即报错停止（全局重试纪律）；归一化出 `sec_code/open/high/low/close(=latest)/pre_close/volume/amount`。
   - `build_synthetic_history(conn, spot_df, today)`：加载最近 `history_days` 历史 → 拼接/替换当日合成 bar。
   - `scan_intraday(conn, params_override=None)`：组装后调 `select_candidates_from_history`，返回候选并写 markdown。
2. `run_daily.py` 加 `--intraday` 分支调用 `scan_intraday`（不进 `execute_daily_pipeline`，不落库）。注意 `run_daily.py` 工作树有另一工作流的未提交改动——本卡只允许新增 argparse 分支与 import，diff 必须最小。
3. 量能口径 v1 决策：条件⑥ `vol ≥ 1.5×前5日均量` 直接用**未缩放的盘中累计量**——14:30 累计量偏低造成漏报而非误报（保守偏差），收盘 EOD 跑仍会补齐全部信号。不做时间比例外推（日内量呈 U 形，线性外推引入未验证的放大误差）。
4. 测试 `tests/test_intraday.py`：合成 spot DataFrame 注入（不联网）——①合成 bar 与真实 EOD bar 相同时，盘中扫描结果与 EOD `select_candidates_from_history` 完全一致；②DB 已有当日行时被替换不重复;③非交易时段调用报错;④spot 两接口均失败时抛出而非返回空。

### 未知项（如实标注）

- **未实证**：盘中部分量能造成的漏报率、14:00 vs 14:30 扫描时点差异——需真实交易时段试跑后才有数据；v1 先上线收集。
- **environment-blocked（可能）**：实现时若非交易时段，"真实盘中试跑"验收项顺延，完成标签最高只能写 locally-verified 并注明。

### 验收

```powershell
python -m unittest tests.test_intraday tests.test_launch_burst tests.test_mining_pipeline
# 单位一致性实测(任意最近交易日收盘后跑):抽 3 只票比对 spot.volume/amount 与当日 kline_daily 行,须同单位(比值≈1,而非≈100)
python -c "from mining.intraday import fetch_spot_frame; ..."  # 实现时补齐比对脚本
# 真实盘中试跑(交易时段内): python run_daily.py --intraday
```

- passed 要求：单测绿 + 单位比对通过 → locally-verified；
- 真实交易时段完整跑通且输出合理 → real-env-verified（未跑到则如实标注 environment-blocked，不得写"完成"）。

---

## 已否决 / 暂缓项（决定记录）

- **失败样本图**：用户暂无法提供，后期补充或由程序筛选假阳性（候选做法：从历史信号中取 t+10 < -5% 的样本自动导出 K 线图供用户标注）。本轮不开卡。
- **市场灯硬闸门**（弱市清空清单）：用户未拍板，M.2 只做分组展示。
- **面板盘中清单 UI / 通知接入**：等卡 N 的信号质量在真实盘中验证后再开卡。

---

## 实现记录（2026-07-17）

### 方案复核

- M.1 的排序修复合理，按卡面改为 `sigma_multiple` 升序、`cluster_days` 降序、`sec_code` 升序；`score` 仅保留为复盘特征。
- M.2 的候选分组按 `strategy_id × market_regime` 实现。`watchlist_outcomes` 没有 `strategy_id` 字段，因此按其真实可用维度 `state × market_regime` 分组，避免伪造策略归属。
- N 的 AkShare API 没有可统一依赖的函数级 timeout 参数，采用 `spawn` 子进程承载单次端点调用，父进程到时终止，保证每次尝试有真实上界。网络快照少于 1,000 只有效 A 股时视为不完整并重试/失败，避免局部行情静默产出误导清单。
- N 的盘中累计量不做时间比例外推；盘中结果只写独立 markdown，不写 `candidates`、`outcomes` 或 watchlist 表。

### M.1

- commit：`6cfc9b2 fix(mining): rank launch bursts by mild sigma`
- 2026-06-18 真实库回放输出 4 只，顺序为 `002821, 688306, 603358, 688131`；002821 排名第 1。
- 验收组合：81 tests 通过，2 skipped；旧测试名字面量和旧排序描述已清扫。

### M.2

- commit：`b3bcdd6 feat(mining): report executable forward returns by regime`
- 新增 RED/GREEN/YELLOW/UNKNOWN 市场状态、次日开盘口径分组汇总、今日主升启动区块与合成 SQLite 报告测试。
- 2026-06-18 真实报告状态为 RED，上涨家数占比 37.2%，主升启动区块含 4 只且包含 002821。当前本地候选历史只有该日的 `launch_burst`，不足以复现跨 RED/GREEN 全区间方向，未据此调整任何扫描器参数。
- 验收组合：84 tests 通过，2 skipped。

### N

- 新增 `mining/intraday.py`，复用 `select_candidates_from_history`；`run_daily.py --intraday` 走独立分支，不进入 EOD 持久化管线。
- 合成验收：`tests.test_intraday tests.test_launch_burst tests.test_mining_pipeline` 共 49 tests 通过，2 skipped；覆盖 EOD 恒等、当日行替换、时窗 fail-closed、双端点有界失败、成交量/成交额归一化。
- 真实端点探测：AkShare 快照成功返回 5,193 只有效 A 股；示例 `600000` 的归一化成交量为 79,624,033、成交额为 707,799,247 元，字段与量级合理。
- **environment-blocked**：执行时已过 14:00-15:00 设计窗口，`python run_daily.py --intraday` 按预期在取数前拒绝运行；本地 `ash.kline_daily` 最新交易日为 2026-07-16，缺少 2026-07-17 同日行，且补充历史接口被远端断连，因此尚不能完成卡面要求的 3 只股票同日 `spot`/DB 单位比对，也不能标记 `real-env-verified`。下一交易日需在窗口内重跑，并在当日 EOD 行入库后完成单位比对。
