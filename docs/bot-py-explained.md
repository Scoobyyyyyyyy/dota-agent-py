# `bot.py` — Explained for Beginners

> A step-by-step walkthrough of `src/bot.py` written for someone who is new to Python.
> You don't need prior Python experience — every concept is explained from scratch.

---

## What is this file, really?

`bot.py` is your robot player for a Dota-like browser game. Every second it wakes up,
asks the game server *"what's happening?"*, makes a decision, and sends a command back.
That's it. The whole file is just that loop, broken into tidy pieces.

---

## Line 1 — The weird future import

```python
from __future__ import annotations
```

Ignore the deep reason for this. All it does is let you write modern type hints (like
`Hero | None`) even on slightly older Python. Think of it as a compatibility switch you
always put at the very top.

---

## The import block

```python
import asyncio
import re
import sys
import time
from typing import Any

import aiohttp
from pydantic import BaseModel, Field

from config import (BASE_URL, LOOP_INTERVAL_S, ...)
```

`import` in Python works just like `require()` or `import` in JS — you're pulling in
tools from other files or libraries.

| What you import | What it does |
|---|---|
| `asyncio` | Python's engine for async/await — like Node's event loop built in |
| `re` | Regular expressions (like JS `RegExp`) |
| `sys` | Access to command-line args and stderr |
| `time` | Get the current timestamp in seconds |
| `aiohttp` | Makes HTTP requests asynchronously, replacing `fetch()` |
| `pydantic` | Validates and structures your data objects |
| `from config import ...` | Pulls constants and helpers from your own `config.py` file |

---

## What are Pydantic models?

This is the biggest Python-specific concept in the whole file. In JS, you might have written:

```js
const hero = { name: "AgentScoob", hp: 100, maxHp: 200 }
```

In Python with Pydantic you define a **class** — a blueprint that describes exactly what
shape your data has:

```python
class Hero(BaseModel):
    name: str
    hp: float = 0
    max_hp: float = Field(0, alias="maxHp")
```

Here's what each part means:

- **`class Hero(BaseModel):`** — Create a new data type called `Hero`. The `(BaseModel)`
  part means it *inherits* Pydantic's superpowers (validation, parsing, etc.). The colon
  `:` at the end opens a block — **Python uses indentation instead of `{}`**.
- **`name: str`** — This field must be a string. The `: str` part is a *type hint* —
  Pydantic will crash at runtime if the data is the wrong type.
- **`hp: float = 0`** — A decimal number that defaults to `0` if missing from the JSON.
- **`Field(0, alias="maxHp")`** — The JSON from the server uses `maxHp` (camelCase), but
  Python convention uses `max_hp` (snake_case). `alias=` tells Pydantic: *"when reading
  JSON, look for `maxHp`, but inside Python call it `max_hp`"*.
- **`model_config = {"populate_by_name": True}`** — Means you can use either `max_hp` or
  `maxHp` to set the value.

All the other models (`Lane`, `Agents`, `GameState`, `Strategy`, `AgentConfig`) work
exactly the same way — they're just different shapes of data the game server sends or
you configure.

---

## Module-level variables — the "global memory"

```python
_committed_lane: str | None = None
_last_recall_time: float = 0.0
_tick_lock = asyncio.Lock()
```

These are variables that live at the top of the file, outside any function. They're the
bot's **persistent memory** between ticks — like `let` at the top of your JS file.

- The `_` prefix is just a Python convention meaning *"this is internal, don't touch
  from outside"*. It's not enforced, just a polite signal.
- `str | None` means the variable can hold either a string OR `None` (Python's version
  of `null`).
- `asyncio.Lock()` is a special object that acts like a "do not disturb" sign. Only one
  piece of code can hold the lock at a time.

---

## `_load_initial_recall_state()` — reading a saved file on startup

```python
def _load_initial_recall_state() -> float:
    saved = load_json(RECALL_STATE_PATH)
    if saved and "serverRecallCooldownUntil" in saved:
        raw = saved["serverRecallCooldownUntil"]
        if raw > 32_503_680_000:
            return raw / 1000.0
        return float(raw)
    return 0.0
```

- `def` defines a function — same as `function` in JS.
- `-> float` after the parentheses is a type hint saying this function returns a decimal number.
- `load_json(RECALL_STATE_PATH)` reads a `.json` file from disk and returns a Python
  dictionary (like a JS object `{}`). If the file doesn't exist, it returns `None`.
- `if saved and "serverRecallCooldownUntil" in saved:` — the `in` keyword checks if a key
  exists in a dictionary, like `"key" in obj`. The `and` is exactly `&&`.
- The JS file stores timestamps in **milliseconds**, but Python's `time.time()` returns
  **seconds**. The `if raw > 32_503_680_000` check detects the unit and divides by 1000
  if needed.

---

