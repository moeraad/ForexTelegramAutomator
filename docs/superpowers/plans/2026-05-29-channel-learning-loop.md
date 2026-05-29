# Channel Learning Loop (CLL) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a per-channel self-distillation loop that captures every interpreter verdict, periodically clusters + synthesizes them into proposed cheap-layer rules with replay evidence, and lets the operator accept proposals into the existing `profile.json` — cutting expensive `interpret` calls without losing accuracy.

**Architecture:** Purely additive to the live pipeline. A capture hook in `orchestrator.process_message` appends `(message, category, action_types, triage_decision)` to a new `learning_samples` table. A rolling bot-process loop runs `channel_learner` once a channel accumulates N new samples; the learner embeds (OpenAI `text-embedding-3-small`), greedily clusters by category + cosine, synthesizes one rule per dense cluster (cheap LLM), replays it over the corpus for evidence, and writes `learning_suggestions`. A GUI tab surfaces ranked, evidence-gated suggestions; Accept is the only writer into `profile.json` fields the live layers already read. Nothing auto-activates.

**Tech Stack:** Python 3.11, SQLite (WAL, via `src/db.py`), `python-telegram-bot` async loops (`src/bot_loops/`), the existing `src/llm_provider.py` (`triage`, `_embed_batch`, `_cosine`), `pytest` (hermetic). Spec: `docs/superpowers/specs/2026-05-29-channel-learning-loop-design.md`.

---

## Shared interfaces (defined here; referenced by later tasks)

These signatures are the contract across modules. Keep names exact.

```python
# src/learning_store.py
@dataclass(frozen=True)
class Sample:
    id: int
    source_channel_id: str
    text: str
    norm_text: str
    category: str            # 'ignore'|'context'|'signal'|'partial_signal'|'alert'
    action_types: str        # csv of emitted action types, '' when none
    triage_decision: str     # 'keep'|'ignore'|'' (blank when triage didn't run)
    seen_count: int
    embedding: list[float] | None

def capture(conn, *, source_channel_id, route_id, source_msg_id,
            text, category, action_types, triage_decision) -> int | None: ...
def recent(conn, source_channel_id, limit) -> list[Sample]: ...
def without_embedding(conn, source_channel_id, limit) -> list[Sample]: ...
def set_embedding(conn, sample_id: int, vec: list[float]) -> None: ...
def count_since(conn, source_channel_id, since_id: int) -> int: ...
def max_id(conn, source_channel_id) -> int: ...

# src/suggestion_store.py
@dataclass(frozen=True)
class Suggestion:
    id: int
    source_channel_id: str
    rule_kind: str           # 'noise'|'action_trigger'|'keep_trigger'|'context_drop'|'profile'
    target_layer: str        # 'prefilter'|'triage'|'matcher'|'profile'
    payload: dict
    evidence: dict
    status: str              # 'proposed'|'accepted'|'dismissed'|'expired'
    created_at: str

def add(conn, *, source_channel_id, rule_kind, target_layer,
        payload: dict, evidence: dict) -> int | None: ...
def list_proposed(conn, source_channel_id: str | None = None) -> list[Suggestion]: ...
def get(conn, sid: int) -> Suggestion | None: ...
def set_status(conn, sid: int, status: str) -> bool: ...
def expire_old(conn, ttl_days: int) -> int: ...
def dedupe_key(rule_kind: str, payload: dict) -> str: ...

# src/channel_learner.py
@dataclass(frozen=True)
class LearnParams:
    min_cluster_size: int
    embed_threshold: float
    min_support: int
    min_purity: float

@dataclass(frozen=True)
class SuggestionDraft:
    rule_kind: str
    target_layer: str
    payload: dict
    evidence: dict

def learn(samples: list[Sample], *, synth_fn, params: LearnParams) -> list[SuggestionDraft]: ...
# synth_fn(cluster_texts: list[str], category: str, action_types_csv: str) -> dict
#   returns {'phrase': str, 'rationale': str}

# src/profile_writer.py
def apply_suggestion(profile_path: Path, suggestion: Suggestion) -> None: ...
```

`embedding` is stored as JSON text. Reuse `trigger_matcher._cosine` and `trigger_matcher._embed_batch` (do NOT reimplement cosine or the OpenAI call).

---

## Task 1: Config defaults for the learning loop

**Files:**
- Modify: `src/db_settings.py` (the `DEFAULT_SETTINGS` dict, around line 38)
- Test: `tests/test_learning_config.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_learning_config.py
"""Learning-loop config defaults are seeded and typed."""
from __future__ import annotations

from src.db import connect, init_schema
from src import db_settings


def test_learning_defaults_present_and_typed(tmp_path):
    db = tmp_path / "s.db"
    conn = connect(str(db))
    init_schema(conn)  # runs _migrate_seed_settings_defaults
    conn.close()

    assert db_settings.get_bool(db, "learning_enabled", False) is True
    assert db_settings.get_int(db, "learning_batch_n", 0) == 50
    assert db_settings.get_int(db, "learning_corpus_max", 0) == 2000
    assert db_settings.get_int(db, "learning_min_cluster_size", 0) == 4
    assert db_settings.get_float(db, "learning_embed_threshold", 0.0) == 0.82
    assert db_settings.get_int(db, "learning_min_support", 0) == 5
    assert db_settings.get_float(db, "learning_min_purity", 0.0) == 0.9
    assert db_settings.get_int(db, "learning_suggestion_ttl_days", 0) == 14
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_learning_config.py -v`
Expected: FAIL — getters return the passed defaults, not the seeded values (keys absent).

- [ ] **Step 3: Add the defaults**

