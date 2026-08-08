# Screening v2.5 R4.2 closure report

## 结论

R4.2 的可执行合同已闭环：C formal 状态、E v2 cache、可验证 history shadow、六态 UI 和真实 sandbox close smoke 均有独立证据。生产四库和 `outcomes` 零变化；没有执行生产 migration、生产写入或行情联网。

本报告区分四层结论：

1. **定义正确**：测试覆盖状态可见性、事务回滚、cache fail-closed、manifest/reuse、UI 日期与过滤排序。
2. **数据闭环**：空 shadow 的连续 formal replay 自然产生 C 状态和 `再启动`，相同 manifest 二跑零增长。
3. **真实 smoke**：生产数据的一次性副本完成 2026-08-06 close-final batch；真实 B/D 活跃度输入缺口如实保留。
4. **收益未验证**：本卡没有授权或要求收益显著性、实盘有效性或生产 canary，不能由本轮结果推断。

## C formal 状态链

- migration 显式、幂等地创建/升级 `pullback_state_history`，新增 `batch_id`、`definition_version`；legacy 行保留但因两字段为空而不具正式资格。
- C evaluation 产出当日状态行；`persist_close_final_batch()` 在同一 `BEGIN IMMEDIATE` 中写 selection batch、A–E runs/candidates 与 C states。
- candidate 与 state 必须匹配；A–E 或 C state 任一持久化失败时全部回滚，并保留旧 complete batch。
- 读取只 join 当前 `v2.5` 各交易日最新 `close_final/complete` batch；intraday、failed、旧版本和 legacy 状态不可见。
- 同 fingerprint 零增长；新 fingerprint 新建 batch。旧 `persist_pullback_support_states()` 已停用为无写入兼容入口，避免双写和独立 commit。

## E v2 cache

- `snapshot_v2.pkl` 是单文件原子 bundle，包含 frame、`trade_date`、`observed_at`、coverage、provider 和有限正数 `benchmark_closes`。
- 重启命中恢复同一 benchmark/as-of；cache-only 验收没有 provider 调用。
- 旧双文件 cache 缺 benchmark 时明确返回 `legacy cache has no benchmark_closes`，E 保持不可用。
- corrupted、stale、low coverage 和日期不匹配均 fail closed。

## 60-session history dry-run

基线：`r4_2-db-sandbox/`；target=`2026-08-06`；sessions=`60`；shadow=`r4_2-history-shadow.db`。

首次运行 `r4_2-history-first.json`：

- processed=60，failed=0，reused=0。
- 增量：selection_batches=60，strategy_runs=360，candidates=23,377，pullback_state_history=88,165，outcomes=0。
- 候选：A=19,562，B=0，C=875，D=0，E=2,940。
- C 状态：`再启动`=1,415，`回调中`=28,751，`回调到位`=3,081，`延伸中`=19,548，`破位失效`=35,370。
- A history coverage 从 0 增至 60；逐日报告保留自然 `再启动`的代码及诊断。

显式 exact reuse 的 `r4_2-history-second.json`：

- processed=60，failed=0，reused=60。
- selection_batches、strategy_runs、candidates、pullback_state_history、outcomes 五表增量全部为 0。
- manifest、fingerprint、candidate counts 与首跑一致；复用路径不再调用 context/evaluator。
- 四个源库首跑前后、二跑前后 SHA-256 均相同。

## 浏览器六态

每个状态均保存 `r4_2-browser-<state>.{png,yml,console.json}`，运行于 `SCREENING_BASE_DIR` 隔离副本和固定测试时钟。所有最终 console 证据均为 Errors=0；24 条 warning 仅来自空 sentiment 图的 Vega infinite-extent 提示。

| 状态 | 证据结论 |
|---|---|
| intraday | selected/effective/formal=`2026-08-06`；v2 cache as-of=`10:28+08:00`；A–E 临时评估成功；B/D 为合法 gate empty，无 `activity_missing`；E 到达 benchmark gate。 |
| pending | 三日期同为 `2026-08-06`；15:04 cache 在 15:05 形成 `close_pending`，页面明确不定版。 |
| final | 三日期同为 `2026-08-05`；只读 batch 7；C 候选 `600003 Gamma` 是 formal replay 自然 `再启动`；页面实际选择 `continuation × 再启动`，表按 rank 显示。 |
| unavailable | 三日期同为 `2026-08-06`；10:28 cache 在 15:05 已 stale，A–E fail closed 为数据过时/缺失。 |
| not_run | 三日期同为 `2026-08-06`；未 migration 的 task0 副本使 A–E 全部显示 `migration_required`。 |
| failed | 三日期同为 `2026-08-06`；隔离 v2 cache 临时损坏，evidence 显示 `provider_failed`；良好 cache 随后恢复。 |

UI 合成 fixture 由 `create_sample_market_dbs()` 与 `_extend_to_p60_history()` 生成，并先经正式逐日路径写出 batch/state；没有 SQL 插入核心状态。为让 final UI 选择自然 retrigger 日，只在 replay 完成后裁剪该 UI 副本中更晚的行情行。排序和强度×阶段过滤另有 UI 单测覆盖。

## 真实 sandbox close smoke

`r4_2-close-smoke-evidence.json` 来自 task0 一次性数据库副本，2026-08-06 写入 sandbox batch 61：

- A=110，C=14，E=85；C 状态中 `再启动`=14。
- B compression=0、momentum=0，D=0；真实源副本的 20 日活跃度不完整，诊断为 `activity_missing`。因此真实 smoke 只证明其 fail-closed 数据现状；B/D 功能验收由有完整活跃度的合成 UI fixture 和单测承担，没有把缺失冒充成功。
- batch status=`complete`，definition_version=`v2.5`，mode=`close_final`。

## 验证与只读保护

- 定向：任务卡规定的六模块，Ran 85 tests，OK。
- 全量：Ran 265 tests，OK (skipped=2)，满足 N>=257。
- 生产最终 SHA-256 与任务卡基线完全一致：
  - `a_share_mvp.db`: `E8181DECF433B72ED28BB6849D02E6E06B9C1E23DA72C377FDDA2A4C3250FD63`
  - `etf_mvp.db`: `4C2585BBFBA57214C1455FFB5F3402D555BCB8C231DF5B0F081BCD2DA3E259D4`
  - `mining_mvp.db`: `2C1D71550C27CD28E7F26B7AE5513736964F3AA05A6452D683B5F4EF91F778EB`
  - `ths_concept.db`: `47E65F2F1D3C245FBE6C879C9FFA9E4F103FEDC79693FEAE160DD4D0EFBDB45F`
- 生产 `mining_mvp.db` 整库哈希不变，因此 candidates、states、selection_batches 与 outcomes 均零变化。

## 限制

- 未执行生产 migration、生产 canary、生产历史写入或任何生产数据库变更。
- 未访问行情 provider；E browser 证据来自预置离线 v2 cache。
- 未验证收益表现、胜率、回撤或实盘可交易性。

## Lessons

- correction/update：formal loader 测试 fixture 必须同时提供 C candidate 对应的 formal state row；不能沿用旧的 candidate-only 假设。
- verified method：先让合成行情通过完整 formal replay 自然生成状态，再做 UI 日期裁剪，可以同时保留状态真实性与可重复浏览器证据。
- decision：两项均 `skip` memory promotion；它们是本卡实现细节，保留在本报告和项目测试即可。
