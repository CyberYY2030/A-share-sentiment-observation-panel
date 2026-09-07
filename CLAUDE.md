# Claude Code Guide

This repository is a local A-share market sentiment dashboard and mining scanner. Use this file as the first-pass map before reading implementation files.

For Codex and other agent workflows, keep `AGENTS.md` aligned with this file. `CLAUDE.md` is the Claude-specific orientation file; `AGENTS.md` carries the project-level agent rules and should be updated whenever commands, paths, or architecture boundaries change.

## Review Goal

When asked to review architecture or code quality, do not read the whole repository sequentially. First identify the slice being reviewed:

- Streamlit market dashboard and daily metrics.
- Daily mining pipeline, scanners, reports, and tests.
- Data backfill scripts and SQLite schema compatibility.
- Runtime path/data-output hygiene.

Then read only the files listed for that slice.

## Fast Orientation

- `app.py` is the Streamlit entrypoint. It only imports and calls `app_panel.main()`; project-local `.streamlit/config.toml` reserves port `8502` so the crypto dashboard can keep `8501`.
- `app_panel.py` is the current main dashboard implementation. It contains auto-backfill checks, SQLite access, market sentiment calculations, intraday snapshot handling, and Streamlit rendering in one large module.
- `backfill_orchestrator.py` starts long-running backfill scripts outside the Streamlit render thread and writes their output to `output/backfill_jobs/`.
- `offline_daily_update.py` is the offline daily updater and the canonical outer repair entrypoint. In core mode it owns one close-ready target day, stock/index quality gates, one repair child, and the provider-free formal batch that follows a successful market gate. `daily_job.ps1` is the schedulable wrapper; it writes logs under `output/backfill_jobs/`, the latest health summary to `output/health_latest.md`, and an optional Telegram digest when `TG_BOT_TOKEN`/`TG_CHAT_ID` or ignored `data/notify_config.json` is configured.
- `repair_market_day_akshare.py` is the bounded stock/index repair child. It classifies the true unresolved set, stages BaoStock → Sina → probed Eastmoney, isolates a timed-out symbol in a killable worker process, and checkpoints accepted rows in the parent. Do not launch it in parallel with the canonical outer updater.
- `runtime_paths.py` centralizes local path resolution for `data/`, root-level legacy DBs, metrics CSVs, and runtime directories.
- `run_daily.py` is the CLI entrypoint for the mining pipeline. Its full path refreshes basics, runs scanners, persists candidates/outcomes, and emits reports; `--date <day> --formal-only` is the bounded provider-free path for one formal v2.6 close batch.
- `mining/` is the newer modular package for strategy scanning and review UI.
- `tests/` covers the mining pipeline, runtime paths, and Streamlit helper behavior with synthetic SQLite fixtures.

## Architecture Map

Core dashboard flow:

1. `streamlit run app.py`
2. `app_panel.main()`
3. Resolve DB/CSV paths through `runtime_paths.build_runtime_paths()`.
4. Check recent date/domain gaps through `offline_daily_update.build_missing_update_plan()`.
5. Start `offline_daily_update.py` through `backfill_orchestrator.py` if local data is behind.
6. Read SQLite market/concept/ETF data.
7. Resolve the selected close date from actual stock/index row coverage.
8. Compute market light, daily sentiment, theme panel, intraday snapshot rows.
9. Render Streamlit dashboard and mining tabs.

Mining flow:

1. `python run_daily.py`
2. `mining.db.connect()` opens `mining_mvp.db` and attaches stock DB as `ash` and concept DB as `ths`.
3. `refresh_basics()` refreshes market-cap/listing basics when enabled.
4. Registered scanners in `mining/scanners/` produce candidates.
5. `backfill_outcomes()` writes forward returns to `outcomes`.
6. `generate_markdown_report()` and `generate_excel_report()` write reports to `output/`.

Formal-only close flow:

1. `python run_daily.py --base-dir . --date YYYY-MM-DD --formal-only`
2. Reuse the shared stock/index quality and universe contracts; quarantine rows stay excluded.
3. Evaluate the capability registry and atomically persist one `v2.6/close_final/complete` batch; v2.5 batches remain historical read-only records.
4. An identical fingerprint is a zero-growth reuse. No provider, legacy, outcome, watchlist, or report work runs in this mode.

Data stores:

- `a_share_mvp.db` / `ashare_mvp.db`: A-share and index daily kline source.
- `ths_concept.db`: THS concept kline/name data.
- `etf_mvp.db`: ETF equity/share data.
- `mining_mvp.db`: strategy runs, candidates, outcomes, stock basics.
- `daily_metrics_last40.csv`: dashboard metrics output.
- `daily_matrics_last40.csv`: misspelled legacy compatibility output.

## Read First By Task

Dashboard bug or UX review:

- `app.py`
- `app_panel.py`
- `runtime_paths.py`
- `mining/streamlit_tabs/tab_scanner.py` if the opportunity/mining tab is involved.

Mining strategy or pipeline review:

- `run_daily.py`
- `mining/db.py`
- `mining/scanners/__init__.py`
- `mining/scanners/rps_stock.py`
- `mining/scanners/rps_concept.py`
- `mining/scanners/momentum_breakout.py`
- `mining/backtest.py`
- `mining/reports.py`
- `tests/test_mining_pipeline.py`
- `tests/test_mining_ui.py`

Runtime/data-path review:

- `runtime_paths.py`
- `mining/db.py`
- `app_panel.py` path constants and DB discovery helpers.
- `tests/test_runtime_paths.py`

