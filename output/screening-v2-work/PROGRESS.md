# Screening v2.5 execution progress

## 2026-08-08 R4.2 — task 0 complete

- Goal/order: close C formal state chain → restore E benchmark/as-of cache → provenance-bound history dry-run → six-state browser evidence plus real sandbox close smoke → report and one independent commit.
- Entry points: `mining/candidate_persistence.py`, `mining/selection_batches.py`, `mining/watchlist.py`, `mining/quote_snapshot.py`, `mining/history_bootstrap.py`, and `run_daily.py`.
- Baseline: HEAD is `589af36`; it is its own verified ancestor. User dirty files are preserved; `run_daily.py` diff SHA-256 is recorded in `r4_2-baseline.md`.
- Data boundary: all four production SHA-256 values matched the task card and their single sandbox copies. Production migration and writes remain forbidden.
- Next: task 1 red-light proof for missing C formal state history, then the smallest atomic persistence/read-path repair.
- Block: none.

## 2026-08-08 R4.1 — acceptance gaps closed in isolated databases

- Task 0: `b610757` is an ancestor; the user-owned dirty worktree remains untouched. The four production databases were copied once to `r4_1-db-sandbox/`; source/copy SHA-256 values are in `r4_1-baseline.md` and match again after all work.
- C coverage: `a_history_coverage` and C's A eligibility query now share one strict-prior usable-date window. T never counts itself: a current-day and previous-day complete A yields `1/60`, while `0/60`, `1/60`, and `60/60` are regression-tested.
- Historical dry-run: `run_daily.py --history-bootstrap-dry-run` writes only a copied shadow mining database. The first isolated 60-session replay processed 60 dates, added 60 selection batches / 360 strategy runs / 22,502 candidates, added zero outcomes, and moved A coverage `0/60 → 60/60`; the second identical run reused all 60 fingerprints and had zero batch/run/candidate/outcome growth.
- Deterministic browser: `SCREENING_ACCEPTANCE_NOW_CN` is fail-closed unless `SCREENING_BASE_DIR` is set. Isolated formal snapshots use the sandbox cache only; unrelated dashboard intraday providers are disabled. PNG/YAML/console evidence covers 2026-08-07 10:30 intraday, 15:05 pending, 2026-08-06 18:00 close-final, and the 2026-08-07 known-bad fail-closed unavailable case.
- Fixture/report: CSV SHA-256 and labels remain frozen. Its manifest now separates source provenance metrics from deterministic replay metrics, defines tolerances, and asserts every replay number, gate boolean, and first failed gate.
- Validation: directed R4.1/DTO tests are `55/55 OK`; full Python 3.11 suite is `257/257 OK (skipped=2)`. Final production SHA-256 values equal task-0 and sandbox-copy values; staged code is limited to the R4.1 whitelist and `git diff --cached --check` is clean.
- Commit: `589af36 fix(screening): complete R4.1 acceptance gaps`. `git diff --check b610757..HEAD` is clean; user-owned dirty files, including `run_daily.py`, remain unstaged. Pause for review. Production migration, canary, and cold-start writes remain unauthorized.

## 2026-08-08 R3 — atomic close-final batch and version isolation

- Result: explicit-only `selection_batches` migration, one `BEGIN IMMEDIATE` batch writer, exact stale-row replacement, failed-batch recording, same-fingerprint reuse, and formal v2.5 outcome exclusion are implemented.  Production connections remain un-migrated until separately authorized.
- Verification: 49 directed tests, 88 affected integration tests, and the full 240-test suite are green (2 skipped).  `r3-report.md` records the before/after schema, union inventory, idempotence, failure rollback, v2.0/v2.5 isolation, and 5,034-stock sandbox smoke.
- Data boundary: all four source DB SHA-256 values match their start values after work; R3 migration/smoke touched only `r3-db-sandbox/`.
- Next: pause for R3 review.  Do not start R4.

## 2026-08-07 R2+ locally verified — supersedes the earlier in-progress entry

