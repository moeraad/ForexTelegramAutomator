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
    assert "555" in out
    assert "666" not in out


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
    pending_section = out.split("PENDING OPEN SIGNALS")[1]
    assert "[executed]" not in pending_section
    assert "(none)" in pending_section
