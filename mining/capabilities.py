from __future__ import annotations

from dataclasses import dataclass


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


def visible_strategy_ids() -> tuple[str, ...]:
    return (*formal_strategy_ids(), *LEGACY_STRATEGY_IDS)
