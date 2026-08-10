# Screening V2 完成质量评审与二次修改方案

> 日期：2026-08-06  
> 评审对象：`codex/screening-v2`，HEAD `5a7b770`  
> 主要读者：Opus、二次修改执行者  
> 状态：评审完成，尚未执行本文修改方案  
> 范围：判断当时是否符合选股形态，不讨论入场时点、持有期或收益有效性

## 1. 技术结论

Terra 已完成 Screening V2 的主要结构性接线，包括共享股票池、复权上下文、盘中/盘后模式、A–E
能力、统一面板与报告入口。定向 V2 测试能够通过，说明模块在理想合成数据下可以运行。

当前交付仍不能通过正式完成验收，建议标记为：

- `implemented`
- `locally-verified`
- `ui-smoke-verified`
- `partial / needs revision`

不能标记为：

- `broadly-verified`
- `close-final-verified`
- `real-data-correctness-verified`

最关键的问题不是再微调几组排名权重，而是以下三层语义同时出现偏差：

1. 数据质量层把少数个股停牌/零值占位升级成全市场坏日。
2. 强趋势定义用长期多头结构和相对前 20% 配额替代“近期仍然强”。
3. 盘中、收盘定版与持久化尚未形成端到端一致性保证。

这会系统性放大过去涨幅较大的旧赢家，遗漏刚进入主升阶段的股票。

## 2. 本次五只样例支持用户的判断

### 2.1 指标定义

下表基于本地 `a_share_mvp.db`，截至 `2026-08-05` 收盘。

`N-bar 收益`按最近 N 个有效个股交易 bar 的 `close/pre_close` 复合收益计算，用于核验个股近期实际方向；
它不是收益预测，也不是交易建议。

### 2.2 逐股复核

| 股票 | 5-bar | 10-bar | 20-bar | 距20-bar高点 | 面板/用户观察 | 评审结论 |
|---|---:|---:|---:|---:|---|---|
| 688141 杰华特 | +12.66% | -10.95% | +21.49% | 89.05% | 16:50 快照 A 榜第 3 | 近期反弹与旧趋势并存，强度不连续，不应自动标为强趋势 |
| 688627 精智达 | -2.41% | -18.51% | -14.88% | 77.03% | 16:50 快照 A 榜第 4 | 近期明显弱势，不符合当前强趋势 |
| 301571 国科天成 | -7.10% | -7.33% | -5.81% | 90.05% | 用户此前快照看到入选；16:50 快照未入 A | 近期处于弱化，长期结构不能替代近期强度 |
| 300996 普联软件 | +48.49% | +77.79% | +67.97% | 100.00% | 16:50 未入 A；Legacy 标为“延伸中” | 新晋主升，因长均线尚未排列而漏选 |
| 601858 中国科传 | +32.08% | +49.26% | +59.77% | 100.00% | 16:50 未入 A；Legacy 标为“延伸中” | 新晋主升，受稀疏窗口与长均线门影响而漏选 |

这五只股票用于定位机制，不得在代码中硬编码白名单，也不得只围绕这五只股票调参。

## 3. P0：数据质量门错误地删除了全市场交易日

### 3.1 真实数据证据

最近三个交易日的当前分类如下：

| 日期 | 股票行数 | 有效行 | 异常行 | 覆盖率（相对基线） | 当前状态 |
|---|---:|---:|---:|---:|---|
| 2026-08-03 | 5,201 | 5,195 | 6 | 100.35% | `partial_missing` |
| 2026-08-04 | 5,202 | 5,195 | 7 | 100.35% | `partial_missing` |
| 2026-08-05 | 5,202 | 5,197 | 5 | 100.39% | `partial_missing` |

`2026-08-05` 导致整日失效的五行是：

- `000838 财信发展`：OHLC 全零，`pre_close=2.50`，量额为零。
- `300246 宝莱特`：OHLC 全零，`pre_close=19.26`，量额为零。
- `300333 兆日科技`：OHLC 全零，`pre_close=8.87`，量额为零。
- `300862 蓝盾光电`：OHLC 全零，`pre_close=19.01`，量额为零。
- `603221 爱丽家居`：O/H/L 为零，`close=pre_close=22.54`，量额为零。

