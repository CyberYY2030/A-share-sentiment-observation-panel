# TNM-V2.2-1C：批准 runner 的唯一 canary 启动卡

状态：`ready_for_terra_canary`

批准 runner commit：`d34673d`

独立 Sol 结论：`approve`。V2 44 passed + 1 skipped，V1 25 passed；生产链与最终定点修复通过。

## 唯一授权

- 只运行 `20240923,20240926` 全市场及形成 D 前历史、2024 内跨日退出所需的数据；
- 命令使用 `v22-canary`、新 output identity、默认 `--max-elapsed-seconds 900`；
- 只读 `E:\分钟数据`、`E:\日K线全部至202606`；不得读取2025/2026，不得写E盘；
- 旧 `v22-canary-3178f827ce5b-e4e9b6ccc022` 永久只读，禁止resume；
- 只启动一个新 identity。PID、完整命令、run dir记录后低频读取 progress；只要CPU/frontier/mtime前进就继续，不能用3分钟替代900秒上限；
- 成功必须原子 SUCCEEDED、完整checkpoint/summary/artifacts/hash；超时原子CANCELLED；异常原子FAILED；禁止盲目重跑。

## 验收

Terra 只读验收：两日 `A_eligible/B_eligible`、A6/B3、300085/300339进入A6、三对照、事件退出、统计schema、九袖套、identity/hash、2025/2026未读、无任务进程。不得调参。

tracked 只允许本卡第3节追加执行证据并做 docs-only commit；runner/test不得改。完成后停止交独立 Sol 产物审核；完整开发批保持锁定。

## 执行证据

### TNM-V2.2-1C（Terra，2026-08-26）

状态：`tnm_v22_1c_failed_container_reopen`。只启动一次新 identity：
`v22-canary-3178f827ce5b-fc4e02daa3a3`；任务自有 PID `16280`，使用本卡冻结的
`v22-canary` 命令和默认 900 秒上限。运行目录：
`output/tail-next-morning-v2/v22-canary/v22-canary-3178f827ce5b-fc4e02daa3a3`。

运行在 472.594 秒原子 `FAILED`：`TailDataError: v22_container_reopen_forbidden:20240910`。
已完成来源日期 13 个，`source_frontier=20240925`，但尚无 target checkpoint 或经济产物；因此
两日 eligible count、A6/B3、fixture、三对照、退出、统计和九袖套均未通过验收，不能报告为结果。

身份证据：`spec_hash=3178f827ce5b347fa5eea62605fff9d903cbc24a5eb2848098c3754876e30342`；
`run_hash=fc4e02daa3a3652267c756c02add728d3e5a7852435112cb9891627aaf29d7be`；
`input_hash=b324a415b355986d566c7463d0614efeb46a9405e886f58a90dcdf2efc3ea7b9`；
`run_manifest.json` SHA-256 `b6fb1e76f050c85542b9c75a67874e157d61cf0cfe496d29417e6a6230557b3e`；
`progress.json` SHA-256 `e28526cb541cf12f6865562d033718851992312e0e5894eaf25a80ad4c1d9d53`；
`completion.json` SHA-256 `3fa853ad65ceea0a18519bfaaebbd5a612c66fa8a06c26d7cccb448aadc35e35`。

PID 已退出；保留 stdout/stderr、manifest、progress、completion 和新旧 attempt，未 resume、重跑或调参。
执行只读取 2024 所需来源；未读取 2025/2026，E 盘未写入。等待独立 Sol 对失败归因和后续修复范围审核。
