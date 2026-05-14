"""Lazy DB-backed config facade.

Boot tier (resolved at import time):
  - DB_PATH       resolved from --db-path CLI / DB_PATH env / default APPDATA
  - BASE_DIR      project root
  - LOGS_DIR      <project_root>/logs

Everything else (API keys, Telegram, AI provider, tuning knobs) lives in
the active DB's `settings` table. Reads happen via module-level
__getattr__ on first access; values are cached per DB_PATH thereafter.

Critical keys with no default cause services to refuse to start until
the GUI's setup wizard populates them.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
SUPPORTED_SYMBOLS = {"XAUUSD"}


def _resolve_db_path() -> str:
    for i, arg in enumerate(sys.argv):
        if arg == "--db-path" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith("--db-path="):
            return arg.split("=", 1)[1]
    env_path = os.environ.get("DB_PATH")
    if env_path:
        return env_path
    stack = os.environ.get("CHANNEL_PROFILE_BOOTSTRAP") or "default"
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return str(Path(appdata) / "CopyTrades" / stack / "copytrades.db")


DB_PATH = _resolve_db_path()
LOGS_DIR = Path(DB_PATH).parent / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)


# Mapping: public Python name → (settings key, type)
_LAZY: dict[str, tuple[str, str]] = {
    "API_HOST": ("api_host", "str"),
    "API_PORT": ("api_port", "int"),
    "EA_SHARED_TOKEN": ("ea_shared_token", "str"),
    "ANTHROPIC_API_KEY": ("anthropic_api_key", "str"),
    "ANTHROPIC_MODEL": ("anthropic_model", "str"),
    "OPENAI_API_KEY": ("openai_api_key", "str"),
    "OPENAI_MODEL": ("openai_model", "str"),
    "OPENAI_TRIAGE_MODEL": ("openai_triage_model", "str"),
    "AI_PROVIDER": ("ai_provider", "str"),
    "AI_TRIAGE_MODEL": ("ai_triage_model", "str"),
    "AI_TRIAGE_ENABLED": ("ai_triage_enabled", "bool"),
    "AI_THINKING_ENABLED": ("ai_thinking_enabled", "bool"),
    "AI_THINKING_BUDGET_TOKENS": ("ai_thinking_budget_tokens", "int"),
    "TG_API_ID": ("tg_api_id", "int"),
    "TG_API_HASH": ("tg_api_hash", "str"),
    "TG_PHONE": ("tg_phone", "str"),
    "TG_SESSION_NAME": ("tg_session_name", "str"),
    "TG_WATCHED_CHAT_ID": ("tg_watched_chat_id", "int"),
    "TG_BOT_TOKEN": ("tg_bot_token", "str"),
    "TG_BOT_OWNER_USER_ID": ("tg_bot_owner_user_id", "int"),
    "CHANNEL_PROFILE": ("channel_profile", "str"),
    "SIGNAL_MEMORY_ENABLED": ("signal_memory_enabled", "bool"),
    "SIGNAL_MEMORY_MAX_ENTRIES": ("signal_memory_max_entries", "int"),
    "SIGNAL_MEMORY_MAX_AGE_HOURS": ("signal_memory_max_age_hours", "int"),
    "FINGERPRINT_BAND_PRICE": ("fingerprint_band_price", "float"),
    "FINGERPRINT_WINDOW_HOURS": ("fingerprint_window_hours", "int"),
    "BACKFILL_MAX_AGE_MIN": ("backfill_max_age_min", "int"),
    "DEFAULT_AUTO_EXECUTE_DELAY_SEC": ("default_auto_execute_delay_sec", "int"),
    "RECENT_CHAT_WINDOW": ("recent_chat_window", "int"),
    "CLASSIFIER_BATCH_SIZE": ("classifier_batch_size", "int"),
    "CLASSIFIER_CONCURRENCY": ("classifier_concurrency", "int"),
    "CLASSIFIER_PROVIDER": ("classifier_provider", "str"),
    "CLASSIFIER_ANTHROPIC_MODEL": ("classifier_anthropic_model", "str"),
    "CLASSIFIER_OPENAI_MODEL": ("classifier_openai_model", "str"),
}

_TYPE_DEFAULT = {"str": "", "int": 0, "float": 0.0, "bool": False}

_cache: dict[str, dict[str, object]] = {}


def _read(name: str) -> object:
    key, kind = _LAZY[name]
    bucket = _cache.setdefault(DB_PATH, {})
    if name in bucket:
        return bucket[name]
    path = Path(DB_PATH)
    if not path.exists():
        return _TYPE_DEFAULT[kind]
    from src import db_settings
    if kind == "str":
        value = db_settings.get_str(path, key, "")
    elif kind == "int":
        value = db_settings.get_int(path, key, 0)
    elif kind == "float":
        value = db_settings.get_float(path, key, 0.0)
    else:
        value = db_settings.get_bool(path, key, False)
    bucket[name] = value
    return value


def __getattr__(name: str):
    if name in _LAZY:
        return _read(name)
    raise AttributeError(f"module 'src.config' has no attribute {name!r}")


def invalidate_cache() -> None:
    """Clear cached settings — call after writes / DB swaps."""
    _cache.clear()


def initialize() -> None:
    """Open DB, run migrations, eagerly load all settings. Idempotent.

    Service entrypoints (api/bot/listener) should call this at startup so
    config failures surface immediately rather than on first attribute use.
    """
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    from src import db, db_settings
    with db.connect(DB_PATH) as conn:
        db.init_schema(conn)
    db_settings.seed_defaults(path)
    invalidate_cache()
    for name in _LAZY:
        _read(name)
