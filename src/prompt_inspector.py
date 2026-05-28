"""Render the four AI prompts in the system for read-only inspection.

The Prompts view (src/gui/views/prompts_view.py) consumes this module
to show operators exactly what the AI sees. No side effects; purely
returns strings. Two modes:

  - "demo": uses hardcoded fixtures so the view is never empty.
  - "live": pulls the latest real message + state from the active
    stack's DB. Falls back to demo if no data yet.

Adding a new prompt: extend PROMPT_IDS, add a render_<id>(...) function,
and route it in render().
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


Mode = Literal["demo", "live"]


@dataclass(frozen=True)
class RenderedPrompt:
    title: str
    system_prompt: str
    user_content: str
    expected_output: str
    notes: str = ""


PROMPT_IDS = ("interpreter", "triage", "evaluator", "discovery")


# --- Demo fixtures --------------------------------------------------------
#
# Generalised, channel-agnostic samples so the inspector is presentable
# regardless of which stack the operator is looking at. Earlier versions
# used Arabic phrasing and channel-specific tags from one stack, which
# read as a debug snippet rather than a neutral example. The shapes
# below mirror what a real OPEN signal + chat + system-state block look
# like, just in plain English with anonymised tags.

_DEMO_MESSAGE = "Secure your entry, take half profit, ride the rest to target."

_DEMO_RECENT_CHAT = (
    "[02:14:33] SignalChannel: GOLD SELL @ 4808-4806 / "
    "TP1 4795 / TP2 4780 / TP3 4760 / SL 4820\n"
    "[02:16:01] SignalChannel: Order placed.\n"
    "[02:24:18] SignalChannel: TP1 hit — close half and trail."
)

_DEMO_STATE_BLOCK = """OPEN POSITIONS (XAUUSD):
  ticket=10000001 side=SELL entry=4807.00 sl=4820.00 tp=4760.00
    vol=0.08 of orig=0.08  partials_taken=0  at_BE=false  moved=false  age=10m

PENDING OPEN SIGNALS: (none)

LAST CLOSED POSITION (XAUUSD, within 24h): (none)

