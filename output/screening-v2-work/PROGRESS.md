# Screening v2.5 execution progress

## 2026-08-11 Screening v2.5 OPS-1 production recovery

- Status: `blocked_after_partial_production_write`; OPS-2 and OPS-3 must not start.
- OPS-1A passed: parent/child harness survived 311.6 seconds, 51-file source manifest froze commit `b38dc5b`, focused 35 and full 295 tests passed (2 skipped), and py_compile passed. Three online recovery backups passed integrity/table-count checks.
- One authorized 2026-08-07 market-only invocation ran with `--skip-mining --no-health`. Mining, THS, ETF, health, pushes, outcomes, and formal batches were not written.
- The repair loop started five identical stock child attempts, exceeding the task-book three-failure maximum. The agent stopped its owned main/child process tree; no matching process remains. The repeated literal `180-second` text in latch-aware coverage was stale diagnostic evidence, not a confirmed timeout of this invocation.
- a_share changed to `D8FEDFF685683DD8D17CCF6710E24867F8B436F01A6B1C29B52A7D0D7ACF5AFB`; THS/ETF/mining remain at their pre-launch hashes and all four SQLite quick checks pass. 08-07 raw is now `usable_with_quarantine` (5180 rows / 5147 valid / 33 quarantined), but its old known-bad latch remains, so latest usable close remains 2026-08-06.
- Root cause: the retry loop consumes latched coverage instead of raw reinspection and permits attempts beyond three after early positive growth. No auto-restore, latch clearing, rerun, source modification, or later OPS task was performed.
- Evidence and required authorization choices are in `ops-1-blocked-report.md` and `ops-1-production-stop.json`.

## 2026-08-10 R4.3.2 — task 0 complete

- Goal: replace pqquotation's 87-request burst with bounded 60-code batches that return partial success.
- Order: baseline → red tests → executor → adapter diagnostics → local gates → one live gate → report/commit.
- First entry: `_tencent_worker` / `_call_tencent_quotes_with_timeout`; public acceptance remains `QuoteSnapshotAdapter.load()`.
- Fixed parameters: workers 8, HTTP timeout 2.0s, worker collection 6.5s, parent budget 8.0s.
- Core SHA-256 values match the task card; HEAD is `92a3bb0`; user dirty files are preserved.
- Production baseline and exact dirty status are recorded in `r4_3_2-baseline.md`; no DB write/migration is authorized.
- Maximum risk: executor shutdown or queue serialization can erase otherwise successful partial batches at the parent deadline.
- Live gate remains forbidden until every required local suite is green and the clock is within 09:45–11:20 or 13:15–14:45.

## 2026-08-10 R4.3.2 — implementation and live acceptance complete

- Red/green: the old code had no partial batch executor; three new tests failed at import. The final quote/intraday suite is 24 OK.
- Executor: 60-code batches, max_workers=8, HTTP timeout=2.0s, collection=6.5s, parent=8.0s; no parameter adjustment was needed.
- Affected regression: 92 OK. Full regression: 278 OK with 2 skips. Final py_compile and task-file diff check passed.
- Unique live gate started 13:42 +08: stock 2.953s, public 3.860s, 87/87 batches successful, max concurrency 8.
- Live result: Tencent `snapshot_usable`, raw/normalized 5203/5202, coverage 0.999808, provider-today ratio 1.0.
- Benchmark: Tencent `000852=7670.07`; same-cycle status ready.
- Fresh-process cache_only: attempts 0, from_cache true; frame, benchmark, as-of and bundle hash all identical.
- Safety: all four production hashes match this task baseline; no migration/write; user dirty files preserved. Whole-worktree diff-check reports only four pre-existing user EOF blanks; the R4.3.2 scoped check is clean.
- Commit: `d90303e` (`fix(screening): bound Tencent snapshot batches`), parent `92a3bb0`; only five allowlisted tracked files were committed.

## 2026-08-09 R4.3.1 — task 0 complete

- Goal/order: strict-prior full denominator → Tencent/EM bounded stock chain → independent benchmark → tests → non-trading live gate → report/commit.
- First entry: `_expected_snapshot_codes`; provider entry: public `QuoteSnapshotAdapter.load()`.
- Baseline: HEAD=`c80f27f`; existing dirty files and four production hashes are frozen in `r4_3_1-baseline.md`.
- Truth: `2026-08-07` known-bad `397/394`; strict-prior usable/full is clean `2026-08-06` `5203/5198`.
- Installed seam: `pqquotation 0.8.5`, Tencent caller-supplied batches of 60; EM is one fallback; Sina is excluded.
- Maximum risk: a bad denominator can manufacture coverage, or package field semantics can swap current and previous close.
- Current window: Sunday/non-trading; external proof is capped at one 20-code Tencent sample after local green.
- Block: trading-window end-to-end/cache proof cannot run today; implementation and local verification continue.

