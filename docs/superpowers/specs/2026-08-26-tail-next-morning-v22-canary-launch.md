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

尚未执行。
