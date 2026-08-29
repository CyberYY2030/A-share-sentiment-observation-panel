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

## 7. R1A 修订：恢复预检本身也必须外部托管

2026-08-28 的首次 R1 实测补充了新的 first divergence：

- Terra 以 Codex 统一执行会话启动 ignored helper `v23_recovery_553887b.py --recovery-only`；
- helper PID `49372` 持续只读复演约2小时，CPU与读取量持续增长、内存平稳，并写出 `v23_recovery_553887b.running.json`；
- Terra 回合结束时 PID `49372` 与父 pwsh `35532` 同时消失，未生成 preflight JSON，未进入 smoke；
- 没有 Task Scheduler task、没有真实 resume、没有移动旧 `run.lock`、没有修改原 run；
- 这不是 checkpoint/data 失败，而是 R1 的逐 marker 消费身份复演也属于 HEAVY-BATCH，却仍由 Codex 会话承载。

因此第3节执行顺序修订为：

1. 先创建唯一 Task Scheduler smoke task `TNM-V23-SMOKE-adfb3b853979`，按第3.2节完成15秒祖先链/原子完成验证并删除 task；
2. smoke 通过后，用同一已验证调度机制创建唯一只读预检 task `TNM-V23-PREFLIGHT-adfb3b853979`，运行固定 SHA helper 的 `--recovery-only`；
3. 预检 task 必须使用 `RunLevel=Limited`、`Hidden=true`、`ExecutionTimeLimit=PT0S`、不因电池切换停止、无自动 restart、`MultipleInstancesPolicy=IgnoreNew`；helper stdout/stderr、PID/PPID/祖先链、task XML、helper SHA、running/preflight JSON路径必须冻结；
4. 预检运行中只低频读取 task state、PID、running/preflight JSON存在性和 stderr尾部；不得由 Codex 等待其计算，不得重复启动；
5. PID消失且无明确 preflight 终态立即 `blocked_evidence`；成功后删除已结束的预检 task并保留XML/日志/hash，再交 Sol 做 R1R；
6. R1A 仍严格禁止移动旧锁或启动真实 resume。原 PID49372 的 running JSON 和进程消失证据永久只读保留，不得冒充成功预检或覆盖。

本修订只改变恢复预检的宿主拓扑，不改变第1～6节的经济身份、checkpoint验证宽度、Sol门禁或最终恢复命令。

## 8. R1B 修订：规划身份使用祖先约束，不绑定可变 HEAD

R1A 的 Task Scheduler smoke 已通过，证明调度宿主与 Codex 生命周期独立；随后唯一 preflight task 在任何生产 calendar/input/marker 读取前以 `recovery_frozen_identity_mismatch:planning_head` 退出。

首个分歧是 ignored helper 把首版规划 commit `553887b` 与当前 HEAD 做精确相等比较。第7节宿主修订形成合法 docs-only commit `b7b8d1b` 后，经济代码、测试和原 run identity 均未改变，但该过度严格检查必然失败。规划/证据提交是可追加的控制面，不能作为已冻结经济 run 的精确 HEAD。

最窄修复：

1. 保持经济身份精确冻结：原 run manifest 的 `base_commit=63a496f...`、spec/run/input hash、module/test/economic/runner blobs必须逐字段相等；当前 working-tree module/test blob也必须相等；
2. 恢复控制面只要求 `b7b8d1b` 是当前 HEAD 的祖先，并记录实际 HEAD；不得要求 HEAD 精确等于任一规划或证据 commit；
3. 创建唯一新 helper/launcher/preflight task 名称并记录 SHA，不覆盖 R1A 失败 helper、stderr、task XML、health 与旧 running JSON；
4. 已通过的 smoke 不重复。只授权一次修正后的 `TNM-V23-PREFLIGHT-adfb3b853979-R2`；它仍按第7节由 Task Scheduler 托管，无自动 restart；
5. preflight 启动后只做短健康检查并立即交低频监控，不由 Terra 回合等待；成功后按原门禁交 Sol R1R；失败则停止，不得第三次 preflight；
6. 本修订仍不授权移动旧锁或真实 resume。

