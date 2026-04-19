# CopyTrades Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Telegram → AI → MT5 bridge that reads a forex signals channel in real time, interprets messages with Claude Sonnet 4.6, and emits structured trading actions for an MQL5 Expert Advisor to execute. User stays in the loop via a Telegram control bot.

**Architecture:** Four cooperating processes around a shared SQLite database — `listener.py` (Telethon + AI), `bot.py` (Telegram control + promotion worker), `api.py` (FastAPI bridge MT5 reads from), and `CopyTrades.mq5` (MT5 EA that executes orders).

**Tech Stack:** Python 3.11+, Telethon (Telegram MTProto), python-telegram-bot (Bot API), Anthropic Python SDK (Claude Sonnet 4.6 with prompt caching), FastAPI + uvicorn, SQLite (WAL mode), Pydantic v2, pytest, MQL5.

**Reference:** spec at `docs/superpowers/specs/2026-04-19-copytrades-design.md`.

---

## File structure

```
copytrades/
├── pyproject.toml
├── .env.example
├── .gitignore
├── README.md
├── src/
│   ├── __init__.py
│   ├── config.py              # env loading, paths, constants
│   ├── db.py                  # SQLite connection, schema init
│   ├── schema.sql             # DDL
│   ├── validators.py          # Pydantic models + business rule validation
│   ├── state_summary.py       # builds OPEN POSITIONS block from DB
│   ├── ai.py                  # Anthropic client, prompt builder, retries
│   ├── ai_logger.py           # JSONL logger for AI calls
│   ├── listener.py            # Telethon entrypoint
│   ├── bot.py                 # Telegram bot + promotion worker
│   ├── telegram_format.py     # render bot notifications
│   └── api.py                 # FastAPI app for MT5
├── ea/
│   └── CopyTrades.mq5         # MT5 Expert Advisor
├── tests/
│   ├── __init__.py
│   ├── conftest.py            # fixtures (in-memory DB, etc.)
│   ├── test_db.py
│   ├── test_validators.py
│   ├── test_state_summary.py
│   ├── test_ai.py             # mocked Anthropic responses
│   ├── test_state_machine.py  # action lifecycle transitions
│   ├── test_telegram_format.py
│   ├── test_api.py
│   ├── test_promotion_worker.py
│   ├── test_replay.py         # AI behavior replay
│   └── test_integration.py    # end-to-end with mock EA
├── fixtures/
│   └── messages.jsonl         # replay test corpus
└── logs/                      # gitignored runtime logs
```

**Module responsibilities (one file = one job):**
- `db.py`: connection, transactions, schema init. No business logic.
- `validators.py`: Pydantic schemas + pure validation functions. No DB access except for ticket lookup.
- `state_summary.py`: build the human-readable OPEN POSITIONS string from the `positions` table.
- `ai.py`: prompt assembly + Anthropic API call + cache_control markers + retries. No DB writes.
- `listener.py`: orchestration only — Telethon callback → DB → state summary → AI → validator → DB.
- `bot.py`: Telegram bot + promotion worker (separate async tasks in same process).
- `api.py`: FastAPI endpoints. Thin wrappers over DB.

---

## Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `.env.example`, `.gitignore`, `src/__init__.py`, `src/config.py`, `tests/__init__.py`, `tests/conftest.py`, `logs/.gitkeep`, `fixtures/.gitkeep`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "copytrades"
version = "0.1.0"
description = "Telegram → AI → MT5 signal bridge for gold trading"
requires-python = ">=3.11"
dependencies = [
    "telethon>=1.36",
    "python-telegram-bot>=21.0",
    "anthropic>=0.40",
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "python-dotenv>=1.0",
    "httpx>=0.27",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "pytest-mock>=3.14",
    "freezegun>=1.5",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Create `.env.example`**

```
# Telegram user account (Telethon)
TG_API_ID=
TG_API_HASH=
TG_PHONE=
TG_SESSION_NAME=copytrades_session

# Watched channel — get this with: client.get_entity('channel_username')
TG_WATCHED_CHAT_ID=

# Telegram bot (control + notifications)
TG_BOT_TOKEN=
TG_BOT_OWNER_USER_ID=

# Anthropic
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-sonnet-4-6

# Local API for MT5
API_HOST=127.0.0.1
API_PORT=8765

# DB
DB_PATH=./copytrades.db
```

- [ ] **Step 3: Create `.gitignore`**

```
__pycache__/
*.pyc
.venv/
.env
*.session
*.session-journal
copytrades.db
copytrades.db-wal
copytrades.db-shm
logs/*.jsonl
logs/*.log
.pytest_cache/
*.egg-info/
build/
dist/
```

- [ ] **Step 4: Create `src/__init__.py`** (empty file)

- [ ] **Step 5: Create `src/config.py`**

```python
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "copytrades.db"))

TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_PHONE = os.getenv("TG_PHONE", "")
TG_SESSION_NAME = os.getenv("TG_SESSION_NAME", "copytrades_session")
TG_WATCHED_CHAT_ID = int(os.getenv("TG_WATCHED_CHAT_ID", "0"))

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_BOT_OWNER_USER_ID = int(os.getenv("TG_BOT_OWNER_USER_ID", "0"))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8765"))

DEFAULT_AUTO_EXECUTE_DELAY_SEC = 30
RECENT_CHAT_WINDOW = 20  # messages
SUPPORTED_SYMBOLS = {"XAUUSD"}
```

- [ ] **Step 6: Create `tests/__init__.py`** (empty), `tests/conftest.py`

```python
import pytest
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "src" / "schema.sql"


@pytest.fixture
def db():
    """In-memory SQLite DB with schema loaded."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    yield conn
    conn.close()
```

- [ ] **Step 7: Create directory placeholders**

```bash
mkdir -p logs fixtures ea src tests
touch logs/.gitkeep fixtures/.gitkeep
```

- [ ] **Step 8: Install dependencies and verify**

```bash
python -m venv .venv
.venv/Scripts/activate  # Windows; use .venv/bin/activate on Unix
pip install -e ".[dev]"
pytest --collect-only
```
Expected: pytest collects 0 tests (no tests yet) and exits 0.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml .env.example .gitignore src tests fixtures logs
git commit -m "chore: project scaffold and dependencies"
```

---

## Task 2: SQLite schema and connection helper

**Files:**
- Create: `src/schema.sql`, `src/db.py`, `tests/test_db.py`

- [ ] **Step 1: Write `src/schema.sql`**

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY,
  tg_message_id   INTEGER NOT NULL,
  chat_id         INTEGER NOT NULL,
  sender          TEXT,
  text            TEXT NOT NULL,
  received_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
  is_backfill     INTEGER DEFAULT 0,
  UNIQUE(chat_id, tg_message_id)
);

CREATE TABLE IF NOT EXISTS actions (
  id              INTEGER PRIMARY KEY,
  source_msg_id   INTEGER REFERENCES messages(id),
  action_type     TEXT NOT NULL,
  payload_json    TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending',
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  notified_at     DATETIME,
  execute_after   DATETIME,
  executed_at     DATETIME,
  ea_response     TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);

CREATE TABLE IF NOT EXISTS positions (
  id              INTEGER PRIMARY KEY,
  action_id       INTEGER REFERENCES actions(id),
  mt5_ticket      INTEGER UNIQUE,
  symbol          TEXT NOT NULL,
  side            TEXT NOT NULL,
  volume          REAL NOT NULL,
  entry_price     REAL,
  sl              REAL,
  tp              REAL,
  status          TEXT NOT NULL DEFAULT 'open',
  opened_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  closed_at       DATETIME,
  close_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS settings (
  key             TEXT PRIMARY KEY,
  value           TEXT NOT NULL
);

INSERT OR IGNORE INTO settings(key, value) VALUES
  ('kill_switch', 'off'),
  ('auto_execute_delay_sec', '30'),
  ('last_seen_tg_msg_id', '0');
```

- [ ] **Step 2: Write the failing test for `db.connect()` and schema init**

In `tests/test_db.py`:

```python
import sqlite3
from src.db import connect, init_schema, get_setting, set_setting


def test_init_schema_creates_tables(tmp_path):
    db_file = tmp_path / "test.db"
    conn = connect(str(db_file))
    init_schema(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = [r["name"] for r in rows]
    assert "actions" in names
    assert "messages" in names
    assert "positions" in names
    assert "settings" in names


def test_default_settings_seeded(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    assert get_setting(conn, "kill_switch") == "off"
    assert get_setting(conn, "auto_execute_delay_sec") == "30"


def test_set_and_get_setting(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    set_setting(conn, "kill_switch", "on")
    assert get_setting(conn, "kill_switch") == "on"
```

- [ ] **Step 3: Run tests to confirm they fail**

```bash
pytest tests/test_db.py -v
```
Expected: ImportError or ModuleNotFoundError (db module missing).

- [ ] **Step 4: Implement `src/db.py`**

```python
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
```

- [ ] **Step 5: Run tests, verify they pass**

```bash
pytest tests/test_db.py -v
```
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/schema.sql src/db.py tests/test_db.py
git commit -m "feat(db): SQLite schema and connection helper"
```

---

## Task 3: Pydantic action models

**Files:**
- Create: `src/validators.py`, `tests/test_validators.py`

- [ ] **Step 1: Write failing tests for action models**

In `tests/test_validators.py`:

```python
import pytest
from pydantic import ValidationError
from src.validators import (
    OpenAction, ModifyAction, CloseAction, CloseAllAction, AlertAction,
    AIResponse, parse_ai_response,
)


def test_open_action_valid():
    a = OpenAction(symbol="XAUUSD", side="BUY",
                   entry_low=4864, entry_high=4866,
                   tps=[4880, 4900], sl=4855)
    assert a.type == "OPEN"
    assert a.tps == [4880, 4900]


def test_open_action_rejects_non_gold():
    with pytest.raises(ValidationError):
        OpenAction(symbol="EURUSD", side="BUY",
                   entry_low=1, entry_high=2, tps=[3], sl=0.5)


def test_open_action_rejects_empty_tps():
    with pytest.raises(ValidationError):
        OpenAction(symbol="XAUUSD", side="BUY",
                   entry_low=4864, entry_high=4866, tps=[], sl=4855)


def test_open_action_rejects_invalid_side():
    with pytest.raises(ValidationError):
        OpenAction(symbol="XAUUSD", side="HOLD",
                   entry_low=4864, entry_high=4866, tps=[4880], sl=4855)


def test_close_action_requires_ticket():
    a = CloseAction(mt5_ticket=12345, reason="ai")
    assert a.type == "CLOSE"
    with pytest.raises(ValidationError):
        CloseAction(reason="ai")  # missing ticket


def test_parse_ai_response_dispatches_action_type():
    raw = '''{
      "actions": [
        {"type":"OPEN","symbol":"XAUUSD","side":"BUY",
         "entry_low":4864,"entry_high":4866,"tps":[4880],"sl":4855}
      ],
      "reasoning":"clean signal"
    }'''
    resp = parse_ai_response(raw)
    assert len(resp.actions) == 1
    assert isinstance(resp.actions[0], OpenAction)


def test_parse_ai_response_rejects_bad_json():
    with pytest.raises(ValueError):
        parse_ai_response("not json")


def test_parse_ai_response_unknown_action_type():
    raw = '{"actions":[{"type":"YOLO"}],"reasoning":"x"}'
    with pytest.raises(ValueError):
        parse_ai_response(raw)
```

- [ ] **Step 2: Run tests, confirm they fail**

```bash
pytest tests/test_validators.py -v
```
Expected: ImportError.

- [ ] **Step 3: Implement `src/validators.py`**

```python
import json
from typing import Literal, Union
from pydantic import BaseModel, Field, field_validator
from src.config import SUPPORTED_SYMBOLS


class OpenAction(BaseModel):
    type: Literal["OPEN"] = "OPEN"
    symbol: str
    side: Literal["BUY", "SELL"]
    entry_low: float
    entry_high: float
    tps: list[float] = Field(min_length=1)
    sl: float
    comment: str = ""

    @field_validator("symbol")
    @classmethod
    def supported(cls, v: str) -> str:
        if v not in SUPPORTED_SYMBOLS:
            raise ValueError(f"unsupported symbol {v}")
        return v


class ModifyAction(BaseModel):
    type: Literal["MODIFY"] = "MODIFY"
    mt5_ticket: int
    new_sl: float | None = None
    new_tp: float | None = None


class CloseAction(BaseModel):
    type: Literal["CLOSE"] = "CLOSE"
    mt5_ticket: int
    reason: str = ""


class CloseAllAction(BaseModel):
    type: Literal["CLOSE_ALL"] = "CLOSE_ALL"
    symbol: str
    reason: str = ""


class AlertAction(BaseModel):
    type: Literal["ALERT"] = "ALERT"
    level: Literal["info", "warning"] = "info"
    text: str


Action = Union[OpenAction, ModifyAction, CloseAction, CloseAllAction, AlertAction]

_ACTION_BY_TYPE = {
    "OPEN": OpenAction,
    "MODIFY": ModifyAction,
    "CLOSE": CloseAction,
    "CLOSE_ALL": CloseAllAction,
    "ALERT": AlertAction,
}


class AIResponse(BaseModel):
    actions: list[Action]
    reasoning: str = ""


def parse_ai_response(raw: str) -> AIResponse:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}")
    actions = []
    for a in data.get("actions", []):
        t = a.get("type")
        cls = _ACTION_BY_TYPE.get(t)
        if cls is None:
            raise ValueError(f"unknown action type: {t}")
        actions.append(cls(**a))
    return AIResponse(actions=actions, reasoning=data.get("reasoning", ""))
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
pytest tests/test_validators.py -v
```
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/validators.py tests/test_validators.py
git commit -m "feat(validators): Pydantic action models"
```

