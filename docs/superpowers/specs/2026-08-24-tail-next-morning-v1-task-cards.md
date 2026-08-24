# Tail Next-Morning V1 离线回测任务卡

- 冻结日期：2026-08-24
- 规划与协调会话：`01a02efc-e34c-7ca0-a727-9df00526e7bd`
- 唯一执行会话（Terra）：`01a03137-df48-7281-b7d7-b48f7e41f209`
- 唯一独立审核会话（Sol）：`01a03141-39e9-7dd1-a359-037346adf01b`
- 仓库：`D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard`
- 冻结起点：`codex/screening-v26@8c2b436`
- 当前阶段：`TNM-2AF3 ready_after_tnm2ar2_changes_required`

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
- 至少 20 个历史交易日；日 K 可用时只读 `date` 列，对去重后的有效 `<D` 日期做精确阈值证明：发现第 20 个即可短路并记录 `listing_history_sessions_capped=20`、`listing_history_count_status=at_least_threshold`；读到文件末尾仍不足 20 时记录精确数量与 `exact_below_threshold`。日 K 缺失或不可读时按分钟源实际可见 session 用同一封顶口径降级并标记 `listing_age_source`。不得用 60 日历日、首日跨度或其他启发式替代；
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
| 潜力诊断 | D+1 `[09:30:00,10:00:00)` MFE/MAE；质量状态与卖出窗独立 |
| 卖出 | D+1 `[10:00:00,10:05:00)` VWAP |

硬断言：修改 D `14:50:00` 以后任意 bar 或 D+1 任意 bar，不得改变 D 的资格、六个特征、分数和排名。D+1 数据只允许进入标签、成交与诊断。

### 4.3 成交、成本和不可成交

- `VWAP = sum(amount) / sum(volume)`，只对 volume>0 的合法分钟计算；
- 买入窗口零容量：该仓位为现金，不允许用事后第 11 名替换；
- 卖出窗口零容量或数据不可用：按下述冻结网格延迟至第一个可执行窗口，继续占用资金并单独报告；不得用收盘价、下一根价格或事后最优价格强制平仓；
- V1 无盘口队列，VWAP 只是小单基准，不能声称真实成交证明；
- 同时报告 gross、15 bps、30 bps；策略主验收固定使用 round-trip `30 bps`；
- 500 万容量诊断固定为 Top 10、50 万/股，报告买卖窗口参与率；`0.5%` 仅为保守 deployability 诊断线，不参与选股或 `signal_edge_verdict`。

延迟卖出只使用固定 5 分钟连续竞价网格。先扫描 D+1 `[10:05,10:10)` 至 `[11:25,11:30)`，再扫描 `[13:00,13:05)` 至 `[14:55,15:00)`；仍不可执行则从下一交易日 `[09:30,09:35)` 起按相同网格继续。一个窗口只有在所需 bar 完整、OHLC/amount/volume 合法且 `sum(volume)>0`、`sum(amount)>0` 时才可作为 VWAP 退出。成功延迟退出必须保留 `exit_kind=delayed`、实际交易日/窗口和延迟网格数，经济汇总从该身份计数，不得转成无法区分的普通 `ready`。不得跨越当前研究阶段边界读取冻结期：到当年/阶段末仍无可执行退出时标记 `unresolved_exit_at_phase_end`，该阶段经济结论为 `blocked_unresolved_exit`，不得伪造价格或删除样本。

主信号研究采用事件组合，容量采用独立资金账本：