## 2026-08-09 R4.3 — task 0 complete

- Goal/order: live benchmark producer → independent date evidence → compact provenance-correct history → three affected browser states → verification and one commit.
- Entry points: `mining/quote_snapshot.py`, `mining/streamlit_tabs/tab_scanner.py`, `mining/history_bootstrap.py`; output becomes local-only.
- Baseline: HEAD=`602e25a`, ancestor check passed; 28 tracked output paths await index removal; user dirty hashes are in `r4_3-baseline.md`.
- Data boundary: four production SHA-256 values match R4.2; production migration, canary, history, and database writes remain forbidden.
- Verification: existing C-directed suite ran 27 tests, all passed.
- Maximum risk: false date alignment or benchmark reuse across refresh cycles; both must fail closed with structured evidence.
- Next: task 1 red light through public `QuoteSnapshotAdapter.load()`, then bounded benchmark producer.
- Block: none.

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

## 2026-08-09 R4.3 preproduction gate

- Result: stock and required benchmark quotes now share one public `QuoteSnapshotAdapter.load()` cycle and atomic cache bundle; E fails closed independently when benchmark evidence is unavailable.
- Date gate: selected query date, effective runtime-context date, and actual complete-batch formal date are independently evidenced; mismatch clears candidates and returns `date_mismatch`.
- History: schema v2 records generator commit/path/source hash; first run processed 60 with deltas 60 batches, 360 strategy runs, 23,377 candidates and 88,165 states; exact reuse reused 60/60 with zero deltas in all five tables.
- Browser: isolated `date_mismatch`, `intraday`, and `final` PNG/YAML/console triplets complete; all console artifacts report zero errors; browser services stopped.
- Verification: directed 87 tests OK; full 268 tests OK with 2 skips; production DB hashes unchanged.
- Git boundary: all 28 previously tracked `output/**` files removed from the index while retained locally; final tracked output count is zero.
- Safety: no production migration/database write, no manual core-state insertion, no provider endpoint call after failed network prerequisite, and no user dirty file included.
- Block: real-provider probe is `environment-blocked` because the required Chrome CDP connection was unavailable. R4.3 is offline-preproduction complete and must not be called production-ready.
- Commit: `c80f27f49850327e4eeb7666f2a461ee7b5809d2` (`fix(screening): close R4.3 live evidence gate`), parent `602e25a73628bb29e32b44b789d165704611c0b8`; post-commit range diff check and commit-scope audit passed.

## 2026-08-09 R4.3.1 completion record

- Denominator: strict-prior data-quality selection excludes `2026-08-07` `known_bad_session`; source date `2026-08-06`, expected codes 5203. Runtime fails before provider access below 5000.
- Provider chain: caller-code Tencent batches once, then EM once; no `stock_zh_a_spot`. Benchmark is an independent Tencent/EM cycle and requires positive finite `000852` for E.
- Red/green: old logic selected 397 and lacked the minimum-denominator guard/Tencent normalizer; corrected focused tests pass.
- Verification: affected suite 66 OK; full suite 272 OK with 2 skips.
- Non-trading live evidence: sample 20/20; full raw 5203, normalized 5199, coverage 0.999231; `000852=7679.53`; bounded-call total about 2.001 seconds.
- Trading-window boundary: Sunday probe proves transport only. Public real-provider `load()` and restart `cache_only` were not run; status is `locally-verified + trading-window environment-blocked`.
- Safety: no migration, no production database write, and all four production database hashes are unchanged.
- Commit: `92a3bb0` (`fix(screening): harden R4.3 live providers`), parent `c80f27f`; the independent commit contains only the six R4.3.1 allowlisted tracked files.

## 2026-08-10 R4.3.1 trading-window live gate

- Started at 10:21 +08 inside the A-share morning session; denominator source `2026-08-06`, expected count 5203.
- Exactly one public real-provider `QuoteSnapshotAdapter.load()` ran. Tencent timed out once at 8 seconds; EM fallback failed once with `RemoteDisconnected`; total 14.906 seconds.
- Result: `provider_failed`, raw/normalized 0/0, coverage 0.0. Benchmark was not executed, so `000852` remains unavailable.
- One fresh-process `cache_only` check ran with attempts 0 and returned `cache_only snapshot unavailable`; no success bundle existed and no provider was revisited.
- Final state: `environment-blocked`, `real_env_verified=false`; the result probe did not succeed.
- Safety: no migration or production write. Four database hashes are identical across this probe. Pre-probe `a_share_mvp.db`/`ths_concept.db` drift from the 2026-08-09 baseline and the pre-existing `ths_concept.db-journal` are recorded in `BLOCKED.md`.
- Evidence: `r4_3_1-trading-live-state.json` and `r4_3_1-trading-live-gate.json`.

