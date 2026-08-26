# TNM-V2.2 development 新 identity 重启卡

日期：2026-08-26
批准 runner：`54dcc08`
独立审核：`approve`，P0/P1/P2=0

旧 FAILED identity `v22-development-8fffe4a3fba4-969e9d05cd0a` 永久只读，禁止 resume、删除或改写。只批准一次全新 identity detached `v22-development`，参数沿用 4a803a0 preflight；禁止2025/26、禁止写E盘、禁止调参。启动后记录PID、命令、run id/hash、日志、run.lock/progress；若再次失败，保留终态并停止，禁止第三次盲启。

## 执行证据

尚未执行。