- 每个 D 日建立 10 个名义槽位，分母恒为 10；少于 10 个候选的空槽、买入失败或零容量槽记现金收益 0，不替补、不把权重摊给成交槽位；
- 已买入且延迟卖出的最终收益仍归属原 D 事件日；主研究不以此冒充“每日同一笔 500 万可连续周转”，只回答信号边际；
- 500 万容量账本固定 10 个 50 万资金袖套。跨日未退出的袖套保持占用；下一 D 日只用空闲袖套依次买当日最高排名，不借款、不加杠杆、不用第 11 名替换失败买入；
- gross 为最终卖出 VWAP / 买入 VWAP - 1；15/30 bps 是每个已完成 round-trip 的总成本，直接从 gross 扣除。现金槽不收成本；主验收使用 30 bps；
- 正收益判断使用等权交易日的 `mean_daily_net30`；同时输出复合曲线和最大回撤作诊断。延迟收益按原事件日归属，复合曲线不得作为资金可周转证明。

### 4.4 公司行为

- P&L 使用未复权成交价；
- Terra 必须先用日 K 文档/字段证明公司行为或复权因子变化的可识别性；
- 能可靠识别时，隔离跨越除权、分红、送转、配股的 D-3～D+1 样本；
- 无法证明时写 `corporate_action_filter=unproven_not_applied`，正式经济结论最高为 `provisional_signal_edge`，不得猜测字段或用当前值回填历史。

## 5. 六个预注册候选特征

所有相对市场值均减去同日信号合格股票横截面中位数。合格横截面只由 D `14:50` 已知的股票范围、历史、D-1 流动性和信号窗口质量决定；买入、D+1 outcome 或未来可成交性不得改变中位数、percentile rank 或排名。零分母必须返回显式缺失原因并隔离，不得无穷大或填 0。

1. `tail_return_rel`：D `[14:20,14:50)` 首价至末价收益减市场中位数。
2. `tail_end_location`：`(末价-low_30)/(high_30-low_30)`。
3. `tail_amount_accel`：`amount[14:35,14:50) / amount[14:20,14:35)`。
4. `pre_tail_return_rel`：D 开盘至 14:20 收益减市场中位数。
5. `activity_ratio`：D `[09:30,14:50)` 累计成交额 / D-1～D-3 同口径成交额中位数。
6. `recent_3date_return_rel`：D-3 收盘至 D-1 收盘收益减市场中位数。

三日振幅、路径效率、最大回撤、MFE、MAE 只做诊断，不得进入 V1 评分。

## 6. 冻结发现与评分程序

- 2023～2024 是唯一开发区；不得读取 2025/2026 结果来选择特征或方向。
- 每个特征按交易日做横截面 percentile rank，固定为 `rank(method="average", pct=True)`；分桶必须与冻结尾部分界一致：第 1 桶为 `(0,0.10]`，第 10 桶为 `[0.90,1.00]`，第 2～8 桶为 `((k-1)/10,k/10]`，第 9 桶为 `(0.80,0.90)`。因此 `rank=0.10` 属第 1 桶，`rank=0.90` 和 `1.00` 属第 10 桶。Bottom decile 为 `rank<=0.10`，Top decile 为 `rank>=0.90`，并列不任意拆分。每桶必须输出 `member_count/resolved_count/unresolved_count`；未决成员保留在实际样本数中，且该桶经济均值为 `null + blocked_unresolved`，不得从分母删除。最终 Top 10 分数并列按 `sec_code` 升序。单独输出十分位表与每日 Spearman rank IC；IC 只做诊断，不参与选择。
- 单日特征 spread 固定为该日 Top decile 全部成员与 Bottom decile 全部成员的 `net30` 算术平均之差；买入失败成员按现金 0 保留，已买入但阶段末未退出则阻断而非删除。年度 spread 是各合格交易日 spread 的等权算术平均。方向只由 2023 spread 的符号决定；2024 同方向才合格，spread 为 0 或任一年无可用日则不合格。
- 三个预注册组：
  - `tail_price`：特征 1、2，最多选 1 个；
  - `tail_amount`：特征 3，合格才选；
  - `background`：特征 4、5、6，最多选 1 个。
