# TNM-V2.3-R：Codex 自动更新中断后的恢复与独立宿主执行卡

状态：`authorized_for_terra_recovery_preflight_only`

规划所有者：根会话

恢复实施与终态验收：Terra 会话 `01a03137-df48-7281-b7d7-b48f7e41f209`

独立只读审核：Sol 会话 `01a03141-39e9-7dd1-a359-037346adf01b`

用户授权：2026-08-28 明确要求找到中断原因、彻底解决并继续完成测试。

冻结经济规格与代码：V2.3 主任务卡、已批准 runner commit `63a496fcbb65c9548027d1b5d00d39a46d85bf63`、spec hash `4dc58bf0106b77124814b75f3ae4ce098d17b279582c1c705a60d38fe698bf36`。本卡只修复执行宿主和恢复流程，不改选股、交易、账户、统计公式、输入范围或经济身份。

## 1. First divergence 与已排除层

期望：唯一 detached writer 独立于 Codex 客户端生命周期运行；任何正常/异常退出都原子写入 `completion.json`。

实际：run `v23-development-4dc58bf0106b-adfb3b853979` 在 `336/474`、`source_frontier=20240611` 后消失；PID `48940` 不存在，`completion.json` 不存在，原 `run.lock` 仍在，stdout/stderr 均为空。

时间证据：

- progress 最后更新时间：`2026-08-28T07:14:17.658730+08:00`；
- Windows Update 在 `07:15:27` 安装 `9PLM9XGG6VKS-OpenAI.Codex`，版本由 `26.820.9563` 更新为 `26.820.10647`；
- AppXDeployment 事件 `9643` 明确记录 `TerminateApplications 成功`；
- AppModel-Runtime 随后销毁旧 Codex AppX 容器，并在 `07:15:28` 创建新 Codex 进程 `77944`；
- Python 无 traceback、无 WER/Application Error、无资源耗尽或系统重启证据。

首个分歧位于启动拓扑：所谓 detached Python 仍继承 Codex AppX/Job 生命周期，客户端更新终止应用时连带终止 writer。生产 runner、数据、策略和内存边界在被杀前均保持正确：`error=null`、`max_cached_market_days=11`、`retained_candidate_rows=0`、所有已记录 source `open_count=1`。

## 2. 恢复目标与排除项

目标：保全原 identity 与 336 个已提交日期，从最新连续 SHA-bound marker 恢复；后续 writer 由 Windows Task Scheduler 独立托管，不属于 Codex 进程树，继续到原子终态。

禁止：

- 新建经济 run identity、重算已提交日期、删除/覆盖旧 checkpoint、修改源数据；
- 直接 `Start-Process`、Codex terminal session、AppX 子进程或未验证 wrapper 承载长批；
- 自动 restart、并行 writer、第四次 preflight、真实 canary、调参；
- 读取/启动 2025/2026；
- 在恢复预检或 Sol approve 前移动旧 `run.lock` 或启动真实 resume；
- 把系统更新中断伪写为 runner 的 `FAILED/CANCELLED`。

## 3. TNM-V23-R1：Terra 只读恢复预检与宿主 smoke

本阶段不得启动真实 resume。

### 3.1 原 run 只读核验

Terra 必须记录并复算：

1. HEAD、spec/module/test/card blobs、run manifest、input manifest hash、run hash/run id 与批准包完全一致；
2. `completion.json` 缺失、PID `48940` 已死亡、旧 `run.lock` 原字节 SHA256；
3. 连续 marker frontier、marker 数、最后日期和索引；逐 marker 校验其 state/signal/outcome/account 引用的路径、SHA、run/date/index/unit；孤儿文件只隔离不删除；
4. 调用现有 V2.3 流式 projected-marker 恢复验证路径，证明 frontier=`20240611`、完成数=`336`、账户状态/事件可恢复、候选峰值有界；
5. 只读取恢复所需的最多10个 warm market days，并验证全部已消费 input identity；任一不匹配立即 `blocked_evidence`，不得修补产物或启动；
6. shared dirty、staging、V2.2/V2.3 旧产物及 E: 写入边界保持不变。

### 3.2 Windows Task Scheduler 生命周期 smoke

