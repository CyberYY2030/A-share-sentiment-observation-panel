# Screening v2.5 R4.3 生产前验收修订卡

本卡是 Terra 的完整 prompt；无需旧会话。先读本卡和 `output/screening-v2-work/PROGRESS.md`。目标：补上 E 盘中 benchmark 的真实生产者，消除日期与证据伪一致，并让最终提交可复现。优先级：用户指令 > 本卡 > R4.2 报告。

## 1. 我替领导拍的板

- 从 `602e25a` 追加独立修订 commit；不 amend/rebase/reset，不执行生产 migration、canary 或历史写入。
- 股票与 benchmark 必须来自同一次有界刷新周期；benchmark 失败时 A–D 可继续，E 明确 unavailable，禁止混用旧指数与新股票。
- required benchmark 使用 `selection_context.BENCHMARK_CODES`，E primary `000852` 必须为有限正数；缓存只保存本周期真实取得的数据。
- 外部 fetcher 可注入测试帧；核心验收必须调用公开 `QuoteSnapshotAdapter.load()`，禁止直接 `_save_success()` 预置 benchmark 冒充生产链。
- 页面三个日期必须各自来自真实对象，发现不一致就显示 `date_mismatch` 并隐藏候选表；禁止用变量赋值制造相等。
- `output/` 证据留在本地，不再提交。把 `602e25a` 新增并跟踪的 `output/screening-v2-work/**` 从索引移除但保留本地；`.gitignore` 已包含 `output/`，不得改用户 dirty hunk。

## 2. 界限

写入白名单：`mining/quote_snapshot.py`、必要时 `mining/intraday.py`、`mining/streamlit_tabs/tab_scanner.py`、`mining/history_bootstrap.py`；`tests/test_r4_screening_regression.py`、`tests/test_mining_ui.py`、`tests/test_history_bootstrap.py`，可新建一个 quote 测试；当前策略文档；本地 `output/screening-v2-work/r4_3-*`、PROGRESS、BLOCKED。允许在新 commit 中停止跟踪 `602e25a` 新增的 output 文件，但不得删除本地证据。本卡只读。

冻结：生产四库、C schema/事务与 A–E scanner/阈值/版本、fixture 标签、`run_daily.py` 用户 diff、`.gitignore`、依赖与测试发现配置。越界先写 `BLOCKED.md`。

## 3. 已知事实、禁止重做与任务 0

- HEAD=`602e25a`；复审实跑定向 `84 OK`，全量 `265 OK (skipped=2)`；四个生产 SHA-256 仍等于 R4.2 基线，生产 `selection_batches`/`pullback_state_history` 均不存在。
- 离线探针调用公开 `adapter.load()` 得到 `snapshot_usable`，但 live/cache `benchmark_closes={}`，cache error=`cached snapshot has no benchmark_closes`。代码入口：`quote_snapshot.py:278`。
- `tab_scanner.py:1699` 令 `effective_trade_date=formal_trade_date`；没有独立日期来源或反例测试。
- 最终代码输出 `c_retriggers`，但 `r4_2-history-first.json` 为旧字段 `c_states`；二者非同一代码生成。代码又将同一 `daily` 同时写为 `fingerprints`；首份 JSON 36,604,851 bytes、约 123 万行，并被提交。
- `do_not_repeat`：不重审 C 状态机、不跑旧全市场策略审计、不改阈值、不重复六态无关页面。

任务 0：记录 status、`602e25a` 祖先关系、生产哈希和用户 diff；确认现有 C 定向测试仍绿。把目标、顺序、最大风险写入 PROGRESS（≤10 行）。

## 4. 执行任务（严格顺序）

1. **补真实 benchmark 生产链。** 为 adapter 增加有界 benchmark fetch/normalize 接缝和结构化 `benchmark_status/provider/errors/observed_at`；默认运行路径实际调用它，并把结果随股票 frame 原子存入 v2 bundle。验证：公开 `load()` 的成功路径产生 `000852` 等有效值，重启恢复数值/as-of；benchmark timeout、缺 primary、非法值时股票仍 usable、E unavailable；provider failure fallback 不联网且不跨日/跨周期拼接。先留“股票成功但 benchmark 为空”的红灯，再绿灯。
2. **修正日期证据。** `selected`取 UI query，`effective`取 `SelectionRuntime` 返回的 `context.trade_date`，`formal`取实际 complete batch trade_date（临时模式取 evaluation context）。三者进入结构化 evidence；不一致时 `date_mismatch`、显示三值且不展示候选。新增正例与故意错日反例，禁止仅断言字符串 caption。
3. **修正 evidence provenance。** history JSON 增加 `evidence_schema_version` 与生成代码 commit/hash；`daily`为唯一明细，`fingerprints`只保留 `{trade_date,input_fingerprint}` 或删除。用最终代码在新 shadow 生成 R4.3 first/reuse，字段一致、二跑五表零增长；报告数字必须由 JSON自动读取。生成物保持 ignored/untracked。将 `602e25a` 新增的 tracked output 以 `git rm --cached` 停止跟踪并确认本地仍存在。
4. **受影响浏览器验收。** 只重放 intraday、date_mismatch、final 三态；PNG 必须显示模块。intraday cache 必须先经公开 adapter `load()` 生成，E 不得为 benchmark missing；错日态必须显式阻断候选；final 日期来自 batch，不得硬编码。保存 PNG/YAML/console 到 `r4_3-*`，不 stage。
5. **验证与提交。** 定向至少覆盖 quote/history/UI/C 回归；全量 `N>=265, OK, skipped=2`；`git diff --check 602e25a..HEAD`；生产哈希不变。commit 只含代码、测试、策略文档及“停止跟踪 output”的删除记录，message：`fix(screening): close R4.3 live evidence gate`。本地交付 `r4_3-report.md`、证据、PROGRESS、BLOCKED。

## 5. 规矩

禁止 skip/todo、放宽断言、mock 核心 adapter/runtime、直接构造成功 cache 作为主验收、伪造截图、写生产库、清理用户文件或修改历史。同一失败最多 3 次/3 分钟；真实 provider 最多 3 次有界探测，失败写 `environment-blocked`，不得宣称生产 ready。

## 6. 完成条件

用户结果：公开实时入口能生产并缓存同周期 benchmark，使 E 可评估；日期不一致会被真实检测；最终代码可重生成同 schema 证据。约束结果：定向/全量全绿、R4.3 history 二跑零增长、tracked `output/` 为空、生产四库哈希零变化、用户 dirty files 保留。只有真实 provider 探测成功或被明确标记 environment-blocked；后者禁止进入生产 migration。
