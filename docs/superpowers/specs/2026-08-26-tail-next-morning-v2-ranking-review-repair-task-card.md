# TNM-V2-R1R：独立审核最小修复卡

状态：`ready_for_terra`

规划基线：`752990ae7606872ecbd82a4b8653c62b31830d1b`

## 1. 唯一目标

关闭 TNM-V2-R1 独立审核的一个 P1 和一个 P2，不改变任何经济公式、gate、阈值、score、排序、Top10、tie、fixture 或 outcome 定义。修复后只重跑同一两日全市场加三个单股 fixture 的新 identity；完整 2023—2024、2025、2026 继续锁定。

## 2. P1：checkpoint/resume 必须绑定实际消费内容

现状：初始 `run_hash` 只绑定路径、size、mtime；实际分钟成员和日 K 摘要在运行结束才写入。旧 checkpoint 只核对 `run_hash`，存在同路径等长改写且目录 mtime 恢复后复用陈旧 checkpoint 的路径。

最小修复合同：

1. 每个 checkpoint 必须保存该单元实际消费的分钟成员 SHA-256、日 K 工作簿 SHA-256 及其规范化 `unit_consumed_input_identity`；
2. resume 或 SUCCEEDED fast path 在接受任何 checkpoint/终态前，必须重新计算该单元实际依赖内容的 identity；
3. 任一成员或工作簿内容不一致即 fail closed，错误固定为 `consumed_input_identity_mismatch:<unit>`，不得复用旧 checkpoint，也不得把新摘要写进旧结果；
4. 内容一致时才允许 resume；路径、size、mtime 仍可作快速定位，不能替代内容校验；
5. 新增集成测试：对已完成 checkpoint 的分钟成员和日 K 分别做等长改写并恢复文件/目录 mtime，断言 resume 拒绝；未变内容时 resume 继续通过；
6. 运行结束的汇总 `consumed_input_identity` 必须由各 checkpoint 的已验证 identity 规范合成，不能从未参与 checkpoint 验证的旁路重新声明。

## 3. P2：逐 gate 淘汰计数

每个全市场日的 checkpoint 和最终 summary 必须保存：

- A 每个硬 gate 的 `predicate_fail_counts`；
- B 每个硬 gate 的 `predicate_fail_counts`；
- common quality、D−1 liquidity、listing、A/B overlap 排除计数；
- `tail_limit_touch` 按通道进入前的共同剔除计数；
- universe、preselection survivor、A eligible、B eligible、Top10 数量。

计数定义为“逐谓词失败次数”，同一股票可失败多个谓词，因此各项之和不要求等于 universe；另保留互斥的 `first_failure_counts`，其合计加 eligible 必须等于进入最终 gate 评估的股票数。任务卡与产物必须明确两种口径。

计数必须在本次计算期间聚合并进入 checkpoint，不能依靠事后扫描仅含合格股票的 artifact 伪造。

## 4. 不变量与真实重跑

- 新规则输出的四个池 score、rank、Top10 必须与已审核 identity `diagnostic-900178221317-bc70a0b260c5` 完全一致；若输入内容 identity 已变化，停止并报告，不声称排名相同；
- 300085、300339 仍为各自 A 第2；旧六只涨停触及股票仍剔除；
- 完整合格池仍为 135 条；两日 A/B 池仍为 `5/6` 与 `104/20`；涨停触及剔除仍为 202；
- 若任何经济结果改变，标记 `diagnostic_changes_required` 并停止，不调参、不重跑第二次；
- 新运行仍只读 2024，不写 E:，新 identity 写 `output/tail-next-morning-v2/diagnostic/`；旧成功产物永久只读保留。

## 5. 白名单、验收与提交

Terra tracked 白名单：

- `mining/tail_next_morning_v2.py`；
- `tests/test_tail_next_morning_v2.py`；
- 本卡只追加执行证据。

测试至少覆盖第2—3节，并运行：

```powershell
python -m unittest tests.test_tail_next_morning_v2
python -m unittest tests.test_tail_next_morning
python -m py_compile mining\tail_next_morning_v2.py tests\test_tail_next_morning_v2.py
git diff --check
```

只允许一个新真实诊断 identity。Terra 创建一个白名单本地 commit，不 push，清理任务自有进程后停止。完成标签只能为 `tnm_v2_r1r_ready|tnm_v2_r1r_changes_required|tnm_v2_r1r_blocked`。随后只交独立 Sol 复审；approve 前不得正式交付排名或解锁完整开发期。

Lessons：`skip`，这是现有项目输入冻结合同的定点补全。

## 6. 执行证据

尚未执行。Terra 只能在本节追加紧凑证据。
