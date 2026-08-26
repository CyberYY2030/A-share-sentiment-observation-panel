# TNM-V2.2-1C2：共享日期修复后的唯一新 canary

状态：`ready_for_terra_canary`

批准 runner：`4045da0`；独立 Sol：`approve`。旧 `v22-canary-3178f827ce5b-fc4e02daa3a3` 原子 FAILED 且永久只读，禁止resume。

只授权一个全新 `v22-canary` identity，targets=`20240923,20240926`，输入/输出/900秒上限与前启动卡相同。只读2024所需数据，不读2025/2026，不写E盘，不改runner/tests，不调参。终态必须原子SUCCEEDED/CANCELLED/FAILED，禁止重跑。

Terra 终态只读验收完整候选数、A6/B3、300085/300339、三对照、跨日退出、事件/年度/月度/固定槽统计、九袖套、identity/hash/进程；仅在下节追加证据并docs-only commit，随后交独立Sol。完整开发批保持锁定。

## 执行证据

尚未执行。
