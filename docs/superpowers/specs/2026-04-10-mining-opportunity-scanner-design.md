# Mining Opportunity Scanner - Design Spec

> 日期: 2026-04-10
> 版本: V1
> 状态: Approved
> 实现备注: 2026-04-29 当前主面板文件为 `app_panel.py`，历史文件名 `app_panel_auto_update_v44.py` 已退役。

---

## 1. 系统总览

### 1.1 项目目标

基于现有 A 股情绪面板 (`adata_sentiment_dashboard`) 的数据基础,构建一套"交易机会挖掘 → 自动复盘"系统。核心价值:在不花一分钱实盘交易前,用历史/每日数据验证策略的真实命中率,为后续在线实盘提供"许可证"。

### 1.2 两条产品线

```
┌──────────────────────────────────────────────────────────────┐
│                    OFFLINE 离线研究线 (V1)                     │
│                                                              │
│  日终K线 → Universe过滤 → Scanner筛选 → 候选入库 → 次日OHLC   │
│     → R1-R5 回填 → WIN判定 → 报告生成 → Streamlit tabs       │
│                                                              │
│  目标:用真实复盘数据验证策略                                  │
└──────────────────────────────────────────────────────────────┘
                              │
                              │ (命中率达预期后)
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                     LIVE 在线实盘线 (V2, 占位不实现)           │
│                                                              │
│  14:30 盘中快照 → Scanner实时筛选 → 信号推送 → 尾盘下单       │
│    → 持仓库 → 次日早盘卖出 → 真实 PnL                        │
└──────────────────────────────────────────────────────────────┘
```

### 1.3 V1 范围

**完整实现:**
- 统一 Scanner 框架 (插件式注册)
- 策略1: `momentum_breakout` (异动尾盘)
- 策略3a: `rps_stock_top20` (个股 RPS top 20,带复盘)
- 策略3b: `rps_concept_top20` (板块 RPS top 20,仅展示不复盘)
- 回测引擎: R1-R5 + WIN 判定
- 报告三件套: Streamlit 3 tabs + Markdown 摘要 + Excel 明细
- CLI 入口 + Windows 任务计划兜底

**V1 不做但留接口:**
- 策略2 `cup_handle` (茶杯柄) — 空 scanner 占位, V1.5 填
- 在线实盘线 — V2
- 个股↔板块归属交叉 — V2 补 `concept_member` 数据后再做
- 严格历史市值 (V1 用今日快照近似)

---

## 2. 文件结构

```
adata_sentiment_dashboard/
├── mining/
│   ├── __init__.py
│   ├── db.py                    # connect() + ATTACH helper
│   ├── universe.py              # build_universe(conn, date) → DataFrame
│   ├── features.py              # pure functions: change_pct, upper_shadow, ret_n, rps, board_kind
│   ├── refresh_basics.py        # akshare → stock_market_cap + stock_listing
│   ├── scanners/
│   │   ├── __init__.py          # Scanner ABC + @register + SCANNER_REGISTRY
│   │   ├── momentum_breakout.py # 策略1
│   │   ├── rps_stock.py         # 个股 RPS top 20
│   │   ├── rps_concept.py       # 板块 RPS top 20
│   │   └── cup_handle.py        # 占位 (V1.5)
│   ├── backtest.py              # backfill_outcomes()
│   ├── reports.py               # generate_markdown / excel / get_review_summary
│   └── streamlit_tabs/
│       ├── __init__.py
│       ├── tab_scanner.py       # "异动捕手"
│       ├── tab_rps.py           # "强势榜"
│       └── tab_review.py        # "策略复盘"
├── run_daily.py                 # CLI 入口
├── mining_mvp.db                # (运行时生成)
├── a_share_mvp.db               # (现有, ATTACH 只读)
├── ths_concept.db               # (现有, ATTACH 只读)
├── output/
│   ├── report_YYYYMMDD.md
│   ├── candidates_YYYYMMDD.xlsx
│   └── cron_YYYYMMDD.log
└── ... (现有文件不动)
```

---

## 3. 数据库 Schema

### 3.1 新建 `mining_mvp.db`

通过 SQLite `ATTACH DATABASE` 跨库访问 `a_share_mvp.db` 和 `ths_concept.db`。

