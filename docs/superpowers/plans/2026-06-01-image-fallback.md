# Chart Image Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a Telegram signal message produces a geometric-inconsistency ALERT (SL on wrong side of entry) AND the message had a chart image, run a second AI pass with the chart attached to extract the correct values and emit a valid OPEN action.

**Architecture:** The listener downloads the photo to bytes, base64-encodes it, and POSTs it in `IncomingMessageBody.image_b64`. The API decodes it and passes `image_bytes` to `process_message`. After the first AI pass produces an inconsistency ALERT, the orchestrator's `_image_fallback_pass` runs a second `interpret()` call with the image prepended as a vision content block. If the second pass yields a valid OPEN, it is persisted with `image_corrected=True` in its payload. The operator DM prefixes a ⚠️ note. A settings toggle (`image_fallback_enabled`, default on) can disable the whole path.

**Tech Stack:** Python 3.13, FastAPI, Pydantic, SQLite (WAL), Telethon (media download), Anthropic SDK (vision blocks), OpenAI SDK (image_url), pytest, PySide6 (no GUI changes needed).

**Spec:** `docs/superpowers/specs/2026-06-01-image-fallback-design.md`

---

## File Structure

| File | Change |
|---|---|
| `src/schema.sql` | Add `has_image INTEGER NOT NULL DEFAULT 0` to `messages` |
| `src/db.py` | Add `_migrate_messages_add_has_image`; add `image_fallback_enabled` to `DEFAULT_SETTINGS` in `src/db_settings.py` |
| `src/db_settings.py` | Add `"image_fallback_enabled": "1"` to `DEFAULT_SETTINGS` |
| `src/api_models.py` | Add `image_b64: str \| None = None` to `IncomingMessageBody` |
| `src/api_helpers.py` | Decode `image_b64` → `image_bytes`; pass to `process_message`; set `has_image` |
| `src/listener.py` | Download photo bytes; base64-encode; add to body (both API-dispatch and direct-call paths) |
| `src/llm_provider.py` | Add `image_bytes: bytes \| None = None` to `LLMProvider.interpret()` Protocol + both concrete impls |
| `src/orchestrator.py` | Add `image_bytes: bytes \| None = None` and `has_image: bool = False` to `process_message`; add `_should_attempt_image_fallback` + `_image_fallback_pass` helpers; pass `image_corrected=True` in action payload when fallback succeeds; propagate `has_image` to `_insert_message` |
| `src/telegram_format.py` | Read `payload.get("image_corrected")` in `render_action_notification` OPEN branch to prefix ⚠️ note |
| `tests/test_api.py` | Tests for `IncomingMessageBody` backward compat + `has_image` persistence |
| `tests/test_orchestrator_image.py` | New: unit tests for `_should_attempt_image_fallback` and `_image_fallback_pass` |
| `tests/test_telegram_format.py` | Tests for ⚠️ note rendering |

**Convention reminders:**
- `image_bytes` is NEVER stored in the DB. In-memory only.
- `image_corrected: True` lives in the action's `payload_json` (not messages) — available without a JOIN when rendering the DM.
- All new settings use `INSERT OR IGNORE` via `DEFAULT_SETTINGS`.
- Use `.venv\Scripts\python.exe -m pytest` on Windows.

---

## Task 1: DB migration — `has_image` column + settings default

**Files:**
- Modify: `src/schema.sql`
- Modify: `src/db.py` (add migration function + call in `init_schema`)
- Modify: `src/db_settings.py` (add default)
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api.py`:

```python
def test_has_image_column_exists_after_migration(tmp_path):
    conn = _setup(tmp_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    assert "has_image" in cols


def test_image_fallback_enabled_default_is_on(tmp_path):
    conn = _setup(tmp_path)
    row = conn.execute(
        "SELECT value FROM settings WHERE key='image_fallback_enabled'"
    ).fetchone()
    assert row is not None
    assert row["value"] == "1"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py::test_has_image_column_exists_after_migration tests/test_api.py::test_image_fallback_enabled_default_is_on -v`
Expected: FAIL (column missing / key absent).

- [ ] **Step 3: Add the column to schema.sql**

In `src/schema.sql`, in the `messages` table definition, add after the last existing column (before the closing `);`):

```sql
  -- True when the original Telegram message included a photo/media.
  -- The image bytes are NOT stored — only this flag for audit.
  has_image INTEGER NOT NULL DEFAULT 0
```

- [ ] **Step 4: Add the migration to db.py**

In `src/db.py`, add this function near the other `_migrate_messages_*` functions:

```python
def _migrate_messages_add_has_image(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "has_image" not in cols:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN has_image INTEGER NOT NULL DEFAULT 0"
        )
```

Then in `init_schema`, add the call after `_migrate_messages_add_source_channel` (or the last messages migration in the sequence):

```python
    _migrate_messages_add_has_image(conn)
