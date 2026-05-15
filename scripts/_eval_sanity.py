"""Synchronous evaluator sanity test — no daemon-thread escape hatch."""
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

DB = r"C:\Users\Administrator\AppData\Roaming\CopyTrades\Forex Engineer\copytrades.db"
os.environ["DB_PATH"] = DB

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)

from src import config as _cfg
print("config.DB_PATH =", _cfg.DB_PATH)
print("config.AI_PROVIDER =", _cfg.AI_PROVIDER)
print("config.OPENAI_API_KEY set?", bool(_cfg.OPENAI_API_KEY))
print("config.ANTHROPIC_API_KEY set?", bool(_cfg.ANTHROPIC_API_KEY))

from src.orchestrator import _evaluator_worker, _build_evaluator_ai_client

print("\n--- building AI client ---")
ai = _build_evaluator_ai_client()
print("ai client:", ai)
if ai is None:
    print("FAIL: evaluator AI client returned None")
    sys.exit(1)

# Find latest OPEN action
c = sqlite3.connect(DB)
row = c.execute(
    "SELECT id, payload_json FROM actions "
    "WHERE action_type='OPEN' ORDER BY id DESC LIMIT 1"
).fetchone()
c.close()
if not row:
    print("FAIL: no OPEN action in DB")
    sys.exit(1)
action_id, raw = row
payload = json.loads(raw)
signal = {k: v for k, v in payload.items() if k != "pending"}
print(f"\n--- running synchronous evaluator on action_id={action_id} ---")
print("signal:", signal)

_evaluator_worker(action_id, signal, DB)

# Verify it landed
c = sqlite3.connect(DB)
row = c.execute(
    "SELECT payload_json FROM actions WHERE id=?", (action_id,)
).fetchone()
c.close()
p = json.loads(row[0]) if row and row[0] else {}
if p.get("evaluation"):
    ev = p["evaluation"]
    print(f"\nSUCCESS: score={ev.get('score')} verdict={ev.get('verdict')}")
    print(f"  data_quality={ev.get('data_quality')}")
    sys.exit(0)
print("\nFAIL: worker returned but no evaluation written")
sys.exit(1)
