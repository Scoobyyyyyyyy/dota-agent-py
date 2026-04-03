"""
dashboard.py — AgentScoob Dashboard Server

A lightweight aiohttp HTTP server that serves the tracking dashboard HTML
and exposes a /api/status endpoint that the browser polls.

Direct port of src/dashboard.js.  All behaviour is identical:
  - Background task polls all 5 game slots every 5 s
  - Win/loss results are persisted to stats.json
  - Recall state is read from recall_state.json (written by bot.py)
  - GET /            → serves dashboard.html
  - GET /api/status  → JSON snapshot consumed by the dashboard UI
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiohttp import web
from pydantic import BaseModel, Field

from config import (
    BASE_URL,
    DASHBOARD_POLL_INTERVAL_S,
    DASHBOARD_PORT,
    GAME_IDS,
    RECALL_STATE_PATH,
    STATS_PATH,
    TEMPLATES_DIR,
    load_json,
    save_json,
)

# ── Constants ─────────────────────────────────────────────────────────────────

AGENT_NAME = "AgentScoob"
_STATS_HISTORY_MAX = 100
_DEDUP_WINDOW_S = 60.0  # seconds — mirrors the 60 000 ms dedup guard in JS

# ── Pydantic models ───────────────────────────────────────────────────────────


class RawHero(BaseModel):
    """Minimal hero shape needed by the dashboard poller."""
    name: str
    faction: str | None = None
    hp: float = 0
    max_hp: float = Field(0, alias="maxHp")
    xp: float = 0
    xp_to_next: float = Field(0, alias="xpToNext")
    level: int = 1
    alive: bool = True
    hero_class: str = Field("melee", alias="class")
    lane: str | None = None
    abilities: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class RawAgents(BaseModel):
    human: list[str] = Field(default_factory=list)
    orc: list[str] = Field(default_factory=list)


class RawLane(BaseModel):
    frontline: float = 0
    human: int = 0
    orc: int = 0


class RawGameState(BaseModel):
    tick: int = 0
    winner: str | None = None
    heroes: list[RawHero] = Field(default_factory=list)
    lanes: dict[str, RawLane] = Field(default_factory=dict)
    agents: RawAgents = Field(default_factory=RawAgents)
    towers: dict[str, Any] | None = None
    bases: dict[str, Any] | None = None

    model_config = {"populate_by_name": True}


# ── Module-level mutable state ────────────────────────────────────────────────

_cached_matches: list[dict[str, Any]] = []          # cachedMatches
_tracked_factions: dict[int, str] = {}              # trackedFactions  { gameId -> faction }


# ── Win / loss tracking ───────────────────────────────────────────────────────


def record_result(game_id: int, winner: str, faction: str) -> None:
    """
    Persist a game result to stats.json.
    Mirrors recordResult() in dashboard.js exactly, including the 60-second
    deduplication guard and the 100-entry history cap.
    """
    stats: dict[str, Any] = load_json(STATS_PATH) or {
        "totalGames": 0,
        "wins": 0,
        "losses": 0,
        "history": [],
    }

    # Dedup guard — don't double-record if the game ended milliseconds ago
    cutoff = time.time() - _DEDUP_WINDOW_S
    recently_recorded = any(
        h["gameId"] == game_id
        and _iso_to_epoch(h.get("timestamp", "")) > cutoff
        for h in stats.get("history", [])
    )
    if recently_recorded:
        return

    won = winner == faction
    stats["totalGames"] = stats.get("totalGames", 0) + 1
    if won:
        stats["wins"] = stats.get("wins", 0) + 1
    else:
        stats["losses"] = stats.get("losses", 0) + 1

    history: list[dict[str, Any]] = stats.get("history", [])
    history.append(
        {
            "gameId": game_id,
            "faction": faction,
            "winner": winner,
            "result": "WIN" if won else "LOSS",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }
    )

    # Keep last 100 entries
    if len(history) > _STATS_HISTORY_MAX:
        history = history[-_STATS_HISTORY_MAX:]
    stats["history"] = history

    save_json(STATS_PATH, stats)
    emoji = "🏆" if won else "💀"
    print(f"[Stats] Game {game_id}: {'WIN ' + emoji if won else 'LOSS ' + emoji} ({winner} won)")


def _iso_to_epoch(ts: str) -> float:
    """Parse an ISO-8601 timestamp string to a Unix epoch float. Returns 0 on error."""
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return 0.0


# ── Background polling ────────────────────────────────────────────────────────


async def poll_games(session: aiohttp.ClientSession) -> None:
    """
    Fetch state for all 5 game slots, update _cached_matches, and record
    any finished games.  Mirrors pollGames() in dashboard.js — sequential
    per-game fetches with individual try/except to skip unreachable slots.
    """
    global _cached_matches, _tracked_factions

    active: list[dict[str, Any]] = []

    for g in GAME_IDS:
        try:
            async with session.get(f"{BASE_URL}/api/game/state?game={g}") as res:
                if res.status != 200:
                    continue
                raw: dict[str, Any] = await res.json()

            state = RawGameState.model_validate(raw)

            hero = next((h for h in state.heroes if h.name == AGENT_NAME), None)
            is_human = AGENT_NAME in state.agents.human
            is_orc = AGENT_NAME in state.agents.orc
            in_game = is_human or is_orc

            # Resolve faction
            faction: str | None = hero.faction if hero else None
            if not faction:
                if is_human:
                    faction = "human"
                elif is_orc:
                    faction = "orc"

            # Remember which team the agent is on
            if faction:
                _tracked_factions[g] = faction

            # Record result when game ends and we remember playing in it
            if state.winner and g in _tracked_factions:
                record_result(g, state.winner, _tracked_factions[g])
                del _tracked_factions[g]  # clear for next match in this slot

            if in_game or hero:
                active.append(
                    {
                        "gameId": g,
                        "tick": state.tick,
                        "winner": state.winner,
                        "faction": faction,
                        # Re-serialise hero back to camelCase dict for the dashboard HTML
                        "hero": _hero_to_dict(hero) if hero else None,
                        "lanes": {
                            name: {"frontline": lane.frontline, "human": lane.human, "orc": lane.orc}
                            for name, lane in state.lanes.items()
                        },
                        "towers": state.towers,
                        "bases": state.bases,
                        "agents": {"human": state.agents.human, "orc": state.agents.orc},
                        "allHeroes": [_hero_to_dict(h) for h in state.heroes],
                    }
                )
        except Exception:
            pass  # skip unreachable game slots

    _cached_matches = active


def _hero_to_dict(hero: RawHero) -> dict[str, Any]:
    """Serialise a RawHero back to the camelCase shape the dashboard HTML expects."""
    return {
        "name": hero.name,
        "faction": hero.faction,
        "hp": hero.hp,
        "maxHp": hero.max_hp,
        "xp": hero.xp,
        "xpToNext": hero.xp_to_next,
        "level": hero.level,
        "alive": hero.alive,
        "class": hero.hero_class,
        "lane": hero.lane,
        "abilities": hero.abilities,
    }


async def _poll_loop(session: aiohttp.ClientSession) -> None:
    """
    Infinite background polling task.
    Mirrors:  setInterval(pollGames, 5000);  pollGames();
    The initial call runs immediately, then repeats every DASHBOARD_POLL_INTERVAL_S.
    """
    while True:
        try:
            await poll_games(session)
        except Exception as err:
            print(f"[Dashboard] Polling error: {err}")
        await asyncio.sleep(DASHBOARD_POLL_INTERVAL_S)


# ── HTTP route handlers ───────────────────────────────────────────────────────


async def handle_index(request: web.Request) -> web.Response:
    """
    GET /  and  GET /index.html — serve dashboard.html.
    Reads the file on every request (mirrors readFileSync in the JS handler)
    so edits to the HTML are reflected without a server restart.
    """
    html_path = TEMPLATES_DIR / "dashboard.html"
    try:
        html = html_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise web.HTTPNotFound(text="dashboard.html not found in src/templates/")
    return web.Response(text=html, content_type="text/html", charset="utf-8")


async def handle_status(request: web.Request) -> web.Response:
    """
    GET /api/status — return JSON snapshot for the dashboard browser UI.
    Mirrors the /api/status branch in dashboard.js exactly, including the
    recall cooldown calculation from recall_state.json.
    """
    stats: dict[str, Any] = load_json(STATS_PATH) or {
        "totalGames": 0,
        "wins": 0,
        "losses": 0,
        "history": [],
    }

    # Read recall state written by bot.py
    # bot.py stores serverRecallCooldownUntil as epoch-milliseconds (JS compat)
    recall_state = load_json(RECALL_STATE_PATH)
    recall_cooldown_remaining_ms = 0.0

    if recall_state:
        # JS dashboard.js checks recallState?.cooldownEnds, but bot.js writes
        # serverRecallCooldownUntil — use that field (same value, different key name).
        cooldown_until_ms: float = (
            recall_state.get("cooldownEnds")
            or recall_state.get("serverRecallCooldownUntil")
            or 0.0
        )
        now_ms = time.time() * 1000
        recall_cooldown_remaining_ms = max(0.0, cooldown_until_ms - now_ms)

    payload = {
        "agent": AGENT_NAME,
        "activeGames": _cached_matches,
        "stats": stats,
        "recall": {
            "lastRecallTime": recall_state.get("lastRecallTime", 0) if recall_state else 0,
            "cooldownRemaining": recall_cooldown_remaining_ms,
            "isOnCooldown": recall_cooldown_remaining_ms > 0,
        },
    }

    return web.Response(
        text=json.dumps(payload),
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


async def handle_404(request: web.Request) -> web.Response:
    return web.Response(text="Not Found", status=404, content_type="text/plain")


# ── App lifecycle ─────────────────────────────────────────────────────────────


async def on_startup(app: web.Application) -> None:
    """
    Create a shared ClientSession and start the background polling task.
    Mirrors:  setInterval(pollGames, 5000);  pollGames();
    """
    session = aiohttp.ClientSession()
    app["session"] = session
    # Run the first poll immediately, then every DASHBOARD_POLL_INTERVAL_S
    app["poll_task"] = asyncio.create_task(_poll_loop(session))


async def on_cleanup(app: web.Application) -> None:
    """Graceful shutdown — cancel polling task and close HTTP session."""
    app["poll_task"].cancel()
    try:
        await app["poll_task"]
    except asyncio.CancelledError:
        pass
    await app["session"].close()


# ── Application factory ───────────────────────────────────────────────────────


def create_app() -> web.Application:
    app = web.Application()

    app.router.add_get("/", handle_index)
    app.router.add_get("/index.html", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_route("*", "/{path_info:.*}", handle_404)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


# ── Entry point ───────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print(f"\n🎮  AgentScoob Dashboard running at http://localhost:{DASHBOARD_PORT}\n")
    web.run_app(create_app(), port=DASHBOARD_PORT, print=None)
