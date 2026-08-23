# Screening v2.6 PROD-1.1 最小收口任务卡

执行者把本文当完整交接包；先读 `output/screening-v2-work/v26/prod-1-1/PROGRESS.md`，拿不准写 `BLOCKED.md` 并继续不受影响部分。目标只有两个可见结果：相同指标不再改写 `daily_matrics.updated_at`；隔离真实浏览器同时正确显示 2026-08-17 指标面板与 v2.6 正式选股。

## 我替领导拍的板

- 采用奥卡姆剃刀：只修共享 UPSERT 的幂等性，不新增缓存、repository、只读框架或第二套面板。
- PROD-1 的 60 行仅时间戳变化不恢复；业务值未损坏，整库恢复风险更高。
- 本卡不补 08-18～08-21 行情，不运行 provider、migration 或 formal-only；生产四库全程只读。
- 浏览器验收只连生产库的 SQLite online backup 副本；完成后停止任务 server，但保留截图供领导查看。

## 界限

唯一写入所有者为执行会话 `01a02773-844b-7e70-8b0d-ed891c95a49f`。允许修改：`app_panel.py`、`tests/test_mining_ui.py`、本文；允许生成 `output/screening-v2-work/v26/prod-1-1/` 证据。其余只读。冻结：所有 scanner、阈值、v2.6 batch 9、schema、数据库、依赖、`run_daily.py`、`offline_daily_update.py`、验收口径。保留且不得暂存现有 `output/screening-v2-work/v26/v26-evidence.json` 修改。禁止 push、全局配置和顺手重构。

## 已知事实、禁止重做与任务 0

- 当前 `codex/screening-v26@05a7711`；accepted code=`b2f9105`；全量基线 382 OK、原 2 skip。
- PROD-1 已独立确认 batch 9=`v2.6/2026-08-17/close_final/complete`，A/B/C/D/E=`20/20/0/20/0`，复用零增长，v2.5 8 batches 未变。
- 首个偏差：`app_panel.py:915-927` 每次生成新 `updated_at`，再 `INSERT OR REPLACE` 60 行；唯一调用在 `:5059`。隔离模式在 `:5043` 已跳过生产写入。不要重审策略、provider、日期缺口或旧回放。
- 任务 0：确认 HEAD/dirty 边界、Python311 可运行依赖、四个生产 DB 路径/SHA/`quick_check`、无真实 writer。任一生产 DB 在本卡变化即停止。将目标、首入口、风险压缩到 PROGRESS ≤10 行。

## 任务 1：一个共享幂等 UPSERT

先在临时文件 SQLite 增加两条红灯测试并证明旧代码失败：①相同 DataFrame 二次 upsert 返回 0、原 `updated_at` 和 DB SHA 均不变；②只改一行业务值时仅该行及其 `updated_at` 改变，另一行不变。

随后只改 `upsert_daily_matrics()`：用 SQLite 原生 `INSERT ... ON CONFLICT(trade_date) DO UPDATE ... WHERE`，以现有动态业务列的 null-safe `IS NOT` 判定真实变化；`updated_at` 只在新行或业务列变化时写入。无业务列时 `DO NOTHING`；返回实际插入/更新行数。不得先整表读取、比较 DataFrame、增加 helper 层或依赖。搜索唯一调用者并确认返回值变化无消费者影响。

## 任务 2：真实副本浏览器闭环

用 SQLite online backup 把四个当前生产 DB 放入全新 `prod-1-1/browser-sandbox/`，记录源库前后 SHA 和副本 `quick_check`。设置 `SCREENING_BASE_DIR=<sandbox>`、`SCREENING_ACCEPTANCE_NOW_CN=2026-08-17T18:00:00+08:00`，隐藏启动单一 Streamlit server；禁止联网、后台修复和生产连接。

真实浏览器保存页面快照、console log 和两张 1920px 截图：
1. `metrics-panel-v26.png`：可见“隔离验收模式”、指标日期/coverage=`2026-08-17`、情绪总分等核心指标，页面无 unavailable/error；
2. `scanner-v26.png`：可见筛选证据 `selected=effective=formal=2026-08-17`、`mode=close_final`、`result_kind=persisted`、`formal_batch_status=complete`，正式 A–E 状态和候选表；DB/DOM 共同断言 A/B/C/D/E=`20/20/0/20/0`、B 无重码。

console error=0、无 `date_mismatch`/“等待正式批次”。浏览器前后 sandbox 四库 SHA 必须相同；生产四库 SHA 必须从任务 0 到结束全部相同。记录 PID/端口并仅停止任务 server及验证子进程，清理可再生 cache，保留报告、截图、日志和恢复副本大小。

## 规矩与完成条件

禁止 skip/todo、删/放宽断言、mock `upsert_daily_matrics`、改 scanner/fixture/阈值、`|| true`。同一失败 3 次换策略；浏览器最多 2 次受控启动，仍失败则 `environment_blocked`，不得假图。只提交一个可逆 commit，白名单外为 0。

明卷：Python311 执行新增两测；`python -m unittest tests.test_mining_ui tests.test_r4_screening_regression`；`python -m unittest` 必须 ≥384 OK、仅原 2 skip；`python -m py_compile app_panel.py`；`git diff --check`、`git diff --cached --check`。完成标签仅 `prod_1_1_verified|changes_incomplete|environment_blocked`。`prod_1_1_verified` 必须同时有“相同输入 DB SHA 不变”和上述两张正确页面截图；输出 `prod-1-1-report.md/json`、commit、测试、生产/副本 hashes、PID 清理与 Lessons。
