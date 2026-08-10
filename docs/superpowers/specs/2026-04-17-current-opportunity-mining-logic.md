# 当前机会挖掘逻辑清单（供与 Claude 讨论）

## 1. 这份文档的目的

这份文档描述的是“当前代码里真正生效的机会挖掘逻辑”，不是最初的理想设计稿。

写这份文档的目的有两个：

1. 帮助快速对齐当前面板到底在展示什么、数据从哪里来、哪些结果会落库。
2. 帮助和 Claude 讨论“这些口径是否合理”，避免把“原始设计想法”和“当前实现状态”混在一起。

如果要对照原始设计，可以再看旧文档：

- `docs/superpowers/specs/2026-04-10-mining-opportunity-scanner-design.md`

但下面这份清单应以当前代码为准。

---

## 2. 当前模块在产品里的位置

### 2.1 面板定位

当前“机会挖掘”是情绪面板里的一个子模块，面向的是“今天有哪些值得关注的标的”和“上一批挖掘标的今天表现如何”这两个问题。

### 2.2 当前面板实际展示的内容

当前只展示两块：

1. `当日挖掘标的`
2. `昨日挖掘标的今日涨幅`

其中右侧标题在历史结果断档时会自动退化成：

- `最近收盘挖掘标的的最新涨幅`

也就是说，右侧并不保证永远是严格的 T-1，只保证是“当前参考日期之前，最近一批已经落库的收盘挖掘结果”。

### 2.3 当前不展示、但仍然保留的内容

下面这些内容仍然会落库，且仍然被日报/复盘链路使用，只是不在这个面板里展示：

- 候选标的历史结果
- 次日/多日复盘 outcome
- Markdown 报告
- Excel 报表
- 策略复盘统计

换句话说，当前产品形态是：

- 面板只展示“今天机会 + 上一批机会跟踪”
- 研究/复盘能力仍然存在，只是退到数据库和报表里

---

## 3. 关键代码入口

如果要和 Claude 对代码实现逐段讨论，主要看这几个文件：

- `app_panel.py`
- `mining/streamlit_tabs/tab_scanner.py`
- `mining/scanners/momentum_breakout.py`
- `mining/scanners/rps_stock.py`
- `mining/universe.py`
- `run_daily.py`
- `mining/reports.py`

各自职责大致如下：

- `app_panel.py`
  负责决定“当前机会挖掘应该按哪一天、哪种数据口径来跑”
- `mining/streamlit_tabs/tab_scanner.py`
  负责拉最新快照、读历史候选、拼左右两块展示数据
- `mining/scanners/momentum_breakout.py`
  负责“异动捕手”筛选
- `mining/scanners/rps_stock.py`
  负责“强势榜”筛选
- `mining/universe.py`
  负责全市场基础股票池过滤
- `run_daily.py`
  负责收盘后正式落库、回填 outcome、生成报表
- `mining/reports.py`
  负责输出日报和复盘摘要

---

## 4. 数据来源与存储关系

### 4.1 当前主要数据源

机会挖掘当前混合使用两类数据：

1. 本地日线收盘数据
2. 实时快照数据

具体来说：

- 收盘数据来自本地数据库 `ash.kline_daily`
- 最新快照通过 AkShare 拉取
  - 优先 `stock_zh_a_spot`（Sina）
  - 失败时回退 `stock_zh_a_spot_em`（Eastmoney）

### 4.2 当前本地存储的关键表

当前机会挖掘真正依赖的本地结果主要有：

- `candidates`
  - 保存某个交易日被筛中的标的
- `strategy_runs`
  - 保存某次策略运行记录
- `outcomes`
  - 保存后续复盘结果

### 4.3 一条很重要的边界

当前代码里：

- `收盘数据筛出来的结果` 会落库
- `盘中/收盘后用实时快照临时筛出来的结果` 只用于展示，不直接落库

这意味着：

- 左侧“当日挖掘标的”在盘中和收盘后看到的，可能是最新快照口径
- 右侧历史跟踪和复盘，仍然依赖已经落库的历史收盘筛选结果

---

## 5. 时间口径：当前到底按哪一天、哪种数据来跑

当前时间决策分成两层：

