import sqlite3
from datetime import datetime, timedelta, timezone
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


def release_stale_claims(conn: sqlite3.Connection, max_age_sec: int = 120) -> int:
    """Release claimed actions whose claimed_at is older than max_age_sec back to 'sent'.

    Why: if the EA crashes between /claim and /result, the action would be stranded
    in 'claimed' forever. This sweeper recovers it after a safe quiet period.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_sec)).isoformat()
    cur = conn.execute(
        "UPDATE actions SET status='sent', claimed_at=NULL "
        "WHERE status='claimed' AND claimed_at IS NOT NULL AND claimed_at < ?",
        (cutoff,),
    )
    return cur.rowcount