- Contract repairs: a complete stock snapshot now remains `price_status=ready`; A–E are re-evaluated from one context, including E's benchmark series. E no longer reads a separate UI benchmark: a same-batch index snapshot is accepted through the context, while its absence leaves only E unavailable instead of mixing a prior close.
- Parity evidence: the runtime test constructs the identical complete stock and index T bar twice (`intraday_snapshot` and `close_final`) and asserts all A–E result tables, fields, ranking, and `input_fingerprint` are equal. The inverse partial-bar test asserts provisional activity changes and is not treated as final parity.
- Quote evidence: a successful adapter result is refreshed on the next loader run; the persisted same-day snapshot is used only after provider failure, subject to the existing 10min/0.80/cross-day/close-final rules.
- Sandbox performance: 5,034 stocks / 919,356 bars: context cold start `58.255s -> 17.775s`; A–E first evaluation `35.481s -> 14.521s`; same-context hot rerun `4.965s`. Optimizations preserve adjusted-price, A, B, and E definitions while removing repeated shared matrix/path work.
- Verification: required R2+ suite `Ran 65 tests ... OK`; final full suite `Ran 234 tests ... OK (skipped=2)`.
- Environment: real provider smoke remains `environment-blocked` because the approved browser/CDP topology is unavailable. This does not block the offline R2+ commit. Do not enter R3/R4 without a new directive.

## 2026-08-07 manager correction — runtime recovery gate

- The earlier `Next: R2` remains historical evidence but is superseded for execution order by `docs/superpowers/specs/2026-08-07-screening-v2-runtime-recovery-task-card.md`.
- Current next: RR0 → R2+ → R3 → R4. RR0 fixes the data-quality consumer before more live-provider work; R2+ includes quote failover/observability and then all original R2 acceptance.
- R0/R0.1/R1 completion status and commits are unchanged. Production databases remain read-only until the original R3 authorization gate is satisfied.

## 2026-08-07 task 0 — runtime-recovery baseline

- Goal/order: RR0 → R2+ → R3 → R4; first entry is `offline_daily_update.stock_coverage_for_date`.
- Interpreter: `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe` from `screening-python.txt`.
- Baseline: task-book/offline file/offline binary-diff SHA-256 all match the runtime-recovery card; dirty worktree retained.
- Facts: 2026-08-06 stock=5203, valid_trade=5198, quality=clean; v1.0+v1.1 candidates=57+50=107; baseline `220 OK (skipped=2)`.
- Topology: only installed AkShare endpoints are known; CDP preflight is unavailable, so no live endpoint was retried or inferred.
- Risk: live-provider and browser proof remain environment gates; offline implementation/tests continue without production DB writes.

## RR0 — data-quality consumer recovery

- Result: stock coverage now accepts only `clean` and `usable_with_quarantine`; `partial_missing`, `known_bad_session`, and `quality_unavailable` enter the repair plan.
- Red/green: the 10-day quarantine/bad-session plan failed before the repair and passed afterward; matrix coverage asserts all five consumer states.
- Verification: `tests.test_data_quality tests.test_backfill_rules` = 26 OK; full suite = `Ran 222 tests ... OK (skipped=2)`.
- Commit: `d453bc2` (`fix(screening): fail close unusable stock sessions`); production DB hashes unchanged and the pre-existing offline diff remains preserved outside this commit.

## R2+ — in progress, not commit-ready

- Implemented: one bounded `QuoteSnapshotAdapter` with two-source failover, structured status/diagnostics, 60s cooling, atomic same-day cache and 10min/0.80/close-final invalidation; Streamlit now uses that path instead of its private AkShare loop.
- Implemented: batch-only amount→turnover activity source, T-1 20-session median ratio, provisional snapshot flag, context benchmark/activity statuses and deterministic input fingerprint.
- Evidence: new quote/activity/parity tests plus full suite `Ran 230 tests ... OK (skipped=2)`; red evidence covered absent adapter and the former mixed/empty intraday activity logic.
- Remaining before R2+ commit: complete formal A–E shared-context parity proof and the task-card performance/real-data matrix; do not start R3/R4 early.