只允许 ignored helper 和一个唯一 smoke task：`TNM-V23-SMOKE-adfb3b853979`。

- helper 必须显式 bootstrap `REPO_ROOT`，固定绝对 Python 与工作目录，写入 PID/PPID/argv/start/finish JSON；
- Task Scheduler 设置：当前用户、`RunLevel=Limited`、`Hidden=true`、`ExecutionTimeLimit=PT0S`、不因电池切换停止、不自动 restart、`MultipleInstancesPolicy=IgnoreNew`；
- smoke 仅等待15秒并原子写完成文件，不访问 E:、不导入经济数据、不创建 run identity；
- 运行中证明进程祖先链不含任何 Codex/OpenAI AppX 进程，Task Scheduler 是启动来源；任务完成后删除 smoke task，保留 helper/task XML/日志/hash；
- 任一 smoke 异常立即停止，不得改参数重试或启动真实 resume。

### 3.3 R1 交付

Terra 交付紧凑证据，不提交生产代码：根因事件、marker/identity/input 验证、warm读取边界、projection峰值、smoke PID/PPID/任务设置/完成文件、task cleanup、dirty/staging/E盘/未来年份边界。

随后只交 Sol 做 `TNM-V23-R1R` 独立只读审核，verdict 仅可为 `approve|changes_required|blocked_evidence`。只有 `approve` 才进入 R2。

## 4. TNM-V23-R2：保全旧锁并执行唯一恢复

Sol approve 后，Terra 才可：

1. 再次确认 PID `48940` 死亡且无任何相同 run hash writer；
2. 记录旧 `run.lock` SHA/mtime/content，并原子重命名为 `run.lock.interrupted-codex-update-20260828T071527Z`，不得删除；
3. 创建 ignored、固定 SHA 的恢复 helper 与唯一任务 `TNM-V23-RESUME-adfb3b853979`；helper 只执行：

```text
python -m mining.tail_next_morning_v2 v23-development
  --minute-root E:\分钟数据
  --daily-root E:\日K线全部至202606
  --output-dir <repo>\output\tail-next-morning-v2\v23-development
  --resume-run-id v23-development-4dc58bf0106b-adfb3b853979
```

4. helper 自行将 stdout/stderr 写入固定日志，并把逻辑 argv、PID、PPID、task name、helper SHA 写入 sidecar；不得调用新 preflight、不得生成新 identity；
5. 启动后只做一次短健康检查：Task Scheduler/进程存活，祖先链无 Codex，新的 `run.lock` PID/command/run hash正确，progress stage=`resumed`，frontier 不倒退，manifest/identity 不变；
6. 记录 task/PID/输出目录/日志后交低频只读 heartbeat；禁止 restart/resume/重复启动。

运行中只读 `progress.json`、`completion.json`、`run.lock`、PID/完整命令行、Task Scheduler状态和 stderr尾部。PID消失且无原子终态立即 blocked；FAILED/CANCELLED/BLOCKED 保留证据并停止。

## 5. 终态验收与清理

`SUCCEEDED` 后：

1. Terra 只读验收完整 474 targets、连续 marker、全部正式产物/hash、经济摘要、风险/暂停/恢复、账户与事件记录；
2. 验证新 `run.lock` 已由 runner 清除，旧中断锁副本仍保留；删除已结束的 Task Scheduler resume task，保留 task XML/helper/log/hash；
3. 主任务卡只追加 docs-only 终态证据 commit；不混入 shared dirty；
4. 只交 Sol 执行 `TNM-V23-2R` 最终独立经济审核；只有 `approve` 才向用户交付回测结论。

## 6. 验收标签与 Lessons

- R1 最低完成标签：`tnm_v23_recovery_preflight_verified`；
- R2 启动标签：`tnm_v23_resume_scheduler_launched`；
- 最终经济完成标签仍为 `tnm_v23_development_completed`，不得降低或另造经济版本。

Lessons 决策：`create` 候选，范围为 Windows AppX/Codex 宿主中的 HEAVY-BATCH：客户端 `Start-Process` 不等于生命周期独立；长批必须由外部调度器托管，并用祖先链 smoke 验证。待真实 resume 终态后再 capture，不提前提升为规则。
