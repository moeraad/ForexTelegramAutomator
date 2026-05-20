import sqlite3, json

db = r"C:\Users\Administrator\Desktop\copytrades.db"
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row

print("=== positions columns ===")
print([r[1] for r in c.execute("PRAGMA table_info(positions)")])

print("\n=== last 8 closed XAUUSD positions ===")
rows = list(c.execute(
    "SELECT p.mt5_ticket as ticket, p.status, p.symbol, p.entry_price, p.sl, p.volume, "
    "p.original_volume, p.closed_at, p.action_id "
    "FROM positions p WHERE p.status='closed' "
    "ORDER BY p.closed_at DESC LIMIT 8"
))
for r in rows:
    print(dict(r))

print("\n=== originating action payloads for those positions ===")
for r in rows:
    a = c.execute(
        "SELECT id, action_type, payload_json FROM actions WHERE id=?",
        (r["action_id"],),
    ).fetchone()
    print(f"position ticket={r['ticket']} closed_at={r['closed_at']}")
    if a:
        try:
            payload = json.loads(a["payload_json"])
        except Exception:
            payload = a["payload_json"]
        print(f"  action id={a['id']} type={a['action_type']}")
        print(f"  payload keys: {list(payload) if isinstance(payload, dict) else 'not dict'}")
        if isinstance(payload, dict):
            for k in ("side", "sl", "tps", "entry_low", "entry_high", "entry_price"):
                print(f"    {k} = {payload.get(k)!r}")
    else:
        print("  (no action row)")
    print()
