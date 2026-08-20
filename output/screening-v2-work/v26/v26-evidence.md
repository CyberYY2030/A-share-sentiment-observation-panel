# v2.6 isolated evidence

- Four production databases were copied by SQLite online backup into `sandbox/`; every source SHA-256 matched before and after the copy.
- Requested replay: 20 usable sessions ending 2026-08-17. Result: blocked partial, with zero v2.6 complete batches. The report intentionally contains no inferred funnel or candidate counts.
- The partial run exposed `compression_launch: KeyError: activity_pct_min`; the missing formal parameter was corrected and its hard activity/amount gate is covered by synthetic tests.
- The replay worker PID 47792 was task-owned, made no progress for about three minutes, and was stopped. Its zero-byte temporary logs were removed; sandbox databases remain as audit evidence and are not staged.
- Production activation, provider calls, and production database writes were not performed.
