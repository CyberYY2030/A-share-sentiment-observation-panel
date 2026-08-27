# TNM-V2.3-D-R1：恢复一致性、账户边界与有界内存修复卡

状态：`authorized_for_terra_synthetic_repair`

规划所有者：根会话

修复所有者：Terra 会话 `01a03137-df48-7281-b7d7-b48f7e41f209`

独立只读审核：Sol 会话 `01a03141-39e9-7dd1-a359-037346adf01b`

冻结经济规格：V2.3 主任务卡第1～9节、development runner 卡与 Sol 已批准的 V2.3 canary 经济定义。待修提交：`91df8e7b4f546201443a973bec04aedaf5283d2b`。

本卡只关闭 TNM-V23-D-R 的四个 P1 与一个正式输出 P2；禁止改选股、排名、退出、风险阈值、暂停天数、费用、对照或统计公式。禁止真实 preflight、真实 development、新 canary、resume 旧 identity、读取2025/2026或写 E:。

## 1. 原子恢复真相

### First divergence

`_v23_development_commit_day` 先发布 SHA-bound daily marker，后更新未被 marker 绑定的 `resume_state.json`。若在两步之间中断，marker frontier 已前进，resume 却从落后一日账户状态继续，事件与账户结果发生分叉。

### 唯一不变量

- 最新连续 daily marker 引用的 `states/<date>.json` 是唯一恢复真相；
- `resume_state.json` 仅为可丢弃缓存，必须与最新 marker state 的日期、index、run hash及内容身份一致；缺失、落后或不一致时从 marker state恢复，不得失败后猜测或从下一日继续；
- marker、state、signal、outcome、account journal 的SHA引用必须先闭合，marker最后原子发布；marker发布后即使进程立即死亡，显式resume与不中断执行的经济产物必须字节一致；
- SUCCEEDED fast path仍重新验证marker链、消费身份和artifact manifest。

故障注入必须精确发生在“最新 marker 已原子落盘、`resume_state.json` 尚未更新”之间；现有在整个 helper 返回后中断的测试不足，必须替换或补充。

## 2. resume 后账户产物恒等

读取每日账户journal时，`account_ledger` 与 `cooldown_skips` 会反序列化为不同对象。最终事件结果已知后，必须以 `event_id` 对两个正式消费者同时确定性回填 `hypothetical_net_return`，不得依赖不中断路径的对象共享副作用。

跨 cooldown 日中断/resume 的验收：

- `account_ledger.csv.gz`、`cooldown_skips.csv.gz`、`account_nav.csv.gz`、`development_summary.json` 与不中断路径逐字节一致；
- 同一个 `event_id` 在 ledger/cooldown 中的假设净收益相同；
- 规避亏损与错过盈利汇总独立复算一致。

## 3. boundary tie fail-closed 等价

已批准的一次性 `_v23_sleeve_account` 遇到任一 A/B 边界并列扩展时，在任何账户交易前整体返回 `blocked_boundary_tie_account`，账户 ledger/nav为空。

流式 runner 必须保持相同结论：

- 可以保留事件研究与候选产物；
- 正式账户 ledger/nav/holdings/risk/cooldown 以及账户收益不得保留并列发生前的局部交易冒充可用账户；
- summary 账户状态必须为 `blocked_boundary_tie_account`，execution label不得写成 `tnm_v23_development_completed`；使用明确的 fail-closed label/原子终态并停止长批经济交付；
- 合成差分测试要求一次性与流式的账户状态、正式账户产物和完成标签一致。

## 4. completed 内存有界

checkpoint可在磁盘保留完整 `all_candidate_rows` 作为审计证据，但 daily marker成功发布并验证后，运行内存不得长期保留每个target的完整候选明细。

最小实现：

- 为运行时保留结构定义一个窄投影，仅包含最终聚合/账户/恢复必需的 `daily`、`eligible_rows`、`seed_events`、消费身份/账本摘要和checkpoint引用；
- 完整checkpoint写盘后立即压缩内存对象；
- resume读取marker后同样立即投影，不把全部历史checkpoint候选对象常驻；
- 最终大CSV若需全量候选，必须从SHA-bound checkpoint流式读取/写出，不能重新构造无身份事实；
- 保持 market cache `<=11`，并新增 retained checkpoint/candidate rows 的明确 progress 指标；
- 100日、大候选合成测试断言常驻候选行/估算字节不随target数线性增长，且经济摘要与朴素小样本一致。

禁止引入数据库、通用回测框架或新依赖。

## 5. 正式账户字段完整性

在现有journal/CSV内补齐主卡第7节要求的可复算字段，至少包括：

- buy/sell/forced exit 的 `buy_window`、`exit_window`、`cost_bps=30`、`holding_days`、`sleeve_cash_after`；
- 每日日末每个袖套的 `cash`、持仓代码、buy price、mark price/date、状态；
- forced/unavailable/cooldown/busy/unavailable_buy 的稳定 action 与 event/episode关联；
- 所有字段在不中断、resume和SUCCEEDED fast path下确定性一致。

不得新增收费模型或改变净收益公式。

## 6. 身份、测试与写入边界

`_v23_development_spec_hash` 和 source blob identity 必须同时绑定 development runner卡与本修复卡；提交后报告新 spec/module/test/card blobs。tracked白名单仍仅：

- `mining/tail_next_morning_v2.py`；
- `tests/test_tail_next_morning_v2.py`。

必须新增/修正以下合成生产测试：

1. marker已发布、resume cache未写时进程中断，resume与不中断全部经济CSV/summary字节一致；
2. 跨cooldown日中断/resume，ledger和cooldown共同回填且字节恒等；
3. boundary tie一次性/流式差分与fail-closed completion label；
4. 100日大候选常驻内存有界、market cache≤11；
5. buy/sell/forced/daily sleeve字段完整且逐笔可复算；
6. 原D/D-1精确身份、short失败、long survivor、event-exit、future guard、原子终态、V2.2/V2.3 canary回归继续通过。

完整门禁：

```powershell
python -m unittest tests.test_tail_next_morning_v2
python -m unittest tests.test_tail_next_morning
python -m py_compile mining/tail_next_morning_v2.py tests/test_tail_next_morning_v2.py
git diff --check
```

Terra完成一个clean两文件修复commit和bounded synthetic preflight后停止，不得创建任何真实identity。交付first divergence、commit/parent/白名单、新hash、测试计数、字节恒等/内存上界/字段证据、PID/lock/staged/shared dirty边界。

随后只交原 Sol 执行 `TNM-V23-D-R2` 独立只读审核。只有 `approve` 才能授权一次 bounded只读真实preflight及唯一detached 2023～2024开发批。

Lessons 决策：`skip`。这是项目专属流式恢复和账户输出修复，待真实长批证明后再决定是否形成通用经验。
