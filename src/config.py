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

# Provider switch: "anthropic" (default) or "openai". Selects the SDK used by
# both the interpreter (AIClient) and the triage pre-filter (TriageClient).
AI_PROVIDER = os.getenv("AI_PROVIDER", "anthropic").lower()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
OPENAI_TRIAGE_MODEL = os.getenv("OPENAI_TRIAGE_MODEL", "gpt-5-nano")

# Two-stage pipeline: a cheap Haiku triage pass decides ignore-vs-keep before
# the full Sonnet interpreter runs. Cuts Sonnet call volume on noisy channels
# without losing precision (full model still sees every KEEP message).
AI_TRIAGE_ENABLED = os.getenv("AI_TRIAGE_ENABLED", "1") not in ("0", "false", "False", "")
AI_TRIAGE_MODEL = os.getenv("AI_TRIAGE_MODEL", "claude-haiku-4-5-20251001")

# Extended thinking: let the model reason step-by-step before producing JSON.
# Materially improves parsing of ambiguous/shorthand Arabic signals where
# entries like "95-94" must be anchored to an explicit SL like "4808".
AI_THINKING_ENABLED = os.getenv("AI_THINKING_ENABLED", "1") not in ("0", "false", "False", "")
AI_THINKING_BUDGET_TOKENS = int(os.getenv("AI_THINKING_BUDGET_TOKENS", "4000"))

API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8765"))
# Shared secret for the EA -> API auth header. Blank = unauth (dev mode).
# When set, the auth_gate middleware in src/api.py requires every request
# to include `X-EA-Token: <this value>` and the EA's WebRequest helpers
# must send the matching header (configured via the EA's ApiSharedToken
# input).
EA_SHARED_TOKEN = os.getenv("EA_SHARED_TOKEN", "")

DEFAULT_AUTO_EXECUTE_DELAY_SEC = 0  # 0 = promote on next tick; no grace window
RECENT_CHAT_WINDOW = 20  # messages
SUPPORTED_SYMBOLS = {"XAUUSD"}

# Dedup: collapse resent/quoted signals into one fingerprint bucket.
# BAND_PRICE is the rounding granularity; WINDOW_HOURS how far back to look.
FINGERPRINT_BAND_PRICE = float(os.getenv("FINGERPRINT_BAND_PRICE", "5.0"))
FINGERPRINT_WINDOW_HOURS = int(os.getenv("FINGERPRINT_WINDOW_HOURS", "6"))

# Backfill replay: on reconnect (not first launch), feed missed Telegram
# messages through the AI only if they're fresher than this cap. Older
# messages are archived with is_backfill=1 and not processed because price
# has likely moved too far for the signal to be safe.
BACKFILL_MAX_AGE_MIN = int(os.getenv("BACKFILL_MAX_AGE_MIN", "30"))

# Signal memory: distilled AI summaries of prior non-ignore messages are
# accumulated and fed back into every call in place of the raw 20-message
# chat window. Cleared when an OPEN action lands (the deliberation resolved).
# Falls back to raw recent-chat when disabled.
SIGNAL_MEMORY_ENABLED = os.getenv("SIGNAL_MEMORY_ENABLED", "1") not in ("0", "false", "False", "")
SIGNAL_MEMORY_MAX_ENTRIES = int(os.getenv("SIGNAL_MEMORY_MAX_ENTRIES", "10"))
SIGNAL_MEMORY_MAX_AGE_HOURS = int(os.getenv("SIGNAL_MEMORY_MAX_AGE_HOURS", "4"))
