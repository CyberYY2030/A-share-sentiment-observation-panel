# TNM-V2.2 2023～2024 开发批执行卡

日期：2026-08-26
规划负责人：根会话
执行负责人：Terra `01a03137-df48-7281-b7d7-b48f7e41f209`
独立审核：Sol `01a03141-39e9-7dd1-a359-037346adf01b`

## 1. 已批准基线

- 经济规格：`2026-08-26-tail-next-morning-v22-task-cards.md` 第 1～11 节，不得调参。
- runner 基线：`4045da0`；成功 canary：`v22-canary-3178f827ce5b-ba59c5af0546`。
- canary 独立审核：`approve`，P0/P1/P2 均为 0。
- 2025/2026 继续锁定；ST 历史和公司行动仍按规格显式降级。

## 2. 目标与最小实现

为同一经济链增加一个 `v22-development` 命令，完成分钟源中 2023～2024 可形成 D-10 历史及 D+1 的全部信号日。经济 gate、score、A6/B3、控制组、买入、动态卖出、30bps、九袖套及统计函数必须直接复用已批准实现，禁止平行重写。

全量运行必须按交易日单向流式推进：每个 2023～2024 分钟容器最多打开一次；只保留最近 11 个全市场日和未平仓事件所需的最小状态，不得把 484 个全市场日同时常驻内存。当天先处理已有事件的上午退出，再形成当日 14:50 信号和 14:51 买入；同日退出现金可用于当日下午新买入。不得回看未来容器决定当日候选。

目标日定义为冻结日历中 `index>=10` 且存在 D+1 的日期；预计应与输入覆盖相符，禁止硬编码 479。2024 年末尚未满足退出条件的事件标记 `unresolved_at_development_end`，禁止读取 2025 补结算。

## 3. 可恢复性与身份

- 独立 mode、run id、run hash、输出目录：`output/tail-next-morning-v2/development/<run-id>`；不得复用 canary identity。
- 输入 manifest 只允许 2023/2024 分钟容器及日 K tree identity；代码、规格、输入共同绑定 run hash。
- 每完成一个信号日原子写不可变 signal checkpoint，并原子更新 resume state；退出结果使用可复演的 outcome journal/state，不得静默改写旧经济事实。
- resume 前重新核验已消费分钟成员与日 K workbook 内容 identity；不一致即拒绝。SUCCEEDED 只能显式同 identity resume 做只读验收。
- 原子终态仅 `SUCCEEDED|FAILED|CANCELLED`。失败保留证据，禁止盲目重启；单 writer，记录 PID、命令、stdout/stderr、run.lock 生命周期。

## 4. 必需产物

- `development_summary.json`：A、B、合并的事件数、resolved/unresolved、胜率、平均/中位/标准差、分位数、平均持有日、算术总收益；2023、2024、combined；月度表；固定槽位统计和月块 bootstrap；三个 same-N 控制比较；coverage 与降级声明。
- `daily_results.csv.gz`：每日完整 A/B eligible pool、score、percentile、rank、A6/B3 标记及当日合格数量。
- `event_results.csv.gz`：strategy 与三个控制的买入、逐日决策、退出、gross/net return、holding days、状态。
- `account_ledger.csv.gz`、`account_nav.csv.gz`：500 万、A1～A6/B1～B3 九个固定袖套，busy skip、现金与持仓；若有未决则最终总收益保持 blocked，同时给 MTM 诊断。
- `progress.json`、`resume_state.json`、`artifact_manifest.json`、`completion.json`，以及紧凑 source/open-count 证据。

必须报告每日 `A_eligible_count/B_eligible_count`，并保留每日实际 A6/B3 名单。总收益同时给出：事件净收益算术和、固定日槽诊断复利、九袖套账户收益；三者名称不得混用。

## 5. 测试与验收门

先仅实现 runner 和合成测试，禁止读取 E:、禁止启动全量批。至少覆盖：

1. 2023/2024 日历与 D-10/D+1 边界，2025 拒绝；
2. 单向日流、每容器单开、内存缓存有界；
3. 退出先于当日下午买入、A/B 动态跨日状态及阶段末 unresolved；
4. checkpoint/outcome replay/resume，等长内容变化且恢复 mtime 时拒绝；
5. 全量结果与双日 canary 在 20240923/20240926 的 eligible counts、A6/B3、事件结果完全一致；
6. 经济聚合、九袖套、三个控制、coverage、hash 与原子终态。

V2 全套、V1 25 tests、`py_compile`、`git diff --check` 必须通过。实施提交白名单仅：

- `mining/tail_next_morning_v2.py`
- `tests/test_tail_next_morning_v2.py`
- 本执行卡的“执行证据”追加

Terra 完成一个本地提交后停止；不得启动真实开发批。Sol 独立只读给出 `approve|changes_required`。只有 `approve` 才进入第 6 节。

## 6. 真实批启动合同