## 2026-08-10 R4.4 task 0

- Goal: isolate activity completeness by `sec_code`; keep B/D gates, ranks, thresholds and R4.3.2 frozen.
- Order: old red evidence -> per-stock builder -> 21/46-day consumers/tests -> read-only sandbox smoke -> full acceptance/commit.
- Baseline: HEAD `d90303e7208a`; directed 62 tests OK with 2 skips; all pre-existing dirty files preserved.
- Safety: four production hashes equal the R4.3.2 baseline; one sandbox copy made and hashes match; no DB writes/migrations.
- Old fixture: public builder and compression scanner both returned 0 rows and `activity_missing=3` instead of A=amount/B=turnover/C=missing.
- Real old result: 2026-08-06 universe 5036; builder returned 0 and `activity_missing=5036`.
- Coverage invariant: amount complete 21/46 = 4981/4950; turnover complete 21/46 = 0/0 under unchanged sandbox hash.
- Max risk: source completeness for compression must be decided on all 46 days while its ratio baseline remains T-20..T-1.
- First entry: `mining/event_activity.py`; scanner edits limited to explicit window wiring and removal of duplicate missing ownership.

## 2026-08-10 R4.4 completion

- Result: per-stock finite-positive completeness and amount/turnover fallback implemented; 21/46-day cache and consumer windows are isolated.
- Diagnostics: builder solely owns mutually exclusive `activity_missing` / `activity_window_missing`; scanner double counting removed.
- Real sandbox: 21-day eligible 4981, 46-day eligible 4950; B momentum/compression candidates 191/3; D gate result 0 with all 5036 accounted.
- Tests: directed baseline 62 OK (skipped=2); new R4.4 tests 8 OK; full 286 OK (skipped=2); py_compile OK.
- Safety: production and sandbox four-DB SHA-256 values unchanged; no outcomes, migration, provider call, or production write.
- Diff: R4.4 scoped `git diff --check` passes; whole-worktree check is limited only by preserved user dirty EOF whitespace recorded in BLOCKED.
- Evidence: `r4_4-red-evidence.md`, `r4_4-sandbox-evidence.md`, `r4_4-report.md`.
- Commit: `440c3f7d94ac6787d46c437553910df6c7e83c96`, parent `d90303e7208a`; commit range diff check passed and contains only 7 allowlisted files.

## 2026-08-10 R4.4 evidence closure

- Scope: evidence-only; HEAD/parent remain `440c3f7d94ac` / `d90303e7208a`, with no amend, rebase, code edit, test rerun, provider probe, or commit.
- Corrected fixture: `000002` amount gap is inside 21 days at `dates[-5]`; actual per-code source/status is emitted.
- Red JSON: parent archive executed directly; rows=0, `activity_missing=3`; SHA-256 `5AC8556D...FC673`.
- Green JSON: A=`amount`, B=`turnover_ratio`, C=`activity_missing`, eligible/missing=2/1; SHA-256 `ACB76EFE...1FB73`.
- Sandbox: universe 5036, eligible 21/46=4981/4950; B candidates 191/3; D 0 with full accounting.
- Outcomes: actual read-only count 24937 -> 24937, delta=0; four sandbox hashes unchanged.
- Validation: both JSON files passed `json.load` and all contracted assertions; production hashes unchanged.
- Evidence paths: `r4_4-red.json`, `r4_4-green.json`, exact commands and assertions in `r4_4-report.md`.

## 2026-08-10 Screening v2.5 P0 production-activation rehearsal

