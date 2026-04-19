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
