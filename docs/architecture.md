# Architecture Overview

This project is a local A-share market sentiment dashboard with an opportunity-mining pipeline. It combines local SQLite market data, daily/offline repair scripts, Streamlit rendering, and a modular scanner package.

## Runtime Flow

Dashboard startup:

1. `streamlit run app.py`
2. `app.py` calls `app_panel.main()`.
3. `app_panel.py` resolves runtime paths through `runtime_paths.build_runtime_paths()`.
4. The panel checks stock, index, concept, ETF, and mining data freshness.
5. If recent local data is behind, `backfill_orchestrator.py` starts `offline_daily_update.py` in the background.
6. The panel reads local SQLite and CSV data, resolves the selectable close date, computes sentiment panels, and renders Streamlit UI.
7. The opportunity-mining tab reads persisted close candidates or uses latest quotes for display-time opportunities when the runtime mode allows it.

Daily/offline update:

1. The panel resolves one `latest_completed_trade_day`; a current intraday session is not treated as a missing close day.
2. `offline_daily_update.py` runs one core `stock index` invocation for that target and owns the total wall-clock budget.
3. `repair_market_day_akshare.py` classifies the true unresolved stock set and stages BaoStock → Sina → Eastmoney only after a same-run Eastmoney probe succeeds.
4. Each active provider request lives in a killable worker process. One timed-out symbol is isolated; later symbols resume in a new worker. Three consecutive provider-call errors open the circuit, while a complete row resets it.
5. The parent persists accepted rows every 200 rows and recomputes the unresolved set before the next provider or invocation.
6. After the shared stock quality postcondition and four-index gate pass, the updater runs only `run_daily.py --date <day> --formal-only` for formal screening.
7. Concept and ETF are optional, independently visible domains; they do not block the core stock/index/formal result.
8. Logs and reports are written under `output/`.

The earlier 180/240-second startup budget deadlock, cross-day `--range` side effects, daemon-thread semaphore saturation, fixed `stock_count >= 2000` success rule, and single-code timeout source-wide abort are closed regressions. Startup has global writer exclusion and a failed target requires explicit manual retry, preventing Streamlit reruns from launching duplicate writers. Isolated screening mode remains the correct path for byte-stable read-only browser acceptance.

Opportunity-mining pipeline:

1. `run_daily.py` opens `mining_mvp.db` through `mining.db.connect()`.
2. The connection attaches stock data as `ash` and concept data as `ths`.
3. `mining.universe.build_universe()` builds the filtered stock universe.
4. Registered scanners in `mining/scanners/` produce candidates.
5. `mining.db.save_candidates()` persists results in `candidates`.
6. `mining.backtest.backfill_outcomes()` updates forward returns in `outcomes`.
7. `mining.reports` writes Markdown and Excel outputs.

Formal-only close path:

1. `run_daily.py --date <day> --formal-only` accepts one explicit close-ready date and skips provider, legacy, outcome, watchlist, and report work.
2. The shared stock/index quality contract builds a universe from valid rows only; quarantined and absent stocks remain excluded.
3. `mining.capabilities.CAPABILITY_REGISTRY` evaluates the six formal A-E strategy ids.
4. `persist_close_final_batch()` writes one atomic `v2.5/close_final/complete` batch. The input fingerprint makes an identical rerun a zero-growth reuse.
5. Streamlit reads that exact complete batch. Optional concept failure is rendered as degraded/unavailable and does not replace the core screening date or results.

## Data Stores

- `a_share_mvp.db` / `ashare_mvp.db`: A-share and index daily kline source.
- `ths_concept.db`: THS concept daily kline and concept names.
- `etf_mvp.db`: ETF share and daily data.
- `mining_mvp.db`: strategy runs, candidates, outcomes, and stock basics.
- `daily_metrics_last40.csv`: market sentiment output.
- `daily_matrics_last40.csv`: legacy misspelled compatibility output.
- `intraday_metrics_latest.csv`: latest intraday snapshot output.
- `output/`: logs, reports, Excel exports, and Streamlit/background job output.

`runtime_paths.py` is the source of truth for choosing root-level legacy files versus newer `data/` paths.

## Product Boundaries

- The dashboard is optimized for local use, not multi-user deployment.
- The panel can use intraday/latest quotes for display, but historical research and backtesting still use persisted close-data candidates.
- A close-mode panel date must be based on actual stock/index row coverage, so sentiment panels and opportunity mining stay aligned.
- Core screening readiness depends on stock, four indices, and a complete formal batch. Concept and ETF readiness remain separately visible optional-domain states.
- The right-side follow-up panel may label results as a recent close rather than strict T-1 if the exact previous candidate date is missing.

## Main Risks

- `app_panel.py` is a large module and mixes data IO, calculation, provider adapters, persistence, and UI.
- External data providers can return empty payloads, time out, or change schemas. Structured `PROVIDER_SUMMARY`, row-quality postconditions, process-isolated timeouts, and durable checkpoints keep those failures bounded without losing accepted work.
- Runtime data compatibility spans root-level DBs and `data/`. Path changes can silently break old local setups.
- Snapshot display results and persisted close candidates use different data boundaries. UI labels must stay explicit when these diverge.

## Preferred Refactor Direction

- Keep scanner, report, and backtest changes inside `mining/` where possible.
- Extract from `app_panel.py` only when it reduces real review cost: path resolution, provider adapters, sentiment calculations, Streamlit rendering, or backfill orchestration.
- Add focused tests with synthetic SQLite fixtures before touching data-selection, date-resolution, scanner, or repair behavior.
