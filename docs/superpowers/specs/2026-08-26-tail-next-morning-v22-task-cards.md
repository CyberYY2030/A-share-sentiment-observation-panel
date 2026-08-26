# TNM-V2.2：放量异动、止跌通道与跨日退出回测任务卡

状态：`ready_for_terra_runner`

规划所有者：根会话

实现所有者：Terra 会话 `01a03137-df48-7281-b7d7-b48f7e41f209`

独立审核：Sol 会话 `01a03141-39e9-7dd1-a359-037346adf01b`

## 1. 目标、阶段和停止边界

在已审核 TNM-V2-R1 基线 `36e41fb` 上形成唯一 V2.2：

1. A 通道删除当日 `activity` 硬门槛，保留量价排序；每日名义 A6；
2. B 通道要求 D 前出现过放量大涨异动，D 日只要求缩量回调后无大阴线、收在相对强位；每日名义 B3；
3. 选股截止仍为 D 日 14:50，买入仍为 `[14:51,14:56)` VWAP；
4. 实现 A/B 各自的跨交易日条件退出；
5. 先完成 runner、测试和固定两日 canary，经独立 Sol `approve` 后才允许一个 2023～2024 detached 开发批；
6. 开发批完成后由 Terra 终态验收，再交独立 Sol 复核。不得自动读取或启动 2025/2026。

Opus 方法论调用于 2026-08-26 超过三分钟无输出，按项目停损规则终止；未把不存在的 Opus 意见写入定义。

本卡不授权调参循环。canary 或开发结果不佳时原样停止，不在同一版本改阈值后重跑。

## 2. 输入、时间和共同资格

- 分钟数据：`E:\分钟数据`，只读；
- 日 K：`E:\日K线全部至202606`，只读且仅用于 D 前上市资格；
- 开发 target：分钟源中 2023～2024 可形成 D-10 历史和 D+1 的交易日；2025/2026 禁读；
- 每日 point-in-time universe 由当日分钟文件集合定义，不与当前股票清单取交集；
- D−1 全日分钟成交额严格 `>200_000_000` 元；
- D 日信号窗 `[09:30,14:50)`，尾窗 `[14:20,14:50)`；
- D 日 `[14:20,14:50)` 任一 minute high 触及标准涨停价，A/B 均剔除；标准涨停仍按主板 10%、`300/301/688/689` 20%及整数分价计算；
- 历史 ST、公司行为仍不可可靠证明，分别标记 `unproven_standard_board_rule_applied`、`unproven_not_applied`，经济结论最高为 provisional；
- 至少 20 个 D 前交易日；分钟质量、未来隔离、输入身份、checkpoint、resume、原子终态沿用已审核 V2；
- A/B 不重叠：任何完整 A 合格股票不进入 B 池，无论是否进入 A6。

## 3. D 前放量大涨异动的唯一公式

只为 B 使用。对候选股票的 `j ∈ {D-5,...,D-1}` 逐日计算：

```text
impulse_return_j = close_j / close_(j-1) - 1
impulse_amount_ratio_j = full_amount_j / median(full_amount_(j-3), full_amount_(j-2), full_amount_(j-1))
impulse_pass_j = impulse_return_j >= 0.05 AND impulse_amount_ratio_j >= 1.50
```

要求至少一个 `impulse_pass_j=true`。若多个通过，`impulse_day` 依次按 `impulse_return_j`、`impulse_amount_ratio_j` 降序，再按日期降序选择；后续 B 排名中的 impulse 字段均来自同一个 `impulse_day`，不得分别挑最大值。

这一定义只需 D-8～D-1 的既有分钟充分统计，没有大盘、行业、L2 或新数据源。

## 4. A 通道：启动/趋势延续

完整 gate：

