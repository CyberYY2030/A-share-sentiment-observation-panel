# TNM-V2.3-D：开发批 runner 闭合与启动卡

状态：`authorized_for_terra_runner_and_preflight`

规划所有者：根会话

实现与执行所有者：Terra 会话 `01a03137-df48-7281-b7d7-b48f7e41f209`

独立只读审核：Sol 会话 `01a03141-39e9-7dd1-a359-037346adf01b`

用户授权：已接受 V2.3 主任务卡并要求按既定工作方式完成；Sol 的 TNM-V23-1R3 verdict 为 `approve`，仅授权进入 TNM-V23-2，2025/2026 继续锁定。

冻结经济规格：`2026-08-27-tail-next-morning-v23-task-cards.md` 第1～9节与已批准提交 `0376652d7e0eb00265ebc33f9333010379209fb3`。本卡只补齐开发批入口、流式执行和证据链，不改任何经济公式或阈值。

## 1. First divergence 与停止边界

预启动检查的期望是存在主任务卡规定的唯一 CLI/runner `v23-development`，可从批准的 V2.3 生产逻辑启动 detached 2023～2024 开发批。

实际状态：当前 CLI 只有 `v23-canary` 和 `v22-development`；`run_v22_development` 固化 V2.2 选股、上午退出和账户逻辑，不能作为 V2.3 开发批入口。首个分歧位于开发批命令分发/runner 消费者缺失，不在已经批准的 V2.3 gate、score、下午退出或风险状态机。

因此禁止：

- 用 `v22-development` 跑 V2.3；
- 扩展 canary targets 冒充 development；
- 复制一个平行策略模块或重写解析/打分/状态机；
- 在 runner 通过独立审核前读取真实 2023～2024 长批数据；
- restart/resume 任一旧 canary identity；
- 读取或启动 2025/2026，写入 `E:` 源数据目录，调参或创建分层经济版本。

## 2. 最小实现

只在 `mining/tail_next_morning_v2.py` 中新增/复用以下生产消费者，并在 `tests/test_tail_next_morning_v2.py` 添加回归：

1. `run_v23_development(...)` 与 CLI `v23-development`，输出独立 `v23-development-*` identity；
2. 复用 V2.3 已批准的 preselection、连续五日流动性、A/B ranking、14:51 buy、13:04 decision、13:05～13:10 exit、风险暂停、事件聚合和九袖套账户逻辑；禁止调用 V2.2 经济消费者替代；
3. 复用成熟的原子 daily commit、checkpoint/outcome journal、lock、progress、completion、artifact manifest 和单 writer 模式，仅做 V2.3 所需的窄适配；
4. target calendar 只含可在 2024 边界内形成信号及退出证据的 2023～2024 日期；末端未退出写 `unresolved_at_development_end`，不得读 2025 补结算；
5. D 与 D-1 的 checkpoint 消费身份必须绑定显式 `set(D point-in-time universe)` 的实际成员，不能把整日容器所有额外代码过绑定；D-5...D-2 short gate 失败成员、D-10...D-6 long survivor、每次 event exit 的实际成员仍按已批准消费账绑定；
6. 每个来源容器每个进程最多物理打开一次；首次打开前合并当时所有已知 selection short/long 与跨 target event-exit 消费者，非子集重开 fail-closed 并先写 progress；
7. 内存保持有界，progress 至少记录 `completed_units/total_target_dates`、`source_frontier`、`open_exit_events`、`max_cached_market_days`、更新时间和异常摘要；
8. 正式输出必须包含主任务卡第7节全部产物，并使用 `development_summary.json` 与 execution label `tnm_v23_development_completed`。

## 3. 不变量与身份

- runner commit、spec hash、input manifest、module/test blob 和 run hash 在真实 preflight 前冻结；
- spec hash 绑定 V2.3 主任务卡冻结经济部分及本卡 runner/身份边界；
- 时间戳、PID、绝对输出路径不进入经济 hash；
- D/D-1 实际成员的内容变化必须使 checkpoint 身份失配；同容器未消费的额外代码变化不得使该 target checkpoint 失效；
- V2.2 全部代码提交、SUCCEEDED/FAILED identity、经济产物和旧 V2.3 canary identity永久只读；
- shared dirty 不暂存、不回退、不混入提交；不 push。

## 4. 必须先失败后通过的测试

至少覆盖：

1. CLI 解析与 execution label/独立 output prefix；
2. 两日以上生产级合成 development 路径逐字段等于对应 V2.3 canary/朴素路径；
3. D/D-1 容器含 universe 外额外代码：改额外代码不改变 checkpoint identity，改实际成员必须拒绝；
4. 跨 target 退出日期与后续 selection 日期重叠且代码集合不相交，物理 open count 仍为1；
5. daily commit 原子性、PID lock、FAILED/CANCELLED/SUCCEEDED 终态、成功 fast-path 只读复演；
6. 2024 末端未决不读2025，输入 manifest与实际 source records均拒绝未来日期；
7. 完整 target calendar 的现金0日、A6/B3固定槽、风险暂停、cooldown skip、账户NAV、leave-best-month与 same-N controls；
8. V2.2 与 V2.3 canary既有测试全部保持通过。

完整门禁：

```powershell
python -m unittest tests.test_tail_next_morning_v2
python -m unittest tests.test_tail_next_morning
python -m py_compile mining/tail_next_morning_v2.py tests/test_tail_next_morning_v2.py
git diff --check
```

## 5. 写入白名单与串行门禁

runner实现白名单仅：

- `mining/tail_next_morning_v2.py`；
- `tests/test_tail_next_morning_v2.py`。

Terra 先创建一个 clean、可逆的 runner commit，完成 bounded synthetic preflight 后停止；不得在同一阶段启动真实 development。交付 commit、测试计数、命令、hash、预计 target 数/输入边界、PID/lock/dirty 证据。

随后只交 Sol 执行 `TNM-V23-D-R` 独立只读审核，verdict 只能为 `approve|changes_required|blocked_evidence`。Sol 必须核验本卡全部不变量，尤其不得把 V2.2 runner 换标签、D/D-1 过绑定与未来日期隔离。

只有 `approve` 后，Terra 才可从该审核通过 commit 做一次 bounded read-only preflight，冻结完整命令、run identity、spec/input/code/test hash，并启动唯一 detached 单 writer 2023～2024开发批。记录 PID、完整命令、日志和输出目录后立即交低频只读监控；禁止模型会话承载重计算、重复启动或盲目 resume。

## 6. 终态验收

开发批 `SUCCEEDED` 后，Terra 只读验收并在 V2.3 主任务卡第10节追加紧凑证据，做单独 docs-only commit：target/信号/成交/未决数量、风险触发/暂停/恢复、事件与账户核心指标、全部正式产物 hash、身份闭环、V2.2与旧canary未变、2025/2026未读、E盘未写、PID退出、run.lock清除、shared dirty边界。

最终只交 Sol 执行主任务卡 `TNM-V23-2R`。只有最终 `approve` 才可向用户交付经济结论。

Lessons 决策：`skip`。这是项目专属的缺失生产消费者闭合；是否形成可复用规则需等待长批与最终审核。
