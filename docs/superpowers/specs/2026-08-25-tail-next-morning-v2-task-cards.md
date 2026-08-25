# Tail Next-Morning V2 趋势延续筛选与回测任务卡

- 冻结日期：2026-08-25
- 规划与协调会话：`01a02efc-e34c-7ca0-a727-9df00526e7bd`
- 唯一执行会话（Terra）：`01a03137-df48-7281-b7d7-b48f7e41f209`
- 唯一独立审核会话（Sol）：`01a03141-39e9-7dd1-a359-037346adf01b`
- 仓库：`D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard`
- V1 终态：`tnm_v1_closed_no_stable_development_signal`
- 当前阶段：`TNM-V2-1 ready_for_terra`

执行者和审核者把本文当完整合同。规划会话负责业务定义、任务拆分、分歧裁决和最终接受；Terra 只负责实现与执行；Sol 只负责独立只读审核。阶段严格串行，任何阶段未通过都不得读取或运行后续冻结区。

## 1. 第一性原理目标与 V1 首个分歧

用户要找的不是“跌得多、波动低”的股票，而是 D 日 14:50 已出现主动上涨、且强度可能延续到 D+1 开盘后半小时的股票；另保留一个独立的小通道识别“已有上涨趋势、回调缩量、D 日重新止跌转强”的股票。

V1 的数据解析、时间隔离和长批治理已经验证；V1 的经济假设失败。首个业务分歧是旧方向会奖励下跌、低波动或低活跃，并惩罚 `300085`、`300339` 这类高活跃、强上涨样本。V2 只复用 V1 已验证的读取、窗口、因果守卫和 outcome 基础设施，不复用 V1 六特征方向、十分位发现规则或关闭结论。

V2 的最低完成定义：

1. 以固定、可解释的绝对门槛区分“上涨加速/延续”A 通道与“趋势回调止跌”B 通道；
2. 门槛决定是否合格，等权量价 rank 只决定合格者先后，不设主观权重或最低综合分；
3. 固定正例、负例和未来数据变形测试通过后，才允许运行 2023～2024 开发期全量回测；
4. 2023～2024 不通过预注册门槛就停止，不读取 2025/2026；
5. 最终区分“次日开盘后的真实拉升”与“隔夜高开”，并同时报告可交易的固定时间退出收益。

V2 不接生产 scanner、不写生产数据库、不下单、不承诺实盘收益。

## 2. 已确认事实与用户裁决

- 决策时间固定为 D 日 `14:50:00`，尾窗固定为 `[14:20,14:50)`；
- 用户确认 D-1 日成交额严格 `> 200_000_000` 元；暂不做资金容量、参与率、盘口队列或是否能够实际成交的筛选；
- 用户确认两个独立通道：A 为开始上涨/上涨加速，B 为上涨趋势中的回调止跌；
- `2024-09-23 / 300085`、`2024-09-26 / 300339` 是 A 通道必须最终选出的 canary；
- `2024-08-26 / 300972` 用于证明 B 形态定义，`2024-08-27 / 300972` 作为较弱观察日；其 D-1 成交额分别不足 2 亿元，因此只检查形态层，不得进入最终候选；
- `2024-08-13 / 300328` 用于证明 B 形态定义；它应通过最终资格，但是否进入 B Top 2 由当日全市场经济排序决定；
- 无历史 point-in-time ST 数据：`st_filter_applied=false`、`st_status=unavailable_not_filtered`；
- 公司行为识别仍无可靠字段合同：`corporate_action_filter=unproven_not_applied`；
- 2024 样例已参与定义，故 2023～2024 全部属于开发期，不能冒充样本外验证；2025 为一次性 validation，2026-01-01～2026-06-30 为一次性 final。

2026-08-25 的 Claude/Opus 只读方法讨论支持两通道、绝对 gate 决定资格、等权 rank 只做排序，并要求把隔夜 gap 与开盘后拉升拆开。本文采纳这些减少自由度的意见；不采用手工权重、clip、最低综合分或跨通道总分。

## 3. 写入边界与禁止项