- 同组多候选时，只在通过 2024 同向否决的候选中按 `abs(spread_2023)` 最大者选择；2024 的幅度不得参与排序。平手按本文编号靠前者。不得改变规则挑结果。
- 最终 1～3 个特征，按已冻结方向做等权 rank 平均；每日 Top 10。
- 不增加 spread、p-value、胜率或 IC 的事后门槛。若没有特征合格，或冻结组合在 2023 与 2024 的 `mean_daily_net30` 不能同时为正，停止为 `no_stable_development_signal`，不得进入 2025。

对照组必须共享相同资格和成交边界：合格池等权；固定种子 `20260824` 的随机 Top 10 固定为按 `SHA256("20260824|trade_date|sec_code")` 升序取前 10，不调用版本相关 PRNG；`tail_return_rel` 基准固定按原值从高到低取 Top 10。两种 Top 10 基准都使用 10 槽恒定分母、相同不可成交规则和代码升序最终 tie-break。

## 7. 时间隔离与 Go/No-Go

### 2025 validation

- 规则冻结后只运行一次；
- `mean_daily_net30 > 0`；
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

TNM-2 是长批，必须拆成代码冻结与数据执行两张串行卡。先提交并审核 runner commit，批次只能从该 clean commit 启动；不得从未提交工作树运行后再补写“代码已冻结”。分钟输入 manifest 只允许选择 2023～2024 容器，并确定性优先同日 ZIP；日 K 只允许读取 `date` 列以精确证明 D 前历史 session 是否达到 20，达到即短路，禁止读取 2025/2026 价格、收益、成交额或结果进入特征选择。每日分钟文件集合本身定义当日 point-in-time universe；不得与当前日 K 文件清单取交集，以免制造幸存者偏差。日 K 缺失时按分钟可见历史降级，不删除历史股票。

输入身份必须覆盖所有真实消费者：

- ZIP 容器记录路径、大小、mtime、成员数及由 ZIP central directory 的成员相对路径/未压缩大小/CRC 形成的确定性 digest；
- 目录容器记录路径，并以全部候选 CSV 的相对路径、大小、mtime 形成确定性 tree digest、成员数和总字节数；不得只记录目录本身 mtime；
- 日 K 根记录全部候选 `.xlsx` 的相对路径、大小、mtime 形成的 tree digest、成员数和总字节数；实际读取文件必须属于该冻结集合；
- 本阶段不强制额外读取全部文件内容做 SHA-256。上述 metadata/CRC 身份满足当前本地只读回测合同；任何 fingerprint 变化都拒绝 resume。

### 8.1 Opus 方法论讨论后的裁决

2026-08-24 通过 Claude Code/Opus 做了一次无写入方法论讨论。采纳两项能减少自由度的建议：Top 10 恒定 10 槽分母；组内强弱只用 2023 排序、2024 只作同向否决。不采纳以下会制造新假设的建议：

- 不用 D+1 收盘价强制平仓；零成交时该价格不可执行，且违反“继续占用资金”。采用上文跨日固定 5 分钟网格，阶段末仍未退出则阻断；
- 不新增 60 bps spread 门槛；开发年同向、组合两年净正和后续单次 OOS 已足够，额外阈值会形成新调参旋钮；
- 不用 `abs(return)>11%` 猜测公司行为；主板、创业板/科创板及 ST 涨跌幅不同，该启发式会混淆真实行情。继续显式 `corporate_action_filter=unproven_not_applied`，并把相关经济结论降级为 provisional。

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

### TNM-2A：开发 runner、长批合同与全市场小 canary（Terra）

在 TNM-1R approve 后派发。白名单仍仅为 `mining/tail_next_morning.py`、`tests/test_tail_next_morning.py`、本文 TNM-2A 结果，以及 ignored `output/tail-next-morning-v1/dev-preflight/`。不得运行完整 2023～2024 发现批。

必须实现：

