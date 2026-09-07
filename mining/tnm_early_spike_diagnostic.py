"""Read-only TNM V2.3 D+1 first-hour spike diagnostic for 2023--2024.

The production V2.3 run and E: minute source are inputs only.  Every artifact is
written below output/tail-next-morning-v2/v24r-early-spike.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
import zipfile

import numpy as np
import pandas as pd

from mining.tail_next_morning import (
    TailDataError,
    canonical_code,
    is_a_share_code,
    locate_day_source,
    parse_minute_frame,
    validate_minute_window,
    vwap_for_window,
    window_bars,
)


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "v23-development-4dc58bf0106b-adfb3b853979"
RUN_DIR = ROOT / "output" / "tail-next-morning-v2" / "v23-development" / RUN_ID
DEFAULT_OUTPUT = ROOT / "output" / "tail-next-morning-v2" / "v24r-early-spike"
DEFAULT_MINUTE_ROOT = Path(r"E:\分钟数据")
EXPECTED_SENTINEL = {"readable": 3394, "high_touch3": 1007, "vwap5_hit3": 791, "high_only3": 216}
PANEL_FEATURES = (
    "prior10_runup", "drawdown", "range_expansion", "prev_ma5_dist", "prev_ma10_dist",
    "ret1450", "tail_return", "vwap_dist", "activity", "position1450", "tail_location",
    "candidate_count", "breadth_ret1450_pos", "breadth_tail_return_pos",
    "median_ret1450", "iqr_ret1450", "median_tail_return", "iqr_tail_return",
    "breadth_x_vwap_dist", "prior10_x_vwap_dist", "tail_return_x_tail_location",
)
FEATURE_BLOCKS = {
    "daily": ("prior10_runup", "drawdown", "range_expansion", "prev_ma5_dist", "prev_ma10_dist"),
    "intraday": ("ret1450", "tail_return", "vwap_dist", "activity", "position1450", "tail_location"),
    "candidate_market": (
        "candidate_count", "breadth_ret1450_pos", "breadth_tail_return_pos",
        "median_ret1450", "iqr_ret1450", "median_tail_return", "iqr_tail_return",
    ),
}
FEATURE_BLOCKS["base_all"] = tuple(PANEL_FEATURES)
LABELS = ("gap_hit3", "intraday_hit3")
TOP_N = 10


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    temporary.replace(path)


def freeze_manifest(output_dir: str | Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    """Freeze the bounded execution package before any long data pass."""
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    checkpoints = sorted((RUN_DIR / "checkpoints").glob("market_*.json"))
    source_files = ("completion.json", "run_manifest.json", "artifact_manifest.json")
    manifest = {
        "schema": "tnm_v24r_early_spike_manifest_v1",
        "created_at": _utc_now(),
        "source_run_id": RUN_ID,
        "source_run_dir": str(RUN_DIR),
        "source_commit": "63a496fcbb65c9548027d1b5d00d39a46d85bf63",
        "source_checkpoint_count": len(checkpoints),
        "source_hashes": {name: _sha256(RUN_DIR / name) for name in source_files},
        "runner_sha256": _sha256(Path(__file__)),
        "minute_root": str(DEFAULT_MINUTE_ROOT),
        "minute_year_allowlist": ["2023", "2024"],
        "candidate_contract": "all_candidate_rows where short_phase_only=false and liquidity_pass=true",
        "expected_candidate_rows": 126443,
        "corporate_action_status": "unproven_not_applied",
        "formal_economic_go_allowed": False,
        "checkpoint_every_dates": 10,
        "top_n": TOP_N,
        "status": "FROZEN",
        "artifacts": {},
    }
    path = destination / "manifest.json"
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        identity = ("source_run_id", "source_hashes", "runner_sha256", "candidate_contract", "expected_candidate_rows")
        if any(previous.get(key) != manifest.get(key) for key in identity):
            raise TailDataError("frozen_manifest_identity_mismatch")
        return previous
    _write_json(path, manifest)
    return manifest


def _verify_frozen_run_inputs(run_dir: str | Path = RUN_DIR, *, expected_count: int = 474) -> dict[str, Any]:
    """Verify every frozen market checkpoint before loading a derived panel."""
    source = Path(run_dir)
    manifest_path = source / "artifact_manifest.json"
    if not manifest_path.is_file():
        raise TailDataError("source_artifact_manifest_missing")
    artifact_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {
        name: evidence for name, evidence in artifact_manifest.items()
        if str(name).startswith("checkpoints/market_") and str(name).endswith(".json")
    }
    actual_paths = sorted((source / "checkpoints").glob("market_*.json"))
    if len(expected) != expected_count or len(actual_paths) != expected_count:
        raise TailDataError(f"source_checkpoint_count_mismatch:{len(expected)}:{len(actual_paths)}")
    identities: list[str] = []
    for path in actual_paths:
        relative = path.relative_to(source).as_posix()
        evidence = expected.get(relative)
        if evidence is None:
            raise TailDataError(f"source_checkpoint_unmanifested:{relative}")
        size = path.stat().st_size
        if size != int(evidence.get("size_bytes", -1)):
            raise TailDataError(f"source_checkpoint_size_mismatch:{relative}")
        digest = _sha256(path)
        if digest != str(evidence.get("sha256")):
            raise TailDataError(f"source_checkpoint_sha256_mismatch:{relative}")
        identities.append(f"{relative}\0{size}\0{digest}")
    tree_sha256 = hashlib.sha256("\n".join(identities).encode("utf-8")).hexdigest()
    return {
        "status": "PASS", "verified_at": _utc_now(), "checkpoint_count": len(actual_paths),
        "checkpoint_tree_sha256": tree_sha256, "artifact_manifest_sha256": _sha256(manifest_path),
    }


def _buy_from_frame(day_bars: pd.DataFrame | None) -> dict[str, Any]:
    if day_bars is None:
        return {"buy_status": "unavailable_day", "buy_vwap": None}
    try:
        validate_minute_window(day_bars, "14:51", "14:56")
        buy = vwap_for_window(day_bars, "14:51", "14:56")
    except TailDataError as exc:
        return {"buy_status": exc.reason, "buy_vwap": None}
    return {"buy_status": str(buy["status"]), "buy_vwap": buy.get("vwap")}


def label_from_buy(buy_vwap: float | None, next_bars: pd.DataFrame | None) -> dict[str, Any]:
    """Apply the frozen label and causal next-five-minute execution contract."""
    empty = {
        "label_status": "unavailable_buy", "gap_hit3": None, "high_touch3": None,
        "vwap5_hit3": None, "intraday_hit3": None, "high_only3": None,
        "first_high_touch_block": None, "first_vwap5_hit_block": None,
        "causal_exit_window": None, "causal_exit_vwap": None,
        "gross_return": None, "net30": None,
    }
    if buy_vwap is None or not math.isfinite(float(buy_vwap)) or float(buy_vwap) <= 0:
        return empty
    if next_bars is None:
        return {**empty, "label_status": "unavailable_next_day"}
    threshold = float(buy_vwap) * 1.03
    try:
        opening = validate_minute_window(next_bars, "09:30", "09:31")
        morning = validate_minute_window(next_bars, "09:30", "10:30")
    except TailDataError as exc:
        return {**empty, "label_status": f"invalid_first_hour:{exc.reason}"}

    gap = bool(float(opening.iloc[0]["open"]) >= threshold)
    block_vwaps: list[float | None] = []
    first_high: int | None = None
    first_vwap: int | None = None
    for block in range(12):
        start = block * 5
        bars = morning.iloc[start:start + 5]
        if first_high is None and float(bars["high"].max()) >= threshold:
            first_high = block
        traded = bars[bars["volume"] > 0]
        amount, volume = float(traded["amount"].sum()), float(traded["volume"].sum())
        value = amount / volume if volume > 0 and not (traded["amount"] <= 0).any() else None
        block_vwaps.append(value)
        if first_vwap is None and value is not None and value >= threshold:
            first_vwap = block
    high_touch = first_high is not None
    vwap_hit: bool | None = first_vwap is not None if all(value is not None for value in block_vwaps) else (True if first_vwap is not None else None)
    exit_block = first_high + 1 if first_high is not None else 12
    exit_start_minutes = 9 * 60 + 30 + exit_block * 5
    exit_end_minutes = exit_start_minutes + 5
    start_label = f"{exit_start_minutes // 60:02d}:{exit_start_minutes % 60:02d}"
    end_label = f"{exit_end_minutes // 60:02d}:{exit_end_minutes % 60:02d}"
    try:
        validate_minute_window(next_bars, start_label, end_label)
        sell = vwap_for_window(next_bars, start_label, end_label)
    except TailDataError as exc:
        sell = {"status": exc.reason, "vwap": None}
    exit_vwap = sell.get("vwap") if sell.get("status") == "ready" else None
    gross = float(exit_vwap) / float(buy_vwap) - 1.0 if exit_vwap is not None else None
    return {
        "label_status": "ready" if gross is not None else f"unavailable_exit:{sell.get('status')}",
        "gap_hit3": gap,
        "high_touch3": high_touch,
        "vwap5_hit3": vwap_hit,
        "intraday_hit3": bool(high_touch and not gap),
        "high_only3": bool(high_touch and vwap_hit is False) if vwap_hit is not None else None,
        "first_high_touch_block": first_high,
        "first_vwap5_hit_block": first_vwap,
        "causal_exit_window": f"{start_label}-{end_label}",
        "causal_exit_vwap": exit_vwap,
        "gross_return": gross,
        "net30": gross - 0.003 if gross is not None else None,
    }


def label_early_spike(day_bars: pd.DataFrame | None, next_bars: pd.DataFrame | None) -> dict[str, Any]:
    buy = _buy_from_frame(day_bars)
    return {**buy, **label_from_buy(buy["buy_vwap"], next_bars)}


def _checkpoint_paths() -> list[Path]:
    paths = sorted((RUN_DIR / "checkpoints").glob("market_*.json"))
    if len(paths) != 474:
        raise TailDataError(f"checkpoint_count_mismatch:{len(paths)}")
    return paths


def _candidate_base(row: Mapping[str, Any], market: Mapping[str, Any]) -> dict[str, Any]:
    close = row.get("close1450")
    ma5, ma10 = row.get("prev_ma5"), row.get("prev_ma10")
    result = {
        "trade_date": str(row["trade_date"]), "sec_code": canonical_code(row["sec_code"]),
        "eligible_pass": bool(row.get("eligible_pass")), "selected": bool(row.get("selected")),
        "channel": row.get("channel"),
        "prev_ma5_dist": float(close) / float(ma5) - 1.0 if close and ma5 else None,
        "prev_ma10_dist": float(close) / float(ma10) - 1.0 if close and ma10 else None,
    }
    for feature in ("prior10_runup", "drawdown", "range_expansion", "ret1450", "tail_return", "vwap_dist", "activity", "position1450", "tail_location"):
        result[feature] = row.get(feature)
    result.update(market)
    result["breadth_x_vwap_dist"] = _product(result.get("breadth_ret1450_pos"), result.get("vwap_dist"))
    result["prior10_x_vwap_dist"] = _product(result.get("prior10_runup"), result.get("vwap_dist"))
    result["tail_return_x_tail_location"] = _product(result.get("tail_return"), result.get("tail_location"))
    return result


def _product(left: Any, right: Any) -> float | None:
    try:
        value = float(left) * float(right)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _market_features(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    ret = pd.to_numeric(pd.Series([row.get("ret1450") for row in rows]), errors="coerce")
    tail = pd.to_numeric(pd.Series([row.get("tail_return") for row in rows]), errors="coerce")
    def stats(series: pd.Series, prefix: str) -> dict[str, Any]:
        valid = series.dropna()
        return {
            f"breadth_{prefix}_pos": float((valid > 0).mean()) if len(valid) else None,
            f"median_{prefix}": float(valid.median()) if len(valid) else None,
            f"iqr_{prefix}": float(valid.quantile(.75) - valid.quantile(.25)) if len(valid) else None,
        }
    return {"candidate_count": len(rows), **stats(ret, "ret1450"), **stats(tail, "tail_return")}


def load_frozen_candidates() -> tuple[list[str], dict[str, list[dict[str, Any]]], dict[str, int]]:
    by_date: dict[str, list[dict[str, Any]]] = {}
    all_count = eligible_count = 0
    corporate_states: set[str] = set()
    for path in _checkpoint_paths():
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload["result"]["all_candidate_rows"]
        all_count += len(rows)
        eligible_count += sum(bool(row.get("eligible_pass")) for row in rows)
        selected = [row for row in rows if not bool(row.get("short_phase_only", False)) and row.get("liquidity_pass") is True]
        market = _market_features(selected)
        date_key = path.stem.removeprefix("market_")
        by_date[date_key] = [_candidate_base(row, market) for row in selected]
        corporate_states.update(str(row.get("corporate_action_filter")) for row in selected)
    dates = sorted(by_date)
    counts = {"checkpoints": len(dates), "all_candidate_rows": all_count, "candidate_rows": sum(map(len, by_date.values())), "eligible_rows": eligible_count}
    if counts != {"checkpoints": 474, "all_candidate_rows": 199643, "candidate_rows": 126443, "eligible_rows": 11884}:
        raise TailDataError(f"candidate_sentinel_mismatch:{counts}")
    if corporate_states != {"unproven_not_applied"}:
        raise TailDataError(f"corporate_action_state_mismatch:{sorted(corporate_states)}")
    return dates, by_date, counts


def _load_requested_frames(minute_root: Path, trade_date: str, codes: Iterable[str]) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    key = str(trade_date).replace("-", "")
    if key[:4] not in {"2023", "2024"}:
        raise TailDataError(f"diagnostic_year_guard:{key}")
    requested = {canonical_code(code) for code in codes if is_a_share_code(code)}
    if not requested:
        return {}, {}
    source, source_kind = locate_day_source(minute_root, key)
    payloads: dict[str, bytes] = {}
    duplicates: set[str] = set()
    if source_kind == "zip":
        with zipfile.ZipFile(source) as archive:
            for name in archive.namelist():
                if name.endswith("/") or PurePosixPath(name).suffix.lower() != ".csv":
                    continue
                code = canonical_code(PurePosixPath(name).stem)
                if code not in requested:
                    continue
                if code in payloads:
                    duplicates.add(code)
                else:
                    payloads[code] = archive.read(name)
    else:
        for item in source.rglob("*.csv"):
            code = canonical_code(item.stem)
            if code not in requested:
                continue
            if code in payloads:
                duplicates.add(code)
            else:
                payloads[code] = item.read_bytes()
    frames: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {code: "minute_code_missing" for code in requested - set(payloads)}
    for code, payload in payloads.items():
        if code in duplicates:
            errors[code] = "ambiguous_minute_code_file"
            continue
        try:
            frames[code] = parse_minute_frame(pd.read_csv(io.BytesIO(payload)))
        except Exception as exc:
            errors[code] = exc.reason if isinstance(exc, TailDataError) else "minute_csv_unreadable"
    return frames, errors


def _pending_rows(rows: list[Mapping[str, Any]], frames: Mapping[str, pd.DataFrame], errors: Mapping[str, str]) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for row in rows:
        code = str(row["sec_code"])
        buy = _buy_from_frame(frames.get(code))
        if code in errors and buy["buy_status"] == "unavailable_day":
            buy["buy_status"] = errors[code]
        pending.append({**row, **buy})
    return pending


def _finalize_pending(pending: list[Mapping[str, Any]], next_date: str | None, frames: Mapping[str, pd.DataFrame], errors: Mapping[str, str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in pending:
        code = str(row["sec_code"])
        label = label_from_buy(row.get("buy_vwap"), frames.get(code))
        if next_date is None:
            label["label_status"] = "unavailable_next_day_within_2024"
        elif code in errors and label["label_status"] == "unavailable_next_day":
            label["label_status"] = errors[code]
        result.append({**row, "next_trade_date": next_date, **label})
    return result


def _write_panel_part(parts_dir: Path, index: int, rows: list[Mapping[str, Any]]) -> Path:
    dates = sorted({str(row["trade_date"]) for row in rows})
    path = parts_dir / f"part_{index:04d}_{dates[0]}_{dates[-1]}.parquet"
    temporary = path.with_suffix(".parquet.tmp")
    pd.DataFrame(rows).to_parquet(temporary, index=False)
    temporary.replace(path)
    return path


def build_label_panel(
    dates: list[str], by_date: Mapping[str, list[Mapping[str, Any]]], output_dir: str | Path,
    *, minute_root: str | Path = DEFAULT_MINUTE_ROOT, name: str = "label_panel", resume: bool = True,
) -> pd.DataFrame:
    """Read each needed day once, checkpoint every ten finalized D dates, and resume safely."""
    destination = Path(output_dir)
    parts_dir = destination / f"{name}_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    part_paths = sorted(parts_dir.glob("part_*.parquet")) if resume else []
    finalized: set[str] = set()
    for path in part_paths:
        finalized.update(pd.read_parquet(path, columns=["trade_date"])["trade_date"].astype(str).unique())
    target_dates = [date for date in dates if date in by_date]
    start = next((index for index, value in enumerate(target_dates) if value not in finalized), len(target_dates))
    if start < len(target_dates):
        date0 = target_dates[start]
        frames, errors = _load_requested_frames(Path(minute_root), date0, (row["sec_code"] for row in by_date[date0]))
        pending = _pending_rows(list(by_date[date0]), frames, errors)
        buffer: list[dict[str, Any]] = []
        buffered_dates: list[str] = []
        part_index = len(part_paths)
        for position in range(start + 1, len(target_dates)):
            current = target_dates[position]
            own_codes = {str(row["sec_code"]) for row in by_date[current]}
            prior_codes = {str(row["sec_code"]) for row in pending}
            frames, errors = _load_requested_frames(Path(minute_root), current, own_codes | prior_codes)
            prior_date = target_dates[position - 1]
            buffer.extend(_finalize_pending(pending, current, frames, errors))
            buffered_dates.append(prior_date)
            pending = _pending_rows(list(by_date[current]), frames, errors)
            if len(buffered_dates) == 10:
                path = _write_panel_part(parts_dir, part_index, buffer)
                print(f"checkpoint {path.name} rows={len(buffer)}", flush=True)
                part_paths.append(path)
                part_index += 1
                buffer, buffered_dates = [], []
                _write_json(destination / f"{name}_progress.json", {"status": "RUNNING", "finalized_through": prior_date, "updated_at": _utc_now()})
        buffer.extend(_finalize_pending(pending, None, {}, {}))
        buffered_dates.append(target_dates[-1])
        if buffer:
            path = _write_panel_part(parts_dir, part_index, buffer)
            part_paths.append(path)
            print(f"checkpoint {path.name} rows={len(buffer)}", flush=True)
    panel = pd.concat((pd.read_parquet(path) for path in sorted(parts_dir.glob("part_*.parquet"))), ignore_index=True)
    panel = panel.sort_values(["trade_date", "sec_code"], kind="stable").reset_index(drop=True)
    panel.to_parquet(destination / f"{name}.parquet", index=False)
    _write_funnel(panel, destination, name)
    _write_json(destination / f"{name}_progress.json", {"status": "SUCCEEDED", "rows": len(panel), "dates": int(panel["trade_date"].nunique()), "updated_at": _utc_now()})
    return panel


def _write_funnel(panel: pd.DataFrame, destination: Path, name: str) -> None:
    daily = panel.groupby("trade_date", sort=True).agg(
        candidates=("sec_code", "size"), buy_ready=("buy_vwap", "count"),
        label_ready=("label_status", lambda values: int((values == "ready").sum())),
        gap_hit3=("gap_hit3", lambda values: int((values == True).sum())),
        high_touch3=("high_touch3", lambda values: int((values == True).sum())),
        vwap5_hit3=("vwap5_hit3", lambda values: int((values == True).sum())),
    ).reset_index()
    daily.to_csv(destination / f"{name}_daily_funnel.csv", index=False)
    anomalies = panel[panel["label_status"] != "ready"].groupby(["trade_date", "label_status"], dropna=False).size().rename("rows").reset_index()
    anomalies.to_csv(destination / f"{name}_anomalies.csv", index=False)


def run_sentinel(output_dir: str | Path = DEFAULT_OUTPUT, minute_root: str | Path = DEFAULT_MINUTE_ROOT) -> dict[str, Any]:
    dates, _, _ = load_frozen_candidates()
    events = pd.read_csv(RUN_DIR / "event_results.csv.gz", dtype={"trade_date": str, "sec_code": str})
    events = events[events["strategy_or_control"] == "strategy"].copy()
    by_date: dict[str, list[dict[str, Any]]] = {}
    for date_key, group in events.groupby("trade_date", sort=True):
        by_date[str(date_key)] = [{"trade_date": str(date_key), "sec_code": canonical_code(code)} for code in group["sec_code"]]
    panel = build_label_panel(dates, by_date, output_dir, minute_root=minute_root, name="sentinel_panel")
    # "Readable" means the price-based first-hour label is observable.  A
    # zero-volume five-minute block makes only the VWAP label/exit unavailable;
    # it must not erase an otherwise valid high-touch observation.
    readable = panel[panel["gap_hit3"].notna() & panel["high_touch3"].notna()]
    actual = {
        "readable": len(readable), "high_touch3": int(readable["high_touch3"].sum()),
        "vwap5_hit3": int(readable["vwap5_hit3"].sum()), "high_only3": int(readable["high_only3"].sum()),
    }
    result = {"expected": EXPECTED_SENTINEL, "actual": actual, "status": "PASS" if actual == EXPECTED_SENTINEL else "FAIL", "created_at": _utc_now()}
    _write_json(Path(output_dir) / "sentinel.json", result)
    return result


def run_full_panel(output_dir: str | Path = DEFAULT_OUTPUT, minute_root: str | Path = DEFAULT_MINUTE_ROOT) -> pd.DataFrame:
    dates, by_date, _ = load_frozen_candidates()
    return build_label_panel(dates, by_date, output_dir, minute_root=minute_root, name="label_panel")


def _month_index(value: str) -> int:
    return int(value[:4]) * 12 + int(value[4:6]) - 1


def _rolling_folds(dates: list[str]) -> list[tuple[str, list[str], list[str]]]:
    months = sorted({value[:6] for value in dates})
    folds: list[tuple[str, list[str], list[str]]] = []
    for start in range(6, len(months) - 1, 2):
        test_months = set(months[start:start + 2])
        test_dates = [value for value in dates if value[:6] in test_months]
        train_dates = [value for value in dates if _month_index(value) < _month_index(months[start])]
        if not train_dates or not test_dates:
            continue
        full_training_boundary = pd.Timestamp(min(train_dates)) + pd.DateOffset(months=6)
        if pd.Timestamp(min(test_dates)) < full_training_boundary:
            continue
        if len(train_dates) > 2:
            train_dates = train_dates[:-2]
        if train_dates and test_dates:
            folds.append((f"{months[start]}-{months[start + 1]}", train_dates, test_dates))
    return folds


def _fit_score(train: pd.DataFrame, test: pd.DataFrame, features: tuple[str, ...], label: str) -> np.ndarray:
    x_train = train.loc[:, features].apply(pd.to_numeric, errors="coerce").to_numpy(float)
    x_test = test.loc[:, features].apply(pd.to_numeric, errors="coerce").to_numpy(float)
    median = np.nanmedian(x_train, axis=0)
    median = np.where(np.isfinite(median), median, 0.0)
    x_train = np.where(np.isfinite(x_train), x_train, median)
    x_test = np.where(np.isfinite(x_test), x_test, median)
    mean, scale = x_train.mean(axis=0), x_train.std(axis=0)
    scale = np.where(scale > 1e-12, scale, 1.0)
    x_train, x_test = (x_train - mean) / scale, (x_test - mean) / scale
    design = np.column_stack([np.ones(len(x_train)), x_train])
    penalty = np.eye(design.shape[1]) * 1e-3
    penalty[0, 0] = 0
    target = train[label].astype(float).to_numpy()
    beta = np.linalg.solve(design.T @ design + penalty, design.T @ target)
    return np.column_stack([np.ones(len(x_test)), x_test]) @ beta


def _seeded_random(date_key: str, code: str, label: str, fold: str) -> float:
    raw = hashlib.sha256(f"20260831|{date_key}|{code}|{label}|{fold}".encode()).digest()[:8]
    return int.from_bytes(raw, "big") / 2**64


def _spearman(left: pd.Series, right: pd.Series) -> float | None:
    """Spearman correlation without the optional SciPy dependency."""
    values = pd.DataFrame({"left": left, "right": right}).replace([np.inf, -np.inf], np.nan).dropna()
    if len(values) < 2 or values["left"].nunique() < 2 or values["right"].nunique() < 2:
        return None
    corr = values["left"].rank(method="average").corr(values["right"].rank(method="average"))
    return float(corr) if pd.notna(corr) else None


def _select_top_n(frame: pd.DataFrame, score: np.ndarray, label: str, *, top_n: int = TOP_N) -> tuple[pd.DataFrame, pd.DataFrame]:
    scored = frame[["trade_date", "sec_code", label, "net30"]].copy()
    scored["score"] = score
    selected = scored.sort_values(
        ["trade_date", "score", "sec_code"], ascending=[True, False, True], kind="stable",
    ).groupby("trade_date", sort=False).head(top_n)
    return scored, selected


def _common_complete_dates(selections: Mapping[str, pd.DataFrame], *, top_n: int = TOP_N) -> set[str]:
    if not selections:
        return set()
    all_dates = set.intersection(*(set(frame["trade_date"].astype(str)) for frame in selections.values()))
    complete: set[str] = set()
    for trade_date in all_dates:
        if all(
            len(day := frame[frame["trade_date"].astype(str) == trade_date]) == top_n
            and day["net30"].notna().all()
            for frame in selections.values()
        ):
            complete.add(trade_date)
    return complete


def _daily_ic(scored: pd.DataFrame, label: str) -> pd.Series:
    return scored.groupby("trade_date", sort=True).apply(
        lambda day: _spearman(day["score"], day[label].astype(float)), include_groups=False,
    ).dropna()


def _selection_metrics(
    scored: pd.DataFrame, selected: pd.DataFrame, label: str, common_dates: set[str], *, top_n: int = TOP_N,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    trade_dates = sorted(scored["trade_date"].astype(str).unique())
    ic = _daily_ic(scored, label)
    own_complete = {
        date for date in trade_dates
        if len(day := selected[selected["trade_date"].astype(str) == date]) == top_n and day["net30"].notna().all()
    }
    daily_rows: list[dict[str, Any]] = []
    for trade_date in trade_dates:
        day = selected[selected["trade_date"].astype(str) == trade_date]
        common = trade_date in common_dates
        daily_rows.append({
            "trade_date": trade_date,
            "daily_ic": float(ic.loc[trade_date]) if trade_date in ic.index else None,
            "top_n_hit_rate": float(day[label].mean()) if len(day) == top_n else None,
            "top_n_net30": float(day["net30"].mean()) if common else None,
            "selected_rows": len(day), "missing_net30_rows": int(day["net30"].isna().sum()),
            "model_top_n_complete": trade_date in own_complete, "common_complete_day": common,
        })
    common_frame = pd.DataFrame(row for row in daily_rows if row["common_complete_day"])
    ic_mean = float(ic.mean()) if len(ic) else None
    return {
        "selected_rows": len(selected), "test_days": int(scored["trade_date"].nunique()),
        "hit_rate": float(common_frame["top_n_hit_rate"].mean()) if len(common_frame) else None,
        "daily_net30": float(common_frame["top_n_net30"].mean()) if len(common_frame) else None,
        "net30_coverage": float(selected["net30"].notna().mean()),
        "daily_ic_mean": ic_mean, "daily_ic_days": len(ic),
        "daily_ic_positive_rate": float((ic > 0).mean()) if len(ic) else None,
        "common_complete_days": len(common_dates), "excluded_common_days": len(trade_dates) - len(common_dates),
        "model_incomplete_days": len(trade_dates) - len(own_complete),
        "direction": 1 if ic_mean is not None and ic_mean > 0 else -1 if ic_mean is not None and ic_mean < 0 else 0,
    }, daily_rows


def _gate_summary(folds: pd.DataFrame, daily: pd.DataFrame, label: str) -> dict[str, Any]:
    base_folds = folds[(folds["label"] == label) & (folds["model"] == "base_all")]
    relevant_daily = daily[(daily["label"] == label) & daily["common_complete_day"]]
    pooled = relevant_daily.groupby("model")["top_n_net30"].mean()
    control_values = pooled[pooled.index.to_series().str.startswith("control_").to_numpy()]
    strongest_name = str(control_values.idxmax())
    strongest_value = float(control_values.max())
    base_daily = relevant_daily[relevant_daily["model"] == "base_all"]
    daily_net30 = float(base_daily["top_n_net30"].mean())
    base_by_fold = base_folds.set_index("fold")["daily_net30"]
    strongest_by_fold = folds[(folds["label"] == label) & (folds["model"] == strongest_name)].set_index("fold")["daily_net30"]
    direction_consistency = float((base_folds["direction"] > 0).mean())
    return {
        "direction_consistency": direction_consistency,
        "daily_ic_mean": float(base_daily["daily_ic"].mean()),
        "daily_net30": daily_net30,
        "positive_net_fold_consistency": float((base_folds["daily_net30"] > 0).mean()),
        "control_win_fold_consistency": float((base_by_fold > strongest_by_fold).mean()),
        "strongest_control": strongest_name, "strongest_control_daily_net30": strongest_value,
        "common_complete_days": int(base_daily["trade_date"].nunique()),
        "excluded_incomplete_days": int(
            daily[(daily["label"] == label) & (daily["model"] == "base_all") & ~daily["common_complete_day"]]["trade_date"].nunique()
        ),
        "passes": bool(direction_consistency >= .70 and daily_net30 > 0 and daily_net30 > strongest_value),
    }


def _feature_results(panel: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for label in LABELS:
        ready = panel[panel[label].notna()].copy()
        for feature in PANEL_FEATURES:
            values = pd.to_numeric(ready[feature], errors="coerce")
            valid = values.notna()
            x, y = values[valid], ready.loc[valid, label].astype(bool)
            pooled = float(x.std(ddof=0)) if len(x) else math.nan
            smd = (float(x[y].mean()) - float(x[~y].mean())) / pooled if pooled > 0 and y.any() and (~y).any() else math.nan
            corr = _spearman(x, y.astype(float)) if len(x) else None
            quantiles: list[float | None] = [None] * 5
            monotonic = None
            try:
                bins = pd.qcut(x.rank(method="first"), 5, labels=False)
                rates = y.groupby(bins).mean()
                quantiles = [float(rates.get(index, math.nan)) for index in range(5)]
                monotonic = bool(all(quantiles[i] <= quantiles[i + 1] for i in range(4)) or all(quantiles[i] >= quantiles[i + 1] for i in range(4)))
            except ValueError:
                pass
            rows.append({"label": label, "feature": feature, "n": int(valid.sum()), "standardized_mean_difference": smd, "spearman": corr, "monotonic_quintiles": monotonic, **{f"q{index + 1}_rate": value for index, value in enumerate(quantiles)}})
    return pd.DataFrame(rows)


def analyze(output_dir: str | Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    destination = Path(output_dir)
    input_verification = _verify_frozen_run_inputs()
    _write_json(destination / "input_verification.json", input_verification)
    sentinel = json.loads((destination / "sentinel.json").read_text(encoding="utf-8"))
    panel = pd.read_parquet(destination / "label_panel.parquet")
    if sentinel["status"] != "PASS" or len(panel) != 126443:
        conclusion = "blocked_data_integrity"
        fold_frame, feature_frame = pd.DataFrame(), _feature_results(panel)
        fold_frame.to_csv(destination / "fold_results.csv", index=False)
        feature_frame.to_csv(destination / "feature_results.csv", index=False)
        summary = {"conclusion": conclusion, "sentinel": sentinel, "panel_rows": len(panel), "created_at": _utc_now()}
        _write_json(destination / "summary.json", summary)
        _write_report(destination, summary, fold_frame, feature_frame)
        return summary
    valid_dates = sorted(panel[panel["gap_hit3"].notna()]["trade_date"].astype(str).unique())
    folds = _rolling_folds(valid_dates)
    rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    for fold_name, train_dates, test_dates in folds:
        train = panel[panel["trade_date"].astype(str).isin(train_dates)]
        test = panel[panel["trade_date"].astype(str).isin(test_dates)].copy()
        for label in LABELS:
            train_ready = train[train[label].notna()]
            test_ready = test[test[label].notna()]
            scored_by_model: dict[str, pd.DataFrame] = {}
            selected_by_model: dict[str, pd.DataFrame] = {}
            for block, features in FEATURE_BLOCKS.items():
                score = _fit_score(train_ready, test_ready, features, label)
                scored_by_model[block], selected_by_model[block] = _select_top_n(test_ready, score, label)
            controls = {
                "control_random": np.array([_seeded_random(str(row.trade_date), str(row.sec_code), label, fold_name) for row in test_ready.itertuples()]),
                "control_activity": pd.to_numeric(test_ready["activity"], errors="coerce").fillna(-np.inf).to_numpy(),
                "control_ret1450": pd.to_numeric(test_ready["ret1450"], errors="coerce").fillna(-np.inf).to_numpy(),
            }
            for name, score in controls.items():
                scored_by_model[name], selected_by_model[name] = _select_top_n(test_ready, score, label)
            common_dates = _common_complete_dates(selected_by_model)
            for model in scored_by_model:
                metrics, model_daily = _selection_metrics(
                    scored_by_model[model], selected_by_model[model], label, common_dates,
                )
                rows.append({"fold": fold_name, "label": label, "model": model, "train_days": len(train_dates), **metrics})
                daily_rows.extend({"fold": fold_name, "label": label, "model": model, **row} for row in model_daily)
    fold_frame = pd.DataFrame(rows)
    fold_frame.to_csv(destination / "fold_results.csv", index=False)
    daily_frame = pd.DataFrame(daily_rows)
    daily_frame.to_csv(destination / "daily_results.csv", index=False)
    feature_frame = _feature_results(panel)
    feature_frame.to_csv(destination / "feature_results.csv", index=False)
    gates = {label: _gate_summary(fold_frame, daily_frame, label) for label in LABELS}
    any_gate = any(value["passes"] for value in gates.values())
    conclusion = "diagnostic_signal_go_research" if any_gate else "no_predictable_edge"
    summary = {
        "conclusion": conclusion, "formal_trading_claim_allowed": False,
        "corporate_action_status": "unproven_not_applied", "sentinel": sentinel,
        "input_verification": input_verification,
        "panel_rows": len(panel), "panel_dates": int(panel["trade_date"].nunique()),
        "statistical_unit": "trade_date", "minimum_candidates_per_day": int(panel.groupby("trade_date").size().min()),
        "days_below_top_n": int((panel.groupby("trade_date").size() < TOP_N).sum()),
        "label_ready_rows": int((panel["label_status"] == "ready").sum()),
        "fold_count": len(folds), "top_n": TOP_N, "gates": gates,
        "extension_permitted": bool(any_gate), "extension_executed": False,
        "extension_stop_reason": "base_signal_failed" if not any_gate else "corporate_action_unproven_research_only",
        "created_at": _utc_now(), "lessons_decision": "skip",
    }
    _write_json(destination / "summary.json", summary)
    _write_report(destination, summary, fold_frame, feature_frame)
    _finalize_manifest(destination)
    return summary


def _write_report(destination: Path, summary: Mapping[str, Any], folds: pd.DataFrame, features: pd.DataFrame) -> None:
    sentinel = summary["sentinel"]
    lines = [
        "# TNM V2.4R D+1 首小时冲高研究报告", "",
        f"## 结论：`{summary['conclusion']}`", "",
        "研究严格使用冻结 V2.3 候选和 2023–2024 本地分钟数据。公司行为调整因子仍为 `unproven_not_applied`，因此结果只用于诊断研究，不能称为可交易策略。", "",
        "## 标签哨兵", "",
        f"- 可读事件：{sentinel['actual']['readable']}（期望 {sentinel['expected']['readable']}）",
        f"- high 触及 +3%：{sentinel['actual']['high_touch3']}；完整五分钟 VWAP 达 +3%：{sentinel['actual']['vwap5_hit3']}；high-only：{sentinel['actual']['high_only3']}",
        f"- 哨兵状态：{sentinel['status']}", "",
        "## 完整面板与滚动验证", "",
        f"- 候选行：{summary.get('panel_rows')}；可执行标签行：{summary.get('label_ready_rows')}；滚动折数：{summary.get('fold_count')}",
        "- 首个测试日距首个训练样本至少完整六个月；两个月测试、移动两个月，训练尾部 purge 两个交易日；横截面 Top-10。",
        "- 方向先逐交易日计算横截面 Spearman/IC，再在每折逐日等权；net30 在全部测试日逐日等权汇总，不对折等权。", "",
    ]
    for label, gate in summary.get("gates", {}).items():
        lines.append(f"- `{label}`：方向一致率 {gate['direction_consistency']:.1%}，逐日 IC 均值 {gate['daily_ic_mean']:.4f}，正 net30 折占比 {gate['positive_net_fold_consistency']:.1%}，胜最强对照折占比 {gate['control_win_fold_consistency']:.1%}；共同完整日 {gate['common_complete_days']}、排除不完整日 {gate['excluded_incomplete_days']}；基础块每日 net30 {gate['daily_net30']:.4%}，最强对照 `{gate['strongest_control']}` {gate['strongest_control_daily_net30']:.4%}，gate={'PASS' if gate['passes'] else 'FAIL'}。")
    lines.extend([
        "", "## 特征与限制", "",
        "`feature_results.csv` 给出正负样本标准化差异、Spearman 秩相关和五分位命中率；`daily_results.csv` 保留逐日 IC、Top-N 完整性和逐日 net30；`fold_results.csv` 给出逐日等权后的每折结果及三个同日 same-N 对照。",
        "", "指数、行业、聚类、soft-peer 与额外阈值均未运行。基础 gate 失败时这是冻结停止规则；基础 gate 通过时，公司行为口径仍把结论限制在进一步诊断研究。",
        "", "正成交量 VWAP 仅是诊断性价格口径。研究没有模拟涨跌停排队、微量成交的可获得数量或参与率；统一 30bp 成本不覆盖无法成交风险。",
        "", "## 数据完整性", "",
        "末个 2024 候选日没有允许范围内的 D+1，明确记为 `unavailable_next_day_within_2024`。其余缺失和异常逐日记录在 `label_panel_anomalies.csv`，未用未来日期替补。",
        "", "## Lessons", "", "决定：`skip`。本任务没有产生超出现有时间隔离、失败关闭和可恢复 checkpoint 规则的新通用规则。", "",
    ])
    (destination / "research_report.md").write_text("\n".join(lines), encoding="utf-8")


def _finalize_manifest(destination: Path) -> None:
    path = destination / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    artifacts = {}
    for artifact in sorted(destination.iterdir()):
        if artifact.is_file() and artifact.name != path.name and not artifact.name.endswith(".tmp"):
            artifacts[artifact.name] = {"bytes": artifact.stat().st_size, "sha256": _sha256(artifact)}
    manifest.update({"status": "SUCCEEDED", "completed_at": _utc_now(), "artifacts": artifacts})
    _write_json(path, manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minute-root", type=Path, default=DEFAULT_MINUTE_ROOT)
    parser.add_argument("--freeze-manifest", action="store_true")
    parser.add_argument("--sentinel-only", action="store_true")
    parser.add_argument("--full-panel", action="store_true")
    parser.add_argument("--analyze", action="store_true")
    args = parser.parse_args(argv)
    if args.freeze_manifest:
        print(json.dumps(freeze_manifest(args.output_dir), ensure_ascii=False, sort_keys=True))
    if args.sentinel_only:
        print(json.dumps(run_sentinel(args.output_dir, args.minute_root), ensure_ascii=False, sort_keys=True))
    if args.full_panel:
        panel = run_full_panel(args.output_dir, args.minute_root)
        print(json.dumps({"panel_rows": len(panel)}, sort_keys=True))
    if args.analyze:
        print(json.dumps(analyze(args.output_dir), ensure_ascii=False, sort_keys=True))
    if not any((args.freeze_manifest, args.sentinel_only, args.full_panel, args.analyze)):
        freeze_manifest(args.output_dir)
        print(json.dumps(run_sentinel(args.output_dir, args.minute_root), ensure_ascii=False, sort_keys=True))
        run_full_panel(args.output_dir, args.minute_root)
        print(json.dumps(analyze(args.output_dir), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