### 允许

- 只读 `E:\分钟数据`、`E:\日K线全部至202606`；
- 新增 `mining/tail_next_morning_v2.py`；
- 新增 `tests/test_tail_next_morning_v2.py`；
- 只在本文追加相应阶段的紧凑执行/审核结论；
- 写 ignored `output/tail-next-morning-v2/` 下的可再生产物；
- 复用 `mining.tail_next_morning` 的公开解析、窗口和充分统计函数，不改 V1 代码与 V1 证据；
- 每张执行卡只形成一个白名单本地 commit，不 push。

### 禁止

- 修改 `mining/tail_next_morning.py`、`tests/test_tail_next_morning.py` 或 V1 任务卡/产物；
- 修改 `app_panel.py`、`run_daily.py`、现有 scanner、UI、生产 DB/schema 或 v2.6 选股定义；
- 写 E: 源目录、调用 provider、联网补 ST、使用 L2、安装 ML 依赖；
- 以 D 日收盘后信息、D+1 数据、未来可成交性或 outcome 改变 D 日资格、分数和排名；
- 为使 canary 通过而在 Terra 阶段改公式、阈值、权重、Top N 或样例预期；
- 使用证券代码决定经济排序或边界入选；
- 提交 `output/`、日志、数据库、缓存或大体量中间数据；
- `git add -A`、回退或覆盖既有 dirty 文件、启动 2025/2026、push 或生产激活。

阶段开始与结束必须记录 `git status --short`。若白名单外文件变化，Terra 只报告，不整理、不暂存。

## 4. 冻结数据、时间与 outcome 合同

### 4.1 Point-in-time 股票范围

- 沪深普通 A 股代码前缀：`000,001,002,003,300,301,600,601,603,605,688,689`；排除北交所、B 股、ETF、指数；
- 当日分钟文件集合定义 point-in-time universe，不得与当前日 K 文件清单取交集；
- 至少 20 个 D 前历史交易 session，沿用 V1 已审核的日 K `date` 短路与分钟可见历史降级口径；
- D-1 完整交易日分钟 `amount` 合计严格 `> 200_000_000` 元；等于 2 亿元或缺失/异常均不通过；日 K 全日 amount 不参与资格；
- 计算形态层指标后再应用流动性门，产物同时保留 `shape_pass`、`liquidity_pass`、`eligible_pass` 与逐项失败原因；
- 所需历史或 D `[09:30,14:50)` 数据不完整、价格非正、amount/volume 非有限或为负时 fail closed，不填 0、不插值。

### 4.2 因果时钟

所有区间左闭右开：

| 用途 | 冻结区间 |
|---|---|
| 当日特征 | D `[09:30,14:50)` |
| 尾窗诊断 | D `[14:20,14:50)`，不作硬门槛或评分 |
| 决策 | D `14:50:00` |
| 延迟缓冲 | D `[14:50,14:51)`，不用作特征或成交 |
| 研究买价 | D `[14:51,14:56)` VWAP |
| 次日开盘 | D+1 第一根 `09:30` bar 的 open |
| 次日拉升诊断 | D+1 `[09:30,10:00)` |
| 固定研究卖价 | D+1 `[10:00,10:05)` VWAP |

硬断言：修改 D `14:50` 后任意 bar 或 D+1 任意 bar，不得改变 `shape_pass/liquidity_pass/eligible_pass`、任一 D 特征、percentile、通道、排名或入选成员。

### 4.3 标签与研究收益

- `buy_vwap`：D `[14:51,14:56)` 的 `sum(amount)/sum(volume)`；
- `next_open`：D+1 09:30 bar open；
- 主预测标签 `intraday_mfe_0930_1000 = max(high[D+1 09:30,10:00)) / next_open - 1`，直接回答开盘后半小时是否继续拉升；
- 主命中率：`intraday_mfe_0930_1000 >= 3%`；同时报告 2% 和 5%，不得在看结果后更换主阈值；
- `gap_return = next_open / buy_vwap - 1`；
- `mfe_from_buy = max(high[D+1 09:30,10:00)) / buy_vwap - 1`；
- `mae_from_buy = min(low[D+1 09:30,10:00)) / buy_vwap - 1`；
- `gross_fixed_exit = sell_vwap[D+1 10:00,10:05) / buy_vwap - 1`；
- `net30 = gross_fixed_exit - 0.003`，只作为保守交易摩擦后的统一研究口径；不做资金容量或订单可成交建模；
- outcome 数据缺失或窗口 `sum(volume)<=0` 时标记 unavailable，D 信号仍保留；不得用未来窗口替代、删掉信号或伪造收益。

