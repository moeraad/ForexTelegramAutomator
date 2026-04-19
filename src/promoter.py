import sqlite3
from datetime import datetime, timezone
from src.db import get_setting


def promote_due_actions(conn: sqlite3.Connection) -> int:
    """Promote pending actions whose execute_after has passed. Returns count promoted."""
    if get_setting(conn, "kill_switch") == "on":
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE actions SET status='sent' "
        "WHERE status='pending' "
        "AND execute_after IS NOT NULL "
        "AND execute_after <= ? "
        "AND action_type IN ('OPEN','MODIFY','CLOSE','CLOSE_ALL')",
        (now_iso,),
    )
    return cur.rowcount
