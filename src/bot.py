"""
bot.py — Defense of the Agents  —  AgentScoob Bot

A self-contained async Python bot that registers with the game server,
observes the battlefield via the REST API, makes strategic lane + ability
decisions, and deploys every cycle.

Direct port of src/bot.js.  All game logic is behaviorally identical.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from typing import Any

import aiohttp
from pydantic import BaseModel, Field

from config import (
    BASE_URL,
    CONFIG_PATH,
    DEFAULT_RECALL_HP_THRESHOLD,
    FULL_SCAN_INTERVAL_S,
    GAME_IDS,
    LOOP_INTERVAL_S,
    MELEE_ABILITY_PRIORITY,
    RANGED_ABILITY_PRIORITY,
    RECALL_CHANNEL_S,
    RECALL_COOLDOWN_S,
    RECALL_STATE_PATH,
    STRATEGY_PATH,
    load_json,
    log,
    save_json,
    warn,
)

# ── Pydantic models for game-state payloads ───────────────────────────────────


class Ability(BaseModel):
    id: str
    level: int = 1


class Hero(BaseModel):
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
    abilities: list[Ability] = Field(default_factory=list)
    ability_choices: list[str] = Field(default_factory=list, alias="abilityChoices")

    model_config = {"populate_by_name": True}


class Lane(BaseModel):
    frontline: float = 0
    human: int = 0
    orc: int = 0


class Agents(BaseModel):
    human: list[str] = Field(default_factory=list)
    orc: list[str] = Field(default_factory=list)


class GameState(BaseModel):
    tick: int = 0
    winner: str | None = None
    heroes: list[Hero] = Field(default_factory=list)
    lanes: dict[str, Lane] = Field(default_factory=dict)
    agents: Agents = Field(default_factory=Agents)

    model_config = {"populate_by_name": True}


class Strategy(BaseModel):
    preferred_hero_class: str = Field("melee", alias="preferredHeroClass")
    recall_hp_threshold: float = Field(DEFAULT_RECALL_HP_THRESHOLD, alias="recallHpThreshold")

    model_config = {"populate_by_name": True}


class AgentConfig(BaseModel):
    agent_name: str = Field("AgentScoob", alias="agentName")
    api_key: str = Field("", alias="apiKey")

    model_config = {"populate_by_name": True}


# ── Module-level mutable state (mirrors JS let declarations) ──────────────────

_committed_lane: str | None = None          # committedLane
_last_recall_time: float = 0.0              # lastRecallTime  (epoch seconds)
_last_chat_game_id: int | None = None       # lastChatGameId
_last_message_lane: str | None = None       # lastMessageLane

_cached_game_id: int | None = None          # cachedGameId
_last_full_scan_time: float = 0.0           # lastFullScanTime  (epoch seconds)

# Persisted across restarts via recall_state.json — epoch seconds
_server_recall_cooldown_until: float = 0.0  # serverRecallCooldownUntil (converted to seconds)

_tick_lock = asyncio.Lock()                 # replaces isTicking boolean


def _load_initial_recall_state() -> float:
    """
    Load persisted recall cooldown from recall_state.json on startup.
    Mirrors the JS IIFE:
        let serverRecallCooldownUntil = (() => {
            const saved = loadJson(RECALL_STATE_PATH);
            return saved?.serverRecallCooldownUntil ?? 0;
        })();

    The JS value is stored as epoch-milliseconds; we convert to seconds here.
    """
    saved = load_json(RECALL_STATE_PATH)
    if saved and "serverRecallCooldownUntil" in saved:
        raw = saved["serverRecallCooldownUntil"]
        # JS stores ms; convert to seconds if the value looks like ms (> year 3000 in seconds)
        if raw > 32_503_680_000:
            return raw / 1000.0
        return float(raw)
    return 0.0


# ── 1. Registration ───────────────────────────────────────────────────────────


async def ensure_registered(config: AgentConfig, session: aiohttp.ClientSession) -> AgentConfig:
    """
    Ensure the agent has a valid API key, registering if necessary.
    Mirrors ensureRegistered() in bot.js.
    """
    if config.api_key:
        log(f'Credentials loaded — agent "{config.agent_name}" already registered.')
        return config

    log(f'No API key found. Registering agent "{config.agent_name}"…')

    async with session.post(
        f"{BASE_URL}/api/agents/register",
        json={"agentName": config.agent_name},
    ) as res:
        if res.status != 200:
            body = await res.text()
            raise RuntimeError(f"Registration failed ({res.status}): {body}")
        data: dict[str, Any] = await res.json()

    config = config.model_copy(update={"api_key": data["apiKey"]})
    save_json(CONFIG_PATH, {"agentName": config.agent_name, "apiKey": config.api_key})
    log("✅  Registered! API key saved to config.json.")
    return config


# ── 2. Observe — fetch game state ─────────────────────────────────────────────


async def fetch_game_state(
    session: aiohttp.ClientSession,
    game_id: int | None = None,
) -> GameState:
    """
    Fetch game state from the API.
    Mirrors fetchGameState() in bot.js.
    """
    url = (
        f"{BASE_URL}/api/game/state?game={game_id}"
        if game_id is not None
        else f"{BASE_URL}/api/game/state"
    )
    async with session.get(url) as res:
        if res.status != 200:
            raise RuntimeError(f"Failed to fetch game state ({res.status})")
        raw: dict[str, Any] = await res.json()
    return GameState.model_validate(raw)


# ── 3. Think — decide lane & ability ─────────────────────────────────────────


def find_my_hero(state: GameState, agent_name: str) -> Hero | None:
    """
    Return this agent's Hero from the game state, or None.
    Mirrors findMyHero() in bot.js.
    """
    return next((h for h in state.heroes if h.name == agent_name), None)


def choose_ability(my_hero: Hero, strategy: Strategy) -> str | None:
    """
    Choose an ability when the hero has a pending level-up.
    Prefer unlocking NEW level 1 abilities over upgrading existing ones.
    Mirrors chooseAbility() in bot.js exactly — two-pass logic preserved.
    """
    if not my_hero.ability_choices:
        return None

    priority_list = (
        RANGED_ABILITY_PRIORITY
        if my_hero.hero_class == "ranged"
        else MELEE_ABILITY_PRIORITY
    )

    current_abilities = {a.id for a in my_hero.abilities}

    # Pass 1: unlock a new ability we don't have yet
    for ability in priority_list:
        if ability in my_hero.ability_choices and ability not in current_abilities:
            return ability

    # Pass 2: upgrade the highest-priority ability we already have
    for ability in priority_list:
        if ability in my_hero.ability_choices:
            return ability

    return None


def should_recall(
    my_hero: Hero,
    strategy: Strategy,
    server_recall_cooldown_until: float,
) -> bool:
    """
    Determine whether the hero should recall to base.
    Triggers when HP drops below the configured threshold and recall is off cooldown.
    Mirrors shouldRecall() in bot.js.

    Note: server_recall_cooldown_until is passed in (rather than read from the
    module global directly) so the logic is pure and testable.
    """
    if my_hero.max_hp == 0 or my_hero.hp <= 0:
        return False

    threshold = strategy.recall_hp_threshold
    hp_percent = my_hero.hp / my_hero.max_hp
    server_off_cooldown = time.time() > server_recall_cooldown_until

    if hp_percent < threshold and not server_off_cooldown:
        cd_left = round(server_recall_cooldown_until - time.time())
        warn(
            f"RECALL BLOCKED by server cooldown: HP at "
            f"{round(hp_percent * 100)}% but {cd_left}s remaining"
        )

    return hp_percent < threshold and server_off_cooldown


# ── 4. Act — post deployment ──────────────────────────────────────────────────


async def deploy(
    api_key: str,
    payload: dict[str, Any],
    session: aiohttp.ClientSession,
    retries: int = 0,
) -> dict[str, Any]:
    """
    POST a deployment to the game server.
    Retries up to 3 times on 429 for recall actions (recall is life-or-death).
    Mirrors deploy() in bot.js.
    """
    global _server_recall_cooldown_until, _last_recall_time

    async with session.post(
        f"{BASE_URL}/api/strategy/deployment",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
    ) as res:
        if res.status != 200:
            body = await res.text()
            # Retry 429 for recall — mirrors JS retry logic
            if res.status == 429 and payload.get("action") == "recall" and retries < 3:
                wait_s = (retries + 1) * 0.5  # 500ms, 1000ms, 1500ms
                warn(
                    f"Recall deploy got 429, retrying in {int(wait_s * 1000)}ms "
                    f"(attempt {retries + 2}/4)"
                )
                await asyncio.sleep(wait_s)
                return await deploy(api_key, payload, session, retries + 1)
            raise RuntimeError(f"Deployment failed ({res.status}): {body}")

        data: dict[str, Any] = await res.json()

    if payload.get("action") == "recall":
        import json as _json
        log(f"📡 RECALL RESPONSE: {_json.dumps(data)}")
        warning_msg: str = data.get("warning", "")
        if "cooldown" in warning_msg:
            match = re.search(r"(\d+)s remaining", warning_msg)
            if match:
                cd_s = int(match.group(1))
                _server_recall_cooldown_until = time.time() + cd_s
                save_json(
                    RECALL_STATE_PATH,
                    {
                        "serverRecallCooldownUntil": _server_recall_cooldown_until * 1000,
                        "lastRecallTime": _last_recall_time * 1000,
                    },
                )
                warn(f"Server recall cooldown: {cd_s}s remaining")
        else:
            # Recall accepted — 120s server-side cooldown starts now
            _server_recall_cooldown_until = time.time() + RECALL_COOLDOWN_S
            save_json(
                RECALL_STATE_PATH,
                {
                    "serverRecallCooldownUntil": _server_recall_cooldown_until * 1000,
                    "lastRecallTime": _last_recall_time * 1000,
                },
            )

    return data


# ── 5. Main game loop ─────────────────────────────────────────────────────────


async def tick(
    config: AgentConfig,
    strategy: Strategy,
    session: aiohttp.ClientSession,
) -> str:
    """
    One Observe → Think → Act cycle.
    Mirrors tick() in bot.js, including the isTicking reentrant guard
    (replaced here with asyncio.Lock), recall channel protection,
    fast-path cache, parallel full-scan, and all chat message logic.
    """
    global _committed_lane, _last_recall_time, _last_chat_game_id
    global _last_message_lane, _cached_game_id, _last_full_scan_time
    global _server_recall_cooldown_until

    if _tick_lock.locked():
        return "ok"

    async with _tick_lock:
        # ── Recall channel protection ─────────────────────────────────────────
        # Keep re-sending { action: recall } for RECALL_CHANNEL_S seconds after
        # initiating a recall so no movement command can cancel the channel.
        is_channeling_recall = (time.time() - _last_recall_time) < RECALL_CHANNEL_S

        if is_channeling_recall:
            elapsed = round(time.time() - _last_recall_time)
            log(f"🏠 RECALL CHANNEL: re-sending recall ({elapsed}s into channel)")
            await deploy(config.api_key, {"action": "recall"}, session)
            return "ok"

        # ── 2. Observe ────────────────────────────────────────────────────────
        state: GameState | None = None
        my_hero: Hero | None = None
        active_game_id: int | None = None

        need_full_scan = (
            _cached_game_id is None
            or (time.time() - _last_full_scan_time) > FULL_SCAN_INTERVAL_S
        )

        if _cached_game_id is not None and not need_full_scan:
            # Fast path: single fetch from known game
            try:
                s = await fetch_game_state(session, _cached_game_id)
                hero = find_my_hero(s, config.agent_name)
                listed = (
                    config.agent_name in s.agents.human
                    or config.agent_name in s.agents.orc
                )
                if hero or listed:
                    state = s
                    my_hero = hero
                    active_game_id = _cached_game_id
            except Exception:
                pass  # fall through to full scan

        if state is None:
            # Full scan: check all games in parallel — mirrors Promise.allSettled
            _last_full_scan_time = time.time()

            async def _fetch_with_id(game_id: int) -> tuple[GameState, int]:
                return await fetch_game_state(session, game_id), game_id

            results = await asyncio.gather(
                *[_fetch_with_id(g) for g in GAME_IDS],
                return_exceptions=True,
            )

            for result in results:
                if isinstance(result, Exception):
                    continue
                s, g = result
                hero = find_my_hero(s, config.agent_name)
                listed = (
                    config.agent_name in s.agents.human
                    or config.agent_name in s.agents.orc
                )
                if hero or listed:
                    state = s
                    my_hero = hero
                    active_game_id = g
                    _cached_game_id = g
                    break

            # Fallback: not found in any game — fetch default so first deploy works
            if state is None:
                _cached_game_id = None
                state = await fetch_game_state(session)

        if state.winner:
            log(f"🏆  Game over! Winner: {state.winner} (game {active_game_id or '?'})")
            return "gameover"

        # ── 3. Think ──────────────────────────────────────────────────────────
        hero_class = strategy.preferred_hero_class

        use_recall = False
        if my_hero and my_hero.hp > 0:
            if should_recall(my_hero, strategy, _server_recall_cooldown_until):
                use_recall = True
                _committed_lane = None
                hp_pct = round((my_hero.hp / my_hero.max_hp) * 100)
                log(f"🏠 RECALL: HP at {hp_pct}% — channeling recall to base!")

        # Lane decision — always mid (all-mid strategy)
        lane = "mid"
        if not use_recall:
            _committed_lane = "mid"

        ability_choice = choose_ability(my_hero, strategy) if my_hero else None

        # Build chat message — only emit when there is something NEW to say
        message: str | None = None

        if active_game_id is not None and _last_chat_game_id != active_game_id:
            message = "Scoob: gl&hf"
            _last_chat_game_id = active_game_id
            _last_message_lane = None  # broadcast first lane next tick
        elif use_recall:
            message = "Scoob 🏠 RECALLING"
            _last_message_lane = None  # reset so lane is mentioned after return
        elif lane != _last_message_lane:
            message = f"Scoob → {lane.upper()}"
            _last_message_lane = lane

        if ability_choice:
            message = (
                f"{message} | leveling {ability_choice}"
                if message
                else f"Scoob leveling {ability_choice}"
            )

        # Build deployment payload
        payload: dict[str, Any]
        if use_recall:
            payload = {"action": "recall"}
            if message:
                payload["message"] = message
        else:
            payload = {"heroClass": hero_class, "heroLane": lane}
            if ability_choice:
                payload["abilityChoice"] = ability_choice
            if message:
                payload["message"] = message

        # Update recall state BEFORE deploy so channel protection starts immediately
        if use_recall:
            _last_recall_time = time.time()
            save_json(
                RECALL_STATE_PATH,
                {
                    "lastRecallTime": _last_recall_time * 1000,
                    "serverRecallCooldownUntil": _server_recall_cooldown_until * 1000,
                },
            )

        # ── 4. Act ────────────────────────────────────────────────────────────
        hero_info = (
            f"Lv{my_hero.level} {my_hero.hero_class} "
            f"({my_hero.hp}/{my_hero.max_hp} HP)"
            if my_hero
            else "first deploy"
        )
        lane_info = " | ".join(
            f"{name}: fl={lane_obj.frontline}"
            for name, lane_obj in state.lanes.items()
        )
        tick_num = state.tick

        try:
            result = await deploy(config.api_key, payload, session)
            log(
                f"🎮  Tick #{tick_num} | {hero_info} | Lane: {lane} | {lane_info}"
                + (f" | 🆙 {ability_choice}" if ability_choice else "")
                + (" | 🏠 RECALL" if use_recall else "")
                + f" | gameId={result.get('gameId', '?')}"
            )
        except Exception as err:
            warn(f"Deploy error at tick #{tick_num}: {err}")

        return "ok"


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    global _server_recall_cooldown_until

    log("═══════════════════════════════════════════════════════")
    log("  Defense of the Agents  —  AgentScoob  🐕")
    log("═══════════════════════════════════════════════════════")

    # Load persisted recall state before anything else (mirrors JS IIFE)
    _server_recall_cooldown_until = _load_initial_recall_state()

    # Load config & strategy
    raw_config = load_json(CONFIG_PATH) or {"agentName": "AgentScoob", "apiKey": ""}
    config = AgentConfig.model_validate(raw_config)

    raw_strategy = load_json(STRATEGY_PATH)
    strategy = Strategy.model_validate(raw_strategy) if raw_strategy else Strategy()

    log(
        f"Strategy loaded: class={strategy.preferred_hero_class}, "
        f"recallHpThreshold={strategy.recall_hp_threshold}"
    )

    # Single shared ClientSession for the lifetime of the bot
    async with aiohttp.ClientSession() as session:
        # Register if needed (first run)
        config = await ensure_registered(config, session)

        # --register-only mode: exit immediately after registration
        if "--register-only" in sys.argv:
            log("Registration complete. Exiting (--register-only).")
            return

        log(f"Starting game loop — deploying every {LOOP_INTERVAL_S}s…")

        # Run the first tick immediately (mirrors the pre-interval call in JS)
        try:
            await tick(config, strategy, session)
        except Exception as err:
            warn(f"First tick failed: {err}")

        # Continuous game loop — mirrors setInterval(async () => { ... }, LOOP_INTERVAL_MS)
        while True:
            await asyncio.sleep(LOOP_INTERVAL_S)
            try:
                # Re-read strategy each tick so hot-edits to strategy.json take effect
                raw_fresh = load_json(STRATEGY_PATH)
                fresh_strategy = (
                    Strategy.model_validate(raw_fresh) if raw_fresh else strategy
                )
                await tick(config, fresh_strategy, session)
            except Exception as err:
                warn(f"Tick error: {err}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Fatal: {e}", file=sys.stderr)
        sys.exit(1)