```

- [ ] **Step 5: Add the settings default to db_settings.py**

In `src/db_settings.py`, find `DEFAULT_SETTINGS: dict[str, str] = {` and add:

```python
    "image_fallback_enabled": "1",
```

alongside the other defaults (e.g. near `"signal_memory_enabled": "1"`).

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py::test_has_image_column_exists_after_migration tests/test_api.py::test_image_fallback_enabled_default_is_on -v`
Expected: PASS

- [ ] **Step 7: Full API suite (no regressions)**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/schema.sql src/db.py src/db_settings.py tests/test_api.py
git commit -m "feat(db): has_image column on messages + image_fallback_enabled default"
```

---

## Task 2: API model + incoming_message plumbing

**Files:**
- Modify: `src/api_models.py:51-105` (`IncomingMessageBody`)
- Modify: `src/api_helpers.py:182-254` (`_run_orchestrator_for_incoming`)
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api.py`:

```python
def test_incoming_message_body_image_b64_optional(tmp_path):
    """Old callers that don't send image_b64 must still parse cleanly."""
    from src.api_models import IncomingMessageBody
    body = IncomingMessageBody(
        channel_id="ch_test",
        tg_chat_id=-100123,
        tg_message_id=1,
        text="test",
        route_id="",
    )
    assert body.image_b64 is None


def test_incoming_message_body_accepts_image_b64(tmp_path):
    import base64
    from src.api_models import IncomingMessageBody
    b64 = base64.b64encode(b"fake_jpeg_bytes").decode()
    body = IncomingMessageBody(
        channel_id="ch_test",
        tg_chat_id=-100123,
        tg_message_id=1,
        text="test",
        route_id="",
        image_b64=b64,
    )
    assert body.image_b64 == b64


def test_post_incoming_message_sets_has_image(tmp_path, monkeypatch):
    """When image_b64 is present, the inserted message row has has_image=1."""
    import base64
    from unittest.mock import MagicMock
    conn = _setup(tmp_path)
    app = build_app(
        conn,
        ai_client=MagicMock(),
        triage_client=MagicMock(),
        profile_ctx=None,
        ai_log_path=tmp_path / "ai.jsonl",
    )
    # Patch process_message to be a no-op so we only test the DB write.
    monkeypatch.setattr("src.api_helpers._run_orchestrator_for_incoming", lambda **kw: None)
    client = TestClient(app)
    b64 = base64.b64encode(b"fake_jpeg").decode()
    r = client.post(
        "/incoming_message",
        json={
            "channel_id": "ch_test",
            "tg_chat_id": -100,
            "tg_message_id": 99,
            "text": "signal text",
            "sender": "analyst",
            "received_at": datetime.now(timezone.utc).isoformat(),
            "is_backfill": False,
            "route_id": "",
            "image_b64": b64,
        },
        headers={"X-Listener-Token": ""},
    )
    assert r.status_code == 202
    row = conn.execute(
        "SELECT has_image FROM messages WHERE tg_message_id=99"
    ).fetchone()
    assert row is not None and row["has_image"] == 1
```

NOTE: `build_app` may require extra params (check the actual signature in `src/api.py`). Look at the existing `build_app` call in other tests and match the parameters. `TestClient` will process the background task synchronously. If the token validation blocks, pass the right token.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py::test_incoming_message_body_image_b64_optional tests/test_api.py::test_incoming_message_body_accepts_image_b64 tests/test_api.py::test_post_incoming_message_sets_has_image -v`
Expected: FAIL (field missing).

- [ ] **Step 3: Add `image_b64` to IncomingMessageBody**

In `src/api_models.py`, in `IncomingMessageBody`, add after `reply_to_tg_message_id`:

```python
    # Base64-encoded JPEG/PNG when the Telegram message included a photo.
    # The image bytes are NOT stored in the DB; only used in-memory during
    # AI processing for the geometric-inconsistency image fallback.
    image_b64: str | None = None
```

- [ ] **Step 4: Decode and pass image_bytes in api_helpers.py**

In `src/api_helpers.py`, in `_run_orchestrator_for_incoming`, add image decoding just before the `try: process_message(...)` call:

```python
    import base64 as _base64
    image_bytes: bytes | None = None
    if body.image_b64:
        try:
            image_bytes = _base64.b64decode(body.image_b64)
        except Exception:
            log.warning(
                "image_b64 decode failed for tg_msg_id=%s — processing without image",
                body.tg_message_id,
            )
```

Then add `image_bytes=image_bytes, has_image=(image_bytes is not None),` to the `process_message(...)` call (after `reply_to_tg_message_id=body.reply_to_tg_message_id`). The parameters don't exist yet on `process_message` — they are added in Task 4; for now this will fail the import but won't break existing tests since we're adding a new keyword arg that isn't there yet.

**IMPORTANT:** `process_message` currently does NOT accept `image_bytes` or `has_image`. Those kwargs are added in Task 4. For now, skip adding them to the call and just add the image decoding block. Add a TODO comment:

```python
    # TODO Task 4: add image_bytes=image_bytes, has_image=(image_bytes is not None)
```

This way Task 2 doesn't fail existing tests waiting for Task 4.

- [ ] **Step 5: Set has_image when inserting the message**

`_insert_message` in `src/orchestrator.py` does the actual INSERT. We need the `has_image` flag to reach it. This is wired in Task 4. For now, this step is a no-op — the column exists (Task 1) and defaults to 0.

- [ ] **Step 6: Run the model tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py::test_incoming_message_body_image_b64_optional tests/test_api.py::test_incoming_message_body_accepts_image_b64 -v`
Expected: PASS (the body model tests pass; skip the has_image DB test for now — it needs Task 4).

- [ ] **Step 7: Full suite (no regressions)**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/api_models.py src/api_helpers.py tests/test_api.py
git commit -m "feat(api): IncomingMessageBody.image_b64 + image_bytes decoding plumbing"
```

---

## Task 3: LLM provider vision support

**Files:**
- Modify: `src/llm_provider.py` (Protocol + both impls)
- Test: `tests/test_llm_provider_vision.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_llm_provider_vision.py`:

```python
"""Tests that llm_provider.interpret() builds correct content blocks for vision."""
import base64
from unittest.mock import MagicMock, patch


def _fake_bytes() -> bytes:
    return b"\xff\xd8\xff" + b"\x00" * 100  # minimal JPEG header


def test_anthropic_interpret_includes_image_block_when_image_bytes_given():
    from src.llm_provider import AnthropicProvider
    captured = {}
    mock_client = MagicMock()
    def fake_create(**kwargs):
        captured["kwargs"] = kwargs
        resp = MagicMock()
        resp.usage.input_tokens = 10
        resp.usage.output_tokens = 5
        resp.usage.cache_read_input_tokens = 0
        resp.usage.cache_creation_input_tokens = 0
        block = MagicMock(); block.type = "text"; block.text = '{"category":"signal"}'
        resp.content = [block]
        return resp
    mock_client.messages.create.side_effect = fake_create
    provider = AnthropicProvider(mock_client, model="claude-sonnet-4-6")
    provider.interpret(
        system_prompt="sys",
        cached_prefix="prefix",
        volatile_suffix="suffix",
        max_output_tokens=256,
        reasoning_level=None,
        image_bytes=_fake_bytes(),
    )
    content = captured["kwargs"]["messages"][0]["content"]
    types = [b["type"] for b in content]
    assert "image" in types
    img_block = next(b for b in content if b["type"] == "image")
    assert img_block["source"]["type"] == "base64"
    assert img_block["source"]["media_type"] == "image/jpeg"
    assert img_block["source"]["data"] == base64.b64encode(_fake_bytes()).decode()


def test_anthropic_interpret_no_image_block_when_image_bytes_none():
    from src.llm_provider import AnthropicProvider
    captured = {}
    mock_client = MagicMock()
    def fake_create(**kwargs):
        captured["kwargs"] = kwargs
        resp = MagicMock()
        resp.usage.input_tokens = 10
        resp.usage.output_tokens = 5
        resp.usage.cache_read_input_tokens = 0
        resp.usage.cache_creation_input_tokens = 0
        block = MagicMock(); block.type = "text"; block.text = '{"category":"signal"}'
        resp.content = [block]
        return resp
    mock_client.messages.create.side_effect = fake_create
    provider = AnthropicProvider(mock_client, model="claude-sonnet-4-6")
    provider.interpret(
        system_prompt="sys",
        cached_prefix="prefix",
        volatile_suffix="suffix",
        max_output_tokens=256,
        reasoning_level=None,
        image_bytes=None,
    )
    content = captured["kwargs"]["messages"][0]["content"]
    types = [b["type"] for b in content]
    assert "image" not in types


def test_openai_interpret_includes_image_url_block_when_image_bytes_given():
    from src.llm_provider import OpenAIProvider
    captured = {}
    mock_client = MagicMock()
    def fake_create(**kwargs):
        captured["kwargs"] = kwargs
        choice = MagicMock()
        choice.message.content = '{"category":"signal"}'
        choice.finish_reason = "stop"
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage.prompt_tokens = 10
        resp.usage.completion_tokens = 5
        resp.usage.prompt_tokens_details = MagicMock(cached_tokens=0)
        return resp
    mock_client.chat.completions.create.side_effect = fake_create
    provider = OpenAIProvider(mock_client, model="gpt-5")
    provider.interpret(
        system_prompt="sys",
        cached_prefix="prefix",
        volatile_suffix="suffix",
        max_output_tokens=256,
        reasoning_level=None,
        image_bytes=_fake_bytes(),
    )
    messages = captured["kwargs"]["messages"]
    user_msg = next(m for m in messages if m["role"] == "user")
    content = user_msg["content"]
    assert isinstance(content, list)
    types = [b["type"] for b in content]
    assert "image_url" in types
    img_block = next(b for b in content if b["type"] == "image_url")
    expected_url = f"data:image/jpeg;base64,{base64.b64encode(_fake_bytes()).decode()}"
    assert img_block["image_url"]["url"] == expected_url
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_llm_provider_vision.py -v`
Expected: FAIL (`interpret()` does not accept `image_bytes`).

- [ ] **Step 3: Add `image_bytes` to the Protocol**

In `src/llm_provider.py`, find the `LLMProvider` Protocol's `interpret` method and add `image_bytes: bytes | None = None` as a keyword-only parameter:

```python
    def interpret(
        self,
        *,
        system_prompt: str,
        cached_prefix: str,
        volatile_suffix: str,
        max_output_tokens: int,
        reasoning_level: str | None,
        image_bytes: bytes | None = None,
    ) -> LLMCallResult: ...
```

- [ ] **Step 4: Add `image_bytes` to AnthropicProvider.interpret**

Add `import base64` near the top of `src/llm_provider.py` (if not already present).

In `AnthropicProvider.interpret`, add `image_bytes: bytes | None = None` to the signature. Then modify the user content list:

Find the lines:
```python
        cached_block = {"type": "text", "text": cached_prefix}
        volatile_block = {"type": "text", "text": volatile_suffix}
        ...
        "messages": [
            {"role": "user", "content": [cached_block, volatile_block]}
        ],
```

Replace with:
```python
        cached_block = {"type": "text", "text": cached_prefix}
        volatile_block = {"type": "text", "text": volatile_suffix}
        user_content: list[dict] = []
        if image_bytes is not None:
            user_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(image_bytes).decode(),
                },
            })
        user_content.extend([cached_block, volatile_block])
        ...
        "messages": [
            {"role": "user", "content": user_content}
        ],
