# 选股系统落地规格：初期异动早发现 + 阶段最强势（真龙/中军）

状态：待实现（实现交给 Codex）
作者：规划 by Claude，依据 D0034《如何系统地找到趋势票》、D0037《谈谈如何选趋势中军票》、`knowledge/cards/setups/SET-trend-strong-leg.md`、`SET-sector-resonance.md`
方向变更：**不做连板**。本规格取代同日的 `2026-06-26-limit-up-relay-mining-system.md`（已删除）。

---

## 0. 目标与边界

**目标**：把两件事做成"高效、实用"的盘后选股能力，并在挖掘面板可见：
1. **初期异动早发现**——在强趋势还没充分加速、带分歧的"胚子"阶段就刷出来（D0034）。
2. **阶段最强势标的**——从一段行情里筛出真正的"真龙/中军"，而非单纯涨幅排名（D0037 + 分离逆势）。

**核心设计判断（必须遵守）**：
1. **新增两个专用扫描器，不改 `momentum_breakout`/`rps_stock` 的口径与测试**（用户决定）。新扫描器继承 `Scanner` ABC、`@register`、产出 `Candidate`，走 `run_daily.py` 的通用 `.run()` 分支自动接入。
2. **选股条件只解决"发现"，不是买入指标**（D0034 原话）。扫描器出候选 + 失败/确认信号，买卖点（逻辑定性、盘口确认）留人工。
3. **纯现有本地数据**：股票/指数日线（`ash.kline_daily`，已确认含 `sec_type='index'`：000001/000300/000852/399001）+ 概念库（`ths`）。**不依赖龙虎榜**（本地无数据源，机构席位那条不做）。
4. **可验证**：每个扫描器配单元测试，用 `tests/_mining_test_helpers.py` 合成 SQLite（需补指数行），不依赖实时行情。
5. **端到端可见**：候选进每日报告（自动）+ Streamlit 挖掘面板（**需手动改白名单**，见 §6）。

**非目标**：连板/空间板/接力；盘中分时入场自动化；龙虎榜机构介入。

---

## 1. 数据契约（已核对）

`ash.kline_daily` 列：`sec_type, sec_code, trade_date, open, high, low, close, pre_close, change, change_pct, volume, amount, turnover_ratio, source, updated_at`。
- `change_pct` 为百分数（9.97 表示 +9.97%）。`board_kind(sec_code)`（`mining/features.py` 已有）：`300/301→gem`、`688/689→star`、其余 `main`。
- **指数**：`sec_type='index'`，可用 `000300`（沪深300）、`000852`（中证1000）、`000001`（上证）、`399001`（深证）。本地覆盖区间约 2025-10 起。
- 交易日序列：`mining/db.list_stock_trade_dates(conn, end_date, limit, include_end)`。
- `build_universe(conn, D)`（`mining/universe.py`）剔除一字板/ST/北交所(8/4/9)/总市值>5000亿/上市<87天——**两个新扫描器都用它**（要的是可交易的强趋势/真龙，剔除一字与次新正合适）。

涨停判定（仅单根，**不做连板高度**）：main 10%、gem/star 20%，`limit_price=round(pre_close*rate,2)`，`is_limit_up = close >= limit_price - 0.01`。

---

## 2. 方法论锚点（来自笔记，指导口径）

三段式：**强趋势胚子发现 → 分离确认 → 中军定位**。现有 `momentum_breakout`（当日大涨）+ `rps_stock`（涨幅RPS）各覆盖一半，缺四个维度：早期性（涨停取反）、形态质量（小阳堆积/独立性）、成交额带、分离逆势强度（价格带按用户决定不纳入）。两个新扫描器各补一头：

- `trend_embryo` ← D0034「强趋势胚子筛选」：`5日涨>13%且涨停取反/≤1` + 加分项。
- `true_leader` ← D0037「中军三特征」（基本面留人工，成交额带可量化；**价格带按用户决定不做限制**）+「分离三步」（陪跑→分离逆势→确认）。分离逆势同时落地 `SET-sector-resonance` 的"抗跌度"。

