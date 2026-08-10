# 选股口径 v2.5 任务书（2026-08-06；v2.2 已交付 + R0–R4 返修；2026-08-07 R1 裁决增补）

目标：把“当下最符合要求的 A 股”筛出来，并让盘中快照与盘后收盘结果使用同一套定义。
本任务只负责机会筛选，不讨论何时买入、持有多久、成交概率、交易成本、仓位或收益回测。

验收样例（66 张 K 线图）：https://claude.ai/code/artifact/47a40d4c-939e-4733-961a-fa8072254941

**文档状态。** 卡 0 / 0M / 1 / M / 2 / 3 / 4 / 5 / 6 / 7 的代码已由 Codex 交付
（commit `62da52e`..`5a7b770`）。第一部分（评审结论 / 已确认的产品决策 / 真值卡 / 卡片）
保留为**已派发原文**，只在事实被实测推翻处就地标注 `【v2.3/v2.5 修订】`。

**第二部分是本轮要执行的内容**，合并了两份独立材料：
`docs/superpowers/specs/2026-08-06-screening-v2-quality-review.md`（Codex 评审）
与 Opus 侧独立只读实测。返修卡为 **R0 → R1 → R2 → R3 → R4**，全串行。
其中 **§3「算法设置更新」是能力 A 的新真值口径**，替代第一部分「长均线多头排列即强趋势」的写法。

**【v2.5 二审优先级】** 第二部分中带 `【v2.5 二审修订】` 的条款、§6 用户裁决和各 R 卡
共同构成本轮验收合同；它们与第一部分已派发原文冲突时，以 v2.5 条款为准。特别是 R3 对
`run_daily.py`、`mining/backtest.py` 的窄范围例外，以及正式能力版本升级，不受第一部分旧冻结区约束。
R4 为浏览器隔离增加 `SCREENING_BASE_DIR` 的最小 `app_panel.py` 接缝，同样属于 v2.5 例外。
**2026-08-07 的 §6 裁决 4 优先于此前 A2 数值冻结条款；正式 definition version 仍为 `v2.5`。**

**开工前必须先读仓库 `AGENTS.md`，再按第二部分「派发入口」执行。** 产品口径与执行边界已于
2026-08-06 定案；脏文件、项目解释器与 66 图标签包均已有无须追问用户的处理规则。

---

## 评审结论

**总体评级：方向正确，原任务需重构后再派发。**

原设计最有价值的部分，是把候选拆成强趋势、异动起爆、回调支撑、平台突破、逆势强度五种观察视角，
并坚持“先过定义闸门，再在合格股票中排名”。主要问题有六个：

1. 把选股与交易执行、持有期、成本后收益混在同一任务，偏离当前产品目标。
2. 盘中只更新两个 legacy 扫描器，A–E 仍停留在收盘口径，无法回答“此刻谁最符合要求”。
3. 原稿硬依赖 MA120/MA200 和大规模历史回填；当前本地数据不足时会让合理功能无法落地。
4. 现有优秀因子没有被系统复用，反而准备平行新建逻辑，容易产生多套相互矛盾的“强势”定义。
5. `true_leader` 把百分比、0/1 哑变量和绝对金额直接相加；`trend_embryo` 全部 `score=0` 后按代码排序；
   固定 Top20/Top30 与固定成交额也容易制造虚假的精确性。
6. 股票池的当前状态、价格复权、坏交易日和消费者硬编码，会直接污染筛选结果。

顶级交易员视角下，真正需要的不是更多指标，而是四件事：

- **结构**：它是否处在可辨认的上升、收缩、突破或回调结构里；
- **相对强度**：同一时点相对全市场和基准指数是否更强；
- **阶段**：当前是延伸、回调、支撑、再启动还是失效；
- **证据质量**：这份判断使用的是盘中临时快照还是已定版收盘数据，历史长度是否足够。

市场状态灯保留为背景信息，不作为硬拦截器；严格口径允许当天空榜。

---

## 已确认的产品决策

1. 输出是**筛选榜单**，不是交易建议；`entry_price` 若因旧 DTO/表结构暂时保留，只作为兼容字段，
   UI 和新特征语义统一称为 `reference_price`。
2. 盘中打开面板时，用同一批次的实时快照更新当日临时 bar，并重新运行 A–E。
3. 盘后官方/本地收盘日线通过质量检查后，将当日状态定版为 `close_final`；次日刷新或重启不得继续把前一日快照当正式结果。
4. 股票池排除北交所，覆盖沪深主板、创业板和科创板；使用 A 股前缀白名单，
   保留 `000/001/002/003/300/301/600/601/603/605/688/689`。
5. 本轮不做大规模历史回填。均线使用全市场统一的自适应档位：P120 → P90 → P60；
   数据未来自然积累到要求后再自动升级到 P200。
6. 现有体系去粗取精后融合：复用 `rps_stock`、`true_leader`、`momentum_breakout`、`trend_embryo`、
   `launch_burst`、`watchlist`、`second_launch` 的有效因子，不保留失真的旧排名方式。

---

## 真值卡

### 五个非互斥能力

| 能力 | 中文名 | 回答的问题 | 主要来源 |
|---|---|---|---|
| A | 强趋势 | 此刻结构合格且横截面最强的是谁 | 结构闸门 + `rps_stock` + `true_leader` |
| B | 异动起爆 | 谁正在从沉寂/蓄势状态进入价格与活动度扩张 | `launch_burst` + `momentum_breakout`，`trend_embryo` 作上下文 |
| C | 回调支撑 | 过去已确认的趋势股现在处于什么回调阶段 | A 的历史资格 + `watchlist` + `second_launch` |
| D | 平台突破 | 谁刚突破中期箱体且收缩与活动度证据成立 | 新增 `base_breakout`，复用共享活动度/趋势因子 |
| E | 逆势强度 | 基准走弱时，谁仍维持绝对上涨与相对强度新高 | `true_leader` 分离度 + 新增 RS 序列 |

同一股票可以同时出现在多个能力里，不去重、不强制互斥。A 的“强度档”与 C 的“阶段”是两条轴，
例如一只股票可以同时是“强趋势 × 回调到位”。

### 现有体系取舍

| 现有组件 | 保留并融合 | 淘汰或降级 |
|---|---|---|
| `rps_stock_top20` | 10/30 日横截面 RPS 思路、确定性排序 | 固定 Top20、没有结构闸门、把短期涨幅等同于趋势 |
| `true_leader` | 指数下跌日分离度、分离比例、相对新高、过度延伸标记 | 不同量纲直接加权、固定金额区间、样本不足时无条件放行 |
| `momentum_breakout` | 主板/双创分板涨幅路径、长上影/冲高路径、近期脉冲上下文 | 与 RPS 扫描器的隐藏排除耦合、固定 10 亿元门槛、按绝对金额排名 |
| `trend_embryo` | 5 日累计涨幅、涨停次数、小阳线数量、单日最大涨幅等“早期路径标签” | 独立榜单；当前 `score=0` 且按代码排序没有排名意义 |
| `launch_burst` | 均线压缩、历史波动率、首次启动、缩量后放量、收盘强度 | 固定金额门槛、坏 bar 污染、把 share volume 当唯一活动度 |
| `watchlist` | 主升幅度、峰值回撤、均线支撑、缩量、止跌、再启动状态 | “曾被任一旧扫描器命中”充当趋势资格、无前态也可判再启动 |
| `second_launch` | 回撤、缩量、支撑、止跌组合 | 复制 watchlist 计算、固定来源扫描器、孤立的混合分数 |

旧扫描器可暂留在“legacy/其他观察”区用于对照；v2 的正式 A–E 只通过共享因子层产生。

### 第一性原理硬约束

1. **先闸门，后排名。** 分数只能给已满足能力定义的股票排序，不能用高分补偿硬条件失败。
2. **同量纲后加权。** 连续因子先转成当日合格股票池内的百分位；布尔条件只做闸门或解释标签。
3. **榜上打印排序证据。** 用户必须能根据显示字段复核名次。
4. **统一时点。** 同一轮所有股票来自同一 `as_of` 快照批次；禁止一部分实时、一部分前收盘。
5. **统一趋势档位。** 每次运行只选择一个 `trend_profile`；禁止按个股数据长度分别降档后混排。
6. **缺失不是未发生。** 必需字段缺失就跳过该股票并累计 `skipped_reason_counts`，不得以 0/False 代替。
7. **快照不是收盘。** `intraday_snapshot` 永远带临时标签，不写入正式日线候选历史和状态历史。
8. **定版覆盖快照。** 一旦当日 `close_final` 可用，所有读取路径都优先正式结果；重启不得复活旧快照。
9. **价格指标使用清洗后的前复权序列。** 原始 OHLC/量额保留作事实展示；量和额不机械复权。
10. **北交所全链排除、双创全链保留。** universe、扫描器、快照、持久化查询、报告和 UI
    统一使用 A 股前缀白名单，不能只用“排除 4/8/9”而误纳 `200xxx` 深市 B 股。
11. **一字价格与停牌分开。** `volume/amount<=0` 的无有效交易 bar 排除；正成交的一字价格 bar 可保留为形态事实，
    不再因“可能买不到”从选股层删除。
12. **数量只做 smoke check。** 命中数变化触发漏斗复核，不作为算法正确性的金标准。
13. **市场状态只分组展示。** 不因强/中/弱市场自动把定义成立的股票删掉。
14. **【v2.3 新增】质量判定按 (行, 字段) 分层，不按整日一刀切。** 一个交易日里 5200 行中有 5 行坏，
    只能剔除那 5 行并在诊断里记录，不得把整天判成不可用。“整日不可用”只保留给覆盖率或有效率
    低于阈值的真实坏日。
15. **【v2.3 新增】能力只有一个定义入口。** 每个能力对外只暴露一个选股函数；面板、扫描器、报告、
    回补都调用它。必需输入（基准序列、clean 日历、活动度基线）由上下文构建阶段解析完毕，
    不得做成“可选参数，缺省 None 时退化成另一套打分”。
16. **【v2.3 新增】定版即替换。** 某个 `(strategy_id, version, trade_date)` 的 `close_final` 结果落库时，
    必须先删除该三元组下的旧行再写入，最终行集等于最后一次成功定版的结果。所有读取路径必须带
    `version` 过滤，旧版本行不得混入 v2 正式面板。

### 运行模式状态机

| 模式 | 数据构成 | 是否正式 | 是否持久化正式候选 |
|---|---|---:|---:|
| `intraday_snapshot` | T-1 及以前定版日线 + T 日同批次快照临时 bar | 否 | 否 |
| `close_pending` | 已收盘，但 T 日正式日线尚未通过完整性检查 | 否 | 否；可显示最后快照但必须显式标注 |
| `close_final` | 截至 T 日的定版日线，且当日覆盖率与字段质量通过 | 是 | 是 |
| `data_unavailable` | 快照/收盘数据缺失、过旧或覆盖率不足 | 否 | 否；不得静默退回前一日冒充当日 |

盘中临时 bar 的 `close` 语义是最新价，`open/high/low` 是当日截至快照的累计值，`volume/amount` 是累计成交。
盘中活动度统一使用：

`intraday_activity_ratio = amount_T_asof / median(amount_{T-20..T-1})`

再转成同一快照股票池的横截面百分位。因为所有股票使用同一时点，公共的交易时段进度不会改变横截面次序；
不得把累计量额直接与完整日固定阈值比较。若快照缺 `amount`，可用 `turnover_ratio`；两者都缺时，依赖活动度的 B/D 跳过，
A/E 的纯价格判断仍可运行并标 `activity_unavailable`。

> **【v2.3 修订】** 交付实现 `mining/event_activity.py` 与本节有三处不一致，R2 卡负责修正：
> (a) `close_final` 用 `mean(amount_{T-20..T-1})` 而非本节写的 `median`；
> (b) `intraday_snapshot` 分支直接把 `activity_ratio` 置空，改用**绝对活动度原值**排百分位，
>     等于盘中和收盘在比两个不同的量；
> (c) 盘中按 `activity_source`（amount / turnover_ratio）分组各自算百分位，
>     不同数据源的股票落在互不可比的两个池子里。
> 正确口径：两种模式都用同一个 `ratio = current / baseline`，baseline 统一取 `median`，
> 百分位在同一个合格池内一次算完；`activity_source` 只作解释标签，不作分池依据。

### 自适应趋势档位

档位由本轮共有的 clean session 数一次性确定，选择可用的最高档：

| 档位 | 最少 clean sessions | 结构闸门 | 长线方向 |
|---|---:|---|---|
| P200（未来） | 220 | `close>MA20>MA60>MA120>MA200` | `MA200>MA200.shift(20)` |
| P120（当前优先） | 140 | `close>MA20>MA60>MA120` | `MA120>MA120.shift(20)` |
| P90（降级） | 110 | `close>MA20>MA60>MA90` | `MA90>MA90.shift(20)` |
| P60（最低） | 75 | `close>MA10>MA30>MA60` | `MA60>MA60.shift(15)` |

低于 75 个共有 clean sessions 时，A/C 不发布正式榜单，显示 `insufficient_history`；B/D/E 只运行各自能够满足的窗口，
不得继续静默降低趋势定义。即使全局选择了某档，单只股票有效 bar 不足仍应跳过，不允许个股单独降档。

> **【v2.3 修订】** 交付实现 `mining/trend_factors.py:TREND_PROFILES` 的 P200 档结构闸门写成
> `close>MA20>MA60>MA200`，漏掉了 MA120（P200 当前不可达，属静默待爆缺陷）。
>
> 更重要的是：**本节整张表在 R1 卡中降级。** 长均线多头排列从「强趋势的必要条件」
> 改为「趋势成熟度证据」，因为它会把 300996、601858 这类刚进入主升的股票挡在门外
> （详见第二部分 §R1）。`trend_profile` 保留用于窗口长度自适应和覆盖率检查，
> 不再单独充当强趋势闸门。

### 当前数据实况

**v2.2 派发时的实测（2026-08-06 上午）：**

- 主库股票交易日 192 天，范围 `2025-10-23..2026-08-05`。
- `2026-07-13` 有 5199 行股票 bar 全部 `close<=0`，且 `volume/amount=0`，是明确坏交易日。
- 其余交易日股票行数约 5149–5206；未发现第二个大面积缺失日。
- `stock_info.updated_at` 最晚到 2026-04-22，不能代表当前 ST/名称状态。
- `stock_market_cap` 只有 2026-06-25 单快照；`stock_listing` 为空。
- 可用基准指数：`000001 / 000300 / 000852 / 399001`。
- 当前历史修复坏日后足以运行 P120，但不足以运行 P200；本轮无需为 P200 大规模回填。

**【v2.3 新增】v2.2 交付后按新质量门实测（2026-08-06 下午，只读）：**

- `mining.data_quality` 判定 192 天中 `clean=146 / partial_missing=45 / bad_session=1`。
  45 个 partial 日里多数只有 3–20 行坏（占 ~5200 行的 0.06%–0.4%）。