Backfill/data freshness review:

- `app_panel.py` startup backfill and close snapshot helpers.
- `offline_daily_update.py`
- `repair_market_day_akshare.py`
- `backfill_orchestrator.py`
- `backfill_baostock_hsA_60d_v2.py`
- `backfill_adata_ths_concept_index_kline_60d.py`
- `backfill_etf_equity_60d_v2.py`

Opportunity/date-selection review:

- `app_panel.py` functions `resolve_available_panel_close_date()` and `resolve_selected_opportunity_runtime()`.
- `mining/streamlit_tabs/tab_scanner.py`
- `run_daily.py`
- `docs/superpowers/specs/current-opportunity-selection-strategy.md`

## Usually Ignore

These paths are generated, archival, local-only, or unrelated to the current source of truth unless the user explicitly asks about them:

- `.venv/`
- `.idea/`
- `.playwright-cli/`
- `__pycache__/`
- `__tmp_*/`
- `data/`
- `output/`
- `csv_out/`
- `debug_probe/`
- `mvp_store/`
- `_db_schema_export/`
- `V1/`
- `old version/`
- `*.db`, `*.db-shm`, `*.db-wal`
- Generated CSV/XLSX/Markdown reports.

## Known Architectural Risks

- `app_panel.py` is a monolith. It mixes data freshness checks, DB schema detection, calculations, live quote adapters, persistence, and UI rendering. This is the main bottleneck for maintainability and review speed.
- There are two eras of code: older root-level scripts and the newer `mining/` package. Treat `mining/` as the cleaner direction unless the user asks for legacy-script behavior.
- Runtime data can live in both root and `data/` for backward compatibility. Always verify `runtime_paths.py` before changing DB or CSV paths.
- Market-data providers (`adata`, `akshare`, `baostock`) are network/runtime dependent. Unit tests mostly use local synthetic SQLite fixtures and should not require live market APIs.
- Startup data freshness is split into two layers: `app_panel.py` detects visible gaps and launches a background job; `offline_daily_update.py` owns the actual gap plan and domain-specific repair commands.
- For close-mode date switching, do not trust only a DB max date. The panel close date is resolved from actual stock/index row coverage so indicator panels and opportunity mining stay on the same selectable day.
- The repo may contain a dirty working tree. Inspect `git status --short` before editing and do not revert unrelated user changes.

## Review Protocol

For architecture/code audit, produce findings in this order:

1. High-impact correctness or data-integrity risks.
2. Maintainability bottlenecks that slow future changes.
3. Test gaps that would hide regressions.
4. Concrete refactor plan with smallest safe steps.

Do not propose a broad rewrite first. Prefer narrow extraction seams, especially around:

- DB/path resolution.
- Market-data adapters.
- Sentiment metric calculation.
- Streamlit rendering.
- Backfill orchestration.

## Verification Commands

Use the smallest relevant check before claiming success:

```powershell
python -m unittest
```

For a focused mining check:

```powershell
python -m unittest tests.test_mining_pipeline tests.test_mining_ui tests.test_runtime_paths
```

For syntax-only sanity:

```powershell
python -m py_compile app.py app_panel.py runtime_paths.py backfill_orchestrator.py offline_daily_update.py repair_market_day_akshare.py run_daily.py
```

For the schedulable daily update wrapper:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\daily_job.ps1 --dry-run --asof 2026-04-22 --days 1 --timeout-sec 30
```

## Communication

- Default to Chinese for explanations.
- Keep code, commands, file paths, and identifiers in English.
- Lead with the conclusion and concrete next step.
- Explain how a choice affects product usability or future modification speed.

## Long-Running Guardrails

- For live data fetching, remote APIs, or bug reproduction that can block, do not keep retrying the same failing path indefinitely.
- Stock repair uses a consecutive provider-call circuit: one timed-out stock code is isolated and left unresolved, later codes continue after a worker restart, success resets the counter, and only three consecutive call errors switch the remaining set to the next provider.
- Parent-owned SQLite checkpoints run every 200 accepted rows. Resume from the persisted unresolved set; never treat all existing rows as complete, never use `stock_count >= 2000` as success, and never throw away good checkpoints after a later timeout.
- Tencent stock fallback is invalid when `amount=None`; reject it before persistence. Tencent index behavior is separate and must not be generalized to stocks.
- Use one outer updater invocation and one stock worker at 0.45 seconds between request starts. Lowering request frequency cannot repair deterministic field mapping, stale completion criteria, or a worker lifecycle that cannot be killed.
- If two different interfaces have both failed and the remaining path is high-cost, slow, or likely to repeat the same failure mode, stop and report the concrete error instead of continuing blind retries.
- For long-running commands, set an explicit timeout whenever feasible and prefer short probe runs before full-range backfills.
- If a command appears stuck, is making no observable progress, or leaves orphan child processes, stop it and report what was learned from the failure.
- Treat task-created processes and temporary artifacts as owned resources. Record their PID/path at launch; at handoff either stop/delete them or explicitly state why they remain.
- Validate command line, parent process, port, and task ownership before stopping anything. Never kill all `python.exe` or `node.exe`; current data writers and shared Codex/MCP services must remain untouched.
- Reproducible browser profiles, acceptance database copies, and tool caches may be cleaned after the owner exits. Production logs, databases, screenshots/YAML, hashes, and reports are audit evidence and require a separate deletion decision.
- When data cannot be fetched after bounded attempts, report:
  - which interfaces were tried
  - how many attempts were used
  - the concrete last error for each failed interface
  - what local fallback or partial result was applied, if any