1. `ret1450 >= 0.04`；
2. `close1450 > open_D`；
3. `position1450 >= 0.55`；
4. `vwap_dist >= 0.01`；
5. `range_expansion >= 1.50`；
6. `close1450 > prev_ma10`；
7. `tail_return >= -0.03`；
8. `tail_limit_touch=false`；
9. `close1450 >= prior10_high`，或 `prev_ma5 > prev_ma10 AND close1450 > prev_ma5`。

删除旧 `activity>=1.50` gate。A 排名公式保持：

```text
rank_price = percentile(ret1450)
volume_score = mean(percentile(activity), percentile(amount1450))
rank_volatility = percentile(range_expansion)
score_A = mean(rank_price, volume_score, rank_volatility)
```

经济排序依次为 `score_A, ret1450, volume_score, range_expansion, amount1450, vwap_dist, position1450, tail_return` 降序。名义取前 6；不足 6 不补票。全部经济字段仍完全并列时全部保留并标记 `boundary_tie_expanded=true`，代码只稳定展示，不得决定经济成员。

## 5. B 通道：放量异动后的缩量回调止跌

完整 gate：

1. 第3节 `impulse_pass=true`；
2. `prior10_runup >= 0.15`；
3. `-0.15 <= drawdown <= -0.03`；
4. `close1450 > prev_ma10`；
5. `pullback_volume_ratio <= 0.80`；
6. `position1450 >= 0.55`；
7. `body1450 >= -0.03`，只去除大阴线，不再要求 D 日收红；
8. `tail_return >= -0.03`；
9. `tail_limit_touch=false`。

删除旧 `close1450 > max(open_D,prev_close)`、`body1450>=0.01`、`range_expansion>=1.00`。D 日 `activity` 不作 gate。

B 排名：

```text
score_B = mean(
  percentile(impulse_return),
  percentile(impulse_amount_ratio),
  percentile(-pullback_volume_ratio),
  percentile(position1450)
)
```

经济排序依次为 `score_B, impulse_return, impulse_amount_ratio, -pullback_volume_ratio, position1450, body1450, tail_return, amount1450` 降序。名义取前 3；不足 3 不补票；完全经济并列处理同 A。

## 6. “其他相对强度”的裁决

V2.2 不新增大盘、行业或板块相对强度。现有定义已包含相对个股自身历史的最小充分强度：

- A：`activity` 相对近3日、`range_expansion` 相对近10日、价格相对 MA5/MA10/前高；
- B：异动量相对其前三日、回调量相对近5日、价格相对 MA10/前高回撤。

这些字段足以检验当前假设。新增横截面相对强度会改变问题并增加调参自由度，留待结果显示明确缺口后另开版本。

## 7. 买入与跨日退出

### 7.1 买入

- 成员在 14:50 冻结；
- 买价为 D `[14:51,14:56)` 有效成交量的 VWAP；
- 买窗无效或成交量为0：保持成员及名义槽位，标记 `unavailable_buy`，不替补；
- 主净收益固定为完成 round-trip 的 `gross_return - 0.003`。

### 7.2 A 退出

从 D+1 起逐个真实交易日检查：

1. 10:30 决策只使用最后完成的 `10:29` minute close；
2. 当日涨停价用该交易日前一交易日 full-session close 和标准板涨停率按整数分计算；
3. 若 `close_1029 >= limit_up_price`，继续持有；
4. 否则在 `[10:30,10:35)` 按 VWAP 卖出；
5. 决策 bar、前收或卖出窗无效/零量时不猜价格、不换窗，继续持有至下一交易日同一规则。

### 7.3 B 退出

从 D+1 起逐个真实交易日检查：

1. 11:00 决策只使用最后完成的 `10:59` minute close；
2. `day_return_1100 = close_1059 / previous_session_close - 1`；
3. `day_return_1100 < 0.03` 时，在 `[11:00,11:05)` VWAP 卖出；
4. `day_return_1100 >= 0.03`（含恰好3%）继续持有；
5. 决策字段或卖出窗无效/零量时继续持有至下一交易日同一规则。