```sql
-- ============================================================
-- 1) strategies  策略注册表
-- ============================================================
CREATE TABLE strategies (
  strategy_id     TEXT NOT NULL,
  version         TEXT NOT NULL,
  kind            TEXT NOT NULL,       -- 'stock' / 'concept'
  description     TEXT,
  params_json     TEXT,
  created_at      TEXT,
  PRIMARY KEY (strategy_id, version)
);

-- ============================================================
-- 2) strategy_runs  每次 scanner 跑动记录
-- ============================================================
CREATE TABLE strategy_runs (
  run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id     TEXT NOT NULL,
  version         TEXT NOT NULL,
  trade_date      TEXT NOT NULL,
  run_at          TEXT NOT NULL,
  universe_size   INTEGER,
  n_candidates    INTEGER,
  status          TEXT,                -- 'ok' / 'empty' / 'error'
  error_msg       TEXT
);
CREATE INDEX idx_runs_strategy_date ON strategy_runs(strategy_id, trade_date);

-- ============================================================
-- 3) candidates  候选标的表
-- ============================================================
CREATE TABLE candidates (
  candidate_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          INTEGER NOT NULL,
  strategy_id     TEXT NOT NULL,
  version         TEXT NOT NULL,
  trade_date      TEXT NOT NULL,
  sec_type        TEXT NOT NULL,       -- 'stock' / 'concept'
  sec_code        TEXT NOT NULL,
  sec_name        TEXT,
  entry_price     REAL NOT NULL,       -- T 日收盘价
  features_json   TEXT,
  rank            INTEGER,
  FOREIGN KEY (run_id) REFERENCES strategy_runs(run_id)
);
CREATE UNIQUE INDEX idx_cand_upsert ON candidates(strategy_id, version, trade_date, sec_code);
CREATE INDEX idx_cand_strat_date ON candidates(strategy_id, trade_date);
CREATE INDEX idx_cand_date_code ON candidates(trade_date, sec_code);

-- ============================================================
-- 4) outcomes  复盘回填表
-- ============================================================
CREATE TABLE outcomes (
  candidate_id    INTEGER PRIMARY KEY,
  open_t1         REAL,
  high_t1         REAL,
  low_t1          REAL,
  close_t1        REAL,
  close_t2        REAL,
  close_t5        REAL,
  r1              REAL,       -- (open_t1  - entry) / entry
  r2              REAL,       -- (high_t1  - entry) / entry
  r3              REAL,       -- (low_t1   - entry) / entry
  r4              REAL,       -- (close_t2 - entry) / entry
  r5              REAL,       -- (close_t5 - entry) / entry
  is_win          INTEGER,    -- 1 if (r2>0.02 AND r3>-0.03) else 0
  backfilled_at   TEXT,
  status          TEXT,       -- 'complete' / 'partial' / 'halted' / 'delisted'
  FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id)
);
CREATE INDEX idx_outcomes_win ON outcomes(is_win);

-- ============================================================
-- 5) stock_market_cap  今日总市值快照
-- ============================================================
CREATE TABLE stock_market_cap (
  sec_code        TEXT PRIMARY KEY,
  total_mv        REAL,                -- 单位: 元
  snapshot_date   TEXT,
  updated_at      TEXT
);

-- ============================================================
-- 6) stock_listing  上市日期
-- ============================================================
CREATE TABLE stock_listing (
  sec_code        TEXT PRIMARY KEY,
  list_date       TEXT,                -- 'YYYY-MM-DD'
  snapshot_date   TEXT,
  updated_at      TEXT
);
```

### 3.2 与现有 db 的关系

```python
conn = sqlite3.connect("mining_mvp.db")
conn.execute("ATTACH DATABASE 'a_share_mvp.db' AS ash")
conn.execute("ATTACH DATABASE 'ths_concept.db' AS ths")
```

行情库保持只读, mining 库只存研究中间产物。

### 3.3 幂等性

同一 `(strategy_id, version, trade_date, sec_code)` 重复跑不产生重复候选 (UPSERT)。`backfill_outcomes` 用 `INSERT OR REPLACE` + status 跟踪,支持反复增量运行。

---

## 4. Universe 过滤层 (`mining/universe.py`)

`build_universe(conn, trade_date) → DataFrame`

所有 scanner (stock 类型) 共用此函数获取当日可交易股票池。

