from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


SCREENING_DEFINITION_VERSION = "v2.6"
CAPABILITY_TOP_N = 20


@dataclass(frozen=True)
class CapabilityDefinition:
    strategy_id: str
    capability: str
    label: str
    subtype: str | None
    fields: tuple[str, ...]
    sort_keys: tuple[str, ...]
    supports_intraday: bool
    required_dependencies: tuple[str, ...] = ("price", "metadata")
    optional_dependencies: tuple[str, ...] = ()
    version: str = SCREENING_DEFINITION_VERSION


CAPABILITY_REGISTRY: tuple[CapabilityDefinition, ...] = (
    CapabilityDefinition(
        "strong_trend", "A", "强趋势", None,
        ("reference_price", "strength_tier", "score", "rps_recent_pct", "rps_prior_pct", "path_context"),
        ("score", "rps_recent_pct", "sec_code"), True, ("price", "metadata"), ("benchmark",),
    ),
    CapabilityDefinition(
        "compression_launch", "B", "异动起爆", "compression_launch",
        ("reference_price", "event_subtype", "score", "activity_pct", "path_context"),
        ("score", "activity_pct", "sec_code"), True, ("price", "activity", "metadata"), (),
    ),
    CapabilityDefinition(
        "momentum_anomaly", "B", "异动起爆", "momentum_anomaly",
        ("reference_price", "event_subtype", "score", "activity_pct", "path_context"),
        ("score", "activity_pct", "sec_code"), True, ("price", "activity", "metadata"), (),
    ),
    CapabilityDefinition(
        "second_launch", "C", "回调支撑", None,
        ("reference_price", "strength_tier", "state", "pullback_pct", "activity_expand"),
        ("state", "strength_tier", "sec_code"), True, ("price", "metadata"), (),
    ),
    CapabilityDefinition(
        "base_breakout", "D", "平台突破", None,
        ("reference_price", "close_box_width", "contraction_ratio", "activity_pct", "breakout_pct"),
        ("close_box_width", "contraction_ratio", "activity_pct", "sec_code"), True, ("price", "activity", "metadata"), (),
    ),
    CapabilityDefinition(
        "counter_trend_rs", "E", "逆势强度", None,
        ("reference_price", "score", "rs_ret20", "rs_breakout_pct", "separation_raw"),
        ("score", "sec_code"), True, ("price", "benchmark", "metadata"), (),
    ),
)

LEGACY_STRATEGY_IDS = (
    "momentum_breakout",
    "rps_stock_top20",
    "trend_embryo",
    "true_leader",
    "launch_burst",
)


def formal_definitions(capability: str | None = None) -> tuple[CapabilityDefinition, ...]:
    return tuple(
        definition
        for definition in CAPABILITY_REGISTRY
        if capability is None or definition.capability == str(capability)
    )


def formal_strategy_ids() -> tuple[str, ...]:
    return tuple(definition.strategy_id for definition in CAPABILITY_REGISTRY)


def formal_definition(strategy_id: str) -> CapabilityDefinition:
    for definition in CAPABILITY_REGISTRY:
        if definition.strategy_id == str(strategy_id):
            return definition
    raise KeyError(f"Unknown formal screening strategy: {strategy_id}")


def visible_strategy_ids() -> tuple[str, ...]:
    return (*formal_strategy_ids(), *LEGACY_STRATEGY_IDS)


def shortlist_capability_rows(rows: pd.DataFrame, *, top_n: int = CAPABILITY_TOP_N) -> pd.DataFrame:
    """Deduplicate one capability before assigning its user-visible ranks.

    Evaluators own eligibility.  This boundary only merges already eligible
    subtypes, retains the strongest score, and makes the Top-N rule explicit.
    """
    if rows.empty:
        return rows.copy()
    result = rows.copy()
    if "sec_code" not in result.columns:
        raise ValueError("capability shortlist requires sec_code")
    result["sec_code"] = result["sec_code"].astype(str).str.zfill(6)
    score_source = result["score"] if "score" in result.columns else pd.Series(float("-inf"), index=result.index)
    activity_source = result["activity_pct"] if "activity_pct" in result.columns else pd.Series(float("-inf"), index=result.index)
    result["_score"] = pd.to_numeric(score_source, errors="coerce").fillna(float("-inf"))
    result["_activity"] = pd.to_numeric(activity_source, errors="coerce").fillna(float("-inf"))
    result = result.sort_values(["_score", "_activity", "sec_code"], ascending=[False, False, True], kind="stable")
    if "event_subtype" in result.columns:
        subtype_evidence = (
            result.groupby("sec_code", sort=False)["event_subtype"]
            .agg(lambda values: tuple(dict.fromkeys(str(value) for value in values if pd.notna(value))))
        )
        result = result.drop_duplicates("sec_code", keep="first")
        result["subtype_evidence"] = result["sec_code"].map(subtype_evidence)
    else:
        result = result.drop_duplicates("sec_code", keep="first")
    result = result.head(int(top_n)).copy()
    result["capability_rank"] = range(1, len(result) + 1)
    return result.drop(columns=["_score", "_activity"]).reset_index(drop=True)
