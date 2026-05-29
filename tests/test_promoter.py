from datetime import datetime, timedelta, timezone
from src.db import connect, init_schema, set_setting
from src.promoter import (
    promote_due_actions,
    release_stale_claims,
    expire_stale_management,
    SINGLETON_MANAGEMENT_TYPES,
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


def _insert_claimed(conn, claimed_at, action_type="OPEN"):
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, claimed_at) "
        "VALUES(?, '{}', 'claimed', ?)",
        (action_type, claimed_at.isoformat()),
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


def test_release_stale_claims_fails_management_not_resurrect(tmp_path):
    """Diagnostics SMC 2026-05-29: a stranded CLOSE_FULL claim must NOT be
    recycled to 'sent' (it would re-claim and close a *different* singleton
    that opened in the interim). Singleton-targeting management types are
    marked terminal 'failed' instead so the EA never re-dispatches them.
    """
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    old = datetime.now(timezone.utc) - timedelta(seconds=400)
    aid = _insert_claimed(conn, old, action_type="CLOSE_FULL")
    n = release_stale_claims(conn, max_age_sec=300)
    assert n == 1
    row = conn.execute(
        "SELECT status, ea_response FROM actions WHERE id=?", (aid,)
    ).fetchone()
    assert row["status"] == "failed"
    assert row["ea_response"] == "stale_claim_not_retried"


def test_release_stale_claims_recycles_open(tmp_path):
    """OPEN keeps the recycle-to-'sent' behavior: re-firing a duplicate
    broker order is blocked by the api.py post_result claim_expired guard,
    and a crashed-mid-open EA legitimately needs the retry."""
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    old = datetime.now(timezone.utc) - timedelta(seconds=400)
    aid = _insert_claimed(conn, old, action_type="OPEN")
    release_stale_claims(conn, max_age_sec=300)
    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "sent"


def _insert_sent_mgmt(conn, execute_after, action_type="CLOSE_FULL"):
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, execute_after) "
        "VALUES(?, '{}', 'sent', ?)",
        (action_type, execute_after.isoformat() if execute_after else None),
    )
    return cur.lastrowid


def test_expire_stale_management_expires_old_sent(tmp_path):
    """A singleton-management action that has been executable (status='sent')
    longer than the TTL is expired before the EA can apply it to whatever the
    current singleton happens to be."""
    conn = connect(str(tmp_path / "e.db"))
    init_schema(conn)
    eligible_long_ago = datetime.now(timezone.utc) - timedelta(seconds=300)
    aid = _insert_sent_mgmt(conn, eligible_long_ago, "CLOSE_FULL")
    n = expire_stale_management(conn, max_age_sec=180)
    assert n == 1
    row = conn.execute(
        "SELECT status, ea_response FROM actions WHERE id=?", (aid,)
    ).fetchone()
    assert row["status"] == "failed"
    assert row["ea_response"] == "management_action_expired"


def test_expire_stale_management_keeps_fresh_sent(tmp_path):
    conn = connect(str(tmp_path / "e.db"))
    init_schema(conn)
    eligible_recently = datetime.now(timezone.utc) - timedelta(seconds=10)
    aid = _insert_sent_mgmt(conn, eligible_recently, "MOVE_SL_BE")
    n = expire_stale_management(conn, max_age_sec=180)
    assert n == 0
    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "sent"


def test_expire_stale_management_ignores_open(tmp_path):
    """OPEN is not a singleton-targeting management action; a stale sent OPEN
    is left for the existing recycle/dup-guard machinery, not expired here."""
    conn = connect(str(tmp_path / "e.db"))
    init_schema(conn)
    long_ago = datetime.now(timezone.utc) - timedelta(seconds=300)
    aid = _insert_sent_mgmt(conn, long_ago, "OPEN")
    n = expire_stale_management(conn, max_age_sec=180)
    assert n == 0
    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "sent"


def test_expire_stale_management_ignores_pending(tmp_path):
    """A management action still in its pre-promotion grace window (pending)
    cannot be claimed by the EA yet, so it carries no retarget risk and must
    not be expired. Backfill-review rows (pending, execute_after=NULL) are
    likewise untouched — they await an explicit operator decision."""
    conn = connect(str(tmp_path / "e.db"))
    init_schema(conn)
    long_ago = datetime.now(timezone.utc) - timedelta(seconds=300)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, execute_after) "
        "VALUES('CLOSE_FULL', '{}', 'pending', ?)",
        (long_ago.isoformat(),),
    )
    parked = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, execute_after) "
        "VALUES('REINFORCE', '{}', 'pending', NULL)",
    )
    n = expire_stale_management(conn, max_age_sec=180)
    assert n == 0
    assert conn.execute(
        "SELECT status FROM actions WHERE id=?", (cur.lastrowid,)
    ).fetchone()["status"] == "pending"
    assert conn.execute(
        "SELECT status FROM actions WHERE id=?", (parked.lastrowid,)
    ).fetchone()["status"] == "pending"


def test_singleton_management_set_covers_ticketless_types():
    """Guard the canonical set so adding a new ticketless management type
    forces a deliberate decision about its stale-execution safety."""
    assert SINGLETON_MANAGEMENT_TYPES == frozenset({
        "MOVE_SL_BE", "MOVE_SL", "CLOSE_PARTIAL", "CLOSE_FULL",
        "REOPEN_LAST", "REINFORCE", "TIGHTEN_SL", "MODIFY_TPS",
    })


