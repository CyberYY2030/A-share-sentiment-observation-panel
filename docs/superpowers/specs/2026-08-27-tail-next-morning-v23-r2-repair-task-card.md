# TNM-V2.3-R2：跨目标消费者与消费身份闭合修复卡

状态：`authorized_for_terra_repair_and_one_new_canary`

规划所有者：根会话

修复所有者：Terra 会话 `01a03137-df48-7281-b7d7-b48f7e41f209`

独立复核：Sol 会话 `01a03141-39e9-7dd1-a359-037346adf01b`

用户授权：2026-08-27 明确“授权修复”，包含一次全新 identity 的真实 canary；不包含 development。

冻结经济规格：V2.3 主任务卡第1～9节；上一修复卡仍有效，除本卡精确修正的调度与身份边界外不得改写。

被审提交：`68980785e82b9c93205b770575314e2bf20dc02b`

永久只读 identity：

- `v23-canary-6a8b222e31a1-6e38e7d68ffa`：`CANCELLED/canary_time_limit`；
- `v23-canary-6a8b222e31a1-b562156e9817`：`FAILED/v23_container_reopen_forbidden:20240924`。

## 1. 目标和停止边界

只关闭 TNM-V23-1R2 的两个 P1 与一个直接 P2：

1. 同一来源日期首次打开前，合并该日期已知的 short、long、跨 target 退出消费者；
2. checkpoint 身份绑定 short gate 通过与失败代码实际消费的 D-5...D-2 内容；
3. 被拒绝的重复请求在抛错前写入结构化 progress 证据。

不改变五日门槛、A/B 形态和排名、13:04/13:05退出、风险阈值、三日暂停、费用、对照或统计公式；
不新增通用调度框架，不重新实现解析器，不启动 development，不读取2025/2026，不改写或 resume 旧 identity。

## 2. First divergence 与唯一调度不变量

观察路径：

- `run_v23_canary` 只合并两 target 的 short/long 信号依赖；
- 20240924 首次按 `20240926 short` 请求1292只代码并打开成功；
- 随后 `20240923` 已选事件把同一日期作为 D+1 退出日，请求另一组代码；
- loader 正确拒绝第二次物理打开，原子失败。

调度不变量：

```text
在 source_date 第一次物理打开前：
requested_codes(source_date)
= 当时所有已知 selection short 消费者
∪ 当时所有已知 selection long 消费者
∪ 所有较早 target 已形成且会在该日检查退出的 open event codes
```

最小实现顺序：

1. 先为所有 target 完成 D 与 D-1 preselection，冻结各 target 候选上界；
2. 按 target 日期升序推进；较早 target 完成 short→long→selection 后，立即形成其 open events；
3. 在打开后续 target 的任一历史日期前，用一个窄的 `codes_for_first_open(source_date)` 计算上式并一次性请求；
4. 若 source_date 已缓存，后续请求必须是首次请求代码集合的子集；否则 fail-closed；
5. 不得用全市场或所有日期的全 evaluation union 绕过调度，也不得允许第二次物理打开；
6. 只有较晚 target long survivor 尚未知且日期将被较早 target 提前打开时，可将该较晚 target 的 preselection 候选上界并入首次请求；这是可证明的最小保守上界；
7. source open count 仍必须逐日等于1。

相邻 gate 已排除：容器解析、首次打开、缓存命中和重开拒绝均按合同工作；修复点仅为首次请求依赖集合不完整。

## 3. 完整消费身份

每个 target checkpoint 的 `unit_consumed_input` 必须由显式消费账构造，而非只从 survivor rows 反推。

至少绑定：

- D 与 D-1 中实际决定 preselection、信号、买窗的成员；
- 每个 preselection 候选在 D-5...D-2 的实际成员内容身份，包括五日门槛失败代码；
- short gate survivor 在 D-10...D-6 的实际成员内容身份；
- 该 target 事件实际消费的每个退出日期/代码成员；
- risk fixture 使用合成身份并与真实源身份隔离。

