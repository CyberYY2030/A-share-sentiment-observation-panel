# v2.6 progress
- Goal: make formal and intraday A–E use the same capability-level deduplicated Top 20 shortlist.
- Frozen semantics: strict v2.6 A–D gates; E keeps its eligibility semantics.
- Task 0: source paths are readable; production root is readable; Python 3.11 is installed but execution is currently denied by the sandbox.
- Risk: isolated real-data replay may need an approved local Python invocation; no provider or production write is allowed.
- First entry: `mining/candidate_persistence.py` evaluator and shared shortlist boundary.
- Sandbox replay PID 47792: keep until v26 evidence completes; then stop only if still owned and clean its temporary logs.
- Validation: targeted 99 tests pass; full suite is blocked by five legacy R4/history expectations plus one frozen-fixture hash mismatch, recorded in BLOCKED.md.