```

- [ ] **Step 5: Add `image_bytes` to OpenAIProvider.interpret**

Add `image_bytes: bytes | None = None` to `OpenAIProvider.interpret` signature. Find:

```python
        user_content = f"{cached_prefix}\n\n{volatile_suffix}"
        ...
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
```

Replace with:
```python
        if image_bytes is not None:
            user_content_body: str | list = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode()}"
                    },
                },
                {"type": "text", "text": f"{cached_prefix}\n\n{volatile_suffix}"},
            ]
        else:
            user_content_body = f"{cached_prefix}\n\n{volatile_suffix}"
        ...
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content_body},
        ],
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_llm_provider_vision.py -v`
Expected: 3 PASS

- [ ] **Step 7: Full suite (no regressions)**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/llm_provider.py tests/test_llm_provider_vision.py
git commit -m "feat(llm): add image_bytes vision support to interpret() for both providers"
```

---

## Task 4: Orchestrator — fallback logic

**Files:**
- Modify: `src/orchestrator.py` (process_message signature + 3 helpers + _insert_message call)
- Create: `tests/test_orchestrator_image.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_orchestrator_image.py`:

```python
"""Unit tests for the image fallback helpers in orchestrator."""
import json
import re
from unittest.mock import MagicMock


# ---- _should_attempt_image_fallback ----

def _make_alert(text: str) -> dict:
    return {"type": "ALERT", "level": "warning", "text": text}


def test_fallback_fires_on_inconsistency_alert_with_image():
    from src.orchestrator import _should_attempt_image_fallback
    actions = [_make_alert("[partial] inconsistent SELL setup: SL 4451 is below entry 4471")]
    assert _should_attempt_image_fallback(
        category="partial_signal",
        actions=actions,
        image_bytes=b"fake",
        fallback_enabled=True,
    ) is True


def test_fallback_fires_on_wrong_side_text():
    from src.orchestrator import _should_attempt_image_fallback
    actions = [_make_alert("BUY SL is on the wrong side of entry")]
    assert _should_attempt_image_fallback(
        category="signal",
        actions=actions,
        image_bytes=b"fake",
        fallback_enabled=True,
    ) is True


def test_fallback_skipped_when_no_image():
    from src.orchestrator import _should_attempt_image_fallback
    actions = [_make_alert("[partial] inconsistent SELL setup")]
    assert _should_attempt_image_fallback(
        category="partial_signal",
        actions=actions,
        image_bytes=None,
        fallback_enabled=True,
    ) is False


def test_fallback_skipped_when_disabled():
    from src.orchestrator import _should_attempt_image_fallback
    actions = [_make_alert("[partial] inconsistent SELL setup")]
    assert _should_attempt_image_fallback(
        category="partial_signal",
        actions=actions,
        image_bytes=b"fake",
        fallback_enabled=False,
    ) is False


def test_fallback_skipped_on_non_alert_actions():
    from src.orchestrator import _should_attempt_image_fallback
    # A CLOSE action alongside an ALERT — not a pure inconsistency, don't try
    actions = [
        {"type": "OPEN", "side": "BUY"},
        _make_alert("[partial] inconsistent"),
    ]
    assert _should_attempt_image_fallback(
        category="signal",
        actions=actions,
        image_bytes=b"fake",
        fallback_enabled=True,
    ) is False


def test_fallback_skipped_on_non_signal_category():
    from src.orchestrator import _should_attempt_image_fallback
    actions = [_make_alert("[partial] inconsistent SELL setup")]
    assert _should_attempt_image_fallback(
        category="context",
        actions=actions,
        image_bytes=b"fake",
        fallback_enabled=True,
    ) is False


def test_fallback_skipped_when_alert_text_not_inconsistency():
    from src.orchestrator import _should_attempt_image_fallback
    actions = [_make_alert("[partial] missing TP")]
    assert _should_attempt_image_fallback(
        category="partial_signal",
        actions=actions,
        image_bytes=b"fake",
        fallback_enabled=True,
    ) is False


# ---- image_corrected flag in action payload ----

def test_image_fallback_pass_returns_actions_on_success(tmp_path):
    """_image_fallback_pass returns (category, [Action]) when provider succeeds."""
    from src.orchestrator import _image_fallback_pass
    from unittest.mock import MagicMock

    # Build a mock LLMCallResult for the provider
    fake_result = MagicMock()
    fake_result.raw_text = json.dumps({
        "category": "signal",
        "actions": [{
            "type": "OPEN", "symbol": "XAUUSD", "side": "SELL",
            "entry_low": 4471.27, "entry_high": 4471.27,
            "sl": 4481.57, "tps": [4419.63],
        }],
    })
    fake_result.usage = {"input_tokens": 20, "output_tokens": 10,
                         "cache_read_tokens": 0, "cache_creation_tokens": 0}
    fake_result.latency_ms = 200

    mock_provider = MagicMock()
    mock_provider.interpret.return_value = fake_result

    ai_mock = MagicMock()
    ai_mock._provider = mock_provider

    result = _image_fallback_pass(
        ai=ai_mock,
        original_alert_text="[partial] inconsistent SELL setup",
        image_bytes=b"fake_jpeg",
        system_prompt="system",
        cached_prefix="prefix",
        volatile_suffix="suffix",
        reasoning_level=None,
        ai_log_path=tmp_path / "ai.jsonl",
    )

    assert result is not None
    category, actions = result
    assert category == "signal"
    from src.validators import OpenAction
    assert any(isinstance(a, OpenAction) for a in actions)
    # image_bytes was passed to provider.interpret
    call_kwargs = mock_provider.interpret.call_args.kwargs
    assert call_kwargs["image_bytes"] == b"fake_jpeg"
    assert _image_fallback_pass.__module__  # sanity


def test_image_fallback_pass_returns_none_on_provider_error(tmp_path):
    """_image_fallback_pass returns None when the provider raises."""
    from src.orchestrator import _image_fallback_pass
    from unittest.mock import MagicMock

    mock_provider = MagicMock()
    mock_provider.interpret.side_effect = RuntimeError("API down")
    ai_mock = MagicMock()
    ai_mock._provider = mock_provider

    result = _image_fallback_pass(
        ai=ai_mock,
        original_alert_text="[partial] inconsistent",
        image_bytes=b"bytes",
        system_prompt="sys",
        cached_prefix="pfx",
        volatile_suffix="sfx",
        reasoning_level=None,
        ai_log_path=tmp_path / "ai.jsonl",
    )
    assert result is None
```