## `ensure_registered()` — first-time setup

```python
async def ensure_registered(config: AgentConfig, session: aiohttp.ClientSession) -> AgentConfig:
    if config.api_key:
        log('Already registered.')
        return config

    async with session.post(
        f"{BASE_URL}/api/agents/register",
        json={"agentName": config.agent_name}
    ) as res:
        if res.status != 200:
            raise RuntimeError(f"Registration failed ({res.status})")
        data = await res.json()

    config = config.model_copy(update={"api_key": data["apiKey"]})
    save_json(CONFIG_PATH, {"agentName": config.agent_name, "apiKey": config.api_key})
    return config
```

- **`async def`** — same as `async function` in JS. You must `await` it when calling it.
- **`f"{BASE_URL}/api/agents/register"`** — f-strings are Python's template literals.
  Anything inside `{}` gets evaluated, exactly like `` `${BASE_URL}/api/agents/register` ``.
- **`async with session.post(...) as res:`** — Makes an HTTP POST request. The block
  automatically closes the connection when done, even if an error happens.
- **`raise RuntimeError(...)`** — Throwing an error, like `throw new Error(...)` in JS.
- **`config.model_copy(update={...})`** — Pydantic models are immutable by default. This
  creates a new copy of `config` with the `api_key` field updated.

---

## `fetch_game_state()` — asking the server what's happening

```python
async def fetch_game_state(session, game_id=None) -> GameState:
    url = (
        f"{BASE_URL}/api/game/state?game={game_id}"
        if game_id is not None
        else f"{BASE_URL}/api/game/state"
    )
    async with session.get(url) as res:
        raw = await res.json()
    return GameState.model_validate(raw)
```

- **`game_id=None`** — a default parameter. If you don't pass a `game_id`, it's `None`
  automatically, same as JS `game_id = null`.
- **`X if condition else Y`** — Python's ternary operator. Same as `condition ? X : Y`
  in JS.
- **`GameState.model_validate(raw)`** — Takes the raw JSON dictionary and converts it
  into your structured, validated `GameState` object.

---

## `find_my_hero()` — searching the hero list

```python
def find_my_hero(state: GameState, agent_name: str) -> Hero | None:
    return next((h for h in state.heroes if h.name == agent_name), None)
```

This one line is equivalent to `state.heroes.find(h => h.name === agentName)` in JS.

- **`h for h in state.heroes if h.name == agent_name`** — a *generator expression*. Like
  `.filter()` but lazy (only evaluates as needed).
- **`next(..., None)`** — takes the first result, or returns `None` if nothing matched.

---

## `choose_ability()` — picking what to level up

```python
def choose_ability(my_hero, strategy) -> str | None:
    if not my_hero.ability_choices:
        return None

    priority_list = RANGED_ABILITY_PRIORITY if my_hero.hero_class == "ranged" else MELEE_ABILITY_PRIORITY
    current_abilities = {a.id for a in my_hero.abilities}

    # Pass 1: unlock a brand new ability
    for ability in priority_list:
        if ability in my_hero.ability_choices and ability not in current_abilities:
            return ability

    # Pass 2: upgrade the highest-priority ability we already have
    for ability in priority_list:
        if ability in my_hero.ability_choices:
            return ability

    return None
```

- **`if not my_hero.ability_choices:`** — `not` is Python's `!`. An empty list is
  *falsy* in Python, so this means "if the list is empty, bail out".
- **`{a.id for a in my_hero.abilities}`** — a *set comprehension*. Builds a `Set` of all
  ability IDs the hero already has. Like `new Set(myHero.abilities.map(a => a.id))` in JS.
- **`for ability in priority_list:`** — Python's `for...of` loop.
- Two-pass strategy: first loop unlocks a *new* ability, second loop falls back to
  upgrading an existing one.

---

## `should_recall()` — deciding whether to go back to base

```python
def should_recall(my_hero, strategy, server_recall_cooldown_until) -> bool:
    if my_hero.max_hp == 0 or my_hero.hp <= 0:
        return False

    threshold = strategy.recall_hp_threshold
    hp_percent = my_hero.hp / my_hero.max_hp
    server_off_cooldown = time.time() > server_recall_cooldown_until

    if hp_percent < threshold and not server_off_cooldown:
        cd_left = round(server_recall_cooldown_until - time.time())
        warn(f"RECALL BLOCKED: {cd_left}s remaining")

    return hp_percent < threshold and server_off_cooldown
```

- **`-> bool`** — this function returns `True` or `False`.
- **`time.time()`** — current time as seconds since 1970 (Unix timestamp), same as
  `Date.now() / 1000` in JS.
- **`round(...)`** — rounds to nearest integer, like `Math.round()`.
- **`return hp_percent < threshold and server_off_cooldown`** — `and` is `&&`. Returns
  `True` only if *both* conditions are true.

---