- 每个日容器只打开一次，按股票抽取满足六特征、买卖、延迟退出、诊断和容量所需的最小充分统计；不得为每只股票重复打开 ZIP；
- 逐日 checkpoint、spec/code/input-manifest hash、hash-bound resume、单 writer、`progress.json`、原子 `completion.json`、确定性 JSON/CSV.gz 与 SHA-256 manifest；
- 固定 `dev-preflight` canary 只跑 2023 年最早可形成 D-3～D+1 的 10 个目标日，但覆盖当日全 A 股文件；输出只验证全市场流式读取、日间连接、恒定 10 槽、三基准、延迟退出状态和 resume，不允许冻结规则；
- `dev-run` 命令内部硬限制 target D、D+1 和延迟退出读取不越过各自 2023/2024 年界；任何 2025/2026 分钟 outcome 访问必须抛错；D+1 诊断窗异常只降级 MFE/MAE，不得推迟独立有效的卖出窗；
- 代表股票/日期的充分统计必须与 TNM-1 原始 240 bar 纯函数逐字段相等；测试覆盖 ZIP/目录、同日 ZIP 优先、全市场 universe 不依赖当前日 K 文件存在、未来数据不改变横截面、失败 checkpoint 可恢复、hash 不一致拒绝 resume；
- resume checkpoint 必须保存继续运行所需的 source frontier、滚动历史、可见 session 计数、pending exits 和已完成 target；恢复最多重读发生中断的一个日容器，不得从头重读全部已完成容器。lock 写 JSON owner（PID、host、run_id、run_hash、启动时间仅作运行证据）；默认拒绝现存 lock，只有显式恢复参数在同机验证 PID 不存活且 run hash 一致时才可保留旧 lock 证据并接管。`KeyboardInterrupt`/正常取消必须原子写 `CANCELLED`；已完成快路径必须先复核 artifact manifest；
- 完整开发产物至少包含每日十分位统计、每日 Spearman rank IC、每日策略与三基准、2023/2024 `mean_daily_net30`、复合曲线/最大回撤、异常/买入失败/延迟退出/未决退出汇总和容量账本。只有选中特征存在、无阻断未决退出且 2023/2024 策略 `mean_daily_net30` 均 `>0` 时才可写 `frozen_rule.json`；否则写 `no_stable_development_signal` 且不得生成可被 TNM-3 使用的规则；
- `frozen_rule.json` 必须绑定 selected feature/direction、完整窗口/资格/成本/槽位/tie-break/基准/延迟退出口径，以及 spec、approved code commit、runner source、input manifest、run 和经济 artifact hashes，足以让 TNM-3 唯一重放；
- 完整开发运行命令冻结为：

```powershell
C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m mining.tail_next_morning dev-run `
  --minute-root "E:\分钟数据" `
  --daily-root "E:\日K线全部至202606" `
  --output-dir "output\tail-next-morning-v1\development" `
  --approved-commit "<TNM-2AR_APPROVED_COMMIT>"
```

`dev-run` 必须要求 `--approved-commit`，验证当前 HEAD 与批准 commit 相同，且任务卡、runner、测试三个 task-owned 路径相对 HEAD 无未提交变更；用户原有其他 dirty 文件不阻断也不得被读取为输入。`dev-preflight` 不要求批准 commit。

TNM-2A 先运行定向测试、`py_compile`、`git diff --check` 和固定 `dev-preflight`，创建一个 runner 本地 commit 后停止，不得启动完整批次。

### TNM-2AR：runner 与 canary 独立审核（Sol）

只读复算充分统计、时间/年份 guard、源只读、恒定分母、选择算法唯一性、checkpoint/resume/hash、白名单和 canary。只有 `approve` 才能启动 TNM-2B。

### TNM-2B：2023～2024 detached 发现批与规则冻结（Terra）