---

## Task 4: Business-rule validation

**Files:**
- Modify: `src/validators.py`, `tests/test_validators.py`

- [ ] **Step 1: Add failing tests for business rules**

Append to `tests/test_validators.py`:

```python
from src.validators import validate_action, ValidationResult
from src.db import connect, init_schema


@pytest.fixture
def db_with_position(tmp_path):
    conn = connect(str(tmp_path / "v.db"))
    init_schema(conn)
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status) "
        "VALUES(1, 99001, 'XAUUSD', 'BUY', 0.10, 4865, 4855, 4880, 'open')"
    )
    return conn


def test_validate_open_passes(db_with_position):
    a = OpenAction(symbol="XAUUSD", side="BUY",
                   entry_low=4870, entry_high=4872,
                   tps=[4900], sl=4860)
    r = validate_action(a, db_with_position)
    assert r.ok


def test_validate_close_unknown_ticket_fails(db_with_position):
    a = CloseAction(mt5_ticket=999999)
    r = validate_action(a, db_with_position)
    assert not r.ok
    assert "unknown" in r.error.lower()


def test_validate_close_known_ticket_passes(db_with_position):
    a = CloseAction(mt5_ticket=99001)
    r = validate_action(a, db_with_position)
    assert r.ok


def test_validate_modify_closed_position_fails(db_with_position):
    db_with_position.execute(
        "UPDATE positions SET status='closed' WHERE mt5_ticket=99001"
    )
    a = ModifyAction(mt5_ticket=99001, new_sl=4866)
    r = validate_action(a, db_with_position)
    assert not r.ok


def test_validate_open_duplicate_zone_fails(db_with_position):
    a = OpenAction(symbol="XAUUSD", side="BUY",
                   entry_low=4864, entry_high=4866,  # overlaps existing 4865
                   tps=[4900], sl=4855)
    r = validate_action(a, db_with_position)
    assert not r.ok
    assert "duplicate" in r.error.lower()
```

- [ ] **Step 2: Run, confirm failures**

```bash
pytest tests/test_validators.py -v
```
Expected: ImportError on `validate_action`/`ValidationResult`.

- [ ] **Step 3: Add the validator to `src/validators.py`**

Append to `src/validators.py`:

```python
import sqlite3
from dataclasses import dataclass


@dataclass
class ValidationResult:
    ok: bool
    error: str = ""


def _ticket_open(conn: sqlite3.Connection, ticket: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM positions WHERE mt5_ticket=? AND status='open'",
        (ticket,),
    ).fetchone()
    return row is not None


def _has_overlapping_open_position(
    conn: sqlite3.Connection, symbol: str, side: str,
    entry_low: float, entry_high: float,
) -> bool:
    rows = conn.execute(
        "SELECT entry_price FROM positions "
        "WHERE symbol=? AND side=? AND status='open'",
        (symbol, side),
    ).fetchall()
    for r in rows:
        ep = r["entry_price"]
        if ep is None:
            continue
        if entry_low <= ep <= entry_high:
            return True
    return False


def validate_action(action: Action, conn: sqlite3.Connection) -> ValidationResult:
    if isinstance(action, OpenAction):
        if _has_overlapping_open_position(
            conn, action.symbol, action.side, action.entry_low, action.entry_high
        ):
            return ValidationResult(False, "duplicate: overlaps existing open position")
        return ValidationResult(True)
    if isinstance(action, (CloseAction, ModifyAction)):
        if not _ticket_open(conn, action.mt5_ticket):
            return ValidationResult(False, f"unknown or closed ticket {action.mt5_ticket}")
        return ValidationResult(True)
    if isinstance(action, CloseAllAction):
        if action.symbol not in SUPPORTED_SYMBOLS:
            return ValidationResult(False, f"unsupported symbol {action.symbol}")
        return ValidationResult(True)
    # AlertAction
    return ValidationResult(True)
```

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/test_validators.py -v
```
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/validators.py tests/test_validators.py
git commit -m "feat(validators): business-rule validation against DB state"
```

---

## Task 5: State summary builder

**Files:**
- Create: `src/state_summary.py`, `tests/test_state_summary.py`

- [ ] **Step 1: Write failing tests**

In `tests/test_state_summary.py`:

```python
from src.db import connect, init_schema
from src.state_summary import render_open_positions


def _setup(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    return conn


def test_no_positions_renders_empty_marker(tmp_path):
    conn = _setup(tmp_path)
    out = render_open_positions(conn)
    assert "OPEN POSITIONS" in out
    assert "(none)" in out


def test_renders_open_positions_only(tmp_path):
    conn = _setup(tmp_path)
    conn.execute("INSERT INTO actions(action_type, payload_json) VALUES('OPEN','{}')")
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status) "
        "VALUES(1, 555, 'XAUUSD', 'BUY', 0.10, 4865.0, 4855.0, 4880.0, 'open')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status, closed_at, close_reason) "
        "VALUES(1, 666, 'XAUUSD', 'BUY', 0.10, 4865.0, 4855.0, 4900.0, "
        "'closed', CURRENT_TIMESTAMP, 'tp')"
    )
    out = render_open_positions(conn)
    assert "555" in out
    assert "666" not in out


def test_renders_groupings_by_action_id(tmp_path):
    conn = _setup(tmp_path)
    conn.execute("INSERT INTO actions(action_type, payload_json) VALUES('OPEN','{}')")
    for tp in (4880, 4900, 4920):
        conn.execute(
            "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
            "entry_price, sl, tp, status) "
            f"VALUES(1, {1000 + tp}, 'XAUUSD', 'BUY', 0.10, 4865.0, 4855.0, {tp}, 'open')"
        )
    out = render_open_positions(conn)
    assert out.count("Signal #1") == 1  # grouped header
    assert "ticket=5880" in out
    assert "ticket=5900" in out
    assert "ticket=5920" in out
```

- [ ] **Step 2: Run, confirm failure**

```bash
pytest tests/test_state_summary.py -v
```
Expected: ImportError.

- [ ] **Step 3: Implement `src/state_summary.py`**

```python
import sqlite3
from collections import defaultdict


def render_open_positions(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp FROM positions WHERE status='open' "
        "ORDER BY action_id, mt5_ticket"
    ).fetchall()

    lines = ["OPEN POSITIONS (from this channel):"]
    if not rows:
        lines.append("  (none)")
        return "\n".join(lines)

    by_signal: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_signal[r["action_id"]].append(r)

    for action_id, group in by_signal.items():
        lines.append(f"  Signal #{action_id}:")
        for r in group:
            lines.append(
                f"    ticket={r['mt5_ticket']}  {r['side']} {r['symbol']}  "
                f"vol={r['volume']:.2f}  entry={r['entry_price']:.2f}  "
                f"sl={r['sl']:.2f}  tp={r['tp']:.2f}"
            )
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests, verify pass**

```bash
pytest tests/test_state_summary.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/state_summary.py tests/test_state_summary.py
git commit -m "feat(state): render open positions summary for AI prompt"
```

---

## Task 6: AI logger

**Files:**
- Create: `src/ai_logger.py`, `tests/test_ai_logger.py`

- [ ] **Step 1: Write failing test**

In `tests/test_ai_logger.py`:

```python
import json
from src.ai_logger import log_call


