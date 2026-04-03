# 🐕 AgentScoob — Defense of the Agents AI Bot (Python)

An autonomous AI agent for **[Defense of the Agents](https://www.defenseoftheagents.com)**, a casual idle MOBA where AI agents and humans fight side by side.

This is a **Python port** of the original Node.js bot, rewritten with `asyncio` + `aiohttp` and typed game-state parsing via Pydantic v2.  All game logic and API behaviour is identical to the JavaScript version.

## Features

- **All-Mid strategy** — brute-forces the middle lane to overwhelm opponents
- **Recall system** — automatically recalls to base when HP drops below 30%
- **Ability priority** — follows a configurable leveling order for melee / ranged heroes
- **Chat messages** — announces lane changes, recalls, and says `gl&hf` at game start
- **Live dashboard** — a local web UI showing real-time hero stats, lane status, and match history
- **Win/loss tracking** — persistent match history with automatic result recording

## Requirements

- Python 3.11+
- `aiohttp >= 3.9`
- `pydantic >= 2.7`

## Quick Start

### 1. Install dependencies

```bash
cd dota-agent-py
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure credentials

Copy the example config and fill in your agent name (the API key is auto-populated on first run):

```bash
cp src/config.example.json src/config.json
```

`src/config.json`:
```json
{
  "agentName": "YourAgentName",
  "apiKey": ""
}
```

To register manually and get an API key without starting the full loop:

```bash
python src/bot.py --register-only
```

### 3. Configure strategy (optional)

Edit `src/strategy.json`:

```json
{
  "preferredHeroClass": "melee",
  "recallHpThreshold": 0.30
}
```

- **`preferredHeroClass`** — `"melee"` or `"ranged"`
- **`recallHpThreshold`** — HP fraction to trigger recall (default: `0.30` = 30%)

Changes to this file are picked up **live** without restarting the bot.

### 4. Run the bot

```bash
python src/bot.py
```

The bot deploys every second and logs its decisions to stdout.

### 5. Run the dashboard (optional)

```bash
python src/dashboard.py
```

Opens a live tracking dashboard at `http://localhost:3333` showing:
- Hero stats (HP, XP, level, abilities)
- Lane frontline positions
- Recall cooldown status
- Match history with win/loss tracking

## Project Structure

```
dota-agent-py/
├── requirements.txt              # Python dependencies (aiohttp, pydantic)
├── .gitignore                    # Excludes secrets & runtime files
├── README.md                     # This file
└── src/
    ├── bot.py                    # Core agent logic (observe → think → act loop)
    ├── dashboard.py              # aiohttp dashboard server + background poller
    ├── config.py                 # Shared constants, paths, JSON I/O, logging
    ├── config.json               # Your credentials (gitignored)
    ├── config.example.json       # Credential template
    ├── strategy.json             # Agent strategy configuration
    ├── stats.json                # Match history (auto-generated, gitignored)
    ├── recall_state.json         # Recall cooldown state (auto-generated, gitignored)
    └── templates/
        └── dashboard.html        # Dashboard UI (served by dashboard.py)
```

## How It Works

The bot follows an **Observe → Think → Act** loop every second:

1. **Observe** — Fetches game state from the API (caches the active game for speed, full re-scan every 30 s via `asyncio.gather`)
2. **Think** — Checks recall HP threshold, picks ability level-up, builds deployment payload
3. **Act** — POSTs deployment; retries recall deploys up to 3× on HTTP 429

### Lane Decision Logic

| Game Phase | Strategy |
|---|---|
| All Phases | **ALL-MID** — Brute force the middle lane regardless of threats |
| Emergency | **Critical HP Recall** — Teleport to base if HP < 30% |

### Key Python ↔ JavaScript Differences

| Concern | JavaScript | Python |
|---|---|---|
| Event loop | Node.js runtime | `asyncio.run()` |
| HTTP client | `fetch()` global | `aiohttp.ClientSession` (single shared instance) |
| HTTP server | `http.createServer` | `aiohttp.web.Application` |
| Interval loop | `setInterval` | `while True` + `asyncio.sleep` |
| Parallel fetch | `Promise.allSettled` | `asyncio.gather(return_exceptions=True)` |
| Reentrant guard | `isTicking` boolean | `asyncio.Lock` |
| JSON payloads | plain objects | Pydantic v2 models with `alias` for camelCase keys |
| Timestamps | `Date.now()` (ms) | `time.time()` (s) — stored as ms in JSON for JS compatibility |

## Links

- 🎮 [Play the game](https://www.defenseoftheagents.com)
- 📖 [API Documentation](https://www.defenseoftheagents.com/game-loop.md)
- 🔧 [Agent Setup Guide](https://defenseoftheagents.com/skill.md)

## License

MIT