该修复只收窄错误的控制面身份约束，不放宽任何经济、输入、checkpoint或单 writer不变量。

## 9. R2A 修订：从冻结 runner checkout 恢复，不修改身份算法

R2 的唯一 Scheduler resume 在取得新 `run.lock` 或修改 `progress.json` 前，以
`TailDataError: v22_resume_identity_mismatch` 退出。失败任务、XML、helper、stdout/stderr、
terminal JSON 和旧锁归档必须原样保留；不得用同名 task 重试。

### 9.1 First divergence

原 run manifest 将以下项目共同绑定为经济执行身份：

- `base_commit=63a496fcbb65c9548027d1b5d00d39a46d85bf63`；
- 已批准的 spec/input/run hash；
- module/test/economic card/runner card blobs。

R2 launcher 却从当前规划 checkout `be1742a...` 启动。虽然其经济 blobs 与原 run 相同，
`_v22_prepare_run` 仍按当前 checkout 的 HEAD 计算 `base_commit`，因此得到不同 run id 并正确
拒绝显式 resume。首个偏差位于 launcher 的 checkout 选择，不在 checkpoint、数据、策略或
`_v22_prepare_run`。

### 9.2 最窄修复

不得修改 `mining/tail_next_morning_v2.py`、`tests/test_tail_next_morning_v2.py` 或放宽
`_v22_prepare_run`。Terra 只可：

1. 在 repo 的 ignored recovery 区创建 detached Git worktree，精确 checkout
   `63a496fcbb65c9548027d1b5d00d39a46d85bf63`；不得切换当前 shared checkout；
2. 固定 worktree 绝对路径、HEAD、module/test/card blobs，并验证与原 manifest/批准包完全一致；
3. 使用冻结 Python，从该 worktree 运行 import-only smoke，必须证明：
   `mining.tail_next_morning_v2.__file__` 位于冻结 worktree、`_current_commit()` 为 `63a496f...`，
   且 `PYTHONPATH`/当前 shared checkout 未劫持 import；
4. 前台运行 identity-only 合成检查：读取原 `run_manifest.json` 中已冻结的
   `input_manifest`，调用同一 `_v22_prepare_run`，必须返回原 run id/run hash，且 run manifest、
   progress、checkpoint、归档锁的 SHA/mtime/bytes 全部不变；该检查不得扫描 E: 或进入真实 runner；
5. 在冻结 worktree 运行 `python -m unittest tests.test_tail_next_morning_v2`、
   `python -m unittest tests.test_tail_next_morning`、`python -m py_compile` 和 `git diff --check`；
6. 保留 R2 失败 task 的 actual/requested XML 后，仅注销
   `TNM-V23-RESUME-adfb3b853979`，只读证明 task 不存在；不得覆盖任何 R2 失败证据；
7. 交 Sol 做 `TNM-V23-R2AR` 独立只读审核。只有 `approve` 才允许一次真实恢复；
   `changes_required` 只回传精确问题，`blocked_evidence` 立即停止。

### 9.3 唯一真实恢复

Sol approve 后，Terra 才可创建新任务 `TNM-V23-RESUME-adfb3b853979-R2`。launcher 必须：

- 使用固定 SHA ignored helper，显式设置 child cwd 为冻结 worktree，移除会改变 import 根的
  `PYTHONPATH`/`PYTHONHOME`，并在启动 writer 前再次断言冻结 HEAD、module path 和全部批准 blobs；
- 使用原第4节的完整逻辑 argv 与显式
  `--resume-run-id v23-development-4dc58bf0106b-adfb3b853979`，输出目录仍为当前 repo 中的原 run；
- 保持 Task Scheduler 的 Limited/Hidden/PT0S/IgnoreNew、battery=false、restart=0 与无 Codex
  祖先链不变量；不得创建新 identity、重跑 preflight、再次移动归档锁或使用同名失败 task；
