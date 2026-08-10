# Screening v2.5 R4.1 完成任务卡

本卡是完整交接包；先读 `output/screening-v2-work/PROGRESS.md`，不确定项写入 `BLOCKED.md`，继续不受影响部分。目标是补齐 C 历史覆盖、回算 dry-run 与浏览器证据；优先级：用户指令 > 本卡 > 原 R4 卡/旧报告。

## 1. 我替领导拍的板

- 保留独立提交 `b610757`，以追加 R4.1 commit 修复，不重做 R0–R4。
- `a_history_coverage=N/60` 只统计评估日 **之前**、当前版本最新 complete A batch 覆盖的最近 60 个 usable session；评估日 T 不计入，且必须与 C 资格查询共享同一日期集合。
- 历史回算仍须逐日调用 `build_selection_context → evaluate_formal_capabilities → persist_close_final_batch`，生成六 formal strategy 原子 batch；禁止 A-only complete batch 和 outcomes/legacy/report/watchlist。
- 授权新增 `SCREENING_ACCEPTANCE_NOW_CN` 测试时钟：仅在 `SCREENING_BASE_DIR` 非空时生效；单独设置测试时钟必须 fail closed。隔离验收可优先读取 sandbox 既有 snapshot cache，禁止联网结果混入确定性证据；两变量均未设置时生产行为完全不变。
- 本轮生产四库绝对只读；只允许复制到 sandbox、写 shadow mining DB。生产 migration、canary、历史落库仍需用户另行授权。

## 2. 界限

写入白名单：`app_panel.py`、`run_daily.py`、`mining/watchlist.py`、`mining/streamlit_tabs/tab_scanner.py`、新建 `mining/history_bootstrap.py`、`tests/test_r4_screening_regression.py`、`tests/test_mining_ui.py`、新建 `tests/test_history_bootstrap.py`、`tests/fixtures/screening_v2/**`（CSV/标签冻结）、当前策略文档及 `output/screening-v2-work/{PROGRESS.md,BLOCKED.md,r4_1-report.md,r4_1-*}`。本卡只读。

冻结区：生产 DB、schema/migration、`candidate_persistence.py`、`selection_runtime.py`、registry、A–E 阈值/版本、fixture CSV/hash/标签、`requirements.txt`、测试发现配置及白名单外改动。越界先写 `BLOCKED.md`。

## 3. 已知事实、禁止重做与任务 0

- HEAD=`b610757`；定向 61/61、全量 252/252（2 skipped）已通过。四库完整 SHA-256 见 `r4-baseline.md`，任务 0 必须重算。
- 已确认 `mining/watchlist.py:590` 使用 `<= trade_date`，使 2026-08-06 显示 2/60，真实 prior coverage 为 1/60；旧测试在 `tests/test_r4_screening_regression.py:226-234` 固化了错误值。
- `r4-report.md` 已承认 60 日回算和盘中/待定浏览器未完成；`.playwright-cli/page-2026-08-08T03-40-51-266Z.yml` 出现 T-2 已选但 formal 卡仍报 `snapshot_not_valid_after_trade_date`。
- `r1-audit.md:32-85` 已有同输入漏斗和代表性增删；复核其 fixture/hash 后引用，禁止重复跑旧版本全市场审计。
- `do_not_repeat`：不做全仓巡检，不重跑上述基线作 discovery，不复制生产数据超过一次，不访问真实行情接口，不修复 2026-08-07 残缺行情。

任务 0：记录 `git status --short`；验证 `git merge-base --is-ancestor b610757 HEAD`；四库只复制一次到 `r4_1-db-sandbox/`，记录生产哈希；使用 `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe`。冲突只阻断相关项。

## 4. 执行任务（严格顺序）

1. **修正 C coverage。** 先新增红灯：当前日有 complete A、前一日有 complete A 时，coverage 只能为 1，C 仍只读前一日；再让 coverage 与 `_load_a_qualified_pool` 共享严格 prior dates。验证 partial recall 不隐藏已有 C 候选，0/60、1/60、60/60 均有断言。
2. **实现历史 dry-run。** `run_daily.py` 增加 `--history-bootstrap-dry-run --target-date YYYY-MM-DD --sessions 60 --out FILE`，实现放在 `mining/history_bootstrap.py`。命令从 sandbox 复制 `mining_mvp.db` 为 shadow，按日期升序回算 target 前 60 个 usable sessions；`trend_profile is None` 记 `skipped_insufficient_history`。JSON 必含日期数、processed/skipped/empty、各 strategy 候选数、每日 fingerprint、selection_batches/strategy_runs/candidates 预计增量、A coverage before/after、outcomes before/after。目标 sandbox 与生产哈希不变；同一 shadow 二次执行必须 fingerprint 复用且零增长。
3. **确定性真实浏览器。** 同一 injected now 传到面板、quote adapter、formal view；close 模式取已解析展示日，页面显示 `selected/effective/formal trade_date`。用 sandbox cache 验收 `2026-08-07 10:30` intraday、`15:05` pending、`2026-08-06 18:00` final，另验 unavailable。每态保存 PNG/YAML/console；覆盖 A–E、筛选、四空态、排序、全部 as-of；formal date 等于选择日后才取证。
4. **强化 fixture 与报告。** CSV/hash/标签不变；manifest 区分 `source_metrics/replay_metrics`，写 `metric_context/tolerance`，逐项断言数值/布尔值及首个失败 gate。报告引用并复核 `r1-audit.md` 漏斗/增删，区分定义正确、真实 smoke、收益未验证；同步策略文档、PROGRESS、BLOCKED。
5. **验证并独立提交。** 使用明确路径的 Python；定向测试必须 `OK`，全量 `Ran N>=252 ... OK (skipped=2)`，`git diff --check b610757..HEAD` 无 R4.1 新错误。重算四个生产哈希必须等于任务 0；提交仅含白名单，建议 message：`fix(screening): complete R4.1 acceptance gaps`。

## 5. 规矩

禁止 `.skip`、放宽/删除断言、mock 被测核心逻辑、硬编码命中数、移动阈值、伪造截图、`|| true`、写生产库或清理用户 dirty files。关键保护须先制造一次红灯再恢复绿灯。同一失败最多 3 次或 3 分钟；换策略仍无新信号则写 `BLOCKED.md` 并停止相关项。

## 6. 完成条件

用户结果：C coverage 口径真实、60 日 dry-run 可复跑、四态浏览器证据可复核且 selected/effective/formal 日期一致。约束结果：全量测试不退化、shadow 二跑零增长、outcomes 与四个生产 DB 哈希零变化。交付 `r4_1-report.md`、浏览器证据、dry-run JSON、PROGRESS、BLOCKED（无阻断写“无”）及一个独立 commit；口头说明不算完成。