MARKET (XAUUSD): bid=4794.20 ask=4794.35 mid=4794.275 age=4s"""

_DEMO_OPEN_PAYLOAD = {
    "symbol": "XAUUSD",
    "side": "SELL",
    "entry_low": 4806.0,
    "entry_high": 4808.0,
    "sl": 4820.0,
    "tps": [4795.0, 4780.0, 4760.0],
    "comment": "SignalChannel",
}


# --- Helpers --------------------------------------------------------------


def _safe_read_settings(db_path: Path) -> dict[str, str]:
    if not db_path.exists():
        return {}
    try:
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT key, value FROM settings"
            ).fetchall()
        return dict(rows)
    except sqlite3.OperationalError:
        return {}


def _latest_message_from_db(db_path: Path) -> tuple[str, int | None]:
    """Return (text, msg_id) of the most recent message, or ("", None)."""
    if not db_path.exists():
        return ("", None)
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT id, text FROM messages "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except sqlite3.OperationalError:
        return ("", None)
    if row is None:
        return ("", None)
    return (str(row[1] or ""), int(row[0]))


def _latest_open_payload(db_path: Path) -> dict:
    if not db_path.exists():
        return {}
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT payload_json FROM actions "
                "WHERE action_type IN ('OPEN','OPEN_INSTANT') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if row is None or not row[0]:
        return {}
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return {}


def _live_state_block(db_path: Path) -> str:
    """Build a SYSTEM STATE block from the live DB, if reachable.

    When the most recent message in the archive is a Telegram reply,
    seed the renderer with that message's tg_message_id + chat_id so
    the REPLY CONTEXT block shows up in the preview — gives the
    operator a concrete idea of what the AI sees when interpreting
    "cancel that order"-style replies. Falls back silently when no
    such message exists.
    """
    if not db_path.exists():
        return ""
    try:
        # state_summary needs a configured config.DB_PATH; the listener
        # process already does this. Here in the GUI we pass an explicit
        # connection to render the block directly.
        from src import state_summary
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            recent_reply = conn.execute(
                "SELECT tg_message_id, chat_id FROM messages "
                "WHERE reply_to_tg_message_id IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            kwargs = {}
            if recent_reply is not None:
                kwargs["current_tg_message_id"] = recent_reply["tg_message_id"]
                kwargs["current_chat_id"] = recent_reply["chat_id"]
            return state_summary.render_open_positions(conn, **kwargs)
    except Exception as e:  # noqa: BLE001
        return f"(failed to render live state: {e})"


# --- Individual prompt renderers -----------------------------------------


def _render_interpreter(
    db_path: Path, mode: Mode, profile_name: str | None = None,
) -> RenderedPrompt:
    from src import ai
    try:
        system = _render_system_for_profile(profile_name) or "(profile not configured yet)"
    except Exception as e:  # noqa: BLE001
        system = f"(failed to render: {type(e).__name__}: {e})"

    if mode == "live":
        msg, _ = _latest_message_from_db(db_path)
        state = _live_state_block(db_path)
        if not msg:
            msg = _DEMO_MESSAGE + "  (no live message yet — falling back to demo)"
        if not state:
            state = _DEMO_STATE_BLOCK + "  (no live state yet — falling back to demo)"
        recent_chat = "(live recent-chat rendering not surfaced here yet)"
    else:
        msg = _DEMO_MESSAGE
        state = _DEMO_STATE_BLOCK
        recent_chat = _DEMO_RECENT_CHAT

    user_content = (
        "RECENT CHAT (last messages, oldest first):\n"
        "[BEGIN UNTRUSTED CHANNEL CONTENT]\n"
        f"{recent_chat}\n"
        "[END UNTRUSTED CHANNEL CONTENT]\n"
        "\n"
        f"{state}\n"
        "\n"
        "NEW MESSAGE:\n"
        "[BEGIN UNTRUSTED CHANNEL CONTENT]\n"
        f"{msg}\n"
        "[END UNTRUSTED CHANNEL CONTENT]"
    )

    expected = (
        "JSON list of action objects, e.g.:\n"
        '  [{"type":"MOVE_SL_BE"}, {"type":"CLOSE_PARTIAL","fraction":0.5}]\n'
        "Or an empty list for context/ignore."
    )

    return RenderedPrompt(
        title="Interpreter (main per-message AI)",
        system_prompt=system,
        user_content=user_content,
        expected_output=expected,
        notes="The first block (recent chat) is cache-eligible. The system "
              "prompt is cached too. Volatile fields: system state + new message.",
    )


def _render_triage(
    db_path: Path, mode: Mode, profile_name: str | None = None,
) -> RenderedPrompt:
    from src import ai_triage
    try:
        system = _render_triage_for_profile(profile_name) or "(profile not configured yet)"
    except Exception as e:  # noqa: BLE001
        system = f"(failed to render: {type(e).__name__}: {e})"

    if mode == "live":
        msg, _ = _latest_message_from_db(db_path)
        if not msg:
            msg = _DEMO_MESSAGE
    else:
        msg = _DEMO_MESSAGE

    user_content = (
        "Decide whether to KEEP this message for interpretation or IGNORE it.\n"
        "\n"
        f"MESSAGE: {msg}\n"
        "\n"
        f"OPEN_POSITIONS_COUNT: 1"
    )
    expected = 'JSON: {"decision":"keep"|"ignore", "reason":"<short>"}'

    return RenderedPrompt(
        title="Triage (cheap pre-filter, binary keep / ignore)",
        system_prompt=system,
        user_content=user_content,
        expected_output=expected,
        notes="Runs on every incoming message before the interpreter. "
              "Cuts ~70% of interpreter calls on noisy channels.",
    )


def _render_evaluator(
    db_path: Path, mode: Mode, profile_name: str | None = None,  # noqa: ARG001
) -> RenderedPrompt:
    from src.ai_evaluator import EVALUATOR_SYSTEM_PROMPT

    if mode == "live":
        payload = _latest_open_payload(db_path) or _DEMO_OPEN_PAYLOAD
    else:
        payload = _DEMO_OPEN_PAYLOAD

    user_content = (
        "SIGNAL UNDER EVALUATION:\n"
        f"  symbol={payload.get('symbol', 'XAUUSD')}\n"
        f"  side={payload.get('side', 'BUY')}\n"
        f"  entry={payload.get('entry_low', '—')}-{payload.get('entry_high', '—')}\n"
        f"  sl={payload.get('sl', '—')}\n"
        f"  tps={payload.get('tps', [])}\n"
        "\n"
        "(plus runtime-built market context block: D1/H4/H1 candles, "
        "DXY, VIX, TNX, ATR, ADR, news veto, session info — assembled per call)"
    )
    expected = (
        'JSON: {"score":0-100, "verdict":"strong|moderate|weak|avoid", '
        '"key_factor":"<one line>", "summary":"<paragraph>", '
        '"factors":{"T1":"...","L1":"..."}, "data_quality":"full|partial"}'
    )

    return RenderedPrompt(
        title="Evaluator (directional-bias scorer, runs after every OPEN)",
        system_prompt=EVALUATOR_SYSTEM_PROMPT,
        user_content=user_content,
        expected_output=expected,
        notes="Informational only — does NOT gate trade execution. "
              "The trade is already queued by the time this fires.",
    )


def _render_discovery(
    db_path: Path, mode: Mode, profile_name: str | None = None,  # noqa: ARG001
) -> RenderedPrompt:
    from src.ai_discovery import _render_discovery_prompt
    msg = _DEMO_MESSAGE
    if mode == "live":
        live_msg, _ = _latest_message_from_db(db_path)
        if live_msg:
            msg = live_msg
    # Try to load real channel context for inspection so the operator
    # sees the prompt that would actually be sent. Falls back to
    # defaults (XAUUSD, no language note, no price hint) when profile
    # is missing.
    context: dict = {}
    try:
        import json
        profile_path = db_path.parent / "profile.json"
        if profile_path.exists():
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            context = {
                "symbol": data.get("symbol", ""),
                "language_note": data.get("language_note", ""),
            }
    except Exception:
        context = {}
    rendered = _render_discovery_prompt(msg, context)
    expected = (
        'JSON: {"action_types":["<bucket>"], "pending":true|false|null, '
        '"phrase":"<verbatim>", "reasoning":"<short>", "confidence":0.0-1.0}'
    )
    return RenderedPrompt(
        title="Discovery classifier (single-message — wizard / bulk-import)",
        system_prompt="Reply with strict JSON only. No code fences.",
        user_content=rendered,
        expected_output=expected,
        notes="Used by the profile generator wizard (after the triage gate) "
              "and the Triggers tab's Bulk import. Substitutes symbol / "
              "language_note from profile.json when available; falls "
              "back to XAUUSD/empty otherwise.",
    )


# --- Dispatcher -----------------------------------------------------------


_RENDERERS = {
    "interpreter": _render_interpreter,
    "triage": _render_triage,
    "evaluator": _render_evaluator,
    "discovery": _render_discovery,
}


def render(
    prompt_id: str, db_path: Path, mode: Mode = "demo",
    profile_name: str | None = None,
) -> RenderedPrompt:
    """Dispatch to per-prompt renderer.

    ``profile_name`` (added in the post-v2 Prompts/Playground gap fix):
    when provided, the interpreter + triage renderers load THAT profile's
    JSON and pass it through ``_render_system_prompt_from_data`` instead
    of the module-level ``config.CHANNEL_PROFILE`` global. ``None``
    preserves the legacy behavior for callers that haven't migrated.
    """
    fn = _RENDERERS.get(prompt_id)
    if fn is None:
        return RenderedPrompt(
            title=f"(unknown prompt id: {prompt_id})",
            system_prompt="",
            user_content="",
            expected_output="",
        )
    return fn(db_path, mode, profile_name)


def _render_system_for_profile(profile_name: str | None) -> str:
    """Render the interpreter SYSTEM_PROMPT for a specific profile name.

    Falls back to the global ``ai._render_system_prompt()`` (which reads
    ``config.CHANNEL_PROFILE``) when ``profile_name`` is None — preserves
    behavior for non-Prompts-view callers.
    """
    from src import ai
    if not profile_name:
        return ai._render_system_prompt() or ""
    # Load the picked profile's JSON via profile_io (handles APPDATA vs
    # legacy channels/ paths). Pass the parsed dict to the from_data
    # renderer — no globals touched.
    from src.gui.services import profile_io
    data = profile_io.load_profile(profile_name)
    if not data:
        return ""
    return ai._render_system_prompt_from_data(dict(data))


def _render_triage_for_profile(profile_name: str | None) -> str:
    """Render the triage SYSTEM_PROMPT for a specific profile name.

    Same shape as ``_render_system_for_profile`` but for the triage stage.
    """
    from src import ai_triage
    if not profile_name:
        return ai_triage._render_triage_prompt() or ""
    from src.gui.services import profile_io
    data = profile_io.load_profile(profile_name)
    if not data:
        return ""
    return ai_triage._render_triage_prompt_from_data(dict(data))


def estimate_tokens(text: str) -> int:
    """Rough token estimate: 1 token ~= 4 chars for English/Latin.
    Arabic and other dense scripts may be different but this is a guide,
    not an exact count.
    """
    return max(1, len(text) // 4)