1. 收盘口径下，`app_panel.py` 先用 `resolve_available_panel_close_date()` 从真实的 stock/index 行覆盖里解析面板日期，而不是只看数据库最大日期。
2. 机会挖掘展示再通过 `resolve_selected_opportunity_runtime()` 对齐这个面板日期；只有快照口径才继续使用 `resolve_opportunity_runtime()` 判断是否需要实时行情。

这意味着用户切换查看日期时，指标面板和机会挖掘模块应跟随同一个可用收盘日。

### 5.1 当前规则总表

| 场景 | `trade_date` | `fallback_trade_date` | 是否强制走最新快照 | 说明 |
| --- | --- | --- | --- | --- |
| 交易日开盘前（`< 09:30`） | 昨日 | 昨日 | 否 | 左右两块都按昨日收盘逻辑 |
| 交易日盘中（`09:30-15:00`） | 今日 | 昨日 | 是 | 左侧优先今天实时快照，右侧用历史候选对比今天最新价 |
| 交易日收盘后 | 今日 | 昨日 | 是 | 仍优先今天最新快照；如果当天收盘数据尚未补齐，也能先展示今天 |
| 非交易日 | 最近一个完成交易日 | 最近一个完成交易日 | 否 | 不再强行拉快照 |

上表描述的是快照口径下的运行时判断。收盘口径下，`trade_date` 和 `fallback_trade_date` 都来自面板当前解析出的 `panel_close_dt`。

### 5.2 一个关键设计点

当前实现的思路不是“收盘后立刻切回昨天”，而是：

- 只要今天是交易日且已经开盘过
- 当前机会挖掘就继续以“今天”为参考日
- 如果本地收盘库还没补到今天，则尽量用今天最新快照顶上

这样做的目的，是避免“刚收盘、收盘库还没追平时，面板却还在显示昨天”的错觉。

### 5.3 当前的真实 fallback 行为

当前当日挖掘的 fallback 逻辑是：

1. 先尝试用最新快照跑今天
2. 如果快照源拿不到数据，再退回到 `fallback_trade_date`
3. 如果是强制快照模式，即使快照筛选结果为空，也不会偷偷回退到昨日收盘候选

这一点很重要，因为它保证了：

- “今天没有筛出标的”会老老实实展示为空
- 不会因为想“看起来有内容”而误把昨天的结果当成今天

---

## 6. 左侧：当日挖掘标的是怎么来的

### 6.1 左侧的目标

左侧要回答的问题是：

- 如果现在就要看今天的机会，当前有哪些标的符合筛选

### 6.2 左侧的数据路径

左侧通过 `load_latest_opportunities()` 取数，逻辑大致是：

1. 判断当前是否应该尝试最新快照
2. 如果应该，则拉实时快照
3. 把实时快照和“上一交易日构建出的股票池”合并
4. 跑两套 scanner
5. 如果快照不可用，再退回本地收盘结果

### 6.3 一个容易被忽略的实现细节

当前盘中/收盘后的“快照股票池”不是完全重新构建的，而是：

1. 先用上一交易日的收盘数据跑 `build_universe()`
2. 得到一个已经过滤过的基准股票池
3. 再把最新快照价格合并进来

这意味着当前快照筛选有一个前提：

- 盘中股票池成员资格，继承自上一交易日的 universe 过滤结果

优点是简单稳定，缺点是它并不是一个完全实时的 universe。

### 6.4 左侧当前展示字段

左侧最终展示字段是：

- 来源
- 排名
- 代码
- 名称
- 涨幅%
- 最高比开盘%
- 最高比收盘%
- 强度分
- 路径

其中：

- `来源`
  - `momentum_breakout` 显示为 `异动`
  - `rps_stock_top20` 显示为 `强势`
- `路径`
  - 用于说明异动标的是通过 `gain`、`shadow` 还是 `both` 路径入选

---

## 7. 右侧：昨日挖掘标的今日涨幅是怎么来的

### 7.1 右侧的目标

右侧要回答的问题是：

- 上一批已经落库的历史挖掘标的，今天表现如何

注意这里的重点不是“再筛一次”，而是“跟踪上一批已经筛出来的票”。

### 7.2 右侧在快照模式下的逻辑

如果当前处于“应该尝试最新快照”的模式，右侧逻辑是：

