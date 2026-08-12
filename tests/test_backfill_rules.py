import unittest
import datetime as dt
import tempfile
from pathlib import Path
from unittest import mock


class BackfillRuleTests(unittest.TestCase):
    def test_close_ready_target_excludes_pre_cutoff_current_day_and_asof_cannot_bypass(self) -> None:
        from offline_daily_update import resolve_target_close_date

        before_close = dt.datetime(2026, 8, 11, 17, 29, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        after_close = dt.datetime(2026, 8, 11, 17, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))

        self.assertEqual(resolve_target_close_date(now_cn=before_close), "2026-08-10")
        self.assertEqual(resolve_target_close_date(now_cn=after_close), "2026-08-11")
        self.assertEqual(resolve_target_close_date("2026-08-12", now_cn=before_close), "2026-08-10")
        self.assertEqual(resolve_target_close_date("2026-08-07", now_cn=after_close), "2026-08-07")

    def test_one_invocation_budget_uses_remaining_time_and_preserves_reserve(self) -> None:
        from offline_daily_update import _run_with_remaining_budget

        calls: list[int] = []

        def runner(_cmd, *, cwd, timeout_sec):
            del cwd
            calls.append(timeout_sec)
            return 0, "ok"

        with mock.patch("offline_daily_update.time.monotonic", return_value=100.0):
            rc, output, allowance = _run_with_remaining_budget(
                ["worker"],
                cwd=".",
                deadline=1_000.0,
                reserve_seconds=120,
                runner=runner,
            )
        self.assertEqual((rc, output, allowance), (0, "ok", 780))
        self.assertEqual(calls, [780])

    def test_market_repair_revalidates_raw_success_before_a_second_child(self) -> None:
        from offline_daily_update import run_offline_update

        initial_plan = {
            "ok": False,
            "expected_dates": ["2026-04-30"],
            "domains": ["stock", "index"],
            "missing_by_day": {"2026-04-30": ["stock"]},
            "missing_by_domain": {"stock": ["2026-04-30"], "index": []},
        }
        complete_plan = {
            "ok": True,
            "expected_dates": ["2026-04-30"],
            "domains": ["stock", "index"],
            "missing_by_day": {},
            "missing_by_domain": {"stock": [], "index": []},
        }
        raw_before = {"stock": False, "index": True, "stock_rows": 3, "session_quality": {"status": "known_bad_session"}}
        raw_after = {"stock": True, "index": True, "stock_rows": 4, "session_quality": {"status": "clean"}}
        revalidation = {
            "trade_date": "2026-04-30",
            "before_latched": True,
            "raw": {"status": "clean"},
            "action": "cleared_after_raw_usable",
            "after": {"status": "clean"},
        }

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with (
                mock.patch(
                    "offline_daily_update.build_missing_update_plan",
                    side_effect=[initial_plan, complete_plan, complete_plan, complete_plan, complete_plan, complete_plan],
                ),
                mock.patch("offline_daily_update._find_script", return_value=base / "repair_market_day_akshare.py"),
                mock.patch("offline_daily_update.raw_stock_coverage_for_repair", side_effect=[raw_before, raw_after]),
                mock.patch("offline_daily_update.stock_coverage_for_date", return_value={"stock": True, "index": True}),
                mock.patch("offline_daily_update._run_with_remaining_budget", return_value=(0, "child complete", 3_000)) as run_child,
                mock.patch("offline_daily_update._revalidate_attempted_stock_sessions", return_value=[revalidation]) as revalidate,
            ):
                result = run_offline_update(
                    base,
                    asof="2026-04-30",
                    target_days=["2026-04-30"],
                    include_mining=False,
                    publish_health=False,
                    market_workers=4,
                    market_min_request_interval_sec=0.25,
                    timeout_sec=3_600,
                )

        self.assertEqual(run_child.call_count, 1)
        self.assertEqual(revalidate.call_count, 1)
        cmd = run_child.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--workers") + 1], "4")
        self.assertEqual(cmd[cmd.index("--min-request-interval-sec") + 1], "0.25")
        self.assertEqual([command["attempts"] for command in result["commands"]], [1])
        self.assertEqual(result["revalidations"], [revalidation])

    def test_market_repair_caps_total_child_starts_at_three(self) -> None:
        from offline_daily_update import run_offline_update

        unresolved_plan = {
            "ok": False,
            "expected_dates": ["2026-04-30"],
            "domains": ["stock", "index"],
            "missing_by_day": {"2026-04-30": ["stock"]},
            "missing_by_domain": {"stock": ["2026-04-30"], "index": []},
        }
        raw_unresolved = {"stock": False, "index": True, "stock_rows": 3, "session_quality": {"status": "known_bad_session"}}

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with (
                mock.patch("offline_daily_update.build_missing_update_plan", return_value=unresolved_plan),
                mock.patch("offline_daily_update._find_script", return_value=base / "repair_market_day_akshare.py"),
                mock.patch("offline_daily_update.raw_stock_coverage_for_repair", return_value=raw_unresolved),
                mock.patch("offline_daily_update._run_with_remaining_budget", return_value=(1, "child incomplete", 3_000)) as run_child,
                mock.patch("offline_daily_update._revalidate_attempted_stock_sessions", return_value=[]),
            ):
                result = run_offline_update(
                    base,
                    asof="2026-04-30",
                    target_days=["2026-04-30"],
                    include_mining=False,
                    publish_health=False,
                    timeout_sec=3_600,
                )

        self.assertEqual(run_child.call_count, 3)
        self.assertEqual([command["attempts"] for command in result["commands"]], [1, 2, 3])
        self.assertIn("stock_index give_up asof=2026-04-30 reason=max_attempts", result["logs"])

    def test_global_request_pacer_spaces_provider_attempts(self) -> None:
        from repair_market_day_akshare import GlobalRequestPacer

        pacer = GlobalRequestPacer(0.25)
        with (
            mock.patch("repair_market_day_akshare.time.monotonic", side_effect=[10.0, 10.1]),
            mock.patch("repair_market_day_akshare.time.sleep") as sleep,
        ):
            pacer.wait()
            pacer.wait()

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.15, places=6)

    def test_provider_timeout_releases_repair_worker_to_try_next_source(self) -> None:
        import threading

        from repair_market_day_akshare import ProviderCallLimiter, fetch_with_sources

        started = threading.Event()
        release = threading.Event()

        def stalled_sina(_code: str, _day_dash: str, _day_compact: str) -> dict:
            started.set()
            release.wait(1.0)
            return {"source": "sina"}

        def tx_fallback(_code: str, _day_dash: str, _day_compact: str) -> dict:
            return {"source": "tx"}

        limiter = ProviderCallLimiter(max_inflight=1, timeout_sec=0.01)
        try:
            row, errors = fetch_with_sources(
                "600001",
                "2026-05-19",
                "20260519",
                [("ak_sina", stalled_sina), ("ak_tx", tx_fallback)],
                attempts_per_source=1,
                provider_calls=limiter,
            )
            with self.assertRaisesRegex(TimeoutError, "ak_sina in-flight limit reached"):
                limiter.call("ak_sina", stalled_sina, "600002", "2026-05-19", "20260519")
        finally:
            release.set()

        self.assertTrue(started.is_set())
        self.assertEqual(row, {"source": "tx"})
        self.assertEqual(limiter.timed_out_calls, 1)
        self.assertEqual(limiter.saturated_calls, 1)
        self.assertTrue(any("ak_sina attempt 1: TimeoutError" in error for error in errors))

    def test_concept_and_etf_coverage_use_explicit_eligible_universes(self) -> None:
        import json
        import sqlite3

        from offline_daily_update import concept_coverage_for_date, etf_coverage_for_date

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            concept = sqlite3.connect(root / "ths_concept.db")
            try:
                concept.execute("CREATE TABLE concept_kline (trade_date TEXT, concept_code TEXT)")
                concept.execute(
                    "CREATE TABLE concept_coverage_expectations (trade_date TEXT PRIMARY KEY, eligible_codes_json TEXT, expect_source TEXT)"
                )
                concept.execute("INSERT INTO concept_kline VALUES ('2026-04-22', '880001')")
                concept.execute(
                    "INSERT INTO concept_coverage_expectations VALUES ('2026-04-22', ?, 'provider_eligible_universe')",
                    (json.dumps(["880001", "880002"]),),
                )
                concept.commit()
            finally:
                concept.close()

            etf = sqlite3.connect(root / "etf_mvp.db")
            try:
                etf.execute("CREATE TABLE etf_master (fund_code TEXT, is_equity INTEGER, updated_at TEXT)")
                etf.execute("CREATE TABLE etf_scale (trade_date TEXT, fund_code TEXT)")
                etf.execute("CREATE TABLE etf_total (trade_date TEXT)")
                etf.executemany(
                    "INSERT INTO etf_master VALUES (?, 1, ?)",
                    [
                        ("510300", "2026-04-22 17:31:00"),
                        ("510500", "2026-04-22 17:31:00"),
                        ("159001", "2026-04-01 17:31:00"),
                    ],
                )
                etf.execute("INSERT INTO etf_scale VALUES ('2026-04-22', '510300')")
                etf.execute("INSERT INTO etf_total VALUES ('2026-04-22')")
                etf.commit()
            finally:
                etf.close()

            concept_coverage = concept_coverage_for_date(root / "ths_concept.db", "2026-04-22")
            etf_coverage = etf_coverage_for_date(root / "etf_mvp.db", "2026-04-22")

        self.assertFalse(concept_coverage["concept"])
        self.assertEqual((concept_coverage["concept_have"], concept_coverage["concept_expect"], concept_coverage["concept_missing"]), (1, 2, 1))
        self.assertFalse(etf_coverage["etf"])
        self.assertEqual((etf_coverage["etf_have"], etf_coverage["etf_expect"], etf_coverage["etf_missing"]), (1, 2, 1))
        self.assertEqual(etf_coverage["etf_expect_source"], "etf_master_latest_provider_snapshot")

    def test_concept_universe_regression_rejects_and_preserves_same_day_expectation(self) -> None:
        import json
        import sqlite3

        import pandas as pd

        import backfill_adata_ths_concept_index_kline_60d as concept_backfill
        from backfill_adata_ths_concept_index_kline_60d import apply_concept_universe_gate, ensure_tables

        original = {f"88{index:04d}" for index in range(349)}
        candidate = set(sorted(original)[:263])
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ths_concept.db"
            con = sqlite3.connect(db_path)
            try:
                ensure_tables(con)
                con.execute(
                    "INSERT INTO concept_coverage_expectations VALUES (?, ?, 'provider_eligible_universe', ?)",
                    ("2026-08-10", json.dumps(sorted(original)), "2026-08-11T00:00:00"),
                )
                con.execute(
                    "INSERT INTO concept_master VALUES ('889999', 'sentinel', '', 'fixture', '2026-08-11T00:00:00')"
                )
                con.execute(
                    "INSERT INTO concept_kline VALUES ('2026/08/10', '889999', 1, 1, 0, 1, 1)"
                )
                con.commit()

                result = apply_concept_universe_gate(con, ["2026-08-10"], candidate)
                persisted = set(
                    json.loads(
                        con.execute(
                            "SELECT eligible_codes_json FROM concept_coverage_expectations WHERE trade_date='2026-08-10'"
                        ).fetchone()[0]
                    )
                )
            finally:
                con.close()

            provider_frame = pd.DataFrame({"index_code": sorted(candidate), "name": ["fixture"] * len(candidate)})
            with (
                mock.patch("sys.argv", ["concept-backfill", "--db", str(db_path), "--target-day", "2026-08-10"]),
                mock.patch.object(concept_backfill, "try_import_adata", return_value=object()),
                mock.patch.object(concept_backfill, "get_ths_concept_list", return_value=provider_frame),
                mock.patch.object(concept_backfill, "load_filtered_concepts", return_value=({"889999"}, "fixture.csv")),
                mock.patch.object(concept_backfill, "fetch_concept_kline", side_effect=AssertionError("gate must stop before fetch")),
            ):
                rc = concept_backfill.main()

            con = sqlite3.connect(db_path)
            try:
                master_rows = con.execute("SELECT COUNT(*) FROM concept_master WHERE index_code='889999'").fetchone()[0]
                kline_rows = con.execute("SELECT COUNT(*) FROM concept_kline WHERE concept_code='889999'").fetchone()[0]
            finally:
                con.close()

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "eligible_universe_regression")
        self.assertEqual(result["candidate_count"], 263)
        self.assertEqual(result["references"][0]["reference_count"], 349)
        self.assertLess(result["references"][0]["count_ratio"], 0.95)
        self.assertLess(result["references"][0]["overlap_ratio"], 0.95)
        self.assertEqual(persisted, original)
        self.assertNotEqual(rc, 0)
        self.assertEqual((master_rows, kline_rows), (1, 1))

    def test_concept_universe_same_day_idempotent_expectation_is_unchanged(self) -> None:
        import json
        import sqlite3

        from backfill_adata_ths_concept_index_kline_60d import apply_concept_universe_gate, ensure_tables

        original = {f"88{index:04d}" for index in range(349)}
        with tempfile.TemporaryDirectory() as tmp:
            con = sqlite3.connect(Path(tmp) / "ths_concept.db")
            try:
                ensure_tables(con)
                con.execute(
                    "INSERT INTO concept_coverage_expectations VALUES (?, ?, 'provider_eligible_universe', ?)",
                    ("2026-08-10", json.dumps(sorted(original)), "2026-08-11T00:00:00"),
                )
                con.commit()
                result = apply_concept_universe_gate(con, ["2026-08-10"], original)
                persisted = set(json.loads(con.execute("SELECT eligible_codes_json FROM concept_coverage_expectations").fetchone()[0]))
            finally:
                con.close()

        self.assertTrue(result["ok"])
        self.assertFalse(result["universe_bootstrap"])
        self.assertEqual(persisted, original)

    def test_concept_universe_same_day_expectation_expands_monotonically(self) -> None:
        import json
        import sqlite3

        from backfill_adata_ths_concept_index_kline_60d import apply_concept_universe_gate, ensure_tables

        original = {f"88{index:04d}" for index in range(349)}
        candidate = original | {"889998", "889999"}
        with tempfile.TemporaryDirectory() as tmp:
            con = sqlite3.connect(Path(tmp) / "ths_concept.db")
            try:
                ensure_tables(con)
                con.execute(
                    "INSERT INTO concept_coverage_expectations VALUES (?, ?, 'provider_eligible_universe', ?)",
                    ("2026-08-10", json.dumps(sorted(original)), "2026-08-11T00:00:00"),
                )
                con.commit()
                result = apply_concept_universe_gate(con, ["2026-08-10"], candidate)
                persisted = set(json.loads(con.execute("SELECT eligible_codes_json FROM concept_coverage_expectations").fetchone()[0]))
            finally:
                con.close()

        self.assertTrue(result["ok"])
        self.assertEqual(len(persisted), 351)
        self.assertEqual(persisted, candidate)

    def test_concept_universe_new_day_rejects_against_latest_trusted_reference(self) -> None:
        import json
        import sqlite3

        from backfill_adata_ths_concept_index_kline_60d import apply_concept_universe_gate, ensure_tables

        trusted = {f"88{index:04d}" for index in range(349)}
        candidate = set(sorted(trusted)[:263])
        with tempfile.TemporaryDirectory() as tmp:
            con = sqlite3.connect(Path(tmp) / "ths_concept.db")
            try:
                ensure_tables(con)
                con.execute(
                    "INSERT INTO concept_coverage_expectations VALUES (?, ?, 'provider_eligible_universe', ?)",
                    ("2026-08-07", json.dumps(sorted(trusted)), "2026-08-11T00:00:00"),
                )
                con.commit()
                result = apply_concept_universe_gate(con, ["2026-08-10"], candidate)
                new_day_rows = con.execute(
                    "SELECT COUNT(*) FROM concept_coverage_expectations WHERE trade_date='2026-08-10'"
                ).fetchone()[0]
            finally:
                con.close()

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "eligible_universe_regression")
        self.assertEqual(result["references"][0]["reference_kind"], "latest_prior")
        self.assertEqual(new_day_rows, 0)

    def test_concept_limit_probe_does_not_contaminate_formal_expectation(self) -> None:
        import json
        import sqlite3

        import pandas as pd

        import backfill_adata_ths_concept_index_kline_60d as concept_backfill

        original = {f"88{index:04d}" for index in range(349)}
        provider_frame = pd.DataFrame({"index_code": sorted(original), "name": ["fixture"] * len(original)})
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ths_concept.db"
            con = sqlite3.connect(db_path)
            try:
                concept_backfill.ensure_tables(con)
                con.execute(
                    "INSERT INTO concept_coverage_expectations VALUES (?, ?, 'provider_eligible_universe', ?)",
                    ("2026-08-10", json.dumps(sorted(original)), "2026-08-11T00:00:00"),
                )
                con.commit()
            finally:
                con.close()

            with (
                mock.patch("sys.argv", ["concept-backfill", "--db", str(db_path), "--target-day", "2026-08-10", "--limit", "1"]),
                mock.patch.object(concept_backfill, "try_import_adata", return_value=object()),
                mock.patch.object(concept_backfill, "get_ths_concept_list", return_value=provider_frame),
                mock.patch.object(concept_backfill, "load_filtered_concepts", return_value=(set(), "fixture.csv")),
                mock.patch.object(concept_backfill, "fetch_concept_kline", return_value=pd.DataFrame()),
            ):
                rc = concept_backfill.main()

            con = sqlite3.connect(db_path)
            try:
                persisted = set(json.loads(con.execute("SELECT eligible_codes_json FROM concept_coverage_expectations").fetchone()[0]))
            finally:
                con.close()

        self.assertNotEqual(rc, 0)
        self.assertEqual(persisted, original)

    def test_parent_preserves_structured_concept_universe_failure(self) -> None:
        from offline_daily_update import run_offline_update

        day = "2026-08-10"
        incomplete_plan = {
            "ok": False,
            "expected_dates": [day],
            "domains": ["concept"],
            "missing_by_day": {day: ["concept"]},
            "missing_by_domain": {"concept": [day]},
            "coverage": {},
        }
        stale_false_green_plan = {
            "ok": True,
            "expected_dates": [day],
            "domains": ["concept"],
            "missing_by_day": {},
            "missing_by_domain": {"concept": []},
            "coverage": {},
        }
        plans = [incomplete_plan, incomplete_plan]

        def build_plan(*_args, **_kwargs):
            return plans.pop(0) if plans else stale_false_green_plan

        child_output = (
            '[THS-KLINE fixture] UNIVERSE_GATE '
            '{"ok": false, "reason": "eligible_universe_regression", "candidate_count": 263, '
            '"references": [{"reference_count": 349, "count_ratio": 0.7535, '
            '"overlap_ratio": 0.7535, "missing_count": 86, "missing_codes": ["880263"]}]}'
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with (
                mock.patch("offline_daily_update.build_missing_update_plan", side_effect=build_plan),
                mock.patch("offline_daily_update._find_script", return_value=base / "backfill_adata_ths_concept_index_kline_60d.py"),
                mock.patch("offline_daily_update._run_with_remaining_budget", return_value=(2, child_output, 3_000)) as run_child,
            ):
                result = run_offline_update(
                    base,
                    asof=day,
                    target_days=[day],
                    include_mining=False,
                    publish_health=False,
                )

        self.assertFalse(result["ok"])
        self.assertEqual(run_child.call_count, 1)
        self.assertEqual(result["quality_failures"][0]["code"], "eligible_universe_regression")
        self.assertEqual(result["quality_failures"][0]["candidate_count"], 263)

    def test_proto_readiness_keeps_concept_regression_optional(self) -> None:
        from offline_daily_update import prototype_readiness_for_date

        def coverage(_base, _day, *, domains=None, **_kwargs):
            selected = set(domains or [])
            result = {"trade_date": "2026-08-10"}
            if {"stock", "index"} & selected:
                result.update(
                    {
                        "stock": True,
                        "index": True,
                        "index_codes": ["000001", "399001", "000300", "000852"],
                        "session_quality": {"status": "usable_with_quarantine", "reasons": ["row_quarantine"]},
                    }
                )
            if "concept" in selected:
                result.update(
                    {
                        "concept": False,
                        "concept_reason_codes": ["eligible_universe_regression"],
                        "concept_have": 263,
                        "concept_expect": 349,
                    }
                )
            if "etf" in selected:
                result["etf"] = True
            if "mining" in selected:
                result["mining"] = False
            return result

        with mock.patch("offline_daily_update.coverage_for_date", side_effect=coverage):
            readiness = prototype_readiness_for_date(Path("."), "2026-08-10")

        self.assertTrue(readiness["screening_ready"])
        self.assertEqual(readiness["overall_status"], "degraded")
        self.assertFalse(readiness["domains"]["concept"]["ready"])
        self.assertEqual(readiness["domains"]["concept"]["reason_codes"], ["eligible_universe_regression"])

    def test_skip_concept_never_starts_a_provider_child(self) -> None:
        from offline_daily_update import run_offline_update

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "ths_concept.db").touch()
            with (
                mock.patch("offline_daily_update.resolve_expected_trade_days", return_value=["2026-04-30"]),
                mock.patch("offline_daily_update._run_with_remaining_budget") as provider_child,
            ):
                result = run_offline_update(
                    base,
                    asof="2026-04-30",
                    include_mining=False,
                    include_concept=False,
                    publish_health=False,
                    dry_run=True,
                )

        provider_child.assert_not_called()
        self.assertTrue(result["concept_deferred"])
        self.assertEqual(result["readiness"]["domains"]["concept"]["status"], "optional_skipped")

    def test_skip_mining_excludes_mining_from_the_ops1_market_recovery_plan(self) -> None:
        from offline_daily_update import run_offline_update

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "a_share_mvp.db").touch()
            (base / "mining_mvp.db").touch()
            with mock.patch("offline_daily_update.resolve_expected_trade_days", return_value=[]):
                result = run_offline_update(base, asof="2026-04-30", include_mining=False, dry_run=True)

        self.assertNotIn("mining", result["plan"]["domains"])
        self.assertTrue(result["mining_deferred"])

    def test_no_health_suppresses_health_file_and_push_for_ops1_recovery(self) -> None:
        from offline_daily_update import run_offline_update

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with (
                mock.patch("offline_daily_update.resolve_expected_trade_days", return_value=[]),
                mock.patch("offline_daily_update.write_health_summary") as write_health,
                mock.patch("offline_daily_update._send_daily_push") as send_push,
            ):
                result = run_offline_update(
                    base,
                    asof="2026-04-30",
                    include_mining=False,
                    publish_health=False,
                    dry_run=True,
                )

        write_health.assert_not_called()
        send_push.assert_not_called()
        self.assertEqual(result["health"], {"status": "not_published"})
        self.assertEqual(result["push"], {"status": "disabled"})

    def test_initial_build_uses_full_history_window(self) -> None:
        from app_panel import calc_backfill_days

        self.assertEqual(calc_backfill_days(None, "2026-04-10", cap=15), 60)

    def test_close_day_catchup_starts_background_daily_backfill_when_snapshot_is_unsafe(self) -> None:
        import app_panel as panel

        local_before = {"stock": "2026-04-21", "index": "2026-04-21", "concept": None, "etf": None}

        with (
            mock.patch.object(panel, "can_use_snapshot_for_close_target", return_value=False),
            mock.patch.object(panel, "pick_existing_script", return_value="backfill_baostock_hsA_60d_v2.py"),
            mock.patch.object(
                panel,
                "start_background_job",
                return_value={"pid": 1234, "log_path": "output/backfill_jobs/test.log"},
            ) as start_job,
            mock.patch.object(panel, "run_cmd", side_effect=AssertionError("startup catch-up must not block")),
            mock.patch.object(panel, "get_local_last_dates", return_value=local_before),
        ):
            result, logs = panel.maybe_backfill_to_close_day(
                stock_db="a_share_mvp.db",
                concept_db=None,
                etf_db=None,
                target_day="2026-04-22",
                local_dates=local_before,
                include_concept=False,
                include_etf=False,
            )

        self.assertEqual(result["stock"], "2026-04-21")
        start_job.assert_called_once()
        self.assertTrue(any("[TARGET_BAOSTOCK_BACKGROUND]" in line for line in logs))

    def test_etf_ref_dates_must_cover_requested_asof(self) -> None:
        from backfill_etf_equity_60d_v2 import ref_dates_cover_asof

        self.assertFalse(ref_dates_cover_asof(["2026-04-18", "2026-04-21"], "2026-04-22"))
        self.assertTrue(ref_dates_cover_asof(["2026-04-21", "2026-04-22"], "2026-04-22"))

    def test_etf_ref_dates_are_filtered_by_asof_before_tail_selection(self) -> None:
        import sqlite3

        from backfill_etf_equity_60d_v2 import load_trade_dates_from_ref_db

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ref.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE kline_daily (trade_date TEXT)")
                for day in [
                    "2026-04-08",
                    "2026-04-09",
                    "2026-04-10",
                    "2026-04-13",
                    "2026-04-14",
                    "2026-04-15",
                    "2026-04-16",
                    "2026-04-17",
                    "2026-04-20",
                    "2026-04-21",
                    "2026-04-22",
                    "2026-04-23",
                ]:
                    conn.execute("INSERT INTO kline_daily VALUES (?)", (day,))
                conn.commit()
            finally:
                conn.close()

            dates = load_trade_dates_from_ref_db(str(db_path), 1, asof="2026-04-22")

        self.assertEqual(dates, ["2026-04-22"])

    def test_offline_update_plan_detects_middle_gap_even_when_latest_is_newer(self) -> None:
        import sqlite3

        from offline_daily_update import build_missing_update_plan

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            conn = sqlite3.connect(base / "a_share_mvp.db")
            try:
                conn.execute(
                    """
                    CREATE TABLE kline_daily (
                        trade_date TEXT,
                        sec_type TEXT,
                        sec_code TEXT,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        pre_close REAL,
                        volume REAL,
                        amount REAL
                    )
                    """
                )
                for day in (
                    "2026-04-16",
                    "2026-04-17",
                    "2026-04-18",
                    "2026-04-19",
                    "2026-04-20",
                    "2026-04-21",
                    "2026-04-23",
                ):
                    conn.execute(
                        """
                        INSERT INTO kline_daily VALUES
                        (?, 'stock', '600001', 10.0, 10.0, 10.0, 10.0, 10.0, 100.0, 1000.0)
                        """,
                        (day,),
                    )
                    for code in ("000001", "399001", "000300", "000852"):
                        conn.execute(
                            "INSERT INTO kline_daily (trade_date, sec_type, sec_code, close) VALUES (?, 'index', ?, 10.0)",
                            (day, code),
                        )
                conn.commit()
            finally:
                conn.close()

            plan = build_missing_update_plan(
                base,
                ["2026-04-21", "2026-04-22", "2026-04-23"],
                stock_min_rows=1,
            )

        self.assertIn("2026-04-22", plan["missing_by_day"])
        self.assertIn("stock", plan["missing_by_day"]["2026-04-22"])
        self.assertIn("index", plan["missing_by_day"]["2026-04-22"])
        self.assertIn("2026-04-22", plan["missing_by_domain"]["stock"])
        self.assertNotIn("2026-04-23", plan["missing_by_domain"]["stock"])

    def test_stock_coverage_accepts_only_usable_session_quality_states(self) -> None:
        import sqlite3

        from mining.data_quality import (
            STATUS_CLEAN,
            STATUS_KNOWN_BAD,
            STATUS_PARTIAL,
            STATUS_UNAVAILABLE,
            STATUS_USABLE_WITH_QUARANTINE,
        )
        from offline_daily_update import raw_stock_coverage_for_repair, stock_coverage_for_date

        with tempfile.TemporaryDirectory() as tmp:
            stock_path = Path(tmp) / "a_share_mvp.db"
            conn = sqlite3.connect(stock_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE kline_daily (
                        trade_date TEXT,
                        sec_type TEXT,
                        sec_code TEXT,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        pre_close REAL,
                        volume REAL,
                        amount REAL
                    )
                    """
                )
                for day in ("2026-04-15", "2026-04-16", "2026-04-17", "2026-04-20", "2026-04-21", "2026-04-22"):
                    for code in ("600001", "600002", "600003"):
                        conn.execute(
                            """
                            INSERT INTO kline_daily VALUES
                            (?, 'stock', ?, 10.0, 10.0, 10.0, 10.0, 10.0, 100.0, 1000.0)
                            """,
                            (day, code),
                        )
                conn.commit()
            finally:
                conn.close()

            expected = {
                STATUS_CLEAN: True,
                STATUS_USABLE_WITH_QUARANTINE: True,
                STATUS_PARTIAL: False,
                STATUS_KNOWN_BAD: False,
                STATUS_UNAVAILABLE: False,
            }
            for status, usable in expected.items():
                with mock.patch("offline_daily_update.inspect_stock_session", return_value={"status": status}):
                    coverage = stock_coverage_for_date(
                        stock_path,
                        "2026-04-22",
                        stock_min_rows=3,
                        required_index_codes=(),
                    )
                self.assertEqual(coverage["stock"], usable, status)

            # A repair decision must inspect raw data, otherwise a stale latch can
            # re-launch a child after the data rows have already been restored.
            raw_coverage = raw_stock_coverage_for_repair(
                stock_path,
                "2026-04-22",
                stock_min_rows=3,
                required_index_codes=(),
            )
            self.assertTrue(raw_coverage["stock"])

    def test_expected_trade_days_use_market_calendar_not_plain_weekdays(self) -> None:
        from unittest import mock

        from offline_daily_update import resolve_expected_trade_days

        calendar_days = [
            "2026-04-23",
            "2026-04-24",
            "2026-04-27",
            "2026-04-28",
            "2026-04-29",
            "2026-04-30",
            "2026-05-06",
        ]
        with mock.patch(
            "offline_daily_update._akshare_trade_days",
            return_value=calendar_days,
            create=True,
        ):
            days = resolve_expected_trade_days(
                "a_share_mvp.db",
                asof="2026-05-06",
                days=5,
            )

        self.assertEqual(days, ["2026-04-27", "2026-04-28", "2026-04-29", "2026-04-30", "2026-05-06"])
        self.assertNotIn("2026-05-01", days)
        self.assertNotIn("2026-05-04", days)
        self.assertNotIn("2026-05-05", days)

    def test_missing_window_size_keeps_single_day_stock_repair_narrow(self) -> None:
        from offline_daily_update import _missing_window_size

        expected = ["2026-04-17", "2026-04-20", "2026-04-21", "2026-04-22", "2026-04-23"]

        self.assertEqual(_missing_window_size(expected, ["2026-04-22"]), 1)
        self.assertEqual(_missing_window_size(expected, ["2026-04-20", "2026-04-22"]), 3)

    def test_run_with_retries_stops_after_three_failures(self) -> None:
        from offline_daily_update import _run_with_retries

        calls = []

        def runner(_cmd, cwd, timeout_sec):
            calls.append((tuple(_cmd), str(cwd), timeout_sec))
            return 999, "interface failed"

        rc, out, attempts = _run_with_retries(
            ["fake-source"],
            cwd=".",
            timeout_sec=1,
            runner=runner,
        )

        self.assertEqual(rc, 999)
        self.assertEqual(attempts, 3)
        self.assertEqual(len(calls), 3)
        self.assertIn("attempt 3/3", out)

    def test_akshare_stock_fallback_aborts_after_three_extraction_failures(self) -> None:
        import sqlite3

        from offline_daily_update import akshare_stock_index_backfill

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "a_share_mvp.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE stock_info(sec_code TEXT PRIMARY KEY, bs_code TEXT, name TEXT)")
                conn.executemany(
                    "INSERT INTO stock_info(sec_code, bs_code, name) VALUES(?,?,?)",
                    [
                        ("600001", "sh.600001", "A"),
                        ("600002", "sh.600002", "B"),
                        ("600003", "sh.600003", "C"),
                        ("600004", "sh.600004", "D"),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            class FakeAk:
                def stock_zh_a_hist(self, **_kwargs):
                    raise RuntimeError("source down")

            result = akshare_stock_index_backfill(db_path, "2026-04-22", ak_module=FakeAk())

        self.assertFalse(result["ok"])
        self.assertLessEqual(result["attempts"], 5)
        self.assertIn("probe", result["error"])

    def test_akshare_stock_fallback_aborts_when_probe_cannot_read_target_day(self) -> None:
        import sqlite3

        import pandas as pd

        from offline_daily_update import akshare_stock_index_backfill

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "a_share_mvp.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE stock_info(sec_code TEXT PRIMARY KEY, bs_code TEXT, name TEXT)")
                conn.executemany(
                    "INSERT INTO stock_info(sec_code, bs_code, name) VALUES(?,?,?)",
                    [
                        ("600000", "sh.600000", "A"),
                        ("000001", "sz.000001", "B"),
                        ("300750", "sz.300750", "C"),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            class FakeAk:
                def stock_zh_a_hist(self, **_kwargs):
                    return pd.DataFrame()

            result = akshare_stock_index_backfill(db_path, "2026-05-06", ak_module=FakeAk())

        self.assertFalse(result["ok"])
        self.assertLessEqual(result["attempts"], 3)
        self.assertIn("probe", result["error"])

    def test_offline_update_does_not_start_slow_baostock_after_akshare_market_failure(self) -> None:
        import sqlite3

        from offline_daily_update import run_offline_update

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            conn = sqlite3.connect(base / "a_share_mvp.db")
            try:
                conn.execute(
                    """
                    CREATE TABLE kline_daily (
                        trade_date TEXT,
                        sec_type TEXT,
                        sec_code TEXT,
                        close REAL
                    )
                    """
                )
                conn.execute("INSERT INTO kline_daily VALUES ('2026-04-29', 'stock', '600001', 10.0)")
                for code in ("000001", "399001", "000300", "000852"):
                    conn.execute("INSERT INTO kline_daily VALUES ('2026-04-29', 'index', ?, 10.0)", (code,))
                conn.commit()
            finally:
                conn.close()

            with (
                mock.patch("offline_daily_update.resolve_expected_trade_days", return_value=["2026-04-29", "2026-04-30"]),
                mock.patch("offline_daily_update.akshare_stock_index_backfill", return_value={"ok": False, "rows": 0, "attempts": 1, "error": "source down"}),
                mock.patch("offline_daily_update._find_script", return_value=None),
            ):
                result = run_offline_update(base, asof="2026-04-30", days=2, timeout_sec=30)

        self.assertFalse(result["ok"])
        self.assertTrue(any("skip baostock" in line for line in result["logs"]))

    def test_repair_market_day_skips_existing_target_day_stock_rows(self) -> None:
        import sqlite3

        from repair_market_day_akshare import KLINE_COLS, repair_day

        fetched_codes: list[str] = []

        def kline_row(sec_type: str, code: str, trade_date: str) -> tuple:
            values = {
                "sec_type": sec_type,
                "sec_code": code,
                "trade_date": trade_date,
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.5,
                "pre_close": 10.0,
                "change": 0.5,
                "change_pct": 5.0,
                "volume": 1000.0,
                "amount": 100000.0,
                "turnover_ratio": 1.0,
                "source": "test",
                "updated_at": "2026-05-20T00:00:00",
            }
            return tuple(values[col] for col in KLINE_COLS)

        def fake_stock_fetch(code: str, day_dash: str, _day_compact: str) -> dict:
            fetched_codes.append(code)
            values = dict(zip(KLINE_COLS, kline_row("stock", code, day_dash)))
            return values

        def fake_index_fetch(code: str, day_dash: str, _day_compact: str) -> dict:
            values = dict(zip(KLINE_COLS, kline_row("index", code, day_dash)))
            return values

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "a_share_mvp.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE stock_info(sec_code TEXT PRIMARY KEY, bs_code TEXT, name TEXT)")
                conn.executemany(
                    "INSERT INTO stock_info(sec_code, bs_code, name) VALUES(?,?,?)",
                    [
                        ("600001", "sh.600001", "A"),
                        ("600002", "sh.600002", "B"),
                        ("600003", "sh.600003", "C"),
                    ],
                )
                conn.execute(
                    f"""
                    CREATE TABLE kline_daily (
                      {",".join(col + " TEXT" for col in KLINE_COLS)},
                      PRIMARY KEY (sec_type, sec_code, trade_date)
                    )
                    """
                )
                conn.execute(
                    f"INSERT INTO kline_daily ({','.join(KLINE_COLS)}) VALUES ({','.join(['?'] * len(KLINE_COLS))})",
                    kline_row("stock", "600001", "2026-05-19"),
                )
                invalid = list(kline_row("stock", "600002", "2026-05-19"))
                invalid[KLINE_COLS.index("amount")] = None
                invalid[KLINE_COLS.index("source")] = "incomplete_fixture"
                conn.execute(
                    f"INSERT INTO kline_daily ({','.join(KLINE_COLS)}) VALUES ({','.join(['?'] * len(KLINE_COLS))})",
                    invalid,
                )
                conn.commit()
            finally:
                conn.close()

            with (
                mock.patch("repair_market_day_akshare.fetch_stock_em", side_effect=fake_stock_fetch),
                mock.patch("repair_market_day_akshare.fetch_stock_sina", side_effect=fake_stock_fetch),
                mock.patch("repair_market_day_akshare.fetch_stock_tx", side_effect=fake_stock_fetch),
                mock.patch("repair_market_day_akshare.fetch_index_sina", side_effect=fake_index_fetch),
                mock.patch("repair_market_day_akshare.fetch_index_tx", side_effect=fake_index_fetch),
            ):
                result = repair_day(db_path, "2026-05-19", workers=1, attempts_per_source=1)
            conn = sqlite3.connect(db_path)
            try:
                replaced_amount, replaced_source = conn.execute(
                    "SELECT amount, source FROM kline_daily WHERE sec_type='stock' AND sec_code='600002' AND trade_date='2026-05-19'"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(result["stock_count"], 3)
        self.assertEqual(result["skipped_existing"], 1)
        self.assertEqual(result["retryable_existing"], 1)
        self.assertEqual(set(fetched_codes), {"600002", "600003"})
        self.assertEqual(float(replaced_amount), 100000.0)
        self.assertEqual(replaced_source, "test")

    def test_stock_provider_quality_gate_rejects_missing_amount_and_continues_fallback(self) -> None:
        from repair_market_day_akshare import fetch_with_sources

        def stock_row(source: str, amount) -> dict:
            return {
                "sec_type": "stock",
                "sec_code": "600001",
                "trade_date": "2026-05-19",
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.5,
                "pre_close": 10.0,
                "volume": 1_000.0,
                "amount": amount,
                "source": source,
            }

        row, errors = fetch_with_sources(
            "600001",
            "2026-05-19",
            "20260519",
            [
                ("ak_tx", lambda *_: stock_row("akshare_stock_zh_a_hist_tx", None)),
                ("complete", lambda *_: stock_row("complete_fixture", 100_000.0)),
            ],
            attempts_per_source=1,
        )

        self.assertEqual(row["source"], "complete_fixture")
        self.assertTrue(any("ak_tx rejected" in error and "missing_required_field" in error for error in errors))

    def test_tencent_stock_cannot_terminate_success_but_tencent_index_is_unchanged(self) -> None:
        from repair_market_day_akshare import fetch_with_sources

        tx_stock = {
            "sec_type": "stock",
            "sec_code": "600001",
            "trade_date": "2026-05-19",
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "pre_close": None,
            "volume": 1_000.0,
            "amount": None,
            "source": "akshare_stock_zh_a_hist_tx",
        }
        tx_index = dict(tx_stock, sec_type="index", sec_code="000852", source="akshare_stock_zh_a_hist_tx_index")

        stock, stock_errors = fetch_with_sources(
            "600001", "2026-05-19", "20260519", [("ak_tx", lambda *_: tx_stock)], attempts_per_source=1
        )
        index, index_errors = fetch_with_sources(
            "000852", "2026-05-19", "20260519", [("ak_tx", lambda *_: tx_index)], attempts_per_source=1
        )

        self.assertIsNone(stock)
        self.assertTrue(any("missing_required_field" in error for error in stock_errors))
        self.assertEqual(index["sec_code"], "000852")
        self.assertEqual(index_errors, [])

    def test_all_invalid_stock_sources_leave_existing_row_retryable(self) -> None:
        import sqlite3

        from mining.data_quality import classify_stock_row
        from repair_market_day_akshare import KLINE_COLS, repair_day

        def row(sec_type: str, code: str, day: str, *, amount, source: str) -> dict:
            return {
                "sec_type": sec_type,
                "sec_code": code,
                "trade_date": day,
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.5,
                "pre_close": 10.0,
                "change": 0.5,
                "change_pct": 5.0,
                "volume": 1_000.0,
                "amount": amount,
                "turnover_ratio": None,
                "source": source,
                "updated_at": "2026-05-20T00:00:00",
            }

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "a_share_mvp.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE stock_info(sec_code TEXT PRIMARY KEY, bs_code TEXT, name TEXT)")
                conn.execute("INSERT INTO stock_info VALUES ('600001', 'sh.600001', 'A')")
                conn.execute(
                    f"CREATE TABLE kline_daily ({','.join(col + ' TEXT' for col in KLINE_COLS)}, PRIMARY KEY (sec_type, sec_code, trade_date))"
                )
                invalid = row("stock", "600001", "2026-05-19", amount=None, source="original_incomplete")
                conn.execute(
                    f"INSERT INTO kline_daily ({','.join(KLINE_COLS)}) VALUES ({','.join(['?'] * len(KLINE_COLS))})",
                    tuple(invalid[col] for col in KLINE_COLS),
                )
                conn.commit()
            finally:
                conn.close()

            invalid_provider = lambda code, day, _compact: row("stock", code, day, amount=None, source="invalid_fixture")
            valid_index = lambda code, day, _compact: row("index", code, day, amount=None, source="index_fixture")
            with (
                mock.patch("repair_market_day_akshare.fetch_stock_em", side_effect=invalid_provider),
                mock.patch("repair_market_day_akshare.fetch_stock_sina", side_effect=invalid_provider),
                mock.patch("repair_market_day_akshare.fetch_stock_tx", side_effect=invalid_provider),
                mock.patch("repair_market_day_akshare.fetch_index_sina", side_effect=valid_index),
                mock.patch("repair_market_day_akshare.fetch_index_tx", side_effect=valid_index),
            ):
                result = repair_day(db_path, "2026-05-19", workers=1, attempts_per_source=1)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                persisted = conn.execute(
                    "SELECT * FROM kline_daily WHERE sec_type='stock' AND sec_code='600001' AND trade_date='2026-05-19'"
                ).fetchone()
                classification = classify_stock_row(persisted)
            finally:
                conn.close()

        self.assertEqual(result["retryable_existing"], 1)
        self.assertEqual(result["stock_count"], 1)
        self.assertEqual(persisted["source"], "original_incomplete")
        self.assertTrue(classification["retryable"])
        self.assertEqual(classification["reason"], "missing_required_field")

    def test_parent_reports_child_quality_mismatch_and_preserves_active_latch(self) -> None:
        import sqlite3

        from mining.data_quality import mark_known_bad_session
        from offline_daily_update import run_offline_update

        day = "2026-04-22"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            stock_path = base / "a_share_mvp.db"
            conn = sqlite3.connect(stock_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE kline_daily (
                      sec_type TEXT, sec_code TEXT, trade_date TEXT, open REAL, high REAL,
                      low REAL, close REAL, pre_close REAL, volume REAL, amount REAL,
                      PRIMARY KEY (sec_type, sec_code, trade_date)
                    )
                    """
                )
                for history_day in ("2026-04-15", "2026-04-16", "2026-04-17", "2026-04-20", "2026-04-21"):
                    for index in range(5):
                        conn.execute(
                            "INSERT INTO kline_daily VALUES ('stock', ?, ?, 10, 10, 10, 10, 10, 100, 1000)",
                            (f"60000{index}", history_day),
                        )
                for index in range(5):
                    conn.execute(
                        "INSERT INTO kline_daily VALUES ('stock', ?, ?, 10, 10, 10, 10, 10, 100, ?)",
                        (f"60000{index}", day, None if index == 4 else 1000),
                    )
                for code in ("000001", "399001", "000300", "000852"):
                    conn.execute("INSERT INTO kline_daily(sec_type, sec_code, trade_date) VALUES ('index', ?, ?)", (code, day))
                conn.commit()
                conn.row_factory = sqlite3.Row
                mark_known_bad_session(conn, day, reason="active_latch")
            finally:
                conn.close()

            latch_seen_by_child: list[bool] = []

            def child_returns_wrong_rc(_cmd, *, cwd, deadline, reserve_seconds):
                del cwd, deadline, reserve_seconds
                child_conn = sqlite3.connect(stock_path)
                try:
                    latch_seen_by_child.append(
                        child_conn.execute(
                            "SELECT COUNT(*) FROM selection_session_diagnostics WHERE trade_date=?",
                            (day,),
                        ).fetchone()[0]
                        == 1
                    )
                    child_conn.execute(
                        "UPDATE kline_daily SET amount=1000 WHERE sec_type='stock' AND sec_code='600004' AND trade_date=?",
                        (day,),
                    )
                    child_conn.commit()
                finally:
                    child_conn.close()
                return 1, "child incorrectly reported failure", 3000

            with (
                mock.patch("offline_daily_update._find_script", return_value=base / "repair_market_day_akshare.py"),
                mock.patch("offline_daily_update._run_with_remaining_budget", side_effect=child_returns_wrong_rc) as child,
            ):
                result = run_offline_update(
                    base,
                    asof=day,
                    target_days=[day],
                    include_mining=False,
                    publish_health=False,
                    timeout_sec=3600,
                )

            conn = sqlite3.connect(stock_path)
            try:
                latch_count = conn.execute(
                    "SELECT COUNT(*) FROM selection_session_diagnostics WHERE trade_date=?",
                    (day,),
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(child.call_count, 1)
        self.assertEqual(latch_seen_by_child, [True])
        self.assertFalse(result["ok"])
        self.assertEqual(result["quality_mismatches"][0]["code"], "child_parent_quality_mismatch")
        self.assertEqual(result["quality_mismatches"][0]["child_ok"], False)
        self.assertEqual(result["quality_mismatches"][0]["parent_ok"], True)
        self.assertEqual(result["revalidations"], [])
        self.assertEqual(latch_count, 1)

    def test_repair_cli_exit_code_is_exactly_shared_raw_postcondition(self) -> None:
        import repair_market_day_akshare as repair

        base_result = {"stock_count": 5_182, "index_count": 4}
        with (
            mock.patch.object(repair, "repair_day", return_value={**base_result, "raw_postcondition": {"ok": False}}),
            mock.patch("sys.argv", ["repair_market_day_akshare.py", "--date", "2026-05-19"]),
        ):
            self.assertEqual(repair.main(), 1)
        with (
            mock.patch.object(repair, "repair_day", return_value={**base_result, "raw_postcondition": {"ok": True}}),
            mock.patch("sys.argv", ["repair_market_day_akshare.py", "--date", "2026-05-19"]),
        ):
            self.assertEqual(repair.main(), 0)


    def test_health_summary_alerts_on_stale_domain_and_zero_complete_streak(self) -> None:
        import json
        import sqlite3

        from offline_daily_update import write_health_summary

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            stock = sqlite3.connect(base / "a_share_mvp.db")
            try:
                stock.execute("CREATE TABLE kline_daily (sec_type TEXT, sec_code TEXT, trade_date TEXT, close REAL)")
                for day in ("2026-04-20", "2026-04-21", "2026-04-22", "2026-04-23", "2026-04-24"):
                    stock.execute("INSERT INTO kline_daily VALUES ('stock', '600001', ?, 10.0)", (day,))
                    stock.execute("INSERT INTO kline_daily VALUES ('index', '000001', ?, 3000.0)", (day,))
                stock.commit()
            finally:
                stock.close()

            concept = sqlite3.connect(base / "ths_concept.db")
            try:
                concept.execute("CREATE TABLE concept_kline (trade_date TEXT, concept_code TEXT)")
                concept.execute("INSERT INTO concept_kline VALUES ('2026-04-20', '880001')")
                concept.commit()
            finally:
                concept.close()

            etf = sqlite3.connect(base / "etf_mvp.db")
            try:
                etf.execute("CREATE TABLE etf_scale (trade_date TEXT)")
                etf.execute("CREATE TABLE etf_total (trade_date TEXT)")
                etf.execute("INSERT INTO etf_scale VALUES ('2026-04-24')")
                etf.execute("INSERT INTO etf_total VALUES ('2026-04-24')")
                etf.commit()
            finally:
                etf.close()

            mining = sqlite3.connect(base / "mining_mvp.db")
            try:
                mining.execute("CREATE TABLE candidates (trade_date TEXT)")
                mining.execute("CREATE TABLE watchlist_snapshots (snapshot_date TEXT)")
                mining.execute("CREATE TABLE outcomes (status TEXT)")
                mining.execute("CREATE TABLE watchlist_outcomes (status TEXT)")
                mining.executemany("INSERT INTO candidates VALUES (?)", [("2026-04-24",), ("2026-04-24",)])
                mining.execute("INSERT INTO watchlist_snapshots VALUES ('2026-04-24')")
                mining.execute("INSERT INTO outcomes VALUES ('complete')")
                mining.commit()
            finally:
                mining.close()

            output = base / "output"
            output.mkdir()
            (output / "health_state.json").write_text(
                json.dumps(
                    {
                        "complete_counts": {"outcomes": 0, "watchlist_outcomes": 0},
                        "zero_complete_delta_streaks": {"outcomes": 0, "watchlist_outcomes": 2},
                    }
                ),
                encoding="utf-8",
            )

            result = write_health_summary(
                base,
                {
                    "ok": False,
                    "plan": {"expected_dates": ["2026-04-24"]},
                    "commands": [{"domain": "etf", "returncode": 999, "output": "source down"}],
                    "logs": [],
                },
            )
            body = Path(result["path"]).read_text(encoding="utf-8")

        self.assertEqual(result["today_rows"]["candidates"], 2)
        self.assertEqual(result["today_rows"]["watchlist_snapshots"], 1)
        self.assertIn("[ALERT] concept lag_vs_stock_days=4", body)
        self.assertIn("[ALERT] watchlist_outcomes complete_delta_zero_streak=3", body)
        self.assertIn("[ALERT] domain_errors=1", body)

    def test_health_summary_handles_missing_tables_as_na(self) -> None:
        from offline_daily_update import write_health_summary

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result = write_health_summary(base, {"ok": True, "plan": {"expected_dates": ["2026-04-24"]}})
            body = Path(result["path"]).read_text(encoding="utf-8")

        self.assertIn("| stock | n/a | n/a |", body)
        self.assertIn("- candidates: n/a", body)
        self.assertIn("- outcomes: n/a; complete_delta=0", body)



    def test_repair_market_prefix_maps_stock_and_index_symbols_separately(self) -> None:
        from repair_market_day_akshare import index_market_prefix, market_prefix

        self.assertEqual(market_prefix("000001"), "sz")
        self.assertEqual(market_prefix("002001"), "sz")
        self.assertEqual(market_prefix("300001"), "sz")
        self.assertEqual(market_prefix("301001"), "sz")
        self.assertEqual(market_prefix("600001"), "sh")
        self.assertEqual(market_prefix("688001"), "sh")
        self.assertEqual(market_prefix("830001"), "bj")
        self.assertEqual(index_market_prefix("000001"), "sh")
        self.assertEqual(index_market_prefix("000852"), "sh")
        self.assertEqual(index_market_prefix("399001"), "sz")

    def test_repair_market_continuity_guard_rejects_index_price_in_stock_rows(self) -> None:
        from repair_market_day_akshare import _passes_continuity_guard

        self.assertFalse(
            _passes_continuity_guard(
                {"sec_type": "stock", "sec_code": "000001", "close": 4000.0},
                previous_close=10.0,
                max_jump=0.30,
            )
        )
        self.assertTrue(
            _passes_continuity_guard(
                {"sec_type": "stock", "sec_code": "000001", "close": 10.8},
                previous_close=10.0,
                max_jump=0.30,
            )
        )
        self.assertTrue(
            _passes_continuity_guard(
                {"sec_type": "index", "sec_code": "000001", "close": 4000.0},
                previous_close=10.0,
                max_jump=0.30,
            )
        )

    def test_etf_safe_call_allows_akshare_read_excel_bytes_payload(self) -> None:
        from io import BytesIO

        import pandas as pd

        from backfill_etf_equity_60d_v2 import _safe_call

        payload = BytesIO()
        pd.DataFrame({"基金代码": ["510300"], "基金份额": [1.0]}).to_excel(payload, index=False)
        raw = payload.getvalue()

        def fake_akshare_endpoint():
            return pd.read_excel(raw, engine="openpyxl")

        result = _safe_call(fake_akshare_endpoint)

        self.assertEqual(result.iloc[0]["基金代码"], 510300)
        self.assertEqual(float(result.iloc[0]["基金份额"]), 1.0)



if __name__ == "__main__":
    unittest.main()
