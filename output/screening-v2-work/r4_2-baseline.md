# R4.2 task-0 baseline

- HEAD: `589af36ebeaaef09bbf43c38684e499455baa01a`; `git merge-base --is-ancestor 589af36 HEAD` exited `0`.
- `git diff --binary -- run_daily.py` SHA-256: `B30A5D9E553F30AA5D0AA2CB57421C0E80D06853B01110CAD6F7F3E010B6A08C`.
- The four production databases were copied exactly once into `r4_2-db-sandbox/`; every source/copy SHA-256 matched the task card.

## Production and sandbox-copy SHA-256

| Database | Source SHA-256 | Copy SHA-256 |
|---|---|---|
| `a_share_mvp.db` | `E8181DECF433B72ED28BB6849D02E6E06B9C1E23DA72C377FDDA2A4C3250FD63` | `E8181DECF433B72ED28BB6849D02E6E06B9C1E23DA72C377FDDA2A4C3250FD63` |
| `etf_mvp.db` | `4C2585BBFBA57214C1455FFB5F3402D555BCB8C231DF5B0F081BCD2DA3E259D4` | `4C2585BBFBA57214C1455FFB5F3402D555BCB8C231DF5B0F081BCD2DA3E259D4` |
| `mining_mvp.db` | `2C1D71550C27CD28E7F26B7AE5513736964F3AA05A6452D683B5F4EF91F778EB` | `2C1D71550C27CD28E7F26B7AE5513736964F3AA05A6452D683B5F4EF91F778EB` |
| `ths_concept.db` | `47E65F2F1D3C245FBE6C879C9FFA9E4F103FEDC79693FEAE160DD4D0EFBDB45F` | `47E65F2F1D3C245FBE6C879C9FFA9E4F103FEDC79693FEAE160DD4D0EFBDB45F` |

## Preserved initial `git status --short`

```text
 M .gitignore
 M AGENTS.md
 M CLAUDE.md
 M backfill_etf_equity_60d_v2.py
 M knowledge/backlog.md
 M mining/backtest.py
 M offline_daily_update.py
 M repair_market_day_akshare.py
 M run_daily.py
 M runtime_paths.py
 M tests/test_backfill_rules.py
 M tests/test_base_breakout.py
 M tests/test_mining_pipeline.py
 M tests/test_strong_trend.py
?? .claude/
?? daily_job.ps1
?? docs/SCREENING-SPEC.md
?? docs/superpowers/specs/2026-07-03-pipeline-reliability-task-cards.md
?? docs/superpowers/specs/2026-07-18-intraday-hardening-task-card.md
?? docs/superpowers/specs/2026-07-19-panel-visibility-task-cards.md
?? docs/superpowers/specs/2026-08-06-screening-v2-quality-review.md
?? docs/superpowers/specs/2026-08-06-screening-v2-task-cards.md
?? docs/superpowers/specs/2026-08-07-screening-v2-runtime-recovery-task-card.md
?? docs/superpowers/specs/2026-08-08-screening-v2-r4-1-completion-task-card.md
?? docs/superpowers/specs/2026-08-08-screening-v2-r4-2-closure-task-card.md
?? knowledge/cards/setups/EXIT-abandon-ladder.md
?? knowledge/cards/setups/EXIT-sell-by-entry.md
?? mining/notify.py
?? tests/test_notify.py
```
