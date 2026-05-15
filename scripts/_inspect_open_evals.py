"""Inspect the latest OPEN/OPEN_INSTANT actions for evaluation presence."""
import json
import sqlite3
import sys

DB = r"C:\Users\Administrator\AppData\Roaming\CopyTrades\Forex Engineer\copytrades.db"

conn = sqlite3.connect(DB)
rows = conn.execute(
    "SELECT id, action_type, status, created_at, payload_json "
    "FROM actions WHERE action_type IN ('OPEN','OPEN_INSTANT') "
    "ORDER BY id DESC LIMIT 10"
).fetchall()
if not rows:
    print("no OPEN/OPEN_INSTANT rows")
    sys.exit(0)
for aid, atype, status, created, raw in rows:
    print(f"id={aid} type={atype:13s} status={status:9s} at={created}")
    try:
        p = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        print("  (payload not parseable)")
        continue
    keys = list(p.keys())
    print(f"  payload keys: {keys}")
    ev = p.get("evaluation")
    if ev is None:
        print("  evaluation: MISSING")
    else:
        print(f"  evaluation: score={ev.get('score')} verdict={ev.get('verdict')}")
        print(f"  data_quality: {ev.get('data_quality')}")