停牌、缺 bar、无量跌停均不得使用不可执行价格结算。退出收益归属原信号日。2024 末仍未退出的事件标记 `unresolved_at_development_end`；禁止读取 2025 来补结算。

## 8. 每日候选、槽位和统计口径

每个信号日必须输出：

- `A_eligible_count/B_eligible_count`：排名前完整合格数；
- A6、B3 的成员、分项 rank、总分和全部 gate；
- 名义槽位 `A=6,B=3,combined=9`；不足额槽位按现金0保留；边界并列扩展另报，不伪称严格6/3。

### 8.1 事件研究（主要回答“规律是否存在”）

按 A、B、合并分别报告，且 2023、2024、合并分别报告：

- selected、bought、resolved、unresolved 数及 outcome coverage；
- 净胜率 `mean(net_return>0)`、毛胜率；
- gross/net 的平均数、中位数、标准差、P10/P25/P75/P90、最小/最大；
- `sum_event_net_return`，明确它是逐笔收益算术和，不是账户收益率；
- 平均/中位持有交易日、最长持有日；
- 每个原信号日按固定 6/3 槽等权，空槽为0，得到 `daily_slot_net`；报告其均值、中位数、算术累计、诊断复合值和最大回撤。延期收益仍归原信号日；该复合值因事件重叠不能称为500万元账户收益。

任一正式均值先按原信号日聚合，再以交易日为样本；不能把同日多只股票当独立观测做显著性推断。

### 8.2 500万元九袖套账户（次要、可执行总收益）

另做唯一简单账户模拟：初始500万元等分为 `A1...A6,B1...B3` 九个固定 rank 袖套，每袖套初始 `5000000/9` 元并独立复利。

- 袖套空闲时，买入当日对应通道/排名信号；当日该排名不存在则持现金；
- 袖套仍持有旧仓时，跳过当天同排名新信号，不挪用其他袖套、不借款；
- 上午退出所得现金可在同日 14:51 再用于该袖套新信号；
- 允许分数股，忽略容量和整手，只扣 round-trip 30bps；
- 日末持仓按当日最后有效 minute close 盯市，输出 NAV 曲线、账户总收益率、年化仅作诊断、最大回撤、资金利用率、跳过信号数；
- 若开发期末有未退出持仓，规则总收益写 `blocked_unresolved_account`；另报最后收盘盯市 NAV，但必须标记 `diagnostic_mtm_not_rule_exit`。

## 9. 固定对照与样本检验

每天在同一通道完整合格池内、使用相同名义槽位和该通道相同退出规则，固定三组对照：

1. `random_same_n`：按 `SHA256("20260826|V22|trade_date|channel|sec_code")` 升序；
2. `ret1450_same_n`：`ret1450` 降序；
3. `activity_same_n`：`activity` 降序。

不足额、买入失败、延期退出和 unresolved 与策略完全同口径；不得看结果后更换对照。

报告：

- 每通道事件数、不同信号日数、月份覆盖；少于100事件或60个不同日标记 `insufficient_sample`；
- 2023、2024 分年结果与月度表；不随机拆股票、不做随机 train/test；
- outcome coverage 分通道/年份均需 `>=99%` 才可给正式经济判断，否则 `blocked_data_quality`，仍展示描述性结果；
- 对 `daily_slot_net`、胜率和相对最强对照差值做按月份重采样的 block bootstrap 95% CI，仅作不确定性诊断；
- 同时报告去掉表现最好一个月后的累计 `daily_slot_net`；
- V2.2 由2024样例参与定义，2023～2024全部是开发样本，不得声称样本外有效；本卡不自动生成 2025 frozen rule。

## 10. 产物与证据

canary 与 development 分目录、旧 identity 永久只读。至少输出：