NOTE: This test requires `process_message` to accept `image_bytes` and `has_image` kwargs, `_should_attempt_image_fallback` to be importable, and the AI mock's `provider.interpret` to be callable. Adjust the mock shape to match the actual `AIClient` interface if the test fails with a different attribute error — read `src/ai.py`'s `AIClient.interpret()` call pattern to fix.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_orchestrator_image.py -v`
Expected: FAIL (import errors, missing kwargs).

- [ ] **Step 3: Add `_should_attempt_image_fallback` helper to orchestrator.py**

Add near the top of `src/orchestrator.py` (after imports, before `process_message`):

```python
import re as _re

_INCONSISTENCY_RE = _re.compile(
    r"\[partial\]\s+inconsistent|wrong\s+side",
    _re.IGNORECASE,
)

_IMAGE_FALLBACK_HINT = (
    "\n\n[CHART IMAGE ATTACHED]\n"
    "The text signal produced a geometric inconsistency (SL on the wrong side of entry).\n"
    "A TradingView chart image is attached showing the intended trade setup.\n"
    "- The RED/pink shaded zone is the risk zone; its far boundary is the SL price "
    "(labeled in a red badge on the right price axis).\n"
    "- The PURPLE/lavender shaded zone is the profit zone; its far boundary is the TP "
    "price (labeled in a purple badge).\n"
    "- The boundary line between zones is the entry price.\n"
    "Read the price badge labels from the right axis and emit the corrected OPEN action.\n"
    "If the price labels cannot be read clearly, emit ALERT with text "
    "\"[partial] image unreadable\"."
)


