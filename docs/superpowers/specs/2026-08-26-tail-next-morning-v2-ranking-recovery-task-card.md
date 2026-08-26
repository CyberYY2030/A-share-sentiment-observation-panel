# TNM-V2-R1R2：证据链恢复任务卡

状态：`ready_for_fresh_terra`

## 1. 现场与目标

基线 HEAD 为 `b295fed963755d389629009fe50d141aab1f7f6b`。前一 Terra 留下未提交的 `mining/tail_next_morning_v2.py`、`tests/test_tail_next_morning_v2.py` 修改；不得回退，先审查后接管。

已保留证据：

- `diagnostic-2ad1049c829b-d84af76c7a3d`：原子 FAILED，`consumed_member_evidence_missing:20240905`；
- `diagnostic-2ad1049c829b-548ecfbb7e17`：违规重复启动后由规划会话按 PID 身份停止；永久只读，不得 resume/改写。

唯一目标：修正上述第一处分歧，完成 R1R 的内容校验和逐 gate 计数，生成一个新 identity。禁止改变经济公式、gate、score、排名、Top10、fixture 或 outcome。

## 2. 冻结修复

选择性读取中“请求但不存在或未成功解析的成员”必须进入内容证据，值固定为 `missing:<code>`；它不是运行错误。若以后该成员出现或可解析，sentinel 变为真实 SHA-256，checkpoint 验证必须拒绝旧结果。

必须新增回归：

1. selective source 中请求集合包含缺失成员时，checkpoint 可写、同输入可验证；
2. 缺失成员后来出现时，resume 拒绝；
3. 已存在成员等长改写并恢复 mtime 时，resume 拒绝；
4. 日 K 等长改写并恢复 mtime 时，resume 拒绝；
5. gate counts 的 predicate 与 first-failure 两种口径满足 R1R 卡。

## 3. 执行边界

tracked 白名单仅：

- `mining/tail_next_morning_v2.py`；
- `tests/test_tail_next_morning_v2.py`；
- 本卡第4节执行证据。

先运行 V2、V1、py_compile、diff check。只有全部通过才允许一个新真实 diagnostic identity；不得 resume 旧两个 identity。新结果必须与 `diagnostic-900178221317-bc70a0b260c5` 的四池排名、135条 eligible、两日 `5/6` 与 `104/20`、202条涨停剔除、300085/300339 A第2完全一致，否则原样停止。

一个白名单本地 commit，不 push。完成后只交全新独立 Sol 只读复审。完整开发期、2025、2026继续锁定。

## 4. 执行证据

2026-08-26，`tnm_v2_r1r_ready`：

- 本地验收：`python -m unittest tests.test_tail_next_morning_v2`（33 passed）、`python -m unittest tests.test_tail_next_morning`（25 passed）、`python -m py_compile mining\\tail_next_morning_v2.py tests\\test_tail_next_morning_v2.py`、`git diff --check` 均通过。
- 修复：selective 读取的缺失或解析失败成员记录稳定 `missing:<code>`；成员以后可解析时，checkpoint/resume/SUCCEEDED 快速路径均以 `consumed_input_identity_mismatch:<unit>` 拒绝旧结果。分钟成员与日 K 的等长改写/mtime 恢复、缺失成员复现、gate predicate 与互斥 first-failure 均有回归覆盖。
- 唯一真实运行：`output/tail-next-morning-v2/diagnostic/diagnostic-d5a2f267b379-39d6a370a90a`，`execution_label=tnm_v2_r1_diagnostic_ready`，`run_hash=39d6a370a90a3580487c8a17d0dd84b97712437a73c695acab8ace4be579e809`，`consumed_input_identity=413c1f986cb35483e4d24d2300754caa8d0fe5ffab4ac9762a0e9dbb64ace6fd`。
- 经济不变量与 `diagnostic-900178221317-bc70a0b260c5` 四池 score/rank/Top10 完全一致；eligible=135，候选数为 `20240923 A/B=5/6`、`20240926 A/B=104/20`，涨停触及剔除=202，`300085` 与 `300339` 均为 A 第 2。
- gate counts：20240923 evaluated=185、A/B eligible=5/6、first-failure=`common_quality:2, listing:172, eligible_A:5, eligible_B:6`；20240926 evaluated=1193、A/B eligible=104/20、first-failure=`common_quality:3, listing:1066, eligible_A:104, eligible_B:20`。两日 first-failure total 分别等于 185/1193；predicate 计数可重叠，详情在新 `diagnostic_summary.json`。
- 旧 identity 只读：`diagnostic-2ad1049c829b-d84af76c7a3d` 保持 `FAILED/consumed_member_evidence_missing:20240905`；`diagnostic-2ad1049c829b-548ecfbb7e17` 仅有 initializing 的 manifest/progress，无 completion，未 resume/改写。
- 仅读 2024；`read_2025_2026=false`、`e_drive_written=false`；真实诊断进程已正常退出，无任务自有后台进程或临时文件待清理。