## 5. 冻结特征定义

所有价格均为分钟源未复权价格。`close1450` 指 `[09:30,14:50)` 最后一根有效 bar 的 close；`open_D` 指 09:30 bar open；`prev_close` 指 D-1 最后一根 close。

### 5.1 共用量价量

- `ret1450 = close1450 / prev_close - 1`；
- `position1450 = (close1450 - low_D_0930_1450) / (high_D_0930_1450 - low_D_0930_1450)`；零振幅为缺失；
- `vwap1450 = amount_D_0930_1450 / volume_D_0930_1450`；
- `vwap_dist = close1450 / vwap1450 - 1`；
- `amount1450 = sum(amount[D 09:30,14:50))`；
- `prev_ma10 = mean(D-10...D-1 的 session close)`；
- `range1450(X) = high_X_0930_1450 / low_X_0930_1450 - 1`；
- `range_expansion = range1450(D) / median(range1450(D-1...D-10))`；
- `activity = amount1450(D) / median(amount1450(D-1...D-3))`；
- `tail_return` 与 `tail_location` 沿用 V1 `[14:20,14:50)` 定义，只输出诊断，永不参与 gate/score/rank。

分母非正或历史不足时相应通道不通过，并输出明确 reason。

### 5.2 A 通道：上涨启动/加速延续

以下条件全部满足才有 `a_shape_pass=true`：

1. `ret1450 >= 0.04`；
2. `position1450 >= 0.55`；
3. `vwap_dist >= 0.01`；
4. `activity >= 1.50`；
5. `close1450 > prev_ma10`。

尾窗可以回吐或横盘，`tail_return`、`tail_location` 不得否决。这是为了保留“前段已完成强势突破、14:20 后消化获利盘”的真实路径。

A 排序只在 `a_shape_pass && liquidity_pass && common_quality_pass` 的当日股票中计算：

- `rank_ret1450 = percentile(ret1450, higher_is_better)`；
- `rank_activity = percentile(activity, higher_is_better)`；
- `rank_range_expansion = percentile(range_expansion, higher_is_better)`；
- `rank_position = percentile(position1450, higher_is_better)`；
- `score_A = mean(上述四项)`。

percentile 固定为 `rank(method="average", pct=True)`。按 `score_A`、四项原始值同方向、`amount1450` 依次排序，名义取 Top 3。若全部经济量仍完全相同，则保留全部边界并列并报告 `boundary_tie_expanded=true`；代码只可在成员已确定后用于文件序列化，不得决定入选。

### 5.3 B 通道：上涨趋势中的缩量回调止跌

- `prior10_high = max(high[D-10...D-1])`；
- `prior10_runup = prior10_high / close[D-10] - 1`；
- `drawdown = close1450 / prior10_high - 1`；
- `pullback_volume_ratio = amount1450(D) / max(amount1450(D-1...D-5))`；
- `body1450 = close1450 / open_D - 1`。

以下条件全部满足才有 `b_shape_pass=true`：

1. `prior10_runup >= 0.15`；
2. `-0.15 <= drawdown <= -0.03`；
3. `close1450 > prev_ma10`；
4. `pullback_volume_ratio <= 0.80`；
5. `close1450 > max(open_D, prev_close)`；
6. `position1450 >= 0.55`。

B 排序只在 `b_shape_pass && liquidity_pass && common_quality_pass` 且未进入 A 的当日股票中计算：