def _should_attempt_image_fallback(
    *,
    category: str,
    actions: list[dict],
    image_bytes: bytes | None,
    fallback_enabled: bool,
) -> bool:
    """True only when ALL conditions for the image fallback are met."""
    if not fallback_enabled:
        return False
    if image_bytes is None:
        return False
    if category not in ("partial_signal", "signal"):
        return False
    if not actions:
        return False
    # All actions must be ALERTs — any OPEN/management action means the
    # first pass succeeded (or produced a different kind of failure).
    if not all(a.get("type") == "ALERT" for a in actions):
        return False
    # At least one ALERT must carry a geometric-inconsistency message.
    return any(
        _INCONSISTENCY_RE.search(a.get("text", ""))
        for a in actions
    )
```

- [ ] **Step 4: Add `image_bytes` and `has_image` kwargs to `process_message`**

In `src/orchestrator.py`, change the `process_message` signature to add at the end of the keyword-only params:

```python
    image_bytes: bytes | None = None,
    has_image: bool = False,
```

Then find the `_insert_message(...)` call (currently `msg_id = _insert_message(conn, tg_message_id, chat_id, sender, text, is_backfill=is_backfill, source_channel_id=source_channel_id, reply_to_tg_message_id=reply_to_tg_message_id)`) and add `has_image=has_image`.

Also update `_insert_message` signature and body:

Find `def _insert_message(conn, tg_message_id, chat_id, sender, text, *, is_backfill=False, source_channel_id="", reply_to_tg_message_id=None)`:

Add `has_image: bool = False` to the kwargs. In the INSERT SQL add `has_image` to the column list and `1 if has_image else 0` to the values tuple:

```python
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages"
        "(tg_message_id, chat_id, sender, text, is_backfill, "
        " source_channel_id, reply_to_tg_message_id, has_image) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (tg_message_id, chat_id, sender, text, 1 if is_backfill else 0,
         source_channel_id or None, reply_to_tg_message_id,
         1 if has_image else 0),
    )
