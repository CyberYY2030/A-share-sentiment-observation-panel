# TNM-V2.3-D-R2：流式恢复读取与 cooldown 身份闭合修复卡

状态：`authorized_for_terra_synthetic_repair`

规划所有者：根会话

修复所有者：Terra 会话 `01a03137-df48-7281-b7d7-b48f7e41f209`

最终定向只读复核：Sol 会话 `01a03141-39e9-7dd1-a359-037346adf01b`

待修提交：`a8fbf7570e5ee3a0e66cc2cee886375455ea038d`。主任务卡、development runner卡、R1修复卡及已批准V2.3经济定义继续冻结。

本卡只关闭 TNM-V23-D-R2 的两个 P1。禁止改选股、排名、下午退出、账户风险公式、费用、统计、输出口径或已关闭的恢复/边界逻辑；禁止真实preflight、真实development、新canary、旧identity resume、E:访问、2025/2026或push。

## 1. Resume 与 SUCCEEDED fast-path 的逐 marker 投影读取

### First divergence

新跑路径已在 marker 发布后压缩 `completed`，但共享 `_v22_development_read_commits` 会先把所有 marker 的完整checkpoint装入 `markers/completed`；V2.3 resume和SUCCEEDED fast path到调用结束后才投影。真实约473个target时，恢复/验产仍可能瞬时持有全部历史候选并OOM。

### 唯一不变量

- V2.3 resume与SUCCEEDED fast path必须逐 marker：读取并验证 marker → 读取SHA-bound signal/state/outcome/account → 当场抽取V2.3运行投影/消费成员需求/seed events → 丢弃完整候选对象 → 处理下一个marker；
- 任一时点常驻的 `all_candidate_rows` 与仅供checkpoint审计的完整 `eligible_rows` 数量/估算字节必须有固定上界，不随marker数量增长；
- marker连续性、frontier、run hash、state日期/index、signal unit、outcome日期、account journal及全部SHA验证不能因投影而减弱；
- 需要生成 `daily_results.csv.gz` 时继续从marker-owned checkpoint流式读取，不把历史完整checkpoint重新聚合进内存；
- V2.2共享消费者不得被破坏。可新增V23专用读取器，或给共享读取器增加显式projector/consumer，但禁止复制整套恢复框架。

必须新增多marker大候选的两条独立测试：

1. CANCELLED/显式resume读取过程中，峰值 retained candidate rows/estimated bytes有界，恢复经济结果与不中断字节一致；
2. SUCCEEDED fast path验证过程中同样有界，并继续拒绝被篡改的实际消费成员、marker或artifact。

测试必须在读取过程中观测峰值，不能只断言函数返回后的投影结果。

## 2. Cooldown event identity fail-closed

### First divergence

当前 `event_id -> event` 使用dict comprehension，重复ID静默last-write-wins；ledger/cooldown的cooldown_skip找不到最终事件时保留null，导致避亏/错盈统计可静默错算。

### 唯一不变量

- 所有需要回填的最终事件 `event_id` 必须为非空字符串且全局唯一；重复或空ID立即抛 `TailDataError`；
- `account_ledger` 中 action=`cooldown_skip` 与 `cooldown_skips` 中的消费者ID集合必须完全一致，数量与每个ID的袖套/日期/代码等稳定键一致；
- 每条 cooldown 消费记录必须且只能匹配一个最终事件，缺失、重复或消费者集合/稳定键不一致立即抛 `TailDataError`；
- 两个消费者均用同一个最终 `net_return` 确定性回填，回填后不得为null/非有限值；
- 任一异常必须原子终态 `FAILED`，不得生成SUCCEEDED summary或静默沿用旧值；
- 正常唯一ID路径的规避亏损、错过盈利及resume字节恒等继续成立。

必须覆盖：重复最终event_id、空event_id、缺失event、ledger多/少一条、cooldown多/少一条、稳定键冲突，以及正常路径。

## 3. 身份、门禁与停止边界

`_v23_development_spec_hash` 与source blob identity必须同时绑定development runner卡、R1卡和本R2卡。tracked写入白名单仍仅：

- `mining/tail_next_morning_v2.py`；
- `tests/test_tail_next_morning_v2.py`。

完整合成门禁：

```powershell
python -m unittest tests.test_tail_next_morning_v2
python -m unittest tests.test_tail_next_morning
python -m py_compile mining/tail_next_morning_v2.py tests/test_tail_next_morning_v2.py
git diff --check
```

Terra完成一个clean两文件修复commit和bounded synthetic preflight后停止，不得创建任何真实identity。交付：first divergence、commit/parent/白名单、新spec/module/test/card blobs、resume/fast-path峰值上界证据、cooldown负面测试证据、既有门禁、PID/lock/staged/shared dirty边界。

随后只交原Sol做一次范围受限的 `TNM-V23-D-R3` 最终审核：只确认本卡两项修复及既有门禁无回归，不重新打开已在R2明确通过且未被本提交修改的经济设计。只有 `approve` 才允许Terra做一次bounded只读真实preflight并启动唯一detached 2023～2024开发批。

Lessons决策：`skip`。这是项目专属恢复内存与审计身份闭合，待真实长批证明后再决定复用价值。
