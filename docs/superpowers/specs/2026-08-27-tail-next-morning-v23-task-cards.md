# TNM-V2.3：下午退出、组合风险暂停与连续五日流动性任务卡

状态：`ready_for_terra_implementation`

规划所有者：根会话

实现与执行所有者：Terra 会话 `01a03137-df48-7281-b7d7-b48f7e41f209`

独立只读审核：Sol 会话 `01a03141-39e9-7dd1-a359-037346adf01b`

冻结基线：`b2fb1c8f4d988be28ad87077b4308592193596a7`

## 1. 目标、唯一版本与停止边界

本卡在已完成 TNM-V2.2 上形成唯一 TNM-V2.3，直接检验以下完整规则：

1. A/B 的普通退出统一改到下午开盘后观察；
2. 账户出现组合性亏损时强制减仓并进入三个交易日只选股、不买入的风险暂停；
3. A/B 共同流动性门槛改为 D 前连续五个交易日每日成交额严格大于2亿元；
4. 其他 A/B gate、score、A6/B3、14:50 截止、14:51 买入、30bps 和 same-N 对照保持 V2.2 原定义。

不生成“只改退出”“退出加暂停”等分层经济版本，不重跑 V2.2 基线，不在结果后改阈值重跑。
固定 same-N 对照是排序有效性的检验，不属于分层调参，继续保留。

阶段顺序固定为：Terra 实现和合成测试 → 唯一 bounded canary → Sol 独立只读审核 →
Terra 唯一 detached 2023～2024 开发批 → Terra 终态验收 → Sol 最终只读审核。
任一阶段失败必须保留证据并停止，禁止盲目 restart/resume。2025/2026 全程锁定。

## 2. Truth card

### 2.1 定义、输入与输出

- 分钟数据：`E:\分钟数据`，只读；只允许读取形成 2023～2024 开发 target、历史特征和阶段内退出所需的数据；
- 日 K：`E:\日K线全部至202606`，只读且只用于 D 前上市资格；不得通过文件尾或整表加载越过 2024 边界；
- point-in-time universe、标准板涨跌停、历史 ST/公司行为限制沿用 V2.2；
- V2.2 的代码提交、SUCCEEDED/FAILED identity、经济产物和审计补充永久只读；V2.3 使用新 spec/run identity 和独立输出目录；
- 继续在 `mining/tail_next_morning_v2.py` 内复用现有解析、gate、score、统计和流式 runner，不新建平行策略模块或通用回测框架；
- 新增唯一 CLI/runner 身份 `v23-development`，不得让 V2.3 输出复用或覆盖 V2.2 identity；
- 每日候选、逐事件结果、实际账户账本、风险状态、NAV、summary 和 artifact manifest 均为正式输出。

### 2.2 状态含义

- `NORMAL`：账户按 A/B 普通退出规则处理，空闲袖套可在 14:51 买入；
- `RISK_OFF`：风险触发日及冻结期只计算并记录候选，实际账户不得买入；
- `cooldown_skip`：候选和买窗有效，但因 `RISK_OFF` 跳过，不得混入 `busy_skip` 或 `unavailable_buy`；
- `forced_risk_exit`：风险触发覆盖普通 A/B 持有判断后的实际卖出；
- `forced_exit_unavailable`：风险退出窗无有效量价，持仓继续存在，后续交易日同窗重试；
- `busy_skip`：仅表示对应固定袖套仍被旧仓占用；
- `unresolved_at_development_end`：到 2024 最后可用交易日仍没有规则允许的真实退出，禁止读取 2025 补结算。

### 2.3 明确排除

- 不新增大盘、行业、板块、L2、ST 推断、止损网格、仓位优化或机器学习；
- 不改变 A/B 排名公式，不因连续五日流动性造成候选不足而降低门槛或补票；
- 不用 D 日收盘后数据选股，不用不可成交价格结算；
- 不将风险暂停后的账户结果解释成选股 alpha；事件研究和账户模拟必须分开报告。

## 3. 连续五日流动性唯一公式

信号日为 D。对共同资格池的每只股票读取前五个真实交易日 `D-5...D-1` 的全日分钟成交额：

```text
prior5_amount_pass_count = count(full_amount_j > 200_000_000, j in D-5...D-1)
prior5_amount_min = min(full_amount_D-5 ... full_amount_D-1)
prior5_amount_all_gt_200m = (prior5_amount_pass_count == 5)
```

