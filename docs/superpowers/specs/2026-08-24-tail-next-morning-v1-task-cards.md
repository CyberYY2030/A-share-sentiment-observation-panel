# Tail Next-Morning V1 离线回测任务卡

- 冻结日期：2026-08-24
- 规划与协调会话：`01a02efc-e34c-7ca0-a727-9df00526e7bd`
- 唯一执行会话（Terra）：`01a03137-df48-7281-b7d7-b48f7e41f209`
- 唯一独立审核会话（Sol）：`01a03141-39e9-7dd1-a359-037346adf01b`
- 仓库：`D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard`
- 冻结起点：`codex/screening-v26@8c2b436`
- 当前阶段：`TNM-1 ready_for_implementation`

执行者和审核者把本文当完整合同。执行工作只能由 Terra 完成，独立审核只能由 Sol 完成；规划会话负责冻结定义、处理分歧、决定是否进入下一阶段。阶段严格串行，不允许执行与审核同时修改同一文件。

## 1. 用户目标与最低完成定义

目标不是先造一个复杂模型，而是验证一个可交易的因果问题：

> 在 D 日 14:50 已知的信息中，哪些尾盘、当日和近 2～3 日特征，能够稳定解释并预测 D 日尾盘买入后、D+1 早盘卖出的净收益？

V1 只回答“是否存在稳定、可复现、样本外仍成立的规律和逻辑结构”。V1 不接生产扫描、不写生产数据库、不下单、不承诺实盘收益。

最终只能产生两个分层结论：

- `signal_edge_verdict`：规律是否通过样本外检验；
- `capacity_verdict_5m`：500 万总资金、Top 10 等权时的成交容量诊断。

容量不参与特征选择，也不允许掩盖或制造信号；容量不通过只阻断部署，不改变规律研究本身的结论。

## 2. 已核验事实，不得重复 discovery

- `E:\分钟数据` 覆盖 2023-01-03～2026-07-08，共 839 个交易日容器；每日约 4,929～5,959 个文件。
- 分钟字段固定为 `open,high,low,close,amount,volume`；代表性跨年样本均为 240 行且价格有效。
- 行 1 对应 `09:30:00～09:30:59` 交易区间，通常显示为 09:31；与 2026-06-01 `002149.SZ` L2 聚合零偏移对齐时，close 相关性 `0.999773`、volume 相关性 `0.994215`。
- 源结构同时存在按日 ZIP、按日目录和路径前缀变化；实现必须统一只读处理，不可把源格式差异解释为停牌。
- `E:\日K线全部至202606` 有 5,532 个股票 `.xlsx`，截至 2026-06-30；抽查 `000001`、`300750`、`600000` 的日成交额与分钟汇总比值约为 `1.000000`。
- 当前完整 L2 只覆盖约 6 个标的、2026-06 的 21 个交易日，不能支持全市场 V1。
- 当前无历史 point-in-time ST 状态表。V1 允许暂不排除 ST，但所有产物必须写 `st_filter_applied=false`、`st_status=unavailable_not_filtered`，不得声称“非 ST 回测”。
- 当前用户设定总资金 500 万；V1 以 50 万/股做容量诊断，不把资金规模作为研究筛选条件。

## 3. 总体禁止项与写入边界

### 允许

- 只读 `E:\分钟数据`、`E:\日K线全部至202606`；
- 新增或修改本文明确白名单中的研究代码和测试；
- 写 `output/tail-next-morning-v1/` 下可再生研究产物；
- 每张执行卡产生一个独立、可回退的本地 commit；只暂存白名单，不得 `git add -A`。

### 禁止

- 修改 `app_panel.py`、`run_daily.py`、现有 scanner、生产 DB/schema、正式选股定义或 v2.6 代码；
- 写 E: 源目录、调用行情 provider、联网补 ST、使用 L2、安装 ML 依赖；
- 使用完整 D 日日 K 成交额做 14:50 决策；
- 随机拆分股票日期样本、查看冻结期后再调参、删除不可成交样本；
- 提交 `output/`、数据库、缓存、日志或大体量中间数据；
- push、生产激活或自动交易。

工作树已有并发 dirty 文件。所有阶段开始和结束都要记录 `git status --short`，不得回退、覆盖、暂存或顺手格式化这些文件。