- Status: `ready_for_authorization`; this is a sandbox-only migration/canary rehearsal, not a production result.
- Task 0 reconfirmed HEAD `440c3f7d94ac6787d46c437553910df6c7e83c96`, all four production DB SHA-256 values, pre-migration schema/counts, zero formal v2.5 baseline, `a_share` WAL=0, no Python/Streamlit process, and latest usable close `2026-08-06` rather than MAX day `2026-08-07`.
- `production-activation-runner.py` used SQLite online backups from read-only source connections to create mining/a_share sandbox copies. Both passed integrity and logical table-count equivalence; ETF/THS were hash-only.
- Security red lights: wrong expected mining SHA and unapproved production-root apply both failed before a write; sandbox and production hashes stayed unchanged.
- Sandbox migration produced one complete batch `1` with six v2.5 close-final runs and fingerprint `03ec0d541c0201396e2670fd00fa430db2f64dc000b9a9368c241ba1c30f45c3`. A/B/E have 110/3+191/85 candidates; C/D are empty; outcomes, legacy rows, and watchlist tables remain unchanged.
- Same-fingerprint apply returned `reused=true` and zero growth in selection batches, runs, candidates, states, and outcomes. Formal panel/report readers selected the same batch and 389-candidate collection.
- Evidence: `production-activation-baseline.md`, `production-activation-rehearsal.json`, and `production-activation-report.md`. Runner SHA-256: `D90E3CF4D90E751FF9817A2B835F9CE1048017A7BEE005A2BA926EB5BFFAA8DF`.
- Next action: pause for independent review and explicit production authorization; no production migration, batch, history replay, or DB write occurred.

## 2026-08-10 Screening v2.5 P0.1 production-activation runner hardening

- Status: `ready_for_explicit_P1_authorization`; P0.1 is sandbox/read-only production evidence only and does not execute production apply.
- Task 0 reconfirmed HEAD `440c3f7d94ac`, old runner SHA `D90E3CF4...BFFAA8DF`, four production hashes, source schema/counts, WAL=0, no Python/Streamlit process, and latest usable close `2026-08-06`.
- Runner now has exact action statuses, read-only production plan/verify, restricted online recovery backup, pre-write source-manifest verification for production apply, and durable `write_stage`/`blocked_after_migration` reporting.
- Production plan passed with all four hashes unchanged. Illegal recovery path stopped before creation; legal `mining_mvp.db` recovery backup passed integrity/table-list/table-count equivalence.
- P0.1 red preflights for wrong SHA, missing production authorization, and a tampered manifest all returned exit 2 with `write_stage=no_write`; production hashes stayed unchanged.
- New P0.1 sandbox apply produced complete batch `1`, frozen fingerprint `03ec0d54...0f45c3`, six formal `ok/empty` runs and 389 candidates. Same fingerprint returned `reused=true` with zero controlled-table deltas; read-only verify passed schema/run/count/reader-parity/integrity.
- Frozen code manifest: 37 actual repository sources, SHA `11F3C2FE...F930FFA`. Final runner SHA `04FF1F43...5BEE0F2`; `py_compile` passed.
- Evidence: `production-activation-p0-1.json`, `production-activation-p0-1-report.md`, and `production-activation-code-manifest.json`. No commit was created.

## 2026-08-10 Screening v2.5 P0.1.1 production post-write verify hash policy

- Status: `ready_for_explicit_P1_authorization`; this is an output-runner correction plus sandbox/read-only proof only, not a P1 authorization or production apply.
- Corrected the runner's production hash gate into `frozen_pre_activation` and `explicit_current_verify`. The latter is reachable only by production-root read-only `verify`; it requires the command's current mining SHA while a-share/ETF/THS remain frozen. First activation, backup, plan, and sandbox retain the all-four frozen baseline.
- Five synthetic hash vectors passed their contracted assertions: frozen baseline accepted; frozen post-write mining rejected; explicit-current matching post-write mining accepted; explicit mining mismatch rejected; and a-share drift rejected.
- Production plan passed exit 0 with `no_write` and all four hashes unchanged. Missing production authorization and a tampered manifest both failed exit 2 with `write_stage=no_write`.
- Existing P0.1 sandbox same-fingerprint apply returned batch `1`, `reused=true`, fingerprint `03ec0d54...0f45c3`, six `ok/empty` runs, and zero growth in every controlled table. Sandbox verify passed with 389 candidates, formal reader parity, and integrity.
- Frozen runner SHA: `11F9304A77C3620317995054057076346B9ECC4AC2BB81148375E2591FAA0A03`; refreshed 37-source manifest SHA: `6A23BD8444E007A7A1C9886A514857BDEB6EADED913C967EE196114BFD6658FA`; py_compile passed. Final production hashes equal the original P0/P0.1 baselines.
- Evidence: `production-activation-p0-1-1.json` and `production-activation-p0-1-1-report.md`. No production migration/apply/write, provider/outcomes operation, or commit occurred.

## 2026-08-10 Screening v2.5 P1 production migration/canary gate

