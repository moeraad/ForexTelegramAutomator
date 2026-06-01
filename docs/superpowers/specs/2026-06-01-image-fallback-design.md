# Chart Image Fallback for Geometric Signal Inconsistencies

**Date:** 2026-06-01
**Status:** Approved (design)
**Branch:** feat/image-fallback

## Problem

When a Telegram signal message contains a typo in a price value (e.g. SL digits transposed: `4451.87` instead of `4481.57`), the AI interpreter correctly reads the typo-d number but cannot fix it ("NEVER rewrite an explicit full-digit number") and emits a `partial_signal` ALERT: `[partial] inconsistent SELL setup: SL 4451.87 is below entry 4471.27`. The chart image attached to the same message shows the correct setup unambiguously via TradingView's colored zone rendering — but the image is currently silently ignored.

**Observed incident:** `SELL LIMIT 4471.27 SL 4451.87 TP 4419.63` with chart showing SL badge `4,481.57` in a red box above entry, TP badge `4,419.63` in a purple box below entry. Text said `4451.87`; chart says `4,481.57` (digits `51.87` ↔ `81.57`).

## Goals

- When a signal fails due to a **geometric inconsistency** (SL on wrong side of entry) AND the original message had a chart image, run a second AI interpreter pass with the image attached.
- If the image pass resolves the inconsistency and produces a valid OPEN: use those values, mark the action as image-corrected, and DM the operator with a ⚠️ note.
- If the image pass also fails: fall through to the original ALERT behavior. Never silently swallow.
- Settings toggle to disable the fallback (`image_fallback_enabled`, default on).

## Non-Goals

- Vision on every message (cost). Fallback only.
- OCR / third-party image processing. Use the same LLM interpreter.
- Storing chart images in the DB. In-memory only during processing.
- Fallback for non-geometric failures (missing SL, ambiguous text, wrong symbol, etc.).
- Non-TradingView chart formats are not explicitly excluded but are not the primary target.

## Trigger Condition (narrow + precise)

The image fallback fires when ALL of:
1. `image_fallback_enabled` setting is truthy (default `1`).
2. The message had a photo (`has_image=True`), AND image bytes were successfully downloaded.
3. The first AI pass produced `category in ("partial_signal", "signal")`.
4. All emitted actions are `ALERT` type (no OPEN, no management action).
5. At least one ALERT's text matches the geometric-inconsistency pattern: starts with `[partial] inconsistent` OR contains `wrong side` (case-insensitive).

This is intentionally narrow. Most ALERTs are genuinely ambiguous messages that an image cannot resolve. Only geometric inconsistencies on otherwise-structured signals benefit from image correction.

## Architecture & Data Flow

```
Telegram message (text + photo)
  ↓ listener.py
  download photo → bytes (in-memory, ~200-500KB JPEG)
  base64-encode → image_b64 str
  POST /incoming_message  {text, ..., image_b64: "..."}
  ↓ api_helpers._run_orchestrator_for_incoming
  base64.b64decode(body.image_b64) → image_bytes
  process_message(conn, ..., image_bytes=image_bytes)
  ↓ orchestrator.process_message
  FIRST PASS: interpret(text only) → partial_signal + ALERT [partial] inconsistent
  CHECK: trigger condition? YES (image_bytes present, inconsistency ALERT)
  SECOND PASS: interpret(text + chart image + fallback hint)
    → either:  valid OPEN action  → use it, image_corrected=True in pipeline_meta
    → or:      ALERT again        → fall through to original ALERT
  ↓ telegram_format.render_action_notification (OPEN case)
  "🟢 NEW SIGNAL #N  (auto-execute in Xs)
   ⚠️ Values corrected from chart image (text: [partial] inconsistent ...)
   OPEN SELL XAUUSD
   Entry: 4471.27–4471.27
   SL:    4481.57
   TPs:   4419.63
   Source: "Xauusd sell limit 4471.27 SL 4451.87 Tp 4419.63""
```