```

- [ ] **Step 5: Add `_image_fallback_pass` to orchestrator.py**

Context: `ai` is an `AIClient` instance. Its `._provider` is an `LLMProvider` (AnthropicProvider or OpenAIProvider). `parse_ai_response` is in `src/validators.py` — it parses raw JSON text into an `AIResponse(actions=[Action, ...], category=str)`. `AICallResult` has `.raw_text`, `.usage`, `.latency_ms`.

After `_should_attempt_image_fallback`, add:

```python
def _image_fallback_pass(
    ai,
    original_alert_text: str,
    image_bytes: bytes,
    system_prompt: str,
    cached_prefix: str,
    volatile_suffix: str,
    reasoning_level,
    ai_log_path,
) -> tuple[str, list] | None:
    """Run a second provider.interpret() call with the chart image attached.

    Returns (category_str, [Action, ...]) on success, or None on any error.
    Calls the provider directly (bypasses ai.call() which has no image param).
    """
    try:
        from src.validators import parse_ai_response
        from src.llm_provider import LLMCallResult
        hint_suffix = volatile_suffix + _IMAGE_FALLBACK_HINT
        result: LLMCallResult = ai._provider.interpret(
            system_prompt=system_prompt,
            cached_prefix=cached_prefix,
            volatile_suffix=hint_suffix,
            max_output_tokens=4096,
            reasoning_level=reasoning_level,
            image_bytes=image_bytes,
        )
        parsed = parse_ai_response(result.raw_text)
        log_call(ai_log_path, {
            "stage": "image_fallback",
            "category": parsed.category or "",
            "action_types": [type(a).__name__ for a in parsed.actions],
            **result.usage,
            "latency_ms": result.latency_ms,
        })
        return parsed.category or "", parsed.actions
    except Exception:
        log.exception("image_fallback_pass failed — falling through to original ALERT")
        return None
```

- [ ] **Step 6: Wire the fallback into process_message**

The AI result is `result` (an `AICallResult`). `result.response.category` is the category string. `result.response.actions` is a list of `Action` objects (Pydantic models). The `_should_attempt_image_fallback` helper needs raw dicts, so convert with `_payload_for(a)` (already defined in orchestrator) and add `"type"`.

In `src/orchestrator.py`, in `process_message`, after the log and CLL capture block (`learning_store.capture(...)`) and BEFORE the `_persist_actions` call, add:

```python
    # Image fallback: if the AI produced a geometric-inconsistency ALERT
    # and the message had a chart image, attempt a corrected second pass.
    if image_bytes is not None:
        _raw_action_dicts = [
            {"type": _action_type(a), **_payload_for(a)}
            for a in result.response.actions
        ]
        _fb_enabled = bool(int(get_setting(conn, "image_fallback_enabled") or "1"))
        if _should_attempt_image_fallback(
            category=result.response.category or "",
            actions=_raw_action_dicts,
            image_bytes=image_bytes,
            fallback_enabled=_fb_enabled,
        ):
            _orig_alert = next(
                (a.get("text", "") for a in _raw_action_dicts if a.get("type") == "ALERT"), ""
            )
            # Build the same cached_prefix / volatile_suffix ai.call() used.
            # These are assembled inside ai.call() — replicate them here.
            # (See src/ai.py AIClient.call for the canonical construction.)
            _recent_chat = _recent_chat_text(conn, chat_id,
                                             int(get_setting(conn, "recent_chat_window") or "20"))
            _cached_prefix = (
                "RECENT CHAT (last messages, oldest first):\n"
                "[BEGIN UNTRUSTED CHANNEL CONTENT]\n"
                f"{_recent_chat}\n"
                "[END UNTRUSTED CHANNEL CONTENT]"
            )
            _volatile_suffix = (
                f"{open_positions_block}\n\nNEW MESSAGE:\n"
                "[BEGIN UNTRUSTED CHANNEL CONTENT]\n"
                f"{sender}: {text}\n"
                "[END UNTRUSTED CHANNEL CONTENT]"
            )
            from src.llm_provider import reasoning_level as _rl
            _level = _rl(ai._thinking_enabled, ai._thinking_budget)
            _system_prompt = profile.system_prompt if profile is not None else None
            _effective_prompt = _system_prompt or ai._system_prompt or None
            # Use the module SYSTEM_PROMPT default if none set
            if not _effective_prompt:
                from src.ai import SYSTEM_PROMPT as _SYSTEM_PROMPT
                _effective_prompt = _SYSTEM_PROMPT
            fb = _image_fallback_pass(
                ai, _orig_alert, image_bytes,
                _effective_prompt, _cached_prefix, _volatile_suffix,
                _level, ai_log_path,
            )
            if fb is not None:
                fb_category, fb_actions = fb
                from src.validators import OpenAction
                has_open = any(isinstance(a, OpenAction) for a in fb_actions)
                if has_open and fb_category in ("signal", "partial_signal"):
                    # Stamp image_corrected into each OPEN action's payload.
                    # Actions are Pydantic models — we need to amend after
                    # _payload_for() serialization in _persist_actions. Do it
                    # by wrapping: subclass or monkey-patch is fragile; instead
                    # pass a payload_extras dict via _persist_actions's existing
                    # mechanism. Simplest: replace the result object.
                    #
                    # Cleanest approach: use payload_extras={} and annotate
                    # OPEN actions directly by building a wrapper Action list.
                    # For simplicity, store the flag in a side-dict and apply
                    # it in _persist_actions via the payload_extras mechanism.
                    # Actually the easiest path: just call _persist_actions with
                    # the fallback actions + an extras dict that adds the flag.
                    from src.orchestrator import _persist_actions  # already in scope
                    _fb_extras = {
                        "image_corrected": True,
                        "image_fallback_reason": _orig_alert,
                    }
                    log.info(
                        "image_fallback succeeded for msg_id=%s — corrected OPEN",
                        msg_id,
                    )
                    ids = _persist_actions(
                        conn, msg_id, fb_actions, ai_log_path,
                        auto_execute_delay_sec, is_backfill=is_backfill,
                        payload_extras=_fb_extras,
                    )
                    _tag_inserted_actions(conn, ids,
                                          source_channel_id=source_channel_id,
                                          route_id=route_id)
                    return ids
    # (fallback did not fire or did not produce a usable OPEN — fall through)
