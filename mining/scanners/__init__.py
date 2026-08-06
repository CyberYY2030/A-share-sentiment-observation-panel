from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Candidate:
    strategy_id: str
    version: str
    trade_date: str
    sec_type: str
    sec_code: str
    sec_name: str
    entry_price: float
    features: dict[str, Any] = field(default_factory=dict)
    rank: int | None = None


class Scanner(ABC):
    strategy_id: str
    version: str
    kind: str
    description: str = ""
    default_params: dict[str, Any] = {}

    def __init__(self, params: dict[str, Any] | None = None):
        self.params = {**self.default_params, **(params or {})}
        self.last_universe_size: int | None = None

    @abstractmethod
    def run(self, conn: Any, trade_date: str) -> list[Candidate]:
        raise NotImplementedError


SCANNER_REGISTRY: list[type[Scanner]] = []


def register(cls: type[Scanner]) -> type[Scanner]:
    SCANNER_REGISTRY.append(cls)
    return cls


_LOADED = False


def load_builtin_scanners() -> None:
    global _LOADED
    if _LOADED:
        return
    from . import momentum_breakout  # noqa: F401
    from . import rps_concept  # noqa: F401
    from . import rps_stock  # noqa: F401
    from . import trend_embryo  # noqa: F401
    from . import true_leader  # noqa: F401
    from . import strong_trend  # noqa: F401
    from . import second_launch  # noqa: F401
    from . import launch_burst  # noqa: F401
    from . import base_breakout  # noqa: F401
    from . import counter_trend_rs  # noqa: F401

    _LOADED = True


def get_registered_scanners() -> list[type[Scanner]]:
    load_builtin_scanners()
    return list(SCANNER_REGISTRY)
