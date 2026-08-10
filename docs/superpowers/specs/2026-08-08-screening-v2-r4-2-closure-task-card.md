# Screening v2.5 R4.2 生产闭环修订任务卡

本卡是 Terra 的完整交接包；无需读取旧会话。先读本卡及 `PROGRESS.md`，疑点写入 `BLOCKED.md`。目标：让 C 状态机、E 快照缓存、history dry-run 和浏览器证据形成可从空库复现的正式闭环。优先级：用户指令 > 本卡 > 旧卡/报告。

## 1. 我替领导拍的板

- 从 `589af36` 追加一个独立 R4.2 commit，不重做 R0–R4.1，不执行生产 migration。
- C 状态是 formal batch 的组成部分：只允许 `close_final`、当前 `definition_version`、最新 complete batch 产生资格；intraday、failed、partial、旧版本和 legacy 均不得写入或被读取。
- `pullback_state_history` 必须带 `batch_id`、`definition_version`；同 A–E 在同一 `BEGIN IMMEDIATE` 事务写入。保留旧 batch 供审计，读取每个历史日最新 complete batch，禁止独立 `commit()`。
- `再启动`必须由连续正式回放自然产生，核心验收禁止 SQL 手工植入状态。
- 快照缓存同时保存/恢复 `benchmark_closes` 与 as-of；旧缓存缺字段时明确降级，不伪造 E 可用。
- history shadow 必须绑定四库哈希、target、sessions、定义版本；默认新建，只有显式 reuse 且 manifest 完全相等才可复用。
- 生产四库绝对只读；迁移、canary、正式历史写入继续等待用户复审授权。

## 2. 界限

写入白名单：`mining/{candidate_persistence.py,selection_batches.py,watchlist.py,quote_snapshot.py,history_bootstrap.py}`、`run_daily.py`、必要时 `app_panel.py`、`mining/streamlit_tabs/tab_scanner.py`；`tests/{test_candidate_persistence.py,test_watchlist_state_machine.py,test_history_bootstrap.py,test_r4_screening_regression.py,test_selection_runtime.py,test_mining_ui.py}`，可新建一个 R4.2 测试；策略文档；`output/screening-v2-work/{PROGRESS.md,BLOCKED.md,r4_2-*}`。本卡只读。

冻结：四个生产 DB、A–E scanner/阈值/版本、fixture CSV/hash/标签、`requirements.txt`、测试发现配置、用户既有 dirty files。禁止 reset/stash/清理。确需越界先写 `BLOCKED.md`，不得自行扩大范围。

## 3. 已知事实、禁止重做与任务 0

- 基线 HEAD=`589af36`；复审实跑定向 `66/66 OK`、全量 `257/257 OK (skipped=2)`。
- 生产哈希：A股 `E8181DECF433B72ED28BB6849D02E6E06B9C1E23DA72C377FDDA2A4C3250FD63`；ETF `4C2585BBFBA57214C1455FFB5F3402D555BCB8C231DF5B0F081BCD2DA3E259D4`；mining `2C1D71550C27CD28E7F26B7AE5513736964F3AA05A6452D683B5F4EF91F778EB`；概念 `47E65F2F1D3C245FBE6C879C9FFA9E4F103FEDC79693FEAE160DD4D0EFBDB45`。
- 已确认 formal 路径只计算 C 候选，状态写入仅在被排除的 legacy scanner；生产/sandbox/shadow 均无 `pullback_state_history`，60 日 `second_launch=0`。
- 已确认快照缓存未保存 `benchmark_closes`；固定 shadow 存在即复用且无 provenance 校验。
- `do_not_repeat`：不做全仓巡检、不重跑旧版本全市场审计、不访问行情接口、不复制生产数据超过一次、不修改策略定义。

任务 0：记录 `git status --short`、`git diff -- run_daily.py` SHA-256；验证 `589af36` 是 HEAD 祖先；重算四库哈希并只复制一次到 `r4_2-db-sandbox/`。哈希不符只阻断相关验证并记录。将目标、顺序、入口以 10 行内写入 `PROGRESS.md`。

## 4. 执行任务（严格顺序）

1. **关闭 C formal 状态链。** 先写红灯证明连续 formal 运行无法产生状态历史。扩展显式、幂等 migration；让 C evaluation 携带当日状态行，`persist_close_final_batch` 原子写入。读取端只 join 当前版本各日最新 complete batch。覆盖：D1 A 入池、D2 回调、D3 自然 `再启动`；同 fingerprint 零增长；新 fingerprint 新 batch；A–E/C 任一失败全部回滚并保留旧 complete；intraday/failed/旧版本不可见。旧 `persist_pullback_support_states` 改为无独立提交的兼容投影或停用，禁止双写。
2. **修复 E 缓存。** 缓存 schema 保存有限正数的 benchmark map、`trade_date/observed_at`，原子替换；命中时恢复相同值。测试新缓存重启恢复、旧缓存降级、损坏/过期/低覆盖、日期不符；不得因缓存命中联网。
3. **使 dry-run 可相信。** CLI 增加显式 shadow 路径/reuse 控制；manifest 含四库 SHA-256、target、sessions、定义版本。输入不符必须非零退出。JSON 增加 `failed`、`empty_by_strategy`、逐日状态；任一持久化失败仍写报告但进程非零。新 shadow 顺序回放必须自然产生 C 历史；完全相同 manifest 的二跑才允许零增长。四个源库前后哈希相同。
4. **完成浏览器验收。** 用隔离 sandbox 和测试时钟，禁止生产写入/联网。保存 `r4_2-browser-{intraday,pending,final,unavailable,not_run,failed}.{png,yml,console.json}`；PNG 必须实际显示选股模块。证明 selected/effective/formal 日期一致、A–E 状态与 as-of、E 缓存可用、C 自然 `再启动`、强度×阶段筛选、排序；B/D 可合法 empty，但不得用 `activity_missing` 冒充功能验收。另做一次真实 sandbox close smoke，与合成 UI 证据分开表述。
5. **报告与提交。** 更新策略文档、`r4_2-report.md`、PROGRESS、BLOCKED；区分定义正确、数据闭环、真实 smoke、收益未验证。独立提交 `fix(screening): close R4.2 state and evidence gaps`，只含白名单文件。

## 5. 规矩

禁止 `.skip`/todo、放宽或删除断言、mock 核心状态机、SQL 手工造核心验收状态、硬编码命中数、伪造截图、`|| true`、写生产库。每个关键保护先留红灯再恢复绿灯。同一失败最多 3 次或 3 分钟；无新信号即记录阻断并停下相关项。

## 6. 完成条件

用户结果：从空 shadow 经正式逐日路径可观察到 C 状态迁移和 `再启动`；重启缓存后 E 保持同一 benchmark/as-of；六态页面证据可见且日期一致。约束结果：定向命令 `python -m unittest tests.test_candidate_persistence tests.test_watchlist_state_machine tests.test_history_bootstrap tests.test_r4_screening_regression tests.test_selection_runtime tests.test_mining_ui` 全绿；全量 `N>=257, OK, skipped=2`；`git diff --check 589af36..HEAD` 无错；四个生产 DB、outcomes 均零变化。交付代码、测试、migration dry-run、两次 history JSON/manifest、浏览器证据、报告、PROGRESS、BLOCKED 和一个独立 commit；口头说明不算完成。
