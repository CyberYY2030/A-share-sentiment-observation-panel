# TNM-V2.2-1F2：最终生产边界修复卡

状态：`ready_for_terra_code_repair`

规划基线：`d86565e`

经济定义仍以 `2026-08-26-tail-next-morning-v22-task-cards.md` 第1～10节为准；本卡禁止改变任何 gate、score、阈值、排序、A6/B3、买卖时点或30bps。

## 1. 必修 P1

1. **退出日期进入内容身份**：按日期退出扫描每实际读取一个未来日期，都必须 `setdefault(date,set()).update(consumed_codes)`，并把该日真实分钟成员 SHA/缺失 sentinel 写入所属 checkpoint 的 `unit_consumed_input`。resume/fast path 必须覆盖这些动态退出日；改写任一退出日成员必须拒绝旧结果。
2. **coverage fail closed**：任一通道/年份正式 outcome coverage `<0.99` 时，`economic_status=blocked_data_quality`，不得返回 ready。unresolved、buy unavailable 等原因分别计数并保留；描述性统计可展示但明确 provisional/blocked。
3. **NAV 禁止未来回填**：每个开放事件保存 `mark_price_by_date`；D 日使用当日最后有效 close，后续每个已扫描日期使用该日最后有效 close。九袖套计算日期 t NAV 只能使用 `<=t` 的价格；不得用最终 `last_mark_price` 回填历史。缺当日价时只允许沿用最后一个更早有效 mark并计数 `stale_mark_days`。
4. **边界并列不得崩溃**：事件研究保留全部经济并列并按边界槽等分 slot weight，使 A/B 日权重总和分别不超过6/3。九袖套若出现超出 A6/B3 的完全经济并列，必须返回 `blocked_boundary_tie_account` 和成员证据，不得 `KeyError`、任意按代码选择或伪造账户总收益。
5. **真实 CLI resume**：`v22-canary` CLI 暴露 `--resume-run-id` 并原样传入 `run_v22_canary`；合成 CLI 测试证明生产入口能恢复。

## 2. 必修 P2 统计 schema

按 channel × strategy/control × year/combined 分开，不得把不同通道/策略混入同一 bootstrap：

- gross/net 胜率；
- gross/net 的 mean、median、std、p10、p25、p75、p90、min、max；
- holding sessions mean、median、max；
- fixed-slot daily 的 mean、median、arithmetic_sum、compound_diagnostic、max_drawdown；
- coverage及缺失原因；
- 固定月 block bootstrap：策略 daily_slot_net 均值、策略日胜率、相对三个对照中**全期 mean_daily_slot_net 最大者**的配对差值，各自95% CI；最强对照先按全期点估计确定，不能逐日挑最好；
- canary 月份/日期不足时返回明确 `insufficient_for_bootstrap`，schema仍完整。

## 3. 进度与15分钟消费者

- 每次 progress 原子写入必须含 `updated_at`；
- `v22-canary` 生产入口内置 `--max-elapsed-seconds`，默认900；在每个来源日期/退出 frontier 后检查；达到上限原子 `CANCELLED:canary_time_limit`；
- 合成测试用可控时钟证明边界前继续、边界时 CANCELLED。外部监控仍只读，不把3分钟当硬停止。

## 4. 测试与授权

必须新增生产路径回归：退出日内容改写拒绝 resume、50% coverage blocked、逐日 mark 不前视、stale mark、rank7/4并列不崩且账户blocked、CLI resume、完整统计字段、分组 bootstrap、updated_at、900秒取消。

Terra tracked 白名单仅：

- `mining/tail_next_morning_v2.py`；
- `tests/test_tail_next_morning_v2.py`；
- 本卡第5节执行证据。

只运行 V2/V1/py_compile/diff-check和临时合成生产测试；禁止 E盘、真实 canary/development。一个白名单本地 commit后停止。随后只交独立 Sol 复审；`approve` 前不得启动新 identity。

## 5. 执行证据

尚未执行。Terra 只能追加本节。

### V22-1F2（Terra，2026-08-26）

状态：`v22_1f2_code_verified`。仅执行本地实现与临时合成 production-runner 测试；未读取 `E:\`，未创建真实 canary/development identity。动态退出扫描现在按实际日期/代码累积 checkpoint 内容身份，合成测试确认单独改写退出日等长成员即拒绝 fast-path resume。coverage 低于99%返回 `blocked_data_quality`；每个开放事件持有 `mark_price_by_date`，NAV 不回填未来价格，缺失日计入 `stale_mark_days`。完全经济并列的事件按槽权重保存，九袖套对 rank 超出 A6/B3 的并列成员返回 `blocked_boundary_tie_account` 和证据。

生产 CLI 现支持 `--resume-run-id` 与默认900秒 `--max-elapsed-seconds`，时间边界写原子 `CANCELLED:canary_time_limit`；progress 含 `updated_at`。通道×策略/对照的 gross/net/holding/coverage/fixed-slot schema、逐年/逐月及月 block bootstrap 已补齐，并以全期最大 mean daily slot net 的同通道对照计算配对差值。

验证：`tests.test_tail_next_morning_v2` 43 项通过、1 项真实源测试显式禁用；`tests.test_tail_next_morning` 25 项通过；`py_compile`、`git diff --check` 通过。等待 Sol 复审；不得派发真实 identity。

旧 attempts 和现有 shared dirty 全部保留；不 push。Lessons：`skip`。