只有 `prior5_amount_all_gt_200m=true` 才可进入 A/B 完整合格池和排名。

不变量：

- 比较为严格大于；恰好 `200_000_000` 失败；
- 缺任一交易日、文件无效、全日 amount 非正或少于五个有效前交易日均失败；
- 不使用 D 日全日成交额；D 日只使用截至14:50的既有量价特征；
- A/B 每日完整合格数必须在应用五日门槛后统计；不足 A6/B3 保留空槽，不递补失败成员；
- 输出五个日期、五个 amount、最小值、通过日数和失败原因 `prior5_amount_not_all_strictly_above_200m`。

冻结实证边界：

- `300085@20240923` 的 D-5...D-1 成交额约为 1.0350亿、0.7173亿、0.6204亿、1.7516亿、17.3238亿，
  `pass_count=1`，V2.3 必须因五日流动性失败被剔除；
- `300339@20240926` 的 D-5...D-1 成交额约为 11.8740亿、20.3092亿、20.9916亿、18.6513亿、18.6252亿，
  `pass_count=5`，必须通过共同流动性门槛，随后再按原 A/B gate 和 score 决定排名。

## 4. A/B 选股与买入保持不变

除第3节共同流动性外，V2.2 第3～6节全部冻结：

- A gate、A score、经济排序、名义 A6 和边界并列处理不变；
- B impulse 同日绑定、B gate、B score、经济排序、名义 B3 和 A/B 不重叠不变；
- 信号 cutoff 为 D 14:50，尾窗 `[14:20,14:50)`；
- `[14:20,14:50)` 触及标准涨停价继续剔除；
- 买价仍为 D `[14:51,14:56)` 有效成交量 VWAP；无量/无效不替补；
- 完成 round-trip 的事件净收益仍为 `exit_price / buy_price - 1 - 0.003`。

## 5. 下午普通退出

从 D+1 起逐个真实交易日检查。决策只使用已经完成的 `13:04` minute close，卖出只使用其后的
`[13:05,13:10)` 有效成交量 VWAP；禁止用同一未完成 bar、13:10 后价格或当日收盘价替代。

### 5.1 A 通道

1. 使用当日前一交易日 full-session close 和标准板整数分价计算涨停价；
2. `close_1304 >= limit_up_price`：继续持有；
3. 否则在 `[13:05,13:10)` VWAP 卖出；
4. 前收、决策 bar 或卖窗无效/零量：继续持有，下一交易日重试。

### 5.2 B 通道

```text
day_return_1304 = close_1304 / previous_session_close - 1
```

- `day_return_1304 >= 0.03`，含恰好3%：继续持有；
- `day_return_1304 < 0.03`：在 `[13:05,13:10)` VWAP 卖出；
- 前收、决策 bar 或卖窗无效/零量：继续持有，下一交易日重试。

旧 `10:29/10:30-10:35` 与 `10:59/11:00-11:05` 只属于 V2.2。V2.3 的 spec hash、消费身份、
测试和输出中不得把旧窗口当作 V2.3 经济定义。

## 6. 组合风险触发与三日暂停

### 6.1 观察顺序与公式

实际500万元九袖套账户每天按以下顺序处理：

1. 在普通 A/B 退出前，对 `13:04` 时点全部实际持仓做组合快照；
2. 对每只持仓计算若按当前价退出的净收益：

```text
r_i = mark_1304_i / buy_price_i - 1 - 0.003
holding_count = 实际持仓数
all_losing = holding_count >= 3 AND all(r_i < 0)
mean_holding_return = arithmetic_mean(r_i)
mean_loss_over_5pct = holding_count > 0 AND mean_holding_return < -0.05
risk_trigger = all_losing OR mean_loss_over_5pct
```

恰好0收益不属于亏损；恰好平均 `-5%` 不触发“超过5%”；至少3只限制只作用于 `all_losing`。
平均值按持仓只数等权，与九袖套设计一致。

`mark_1304_i` 优先使用当日有效 `13:04` close；缺失时只允许使用不晚于该时点的最近有效市场价格做风险估值，
同时标记 stale 日期和数量。stale mark 不能充当卖出价。若连历史有效 mark 都没有，该日风险快照为
`risk_snapshot_unavailable`，不得用部分持仓推断“全部亏损”或平均亏损。

