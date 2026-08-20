# 当前机会挖掘选股口径（v2.6）

状态：当前真值；v2.5 批次保持历史只读，v2.6 以 capability 合并、去重后的 Top 20 为唯一展示与持久化边界。本文描述 `SCREENING_DEFINITION_VERSION` 与 capability registry 已定义的正式 A–E，不把命中数量、页面展示或历史价格路径当成收益有效性证明。

## 1. 数据、股票池与质量边界

正式选股只使用本地收盘数据或明确标记的盘中快照。股票池由统一 universe 构造：沪深主板、创业板（`300`/`301`）和科创板（`688`）可见；北交所及 `43`、`83`、`92` 前缀不进入正式股票池。ST 名称过滤来自本地 `stock_info`；名称或元数据陈旧时会保留其 freshness/provisional 状态，不能宣称它是历史时点真相。

市场日先经过行级质量分类。`clean` 与允许带逐行隔离的可用日可以进入窗口；全市场坏日和 `known_bad_session` 被排除。确认停牌或可验证的 provider 停牌占位可维持历史价格连续性，但当日不可交易，不能成为当日候选；无有效成交、无效 OHLC 或缺失关键字段也不会被伪装成停牌。

原型日质量合同以集中定义的 `0.80 / 0.95 / 0.90` 为边界：系统性坏日、覆盖不足和 present rows 可用率不足仍 fail closed；达到日级门槛但逐行不完整的股票继续 quarantine，绝不进入正式 universe。`2026-07-13` 的 5,199 行虽然数量完整，但没有有效成交且绝大多数是 provider 占位，是“行数不等于可用日”的 fail-closed 反例。

## 2. 正式 A–E 能力

正式能力、字段、排序和版本由 `mining.capabilities.CAPABILITY_REGISTRY` 集中声明。

| 能力 | strategy_id | 定义与展示重点 |
| --- | --- | --- |
| A | `strong_trend` | 强趋势：`close>MA20>MA60`、两条均线不走弱、RPS20≥0.90；60 日高位距离和 MA20 乖离按板块硬门槛。fresh breakout 还需收盘突破、强收盘与活跃确认。 |
| B | `compression_launch`、`momentum_anomaly` | 异动启动：两子策略合并评分并按代码去重。momentum 只认当日需求冲击；compression 的活跃度和 1 亿元成交额均为硬门槛。 |
| C | `second_launch` | 回调支撑/再启动：点时价格路径证明 5–30 日前主升，A 历史仅作证据；近期创新低直接否决。 |
| D | `base_breakout` | 平台突破：15–40 日平台、两次阻力触及、窄箱体与 ATR 收缩，再以强收盘、活跃度和成交额确认。 |
| E | `counter_trend_rs` | 逆势强度。保留现有资格语义，参加能力级排名。 |

市场背景只用于分组说明，不会为了弱市强制删除已经满足 A–E 定义的股票。弱市出现 A 空榜是合法结果。

每个能力先合并其全部子策略，按 `sec_code` 保留最高分并记录 B 的 subtype evidence，再写入 `capability_rank=1..N`。每能力最多 20 条；不足不补位，空榜是有效结果。正式和盘中均调用同一 shortlist helper。盘中若上一预期交易日日线未覆盖，返回 `stale_history` 并展示最近正式日，不跨缺口计算。

## 3. 三种运行模式与页面语义

| 模式 | 页面含义 | 持久化边界 |
| --- | --- | --- |
| `intraday_snapshot` | 盘中临时结果；显示 snapshot 来源、覆盖率和所有 as-of 字段。 | 不写正式候选或 C 状态历史。 |
| `close_pending` | 收盘待定；可显示最后一批临时快照，但提示需经过日线质量检查。 | 不写 close-final。 |
| `close_final` | 通过质量检查后的正式收盘定版。 | 只读或写入当前版本、最新 complete batch 的 `close_final` 记录。 |

正式读取首先验证 batch schema，再选择同一交易日最新 complete batch。缺失 migration、缺列、损坏 schema、无 complete batch 或意外 SQL 异常都会显示结构化未定版/错误状态和空结果；不会回退到 unbatched、v2.0、legacy 或旧 batch 候选。日报和正式 Excel 也只读取 `close_final`。

生产单日正式 A–E 批次使用专用入口：

```powershell
python run_daily.py --base-dir . --date YYYY-MM-DD --formal-only
```

该模式只运行正式 capability、原子持久化 close-final batch，并以 fingerprint 保证同输入复跑零增长；不会执行 legacy scanner、outcome 回填、watchlist、报告或 provider 抓取。目标日必须是已通过 stock/index 质量门禁的 close-ready 日期。

## 4. C 历史覆盖与冷启动

C 在第一个有效 A complete batch 后即可对后续日期运行。面板显示 `a_history_coverage=N/60`：`N<60` 时标记 `bootstrapping/partial recall`，已有 C 候选继续展示，不能被表达成“今天没有机会”。