---

## 3. 共享特征补强（`mining/features.py`，追加，不改现有）

```python
def limit_up_price(pre_close: float, board: str) -> float:
    """main=10%, gem/star=20%；round 到 0.01。"""

def is_limit_up(close, pre_close, board, tol: float = 0.01) -> bool:
    """单根涨停判定（不做连板）。pre_close>0、close>0 才判。"""

def rolling_new_high(series: pd.Series, window: int) -> bool:
    """最后一日 close 是否 >= 近 window 日最高 close（含当日）。"""
```
其余（`change_pct/upper_shadow_pct/ret_n` 等）复用现有。小阳判定（`0 < change_pct < small_body_max`）在扫描器内联即可，不必单列函数。

**测试**（`tests/test_mining_features.py` 追加）：覆盖 main 10% 临界（9.97/10.01，pre_close=10.00→limit=11.00）、gem 20% 临界、`rolling_new_high` 边界。

---

## 4. 扫描器一：`trend_embryo`（初期异动早发现）

**文件**：新建 `mining/scanners/trend_embryo.py`；在 `mining/scanners/__init__.py` 的 `load_builtin_scanners()` 加 `from . import trend_embryo`。
**类**：`strategy_id="trend_embryo"`, `version="v1.0"`, `kind="stock"`。
**说明**：本扫描器即 D0116/D0034 选股公式的实现，统一收口于此（不再单设 `trend_strong`，避免重复）。

### 4.1 算法（盘后，截至 D）
1. `universe = build_universe(conn, D)`。
2. 取近 6 个交易日（`list_stock_trade_dates(D, limit=6, include_end=True)`）的窗口日线，pivot 出 close/change_pct。
3. 计算：`ret_5d = close(D)/close(D-5)-1`（×100）；`limit_up_count_5d`（窗口内 `is_limit_up` 次数）。
4. **早期性硬过滤（核心，D0034）**：
   - 命中条件：`ret_5d > embryo_ret_lo`（默认 13）**且** `limit_up_count_5d <= 1`。
   - 上界：`ret_5d < embryo_ret_hi`（默认 40）——剔除已过度演绎（D0034/启动卡：涨幅>30–40%降级）。
   - path 标签：`no_limit`（涨停=0，最优，"涨停取反"）/ `one_limit`（=1）。
5. **形态加分（打分，非硬过滤）**：
   - `small_yang_count`：窗口内 `0 < change_pct < small_body_max`（默认 5）的阳线根数——小阳堆积越多越好。
   - `single_day_max_change`：窗口内最大单日涨幅——越大越像"一步到位"，减分（剔除"创业板单日干 10cm 没封板/次日被砸"）。
   - `amount(D) >= amount_min`（默认 5 亿，流动性下限）。
6. **独立性加分（可选，v1.0 可先不做，标 v1.1）**：用 `ths` 概念成员映射判断该票所属概念当日是否"齐涨"（成员上涨占比高=跟风，低=独立）。**若 `ths` 无可用"概念→成员"映射表，v1.0 跳过此项并在 features 标 `independence=None`**，Codex 先核对再决定。
7. **打分与排序**：
   ```
   score = w_path*(limit_up_count_5d==0)        # 早期优先
         + w_yang*small_yang_count
         - w_spike*max(0, single_day_max_change - spike_ref)
         + w_indep*independence_bonus(可选)
   ```
   默认权重 `w_path=2, w_yang=1, w_spike=0.3, spike_ref=9.5, w_indep=1`。排序 `score desc → ret_5d desc → sec_code`。取 `top_n`（默认 30）。