只能从 TNM-2AR 批准的 clean runner commit 启动。启动前写完整 manifest 和 `run_id`；以隐藏独立进程运行，Terra 记录 PID/命令/路径后停止模型轮询。批次不得访问或汇总 2025/2026 outcome。成功后 Terra 只验收 `completion.json` 与紧凑证据，生成确定性 `frozen_rule.json`、特征十分位/IC、Top10 组合、三基准、成本/异常/延迟退出/容量诊断和 hash manifest；追加本文 `TNM-2B 执行结果`，创建结果本地 commit 后停止。若开发门槛失败，按 `no_stable_development_signal` 正常停止，不得进入 2025。

### TNM-2R：冻结规则与开发证据独立审核（Sol）

只读复算固定样本与年度聚合，核验 2025/2026 未被用于选择、2023 负责方向与组内排序、2024 只作同向否决、规则唯一且可重放、恒定槽位与不可成交没有被删除、结果没有多重候选偷换。只有 `approve` 且开发门槛通过才可进入 TNM-3。

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

#### TNM-1R3：最终独立批准（Sol）

- 最终 verdict：`tnm1r_approve`；审核对象 `8c580b424bc6ee9ff5646249e4bf6bb47b603be0`，会话 `01a03141-39e9-7dd1-a359-037346adf01b`。
- R1 的 P1 因果泄漏和 R2 的 P2 amount 诊断误报均关闭；Sol 独立完成 960 个全位置 amount 异常注入、24 个真实 ZIP/preflight 代表注入及 P1 快速回归，未发现新 P0/P1/P2。
- 10/10 定向测试、`py_compile`、`git diff --check` 和 attempt-06 manifest/SHA 独立通过；原有 10 个 dirty 文件未进入提交或暂存，无遗留进程。
- TNM-1 审核锁解除；只解锁本文重新冻结后的 TNM-2A，不授权完整开发长批、2025 或 2026。

### TNM-2A 执行结果

状态：`ready_for_terra_implementation`

- 执行结论：`tnm2a_runner_verified`。只实现 runner、重启/哈希治理与固定全市场 canary；未执行 `dev-run` 或完整 2023～2024 发现批，未读取 2025/2026 分钟 outcome、未联网/provider/L2/ML/生产 DB/scanner。
- runner：日 ZIP/目录各容器单次枚举/打开，分钟文件集合定义当日 universe；240-bar 原始行立即压缩为信号、买入、D+1、延迟退出所需统计。D `14:50` 后和 D+1 只进入 outcome；六特征、横截面中位数、percentile rank、最终排名保持 TNM-1 因果隔离。实现 D-1 严格 >5 亿、2023 定方向/组内排序、2024 仅同向否决、平均 percentile/decile、恒定 10 槽、三基准、gross/15/30 bps、跨日 5 分钟退出、年末 unresolved 阻断、独立 10×50 万容量袖套，以及 ST/公司行为/queue 降级。
- 治理修复：`spec_hash` 仅为本文开头至 `## 11. 执行结果` 标记前的冻结定义字节 SHA-256；执行证据不参与哈希，防止 `run_id` 自引用。单元回归已证明追加第 11 节内容不改变 hash、修改冻结第 4～10 节会改变 hash；run manifest 同时记录 `code_commit`、`runner_source_hash`、输入 `path,size,mtime_ns`、单 writer/checkpoint/resume hash。
- 定向验收：`C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_tail_next_morning`（16 tests）、`-m py_compile mining\tail_next_morning.py tests\test_tail_next_morning.py` 与 `git diff --check` 均通过。回归包含 2025 guard、单容器单开、ZIP/目录/ZIP 优先、无日 K 股票仍在 minute universe、10 槽与买入失败现金 0、SHA 随机基准、2023/2024 选择隔离、跨日/跨年延迟退出、unresolved、checkpoint hash 冲突、canary 禁写 frozen_rule 与充分统计逐字段对照 TNM-1 240-bar 纯函数。
- 最终真实只读 canary（ignored）：`output/tail-next-morning-v1/dev-preflight/dev-preflight-4cc41a037e6aa449`；`run_hash=4cc41a037e6aa449b1b0a83d94a33653d11c43536f46a6278e905881b0658fa5`，`spec_hash=2286778eebaf43e7b693355536c5a1b91b6ba0a497d0b2c575f960082a6a678c`，`runner_source_hash=5a8f1fb3a0f3d63d097a9cea22e1d0dd719321f9ddc1bb9d55eb57563907d81d`。195 秒完成 20230106、09～13、16～19 的 10 target、14 个 ZIP 输入容器、10 checkpoint；每日 minute universe 4,766～4,770，ready 245～379，`selection_status=canary_probe_not_frozen`，`frozen_rule` 未写，容量为独立诊断 `capacity_constrained`。
- 终态与哈希：`completion.json=SUCCEEDED`；`development_results.json` SHA-256 `fa67c3b535ed32f159aee81dcc158f3e4a69900b76afd5c620813f5276b9004c`，`daily_results.csv.gz` SHA-256 `22ce8541a5b0c13a93a9e0bdb212bba0340161dadb6b0394d4f46e10d2383740`。第 11 节证据追加后须复算并保持上述 `spec_hash/run_id` 不变。
- 先前 ignored attempts 保留为 superseded evidence：`dev-preflight-b25f014afa3d8ad8`、`dev-preflight-d9bf2f7fba637e98` 为治理字段补全前的成功尝试；`dev-preflight-cef0d603978ed695` 在发现执行结果自引用缺陷后由任务自有 PID 安全停止，`completion.json=CANCELLED: superseded_spec_hash_execution_results_boundary`。未删除任何 attempt。
- 审核锁：只允许 Sol TNM-2AR 对本 runner/canary 作只读审核；TNM-2B、2025/2026、规则冻结、任何 provider/DB/交易操作继续锁定。

