"""Qt smoke tests for the Audit view (Step 19).

Aggregator behavior is covered hermetically in
tests/test_step19_audit_aggregator.py. These tests check the view's
search button wiring + tree rendering.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src import config_v2
from src.config_v2 import (
    Account,
    Bot,
    BotBinding,
    Channel,
    ConfigV2,
    Destination,
    Profile,
    Route,
)
from src.db import connect, init_schema


def _make_dest_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(path))
    init_schema(conn)
    return conn


def _baseline_cfg(db_path: Path) -> ConfigV2:
    return ConfigV2(
        accounts=(Account(
            id="acc_a", name="P", phone="",
            session_path="x.session", service_name="CT-Listener-acc_a",
        ),),
        profiles=(Profile(
            id="prof", name="P", path="/p.json",
            language="en", symbol="XAUUSD",
        ),),
        channels=(Channel(
            id="ch", name="C", account_id="acc_a",
            chat_id=-1001, profile_id="prof",
        ),),
        destinations=(Destination(
            id="dest_x", name="X", db_path=str(db_path),
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-Api-X",
        ),),
        bots=(Bot(id="bot_main", name="B", token_setting_key="t",
                  service_name="CT-Bot-Main"),),
        routes=(Route(id="r", channel_id="ch", destination_id="dest_x"),),
        bot_bindings=(BotBinding(
            id="bind", bot_id="bot_main", scope="destination",
            destination_id="dest_x",
        ),),
    )


def test_audit_view_constructs(qtbot, tmp_stack, monkeypatch, tmp_path):
    """The view must render even when v2 config is absent."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata-empty"))
    from src.gui.views.audit_view import AuditView
    view = AuditView(tmp_stack)
    qtbot.addWidget(view)
    assert view._tree.topLevelItemCount() == 0


def test_audit_view_search_renders_results(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    """End-to-end: seed a destination DB → write v2 config pointing
    at it → trigger Search → verify the tree populates."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    db_path = tmp_path / "appdata" / "CopyTrades" / "dest_x" / "copytrades.db"
    conn = _make_dest_db(db_path)
    # Seed a message + one action + one DM.
    cur = conn.execute(
        "INSERT INTO messages (tg_message_id, chat_id, sender, text, "
        "is_backfill, source_channel_id) VALUES (?, ?, ?, ?, 0, ?)",
        (42, -1001, "a", "buy gold", "ch"),
    )
    msg_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO actions (source_msg_id, action_type, payload_json, "
        "status, source_channel_id, route_id) VALUES (?, ?, ?, ?, ?, ?)",
        (msg_id, "OPEN", json.dumps({"symbol": "XAUUSD", "side": "BUY"}),
         "executed", "ch", "r"),
    )
    act_id = cur.lastrowid
    conn.execute(
        "INSERT INTO bot_outbox (bot_id, event_type, event_payload, "
        "action_id, delivered_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("bot_main", "action_terminal", "{}", act_id,
         "2026-05-24T12:00:00+00:00"),
    )
    conn.commit()

    config_v2.save_v2(_baseline_cfg(db_path))

    from src.gui.views.audit_view import AuditView
    view = AuditView(tmp_stack)
    qtbot.addWidget(view)

    # Trigger search by tg_message_id = 42.
    view._tg_msg_id.setValue(42)
    view._run_search()

    # Tree should now show 1 destination top-level item.
    assert view._tree.topLevelItemCount() == 1
    top = view._tree.topLevelItem(0)
    assert "dest_x" in top.text(0) or "X" in top.text(0)
    # 1 message child.
    assert top.childCount() == 1
    msg_node = top.child(0)
    assert "tg#42" in msg_node.text(0)
    # 1 action child under the message.
    assert msg_node.childCount() == 1
    act_node = msg_node.child(0)
    assert "OPEN" in act_node.text(1)
    # 1 DM grandchild (under the action; some "↳ executed_at" rows
    # may also be present).
    dm_nodes = [
        act_node.child(i) for i in range(act_node.childCount())
        if act_node.child(i).text(0).startswith("💬")
    ]
    assert len(dm_nodes) == 1
    assert "bot_main" in dm_nodes[0].text(2)


def test_audit_view_handles_no_destinations_gracefully(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    """Search with v2 absent → summary shows pointer to Settings,
    no crash."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata-empty"))
    from src.gui.views.audit_view import AuditView
    view = AuditView(tmp_stack)
    qtbot.addWidget(view)
    view._tg_msg_id.setValue(1)
    view._run_search()
    assert view._tree.topLevelItemCount() == 0
    assert "destinations" in view._summary.text().lower() \
        or "settings" in view._summary.text().lower()