这些是典型的停牌/供应商占位格式，不足以否定同日其余 5,197 只股票的有效价格。

### 3.2 代码根因

`mining/data_quality.py::_classify_summary()` 当前只要出现任意一行：

- 非有限值；
- `close<=0`；
- OHLC 关系异常；
- 活动度异常；

就把整日设为 `partial_missing`。

随后 `clean_stock_trade_dates()` 只保留状态为 `clean` 的日期，导致这些日期从所有股票的 MA、RPS、波动率和
活动度窗口中一起消失。

### 3.3 影响

- `2026-08-03` 至 `2026-08-05` 不能进入 `close_final`。
- 所谓“近 20 日”实际由稀疏日期组成，可能跨越远超过 20 个市场交易日的日历区间。
- 近期走弱的旧赢家仍可能保留较高的稀疏 RPS。
- 近期刚进入主升的股票可能因为其有效涨幅发生在被删除的日期而漏选。
- 面板盘后仍停留在 `close_pending`，无法真正完成定版覆盖。

## 4. P0：市场交易日质量与个股行质量必须分层

### 4.1 建议状态模型

市场日状态与个股行状态分开记录。

市场日建议状态：

- `clean`：覆盖和行质量均正常。
- `usable_with_quarantine`：市场覆盖完整，仅少量个股行被隔离；该日可进入公共交易日历和收盘定版。
- `partial_missing`：市场覆盖明显不足，但仍存在部分有效数据；不可发布完整榜单。
- `known_bad_session`：系统性坏日或所有数据源失败。

个股行建议状态：

- `valid_trade`
- `confirmed_halt`
- `provider_halt_placeholder`
- `invalid_price`
- `invalid_activity`
- `missing`

### 4.2 首版可验证阈值

以下阈值作为初始工程定义，需在真实库分布上验证后冻结：

- `valid_rows / recent_clean_median >= 0.98`；
- 异常个股行占比 `<=0.5%`；
- 满足以上条件时，将坏行隔离并把市场日标为 `usable_with_quarantine`；
- 市场覆盖低于 `0.80` 或出现系统性全零时仍然 fail-closed；
- `0.80–0.98` 区间明确为 `partial_missing`，不得伪装成完整榜单。

`2026-08-03` 至 `2026-08-05` 应因覆盖完整而成为可用市场日，同时隔离少量异常个股行。

### 4.3 股票时间序列处理

- 公共窗口使用真实市场交易日历，禁止使用“全市场每一行都完美”的日期交集。
- 已确认停牌日可使用前一有效复权收盘维持价格序列，同时保留 `tradable_today=false`。
- 未确认的坏行保持缺失，不得盲目前向填充。
- 20 日窗口建议至少有 18 个有效/确认停牌点；60 日至少 54 个；长窗口至少达到 90% 覆盖。
- 当前日无有效成交的股票仍从当日选股池排除，但历史停牌不能让它永久失去均线资格。

## 5. P0：复权层不能因一行坏值淘汰整只股票

`mining/adjusted_prices.py::build_forward_adjusted_bars()` 当前只要某只股票历史中出现任意无效 OHLC、close、
pre_close 或复权比例，就会将该股票整段复权序列设为无效。

建议：

1. 先按个股行质量过滤或标记。
2. 已确认停牌占位不参与复权边计算。
3. 无效复权边只隔离对应边或局部片段，不得使整只股票全部历史永久失效。
4. 每只股票输出：
   - `valid_adjusted_bars`
   - `invalid_edges`
   - `coverage_ratio`
   - `latest_valid_segment_start`
5. 均线和 RPS 根据能力窗口检查覆盖率；不足时按统一市场 profile 降级或跳过，禁止个股私自混档。

## 6. P1：当前“强趋势”定义混淆了状态与排名

### 6.1 当前实现问题

当前 A 的主要逻辑是：

1. 通过统一 profile 的 `close > MA短 > MA中 > MA长`。
2. 长均线相对过去方向向上。
3. 距长窗口最高收盘不低于 85%。
4. 流动性不低于 40 分位。
5. 对合格池内因子做百分位排名。
6. 机械地把前 20% 标为“强趋势”，其余标为“趋势成型中”。