- Status: `blocked_no_write`.
- Explicit authorization reference `P1-20260810-CY` was present. HEAD, frozen runner/37-source manifest, four production hashes, read-only schema/counts, latest usable close `2026-08-06`, and `a_share_mvp.db-wal=0` all passed Task 0.
- Stop condition: `Get-Process` found PID `19132` `C:\同花顺软件\同花顺\UpdateWorking\updater.exe`. The user contract forbids a market-data/update process before backup or production write; it was preserved and not terminated.
- No P1 recovery-backup directory was created; runner backup, production apply/migration, and post-write verify were not started. The initial gate's production hashes were pre-activation values. Evidence: `production-activation-p1-backup.json`, `production-activation-p1-apply.json`, `production-activation-p1-verify.json`, and `production-activation-p1-report.md`.

## 2026-08-10 Screening v2.5 P1 one authorized retry

- Status: `blocked_no_write`.
- The THS updater was absent; a full Task 0 rerun again passed frozen HEAD/runner/manifest/hash/schema/date/WAL checks and showed no Python/Streamlit/THS updater at that instant.
- Immediate pre-backup process recheck then found a new Python PID `23180` at `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe`. The P1 no-Python gate stopped the retry before backup creation or any production write. Evidence: `production-activation-p1-retry.json`.
- The four hashes were last verified before this new external process appeared; P1 made no write and does not assert their post-process state.
- One user-authorized read-only retry still found the same THS updater PID `19132`; all non-process Task 0 gates remained frozen. No additional P1 action ran.

## 2026-08-10 Screening v2.5 P1 R2 production activation

- Status: `production_activated_verified` under explicit authorization reference `P1-20260810-CY-R2`.
- Revised Task 0 passed: no `updater.exe`/Streamlit process, frozen HEAD/runner/37-source manifest, four baseline hashes, zero-byte a-share WAL, read-only SQLite lock checks, schema `migration_required`, baseline counts, and latest usable close `2026-08-06`.
- New recovery backup `production-activation-backups/p1-20260810-CY-R2/mining_mvp.db` used SQLite online backup from a `mode=ro` source. Both source and backup passed integrity/table-list/table-count equivalence; production hashes stayed frozen after backup.
- Exactly one production apply ran. It migrated mining schema to ready and created complete non-reused batch `1`: six formal `ok/empty` runs with 110/3/191/0/0/85 candidates (389 total). Only mining SHA changed to `9153F14D...215BBE`; outcomes, legacy rows, state rows, failed batches, and watchlist tables did not change.
- Fresh-process read-only verify passed with `explicit_current_verify`, complete batch 1, reader parity, integrity, and unchanged verify hashes. Final production counts are strategies 20, runs 2530, candidates 26828, outcomes 24937, selection_batches 1, states 0, formal candidates 389, and formal outcomes 0.
- Evidence: `production-activation-p1-backup.json`, `production-activation-p1-apply.json`, `production-activation-p1-verify.json`, and `production-activation-p1-report.md`. No history replay, provider, outcomes operation, browser, automatic recovery, code/test edit, or commit occurred.

## 2026-08-11 OPS-1 remediation: bounded retry and latch closure

- Source fix committed as `66b93f857b6f33e47d5006dd5f593fa97eb2d06f` on `codex/screening-v2`.
- Root cause confirmed: repair used latch-aware coverage until the end of its loop, so raw-usable 2026-08-07 data could repeatedly start the same child before atomic revalidation cleared `known_bad_session`.
- The prior `timed out after 180 seconds` text is historical diagnostic data, not proof of a current timeout or provider rate limit. Rate limiting remains unproven.
- Fix: raw repair inspection is revalidated immediately; each date has a hard three-child maximum; `--market-workers` and a global `--market-min-request-interval-sec` add load control without changing quality thresholds.
- Verification: targeted 27 tests OK; full `python -m unittest` 298 tests OK (skipped=2); py_compile and allowlisted diff check OK.
- Production read-only preflight: all four `quick_check=ok`; hashes are unchanged; 2026-08-07 is raw-usable (5147 valid, 33 quarantined) but remains correctly latched pending the authorized revalidation; latest usable close remains 2026-08-06.
- No production repair, provider call, backup, migration, formal batch, outcome operation, rollback, or process termination occurred after the code fix.
- Stop: PID 41800 is `C:\同花顺软件\同花顺\UpdateWorking\updater.exe`. The current process gate forbids `updater.exe`; it was preserved and OPS-1 production execution remains blocked until that gate clears or the user explicitly narrows it.
- Evidence: `ops-1-remediation.json`, `ops-1-remediation-report.md`.

## 2026-08-11 OPS-1 2026-08-07 production recovery acceptance

