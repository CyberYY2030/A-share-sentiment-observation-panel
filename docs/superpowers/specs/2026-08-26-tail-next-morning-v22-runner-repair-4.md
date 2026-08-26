# TNM-V2.2-1F4：多 target 共享日期调度修复

状态：`ready_for_terra_code_repair`

基线：`d6d8f21`（代码仍为 `d34673d`）。失败 attempt `v22-canary-3178f827ce5b-fc4e02daa3a3` 永久只读，禁止resume。

## 首次分歧与唯一修复

两个 target 共享历史日期但 survivor code 集不同。旧 runner 按 target 串行加载，首 target 已打开 `20240910`，第二 target 请求不同集合时正确触发 reopen guard。

生产调度改为：

1. 对所有 target 先分别加载 D/D1、完成必要预筛；
2. 建立 `evaluation_union = union(all target survivors)`；
3. 所有会作为任一 target 历史依赖、或可能与另一 target 退出日期重叠的日期，第一次打开时选择性加载完整 `evaluation_union`；每日期仍最多一次；
4. 用同一共享 source cache 分别构建每个 target 的 row/gate/rank，成员与经济结果仍按 target 隔离；
5. 选股完成后沿用现有全局开放事件按日期扫描退出；不得放宽 reopen guard。

checkpoint identity 必须按各 unit 实际消费的选择/退出成员从 union source record 过滤；共享读取不得让一个 unit 声称消费另一 unit 无关成员，实际依赖成员改写仍拒绝resume/fast path。

## 回归与授权

生产runner合成测试必须使用两个 target 的不同 survivor 集，并覆盖：

- 多个重叠历史日；
- 一个日期同时是 target2 历史日和 target1 退出日；
- 每日期只打开一次且请求集合为完整 union；
- 两 target 均完成选择；
- checkpoint identity 彼此隔离；任一实际依赖成员改写拒绝复用。

Terra 白名单仅 V2 module/test及本卡第3节。禁止E盘/真实identity；跑V2/V1/py_compile/diff-check，一个commit后交独立Sol只审本卡。批准后才可另发一个新 canary identity；旧FAILED不重跑。

## 执行证据

### TNM-V2.2-1F4（Terra，2026-08-26）

状态：`tnm_v22_1f4_runner_verified`。修复将 V22 market 选择拆为两个阶段：先对全部
target 的 D/D-1 完成预筛，再按 `evaluation_union` 一次加载任一 target 的共享历史日期；
退出仍沿用既有全局按日期扫描和严格 reopen guard。checkpoint 继续从共享 source record
按各 unit 实际选择/退出成员裁剪；saved checkpoint 验证也先合并成员后每日期只读取一次。

新增生产链合成回归使用两个 target 的不同 survivor 集，证明重叠历史日期及
`target1` 退出/`target2` 历史重叠日期各只打开一次、请求完整 union、两个 target 均完成，
且两个 checkpoint 的成员身份隔离；等成员内容改写后 resume 被
`consumed_input_identity_mismatch` 拒绝。

本地验证：Python 3.11 `tests.test_tail_next_morning_v2` 45 passed / 1 skipped；
`tests.test_tail_next_morning` 25 passed；`py_compile` 通过；`git diff --check` 通过。
未读取 E 盘、未创建真实 canary identity；旧 FAILED attempt
`v22-canary-3178f827ce5b-fc4e02daa3a3` 未改动。经济定义、统计和退出规则未修改；
shared dirty 保持，未 push。Lessons：`skip`。
