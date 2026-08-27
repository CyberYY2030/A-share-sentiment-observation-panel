# TNM-V2.3-R1：canary 性能与账户审计窄修复卡

状态：`ready_for_terra_repair`

规划所有者：根会话

修复所有者：Terra 会话 `01a03137-df48-7281-b7d7-b48f7e41f209`

独立复核：Sol 会话 `01a03141-39e9-7dd1-a359-037346adf01b`

冻结经济规格：`docs/superpowers/specs/2026-08-27-tail-next-morning-v23-task-cards.md` 第1～9节

被审提交：`69e63dac26cf8d8faaad46c26ee4c761f165cd80`

旧 canary：`v23-canary-6a8b222e31a1-6e38e7d68ffa`，原子 `CANCELLED/canary_time_limit`，永久只读。

## 1. 目标与禁止事项

只修复 TNM-V23-1R 确认的五个 P1 和三个直接相关 P2 证据缺口，使唯一全新 canary 在有界时间内
产出可审核的两日选股、正反五日流动性 fixture 和合成风险轨迹证据。

本卡不改变任何选股 gate、score、A6/B3、买卖时间、费用、风险阈值、暂停天数、same-N 对照或样本边界；
不授权调参、分层经济版本、完整 development、2025/2026、旧 identity resume/改写或新框架。

## 2. First divergence 与最小修复

### R1.1 两阶段 canary 依赖读取

期望：五日流动性是完整 A/B gate 的最上游共同消费者，失败代码不应读取 D-10...D-6 的深历史。

观察：`run_v23_canary` 在 `mining/tail_next_morning_v2.py:2235-2241` 先合并两日全部预选代码，
再按最早 target 读取全部 D-10...D-2；真实 canary 在第5个历史容器后耗尽900秒，尚未消费五日门槛。

唯一修复方向：

1. 对全部 target 先完成 short phase：使用已读 D-1，并只读取各 target 的 D-5...D-2；
2. 立即计算严格五日 amount gate，得到每个 target 的 survivor code set；
3. long phase 只为 survivor 读取尚缺的 D-10...D-6；
4. 同一来源日期在两 target/两 phase 间只物理打开一次；在首次打开前合并该日期已经可知的实际依赖代码。
   若某日期可能在另一 target 的 long phase 复用但 survivor 尚未知，只允许使用该 target 的候选上界做保守请求，
   禁止回退到“所有旧日期读取全部 evaluation_union”；
5. progress 至少记录 `phase,target,source_date,requested_code_count,survivor_count,open_count`；
6. 朴素全历史合成路径与优化路径必须逐字段经济恒等，优化只能减少读取，不能改变成员或排序。

相邻 gate 已排除：源容器可读、单容器 open_count=1、spec/input/run hash 和原子取消均正常；本修复不得重写解析器。

### R1.2 风险 episode 延迟退出回填

期望：一次 risk episode 的 `actual_forced_exits` 等于该 episode 计划强制退出且最终真实成交的持仓数，
包括触发日不可卖、后续重试成功的持仓。

观察：触发日 planned=3、当日成功2只、后续第3只成功时 ledger 已有3次 `forced_risk_exit`，
episode summary 仍为 actual=2。

最小修复：给未退出持仓保存稳定 `risk_episode_id`；任何后续 `forced_risk_exit` 成功时，回填所属 episode 的
实际退出数、退出日期和持仓标识。不得按自然日另建 episode 或重复计数。

### R1.3 cooldown 与无效买窗的分类顺序

期望：`cooldown_skip` 只表示本可成交但因 `RISK_OFF` 未买；买窗无效始终为 `unavailable_buy`。

最小修复：处理当日信号时先判断 `event.bought/buy_price`，无效则记录 `unavailable_buy`；仅对买窗有效事件
再判断 `RISK_OFF` 并记录 `cooldown_skip`。暂停规避亏损/错过盈利只统计有效买窗事件。

### R1.4 canary 必须消费合成风险轨迹

canary 必须在与真实选股结果隔离的确定性 fixture 上实际调用生产账户状态机，并输出
`risk_fixture_evidence`。至少证明：

- 3只全部净亏触发；平均恰好-5%不触发、严格低于-5%触发；
- 触发日及后三个真实交易日不买，最早第4日且前日已清仓才恢复；
- 触发日不可卖、后续重试成功会回填原 episode；
- 无效买窗在暂停日仍为 `unavailable_buy`；
- 事件研究保留候选，账户才执行 `cooldown_skip`。

