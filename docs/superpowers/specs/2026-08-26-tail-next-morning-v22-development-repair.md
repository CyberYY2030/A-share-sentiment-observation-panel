# TNM-V2.2 development runner 修复卡

日期：2026-08-26
基线：`b42a2a7`
审核结论：`changes_required`（P0=0、P1=2、P2=1）

## 1. 冻结边界

不得修改任何经济 gate、score、rank、A6/B3、控制组选法、买卖窗口、动态退出、30bps、九袖套定义或 2023～2024/2025 年界。只修复下列三个审核缺陷。禁止读取 E: 或启动真实批。

## 2. P1：日处理原子边界

把一个交易日的退出更新、当日信号及恢复前沿视为同一个可提交事务。允许先写临时/内容寻址文件，但必须以单个原子 daily commit marker 作为唯一可见完成点；marker 必须绑定 outcome、signal、state 内容哈希。resume 只重放已提交日，未提交的孤儿文件不得被当作经济事实，也不得因同名文件直接失败；孤儿须保留或隔离为审计证据，不能静默覆盖。

增加合成故障注入：在 outcome 落盘后、signal/marker 前中断，原终态为 `CANCELLED`；显式同 identity resume 后必须成功、无重复退出/信号、结果与不中断运行字节级经济恒等。

## 3. P1：resume 每容器单开

resume 启动先汇总每个日期的全部需求：历史 checkpoint/outcome identity 校验所需代码，以及恢复前沿最近 10/11 日的全市场预热。每个日期只调用一次物理加载：若该日需全市场则加载全市场，否则加载代码并集；校验后只保留滚动窗口所需数据，其余释放。禁止 checkpoint 校验、outcome 校验和 warm-cache 分别重开同一容器。

测试必须记录单次 resume 的物理 open count，所有日期均为 1；缓存全市场日仍不超过 11。`source_open_counts` 必须准确反映本次进程，不把历史进程计数相加冒充单开。

## 4. P2：真实 A+B 合并通道统计

在 A、B 之外增加明确命名的 `AB`（或 `combined_channel`）消费者，避免与年份键 `combined` 混淆。对 strategy 和三个 same-N controls 分别报告：overall、2023、2024、monthly、fixed daily-slot summary、month-block bootstrap。AB 日槽等于同一日期 A 与 B 槽位净收益按实际槽权重合并；不得把事件逐笔连乘或把 A/B 样本量差异误作新权重。九袖套账户仍单独报告，不改定义。

测试用 A/B 不同收益和缺额日，独立断言 AB 年度、月度、固定槽、控制组和 bootstrap 键及数值。

## 5. 执行与验收

Terra 只可写 `mining/tail_next_morning_v2.py`、`tests/test_tail_next_morning_v2.py` 和本卡“执行证据”。完成一个本地提交后停止。V2、V1 25 tests、`py_compile`、`git diff --check` 全部通过；shared dirty 保留，不 push。Sol 只读复审给 `approve|changes_required`；approve 前禁止 preflight 与真实 development。

## 6. 执行证据

尚未执行。
