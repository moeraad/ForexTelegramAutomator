# 02 — Tech Stack

**Summary.** Python 3.11+ on Windows, MQL5 on MT5, single SQLite DB per stack, PySide6 GUI, optional NSSM Windows service hosting. The Python codebase is ~47K LOC across `src/`; the EA is ~5K LOC of MQL5. Build/deploy is informal — `pip install -e ".[dev]"` for dev, PyInstaller + Inno Setup for the GUI installer.

## Languages and primary runtimes

| Layer | Language | Runtime | LOC |
|---|---|---|---|
| Services + GUI | Python | CPython ≥ 3.11 (`pyproject.toml` line 4) | ~47,000 (`src/`) |
| Expert Advisor | MQL5 | MetaTrader 5 build ≥ 4885 (broker-dependent) | ~5,000 (`ea/`) |
| SQL | SQLite dialect | SQLite ≥ 3.35 (window funcs / JSON1 used) | `src/schema.sql` + 17 migration functions in `src/db.py` |
| Installer scripts | Batch + PowerShell + Inno Setup | Windows-only | `setup.bat`, `launch.bat`, `CopyTrades.iss`, `make-installer.ps1` |

## Python dependencies (`pyproject.toml`)

```
telethon              >=1.36       # listener (user-account Telegram client)
python-telegram-bot   >=21.0       # control bot (bot-token client)
anthropic             >=0.40       # Claude SDK (interpreter + triage when AI_PROVIDER=anthropic)
openai                >=1.50       # OpenAI SDK (gpt-5 / gpt-5-nano + text-embedding-3-small)
fastapi               >=0.115      # api.py
uvicorn[standard]     >=0.32       # api server (note: timeout_keep_alive=0 — see src/api.py:1280)
pydantic              >=2.9        # action validators
python-dotenv         >=1.0        # only used by the v1→v2 settings migration in src/db.py
httpx                 >=0.27       # used by news/feed scrapers in src/feeds/
yfinance              >=0.2.40     # macro feed (gold price, DXY, US10Y) for evaluator
PySide6               >=6.8        # GUI
pyqtgraph             >=0.13       # equity / cost / pipeline charts
certifi               >=2024.7     # explicit cert bundle for PyInstaller bundle
```

Dev: `pytest>=8.3`, `pytest-asyncio>=0.24`, `pytest-mock>=3.14`, `freezegun>=1.5`.

**Not present** (worth noting for the gaps doc): no `alembic` / migration framework (hand-rolled functions in `src/db.py`), no `passlib` / `argon2` / `bcrypt` (no password handling — single-operator), no Stripe/Paddle/Lemon Squeezy SDK, no email library, no FastAPI auth middleware beyond a shared static header.

## External services

| Service | Used by | Auth | Failure mode |
|---|---|---|---|
| Telegram MTProto (`api_id` + `api_hash` + phone number) | listener Telethon session | DPAPI-encrypted session blob in `settings.tg_session_blob` (`src/listener.py:489`) | session revocation → `is_user_authorized()` False → process raises RuntimeError on startup |
| Telegram Bot API (`bot_token`) | bot.py | DPAPI-encrypted in `settings.tg_bot_token` | bootstrap_retries=-1 — retries forever |
| Anthropic API | AIClient + TriageClient | `settings.anthropic_api_key` (DPAPI-encrypted) | listener exits at startup if missing; per-call failure → ALERT row |
| OpenAI API | when `AI_PROVIDER=openai` | `settings.openai_api_key` | same |
| OpenAI Embeddings (`text-embedding-3-small`) | trigger_matcher.py | reuses `openai_api_key` | embedding cache stale → falls through to Sonnet (safe) |
| yfinance (no key) | macro feed | unauth public scraper | stale snapshot → evaluator marks `data_quality: "reduced"` |
| Forex Factory / RSS-style news (`src/feeds/news_scan.py`, `calendar_fetch.py`) | bot feed loops | unauth | feed loop logs and continues |
| MetaTrader 5 broker | EA via OrderSend | broker credentials live in MT5 terminal, NOT in this codebase | broker reject → `failed` status with retcode in `ea_response` |

The system has no payment processor, no email provider, no SMS, no CRM, no cloud DB, no analytics, no CDN, no domain — it is **entirely local-to-one-Windows-machine**.

## Operating-system requirements