### 4.2 Candidate
- `entry_price=close(D)`；`features`：`ret_5d, limit_up_count_5d, small_yang_count, single_day_max_change, amount, board, path, score, independence(可选)`。
- `description`："强趋势胚子（初期异动）盘后发现：5日涨>13%且涨停≤1，带分歧未加速；逻辑定性与买点人工。"
- `default_params`：`{"embryo_ret_lo":13.0, "embryo_ret_hi":40.0, "small_body_max":5.0, "amount_min":500_000_000, "spike_ref":9.5, "top_n":30, "w_path":2.0, "w_yang":1.0, "w_spike":0.3, "w_indep":1.0}`。

### 4.3 测试（`tests/test_mining_pipeline.py` 追加）
- 5日涨 15% 无涨停、3 根小阳堆积 → 命中，path=`no_limit`，分数高于含 1 涨停者。
- 5日涨 22% 含 2 次涨停 → 被早期过滤排除。
- 单日 +10%（gem 类一步到位）主导涨幅、小阳少 → 命中但分数显著偏低（`single_day_max_change` 大、`small_yang_count` 小）。
- 5日涨 45% → 触上界排除。

---

## 5. 扫描器二：`true_leader`（阶段最强势 / 真龙·中军）

**文件**：新建 `mining/scanners/true_leader.py`；`load_builtin_scanners()` 加 import。
**类**：`strategy_id="true_leader"`, `version="v1.0"`, `kind="stock"`。
**与 `rps_stock` 区别**：rps 是纯涨幅 RPS 排名；true_leader 在"阶段强"的基础上加 **分离逆势 + 成交额带**，把"涨得多"升级为"真龙"。（价格带按用户决定不设限制。）

### 5.1 指数序列辅助
```python
def load_index_series(conn, benchmark_code: str, dates: list[str]) -> pd.DataFrame:
    """查 ash.kline_daily where sec_type='index' and sec_code=benchmark_code；返回 trade_date/close/change_pct。"""
```
默认 benchmark `000852`（中证1000，贴近中小盘短线票），可配置。

### 5.2 算法（盘后，截至 D，窗口 N=20）
1. `universe = build_universe(conn, D)`；取近 `lookback`（默认 20，`include_end=True`）日个股日线 + 同期 benchmark 指数序列。
2. **阶段强度门槛**：`ret_N`（默认 20 日涨幅）排名进入前分位，或 `rps_N >= strength_min`（默认 70）。仅在"已是阶段强势"的池子里继续。
3. **分离逆势强度（核心判据，D0037 + 抗跌度）**：
   - 指数下跌日集合 `D_down = {t : index.change_pct(t) < 0}`（窗口内）。
   - `separation_ratio = |{t∈D_down : stock.change_pct(t) > 0}| / |D_down|`（指数跌时个股逆势上涨占比；`D_down` 为空则置 0 并标 `insufficient`）。
   - `divergence_today`：`index.change_pct(D) < 0 且 stock.change_pct(D) > sep_today_min`（默认 0）——当日是否在演绎"分离确认"。
   - `is_new_high = rolling_new_high(close, new_high_window)`（默认 20）。
   - **硬门槛**：`separation_ratio >= sep_min`（默认 0.3）。指数跌它就跟跌的票，不是真龙。
4. **成交额带（D0037，打分项，非硬阈值——可调）**：
   - `amount(D) in [amt_band_lo, amt_band_hi]`（默认 20 亿~80 亿）加分；超出带宽减分。
   - **价格带：按用户决定不做限制**（不按 close 高低过滤或加减分）。
5. **确认动作（可选 feature）**：近窗口出现 `divergence` 后次日是否快速反包/涨停/长阳（越快越强），标 `confirm_speed`。
6. **打分与排序**：
   ```
   score = w_sep*separation_ratio
         + w_high*is_new_high
         + w_div*divergence_today
         + w_amt*amount_band_bonus
   ```
   默认 `w_sep=3, w_high=1, w_div=1, w_amt=1`。排序 `score desc → separation_ratio desc → ret_N desc → sec_code`。取 `top_n`（默认 20）。