- `rank_prior10_runup = percentile(prior10_runup, higher_is_better)`；
- `rank_volume_contraction = percentile(-pullback_volume_ratio, higher_is_better)`；
- `rank_body = percentile(body1450, higher_is_better)`；
- `rank_position = percentile(position1450, higher_is_better)`；
- `score_B = mean(上述四项)`。

按 `score_B`、`prior10_runup` 降序、`pullback_volume_ratio` 升序、`body1450` 降序、`position1450` 降序、`amount1450` 降序，名义取 Top 2；完全相同的边界并列沿用 A 的处理。

### 5.4 通道合并

- A、B 独立 gate、独立 percentile、独立排序，禁止跨通道比较 score；
- 同一股票同时满足 A/B 时归 A，B 池排除该股票；
- 正常每日最终 0～5 只；无合格者输出空列表，禁止补位；完全经济并列时可因保留边界并列略高于 5，并单独计数；
- 最终输出顺序为 A rank 后 B rank，只是展示分组，不代表 A 永远优于 B。

## 6. TNM-V2-1：实现、定向测试与五日全市场 canary（Terra）

### 6.1 唯一目标

证明公式、时间隔离、流动性门和经济排序按本文运行，并用用户给出的五个日期做全市场 canary。不得运行完整 2023～2024 回测，不得读取 2025/2026 outcome。

### 6.2 白名单与实现约束

- `mining/tail_next_morning_v2.py`；
- `tests/test_tail_next_morning_v2.py`；
- 本文只允许追加 `TNM-V2-1 执行结果`；
- ignored `output/tail-next-morning-v2/canary/`。

V2 应导入并复用 V1 已审核的源定位、分钟解析、窗口/VWAP和充分统计能力；只新增 V2 业务纯函数和最小 canary runner。若现有公开函数不能满足需求，先报告阻塞，不得修改 V1 或复制整套 loader。

固定命令界面：

```powershell
C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m mining.tail_next_morning_v2 canary `
  --minute-root "E:\分钟数据" `
  --daily-root "E:\日K线全部至202606" `
  --output-dir "output\tail-next-morning-v2\canary"