helper 单测不能替代该生产链证据。fixture 输出进入 canary summary/artifact manifest，但不得混入真实候选、账户或经济聚合。

### R1.5 完整 target calendar 的固定现金0日

期望：A、B及三个 same-N 对照都以完整 target calendar 为日样本；无事件日产生固定槽收益0；
AB 每日始终采用 A6/B3 的 `6/9` 与 `3/9` 权重。

最小修复：

- `_v22_daily_slots`/V2.3消费者显式接收完整 target dates；
- 为每个通道和每个 strategy/control 生成每个 target 的一行，空日 `selected_slots=0,daily_slot_net=0`；
- AB 不对当日存在的通道重新归一；
- 新增并输出“按月汇总后删除表现最好一个月”的月份、该月值和剩余累计；
- account summary 同时输出 `annualized_return,max_drawdown,average_utilization,initial_nav,final_nav,N`。

不得修改已有事件级收益或用错误旧聚合覆盖 V2.2 产物。V2.3 新 identity 自己生成正确聚合。

## 3. P2 证据语义补齐

- 全新 canary 必须从已提交的 clean implementation HEAD 启动，使 manifest 的 `base_commit` 就是代码/测试实现提交；
- `source_blob_identity` 仍逐项绑定 module/test/spec，三者必须与该提交内容一致；
- 每个 stale risk mark 保存 `mark_price,mark_source_date,decision_date,stale_days`；stale mark 只用于风险估值，不作卖价；
- progress 的结构化阶段字段、风险 fixture 和完整 calendar 聚合均须有生产链测试，不能只测 helper。

## 4. 写入白名单与提交顺序

修复提交 tracked 白名单仅：

- `mining/tail_next_morning_v2.py`；
- `tests/test_tail_next_morning_v2.py`。

Terra 先完成代码与测试并创建一个 clean 白名单 commit，再从该 commit 启动唯一全新 canary。
canary 证据只写 ignored output，不修改任务卡或 amend 实现 commit，确保 manifest `base_commit` 语义真实。
shared dirty 保持，不暂存、不回退；不得 push。

## 5. 验收

必须新增并通过：

1. 朴素/两阶段读取的候选、gate、score、A6/B3逐字段恒等；
2. 两 target 共享日期的请求代码集合、每容器单开和 progress 阶段计数；
3. 五日门槛失败代码不进入 long phase；
4. 延迟 forced exit 回填原 episode 且不重复计数；
5. 暂停日无效买窗分类与机会成本不受污染；
6. 合成 `risk_fixture_evidence` 走 canary 生产链；
7. A空、B空、双空、不足额四类完整 calendar 日槽和冻结 AB 权重；
8. leave-best-month 与 account summary 字段独立复算一致；
9. stale mark 来源字段及完全无 mark 的 fail-closed；
10. V2.2 回归与 V1 25 tests 不变，2025/2026 年界继续拒绝。

命令：

```powershell
python -m unittest tests.test_tail_next_morning_v2
python -m unittest tests.test_tail_next_morning
python -m py_compile mining/tail_next_morning_v2.py tests/test_tail_next_morning_v2.py
git diff --check
```

全新 canary 仍只允许 `20240923/20240926` 及2024内必要历史/退出数据。必须在有界时间内原子终结并至少输出：

- `300085@20240923` prior5 pass_count=1、因流动性剔除；
- `300339@20240926` prior5 pass_count=5、共同门槛通过及最终排名；
- 两日完整 eligible/A6/B3；
- 确定性 `risk_fixture_evidence`；
- 读取 phase、requested/survivor counts、source open counts；
- spec/input/run/code/test/base commit/artifact hash、2025/2026未读、E盘未写、PID/lock清理。

若再次超时或无经济 checkpoint，保留新 identity 原子终态并停止，不得第三次尝试。

## 6. 交付与后续门禁

Terra 交付实现 commit、测试计数、canary identity/终态/hash、两日名单/fixture、性能计数、dirty/PID边界后停止。
只交原 Sol 执行 TNM-V23-1R2 独立只读审核。只有 `approve` 才允许 TNM-V23-2；
`changes_required|blocked_evidence` 均继续锁定 development 和 2025/2026。

Lessons 决策：`skip`。当前是项目专属实现缺陷修复；是否形成可复用 lesson 等真实 canary 验证后再判断。
