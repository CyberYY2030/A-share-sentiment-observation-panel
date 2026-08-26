# TNM-V2-R1R3：最终证据语义修复

状态：`ready_for_terra`

基线：`837445b`。本卡是恢复阶段最后一次实现/审核轮次，不改变任何经济规则或结果。

## 1. P1：SUCCEEDED fast path 不得修改成功产物

在写 `progress.json=initializing` 之前先完成 existing SUCCEEDED 检测、checkpoint 内容 identity、completion 与 artifact manifest 全部验证。验证通过直接返回，所有 artifact 字节和 mtime 均不得改变。验证失败 fail closed，不得覆盖旧文件。

回归必须复制一个合成成功 run，记录全部 artifact bytes/hash/mtime，执行 fast path 后逐项完全相同，且 `progress.stage` 不退回 initializing。

## 2. P2：未评估上市资格不得计为上市失败

`listing_age_source=not_evaluated_shape_or_liquidity_failed` 的行：

- `listing_failed` 不计数；
- first failure 必须归入 `both_channel_shape_failed` 或更早的 common/liquidity；
- 只有实际执行上市证据读取且 `listing_pass=false` 才可归入 `listing`。

新增生产 `_final_row` 路径测试，禁止只测试手工构造 row。predicate 与 first-failure 口径继续明确，first-failure 合计守恒。

## 3. 执行与验收

tracked 白名单仅 V2 module、V2 tests、本卡执行证据。先跑 V2/V1/py_compile/diff check；通过后只运行一个新 diagnostic identity。新四池 score/rank/Top10、135 eligible、`5/6`、`104/20`、202涨停剔除、300085/300339 A第2必须与 `diagnostic-d5a2f267b379-39d6a370a90a` 完全一致。

旧 identity 全部只读，不 resume。一个本地白名单 commit，不 push；无任务进程/临时文件。完成后只做一次最终 Sol 定点复验；完整开发期、2025、2026继续锁定。

## 4. 执行证据

2026-08-26，最终证据修复通过：

- 本地验证：`python -m unittest tests.test_tail_next_morning_v2`（34 passed）、`python -m unittest tests.test_tail_next_morning`（25 passed）、`python -m py_compile mining\\tail_next_morning_v2.py tests\\test_tail_next_morning_v2.py`、`git diff --check` 均通过。
- SUCCEEDED fast path：合成成功 run 复制后，先验证 checkpoint 内容 identity、completion、summary 与 artifact manifest；合法复用直接返回，递归记录的全部文件 bytes、SHA-256 与 mtime 完全不变，`progress.json` 不会退回 `initializing`。验证异常保留既有终态，fail closed。
- listing 计数：production `_final_row` 路径证明 shape 失败时不读取上市证据，`listing_age_source=not_evaluated_shape_or_liquidity_failed` 归入 `both_channel_shape_failed`，不计 `listing_failed`；真实上市证据读取后失败才归入 `listing`。
- 唯一真实运行：`output/tail-next-morning-v2/diagnostic/diagnostic-ba6bc1632df7-bce252f213e7`，`execution_label=tnm_v2_r1_diagnostic_ready`，`run_hash=bce252f213e770586c6289596da08d0159fd56cbefa30ffe981507343c6a3b12`，`consumed_input_identity=413c1f986cb35483e4d24d2300754caa8d0fe5ffab4ac9762a0e9dbb64ace6fd`。
- 与 `diagnostic-d5a2f267b379-39d6a370a90a` 经济输出逐字一致：四池 Top10 JSON、eligible CSV、fixture JSON；eligible=135，`20240923 A/B=5/6`，`20240926 A/B=104/20`，涨停触及剔除=202，300085/300339 均为 A 第 2。
- 新 gate counts：20240923 evaluated=185，first-failure=`common_quality:2, both_channel_shape_failed:172, eligible_A:5, eligible_B:6`；20240926 evaluated=1193，first-failure=`common_quality:3, both_channel_shape_failed:1066, eligible_A:104, eligible_B:20`。两日 `listing_failed=0`，first-failure total 均守恒；predicate 计数仍可重叠。
- 仅读 2024，`read_2025_2026=false`、`e_drive_written=false`。真实诊断进程已正常退出；仅观察到既存共享 Python 进程，未停止它们，无任务自有临时文件待清理。