- 真正大面积退化的只有 3 天：`2026-05-19`（5174 行中 2819 有效）、`2026-06-05`（5186 行中 1882 有效）、
  `2026-07-13`（5199 行中 0 有效）。
- `clean_stock_trade_dates` 因此止于 `2026-07-31`；`2026-08-03/04/05` 全部不可见。
  `2026-08-05` 实为 5202 行中 5197 有效（5 行坏）。
- `change_pct` 空值是全库级别问题：`2026-07-17` 全部 5175 行为 NULL，`2026-07-31` 5178 行中 2769 行为 NULL；
  2 月至 7 月区间全部 5202 个代码都至少有 1 个 NULL，合计 101,977 空行。
- 上述三点叠加，v2.2 在真实数据上 A/B/C/D/E 全线 0 行；证据与复现见「v2.2 交付实况」一节。

这些数字是数据漂移 smoke 基线，不是固定命中数或有效性证明。

### 当前工作区边界（2026-08-06 实测）

工作树已有其他任务的在途改动。后续派卡必须先执行 `git status --short`，并遵守以下所有权：

- **【历史卡 0，非本轮 R0】** 与当前脏文件 `offline_daily_update.py` 重叠；该旧卡若重开仍须原 owner
  commit/stash 或明确交接。本轮 R0 不修改该文件。
- `repair_market_day_akshare.py` 当前在途，本任务只允许调用，不修改。
- `tests/test_backfill_rules.py` 当前在途，本任务只运行，不修改。
- ~~`tests/test_mining_pipeline.py` 只运行~~ **【v2.4 已放行，范围受限】** 用户于 2026-08-06 裁决放行，
  但只限 R1 卡、只限以下两个用例，其余用例与该文件其他部分仍为禁止修改范围：
  - `MiningPipelineTests.test_second_launch_selects_pullback_shrink_and_stop_setup`
  - `MiningPipelineTests.test_execute_daily_pipeline_persists_second_launch_via_generic_scanner_branch`

  放行方式是**迁移 fixture 使样例显式满足新的 A 资格**，不得为恢复旧测试而回退新语义，
  不得用 `skip`/`expectedFailure` 绕过。详见第二部分 §6 裁决 1。
  其余各卡的新增断言仍放入本任务专属测试文件。
- `runtime_paths.py`、`backfill_etf_equity_60d_v2.py`、`.gitignore`、`AGENTS.md`、`CLAUDE.md`
  与现有通知相关文件均为禁止修改范围。
- **【v2.5 二审例外】** `run_daily.py` 仅允许 R3 修改“正式 A–E 共享 context + close-final 批次持久化”
  的最小接缝；`mining/backtest.py` 仅允许 R3 增加“正式 v2.5 不生成交易 outcomes”的版本过滤。
  两文件当前有他流在途改动；按第二部分 §7 将当前内容冻结为不可覆盖的用户基线后即可叠加最小改动，
  无须等待原 owner commit/stash，也不得把基线 diff 纳入本任务 commit。
- `knowledge/backlog.md` 当前在途；卡 5 不处理旧 `cup_handle` 文档/死桩，只新增并注册 `base_breakout`。

若交付时状态与上述快照不同，以最新 `git status --short` 为准。除 R3 对 `run_daily.py/mining/backtest.py`
按第二部分 §7 执行“基线隔离 + 最小叠加”的明列例外外，其他白名单文件若与未知在途改动重叠，必须停止该文件并记录交接，
不能假设在途改动可覆盖。

---

## 依赖顺序

```text
卡 0（坏日/质量）
  -> 卡 0M（当前股票池）
  -> 卡 1（共享复权读取层）
  -> 卡 M（盘中快照/收盘定版）
  -> 卡 2（共享趋势因子 + A）
       -> 卡 3（C）
       -> 卡 4（B，可在卡 3 完成后执行）
       -> 卡 5（D）
       -> 卡 6（E）
  -> 卡 7（统一面板与报告）
```

卡片顺序是交付顺序。除明确写出的并行边界外，不重排；每张卡一个 commit。

---

## 卡片

### 卡 0 — 修复坏交易日并建立统一数据质量门

**问题。** `2026-07-13` 的全零 bar 会同时破坏均线、波动率、量能基线和交易日窗口。

**任务。**

1. 新增共享数据质量检查，至少识别：非有限值、`close<=0`、有成交语义但 `volume/amount<=0`、
   OHLC 关系错误、覆盖率相对近 20 个 clean sessions 中位数异常。
2. `offline_daily_update.py` 的缺口检测必须同时发现“日期存在但整日坏值”，不能只看日期是否存在。
3. 对 `2026-07-13` 先做单日、限定源、限定超时的修复；不得启动大范围回填。
4. 若所有可用源均失败，把该日写入显式不可用日清单；共享交易日历跳过它，且记录来源错误。
5. 扫描器不各自复制坏日判断，只消费共享 clean calendar/diagnostics。

**范围。** `mining/data_quality.py`、`offline_daily_update.py`、必要的单日修复调用与专属测试。
不修改数据源抓取算法，不运行宽日期区间回填。

**验收。**

```powershell
python -m unittest tests.test_data_quality tests.test_backfill_rules
python -m unittest
```

专属断言：全零整日、部分缺失日、正常停牌、OHLC 错误分别被正确分类；坏日不会进入 MA/rolling 窗口；
修复失败时状态为显式 `known_bad_session`，不是“今日无信号”。

---

### 卡 0M — 建立当前选股股票池，不做大规模历史元数据回填

**问题。** 当前 `stock_info` 过旧、`stock_listing` 为空、单快照市值有前视/陈旧风险。

**任务。**

