import sqlite3, json, sys

db = r"C:\Users\Administrator\Desktop\copytrades.db"
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row

cols = [r[1] for r in c.execute("PRAGMA table_info(actions)")]
print("=== actions columns ===")
print(cols)

print("\n=== REOPEN_LAST rows (last 10) ===")
rows = list(c.execute(
    "SELECT * FROM actions WHERE action_type='REOPEN_LAST' ORDER BY id DESC LIMIT 10"
))
print("count:", len(rows))
for r in rows:
    print(json.dumps(dict(r), indent=2, ensure_ascii=False))

print("\n=== kill_switch + promotion_delay ===")
for k in ("kill_switch", "promotion_delay_seconds"):
    row = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    print(f"{k} = {row['value'] if row else '<unset>'}")

print("\n=== last 15 actions of any type (for context) ===")
rows = list(c.execute(
    "SELECT id, action_type, status, created_at FROM actions ORDER BY id DESC LIMIT 15"
))
for r in rows:
    print(dict(r))