In `src/db_settings.py`, add these entries inside the `DEFAULT_SETTINGS` dict (all values are strings — that is the table's storage type):

```python
    # --- Channel Learning Loop (CLL) ---
    "learning_enabled": "1",
    "learning_batch_n": "50",
    "learning_corpus_max": "2000",
    "learning_min_cluster_size": "4",
    "learning_embed_threshold": "0.82",
    "learning_min_support": "5",
    "learning_min_purity": "0.9",
    "learning_suggestion_ttl_days": "14",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_learning_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/db_settings.py tests/test_learning_config.py
git commit -m "feat(cll): seed learning-loop config defaults"
```

---

## Task 2: `learning_samples` table migration

**Files:**
- Modify: `src/db.py` (add migration fn + register it in `init_schema`)
- Test: `tests/test_learning_schema.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_learning_schema.py
"""learning_samples + learning_suggestions tables exist with the right shape."""
from __future__ import annotations

from src.db import connect, init_schema


def _cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_learning_samples_table_shape(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    cols = _cols(conn, "learning_samples")
    assert {
        "id", "source_channel_id", "route_id", "source_msg_id",
        "text", "norm_text", "category", "action_types",
        "triage_decision", "seen_count", "embedding_json",
        "created_at", "last_seen_at",
    } <= cols
    # uniqueness on (source_channel_id, norm_text)
    conn.execute(
        "INSERT INTO learning_samples(source_channel_id, norm_text, text, "
        "category, action_types, triage_decision, seen_count) "
        "VALUES('c','n','t','ignore','','keep',1)"
    )
    cur = conn.execute(
        "INSERT OR IGNORE INTO learning_samples(source_channel_id, norm_text, text, "
        "category, action_types, triage_decision, seen_count) "
        "VALUES('c','n','t2','ignore','','keep',1)"
    )
    assert cur.rowcount == 0  # duplicate (c,n) ignored
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_learning_schema.py -v`
Expected: FAIL — `no such table: learning_samples`.

- [ ] **Step 3: Add the migration**

In `src/db.py`, add the function (follow the existing `_migrate_create_unmatched_messages` style — `CREATE TABLE IF NOT EXISTS`, no table rebuild):

```python
def _migrate_create_learning_samples(conn: sqlite3.Connection) -> None:
    """Labeled corpus for the Channel Learning Loop.

    Captures every interpreted message + its category/action_types and the
    triage decision, keyed by source_channel_id. Superset of the old
    unmatched_messages queue (which only held emittable-action misses):
    this table also holds ignore/context rows, where the cost savings live.

    UNIQUE(source_channel_id, norm_text) collapses verbatim resends into
    one row; the capture path bumps seen_count/last_seen_at on conflict.
    embedding_json is filled lazily by the learner (NULL until then).
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS learning_samples ("
        "  id                INTEGER PRIMARY KEY,"
        "  source_channel_id TEXT NOT NULL,"
        "  route_id          TEXT,"
        "  source_msg_id     INTEGER,"
        "  text              TEXT NOT NULL,"
        "  norm_text         TEXT NOT NULL,"
        "  category          TEXT NOT NULL,"
        "  action_types      TEXT NOT NULL DEFAULT '',"
        "  triage_decision   TEXT NOT NULL DEFAULT '',"
        "  seen_count        INTEGER NOT NULL DEFAULT 1,"
        "  embedding_json    TEXT,"
        "  created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,"
        "  last_seen_at      DATETIME DEFAULT CURRENT_TIMESTAMP,"
        "  UNIQUE(source_channel_id, norm_text)"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_learning_samples_channel "
        "ON learning_samples(source_channel_id, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_learning_samples_needs_embed "
        "ON learning_samples(source_channel_id) WHERE embedding_json IS NULL"
    )
```

Register it at the end of `init_schema` (after `_migrate_messages_add_pipeline_columns(conn)`):

```python
    _migrate_create_learning_samples(conn)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_learning_schema.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_learning_schema.py
git commit -m "feat(cll): add learning_samples table + migration"
```

---

## Task 3: `learning_suggestions` table migration

**Files:**
- Modify: `src/db.py` (add migration fn + register it)
- Test: `tests/test_learning_schema.py` (extend)

- [ ] **Step 1: Write the failing test (append)**

```python
def test_learning_suggestions_table_shape(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    cols = _cols(conn, "learning_suggestions")
    assert {
        "id", "source_channel_id", "rule_kind", "target_layer",
        "payload_json", "evidence_json", "dedupe_key", "status",
        "created_at", "decided_at",
    } <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_learning_schema.py::test_learning_suggestions_table_shape -v`
Expected: FAIL — `no such table: learning_suggestions`.

- [ ] **Step 3: Add the migration**

In `src/db.py`:

```python
def _migrate_create_learning_suggestions(conn: sqlite3.Connection) -> None:
    """Proposed cheap-layer rules awaiting operator approval.

    payload_json is the concrete rule to write into profile.json on Accept;
    evidence_json holds the replay stats the GUI renders. dedupe_key is a
    stable hash of (rule_kind, normalized payload) so the same suggestion
    isn't re-proposed while still pending. Nothing here ever executes — the
    GUI Accept handler is the sole writer into profile.json.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS learning_suggestions ("
        "  id                INTEGER PRIMARY KEY,"
        "  source_channel_id TEXT NOT NULL,"
        "  rule_kind         TEXT NOT NULL,"
        "  target_layer      TEXT NOT NULL,"
        "  payload_json      TEXT NOT NULL,"
        "  evidence_json     TEXT NOT NULL,"
        "  dedupe_key        TEXT NOT NULL,"
        "  status            TEXT NOT NULL DEFAULT 'proposed'"
        "                    CHECK(status IN ('proposed','accepted','dismissed','expired')),"
        "  created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,"
        "  decided_at        DATETIME"
        ")"
    )
    # One live proposal per dedupe_key per channel.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_sugg_dedupe "
        "ON learning_suggestions(source_channel_id, dedupe_key) "
        "WHERE status='proposed'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_learning_sugg_proposed "
        "ON learning_suggestions(source_channel_id, status, created_at)"
    )
```

Register it in `init_schema` immediately after `_migrate_create_learning_samples(conn)`:

```python
    _migrate_create_learning_suggestions(conn)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_learning_schema.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_learning_schema.py
git commit -m "feat(cll): add learning_suggestions table + migration"
```

---

## Task 4: `learning_store` — capture, dedup, prune, query

**Files:**
- Create: `src/learning_store.py`
- Test: `tests/test_learning_store.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_learning_store.py
"""Tests for the labeled-corpus capture store."""
from __future__ import annotations

from src import learning_store
from src.db import connect, init_schema


def _setup(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    return conn


def test_capture_inserts_row(tmp_path):
    conn = _setup(tmp_path)
    sid = learning_store.capture(
        conn, source_channel_id="ch1", route_id="r1", source_msg_id=10,
        text="احجز نصف", category="signal", action_types="CLOSE_PARTIAL",
        triage_decision="keep",
    )
    assert sid is not None
    rows = learning_store.recent(conn, "ch1", 10)
    assert len(rows) == 1
    assert rows[0].category == "signal"
    assert rows[0].action_types == "CLOSE_PARTIAL"
    assert rows[0].seen_count == 1


def test_capture_dedups_and_bumps_seen_count(tmp_path):
    conn = _setup(tmp_path)
    learning_store.capture(
        conn, source_channel_id="ch1", route_id="", source_msg_id=1,
        text="صباح الخير", category="ignore", action_types="", triage_decision="keep")
    learning_store.capture(
        conn, source_channel_id="ch1", route_id="", source_msg_id=2,
        text="صباح الخير", category="ignore", action_types="", triage_decision="keep")
    rows = learning_store.recent(conn, "ch1", 10)
    assert len(rows) == 1
    assert rows[0].seen_count == 2


def test_capture_isolated_per_channel(tmp_path):
    conn = _setup(tmp_path)
    learning_store.capture(conn, source_channel_id="a", route_id="", source_msg_id=1,
                           text="x", category="ignore", action_types="", triage_decision="")
    learning_store.capture(conn, source_channel_id="b", route_id="", source_msg_id=2,
                           text="x", category="ignore", action_types="", triage_decision="")
    assert len(learning_store.recent(conn, "a", 10)) == 1
    assert len(learning_store.recent(conn, "b", 10)) == 1


def test_capture_blank_channel_is_noop(tmp_path):
    conn = _setup(tmp_path)
    assert learning_store.capture(
        conn, source_channel_id="", route_id="", source_msg_id=1,
        text="x", category="ignore", action_types="", triage_decision="") is None


def test_capture_never_raises_on_bad_conn():
    # A broken connection must be swallowed (live path safety).
    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db down")
    assert learning_store.capture(
        Boom(), source_channel_id="c", route_id="", source_msg_id=1,
        text="x", category="ignore", action_types="", triage_decision="") is None


def test_prune_keeps_newest(tmp_path):
    conn = _setup(tmp_path)
    for i in range(10):
        learning_store.capture(conn, source_channel_id="c", route_id="",
                               source_msg_id=i, text=f"m{i}", category="ignore",
                               action_types="", triage_decision="")
    learning_store.prune(conn, "c", keep=5)
    rows = learning_store.recent(conn, "c", 100)
    assert len(rows) == 5
    assert rows[0].text == "m9"  # newest first


def test_embedding_roundtrip(tmp_path):
    conn = _setup(tmp_path)
    sid = learning_store.capture(conn, source_channel_id="c", route_id="",
                                 source_msg_id=1, text="m", category="ignore",
                                 action_types="", triage_decision="")
    assert learning_store.without_embedding(conn, "c", 10)[0].id == sid
    learning_store.set_embedding(conn, sid, [0.1, 0.2, 0.3])
    assert learning_store.without_embedding(conn, "c", 10) == []
    assert learning_store.recent(conn, "c", 10)[0].embedding == [0.1, 0.2, 0.3]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_learning_store.py -v`
Expected: FAIL — `No module named 'src.learning_store'`.

- [ ] **Step 3: Write the implementation**

```python
# src/learning_store.py
"""Labeled-corpus capture for the Channel Learning Loop.

Every interpreted message is appended here with the interpreter's verdict
(category + emitted action types) and the triage decision. The batch
learner reads this corpus per channel to propose cheap-layer rules.

Capture MUST NEVER break the live trade path — every public function that
runs inline (capture) swallows exceptions and returns None on failure.
Supersedes src/unmatched_store.py.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from src.text_normalize import normalize

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sample:
    id: int
    source_channel_id: str
    text: str
    norm_text: str
    category: str
    action_types: str
    triage_decision: str
    seen_count: int
    embedding: list[float] | None


def _row_to_sample(r: sqlite3.Row) -> Sample:
    emb = None
    if r["embedding_json"]:
        try:
            emb = json.loads(r["embedding_json"])
        except (TypeError, ValueError):
            emb = None
    return Sample(
        id=r["id"], source_channel_id=r["source_channel_id"], text=r["text"],
        norm_text=r["norm_text"], category=r["category"],
        action_types=r["action_types"], triage_decision=r["triage_decision"],
        seen_count=r["seen_count"], embedding=emb,
    )


def capture(conn, *, source_channel_id, route_id, source_msg_id,
            text, category, action_types, triage_decision) -> int | None:
    """Append one labeled sample. Dedups on (source_channel_id, norm_text):
    a verbatim resend bumps seen_count + last_seen_at instead of inserting.

    Returns the row id on first insert, None on dedup / blank input / error.
    Exceptions are swallowed — the corpus is augmentation, never required.
    """
    if not source_channel_id or not text or not text.strip():
        return None
    try:
        norm = normalize(text)
        if not norm:
            return None
        cur = conn.execute(
            "INSERT INTO learning_samples("
            "  source_channel_id, route_id, source_msg_id, text, norm_text,"
            "  category, action_types, triage_decision, seen_count) "
            "VALUES(?,?,?,?,?,?,?,?,1) "
            "ON CONFLICT(source_channel_id, norm_text) DO UPDATE SET "
            "  seen_count = seen_count + 1, "
            "  last_seen_at = CURRENT_TIMESTAMP",
            (source_channel_id, route_id or None, source_msg_id, text, norm,
             category, action_types or "", triage_decision or ""),
        )
        # lastrowid is unreliable on the UPDATE branch; detect insert vs bump.
        if cur.rowcount == 1 and cur.lastrowid:
            row = conn.execute(
                "SELECT id FROM learning_samples "
                "WHERE source_channel_id=? AND norm_text=?",
                (source_channel_id, norm),
            ).fetchone()
            return int(row["id"]) if row else None
        return None
    except Exception as e:  # noqa: BLE001 — live-path safety
        log.warning("learning_store.capture failed: %s", e)
        return None


def recent(conn, source_channel_id, limit) -> list[Sample]:
    rows = conn.execute(
        "SELECT * FROM learning_samples WHERE source_channel_id=? "
        "ORDER BY id DESC LIMIT ?",
        (source_channel_id, limit),
    ).fetchall()
    return [_row_to_sample(r) for r in rows]


def without_embedding(conn, source_channel_id, limit) -> list[Sample]:
    rows = conn.execute(
        "SELECT * FROM learning_samples "
        "WHERE source_channel_id=? AND embedding_json IS NULL "
        "ORDER BY id DESC LIMIT ?",
        (source_channel_id, limit),
    ).fetchall()
    return [_row_to_sample(r) for r in rows]


def set_embedding(conn, sample_id: int, vec: list[float]) -> None:
    conn.execute(
        "UPDATE learning_samples SET embedding_json=? WHERE id=?",
        (json.dumps(vec), sample_id),
    )


def count_since(conn, source_channel_id, since_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM learning_samples "
        "WHERE source_channel_id=? AND id>?",
        (source_channel_id, since_id),
    ).fetchone()
    return int(row["n"]) if row else 0


def max_id(conn, source_channel_id) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(id),0) AS m FROM learning_samples "
        "WHERE source_channel_id=?",
        (source_channel_id,),
    ).fetchone()
    return int(row["m"]) if row else 0


def prune(conn, source_channel_id, keep: int) -> None:
    """Drop oldest rows beyond `keep` for one channel."""
    conn.execute(
        "DELETE FROM learning_samples WHERE source_channel_id=? AND id NOT IN ("
        "  SELECT id FROM learning_samples WHERE source_channel_id=? "
        "  ORDER BY id DESC LIMIT ?"
        ")",
        (source_channel_id, source_channel_id, keep),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_learning_store.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/learning_store.py tests/test_learning_store.py
git commit -m "feat(cll): learning_store capture/dedup/prune/query"
```

---

## Task 5: Capture hook in the orchestrator

**Files:**
- Modify: `src/orchestrator.py` (the interpret path, after `_record_pipeline_decision` for the AI verdict — around lines 621-647)
- Test: `tests/test_orchestrator_capture.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator_capture.py
"""The orchestrator appends a learning_samples row on the interpret path."""
from __future__ import annotations

from dataclasses import dataclass

from src import learning_store, orchestrator
from src.db import connect, init_schema


@dataclass
class _Resp:
    category: str
    actions: list


@dataclass
class _Result:
    response: _Resp
    raw_text: str = "{}"
    latency_ms: int = 1
    usage: dict = None

    def __post_init__(self):
        if self.usage is None:
            object.__setattr__(self, "usage", {})


class _FakeAI:
    def __init__(self, category, actions):
        self._r = _Result(_Resp(category, actions))

    def call(self, *a, **k):
        return self._r


def test_interpret_verdict_is_captured(tmp_path, monkeypatch):
    monkeypatch.setenv("COPYTRADES_DISABLE_EVALUATOR", "1")
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    ai = _FakeAI("ignore", [])  # noise that triage let through
    orchestrator.process_message(
        conn, ai, tg_message_id=1, chat_id=99, sender="x",
        text="مبروك على الارباح", ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=0, source_channel_id="chX", route_id="rX",
    )
    rows = learning_store.recent(conn, "chX", 10)
    assert len(rows) == 1
    assert rows[0].category == "ignore"
    assert rows[0].text == "مبروك على الارباح"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_orchestrator_capture.py -v`
Expected: FAIL — `learning_store.recent` returns `[]` (no capture yet).

- [ ] **Step 3: Add the capture hook**

In `src/orchestrator.py`, first hoist a triage-decision variable so the hook can record it. Find the triage block (around line 542) and capture the decision into an outer variable. Change:

```python
    if triage is not None:
        try:
            tri = triage.classify(
```

so that just before `if triage is not None:` you initialize:

```python
    triage_decision_for_capture = ""
```

and inside the `try`, right after `tri = triage.classify(...)` succeeds, set:

```python
            triage_decision_for_capture = tri.decision
```

Then, in the interpret path, immediately AFTER the existing
`trades.info("ai_decision ...")` call (around line 638-641) and BEFORE the
`signal_memory.record(...)` block, add:

```python
    # CLL capture: the interpreter verdict is ground-truth training data.
    # Swallow all errors — must never break the live path.
    try:
        learning_store.capture(
            conn,
            source_channel_id=source_channel_id,
            route_id=route_id,
            source_msg_id=msg_id,
            text=text,
            category=result.response.category or "ignore",
            action_types=",".join(
                _action_type(a) for a in result.response.actions
            ),
            triage_decision=triage_decision_for_capture,
        )
    except Exception:  # noqa: BLE001
        log.exception("CLL capture failed for msg_id=%s", msg_id)
```

Add the import at the top with the other `from src import ...` imports:

```python
from src import learning_store
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_orchestrator_capture.py -v`
Expected: PASS

- [ ] **Step 5: Run the full orchestrator suite to confirm no regression**

Run: `.venv\Scripts\python.exe -m pytest tests/test_orchestrator.py -q`
Expected: PASS (existing behavior unchanged — capture is additive)

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator.py tests/test_orchestrator_capture.py
git commit -m "feat(cll): capture interpreter verdict into learning_store"
```

---

## Task 6: Retire `unmatched_store` — migrate pending rows into the corpus

**Files:**
- Modify: `src/db.py` (one-time data migration copying `unmatched_messages` → `learning_samples`)
- Test: `tests/test_learning_unmatched_migration.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_learning_unmatched_migration.py
"""Pending unmatched_messages rows are folded into learning_samples once."""
from __future__ import annotations

from src.db import connect, init_schema, _migrate_unmatched_into_learning


def test_unmatched_rows_migrated(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    # Simulate a pre-CLL DB with queued unmatched rows lacking channel id.
    conn.execute(
        "INSERT INTO unmatched_messages(text, norm_text, suggested_action_type, "
        "source_msg_id) VALUES('احجز نصف','احجز نصف','CLOSE_PARTIAL',7)"
    )
    n = _migrate_unmatched_into_learning(conn, fallback_channel_id="legacy")
    assert n == 1
    rows = conn.execute(
        "SELECT category, action_types, source_channel_id FROM learning_samples"
    ).fetchall()
    assert rows[0]["action_types"] == "CLOSE_PARTIAL"
    assert rows[0]["category"] == "signal"
    assert rows[0]["source_channel_id"] == "legacy"
    # Idempotent: second run migrates nothing (dedup on norm_text).
    assert _migrate_unmatched_into_learning(conn, fallback_channel_id="legacy") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_learning_unmatched_migration.py -v`
Expected: FAIL — `_migrate_unmatched_into_learning` not defined.

- [ ] **Step 3: Add the migration helper**

In `src/db.py`:

```python
def _migrate_unmatched_into_learning(
    conn: sqlite3.Connection, fallback_channel_id: str = "legacy"
) -> int:
    """One-time fold of the old unmatched_messages queue into learning_samples.

    unmatched_messages only held emittable-action misses (suggested_action_type
    set), so each becomes a 'signal'-category learning sample carrying that
    action type. Dedups on (source_channel_id, norm_text) via INSERT OR IGNORE,
    so re-running is a no-op. Returns rows migrated. Safe when the source table
    is absent (returns 0).
    """
    try:
        rows = conn.execute(
            "SELECT text, norm_text, suggested_action_type, source_msg_id "
            "FROM unmatched_messages"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    migrated = 0
    for r in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO learning_samples("
            "  source_channel_id, source_msg_id, text, norm_text, "
            "  category, action_types, triage_decision) "
            "VALUES(?,?,?,?, 'signal', ?, '')",
            (fallback_channel_id, r["source_msg_id"], r["text"],
             r["norm_text"], r["suggested_action_type"] or ""),
        )
        migrated += cur.rowcount or 0
    return migrated
```

Register it in `init_schema`, AFTER `_migrate_create_learning_suggestions(conn)`:

```python
    _migrate_unmatched_into_learning(conn)
```

Note: `unmatched_store.py` and its table are left in place (read-only, no
longer written to by the orchestrator after Task 5 replaced its `record`
call path). A later cleanup PR removes the module once the GUI no longer
imports it (Task 11 repoints the GUI).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_learning_unmatched_migration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_learning_unmatched_migration.py
git commit -m "feat(cll): fold unmatched_messages into learning_samples"
```

---

## Task 7: `suggestion_store` — CRUD, dedupe, expiry

**Files:**
- Create: `src/suggestion_store.py`
- Test: `tests/test_suggestion_store.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_suggestion_store.py
from __future__ import annotations

from src import suggestion_store
from src.db import connect, init_schema


def _setup(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    return conn


def test_add_and_list(tmp_path):
    conn = _setup(tmp_path)
    sid = suggestion_store.add(
        conn, source_channel_id="c", rule_kind="noise", target_layer="triage",
        payload={"phrase": "مبروك"}, evidence={"would_suppress": 12})
    assert sid is not None
    items = suggestion_store.list_proposed(conn, "c")
    assert len(items) == 1
    assert items[0].rule_kind == "noise"
    assert items[0].payload == {"phrase": "مبروك"}
    assert items[0].evidence["would_suppress"] == 12


def test_add_dedups_same_pending(tmp_path):
    conn = _setup(tmp_path)
    suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                         target_layer="triage", payload={"phrase": "مبروك"},
                         evidence={})
    again = suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                                 target_layer="triage", payload={"phrase": "مبروك"},
                                 evidence={})
    assert again is None
    assert len(suggestion_store.list_proposed(conn, "c")) == 1


def test_set_status_frees_dedupe(tmp_path):
    conn = _setup(tmp_path)
    sid = suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                              target_layer="triage", payload={"phrase": "x"},
                              evidence={})
    assert suggestion_store.set_status(conn, sid, "dismissed") is True
    assert suggestion_store.list_proposed(conn, "c") == []
    # Same payload can be proposed again now that the prior one is decided.
    assert suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                                target_layer="triage", payload={"phrase": "x"},
                                evidence={}) is not None


def test_expire_old(tmp_path):
    conn = _setup(tmp_path)
    sid = suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                              target_layer="triage", payload={"phrase": "x"},
                              evidence={})
    conn.execute(
        "UPDATE learning_suggestions SET created_at=datetime('now','-30 days') "
        "WHERE id=?", (sid,))
    assert suggestion_store.expire_old(conn, ttl_days=14) == 1
    assert suggestion_store.list_proposed(conn, "c") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_suggestion_store.py -v`
Expected: FAIL — `No module named 'src.suggestion_store'`.

- [ ] **Step 3: Write the implementation**

```python
# src/suggestion_store.py
"""Storage for proposed cheap-layer rules awaiting operator approval.

Nothing here activates a rule — the GUI Accept handler (profile_writer) is
the sole writer into profile.json. This module only persists, dedups, lists,
and expires proposals.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Suggestion:
    id: int
    source_channel_id: str
    rule_kind: str
    target_layer: str
    payload: dict
    evidence: dict
    status: str
    created_at: str


def dedupe_key(rule_kind: str, payload: dict) -> str:
    """Stable hash of rule_kind + normalized payload so the same proposal
    isn't re-queued while pending."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{rule_kind}|{blob}".encode("utf-8")).hexdigest()


def _row(r: sqlite3.Row) -> Suggestion:
    return Suggestion(
        id=r["id"], source_channel_id=r["source_channel_id"],
        rule_kind=r["rule_kind"], target_layer=r["target_layer"],
        payload=json.loads(r["payload_json"]),
        evidence=json.loads(r["evidence_json"]),
        status=r["status"], created_at=r["created_at"],
    )


def add(conn, *, source_channel_id, rule_kind, target_layer,
        payload: dict, evidence: dict) -> int | None:
    """Insert a proposal. Returns None if an identical proposal is already
    pending (unique partial index on (channel, dedupe_key) where proposed)."""
    key = dedupe_key(rule_kind, payload)
    cur = conn.execute(
        "INSERT OR IGNORE INTO learning_suggestions("
        "  source_channel_id, rule_kind, target_layer, payload_json, "
        "  evidence_json, dedupe_key) VALUES(?,?,?,?,?,?)",
        (source_channel_id, rule_kind, target_layer,
         json.dumps(payload, ensure_ascii=False),
         json.dumps(evidence, ensure_ascii=False), key),
    )
    return cur.lastrowid if cur.rowcount else None


def list_proposed(conn, source_channel_id: str | None = None) -> list[Suggestion]:
    if source_channel_id is None:
        rows = conn.execute(
            "SELECT * FROM learning_suggestions WHERE status='proposed' "
            "ORDER BY created_at DESC, id DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM learning_suggestions "
            "WHERE status='proposed' AND source_channel_id=? "
            "ORDER BY created_at DESC, id DESC", (source_channel_id,)).fetchall()
    return [_row(r) for r in rows]


def get(conn, sid: int) -> Suggestion | None:
    r = conn.execute(
        "SELECT * FROM learning_suggestions WHERE id=?", (sid,)).fetchone()
    return _row(r) if r else None


def set_status(conn, sid: int, status: str) -> bool:
    cur = conn.execute(
        "UPDATE learning_suggestions SET status=?, decided_at=CURRENT_TIMESTAMP "
        "WHERE id=?", (status, sid))
    return (cur.rowcount or 0) > 0


def expire_old(conn, ttl_days: int) -> int:
    cur = conn.execute(
        "UPDATE learning_suggestions SET status='expired', "
        "decided_at=CURRENT_TIMESTAMP "
        "WHERE status='proposed' AND created_at < datetime('now', ?)",
        (f"-{ttl_days} days",))
    return cur.rowcount or 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_suggestion_store.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/suggestion_store.py tests/test_suggestion_store.py
git commit -m "feat(cll): suggestion_store CRUD/dedupe/expiry"
```

---

## Task 8: `channel_learner` — clustering + replay/evidence (pure logic)

**Files:**
- Create: `src/channel_learner.py`
- Test: `tests/test_channel_learner.py` (create)

This task is pure logic: no DB, no network. `learn()` takes already-embedded
`Sample`s and a `synth_fn` callback, and returns `SuggestionDraft`s with
evidence. The bot loop (Task 9) wires embeddings + synthesis + persistence.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_channel_learner.py
from __future__ import annotations

from src import channel_learner as cl
from src.learning_store import Sample


def _s(i, text, category, action_types, vec, triage="keep", seen=1):
    return Sample(id=i, source_channel_id="c", text=text, norm_text=text,
                  category=category, action_types=action_types,
                  triage_decision=triage, seen_count=seen, embedding=vec)


PARAMS = cl.LearnParams(min_cluster_size=3, embed_threshold=0.9,
                        min_support=3, min_purity=0.9)


def _synth(texts, category, action_types_csv):
    return {"phrase": texts[0], "rationale": f"{len(texts)} like this"}


def test_noise_cluster_with_zero_signal_is_clean(tmp_path):
    # 4 near-identical 'ignore' messages → a clean noise suggestion.
    samples = [_s(i, "مبروك على الارباح", "ignore", "", [1.0, 0.0]) for i in range(4)]
    drafts = cl.learn(samples, synth_fn=_synth, params=PARAMS)
    noise = [d for d in drafts if d.rule_kind == "noise"]
    assert len(noise) == 1
    ev = noise[0].evidence
    assert ev["would_suppress"] >= 4
    assert ev["false_suppression_count"] == 0
    assert noise[0].target_layer == "triage"


def test_noise_cluster_flags_false_suppression(tmp_path):
    # 3 ignore + 1 signal in the SAME embedding cluster → unsafe.
    samples = [_s(i, "x", "ignore", "", [1.0, 0.0]) for i in range(3)]
    samples.append(_s(99, "x signal", "signal", "OPEN", [1.0, 0.0]))
    drafts = cl.learn(samples, synth_fn=_synth, params=PARAMS)
    noise = [d for d in drafts if d.rule_kind == "noise"]
    assert len(noise) == 1
    assert noise[0].evidence["false_suppression_count"] == 1


def test_action_trigger_cluster(tmp_path):
    # 3 'احجز نصف' all mapped to CLOSE_PARTIAL → action_trigger suggestion.
    samples = [_s(i, "احجز نصف", "signal", "CLOSE_PARTIAL", [0.0, 1.0])
               for i in range(3)]
    drafts = cl.learn(samples, synth_fn=_synth, params=PARAMS)
    trig = [d for d in drafts if d.rule_kind == "action_trigger"]
    assert len(trig) == 1
    assert trig[0].payload["action_types"] == ["CLOSE_PARTIAL"]
    assert trig[0].evidence["purity"] == 1.0
    assert trig[0].target_layer == "matcher"


def test_impure_action_cluster_is_blocked_not_silent(tmp_path):
    # mixed actions in one cluster → purity < threshold → conflicts recorded.
    samples = [_s(0, "x", "signal", "CLOSE_PARTIAL", [0.0, 1.0]),
               _s(1, "x", "signal", "CLOSE_PARTIAL", [0.0, 1.0]),
               _s(2, "x", "signal", "MOVE_SL_BE", [0.0, 1.0])]
    drafts = cl.learn(samples, synth_fn=_synth, params=PARAMS)
    trig = [d for d in drafts if d.rule_kind == "action_trigger"]
    assert len(trig) == 1
    assert trig[0].evidence["purity"] < 0.9
    assert trig[0].evidence["conflicts"]  # non-empty


def test_keep_trigger_from_triage_false_negative(tmp_path):
    # signal messages triage marked 'ignore' → keep_trigger (accuracy gain).
    samples = [_s(i, "ادخل شراء الان", "signal", "OPEN_INSTANT", [0.5, 0.5],
                  triage="ignore") for i in range(3)]
    drafts = cl.learn(samples, synth_fn=_synth, params=PARAMS)
    keep = [d for d in drafts if d.rule_kind == "keep_trigger"]
    assert len(keep) == 1
    assert keep[0].target_layer == "triage"


def test_small_cluster_below_min_is_ignored(tmp_path):
    samples = [_s(0, "x", "ignore", "", [1.0, 0.0]),
               _s(1, "x", "ignore", "", [1.0, 0.0])]  # only 2 < min 3
    assert cl.learn(samples, synth_fn=_synth, params=PARAMS) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_channel_learner.py -v`
Expected: FAIL — `No module named 'src.channel_learner'`.

- [ ] **Step 3: Write the implementation**

```python
# src/channel_learner.py
"""Batch learner: cluster a channel's labeled corpus and propose cheap-layer
rules with replay evidence. Pure logic — no DB, no network. The bot loop
supplies embeddings (already on each Sample) and a synth_fn callback, and
persists the returned drafts via suggestion_store.

Generalization = greedy cosine clustering within a category group, then one
LLM synthesis per dense cluster, then a replay of the candidate rule over the
whole corpus to compute evidence (the accuracy guarantee).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from src.trigger_matcher import _cosine

# Categories that mean "no trade-relevant action" — candidates for suppression.
_NOISE_CATEGORIES = frozenset({"ignore"})
# Action types the matcher can deterministically emit (mirror of
# trigger_matcher._ALLOWED_ACTION_TYPES minus reply-intent meta types).
_TRIGGERABLE = frozenset({
    "CLOSE_FULL", "MOVE_SL_BE", "CLOSE_PARTIAL", "REOPEN_LAST",
    "REINFORCE", "TIGHTEN_SL", "CANCEL_PENDING",
})
# Verdicts that mean "a real trade would have been missed" if suppressed.
_SIGNAL_CATEGORIES = frozenset({"signal", "partial_signal"})


@dataclass(frozen=True)
class LearnParams:
    min_cluster_size: int
    embed_threshold: float
    min_support: int
    min_purity: float


@dataclass(frozen=True)
class SuggestionDraft:
    rule_kind: str
    target_layer: str
    payload: dict
    evidence: dict


def _greedy_clusters(samples, threshold):
    """Greedy single-pass clustering by cosine vs each cluster's first member.
    Returns list[list[Sample]]. Samples without embeddings are skipped."""
    clusters: list[list] = []
    for s in samples:
        if not s.embedding:
            continue
        placed = False
        for c in clusters:
            if _cosine(s.embedding, c[0].embedding) >= threshold:
                c.append(s)
                placed = True
                break
        if not placed:
            clusters.append([s])
    return clusters


def _suppression_evidence(cluster, all_samples, threshold):
    """Replay: how many corpus messages does this cluster's centroid match,
    and how many of those were actually trade-relevant (false suppression)."""
    centroid = cluster[0].embedding
    would_suppress = 0
    false_suppression = 0
    breakdown: Counter = Counter()
    for s in all_samples:
        if not s.embedding:
            continue
        if _cosine(s.embedding, centroid) >= threshold:
            would_suppress += 1
            breakdown[s.category] += 1
            if s.category in _SIGNAL_CATEGORIES or s.action_types:
                false_suppression += 1
    return {
        "would_suppress": would_suppress,
        "false_suppression_count": false_suppression,
        "verdict_breakdown": dict(breakdown),
        "support": sum(s.seen_count for s in cluster),
    }


def learn(samples, *, synth_fn, params: LearnParams) -> list[SuggestionDraft]:
    drafts: list[SuggestionDraft] = []
    embedded = [s for s in samples if s.embedding]
    if not embedded:
        return drafts

    # Partition by coarse intent for clustering.
    noise = [s for s in embedded if s.category in _NOISE_CATEGORIES]
    signal = [s for s in embedded if s.category in _SIGNAL_CATEGORIES]
    context = [s for s in embedded if s.category == "context"]

    # --- Suppression (noise) rules ---
    for cluster in _greedy_clusters(noise, params.embed_threshold):
        if len(cluster) < params.min_cluster_size:
            continue
        ev = _suppression_evidence(cluster, embedded, params.embed_threshold)
        syn = synth_fn([s.text for s in cluster], "ignore", "")
        drafts.append(SuggestionDraft(
            rule_kind="noise", target_layer="triage",
            payload={"phrase": syn["phrase"],
                     "samples": [s.text for s in cluster[:5]]},
            evidence={**ev, "rationale": syn.get("rationale", ""),
                      "one_tap_safe": ev["false_suppression_count"] == 0
                      and ev["support"] >= params.min_support},
        ))

    # --- Deterministic action triggers ---
    triggerable = [s for s in signal
                   if any(t in _TRIGGERABLE for t in s.action_types.split(",") if t)]
    for cluster in _greedy_clusters(triggerable, params.embed_threshold):
        if len(cluster) < params.min_cluster_size:
            continue
        types = Counter()
        for s in cluster:
            for t in s.action_types.split(","):
                if t in _TRIGGERABLE:
                    types[t] += 1
        if not types:
            continue
        top_type, top_n = types.most_common(1)[0]
        purity = top_n / len(cluster)
        conflicts = {t: n for t, n in types.items() if t != top_type}
        syn = synth_fn([s.text for s in cluster], "signal", top_type)
        drafts.append(SuggestionDraft(
            rule_kind="action_trigger", target_layer="matcher",
            payload={"phrase": syn["phrase"], "action_types": [top_type],
                     "samples": [s.text for s in cluster[:5]]},
            evidence={"support": sum(s.seen_count for s in cluster),
                      "purity": purity, "conflicts": conflicts,
                      "rationale": syn.get("rationale", ""),
                      "one_tap_safe": purity >= params.min_purity
                      and not conflicts
                      and sum(s.seen_count for s in cluster) >= params.min_support},
        ))

    # --- Triage keep-triggers (accuracy gain): signals triage marked ignore ---
    triage_misses = [s for s in signal if s.triage_decision == "ignore"]
    for cluster in _greedy_clusters(triage_misses, params.embed_threshold):
        if len(cluster) < params.min_cluster_size:
            continue
        syn = synth_fn([s.text for s in cluster], "signal", "")
        drafts.append(SuggestionDraft(
            rule_kind="keep_trigger", target_layer="triage",
            payload={"phrase": syn["phrase"],
                     "samples": [s.text for s in cluster[:5]]},
            evidence={"support": sum(s.seen_count for s in cluster),
                      "triage_false_negatives": len(cluster),
                      "rationale": syn.get("rationale", ""),
                      "one_tap_safe": True},  # accuracy gain — always safe
        ))

    # --- Context-drop tuning (lower-risk cost saving) ---
    for cluster in _greedy_clusters(context, params.embed_threshold):
        if len(cluster) < params.min_cluster_size:
            continue
        ev = _suppression_evidence(cluster, embedded, params.embed_threshold)
        syn = synth_fn([s.text for s in cluster], "context", "")
        drafts.append(SuggestionDraft(
            rule_kind="context_drop", target_layer="triage",
            payload={"phrase": syn["phrase"],
                     "samples": [s.text for s in cluster[:5]]},
            evidence={**ev, "rationale": syn.get("rationale", ""),
                      "one_tap_safe": ev["false_suppression_count"] == 0
                      and ev["support"] >= params.min_support},
        ))

    return drafts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_channel_learner.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/channel_learner.py tests/test_channel_learner.py
git commit -m "feat(cll): channel_learner clustering + replay evidence"
```

---

## Task 9: `learning_loop` — the rolling bot-process scheduler

**Files:**
- Create: `src/bot_loops/learning_loop.py`
- Modify: `src/bot_loops/__init__.py` (export), `src/bot.py` (register in `post_init`)
- Test: `tests/test_learning_loop.py` (create — tests the pure `run_channel_once` core, not the infinite loop)

The loop owns: embedding uncached samples (batched), calling `channel_learner.learn`,
synthesizing via the triage provider, and persisting drafts via `suggestion_store`.
Factor the per-channel work into `run_channel_once(conn, channel_id, *, embed_fn,
synth_fn, params, corpus_max)` so it's unit-testable without asyncio.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_learning_loop.py
from __future__ import annotations

from src import learning_store, suggestion_store
from src.bot_loops.learning_loop import run_channel_once
from src.channel_learner import LearnParams
from src.db import connect, init_schema


PARAMS = LearnParams(min_cluster_size=3, embed_threshold=0.9,
                     min_support=3, min_purity=0.9)


def _fake_embed(texts):
    # Deterministic 2-d vectors: noise vs not, by a marker char.
    return [[1.0, 0.0] if "مبروك" in t else [0.0, 1.0] for t in texts]


def _fake_synth(texts, category, action_types_csv):
    return {"phrase": texts[0], "rationale": "x"}


def test_run_channel_once_embeds_and_proposes(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    for i in range(4):
        learning_store.capture(conn, source_channel_id="c", route_id="",
                               source_msg_id=i, text="مبروك على الارباح",
                               category="ignore", action_types="",
                               triage_decision="keep")
    n = run_channel_once(conn, "c", embed_fn=_fake_embed, synth_fn=_fake_synth,
                         params=PARAMS, corpus_max=2000)
    assert n >= 1
    sugg = suggestion_store.list_proposed(conn, "c")
    assert any(s.rule_kind == "noise" for s in sugg)
    # Embeddings were persisted — a second run finds nothing new to embed.
    assert learning_store.without_embedding(conn, "c", 100) == []


def test_run_channel_once_is_idempotent_on_suggestions(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    for i in range(4):
        learning_store.capture(conn, source_channel_id="c", route_id="",
                               source_msg_id=i, text="مبروك على الارباح",
                               category="ignore", action_types="",
                               triage_decision="keep")
    run_channel_once(conn, "c", embed_fn=_fake_embed, synth_fn=_fake_synth,
                     params=PARAMS, corpus_max=2000)
    before = len(suggestion_store.list_proposed(conn, "c"))
    run_channel_once(conn, "c", embed_fn=_fake_embed, synth_fn=_fake_synth,
                     params=PARAMS, corpus_max=2000)
    after = len(suggestion_store.list_proposed(conn, "c"))
    assert before == after  # dedupe prevents duplicates
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_learning_loop.py -v`
Expected: FAIL — module / `run_channel_once` not defined.

- [ ] **Step 3: Write the implementation**

```python
# src/bot_loops/learning_loop.py
"""Rolling per-channel learning scheduler for the Channel Learning Loop.

Once a channel accumulates `learning_batch_n` new samples, embed the uncached
ones, run channel_learner, and persist suggestions. Runs inside the bot
process alongside the other sweepers. A crash here can never affect trading —
the loop body is fully wrapped.

`run_channel_once` is the testable per-channel core; `learning_loop` is the
asyncio wrapper that polls all channels.
"""
from __future__ import annotations

import asyncio
import logging

from telegram.ext import Application

from src import channel_learner, learning_store, suggestion_store
from src.channel_learner import LearnParams

log = logging.getLogger("bot")

# How often the asyncio wrapper wakes to check per-channel counters.
_POLL_SEC = 120.0
# Max samples embedded per run (bounds one batch's OpenAI cost).
_EMBED_BATCH_CAP = 200


def run_channel_once(conn, channel_id, *, embed_fn, synth_fn, params: LearnParams,
                     corpus_max: int) -> int:
    """Embed uncached samples for one channel, learn, persist suggestions.
    Returns number of NEW suggestions written. Pure of asyncio + provider
    construction (both injected) so it unit-tests with fakes."""
    # 1. Embed uncached samples (oldest-relevant first), persist vectors.
    pending = learning_store.without_embedding(conn, channel_id, _EMBED_BATCH_CAP)
    if pending:
        vecs = embed_fn([s.text for s in pending])
        if vecs and len(vecs) == len(pending):
            for s, v in zip(pending, vecs):
                learning_store.set_embedding(conn, s.id, v)

    # 2. Learn from the (now-embedded) recent corpus.
    samples = learning_store.recent(conn, channel_id, corpus_max)
    drafts = channel_learner.learn(samples, synth_fn=synth_fn, params=params)

    # 3. Persist as proposals (dedupe handled by suggestion_store).
    written = 0
    for d in drafts:
        sid = suggestion_store.add(
            conn, source_channel_id=channel_id, rule_kind=d.rule_kind,
            target_layer=d.target_layer, payload=d.payload, evidence=d.evidence)
        if sid is not None:
            written += 1

    # 4. Bound corpus growth.
    learning_store.prune(conn, channel_id, corpus_max)
    return written


def _build_embed_fn(db_path):
    """OpenAI embedding callback reusing trigger_matcher's batched call."""
    from pathlib import Path
    from src.trigger_matcher import _embed_batch

    def embed_fn(texts):
        return _embed_batch(texts, db_path=Path(db_path))
    return embed_fn


def _build_synth_fn(db_path):
    """Cheap-LLM synthesis callback via the triage provider (gpt-5-nano/Haiku)."""
    import json
    from src.llm_provider import build_triage_provider

    provider = build_triage_provider(model="")
    system = (
        "You summarize a CLUSTER of similar trading-channel messages into ONE "
        "short, distinctive phrase that characterizes them. Output strict JSON: "
        '{\"phrase\": \"<short distinctive phrase>\", \"rationale\": \"<one line>\"}. '
        "No code fences."
    )

    def synth_fn(texts, category, action_types_csv):
        sample = "\n".join(f"- {t}" for t in texts[:8])
        user = (f"category={category} action_types={action_types_csv}\n"
                f"messages:\n{sample}")
        try:
            res = provider.triage(system_prompt=system, user_content=user,
                                  max_output_tokens=120)
            obj = json.loads(res.raw_text.strip().strip("`"))
            phrase = str(obj.get("phrase") or texts[0])
            return {"phrase": phrase, "rationale": str(obj.get("rationale") or "")}
        except Exception as e:  # noqa: BLE001 — fall back to representative text
            log.warning("CLL synth failed: %s", e)
            return {"phrase": texts[0], "rationale": ""}
    return synth_fn


async def learning_loop(app: Application):
    """Poll channels; when one crosses learning_batch_n new samples, learn.
    Silent on idle ticks. Fully wrapped — never breaks the bot."""
    import sqlite3
    from src import config, db_settings
    from pathlib import Path

    conn: sqlite3.Connection = app.bot_data["conn"]
    db_path = Path(config.DB_PATH)
    last_seen: dict[str, int] = {}
    embed_fn = _build_embed_fn(str(db_path))
    synth_fn = _build_synth_fn(str(db_path))

    while True:
        try:
            if not db_settings.get_bool(db_path, "learning_enabled", True):
                await asyncio.sleep(_POLL_SEC)
                continue
            batch_n = db_settings.get_int(db_path, "learning_batch_n", 50)
            corpus_max = db_settings.get_int(db_path, "learning_corpus_max", 2000)
            ttl_days = db_settings.get_int(db_path, "learning_suggestion_ttl_days", 14)
            params = LearnParams(
                min_cluster_size=db_settings.get_int(db_path, "learning_min_cluster_size", 4),
                embed_threshold=db_settings.get_float(db_path, "learning_embed_threshold", 0.82),
                min_support=db_settings.get_int(db_path, "learning_min_support", 5),
                min_purity=db_settings.get_float(db_path, "learning_min_purity", 0.9),
            )
            suggestion_store.expire_old(conn, ttl_days)

            channels = [r["source_channel_id"] for r in conn.execute(
                "SELECT DISTINCT source_channel_id FROM learning_samples"
            ).fetchall()]
            for ch in channels:
                cur_max = learning_store.max_id(conn, ch)
                seen = last_seen.get(ch)
                if seen is None:
                    last_seen[ch] = cur_max  # first sight — baseline, don't run
                    continue
                if learning_store.count_since(conn, ch, seen) < batch_n:
                    continue
                last_seen[ch] = cur_max
                try:
                    n = run_channel_once(conn, ch, embed_fn=embed_fn,
                                         synth_fn=synth_fn, params=params,
                                         corpus_max=corpus_max)
                    if n:
                        log.info("CLL: %d new suggestions for channel %s", n, ch)
                        from src.notify import notify_owner
                        notify_owner(
                            f"💡 {n} new classification suggestion(s) for "
                            f"{ch}. Review in the Suggestions tab.")
                except Exception:  # noqa: BLE001 — per-channel isolation
                    log.exception("CLL run_channel_once failed for %s", ch)
        except Exception as e:  # noqa: BLE001 — loop must never die
            log.exception("learning_loop error: %s", e)
        await asyncio.sleep(_POLL_SEC)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_learning_loop.py -v`
Expected: PASS

- [ ] **Step 5: Register the loop**

In `src/bot_loops/__init__.py`, add the import and `__all__` entry:

```python
from src.bot_loops.learning_loop import learning_loop
```
and add `"learning_loop",` to `__all__`.

In `src/bot.py` `post_init` (the import block around line 333-345), add
`learning_loop` to the `from src.bot_loops import (...)` list, and after the
`cost_guard_loop` registration (line 426) add:

```python
    _supervise(asyncio.create_task(learning_loop(app)), "learning_loop")
```

- [ ] **Step 6: Run the bot-loops import smoke test**

Run: `.venv\Scripts\python.exe -c "import src.bot_loops as b; assert hasattr(b,'learning_loop'); print('ok')"`
Expected: `ok`

- [ ] **Step 7: Commit**

```bash
git add src/bot_loops/learning_loop.py src/bot_loops/__init__.py src/bot.py tests/test_learning_loop.py
git commit -m "feat(cll): rolling learning_loop in bot process"
```

---

## Task 10: `profile_writer` — the Accept path (writes into profile.json)

**Files:**
- Create: `src/profile_writer.py`
- Test: `tests/test_profile_writer.py` (create)

Accept is the ONLY mutation of `profile.json`. It routes a suggestion's payload
into the correct existing field by `target_layer`/`rule_kind`, validates shape,
and writes atomically (temp + rename). The live layers pick up the change via
their mtime caches — no restart.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_profile_writer.py
from __future__ import annotations

import json

from src import profile_writer, suggestion_store
from src.db import connect, init_schema


def _profile(tmp_path, data):
    p = tmp_path / "profile.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def _sugg(conn, **kw):
    sid = suggestion_store.add(conn, **kw)
    return suggestion_store.get(conn, sid)


def test_noise_appends_to_noise_patterns(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    p = _profile(tmp_path, {"symbol": "XAUUSD", "noise_patterns": "old"})
    s = _sugg(conn, source_channel_id="c", rule_kind="noise",
              target_layer="triage", payload={"phrase": "مبروك"}, evidence={})
    profile_writer.apply_suggestion(p, s)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert "مبروك" in out["noise_patterns"]
    assert "old" in out["noise_patterns"]  # preserved


def test_action_trigger_appends_trigger_entry(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    p = _profile(tmp_path, {"symbol": "XAUUSD", "triggers": []})
    s = _sugg(conn, source_channel_id="c", rule_kind="action_trigger",
              target_layer="matcher",
              payload={"phrase": "احجز نصف", "action_types": ["CLOSE_PARTIAL"],
                       "samples": ["احجز نصف ارباحك"]}, evidence={})
    profile_writer.apply_suggestion(p, s)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert len(out["triggers"]) == 1
    t = out["triggers"][0]
    assert t["action_types"] == ["CLOSE_PARTIAL"]
    assert t["samples"] == ["احجز نصف ارباحك"]


def test_keep_trigger_appends_triage_keep(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    p = _profile(tmp_path, {"symbol": "XAUUSD"})
    s = _sugg(conn, source_channel_id="c", rule_kind="keep_trigger",
              target_layer="triage", payload={"phrase": "ادخل الان"}, evidence={})
    profile_writer.apply_suggestion(p, s)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert "ادخل الان" in out["triage_keep_triggers"]


def test_unknown_kind_raises(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    p = _profile(tmp_path, {"symbol": "XAUUSD"})
    s = _sugg(conn, source_channel_id="c", rule_kind="action_trigger",
              target_layer="matcher", payload={"phrase": "x"}, evidence={})
    # missing action_types in payload → invalid
    import pytest
    with pytest.raises(ValueError):
        profile_writer.apply_suggestion(p, s)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_profile_writer.py -v`
Expected: FAIL — `No module named 'src.profile_writer'`.

- [ ] **Step 3: Write the implementation**

```python
# src/profile_writer.py
"""Apply an accepted learning suggestion into the channel profile.json.

The SOLE writer of learned rules into the live config. Each rule_kind maps to
an existing profile field that a live layer already reads:
  noise / context_drop -> noise_patterns (triage prompt)
  keep_trigger         -> triage_keep_triggers (triage prompt)
  action_trigger       -> triggers[] (trigger_matcher)
Writes atomically (temp + os.replace) so a crash mid-write can't corrupt the
profile. Newline-delimited text fields get the phrase appended de-duplicated.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from src.suggestion_store import Suggestion


def _append_line(existing: str, phrase: str) -> str:
    lines = [l for l in (existing or "").splitlines() if l.strip()]
    if phrase not in lines:
        lines.append(phrase)
    return "\n".join(lines)


def apply_suggestion(profile_path: Path, suggestion: Suggestion) -> None:
    profile_path = Path(profile_path)
    data = json.loads(profile_path.read_text(encoding="utf-8"))
    kind = suggestion.rule_kind
    payload = suggestion.payload
    phrase = str(payload.get("phrase") or "").strip()

    if kind in ("noise", "context_drop"):
        if not phrase:
            raise ValueError("noise suggestion missing phrase")
        data["noise_patterns"] = _append_line(data.get("noise_patterns", ""), phrase)
    elif kind == "keep_trigger":
        if not phrase:
            raise ValueError("keep_trigger suggestion missing phrase")
        data["triage_keep_triggers"] = _append_line(
            data.get("triage_keep_triggers", ""), phrase)
    elif kind == "action_trigger":
        action_types = payload.get("action_types")
        samples = payload.get("samples")
        if not phrase or not action_types or not samples:
            raise ValueError("action_trigger payload incomplete")
        triggers = data.get("triggers") or []
        triggers.append({
            "phrase": phrase,
            "action_types": list(action_types),
            "samples": list(samples),
            "context_tokens": [],
        })
        data["triggers"] = triggers
    else:
        raise ValueError(f"unknown rule_kind: {kind}")

    tmp = profile_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, profile_path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_profile_writer.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/profile_writer.py tests/test_profile_writer.py
git commit -m "feat(cll): profile_writer atomic Accept path"
```

---

## Task 11: GUI Suggestions tab

**Files:**
- Create: `src/gui/views/suggestions_view.py`
- Modify: `src/gui/windows/main_window.py` (register the new tab; follow how `pipeline_view` / Triggers view is added)
- Modify: the Triggers view's "Unmatched" pane source — repoint from `unmatched_store` to `suggestion_store.list_proposed` (or hide the old pane), so there's one review surface.
- Test: `tests/test_suggestions_view_logic.py` (create — test the non-Qt logic: ranking + accept wiring)

GUI widgets are hard to unit-test; keep all decision logic in a pure helper
module so it is testable, and keep the Qt view thin.

- [ ] **Step 1: Write the failing test (pure ranking/accept logic)**

```python
# tests/test_suggestions_view_logic.py
from __future__ import annotations

import json

from src import suggestion_store
from src.db import connect, init_schema
from src.gui.views.suggestions_logic import rank_suggestions, accept_suggestion


def test_rank_puts_accuracy_gains_and_one_tap_first(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                         target_layer="triage", payload={"phrase": "a"},
                         evidence={"one_tap_safe": False, "would_suppress": 3})
    suggestion_store.add(conn, source_channel_id="c", rule_kind="keep_trigger",
                         target_layer="triage", payload={"phrase": "b"},
                         evidence={"one_tap_safe": True})
    suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                         target_layer="triage", payload={"phrase": "d"},
                         evidence={"one_tap_safe": True, "would_suppress": 40})
    ranked = rank_suggestions(suggestion_store.list_proposed(conn, "c"))
    # keep_trigger (accuracy gain) first, then high-savings one-tap noise,
    # then the non-one-tap (needs confirmation) last.
    assert ranked[0].rule_kind == "keep_trigger"
    assert ranked[-1].evidence["one_tap_safe"] is False


def test_accept_writes_profile_and_marks_accepted(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    p = tmp_path / "profile.json"
    p.write_text(json.dumps({"symbol": "XAUUSD"}), encoding="utf-8")
    sid = suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                               target_layer="triage", payload={"phrase": "مبروك"},
                               evidence={"one_tap_safe": True})
    accept_suggestion(conn, sid, profile_path=p)
    assert suggestion_store.get(conn, sid).status == "accepted"
    assert "مبروك" in json.loads(p.read_text(encoding="utf-8"))["noise_patterns"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_suggestions_view_logic.py -v`
Expected: FAIL — `src.gui.views.suggestions_logic` not found.

- [ ] **Step 3: Write the pure logic helper**

```python
# src/gui/views/suggestions_logic.py
"""Non-Qt logic for the Suggestions tab: ranking + accept orchestration.
Kept separate from the Qt view so it is unit-testable."""
from __future__ import annotations

from pathlib import Path

from src import profile_writer, suggestion_store
from src.suggestion_store import Suggestion

# Accuracy-gain kinds rank above cost-saving kinds.
_KIND_RANK = {"keep_trigger": 0, "action_trigger": 1, "noise": 2, "context_drop": 3}


def rank_suggestions(items: list[Suggestion]) -> list[Suggestion]:
    """Order: accuracy gains first; within a kind, one-tap-safe first, then by
    estimated savings (would_suppress/support) descending."""
    def key(s: Suggestion):
        safe = 0 if s.evidence.get("one_tap_safe") else 1
        savings = -(s.evidence.get("would_suppress")
                    or s.evidence.get("support") or 0)
        return (_KIND_RANK.get(s.rule_kind, 9), safe, savings)
    return sorted(items, key=key)


def accept_suggestion(conn, sid: int, *, profile_path: Path) -> None:
    """Write the rule into profile.json then mark the suggestion accepted.
    Order matters: only flip status if the profile write succeeds."""
    s = suggestion_store.get(conn, sid)
    if s is None:
        raise ValueError(f"suggestion {sid} not found")
    profile_writer.apply_suggestion(Path(profile_path), s)
    suggestion_store.set_status(conn, sid, "accepted")


def dismiss_suggestion(conn, sid: int) -> None:
    suggestion_store.set_status(conn, sid, "dismissed")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_suggestions_view_logic.py -v`
Expected: PASS

- [ ] **Step 5: Write the Qt view (thin) and register it**

Read an existing view for the exact base class + registration idiom:
`src/gui/views/pipeline_view.py` and `src/gui/windows/main_window.py`. Create
`src/gui/views/suggestions_view.py` mirroring that structure. The view must:
- List `rank_suggestions(suggestion_store.list_proposed(conn, active_channel))`.
- Per row show: rule_kind, phrase, and an evidence summary string —
  for noise/context_drop: `would_suppress`, `false_suppression_count`,
  `verdict_breakdown`; for action_trigger: `support`, `purity`, `conflicts`;
  for keep_trigger: `triage_false_negatives`.
- Render `one_tap_safe is False` rows with a red flag and require a confirm
  dialog before calling `accept_suggestion` (the "accept anyway" gate from the
  spec). `one_tap_safe is True` rows get a direct Accept button.
- Wire Accept → `suggestions_logic.accept_suggestion(conn, sid,
  profile_path=<active profile.json>)`; Dismiss → `dismiss_suggestion`.
- Resolve the active profile.json via the same path the matcher uses:
  `Path(config.DB_PATH).parent / "profile.json"`.

Register the tab in `main_window.py` next to the existing Triggers/Pipeline
tabs. Repoint (or remove) the Triggers view's "Unmatched" pane to read from
`suggestion_store` so there is a single review surface.

- [ ] **Step 6: Manual smoke check**

Run the GUI per `CLAUDE.md` and confirm the Suggestions tab loads, lists
proposals, and Accept writes to `profile.json`. (No automated Qt test.)

- [ ] **Step 7: Commit**

```bash
git add src/gui/views/suggestions_view.py src/gui/views/suggestions_logic.py src/gui/windows/main_window.py tests/test_suggestions_view_logic.py
git commit -m "feat(cll): Suggestions GUI tab + ranking/accept logic"
```

---

## Task 12: End-to-end integration test (the 2026-05-28 distribution)

**Files:**
- Test: `tests/test_learning_integration.py` (create)

Proves the loop would have proposed the noise-suppression rules that kill the
50 wasted gpt-5 calls/day with `false_suppression_count == 0`, and the
recurring management trigger — using fake embed/synth so it's hermetic.

- [ ] **Step 1: Write the test**

```python
# tests/test_learning_integration.py
"""End-to-end: capture a realistic mixed corpus, run the loop, assert the
right suggestions appear with safe evidence — no live API."""
from __future__ import annotations

from src import learning_store, suggestion_store
from src.bot_loops.learning_loop import run_channel_once
from src.channel_learner import LearnParams
from src.db import connect, init_schema


PARAMS = LearnParams(min_cluster_size=3, embed_threshold=0.9,
                     min_support=3, min_purity=0.9)


def _embed(texts):
    # 3 well-separated buckets by marker substring.
    out = []
    for t in texts:
        if "مبروك" in t:        # noise
            out.append([1.0, 0.0, 0.0])
        elif "احجز" in t:       # CLOSE_PARTIAL management
            out.append([0.0, 1.0, 0.0])
        else:                    # signal-ish
            out.append([0.0, 0.0, 1.0])
    return out


def _synth(texts, category, action_types_csv):
    return {"phrase": texts[0], "rationale": "cluster"}


def test_loop_proposes_safe_noise_and_action_trigger(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    # 5 noise (all 'ignore'), 4 management (all CLOSE_PARTIAL signals).
    for i in range(5):
        learning_store.capture(conn, source_channel_id="c", route_id="",
                               source_msg_id=i, text="مبروك على الارباح اليوم",
                               category="ignore", action_types="",
                               triage_decision="keep")
    for i in range(4):
        learning_store.capture(conn, source_channel_id="c", route_id="",
                               source_msg_id=100 + i, text="احجز نصف ارباحك",
                               category="signal", action_types="CLOSE_PARTIAL",
                               triage_decision="keep")
    run_channel_once(conn, "c", embed_fn=_embed, synth_fn=_synth,
                     params=PARAMS, corpus_max=2000)
    sugg = suggestion_store.list_proposed(conn, "c")

    noise = [s for s in sugg if s.rule_kind == "noise"]
    assert noise and noise[0].evidence["false_suppression_count"] == 0
    assert noise[0].evidence["one_tap_safe"] is True

    trig = [s for s in sugg if s.rule_kind == "action_trigger"]
    assert trig and trig[0].payload["action_types"] == ["CLOSE_PARTIAL"]
    assert trig[0].evidence["purity"] == 1.0
    assert trig[0].evidence["one_tap_safe"] is True
```

- [ ] **Step 2: Run the test**

Run: `.venv\Scripts\python.exe -m pytest tests/test_learning_integration.py -v`
Expected: PASS

- [ ] **Step 3: Run the full hermetic suite to confirm no regressions**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: PASS (all prior tests + the new CLL tests)

- [ ] **Step 4: Commit**

```bash
git add tests/test_learning_integration.py
git commit -m "test(cll): end-to-end learning loop integration"
```

---

## Task 13: Documentation

**Files:**
- Modify: `CLAUDE.md` (add a "Channel Learning Loop (CLL)" subsection under Architecture, and the new modules to Key modules)

- [ ] **Step 1: Add the docs**

In `CLAUDE.md`, under `### Key modules`, add:

```markdown
- `src/learning_store.py` — labeled corpus for the Channel Learning Loop. Captures every interpreted message + category/action_types + triage decision, keyed by `source_channel_id`. Supersedes `unmatched_store`.
- `src/channel_learner.py` — pure batch learner: greedy cosine clustering by category + LLM synthesis + replay evidence (incl. `false_suppression_count`). No DB/network.
- `src/suggestion_store.py` — proposed cheap-layer rules (`proposed|accepted|dismissed|expired`), dedup + TTL expiry.
- `src/profile_writer.py` — the sole writer of accepted rules into `profile.json` (atomic temp+rename).
- `src/bot_loops/learning_loop.py` — rolling per-channel scheduler; runs the learner after `learning_batch_n` new samples. Wrapped so a learner failure can't touch the trade path.
```

And after the "Position-state context" subsection, add a short "Channel
Learning Loop (CLL)" paragraph summarizing: capture → rolling cluster/synthesize
→ replay-evidenced suggestions → operator Accept writes profile.json → live
layers pick it up via mtime cache. Note it is propose-only and per-channel, and
that shadow re-evaluation is a planned v2 fast-follow.

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(cll): document the Channel Learning Loop"
```

---

## Self-review checklist (completed by plan author)

- **Spec coverage:** capture hook (Task 5), learning_store (Tasks 2,4), all four
  learning targets — noise, action_trigger, keep_trigger, context_drop (Task 8);
  profile refinement (`target_layer=profile`) is represented in the
  suggestion/profile-writer schema but its synthesis is intentionally deferred
  with the GUI's generic path — see Note below; embedding-cluster + LLM synthesis
  (Tasks 8,9); rolling cadence in bot process (Task 9); propose-only + evidence
  gate (Tasks 8,11); per-channel isolation (everywhere, keyed by
  source_channel_id); supersede unmatched_store (Task 6); expiry (Task 7); shadow
  re-eval correctly OUT of scope (v2).
- **Scope decision (4th target = fast-follow, confirmed by operator
  2026-05-29):** v1 ships the cost-cutting three targets — noise suppression,
  action triggers, triage keep/context tuning — which is where ~all the savings
  are. **Continuous profile refinement** (auto-suggesting
  `vocabulary_table`/`worked_examples`) is intentionally OUT of v1 and becomes a
  separate small follow-up plan (alongside shadow re-evaluation). The data path
  is already open (`target_layer='profile'` allowed), so the follow-up adds one
  branch to `channel_learner.learn` + one case to `profile_writer` — no rework.
- **Placeholder scan:** none — every code step has complete code.
- **Type consistency:** `Sample`, `Suggestion`, `SuggestionDraft`, `LearnParams`
  signatures match across Tasks 4/7/8/9/10/11; `capture(...)` kwargs identical in
  Tasks 4/5/12; `run_channel_once(...)` signature identical in Tasks 9/12;
  `apply_suggestion(profile_path, suggestion)` identical in Tasks 10/11.