- Status: `ops1_20260807_recovery_verified`; this closes the one authorized 08-07 market-only job, not all remaining OPS-1/OPS-2/OPS-3 work.
- The one background job finished with stock raw revalidation `known_bad_session -> usable_with_quarantine`, action `cleared_after_raw_usable`; its audit row is persisted in `a_share_mvp.db` and copied to `ops-1-revalidation.json`.
- Concept completed `349/349` eligible codes. ETF completed `1496/1496` against the provider snapshot stamped `2026-08-11 11:14:34`; index has all four required codes. Mining was skipped and remains SHA `9153F14D...215BBE`, with all P1 counts unchanged.
- The original job emitted `ok=false` only because its ETF postcondition used 1,559 historic master rows. Commit `c3cf397` changes that read-only postcondition to the current provider-stamped snapshot, fails closed when no snapshot exists, and passed focused tests, full `python -m unittest` (`298`, 2 skipped), and compile. It made no production write.
- Final read-only integrity is `ok` for all four databases. Final SHA-256 values: a_share `1A6438CF...526ACCC`, THS `D73F7C50...157262`, ETF `EF1E5AB2...05B8FC`, mining `9153F14D...215BBE`; `a_share_mvp.db-wal` is 0 bytes and no update process remains.
- 07-13 is still correctly known-bad: raw reason `no_valid_trade`, 0 valid trades, despite 5,199 distinct codes.
- `2026-08-10` is close-ready and currently missing stock/index/concept/ETF. It requires a separately authorized invocation with fresh gates and an online backup; no automatic second recovery was started.
- Evidence: `ops-1-production-acceptance.json`, `ops-1-production-acceptance.md`, `ops-1-revalidation.json`.

## 2026-08-11 OPS-1 2026-08-10 controlled recovery stop

- Status: `blocked_after_partial_production_write`; this is a real current timeout, not the old 180-second diagnostic text.
- Prewrite source commit `c3cf397` and 51-file manifest passed zero-drift verification. New mode=ro SQLite online backups of a_share/THS/ETF passed source/backup integrity, table-list and row-count checks. Four hashes/WAL/process gates were green immediately before the one job started.
- The sole job ran for the 08-10 market domain with `--skip-mining`, four workers, a 0.25-second global request-start interval, and a one-hour invocation deadline. Its repair child timed out after 3,359 seconds after adding 3,383 stock codes and four indexes; coverage stayed 3,375 valid / 5,199 baseline (`0.650702...`), so the atomic revalidation correctly retained `known_bad_session`.
- a_share is integrity-valid but changed: kline rows +3,387, diagnostics +1, revalidation audits +1; SHA `B377263B...CAB8B0`. THS `D73F7C50...157262`, ETF `EF1E5AB2...05B8FC`, and mining `9153F14D...215BBE` are unchanged and all four `quick_check=ok`. No writer remains; a_share WAL is zero.
- Cause assessment: the repair's executor waits for every future and its Sina fallback has no request timeout. A stalled fallback can consume the total budget despite many persisted rows. The specific provider call is unproven because the killed child emitted no per-call final trace.
- Stop policy: no retry, rollback, manual clear, ETF retry, or OPS-2/OPS-3 work. A narrow bounded-request/future-deadline source fix, tests, commit, new manifest/backup and new explicit retry authorization are required.
- Evidence: `ops-1-20260810-backup.json`, `ops-1-20260810-blocked.json`, `ops-1-20260810-blocked.md`.

## 2026-08-11 OPS-1 08-10 bounded provider-call source fix

- Status: `retry_code_ready_needs_explicit_production_authorization`; no production retry was started.
- Commit `bb3abbf54ffd8c7df94313a6a7933fe3cfa3426f` touches only `repair_market_day_akshare.py` and `tests/test_backfill_rules.py`. It keeps EM -> Sina -> Tencent, quality thresholds, request pacing, and bounded repair attempts unchanged.
- New `ProviderCallLimiter` bounds every provider call to 12 seconds and caps each source to `workers` in-flight calls. A timed-out call no longer holds `as_completed`; same-source saturation fails fast and allows the ordinary next fallback.
- Proof: red test first failed for absent limiter; green test proves 10ms stalled Sina -> Tencent fallback and rejects a second Sina call at the source cap. Backfill rules 27 OK; full suite 299 OK (2 skipped); py_compile and commit-range diff check passed.
- Fresh retry source manifest: 51 files, zero mismatches, SHA `1160499D...FAFCFCB`. This source-only work left four production hashes unchanged from the partial-stop state.
- Next: a new preflight + online backup and explicit one-retry authorization are required. No automatic rollback, production retry, or OPS-2/OPS-3 work is authorized.
- Evidence: `ops-1-20260810-timeout-fix.json`, `ops-1-20260810-timeout-fix.md`, `ops-1-20260810-retry-source-manifest.json`.