当前面板普通读取不会现场回算或创建正式 batch。收盘后的有限最近日期追补是显式流程；60 日 A 历史冷启动是另一项授权：先在隔离库 dry-run，输出日期数、空榜数、候选数、fingerprint 与预计写入量；生产写入须另行授权，且不生成 outcomes。

## 5. 隔离、浏览器和当前生产原型边界

浏览器验收可通过 `SCREENING_BASE_DIR` 指向包含四个数据库副本的隔离目录；未设置该变量时保持仓库原有行为。R4 的浏览器、schema rehearsal、真实 smoke 与历史 dry-run 只使用隔离库，启动前后核对生产库 SHA-256。

PROTO-2 在冻结 commit `7fd57fc` 上完成 2026-08-10 受控 canary：该日为 `usable_with_quarantine`，正式 batch id=2，六个 formal runs 共 470 条候选；同 fingerprint 复跑零增长。该历史证据继续保留。

OPS-HARDEN-1B.2 已在冻结修复链上完成 2026-08-11 核心生产闭环：股票有效行 5,180、基准 eligible 5,199、coverage `0.9963454510`、usable `1.0`，四指数齐全；22 个尾项保持结构化未解决（18 `provider_empty`、3 `provider_invalid`、1 `continuity_guard_rejected`），不被伪装成 100% 完整。原子 revalidation 将旧闩锁转为 `clean`，正式 batch id=4、六个 formal runs、候选 359（strong trend 115、compression 1、momentum 204、second launch 0、base breakout 0、counter-trend RS 39），reader 选择日期为 2026-08-11。concept/ETF 仍按独立可选域展示，不阻断核心结果。

2026-08-13 的正常启动进一步完成 2026-08-12 收盘闭环：股票有效行 5,179、coverage baseline 5,190、coverage `0.9978805395`、usable `1.0`、状态 `clean`，四指数齐全；23 个尾项保持在 provider/continuity 排除集合。正式 batch id=5、六个 formal runs、候选 356（strong trend 142、compression 6、momentum 125、second launch 1、base breakout 2、counter-trend RS 80）。这证明普通“打开才更新”链路能够在下一次启动时完成上一 close-ready 日，不要求当天夜间常驻调度器。

收盘修复现采用质量感知未解决集合、单请求可杀死 worker、单代码超时隔离、连续三次 provider 调用错误切源、每 200 条 SQLite checkpoint 和单日 `--formal-only`。普通启动只针对最近已完成交易日运行一个全局互斥 writer；失败后需要显式重试，页面 rerun 不会并发重复写入。详细事故与恢复合同见 `docs/runbook.md`。

## 6. 解释边界

本口径证明的是定义、数据状态和持久化边界可复跑。历史价格路径不是可交易收益：A 股执行评估仍需在 T+1、涨跌停、成本、流动性与样本时点条件下单独验证。

## 7. R4.1 验收补充：prior coverage、dry-run 与确定性页面

`a_history_coverage=N/60` 的 `N` 只统计评估日之前、当前
`SCREENING_DEFINITION_VERSION` 的最新 `complete` A batch 所覆盖的最近 60 个
usable sessions；评估日 T 不进入分母。C 的 A 资格查询和这个 coverage 共用同一
strict-prior 日期集合。因此 T 有 complete A 且 T-1 有 complete A 时，T 的 coverage
只能是 `1/60`，C 只能读取 T-1 的 A。

60 日冷启动仍不是面板副作用，也不是生产写入。隔离库可显式执行：

```powershell
& 'C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe' run_daily.py `
  --base-dir <isolated-sandbox> --history-bootstrap-dry-run `
  --target-date YYYY-MM-DD --sessions 60 --out <report.json>
```

它会先复制 `mining_mvp.db` 为 shadow，再按日期升序逐日走
`build_selection_context → evaluate_formal_capabilities → persist_close_final_batch`。
每一个可处理日写入六个 formal strategy 的原子 close-final batch；
`trend_profile is None` 只记为 `skipped_insufficient_history`。它不写
`outcomes`、legacy candidates、watchlist 或日报。对同一 shadow 的第二次运行必须
复用 fingerprint，`selection_batches`、`strategy_runs` 和 `candidates` 增量均为零。

浏览器验收可使用 `SCREENING_BASE_DIR` 与
`SCREENING_ACCEPTANCE_NOW_CN`。后者只在前者非空时生效，单独设置会 fail closed；
两者均未设置时生产行为不变。在隔离验收中，正式 snapshot 只读 sandbox cache，
不得调用 provider；其他面板盘中 provider 同时关闭，避免外部行情混入证据。页面对
每一状态都显示 `selected/effective/formal trade_date`，只有三者等于目标选择日才可把
该页面作为 formal 证据。