1. 建立唯一 `build_selection_universe()`：
   - 使用唯一白名单 `000/001/002/003/300/301/600/601/603/605/688/689`；
   - 因此北交所和 B 股均被排除，创业板与科创板明确保留；
   - 当前 ST/*ST 依据本轮快照名称或当日刷新后的元数据排除；
   - `volume/amount<=0` 的无有效交易股票排除；正成交的一字价格 bar 不因可交易性假设排除。
2. 盘中优先使用同批次快照名称判断当前 ST；盘后使用最新成功刷新的 `stock_info`，显示 `metadata_as_of`。
3. 当前元数据早于最近完整交易日且快照也无名称时，v2 状态为 `metadata_stale`，不发布“正式完整榜单”。
   > **【v2.3 修订】** 本条措辞含混，被实现为“整站停摆”：`stock_info.updated_at=2026-04-22` 恒久早于
   > 最近交易日，于是每天都是 `metadata_stale`，各能力 `if context.data_status != "ready"` 直接返回
   > `skipped={'data_unavailable': 5016}`，A–E 全部 0 行。
   > 正确语义是把「能不能算」和「能不能宣称权威」分开：`metadata_stale` 只降级为
   > 「榜单可用，ST/名称信息截至 `metadata_as_of`」的展示标注，**不得**清空榜单。
   > 真正必须拦截的只有价格数据不可用（`data_unavailable`）。R0 卡负责。
4. 本轮不建立逐日历史 ST/市值/上市状态表，不补 750 天历史；历史回放明确标为受限研究，不声称无前视。
5. 移除 v2 对单快照 `total_mv` 的依赖；流动性改用 T-1 为止的 20 日成交额/换手率分位。
6. 上市时长以可用 clean bar 数满足各能力窗口为准；不因 `stock_listing` 缺失而全市场 fail-open。

**范围。** `mining/universe.py`、共享 context、专属测试。旧 universe API 可保留兼容，但 v2 不可绕过新入口。

**验收。**

```powershell
python -m unittest tests.test_selection_universe tests.test_mining_pipeline
```

专属断言：`920xxx/83xxxx/43xxxx` 被排除；`300xxx/301xxx/688xxx` 被保留；ST、停牌、正成交一字 bar、
元数据过旧分别走预期分支；结果带 `universe_count / excluded_reason_counts / metadata_as_of`。

---

### 卡 1 — 建立共享前复权读取层与 SelectionContext

**问题。** 当前日线以不复权价入库，除权缺口会把收益、均线、箱体和新高全部扭曲；各扫描器又各自 SQL 取数。

**任务。**

1. 使用 `pre_close / previous_valid_close` 重建可对账的本地因子链，以最新 bar 因子固定为 1。
2. 仅对 OHLC/close 生成前复权研究序列；`volume/amount/turnover_ratio` 保留原始事实，不机械乘除价格因子。
3. 复权跳变需满足有限、为正、合理范围和价格 tick 差异；异常值 fail-closed 并写 diagnostics。
4. 新增 `SelectionContext`，统一携带 `bars`、`universe`、`trade_date`、`mode`、`as_of`、`price_as_of`、
   `metadata_as_of`、`trend_profile`、`data_status`、`diagnostics`。
5. 注册扫描器的 `run(conn, trade_date)` 与盘中 UI 都调用同一个 `select_from_context(context)` 纯逻辑入口，
   禁止维护两套盘中/盘后公式。

**范围。** 优先新增一个职责单一的共享读取模块并改 v2 消费者；不改原始 SQLite 价格。

**验收。**

```powershell
python -m unittest tests.test_adjusted_prices tests.test_selection_context
python -m unittest tests.test_mining_pipeline
```

专属断言：50% 除权缺口复权后不产生 -50% 假跌幅；最新复权价等于原始收盘；量额不变；
坏日被 clean calendar 跳过；同一 context 重跑结果完全一致。

---

### 卡 M — 盘中快照、收盘待定与收盘定版

**问题。** 当前面板盘中只重跑 `momentum_breakout/rps_stock_top20`，不能代表 v2；盘后也缺少快照退出与收盘定版合同。

**任务。**

1. 实现唯一模式解析器：按交易日历、当前时间、快照可用性和当日正式日线质量，返回四种模式之一。
2. `intraday_snapshot`：以 T-1 及以前 clean close history 为基线，把同批次快照作为 T 日临时 bar；
   A–E 全部调用相同 `select_from_context()` 重算。
3. 快照必须记录批次 `snapshot_as_of`、来源和覆盖率；过旧/覆盖不足走 `data_unavailable`，
   不得拿前收盘伪装成今日最新价。时效/覆盖阈值做配置项并在 UI 打印实值。
4. `close_pending`：收盘后但正式日线尚未通过质量检查时，可继续显示最后快照，显式标“待收盘定版”。
5. `close_final`：正式日线覆盖率、OHLC、量额和交易日检查通过后，重跑并持久化正式候选/状态；
   清除或忽略同日临时缓存。
6. 次日刷新或重启时，前一交易日只能读取 `close_final`；若不存在就显示“前日收盘数据未定版”并触发已有小范围修复流程，
   不得恢复旧 snapshot。
7. 盘中快照不写 `candidates/outcomes/watchlist_snapshots` 正式历史；可使用进程内缓存，缓存 key 必须含 `trade_date+snapshot_as_of+mode`。
8. 增加模式等价断言：当构造的快照 bar 与最终日线完全相同时，两种模式的 A–E 结果和排序必须一致，仅元数据标签不同。

**范围。** 新增/修改共享 selection runtime 与测试；面板视觉接线留到卡 7。不得修改 `run_daily.py`。

**验收。**

```powershell
python -m unittest tests.test_selection_runtime tests.test_selection_context
```

专属断言：盘中更新最新价会改变相应榜单；快照结果不落正式库；收盘定版覆盖快照；次日重启不复活旧快照；
快照与 final bar 相同时结果等价；快照缺量额时 A/E 可运行、B/D 明确降级。

---

### 卡 2 — 共享趋势因子与能力 A：强趋势

**问题。** 现有 RPS 回答“最近谁涨得多”，没有回答“谁处在可靠上升结构”；`true_leader` 的混合分数不可解释。

**任务。**

1. 新增共享 `resolve_trend_profile()` 与 `evaluate_trend_structure()` 纯函数，严格执行 P200/P120/P90/P60 表。
2. profile 在全市场每轮只解析一次；单股 bar 不足则跳过，禁止个股降档。
3. A 的硬闸门：
   - 通过当前 profile 的均线排列与长期方向；
   - 收盘位于对应长窗口最高收盘的 85% 以上；
   - T-1 为止的 20 日中位成交额/换手率位于当日股票池 40 分位以上；
   - 股票池与数据质量合格。
4. 融合现有最好因子：
   - `rps_recent_pct`：近 20 日收益的横截面百分位；
   - `rps_prior_pct`：从 `T-long` 到 `T-20` 的非重叠收益窗百分位；
   - `separation_pct`：复用 `true_leader` 的基准下跌日超额表现，样本不足时该因子标 unknown，不用 0 代替；
   - `near_high_pct`：距长窗口高点的横截面百分位。
5. 初始排名：`0.35*rps_recent_pct + 0.25*rps_prior_pct + 0.25*separation_pct + 0.15*near_high_pct`。
   某可选因子只有在**本轮全市场统一不可用**时才可对所有股票一起移除并重归一，同时显示 `score_components_used`；
   单只股票缺字段则跳过，禁止每只股票使用不同权重后混排。不得用布尔量直接加分。
6. **【v2.5 已替代】** 原“前 20% 强趋势 / 其余趋势成型中”配额口径不得再实现；唯一真值见第二部分 §3。
7. `trend_embryo` 的 `limit_up_count_5d / small_yang_count / single_day_max_change / path` 只作为路径解释标签，
   不再生成独立的零分榜单。

上述阈值是首版定义值，66 图只用于检查定义与人工形态是否一致；若需调整，必须记录逐条件漏斗和被修正的反例。

**范围。** 新增 v2 共享因子与 A scanner，注册新 scanner，新增专属测试；不删除 legacy scanner。

**验收。**

```powershell
python -m unittest tests.test_trend_factors tests.test_strong_trend tests.test_mining_pipeline
```

专属断言：P120/P90/P60 逐级选择正确；一次运行不会混档；139/140 根在 P120 下分别不合格/可计算；
严格多头排列通过、均线纠缠失败；闸门失败不能靠分数进入；百分位和最终分数可手算复核。

---

### 卡 3 — 能力 C：趋势股回调阶段

**问题。** 当前 watchlist 用“曾被旧强度扫描器命中 + 涨过 30%”替代趋势资格，`再启动` 也没有前态约束。

**任务。**

1. C 的候选资格改为：近 60 个 clean sessions 内，曾连续至少 5 个交易日通过卡 2 的共享结构闸门，
   且至少一次进入 A 的合格池。禁止复制均线公式。
2. 保留并统一现有有效特征：主升基点与峰值、峰值回撤、MA10/20/长线 MA 距离、缩量比、回调阴线数、
   近期新低、重新站回 MA10、活动度扩张。
3. 状态固定为：`延伸中 / 回调中 / 回调到位 / 再启动 / 破位失效`。
4. `再启动` 必须要求前 1–5 个 close-final sessions 曾处于 `回调中/回调到位`，且当前重新站回触发位并活动度扩张；
   无历史前态时最多判为 `回调到位`。
5. close-final 状态可持久化；intraday 状态只读取最近 close-final 前态并临时计算，不写回状态历史。
6. `second_launch` 改为复用同一状态/特征结果，不再单独重新查询并复制量价计算。

**范围。** `mining/watchlist.py`、`mining/scanners/second_launch.py`、共享趋势因子、专属测试。

**验收。**

```powershell
python -m unittest tests.test_watchlist_state_machine tests.test_second_launch
```

专属断言：只有旧扫描器标记但从未通过趋势闸门的股票不能进入；五种状态边界有合成样例；
没有历史前态不能判再启动；盘中临时状态不会污染次日 close-final 历史。

> **【v2.5 二审修订】C 的历史覆盖前提。** C 的资格依赖**已落库**的 A 候选历史，
> 而 A 在 R0–R3 修复前一行都存不下来，`candidates` 表里 `strong_trend` 当前为 0 行。
> 修复后只要已有一个更早的 A close-final 日期，C 就可能非空；未满 60 日只表示资格回看召回不完整，
> 不能把结果压成空榜。覆盖显示与可选历史回算见第二部分 R4。
> 实测 `2026-07-31` 强制放行元数据后，C 跳过原因为
> `five_session_structure_missing: 4954, never_in_a_qualified_pool: 62`，与上述判断一致。
> 处理办法见第二部分 R4 卡第 2 条。
>
> **另：** 本卡第 1 条的 C 资格口径（「曾连续 5 日通过结构闸门」）在 R1 中改为
> 「近 60 个可用交易日内曾进入 A 的强趋势档」，第 3 条状态词表增加承接 A 原 `weakening` 的语义
> （见第二部分 §3.5）。

---

### 卡 4 — 能力 B：异动起爆，融合 launch_burst 与 momentum_breakout

**问题。** 两个现有扫描器各自捕捉了有价值但不同的事件路径，当前坏日、固定金额门槛和隐藏 RPS 排除使其不可比较。

**任务。**

1. B 输出可重叠的两个 `event_subtype`：
   - `compression_launch`：复用均线压缩、历史 sigma、首次启动、缩量后活动度扩张、收盘靠近最高；
   - `momentum_anomaly`：复用分板涨幅、冲高/长上影、近期脉冲和 5 日路径。
2. 删除 `momentum_breakout -> rps_stock_top20` 的隐藏排除耦合。股票可同时是 A 强趋势和 B 异动，不应互相删除。
3. 共享活动度：close-final 用当前量额相对 T-1 历史基线；intraday 用本任务真值卡的累计量额横截面百分位。
   固定 `amount_min` 只保留为可配置诊断，不再作主闸门。
4. 活动度优先 `turnover_ratio`，其次 `amount`；不得直接比较不同股本股票的 share volume。
5. `trend_embryo` 的小阳累积、涨停次数和单日最大涨幅作为 `path_context`，帮助解释“蓄势/加速/已过度延伸”，
   不作为第三个独立榜单。
6. 两种 subtype 分别在自身合格池内使用同量纲百分位排名，不强行合并成一个跨形态总分；UI 可合并展示并保留 subtype。
7. 使用共享 clean context；未知坏日、缺必需窗口或活动度缺失时按原因跳过。

**范围。** `mining/scanners/launch_burst.py`、`mining/scanners/momentum_breakout.py`、
`mining/scanners/trend_embryo.py` 的 v2 复用入口与专属测试；legacy 类可兼容保留。

**验收。**

```powershell
python -m unittest tests.test_launch_burst tests.test_momentum_breakout tests.test_event_fusion
```

专属断言：坏日不污染 rolling 窗口；T bar 不进入自身历史基线；A/B 可重叠；两个 subtype 不混分；
盘中累计量额按横截面口径工作；缺活动度时给出明确原因。

---

### 卡 5 — 能力 D：平台突破

**问题。** B 主要覆盖均线压缩后的起爆，尚未覆盖更宽的中期箱体突破。

**任务。** 新增 `base_breakout`，复用共享 context、活动度和趋势 profile。

| 条件 | 首版定义 |
|---|---|
| 观察窗 | `T-60..T-1`，T 不进入箱体定义 |
| 箱体宽度 | `max(high)/min(low)-1 <= 0.35`，同时输出 close 宽度 |
| 波动收缩 | 最近 10 日 `ATR/close` 与 close 宽度均低于前 40 日对应值 |
| 突破 | `close_T >= max(high_{T-60..T-1}) * 1.005` |
| 当日确认 | 涨幅至少 3%，收盘位于当日振幅上 25%；`high==low` 时标 path unknown，不做除零 |
| 活动度 | 使用共享 activity percentile，T 不进入自身历史基线 |
| 趋势底座 | `close > 当前 profile 的中长线 MA`，且该 MA 不下降 |

排名按 `close_box_width` 升序、`contraction_ratio` 升序、`activity_pct` 降序、`sec_code` 升序；
输出所有合格者，UI 默认显示前 20，不把 UI 截断写成选股定义。

本轮不删除未注册且永远返回空的 `cup_handle.py`：它没有运行时影响，且相关 backlog 正在被其他任务修改。
待工作区干净后另开清理卡，避免把死桩清理混入选股能力交付。

**验收。**

```powershell
python -m unittest tests.test_base_breakout tests.test_mining_pipeline
```

专属断言：T 日不泄漏进箱体；收缩与未收缩、真突破与长上影假突破有成对样例；盘中/收盘使用同一算法；
排名字段与 UI 一致。

---

### 卡 6 — 能力 E：逆势相对强度

**问题。** 绝对收益在弱市中会共同下压，现有 scanner 缺少持续的股/指相对强度序列。

**任务。** 新增 `counter_trend_rs`，主基准预先固定为 `000852`，并报告 `000300/000001/399001` 敏感性，
不得事后挑选最漂亮的基准。

硬闸门：

1. 基准近 20 日收益 `<0`；否则 E 空榜。
2. 个股近 20 日收益 `>0`，只“跌得少”不算逆势强。
3. `rs=adj_close/aligned_benchmark_adj_close`；`rs_T` 突破 `T-60..T-1` 历史上沿至少 0.1%。
4. `rs > MA20(rs)`，且 `MA20(rs) > MA20(rs).shift(10)`，排除单日脉冲。
5. 个股 `close > 当前 profile 可用的中长线 MA`，当日收阳。
6. 基准缺日或异常不前向填充，整只股票跳过并记录。

排名：先把 `rs_ret20`、`rs_breakout_pct`、基准下跌日分离度转成横截面百分位，再按
`0.4 / 0.35 / 0.25` 加权。输出全量合格者，UI 默认前 20。

**验收。**

```powershell
python -m unittest tests.test_counter_trend_rs tests.test_mining_pipeline
```

专属断言：基准上涨时空榜；个股 -5%/基准 -15% 仍不合格；缺基准日不填充；T 不进入 60 日历史上沿；
四基准敏感性只改变报告，不偷改主榜。

---

### 卡 7 — 面板与报告：A–E、强度 × 阶段、快照 × 定版

**问题。** `tab_scanner.py` 的候选 SQL、中文映射和字段列表硬编码；新 scanner 注册后不会自动成为消费者可见结果。

**任务。**

1. 建立单一 capability registry，声明 `strategy_id`、`capability`、`label`、`subtype`、`fields`、
   `sort_keys`、`supports_intraday`；面板与报告共同消费，避免维护两份白名单。
2. 正式区域按 A 强趋势、B 异动起爆、C 回调支撑、D 平台突破、E 逆势强度展示；legacy 放在折叠对照区。
3. 顶部固定显示 `mode`、`as_of`、`price_as_of`、`metadata_as_of`、`trend_profile`、`data_status`、
   `snapshot_source`、`snapshot_coverage`。`intraday_snapshot` 使用醒目的“盘中临时结果”标识。
4. 盘中 A–E 展示卡 M 的实时重算结果；`close_pending` 显示待定；`close_final` 只读正式持久化结果。
5. C 同时显示可筛选的 `强度档 × 阶段` 两列；支持一步筛出“强趋势 × 回调到位”等组合。
6. 每个能力先查运行状态再查候选，区分：未运行、运行失败、成功但 0 条、数据过旧/缺失；
   成功但 0 条时显示最近一次同 `strategy_id/version` 的出现日期。
7. 每榜显示实际排序字段、`reference_price`、`event_subtype/path_context`、数据跳过摘要；
   不显示 `fillable/entry timing/holding period/expected return`。
8. 保留现有 `compute_market_regime()`，文案统一为“当时市场背景”，只分组展示不拦截。
9. markdown/excel 正式报告只使用 `close_final`；若允许导出盘中结果，文件名和页首必须含 `snapshot_as_of` 与临时标记。
10. 收盘定版后刷新、退出重开、跨日重启三种路径均做真实页面验收，确认前一日不再显示快照价。

**范围。** `mining/streamlit_tabs/tab_scanner.py`、`mining/reports.py`、相关 UI/report 测试；
不修改 `app_panel.py` 和 `run_daily.py`。

**验收。**

```powershell
python -m unittest tests.test_mining_ui tests.test_mining_reports tests.test_selection_runtime
python -m unittest
streamlit run app.py
```

浏览器必须确认：A–E 五区、双创可见且北交所不可见、盘中/待定/定版三态、强度 × 阶段筛选、
四类空态、排序证据、市场背景及所有 as-of 字段。只跑单元测试不能标记 UI 验收完成。

---

## 全卡完成闸门

基线（2026-08-06 改动前）：`python -m unittest` 为 `Ran 153 tests ... OK (skipped=2)`。
每张卡完成后必须保持全量测试通过，测试总数只增不减；网络源测试不得进入单元测试。

> **【v2.3 修订】v2.2 交付后实测基线为 `Ran 205 tests ... FAILED (failures=2, skipped=2)`。**
> 测试数 153 → 205（+52），但闸门是红的。两个失败都在 `tests/test_mining_pipeline.py`：
> - `test_second_launch_selects_pullback_shrink_and_stop_setup` → `AssertionError: Lists differ: [] != ['600001']`
> - `test_execute_daily_pipeline_persists_second_launch_via_generic_scanner_branch`
>
> 原因是卡 3 把 `second_launch` 改接到 C 的共享状态机（需要 60 个 clean sessions + 已落库的 A 历史），
> 旧合成 fixture 构造不出这个前提，因此必然为空。这是**卡 3 的预期语义变更**，不是新引入的逻辑错误。
>
> **【v2.4 已裁决】** 用户于 2026-08-06 放行 `tests/test_mining_pipeline.py` 的这两个用例，
> 由 **R1 卡**迁移 fixture 使样例显式满足新的 A 资格。`2026-08-06-screening-v2-quality-review.md` §14.2
> 独立得出同一主张。
>
> **R 系列分阶段闸门（硬性，v2.5 以“测试身份”而非固定数量判定）：**
>
> | 卡 | 要求 |
> |---|---|
> | R0 | 依赖完整的项目解释器下无 import/environment errors；只允许下述 2 个已知失败，且失败身份与基线完全一致。测试总数 `N >= N0` |
> | R1 | **必须恢复 `OK`**；测试总数只增不减 |
> | R2 / R3 / R4 | 保持 `OK`；不得再触碰 `tests/test_mining_pipeline.py` |

完成标签只描述实现证据：

- `implemented`
- `locally-verified`
- `broadly-verified`
- `real-data-smoke-verified`
- `ui-verified`
- `partial / environment-blocked`

本任务不设置 `has_edge / fill_rate / net_return` 等交易有效性状态。若未来单独启动收益研究，另立任务书，
不得反向改变本任务的选股定义。

---

## 交付要求（每张卡）

1. 分支名与 commit hash；每卡一个 commit，只包含本卡文件。
2. 检查过的文件、修改的文件、未修改的禁止文件。
3. 验收命令实际输出，按 passed / failed / environment-blocked 分列。
4. 数据证据：`mode`、`as_of`、`price_as_of`、`metadata_as_of`、`trend_profile`、`data_status`、
   `universe_count`、`eligible_count`、`result_count`、`skipped_reason_counts`。
5. 真实库新旧结果有差异时给逐条件漏斗和代表性增删样例，不得只报最终命中数量。
6. 假设、限制和未验证项；明确区分定义验收、真实数据 smoke 与 UI 验收。
7. `Lessons`：列出本卡收到的纠正与验证有效的方法；无则写 `none`。

**禁止事项：**

- 不得修改 `run_daily.py`，**但 v2.5 第二部分 R3 明列的窄范围例外除外**；例外须执行 §7 的基线隔离与定向暂存。
- 不得运行宽范围历史回填；只允许卡 0 的单日/小范围修复。
- 不得 `git push`。
- 不得覆盖或回滚工作区中与本任务无关的用户改动。
- 不得把盘中快照写成正式收盘结果。
- 不得把固定命中数、66 图观感或实现完成状态写成收益有效性证明。

---

## 本轮已消除的未知项

- 使用场景：盘中与盘后都要筛选；不考虑交易时点和持有期。
- 市场范围：排除北交所，覆盖创业板和科创板。
- 历史策略：本轮不做大规模回填，使用统一自适应均线档位。
- 现有逻辑：保留有效因子并融合，不做全部推倒重来。

剩余未验证项只影响后续实现，不阻断本任务书：实时快照源在不同交易时段的字段完整率、当前 ST 元数据刷新成功率、
以及 66 张图是否已有稳定的人工正反例标签。实现卡 M/0M/2 时必须记录实测结果；若字段不足，按真值卡的降级状态处理，
不得临时改变产品范围。

---
# 第二部分：v2.2 交付实况与 R0–R4 返修（2026-08-06 晚；v2.5 二审）

## 派发入口（Terra 只从这里开始）

**阅读/执行顺序固定为：§6 → §7 → §2 → §3 → 回到 §7 完成基线与备份 → R0 → R1 → R2 → R3 → R4。**
§7 是开工检查的路由页，不是读完后直接动代码：先完成 §7 第 1–2 项，再读 §2/§3，随后返回完成其余检查。
若从 2026-08-06 的 R1 阻断恢复，顺序改为：**先读 §6 裁决 4 → R0.1 → 完成 R1 → R2 → R3 → R4**；
不得跳过 R0.1 后重新生成五股 canonical fixture 与全量回归。

**Terra 最小必读集合：** 仓库 `AGENTS.md` + 本任务书。第一部分不必通读旧卡，只需按 §7 路由读取
「当前工作区边界」「全卡完成闸门」「交付要求（每张卡）/禁止事项」四处通用边界；Terra 从本节进入。
`2026-08-06-screening-v2-quality-review.md`、Opus 原评审和聊天记录均已合并进本任务书，**不要求另读，也不得把它们
当作高于 v2.5 的第二套真值**。实现时按各 R 卡白名单读取其点名源码/测试即可，无须重新做仓库级规划或重复评审。

**【v2.5 二审修订】五项纠偏：**

1. A 的正式状态只保留 `continuation / fresh_breakout`；未满足 A 的股票不以“趋势成型中”混入 A 榜，
   近似合格者只进入诊断漏斗。两路径有明确优先级，不能让成熟趋势被重复标成“新晋”。
2. 质量门采用可执行的二维状态机；`clean` 的定义、覆盖不足时的状态、阈值优先级和 36% 有效率的
   预期全部写死，修复原稿内部矛盾。
3. 盘中与定版只要求**同输入函数等价**，不声称半日成交额等于全天成交额；盘中活动度必须标临时，
   数据源按整批统一选择，不得混池。
4. 正式能力定义版本统一升级为 `v2.5`，收盘定版按一个 batch 原子发布；不能继续把新定义写进 `v2.0`，
   也不能只靠逐策略 upsert 冒充完整定版。
5. A–E v2.5 不创建或回填 `outcomes`。本任务没有入场价/持有期真值，继续生成 20 日 outcome 会越过产品边界；
   旧策略历史 outcome 原样保留，只处理 replace 时的外键完整性。

**写入边界。** R0–R2 与 R4 历史回算一律在临时库副本完成；R3 只实现、测试和演练生产迁移。
没有用户在 R3 完成报告后的再次授权，不得迁移、清理或历史回算生产 `mining_mvp.db`。

## 0. 来源与编号约定

本部分合并两份独立材料：

1. `docs/superpowers/specs/2026-08-06-screening-v2-quality-review.md`（Codex 评审，HEAD `5a7b770`，20:53 落盘）。
2. Opus 侧独立只读实测（直连 `a_share_mvp.db` / `mining_mvp.db`，逐层调用实现函数打印诊断，不写库）。

两份材料在**数据质量分层、强趋势双路径、盘中/定版等价性、replace 持久化语义**四个方向独立得出同向结论，
可信度高。差异部分在 §1 逐条标注。

**编号约定（与评审文件的差异，执行者必须注意）：**

| 本任务书 | 评审文件 | 说明 |
|---|---|---|
| R0 | R0 | 数据真值与交易日历，**增加**复权层行级化、`change_pct` 解依赖、`metadata_stale` 语义 |
| R0.1 | 2026-08-07 阻断裁决增补 | provider 全零停牌占位窄修正；只在 R1 阻断恢复路径执行 |
| R1 | R1 | 强趋势重定义，**增加**新晋主升的防反弹底线、量能健康度、停复牌规则 |
| R2 | R2 的前半 | 口径统一与盘中/定版等价性 |
| R3 | R2 的后半 | 持久化与定版完整性 |
| R4 | R3 | 正反例回归、全量集成与文档 |

拆分 R2 的原因：评审的 R2 同时改 `selection_runtime / selection_context / tab_scanner / universe / db`，
违反「一卡一 commit、可独立 revert」与「同文件域串行」的派卡约束，且等价性与持久化的失败模式不同、
回滚代价不同。拆开后 R2 与 R3 都仍触及 `tab_scanner.py`，因此**必须串行，不得并行**。

首次执行顺序：**R0 → R1 → R2 → R3 → R4**。当前阻断恢复顺序：
**R0（已完成）→ R0.1 → 完成 R1 → R2 → R3 → R4**。全串行，每卡一个 commit；R0.1 必须独立提交。

---

## 1. 两份材料的一致、补充与分歧

### 1.1 完全一致（可直接执行，无需再讨论）

- 数据质量门把少数个股坏行升级成全市场坏日，是 P0 阻断。
- 强趋势当前是「长期多头结构 + 相对排名」，不是「当前仍然强」，系统性偏向旧赢家。
- 盘中 A 不传 benchmark、close-final scanner 传，两条路径打分口径不同。
- `save_candidates()` 只 upsert 不删除，收盘定版无法真正覆盖旧结果。
- 全量测试红闸门的两个失败源于卡 3 的语义变更，不是新 bug。

### 1.2 评审发现、Opus 上一轮漏掉的（三项，全部已复核确认）

**(a) 复权层按「股票」而非「行」作废——影响面远超评审估计。**
`mining/adjusted_prices.py:104`：

```python
ordered["adjustment_valid"] = ~codes.isin(code_reasons)   # 代码级，不是行级
```

只要一只股票在整段历史中出现过一行坏值，它的**全部**复权 OHLC 被置 NaN。实测：

| 口径 | 受影响股票数 | 占比 |
|---|---:|---:|
| 含 `2026-07-13`（全零日） | 5212 / 5220 | **99.85%** |
| 排除 `2026-07-13` | 176 / 5220 | 3.4%（532 行坏值导致） |

也就是说：一旦 R0 放宽交易日历却没有同步把复权层改成行级，全市场复权序列会整片 NaN。
这是本轮最危险的耦合点，必须与 R0 的日历放宽**在同一张卡内**完成。

**(b) 快照名称列缺失 `名称`，并把 `<NA>` 当成有效名。**
`tab_scanner.py:125` 只找 `["name", "sec_name"]`；AkShare 快照实际列名为 `名称`。
名称列缺失时 `:144` 写入 `pd.NA`；`universe.py:117`：

```python
normalized_name = str(name).strip() if name is not None else ""
```

`pd.NA is not None` 为真，`str(pd.NA)` = `"<NA>"`，非空 → 被当作有效快照名。后果是三重的：
UI 显示 `nan`；ST 名称过滤静默失效；`metadata_stale` 保护被错误解除。

这条解释了此前测到的一个矛盾现象：**盘中有榜（保护被假名解除，但 ST 没过滤干净），
收盘定版无榜（保护生效，整站清空）**。两个模式的产品行为完全相反，而根因是同一个字段。

**(c) 定版完整性按日期判断，V2 策略可能永远不运行。**
`tab_scanner.py:374` `_persisted_candidate_dates()` 只 `SELECT DISTINCT trade_date FROM candidates`，
`:706` 据此计算 `missing_dates`。只要该日存在**任意** legacy 策略候选，该日就被视为已持久化，
新增的 V2 策略即使一次都没跑过也不会被补算。这与实测「`candidates` 表中六个 formal strategy 0 行」互相印证：
即使 R0/R1 修好，不修这条，V2 结果仍然永远落不了库。

### 1.3 Opus 实测发现、评审未覆盖的（四项）

**(a) `change_pct` 空值让 D 与 momentum_anomaly 恒为 0 行。**
`base_breakout.py:62` 与 `momentum_breakout.py:267` 硬要求最近 121 根 bar 的 `change_pct` 连续非空：

```python
raw_change = pd.to_numeric(indexed.get("change_pct"), errors="coerce")
if not all(series.notna().iloc[-required:].all() for series in (close, high, low, raw_change)):
    skipped["price_window_missing"] += 1
    continue
```

实测 `2026-07-17` 全部 5175 行为 NULL、`2026-07-31` 5178 行中 2769 行为 NULL，
2–7 月区间全部 5202 个代码至少有 1 个 NULL，合计 101,977 空行。
`2026-07-31` 实测 `skipped={'price_window_missing': 4959}`，D 为 0 行。
该字段可由复权后 close 直接推导，不应作为硬依赖。评审的 R0 未覆盖此项，**修完日历 D 仍然是 0 行**。

**(b) 双路径分歧已量化。** `2026-07-31` 同一份数据，传/不传 benchmark：
打分因子 `rps_recent/rps_prior/near_high/separation` vs 少一项 `separation`；
**Top20 重合 14/20 且顺序不同；强趋势档各 11 只，仅 7 只相同**。
面板看到的 A 榜与落库的 A 榜是两份名单。

**(c) 历史并集污染已量化。** `strategy_runs` 显示 `2026-07-13` 单日 25 次运行
（momentum_breakout / rps_concept_top20 / rps_stock_top20 / second_launch / trend_embryo / true_leader 各 25，
launch_burst 16），`2026-06-05`、`2026-05-20` 各 8 次。upsert-never-delete 使这些日期的候选集是历次并集。
同时 legacy `second_launch` v1.0 在 `2026-08-03/04/05` 有 20/20/16 行，而
`tab_scanner.py:390` `_load_persisted_candidates()` 的 SQL **没有 version 过滤**，v2 一旦落库将新旧混显。

**(d) 活动度口径盘中/收盘不是同一个量。** `mining/event_activity.py`：
`close_final` 用 `current / mean(baseline)`（真值卡要求 `median`）；
`intraday_snapshot` 把 `activity_ratio` 置 `pd.NA`，改用**绝对活动度原值**排名，
并按 `activity_source`（amount / turnover_ratio）**分池**各算各的百分位。
评审 §9 只覆盖了 benchmark，未覆盖活动度；这是等价性的第二个独立破口。

**(e) C 的历史资格当前为 0。** C 的资格读**已落库**的 A 候选历史，而 A 现在 0 行；
修复后不必等满 60 日才运行，但 N<60 时对更早 A 资格存在漏判，必须显示 coverage。
实测 `2026-07-31` 强制放行后 C 的跳过原因为
`five_session_structure_missing: 4954, never_in_a_qualified_pool: 62`，与判断一致。

### 1.4 分歧（Opus 不同意评审的两处，理由如下）

**(a) `usable_with_quarantine` 阈值 `98% / 0.5%` 中的 0.5% 太紧。**
实测坏行占比分布是**双峰且中间完全空白**的：

| 类别 | 实测坏行占比 |
|---|---|
| 正常日（含 08-03/04/05） | 0.06% – 0.4% |
| 真实坏日（05-19 / 06-05 / 07-13） | 45.5% / 63.7% / 100% |

0.4% 与 45% 之间没有任何一天。0.5% 的阈值距离观测上界只有 0.1 个百分点，
一次略差的供应商回补就会把整个市场关掉——这正是本轮事故的形态。
**建议改为 2%**（对观测上界留 5 倍余量，距最近的真实坏日仍有 22 倍距离）。

同时把「覆盖」与「有效率」拆成两个独立轴，评审把两者混在一个比值里：

- 覆盖：`stock_rows / median(近 20 个可用日 stock_rows) >= 0.95`（抓「供应商只回了一半」）
- 有效率：`valid_rows / stock_rows`，`>= 0.98` → `usable_with_quarantine`；
  `0.80–0.98` → `partial_missing`；`< 0.80` → `known_bad_session`

评审提的 `valid_rows / recent_clean_median >= 0.98` 会把「多上市了几只新股」也算进分子，
在正常日实测为 100.35%–100.39%，看似安全，但它同时承担了覆盖和质量两件事，语义不清。

**(b) 评审 §7.4 的 `weakening` 状态与能力 C 的 `破位失效` 重复。**
真值卡已确立 A（强度档）与 C（阶段）**正交**。再在 A 里引入 `weakening`，
同一只股票会同时拿到 A 的 `weakening` 和 C 的 `回调中`，产生互相矛盾的两个标签。
**【v2.5 二审修订】A 正式结果只保留 `continuation / fresh_breakout`；未合格与近似合格进入诊断漏斗，
不持久化为 A。`weakening` 并入 C 的 `破位失效`**，由 C 单独负责阶段语义。

---

## 2. 对评审 §17.3 六个问题的答复（产品决策，已定案）

**Q1 是否同意把「强趋势」改为绝对状态，前 20% 降为排名带？**
**同意。** 旧的配额排名会强制每天有输出，即使当天没有任何股票符合定义——这正是用户抱怨的来源。
`rank_band=top20_pct` 只作排序带标签，弱市允许 0 只强趋势。

**Q2 是否同意 A 包含 `continuation` 与 `fresh_breakout` 两条路径？**
**同意，但 `fresh_breakout` 必须加防反弹底线。** 评审给的条件
（近期突破 + `rps10/rps20>=0.80` + MA20 向上，不要求 MA60 排列）
无法区分「新晋主升」和「暴跌后的反抽」：一只从 100 跌到 30 的股票反弹到 35，
同样可以满足 rps10 高、创 20 日新高、MA20 上翘。
**【2026-08-07 裁决 4 修订】** 使用 `close >= prior_close_high_20` 证明新的收盘突破，另以
`close >= 0.85 × rolling_high_60` 排除中期结构仍深度受损的反抽；二者仍须与共享绝对门及
A1→A2 互斥优先级共同约束。原 `0.98 × rolling_high_60` 已被真实正例证明会误杀重启型领涨股，不再生效。

**Q3 已确认停牌日，MA/RPS 是否统一用前收盘维持市场日历？**
**是，但有四条边界：**
1. 只有存在明确停牌证据，或供应商占位同时满足“无成交 + 正的 `pre_close` + 无矛盾价格”时，
   才用**前一有效复权收盘**维持价格序列；分别标 `confirmed_halt` / `provider_halt_placeholder`，
   二者都令 `tradable_today=false`，不得把任意坏行伪装成停牌；
2. `volume/amount` **保持缺失，既不填 0 也不填前值**——填任何值都会污染活动度基线；
3. 窗口内停牌占比 `> 20%` 时该股在该能力上跳过（否则停牌半年复牌的股票会拿到一条假均线）；
4. 连续停牌 `> 10` 个交易日后复牌的股票标 `resumption_watch`，复牌后 5 个交易日内
   **不参与 A/E 的 RPS 横截面**（其区间收益与连续交易股票不可比），但仍可进入 B。
   若仓库没有可验证的停复牌来源，只能使用 `provider_halt_placeholder`，不得声称“已确认停牌”。

第 3、4 条是评审未覆盖的真实市场行为，重大资产重组停牌复牌后的连续涨停会直接扭曲 RPS 排名。

**Q4 `usable_with_quarantine` 是否接受 `98% / 0.5%`？**
**不接受 0.5%，改 2%；并把覆盖与有效率拆成两个独立判据。** 理由见 §1.4(a)。
二维状态的合并优先级、`clean` 定义和边界值见 R0；原稿“有效率 0.36 判 partial”是笔误，
按已裁决阈值必须判 `known_bad_session`。R0 先跑真实分布并报告敏感性，但 Terra 不得自行移动阈值。

**Q5 `rps_prior` 保留为成熟度证据，还是以 ≤10% 权重参与排名？**
**从打分中完全移除，降为展示字段 `strength_age`。** 理由：`rps_prior` 度量的是「过去强了多久」，
与「当前谁最强」方向相反，正是 688627/301571 这类旧赢家被保送的直接原因。
若要表达持续性，应写成**闸门**（如「已连续 N 日通过结构门」）而非分数权重——
真值卡硬约束 #1 就是先闸门后排名，把持续性塞进分数会让名次无法用显示字段复核。

**Q6 candidate replace 时已有 outcomes 的迁移规则？**
**【v2.5 二审修订】正式 A–E 不定义 entry/holding horizon，因此不得新建或回填 `outcomes`。**

1. `backfill_outcomes()` 必须排除正式 `version='v2.5'` 候选；旧版本/legacy outcome 原样保留；
2. replace 删除正式候选前，先在同一事务删除其意外残留 outcome（或用已验证的 `ON DELETE CASCADE`）；
   不得按“候选仍存在”保留可能基于旧参考价计算的派生 outcome；
3. 历史重新定版仍须显式 `--allow-historical-refinalize`，按**可用交易日**而非自然日判断历史范围，
   并记录 batch 审计；此开关不等于授权生产写入；
4. 事务失败整体回滚，不允许出现「删了没写」的空洞。

### 追加决策（Opus 提出，用户 2026-08-06 已确认，全部生效）

**D1. R3 完成且用户审阅迁移盘点前，禁止对生产库执行任何 `close_final` 落库。**
否则会在旧 replace 语义下先写一批 v2 候选，R1 改定义后又留下一批语义不同的残行。
R0–R2 的真实数据验证一律用只读探针 + 临时库副本。
**执行细则：** R0–R2 期间如需真实数据验证，先 `copy mining_mvp.db` 到临时路径再连接；
任何调用 `save_candidates()` 的路径在这三张卡中都不得对 `mining_mvp.db` 生效。

**D2. `tests/test_mining_pipeline.py` 的两个 `second_launch` 用例放行给 R1 卡。**
迁移 fixture，使样例在**当前 C 评估日之前**至少一个 close-final 交易日显式满足新的 A 资格；
不得要求回调中的股票在当前日仍通过 A，也不得回退新语义或用 `skip`/`expectedFailure` 绕过，
不得改动该文件的其余用例。分阶段闸门见第一部分「全卡完成闸门」的 v2.5 裁决表。

**D3. 五只样例（688141/688627/301571/300996/601858）只作回归断言，不得进入实现代码。**
与评审一致，此处重申并纳入每张卡的禁止事项。

---

## 3. 算法设置更新：能力 A 的新定义（真值卡增补）

> **状态：v2.5 结构与首版数值均为实现合同。**
> 状态/排名分离、共享绝对门、A1/A2、A/C 分工和排名因子集合不得改写；数值按 §3.10 做分布审计，
> Terra 本轮只报告病态阈值，不自行调参。任何数值调整另交 Opus/Codex 与用户复核，防止执行者在五股上拟合。

本节替代第一部分「自适应趋势档位」中「长均线多头排列即强趋势」的口径。
`trend_profile` 保留，但降级为**窗口长度自适应与覆盖率检查**的依据，不再单独充当强趋势闸门。

### 3.0 统一计算定义

- `T` 是本 batch 的当前 bar；收盘模式为定版日线，盘中模式为同一批快照构造的临时 bar。
- `retN = adj_close_T / adj_close_{T-N} - 1`，按最近 N 个**可用市场交易日**计算；停牌占位可维持价格，
  但缺失/坏行不得压缩成“个股自己的 N 根”。
- `rolling_high_N = max(adj_high)`，窗口含 T，用于衡量中期结构损伤；
  `prior_close_high_20 = max(adj_close_{T-20..T-1})`，明确排除 T，用于判断 T 是否以收盘价完成近期突破。
  `ATR20` 用清洗后复权 OHLC 和前一复权收盘计算标准 True Range。
- `rpsN_pct` 在同 batch、具有完整 N 日收益且未处于 `resumption_watch` 的预闸门股票池计算，
  不得先筛 A 再算 RPS。缺失值不参与分位，排序并列用 `sec_code` 稳定打破。
- 流动性原值为 T-20..T-1 的 `median(amount)`，只统计 `valid_trade`，至少 16 个观测；
  再在同一预闸门池计算 `liquidity_pct`。T 不进入自己的流动性基线。

### 3.1 状态与排名彻底分离

```text
strength_tier ∈ { continuation, fresh_breakout }  ← 只有正式 A 候选才有值
rank_band     ∈ { top20_pct, remainder }          ← A 合格集内部的排名带
```

未通过 A 的股票不持久化 `strength_tier=不合格`；原因进入逐条件漏斗。原稿 `趋势成型中` 没有可执行定义，
本轮删除该正式标签，可在 UI 诊断区显示“接近合格 + first_failed_gate”，但不得与 A 榜混排。

弱市允许 0 只强趋势。由于共享门含 `rps20_pct >= 0.80`，A 数量天然有约 20% 全市场上限；
原稿“普遍强市可超过 20%”与该硬门数学矛盾，现删除。`top20_pct` 只表示 A 合格集内部前 20%，不保送任何股票。
排名只回答「已确认强趋势的股票里谁更强」，不回答「市场里是否存在强趋势」。

### 3.2 共享绝对强度门（A 的必要条件，全部满足）

| # | 条件 | 含义 |
|---|---|---|
| 1 | 对所走路径历史充分、T 为 `valid_trade`、通过股票池白名单与 ST 过滤 | 数据与当前可筛选性 |
| 2 | `close > MA10 > MA20` | 短中期结构仍在控制中 |
| 3 | `MA20 > MA20.shift(5)` | 中期方向向上 |
| 4 | `ret10 > 0` 且 `ret20 > 0` | **绝对**近期方向，非相对 |
| 5 | `rps20_pct >= 0.80`（§3.0 预闸门池横截面） | 相对强度 |
| 6 | `(rolling_high_20 - close) / ATR20 <= 2.5` | 距近期高点未受结构损伤 |
| 7 | §3.0 的 `liquidity_pct >= 0.40` | 流动性 |

条件 4 与条件 5 构成**绝对下限 + 相对上限的双闸门**：单用相对分位会在普跌市里
把「跌得少」的股票选成强趋势；单用绝对收益会在普涨市里放进太多平庸股。

条件 6 用 ATR 归一化而非固定百分比，原因是 20cm 与 10cm 板的同一百分比含义完全不同：
双创股 8% 的回撤只是一天的正常波动，主板股 8% 已是明显破位。
该值是**非负距离**；实现必须保存 `near_high_distance_atr = (rolling_high_20-close)/ATR20` 后再判
`<= 2.5`。不得先取负号再复用到闸门；负号只可在排名字段 `near_high_raw` 中表示“越大越近”。
ATR20 在具备 20 日价格历史的 batch 预闸门池覆盖率 `>=0.98` 时使用；单只股票 ATR 缺失即
`missing_atr20`，不得个股静默换公式。
只有 ATR 输入在整批系统性不可用时，才整批回退固定阈值：主板 `close/rolling_high_20 >= 0.92`，
双创 `>= 0.88`，并记录 `near_high_profile=board_fallback`。同一 batch 禁止两套近高公式混排。

评审原稿的 `close/rolling_high_20 >= 0.92` 单一阈值与旧实现的「长窗口高点 85%」相比是重大改进，
此处只是把它做成板块与波动率自适应的形式。

### 3.3 A1 `continuation`（成熟趋势延续）

共享门 + `MA20 > MA60` + `MA60 >= MA60.shift(10)`。
更长均线（MA90/MA120/MA200）只输出为 `maturity_evidence` 展示字段，**不再作为闸门**。
该路径至少需要 70 个最新连续可用市场日；不足只跳过该路径，不得按个股改用另一套均线。

### 3.4 A2 `fresh_breakout`（新晋主升）

共享门 +

- **`close >= prior_close_high_20`**（T 的收盘价完成最近 20 个先前可用市场日的收盘突破）
- **`close >= 0.85 × rolling_high_60`**（只作中期结构未深度损伤的防反弹底线）
- `rps10_pct >= 0.80` 且 `rps20_pct >= 0.80`
- 不要求 MA60/MA90/MA120 完成多头排列

两道门分工固定：前者证明“现在发生了新的收盘突破”，后者排除仍深陷旧高之下的深跌反抽。
`0.85` 沿用本产品此前的长窗口结构底线，不是围绕 601858 单点拟合；不得改为
`close >= 0.98 × rolling_high_20/60`，因为包含 T 的盘中高点会把当日冲高后正常收盘的新突破误杀，
而 60 日旧高 98% 会系统性漏掉经历充分换手后重新启动的当前领涨股。

**路径互斥优先级：先判 A1；A1 通过即标 `continuation`，只有 A1 未通过而本节全部通过才标
`fresh_breakout`。** 这样成熟趋势不会因为仍在 60 日高点附近而被重复叫作“新晋”。

五股锚点固定为本地库截至 `2026-08-05` 收盘的冻结数据切片：300996 与 601858 是正例，
应由 A2 入选；688141/688627/301571 是反例，不得进入正式 A。R0.1 后必须重新导出五股的 canonical
复权 bars 与逐条件值到 fixture；不得手填价格或移动阈值凑标签。若重新生成后仍与锚点冲突，停止并报告
逐条件值与数据来源差异，交回用户裁决。

### 3.5 A 与 C 的分工（消除标签冲突）

| 轴 | 归属 | 取值 |
|---|---|---|
| 强度档 | A | `continuation` / `fresh_breakout`（仅合格股票） |
| 阶段 | C | 延伸中 / 回调中 / 回调到位 / 再启动 / 破位失效 |

评审 §7.4 的 `weakening` **并入 C 的 `破位失效`**，A 不再产生该标签（理由见 §1.4(b)）。

C 的资格来源相应改为：**T 之前近 60 个已定版可用交易日内，曾进入同一正式版本 A（任一路径）**。
盘中临时 A、当前 T 的 A 和 legacy A 都不能反向创造当日 C 资格。
注意这依赖已落库的 A 历史，冷启动处理见 R4。

### 3.6 排名因子（闸门内）

移除 `rps_prior` 权重。`near_high_raw = -((rolling_high_20-close)/ATR20)`，因此越接近高点越大；
无 ATR 的整批回退使用 `close/rolling_high_20`。基准与股票同批次可用时：

```text
rps10_pct 0.30 + rps20_pct 0.30 + near_high_pct 0.20 + separation_pct 0.20
```

基准不可用或最近 20 日不足 3 个基准下跌日时，设置 `ranking_profile=no_benchmark`，
**全市场统一**移除 `separation_pct` 并把其余权重重归一为 0.375/0.375/0.25——
不允许出现「一部分股票有分离度、一部分没有」的混合排名。
最终稳定排序键固定为 `(score DESC, rps20_pct DESC, sec_code ASC)`。
`strength_age` 定义为截至 T 连续通过任一 A 路径的可用交易日数（上限 60），`maturity_evidence` 只展示。

### 3.7 活动度描述标签（不作交易结论）

A 不设硬活动度门（避免过度约束），但可输出：

```text
amount_health = median(amount_{T-19..T}) / median(amount_{T-59..T})
```

当 `close` 创 60 日新高且 `amount_health < 0.6` 时打事实标签 `activity_contraction_at_high`。
原稿把成交额收缩直接解释成“典型派发”属于交易含义外推；本任务只展示事实，不判断派发/吸筹。
该标签只影响展示与排序提示，不改变 `strength_tier`。盘中 T 的成交额未走完，只显示
`amount_health_so_far` 与 `activity_provisional=true`，不得提前生成 close-final 的收缩标签。

### 3.8 停复牌与新股（Opus 追加，见 §2 Q3）

- `confirmed_halt/provider_halt_placeholder`：前一有效复权收盘维持序列，`volume/amount` 保持缺失，
  `tradable_today=false`；无法确认时不得升级成 `confirmed_halt`；
- 窗口内停牌占比 `> 20%` → 该能力跳过该股，计入 `halt_ratio_exceeded`；
- 连停 `> 10` 日复牌 → `resumption_watch`，复牌后 5 个交易日不参与 A/E 的 RPS 横截面；
- 上市不足窗口 → 计入 `new_listing`，与 `insufficient_history` **分开计数**。
- `limit_up_streak` 不是 R1 硬验收。若保留，必须使用按日期生效的主板/双创/ST/新股规则与 tick 舍入；
  上市前 5 个交易日等无价格涨跌幅限制阶段或规则元数据缺失时输出 `limit_state_unknown`，不得硬算“涨停”。
  规则依据至少核对[上交所 2026 年交易规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)
  与[深交所创业板交易规定](https://www.szse.cn/www/investor/index/update/t20200729_580056.html)；
  不得把当前比例反向套到历史日期。

### 3.9 可解释性与离榜追溯（Opus 追加）

评审 §17.2 记录了「301571 在 16:50 快照未入 A，用户此前快照看到过」。
盘中榜单随价格变化是正确行为，但用户必须能看到**为什么它走了**。要求：

- A 的每一行携带 `first_seen_as_of`（当前连续 A 资格开始日）与**逐条件通过明细**；
- 未入 A 的诊断行携带 `first_failed_gate` 与全部 gate 值；
- 「今日曾入选、当前掉出」只允许放在按 `(trade_date, definition_version)` 隔离的进程/Session 临时状态，
  明确标 `session_only=true`；跨进程不保证追溯，跨日必须清空，绝不写正式 `candidates`。

### 3.10 阈值冻结流程（强制）

§3.2–3.4 的数值是**v2.5 工程定义，不是收益结论**。R1 必须：

1. 使用现有本地数据的最近至少 60 个可用交易日（实在不足才降到 20，并标 `insufficient_audit_window`）
   打印每个条件的真实分布、分位数与按月稳定性；不得联网补历史；
2. 输出逐条件漏斗：每个条件各自淘汰多少只、累计剩多少只；
3. 报告冻结五股 fixture 在每个条件上的实际取值与通过/失败；
4. 对非零数值阈值报告 ±10% 扰动时命中集变化率，检查是否卡在密集峰；`retN>0` 这类方向门不扰动；
5. 代码常量使用本节数值，不得由运行时样本自动学习或覆盖。

若分布显示阈值处于病态位置（落在密集峰、±10% 即导致命中集大幅翻转，或淘汰率接近 0%/100%），
R1 仍按合同实现，但标记 `needs-threshold-review` 并提交证据；**不得由 Terra 当场调参**。

**明确禁止**：因为「某只样例没进来 / 不该进来」而单点调参。
样例是回归断言，不是拟合目标；把阈值调到刚好让五只股票各归其位，
得到的是一个只在这五只股票上正确的定义。

---

## 4. 返修卡

所有 R 卡沿用第一部分的「交付要求」与「禁止事项」，并追加：

- 不得为了让某个能力有输出而放宽定义闸门；空榜是允许的结果，静默降级不是。
- 不得把五只样例写进实现代码的任何分支或常量。
- **R3 完成并经用户审阅 migration/dry-run 报告前，不得对生产库执行 `close_final` 落库**（决策 D1）。

**写入白名单（其余仓库只读；后一张卡接手同文件前必须已有前卡 commit）：**

| 卡 | 允许写入 |
|---|---|
| R0 | `mining/data_quality.py`、`adjusted_prices.py`、`selection_context.py`、`universe.py`、`trend_factors.py`、`scanners/base_breakout.py`、`scanners/momentum_breakout.py`、`tests/test_data_quality.py`、`test_adjusted_prices.py`、`test_selection_context.py` |
| R0.1 | `mining/data_quality.py`、`mining/adjusted_prices.py`、`tests/test_data_quality.py`、`tests/test_adjusted_prices.py`；仅当上下文窗口断言确有需要时可改 `tests/test_selection_context.py` |
| R1 | `mining/trend_factors.py`、`scanners/strong_trend.py`、`watchlist.py`、`scanners/second_launch.py`（仅兼容输出投影）、`tests/test_strong_trend.py`、`test_trend_factors.py`、冻结五股 fixture；以及 §6 点名的 `test_mining_pipeline.py` 两个用例 |
| R2 | `mining/selection_context.py`、`event_activity.py`、`selection_runtime.py`、`universe.py`、`streamlit_tabs/tab_scanner.py`、`capabilities.py`、A–E scanner adapter、相关现有测试与新 `tests/test_capability_parity.py` |
| R3 | R3「范围」点名文件、新批次持久化模块、`tests/test_candidate_persistence.py`、必要的 `test_mining_ui.py`/`test_mining_reports.py` 定向断言 |
| R4 | `tests/fixtures/screening_v2/`、相关正反例测试、`docs/superpowers/specs/current-opportunity-selection-strategy.md`、面板文案文件、`app_panel.py`（仅 `SCREENING_BASE_DIR` 测试接缝） |

不得修改 `requirements.txt`、数据库源文件、旧 fixture 标签或测试发现配置来制造绿灯；确需超白名单先写 `BLOCKED.md`。

---

### R0 — 数据真值、交易日历与复权层行级化（P0）

**目标。** 少量个股坏行不得删除全市场有效交易日，也不得作废整只股票的复权历史；
同时保留对系统性坏日的 fail-closed。

**根因（均已复核）。**

- `mining/data_quality.py:_classify_summary()` — 任意一行坏值 → 整日 `partial_missing`；
  `clean_stock_trade_dates()` 只收 `clean`，导致 `2026-08-03/04/05` 全部不可见。
- `mining/adjusted_prices.py:104` — `adjustment_valid` 按 `sec_code` 判定，
  一行坏值作废整只股票全部历史。实测含 `2026-07-13` 时命中 **5212/5220（99.85%）**，
  排除该日仍有 176 只股票因 532 行坏值被整段作废。
- `mining/scanners/base_breakout.py:62`、`momentum_breakout.py:267` — 硬要求 121 根连续非空
  `change_pct`；实测该字段 101,977 行为 NULL，D 与 momentum_anomaly 恒为 0 行。
- `mining/universe.py` — `metadata_stale` 被写进 `data_status`，各能力
  `if context.data_status != "ready"` 一刀切拦截；`stock_info.updated_at=2026-04-22`
  恒久早于最近交易日，等于每天全站停摆（实测 A 返回 `skipped={'data_unavailable': 5016}`）。

**改法。**

1. **先建立唯一键与行状态。** 每个 `(trade_date, sec_code)` 只能有一行；完全相同的重复行可折叠并计数，
   冲突重复行一律 `invalid_price`，不得靠 SQL 顺序任选。行状态互斥且优先级固定：
   - `valid_trade`：有限正 OHLC、关系合法、`volume>0`、`amount>0`；
   - `confirmed_halt`：有可追溯停牌来源；
   - `provider_halt_placeholder`：无成交、`pre_close>0`，且为全零/平价占位而无矛盾价格；
   - `invalid_price` / `invalid_activity` / `missing`。
   `price_usable_rows` 只含前三类；后两类隔离。两类 halt 都不可进入 T 日候选，量额保持缺失。
2. **覆盖基线无循环依赖。** `reference_stock_rows` 是 T 前最近 20 个“非人工 known-bad 且有股票行”的
   日期之 distinct code 数中位数；不足 5 日则 `coverage_baseline_unavailable`，不得猜阈值。
   `coverage_ratio = distinct_stock_codes / reference_stock_rows`；
   `usable_ratio = price_usable_rows / distinct_stock_codes`。
3. **二维状态合并表（边界包含关系固定）：**
   - 人工 known-bad、无股票行、无 `valid_trade`，或 `coverage_ratio < 0.80` / `usable_ratio < 0.80`
     → `known_bad_session`；
   - 否则 `coverage_ratio < 0.95` 或 `usable_ratio < 0.98` → `partial_missing`；
   - 否则若隔离行数为 0 → `clean`；有隔离行 → `usable_with_quarantine`。
   因此有效率 0.36 的 2026-06-05 对标样例必须是 `known_bad_session`，不是原稿写的 `partial_missing`。
4. 「可用交易日历」改名/别名为 `usable_stock_trade_dates`，只收 `clean` 与
   `usable_with_quarantine`；同时返回 `(trade_date, sec_code, row_status, reason)` 隔离清单。
5. **处理顺序强制且只读原始事实：** 原始行分类 → 市场日分类 → 排除系统坏日并隔离坏行 →
   在剩余序列建复权边 → 计算收益/均线。原稿“先建复权再剔除坏日”会让全零日进入复权乘积，已纠正；
   R0 不“修复”或覆盖原始 OHLC。
6. **复权层行级/边级隔离：** halt 占位沿用前一有效复权收盘且不产生新复权边；无效边将序列切段，
   当前筛选只使用最新连续有效片段，不能跨断点计算 MA/RPS。输出
   `valid_adjusted_bars / invalid_edges / coverage_ratio / latest_valid_segment_start`。
7. 停复牌按 §2 Q3/§3.8；未确认坏行不前向填充。窗口 halt 占比与 `new_listing` 在各能力依赖窗口中计算。
8. `change_pct` 统一由相邻可用市场日的复权 close 推导；库内 `change_pct` 只作原始展示，
   不作计算回退，也不进入窗口完整性硬门，避免同一能力两套真值。
9. **状态轴拆分：**
   - `price_status ∈ {ready, partial, unavailable}`；`clean/usable_with_quarantine → ready`，
     `partial_missing → partial`，`known_bad_session/基线不可判 → unavailable`；
   - `metadata_as_of`、`metadata_fresh`、`metadata_coverage`、每行 `st_status_known` 单列；
   - `metadata_fresh=False` 时使用最后已知名称做 best-effort 过滤，结果标 `metadata_provisional=true`，
     不得声称 ST 全覆盖；价格结果可展示但联合验收不能称“元数据完整定版”；
   - `data_status` 兼容值只映射自 `price_status`，不得再装入 `metadata_stale`。各能力只对其依赖状态 fail-closed。

**范围。** `mining/data_quality.py`、`mining/adjusted_prices.py`、`mining/selection_context.py`、
`mining/universe.py`、`mining/trend_factors.py`（仅覆盖率检查接口）、
`mining/scanners/base_breakout.py`、`mining/scanners/momentum_breakout.py`、专属测试。
**不改** `mining/db.py`、`app_panel.py`、`run_daily.py`。

**验收命令。**

```powershell
& $screeningPy -m unittest tests.test_data_quality tests.test_adjusted_prices tests.test_selection_context
& $screeningPy -m unittest
```

**防作弊验收断言。**

- 合成正常日含 5–7 个占位/坏行时进入可用日历，只有对应代码被隔离；真实 2026-08-03–05
  的 5,195+ 行结果放 smoke，不写进依赖生产 DB 的单元测试。
- 有效率 0.36 的合成日（对标 `2026-06-05`）与全零日均判 `known_bad_session`；
  覆盖 0.90、有效率 0.99 判 `partial_missing`；覆盖 0.99、有效率 0.995 判 `usable_with_quarantine`。
- 冲突重复 `(date, code)` 不得重复计数或被静默任选。
- **单只股票在历史中有 1 行坏值时，只作废该行/该边，其余复权 bar 仍可用**
  （直接针对 99.85% 那条缺陷）。
- 未确认坏行不得前向填充；已确认停牌可用前收盘维持且 `tradable_today=false`。
- 20 日窗口确实对应最近 20 个可用市场交易日，且不跨 invalid edge。
- `change_pct` 全 NULL 的合成数据下 `base_breakout` 仍能产出候选。
- 元数据 4 个月前 + 价格齐全 → 榜单非空且带 `metadata_note`；价格缺失 → 空榜且
  `price_status == 'unavailable'`。

**真实数据 smoke（必须贴进完成报告）。**

1. 可用交易日数量由 146 变为多少、末日是否为 `2026-08-05`；
2. 覆盖率与有效率在 192 天上的**真实分布直方图/分位表**（阈值冻结依据）；
3. 因行级化而恢复的股票数（对照 176 / 5212 两个基线）；
4. `base_breakout` 在 `2026-08-05` 的 `skipped_reason_counts`；
5. 五股 canonical fixture 的 SHA-256、日期范围与数据来源，供 R1 固定回归使用。

---

### R0.1 — provider 全零停牌占位修正（R1 恢复前置，P0）

**目标。** 修正 R1 实测暴露出的行状态漏判：数据源用全零 OHLC/量额表示停牌时，既不能把它当成
`invalid_price` 切断复权窗口，也不能把它当作可交易 bar。R0.1 是对已完成 R0 的窄修正，单独提交后
重新生成 canonical fixture；不得重做 R0、改生产库或扩大到行情回填。

**冻结事实。** 300996 的 `2026-07-27/28/29` 三行是 OHLC、量额全零而 `pre_close=12.93`，
`2026-07-30` 恢复有效交易且 `pre_close=12.93`。公司公告明确其自 7 月 27 日停牌，故这三行是
provider 停牌占位，不是三天真实零价，也不是可任意手填的缺失行情。全市场同日无任何 `valid_trade`
的全零数据仍是系统坏日，不能借本规则洗成可用日。

**改法。**

1. 在重复冲突检查之后，将“OHLC 全部严格等于 0、`volume/amount` 为 0 或空、`pre_close>0`，且无任何
   矛盾正价格”的单股行分类为 `provider_halt_placeholder`，reason 固定为
   `provider_zero_no_activity_placeholder`；保留原始全零值供审计，`tradable_today=false`。
2. 市场日分类优先保留 fail-closed：该日 `valid_trade_rows==0` 时仍判 `known_bad_session`；因此
   `2026-07-13` 一类全市场异常不能被停牌占位规则救活。
3. 复权层仅对已归类的 `provider_halt_placeholder` 使用前一最新有效复权收盘构造计算 bar，
   OHLC 四价同值、量额缺失，且**不生成复权边**；找不到前一有效收盘则隔离，不得猜价。
4. 恢复交易的首个有效 bar 继续使用数据源报告的 `pre_close` 建边；多日占位不得切段、重复复权或
   被计作个股独有交易日。原始数据库和冻结源切片均保持只读。
5. 从 sandbox 重新生成五股 canonical fixture、逐条件值与 SHA-256；禁止人工改 300996 的三行价格。

**范围。** 仅本节白名单。实现若发现需要改变 `SelectionContext` 公共接口，先更新 `BLOCKED.md`，
不得借 R0.1 顺手重构。

**验收命令。**

```powershell
& $screeningPy -m unittest tests.test_data_quality tests.test_adjusted_prices tests.test_selection_context
& $screeningPy -m unittest
```

**防作弊验收断言。**

- 单股孤立全零停牌占位被标为 `provider_halt_placeholder`，不可作为 T 候选，但 10/20 日市场窗口连续；
- 同样形态覆盖全市场且 `valid_trade_rows==0` 时仍为 `known_bad_session`；
- 三日连续占位只沿用前收盘，不切复权段、不制造收益；复牌首日按原始 `pre_close` 延续；
- 无前一有效价格、含矛盾正价格或有成交的全零行不得前填；
- fixture 只能由 sandbox 数据生成，源库 SHA-256 不变。

---

### R1 — 强趋势分层重定义（P1，产品价值最高的一张卡）

**目标。** 用绝对近期状态定义强趋势，同时覆盖成熟趋势与新晋主升；
把「前 20%」降级为排名带。完整口径见 §3。

**根因。** `mining/scanners/strong_trend.py` 现行逻辑为
「统一 profile 长均线多头排列 + 长窗口高点 85% + 合格池内百分位 + 机械取前 20% 标强趋势」，
缺少 `ret10>0`/`ret20>0` 绝对方向门、MA20 斜率门、近高回撤门；
`rps_prior` 权重 0.25 直接奖励已走弱的旧赢家。实测五只样例结果见评审 §2.2。

**改法。** 严格按 §3.1–3.10 实现：

1. `strength_tier` 与 `rank_band` 拆成两个独立字段；A 榜只含 `continuation/fresh_breakout`，
   未通过者只进诊断漏斗（§3.1）。
2. 严格实现 §3.0 的窗口/RPS/流动性口径与共享绝对强度门 7 条（§3.2）；
   `near_high_distance_atr` 必须以非负距离参与 `<=2.5` 闸门。现有实现先取负号再 `.le(2.5)`
   会让远离高点的股票几乎全部通过，属于必须修复的合同 bug。
3. A1 `continuation`（§3.3）；A2 `fresh_breakout` 使用
   `close >= prior_close_high_20` 与 `close >= 0.85 × rolling_high_60` 两级结构门，
   并按 A1→A2 的优先级输出唯一状态（§3.4）。
4. 长均线（MA90/MA120/MA200）降级为 `maturity_evidence` 展示字段，不再作闸门；
   `trend_profile` 只用于窗口长度自适应与覆盖率检查。补回 P200 档遗漏的 MA120。
5. 移除 `rps_prior` 打分权重，改为展示字段 `strength_age`（§3.6）。
6. 输出中性事实字段 `amount_health` / `activity_contraction_at_high`，不得写“派发”结论（§3.7）。
7. `weakening` 不在 A 中产生，并入 C 的 `破位失效`；C 的资格来源改为
   「近 60 个可用交易日内曾进入 A 的强趋势档」（§3.5）。
8. 合格股票输出逐条件通过明细与 `first_seen_as_of`；未合格诊断输出 `first_failed_gate`（§3.9）。
9. 弱市允许 0 只强趋势；禁止任何「至少产出 N 只」的兜底逻辑。
10. **迁移 `tests/test_mining_pipeline.py` 的两个 `second_launch` 用例**（用户已放行，见 D2）。
    fixture 必须构造出「该股在当前评估日之前的近 60 个 close-final 可用交易日内确实进入过 A」这一前提，
    再验证回调/缩量/止跌形态。迁移后这两个用例必须**因为满足新语义而通过**，
    不得用 `skip`、`expectedFailure`、放宽断言或回退 `second_launch` 的资格来源来变绿。
    该文件的其余用例一行都不许动。
11. **仅在 `mining/scanners/second_launch.py` 补齐兼容输出投影**，不得参与 C 资格、阶段或排序：
    - `ma_proximity = min(abs(dist_ma10), abs(dist_ma20))`，只取有限值；两者均缺失时输出 `null`；
    - `stop_signal = (state == PULLBACK_STATE_RETRIGGER)`，它只是对当前已确认“再启动”阶段的兼容别名；
    - `flag_strategies = "strong_trend"`，如需新增独立血缘字段须留到后续卡，不在本次扩域。
    禁止重新查询 `trend_embryo/true_leader` 来伪造旧来源。被授权的第一个用例可将旧断言
    `"trend_embryo,true_leader"` 改为 `"strong_trend"`；这是来源语义迁移，不是放宽候选断言。

**范围。** `mining/trend_factors.py`、`mining/scanners/strong_trend.py`、
`mining/watchlist.py`（仅阶段词表与 A 资格来源对齐）、
`mining/scanners/second_launch.py`（仅第 11 条三个兼容字段，不改筛选/排序/version/default params）、
`tests/test_mining_pipeline.py`（**仅上述两个用例**）、专属测试。
**不改** `mining/db.py`、`mining/selection_runtime.py`、`tab_scanner.py`。

**本卡数据边界。** 受决策 D1 约束，本卡的真实数据验证只能连 `mining_mvp.db` 的临时副本，
不得对生产库执行 `close_final` 落库。

**验收命令。**

```powershell
& $screeningPy -m unittest tests.test_strong_trend tests.test_trend_factors
& $screeningPy -m unittest
```

**防作弊验收断言。**

- 弱市合成数据允许 0 只强趋势，且不触发任何兜底。
- `top20_pct` 只改变 `rank_band`，不改变 `strength_tier`。
- `688627` / `301571`：5/10/20-bar 收益为负 → 不得因长期结构进入强趋势。
- `688141`：`ret10<0` 且距 20-bar 高点 89.05% → 不得因短期反弹进入强趋势。
- 冻结的 `2026-08-05` 五股 fixture：`300996/601858` 通过 A2；另外三只不进入 A。
- `near_high_distance_atr` 为非负值；构造距 20 日高点超过 `2.5×ATR20` 的股票必须失败，
  防止负号 bug 让近高闸门失效。
- **新增反例：** 构造「从高点跌 70% 后 10 日反弹 30%、突破此前 20 日收盘高点但仍低于 60 日高点 85%」的合成股，
  断言其**不能**通过 `fresh_breakout`（验证防反弹底线真的生效）。
- 五只样例不出现在实现代码的任何分支或常量中（提交前 grep 断言）。
- 输出每只股票的逐条件值与首个失败原因。
- **全量 unittest 必须回到 `OK`**（D2 分阶段闸门），且测试总数不低于 task 0 记录的 `N0`。
- 迁移后的两个 `second_launch` 用例中，fixture 里被判定为 C 候选的股票必须在**较早日期**有可验证的
  A close-final 资格；不得断言它在当前回调日仍是 A。
- 添加兼容字段前后，`second_launch` 的候选代码与顺序完全相同；`flag_strategies` 必须真实反映
  `strong_trend` 资格来源，不得保留历史扫描器名称。

**真实数据 smoke（必须贴进完成报告）。**

1. §3.10 要求的分布/稳定性/扰动审计、逐条件漏斗、五股逐条件取值；
2. 新旧定义在同一天的增删对照（新进榜 / 掉榜各列代表性样例及其决定性条件）；
3. A1 与 A2 各自的命中数量分布；
4. 任何病态阈值只报告 `needs-threshold-review`，本卡不得自行调整；
5. 修正停牌占位、ATR 符号和 A2 合同后的参考 smoke 约为 A=104/5034（A1=60、A2=44），
   只用于发现数量级突变，**不是固定验收数量**；真实运行必须提交当次逐门漏斗与差异名单。

---

### R2 — 口径统一与盘中/定版等价性（P1）

**目标。** 相同价格输入必须产生完全相同的 A–E 结果；消灭「必需输入可选、缺省换一套打分」的双路径。

**根因。**

- `mining/scanners/strong_trend.py` — `benchmark_returns` 可选，缺省静默摘掉 `separation_pct`
  并重归一权重；`tab_scanner.py:566` 调用时不传。实测 Top20 重合 14/20、强趋势档 11 只仅 7 只相同。
- `mining/event_activity.py` — 盘中排绝对值且按 `activity_source` 分池，收盘排 `mean` 比值，
  两种模式不是同一个量；真值卡要求的 `median` 两边都没实现。
- `mining/selection_runtime.py` — 盘中传 `allow_missing_activity=True` 与 `clean_dates`，收盘都不传。
- `tab_scanner.py:125` — 快照名称列不含 `名称`；`universe.py:117` 把 `<NA>` 当有效名，
  同时造成 UI `nan`、ST 过滤失效、`metadata_stale` 保护被错误解除。
- 现有模式等价测试只比较复权 OHLC，未比较 A–E 候选、字段、排序与状态。

**改法。**

1. **依赖上移并分轴状态化**：`build_selection_context()` 一次装载股票 bars、可用日历、基准收益与活动度基线，
   输出 `price_status / benchmark_status / activity_status / metadata_fresh / trend_profile`；
   不得把 benchmark/activity 缺失伪装成 `price_status` 失败。每个能力在 registry 声明 required/optional 依赖：
   A 可统一降为 `ranking_profile=no_benchmark`，E 缺 benchmark 必须空榜并报原因，B/D 缺统一活动度源时按其定义 fail-closed。
2. 每个能力对外只保留一个函数签名 `evaluate_<capability>(context)`；
   删除所有「必需输入设为可选、缺省切换打分方式」的分支；扫描器 `run()` 内部调用同一函数。
3. `event_activity` 统一为 `ratio = current / median(T-20..T-1)`，T 不进基线；source 按**整批**选择：
   优先 amount，覆盖不足时整批回退 turnover ratio；不得一部分股票用 amount、一部分用 turnover 后混排。
   `intraday_snapshot` 的 current 是截至快照的部分日累计，必须输出 `activity_provisional=true`；
   原稿“分子分母同进度缩放”不成立，禁止声称盘中数值等于收盘数值。只有输入同一根完整 bar 时才要求结果等价。
4. `selection_runtime` 模式差异只允许存在于「T bar 来源」「provisional 标记」「是否允许持久化」；
   股票快照必须来自同一次 loader 调用，记录 `observed_at`、source 和 coverage。指数没有同批快照时，
   整批设置 `benchmark_status=unavailable` 并按依赖矩阵处理，禁止混入前收盘指数冒充同一时点。
5. 补 `SelectionContext.trend_profile` 的实际填充（当前恒为 `None`）。
6. **快照名称修复**：支持 `名称/股票简称/证券简称` 等实际列名；用 `pd.isna()` 判缺失；
   显式拒绝 `"nan"/"<NA>"/"None"` 哨兵字符串；有效快照名称覆盖不足时不得声称元数据已刷新。
7. **面板模式文案与跨日规则**（评审 §13）：`intraday_snapshot` → 「盘中临时筛选 A–E」；
   `close_pending` → 「收盘待定筛选 A–E」；`close_final` → 「收盘定版筛选 A–E」；
   `data_unavailable` → 「数据不可用」且禁止用旧榜单顶替。盘中榜继续不写正式候选与状态历史。
   Session 内“今日曾入选”必须按 `(trade_date, definition_version)` 隔离；日期变化立即清空。
   请求历史日期时只能读完成的 close-final batch，绝不回退到内存/缓存快照。
8. **等价性测试必须真的跑两遍**：同一份合成数据，一次以 `intraday_snapshot`（当日 bar 由快照构造）、
   一次以 `close_final`（同一根 bar 作定版日线），断言相同候选集合、相同
   `strength_tier/rank_band/state/event_subtype`、相同评分字段、相同排序，
   **只允许 `mode/as_of/source/provisional` 元数据不同**。另设真实盘中 partial bar 测试，明确允许其结果随收盘改变。
9. context 生成稳定 `input_fingerprint`（排序后 canonical bars + 日历 + 基准 + definition version）；
   A–E 同一轮必须共享该指纹，Streamlit 同 fingerprint rerun 不得重复抓取或重复构建 context。

**范围。** `mining/selection_context.py`、`mining/event_activity.py`、`mining/selection_runtime.py`、
`mining/universe.py`（名称归一）、`mining/streamlit_tabs/tab_scanner.py`、
A–E 六个 formal scanner 的入口签名、`mining/capabilities.py`（只加依赖声明）、专属测试。
**不改** `mining/db.py`；`run_daily.py` 的批次接线留给 R3。

**验收命令。**

```powershell
& $screeningPy -m unittest tests.test_event_activity_v2 tests.test_selection_runtime tests.test_capability_parity
& $screeningPy -m unittest
```

**防作弊验收断言。**

- grep 断言 A–E 各自只有一个对外选股函数，且无 `= None` 的必需输入。
- 相同 snapshot/final bars 时 A–E 候选、字段、排序完全相同（元数据字段除外）。
- 活动度在两种模式下对同一输入返回同一 `ratio`。
- mixed-source 输入不得混池：要么整批选择一个 source，要么该能力显式 unavailable。
- 盘中 partial amount 带 `activity_provisional=true`，测试不得强求它与全天 amount 结果相同。
- 快照列名为 `名称` 时 `sec_name` 正确解析；值为 `pd.NA` 时**不得**被当作有效名，
  且 `metadata_fresh` 保持 `False`。
- ST 名称过滤对真实快照字段生效（用真实列名的合成快照测试）。

**真实数据 smoke。** 把同一份冻结 bars 分别送入面板 evaluator 与 scanner adapter，A 榜逐行相同，
贴出 diff（应为空）；不得拿真实盘中 partial bar 与另一份收盘 bar 比较后要求为空。

**性能。** 记录冷启动 `build_selection_context`、A–E 总评估和同 fingerprint 热 rerun 耗时。
当前实测约 10s / 10–14s，主因 `evaluate_trend_structure_history()` 是 stocks × dates 的 Python 双层循环。
硬闸门是每个 batch 只构建一次 context、热 rerun 不重复 fetch/build、相对基线不退化 >20%；
可低风险向量化则处理，否则单列待办——**不得为提速改变定义**。

---

### R3 — 原子定版、持久化覆盖与版本隔离（P1）

**目标。** 同一输入的全部正式能力作为一个 `close_final` batch 原子发布；精确覆盖旧结果，空榜可审计，
旧版本/半成品 batch/跨日快照均不能混入正式面板。

**根因。**

- `mining/db.py:303` `save_candidates()` 只 select→update/insert，**从不 delete**。
  实测 `2026-07-13` 单日 25 次运行，候选集为历次并集；`2026-06-05`、`2026-05-20` 各 8 次。
- `tab_scanner.py:374` `_persisted_candidate_dates()` 只按 `trade_date` 判断，
  该日存在任意 legacy 候选即视为已持久化，V2 策略永远不被补算
  （实测六个 formal strategy 在 `candidates` 表中 0 行）。
- `tab_scanner.py:390` `_load_persisted_candidates()` 的 SQL **无 `version` 过滤**；
  唯一索引为 `(strategy_id, version, trade_date, sec_code)`，v1.0 与 v2.0 同表共存。
  实测 legacy `second_launch` v1.0 在 `2026-08-03/04/05` 有 20/20/16 行，会被渲染进正式 C 区。
- `mining/watchlist.py:844` `_load_a_qualified_pool()` 读 `candidates WHERE strategy_id='strong_trend'`，
  无 `version` 与 `mode` 过滤。

**改法。**

1. **版本先行。** 在 `mining/capabilities.py` 建唯一 `SCREENING_DEFINITION_VERSION='v2.5'`，
   formal registry 每项携带 version；A、B×2、C、D、E 的 scanner/candidate/reader/test 全部从 registry 取值，
   禁止散落硬编码 `'v2.0'`。旧 v2.0 不删除、不与 v2.5 混读。
2. 新增可迁移的 `selection_batches`（或等价 batch manifest）：至少包含
   `batch_id, trade_date, definition_version, mode, observed_at, price_as_of, input_fingerprint,
   status, error_msg, created_at, completed_at`；`strategy_runs` 增加 `batch_id/mode/input_fingerprint`。
   migration 必须兼容已有库，先在副本验证，不能靠删库重建。
3. `save_candidates(..., replace=True)` **不足以保证定版**；实现单一 unit-of-work
   `persist_close_final_batch(context, capability_results)`：
   ① `BEGIN IMMEDIATE` + 建 `building` batch；② 为 registry 中全部正式 strategy 写 success/empty run；
   ③ upsert 本轮候选；④ 找出各 `(strategy_id, version, trade_date)` stale IDs，先删其 outcome 再删候选；
   ⑤ 所有正式 strategy 均完成后标 batch `complete`；⑥ 一次 commit。任一步失败全部 rollback，
   再用独立事务记录 failed batch（不得留下部分正式结果）。同 `(trade_date, version, input_fingerprint)`
   已有 complete batch 时必须幂等复用，不新增重复 run。
4. **定版完整性按 batch 判断**：同一 `trade_date + v2.5 + input_fingerprint` 的全部 formal definitions
   均 success/empty 才是 `close_final`。空榜是成功；存在任一 missing/failed 或不同 fingerprint 就是
   `close_pending/failed`，读取端不得拼接多个 batch。
5. `run_daily.py` 的正式 A–E 分支必须构建一次 shared context、评估一次、调用上述 batch API；
   legacy 流程可保留原路径。该窄改动是第一部分冻结区的 v2.5 例外，但必须保留当前 dirty diff。
6. 所有正式读取路径从 registry 取当前 version，并只读 `selection_batches.status='complete' AND mode='close_final'`：
   `tab_scanner.py`、`reports.py`、`watchlist.py` 及全仓所有 `FROM candidates` 消费者。
   同日多个 complete batch 时只选 `completed_at/batch_id` 最新者，并经 `run_id→batch_id` 读取，绝不拼接。
   `_load_a_qualified_pool()` 只读 T 之前 60 个 v2.5 complete batch 的 `strong_trend`。
7. `intraday_snapshot/close_pending` 一律不落 `candidates/strategy_runs` 正式历史；请求跨日或重启后，
   若目标日没有 complete batch，就显示“未定版/不可用”，不得复活任何旧快照或拿前一榜单顶替。
8. **正式 v2.5 不生成 outcome**：`backfill_outcomes()` 明确排除 registry formal v2.5；replace 只为外键安全
   删除意外残留 children。不得新增 entry/holding 计算。
9. **历史并集数据不自动清理**：提供只读盘点脚本，列出每个
   `(strategy_id, version, trade_date)` 的运行次数与行数，在完成报告中给出受影响日期清单，
   以及旧 v2.0/legacy/不完整 batch。R3 只在临时副本演练；用户审阅报告并授权后才可迁移生产库。

**范围。** `mining/db.py`、`mining/capabilities.py`、`mining/streamlit_tabs/tab_scanner.py`、
`mining/reports.py`、`mining/watchlist.py`、`run_daily.py`（仅 formal batch 接缝）、
`mining/backtest.py`（仅排除 formal v2.5 outcome）、可新增一个批次持久化模块、专属测试。
`run_daily.py/mining/backtest.py` 均为脏文件：以 §7 保存的内容与 SHA-256 为不可覆盖基线，在其上做最小叠加；
无须等待原 owner 交接。提交时只定向暂存本任务增量。若某一改动与基线发生同一行语义冲突，禁止 stash/回滚/重写基线；
把该接缝标为 `commit-packaging-blocked`，保留工作树与证据，其余不冲突的实现、测试和后续卡继续完成。

**验收命令。**

```powershell
& $screeningPy -m unittest tests.test_candidate_persistence tests.test_mining_ui
& $screeningPy -m unittest
```

**防作弊验收断言。**

- 同一 batch 输入连跑两次幂等；新 fingerprint 的第二次结果集更小时，v2.5 旧行必须消失。
- 第 1/中间/最后一个 capability 分别故意抛错时，旧 complete batch 仍可读，新 batch 不可见且无部分候选。
- 某日已有 legacy 候选但 V2 策略从未运行时，**该日必须被识别为需要补算**。
- 任一 capability 空榜与未运行可区分；只有全部 success/empty 才完成 batch。
- v1.0/v2.0/v2.5 同日共存时，正式面板只取当前 registry v2.5 complete batch。
- `intraday_snapshot` 运行后 `candidates` 表行数不变。
- `backfill_outcomes()` 不为 formal v2.5 创建记录；replace 删除候选时无外键残留。
- 日期切换并清空 Session 后，无 complete batch 的昨日绝不显示昨日快照。

**真实数据 smoke（临时副本）。** 输出受影响 triple/运行次数、migration 前后 schema、同日 v2.0/v2.5 隔离，
以及最新可用日 v2.5 六个 formal strategy 的 run/候选数；不写生产库。

---

### R4 — 正反例回归、C 历史覆盖与文档（P2）

**目标。** 固化可复跑的正反例；补齐 C 的历史资格覆盖（不是把当前空榜误称死锁）；把真值文档对齐到 A–E。

**改法。**

1. **可重复正反例集（机器验收）**：以冻结五股的本地行情切片及合成边界样例为唯一强制 fixture，
   每条记录 `as_of/source/data_hash/expected_path/关键指标/失败条件`。66 图远端 artifact 仅作为用户后续人工视觉校准集；
   没有本地图片和稳定人工标签时记 `human-calibration=not-provided`，**不阻断 R4，也不降低 `broadly-verified`**。
   不得凭图名或模型观感伪造 `human_label/labeler`；以后收到标签包时另做校准报告，未经用户裁决不得自动改 v2.5 阈值。
   新增成对测试：
   fresh breakout / 破位失效 / 弱市 0 强趋势 / 停牌不删市场日 / 反抽不得进 fresh_breakout。
2. **C 的历史覆盖**：C 在第一个 A complete batch 后即可对随后日期产生候选，不存在“必须等满 60 日才运行”的死锁；
   但最近 60 日未回算会造成旧趋势资格漏判。要求：
   - 显示 `a_history_coverage=N/60`；N<60 时结果可产生但标 `bootstrapping/partial recall`，
     **不得表现成「今天没机会」或压制已有候选**；
   - 允许一次性用历史可用交易日逐日回算 A 并落 `close_final` 作冷启动——
      这**不属于**被禁止的「大范围历史行情回填」，因为它只读已有 bar、不拉网络数据；
   - 回算日期必须各自拥有 A 所需前置窗口；不足历史的日期记 skipped，不凑满；
   - 先在临时副本 dry-run，输出日期数、空榜数、候选数、input fingerprint 与预计写入量；
     用户再次授权后才可在生产库按 R3 batch 语义执行，并记录审计；不生成 outcomes。
3. **文档**：更新 `docs/superpowers/specs/current-opportunity-selection-strategy.md`
   （当前仍描述 `momentum_breakout` / `rps_stock_top20` / legacy universe，已不是真值），
   至少覆盖：A–E 能力定义、股票池与 ST 名称来源、行/市场日质量、三种运行模式、
   强趋势路径与状态、持久化与降级边界。
4. **真实浏览器三态验收（必须隔离生产库）**：允许在 `app_panel.py` 增加唯一测试接缝
   `SCREENING_BASE_DIR`（未设置时行为不变），指向含四个 DB 副本的任务临时目录；禁止直接以仓库根目录启动验收。
   确认盘中/待定/定版三态文案、A–E 五区、强度 × 阶段筛选、四类空态、排序证据、所有 as-of 字段。
   启动前后重算生产 DB SHA-256，必须完全不变。

**验收命令。**

```powershell
& $screeningPy -m unittest
$env:SCREENING_BASE_DIR = (Resolve-Path 'output/screening-v2-work/ui-sandbox').Path
& $screeningPy -m streamlit run app.py
```

**防作弊验收断言。**

- 定向测试与全量 unittest 全部通过（受决策 D2 影响，见下）。
- 输出逐条件漏斗与代表性增删样例。
- 不以固定命中数、五股硬编码或收益表现证明定义正确。
- 明确区分定义正确性、真实数据 smoke 与收益有效性。
- 五股本地行情切片与合成边界 fixture 必须可复跑；66 图未提供时只报告
  `human-calibration=not-provided`，不得声称人工视觉校准完成，但不把机器交付标成 partial。

---

## 5. 联合验收（R0–R4 全部完成后一次性给出）

1. 依赖完整解释器的全量 unittest 完整输出与 `N0→Nfinal`。
2. 最新可用交易日的 A/B/C/D/E 行数与各自 `skipped_reason_counts`。
3. 同一冻结输入的面板 evaluator 与 scanner adapter 的 A 榜 diff（必须为空）。
4. 同输入 snapshot/final 等价性测试，以及真实 partial snapshot 允许变化的反向测试。
5. 五只样例在新定义下的逐条件取值与最终归属。
6. 新旧定义的全市场增删对照与逐条件漏斗。
7. C 的 `a_history_coverage=N/60` 与回算 dry-run 计划。
8. 受历史并集污染的日期清单，以及是否需要用户授权迁移/回算/清理。
9. v2.5 complete batch manifest：六个 formal strategy 同一 fingerprint，旧版本/failed batch 不可见。
10. `outcomes` 表在 formal v2.5 运行前后均无新增行。

只有第 3/4/9/10 项通过、第 5 项符合冻结标签，且全量测试无环境错误，才可标记 `broadly-verified`。
榜单是否「好看」由用户在 66 图上人工指认，属于交付后的校准环节，不属于本轮机器验收范围；
本任务书全程不产出 `has_edge / fill_rate / net_return` 类结论。

---

## 6. 用户裁决记录（2026-08-06；2026-08-07 增补，已生效）

本节是执行者开工前必读的授权边界。产品裁决已定案，原“待确认项”按以下原则关闭：

- `run_daily.py/mining/backtest.py` 的现有 diff 是不可覆盖的用户基线，采用留档、最小叠加和定向暂存；
  无须原 owner 先 commit/stash。
- 66 图是后续人工视觉校准材料，不是可重复机器真值；缺少本地标签包不阻断 R4。
- 项目解释器优先在任务目录内自修复；只有无法取得 bootstrap Python 或依赖安装连续失败，才属于环境阻断。

### 裁决 1 — 放行 `tests/test_mining_pipeline.py`（范围受限）

**结论：放行。** 授权范围：

| 项 | 内容 |
|---|---|
| 授权卡 | **仅 R1**。R0、R2、R3、R4 均不得修改该文件 |
| 授权用例 | 仅 `test_second_launch_selects_pullback_shrink_and_stop_setup` 与 `test_execute_daily_pipeline_persists_second_launch_via_generic_scanner_branch` |
| 授权方式 | 迁移 fixture，使样例在 C 评估日前的一个 close-final 日期显式满足新的 A 资格 |
| 明确禁止 | `skip` / `expectedFailure` / 放宽断言 / 回退 `second_launch` 的资格来源 |
| 其余部分 | 该文件其他用例与公共 fixture 仍为禁止修改范围 |

**分阶段闸门：** 在依赖完整、无 import errors 的解释器下，R0 只允许 `failures==2` 且失败名单与基线一致；
**R1 必须恢复 `OK`**；R2–R4 保持 `OK`。

选择放行而非保留红闸门的理由：一个长期存在的 known failure 会让后续每一张卡都失去
「全量测试通过」这个唯一的客观锚点，之后任何新增回归都会被淹没在「反正本来就有 2 个红」里。

### 裁决 2 — §3 算法口径按 v2.5 二审版冻结

**结论：冻结结构与本轮首版数值，真实分布只做敏感性审计。** Opus 三处修订保留，二审再消除
`forming` 未定义、A1/A2 重叠、横截面池/窗口不明确与“强市超过 20%”数学矛盾；以 §3.0–§3.10 为唯一真值。

| # | 修订 | 理由 |
|---|---|---|
| 1 | `fresh_breakout` 追加 `close >= 0.98 × rolling_high_60` | **已被裁决 4 替代**；保留本行只记录历史决策，不得执行 |
| 2 | `weakening` 并入 C 的 `破位失效`，不留在 A | 否则同一股票会同时拿到 A「弱化」与 C「回调中」两个矛盾标签 |
| 3 | 质量阈值 `0.5%` → `2%`，覆盖与有效率拆成两轴 | 实测坏行占比双峰（0.06–0.4% vs 45–100%），0.5% 距观测上界仅 0.1pp |

冻结边界见 §3 开头：Terra 不得自行调数；病态阈值以 `needs-threshold-review` 交回，不得围绕五股拟合。

### 裁决 3 — D1/D2/D3 三条追加决策全部生效

见 §2「追加决策」。D1 经二审收紧为：R3 完成且用户审阅 migration/dry-run 报告后才能生产写入。
D2 的 A 资格必须发生在 C 评估日前；D3 继续禁止五股硬编码。

### 裁决 4 — R1 阻断处理（2026-08-07，已生效）

| 阻断 | 裁决 | 禁止路径 |
|---|---|---|
| `second_launch` 缺三个旧消费者字段 | **放宽 R1 白名单**，只允许在 `mining/scanners/second_launch.py` 按 R1 第 11 条投影 `ma_proximity/stop_signal/flag_strategies` | 不得查询旧扫描器、改变 C 资格/阶段/排序、保留虚假旧来源或删除断言 |
| 300996 三行 `invalid_price` 导致窗口断裂 | **先执行 R0.1**，把可验证的 provider 全零停牌占位纳入行状态与复权连续性，再从 sandbox 重生 fixture | 不得手填 OHLC、改源库、删正例标签或把全市场零价日洗成可用日 |
| 601858 `close/rolling_high_60=0.8626` 未达旧 A2 0.98 | **保留正例，修订 A2 合同**为 `close>=prior_close_high_20` 且 `close>=0.85×rolling_high_60` | 不得修改真实价格、把 601858 改负例、只为单股把阈值调到 0.8626 附近 |
| 现实现近高 ATR 值先取负再判 `<=2.5` | **R1 必修合同 bug**：闸门使用非负 `near_high_distance_atr`，排名才可取负 | 不得在符号 bug 未修时用扩大后的 A2 合同跑正式 smoke |

**裁决理由与后果。**

1. 兼容字段是接口投影，候选集前后必须完全相同；若复刻 `trend_embryo,true_leader`，会把已经取消的
   资格来源重新耦合进 C，并向用户展示错误血缘。
2. 300996 是数据状态建模不足，改 fixture 等于伪造行情，改标签则会把停牌股系统性误判为历史不足；
   R0.1 让同类停牌占位都得到一致处理，同时保留全市场坏日 fail-closed。
3. 601858 的原始行情没有质量异常。把它改负例会把“贴近两个月旧高”偷换成“当前强趋势”；单独放宽
   60 日门又不足以证明近期突破。两级结构门同时保留当前突破与防深跌反抽，预计扩大 A2，故必须用
   逐门漏斗、ATR 反例和新旧名单审计控制误报。
4. 裁决修改的是被真实证据证伪的合同，不改变 `definition_version=v2.5`；R1 继续原 commit，
   R0.1 单独在其之前提交。若 Git 已有未提交 R1 改动，先保持原位，不得 stash/回滚；只分卡暂存。
5. 恢复执行前在现有 `output/screening-v2-work/BLOCKED.md` **追加**本裁决日期、任务书 SHA-256 和
   `resolution=approved`；保留原阻断证据，不得删除或改写历史记录。R0.1/R1 各自完成后再追加 commit 与验收结果。

---

## 7. 开工前检查清单

执行者按序确认；条件失败时只停止确实依赖该条件的动作，其余只读、实现与可运行测试继续：

1. **规则与工作区 task 0：** 已读仓库 `AGENTS.md`；本任务书第一部分只读「当前工作区边界」
   「全卡完成闸门」「交付要求（每张卡）/禁止事项」，第二部分先读 §6/本节，再按派发入口读 §2 与 §3。
   运行 `git status --short`，
   把完整输出写入 `output/screening-v2-work/baseline-status.txt`；禁止修改、stash、回滚或顺手整理白名单外文件。
2. **脏文件基线隔离：** 在编辑前复制 `run_daily.py/mining/backtest.py` 当前内容到
   `output/screening-v2-work/preexisting/`，并保存两文件的 `git diff --binary`、文件 SHA-256 与 diff SHA-256。
   这些材料定义“用户基线”。R3 可以直接叠加白名单内最小修改，无须原 owner 交接；完成后必须逐文件证明基线语义仍在。
   只暂存任务新增行/块，禁止 `git add run_daily.py mining/backtest.py` 整文件暂存。若同一行冲突导致任务增量无法与基线隔离，
   记录 `commit-packaging-blocked`，保留证据并继续不冲突工作，不得擅自选择任一方覆盖。
3. **解释器 task 0（仓库内自修复）：** 依次尝试可用的 Python 3.11/3.12（`python`、`py -3`、执行器自带 Python）
   作为 bootstrap；不要使用已损坏的仓库 `.venv`。若候选不能通过
   `import pandas, requests, streamlit, tabulate`，就在 `output/screening-v2-work/venv` 创建隔离 venv，
   用其 `python -m pip install -r requirements.txt`，然后把该解释器绝对路径固定为 `$screeningPy`。
   不改系统 Python、不改 `requirements.txt`，同一路径失败最多重试 3 次。确无 bootstrap Python 或依赖安装仍失败时，
   在 `BLOCKED.md` 记录命令与原始错误；此时可以继续源码审阅，禁止宣称测试通过。
   当前二审实测的 `python` 缺失、旧 `.venv` 失效和 bundled Python 缺包只用于解释为何必须执行此步，不能当新基线。
4. **测试基线：** 在依赖完整解释器上执行 `& $screeningPy -m unittest`，记录测试总数 `N0`、skip 与完整失败身份。
   合法起点是**无 import/environment errors，且只有本任务书「全卡完成闸门」点名的两个
   `tests/test_mining_pipeline.py` second_launch 失败**；
   其他失败先分类为 `preexisting-test-drift`，保留原输出并只阻断依赖该断言的完成声明，不得改无关测试制造绿灯。
5. **数据 sandbox：** 创建任务专用 DB 副本，至少复制 `mining_mvp.db/a_share_mvp.db`；R4 浏览器 sandbox
   再包含 `ths_concept.db/etf_mvp.db`。记录每个源文件大小/SHA-256；R0–R4 smoke、migration、历史回算只连副本。
   生产库不得用作“测试副本”，也不得因已有 backup 文件就跳过本次哈希记录。
6. **进度与阻断：** 建立 `output/screening-v2-work/PROGRESS.md` 与 `BLOCKED.md`；每卡记录目标、白名单、
   基线、命令、结果、下一步与阻断。同一检查连续失败 3 次必须换策略并如实记录；无事项时写“无”。
7. **执行与提交：** 首次执行为 R0 → R1 → R2 → R3 → R4；本次从已阻断 R1 恢复时改为
   **R0.1 → 完成 R1 → R2 → R3 → R4**。全串行，R0.1 必须独立 commit，其余原则上每卡一个 commit；
   只 stage 本卡白名单文件或
   经人工拆分确认的本卡 hunk。commit 前同时检查 `git diff --cached --name-only` 与 `git diff --cached`。
   只有第 2 项定义的真实同一行冲突可例外保留未提交，并在完成报告列出精确文件/行与后续人工合并步骤；
   该包装例外不授权跳过实现、测试或 R4，也不授权提交用户基线。
