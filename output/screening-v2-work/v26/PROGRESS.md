# v2.6 progress
- Goal: make formal and intraday A–E use the same capability-level deduplicated Top 20 shortlist.
- Frozen semantics: strict v2.6 A–D gates; E keeps its eligibility semantics.
- Task 0 repair reproduction: intraday reads an unwritten diagnostics key; C lacks a 0.75 shrink gate and has a degenerate reclaim gate; B loses existing subtype evidence; C truncates before capability aggregation.
- Risk: isolated real-data replay may need an approved local Python invocation; no provider or production write is allowed.
- First entry: `mining/candidate_persistence.py` evaluator and shared shortlist boundary.
- Historical baseline: prior full run was 364 tests, 4 failures, 1 error, 2 skipped; no provider/production investigation will be repeated.
- Repair result: local calendar, C gates, B idempotency and R4/history migration landed; 126 targeted tests and compilation pass; replay checkpoint is 1/20 and full-suite completion signal was unavailable.