这套规则更接近“长期多头结构中的相对排名”，不能准确回答“谁当前仍处于强趋势”。

主要缺口：

- 没有 `ret10>0`、`ret20>0` 等绝对近期方向门。
- 没有 MA20 近期斜率门。
- 没有短期高点回撤/结构损伤门。
- 85% 的长窗口高点门过松，尤其对 20% 涨跌幅板块。
- `rps_prior` 权重过高，容易奖励已走弱的旧赢家。
- 弱市中仍强制产生至少一只“强趋势”。

### 6.2 第一性原则

“强趋势”必须先是绝对状态，再在同状态股票中排名。

排名可以回答：

> 已经确认属于强趋势的股票里，谁更强？

排名不能回答：

> 市场里是否存在强趋势？

因此必须允许弱市中出现 0 只强趋势，也允许普遍强市中超过 20% 的合格股票同时属于强趋势。

## 7. P1：能力 A 建议拆成两条强趋势路径

### 7.1 共享绝对近期强度门

首版建议共同要求：

- `close > MA10 > MA20`
- `MA20 / MA20.shift(5) - 1 > 0`
- `ret10 > 0`
- `ret20 > 0`
- `rps20_pct >= 0.80`
- `close / rolling_high_20 >= 0.92`
- T-1 流动性分位 `>=0.40`
- 数据质量、股票池与窗口覆盖率合格

这些是定义验收的初始阈值，不是收益有效性结论。

### 7.2 A1：趋势延续 `continuation`

用于成熟、稳定上行趋势：

- 通过共享绝对门；
- `MA20 > MA60`；
- MA60 不下降；
- 更长 MA 只作为成熟度证据或在数据充足时增强确认，不再统一封杀所有股票。

### 7.3 A2：新晋主升 `fresh_breakout`

用于 300996、601858 这类长均线尚未来得及排列、但近期已明显进入主升的股票：

- 通过共享绝对门；
- `close / rolling_high_20 >=0.98`；
- `rps10_pct>=0.80` 且 `rps20_pct>=0.80`；
- MA20 明确向上；
- 不要求 MA60/MA90/MA120 已完成严格多头排列。

### 7.4 其他状态

- `forming`：长期或中期结构改善，但未通过绝对近期强度门。
- `weakening`：曾有趋势结构，但出现 `ret10<=0`、跌破 MA10/MA20 或近高回撤过大。
- `invalid`：数据、股票池或窗口覆盖率不足。

`forming` 和 `weakening` 可以在折叠观察区展示，但不能与正式“强趋势”混在一起。

### 7.5 排名与标签

- `strength_tier` 由绝对门和路径决定。
- 前 20% 只能命名为 `rank_band=top20_pct`，不得命名为“强趋势”。
- 建议降低或移除 `rps_prior` 在当前强度排名中的权重，将其保留为趋势成熟度证据。
- 排名因子必须在同一股票池、同一时间窗口、同一因子集合中计算。
- benchmark 分离度只有在全市场同批次可用时才参与排名；不可用时全市场统一移除并重归一。

## 8. 五只样例在新定义下的预期

这些预期只作为回归断言，不允许写成代码白名单：

- `688141`：因 `ret10<0` 且距 20-bar 高点不足 92%，不应标为当前强趋势；可标为反弹/弱化观察。
- `688627`：因 5/10/20-bar 收益均弱且远离 20-bar 高点，不应进入强趋势。
- `301571`：因 5/10/20-bar 收益均为负，不应由长期结构继续保送。
- `300996`：应能通过 `fresh_breakout` 路径，不被 MA60/MA120 尚未排列阻挡。
- `601858`：应能通过 `fresh_breakout` 路径，不被稀疏“20 日”或长均线门阻挡。

## 9. P1：盘中与收盘定版尚未结果等价

### 9.1 已实现的机制骨架

`mining/selection_runtime.py` 已实现：

