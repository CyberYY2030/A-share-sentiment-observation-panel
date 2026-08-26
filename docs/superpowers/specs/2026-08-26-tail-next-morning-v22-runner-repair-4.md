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

尚未执行。经济定义、统计、退出及其他已通过代码禁止改变。shared dirty保持，不push。Lessons：`skip`。
