# Screening v2.6 PROD-RECOVERY-1 生产数据恢复任务卡

- 授权日期：2026-08-23
- 唯一执行会话：`01a02773-844b-7e70-8b0d-ed891c95a49f`
- 生产 checkout：`D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard`
- 冻结分支/起点：`codex/screening-v26@991a1d48cb54065bbc11fee5246a9d1d7c2c3dd3`

执行者把本文当完整且唯一合同。先读本文、项目 `AGENTS.md`、`docs/runbook.md` 的 Market Repair Contract；不得依赖旧会话隐含结论扩大范围。你并非独自在代码库中，不得回退、暂存或改写既有 `output/screening-v2-work/v26/v26-evidence.json` 修改。

## 1. 目标与完成定义

本卡只做一次受控生产恢复：利用 2026-08-18 已保存的有效检查点，按 `08-18 -> 08-19 -> 08-20 -> 08-21` 严格顺序补齐 stock/index 收盘数据；每个日期只有在共享质量门禁为 `clean|usable_with_quarantine`、四个必需指数齐全后，才允许生成同日 `v2.6/close_final/complete` 正式批次。最终在 2026-08-23（周末）启动真实面板时，指标面板与选股模块都必须显示最近完成交易日 `2026-08-21`。

“取得若干行”“子进程 rc=0”“固定股票数量”都不代表成功。可选 concept/ETF 数据允许滞后或空置，不阻断本卡。

## 2. 已核验真值，不得重复 discovery

- `a_share_mvp.db`：stock/index 完整正式日止于 `2026-08-17`；`2026-08-18` 有 4,599 条 stock 与 4 条 index，stock 全部来自 Baostock，写入区间为 `2026-08-19T07:15:57` 至 `08:10:27`，属于未通过门禁的部分检查点；`08-19`～`08-21` 缺失。
- `mining_mvp.db`：最新正式结果为 batch 9，`v2.6/2026-08-17/close_final/complete`；`08-18`～`08-21` 无正式批次。
- 遗留 worker PID `49020` 已不存在；零字节日志为 `output/backfill_jobs/offline_daily_update_2026-08-18_20260819_071543.log`。
- 全局锁 `output/backfill_jobs/offline_daily_update_core_D__BaiduNetdiskDownload_cursor_workflow_adata_sentiment_dashboard.active.json` 当前解析为 `orphaned_manual_intervention`、`last_error=worker_exceeded_budget_without_terminal_status`，会阻止所有新日期启动。
- 本机未发现动作指向 `daily_job.ps1`/`offline_daily_update.py` 的 Windows 计划任务。本卡不安装调度；调度硬化属于下一张卡。
- 当前证据无法证明旧 PID 消失的外部原因；标记为 `unproven`。已有 4,599 条成功 Baostock 行，禁止把根因直接写成“行情接口不可用”。

## 3. 写入边界与禁止项

允许生产写入仅限：

1. 将已确认无主的全局 `active.json` 原子改名归档到本卡证据目录，保留原字节与 SHA-256，禁止删除；
2. 通过仓库既有 `offline_daily_update.py` 单 writer 顺序恢复 `2026-08-18`～`2026-08-21` 的 stock/index 及其同日正式批次；
3. 因上述正式流程产生的 `a_share_mvp.db`、`mining_mvp.db` 合法变化。

允许生成：`output/screening-v2-work/v26/prod-recovery-1/` 下的 online backup、哈希、日志、状态、截图和报告。允许在任务完成后只更新本文的“执行结果”区并做一个 docs-only commit；无源码变更时不要制造空 commit。

禁止：修改任何 `.py`/`.ps1`/测试、provider 映射、阈值、schema、scanner、策略版本、全局配置或 Windows 计划任务；直接运行 `repair_market_day_akshare.py`；并行 updater/provider；`run_daily.py --range`；删库、VACUUM、手工 SQL 修数据、清空 08-18 好行、恢复整库覆盖有效检查点、跳过失败日继续后日、kill 未证明归属的进程、push。

## 4. Task 0：写前门禁与可恢复备份

把本任务视作 HEAVY-BATCH：冻结执行包、独立运行、用日志/状态文件观察，不以模型高频轮询维持任务。

1. 记录 HEAD、`git status --short`、Python 路径、当前时间和证据目录；现有 dirty 文件必须保持未暂存。
2. 枚举与本仓库行情/更新有关的 Python、PowerShell、Streamlit 进程，读取 PID、命令行、父子关系和端口。任何无法排除的真实 writer 存在时，标签 `blocked_no_write` 并停止；不得按进程名批量结束。
3. 对 `a_share_mvp.db`、`mining_mvp.db`、`ths_concept.db`、`etf_mvp.db` 记录 SHA-256、大小、mtime、SQLite `quick_check`、关键表行数与 max date。
4. 用 SQLite online backup 把四库复制到全新 `prod-recovery-1/backups/`；源库和副本均须 `quick_check=ok`，比较逻辑表/行数。online backup 前后再次记录源库 SHA；若备份期间源库被其他进程改写，停止。
5. 只读确认 PID 49020 不存在、锁内 claim/target/log 与已知事实一致、日志仍无终态；复制锁和日志到证据目录，记录 SHA。随后仅把原锁原子改名为带 UTC 时间与 claim_id 的 `.orphaned-*.json` 证据文件。不得直接删除。

任一数据库 `quick_check` 失败时不得自动恢复备份，停止并请求裁决。

## 5. Task 1：逐日单 writer 恢复