#### TNM-2AR：首次独立审核（Sol）

- verdict：`tnm2ar_changes_required`；canary 输入范围、哈希、恒定 10 槽、因果时间窗、充分统计、成本与基准实现可信，未发现 P0；TNM-2B 继续锁定。
- P1-1：D+1 `[09:30,10:00)` MFE/MAE 诊断与 `[10:00,10:05)` 卖出共用质量门，诊断坏 bar 会错误触发延迟卖出；必须拆成独立状态。
- P1-2：上市满 20 日被 60 日历日启发式替代；必须只读日 K `date` 列精确计数，缺失时按分钟实际可见 session 降级。
- P1-3：完整开发运行没有强制 2023、2024 年策略 `mean_daily_net30` 均为正；负收益仍可能生成规则。
- P1-4：经济产物及 `frozen_rule.json` 不足以独立复算，缺少 IC、全十分位、复合曲线/回撤、异常与延迟汇总及完整合同/哈希绑定。
- P1-5：输入 manifest 未覆盖真实读取的日 K 文件，目录容器只记录目录元数据，成员变化可能不改变输入身份。
- P1-6：resume 会重读已完成容器，lock 无 owner 身份，普通异常/中断/已完成快路径的终态与 artifact 复核不完备。
- 修复边界已回写第 4、8、9 节；只解锁 Terra 的 TNM-2AF 定点修复与新 canary，不授权完整开发批、2025 或 2026。

### TNM-2AF 执行结果

状态：`tnm2af_partial`