| # | 规则 | 实现 |
|---|---|---|
| 1 | 北交所剔除 | 代码前缀不在 `{'8', '4', '9'}` |
| 2 | 当日有成交 | `amount > 0 AND volume > 0` |
| 3 | 非一字板 | `NOT (open = high AND high = low AND low = close)` |
| 4 | ST 剔除 | `stock_info.name` 不含 'ST' / '*ST' |
| 5 | 总市值 ≤ 5000 亿 | LEFT JOIN `stock_market_cap`; 缺失时不剔但标记 `mv_missing` |
| 6 | 上市 ≥ 60 交易日 | JOIN `stock_listing.list_date`; 用 `(trade_date - list_date) 自然日 / 1.45 ≥ 60` 近似 |

返回 DataFrame 字段: `sec_code, sec_name, open, high, low, close, pre_close, change, change_pct, volume, amount, turnover_ratio, board, total_mv, list_date`

---

## 5. 特征层 (`mining/features.py`)

Pure functions, 无 db 依赖, 可单元测试。

```python
def change_pct(row) -> float:
    """涨幅% = (close - pre_close) / pre_close * 100"""

def upper_shadow_pct(row) -> float:
    """上影线% = (high - max(open, close)) / max(open, close) * 100"""

def lower_shadow_pct(row) -> float:
    """下影线% (预留)"""

def ret_n(series_close: pd.Series, n: int) -> float:
    """N 日累计涨幅%"""

def rps(returns: pd.Series) -> pd.Series:
    """RPS 百分位 (0-100), 分母 = 输入 Series 长度(过滤后样本)"""

def board_kind(sec_code: str) -> str:
    """'main' / 'gem' (创业板300/301) / 'star' (科创板688/689)"""
```

**设计原则:**
- 涨幅类字段一律百分数(返回 6.2 不是 0.062)
- outcomes 表的 R1-R5 用 decimal (0.062), 展示层乘 100
- NaN 处理: 数据不足返回 NaN, 由 scanner 决定剔除/保留
- 批量优先: RPS 等操作在 Series 上向量化

---

## 6. Scanner 框架 + V1 Scanners

### 6.1 Scanner 抽象

```python
class Scanner(ABC):
    strategy_id: str
    version: str
    kind: str           # 'stock' / 'concept'
    default_params: Dict

    def __init__(self, params: Dict = None):
        self.params = {**self.default_params, **(params or {})}

    @abstractmethod
    def run(self, conn, trade_date: str) -> List[Candidate]:
        ...

SCANNER_REGISTRY: List[type] = []

def register(cls):
    SCANNER_REGISTRY.append(cls)
    return cls
```

### 6.2 Scanner 1: `momentum_breakout` (异动尾盘)

**默认参数 (全部可配, 改参数 bump version):**

| 参数 | 默认值 | 含义 |
|---|---|---|
| `amount_min` | 1e9 | 成交额 ≥ 10 亿 |
| `gain_main_lo` | 6.0 | 主板涨幅下限 % |
| `gain_main_hi` | 9.5 | 主板涨幅上限 % (天然避开 10% 涨停) |
| `gain_gem_lo` | 7.0 | 创业板/科创板涨幅下限 % |
| `gain_gem_hi` | 13.0 | 创业板/科创板涨幅上限 % (天然避开 20% 涨停) |
| `upper_shadow_min` | 5.0 | 上影线幅度 ≥ 5% |
| `ret_5d_lo` | 5.0 | 5 日涨幅下限 % |
| `ret_5d_hi` | 30.0 | 5 日涨幅上限 % |

**筛选逻辑:**

```
1. universe = build_universe(conn, trade_date)
2. 成交额门槛: amount >= amount_min (AND, 所有路径共用)
3. 涨幅路径 (分层区间):
   - 主板: change_pct ∈ (gain_main_lo, gain_main_hi)  exclusive
   - 创业板/科创板: change_pct ∈ (gain_gem_lo, gain_gem_hi)  exclusive
4. 上影线路径 (同套分层上限避开涨停):
   - 主板: upper_shadow >= upper_shadow_min AND change_pct < gain_main_hi
   - 创业板/科创板: upper_shadow >= upper_shadow_min AND change_pct < gain_gem_hi
5. 路径合并: 涨幅路径 OR 上影线路径
6. 5日涨幅 AND 过滤:
   - ret_5d = close[T-1] / close[T-6] - 1  (不含 T 日自身)
   - ret_5d ∈ (ret_5d_lo, ret_5d_hi)  exclusive
```