- 短健康检查必须证明新 `run.lock` 的 run hash/PID/command 正确，`progress.stage=resumed`、
  completed/frontier 不倒退，manifest 字节不变。任一断言失败立即停止，禁止第三次真实 resume。

运行与终态验收继续遵守第5节。冻结 worktree 在最终 Sol `TNM-V23-2R` approve 前保留；
最终验收完成后，停止/注销 task 并用 Git worktree 机制移除该可复现 checkout，保留其
commit、路径、launcher/helper/task XML 和 SHA 证据。

### 9.4 对抗性验收

最便宜的证伪实验必须覆盖：

- 当前 shared checkout 启动 identity-only 检查仍得到 `v22_resume_identity_mismatch`；
- 冻结 `63a496f` worktree 启动同一检查得到原 run id/hash；
- 故意注入当前 repo 到 `PYTHONPATH` 时，launcher 在启动 writer 前拒绝 module path 不匹配；
- 错误冻结 commit、任一 module/test/card blob、spec/input/run id/hash 不匹配均 fail closed；
- 原 run manifest/progress/checkpoints/归档锁在全部 smoke/测试后字节与时间戳不变。

本修订不改变产品代码或身份定义。它让执行环境满足原有 fail-closed 合约，是恢复原 run 的
唯一最小充分变更。

## 10. TNM-V23-R2B 终态执行证据（2026-08-29）

- 唯一原 identity `v23-development-4dc58bf0106b-adfb3b853979` 已由冻结 worktree
  `63a496fcbb65c9548027d1b5d00d39a46d85bf63` 的 Scheduler writer 原子完成：
  `completion.status=SUCCEEDED`、`execution_label=tnm_v23_development_completed`、
  `run_hash=adfb3b8539799634895b06b67f7f2cd43b4866d8b87fb7ed27717b8b69c69a0d`。
- `progress.stage=aggregated`、target `474/474`、frontier `20241231`、open exit `0`；
  474 个 target checkpoint 与 484 个 source-day marker 的 state/signal/outcome/account
  SHA 引用全部逐项复算通过，artifact manifest 的 2,407 项文件与大小/SHA 全部一致。
  artifact manifest SHA256=`c292ac5f6f98a802569c683500a1b87b2eb26fe3ba73c57406c34197cb2a7539`；
  development summary SHA256=`92214e875a0f5b78007e44f5d34b8ce546fd3eca00576cd6db5ca9ead3f513dc`。
- 原 manifest SHA256 仍为 `0115b3a120a89f765b2b57cf0b741c188a4b28abc2d34a258c3fd4eec0d68d65`；
  中断归档锁保持 `c128e9729aa79ca79dea0881c6cb08e9a2217d272b59494b3435cff950873cb2`。
  新 `run.lock` 已清除；R2B helper terminal returncode=0，writer/helper stderr 合计 0 bytes。
- Task `TNM-V23-RESUME-adfb3b853979-R2` 的实际设置为
  Limited/Hidden/PT0S/IgnoreNew、battery=false、RestartCount=0；完成时 Ready/LastTaskResult=0，
  已仅注销该结束任务并证明不存在。requested/actual XML、helper、launcher、started/writer/terminal
  JSON 均保留在 `output/tail-next-morning-v2/v23-development/__recovery/r2b-resume/`。
- 只读终态审计：
  `TNM-V23-RESUME-adfb3b853979-R2.terminal-audit.json`，
  SHA256=`fbfd8ab7cdd7a26920690e18e69033b331bc5be0e5fa6e68c660c5da444cccb3`。
  它还记录经济终态：13,596 events、13,580 resolved、16 unresolved；A/B/AB 因未决项为
  `blocked_unresolved`，九袖套账户为 `blocked_unresolved_account`。这是待 Sol `TNM-V23-2R`
  独立经济审核的结果，未调参、未重跑、未读 2025/2026、未写 E: 源目录。