- 原子 `run_manifest.json/progress.json/completion.json`、按日 checkpoint、hash manifest；
- 每日完整合格数和 A6/B3；
- 逐事件买入、每日决策、延期原因、退出、持有日、gross/net；
- `daily_results.csv.gz`、`event_results.csv.gz`、`account_ledger.csv.gz`、`account_nav.csv.gz`；
- `development_summary.json`：第8～9节全部指标、年度/月度/对照/coverage/未决状态；
- 输入实际消费 identity、code/spec/input/run hash、2025/2026未读、E盘未写、异常和任务进程清理。

经济 JSON/CSV.gz 必须确定性；时间戳、PID、绝对输出路径不进入经济 hash。

## 11. 串行执行卡

### TNM-V22-1：Terra runner + 单测 + 唯一 canary

tracked 白名单仅：

- `mining/tail_next_morning_v2.py`；
- `tests/test_tail_next_morning_v2.py`；
- 本卡第12节执行证据。

必须先测试纯 gate、impulse 同日绑定、排名、A6/B3、不足额/并列、10:29/10:59决策、整数涨停、3%边界、跨日状态、无量/缺bar、阶段末 unresolved、九袖套现金占用、未来数据隔离、resume/content identity。V1 25 tests 必须继续通过。

固定两日 canary 只读 `20240923/20240926` 全市场及形成退出所需的后续2024日；300085、300339 必须进入各自 A6。输出新候选数和名单，但不得因不符合主观预期调参。只允许一个真实 canary identity。完成一个白名单 commit 后停止，交独立 Sol；Sol `approve` 前不得启动完整开发批。

### TNM-V22-1R：Sol 独立只读审核

核验定义与代码逐项一致、D 14:50未来隔离、退出无前视、九袖套不透支、输入/终态证据、两 fixture、白名单/dirty边界。只能 `approve|changes_required|blocked_evidence`。

### TNM-V22-2：Terra detached 2023～2024 开发批

只能从 Sol 批准的 clean runner commit 冻结新 run identity。先做 bounded preflight；通过后只启动一个 detached 单 writer，记录 PID/命令/目录后由 heartbeat 低频只读监控，模型不得轮询大产物。失败保留原子证据，禁止盲目重启/resume。成功后 Terra 只验收终态、汇总和 hash，追加第12节并做 docs-only commit。

### TNM-V22-2R：Sol 最终只读审核

独立复算固定日期、年度/月度聚合、事件胜率/均值、固定槽位、三对照、九袖套账本/NAV、coverage、未决、hash/identity及2025/2026未读。只有 `approve` 后规划会话才向用户交付回测结论；不得自动启动2025。

## 12. 执行证据

尚未执行。Terra 只能在本节追加紧凑证据，不得改写第1～11节。

### TNM-V22-1（Terra，2026-08-26）

状态：`tnm_v22_1_blocked_performance`。实现与最小验证已完成：V2 37 tests、V1 25 tests、`py_compile`、`git diff --check` 均通过。唯一真实 identity `v22-canary-3178f827ce5b-e4e9b6ccc022`（run hash `e4e9b6ccc0222d5dfb153566c3d6efaeaf0c5ee48ad136b09a91120ea3c234ba`，spec hash `3178f827ce5b347fa5eea62605fff9d903cbc24a5eb2848098c3754876e30342`）在 2026-08-26 11:00:58 +08:00 启动，3分38秒后仍为 `initializing`、0 checkpoint；按项目三分钟无新信号规则仅终止任务自有 PID 49028，未创建第二 identity、未执行 2025/2026、未写 E:。保留 `output/tail-next-morning-v2/v22-canary/v22-canary-3178f827ce5b-e4e9b6ccc022/` 的 `run_manifest.json` 与 `progress.json` 原样供 Sol 归因；未产生可核验的 A6/B3、fixture 或经济结果，不得进入开发批。

Lessons：`skip`。本卡是项目专属经济假设，尚无跨项目复用证据。