1. 先找当前参考日之前，最近一批已经落库的候选日期
2. 读出那一天的候选标的
3. 再拿今天的最新价格去算这些标的的最新涨幅

因此右侧并不是重新跑 scanner，而是：

- 历史候选集合来自数据库
- 当下涨幅来自最新价格

### 7.3 为什么有时会显示“最近收盘”而不是“昨日”

如果数据库里缺少严格的前一交易日候选结果，比如：

- 当前参考日是 `2026-04-17`
- 但最近一次落库候选只有 `2026-04-14`

那右侧会退化成：

- `最近收盘挖掘标的的最新涨幅（2026-04-14 -> 2026-04-17）`

这是一种“不要误导用户说成昨日”的保护逻辑。

### 7.4 右侧在收盘口径下的逻辑

如果当前不走最新快照，则右侧逻辑是：

1. 找到当前可用的最近收盘日
2. 找到它之前最近一批已落库候选
3. 用当前收盘价去算这些标的的今日涨幅

### 7.5 右侧当前展示字段

右侧最终展示字段是：

- 来源
- 排名
- 代码
- 名称
- 今日涨幅%
- 最新价
- 口径

`口径` 当前会标识为：

- `盘中快照`
- `最新价格`
- `日线收盘`

---

## 8. 当前两套筛选器的真实规则

### 8.1 股票池基础过滤（两套策略共用）

所有股票策略先经过 `build_universe()` 过滤，当前规则包括：

1. 只取当日有成交的股票
2. 剔除北交所等 `8/4/9` 开头代码
3. 剔除一字板
4. 剔除 `ST`
5. 如果有总市值数据，则只保留 `<= 5000 亿`
6. 如果有上市日期，则只保留上市时间足够长的标的

当前上市天数门槛在代码里是 `87` 个自然日近似值。

### 8.2 异动捕手 `momentum_breakout`

当前异动策略有两条入选路径：

1. `gain`
2. `shadow`

#### `gain` 路径

按板块分不同涨幅区间：

- 主板：`6% < 涨幅 < 9.5%`
- 创业板 / 科创板：`7% < 涨幅 < 13%`

#### `shadow` 路径

当前 `shadow` 条件是：

1. `最高价 > 开盘价 3%`
2. `最高价 > 收盘/最新价 5%`
3. `最新涨跌幅 > -3%`，剔除已经跌超 3% 的走弱票
4. 同时涨幅不能超过对应板块的上限

也就是说，当前 `shadow` 已经不是单纯的“长上影”，而是“长上影 + 未明显转弱”的组合条件。

#### 两条路径共用的附加条件

无论 `gain` 还是 `shadow`，还都要满足：

1. `5日涨幅 > 5%`
2. `5日涨幅 < 30%`
3. `成交额 >= 10 亿`

#### 排序方式

异动结果按以下顺序排序：

1. `amount` 降序
2. `change_pct` 降序
3. `sec_code` 升序

### 8.3 强势榜 `rps_stock_top20`

当前强势策略逻辑是：

1. 用最近 `10` 日和 `30` 日收益率计算 `RPS`
2. 同时要求：
   - `rps_10 >= 80`
   - `rps_30 >= 80`
3. 最后按综合强度排序取前 `20`

当前综合分：

- `score = (rps_10 + rps_30) / 2`

排序优先级：

1. `score` 降序
2. `rps_30` 降序
3. `sec_code` 升序

---

## 9. 历史结果如何落库，以及面板和落库的关系

### 9.1 正式落库入口

正式把某个交易日的机会挖掘结果写入本地数据库，当前主要依赖 `execute_daily_pipeline()`。

这个流程会做：

1. 跑所有注册的 scanner
2. 写 `strategy_runs`
3. 写 `candidates`
4. 回填 `outcomes`
5. 可选生成 Markdown/Excel 报表

### 9.2 面板打开时会不会补历史

会，但当前不是无限补，而是“补最近缺口”。

当前 `ensure_close_history_persisted()` 的逻辑是：

1. 找到截止某个交易日为止的收盘日列表
2. 对比当前已经落库的候选日期
3. 只补最近一段缺失日期

默认限制是：

- 最多回补最近 `10` 个收盘日的缺口