```

**Important lookup before writing this code:** Read `src/orchestrator.py` around line 590 to find the exact names of `open_positions_block`, `context_block`, `sender`, `text` as they are used in the `ai.call(...)` invocation. Also confirm `ai._thinking_enabled`, `ai._thinking_budget`, `ai._system_prompt` are the right attribute names on `AIClient` (see `src/ai.py:AIClient.__init__`). If attribute names differ, use the actual names. The goal is to replicate the same `cached_prefix` / `volatile_suffix` construction that `ai.call()` uses internally.

- [ ] **Step 7: Wire api_helpers.py TODO from Task 2**

Now that `process_message` accepts `image_bytes` and `has_image`, replace the TODO comment in `src/api_helpers.py` with the actual kwargs:

```python
        process_message(
            conn,
            ...
            reply_to_tg_message_id=body.reply_to_tg_message_id,
            image_bytes=image_bytes,
            has_image=(image_bytes is not None),
        )
```

- [ ] **Step 8: Run orchestrator tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_orchestrator_image.py -v`

If `test_image_corrected_flag_in_payload_when_fallback_succeeds` fails because of the AI mock shape, read `src/ai.py` to understand how `AIClient` wraps the provider and adjust the mock accordingly (e.g. if `AIClient.interpret(...)` is the call rather than `ai.provider.interpret(...)`).

Expected: all pass.

- [ ] **Step 9: Full suite (no regressions)**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add src/orchestrator.py src/api_helpers.py tests/test_orchestrator_image.py
git commit -m "feat(orchestrator): image fallback pass on geometric inconsistency ALERTs"
```

---

## Task 5: DM ⚠️ note in render_action_notification

**Files:**
- Modify: `src/telegram_format.py:15-56` (`render_action_notification`)
- Test: `tests/test_telegram_format.py` (new or add to existing)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_telegram_format.py`:

```python
from src.telegram_format import render_action_notification


def _open_payload(image_corrected: bool = False, reason: str = "") -> dict:
    p = {
        "symbol": "XAUUSD", "side": "SELL",
        "entry_low": 4471.27, "entry_high": 4471.27,
        "sl": 4481.57, "tps": [4419.63],
    }
    if image_corrected:
        p["image_corrected"] = True
        p["image_fallback_reason"] = reason or "[partial] inconsistent SELL setup"
    return p


def test_open_dm_has_warning_note_when_image_corrected():
    text = render_action_notification(
        action_id=42,
        action_type="OPEN",
        payload=_open_payload(image_corrected=True),
        source_text="Xauusd sell limit 4471.27 SL 4451.87 Tp 4419.63",
        auto_execute_delay_sec=5,
    )
    assert "⚠️" in text
    assert "corrected from chart image" in text.lower() or "chart image" in text.lower()
    assert "4481.57" in text  # corrected SL present


def test_open_dm_no_warning_note_when_not_image_corrected():
    text = render_action_notification(
        action_id=42,
        action_type="OPEN",
        payload=_open_payload(image_corrected=False),
        source_text="Xauusd sell limit 4471.27 SL 4481.57 Tp 4419.63",
        auto_execute_delay_sec=5,
    )
    assert "corrected from chart image" not in text.lower()


def test_image_corrected_note_shows_original_text():
    reason = "[partial] inconsistent SELL setup: SL 4451 is below entry 4471"
    p = _open_payload(image_corrected=True, reason=reason)
    text = render_action_notification(
        action_id=1, action_type="OPEN", payload=p,
        source_text="test", auto_execute_delay_sec=0,
    )
    # The reason (or a truncated form) should appear so the operator
    # knows why the fallback was triggered.
    assert "inconsistent" in text.lower() or "wrong side" in text.lower() or "below entry" in text.lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_telegram_format.py -v`
Expected: FAIL (no ⚠️ note for image_corrected).

- [ ] **Step 3: Update render_action_notification**

In `src/telegram_format.py`, in `render_action_notification`, find the `OPEN` branch:

```python
    if action_type == "OPEN":
        tps = " / ".join(str(t) for t in payload.get("tps", []))
        return (
            f"🟢 NEW SIGNAL #{action_id}  (auto-execute {when})\n\n"
            f"OPEN {payload['side']} {payload['symbol']}\n"
            f"Entry: {payload['entry_low']}–{payload['entry_high']}\n"
            f"SL:    {payload['sl']}\n"
            f"TPs:   {tps}\n\n"
            f"Source: \"{source_text[:120]}\"{reply}"
        )
```

Replace with:

```python
    if action_type == "OPEN":
        tps = " / ".join(str(t) for t in payload.get("tps", []))
        image_note = ""
        if payload.get("image_corrected"):
            reason = payload.get("image_fallback_reason", "")
            reason_snippet = (reason[:100] + "…") if len(reason) > 100 else reason
            image_note = (
                f"⚠️ Values corrected from chart image\n"
                f"   (text had: {reason_snippet})\n\n"
            )
        return (
            f"🟢 NEW SIGNAL #{action_id}  (auto-execute {when})\n\n"
            f"{image_note}"
            f"OPEN {payload['side']} {payload['symbol']}\n"
            f"Entry: {payload['entry_low']}–{payload['entry_high']}\n"
            f"SL:    {payload['sl']}\n"
            f"TPs:   {tps}\n\n"
            f"Source: \"{source_text[:120]}\"{reply}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_telegram_format.py -v`
