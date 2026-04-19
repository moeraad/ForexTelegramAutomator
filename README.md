# CopyTrades

Telegram → AI → MT5 signal bridge for gold (XAUUSD). Reads a Telegram channel in real time, interprets messages with Claude Sonnet 4.6, and emits structured trading actions for an MT5 EA to execute. User stays in the loop via a Telegram control bot with a kill switch.

## Architecture

See `docs/superpowers/specs/2026-04-19-copytrades-design.md`.

Four processes share one SQLite DB:
- `listener.py` — Telethon, watches the channel, calls AI
- `bot.py` — Telegram bot (notifications + commands + promotion worker)
- `api.py` — FastAPI bridge MT5 reads from
- `ea/CopyTrades.mq5` — MT5 EA, executes orders

## First-time setup

### 1. Python environment

```bash
python -m venv .venv
.venv/Scripts/activate   # Windows
pip install -e ".[dev]"
```

### 2. Configure credentials

Copy `.env.example` → `.env`. Fill in:

- **Telegram user** (Telethon): `TG_API_ID`, `TG_API_HASH`, `TG_PHONE` from https://my.telegram.org/apps.
- **Watched chat ID**: easiest to find by running:
  ```python
  from telethon import TelegramClient
  import asyncio, os
  from dotenv import load_dotenv; load_dotenv()
  async def main():
      c = TelegramClient(os.getenv("TG_SESSION_NAME"), int(os.getenv("TG_API_ID")), os.getenv("TG_API_HASH"))
      await c.start(phone=os.getenv("TG_PHONE"))
      async for d in c.iter_dialogs():
          print(d.id, d.name)
  asyncio.run(main())
  ```
  Pick the channel ID (negative number for groups/channels), put in `TG_WATCHED_CHAT_ID`.
- **Telegram bot**: chat with @BotFather, `/newbot`, save the token to `TG_BOT_TOKEN`. Get your own user ID by DMing @userinfobot, save to `TG_BOT_OWNER_USER_ID`.
- **Anthropic**: `ANTHROPIC_API_KEY` from https://console.anthropic.com/.

### 3. MT5 setup

1. Tools → Options → Expert Advisors:
   - ✅ Allow algorithmic trading
   - ✅ Allow WebRequest for listed URL → add `http://127.0.0.1:8765`
2. Copy `ea/CopyTrades.mq5` to `<MT5 data>/MQL5/Experts/`
3. Open in MetaEditor (F4 in MT5), F7 to compile.
4. Drag onto any chart. Configure inputs (start with `MaxLotsPerSignal=0.01` for safety).

### 4. Run

In three separate terminals (or use NSSM/Task Scheduler for production):

```bash
# Terminal 1 — API for MT5
python -m src.api

# Terminal 2 — Telegram bot
python -m src.bot

# Terminal 3 — Telegram listener
python -m src.listener
```

First Telethon run prompts for your phone code in the terminal.

### 5. Verify

- DM your bot `/status` → should respond with kill switch state.
- DM your bot `/halt` → kill switch on. `/resume` → off.

## Tests

```bash
pytest                            # all tests except live AI replay
ANTHROPIC_API_KEY=... pytest tests/test_replay.py -v   # live AI replay
```

## Operations

- **Stop everything fast**: DM bot `/halt`. New actions won't be promoted to `sent`.
- **Cancel a single action**: tap [Cancel] on the notification, or `/cancel <id>`.
- **Force execute**: tap [Execute now] or `/execute <id>`.
- **See live state**: `/status`, `/positions`.
- **Manually close all**: `/closeall`.

## Risk warnings

- Run on a demo account for ≥2 weeks before going live.
- When going live, set `MaxLotsPerSignal=0.01` and run for ≥2 weeks before increasing.
- The kill switch does NOT cancel actions already promoted to `sent`. Use `/cancel <id>` for in-flight ones.
- Telethon uses your user account — keep API call rates low to avoid Telegram flagging.
