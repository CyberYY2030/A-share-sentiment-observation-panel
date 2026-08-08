# Screening v2.5 blockers

## 2026-08-08 R4.1

- 无。所有 R4.1 可执行项均在隔离副本完成；2026-08-07 的 397 行 `known_bad_session` 仅作为 fail-closed unavailable 反例，不是 close-final 真值。生产 migration、canary 与历史写入仍需用户另行授权，属于明确延后事项而非本卡阻断。

## 2026-08-08 R4.2

- 无。本卡要求的代码、测试、history dry-run、六态浏览器证据、真实 sandbox smoke 和只读保护均已完成。
- 生产 migration、canary、历史写入及收益验证不在本卡授权范围；它们是明确延后项，不作为 R4.2 完成声明。

## 2026-08-07 approved resolution

- Task-book SHA-256: `A3D61BE3AD923FE6ED3193EC223BC00B650F55BD633EE07A5F76E14C540CE2DB`.
- resolution=approved.
- Execution order: R0.1 must be independently committed before completing the already-present R1 work. The historical blocker evidence below remains unchanged.

### R0.1 completion

- commit=`a7544765f1638f93e4a21676123780915ffebaf6` (`fix(screening): preserve provider zero halt placeholders`).
- Verification: targeted data-quality, adjusted-price, selection-context, and selection-runtime coverage passed (`Ran 29 tests ... OK`). Full `python -m unittest` ran 218 tests; its only two failures are the approved, not-yet-completed R1 `second_launch` compatibility projections (`ma_proximity`, `stop_signal`).
- Sandbox fixture regenerated from unchanged `a_share_mvp.db` SHA-256 `d6a505adf402a4200d8074d7d708f33fd8eae1c00ac938545c948821eaa33de4`; fixture SHA-256 is `185fd7ee4dd3175328855d7d9e3f77509eaf2bc881a11c1c88aa5f0ceb3f89e6`.

### R1 completion

- commit=`eb956f7b64f9ca9c8e3ff0b5d610395fc27b62ff` (`feat(screening): redefine formal strong trend tiers`).
- Verification: `tests.test_strong_trend tests.test_trend_factors tests.test_second_launch tests.test_mining_pipeline` passed 46 tests; final `python -m unittest` passed `Ran 220 tests ... OK (skipped=2)`.
- Sandbox smoke: A=104/5034 (continuation=60, fresh_breakout=44). `r1-audit.md` records the frozen five-stock gates, distribution, threshold sensitivity, and same-input legacy/new comparison. No unresolved R1 blocker remains.

## 2026-08-07 RR0 completion

- none.

## 2026-08-07 R2+ environment gate

- CDP preflight cannot connect because Chrome remote debugging is not enabled. No live endpoint was retried through an unapproved path; real-provider and browser acceptance remain unproven while offline work continues.

## R1 — frozen five-stock fixture conflicts with the required A2 assertions

- Date: 2026-08-06.
- Source: `output/screening-v2-work/fixtures/five-stock-canonical-2026-08-05.csv`, SHA-256 `d3444ff22c1e85ccdc1f6a0ad25212b030da843140985e98433fd16073fd079b`, evaluated with full-market RPS/liquidity ranks from the isolated `db-sandbox` copy.
- Verified state: `300996` has `invalid_price` rows on 2026-07-27/28/29, leaving no complete current 10/20-session adjusted return window. It therefore fails the shared A gates. `601858` passes the shared gates with `ret10=0.4926`, `ret20=0.5612`, `rps20_pct=0.9978`, but `close=25.36` is only `0.8626 * rolling_high_60=29.400958881953624`; it fails A2's frozen `close >= 0.98 * rolling_high_60` gate and cannot use A1 either.
- Block: R1's mandatory assertion says both `300996` and `601858` must pass A2 on the frozen 2026-08-05 fixture. The canonical data falsifies that assertion. §3.4 requires stopping and reporting a canonical-data disagreement rather than moving thresholds or adding stock-specific logic.
- Next: user review must choose whether the fixture/data provenance is corrected, the expected labels are corrected, or the frozen A2 contract is explicitly changed. No threshold or code exception is authorized here.

## R1 — second_launch compatibility-field scope conflict

- Date: 2026-08-06.
- Command: `python -m unittest tests.test_mining_pipeline.MiningPipelineTests.test_second_launch_selects_pullback_shrink_and_stop_setup tests.test_mining_pipeline.MiningPipelineTests.test_execute_daily_pipeline_persists_second_launch_via_generic_scanner_branch`.
- Verified state: the locally migrated fixture now has 75 usable sessions, an earlier v2.5 `strong_trend` `continuation` candidate, a persisted prior `回调中` state, and produces C state `再启动` with `shrink_ratio=0.5`.
- Block: the two frozen assertions additionally require `ma_proximity`, `stop_signal`, and `flag_strategies` in the emitted `second_launch` candidate features. `mining/scanners/second_launch.py` currently constructs its feature dictionary without these keys. R1's explicit allowlist excludes that file; changing the two assertions or weakening them is forbidden by §6.
- Next: continue R1 code/tests that stay within its allowlist. A later authorized resolution must either add a compatibility-only feature projection in `mining/scanners/second_launch.py` or amend the R1 allowlist; it must not restore legacy flags as C eligibility.