### 9.3 自动补齐现在会在哪些场景触发

当前会在两类场景补历史：

1. 面板展示机会挖掘时
2. 收盘数据更新完成后，主动调用同步逻辑时

面板启动时还会先调用 `offline_daily_update.build_missing_update_plan()` 检查最近交易日是否缺 stock/index/concept/etf/mining 任一域。发现缺口后，`app_panel.py` 会通过 `backfill_orchestrator.py` 后台启动 `offline_daily_update.py`，再由离线脚本决定调用 BaoStock、AkShare fallback、题材回填、ETF 回填和 `run_daily.py`。

也就是说，理论上只要收盘数据库补到了某天，机会挖掘历史结果也应该跟着补齐。

### 9.4 一个需要明确的边界

当前“历史可复盘结果”本质上仍然是：

- 基于收盘数据筛出来并落库的结果

而不是：

- 把盘中实时快照筛选结果原样存成历史记录

如果后面要讨论“盘中信号是否也应落库”，那会是一个新的产品口径决定。

---

## 10. 当前已经做过的性能优化

在不删功能的前提下，当前机会挖掘已经有几层优化：

### 10.1 快照只拉一次

左右两块现在共用同一份快照加载器。

这避免了：

- 左侧拉一次实时快照
- 右侧又重复拉一次

### 10.2 已落库收盘候选优先直接读取

当前如果某个收盘日的候选已经在 `candidates` 里，就直接读库，不再重新全市场扫描。

### 10.3 历史补齐只补最近缺口

当前不会每次面板打开都从很久以前开始重跑，而是只补最近一段缺失日期。

### 10.4 当前仍然慢的主要来源

即使做了上面这些，当前机会挖掘仍可能慢，主要还是因为：

1. 实时快照本身是外部请求
2. 盘中/收盘后如果要按最新价格重跑 scanner，仍然需要在较大的股票池上重新筛一遍
3. 如果最近几天历史候选确实没落库，首次展示时仍会触发补齐

所以当前性能瓶颈主要不是“代码里重复渲染”，而是：

- 外部快照获取
- 最新价格口径下的全市场筛选
- 首次补历史缺口

### 10.5 快照接口顺序

当前快照加载优先 `stock_zh_a_spot`，再回退 `stock_zh_a_spot_em`。这个顺序和 `app_panel.py`、`mining/streamlit_tabs/tab_scanner.py` 保持一致，用于减少 Eastmoney 偶发断连或空响应时对面板加载的影响。

---

## 11. 当前实现里最值得和 Claude 讨论的点

下面这些问题，是我觉得最适合和 Claude 讨论“是否合理”的地方。

### 11.1 收盘后继续用最新快照，是否是最好的口径

当前实现是：

- 收盘后仍优先按今天最新快照展示

好处是不会卡在昨天。

但也值得讨论：

- 是否应该在某个时间点自动切换成“今天收盘版正式结果”
- 或者是否要在 UI 上明确标注“这还是快照口径，不是正式收盘落库结果”

### 11.2 右侧是否应该允许“最近收盘”退化

当前实现为了避免误导，会把缺失 T-1 的情况改成：

- `最近收盘挖掘标的的最新涨幅`

需要讨论的是：

- 这是更诚实的做法
- 还是应该更严格，T-1 缺失就直接显示为空并提示需要补库

### 11.3 盘中快照是否应继续沿用上一交易日 universe

当前盘中筛选的 universe 来自上一交易日过滤结果。

这很务实，但并不完全实时。

值得讨论：

- 这是否已经足够
- 还是应该在盘中重建一套“实时 universe”

### 11.4 盘中/收盘后临时筛选结果是否也应该落库

当前只把收盘筛选结果作为正式历史记录。

这意味着：

- 面板里看到的盘中机会，默认不会成为历史复盘样本

如果后面想研究“盘中抓到的机会”和“收盘正式候选”的差异，那可能需要新增一层：

- snapshot candidates
- snapshot run log

### 11.5 `shadow` 的产品定义是否足够清晰

当前 `shadow` 已经变成：

- 上影结构
- 最新涨跌幅没有跌破 -3% 走弱线
- 同时满足 5 日趋势和成交额过滤