### 6.2 触发后的执行

- `risk_trigger=true` 时，风险规则覆盖该日 A 涨停继续持有和 B 涨幅继续持有判断；
- 全部可卖持仓在 `[13:05,13:10)` VWAP 记录 `forced_risk_exit`；无有效卖窗的持仓记录
  `forced_exit_unavailable` 并在后续交易日同窗继续尝试；
- 触发日 14:51 不买入；触发日不计入三个完整暂停日；
- 随后的三个真实交易日继续形成 A6/B3、买窗和假设事件结果，但实际账户全部记录 `cooldown_skip`；
- 原风险触发在无新仓期间不重复计数，也不滚动延长三个交易日；
- 恢复必须同时满足：三个完整暂停交易日已经结束、账户在前一交易日结束时已清仓；
- 因无法卖出而晚于第三日清仓时，清仓当日仍不买入，下一真实交易日恢复 `NORMAL`；
- 最早恢复日为触发后的第4个真实交易日。

风险暂停只属于实际账户。逐信号事件研究仍对每日全部被选且买窗有效的候选应用第5节独立退出，
不因账户 `cooldown_skip` 删除事件，由此才能分别判断选股规律和账户保护效果。

## 7. 账户、统计与审计产物

账户仍为500万元等分 `A1...A6,B1...B3` 九个固定 rank 袖套，不挪用、不借款、允许分数股。
普通下午退出所得现金可在同日14:51用于新信号；风险强制退出日禁止同日买入。

除 V2.2 已有指标外，必须新增：

- 风险触发次数，分别由 `all_losing`、`mean_loss_over_5pct`、两者同时触发的次数；
- 每次触发的持仓数、每只 `r_i`、平均收益、stale marks、计划/实际退出数；
- 暂停交易日、最早恢复日、实际恢复日、因未清仓延迟的天数；
- `cooldown_skip` 数量、候选清单及其假设净收益；
- 暂停规避的假设亏损和错过的假设盈利，必须分别报告，不合并成单一“保护收益”；
- 实际账户逐笔买卖价、窗口、费用、持有日、实现收益、袖套现金、日末持仓和 NAV；
- 账户总收益、胜率、平均/中位交易收益、最大回撤、资金利用率、最长连续亏损、年度和月度结果；
- 事件层 A/B/AB 的胜率、均值、中位数、分位数、固定槽 `daily_slot_net`、月份 block bootstrap、
  去掉最好一个月、三个原 same-N controls 和 coverage，口径沿用 V2.2。

正式产物至少为：

- `daily_results.csv.gz`、`event_results.csv.gz`；
- `account_ledger.csv.gz`、`account_nav.csv.gz`；
- `account_holdings_1304.csv.gz`、`risk_state.csv.gz`、`cooldown_skips.csv.gz`；
- `development_summary.json`；
- 原子 `run_manifest.json/progress.json/completion.json`、checkpoint/outcome journal、artifact manifest。

经济产物必须确定性。时间戳、PID、绝对输出路径不进入经济 hash。事件账和账户账必须能用独立脚本逐笔复算。

## 8. 未知项与结论上限

- 历史 ST 状态：`unproven_standard_board_rule_applied`；
- 公司行为：`unproven_not_applied`；
- 13:05 卖出队列与真实冲击成本：`partial`，沿用现有 VWAP+30bps 简化；
- 2023～2024 已参与规则形成，只是开发样本；任何经济结论最高为 `provisional_development_only`；
- 2025/2026：`locked_unread`；只有 V2.3 完整冻结且 Sol approve 后，规划会话才能另行决定是否授权2025。

上述未知不授权新增数据源或扩大实现范围。

## 9. 串行执行与所有权

### TNM-V23-1：Terra 实现、测试和唯一 canary

tracked 白名单仅：

- `mining/tail_next_morning_v2.py`；
- `tests/test_tail_next_morning_v2.py`；
- 本卡第10节执行证据。

要求：

1. 先搜索并列出旧成交额与旧退出窗口的全部生产、测试、说明消费者；
2. 复用现有模块和流式 runner，不创建平行策略文件或抽象框架；
3. 保持 V1 25 tests 和 V2.2 既有合成回归通过；新增 V2.3 测试不得改写 V2.2 期望冒充通过；
4. 至少覆盖：五日 amount 的严格边界/缺日/正反样例、13:04/13:05 时间隔离、A涨停、B恰好3%、
   1/2/3只全亏、恰好0、平均恰好-5%、低于-5%、风险覆盖普通持有、部分不可卖、stale/完全缺 mark、
   触发日及后3日不买、最早D+4恢复、延迟清仓、事件不受 cooldown 删除、九袖套现金和未来数据隔离；
