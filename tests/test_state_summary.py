import json
from datetime import datetime, timedelta, timezone

from src.db import connect, init_schema
from src.state_summary import render_open_positions


def _setup(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    return conn


def test_no_positions_renders_empty_marker(tmp_path):
    conn = _setup(tmp_path)
    out = render_open_positions(conn)
    assert "OPEN POSITIONS" in out
    assert "(none)" in out


def test_renders_open_positions_only(tmp_path):
    conn = _setup(tmp_path)
    conn.execute("INSERT INTO actions(action_type, payload_json) VALUES('OPEN','{}')")
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status) "
        "VALUES(1, 555, 'XAUUSD', 'BUY', 0.10, 4865.0, 4855.0, 4880.0, 'open')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status, closed_at, close_reason) "
        "VALUES(1, 666, 'XAUUSD', 'BUY', 0.10, 4865.0, 4855.0, 4900.0, "
        "'closed', CURRENT_TIMESTAMP, 'tp')"
    )
    out = render_open_positions(conn)
    open_section = out.split("PENDING OPEN SIGNALS")[0]
    # Section-scoped: 555 appears in OPEN POSITIONS, 666 (closed) does not.
    # 666 may legitimately appear later under LAST CLOSED POSITION.
    assert "555" in open_section
    assert "666" not in open_section


def test_renders_groupings_by_action_id(tmp_path):
    conn = _setup(tmp_path)
    conn.execute("INSERT INTO actions(action_type, payload_json) VALUES('OPEN','{}')")
    for tp in (4880, 4900, 4920):
        conn.execute(
            "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
            "entry_price, sl, tp, status) "
            f"VALUES(1, {1000 + tp}, 'XAUUSD', 'BUY', 0.10, 4865.0, 4855.0, {tp}, 'open')"
        )
    out = render_open_positions(conn)
    # Only assert within the executed-positions section — the pending section
    # also lists this action (since its status defaults to 'pending') and would
    # otherwise inflate the count.
    exec_section = out.split("PENDING OPEN SIGNALS")[0]
    assert exec_section.count("Signal #1") == 1  # grouped header
    assert "ticket=5880" in out
    assert "ticket=5900" in out
    assert "ticket=5920" in out


def test_null_sl_tp_render_as_dash(tmp_path):
    conn = _setup(tmp_path)
    conn.execute("INSERT INTO actions(action_type, payload_json) VALUES('OPEN','{}')")
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status) "
        "VALUES(1, 777, 'XAUUSD', 'BUY', 0.10, 4865.0, NULL, NULL, 'open')"
    )
    out = render_open_positions(conn)
    assert "ticket=777" in out
    assert "sl=-" in out
    assert "tp=-" in out


def test_pending_open_signals_listed(tmp_path):
    conn = _setup(tmp_path)
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'pending')",
        ('{"symbol":"XAUUSD","side":"BUY","entry_low":4864,"entry_high":4866,'
         '"tps":[4880,4890],"sl":4855}',),
    )
    out = render_open_positions(conn)
    assert "PENDING OPEN SIGNALS" in out
    assert "[pending]" in out
    assert "BUY XAUUSD" in out
    assert "4880" in out and "4890" in out


def test_pending_section_shows_none_when_empty(tmp_path):
    conn = _setup(tmp_path)
    out = render_open_positions(conn)
    assert "PENDING OPEN SIGNALS" in out
    # Both sections show (none)
    assert out.count("(none)") == 2


def test_sent_and_claimed_opens_also_listed(tmp_path):
    conn = _setup(tmp_path)
    payload = ('{"symbol":"XAUUSD","side":"SELL","entry_low":4900,"entry_high":4902,'
               '"tps":[4880],"sl":4910}')
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'sent')", (payload,))
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'claimed')", (payload,))
    out = render_open_positions(conn)
    assert "[sent]" in out
    assert "[claimed]" in out


def test_executed_open_not_in_pending_section(tmp_path):
    """Once executed, the signal belongs under OPEN POSITIONS, not pending."""
    conn = _setup(tmp_path)
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')")
    out = render_open_positions(conn)
    pending_section = (
        out.split("PENDING OPEN SIGNALS")[1].split("LAST CLOSED POSITION")[0]
    )
    assert "[executed]" not in pending_section
    assert "(none)" in pending_section


