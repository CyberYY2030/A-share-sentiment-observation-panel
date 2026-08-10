from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class RuntimePaths:
    base_dir: str
    data_dir: str
    stock_db: str
    concept_db: str
    etf_db: str
    metrics_csv: str
    legacy_metrics_csv: str


def choose_existing_path(candidates: Iterable[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def build_runtime_paths(base_dir: str) -> RuntimePaths:
    base = Path(base_dir).absolute()
    data_dir = base / "data"

    stock_candidates = [
        data_dir / "a_share_mvp.db",
        base / "a_share_mvp.db",
        base / "ashare_mvp.db",
        base / "a" / "a_share_mvp.db",
        base / "a" / "ashare_mvp.db",
    ]
    concept_candidates = [
        data_dir / "ths_concept.db",
        base / "ths_concept.db",
    ]
    etf_candidates = [
        data_dir / "etf_mvp.db",
        base / "etf_mvp.db",
    ]

    stock_db = choose_existing_path(stock_candidates) or stock_candidates[0]
    concept_db = choose_existing_path(concept_candidates) or concept_candidates[0]
    etf_db = choose_existing_path(etf_candidates) or etf_candidates[0]

    return RuntimePaths(
        base_dir=str(base),
        data_dir=str(data_dir),
        stock_db=str(stock_db),
        concept_db=str(concept_db),
        etf_db=str(etf_db),
        metrics_csv=str(data_dir / "daily_metrics_last40.csv"),
        legacy_metrics_csv=str(base / "daily_matrics_last40.csv"),
    )


def ensure_runtime_dirs(paths: RuntimePaths) -> None:
    Path(paths.data_dir).mkdir(parents=True, exist_ok=True)