- 已完成定点 runner 修复并通过定向验收：D+1 `[09:30,10:00)` 诊断与 `[10:00,10:05)` 卖出质量状态分离；日 K 只读取 `date` 列并以去重有效日期计算 `<D` session；输入身份加入 ZIP central-directory、目录 CSV tree 和全日 K XLSX tree；checkpoint 保存 source frontier、滚动历史、visible session、pending exit 与完成 target；lock 为带 PID/host/run hash 的 JSON，显式同机死 PID 恢复保留证据；`CANCELLED`/`FAILED`/完成 artifact 复核分开；完整开发冻结增加双年度正 `mean_daily_net30`、无 unresolved exit 和完整合同/产物哈希门槛。`dev-run` 现要求 `--approved-commit`，本轮未调用。
- 定向验收已通过：`C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_tail_next_morning`（21 tests）、`-m py_compile mining\tail_next_morning.py tests\test_tail_next_morning.py`、`git diff --check`。新增回归包含 D+1 诊断/卖出隔离、30 个真实 session 跨 50 日与 10 个 session 跨 60+ 日、ZIP/目录/日 K 输入身份、死 PID lock 接管、取消后从 source frontier 增量恢复、双年度冻结门槛和 frozen contract 绑定。
- 新真实只读 canary attempt 保留为取消证据：`output/tail-next-morning-v1/dev-preflight/dev-preflight-31f2a75b8db2112d`，`run_hash=31f2a75b8db2112da34432fba2b926bea14c42975ebda61e03084e693844127f`，`spec_hash=495a00ccdec0693aea761a4cfdd642630d130fe94283910a96188e3bcd820f0f`，`input_manifest_hash=6e024046e05f836642b5350fd943068630ccfc86a807944458399014e9763acd`。manifest 已冻结 14 个 2023 ZIP 容器和 5,532 个日 K XLSX（tree digest `4ee865a94382255d225bc692bf75843208333b1cbb77a35a34790d5c828e890a`）；首 target `20230106` 的 minute universe 为 4,769。
- 性能停止：现场在约 3 分钟仅完成日 K 精确 `date` 扫描 100/4,769，任务自有 Python PID `25332` 的 CPU 已约 148 秒，投影超过 30 分钟。已安全停止该 PID，写入 `completion.json=status:CANCELLED, reason=canary_listing_date_extraction_projection_exceeds_30_minutes`，并保留 `resume_state.json`、`progress.json`、`lock_evidence/cancelled-4dc22eab094ad70e.json`；没有删除或覆盖任何旧 attempt，也没有启动完整 `dev-run`、2025/2026、provider/L2/ML/生产 DB/scanner。
- 新信号建议的“读取到第 20 个 `<D` session 即停止”可保持上市资格判断，却与第 4 节“按去重有效日期精确计算 `<D` 的交易日数”相冲突。未静默改变该冻结口径，等待规划会话裁决后才可继续 canary；TNM-2B/TNM-3 继续锁定。

#### TNM-2AF 规划裁决：精确阈值证明

- 回测资格只消费“历史交易日是否至少 20”这一布尔条件，第 21 日及以后精确总数不进入任何特征、排名、成交或结果；要求完整总数属于无收益计算。
- 裁决为读取去重有效 `<D` 日期到第 20 个即短路；不足 20 时必须读到末尾并给出精确数量。这与全量计数产生完全相同的资格集合，不依赖日期排序，也没有引入近似或新参数。
- 第 4、8 节已据此收窄合同。只解锁 Terra 的 TNM-2AF2 短路实现、定向测试与一个新固定 canary；TNM-2B、2025/2026 仍锁定。

### TNM-2AF2 执行结果

状态：`tnm2af2_runner_verified`