# ---- Phase 1: enriched state, last-closed, market-price blocks ----------

def _open_pos(conn, ticket, **overrides):
    defaults = dict(
        action_id=1, mt5_ticket=ticket, symbol="XAUUSD", side="BUY",
        volume=0.04, original_volume=0.08, partial_close_count=1,
        entry_price=4700.0, sl=4700.0, tp=4720.0,
        sl_moved_at=None,
    )
    defaults.update(overrides)
    conn.execute(
        "INSERT OR IGNORE INTO actions(id, action_type, payload_json) "
        "VALUES(?, 'OPEN', '{}')", (defaults["action_id"],),
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "original_volume, partial_close_count, entry_price, sl, tp, "
        "sl_moved_at, status) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'open')",
        (defaults["action_id"], defaults["mt5_ticket"], defaults["symbol"],
         defaults["side"], defaults["volume"], defaults["original_volume"],
         defaults["partial_close_count"], defaults["entry_price"],
         defaults["sl"], defaults["tp"], defaults["sl_moved_at"]),
    )


def test_open_position_shows_original_volume_and_partials(tmp_path):
    conn = _setup(tmp_path)
    _open_pos(conn, 200, volume=0.04, original_volume=0.08, partial_close_count=1)
    out = render_open_positions(conn)
    open_section = out.split("PENDING OPEN SIGNALS")[0]
    assert "vol=0.04" in open_section
    assert "of 0.08 orig" in open_section
    assert "partials_taken=1" in open_section


def test_open_position_shows_at_BE_when_sl_equals_entry(tmp_path):
    conn = _setup(tmp_path)
    _open_pos(conn, 201, entry_price=4700.0, sl=4700.0)
    out = render_open_positions(conn)
    open_section = out.split("PENDING OPEN SIGNALS")[0]
    assert "at_BE" in open_section


def test_open_position_no_at_BE_when_sl_far_from_entry(tmp_path):
    conn = _setup(tmp_path)
    _open_pos(conn, 202, entry_price=4700.0, sl=4690.0)
    out = render_open_positions(conn)
    open_section = out.split("PENDING OPEN SIGNALS")[0]
    assert "at_BE" not in open_section


def test_open_position_shows_moved_when_sl_moved_at_set(tmp_path):
    conn = _setup(tmp_path)
    _open_pos(conn, 203, sl_moved_at="2026-05-01T22:31:15+00:00")
    out = render_open_positions(conn)
    open_section = out.split("PENDING OPEN SIGNALS")[0]
    assert "moved" in open_section


def test_last_closed_block_present_when_recent(tmp_path):
    conn = _setup(tmp_path)
    payload = json.dumps({
        "type": "OPEN", "symbol": "XAUUSD", "side": "BUY",
        "entry_low": 4699, "entry_high": 4701,
        "sl": 4690, "tps": [4710, 4720, 4735],
    })
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json) VALUES('OPEN', ?)",
        (payload,)
    )
    aid = cur.lastrowid
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "original_volume, partial_close_count, entry_price, sl, tp, "
        "status, closed_at, close_reason) "
        "VALUES(?, 204, 'XAUUSD', 'BUY', 0.04, 0.08, 1, 4700.0, 4700.0, 4720.0, "
        "'closed', ?, 'tp1_hit')",
        (aid, datetime.now(timezone.utc).isoformat()),
    )
    out = render_open_positions(conn)
    closed_section = out.split("LAST CLOSED POSITION")[1]
    assert "ticket=204" in closed_section
    assert "tp1_hit" in closed_section
    assert "original_volume=0.08" in closed_section
    assert "tps=[4710,4720,4735]" in closed_section


def test_last_closed_block_empty_when_none(tmp_path):
    conn = _setup(tmp_path)
    out = render_open_positions(conn)
    closed_section = out.split("LAST CLOSED POSITION")[1]
    assert "(none" in closed_section


