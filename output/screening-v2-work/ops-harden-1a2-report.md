# OPS-HARDEN-1A.2 — core worker lifecycle and 08-11 sandbox closure

- Status: `ready_for_ops_harden_1b_review`
- Start commit: `e4691a6f02325f67c2a4ae0ded796b56ce9474f1`
- Scope: source, existing tests, task-card/progress correction, and isolated SQLite copies only. `OPS-HARDEN-1B` was not entered.

## passed

- `app_panel.py` now makes the target-day decision from structured readiness and persisted lifecycle state. One resolved `(base_dir, target_day)` automatic open reuses the active exclusive claim; ordinary reruns do not bypass `next_retry_at`, sleep, or wait for a child.
- `market_failed` after three in-worker market attempts is terminal and requires a manual retry. `formal_failed` is terminal after one local formal attempt per worker and also requires a manual retry. An explicit retry increments `generation` once; inherited attempt counters cannot fall when a child reports a smaller value.
- `backfill_orchestrator.py` records and returns `target_day`, `generation`, `market_attempts`, `formal_attempt`, `phase`, `terminal`, `next_retry_at`, `last_error`, and `claim_id`. Terminal scheduler-log evidence takes priority over a reusable PID. A task over budget plus grace without terminal evidence becomes `orphaned_manual_intervention` and continues blocking a second writer.
- Publishing the active state after `Popen` is now fail-closed: the child is terminated and waited exactly once, ownership is cleared, and the launch result is `state_publish_failed`.
- `offline_daily_update.py` keeps the established one-day `stock/index` contract, a single 3600-second budget, up to three quality-aware market attempts in one worker, and one `run_daily.py --date DAY --formal-only` formal attempt. It does not run `--range`, concept, ETF, legacy, outcome, watchlist, or report work from the daily core path.
- Lifecycle regression proof: `tests.test_backfill_rules` covers ten formal-failure reruns with no new `Popen`, ten market-failure reruns with no second worker, future `next_retry_at`, monotonic attempt accounting, terminal-log/PID reuse, state-publish termination/wait, orphan blocking, one-generation manual retry, cross-date exclusive ownership, command parity, and the render-path static gate.
- Targeted validation: `python -m unittest tests.test_backfill_rules tests.test_mining_ui tests.test_mining_pipeline tests.test_selection_runtime` — `Ran 161 tests`, `OK`.
- Full validation: `python -m unittest` — `Ran 348 tests in 22.742s`, `OK (skipped=2)`.
- Static validation: `python -m py_compile app_panel.py backfill_orchestrator.py offline_daily_update.py tests/test_backfill_rules.py` and `git diff --check` both exited 0.

### 08-11 isolated SQLite online-backup acceptance

- Sandbox: `output/screening-v2-work/ops-harden-1a2-sandbox-20260812-1531`. Four databases were created with SQLite online backup. Source/copy table lists and row counts matched before fixture setup; the initial copy had no 2026-08-11 stock/index rows and no 2026-08-11 formal batch.
- A deterministic **control-flow fixture only** copied the 2026-08-10 stock and four required-index rows to 2026-08-11. `fixture-manifest.json` records `not_real_market_data=true`; it is not provider or production-market evidence. It yielded raw `usable_with_quarantine` with 5,018 valid and 164 quarantined stock rows and all four index codes. Sandbox-only revalidation cleared the inherited latch through the existing API.
- The single core worker invocation was `python offline_daily_update.py --base-dir <sandbox> --asof 2026-08-11 --target-day 2026-08-11 --domains stock index --timeout-sec 3600 --no-health`. It made zero provider calls and launched exactly one formal child: `python run_daily.py --base-dir <sandbox> --date 2026-08-11 --formal-only`.
- First formal result: batch `id=4`, `trade_date=2026-08-11`, `definition_version=v2.5`, `mode=close_final`, `status=complete`, fingerprint `e02d53fbca06b27da153ef708888f3cd733b2d4338976698f44d7243eb7d40f4`; exactly six formal runs and no partial 08-11 batch. First-write deltas: `selection_batches +1`, `strategy_runs +6`, `candidates +319`, `pullback_state_history +297`.
- Identical rerun: `selection_batches/strategy_runs/candidates/pullback_state_history = +0/+0/+0/+0`.

### Browser acceptance (isolated and read-only)

- No 08-11 market rows: page selected/effective/formal stayed at 2026-08-10. Screenshot: `output/screening-v2-work/ops-harden-1a2-market-missing-browser-20260812-1550/browser-08-11-market-missing-rolls-back-0810-visible.png`.
- 08-11 market-ready fixture without formal batch: page stayed at 2026-08-10 and visibly reported `waiting_for_formal_batch`. Screenshot: `output/screening-v2-work/ops-harden-1a2-preformal-browser-20260812-1545/browser-08-11-formal-missing-rolls-back-0810-visible.png`.
- Complete 08-11 batch: page visibly reported `Core worker: target=2026-08-11; market=market_ready; formal=complete` and `selected=2026-08-11 / effective=2026-08-11 / formal trade_date=2026-08-11`; the Metric coverage caption and formal A-E panel rendered. Screenshot: `output/screening-v2-work/ops-harden-1a2-sandbox-20260812-1531/browser-08-11-complete-visible.png`.
- Browser console errors: 0. The Streamlit runtime emitted its own telemetry requests; these are not market-data-provider requests. The isolated core worker made zero provider calls and no browser action invoked a market provider.

### Production integrity

| database | SHA-256 before | SHA-256 after |
|---|---|---|
| `a_share_mvp.db` | `1E83DA69B6BC3284AE3062524FB4EF789AF68D67CC3565D6C7FADCEFDD4AE51D` | unchanged |
| `mining_mvp.db` | `B390562F13301CDEAA07A99F009F292EB8537885691C5EF5B6707D9F43C0278E` | unchanged |
| `ths_concept.db` | `837DB4226AB1AB1F807163D8CEC99AB35211E1D64371E682B7DE30315F323B65` | unchanged |
| `etf_mvp.db` | `721ED4A41372B582189E93BD9363F343FC4EB9314AAED9F5A9A3005251A1BF28` | unchanged |

## failed

- none.

## environment-blocked

- none.

## Lessons

- Correction appended to the 1A.1 record: a one-entry UI control plane still needs durable cross-process terminal evidence and explicit retry ownership to prevent reused-PID and deterministic-formal-failure loops.
- Effective method: use a real SQLite online backup plus an explicitly non-real control-flow fixture to prove lifecycle and idempotency without converting fixture output into a production-data claim.