## R1 recovery - v2.5 strong-trend contract

- Result: A now has only `continuation` / `fresh_breakout`, A2 uses `prior_close_high_20` plus the 0.85 60-session structure floor, and A1 retains priority. The near-high gate uses nonnegative `near_high_distance_atr`; ranking alone uses the negated raw value. P200 now restores MA120 between MA60 and MA200 when legacy trend structure is evaluated.
- C source: only earlier `strong_trend` candidates at `version=v2.5` with a formal tier qualify C; no legacy scanner source or current-day A record can qualify it.
- Compatibility: `second_launch` projects only `ma_proximity`, `stop_signal`, and `flag_strategies="strong_trend"`; its C query, state, candidate set, and ordering stay unchanged.
- Verification: `tests.test_strong_trend tests.test_trend_factors tests.test_second_launch tests.test_mining_pipeline` passed 46 tests. The final full suite passed `Ran 220 tests ... OK (skipped=2)`.
- Sandbox smoke: `r1-audit.md` records A=104/5034 (A1=60, A2=44), five-stock gates, 60-session distribution, sensitivity, and same-input parent/new list comparison. No threshold was changed.
- Commit: `eb956f7b64f9ca9c8e3ff0b5d610395fc27b62ff` (`feat(screening): redefine formal strong trend tiers`).
- Next: R2; block: none.

## R0.1 - provider zero-halt recovery

- Result: R0.1 classifies the narrow full-zero/no-activity/positive-`pre_close` provider shape after duplicate conflict handling, preserves raw values, and uses only the prior valid close for adjusted OHLC. Current-day placeholders are excluded from close-final candidates while the existing price-only intraday snapshot exception remains intact.
- Evidence: the sandbox-only five-stock canonical fixture was regenerated with SHA-256 `185fd7ee4dd3175328855d7d9e3f77509eaf2bc881a11c1c88aa5f0ceb3f89e6`; the sandbox source `a_share_mvp.db` SHA-256 remained `d6a505adf402a4200d8074d7d708f33fd8eae1c00ac938545c948821eaa33de4`.
- Verification: targeted R0.1 plus selection-runtime coverage `Ran 29 tests ... OK`. Full suite ran 218 tests and has only the two already-approved R1 `second_launch` compatibility projection failures.
- Commit: `a7544765f1638f93e4a21676123780915ffebaf6` (`fix(screening): preserve provider zero halt placeholders`).
- Next: complete R1.

## R1 update — audit and threshold evidence

- Result: `r1-audit.md` records the 60-session full-market distribution, funnel, monthly stability, +/-10% sensitivity, and five-stock per-gate evidence from the isolated database copy. The optimized batch calculation preserves 63 formal A candidates (60 `continuation`, 3 `fresh_breakout`) while reducing A evaluation time from about 30 seconds to about 11 seconds on that copy.
- Next: R1 is implementation-complete within its file whitelist, but cannot satisfy the mandatory full-suite and frozen-fixture gates without an authorized resolution.
- Block: see both R1 entries in `BLOCKED.md`.

## §7 task 0-3 — workspace, baseline isolation, interpreter

- Goal: protect user-owned in-progress changes and establish a reproducible task interpreter.
- Allowlist: `output/screening-v2-work/**` only.
- Baseline: `baseline-status.txt` records the full initial dirty worktree. `preexisting/` contains copies, binary diffs, and SHA-256 records for `run_daily.py` and `mining/backtest.py`.
- Commands: read `AGENTS.md`; read task-book §6, §7 task 1-2, §2, and §3; `git status --short`; bootstrap interpreter import probe.
- Result: bootstrap interpreter fixed at `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe`; Python 3.11.9 imports `pandas`, `requests`, `streamlit`, and `tabulate` successfully.
- Test baseline: `python -m unittest` ran 205 tests in 17.602s with 2 skips and exactly the two authorized R1 `second_launch` failures; complete output is in `baseline-unittest.txt`.
- Data sandbox: copied `mining_mvp.db`, `a_share_mvp.db`, `ths_concept.db`, and `etf_mvp.db` into `db-sandbox/`; source/copy size and SHA-256 match and are recorded in `db-sandbox/source-manifest.sha256`.
- Next: complete the §7 routing-boundary read, then begin R0.
- Block: 无。