- 唯一业务改动为日 K 上市资格的精确阈值证明：流式读取 active sheet 声明的 `date` 列，不依赖行日期顺序；仅计不同、有效且 `<D` 的日期。第 20 个出现即短路，记录 `listing_history_sessions_capped=20` 与 `listing_history_count_status=at_least_threshold`；EOF 未达 20 时记录精确数量与 `exact_below_threshold`。未恢复 60 日历日、首日跨度或其他启发式。早期不足阈值的文件缓存完整有效日期集，后续 D 重新计数；早期达到阈值仅对不早于已证明 D 的 target 复用。分钟可见历史降级使用同一 capped/status 语义。其余 TNM-2AF 六项 P1 修复未变。
- 定向验收：`C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_tail_next_morning`（23 tests）、`-m py_compile mining\tail_next_morning.py tests\test_tail_next_morning.py`、`git diff --check` 均通过。新增回归证明 20 个有效 `<D` 日期后即使后续有大量、无效或未来行也短路；无序/重复/无效日期不计；10 个 session 跨 60+ 日仍为 `exact_below_threshold`；以及早期不足、后续 D 达到 20 时从 EOF cache 正确重算。此前 Sol 的 6 项 P1 回归继续通过。
- 唯一新真实只读 fixed canary：`output/tail-next-morning-v1/dev-preflight/dev-preflight-d98305b5731c58fd`；175.1 秒完成 10 个 target（`20230106,09-13,16-19`）、14 个 2023 ZIP 输入和 10 checkpoint，无完整 `dev-run`、无 2025/2026、provider/L2/ML/生产 DB/scanner。`run_hash=d98305b5731c58fd172d457ff35db3b501d9f878a74c491f44744691f3008da4`，`spec_hash=9c5c3caa01b50e515d40c8a7d1a8fadfd2da3d1fe1f8ad070470cf7fe65bf747`，`runner_source_hash=80b5d56874bca446efbbc805cd04e13e1958d9171f583cb75707f95ec7e60604`，`input_manifest_hash=6e024046e05f836642b5350fd943068630ccfc86a807944458399014e9763acd`。输入含 5,532 个日 K XLSX、tree digest `4ee865a94382255d225bc692bf75843208333b1cbb77a35a34790d5c828e890a`。
- 终态与可重启证据：`completion.json=SUCCEEDED`，`resume_state.json` frontier=`next_index:14,last_trade_date:20230120`，`progress.json=10/10`，无 `frozen_rule.json`；`development_results.json` SHA-256 `0fbf4220e36c7bfef4ca3fee528ef4272384017ca9c7541abd8a771b037dcb11`，`daily_results.csv.gz` SHA-256 `574c6f8405bc54e2b1c04977ada6daefbe8f66d8b183ae0782423b965419f462`。每日 eligible 数为 `340,362,344,300,289,266,279,379,271,245`；容量诊断 `capacity_constrained`，只作独立袖套报告。
- 原 `dev-preflight-31f2a75b8db2112d` 和所有更早 attempt 原样保留；本轮未启动可见的后台 runner，TNM-2B/TNM-3 继续锁定，等待 Sol 只读复审。

#### TNM-2AR2：第二次独立审核（Sol）

- verdict：`tnm2ar2_changes_required`；原指定 Sol 会话已独立关闭 D+1 诊断/卖出隔离、上市阈值证明和完整输入哈希，后因会话 `notLoaded` 故障无法给最终 verdict；规划会话按进度上限改派全新、无上下文继承的 `gpt-5.6-sol` 只读补审，未降低执行/审核隔离。
- 已关闭：双年正收益 ready gate，任一年负收益/no feature/unresolved 的 `no_stable_development_signal`，旧 `frozen_rule.json` 清除；resume 从 checkpoint frontier 继续且不重读已完成容器，状态含 rolling history、visible sessions、pending exits 和 completed targets。
- P1-4a：十分位实现把 `rank=0.10` 放入第 2 桶，且 unresolved 成员从分桶样本数删除，导致每日十分位证据不能按冻结规则复算。
- P1-4b：成功延迟卖出经完成态转换后丢失延迟身份，`_execution_summary` 会把真实延迟退出计为 0，导致开发产物低报延迟执行。
- 最窄修复边界已回写第 4、6 节；只解锁 Terra 的 TNM-2AF3 分桶/退出身份修复、定向测试与新 fixed canary。TNM-2B、2025/2026 继续锁定。

### TNM-3 执行结果

状态：`locked_until_tnm2r_approve`

### TNM-4 执行结果

状态：`locked_until_tnm3r_validation_go`
