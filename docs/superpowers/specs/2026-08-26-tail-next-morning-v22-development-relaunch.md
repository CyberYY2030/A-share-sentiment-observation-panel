# TNM-V2.2 development 新 identity 重启卡

日期：2026-08-26
批准 runner：`54dcc08`
独立审核：`approve`，P0/P1/P2=0

旧 FAILED identity `v22-development-8fffe4a3fba4-969e9d05cd0a` 永久只读，禁止 resume、删除或改写。只批准一次全新 identity detached `v22-development`，参数沿用 4a803a0 preflight；禁止2025/26、禁止写E盘、禁止调参。启动后记录PID、命令、run id/hash、日志、run.lock/progress；若再次失败，保留终态并停止，禁止第三次盲启。

## 执行证据

### TNM-V2.2 development 唯一新 identity detached launch（Terra，2026-08-26）

状态：`tnm_v22_development_relaunch_started`。启动前 HEAD 为
`e3fa688c7bed00eb942f9fe632314bccb6fe2cae`，runner/test blobs 分别为
`f758c9b92d0f00546c6e1b28e08b193b4534576d`、
`936ba5efc67ea5108b80b78a41dffb4034df2b2d`；暂存区为空、无 development writer/lock，旧
`v22-development-8fffe4a3fba4-969e9d05cd0a` 只读保留且未 resume。

唯一任务 PID `73692` 于 `2026-08-26T05:54:02.0995269Z` 以隐藏 `Start-Process -PassThru` 启动：

```text
C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m mining.tail_next_morning_v2 v22-development --minute-root "E:\分钟数据" --daily-root "E:\日K线全部至202606" --output-dir "output\tail-next-morning-v2\development"
```

新 identity 为 `v22-development-8fffe4a3fba4-7afabfa01de2`；run hash 为
`7afabfa01de259fffb6db850e212b67d98211ed9c244c82372497a30ea522c81`，spec hash 为
`8fffe4a3fba47496d68c49269fca70bc83da3633eca89bc5ff06b54899365a45`，input manifest hash 为
`45aaeaed69e27f44d53989085cee2893da64160d970e920b7416679cb7ff4a57`。stdout/stderr 为
`output/tail-next-morning-v2/development/v22-development-relaunch-20260826T055402Z.stdout.log`
及同名 `.stderr.log`。

启动期核验：`run_manifest.json`、`run.lock`、`progress.json` 已存在；progress 已推进为
`source:20230103`，更新时间 `2026-08-26T13:56:32.151963+08:00`，completed units=0，stderr=0，
尚无 completion。首个 2023 历史容器已跨过旧 `v2_canary_year_guard:20230103` 失败点。进程分类
`keep`；本轮不等待批完成、不轮询大产物、不 resume/重跑。未读取 2025/2026，未写 E 盘，shared dirty
保持，未 push。后续仅由获授权的低频监控/终态验收处理。Lessons：`skip`。