## 4. 冻结业务与时间合同

### 4.1 股票范围

- 沪深普通 A 股代码前缀：`000,001,002,003,300,301,600,601,603,605,688,689`；
- 排除北交所、B 股、ETF、指数；
- 至少 20 个历史交易日；上市日证据优先取日 K 首日，缺失时用分钟源可见历史并标记 `listing_age_source`；
- D-1 日成交额严格 `> 500_000_000` 元，直接由分钟数据聚合；
- D-3～D+1 必需窗口数据缺失或异常时隔离并计数，不得静默补值；
- ST 暂不排除，显式报告限制。

### 4.2 因果时钟

所有区间采用左闭右开 `[start,end)`：

| 用途 | 冻结区间 |
|---|---|
| 尾窗特征 | D `[14:20:00,14:50:00)`，30 个已完成分钟 |
| 决策 | D `14:50:00` |
| 延迟缓冲 | D `[14:50:00,14:51:00)`，不用作特征或成交 |
| 买入 | D `[14:51:00,14:56:00)` VWAP |
| 潜力诊断 | D+1 `[09:30:00,10:00:00)` MFE/MAE |
| 卖出 | D+1 `[10:00:00,10:05:00)` VWAP |

硬断言：修改 D `14:50:00` 以后任意 bar 或 D+1 任意 bar，不得改变 D 的资格、六个特征、分数和排名。D+1 数据只允许进入标签、成交与诊断。

### 4.3 成交、成本和不可成交

- `VWAP = sum(amount) / sum(volume)`，只对 volume>0 的合法分钟计算；
- 买入窗口零容量：该仓位为现金，不允许用事后第 11 名替换；
- 卖出窗口零容量或无法卖出：延迟至第一个可执行窗口，继续占用资金并单独报告；
- V1 无盘口队列，VWAP 只是小单基准，不能声称真实成交证明；
- 同时报告 gross、15 bps、30 bps；策略主验收固定使用 round-trip `30 bps`；
- 500 万容量诊断固定为 Top 10、50 万/股，报告买卖窗口参与率；`0.5%` 仅为保守 deployability 诊断线，不参与选股或 `signal_edge_verdict`。

### 4.4 公司行为

- P&L 使用未复权成交价；
- Terra 必须先用日 K 文档/字段证明公司行为或复权因子变化的可识别性；
- 能可靠识别时，隔离跨越除权、分红、送转、配股的 D-3～D+1 样本；
- 无法证明时写 `corporate_action_filter=unproven_not_applied`，正式经济结论最高为 `provisional_signal_edge`，不得猜测字段或用当前值回填历史。

## 5. 六个预注册候选特征

所有相对市场值均减去同日合格股票横截面中位数。零分母必须返回显式缺失原因并隔离，不得无穷大或填 0。

1. `tail_return_rel`：D `[14:20,14:50)` 首价至末价收益减市场中位数。
2. `tail_end_location`：`(末价-low_30)/(high_30-low_30)`。
3. `tail_amount_accel`：`amount[14:35,14:50) / amount[14:20,14:35)`。
4. `pre_tail_return_rel`：D 开盘至 14:20 收益减市场中位数。
5. `activity_ratio`：D `[09:30,14:50)` 累计成交额 / D-1～D-3 同口径成交额中位数。
6. `recent_3date_return_rel`：D-3 收盘至 D-1 收盘收益减市场中位数。

三日振幅、路径效率、最大回撤、MFE、MAE 只做诊断，不得进入 V1 评分。

## 6. 冻结发现与评分程序

- 2023～2024 是唯一开发区；不得读取 2025/2026 结果来选择特征或方向。
- 每个特征按交易日做横截面 percentile rank，并单独输出十分位表与每日 rank IC。
- 方向先由 2023 的 Top-vs-Bottom decile spread 决定；只有 2024 同方向时才合格。
- 三个预注册组：
  - `tail_price`：特征 1、2，最多选 1 个；
  - `tail_amount`：特征 3，合格才选；
  - `background`：特征 4、5、6，最多选 1 个。