def test_log_call_appends_jsonl(tmp_path):
    log_path = tmp_path / "ai.jsonl"
    log_call(log_path, {
        "prompt": "hello",
        "response": "world",
        "latency_ms": 42,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 7,
    })
    log_call(log_path, {"prompt": "again", "response": "x"})
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["prompt"] == "hello"
    assert "ts" in rec
```

- [ ] **Step 2: Run, confirm failure**

```bash
pytest tests/test_ai_logger.py -v
```

- [ ] **Step 3: Implement `src/ai_logger.py`**

```python
import json
from datetime import datetime, timezone
from pathlib import Path


def log_call(path: Path | str, record: dict) -> None:
    record = dict(record)
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
```

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/test_ai_logger.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/ai_logger.py tests/test_ai_logger.py
git commit -m "feat(ai): JSONL logger for AI calls"
```

---

## Task 7: AI client and prompt builder

**Files:**
- Create: `src/ai.py`, `tests/test_ai.py`

The AI module assembles three prompt blocks (system, recent chat, fresh state + new message), calls Claude Sonnet 4.6 with `cache_control` markers on the first two blocks, and returns the parsed `AIResponse`.

**Reference:** Anthropic prompt-caching docs use `cache_control: {"type": "ephemeral"}` on content blocks. Place markers on the END of each block you want cached so everything up to that marker is part of the cache key.

- [ ] **Step 1: Write the system prompt**

This is the model's job description. Keep it stable — it changes only when we tune behavior.

Save inline in `src/ai.py` as `SYSTEM_PROMPT`:

```
You are a signal interpreter for a forex Telegram channel that posts gold (XAUUSD) trade ideas. You read incoming messages plus the current state of open positions and decide what trading actions to emit.

OUTPUT FORMAT:
You MUST output a single JSON object and nothing else. Schema:
{
  "actions": [ ... zero or more action objects ... ],
  "reasoning": "short explanation of why you chose these actions"
}

ACTION TYPES:

OPEN — a new trade signal:
  {"type":"OPEN","symbol":"XAUUSD","side":"BUY"|"SELL",
   "entry_low":<float>,"entry_high":<float>,
   "tps":[<float>,...],"sl":<float>,"comment":"short tag"}

MODIFY — change SL/TP on an existing position. Reference an mt5_ticket from OPEN POSITIONS:
  {"type":"MODIFY","mt5_ticket":<int>,"new_sl":<float|null>,"new_tp":<float|null>}

CLOSE — close one specific position by mt5_ticket:
  {"type":"CLOSE","mt5_ticket":<int>,"reason":"<text>"}

CLOSE_ALL — close every open position for the given symbol:
  {"type":"CLOSE_ALL","symbol":"XAUUSD","reason":"<text>"}

ALERT — info only, no trade:
  {"type":"ALERT","level":"info"|"warning","text":"<text>"}

DECISION RULES:
1. Emit OPEN only when the message is a CLEAR new trade with at least an entry, an SL, and one TP. Vague analysis or commentary → no OPEN.
2. If the message references an existing position (e.g. "move SL to BE", "take partial at TP1", "close half"), emit MODIFY or CLOSE with the right mt5_ticket from OPEN POSITIONS. If you can't tell which position, emit ALERT.
3. "Close all gold", "exit everything", "out now" → CLOSE_ALL.
4. News warnings ("NFP coming, be careful"), opinions, market commentary → ALERT only.
5. NEVER emit OPEN for a signal that is already represented in OPEN POSITIONS (entry zone overlaps, same side).
6. If you are uncertain, emit ALERT and explain in `reasoning`. Do NOT emit speculative trades.
7. Symbol is always XAUUSD. If a non-gold instrument is mentioned, emit ALERT, do not OPEN.

Be precise. Output JSON ONLY.
```

- [ ] **Step 2: Write failing tests using a mocked Anthropic client**

In `tests/test_ai.py`:

```python
from unittest.mock import MagicMock, patch
from src.ai import build_messages, call_ai, AIClient


def test_build_messages_structure():
    msgs = build_messages(
        recent_chat="[14:30] Yusuf: gold pumping",
        open_positions_block="OPEN POSITIONS:\n  (none)",
        new_message="[14:35] Yusuf: BUY GOLD 4866-4864 SL 4855 TP 4880",
    )
    assert msgs[0]["role"] == "user"
    blocks = msgs[0]["content"]
    assert isinstance(blocks, list)
    assert any("recent_chat".lower() in str(b).lower() or "14:30" in str(b) for b in blocks)
    cached = [b for b in blocks if b.get("cache_control")]
    assert len(cached) >= 1


def test_call_ai_returns_parsed_response():
    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.content = [MagicMock(text='{"actions":[{"type":"ALERT","level":"info","text":"ok"}],"reasoning":"x"}')]
    fake_resp.usage.input_tokens = 100
    fake_resp.usage.output_tokens = 20
    fake_resp.usage.cache_read_input_tokens = 80
    fake_resp.usage.cache_creation_input_tokens = 0
    fake_client.messages.create.return_value = fake_resp

    client = AIClient(client=fake_client, model="claude-sonnet-4-6")
    result = client.call(
        recent_chat="...",
        open_positions_block="OPEN POSITIONS:\n  (none)",
        new_message="hi",
    )
    assert result.response.actions[0].type == "ALERT"
    assert result.usage["cache_read_tokens"] == 80


def test_call_ai_retries_on_transient_error():
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        Exception("boom"),
        Exception("boom2"),
        MagicMock(content=[MagicMock(text='{"actions":[],"reasoning":""}')],
                  usage=MagicMock(input_tokens=1, output_tokens=1,
                                  cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0)),
    ]
    client = AIClient(client=fake_client, model="claude-sonnet-4-6", max_retries=3, retry_sleep=0.0)
    result = client.call("", "", "hi")
    assert result.response.actions == []
    assert fake_client.messages.create.call_count == 3
```

- [ ] **Step 3: Run, confirm failure**

```bash
pytest tests/test_ai.py -v
```

- [ ] **Step 4: Implement `src/ai.py`**

```python
import time
from dataclasses import dataclass
from typing import Any
import anthropic
from src.validators import AIResponse, parse_ai_response

SYSTEM_PROMPT = """[paste the SYSTEM_PROMPT text from Step 1 above, verbatim]"""


def build_messages(
    recent_chat: str,
    open_positions_block: str,
    new_message: str,
) -> list[dict[str, Any]]:
    """Three-block content with cache_control on the first two blocks."""
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"RECENT CHAT (last messages, oldest first):\n{recent_chat}",
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": f"{open_positions_block}\n\nNEW MESSAGE:\n{new_message}",
                },
            ],
        }
    ]


@dataclass
class AICallResult:
    response: AIResponse
    raw_text: str
    usage: dict
    latency_ms: int


class AIClient:
    def __init__(self, client=None, model: str = "claude-sonnet-4-6",
                 max_retries: int = 3, retry_sleep: float = 1.5):
        self._client = client or anthropic.Anthropic()
        self._model = model
        self._max_retries = max_retries
        self._retry_sleep = retry_sleep

    def call(self, recent_chat: str, open_positions_block: str,
             new_message: str) -> AICallResult:
        messages = build_messages(recent_chat, open_positions_block, new_message)
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                t0 = time.monotonic()
                resp = self._client.messages.create(
                    model=self._model,
                    max_tokens=1024,
                    system=[
                        {"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}
                    ],
                    messages=messages,
                )
                latency_ms = int((time.monotonic() - t0) * 1000)
                raw = resp.content[0].text
                parsed = parse_ai_response(raw)
                usage = {
                    "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                    "cache_read_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
                    "cache_creation_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
                }
                return AICallResult(parsed, raw, usage, latency_ms)
            except Exception as e:
                last_exc = e
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_sleep * (2 ** attempt))
        raise RuntimeError(f"AI call failed after retries: {last_exc}")


def call_ai(recent_chat: str, open_positions_block: str,
            new_message: str) -> AICallResult:
    return AIClient().call(recent_chat, open_positions_block, new_message)
```

**IMPORTANT:** Replace the `SYSTEM_PROMPT = """..."""` placeholder string with the actual text from Step 1. The triple-quoted string must contain the entire system prompt verbatim.

- [ ] **Step 5: Run tests, verify pass**

```bash
pytest tests/test_ai.py -v
```
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/ai.py tests/test_ai.py
git commit -m "feat(ai): Anthropic client with prompt caching and retries"
```

---

## Task 8: Action persistence and orchestration helper

**Files:**
- Create: `src/orchestrator.py`, `tests/test_orchestrator.py`

`orchestrator.process_message()` is the single entry point that `listener.py` will call: it inserts the message, builds the AI prompt, calls AI, validates each action, persists valid ones with `execute_after`, persists rejected ones with `status='rejected'`, and returns the list of inserted action IDs.

- [ ] **Step 1: Write failing tests**

In `tests/test_orchestrator.py`:

```python
import json
from unittest.mock import MagicMock
from src.db import connect, init_schema
from src.orchestrator import process_message
from src.ai import AICallResult
from src.validators import AIResponse, OpenAction, AlertAction


def _make_ai(actions, reasoning=""):
    client = MagicMock()
    client.call.return_value = AICallResult(
        response=AIResponse(actions=actions, reasoning=reasoning),
        raw_text="{}",
        usage={"input_tokens": 1, "output_tokens": 1,
               "cache_read_tokens": 0, "cache_creation_tokens": 0},
        latency_ms=10,
    )
    return client