Expected: all PASS.

- [ ] **Step 5: Full suite (no regressions)**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_format.py tests/test_telegram_format.py
git commit -m "feat(bot): prefix image-corrected OPEN DMs with chart-correction warning"
```

---

## Task 6: Listener — download and attach photo

**Files:**
- Modify: `src/listener.py` (both the API-dispatch path ~line 556 and the direct-call path ~line 590)

This task has no hermetic unit tests (the Telethon media download requires a live TG session). Verification is import-smoke + code review.

- [ ] **Step 1: Add import for base64 at the top of listener.py**

In `src/listener.py`, check if `import base64` is already present. If not, add it near the other stdlib imports.

- [ ] **Step 2: Extract photo bytes in the NewMessage handler (API-dispatch path)**

In the `handler` function in `src/listener.py`, after:
```python
text = msg.message or ""
```
Add:
```python
        image_b64: str | None = None
        _MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB guard
        if msg.photo or (msg.media and hasattr(msg.media, "photo")):
            try:
                raw_img = await msg.download_media(bytes)
                if raw_img and len(raw_img) <= _MAX_IMAGE_BYTES:
                    image_b64 = base64.b64encode(raw_img).decode()
                elif raw_img:
                    log.warning(
                        "tg_msg_id=%s photo too large (%d bytes) — skipping image fallback",
                        msg.id, len(raw_img),
                    )
            except Exception:
                log.warning(
                    "tg_msg_id=%s photo download failed — continuing without image",
                    msg.id, exc_info=True,
                )
```

Then, in the `_post_incoming_message(...)` call body dict, add `"image_b64": image_b64` (where the other body fields like `"text"`, `"sender"` are assembled).

- [ ] **Step 3: Pass image_bytes in the direct-call path**

In the direct `process_message(...)` call (~line 590, inside `else:` when `dispatch_target is None`):

First, add the same photo-download block right after `text = msg.message or ""` (same code as Step 2 but storing `image_bytes` as `bytes | None` instead of base64):

```python
        image_bytes_direct: bytes | None = None
        if msg.photo or (msg.media and hasattr(msg.media, "photo")):
            try:
                raw_img = await msg.download_media(bytes)
                if raw_img and len(raw_img) <= _MAX_IMAGE_BYTES:
                    image_bytes_direct = raw_img
                elif raw_img:
                    log.warning(
                        "tg_msg_id=%s photo too large (%d bytes) — skipping",
                        msg.id, len(raw_img),
                    )
            except Exception:
                log.warning(
                    "tg_msg_id=%s photo download failed",
                    msg.id, exc_info=True,
                )
```

Then add `image_bytes=image_bytes_direct, has_image=(image_bytes_direct is not None),` to the `process_message(...)` lambda.

NOTE: `_MAX_IMAGE_BYTES` is already defined in the API-dispatch block above in this handler — if both paths are in the same function, you can define it once at the start of the handler.

- [ ] **Step 4: Import-smoke check**

Run: `.venv\Scripts\python.exe -c "import src.listener; print('ok')"`
Expected: `ok` (no import errors).

- [ ] **Step 5: Full suite (no regressions)**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/listener.py
git commit -m "feat(listener): download and pass chart image bytes for image fallback"
```

---

## Task 7: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add image fallback to the orchestrator bullet**

In `CLAUDE.md`, find the bullet for `src/orchestrator.py`. After the existing description, append:

```
`_should_attempt_image_fallback` detects when all actions are geometric-inconsistency ALERTs and the message had a chart image; `_image_fallback_pass` runs a second `interpret()` call with the image attached. On a corrected OPEN, `image_corrected=True` and `image_fallback_reason` are written into the action payload; the operator DM prefixes a ⚠️ note via `render_action_notification`. Toggle with `image_fallback_enabled` setting (default on).
```

- [ ] **Step 2: Add image_bytes to the llm_provider bullet**

Find the bullet for `src/llm_provider.py` (or the relevant conventions section) and add:

```
`LLMProvider.interpret()` accepts an optional `image_bytes: bytes | None` parameter; both `AnthropicProvider` and `OpenAIProvider` prepend an image content block when provided (vision pass for the image fallback).
```

- [ ] **Step 3: Final regression gate**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: all pass (baseline + new tests).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document image fallback pipeline in CLAUDE.md"
```

---

## Self-Review Coverage Map

| Spec requirement | Task |
|---|---|
| `has_image` column + migration | 1 |
| `image_fallback_enabled` default on | 1 |
| `IncomingMessageBody.image_b64` optional (backward compat) | 2 |
| `has_image=1` persisted in messages | 2, 4 |
| LLM provider vision blocks (Anthropic + OpenAI) | 3 |
| `_should_attempt_image_fallback` narrow trigger | 4 |
| `_IMAGE_FALLBACK_HINT` zone-aware prompt | 4 |
| `_image_fallback_pass` with error handling / fall-through | 4 |
| `image_corrected`/`image_fallback_reason` in action payload | 4 |
| ⚠️ note in render_action_notification | 5 |
| Listener photo download + size guard + error swallow | 6 |
| API-dispatch and direct-call paths both covered | 6 |
| CLAUDE.md | 7 |
| Toggle gate tested | 4 |
| `image_bytes` never stored in DB | convention — enforced by design (no DB write) |
| image > 10 MB discarded | 6 (size guard) |