- 同组多候选时，用 `min(abs(spread_2023), abs(spread_2024))` 最大者；平手按本文编号靠前者。不得改变规则挑结果。
- 最终 1～3 个特征，按已冻结方向做等权 rank 平均；每日 Top 10。
- 若没有特征合格，或冻结组合在 2023 与 2024 的 30 bps 后组合收益不能同时为正，停止为 `no_stable_development_signal`，不得进入 2025。

对照组必须共享相同资格和成交边界：合格池等权、固定种子 `20260824` 的随机 Top 10、只按 `tail_return_rel` 的 Top 10。

## 7. 时间隔离与 Go/No-Go

### 2025 validation

- 规则冻结后只运行一次；
- 30 bps 后组合收益 >0；
- 相对合格池等权和随机 Top 10 的超额点估计均 >0；
- 任一失败：`validation_no_go`，停止，不得调参后重跑 2025。

### 2026-01-01～2026-06-30 final

- 只有 2025 通过且 Sol 接受冻结链后才可运行一次；
- 30 bps 后 2025、2026 各自净收益均 >0；
- 2025+2026 按交易日/月度块 bootstrap 的日组合超额，相对两个基准的 95% CI 下沿均 >0，且 2026 点估计 >0；
- 去掉表现最佳的一个 OOS 月份后，累计净收益仍 >0；
- 不可成交按现金/延迟退出重算后，`signal_edge_verdict` 仍通过；
- 任一 2026 结果导致改参数，当前 2026 立即失去 final 资格。

通过仅为 `signal_edge_go_research`；500 万容量另报 `capacity_pass|capacity_constrained|capacity_unproven`。实盘、纸面跟踪和 L2 属后续新卡。

## 8. 长批治理

任何预计超过 30 分钟的扫描视为 HEAVY-BATCH：

- 冻结 `spec_hash`、代码 commit、输入 manifest（路径、大小、mtime）和 `run_id`；
- 单 writer、按交易日 checkpoint、hash 不一致禁止 resume；
- 独立进程运行，模型零轮询；状态只写紧凑 `progress.json`；
- 终态原子写 `completion.json = SUCCEEDED|FAILED|CANCELLED`；
- 输出确定性 JSON/CSV.gz 和 SHA-256 manifest；时间戳、PID、路径不得进入经济结果哈希；
- 失败保留完成的只读 checkpoint 和异常，禁止盲目重跑；
- 任务自有进程、临时缓存和测试产物在交付前按项目清理合同处理。

## 9. 串行任务卡

### TNM-1：解析、因果守卫与真实小样本预检（Terra）

目标：先证明研究工具读对源、时间不泄漏、异常会 fail-closed；不跑全量发现。

写入白名单：

- `mining/tail_next_morning.py`（新增）；
- `tests/test_tail_next_morning.py`（新增）；
- 本文仅允许追加 `TNM-1 执行结果`；
- ignored `output/tail-next-morning-v1/preflight/`。

必须实现：

- 同时读取日 ZIP、日目录、带/不带 market 前缀文件；E: 全程只读；
- 240 行/session 映射、六列数值校验、非正 OHLC、重复/缺行、零容量窗口隔离；
- 六特征、D-1 5 亿门槛、VWAP/标签、ST/公司行为/容量状态字段；
- 纯函数或最小数据结构确保 feature path 与 outcome path 分离；
- CLI：

```powershell
python -m mining.tail_next_morning preflight `
  --minute-root "E:\分钟数据" `
  --daily-root "E:\日K线全部至202606" `
  --output-dir "output\tail-next-morning-v1\preflight"