## R1 — absolute strong-trend definition and C eligibility source

- Goal: implement §3 A1/A2 absolute gates, remove formal `趋势成型中`/`weakening`, and require C to use an earlier v2.5 A close-final qualification.
- Allowlist: `mining/trend_factors.py`, `mining/scanners/strong_trend.py`, `mining/watchlist.py`, the two §6-authorized `tests/test_mining_pipeline.py` methods, and R1-specific tests.
- Baseline: R0 commit `9ba613e`; full unittest has only the two authorized second-launch fixture failures.
- Commands so far: strong-trend/C focused tests; the two authorized fixture tests; temporary DB inspections of `clean_dates`, A eligibility and C state.
- Result: A now emits only `continuation` / `fresh_breakout` after absolute gates; C filters A history to v2.5 and dates before the evaluation date; the two fixtures now establish a 75-session history, earlier A record, prior pullback state and current `再启动` result.
- Next: complete R1 audit/real-data smoke and run all tests that do not depend on the blocked compatibility projection.
- Block: see `BLOCKED.md` R1 entry.

## R0 — data truth, usable calendar, and row-level adjustment

- Goal: preserve usable market sessions and valid stock segments while fail-closing systemically bad sessions.
- Allowlist: `mining/data_quality.py`, `mining/adjusted_prices.py`, `mining/selection_context.py`, `mining/universe.py`, `mining/scanners/base_breakout.py`, `mining/scanners/momentum_breakout.py`, and R0-specific tests only.
- Baseline: full unittest `N0=205`, only the two §6-authorized R1 `second_launch` failures; production data retained only in the §7 source-hash manifest.
- Commands: targeted data-quality / adjusted-price / selection-context tests; affected scanner/runtime/universe tests; full unittest; sandbox-only 192-date quality audit; sandbox-only adjusted-price and base-breakout smoke.
- Result: two-axis session states, deterministic duplicate quarantine, usable calendar plus quarantine ledger, row/edge-level adjusted segments, derived adjusted `change_pct`, and price/metadata state separation are implemented. The canonical five-stock fixture and full smoke evidence are in `r0-smoke.md`.
- Verification: targeted `Ran 15 tests ... OK`; final full suite `Ran 209 tests ... FAILED (failures=2, skipped=2)` with exactly the R1 fixture failures required by the R0 gate. No import or environment error occurred.
- Next: commit R0 after cached-diff review; then execute R1 only from the task card.
- Block: 无。

## 2026-08-08 R4.2 closure

- Result: C formal state rows now share the A–E transaction and current complete-batch read boundary; v2 cache restores benchmark/as-of atomically.
- History: 60-session first run completed with 60 batches, 23,377 candidates, 88,165 C states and natural `再启动`; exact reuse added zero rows and reused 60/60 batches.
- Browser: six isolated states saved as PNG/YAML/console JSON; dates align, B/D UI proof has no `activity_missing`, and final shows `continuation × 再启动` filtering.
- Real smoke: sandbox 2026-08-06 batch 61 completed with A=110, C=14, E=85; B/D real-data activity remains unavailable and is reported separately.
- Verification: directed 85 tests OK; full 265 tests OK with 2 skips; production DB hashes unchanged.
- Safety: no production migration/database write, no provider call, no SQL state insertion; user dirty files preserved.
- Block: none. Production migration/canary/history writes remain outside this task and still require separate authorization.