- **Windows 10 / 11** is the only supported target. Verified by:
  - `src/secret_box.py` uses `ctypes.windll.crypt32` (Windows DPAPI) — no fallback.
  - `services/install_services.bat` requires NSSM and `NET SESSION` (admin check).
  - `src/gui/services/elevation.py`, `src/gui/services/nssm_client.py` — Windows-specific.
  - PyInstaller spec is Windows-only (`CopyTrades.spec`).
- MT5 must run on the same machine (EA POSTs to `http://127.0.0.1:8765`). Cross-host setups would need WebRequest URL whitelisting and a token, but the loopback assumption is everywhere.
- Python venv at `.venv\Scripts\python.exe`.

## Build / install / deploy — as it exists today

### Developer setup

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env
:: edit .env manually OR run the GUI setup wizard
```

Or one-shot: `setup.bat`.

### Running services (dev — three console windows)

```cmd
launch.bat
```

Spawns `cmd /k` windows for `python -m src.api`, `python -m src.bot`, `python -m src.listener`. Guards against port 8765 already bound (`launch.bat:14`).

### Running as Windows services (production-ish)

```cmd
:: must run elevated
services\install_services.bat
```

Creates NSSM-managed services with hardcoded paths (`services/install_services.bat:23`):
- `CT-SMC-Api`, `CT-SMC-Bot`, `CT-SMC-Listener` (port 8766, SMC stack)
- `CT-AR-Api`, `CT-AR-Bot`, `CT-AR-Listener` (port 8765, Arabic stack)

The script encodes two-stack deployment as the default — both Arabic-language Forex Engineer AND a separate "SMC" channel from a sibling project. **Paths are absolute and hardcoded to one Windows username (`C:\Users\Administrator\Documents\Copy Trades` and `C:\Users\Administrator\Documents\projects\copytrades`).** This is not parametrized.

The GUI services-bar can also install/start/stop services via `src/gui/services/nssm_client.py`.

### Building the GUI installer

- `CopyTrades.spec` — PyInstaller spec (Windows, one-folder mode).
- `make-installer.ps1` → calls PyInstaller, then Inno Setup (`CopyTrades.iss`) to bundle the result into `installer/`.
- Build artifacts: `build/`, `dist/CopyTrades/`, `installer/Output/CopyTrades-Setup.exe`.
- Code signing: `scripts/sign_exe.bat` (Authenticode signtool wrapper — unclear from source whether a valid cert is currently configured; see `docs/08-OPEN-QUESTIONS.md`).

### Building the EA

Manual: open `ea/CopyTrades.mq5` in MetaEditor (F4 from MT5 → F7 to compile). `ea/compile_ea.bat` exists but its working state is **UNCLEAR** — it depends on a `metaeditor64.exe` path on the operator's machine that isn't parameterized in the bat file (file not opened in this audit).

### Tests

```cmd
.venv\Scripts\python.exe -m pytest -q              :: hermetic suite, ~166 tests
.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term-missing
```

Live AI replay tests are excluded by default (need `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`):
- `tests/test_replay.py` — OPEN-only fixtures.
- `tests/test_management_replay.py` — 7 management action types, state-driven.

GUI tests use Qt's `qtbot` (`tests/gui/conftest.py`).

## Inferred runtime numbers

- LLM cost: `cost_daily_budget_usd` default `5.00` USD/day, cap multiplier `1.2` → kill switch trips at $6.00/day (`src/db_settings.py:57`). Inferred: roughly bounds 50–200 interpreter calls/day depending on cache hit rate.
- EA polling: 1 Hz `/actions?status=sent`. 15 s market heartbeat. 60 s snapshot. 5 s evaluator fetch.
- Promoter loop: 1 Hz. Claim sweeper: 15 s, 300 s threshold.
- WAL checkpoint: every 1000 pages.

## Versions to be explicit about (so a stranger can reproduce)

- Anthropic default model: `claude-sonnet-4-6` (`src/db_settings.py:42`), triage `claude-haiku-4-5-20251001`.
- OpenAI defaults: `gpt-5` interpreter, `gpt-5-nano` triage (`src/db_settings.py:43`).
- Extended thinking enabled by default (`ai_thinking_enabled=1`, budget 4000 tokens).
- Magic number default: `919191` (override per stack).
- Symbol: `XAUUSD` — hardcoded everywhere (`config.SUPPORTED_SYMBOLS = {"XAUUSD"}`, `src/config.py:23`).
- Default `EA_SHARED_TOKEN` is blank → API runs unauthenticated when blank, which is the dev default (`src/api.py:138`). `_enforce_auth_bind_policy` warns if API binds non-loopback without a token (`src/api_helpers.py`).
