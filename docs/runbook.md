# Runbook

This runbook covers local setup, dashboard startup, daily repair, focused verification, and common failure handling.

## Setup

Install dependencies:

```powershell
pip install -r requirements.txt
```

The project uses local SQLite databases and generated outputs. These are intentionally not committed:

- `*.db`, `*.db-shm`, `*.db-wal`
- `data/`
- `output/`
- `csv_out/`
- `debug_probe/`

## Start The Dashboard

```powershell
streamlit run app.py
```

The dashboard entrypoint is `app.py`; implementation lives in `app_panel.py`.

On startup, the panel resolves the latest completed close day. If the core stock/index/formal result is behind, it starts one globally exclusive background job through `backfill_orchestrator.py`. That job runs a target-day `stock index` update and then a single `--formal-only` batch after the market gate passes. Optional concept/ETF gaps do not block core screening. A failed target is latched for explicit manual retry; Streamlit reruns do not start another writer.

The close-ready cutoff is 17:30 China time. On a trading day, starting or refreshing after 17:30 targets that day's close; before 17:30 it targets the previous completed trading day. The same previous close remains the target on the next morning, so next-day startup is a recovery opportunity rather than a requirement. A panel left open before the cutoff needs a Streamlit rerun/refresh after 17:30 to evaluate the new target. The displayed close date advances only after stock quality, four indices, and the same-day formal batch are all ready.

For a provider-free, no-backfill review of the current local databases, start a process-local isolated view:

```powershell
$env:SCREENING_BASE_DIR = (Resolve-Path ".\output\screening-v2-work\panel-acceptance-copy").Path
$env:SCREENING_ACCEPTANCE_NOW_CN = "2026-08-11T18:00:00+08:00"
streamlit run app.py --server.address 127.0.0.1 --server.port 8532
```

This mode disables automatic repair and provider calls. `SCREENING_BASE_DIR` must point to a validated SQLite online-backup copy, not the repository root, when byte-stable production review matters. Use an acceptance time matching the close date you need to inspect. A normal startup is suitable for daily use and may start the one core background writer when the latest close is genuinely missing; use isolated mode for review that must cause zero production writes.

## Check And Repair Daily Data

Dry-run a recent missing-data check:

```powershell
python offline_daily_update.py --base-dir . --asof 2026-04-22 --days 10 --dry-run
```

Repair missing domains for the same window:

```powershell
python offline_daily_update.py --base-dir . --asof 2026-04-22 --days 10
```

Run the bounded core contract for one target close day:

```powershell
python offline_daily_update.py --base-dir . --asof YYYY-MM-DD --target-day YYYY-MM-DD --domains stock index --no-health --timeout-sec 5400 --formal-reserve-sec 300 --market-workers 1 --market-min-request-interval-sec 0.45
```

Use one invocation only. It stages providers internally and runs the formal batch only after the stock/four-index quality gate passes. Do not start `repair_market_day_akshare.py` beside it, do not raise worker count to chase speed, and do not run `run_daily.py --range` for the close-final operational path.

Run only opportunity mining and review outputs:

```powershell
python run_daily.py --base-dir . --range 2026-04-21 2026-04-22
```

Generate only the formal v2.5 A-E close batch for one close-ready date:

```powershell
python run_daily.py --base-dir . --date YYYY-MM-DD --formal-only
```

Use `--formal-only` for a bounded single-day formal-batch operation. It does not call market-data providers or run legacy scanners, outcome backfills, watchlists, or reports. Confirm stock/index readiness first; a missing optional concept domain may produce a visible degraded state but does not invalidate the core screening result. Repeating the same input must reuse the fingerprint with zero batch/run/candidate growth.

## Verification

Run the full local unit test suite:

```powershell
python -m unittest
```

Focused mining and UI checks:

```powershell
python -m unittest tests.test_mining_pipeline tests.test_mining_ui tests.test_runtime_paths
```