def test_process_message_persists_valid_open(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _make_ai([OpenAction(symbol="XAUUSD", side="BUY",
                              entry_low=4864, entry_high=4866,
                              tps=[4880], sl=4855)])
    ids = process_message(
        conn, ai, tg_message_id=1, chat_id=42,
        sender="Yusuf", text="BUY GOLD",
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
    )
    assert len(ids) == 1
    row = conn.execute("SELECT * FROM actions WHERE id=?", (ids[0],)).fetchone()
    assert row["status"] == "pending"
    assert row["action_type"] == "OPEN"
    assert row["execute_after"] is not None
    payload = json.loads(row["payload_json"])
    assert payload["entry_low"] == 4864


def test_process_message_alerts_have_no_execute_after(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _make_ai([AlertAction(level="warning", text="NFP coming")])
    ids = process_message(
        conn, ai, tg_message_id=2, chat_id=42,
        sender="Yusuf", text="careful, NFP",
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
    )
    row = conn.execute("SELECT * FROM actions WHERE id=?", (ids[0],)).fetchone()
    assert row["action_type"] == "ALERT"
    assert row["execute_after"] is None


def test_process_message_skips_duplicate_message(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _make_ai([])
    process_message(conn, ai, 5, 42, "Y", "hi", tmp_path / "a.jsonl", 30)
    process_message(conn, ai, 5, 42, "Y", "hi", tmp_path / "a.jsonl", 30)
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert n == 1


def test_process_message_rejects_invalid_action_writes_rejected_row(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    # AI returns a CLOSE on a ticket that doesn't exist
    from src.validators import CloseAction
    ai = _make_ai([CloseAction(mt5_ticket=12345)])
    ids = process_message(conn, ai, 7, 42, "Y", "close", tmp_path / "a.jsonl", 30)
    row = conn.execute("SELECT * FROM actions WHERE id=?", (ids[0],)).fetchone()
    assert row["status"] == "rejected"
    assert row["execute_after"] is None
```

- [ ] **Step 2: Run tests, confirm failures**

```bash
pytest tests/test_orchestrator.py -v
```

- [ ] **Step 3: Implement `src/orchestrator.py`**

```python
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src.ai import AIClient
from src.ai_logger import log_call
from src.state_summary import render_open_positions
from src.validators import (
    Action, AlertAction, validate_action,
    OpenAction, ModifyAction, CloseAction, CloseAllAction,
)


RECENT_CHAT_WINDOW = 20


def _insert_message(conn: sqlite3.Connection, tg_message_id: int, chat_id: int,
                    sender: str, text: str) -> int | None:
    """Returns row id, or None if duplicate."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages(tg_message_id, chat_id, sender, text) "
        "VALUES(?,?,?,?)",
        (tg_message_id, chat_id, sender, text),
    )
    if cur.rowcount == 0:
        return None
    return cur.lastrowid


def _recent_chat_text(conn: sqlite3.Connection, chat_id: int, limit: int) -> str:
    rows = conn.execute(
        "SELECT sender, text, received_at FROM messages "
        "WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    rows = list(reversed(rows))
    return "\n".join(f"[{r['received_at']}] {r['sender']}: {r['text']}" for r in rows)


def _payload_for(action: Action) -> dict:
    return action.model_dump(exclude={"type"})


def _action_type(action: Action) -> str:
    return action.type


def process_message(
    conn: sqlite3.Connection,
    ai: AIClient,
    tg_message_id: int,
    chat_id: int,
    sender: str,
    text: str,
    ai_log_path: Path | str,
    auto_execute_delay_sec: int,
) -> list[int]:
    """Insert message, call AI, validate + persist actions. Returns inserted action IDs."""
    msg_id = _insert_message(conn, tg_message_id, chat_id, sender, text)
    if msg_id is None:
        return []  # duplicate

    open_positions_block = render_open_positions(conn)
    recent_chat = _recent_chat_text(conn, chat_id, RECENT_CHAT_WINDOW)

    try:
        result = ai.call(recent_chat, open_positions_block, f"{sender}: {text}")
    except Exception as e:
        # Persist as ALERT so user is informed
        cur = conn.execute(
            "INSERT INTO actions(source_msg_id, action_type, payload_json, status) "
            "VALUES(?, 'ALERT', ?, 'pending')",
            (msg_id, json.dumps({"level": "warning", "text": f"AI error: {e}"})),
        )
        log_call(ai_log_path, {"error": str(e), "msg_id": msg_id})
        return [cur.lastrowid]

    log_call(ai_log_path, {
        "msg_id": msg_id,
        "raw_response": result.raw_text,
        "latency_ms": result.latency_ms,
        **result.usage,
    })

    inserted: list[int] = []
    for action in result.response.actions:
        v = validate_action(action, conn)
        payload = json.dumps(_payload_for(action))
        if not v.ok:
            cur = conn.execute(
                "INSERT INTO actions(source_msg_id, action_type, payload_json, "
                "status, ea_response) VALUES(?, ?, ?, 'rejected', ?)",
                (msg_id, _action_type(action), payload, v.error),
            )
            inserted.append(cur.lastrowid)
            continue

        # ALERTs do not get auto-executed
        if isinstance(action, AlertAction):
            cur = conn.execute(
                "INSERT INTO actions(source_msg_id, action_type, payload_json, status) "
                "VALUES(?, 'ALERT', ?, 'pending')",
                (msg_id, payload),
            )
            inserted.append(cur.lastrowid)
            continue

        execute_after = (
            datetime.now(timezone.utc) + timedelta(seconds=auto_execute_delay_sec)
        ).isoformat()
        cur = conn.execute(
            "INSERT INTO actions(source_msg_id, action_type, payload_json, "
            "status, execute_after) VALUES(?, ?, ?, 'pending', ?)",
            (msg_id, _action_type(action), payload, execute_after),
        )
        inserted.append(cur.lastrowid)
    return inserted
```

- [ ] **Step 4: Run tests, verify pass**

```bash
pytest tests/test_orchestrator.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): single-message pipeline (insert → AI → validate → persist)"
```

---

## Task 9: Telethon listener entrypoint

**Files:**
- Create: `src/listener.py`

This is a runtime script — no automated tests (covered by integration tests later). Manual smoke test in Step 5.

- [ ] **Step 1: Implement `src/listener.py`**

```python
import asyncio
import logging
from telethon import TelegramClient, events
from src import config
from src.db import connect, init_schema, get_setting, set_setting
from src.ai import AIClient
from src.orchestrator import process_message
from src.config import LOGS_DIR

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [listener] %(levelname)s %(message)s")
log = logging.getLogger(__name__)


async def main() -> None:
    conn = connect(config.DB_PATH)
    init_schema(conn)
    ai = AIClient(model=config.ANTHROPIC_MODEL)
    ai_log_path = LOGS_DIR / "ai_calls.jsonl"

    client = TelegramClient(config.TG_SESSION_NAME, config.TG_API_ID, config.TG_API_HASH)
    await client.start(phone=config.TG_PHONE)

    last_seen = int(get_setting(conn, "last_seen_tg_msg_id") or "0")
    log.info("listener started; last_seen_tg_msg_id=%s", last_seen)

    @client.on(events.NewMessage(chats=config.TG_WATCHED_CHAT_ID))
    async def handler(event):
        msg = event.message
        delay = int(get_setting(conn, "auto_execute_delay_sec") or "30")
        sender_name = "unknown"
        try:
            sender = await event.get_sender()
            sender_name = getattr(sender, "username", None) or getattr(sender, "first_name", "unknown")
        except Exception:
            pass
        text = msg.message or ""
        log.info("received tg_msg_id=%s text=%r", msg.id, text[:80])
        ids = await asyncio.to_thread(
            process_message, conn, ai, msg.id, config.TG_WATCHED_CHAT_ID,
            sender_name, text, ai_log_path, delay,
        )
        if ids:
            log.info("inserted action ids=%s", ids)
        set_setting(conn, "last_seen_tg_msg_id", str(msg.id))

    # Backfill any missed messages while offline (do NOT process via AI)
    backfilled = 0
    async for old_msg in client.iter_messages(config.TG_WATCHED_CHAT_ID, min_id=last_seen):
        sender_name = "unknown"
        try:
            sender = await old_msg.get_sender()
            sender_name = getattr(sender, "username", None) or getattr(sender, "first_name", "unknown")
        except Exception:
            pass
        text = old_msg.message or ""
        conn.execute(
            "INSERT OR IGNORE INTO messages(tg_message_id, chat_id, sender, text, is_backfill) "
            "VALUES(?,?,?,?,1)",
            (old_msg.id, config.TG_WATCHED_CHAT_ID, sender_name, text),
        )
        backfilled += 1

    if backfilled > 0:
        # Single ALERT action so the user knows
        import json
        conn.execute(
            "INSERT INTO actions(source_msg_id, action_type, payload_json, status) "
            "VALUES(NULL, 'ALERT', ?, 'pending')",
            (json.dumps({
                "level": "warning",
                "text": f"You missed {backfilled} messages while listener was offline. "
                        "These were NOT processed by AI to avoid stale signals."
            }),),
        )
        log.info("backfilled %s messages (no AI processing)", backfilled)

    log.info("listener live; awaiting new messages")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Smoke-test the import**

```bash
python -c "import src.listener; print('ok')"
```
Expected: `ok` (no syntax errors).

- [ ] **Step 3: Document the Telegram first-run setup in README**

(Will be done in Task 17; for now, leave a note in the file header.)

- [ ] **Step 4: Commit**

```bash
git add src/listener.py
git commit -m "feat(listener): Telethon entrypoint with backfill alert"
```

---

## Task 10: FastAPI bridge for MT5

**Files:**
- Create: `src/api.py`, `tests/test_api.py`

- [ ] **Step 1: Write failing tests**

In `tests/test_api.py`:

```python
import json
from fastapi.testclient import TestClient
from src.db import connect, init_schema
from src.api import build_app


def _setup(tmp_path):
    conn = connect(str(tmp_path / "api.db"))
    init_schema(conn)
    return conn


def test_get_actions_returns_only_sent(tmp_path):
    conn = _setup(tmp_path)
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'sent')",
        (json.dumps({"symbol": "XAUUSD", "side": "BUY",
                     "entry_low": 4864, "entry_high": 4866,
                     "tps": [4880], "sl": 4855}),),
    )
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'pending')"
    )
    app = build_app(conn)
    client = TestClient(app)
    r = client.get("/actions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["actions"]) == 1
    assert body["actions"][0]["action_type"] == "OPEN"


def test_get_kill_switch(tmp_path):
    conn = _setup(tmp_path)
    app = build_app(conn)
    client = TestClient(app)
    r = client.get("/settings/kill_switch")
    assert r.status_code == 200
    assert r.json() == {"key": "kill_switch", "value": "off"}


def test_post_result_executed(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'sent')"
    )
    aid = cur.lastrowid
    app = build_app(conn)
    client = TestClient(app)
    r = client.post(f"/actions/{aid}/result", json={
        "status": "executed",
        "mt5_ticket": 99001,
        "snapshot": {
            "symbol": "XAUUSD", "side": "BUY", "volume": 0.10,
            "entry_price": 4865.0, "sl": 4855.0, "tp": 4880.0,
        },
    })
    assert r.status_code == 200
    row = conn.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "executed"
    pos = conn.execute("SELECT * FROM positions WHERE mt5_ticket=99001").fetchone()
    assert pos is not None
    assert pos["status"] == "open"


def test_post_result_close_marks_position_closed(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status) VALUES(?, 555, 'XAUUSD', 'BUY', "
        "0.10, 4865, 4855, 4880, 'open')",
        (cur.lastrowid,),
    )
    app = build_app(conn)
    client = TestClient(app)
    r = client.post("/positions/555/close", json={"reason": "tp"})
    assert r.status_code == 200
    pos = conn.execute("SELECT * FROM positions WHERE mt5_ticket=555").fetchone()
    assert pos["status"] == "closed"
    assert pos["close_reason"] == "tp"
```

- [ ] **Step 2: Run, confirm failures**

```bash
pytest tests/test_api.py -v
```

- [ ] **Step 3: Implement `src/api.py`**

```python
import json
import sqlite3
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


class ResultBody(BaseModel):
    status: str  # "executed" | "failed" | "rejected"
    mt5_ticket: int | None = None
    error: str | None = None
    snapshot: dict | None = None


class CloseBody(BaseModel):
    reason: str = ""


def build_app(conn: sqlite3.Connection) -> FastAPI:
    app = FastAPI()

    @app.get("/actions")
    def get_actions(status: str = "sent", limit: int = 50):
        rows = conn.execute(
            "SELECT id, action_type, payload_json, status, created_at "
            "FROM actions WHERE status=? ORDER BY id ASC LIMIT ?",
            (status, limit),
        ).fetchall()
        return {
            "actions": [
                {
                    "id": r["id"],
                    "action_type": r["action_type"],
                    "payload": json.loads(r["payload_json"]),
                    "status": r["status"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }

    @app.get("/settings/{key}")
    def get_setting(key: str):
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            raise HTTPException(404)
        return {"key": key, "value": row["value"]}

    @app.post("/actions/{action_id}/result")
    def post_result(action_id: int, body: ResultBody):
        row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if row is None:
            raise HTTPException(404)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE actions SET status=?, executed_at=?, ea_response=? WHERE id=?",
            (body.status, now, body.error, action_id),
        )
        if body.status == "executed" and body.mt5_ticket and body.snapshot:
            s = body.snapshot
            conn.execute(
                "INSERT OR REPLACE INTO positions(action_id, mt5_ticket, symbol, side, "
                "volume, entry_price, sl, tp, status, opened_at) "
                "VALUES(?,?,?,?,?,?,?,?, 'open', ?)",
                (action_id, body.mt5_ticket, s.get("symbol"), s.get("side"),
                 s.get("volume"), s.get("entry_price"), s.get("sl"), s.get("tp"), now),
            )
        return {"ok": True}

    @app.post("/positions/{ticket}/close")
    def close_position(ticket: int, body: CloseBody):
        row = conn.execute(
            "SELECT 1 FROM positions WHERE mt5_ticket=?", (ticket,)
        ).fetchone()
        if row is None:
            raise HTTPException(404)
        conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, close_reason=? "
            "WHERE mt5_ticket=?",
            (datetime.now(timezone.utc).isoformat(), body.reason, ticket),
        )
        return {"ok": True}

    return app


def run() -> None:
    import uvicorn
    from src import config
    from src.db import connect, init_schema
    conn = connect(config.DB_PATH)
    init_schema(conn)
    app = build_app(conn)
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)


if __name__ == "__main__":
    run()
```

- [ ] **Step 4: Run tests, verify pass**

```bash
pytest tests/test_api.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/api.py tests/test_api.py
git commit -m "feat(api): FastAPI bridge for MT5 (actions + position lifecycle)"
```

---

## Task 11: Telegram bot — notification rendering

**Files:**
- Create: `src/telegram_format.py`, `tests/test_telegram_format.py`

- [ ] **Step 1: Write failing tests**

In `tests/test_telegram_format.py`:

```python
from src.telegram_format import render_action_notification


def test_render_open_notification():
    payload = {
        "symbol": "XAUUSD", "side": "BUY",
        "entry_low": 4864, "entry_high": 4866,
        "tps": [4880, 4900, 4920], "sl": 4855,
    }
    text = render_action_notification(
        action_id=87, action_type="OPEN", payload=payload,
        source_text="GOLD BUY @ 4866-4864", auto_execute_delay_sec=30,
    )
    assert "#87" in text
    assert "BUY" in text
    assert "XAUUSD" in text
    assert "4864" in text and "4866" in text
    assert "30s" in text


def test_render_close_all_notification():
    payload = {"symbol": "XAUUSD", "reason": "trader_emergency_exit"}
    text = render_action_notification(88, "CLOSE_ALL", payload, "Close all", 30)
    assert "CLOSE_ALL" in text
    assert "XAUUSD" in text


def test_render_alert_no_buttons_implied():
    payload = {"level": "warning", "text": "NFP coming"}
    text = render_action_notification(89, "ALERT", payload, "be careful", 0)
    assert "ALERT" in text
    assert "NFP" in text
```

- [ ] **Step 2: Run, confirm failure**

```bash
pytest tests/test_telegram_format.py -v
```

- [ ] **Step 3: Implement `src/telegram_format.py`**

```python
def render_action_notification(
    action_id: int, action_type: str, payload: dict,
    source_text: str, auto_execute_delay_sec: int,
) -> str:
    if action_type == "OPEN":
        tps = " / ".join(str(t) for t in payload.get("tps", []))
        return (
            f"🟢 NEW SIGNAL #{action_id}  (auto-execute in {auto_execute_delay_sec}s)\n\n"
            f"OPEN {payload['side']} {payload['symbol']}\n"
            f"Entry: {payload['entry_low']}–{payload['entry_high']}\n"
            f"SL:    {payload['sl']}\n"
            f"TPs:   {tps}\n\n"
            f"Source: \"{source_text[:120]}\""
        )
    if action_type == "MODIFY":
        return (
            f"🔧 MODIFY #{action_id}  (auto in {auto_execute_delay_sec}s)\n"
            f"Ticket: {payload['mt5_ticket']}\n"
            f"new_sl: {payload.get('new_sl')}  new_tp: {payload.get('new_tp')}\n\n"
            f"Source: \"{source_text[:120]}\""
        )
    if action_type == "CLOSE":
        return (
            f"🔴 CLOSE #{action_id}  (auto in {auto_execute_delay_sec}s)\n"
            f"Ticket: {payload['mt5_ticket']}\n"
            f"Reason: {payload.get('reason','')}\n\n"
            f"Source: \"{source_text[:120]}\""
        )
    if action_type == "CLOSE_ALL":
        return (
            f"🔴 CLOSE_ALL #{action_id}  (auto in {auto_execute_delay_sec}s)\n"
            f"Symbol: {payload['symbol']}\n"
            f"Reason: {payload.get('reason','')}\n\n"
            f"Source: \"{source_text[:120]}\""
        )
    if action_type == "ALERT":
        level = payload.get("level", "info").upper()
        return f"⚠️ ALERT [{level}] #{action_id}\n{payload.get('text','')}"
    return f"#{action_id} {action_type}: {payload}"
```

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/test_telegram_format.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/telegram_format.py tests/test_telegram_format.py
git commit -m "feat(bot): notification rendering"
```

---

## Task 12: Promotion worker

**Files:**
- Create: `src/promoter.py`, `tests/test_promoter.py`

The promoter promotes `pending → sent` once `execute_after` has passed AND kill switch is off. Runs as an async task inside `bot.py`.

- [ ] **Step 1: Write failing tests**

In `tests/test_promoter.py`:

```python
import json
from datetime import datetime, timedelta, timezone
from src.db import connect, init_schema, set_setting
from src.promoter import promote_due_actions


def _insert_pending(conn, execute_after, action_type="OPEN"):
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, execute_after) "
        "VALUES(?, ?, 'pending', ?)",
        (action_type, "{}", execute_after.isoformat() if execute_after else None),
    )
    return cur.lastrowid


def test_promotes_due_action(tmp_path):
    conn = connect(str(tmp_path / "p.db"))
    init_schema(conn)
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    aid = _insert_pending(conn, past)
    n = promote_due_actions(conn)
    assert n == 1
    assert conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()["status"] == "sent"


def test_does_not_promote_future_action(tmp_path):
    conn = connect(str(tmp_path / "p.db"))
    init_schema(conn)
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    aid = _insert_pending(conn, future)
    n = promote_due_actions(conn)
    assert n == 0
    assert conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()["status"] == "pending"


def test_does_not_promote_when_kill_switch_on(tmp_path):
    conn = connect(str(tmp_path / "p.db"))
    init_schema(conn)
    set_setting(conn, "kill_switch", "on")
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    _insert_pending(conn, past)
    assert promote_due_actions(conn) == 0


def test_skips_alerts(tmp_path):
    """ALERTs have no execute_after and should never be promoted to sent."""
    conn = connect(str(tmp_path / "p.db"))
    init_schema(conn)
    aid = _insert_pending(conn, None, action_type="ALERT")
    assert promote_due_actions(conn) == 0
    assert conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()["status"] == "pending"
```

- [ ] **Step 2: Run, confirm failure**

```bash
pytest tests/test_promoter.py -v
```

- [ ] **Step 3: Implement `src/promoter.py`**

```python
import sqlite3
from datetime import datetime, timezone
from src.db import get_setting


def promote_due_actions(conn: sqlite3.Connection) -> int:
    """Promote pending actions whose execute_after has passed. Returns count promoted."""
    if get_setting(conn, "kill_switch") == "on":
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE actions SET status='sent' "
        "WHERE status='pending' "
        "AND execute_after IS NOT NULL "
        "AND execute_after <= ? "
        "AND action_type IN ('OPEN','MODIFY','CLOSE','CLOSE_ALL')",
        (now_iso,),
    )
    return cur.rowcount
```

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/test_promoter.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/promoter.py tests/test_promoter.py
git commit -m "feat(bot): promotion worker (pending → sent)"
```

---

## Task 13: Telegram bot entrypoint

**Files:**
- Create: `src/bot.py`

This wires together: command handlers, the notification dispatcher (polls for new actions and DMs the owner), and the promotion worker. Tests are integration-level (Task 16); manual smoke test here.

- [ ] **Step 1: Implement `src/bot.py`**

```python
import asyncio
import json
import logging
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)
from src import config
from src.db import connect, init_schema, get_setting, set_setting
from src.telegram_format import render_action_notification
from src.promoter import promote_due_actions

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [bot] %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _owner_only(user_id: int) -> bool:
    return user_id == config.TG_BOT_OWNER_USER_ID


def _kb_for_action(action_id: int, action_type: str) -> InlineKeyboardMarkup | None:
    if action_type == "ALERT":
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Cancel", callback_data=f"cancel:{action_id}"),
        InlineKeyboardButton("Execute now", callback_data=f"execute:{action_id}"),
    ]])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    await update.message.reply_text("CopyTrades bot ready.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    conn: sqlite3.Connection = ctx.application.bot_data["conn"]
    ks = get_setting(conn, "kill_switch")
    pend = conn.execute("SELECT COUNT(*) FROM actions WHERE status='pending'").fetchone()[0]
    sent = conn.execute("SELECT COUNT(*) FROM actions WHERE status='sent'").fetchone()[0]
    pos = conn.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
    await update.message.reply_text(
        f"kill_switch: {ks}\npending: {pend}\nsent: {sent}\nopen positions: {pos}"
    )


async def cmd_halt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    conn = ctx.application.bot_data["conn"]
    set_setting(conn, "kill_switch", "on")
    await update.message.reply_text("🛑 KILL SWITCH ON. Already-sent actions will still run.")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    conn = ctx.application.bot_data["conn"]
    set_setting(conn, "kill_switch", "off")
    await update.message.reply_text("✅ Kill switch OFF.")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /cancel <action_id>")
        return
    aid = int(ctx.args[0])
    await _do_cancel(ctx.application.bot_data["conn"], aid, update.message.reply_text)


async def cmd_execute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /execute <action_id>")
        return
    aid = int(ctx.args[0])
    await _do_execute(ctx.application.bot_data["conn"], aid, update.message.reply_text)


async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    conn = ctx.application.bot_data["conn"]
    rows = conn.execute(
        "SELECT mt5_ticket, side, symbol, volume, entry_price, sl, tp "
        "FROM positions WHERE status='open' ORDER BY id"
    ).fetchall()
    if not rows:
        await update.message.reply_text("No open positions.")
        return
    lines = ["Open positions:"]
    for r in rows:
        lines.append(
            f"  #{r['mt5_ticket']} {r['side']} {r['symbol']} vol={r['volume']:.2f} "
            f"entry={r['entry_price']} sl={r['sl']} tp={r['tp']}"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_closeall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    conn = ctx.application.bot_data["conn"]
    payload = json.dumps({"symbol": "XAUUSD", "reason": "manual_user_command"})
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, execute_after) "
        "VALUES('CLOSE_ALL', ?, 'pending', datetime('now'))",
        (payload,),
    )
    await update.message.reply_text(f"Queued CLOSE_ALL action #{cur.lastrowid}")


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    q = update.callback_query
    await q.answer()
    op, aid = q.data.split(":")
    aid = int(aid)
    conn = ctx.application.bot_data["conn"]
    if op == "cancel":
        await _do_cancel(conn, aid, lambda t: q.edit_message_text(t))
    elif op == "execute":
        await _do_execute(conn, aid, lambda t: q.edit_message_text(t))


async def _do_cancel(conn, aid, reply):
    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    if row is None:
        await reply(f"Action #{aid} not found.")
        return
    if row["status"] != "pending":
        await reply(f"Action #{aid} is {row['status']}, cannot cancel.")
        return
    conn.execute("UPDATE actions SET status='cancelled' WHERE id=?", (aid,))
    await reply(f"Cancelled action #{aid}.")


async def _do_execute(conn, aid, reply):
    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    if row is None:
        await reply(f"Action #{aid} not found.")
        return
    if row["status"] != "pending":
        await reply(f"Action #{aid} is {row['status']}, cannot execute.")
        return
    conn.execute("UPDATE actions SET status='sent' WHERE id=?", (aid,))
    await reply(f"Promoted action #{aid} to sent.")


async def notification_dispatcher(app: Application):
    """Polls for unnotified actions and DMs the owner."""
    conn: sqlite3.Connection = app.bot_data["conn"]
    while True:
        try:
            rows = conn.execute(
                "SELECT id, action_type, payload_json, source_msg_id "
                "FROM actions WHERE notified_at IS NULL "
                "AND status IN ('pending','rejected','cancelled') "
                "ORDER BY id ASC LIMIT 20"
            ).fetchall()
            for r in rows:
                payload = json.loads(r["payload_json"])
                src = ""
                if r["source_msg_id"]:
                    m = conn.execute(
                        "SELECT text FROM messages WHERE id=?", (r["source_msg_id"],)
                    ).fetchone()
                    if m:
                        src = m["text"]
                delay = int(get_setting(conn, "auto_execute_delay_sec") or "30")
                text = render_action_notification(
                    r["id"], r["action_type"], payload, src, delay
                )
                kb = _kb_for_action(r["id"], r["action_type"])
                await app.bot.send_message(
                    chat_id=config.TG_BOT_OWNER_USER_ID,
                    text=text,
                    reply_markup=kb,
                )
                conn.execute(
                    "UPDATE actions SET notified_at=CURRENT_TIMESTAMP WHERE id=?",
                    (r["id"],),
                )
        except Exception as e:
            log.exception("notification_dispatcher error: %s", e)
        await asyncio.sleep(1.0)


async def promotion_loop(app: Application):
    conn: sqlite3.Connection = app.bot_data["conn"]
    while True:
        try:
            n = promote_due_actions(conn)
            if n:
                log.info("promoted %s actions", n)
        except Exception as e:
            log.exception("promotion_loop error: %s", e)
        await asyncio.sleep(1.0)


async def post_init(app: Application):
    asyncio.create_task(notification_dispatcher(app))
    asyncio.create_task(promotion_loop(app))


def main() -> None:
    conn = connect(config.DB_PATH)
    init_schema(conn)
    app = Application.builder().token(config.TG_BOT_TOKEN).post_init(post_init).build()
    app.bot_data["conn"] = conn
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("halt", cmd_halt))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("execute", cmd_execute))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("closeall", cmd_closeall))
    app.add_handler(CallbackQueryHandler(on_button))
    app.run_polling()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test the import**

```bash
python -c "import src.bot; print('ok')"
```

- [ ] **Step 3: Commit**

```bash
git add src/bot.py
git commit -m "feat(bot): Telegram bot with commands, dispatcher, promotion loop"
```

---

## Task 14: MT5 Expert Advisor — `CopyTrades.mq5`

**Files:**
- Create: `ea/CopyTrades.mq5`

The EA polls the FastAPI bridge every second. For each `sent` action, it applies risk rules, executes via MT5 trade functions, and POSTs the result. It also reconciles closed positions.

**MT5 setup notes (paste into README later):**
- In MT5: Tools → Options → Expert Advisors → "Allow WebRequest for listed URL", add `http://127.0.0.1:8765`.
- Drop the `.mq5` file in `<MT5 dir>/MQL5/Experts/`, F7 to compile, attach to any chart.

- [ ] **Step 1: Implement `ea/CopyTrades.mq5`**

```mql5
//+------------------------------------------------------------------+
//|  CopyTrades.mq5 — polls FastAPI bridge for actions, executes     |
//+------------------------------------------------------------------+
#property strict
#include <Trade\Trade.mqh>

input string ApiBaseUrl              = "http://127.0.0.1:8765";
input int    PollIntervalSec         = 1;
input double RiskPercentPerTrade     = 1.0;
input double MaxLotsPerSignal        = 0.50;
input int    MaxOpenPositions        = 3;
input int    EntryZoneMode           = 1;   // 0=midpoint limit, 1=market if in zone
input int    TPMode                  = 1;   // 0=single TP1, 1=split per TP
input int    SlippagePoints          = 50;
input string Symbol_Override         = "XAUUSD";

CTrade trade;

int OnInit() {
   trade.SetExpertMagicNumber(919191);
   trade.SetDeviationInPoints(SlippagePoints);
   EventSetTimer(PollIntervalSec);
   Print("CopyTrades EA started. API=", ApiBaseUrl);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) {
   EventKillTimer();
}

void OnTimer() {
   if(KillSwitchOn()) return;
   PollAndExecute();
   ReconcileClosedPositions();
}

bool KillSwitchOn() {
   string body;
   if(!HttpGet(ApiBaseUrl + "/settings/kill_switch", body)) return false;
   return StringFind(body, "\"value\":\"on\"") >= 0;
}

void PollAndExecute() {
   string body;
   if(!HttpGet(ApiBaseUrl + "/actions?status=sent", body)) return;
   // Minimal JSON parse: find action objects
   ProcessActionsJson(body);
}

// ---- HTTP helpers ----
bool HttpGet(string url, string &outBody) {
   char post[]; char result[]; string headers;
   int res = WebRequest("GET", url, "", "", 5000, post, 0, result, headers);
   if(res == -1) {
      Print("WebRequest GET error ", GetLastError(), " url=", url);
      return false;
   }
   outBody = CharArrayToString(result);
   return true;
}

bool HttpPostJson(string url, string jsonBody, string &outBody) {
   char post[]; char result[]; string headers = "Content-Type: application/json\r\n";
   StringToCharArray(jsonBody, post, 0, StringLen(jsonBody));
   ArrayResize(post, StringLen(jsonBody));
   int res = WebRequest("POST", url, headers, "", 5000, post, ArraySize(post), result, headers);
   if(res == -1) {
      Print("WebRequest POST error ", GetLastError(), " url=", url);
      return false;
   }
   outBody = CharArrayToString(result);
   return true;
}

// ---- Lightweight JSON helpers (copytrades only emits the fields below) ----
string JsonField(string s, string key) {
   string pat = "\"" + key + "\":";
   int p = StringFind(s, pat);
   if(p < 0) return "";
   p += StringLen(pat);
   while(p < StringLen(s) && (StringGetCharacter(s, p) == ' ')) p++;
   if(p >= StringLen(s)) return "";
   ushort c = StringGetCharacter(s, p);
   if(c == '"') {
      int end = StringFind(s, "\"", p + 1);
      return StringSubstr(s, p + 1, end - p - 1);
   }
   int end = p;
   while(end < StringLen(s)) {
      ushort cc = StringGetCharacter(s, end);
      if(cc == ',' || cc == '}' || cc == ']') break;
      end++;
   }
   return StringSubstr(s, p, end - p);
}

// ---- Action processing ----
void ProcessActionsJson(string body) {
   // body looks like: {"actions":[ {...}, {...} ]}
   int pos = 0;
   while(true) {
      int objStart = StringFind(body, "{\"id\":", pos);
      if(objStart < 0) break;
      int depth = 0;
      int objEnd = -1;
      for(int i = objStart; i < StringLen(body); i++) {
         ushort c = StringGetCharacter(body, i);
         if(c == '{') depth++;
         else if(c == '}') { depth--; if(depth == 0) { objEnd = i; break; } }
      }
      if(objEnd < 0) break;
      string obj = StringSubstr(body, objStart, objEnd - objStart + 1);
      pos = objEnd + 1;
      ExecuteOne(obj);
   }
}

void ExecuteOne(string obj) {
   long id = StringToInteger(JsonField(obj, "id"));
   string atype = JsonField(obj, "action_type");
   string payload = ExtractPayload(obj);
   if(id <= 0 || atype == "") return;

   if(CountOurOpenPositions() >= MaxOpenPositions && atype == "OPEN") {
      PostResult(id, "rejected", 0, "max_positions");
      return;
   }

   if(atype == "OPEN")        DoOpen(id, payload);
   else if(atype == "MODIFY") DoModify(id, payload);
   else if(atype == "CLOSE")  DoClose(id, payload);
   else if(atype == "CLOSE_ALL") DoCloseAll(id, payload);
}

string ExtractPayload(string obj) {
   int p = StringFind(obj, "\"payload\":");
   if(p < 0) return "";
   p += StringLen("\"payload\":");
   int depth = 0;
   int start = -1, end = -1;
   for(int i = p; i < StringLen(obj); i++) {
      ushort c = StringGetCharacter(obj, i);
      if(c == '{') { if(depth == 0) start = i; depth++; }
      else if(c == '}') { depth--; if(depth == 0) { end = i; break; } }
   }
   if(start < 0 || end < 0) return "";
   return StringSubstr(obj, start, end - start + 1);
}

double LotsFromRisk(double slPrice, double entryPrice) {
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskCash = equity * (RiskPercentPerTrade / 100.0);
   double pip = SymbolInfoDouble(Symbol_Override, SYMBOL_TRADE_TICK_SIZE);
   double tickValue = SymbolInfoDouble(Symbol_Override, SYMBOL_TRADE_TICK_VALUE);
   if(pip <= 0 || tickValue <= 0) return 0.01;
   double dist = MathAbs(entryPrice - slPrice);
   double ticks = dist / pip;
   if(ticks <= 0) return 0.01;
   double lots = riskCash / (ticks * tickValue);
   double lotStep = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_STEP);
   lots = MathFloor(lots / lotStep) * lotStep;
   if(lots > MaxLotsPerSignal) lots = MaxLotsPerSignal;
   double minLot = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_MIN);
   if(lots < minLot) lots = minLot;
   return NormalizeDouble(lots, 2);
}

void DoOpen(long id, string payload) {
   string side = JsonField(payload, "side");
   double entryLow = StringToDouble(JsonField(payload, "entry_low"));
   double entryHigh = StringToDouble(JsonField(payload, "entry_high"));
   double sl = StringToDouble(JsonField(payload, "sl"));
   string tpsStr = JsonField(payload, "tps");
   double tps[];
   ParseTps(tpsStr, tps);
   if(ArraySize(tps) == 0) { PostResult(id, "failed", 0, "no_tps"); return; }

   double entry = (entryLow + entryHigh) / 2.0;
   double price = SymbolInfoDouble(Symbol_Override, side == "BUY" ? SYMBOL_ASK : SYMBOL_BID);
   bool inZone = (price >= entryLow && price <= entryHigh);

   ENUM_ORDER_TYPE type;
   bool useMarket = (EntryZoneMode == 1 && inZone);
   if(useMarket) {
      type = (side == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      entry = price;
   } else {
      type = (side == "BUY") ? ORDER_TYPE_BUY_LIMIT : ORDER_TYPE_SELL_LIMIT;
      entry = (entryLow + entryHigh) / 2.0;
   }

   long lastTicket = 0;
   string lastErr = "";
   int n = (TPMode == 1) ? ArraySize(tps) : 1;
   double lotsTotal = LotsFromRisk(sl, entry);
   double lotsEach = NormalizeDouble(lotsTotal / n, 2);
   double minLot = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_MIN);
   if(lotsEach < minLot) lotsEach = minLot;

   for(int i = 0; i < n; i++) {
      double tp = tps[i];
      bool ok;
      if(useMarket) {
         ok = (side == "BUY")
            ? trade.Buy(lotsEach, Symbol_Override, 0, sl, tp, "copytrades")
            : trade.Sell(lotsEach, Symbol_Override, 0, sl, tp, "copytrades");
      } else {
         ok = (side == "BUY")
            ? trade.BuyLimit(lotsEach, entry, Symbol_Override, sl, tp, ORDER_TIME_GTC, 0, "copytrades")
            : trade.SellLimit(lotsEach, entry, Symbol_Override, sl, tp, ORDER_TIME_GTC, 0, "copytrades");
      }
      if(!ok) { lastErr = "trade.send failed: " + IntegerToString(trade.ResultRetcode()); continue; }
      lastTicket = (long)trade.ResultOrder() != 0 ? (long)trade.ResultOrder() : (long)trade.ResultDeal();
      // Snapshot back to API
      string snap = StringFormat(
         "{\"status\":\"executed\",\"mt5_ticket\":%I64d,"
         "\"snapshot\":{\"symbol\":\"%s\",\"side\":\"%s\",\"volume\":%.2f,"
         "\"entry_price\":%.2f,\"sl\":%.2f,\"tp\":%.2f}}",
         lastTicket, Symbol_Override, side, lotsEach, entry, sl, tp
      );
      string resp;
      HttpPostJson(ApiBaseUrl + "/actions/" + IntegerToString(id) + "/result", snap, resp);
   }
   if(lastTicket == 0) PostResult(id, "failed", 0, lastErr);
}

void DoModify(long id, string payload) {
   long ticket = StringToInteger(JsonField(payload, "mt5_ticket"));
   double newSl = StringToDouble(JsonField(payload, "new_sl"));
   double newTp = StringToDouble(JsonField(payload, "new_tp"));
   if(!PositionSelectByTicket(ticket)) { PostResult(id, "failed", ticket, "no_position"); return; }
   double curSl = PositionGetDouble(POSITION_SL);
   double curTp = PositionGetDouble(POSITION_TP);
   if(newSl == 0) newSl = curSl;
   if(newTp == 0) newTp = curTp;
   if(trade.PositionModify(ticket, newSl, newTp))
      PostResult(id, "executed", ticket, "");
   else
      PostResult(id, "failed", ticket, "modify_failed:" + IntegerToString(trade.ResultRetcode()));
}

void DoClose(long id, string payload) {
   long ticket = StringToInteger(JsonField(payload, "mt5_ticket"));
   if(trade.PositionClose(ticket)) {
      PostResult(id, "executed", ticket, "");
      string body;
      HttpPostJson(ApiBaseUrl + "/positions/" + IntegerToString(ticket) + "/close",
                   "{\"reason\":\"ai_close\"}", body);
   } else {
      PostResult(id, "failed", ticket, "close_failed:" + IntegerToString(trade.ResultRetcode()));
   }
}

void DoCloseAll(long id, string payload) {
   string sym = JsonField(payload, "symbol");
   int closed = 0, failed = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--) {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != sym) continue;
      if(trade.PositionClose(t)) {
         closed++;
         string body;
         HttpPostJson(ApiBaseUrl + "/positions/" + IntegerToString(t) + "/close",
                      "{\"reason\":\"close_all\"}", body);
      } else failed++;
   }
   PostResult(id, "executed", 0, StringFormat("closed=%d failed=%d", closed, failed));
}

int CountOurOpenPositions() {
   int n = 0;
   for(int i = 0; i < PositionsTotal(); i++) {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) == 919191) n++;
   }
   return n;
}

void ReconcileClosedPositions() {
   // For each ticket the API thinks is open, if MT5 has no such open position, POST close.
   string body;
   if(!HttpGet(ApiBaseUrl + "/actions?status=executed&limit=200", body)) return;
   // (Simpler reconciliation: iterate trade history of last hour and POST closes for any
   //  matching MagicNumber that closed.) We'll do: walk PositionsTotal — anything with our
   //  magic stays open; rely on /positions/{ticket}/close being POSTed at close time by
   //  history scanning.
   datetime since = TimeCurrent() - 3600;
   HistorySelect(since, TimeCurrent());
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++) {
      ulong dealTicket = HistoryDealGetTicket(i);
      if(dealTicket == 0) continue;
      if(HistoryDealGetInteger(dealTicket, DEAL_MAGIC) != 919191) continue;
      if(HistoryDealGetInteger(dealTicket, DEAL_ENTRY) != DEAL_ENTRY_OUT) continue;
      ulong posId = HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID);
      string url = ApiBaseUrl + "/positions/" + IntegerToString(posId) + "/close";
      string resp;
      HttpPostJson(url, "{\"reason\":\"mt5_close\"}", resp);
   }
}

void ParseTps(string tpsStr, double &out[]) {
   ArrayResize(out, 0);
   string s = tpsStr;
   StringReplace(s, "[", ""); StringReplace(s, "]", "");
   string parts[]; int n = StringSplit(s, ',', parts);
   for(int i = 0; i < n; i++) {
      double v = StringToDouble(parts[i]);
      if(v > 0) { ArrayResize(out, ArraySize(out) + 1); out[ArraySize(out) - 1] = v; }
   }
}

void PostResult(long id, string status, long ticket, string err) {
   string body = "{\"status\":\"" + status + "\"";
   if(ticket > 0) body += ",\"mt5_ticket\":" + IntegerToString(ticket);
   if(err != "") {
      string esc = err; StringReplace(esc, "\"", "'");
      body += ",\"error\":\"" + esc + "\"";
   }
   body += "}";
   string resp;
   HttpPostJson(ApiBaseUrl + "/actions/" + IntegerToString(id) + "/result", body, resp);
}
```

- [ ] **Step 2: Commit**

```bash
git add ea/CopyTrades.mq5
git commit -m "feat(ea): MQL5 EA polling FastAPI bridge"
```

---

## Task 15: Replay test framework

**Files:**
- Create: `fixtures/messages.jsonl`, `tests/test_replay.py`

This is the most important test layer. The fixture file holds real (or realistic) channel messages with expected actions. The test replays each through the AI prompt builder and asserts.

- [ ] **Step 1: Seed `fixtures/messages.jsonl`**

Each line is a JSON object: `{id, prior_chat (list of strings), open_positions_block, new_message, expected_action_types, must_contain_fields}`.

```jsonl
{"id":"clean_buy","prior_chat":[],"open_positions_block":"OPEN POSITIONS:\n  (none)","new_message":"FX Yusuf: GOLD BUY @ 4866 - 4864, TP1 4880, TP2 4900, TP3 4920, SL 4855","expected_action_types":["OPEN"],"must_contain":{"OPEN":{"side":"BUY","sl":4855}}}
{"id":"commentary_only","prior_chat":[],"open_positions_block":"OPEN POSITIONS:\n  (none)","new_message":"FX Yusuf: gold looking strong above 4860, watching for breakout","expected_action_types":[],"must_contain":{}}
{"id":"move_sl_to_be","prior_chat":["[14:35] FX Yusuf: GOLD BUY @ 4865, SL 4855, TP 4880"],"open_positions_block":"OPEN POSITIONS:\n  Signal #42:\n    ticket=99001 BUY XAUUSD vol=0.10 entry=4865.00 sl=4855.00 tp=4880.00","new_message":"FX Yusuf: TP1 hit, move SL to BE","expected_action_types":["MODIFY"],"must_contain":{"MODIFY":{"mt5_ticket":99001,"new_sl":4865}}}
{"id":"close_all","prior_chat":[],"open_positions_block":"OPEN POSITIONS:\n  Signal #42:\n    ticket=99001 BUY XAUUSD vol=0.10 entry=4865.00 sl=4855.00 tp=4880.00","new_message":"FX Yusuf: close all gold, NFP coming","expected_action_types":["CLOSE_ALL"],"must_contain":{"CLOSE_ALL":{"symbol":"XAUUSD"}}}
{"id":"news_warning","prior_chat":[],"open_positions_block":"OPEN POSITIONS:\n  (none)","new_message":"FX Yusuf: NFP in 30 min, careful","expected_action_types":["ALERT"],"must_contain":{}}
{"id":"non_gold_signal","prior_chat":[],"open_positions_block":"OPEN POSITIONS:\n  (none)","new_message":"FX Yusuf: BUY EURUSD @ 1.0850 SL 1.0800 TP 1.0900","expected_action_types":["ALERT"],"must_contain":{}}
```

(Add more from real channel history later — aim for 30+.)

- [ ] **Step 2: Write the replay test**

In `tests/test_replay.py`:

```python
import json
import os
import pytest
from pathlib import Path
from src.ai import AIClient

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "messages.jsonl"

pytestmark = pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="No ANTHROPIC_API_KEY set; skipping live AI tests."
)


def _load_fixtures():
    with open(FIXTURE_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.mark.parametrize("fx", _load_fixtures(), ids=lambda fx: fx["id"])
def test_replay(fx):
    client = AIClient()
    recent = "\n".join(fx["prior_chat"])
    res = client.call(recent, fx["open_positions_block"], fx["new_message"])
    actual_types = sorted(a.type for a in res.response.actions)
    expected_types = sorted(fx["expected_action_types"])
    assert actual_types == expected_types, (
        f"types mismatch in {fx['id']}: got {actual_types}, expected {expected_types}\n"
        f"reasoning: {res.response.reasoning}"
    )
    for atype, must in fx.get("must_contain", {}).items():
        match = next((a for a in res.response.actions if a.type == atype), None)
        assert match is not None, f"{atype} missing in {fx['id']}"
        for k, v in must.items():
            actual_v = getattr(match, k)
            assert actual_v == v, (
                f"{fx['id']} {atype}.{k}: got {actual_v}, expected {v}"
            )
```

- [ ] **Step 3: Run tests with API key set (this WILL hit Anthropic API)**

```bash
ANTHROPIC_API_KEY=sk-... pytest tests/test_replay.py -v
```
Expected: 6 passed (or some failing, in which case iterate on `SYSTEM_PROMPT` until they pass).

- [ ] **Step 4: Commit**

```bash
git add fixtures/messages.jsonl tests/test_replay.py
git commit -m "test(ai): replay test framework with seed fixtures"
```

---

## Task 16: End-to-end integration test (mock EA)

**Files:**
- Create: `tests/test_integration.py`

- [ ] **Step 1: Write integration test**

In `tests/test_integration.py`:

```python
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from src.db import connect, init_schema
from src.api import build_app
from src.orchestrator import process_message
from src.promoter import promote_due_actions
from src.ai import AICallResult
from src.validators import AIResponse, OpenAction


def _ai_returning(actions):
    client = MagicMock()
    client.call.return_value = AICallResult(
        response=AIResponse(actions=actions, reasoning=""),
        raw_text="{}", usage={"input_tokens":1,"output_tokens":1,
                              "cache_read_tokens":0,"cache_creation_tokens":0},
        latency_ms=1,
    )
    return client


def test_full_pipeline_open_to_executed(tmp_path):
    conn = connect(str(tmp_path / "i.db"))
    init_schema(conn)
    ai = _ai_returning([OpenAction(symbol="XAUUSD", side="BUY",
                                   entry_low=4864, entry_high=4866,
                                   tps=[4880], sl=4855)])

    # 1. Listener processes a Telegram message
    ids = process_message(conn, ai, tg_message_id=1, chat_id=42,
                          sender="Yusuf", text="BUY GOLD",
                          ai_log_path=tmp_path / "ai.jsonl",
                          auto_execute_delay_sec=0)
    assert len(ids) == 1
    aid = ids[0]
    # execute_after is now (delay=0), so promotion should run
    n = promote_due_actions(conn)
    assert n == 1

    # 2. EA polls API
    app = build_app(conn)
    client = TestClient(app)
    r = client.get("/actions?status=sent")
    actions = r.json()["actions"]
    assert len(actions) == 1
    assert actions[0]["id"] == aid

    # 3. EA reports executed
    r = client.post(f"/actions/{aid}/result", json={
        "status": "executed",
        "mt5_ticket": 12345,
        "snapshot": {"symbol":"XAUUSD","side":"BUY","volume":0.10,
                     "entry_price":4865.0,"sl":4855.0,"tp":4880.0},
    })
    assert r.status_code == 200

    # 4. State settles
    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "executed"
    pos = conn.execute("SELECT * FROM positions WHERE mt5_ticket=12345").fetchone()
    assert pos["status"] == "open"

    # 5. EA reconciles — position closed in MT5
    r = client.post("/positions/12345/close", json={"reason": "tp"})
    assert r.status_code == 200
    pos = conn.execute("SELECT * FROM positions WHERE mt5_ticket=12345").fetchone()
    assert pos["status"] == "closed"
    assert pos["close_reason"] == "tp"


def test_kill_switch_blocks_promotion(tmp_path):
    conn = connect(str(tmp_path / "k.db"))
    init_schema(conn)
    from src.db import set_setting
    set_setting(conn, "kill_switch", "on")
    ai = _ai_returning([OpenAction(symbol="XAUUSD", side="BUY",
                                   entry_low=4864, entry_high=4866,
                                   tps=[4880], sl=4855)])
    process_message(conn, ai, 1, 42, "Y", "BUY", tmp_path / "a.jsonl", 0)
    assert promote_due_actions(conn) == 0
    set_setting(conn, "kill_switch", "off")
    assert promote_due_actions(conn) == 1
```

- [ ] **Step 2: Run, verify pass**

```bash
pytest tests/test_integration.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration.py
git commit -m "test(integration): end-to-end pipeline with mock EA"
```

---

## Task 17: README and operational notes

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write `README.md`**

````markdown
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
````

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README with setup, run, and ops"
```

---

## Task 18: Final smoke test

- [ ] **Step 1: Full test suite (no live AI)**

```bash
pytest -v
```
Expected: all tests pass except `test_replay.py` skipped (no API key in CI).

- [ ] **Step 2: Manual end-to-end on a demo account**

This is for the operator, not an automated step:
1. Start `api.py`, `bot.py`, `listener.py`.
2. Compile and attach `CopyTrades.mq5` to a chart in MT5 (demo broker).
3. Wait for a real signal in the channel.
4. Verify: bot DMs you → notification has correct entry/SL/TPs → after 30s, EA executes → `/status` shows the position.
5. If the trader posts "move SL to BE" → AI emits MODIFY → bot DMs → EA modifies.
6. Test `/halt` mid-signal: cancel an in-flight `pending` to verify the kill switch path.

- [ ] **Step 3: Commit any small fixes from smoke testing**

---

## Self-review

Spec coverage check:
- ✅ Telethon listener (Task 9)
- ✅ AI with prompt caching (Task 7)
- ✅ Pydantic validators + business rules (Tasks 3, 4)
- ✅ State summary builder (Task 5)
- ✅ Orchestrator pipeline (Task 8)
- ✅ FastAPI bridge (Task 10)
- ✅ Bot with all commands + promotion + dispatcher (Tasks 11, 12, 13)
- ✅ MQL5 EA (Task 14)
- ✅ Replay tests (Task 15)
- ✅ Integration test (Task 16)
- ✅ README with setup (Task 17)
- ✅ Backfill alert (Task 9)
- ✅ Kill switch (Tasks 12, 13)
- ✅ Defense-in-depth (validators, kill switch checked twice, EA risk caps)

Open items deferred to operations (not implementation):
- Real fixture corpus (Task 15 has 6 seed examples; the operator adds more).
- Service supervision (NSSM/Task Scheduler) — covered briefly in README.
