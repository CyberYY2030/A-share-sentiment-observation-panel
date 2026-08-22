# Screening v2.6 PROD-1 production activation

Authorization date: 2026-08-22
Accepted code: `b2f9105de165f9d8e24dbdfed22330e23917ebb6`
Accepted chain: `944ac51 -> 3b25fab -> 154f744 -> b2f9105`
Code acceptance: 382 tests passed, 2 original skips; 20-session evidence accepted with an observability gap.

## Objective

Publish one reversible `v2.6/close_final` formal-only canary for `2026-08-17` in the production checkout. Confirm the panel reads that completed batch. This card does not repair 2026-08-18 through 2026-08-21 market data.

## Guardrails

- Preserve the existing tracked `output/screening-v2-work/v26/v26-evidence.json` modification; do not stage, modify, or commit it.
- Before any production write, record SHA-256 and `quick_check` for `a_share_mvp.db`, `etf_mvp.db`, `ths_concept.db`, and `mining_mvp.db`; require selection batch schema `ready`.
- Create SQLite online backups of `a_share_mvp.db` and `mining_mvp.db` in the PROD-1 evidence directory. Source and backup must have `quick_check=ok` and identical logical tables and row counts.
- Only `mining_mvp.db` may change, solely through `python run_daily.py --base-dir . --date 2026-08-17 --formal-only`.
- Do not write `data/a_share_trade_calendar.json`, fetch providers, repair 2026-08-18 through 2026-08-21, run range/history/outcome/watchlist/report work, push, or change global configuration.

## Acceptance

- The canary yields one `v2.6/close_final/complete` batch with A-E each at most 20, B deduplicated by `sec_code`, C ranks continuous, and unchanged v2.5 history.
- A same-command rerun reuses the batch and fingerprint with zero growth in selection batches, strategy runs, and candidates.
- New-process reader/UI parity shows 2026-08-17 and the v2.6 completed batch; Streamlit is stopped after screenshot capture.
- Dry-run only documents the 2026-08-18 through 2026-08-21 backlog.

## Completion labels

Use only `production_v26_canary_verified`, `blocked_no_write`, or `production_v26_canary_partial`. Preserve recovery backups, logs, screenshots, hashes, and the PROD-1 report for fixed audit review.
