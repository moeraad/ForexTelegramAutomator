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
    "classifier_batch_size": "10",
    "classifier_concurrency": "4",
    "classifier_provider": "anthropic",
    "classifier_anthropic_model": "claude-haiku-4-5-20251001",
    "classifier_openai_model": "gpt-5-nano",
    "cost_daily_budget_usd": "5.00",
    "classifier_custom_prompt": "",
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