**features_json 示例:**
```json
{"change_pct": 7.2, "upper_shadow": 1.3, "amount": 1.54e10, "ret_5d": 12.3, "board": "main", "path": "gain"}
```

`path` 字段记录命中的路径 (`gain` / `shadow` / `both`),便于复盘时分路径统计。

### 6.3 Scanner 2: `rps_stock_top20` (个股 RPS)

**默认参数:**

| 参数 | 默认值 | 含义 |
|---|---|---|
| `lookback_short` | 10 | 短窗口天数 |
| `lookback_long` | 30 | 长窗口天数 |
| `threshold` | 80 | 双窗 RPS 下限 (前 20%) |
| `top_n` | 20 | 取 top N |

**筛选逻辑:**

```
1. universe = build_universe(conn, trade_date)
2. 批量计算所有 universe 股票的 ret_10, ret_30
3. rps_10, rps_30 = percentile_rank(ret_10), percentile_rank(ret_30)
   分母 = universe 过滤后样本数
4. AND 软化: rps_10 >= threshold AND rps_30 >= threshold
5. score = 0.5 * rps_10 + 0.5 * rps_30
6. 取 score 最高的 top_n 只
```

**features_json 示例:**
```json
{"ret_10": 15.3, "ret_30": 28.7, "rps_10": 95.2, "rps_30": 92.1, "score": 93.65}
```

参与复盘: 进入 candidates → outcomes 回填 → WIN 统计。

### 6.4 Scanner 3: `rps_concept_top20` (板块 RPS)

与 Scanner 2 逻辑一致,差异:
- 数据源: `ths_concept.concept_kline` + `concept_master`
- `sec_type = 'concept'`, `sec_code = concept_code`, `sec_name = concept_master.name`
- 不套 universe 过滤 (概念没有 ST/北交所/市值)
- **仅展示, 不参与 outcomes 回填**

### 6.5 Scanner 占位: `cup_handle` (V1.5)

空 `run()` 返回 `[]`, 不注册到 `SCANNER_REGISTRY`。V1.5 实现时填入逻辑后加 `@register`。

---

## 7. 回测引擎 (`mining/backtest.py`)

### 7.1 核心函数

```python
def backfill_outcomes(conn, trade_date: str = None, force: bool = False):
    """
    回填候选的 outcomes.
    - trade_date=None: 回填所有 pending/partial
    - force=True: 已有 outcome 也重算
    - 只处理 sec_type='stock' 的候选
    """
```

### 7.2 算法

```
1. 选取待回填 candidates:
   WHERE sec_type='stock'
     AND (outcome 不存在 OR outcome.status='partial')

2. 交易日历: SELECT DISTINCT trade_date FROM ash.kline_daily ORDER BY trade_date
   - 无外部依赖, 从现有数据派生

3. 对每个候选:
   a) 定位 T+1, T+2, T+5 交易日 (交易日 offset, 非自然日)
   b) 从 kline_daily 拉对应 OHLC
   c) entry = candidates.entry_price (= close[T])
   d) 计算:
      r1 = (open_t1  - entry) / entry
      r2 = (high_t1  - entry) / entry
      r3 = (low_t1   - entry) / entry
      r4 = (close_t2 - entry) / entry
      r5 = (close_t5 - entry) / entry
   e) is_win = 1 if (r2 > 0.02 AND r3 > -0.03) else 0
   f) status:
      - 'complete': R1-R5 全部非 NaN
      - 'partial':  T+1 有但 T+5 还没到
      - 'halted':   T+1 无数据 (停牌)
      - 'delisted': 无任何后续 kline
   g) UPSERT 到 outcomes
```

### 7.3 CLI

```bash
python -m mining.backtest                     # 增量回填
python -m mining.backtest --force             # 全部重算
python -m mining.backtest --date 2026-04-01   # 只回填某天候选
```

---

## 8. 报告生成 (`mining/reports.py`)

### 8.1 Markdown 摘要 (`output/report_YYYYMMDD.md`)

固定 5 个 section:

1. **今日异动候选** (momentum_breakout): 代码/名称/涨幅%/上影%/成交额(亿)/5日涨幅%/路径
2. **今日个股强势榜** (rps_stock_top20): Rank/代码/名称/ret_10/ret_30/RPS_10/RPS_30/Score
3. **今日板块强势榜** (rps_concept_top20): 同上
4. **昨日复盘**: 昨日候选的次日表现 (R1-R5 + WIN)
5. **累计统计** (近 30 天): 各策略 总候选/已回填/WIN数/命中率/平均R1-R3

格式: 百分号 1 位小数, 金额亿为单位。空则显示"无候选"。

### 8.2 Excel 明细 (`output/candidates_YYYYMMDD.xlsx`)

3 个 Sheet:

| Sheet | 内容 | 排序 |
|---|---|---|
| 异动候选 | momentum_breakout 候选 + features 展开列 | 成交额降序 |
| 个股强势 | rps_stock_top20 + features 展开列 | rank 升序 |
| 板块强势 | rps_concept_top20 + features 展开列 | rank 升序 |

`features_json` 展开为独立列。用 `openpyxl`, 表头加粗 + 冻结首行 + 列宽自适应。

### 8.3 函数签名

```python
def generate_markdown_report(conn, trade_date, out_dir="output") -> str
def generate_excel_report(conn, trade_date, out_dir="output") -> str
def get_review_summary(conn, trade_date, lookback=30) -> dict
```

---

## 9. Streamlit Tabs

在现有 `app_panel.py` 中新增 3 个 tab, 逻辑封装在 `mining/streamlit_tabs/` 下。

### 9.1 Tab "异动捕手" (`tab_scanner.py`)

- 日期选择器 (默认最新交易日)
- 当日 momentum_breakout 候选表 (展开 features 列, 可排序)
- path 标签 (gain/shadow/both) 用颜色区分
- 若选过去日期, 底部显示该批候选的 outcomes (R1-R5 + WIN, 绿/红标记)

### 9.2 Tab "强势榜" (`tab_rps.py`)

- 上下两区: 个股 top 20 / 板块 top 20
- 展示: Rank / 代码 / 名称 / ret_10 / ret_30 / RPS_10 / RPS_30 / Score
- 日期选择器, 可回看历史榜单
- 个股榜底部: 若有 outcomes, 显示该日 top 20 平均 R1-R5

### 9.3 Tab "策略复盘" (`tab_review.py`)

- 策略选择器 (momentum_breakout / rps_stock_top20) + 时间窗口 (7/14/30/全部)
- 核心指标卡: 总候选/已完成/WIN数/命中率/平均R1/平均R2/平均R3
- 命中率走势折线图 (每日命中率 + 7日移动均线)
- R1-R5 分布直方图
- 底部: 全部候选明细表 (可筛选 WIN/LOSE, 可排序)

### 9.4 设计原则

- Tab 模块只做展示 + 查询, 不触发写操作
- 每个 tab 内部用 `@st.cache_data` 缓存
- 图表用 Streamlit 内置 (`st.bar_chart` / `st.line_chart`), 不引入额外图表库

---

## 10. CLI 入口 + 调度

### 10.1 `run_daily.py`

```bash
python run_daily.py                           # 默认运行日: scan + backfill + report
python run_daily.py --date 2026-04-01         # 指定日
python run_daily.py --backfill-only           # 只回填 outcomes
python run_daily.py --range 2026-02-10 2026-04-08  # 历史批量回填
```

**默认执行流程:**

```
1. refresh_basics()           # akshare → stock_market_cap + stock_listing
2. for scanner in REGISTRY:   # 遍历注册的 scanner
       candidates = scanner.run(conn, date)
       save_candidates(conn, candidates)
3. backfill_outcomes(conn)    # 增量回填 pending/partial
4. generate_markdown_report() # .md
   generate_excel_report()    # .xlsx
```

**--range 历史批量模式 (V1 第一周回填 60 天历史):**

```
for date in trading_days(start, end):
    # 每天跑 scan + backfill, 跳过 refresh_basics (市值用今日近似)
# 最后一次性出汇总报告
```

### 10.2 Windows 任务计划

```
schtasks /create /tn "MiningDaily" /tr "python run_daily.py" /sc daily /st 17:00
```

- 17:00: 15:00 收盘 → 数据源 30-60 分钟更新 → 17:00 有余量
- stdout/stderr → `output/cron_YYYYMMDD.log`
- 幂等: 当天手动跑过不会重复写入