```

canary 仅允许目标 D：`20240813,20240826,20240827,20240923,20240926`。可读取计算这些 D 所必需的 D 前历史和 D+1，不得顺带扫描其他 target D。

### 6.3 必须测试

单元/合成测试至少覆盖：

1. D-1 amount `=200_000_000` 不通过，`>200_000_000` 通过；
2. A 的 `ret1450` 降到 4% 以下即失败；
3. A 在其他量不变时，负 `tail_return` 或 `tail_location=0` 不改变资格/评分；
4. `close1450 <= prev_ma10` 时 A/B 均失败；
5. B 的高开回落（`close1450<=open_D` 或 `close1450<=prev_close`）失败；
6. B 的 prior runup、drawdown 区间、缩量比任一失败即不通过；
7. overlap 固定归 A，A/B percentile 不跨池；
8. 空池不补位；边界并列不使用 code 决定成员；
9. 修改 D 14:50 后或 D+1 任意数据不改变 D 资格、特征、score、rank 和成员；
10. 次日开盘后 MFE、隔夜 gap、从买价 MFE、MAE 与固定退出收益公式分别正确；
11. invalid/zero-volume outcome 只降级 outcome，不删除信号；
12. ST/公司行为状态按本文显式降级。

真实五日 canary 必须断言：

| D | 股票 | 冻结预期 |
|---|---|---|
| 2024-09-23 | 300085 | `a_shape_pass=true`、流动性通过、最终 A Top 3 入选 |
| 2024-09-26 | 300339 | `a_shape_pass=true`、流动性通过、最终 A Top 3 入选 |
| 2024-08-26 | 300972 | `b_shape_pass=true`；因 D-1 amount 不足 2 亿元，最终不入选 |
| 2024-08-27 | 300972 | `b_shape_pass=false`，主要失败项应含 `position1450<0.55`；流动性亦不通过 |
| 2024-08-13 | 300328 | `b_shape_pass=true`、流动性通过；报告 B rank/是否进入 Top 2，不强制 Top 2 |

任一必选 A 未进入 Top 3，或任一固定形态断言不符，结论必须为 `changes_required_by_frozen_canary` 并停止。Terra 不得自行调参。

### 6.4 Canary 产物与验收

产物至少包含：

- `canary_summary.json`：状态、代码/任务卡 hash、五日候选数、断言结果、异常计数；
- `canary_candidates.csv.gz`：五日所有 A/B 最终候选及通道、rank、score、原始指标、outcome；
- `canary_fixture_rows.json`：五个指定股票日的全部 gate、失败原因、原始指标、D-1 amount、排名与 outcome；
- `canary_daily_top.json`：每天 A/B 完整 Top 列表与边界并列状态；
- `artifact_manifest.json`：文件大小与 SHA-256；
- 输出显式写 `capacity_filter_applied=false`、`st_filter_applied=false`、`corporate_action_filter=unproven_not_applied`。

`spec_hash` 固定为本文第 1～10 节在 `CRLF -> LF` 规范化后的 UTF-8 SHA-256，不包含第 11 节执行结果，避免追加证据改变业务合同身份。代码 hash 取实际执行 commit 中 V2 模块与测试文件的 git blob identity；时间戳、PID 和输出路径不得进入经济内容 hash。

最小验收命令：

```powershell
C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_tail_next_morning_v2
C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_tail_next_morning
C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m py_compile mining\tail_next_morning_v2.py tests\test_tail_next_morning_v2.py
git diff --check
```

Terra 必须记录前后 `git status --short`、实际命令、测试数、canary run 路径/hash、白名单 diff、进程与临时文件清理。只暂存白名单代码/测试和本文执行结果，创建一个本地 commit 后停止。完成标签只能为：

- `tnm_v2_1_canary_verified`
- `changes_required_by_frozen_canary`
- `tnm_v2_1_blocked`

## 7. TNM-V2-1R：独立只读审核（Sol）

仅在 Terra 停止并提交后派发。Sol 不改任何文件，独立核验：

- commit/dirty 边界和 V1 未被修改；
- D-1 2 亿元严格门槛、五个 fixture 原始值与逐 gate 结论；
- `300085`、`300339` 的全市场 Top 3 成员资格；
- A/B 分池 percentile、无代码经济 tie-break、无补位；
- 14:50/D+1 未来变形不改变 selection；
- outcome 标签确实区分开盘后拉升、隔夜 gap 与从买价收益；
- canary target 严格限五日、2025/2026 未被读取、E: 未写；
- artifact hashes、测试证据和任务自有进程清理。

结论只能为 `approve|changes_required|blocked_evidence`，findings 分 P0/P1/P2，并提供最小复现。只有 `approve` 才解锁 TNM-V2-2；Terra 不得自审，规划会话不得代替独立审核。

## 8. TNM-V2-2：2023～2024 固定规则开发期回测（审核通过后才派发）

本节预注册统计方法和后续门槛，当前不授权执行。

### 8.1 固定对照

每天使用同一 point-in-time 流动性/质量池、相同名义槽位数和相同 outcome 可用性：

1. `random_same_n`：固定 SHA-256 顺序抽与实际策略同数量股票；
2. `ret1450_same_n`：按 `ret1450` 从高到低取相同数量；
3. `activity_same_n`：按 `activity` 从高到低取相同数量。

不得因看到开发结果新增、删除或更换对照。A、B 必须分别报告，合并组合另报。

### 8.2 样本单位与统计

- 股票事件用于分布描述；正式均值、命中率、超额和推断先按 D 日对当日入选股票等权聚合，再以交易日为样本，避免把同一市场日的多只股票误当独立观测；
- 报告每通道事件数、不同交易日数、月度覆盖、年度覆盖；任一通道少于 100 个事件或 60 个不同交易日，标记 `insufficient_sample`，该通道不能进入 validation；
- 选择后的 outcome 缺失不得反向删除信号或替补。正式指标使用 ready outcome，并同时报告分子/分母和缺失原因；每通道在 2023、2024 各年的主标签与固定退出覆盖率均须 `>=99%`，否则为 `blocked_data_quality`，不得以删样本形成 go；
- 主预测指标：日均 `intraday_mfe_0930_1000` 和日均 3% 命中率；2%/5%、gap、MFE/MAE from buy 为诊断；
- 主经济指标：日均 `net30`；同时报告 gross、月度分布、最大回撤；
- 2023、2024 各自报告，不随机切分，不按股票随机 train/test；
- 开发期置信区间只作诊断，固定以交易日/月为块 bootstrap，不作为调参依据。

### 8.3 进入 2025 的固定门槛

每个希望冻结的通道必须同时满足：

1. 样本数门槛通过；
2. 2023 与 2024 的日均 `net30` 均 `>0`；
3. 2023～2024 合并后，日均 3% 命中率和日均主 MFE 相对三个对照中的最强者点估计均 `>0`；
4. 2023～2024 合并后，日均 `net30` 相对三个对照中的最强者点估计 `>0`；
5. 所有公式、阈值、通道上限和 tie 规则与本文完全一致。

满足才生成唯一 `frozen_rule.json`；否则该通道为 `no_stable_development_signal`。A/B 可独立通过；两者都失败则 V2 正常关闭，不读取 2025。任何参数修改都必须创建 V3 新任务卡，当前 2025 资格清零。

完整长批必须沿用 V1 已审核的单 writer、输入 manifest、code/spec/input hash、按日 checkpoint、hash-bound resume、原子终态、确定性经济产物和 detached 执行合同；具体实现/启动仍须拆成 runner+canary 审核与 detached batch 两张串行子卡。

## 9. TNM-V2-3/4：冻结期门槛（当前锁定）

### 2025 一次性 validation

只加载经 Sol 批准的唯一 frozen rule，一次运行，不调参。每个通道必须同时满足：

- 日均 `intraday_mfe_0930_1000`、3% 命中率和 `net30` 相对三个对照中的最强者点估计均 `>0`；
- 日均 `net30 >0`；
- 公式、样本资格和 outcome 合同无变化。

任一通道失败即该通道 `validation_no_go`；只允许通过的通道进入 2026。查看 2025 后改参数会使当前 2025 永久失去 validation 身份。

### 2026-01-01～2026-06-30 一次性 final

- 只运行 2025 通过的同一冻结通道；
- 2025+2026 以交易日/月为块 bootstrap，`net30` 相对最强固定对照的 95% CI 下沿 `>0`；
- 2026 的主 MFE、3% 命中率和 `net30` 超额点估计均 `>0`；
- 去掉 OOS 表现最佳的一个月份后，累计 `net30` 仍 `>0`；
- 任一失败为 `final_no_go`，不得用 2026 调参后重跑。

通过只表示 `signal_edge_go_research`，仍不表示容量、真实成交或可直接实盘。

## 10. 卡片真值、未知项与 Lessons 决策

### 不变量

- D 14:50 后信息永不进入 selection；
- 门槛决定资格，等权 rank 只排序；
- A/B 不跨池比较；
- 代码不决定经济入选；
- D-1 amount 严格大于 2 亿元；
- 正例只能检验冻结定义，不能证明总体有效；
- 2023～2024 开发、2025 validation、2026 final 严格隔离。

### 当前未知

- 固定 A score 是否能使 `300085`、`300339` 在各自日期进入全市场 Top 3；
- B 的合格频率是否足以达到独立样本门槛；
- 两通道的次日开盘后拉升是否超越简单动量/活跃度对照；
- 未过滤历史 ST 和未识别公司行为对收益的影响。

这些未知由 V2-1/V2-2 证据回答，不能由实现者用新参数消除。

### Lessons 决策

当前 `skip`。本卡已把“形态层与流动性层分开”和“必须区分隔夜 gap 与开盘后拉升”固化为项目合同；只有后续出现可跨项目复用、且有真实失败证据的新规则时，才创建 lesson candidate。

## 11. 执行结果

尚未执行。Terra 完成 TNM-V2-1 后只能在本节追加紧凑证据，不得改写第 1～10 节。