Terra 先做只读 bounded preflight：冻结目标数、输入年界、磁盘余量、单 writer、输出不存在、命令可解析；不得计算经济结果。规划会话单独批准后，Terra 只启动一个 detached `v22-development`，记录 PID/命令/run id/log；由 heartbeat 低频读取 `progress.json`、`completion.json`、run.lock、PID 与 stderr。模型不得轮询大产物。

SUCCEEDED 后 Terra 只读验收终态、目标/checkpoint/outcome 数、经济汇总和全部 SHA，并追加 docs-only 证据提交。随后 Sol 做最终独立审核；只有最终 `approve` 才可对用户交付 2023～2024 回测结论，仍不得自动启动 2025/2026。

## 7. 执行证据

### TNM-V2.2 开发 runner 实现（Terra，2026-08-26）

状态：`tnm_v22_development_runner_verified`。新增 `v22-development`，直接复用已批准的
V22 gate、score、A6/B3、三个 same-N controls、买入/动态退出、30bps、九袖套和 aggregates。
该 runner 仅发现/冻结 2023～2024 交易日，按日单向读取完整容器，最多缓存 11 个全市场日；
每个 signal 日写不可变 checkpoint，退出更新写独立 outcome journal，resume 前复核选择与退出
实际消费的分钟成员和日 K 内容身份。独立 development run hash 绑定经济规格、执行卡冻结部分、
代码和输入 manifest；`run.lock` 为 JSON owner，`CANCELLED` 的原终态保留为
`completion.cancelled.json` 后才允许显式恢复。

合成生产链覆盖：2023/2024 与 D-10/D+1 边界、2025 年界拒绝、单容器单开/11 日缓存、
上午退出先于当日下午信号、A/B 状态、阶段末 unresolved、checkpoint/outcome 回放、
等长内容改写拒绝 resume、CANCELLED→显式恢复、canary 20240923/20240926 的 eligible count、
A6/B3 及 event economics 完全一致，以及 controls/coverage/固定槽/九袖套/原子终态。

本地验证：Python 3.11 `tests.test_tail_next_morning_v2` 48 passed / 1 skipped；
`tests.test_tail_next_morning` 25 passed；`py_compile` 通过；`git diff --check` 通过。
全程未读取 E 盘、未创建真实 development/preflight/canary identity、未读取 2025/2026、无任务进程。
shared dirty 保持，未 push。后续真实批仍须按第 6 节另行批准。Lessons：`skip`。

### TNM-V2.2 真实 development bounded 只读 preflight（Terra，2026-08-26）

状态：`tnm_v22_development_preflight_ready_pending_launch_approval`。核验基线为
`ba720c7bc6ddb616d40f00565c4205203f3380b7`，runner/test blob 分别为
`b982a80c3de23e7416ee350ce0e3ebdda58dff81`、
`d636c3a111a9c17190b1ed47b97d30de60397dde`；经济 spec hash 为
`3178f827ce5b347fa5eea62605fff9d903cbc24a5eb2848098c3754876e30342`，
development spec hash 为 `8fffe4a3fba47496d68c49269fca70bc83da3633eca89bc5ff06b54899365a45`。
暂存区为空；既有 shared dirty 保持在白名单外。

分钟源仅按 2023/2024 路径、文件名和 `stat` 元数据枚举：484 个会话（2023=242、2024=242），
473 个 target（`20230117` 至 `20241230`，index 10 至 482），ZIP=242、目录=242；轻量日历
冻结 SHA-256 为 `28bd5c40a639150148b14e1b26eea974a1b9fb755c528299c0fcb831a13f1415`。
ZIP member read 和 `pandas.read_excel` 在预检中显式禁止。日 K 根可读，5532 个 `.xlsx`、
3,694,499,418 bytes；生产身份方案仍为既有的 `relative_path,size_bytes,mtime_ns -> tree_digest`，
不读取任何工作簿交易内容。未枚举分钟 2025/2026 路径，也未读取 2025/2026 内容。

`output/tail-next-morning-v2/development/` 当前不存在，递归 `run.lock`=0，无同类 writer；D 盘空闲
64,977,162,240 bytes。现有两日 canary 6,609,936 bytes 的线性 473-target 上界为
1,566,554,832 bytes，容量充足。CLI `v22-development --help` 已解析。预期启动命令为：

```text
C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m mining.tail_next_morning_v2 v22-development --minute-root "E:\分钟数据" --daily-root "E:\日K线全部至202606" --output-dir "output\tail-next-morning-v2\development"
```

启动时 stdout/stderr 应分别重定向至 development 父目录的 `<run_id>.stdout.log` 和
`<run_id>.stderr.log`；监控字段为 PID、run_manifest 的 spec/source/input/run hash、run.lock、
progress 的 source_frontier/completed_units/open_exit_events/updated_at、completion 和 stderr。具体
`run_id` 必须在获批 launch HEAD 下由完整 input manifest 计算；本 preflight 的 docs-only commit
会改变 `base_commit`，所以不得把 preflight 前的 identity 当作可启动 identity。未启动 runner、
未生成经济结果、未写 E 盘、无任务进程。等待规划会话批准 launch。Lessons：`skip`。