- clean close 优先于快照；
- 快照超时和覆盖率检查；
- `close_final` 时清除同日进程内快照缓存；
- 跨日拒绝复活旧快照。

### 9.2 尚未满足的验收

- 盘中 A 调用 `evaluate_strong_trend(context)`，没有传 benchmark。
- close-final scanner 会读取 `000852` benchmark 并可能加入 `separation_pct`。
- 现有模式等价测试只比较复权 OHLC，没有比较 A–E 候选、字段、排序与状态。
- E 在盘中缺少同 `price_as_of` 的实时基准序列时会整体降级。

因此“相同快照 bar 与最终日线相同时，A–E 结果和排序完全相同”尚未得到证明。

### 9.3 修改要求

1. 股票快照和指数快照使用同一 `price_as_of`。
2. 所有能力通过同一 `SelectionContext` 获取市场基准，不在 UI 和 scanner 内分别查询。
3. snapshot 与 final bar 相同时，必须断言：
   - 相同候选集合；
   - 相同 `strength_tier/state/event_subtype`；
   - 相同评分字段；
   - 相同排序；
   - 只允许 `mode/as_of/source` 元数据不同。

## 10. P1：快照名称与 ST 保护存在漏洞

`mining/streamlit_tabs/tab_scanner.py::_normalize_snapshot_quotes()` 的名称列候选只包含：

- `name`
- `sec_name`

AkShare 快照常用中文列名 `名称`，因此本次页面 A 榜的 `sec_name` 显示为 `nan`。

`mining/universe.py::_snapshot_name_map()` 又会把 `pd.NA` 转成字符串 `<NA>`，并当作非空有效名称，造成：

- `metadata_stale` 保护被错误解除；
- ST 名称过滤可能失效；
- UI 出现 `nan/<NA>`。

修改要求：

- 支持 `名称/股票简称/证券简称` 等实际字段。
- 使用 `pd.isna(name)` 判断缺失。
- 显式拒绝 `"nan"`、`"<NA>"`、`"None"` 等哨兵字符串。
- 有效快照名称覆盖不足时，不能声称元数据已刷新。
- 名称与 ST 过滤必须有真实快照字段测试。

## 11. P1：收盘定版完整性和候选覆盖语义不足

### 11.1 日期级判断不够

`ensure_close_history_persisted()` 当前通过 `_persisted_candidate_dates()` 判断某日是否已经存在候选。

如果该日已经有任意 Legacy 或其他策略候选，新增 V2 策略即使从未运行，该日期也可能被视为“已经持久化”。

定版完整性必须按以下键判断：

```text
(trade_date, strategy_id, version)
```

并以 `strategy_runs` 的成功/空榜/失败状态为准，不能以 candidates 是否非空代替运行状态。

### 11.2 upsert 不能实现覆盖

`mining/db.py::save_candidates()` 只更新或新增本轮仍然入选的股票，不删除本轮已经落选的旧候选。

这会造成：

- 同策略同日重新定版后残留旧股票；
- 调整阈值后旧候选继续显示；
- 所谓“收盘定版覆盖快照/旧结果”不完整。

建议增加事务化 replace API：

1. 创建本轮 `strategy_run`。
2. upsert 本轮候选。
3. 删除同 `(strategy_id, version, trade_date)` 下本轮不再存在的候选。
4. 正确处理相关 outcomes；定版前不得生成 outcomes，已有 outcome 时必须有明确迁移规则。
5. 一次事务提交，失败则全部回滚。

## 12. Annotation 1：盘后定版覆盖与跨日快照结论

原说法：

> 盘后定版覆盖快照，跨日不得复活旧快照。

评审结论：当前只实现了运行时骨架，尚未实现真实端到端保证。

已实现：

- close-final 优先；
- 同日进程内快照缓存可清除；
- 快照日期不等于当前交易日时拒绝使用。

尚未实现：

- 最近三天因少量停牌行被判 `partial_missing`，实际无法进入定版。
- 定版完整性只按日期判断，不能保证每个 V2 strategy/version 已运行。
- 候选 upsert 不删除旧落选行，不能证明正式结果真正覆盖旧结果。
- 相同 snapshot/final 的 A–E 结果与排序没有完整等价测试。

