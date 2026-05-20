import sqlite3
from src.position_signal import find_last_replayable_closed

db = r"C:\Users\Administrator\Desktop\copytrades.db"
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row

found = find_last_replayable_closed(c, symbol="XAUUSD", within_hours=24)
if found is None:
    print("No replayable close in last 24h — endpoint would 404.")
else:
    row, sig = found
    print("Replayable last close found:")
    print(f"  ticket={row['mt5_ticket']}  closed_at={row['closed_at']}")
    print(f"  side={sig['side']}  sl={sig['sl']}  entry_low={sig['entry_low']}  entry_high={sig['entry_high']}")
    print(f"  tps={sig['tps']}")