def test_last_closed_block_skips_unreplayable_open_instant_orphan(tmp_path):
    """OPEN_INSTANT orphan (no ATTACH_SIGNAL) is not replayable, so the
    LAST CLOSED block must show the "none" marker — otherwise the prompt
    would emit REOPEN_LAST against a trade the EA can't replay,
    producing `last_closed_unparseable`.
    """
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN_INSTANT', ?, 'executed')",
        (json.dumps({"symbol": "XAUUSD", "side": "BUY", "comment": "x"}),),
    )
    aid = cur.lastrowid
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, status, opened_at, closed_at, close_reason) "
        "VALUES(?, 207, 'XAUUSD', 'BUY', 1.0, 4700, 'closed', ?, ?, 'mt5_sl')",
        (
            aid,
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    out = render_open_positions(conn)
    closed_section = out.split("LAST CLOSED POSITION")[1]
    assert "(none" in closed_section


def test_last_closed_block_walks_back_past_orphan(tmp_path):
    """When the most recent close is an unreplayable OPEN_INSTANT
    orphan, the LAST CLOSED block surfaces the earlier replayable close
    so REOPEN_LAST has something to act on."""
    conn = _setup(tmp_path)
    # Earlier: replayable OPEN.
    earlier = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    payload = json.dumps({
        "type": "OPEN", "symbol": "XAUUSD", "side": "BUY",
        "entry_low": 4699, "entry_high": 4701,
        "sl": 4690, "tps": [4710, 4720, 4735],
    })
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json) VALUES('OPEN', ?)",
        (payload,),
    )
    aid = cur.lastrowid
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "original_volume, partial_close_count, entry_price, sl, tp, "
        "status, opened_at, closed_at, close_reason) "
        "VALUES(?, 208, 'XAUUSD', 'BUY', 0.04, 0.08, 1, 4700.0, 4700.0, 4720.0, "
        "'closed', ?, ?, 'tp1_hit')",
        (aid, earlier, earlier),
    )
    # Later: orphan OPEN_INSTANT.
    now_iso = datetime.now(timezone.utc).isoformat()
    cur2 = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN_INSTANT', ?, 'executed')",
        (json.dumps({"symbol": "XAUUSD", "side": "BUY", "comment": "x"}),),
    )
    aid2 = cur2.lastrowid
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, status, opened_at, closed_at, close_reason) "
        "VALUES(?, 209, 'XAUUSD', 'BUY', 1.0, 4700, 'closed', ?, ?, 'mt5_sl')",
        (aid2, now_iso, now_iso),
    )
    out = render_open_positions(conn)
    closed_section = out.split("LAST CLOSED POSITION")[1]
    assert "ticket=208" in closed_section
    assert "tps=[4710,4720,4735]" in closed_section


def test_last_closed_block_skips_too_old(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json) VALUES('OPEN', '{}')"
    )
    aid = cur.lastrowid
    too_old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "original_volume, entry_price, sl, tp, status, closed_at, close_reason) "
        "VALUES(?, 205, 'XAUUSD', 'BUY', 0.04, 0.08, 4700.0, 4700.0, 4720.0, "
        "'closed', ?, 'tp')",
        (aid, too_old),
    )
    out = render_open_positions(conn)
    closed_section = out.split("LAST CLOSED POSITION")[1]
    assert "(none" in closed_section
    assert "ticket=205" not in closed_section


def test_market_price_renders_recent_quote(tmp_path):
    conn = _setup(tmp_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    for k, v in (("market_XAUUSD_bid", "4823.45"),
                 ("market_XAUUSD_ask", "4823.85"),
                 ("market_XAUUSD_at", now_iso)):
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (k, v)
        )
    out = render_open_positions(conn)
    assert "MARKET (XAUUSD)" in out
    assert "bid=4823.45" in out
    assert "ask=4823.85" in out
    assert "mid=4823.65" in out


def test_market_price_renders_no_recent_quote_when_unset(tmp_path):
    conn = _setup(tmp_path)
    out = render_open_positions(conn)
    assert "MARKET (XAUUSD)" in out
    assert "no recent quote" in out


def test_market_price_renders_stale_marker(tmp_path):
    conn = _setup(tmp_path)
    stale_iso = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    for k, v in (("market_XAUUSD_bid", "4823.45"),
                 ("market_XAUUSD_ask", "4823.85"),
                 ("market_XAUUSD_at", stale_iso)):
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (k, v)
        )
    out = render_open_positions(conn)
    assert "STALE" in out