Backfill and retry-rule checks:

```powershell
python -m unittest tests.test_backfill_rules
```

Syntax sanity for main scripts:

```powershell
python -m py_compile app.py app_panel.py runtime_paths.py backfill_orchestrator.py offline_daily_update.py repair_market_day_akshare.py run_daily.py
```

## Failure Handling

### 2026-08-12 retry incident: permanent record

The repeated failures were caused by multiple deterministic defects, not by request frequency alone:

- Tencent stock fallback persisted `amount=None`; all 595 such rows failed `missing_required_field`.
- Retry planning treated any existing row as complete, so those invalid rows could not be fetched and replaced.
- The child returned success at `stock_count >= 2000`, while the shared quality postcondition correctly rejected the day.
- The former daemon-thread/semaphore limiter could not kill a slow request; one blocked call held capacity and made later attempts look like provider-wide saturation.
- A later circuit breaker improved that lifecycle but initially treated one 12-second symbol timeout as a source-wide failure. A 20/20 probe could not prove the absence of a rare timeout in a 5,000-code sequence.
- Without durable checkpoints, a late timeout could discard already accepted rows and force another large retry.

The successful contract is now:

1. Classify rows with the shared quality function. Invalid or missing rows remain retryable; reject incomplete Tencent stock rows before persistence.
2. Run one outer updater and one stock worker, with a 0.45-second request-start interval. Provider order is BaoStock → Sina → Eastmoney after a same-run probe.
3. Put each active provider request in a killable process. A one-code timeout leaves that code unresolved, flushes accepted rows, terminates the worker, and resumes later codes in a new worker.
4. Count only consecutive provider-call errors toward the circuit. A complete row resets the count; the third consecutive error transfers the exact unresolved tail to the next provider.
5. Persist parent-owned SQLite checkpoints every 200 accepted rows. Every provider pass and later invocation recomputes the true unresolved set, so good work is retained and guard-rejected rows remain eligible for fallback.
6. Decide success from `clean|usable_with_quarantine` plus all four indices, then run only the single-day `--formal-only` batch. `rc=0`, nonzero row count, or a fixed count threshold is never sufficient.

Operator response after a failed invocation:

- Do not immediately rerun the same command or reduce frequency again.
- Read the last `PROVIDER_SUMMARY`: `codes_in`, `timeout_codes`, `worker_restarts`, `unresolved`, stop reason, and maximum consecutive errors.
- Verify the unresolved set and persisted checkpoint growth. Resume once from that state only after the summary shows a credible remaining provider path.
- Keep `provider_empty`, `provider_invalid`, and `continuity_guard_rejected` codes explicitly unresolved/quarantined. A day may be usable without claiming 100% stock coverage.
- If the shared market gate fails, stop before formal mining. Preserve the diagnostic and the good checkpoints; do not delete latches or rows manually.
- Avoid broad date-range repairs until a dry-run confirms the missing-domain window.

## Data Hygiene

- Do not commit local databases, generated reports, cache files, or background logs.
- Before changing DB or CSV locations, inspect `runtime_paths.py`.
- Keep `README.md`, `AGENTS.md`, `CLAUDE.md`, this runbook, and `docs/architecture.md` aligned when commands or data boundaries change.

### Agent task cleanup

At the end of a debugging, browser-acceptance, or data-repair task:

1. Re-read the process command line, parent PID, listening port, and task-created PID record. Stop only the verified task-owned tree.
2. Confirm current production writers and shared Codex/MCP services are excluded.
3. Remove task-specific browser temp profiles and reproducible caches after process exit.
4. Preserve production DBs, repair logs, reports, hashes, screenshots/YAML, and other audit evidence. Large evidence-like files are reported with exact paths/sizes and deleted only under a separate decision.
5. Completion reporting includes stopped PIDs, reclaimed memory/disk, retained artifacts, and any intentionally persistent user-facing server.
