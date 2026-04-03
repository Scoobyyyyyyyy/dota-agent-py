"""
config.py — Shared constants, path helpers, JSON I/O, and logging.

Consolidates the duplicated helpers that exist in both bot.js and dashboard.js:
  - loadJson / saveJson  →  load_json / save_json
  - log / warn           →  log / warn
  - BASE_URL and file paths
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

# ── Paths ─────────────────────────────────────────────────────────────────────

SRC_DIR: Path = Path(__file__).parent
CONFIG_PATH: Path = SRC_DIR / "config.json"
STRATEGY_PATH: Path = SRC_DIR / "strategy.json"
RECALL_STATE_PATH: Path = SRC_DIR / "recall_state.json"
STATS_PATH: Path = SRC_DIR / "stats.json"
TEMPLATES_DIR: Path = SRC_DIR / "templates"

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL: str = "https://www.defenseoftheagents.com"

LOOP_INTERVAL_S: float = 1.0          # 1 second between bot cycles  (was LOOP_INTERVAL_MS = 1_000)
RECALL_CHANNEL_S: float = 7.0         # recall channel window in seconds  (was RECALL_CHANNEL_MS = 7_000)
FULL_SCAN_INTERVAL_S: float = 30.0    # re-scan all games every 30s  (was FULL_SCAN_INTERVAL_MS = 30_000)
RECALL_COOLDOWN_S: float = 120.0      # server-side recall cooldown  (was 120_000 ms)
DEFAULT_RECALL_HP_THRESHOLD: float = 0.30

DASHBOARD_PORT: int = 3333
DASHBOARD_POLL_INTERVAL_S: float = 5.0

GAME_IDS: list[int] = [1, 2, 3, 4, 5]

# ── Ability priority lists ────────────────────────────────────────────────────

MELEE_ABILITY_PRIORITY: list[str] = [
    "cleave",
    "thorns",
    "divine_shield",
    "fury",
    "fortitude",
]

RANGED_ABILITY_PRIORITY: list[str] = [
    "volley",
    "bloodlust",
    "critical_strike",
    "fury",
    "fortitude",
]

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(message)s",  # bot.js used bare ISO timestamp prefix; handled below
)

_logger = logging.getLogger("agentscoob")


def log(msg: str) -> None:
    """Info-level log with ISO timestamp — mirrors bot.js log()."""
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    _logger.info("[%s] %s", ts, msg)


def warn(msg: str) -> None:
    """Warning-level log with ISO timestamp — mirrors bot.js warn()."""
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    _logger.warning("[%s] ⚠  %s", ts, msg)


# ── JSON file helpers ─────────────────────────────────────────────────────────


def load_json(path: Path) -> dict[str, Any] | None:
    """
    Read and parse a JSON file.  Returns None on missing file or parse error.
    Mirrors bot.js / dashboard.js loadJson().
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_json(path: Path, data: dict[str, Any]) -> None:
    """
    Serialise data to a JSON file with 2-space indent and a trailing newline.
    Mirrors bot.js / dashboard.js saveJson().
    """
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