必须加载 `web-access` skill 后再进行真实 provider 网络操作。每次只运行一个外层命令，工作目录固定为仓库根目录；使用项目保守参数：一个 worker、请求启动间隔 0.45 秒、每 200 个 accepted rows SQLite checkpoint、总预算 5,400 秒并预留 300 秒给 formal。命令语义为：

```powershell
python offline_daily_update.py --base-dir . --asof YYYY-MM-DD --target-day YYYY-MM-DD --domains stock index --no-health --timeout-sec 5400 --formal-reserve-sec 300 --market-workers 1 --market-min-request-interval-sec 0.45
```

日期顺序固定为 `2026-08-18`、`08-19`、`08-20`、`08-21`。08-18 必须从 persisted unresolved set 恢复，不重抓已 checkpointed complete rows。一次真实外层调用失败后，先读取其结构化 `PROVIDER_SUMMARY`、unresolved set、终态和数据库质量，不得盲目重复同一命令；本卡默认在首个失败日停止，标签 `production_recovery_partial`，保留所有好检查点。

每个日期进入下一个日期前必须记录并断言：

- 共享 `screening_readiness_for_date` 为 `market_data_ready=true`，stock 为 `clean|usable_with_quarantine`，四个必需指数齐全；
- `selection_batches` 恰有可被正式 reader 选择的同日 `v2.6/close_final/complete` 批次；A～E 各 `<=20`，B 按 `sec_code` 无重复，rank 连续；
- v2.5 历史未被改写，既有 batch 9 内容未被改写；
- 生产库 `quick_check=ok`；concept/ETF 未被写入且 SHA 与 Task 0 相同；
- 日志包含结构化终态，不允许用 `rc=0` 代替门禁。

如果 updater 已经在同一外层调用中生成 formal，禁止再额外运行 `formal-only`。只有结构化状态明确为 `market_ready` 且 `formal_failed/not_run`，并证明没有 writer 时，才允许执行一次同日 `python run_daily.py --base-dir . --date YYYY-MM-DD --formal-only`；不得运行 range/history/outcome/watchlist/report。

## 6. Task 2：最终生产与页面验收

完成 08-21 后，以新进程只读核验：

- `a_share_mvp.db` 最新合格 stock/index day=`2026-08-21`；08-18～08-21 每日质量状态和有效/隔离/缺席数量完整；
- `mining_mvp.db` 08-18～08-21 均有 `v2.6/close_final/complete`，reader 最新选择日为 `2026-08-21`；
- 指标表包含至 08-21 的业务值，重复页面渲染不得无条件改写相同 `daily_matrics.updated_at`；
- concept/ETF 前后 SHA 完全一致，四库 `quick_check=ok`。

随后仅启动一个任务自有 Streamlit server，真实浏览器使用最近收盘口径保存 1920px 截图、DOM/页面快照和 console log。必须同时看到：指标日期/coverage=`2026-08-21`；选股 `selected=effective=formal=2026-08-21`、`mode=close_final`、`result_kind=persisted`、`formal_batch_status=complete`；页面无 `unavailable`、`date_mismatch` 或“等待正式批次”。验收结束停止任务 server及其经验证的子进程，清理仅本任务可再生 cache；保留备份、provider/job 日志、状态、截图、报告和哈希。

## 7. 交付与完成标签

输出 `prod-recovery-1-report.md/json`，至少包含：

- 写前门禁、归档锁的原/新路径及 SHA、备份路径/大小/`quick_check`；
- 每日 provider summary、resolved/unresolved、质量比例、四指数、batch_id/fingerprint、A～E 数量；
- 四库前后 SHA/mtime/行数差异和所有执行命令/退出码；
- 页面截图、DOM、console 证据；任务 PID、停止对象、保留对象和空间占用；
- Git 状态，证明既有 dirty 文件未被暂存/改写；
- Lessons：`create` 候选——“页面启动补齐不能替代每日调度；死亡 writer 的锁需要带心跳、终态和安全恢复”。本卡只记录候选，不修改源码或全局记忆。

完成标签只能是：

- `production_recovery_verified`：四日全部通过，页面显示 08-21，证据闭环；
- `production_recovery_partial`：至少一个日期留下有效检查点或正式批次，但未到 08-21；
- `blocked_no_write`：写前门禁失败，生产库和锁均未改；
- `recovery_failed_preserved`：写入已开始但首个失败日未通过，已保留备份、检查点和结构化原因。

禁止因运行时间较长、已有部分行、`rc=0` 或页面能打开而升级标签。完成后停止，等待规划/审核会话复核；不得进入调度硬化。

## 执行结果

状态：`production_recovery_verified`

- 2026-08-23 已按 `08-18 -> 08-19 -> 08-20 -> 08-21` 完成生产恢复；四日 stock 均为 `clean`、`usable_ratio=1.0`、`missing_rows=0`，四指数齐全。
- v2.6 正式批次 10～13 均为 `close_final/complete`；A～E 每项 `<=20`，B 无重复，rank 连续，v2.5/batch 9 未改写。
- 真实面板指标和选股日期均为 2026-08-21；`mode=close_final`、`result_kind=persisted`、`data_status=complete`，重复渲染未改写相同指标的 `updated_at`。
- concept/ETF 保持原 SHA，可选 concept 数据以 `concept_coverage_incomplete` 显式降级；四库最终 `quick_check=ok`。
- 任务自有 updater、repair/formal、Streamlit 8505 和 Playwright 进程均已清理；未触碰其他项目的 8501/8502 服务。
- 完整证据见 `output/screening-v2-work/v26/prod-recovery-1/attempt-2/prod-recovery-1-report.md` 与同名 JSON。