## Components & Changes

### 1. `src/schema.sql` + `src/db.py` — `has_image` column

```sql
ALTER TABLE messages ADD COLUMN has_image INTEGER NOT NULL DEFAULT 0;
```

Migration: `_migrate_messages_add_has_image` (idempotent, added to `init_schema`).

### 2. `src/api_models.py` — `IncomingMessageBody.image_b64`

```python
image_b64: str | None = None  # base64-encoded JPEG/PNG when message had photo
```

Optional with `None` default — backward compatible with all existing callers.

### 3. `src/listener.py` — download and encode photo

In the `NewMessage` handler and backfill path, detect `msg.photo or (msg.media and hasattr(msg.media, 'photo'))`. If present: `await msg.download_media(bytes)` → base64-encode → pass as `image_b64` in the `IncomingMessageBody` POST. Swallow any download error (log + continue without image — never block message processing on a failed image download).

In the direct `process_message` path (non-API dispatch): pass `image_bytes` directly.

**Size guard:** if the downloaded bytes exceed 10 MB, log a warning and discard (set `image_b64 = None`). Prevents unexpectedly large media from bloating the HTTP body.

### 4. `src/api_helpers.py` — decode and pass image_bytes

In `_run_orchestrator_for_incoming`: decode `body.image_b64` to `bytes` and pass as `image_bytes=` to `process_message`. Decoding errors → log + pass `None`.

