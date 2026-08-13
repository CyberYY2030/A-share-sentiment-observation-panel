# LESSONS — adata_sentiment_dashboard project

<!-- lessons-schema: lessons-ledger/1 -->
<!-- lessons-scope: project -->
<!-- lessons-project: adata-sentiment-dashboard -->

本文件只保存本项目成立且可复用的 `·项目` 教训，随仓库版本化。

## 活跃区

- **[[lesson:ADATA-1]] [checklist·项目] 全市场收盘数据修复的“完成”必须由共享行级质量与日级 postcondition 决定，不能由已有行、非空响应、`rc=0` 或固定数量阈值决定。** 触发：修改 stock/index provider、retry selection、repair success 或 bad-session revalidation。根因与代价：Tencent stock fallback 曾固定写入 `amount=None`，595 行全部为 `missing_required_field`；旧重试又把所有已有行视为完成，无法替换无效行；子进程以 `stock_count >= 2000` 返回成功而上层质量门失败，造成反复伪成功。方法：只把 `valid_trade`、`confirmed_halt`、`provider_halt_placeholder` 视为完整；invalid/missing 保持 retryable；Tencent stock 缺 `amount` 在持久化前拒绝（index 合同另算）；stock 达 `clean|usable_with_quarantine` 且四指数齐全后才运行单日 `--formal-only`。验收必须同时断言 child 与 parent 使用同一 postcondition。sink → `mining/data_quality.py`、`offline_daily_update.py`、`repair_market_day_akshare.py`、`tests/test_backfill_rules.py`。
- **[[lesson:ADATA-2]] [checklist·项目] 长序列 provider 修复必须采用“单请求可杀死进程 + 单代码超时隔离 + 连续错误熔断 + 持久化 checkpoint + 精确 unresolved 续跑”，不能靠 daemon thread、盲目降频或整批重跑。** 触发：任何 1,000+ 股票日线抓取、provider timeout/circuit-breaker 或生产补数。根因与代价：不可杀死的 daemon-thread/semaphore 让慢请求长期占槽；改造初版又让一个 12 秒 symbol timeout 提前关闭整源；20/20 探针无法覆盖 5,000-code 长序列的稀有超时；缺少 checkpoint 会丢掉此前成功行。方法：一个 outer updater、一个 stock worker、请求起始间隔 0.45 秒；BaoStock → Sina → Eastmoney 探针/全量；active code timeout 时 flush 已接受行、杀 worker、仅该 code 留给 fallback，后续 code 由同源新 worker 继续；成功行重置连续错误计数，第三次连续调用错误才切源；parent 每 200 条写 SQLite checkpoint，下一源/下一 invocation 只消费真实 unresolved；保存 `PROVIDER_SUMMARY` 的 `codes_in/timeout_codes/worker_restarts/unresolved/stop_reason/max_consecutive_errors`。出现失败时先读 summary 与 checkpoint，禁止立即重复 invocation。sink → `repair_market_day_akshare.py`、`offline_daily_update.py`、`docs/runbook.md`、`tests/test_backfill_rules.py`。
- **[[lesson:ADATA-3]] [checklist·项目] Agent 创建的后台进程、浏览器 daemon、代理、本地 server 和临时数据副本必须有生命周期所有权，交付前执行 `keep|cleanup` 裁决。** 触发：任何 `Start-Process`、Streamlit/browser/CDP/Playwright 启动、临时 SQLite online backup 或工具缓存生成。代价：2026-08-13 清点发现，已完成验收遗留两个 Playwright daemon 及 headless Chrome、一个 CDP proxy 和一个固定 08-11 的 Streamlit，共占约 2.16GB 工作集；隔离数据库副本与 `.playwright-cli` console 缓存另占约 709MB。方法：启动时记录 PID、父进程、端口、命令行和临时路径；结束时验证 ownership 后停止精确进程树，禁止按 `node.exe`/`python.exe` 名称批量杀；进程退出后清理专用 temp profile 和可再生缓存；生产 DB/job log/report/hash/screenshot/YAML 属审计证据，单独列路径与大小，未经明确裁决不删；交付报告写明释放量、保留项与原因。sink → `AGENTS.md` Agent Task Cleanup Contract、`CLAUDE.md` Long-Running Guardrails、`docs/runbook.md` Agent task cleanup。

## 归档区
