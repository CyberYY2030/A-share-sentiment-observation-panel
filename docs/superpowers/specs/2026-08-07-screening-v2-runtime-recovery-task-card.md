# Screening v2.5 运行恢复与续跑任务卡（RR0 → R2+ → R3 → R4）

本卡是 Terra 的完整交接入口。新会话先读 `output/screening-v2-work/PROGRESS.md`，再执行本卡；基础合同仍是 `docs/superpowers/specs/2026-08-06-screening-v2-task-cards.md` 的 R2–R4。本卡只在 RR0 和 R2+ 增补处优先于基础合同。拿不准的写入 `BLOCKED.md`，继续不受影响部分。目标是恢复可诊断、可容灾的盘中选股，并完成 R2→R4，不用昨日榜冒充今日结果。

## 我替领导拍的板

- 所有实时源失败时继续 `data_unavailable`，禁止回退昨日收盘/旧榜；界面必须显示失败源、错误类、观测时间和重试状态。
- 允许使用同交易日最近一次成功快照作重启容灾：年龄 `<=10min`、股票覆盖率 `>=0.80`、明确标记 `snapshot_cache_fallback`；跨日、已有 `close_final` 或超龄立即失效。
- 不预设第三数据源。任务 0 只探测已安装来源；新增依赖、密钥或付费源先阻断请示。外部源全挂不阻断离线实现/测试，但不得标记“真实盘中恢复完成”。
- 修复顺序冻结为 RR0 → R2+ → R3 → R4；每卡一 commit，不改 R1 阈值和候选定义。

## 界限

- RR0 写入：`offline_daily_update.py`、`tests/test_data_quality.py`、`tests/test_backfill_rules.py`。
- R2+ 写入：基础任务书 R2 白名单，另加 `mining/intraday.py`、可新增 `mining/quote_snapshot.py`、`tests/test_intraday.py`、可新增 `tests/test_quote_snapshot.py`。
- R3/R4 严格使用基础任务书各自白名单；其余只读。生产 `*.db`、冻结五股 fixture、R0/R1 实现、基础任务书、非本卡测试均冻结。
- `offline_daily_update.py` 有用户既有 302+/2- 行，当前文件 SHA-256 `090D6DF7665DEDB4069FD89B4427995234BBA2E5E38167CC81AFE05A406B2DC7`，binary diff SHA-256 `2604167834137382F49178F8371EB4B9C03E8831ADAD19F8354AD986A30EE862`；开工重新保存 diff/hash，禁止 stash、回滚或覆盖，只定向暂存本卡 hunks。

## 已知事实、禁止重做与任务 0

- 基础任务书 SHA-256 `A3D61BE3AD923FE6ED3193EC223BC00B650F55BD633EE07A5F76E14C540CE2DB`。2026-08-07 已验：`2026-08-06` 日线 5,203 行、5,198 只有效成交，质量 `clean`；挖掘库 107 候选；R1 为 `Ran 220 tests ... OK (skipped=2)`、commit `eb956f7`。
- 实测 `stock_zh_a_spot` 返回 HTML 导致 `JSONDecodeError`，`stock_zh_a_spot_em` 为 `RemoteDisconnected`；`tab_scanner.py:168-185` 吞异常后返回空表。`offline_daily_update.py:146-147` 错误接受 `quality_unavailable`、拒绝 `usable_with_quarantine`，曾把 6 个可用日误报缺失。`tab_scanner.py:438/452` 的 `v2.0` 留给 R3 版本统一。
- `do_not_repeat`：不重新全仓巡检、不重跑 R0/R1 调研/五股审计、不重复无界实时请求、不做历史回填、不改生产库。
- 任务 0：从 `screening-python.txt` 设置 `$screeningPy`；核对 git 状态、上述三个事实和基线 hash。每个实时 endpoint 最多一次、每次 `<=8s`；记录 `provider/status/error/raw_rows/normalized_rows/coverage/observed_at`。只有证据漂移影响合同才阻断；把目标、顺序、最大风险、首入口压缩至 10 行内追加到 `PROGRESS.md`。

## RR0 — 修正数据质量消费者（先止住错误修复请求）

`stock_rows >= min` 且状态属于 `{clean, usable_with_quarantine}` 才算股票域可用；`partial/known_bad_session/quality_unavailable` 全部 fail-closed。增加状态矩阵测试和合成 10 日计划测试，证明 quarantine 日不进 missing，坏日仍进入。先写会失败的红灯测试，再修实现。

验收：

```powershell
& $screeningPy -m unittest tests.test_data_quality tests.test_backfill_rules
& $screeningPy -m unittest
```

不得用真实 DB 跑写入/修复命令，不得触发通知。提交 RR0 后在 `PROGRESS.md` 记录 commit、测试数和既有 diff 保存证明。

## R2+ — 执行基础 R2，并补齐行情容灾与可观测性

1. 先完成基础任务书 R2 全部合同。抽取统一 quote adapter，复用 `mining/intraday.py` 的进程隔离/超时；Streamlit 不再自建第二套 AkShare 循环。
2. 返回结构化结果：`frame, provider, observed_at, raw_rows, normalized_rows, coverage, attempts, errors, status, from_cache`。状态至少区分 `provider_failed/empty_payload/normalization_empty/coverage_below_threshold/snapshot_stale/snapshot_usable/snapshot_cache_fallback`；只有 usable/fresh cache 才设置 `snapshot_source`。
3. 单次 UI 运行每源最多 1 次、每次 5–8 秒；进程内失败冷却 `>=60s`，同 fingerprint rerun 不重抓。持久缓存只保存成功归一化快照，原子写入任务 sandbox/cache，按本卡 10min/0.80/同日规则读取；不得写候选历史。
4. UI 展示 provider、错误摘要、`observed_at`、行数、coverage、缓存年龄/下次可重试时间；外层异常写本地日志，界面不得只剩裸 `data_unavailable`。错误文本不能被下游解析为状态。
5. 新测试覆盖：首源失败次源成功、全失败、空 payload、归一化为空、低覆盖、超时、冷却、同日重启缓存、超龄/跨日/close-final 禁复活；故意让 provider 抛错贴红灯，再恢复全绿。

验收除基础 R2 命令外再跑：

```powershell
& $screeningPy -m unittest tests.test_intraday tests.test_quote_snapshot tests.test_selection_runtime tests.test_mining_ui
& $screeningPy -m unittest
```

随后原样执行基础任务书 R3、R4；R3 必须消灭正式能力读取路径的散落 `v2.0`，R4 浏览器只连四库 sandbox。生产 migration、历史回算仍等用户另行授权。

## 规矩与完成条件

禁止 skip/todo、删测试、放宽断言、mock 被测 adapter、调阈值、安装依赖、写生产 DB、`|| true`。落实匹配 lessons：L-1（改状态/版本须搜所有消费者）与 L-20（外部数据和运行拓扑先实证）。同一路径失败 3 次或 10 分钟即换策略；无可信替代则记录阻断。

完成必须同时满足：① RR0 不再误报 quarantine 日，后台不会因该 bug 重启修复；② 合成及冻结快照下 R2–R4 全绿且全量测试不少于 220；③ 真实交易时段至少一个源达到 coverage `>=0.80` 并进入 `snapshot_usable`，否则交付标 `environment-blocked` 而非完成；④ 生产 DB 开始/结束 SHA-256 相同。交付 `PROGRESS.md`、`BLOCKED.md`（无则写“无”）、各卡 commit、红/绿证据、实时源矩阵、浏览器三态截图和 R3 migration dry-run；口头完成无效。
