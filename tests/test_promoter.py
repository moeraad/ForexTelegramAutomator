from datetime import datetime, timedelta, timezone
from src.db import connect, init_schema, set_setting
from src.promoter import (
    promote_due_actions,
    release_stale_claims,
)


def _insert_pending(conn, execute_after, action_type="OPEN"):
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, execute_after) "
        "VALUES(?, ?, 'pending', ?)",
        (action_type, "{}", execute_after.isoformat() if execute_after else None),
    )
    return cur.lastrowid


def test_promotes_due_action(tmp_path):
    conn = connect(str(tmp_path / "p.db"))
    init_schema(conn)
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    aid = _insert_pending(conn, past)
    n = promote_due_actions(conn)
    assert n == 1
    assert conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()["status"] == "sent"


def test_does_not_promote_future_action(tmp_path):
    conn = connect(str(tmp_path / "p.db"))
    init_schema(conn)
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    aid = _insert_pending(conn, future)
    n = promote_due_actions(conn)
    assert n == 0
    assert conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()["status"] == "pending"


def test_does_not_promote_when_kill_switch_on(tmp_path):
    conn = connect(str(tmp_path / "p.db"))
    init_schema(conn)
    set_setting(conn, "kill_switch", "on")
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    _insert_pending(conn, past)
    assert promote_due_actions(conn) == 0


def test_skips_alerts(tmp_path):
    """ALERTs have no execute_after and should never be promoted to sent."""
    conn = connect(str(tmp_path / "p.db"))
    init_schema(conn)
    aid = _insert_pending(conn, None, action_type="ALERT")
    assert promote_due_actions(conn) == 0
    assert conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()["status"] == "pending"


def _insert_claimed(conn, claimed_at):
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, claimed_at) "
        "VALUES('OPEN', '{}', 'claimed', ?)",
        (claimed_at.isoformat(),),
    )
    return cur.lastrowid


def test_release_stale_claims_releases_old(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    old = datetime.now(timezone.utc) - timedelta(seconds=200)
    aid = _insert_claimed(conn, old)
    n = release_stale_claims(conn, max_age_sec=120)
    assert n == 1
    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "sent"


def test_release_stale_claims_keeps_fresh(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    fresh = datetime.now(timezone.utc) - timedelta(seconds=10)
    aid = _insert_claimed(conn, fresh)
    n = release_stale_claims(conn, max_age_sec=120)
    assert n == 0
    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "claimed"