这其实已经是一个“形态 + 走弱过滤”的复合条件。

值得讨论的是：

- 这个名字还是否准确
- 是否应该在面板上更明确区分“纯上影异动”和“强势回落但未转弱”

---

## 12. 建议给 Claude 的一句话背景描述

如果你想先用一句话把背景讲清楚，可以直接这样说：

> 当前机会挖掘模块已经分成“当日机会”和“历史机会跟踪”两块；当日机会在盘中和收盘后优先走实时快照筛选，历史机会仍然基于本地收盘数据落库结果做复盘和跟踪。现在我想讨论的是，这套时间口径、历史落库边界、右侧退化逻辑以及 `shadow` 的定义，是否是合理的产品与策略表达。

---

## 13. 如果要和 Claude 重点讨论，建议直接问这 5 个问题

1. 交易日收盘后，机会挖掘继续优先展示快照结果，直到正式收盘数据补齐，这个口径是否合理？
2. 右侧历史跟踪在缺少严格 T-1 结果时，退化成“最近收盘挖掘标的的最新涨幅”，这个交互是否合适？
3. 盘中筛选沿用上一交易日的 universe，而不是完全实时重建 universe，这个权衡是否合适？
4. 当前盘中/收盘后快照结果只展示不落库，历史复盘只认收盘数据，这个边界是否合理？
5. 当前 `shadow` 已经是“长上影 + 未跌破 -3% 走弱线 + 趋势/成交额过滤”的复合条件，这个定义是否清晰，是否需要重新命名或拆分？

---

## 14. 一句话总结

当前机会挖掘模块本质上已经分成两条链路：

- 展示链路：尽量让面板在盘中和收盘后看到“今天”
- 研究链路：仍然把“收盘后正式筛选并落库”的结果作为历史复盘基准

这套做法的优点是实用，缺点是“实时展示”和“正式历史样本”不是同一套口径，这正是最值得和 Claude 讨论的核心点。

---

## 15. 2026-08-08 Screening v2.5 R4.2 正式闭环

本节记录当前正式 A–E 链路的边界；旧的 legacy 展示和 outcome 说明仍按前文处理。

### 15.1 C 状态是 formal batch 的原子组成部分

- `pullback_state_history` 保存 `batch_id` 与 `definition_version`。
- A–E runs、candidates 与 C 当日状态在同一个 `BEGIN IMMEDIATE` 事务内写入；任一部分失败会整体回滚，并保留此前 complete batch。
- 读取端只认当前 `definition_version`、`close_final`、每个交易日最新 complete batch。intraday、failed、旧版本及没有 batch/version 的 legacy 状态均不可见。
- 同 fingerprint 重跑复用原 complete batch，不增加 runs、candidates 或状态行；新 fingerprint 才创建新 batch。
- `再启动`必须由先前正式 A 入池和连续正式状态演化产生，禁止通过 SQL 手工植入状态。

### 15.2 E 的快照缓存边界

- v2 cache 是单文件原子 bundle，同时保存行情 frame、`trade_date`、`observed_at` 与有限正数的 `benchmark_closes`。
- cache hit 必须恢复同一 benchmark/as-of，且不得触发 provider 请求。
- 旧双文件缓存仍可只读降级；缺少 benchmark 时明确标记，E 不会伪造可用。
- 损坏、过期、低覆盖或日期不符均 fail closed。

### 15.3 history dry-run 与浏览器证据边界

- history CLI 必须显式指定 shadow DB。manifest 绑定四个源库 SHA-256、target、sessions 与定义版本；默认拒绝已有 shadow，只有显式 reuse 且 manifest 完全一致才可零增长复用。
- JSON 报告记录失败、各策略 empty、逐日 C 状态、自然 `再启动`、表增量和源库前后哈希；持久化失败仍写报告并非零退出。
- 浏览器的 selected/effective/formal 日期按 formal 目标日展示；六态分别保留 intraday、pending、final、unavailable、not_run、failed 证据。
- 盘中和 pending 只展示临时结果，不写正式候选或状态历史；final 只读正式 complete batch。

---

## 16. 2026-08-09 Screening v2.5 R4.3 生产前证据门

### 16.1 股票快照与 benchmark 属于同一刷新周期