## 2026-08-11 OPS-1 08-10 retry preflight stop after unexpected dashboard write

- Status: `blocked_no_write`. The new `bb3abbf`/51-file source manifest gate passed, but the required partial-stop production baseline did not: a_share is now `C5BCE5A9...D6C76C55` and THS is `AC542AC7...1C156A376`; ETF/mining remain unchanged.
- No backup, provider call, controlled retry, mining action, rollback, deletion, or production write was performed by this retry attempt.
- Read-only log correlation found a separate 14:36 dashboard-style `offline_daily_update` job using the old broad 10-day/180-second contract. It wrote `daily_matrics`, a new 08-10 revalidation audit, 220 slash-formatted 08-10 concept rows, and concept coverage expectations; it also attempted `run_daily.py` but that launch was refused for exhausted budget. This is outside the authorized market-only retry and cannot become its baseline without an explicit user decision.
- All four `quick_check` results are `ok`; a_share WAL is zero; latest usable close remains 2026-08-07. Integrity does not waive the hash gate.
- Evidence: `ops-1-20260810-r2-preflight.json`, `ops-1-20260810-r2-blocked.md`, and `output/backfill_jobs/offline_daily_update_2026-08-10_20260811_143654.log`.

## 2026-08-11 OPS-1A.1 quality-contract task start

- Goal: unify row completion, provider admission, and parent/child raw market success so incomplete stock rows remain retryable and cannot be persisted as successful repair output.
- Order: freeze four production hashes -> add red row/provider/CLI/postcondition tests -> implement the shared low-level contracts -> run the isolated production-copy fixture -> run focused/full/static/hash/allowlist gates -> report and commit.
- First entry: public single-row classification in `mining/data_quality.py`, then `repair_market_day_akshare.fetch_with_sources()` and existing-row selection.
- Baseline: HEAD `bb3abbf`; only user-owned untracked `.claude/` and the original task card are present. Exact production hashes are recorded in `ops-1a1-baseline.md`.
- Maximum risk: the child can currently return success from raw row counts while the parent rejects the same date on quality; any child/parent divergence must become `child_parent_quality_mismatch` and fail without clearing the latch.
- Data boundary: source, tests, and an SQLite online-backup sandbox only. No network, offline update execution, Streamlit, production database write, restore, or latch clear is authorized.

## 2026-08-11 OPS-1A.1 quality-contract complete

- Commit: `9805084` (`fix(ops): enforce market repair quality contract`), containing only the five allowlisted source/test files.
- Row contract: `classify_stock_row()` exposes the existing `_row_status` semantics; only `valid_trade`, `confirmed_halt`, and `provider_halt_placeholder` are complete. Existing retryable rows are selected again and can be atomically replaced only by a complete provider row.
- Provider contract: every stock repair result passes the shared classifier before fallback termination or continuity checks. Tencent `stock_zh_a_hist_tx` remains available only as an attempted OHLCV source and is deterministically rejected for missing `amount`; the index Tencent path is unchanged.
- Parent/child contract: `raw_market_postcondition()` is shared by repair CLI and updater, ignores the old latch, requires raw status `clean` or `usable_with_quarantine`, and requires all four indexes. Any rc disagreement becomes `child_parent_quality_mismatch`, fails, and preserves the latch.
- Evidence: red 6 tests failed in the expected 4-failure/2-error shape; green focused suite is 43 OK; full suite is 306 OK with 2 skipped; py_compile, diff-check, zero old-threshold search, and allowlist gates passed.
- Sandbox: SQLite online backup only. All 595 Tencent/missing-amount rows entered retryable selection; a clearly synthetic deterministic fixture replaced those 595 rows in the copy, improving raw quality from `partial_missing` (4587 valid, 595 missing) to `clean` (5182 valid, 0 missing). This is not real-provider or production-recovery evidence.
- Production boundary: all four final SHA-256 values equal the task-start values. No production database, latch, provider, offline update, or Streamlit execution was touched.
- Report: `ops-1a1-report.md`; raw evidence: `ops-1a1-red.txt`, `ops-1a1-green.txt`, `ops-1a1-sandbox-proof.json`.

## 2026-08-11 OPS-1 08-10 r3 bounded-retry outcome