```

真实预检只限固定跨格式样本，不得扩展全市场长跑：2023 ZIP、2024 日目录、2026 ZIP；代表代码优先 `000001,300750,600000`，缺失要显式报告而非替换样本。

测试至少覆盖：

1. `[14:20,14:50)` 恰为 30 bar，买卖窗口恰为 5 bar；
2. 修改 14:50 后数据不改变任何 feature/eligibility/rank；
3. 修改 D+1 数据只改变 outcome，不改变 D 特征；
4. D-1 amount 等于 5 亿不通过，严格大于才通过；
5. amount/volume VWAP、零 volume、全零窗口、异常价格；
6. ZIP/目录/文件名前缀漂移；
7. 日 K 与分钟成交额代表样本交叉验证；
8. ST 不可用和公司行为未证明时的降级标签；
9. 500 万/Top10 容量诊断不影响排名。

验收：

```powershell
C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_tail_next_morning
C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m py_compile mining\tail_next_morning.py tests\test_tail_next_morning.py
git diff --check
```

必须一个白名单 commit。完成标签只能为 `tnm1_preflight_verified`、`tnm1_partial` 或 `tnm1_blocked`。完成后停止，等待 Sol 审核。

### TNM-1R：独立代码与预检审核（Sol）

只读审查 TNM-1 commit 和预检产物；独立重跑定向测试、检查因果变形测试、源只读、异常语义、白名单和进程清理。不得修复。输出 `approve|changes_required|blocked_evidence` 及 P0/P1/P2 findings。只有 `approve` 才能进入 TNM-2。

### TNM-2：2023～2024 发现批与规则冻结（Terra）

在 TNM-1R approve 后另行派发。允许补齐 detached runner/checkpoint，但不得访问或汇总 2025/2026 outcome。运行完成后按第 6 节生成确定性 `frozen_rule.json`、特征十分位/IC、Top10 组合、三基准、成本/异常/容量诊断和 hash manifest；追加本文 `TNM-2 执行结果`，一个 commit 后停止。

### TNM-2R：冻结规则独立审核（Sol）

只读复算固定样本与聚合，核验 2025/2026 未被用于选择、规则唯一且可重放、结果没有多重候选偷换。只有 `approve` 才能进入 TNM-3。

### TNM-3：2025 单次验证（Terra）

只加载已审核的 frozen rule；一次运行，禁止修改代码/参数后重跑。按第 7 节给 `validation_go|validation_no_go|validation_blocked`。Sol 独立审核后，只有 go 才能进入 TNM-4。

### TNM-4：2026 单次 final（Terra）

只加载同一 frozen rule；一次运行 2026-01-01～2026-06-30，产生最终两层 verdict、bootstrap、去最佳月、不可成交重算、容量诊断和完整哈希。Sol 做最终独立审核；规划会话只在审计通过后对用户报告结论。

## 10. 审核与升级规则

- Terra 的成功声明不是验收；Sol 必须从代码、测试和紧凑证据独立复算。
- Sol 不得改源码、测试、规则或执行产物；发现问题返回最小复现与修复边界。
- 规划会话决定是向 Terra 派发独立修复卡、停止，还是进入下一阶段。
- Terra 与 Sol 出现会改变业务定义的实质分歧时暂停。规划会话可再次检查 Claude/Opus 可用入口；若仍不可用，必须明确 `opus_unavailable`，不得冒充 Opus 结论。
- Lessons 决策：当前 `skip`。只有出现可跨项目复用、且有真实失败证据的新规律时，另向用户申请更新记忆。

## 11. 执行结果

### TNM-1 执行结果

状态：`tnm1_preflight_verified`

- 执行者：Terra，会话 `01a03137-df48-7281-b7d7-b48f7e41f209`；未进入 TNM-2。
- 定向验证通过：`C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_tail_next_morning`（7 tests）和 `-m py_compile mining\tail_next_morning.py tests\test_tail_next_morning.py`。
- 真实只读预检通过：固定 `2023-01-06` ZIP、`2024-01-05` 日目录、`2026-01-08` ZIP；每组只读 D-3～D+1 的 `000001,300750,600000`，未扫描 839 日，未读取任何冻结期结果、L2 或 provider。
- 预检产物（ignored）：`output/tail-next-morning-v1/preflight/preflight-attempt-03.json`，SHA-256 `a4e76387e843f7f9d9b24f0a7d9063824e251f048def2d4597368e687cc2c792`；三组共 15 个日容器均为预期格式，9 个代码样本中 7 个特征就绪，2 个 `600000` 因 D-1 amount 未严格大于 5 亿隔离；日 K/分钟成交额比范围 `0.999934110`～`1.000012341`。
- 降级边界已写入产物：`st_filter_applied=false`、`st_status=unavailable_not_filtered`、`corporate_action_filter=unproven_not_applied`、`capacity_verdict_5m=capacity_unproven_no_frozen_rank`；容量未影响排名，且尚无冻结 Top 10，因此不形成容量结论。
- 已知限制：日 K 仅用于上市历史与成交额交叉核对，未用于 D 14:50 决策；公司行为没有可证明的字段合同，故未猜测或过滤；VWAP 仍只是小单基准。此前一次因同日 ZIP/目录并存触发的解析 partial 产物已保留在同目录，修正为 ZIP 确定性优先后才得到本结果。

#### TNM-1F：因果用途隔离修复（Terra）

- 修复背景：Sol 的 TNM-1R 为 `tnm1r_changes_required`（P1）。首个分歧是 loader 在用途切分前整日验证 OHLC/有限数值，使 D 14:50 后或 D+1 无关区的坏值删除 D 信号。
- 最小修复：保留全局 240 行、六列、时间映射和 raw value coercion 的结构检查；有限数值、正 OHLC、amount/volume 非负由各自消费窗口或完整 amount crosscheck 校验。D 信号只严格校验 `[09:30,14:50)` 与完整 D-3～D-1；买入只校验 `[14:51,14:56)`；D+1 标签/卖出只校验 `[09:30,10:05)`。D+1 文件缺失或 239 行未知缺口保留 D feature，并把 outcome 标记为 unavailable。
- 定向验证通过：同一真实 ZIP loader + `_preflight_sample` 控制流覆盖 D `14:50` 字符串、买入窗 `NaN`、D `14:56` 后 `Inf`、D+1 结果窗字符串、D+1 `10:05` 后 `NaN`、缺失 D+1 文件、239 行 D+1 与 D `<14:50` 非法。前五类未来变形保持 D feature/eligibility/rank；相关 outcome 正确降级；D `<14:50` 仍 fail-closed。`C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_tail_next_morning`（9 tests）和 `-m py_compile mining\tail_next_morning.py tests\test_tail_next_morning.py` 均通过。
- 真实预检复核通过：`output/tail-next-morning-v1/preflight/preflight-attempt-05.json`，SHA-256 `a4e76387e843f7f9d9b24f0a7d9063824e251f048def2d4597368e687cc2c792`，与 attempt-03 字节相同；仍为 `tnm1_preflight_verified`、7 ready、2 个 D-1 门槛隔离。
- 审核锁：本修复不是审核批准；TNM-1R 仍为 `changes_required`，TNM-2 继续锁定，必须等待 Sol 对本修复提交只读复审。

#### TNM-1F2：全日 amount 交叉核对诊断修复（Terra）

- 修复背景：Sol R2 发现 P2：D `14:56` 后 `amount=NaN` 时，因 `pandas.sum()` 默认跳过 NaN，daily/minute crosscheck 错误报告 `ready`，但该值只属于完整成交额诊断，不能重新成为信号或 outcome 的全日门。
- 最小修复：full-day amount crosscheck 在求和前单独要求完整 `amount` 列有限且非负；minute `NaN`、`Inf`、coercion 后 NaN 或负值返回 `status=invalid` 与明确 reason，日 K amount 缺失保留 `status=unavailable`。invalid 仅写入 preflight `sample_errors`，不改变 D feature/eligibility/rank 或 outcome，也未重新引入全日 OHLC 验证。
- 定向验证通过：同一 loader + `_preflight_sample` 控制流覆盖 D `14:56` 后 amount `NaN`/`Inf`；每例保持 feature/rank/outcome 与基线一致，crosscheck 为 `invalid:minute_amount_non_finite` 且含 `daily_minute_amount_invalid` sample error。有效数据仍为 `ready`，日 K amount 缺失为 `unavailable`。`C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_tail_next_morning`（10 tests）和 `-m py_compile mining\tail_next_morning.py tests\test_tail_next_morning.py` 均通过。
- 真实预检复核通过：`output/tail-next-morning-v1/preflight/preflight-attempt-06.json`，SHA-256 `a4e76387e843f7f9d9b24f0a7d9063824e251f048def2d4597368e687cc2c792`，与 attempt-03 字节相同；仍为 `tnm1_preflight_verified`、7 ready、2 个 D-1 门槛隔离。
- 审核锁：TNM-1R 尚未批准，TNM-2 继续锁定，等待 Sol R3 对本修复提交只读复审。

### TNM-2 执行结果

状态：`locked_until_tnm1r_approve`

### TNM-3 执行结果

状态：`locked_until_tnm2r_approve`

### TNM-4 执行结果

状态：`locked_until_tnm3r_validation_go`
