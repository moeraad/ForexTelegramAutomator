"""Typed accessors over the `settings` table.

All app config (except DB_PATH itself) flows through here. Secrets are
DPAPI-encrypted in storage; the get_secret / set_secret helpers wrap that
transparently. Critical keys have no default — call sites check
`is_set(key)` or rely on `missing_critical_keys()` to decide whether
the app can run.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src.secret_box import decrypt, encrypt


# Keys that must be present before any service can start.
CRITICAL_KEYS = (
    "tg_api_id",
    "tg_api_hash",
    "tg_phone",
    "tg_watched_chat_id",
    "tg_bot_token",
    "ai_provider",
)

# Keys whose values are encrypted at rest.
SECRET_KEYS = frozenset({
    "tg_api_hash",
    "tg_bot_token",
    "tg_session_blob",
    "anthropic_api_key",
    "openai_api_key",
    "ea_shared_token",
})

# Sensible defaults for non-critical keys. Seeded once on empty DB.
DEFAULT_SETTINGS: dict[str, str] = {
    "api_host": "127.0.0.1",
    "api_port": "8765",
    "channel_profile": "",
    "anthropic_model": "claude-sonnet-4-6",
    "openai_model": "gpt-5",
    "openai_triage_model": "gpt-5-nano",
    "ai_triage_model": "claude-haiku-4-5-20251001",
    "ai_triage_enabled": "1",
    "ai_thinking_enabled": "1",
    "ai_thinking_budget_tokens": "4000",
    "signal_memory_enabled": "1",
    "signal_memory_max_entries": "10",
    "signal_memory_max_age_hours": "4",
    "fingerprint_band_price": "5.0",
    "fingerprint_window_hours": "6",
    "backfill_max_age_min": "30",
    "default_auto_execute_delay_sec": "0",
    "recent_chat_window": "20",
    "cost_daily_budget_usd": "5.00",
    "cost_cap_multiplier": "1.2",
    "bot_telegram_ok_at": "",
    "listener_telegram_ok_at": "",
    # Trading style: drives evaluator rubric weights, scored horizons,
    # and which data feeds need to be fresh. The closed set lives in
    # src.trading_style.TradingStyle; this string is the .value of the
    # enum member. Default to intraday — covers most Telegram channels'
    # typical hold window.
    # Which evaluator implementation to call. "v1" = the legacy
    # ai_evaluator (15-axis LLM-as-judge). "v2" = the new layered
    # implementation (deterministic scorers + regime tag + LLM
    # synthesizer for forward estimates). Default v2 — but flip back
    # to v1 in production if v2 produces obviously bad scores during
    # the rollout window.
    "evaluator_version": "v2",
    # Per-trade SL risk cap. The EA computes the dollar loss that would
    # be taken if the signal's stop-loss hits at the baseline lot size;
    # if that loss exceeds `max_sl_loss_percent` of the account balance,
    # the lot is shrunk to keep the worst-case loss at the cap. When
    # even minLot can't fit under the cap (very wide SL), the trade is
    # rejected with reason `sl_too_wide_for_max_risk_pct`. Default 1%
    # is conservative for retail accounts; raise carefully — losing 5%
    # on a single trade requires 20 winners to recover.
    "max_sl_loss_percent": "1.0",
    # Break-even base price selection. `entry_fill` (default) uses the
    # position's actual fill price — what's been there forever. The
    # alternative `signal_zone` reads the signal's entry_low/entry_high
    # off the registered plan and anchors BE to a position within that
    # zone, picked by `be_signal_zone_position` below. The cost offset
    # (commission + swap) layers on top of whichever base is chosen.
    # When `signal_zone` is selected but the plan has no zone data
    # (e.g. naked OPEN_INSTANT before ATTACH_SIGNAL fires), the EA
    # silently falls back to entry_fill.
    "be_target": "signal_zone",
    # Where in the signal's entry zone to anchor BE when
    # `be_target = "signal_zone"`. Operator-facing terms are price-
    # range positional: `low` = entry_low (more conservative SL for
    # BUYs, more aggressive for SELLs), `mid` = midpoint, `high` =
    # entry_high (more aggressive SL for BUYs, more conservative for
    # SELLs). Defaults to `mid` so the choice is neutral.
    "be_signal_zone_position": "mid",
    # OPEN_INSTANT "naked position" fallback settings. These govern the
    # behavior between an OPEN_INSTANT and its expected ATTACH_SIGNAL,
    # and what happens if the structured signal never arrives.
    #
    #   instant_risk_percent     — emergency SL is placed so its hit
    #                              loses this % of balance. Default 1%.
    #   instant_timeout_minutes  — wait this long for ATTACH_SIGNAL.
    #                              After timeout, install fallback TP +
    #                              start trailing SL. Default 2 min.
    #   instant_tp_multiplier    — fallback TP distance from entry =
    #                              this × emergency-SL distance. 2.0 =
    #                              2:1 R:R. Default 2.0.
    #   instant_trail_points     — once fallback is armed, trail SL
    #                              this many MT5 points behind price
    #                              (ratchet-only). 1000 ≈ $10 on XAUUSD,
    #                              roughly 1× H1 ATR in calm regimes.
    #                              Default 1000.
    "instant_risk_percent":    "1.0",
    "instant_timeout_minutes": "2",
    "instant_tp_multiplier":   "2.0",
    "instant_trail_points":    "1000",
    # Score-tied sizing — the evaluator writes a `sizing` block into
    # every evaluation. The EA reads `sizing.multiplier` and scales
    # lots accordingly when its `EnableScoreTiedSizing` input is on.
    # `score_tied_skip_threshold` is the p_profit below which the
    # evaluator marks `skip=true` (EA posts rejected without trading).
    # 0.45 default: trades with sub-coin-flip conviction are skipped.
    "score_tied_skip_threshold": "0.45",
    "trading_style": "intraday",
    # When "1", the AI interpreter may override the stack default for a
    # single signal if the message text declares a different style
    # (e.g. "اسكالب فقط" → scalp, "صفقة طويلة" → swing). When "0",
    # every signal from this stack uses the stack default regardless of
    # what the message says.
    "trading_style_ai_override": "1",
}


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _read_raw(db_path: Path, key: str) -> str | None:
    if not db_path.exists():
        return None
    try:
        with _open(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
    except sqlite3.OperationalError:
        # DB file exists but the settings table hasn't been created yet
        # (orphan empty DB from a prior run, or pre-migration state).
        return None
    return row[0] if row else None


def _write_raw(db_path: Path, key: str, value: str) -> None:
    with _open(db_path) as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def is_set(db_path: Path, key: str) -> bool:
    raw = _read_raw(db_path, key)
    return raw is not None and raw != ""


def get_str(db_path: Path, key: str, default: str = "") -> str:
    raw = _read_raw(db_path, key)
    if raw is None:
        return default
    if key in SECRET_KEYS:
        return decrypt(raw)
    return raw


def get_int(db_path: Path, key: str, default: int = 0) -> int:
    raw = get_str(db_path, key, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def get_float(db_path: Path, key: str, default: float = 0.0) -> float:
    raw = get_str(db_path, key, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def get_bool(db_path: Path, key: str, default: bool = False) -> bool:
    raw = get_str(db_path, key, "")
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def set_str(db_path: Path, key: str, value: str) -> None:
    payload = encrypt(value) if key in SECRET_KEYS else value
    _write_raw(db_path, key, payload)


def set_int(db_path: Path, key: str, value: int) -> None:
    _write_raw(db_path, key, str(int(value)))


def set_bool(db_path: Path, key: str, value: bool) -> None:
    _write_raw(db_path, key, "1" if value else "0")


def seed_defaults(db_path: Path) -> None:
    """Insert default rows for any non-critical key not yet present."""
    with _open(db_path) as conn:
        for key, val in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, val),
            )
        conn.commit()


def missing_critical_keys(db_path: Path) -> list[str]:
    """Return critical keys that are absent or empty."""
    return [k for k in CRITICAL_KEYS if not is_set(db_path, k)]