## `deploy()` — sending the command to the server

```python
async def deploy(api_key, payload, session, retries=0) -> dict[str, Any]:
    global _server_recall_cooldown_until, _last_recall_time

    async with session.post(
        f"{BASE_URL}/api/strategy/deployment",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
    ) as res:
        if res.status != 200:
            if res.status == 429 and payload.get("action") == "recall" and retries < 3:
                await asyncio.sleep(wait_s)
                return await deploy(api_key, payload, session, retries + 1)
            raise RuntimeError(...)
        data = await res.json()
```

- **`global _server_recall_cooldown_until`** — Required in Python whenever you want to
  *reassign* a module-level variable inside a function. Without `global`, Python would
  create a new local variable instead of updating the shared one.
- **`payload.get("action")`** — Dictionaries have a `.get()` method that returns `None`
  if the key doesn't exist, instead of crashing. Like `payload?.action` in JS.
- **`return await deploy(..., retries + 1)`** — Recursive retry. The function calls
  itself with one more retry count.
- After a successful recall, the cooldown timestamp is saved to a JSON file so it
  survives a bot restart.

---

## `tick()` — the heart of the bot (one full cycle)

This is the biggest function and contains everything: **Observe → Think → Act**.

### The lock guard

```python
if _tick_lock.locked():
    return "ok"
async with _tick_lock:
    ...
```

If the previous tick is still running (slow server), this one exits immediately. The
`async with _tick_lock:` claims the lock for the duration of the block and automatically
releases it at the end — even if an error happens.

### The recall channel protection

```python
is_channeling_recall = (time.time() - _last_recall_time) < RECALL_CHANNEL_S
```

For 7 seconds after a recall starts, every tick just re-sends the recall command. This
prevents any movement command from accidentally cancelling the recall channel.

### Fast path vs full scan

```python
need_full_scan = (
    _cached_game_id is None
    or (time.time() - _last_full_scan_time) > FULL_SCAN_INTERVAL_S
)
```

- **Fast path**: if we already know which game we're in, just fetch that one.
- **Full scan**: every 30 seconds (or on first boot), check all 5 games in parallel.

### The parallel fetch

```python
results = await asyncio.gather(
    *[_fetch_with_id(g) for g in GAME_IDS],
    return_exceptions=True,
)
```

- **`[... for g in GAME_IDS]`** — a *list comprehension*. Builds a list of fetch calls,
  one per game ID. Like `GAME_IDS.map(g => _fetch_with_id(g))` in JS.
- **`*[...]`** — the `*` unpacks the list as separate arguments, like JS spread `...[...]`.
- **`asyncio.gather(...)`** — runs all the fetches at the same time (in parallel), like
  `Promise.allSettled()`. `return_exceptions=True` means failed fetches don't crash
  everything.

### Building the payload

```python
if use_recall:
    payload = {"action": "recall"}
else:
    payload = {"heroClass": hero_class, "heroLane": lane}
    if ability_choice:
        payload["abilityChoice"] = ability_choice
```

Python dictionaries `{}` are JS objects. You add keys with `dict["key"] = value`.

---

## `main()` — the startup sequence

```python
async def main() -> None:
    async with aiohttp.ClientSession() as session:
        config = await ensure_registered(config, session)
        await tick(config, strategy, session)  # first tick immediately
        while True:
            await asyncio.sleep(LOOP_INTERVAL_S)
            await tick(config, fresh_strategy, session)
```

- **`async with aiohttp.ClientSession() as session:`** — Creates one shared HTTP client
  for the whole bot lifetime. More efficient than creating a new one every request.
- **`while True:`** — an infinite loop, like `setInterval` but written as a loop.
  `await asyncio.sleep(LOOP_INTERVAL_S)` pauses for 1 second without blocking.
- The strategy file is **re-read every tick** (`load_json(STRATEGY_PATH)`). You can edit
  `strategy.json` while the bot is running and changes take effect immediately without
  restarting.

---

## The entry point — the very last lines

```python
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Fatal: {e}", file=sys.stderr)
        sys.exit(1)
```

- **`if __name__ == "__main__":`** — Python's most important idiom. When you run the
  file directly (`python bot.py`), `__name__` equals `"__main__"`. When another file
  imports it, `__name__` is the module name instead. This guard means: *only run the bot
  if this file is the starting point*.
- **`asyncio.run(main())`** — Starts Python's async event loop and runs `main()` inside
  it. This is the single line that boots everything.
- **`except KeyboardInterrupt: pass`** — When you press `Ctrl+C` in the terminal, Python
  raises `KeyboardInterrupt`. `pass` means "do nothing, just exit cleanly".
- **`file=sys.stderr`** — Prints the fatal error to the error stream instead of normal
  output, so it's clearly visible in logs.

---

*This documentation was auto-generated on 2026-04-03.*