- 默认 `QuoteSnapshotAdapter.load()` 在股票快照通过 normalization 与 coverage 后，使用同一有界刷新周期获取 `selection_context.BENCHMARK_CODES`。
- E 的 primary benchmark `000852` 必须是有限正数；timeout、缺少 primary 或非法值不会影响 A–D 使用股票快照，但 E 明确为 unavailable。
- v2 bundle 原子保存本周期实际取得的股票 frame、benchmark map、各自 provider/status/errors 与同一 as-of。股票 provider 失败时只允许整体回退到同日、同周期旧 bundle，不会另取新 benchmark 与旧股票拼接。
- 测试可注入股票和 benchmark DataFrame；核心验收仍从公开 `load()` 进入，不直接调用 cache writer。

### 16.2 selected、effective、formal 日期独立取证

- `selected_trade_date` 来自 UI query；`effective_trade_date` 来自 `SelectionRuntime.context.trade_date`；`formal_trade_date` 在 close-final 模式来自实际 complete batch，在临时模式来自 evaluation context。
- 三值进入结构化 evidence。任一缺失或不相等时标记 `date_mismatch`，显示三值、将 A–E 状态改为阻断，并隐藏所有候选表。
- 页面不得通过把 effective/formal 重新赋值为 selected 来制造日期一致。

### 16.3 history 与 generated evidence 的版本边界

- history JSON 使用显式 `evidence_schema_version`，并记录生成时的 Git HEAD、`history_bootstrap.py` 路径和 SHA-256。
- `daily` 是唯一逐日明细；不再复制整份明细到 `fingerprints`，从而保证 first/reuse 由同一最终代码生成且字段一致。
- `output/` 下的 history、浏览器截图、YAML、console 和报告是本地验收物，不进入 Git；代码提交只保留代码、测试、策略文档以及停止跟踪 generated output 的删除记录。

---

## 17. 2026-08-09 R4.3.1 实时 provider 修订

本节是对 16.1 的日期修订；保留此前 R4.3/CDP 记录作为历史证据。

### 17.1 coverage 分母

- 实时股票快照的 expected universe 必须取当前日期之前、数据质量层认定为 usable/full 的最近股票交易日；`known_bad_session` 不得作为 coverage 分母。
- 当前生产只读证据中，`2026-08-07` 的 397 只属于 `known_bad_session`，最近可用分母是 `2026-08-06` 的 5203 只。
- runtime live gate 在 `expected_codes_count < 5000` 时于任何 provider 调用前返回 `invalid_coverage_denominator`；0.80 coverage 阈值保持不变。

### 17.2 同步 provider 与 benchmark

- 股票主 provider 是 `pqquotation` Tencent 批量接口，按调用方传入的 expected codes 分批；AkShare `stock_zh_a_spot_em` 只作一次 fallback。
- `stock_zh_a_spot` 不再位于同步链。每个 provider 有独立、进程级有界 timeout，同一 provider 每个刷新周期最多调用一次；失败保留结构化 provider、status、errors、raw_rows、normalized_rows 与 coverage。
- benchmark 在股票快照可用后独立刷新，优先 Tencent 指数批量路径，再使用一次 AkShare EM fallback；`000852` 必须是有限正数。benchmark 失败只使 E fail closed，不禁用 A-D。

### 17.3 2026-08-09 非交易时段证据边界

- Tencent 20 只样本返回 20/20，coverage 1.0；随后 5203 只全市场返回 raw 5203、normalized 5199、coverage 0.999231。
- 同刷新周期独立 benchmark 返回 `000852=7679.53`；样本、全市场和 benchmark 三次调用合计约 2.001 秒，单次 timeout 均为 8 秒。
- 该证据发生于周日，只证明实现与传输能力。公共 `QuoteSnapshotAdapter.load()` 的真实交易时段端到端及同日重启 cache 恢复尚未执行，状态为 `locally-verified + trading-window environment-blocked`，不得标记 `real-env-verified`。

---

## 18. 2026-08-10 R4.3.2 Tencent 交易时段批次收敛

### 18.1 有界批次与部分成功