- Status: `blocked_after_partial_production_write`. Under the user-accepted r3 baseline, new online backups and prelaunch gates passed; exactly one `--skip-mining --no-health` invocation then completed `ok=False`. No retry was started.
- The bounded provider fix worked operationally: four 12-second provider timeouts, zero saturation, and no one-hour deadline exhaustion. The date remains fail-closed because stock quality is `partial_missing`: 5,182/5,199 codes, 4,587 valid rows and 595 missing-required rows (`usable_ratio=0.882...`). Revalidation retained the known-bad latch.
- Confirmed source cause: Tencent fallback emits `amount=None`; all 595 Tencent rows fail the shared required-field check. The repair's existing-code filter then excludes those invalid rows from subsequent attempts, and its own stale `stock_count >= 2000` exit rule returns `rc=0` despite a quality failure. The updater's postcondition correctly stopped after three attempts.
- THS added 129 rows to reach 349 slash-formatted 08-10 concept rows; ETF/mining are unchanged, with no outcome or formal-batch change. Final SHA-256: a_share `BB7A1D2F...9C07A8B7`, THS `837DB422...5F323B65`, ETF `EF1E5AB2...3105B8FC`, mining `9153F14D...DF215BBE`.
- Next requires a new source/sandbox/commit/manifest/backup cycle and new explicit production authorization. Evidence: `ops-1-20260810-r3-readonly-status.json`, `ops-1-20260810-r3-blocked.md`, and `ops-1-production-run/ops1-production-20260810-r3_20260811_150031.log`.

## 2026-08-11 OPS-1A.2 eligible-universe anti-shrink task start

- Goal: prevent a transiently truncated concept provider list from shrinking the formal eligible-universe denominator or being reported as complete.
- Order: freeze HEAD/four production hashes/08-07 and 08-10 expectations -> add deterministic red tests -> implement pre-write retention and overlap gates -> prove reject/idempotent/expansion/bootstrap/limit behavior in temporary SQLite -> run focused/full/static/hash/allowlist gates -> report and commit.
- First entry: the expectation write immediately after provider-list normalization in `backfill_adata_ths_concept_index_kline_60d.py`.
- Baseline: HEAD `9805084`; the production expectations are 349 codes for 2026-08-07 and 263 codes for 2026-08-10. Exact production hashes are recorded in `ops-1a2-baseline.md`.
- Maximum risk: an incomplete candidate universe can currently overwrite an existing larger denominator before any kline work, turning provider degradation into a false success. The gate must run before expectation, master, or kline writes.
- Boundary: source, tests, and temporary SQLite only. No network, offline update execution, Streamlit, OPS-1B work, or production database write is authorized.

## 2026-08-11 OPS-1A.2 eligible-universe anti-shrink complete

- Status: `ready_for_ops_1b_review`; stopped before OPS-1B.
- Commit: `a2461a92c71cc6ac3c0eccfed225250f952e117a` (`fix(ops): prevent concept universe shrinkage`), containing only the three allowlisted source/test files.
- Child contract: candidate universes are checked against every present same-day and latest earlier trusted reference using both count and overlap retention at 0.95. Regressions fail before expectation/master/kline content writes; passing same-day expectations are monotonic unions; bootstrap and limit modes are explicit.
- Parent contract: structured `eligible_universe_*` failure is preserved in `quality_failures`, ends deterministic retries, and forces final `ok=false` even if an old smaller expectation would make the rebuilt plan appear complete.
- Evidence: deliberate red fixtures reproduced expectation shrinkage and parent false-green; final focused suite 38 OK; full suite 312 OK with 2 skipped; py_compile and diff-check passed.
- Sandbox: local deterministic SQLite proved reject/idempotent/expand/new-day/limit/bootstrap behavior. It is not real-provider or production-recovery evidence.
- Production boundary: all four SHA-256 values exactly match task start. No production database, provider, offline update, Streamlit, latch, or OPS-1B action was touched.
- Report: `ops-1a2-report.md`; raw evidence: `ops-1a2-red.txt`, `ops-1a2-green.txt`, `ops-1a2-sandbox-proof.json`.

## 2026-08-12 OPS-HARDEN-1A.1 single daily write-control-plane closure

- Status: `ready_for_ops_harden_1b_review`; stopped before OPS-HARDEN-1B.
- `app_panel` automatic, manual, and formal-retry paths now enqueue the same one-day core worker. The render path no longer invokes legacy writers or waits on a provider/repair/formal child.
- The worker reports structured launch facts; background state uses a reclaimable claim/lease and atomic post-Popen replacement. Formal failure is visible and capped rather than re-labeled as waiting.
- Evidence: focused 154 tests OK; full 341 tests OK (2 skipped); py_compile and diff check passed; SQLite online-backup sandbox formal batch reused with zero growth; isolated browser showed metric coverage, persisted same-day A-E data, optional concept unavailable state, and zero console errors.
- All four production SHA-256 values remained unchanged. Report: `ops-harden-1a1-report.md` and `.json`.