因此二次修改完成前，不得宣称该验收已经通过。

## 13. P2：面板语义仍会误导用户

当前页面在 `intraday_snapshot` 或 `close_pending` 时仍使用标题：

> 正式筛选 A–E

空榜文案也统一显示“暂无收盘定版候选”。

建议根据模式动态显示：

- `intraday_snapshot`：盘中临时筛选 A–E
- `close_pending`：收盘待定筛选 A–E
- `close_final`：收盘定版筛选 A–E
- `data_unavailable`：数据不可用，禁止展示旧榜单代替

盘中临时榜必须继续保持不写入正式候选和状态历史。

## 14. P2：文档和仓库级测试未完成同步

### 14.1 文档

`docs/superpowers/specs/current-opportunity-selection-strategy.md` 仍主要描述：

- `momentum_breakout`
- `rps_stock_top20`
- Legacy universe 和旧面板口径

它不能作为当前 A–E 实现的真值文档。

二次修改完成后必须更新该文档，至少说明：

- A–E 能力定义；
- 股票池与 ST 名称来源；
- 行质量/市场日质量；
- 盘中、待定、定版三种模式；
- 强趋势路径与状态；
- 持久化和降级边界。

### 14.2 测试

本次实际运行结果：

- 定向 V2 测试：`Ran 82 tests ... OK`
- 全量仓库测试：`Ran 205 tests ... FAILED (failures=2, skipped=2)`

失败测试：

- `tests.test_mining_pipeline.MiningPipelineTests.test_second_launch_selects_pullback_shrink_and_stop_setup`
- `tests.test_mining_pipeline.MiningPipelineTests.test_execute_daily_pipeline_persists_second_launch_via_generic_scanner_branch`

原因是新的 `second_launch` 状态机要求真实 A 资格，旧 fixture 仍只种入 Legacy flag。

正确处理方式是迁移 fixture，使样例显式满足新的 A 资格；不得为了恢复旧测试而回退新的资格语义。

## 15. 二次修改任务卡

### R0 — 数据真值与交易日历（P0）

**目标：** 少量个股坏行不得删除全市场有效交易日，同时保留对系统性坏日的 fail-closed。

**主要文件：**

- `mining/data_quality.py`
- `mining/selection_context.py`
- `mining/adjusted_prices.py`
- `mining/trend_factors.py`
- 对应专属测试

**防作弊验收：**

- `2026-08-03` 至 `2026-08-05` 可进入可用市场日历和收盘定版。
- 五个坏行只隔离五只股票，不删除 5,197 个有效股票行。
- 全零整日仍为坏日。
- 未确认坏行不得盲目前向填充。
- 20 日窗口确实对应最近 20 个市场交易日。

### R1 — 强趋势分层重定义（P1）

**目标：** 用绝对近期状态定义强趋势，同时覆盖成熟趋势和新晋主升。

**主要文件：**

- `mining/trend_factors.py`
- `mining/scanners/strong_trend.py`
- `mining/watchlist.py`
- 对应专属测试

**防作弊验收：**

- 弱市允许 0 只强趋势。
- top 20% 只影响排名带，不改变绝对状态。
- `688627/301571` 不因旧结构进入强趋势。
- `688141` 不因短期反弹自动进入强趋势。
- `300996/601858` 可通过 `fresh_breakout` 路径。
- 五只股票不允许出现在实现代码的硬编码分支中。
- 输出每只股票的逐条件失败原因。

### R2 — 快照、定版与持久化一致性（P1）

**目标：** 相同价格输入产生相同 A–E 结果，并确保正式定版完整覆盖旧结果。

**主要文件：**

- `mining/selection_runtime.py`
- `mining/selection_context.py`
- `mining/streamlit_tabs/tab_scanner.py`
- `mining/universe.py`
- `mining/db.py`
- `run_daily.py` 仅在解除当前 no-touch 冲突并获得范围许可后修改

**防作弊验收：**

