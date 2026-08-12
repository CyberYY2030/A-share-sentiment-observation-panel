# OPS-HARDEN-1A.1 report

Status: `ready_for_ops_harden_1b_review`

Scope: source, tests, and `output/screening-v2-work/ops-harden-1a1-sandbox-20260812-1445` only. No production database, live provider repair, production formal batch, schema, strategy formula, threshold, DTO, intraday snapshot contract, or historical batch was changed.

## passed

- Frozen start: `codex/screening-v2` at `f59a93407d98290bd735405860dc5692490ac5e2`; unrelated pre-existing `.claude/` and documentation changes were retained.
- One testable `target_day_core_decision()` now dispatches only one target-day core worker: complete screening does nothing; missing market runs repair then formal; market-ready/formal-absent runs only `--formal-only`; a formal failure is `formal_failed`, with a capped three automatic attempts and an explicit retry button.
- Automatic checkbox, manual update, and formal retry use `enqueue_core_update()` with the same command and exclusive key. AST gate found no calls from `app_panel.main()` to `maybe_backfill_all`, `sync_mining_history_after_close_update`, `repair_latest_trade_day_daily`, or blocking `run_cmd`.
- Core child results are structured as `started`, `returncode`, `timed_out`, `error_kind`, `output`, and `allowance`. `FileNotFoundError`, `PermissionError`, and `OSError` are no-launch results; only a child known to have started can lead to an observed-bad-session marker.
- Background reservation now has `claim_id`, UTC claim/lease timestamps, stale/corrupt/invalid-time recovery, live-PID reuse, and atomic post-Popen state publication with `os.replace`.
- New regressions cover the formal-only decision path, retry/cap behavior, no-launch diagnostics, started-timeout bad-session marking, stale/fresh/corrupt/invalid lease recovery, live PID reuse, identical entry commands, forbidden command terms, and render-path AST gate.
- Focused suites: `tests.test_backfill_rules tests.test_mining_ui tests.test_mining_pipeline tests.test_selection_runtime` = 154 passed.
- Full suite: `python -m unittest` = 341 passed, 2 skipped.
- `py_compile` passed for all modified Python files; `git diff --check` passed.
- SQLite online backups of all four source databases formed the sandbox. Source/backup table counts matched. Mining source and sandbox both have `selection_batches=3`, `strategy_runs=2548`, `candidates=27746`, `pullback_state_history=101`.
- Sandboxed `run_daily.py --date 2026-08-10 --formal-only` returned `status=complete`, `batch_id=2`, `reused=true`, six formal strategy runs, and no table growth.
- Read-only browser evidence used `SCREENING_BASE_DIR` sandbox with a fixed 2026-08-11 acceptance clock. It shows `return_valid=5179`, `amount_valid=5018`, `hot_valid=5015`, `expected=5182`, `excluded=167`; `selected=effective=formal=2026-08-10`; A-E persisted statuses; and `Concept domain unavailable: unavailable (eligible_universe_regression)`. Console errors were 0. Screenshots: `browser/ops-harden-1a1-readonly-complete.png` and `browser/ops-harden-1a1-selection-visible.png`.
- Production SHA-256 remained unchanged:
  - `a_share_mvp.db`: `1e83da69b6bc3284ae3062524fb4ef789af68d67cc3565d6c7fadcefdd4ae51d`
  - `mining_mvp.db`: `b390562f13301cdeaa07a99f009f292eb8537885691c5ef5b6707d9f43c0278e`
  - `ths_concept.db`: `837db4226ab1ab1f807163d8cec99ab35211e1d64371e682b7de30315f323b65`
  - `etf_mvp.db`: `721ed4a41372b582189e93bd9363f343fc4eb9314aaed9f5a9a3005251a1bf28`

## failed

- None remaining. The first full run exposed one outdated fixture using `child_started`; it was migrated to the structured `started` contract and the final full run passed.

## environment-blocked

- None. Browser emitted 36 visualization warnings about empty chart extents, but console errors were 0 and they do not affect the core-worker control plane.

## Lessons

- A process return code cannot establish whether a child launched. Persisting launch state as a structured fact prevents both false bad-session diagnostics and unsafe retry behavior.
- A short PID-less lease closes the Streamlit rerun race while retaining recovery after a crashed launcher or malformed state file.