5. 新 spec hash 必须绑定本卡冻结部分、代码 blob、测试 blob和输入 manifest；V2.2 产物 hash 不变；
6. canary 只读固定 `20240923/20240926` 及形成历史/2024内退出所需的数据；必须核验
   `300085@20240923` 五日门槛失败与 `300339@20240926` 五日门槛通过，并输出两日完整 eligible/A6/B3；
7. canary 额外使用合成账户轨迹验证风险暂停，不为制造真实触发而挑日期；
8. 只允许一个真实 canary identity。完成一个白名单 commit 后停止并交 Sol，不得启动完整开发批。

最小验证：

```powershell
python -m unittest tests.test_tail_next_morning_v2
python -m unittest tests.test_tail_next_morning
python -m py_compile mining/tail_next_morning_v2.py tests/test_tail_next_morning_v2.py
git diff --check
```

### TNM-V23-1R：Sol 独立只读审核

核验经济定义逐项一致、正反五日 amount fixture、未来隔离、下午退出无前视、风险触发边界、暂停状态、
账户与事件分离、费用、九袖套现金、身份/hash/原子终态、tracked 白名单和 shared dirty。
verdict 只能为 `approve|changes_required|blocked_evidence`。

### TNM-V23-2：Terra 唯一 detached 2023～2024 开发批

仅在 Sol approve 后执行。先做 bounded preflight，冻结 approved runner commit、spec/input/code/test hash；
随后只启动一个 detached 单 writer。记录 PID、完整命令、run identity、日志和输出目录后交低频只读 heartbeat。
模型会话不得承载重计算，不得轮询大经济产物。PID 消失且无原子终态时立即 blocked，禁止盲目 resume。

SUCCEEDED 后 Terra 只做终态验收和紧凑汇总：target/信号/成交/未决数量、风险触发/暂停/恢复、事件与账户
核心指标、所有正式产物 hash、旧 V2.2 identity 未变、2025/2026 未读、任务进程清理；只在本卡第10节追加
证据并做 docs-only commit。

### TNM-V23-2R：Sol 最终独立只读审核

独立抽查和复算五日门槛、每日 A6/B3、买卖流水、13:04 快照、风险触发、三日暂停、恢复、
`cooldown_skip` 假设收益、事件聚合、same-N controls、九袖套 NAV、coverage、未决、hash/identity、
旧产物只读边界及 2025/2026 未读。只有 `approve` 后规划会话才向用户交付正式结果。

## 10. 执行证据

### TNM-V23-1（Terra，2026-08-27）

- 状态：`tnm_v23_1_blocked`。合成 V2 58 项（1 项显式 real-source skip）、V1 25 项、`py_compile` 与 `git diff --check` 均通过；新增覆盖五日严格门槛、13:04/13:05 隔离、A/B 边界、风险快照/三日暂停/强制退出和最小生产链。
- 唯一真实 canary：`v23-canary-6a8b222e31a1-6e38e7d68ffa`，`run_hash=6e38e7d68ffaa96ecc8e9825b90e7c12398e68533c859b2893ae0f08f0d06835`。在 900 秒受控上限后原子 `CANCELLED`（实际 `908.781s`，`reason=canary_time_limit`）；无经济 checkpoint、无 A6/B3 或 fixture 结论，禁止重跑/调参。
- 已保留只读证据：`run_manifest.json`、`progress.json`、`completion.json`、stdout/stderr；progress 记录九个 2024 source 且均 `open_count=1`（D/D-1 全市场和部分幸存代码历史）。未读取 2025/2026、未写 E:、未改 V2.2 identity/产物；任务 PID 已退出。
- 环境说明：`dispatching-task-cards/CASE_LAW.md` 本机缺失，标记 `environment-blocked`，不扩大冻结范围。shared dirty 与暂存边界保持原状；等待规划会话决定是否另立性能修复卡。

Lessons 决策：`skip`。本卡是尚未验证的项目专属经济规则，没有形成跨任务可复用的新事实。