- 相同 snapshot/final bars 时 A–E 候选、字段和排序完全相同。
- 股票和基准具有同一 `price_as_of`。
- 快照中文名称与 ST 过滤真实可用。
- 定版完整性按 `(date,strategy,version)` 检查。
- 同策略同日重跑后旧落选候选被删除。
- 刷新、重启和跨日都不复活旧快照或旧候选。

### R3 — 反例回归、全量集成与文档（P2）

**目标：** 用真实正反例和完整仓库验收替代理想单调样例。

**主要内容：**

- 将已有 66 张图和本次五股建立带标签的正反例集。
- 每张图记录人工标签、关键指标、入选路径和失败条件。
- 新增 fresh breakout、weakening、弱市 0 强趋势、停牌不删市场日等成对测试。
- 更新 `current-opportunity-selection-strategy.md`。
- 完成真实浏览器三态验收。

**防作弊验收：**

- 定向测试与 `python -m unittest` 全部通过。
- 输出逐条件漏斗和代表性增删样例。
- 不以固定命中数、五股硬编码或收益表现证明定义正确。
- 明确区分定义正确性、真实数据 smoke 和收益有效性。

## 16. Opus 审核时应读取的文件

建议按以下顺序阅读：

1. `docs/superpowers/specs/2026-08-06-screening-v2-task-cards.md`
2. 本文：`docs/superpowers/specs/2026-08-06-screening-v2-quality-review.md`
3. `mining/data_quality.py`
4. `mining/adjusted_prices.py`
5. `mining/selection_context.py`
6. `mining/selection_runtime.py`
7. `mining/universe.py`
8. `mining/trend_factors.py`
9. `mining/scanners/strong_trend.py`
10. `mining/streamlit_tabs/tab_scanner.py`
11. `mining/db.py`
12. `tests/test_data_quality.py`
13. `tests/test_strong_trend.py`
14. `tests/test_selection_runtime.py`
15. `tests/test_mining_pipeline.py`
16. `docs/superpowers/specs/current-opportunity-selection-strategy.md`，只用于确认其已过时，不作为现行真值

给 Opus 的建议指令：

> 先审查原任务书和 quality review，再核对实现与测试。重点评审 R0–R3、强趋势双路径、市场日/个股行质量分层、snapshot/final A–E 等价性、按 strategy/version 定版以及 replace 持久化语义。先完善任务书并列出异议，不立即修改代码，不以五只样例调参或硬编码。

## 17. 方法、限制与后续问题

### 17.1 本次方法

- 阅读原任务书与 Terra 的 12 个提交。
- 审查真实控制路径和数据路径。
- 使用本地原始日线复算五只样例。
- 用本地 Streamlit 页面进行只读验收。
- 运行定向测试与全量测试。

五股数量很少，精确审计表比趋势图更适合作为本 MD 的主证据，因此本文件使用表格而未嵌入单独图像；
后续 66 图回归应使用统一形态卡或小多图进行视觉验收。

### 17.2 限制

- 本评审不证明策略具有超额收益。
- `301571` 在 16:50 快照未入 A；用户观察来自此前快照，说明榜单可能随快照变化。
- 建议阈值是定义验收的初始值，必须在独立正反例集上冻结后再实现。
- 当前真实页面是 `close_pending`，尚无最近三日的可靠 close-final 验收。

### 17.3 Opus 需要明确回答的问题

1. 是否同意把“强趋势”改为绝对状态，并把前 20% 降为排名带？
2. 是否同意 A 同时包含 `continuation` 和 `fresh_breakout` 两条路径？
3. 对已确认停牌日，MA/RPS 是否统一使用前收盘维持市场日历？
4. `usable_with_quarantine` 的初始覆盖率和异常行比例阈值是否接受 `98% / 0.5%`？
5. `rps_prior` 是保留为成熟度证据，还是以不超过 10% 的权重继续参与排名？
6. 候选 replace 时已有 outcomes 的迁移/保护规则如何定义？

## 18. Lessons

- 收到的纠正：强趋势必须体现近期状态，不能由长期结构和相对名次替代。
- 验证有效的方法：真实页面快照、原始日线 SQL、逐股反例和定向/全量测试交叉验证。
- 本文只形成项目内评审记录，未更新全局或跨项目记忆。
