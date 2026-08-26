# TNM-V2.2 development 2023 年界修复卡

日期：2026-08-26
失败 identity：`v22-development-8fffe4a3fba4-969e9d05cd0a`
失败终态：`FAILED`，`v2_canary_year_guard:20230103`

## 1. 首个偏差

development 的 calendar 与 input manifest 已正确冻结 2023/2024，但共享 `load_day_v2_statistics` 仍硬编码只接受 2024。首次加载 2023 历史日即失败；0 checkpoint、0 经济产物。旧 identity 与日志永久只读，禁止 resume。

## 2. 最窄修复

只把共享分钟统计加载器的允许年界改为 2023 或 2024；2022、2025、2026 继续拒绝。canary 自身仍由 `_v22_calendar`、`_v22_input_manifest` 限定只读 2024，development 仍由自己的 calendar/input manifest 限定 2023/2024，因此不得移除上层 mode guard，也不得改变经济函数。

新增生产链回归：真实 loader 路径接受合成 2023 容器，接受 2024，拒绝 2022/2025/2026；`v22-canary` 的 input guard 继续拒绝 2023；`v22-development` 合成流能从 2023 历史日推进到首个 target。不得只 patch loader 绕过测试。

## 3. 执行门

Terra 只可写 `mining/tail_next_morning_v2.py`、`tests/test_tail_next_morning_v2.py` 和本卡执行证据。禁止 E:、真实 runner 和 2025/26；V2、V1 25 tests、`py_compile`、`git diff --check` 通过后一个本地提交并停止。Sol 独立只读 `approve` 前禁止新 launch。

## 4. 执行证据

### TNM-V2.2 development 2023 年界最窄修复（Terra，2026-08-26）

状态：`tnm_v22_development_year_guard_fix_verified`。共享 `load_day_v2_statistics` 仅把允许年份从
硬编码 `2024` 扩展为 `{2023, 2024}`；2022、2025、2026 以
`v2_supported_year_guard` fail-closed。`_v22_input_manifest` 的 canary 2024-only 守卫和
`_v22_development_input_manifest` 的 development 2023/2024 守卫均未改变；经济函数、gate、score、
rank、A6/B3、controls、退出、成本和账户均未修改。

新增临时真实 loader 回归：2023/2024 容器均可解析，2022/2025/2026 均拒绝；canary input guard
继续拒绝 2023；development 由 2023 的 D-10 历史日推进至首个 `20230117` target。Python 3.11
本地验证：`tests.test_tail_next_morning_v2` 51 passed / 1 skipped；
`tests.test_tail_next_morning` 25 passed；`py_compile` 与 `git diff --check` 通过。

全程未读取 E 盘、未启动真实 runner、未读取 2025/2026、无任务进程。失败 identity
`v22-development-8fffe4a3fba4-969e9d05cd0a` 及其 manifest/progress/completion/log 只读保留，
未 resume、未改写；shared dirty 保持，未 push。等待 Sol 独立审核，approve 前禁止新 launch。
Lessons：`create` 候选已在失败证据中登记，未超出本卡写入范围。