### 5.3 Candidate
- `entry_price=close(D)`；`features`：`ret_N, rps_N, close, amount, separation_ratio, divergence_today, is_new_high, amount_band_ok, confirm_speed(可选), benchmark, score`。
- `description`："阶段最强势（真龙/中军）：阶段强度+指数下跌日逆势分离+成交额带；价格不设限；基本面与买点人工。"
- `default_params`：`{"lookback":20, "strength_min":70.0, "benchmark_code":"000852", "sep_min":0.3, "sep_today_min":0.0, "new_high_window":20, "amt_band_lo":2_000_000_000, "amt_band_hi":8_000_000_000, "top_n":20, "w_sep":3.0, "w_high":1.0, "w_div":1.0, "w_amt":1.0}`。

### 5.4 测试（`tests/test_mining_pipeline.py` 追加）
- 合成 benchmark 指数有若干下跌日；股票A 在指数跌日逆势上涨多次（高 separation）→ 排名高于只跟指数同涨的股票B。
- `separation_ratio < sep_min` 的票被排除。
- 成交额在 20–80 亿带内命中体现在 `amount_band_ok` 与分数；带外票得分更低（不因价格高低被过滤或加减分）。
- **fixture 需插入 index 行**（`sec_type='index'`），`_mining_test_helpers.py` 若无指数构造，Codex 补一个 helper。

---

## 6. 集成点（务必全接，否则面板看不到）

| 环节 | 是否自动 | Codex 需做 |
|---|---|---|
| 注册/运行/持久化/回测 | ✅ 自动 | `@register` + `load_builtin_scanners()` 加两行 import |
| markdown/excel 报告 | ✅ 自动（`reports.py` 按 strategy_id 通用遍历） | 无 |
| **Streamlit 挖掘面板** | ❌ **硬编码白名单** | **必须改** `mining/streamlit_tabs/tab_scanner.py` |

`tab_scanner.py` 约 345、358 行硬编码 `strategy_id IN ('momentum_breakout','rps_stock_top20')`。Codex 需：
1. 两处 IN 列表加入 `'trend_embryo'`、`'true_leader'`（及任何同样硬编码 strategy_id 的读取/展示位）。
2. 策略中文名映射补：`trend_embryo→"强趋势胚子"`、`true_leader→"真龙/中军"`。
3. 面板"实时刷新"分支（约 228/238/309/320）目前直连 momentum/rps 的 live 函数。新扫描器**无盘中实时分支可接受**——盘后持久化读取路径（白名单 IN）即满足"选股可见"。如要实时，另立任务。

`run_daily.py` 无需改动（两个新扫描器走通用 `else: scanner.run(...)` 分支）。

---

## 7. 交付顺序

```
Phase 0：features 补 is_limit_up / rolling_new_high + 测试
Phase 1：true_leader（阶段最强 + 分离逆势）+ 测试 + 面板白名单   ← 用户选"两侧都做、含分离逆势"，此条价值最高
Phase 2：trend_embryo（初期异动胚子）+ 测试 + 面板白名单
Phase 3（可选）：trend_embryo 概念独立性加分（依赖 ths 成员映射，先核对数据）
```
建议三个可独立验证的增量提交。

---

## 8. 给 Codex 的执行须知

**验证命令**：
```powershell
python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui
python -m py_compile mining/features.py mining/scanners/trend_embryo.py mining/scanners/true_leader.py run_daily.py
```
冒烟（有本地 DB 时）：`python run_daily.py --date <最近交易日>`，确认 candidates 出现两个新 strategy_id、报告不报错。

**约束**：
- 不改 `momentum_breakout`/`rps_stock` 与 `mining/universe.py`。
- 不依赖实时行情/龙虎榜；测试用合成 SQLite（含 index 行）。
- 成交额带默认 20–80 亿，**做成可配置 `default_params`**，并在代码注释提示按当前市场调参；不要写死在算法体。**价格不设任何过滤/加减分**（用户决定）。
- 不提交 `knowledge/_raw/` 原文（已 gitignore）。
- 编辑前 `git status --short`，勿回退无关改动。
- 向量化优先；分离逆势是窗口×全市场计算，避免逐票 Python 循环拖慢每日管线。

