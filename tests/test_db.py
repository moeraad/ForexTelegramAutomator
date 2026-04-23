from src.db import connect, init_schema, get_setting, set_setting


def test_init_schema_creates_tables(tmp_path):
    db_file = tmp_path / "test.db"
    conn = connect(str(db_file))
    init_schema(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = [r["name"] for r in rows]
    assert "actions" in names
    assert "messages" in names
    assert "positions" in names
    assert "settings" in names


def test_default_settings_seeded(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    assert get_setting(conn, "kill_switch") == "off"
    assert get_setting(conn, "auto_execute_delay_sec") == "0"


def test_set_and_get_setting(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    set_setting(conn, "kill_switch", "on")
    assert get_setting(conn, "kill_switch") == "on"


def test_get_missing_setting_returns_none(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    assert get_setting(conn, "nonexistent_key") is None
