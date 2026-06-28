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

On startup, the panel checks whether local market data is behind. If it finds recent gaps, it starts a background job through `backfill_orchestrator.py`, which runs `offline_daily_update.py` and writes logs under `output/backfill_jobs/`.

## Check And Repair Daily Data

Dry-run a recent missing-data check:

```powershell
python offline_daily_update.py --base-dir . --asof 2026-04-22 --days 10 --dry-run
```

Repair missing domains for the same window:

```powershell
python offline_daily_update.py --base-dir . --asof 2026-04-22 --days 10
```

Repair one market day with AkShare fallback when BaoStock cannot fill stock/index rows:

```powershell
python repair_market_day_akshare.py --db a_share_mvp.db --date 2026-04-22 --workers 8 --attempts-per-source 3
```

Run only opportunity mining and review outputs:

```powershell
python run_daily.py --base-dir . --range 2026-04-21 2026-04-22
```

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

External data providers are expected to fail sometimes. Treat these as operational cases, not reasons to rewrite the pipeline immediately.

- For BaoStock failures, first confirm the requested trade day and stock/index row coverage.
- For AkShare failures, check whether the failing interface returns empty data, times out, or changed columns.
- Use no more than 3 attempts per provider path in one repair flow.
- Switch to a fallback provider or report the exact failure after repeated identical errors.
- Avoid broad date-range repairs until a dry-run confirms the missing-domain window.

## Data Hygiene

- Do not commit local databases, generated reports, cache files, or background logs.
- Before changing DB or CSV locations, inspect `runtime_paths.py`.
- Keep `README.md`, `AGENTS.md`, `CLAUDE.md`, this runbook, and `docs/architecture.md` aligned when commands or data boundaries change.

