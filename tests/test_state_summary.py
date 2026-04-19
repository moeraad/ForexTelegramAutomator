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
    assert out.count("Signal #1") == 1  # grouped header
    assert "ticket=5880" in out
    assert "ticket=5900" in out
    assert "ticket=5920" in out