同一来源文件可被物理读取为保守上界，但只有实际参与该 target 判定的成员进入该 target 消费账。
resume/checkpoint 快路径必须重新核验这些成员内容；任一内容变化即拒绝，不能仅依赖 size/mtime 或 survivor 子集。

## 4. 拒绝请求的 progress 证据

发现已缓存日期收到非子集请求时，必须先原子写 progress，再抛 `v23_container_reopen_forbidden`。记录：

- `phase,target,source_date`；
- `first_requested_code_count,rejected_requested_code_count,missing_code_count`；
- missing code set 的确定性 SHA-256，不在主 progress 展开巨型名单；
- `open_count`、首个请求阶段及触发消费者类型 `selection_short|selection_long|event_exit`。

正常 SUCCEEDED 路径也保留每日期首次请求的消费者类型和代码数，供 Sol 证明依赖闭合。

## 5. 必须先失败后通过的测试

新增生产级合成测试，数据形状不得复用同一代码集合掩盖问题：

1. 两个 target 的 preselection 代码集合不相交；
2. 较早 target 选出代码 `EARLY_EXIT`，其 D+1 恰好与较晚 target short 日期重叠；
3. `EARLY_EXIT` 不属于较晚 target 候选；首次打开必须同时包含它和较晚 short codes，且 open_count=1；
4. 较晚 target long 日期与较早 target short/long 重叠时仍单开，经济结果与朴素全依赖路径逐字段一致；
5. 修改一个 short gate 失败代码的 D-5...D-2 内容，保持 size/mtime 不变，checkpoint/resume 必须拒绝；
6. 修改未实际参与 target 判定的保守读取成员，不得错误改变该 target 消费身份；
7. 人为制造遗漏请求，断言抛错前 progress 已包含被拒请求计数、missing SHA和消费者类型；
8. 上一修复卡的风险 episode、无效买窗、risk fixture、完整 calendar和聚合测试继续通过。

完整门禁：

```powershell
python -m unittest tests.test_tail_next_morning_v2
python -m unittest tests.test_tail_next_morning
python -m py_compile mining/tail_next_morning_v2.py tests/test_tail_next_morning_v2.py
git diff --check
```

## 6. 写入白名单、提交和唯一真实 canary

tracked 白名单仅：

- `mining/tail_next_morning_v2.py`；
- `tests/test_tail_next_morning_v2.py`。

Terra 必须先创建一个 clean 白名单修复 commit，再从该 commit 启动一次全新 identity canary；
manifest `base_commit` 必须等于该修复 commit，module/test blob 必须与提交内容一致。证据只写 ignored output，
不修改任务卡、不 amend 实现 commit。shared dirty 不暂存、不回退，不 push。

新 canary 硬上限为1800秒；只有 progress 持续前进时才允许运行到上限。不得第三次重试本卡 canary。
无论 `SUCCEEDED|FAILED|CANCELLED` 均原子落盘、停止进程并保留证据。

SUCCEEDED 至少必须输出：

- 两日完整 eligible/A6/B3；
- `300085@20240923` prior5 pass_count=1并剔除；
- `300339@20240926` prior5 pass_count=5及最终排名；
- risk fixture、完整 calendar聚合和账户字段；
- 每日期首次请求消费者、requested codes、open_count=1；
- 完整 target消费身份和artifact hash；
- 2025/2026未读、E盘未写、PID退出、run.lock清除。

## 7. 交付与门禁

Terra 交付修复 commit、测试计数、新 identity/终态/hash、经济/fixture/消费身份/性能/PID/dirty 证据后停止。
随后只交原 Sol 执行 TNM-V23-1R3 独立只读审核。只有 `approve` 才可进入 TNM-V23-2；
若仍 `changes_required|blocked_evidence`，停止并重新请求用户决定。

Lessons 决策：`skip`。当前是项目专属调度与证据闭合修复，待真实 canary 验证后再判断复用价值。