Also: set `has_image = 1` when inserting/updating the messages row (the messages INSERT currently happens inside `process_message` via `_insert_message` — the `has_image` flag needs to reach there too, so add `has_image: bool = False` to `process_message`'s signature and propagate to `_insert_message`).

### 5. `src/llm_provider.py` — vision content block in `interpret()`

Add `image_bytes: bytes | None = None` to the `LLMProvider.interpret()` Protocol and both concrete implementations.

**Anthropic:** When `image_bytes` is present, prepend an image content block to the user message:
```python
image_block = {
    "type": "image",
    "source": {
        "type": "base64",
        "media_type": "image/jpeg",  # TradingView exports JPEG
        "data": base64.b64encode(image_bytes).decode(),
    }
}
# user content becomes: [image_block, cached_block, volatile_block]
```

**OpenAI:** When `image_bytes` is present, build the content as a list:
```python
[
    {"type": "image_url", "image_url": {
        "url": f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode()}"
    }},
    {"type": "text", "text": volatile_suffix}
]
```

`cached_prefix` handling: for the image pass, the user content shape changes — prepend the image block before the text. The system prompt (and its cache_control) is unchanged.

### 6. `src/orchestrator.py` — fallback logic

Add `image_bytes: bytes | None = None` and `has_image: bool = False` to `process_message`.

After the first AI pass, add:
```python
if _should_attempt_image_fallback(result, image_bytes, conn):
    result = _image_fallback_pass(
        ai, result, image_bytes, system_prompt, cached_prefix, volatile_suffix,
        reasoning_level, max_output_tokens, conn
    )
    # result is now either the corrected OPEN or the original ALERT (unchanged)
```

`_should_attempt_image_fallback(result, image_bytes, conn) -> bool`:
- Reads `image_fallback_enabled` from settings (truthy check).
- `image_bytes is not None`.
- `result.category in ("partial_signal", "signal")`.
- All actions are ALERT type AND at least one ALERT.text matches `_INCONSISTENCY_RE = re.compile(r'\[partial\] inconsistent|wrong side', re.IGNORECASE)`.

`_image_fallback_pass(...)`:
- Appends to `volatile_suffix`:
  ```
  \n\n[CHART IMAGE ATTACHED]
  The text signal produced a geometric inconsistency (SL on the wrong side of entry).
  A TradingView chart image is attached showing the intended trade setup.
  - The RED/pink shaded zone is the risk zone; its far boundary is the SL price (labeled in a red badge on the right axis).
  - The PURPLE/lavender shaded zone is the profit zone; its far boundary is the TP price (labeled in a purple badge).
  - The zone boundary line is the entry price.
  Read the price badge labels from the right axis and emit the corrected OPEN action.
  If the price labels cannot be read clearly, emit ALERT with text "[partial] image unreadable".
  ```
- Calls `ai.interpret(... image_bytes=image_bytes)`.
- Parses and validates the result through the existing action validator.
- If valid OPEN produced: sets `pipeline_meta["image_corrected"] = True`, `pipeline_meta["image_fallback_reason"] = <original alert text>`.
- If still ALERT or fails validation: returns the original result unchanged (logs the failure at INFO level).

### 7. `src/telegram_format.py` — ⚠️ note in `render_action_notification`

`render_action_notification` gains an optional `image_corrected: bool = False` parameter. When `True`, the OPEN notification prepends a warning line:

```
🟢 NEW SIGNAL #42  (auto-execute in 5s)
⚠️ Values corrected from chart image (text had geometric inconsistency)

OPEN SELL XAUUSD
Entry: 4471.27–4471.27
SL:    4481.57
TPs:   4419.63

Source: "Xauusd sell limit 4471.27 SL 4451.87 Tp 4419.63"
```

All existing callers pass `image_corrected=False` by default — no change needed.

### 8. `src/db.py` — `image_fallback_enabled` settings default

In `_migrate_seed_settings_defaults`: add `("image_fallback_enabled", "1")`.

## Cost Profile

Image tokens for a TradingView JPEG (~400×700px) ≈ 800–1200 input tokens on Anthropic, ~500–800 on OpenAI. At current pricing: ~$0.003–0.005 per fallback call. Fires only on geometric inconsistency with an image present — a rare case. Negligible marginal cost.

## Error Handling

| Failure | Behavior |
|---|---|
| Image download fails | `image_bytes=None`, fallback never triggered, original ALERT DM sent |
| Image > 10 MB | Discarded (size guard), same as above |
| Image pass fails validation | Original ALERT result used; fallback attempt logged at INFO |
| Image pass returns ALERT `[partial] image unreadable` | Falls through to original ALERT DM |
| `image_fallback_enabled = 0` | Fallback skipped entirely |
| Provider doesn't support vision | Log warning; fall through (both Claude and GPT-4o do support vision) |

## Testing

Hermetic:
- `_should_attempt_image_fallback`: parametrize over category/action-types/text patterns, confirm only the right combinations trigger.
- `render_action_notification` with `image_corrected=True`: confirm ⚠️ line present.
- `POST /incoming_message` with `image_b64`: confirm `has_image=1` in messages table.
- `IncomingMessageBody` backward compat: no `image_b64` field → `None`, no error.
- `_pending_risk` style unit test for the `_image_fallback_pass` decision logic (mock AI client returns a corrected OPEN → `image_corrected=True` in meta; mock returns ALERT → original result unchanged).

Live replay:
- `tests/test_replay.py` and `tests/test_management_replay.py` are unaffected (no image passed in those fixtures; fallback never triggers).

Manual:
- Send the exact failing signal `SELL LIMIT 4471.27 SL 4451.87 TP 4419.63` with the TradingView chart image → confirm DM shows corrected SL `4481.57` with ⚠️ note.
- Set `image_fallback_enabled=0` → confirm fallback does not fire, ALERT DM sent instead.

## Conventions

- `image_bytes` is never stored in the DB. In-memory only during message processing.
- `has_image` in the messages table is the only persistent artifact (audit trail).
- `pipeline_meta_json["image_corrected"]` and `["image_fallback_reason"]` carry the audit into the actions table.
- All timestamps remain ISO-8601 UTC with `+00:00`.
- The fallback path must NEVER raise an unhandled exception that disrupts the main pipeline. Every step is wrapped in try/except with log + fall-through.