---

## 11. 数据依赖

### 11.1 现有数据 (只读, ATTACH)

| DB | 表 | 用途 |
|---|---|---|
| a_share_mvp.db | kline_daily | 全A日线 OHLCV (5188只 × 77天) |
| a_share_mvp.db | stock_info | 股票代码/名称/交易状态 |
| a_share_mvp.db | stock_kline_d (view) | 仅股票日线的便捷视图 |
| ths_concept.db | concept_kline | 390 概念指数日线 |
| ths_concept.db | concept_master | 概念代码/名称映射 |

### 11.2 新增数据 (`mining/refresh_basics.py`)

| 目标表 | 来源 | 频率 | 容错 |
|---|---|---|---|
| stock_market_cap | akshare (总市值) | 每日盘后 1 次 | 失败用旧快照 |
| stock_listing | akshare (上市日期) | 每日盘后 1 次 | 失败用旧快照 |

首次全量拉 5188 只, 后续 UPSERT 增量。两张表合并在一个 refresh 任务里, 一次 API 同时取市值和上市日期。

### 11.3 V2 需补数据

- `concept_member` (概念成分股映射) — 用于个股↔板块归属交叉
- 分钟级行情 — 用于在线实盘线 14:30 快照
- 历史每日市值 — 用于严格历史回测

---

## 12. 核心公式汇总

### 12.1 入场

```
entry_price = close[T]    (T 日收盘价, 对应"尾盘买入"语义)
```

### 12.2 出场口径 (全部相对 entry_price)

```
R1 = (open[T+1]  - close[T]) / close[T]    次日开盘 vs 入场
R2 = (high[T+1]  - close[T]) / close[T]    次日最高 vs 入场 (止盈上限)
R3 = (low[T+1]   - close[T]) / close[T]    次日最低 vs 入场 (风险下限)
R4 = (close[T+2] - close[T]) / close[T]    持有 1.5 天
R5 = (close[T+5] - close[T]) / close[T]    持有 1 周
```

T+1, T+2, T+5 均为交易日 offset (非自然日)。

### 12.3 WIN 判定

```
is_win = 1  if  R2 > 0.02  AND  R3 > -0.03
         0  otherwise
```

含义: 次日有 ≥2% 浮盈机会 且 最大回撤未破 3%。

### 12.4 5 日涨幅

```
ret_5d = close[T-1] / close[T-6] - 1    (不含 T 日自身, 避免双重计数)
```

### 12.5 RPS

```
ret_n[stock]  = close[T] / close[T-n] - 1     (n=10 或 30)
RPS_n[stock]  = percentile_rank(ret_n) × 100   (0-100)
分母 = universe 过滤后样本
```

### 12.6 上影线

```
upper_shadow_pct = (high - max(open, close)) / max(open, close) × 100
```

### 12.7 涨幅分层

```
主板 (60x/00x):          change_pct ∈ (6.0%, 9.5%)   exclusive
创业板/科创板 (300/301/688/689): change_pct ∈ (7.0%, 13.0%)  exclusive
```

上影线路径同套涨幅上限: 主板 `change_pct < 9.5%`, 创/科 `change_pct < 13.0%`。

---

## 13. 版本与参数管理

- 每个 scanner 的筛选阈值集中在 `default_params` dict
- 改参数 → bump `version` (如 v1.0 → v1.1) → 历史候选永远绑定当时的 version
- `strategies` 表存 `params_json`, 可追溯任何时点的筛选口径
- 后续可通过改 `params` 并保持框架不动来调优 (如成交额从 10 亿改 8 亿)

---

## 14. V1 第一周目标

1. **Day 1-2**: 搭框架 (db.py, universe.py, features.py, scanner 抽象, run_daily.py CLI)
2. **Day 3**: 实现 momentum_breakout + rps_stock + rps_concept 三个 scanner
3. **Day 4**: 实现 backtest.py + refresh_basics.py
4. **Day 5**: 实现 reports.py (Markdown + Excel)
5. **Day 5-6**: 实现 Streamlit 3 tabs
6. **Day 6**: 历史回填 (`--range 2026-02-10 2026-04-08`)
7. **Day 7**: 第一份完整复盘报告 → 看 60 天的命中率基线 → 决策下一步