- Tencent 股票请求按排序、去重后的 caller-supplied codes 固定每批最多 60 只；同步刷新最多 8 个在途请求，不再把 5200+ codes 交给 `pqquotation.real()` 形成 87 并发。
- 每批 HTTP timeout 为 2.0 秒，worker 收集预算为 6.5 秒，父进程预算为 8.0 秒。刷新不重试失败批次；内部 deadline 到达后取消未启动批次并返回已经完成的行。
- 每批保留 status、elapsed、rows、error；汇总保留成功/timeout/error 批次数、received/missing codes、最大并发、p50/p95/max latency 与 provider timestamp/date 分布。
- 合并只在同一 Tencent provider 内进行，按 `sec_code` 去重并排除北交所；不同 provider 的股票行不会拼成一个快照。

### 18.2 adapter 与 cache 合同不变

- Tencent 部分结果继续进入原有 normalization 与 coverage：coverage >=0.80 时直接 `snapshot_usable` 且不调用 EM；低于 0.80 时保留真实 raw/normalized/coverage 与批次摘要，再进入原有一次 EM fallback 或同日 cache。
- `QuoteSnapshotResult` 字段、v2 cache bundle schema、0.80 阈值、benchmark/E、跨日与 close-final 规则不变。内部批次诊断随 frame attrs 传递，不扩展公开 DTO。

### 18.3 真实交易时段证据

- 2026-08-10 13:42 +08 的唯一公共 `QuoteSnapshotAdapter.load()` 使用 2026-08-06 clean/full 的 5203-code 分母。
- 87/87 批成功，最大并发 8，股票收集 2.953 秒，公共入口 3.860 秒；raw 5203、normalized 5202、coverage 0.999808，provider 当日比例 1.0。
- batch latency p50 0.078 秒、p95 0.234 秒、max 2.110 秒；provider timestamp 范围为 13:41:57–13:43:02。
- Tencent benchmark 返回 `000852=7670.07`。新 Python 进程 `cache_only` 的 attempts=0、from_cache=true，frame/benchmark/as-of 与 bundle SHA-256 完全一致。
- 四个生产数据库 SHA-256 前后相同；没有 migration 或生产数据库写入。本证据状态为 `real-env-verified`，但不构成生产 migration 授权。

---

## 19. 2026-08-10 R4.4 B/D 逐股 activity 可用性修订

### 19.1 source 与完整窗口按股票判定

- activity builder 按 `sec_code` 独立检查完整交易日窗口；窗口内每个值都必须有限且大于 0。单只停牌、新股或缺行只使该股票 fail closed，不再令全市场 activity 一起不可用。
- 每只股票优先使用完整窗口内全部可用的 `amount`；仅当 `amount` 不完整时，才整体改用同样完整的 `turnover_ratio`。同一股票不得混用两个 source。
- `momentum_anomaly` 与 `base_breakout` 要求 T 加 T-20…T-1 共 21 日；`compression_launch` 按 T 加 T-45…T-1 共 46 日选择 source。
- `compression_launch` 的 `activity_ratio` baseline 仍严格只取 T-20…T-1 的中位数，T 仅作为当前值；46 日窗口不会扩大或改变 ratio baseline。
- activity cache key 同时包含 `baseline_days` 与 `required_window_days`；返回值保持副本隔离，盘中结果继续标记 provisional。

### 19.2 缺失诊断的唯一所有者

- builder 是 activity 缺失计数的唯一所有者，scanner 只消费可用行，不重复累计同一股票。
- 对 46 日消费者，连 21 日完整 source 都不存在时记 `activity_missing`；21 日可用但 46 日不完整时记 `activity_window_missing`。两个原因互斥。
- activity percentile 只在当前消费者实际可用的股票横截面内计算。B/D 的 gate、rank、阈值与 A/C/E 均未修改。

### 19.3 真实只读 sandbox 证据

- `2026-08-06` universe=5036：21 日 eligible=4981、`activity_missing=55`；46 日 eligible=4950、`activity_missing=55`、`activity_window_missing=31`。
- B momentum 候选 191、compression 候选 3；D 候选 0 是真实策略 gate 结果。三个漏斗均逐股核算到 5036，不再出现 `activity_missing=5036`。
- 四个 sandbox source SHA-256 前后相同，生产四库未写入；没有 outcomes、schema 或 migration 变更。
