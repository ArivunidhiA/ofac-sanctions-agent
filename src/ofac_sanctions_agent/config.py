from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


# Project-relative paths
ROOT_DIR: Path = Path(__file__).resolve().parents[2]
CONFIG_DIR: Path = ROOT_DIR / "config"
OUTPUT_DIR: Path = ROOT_DIR / "output"
LOG_DIR: Path = ROOT_DIR / "logs"

CONFIG_PATH: Path = CONFIG_DIR / "targets.json"
OUTPUT_PATH: Path = OUTPUT_DIR / "results.json"
LOG_PATH: Path = LOG_DIR / "agent.log"


DEFAULT_SEARCH_SETTINGS: Dict[str, Any] = {
    "score_threshold": 0,
    "max_results_per_entity": 50,
    "search_type": "name",
}


@dataclass
class SearchSettings:
    score_threshold: int
    max_results_per_entity: int
    search_type: str


@dataclass
class AgentConfig:
    entities: List[Dict[str, Any]]
    search_settings: SearchSettings


def load_raw_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """Load the raw JSON config file."""
    if not path.exists():
        raise FileNotFoundError(str(path))
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _build_search_settings(raw: Dict[str, Any]) -> SearchSettings:
    merged = {**DEFAULT_SEARCH_SETTINGS, **(raw or {})}
    return SearchSettings(
        score_threshold=int(merged["score_threshold"]),
        max_results_per_entity=int(merged["max_results_per_entity"]),
        search_type=str(merged["search_type"]),
    )


def load_config(path: Path = CONFIG_PATH) -> AgentConfig:
    """Load and normalise agent configuration."""
    raw = load_raw_config(path)
    entities = list(raw.get("entities", []))
    search_settings = _build_search_settings(raw.get("search_settings", {}))
    return AgentConfig(entities=entities, search_settings=search_settings)