**完成定义（DoD）**：
1. Phase 0/1/2 测试全绿、py_compile 通过。
2. `trend_embryo`、`true_leader` 出现在每日 markdown 报告**且**在 Streamlit 挖掘面板可选/可见。
3. 遵循现有 `mining/` 风格（类型注解、`@register`、features dict）。
4. 末尾"实现记录"小节回填实际函数/参数与任何偏离。

---

## 9. 实现记录（Codex 填写）

### 实际函数/文件清单
- `mining/features.py`：新增 `limit_up_price(pre_close, board)`、`is_limit_up(close, pre_close, board, tol=0.01)`、`rolling_new_high(series, window)`。
- `mining/scanners/true_leader.py`：新增 `load_index_series()`、`select_candidates_from_universe()`、`TrueLeaderScanner`，`strategy_id="true_leader"`，通过 `@register` 接入 `run_daily.py` 通用 `.run()` 分支。
- `mining/scanners/trend_embryo.py`：新增 `select_candidates_from_universe()`、`TrendEmbryoScanner`，`strategy_id="trend_embryo"`，通过 `@register` 接入 `run_daily.py` 通用 `.run()` 分支。
- `mining/scanners/__init__.py`：`load_builtin_scanners()` 加载 `trend_embryo`、`true_leader`。
- `mining/streamlit_tabs/tab_scanner.py`：持久化候选白名单加入 `trend_embryo`、`true_leader`；来源映射加入 `强趋势胚子`、`真龙/中军`。盘中实时分支保持原有 `momentum_breakout`/`rps_stock_top20`。
- `mining/reports.py`：Markdown 增加“今日强趋势胚子”“今日真龙/中军”章节；Excel 增加“强趋势胚子”“真龙中军”工作表。
- `tests/_mining_test_helpers.py`：合成 SQLite fixture 补充 `sec_type='index'` 的 `000001/000300/000852/399001` 指数行。

### 默认参数
- `true_leader`：`lookback=20`、`strength_min=70.0`、`benchmark_code="000852"`、`sep_min=0.3`、`sep_today_min=0.0`、`new_high_window=20`、`amt_band_lo=2_000_000_000`、`amt_band_hi=8_000_000_000`、`top_n=20`、`w_sep=3.0`、`w_high=1.0`、`w_div=1.0`、`w_amt=1.0`。
- `trend_embryo`：`embryo_ret_lo=13.0`、`embryo_ret_hi=40.0`、`small_body_max=5.0`、`amount_min=500_000_000`、`spike_ref=9.5`、`top_n=30`、`w_path=2.0`、`w_yang=1.0`、`w_spike=0.3`、`w_indep=1.0`。

### 与设计差异
- `trend_embryo` 的概念独立性未纳入 v1.0，候选 features 中保留 `independence=None`。原因：当前规格要求先核对 `ths` 概念成员映射，且 v1.0 可跳过。
- `true_leader` 的成交额带按可配置参数打分，带内加分、带外不硬过滤；价格带不做任何过滤或加减分。
- `run_daily.py` 未改动；两个新扫描器依靠注册表进入既有通用分支。

### 测试覆盖点
- `tests.test_mining_features`：涨停价、涨停容差、非法价格、rolling new high 边界。
- `tests.test_mining_pipeline`：`true_leader` 的指数下跌日分离比例、当日分离确认、新高、成交额带打分、低分离排除、通用分支持久化；`trend_embryo` 的 5 日涨幅上下界、涨停次数、无涨停/一涨停 path、小阳堆积、单日尖峰扣分、通用分支持久化。
- `tests.test_mining_ui`：持久化白名单读取 `trend_embryo`、`true_leader`，并映射为“强趋势胚子”“真龙/中军”。