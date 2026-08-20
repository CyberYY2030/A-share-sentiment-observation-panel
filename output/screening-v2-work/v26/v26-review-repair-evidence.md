# v2.6 review-repair evidence

Status: `changes_incomplete`; no production activation.

## Source integrity

The 2026-08-20 online SQLite backup used only read connections. SHA-256 before and after matched for all source databases:

- `a_share_mvp.db`: `77C4A00660FE9BC398A69A5DFDA22FDDAE42FAEF0E4AF11591C34187F35A6464`
- `ths_concept.db`: `837DB4226AB1AB1F807163D8CEC99AB35211E1D64371E682B7DE30315F323B65`
- `etf_mvp.db`: `721ED4A41372B582189E93BD9363F343FC4EB9314AAED9F5A9A3005251A1BF28`
- `mining_mvp.db`: `EE2FD0419B3A0396EA471BE364BBF63E8D543EB3C7AFFDC8169A7AFE52D6E6CC`

## Replay

A new sandbox completed the bounded `2026-07-20 --formal-only` smoke. Its v2.6 batch is `complete`; A=20, B=20 (compression=4 and momentum=16, unique codes=20), C=1, D=5, E=20.

The required sequential 20-session runner stopped after the first checkpoint without a nonzero stdout/stderr diagnostic. No later date ran and no process remains. Evidence is therefore exactly `1/20`, not a completed replay. Sandbox databases and the single-day logs are retained locally but excluded from the commit.
