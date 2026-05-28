"""Channel column attribution for Evaluation + Replay views.

Mirrors the journal/live tests: data layer populates source_channel_id,
view renders friendly name (or '—' for legacy rows).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.db import connect, init_schema


# ---- Evaluation data layer ---------------------------------------------


def _seed_closed_open_action(
    conn: sqlite3.Connection, *,
    source_channel_id: str | None = None,
    ticket: int = 1,
):
    payload = json.dumps({"side": "BUY", "symbol": "XAUUSD"})
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, "
        "executed_at, source_channel_id) "
        "VALUES (?, ?, 'executed', ?, ?)",
        ("OPEN", payload, "2026-05-24T10:00:00+00:00", source_channel_id),
    )
    action_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "original_volume, entry_price, exit_price, realized_pnl, status, "
        "opened_at, closed_at, close_reason) "
        "VALUES (?, ?, 'XAUUSD', 'BUY', 0.1, 0.1, 4000, 4010, 10, 'closed', "
        "'2026-05-24T10:00:00+00:00', '2026-05-24T11:00:00+00:00', 'tp')",
        (action_id, ticket),
    )
    conn.commit()
    return action_id


def test_evaluated_trade_picks_up_source_channel_id(tmp_path: Path):
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    _seed_closed_open_action(conn, source_channel_id="ch_a", ticket=42)
    _seed_closed_open_action(conn, source_channel_id=None, ticket=43)

    from src.gui.services.evaluation_data import evaluated_trades
    trades = evaluated_trades(db_path, days=None)
    by_ticket = {t.ticket: t for t in trades}
    assert by_ticket[42].source_channel_id == "ch_a"
    # Legacy row (NULL in DB) → empty string in dataclass.
    assert by_ticket[43].source_channel_id == ""


# ---- Replay messages data layer ----------------------------------------


def test_hist_message_picks_up_source_channel_id(tmp_path: Path):
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    conn.execute(
        "INSERT INTO messages(tg_message_id, chat_id, sender, text, "
        "source_channel_id, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (100, -1, "x", "hello", "ch_a", "2026-05-24T10:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO messages(tg_message_id, chat_id, sender, text, "
        "source_channel_id, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (101, -1, "x", "world", None, "2026-05-24T10:01:00+00:00"),
    )
    conn.commit()

    from src.gui.services.replay import load_messages
    msgs = load_messages(db_path, days=None, limit=50)
    by_id = {m.id: m for m in msgs}
    # Sort by msg_id is descending by default; just grab the relevant ones.
    tagged = next(m for m in msgs if m.text == "hello")
    legacy = next(m for m in msgs if m.text == "world")
    assert tagged.source_channel_id == "ch_a"
    assert legacy.source_channel_id == ""


# ---- Evaluation view _PerTradeTab rendering ----------------------------


def test_eval_view_headers_include_channel(qapp):
    """Channel column is in the header row, before Score."""
    from src.gui.views.evaluation_view import _PerTradeTab
    assert "Channel" in _PerTradeTab._HEADERS
    # Channel sits BEFORE Score so it groups with identity columns
    # (When/Type/Side) rather than scoring columns (Score/Verdict/Mult).
    headers = list(_PerTradeTab._HEADERS)
    assert headers.index("Channel") < headers.index("Score")


def test_eval_view_column_weights_sum_to_100():
    """Adding a column without re-balancing the weights skews the table —
    regression-check the constraint that's enforced at render time."""
    from src.gui.views.evaluation_view import _PerTradeTab
    assert sum(_PerTradeTab._COLUMN_WEIGHTS) == 100


def test_eval_view_column_weights_aligned_with_headers():
    from src.gui.views.evaluation_view import _PerTradeTab
    assert len(_PerTradeTab._COLUMN_WEIGHTS) == len(_PerTradeTab._HEADERS)


# ---- Replay view header check ------------------------------------------


def test_replay_model_headers_include_channel(qapp):
    from src.gui.views.replay_view import _ReplayModel
    assert "Channel" in _ReplayModel.HEADERS


def test_replay_model_renders_dash_for_legacy(qapp):
    from PySide6.QtCore import Qt
    from src.gui.services.replay import HistMessage
    from src.gui.views.replay_view import _ReplayModel, ReplayRow
    msg = HistMessage(
        id=1, text="hi", received_at="2026-05-24T10:00:00+00:00",
        sender="a", source_channel_id="",
    )
    row = ReplayRow(
        msg=msg, original=[], replayed=[], drift="match",
        triage_decision=None, error="",
    )
    model = _ReplayModel()
    model.append(row)
    # Channel column is index 2 in the new headers.
    headers = list(_ReplayModel.HEADERS)
    ch_col = headers.index("Channel")
    idx = model.index(0, ch_col)
    assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "—"


def test_replay_model_renders_raw_id_when_v2_missing(
    qapp, monkeypatch, tmp_path,
):
    from PySide6.QtCore import Qt
    from src.gui.models.actions_model import clear_channel_name_cache
    from src.gui.services.replay import HistMessage
    from src.gui.views.replay_view import _ReplayModel, ReplayRow
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))
    clear_channel_name_cache()
    msg = HistMessage(
        id=1, text="hi", received_at="2026-05-24T10:00:00+00:00",
        sender="a", source_channel_id="ch_a",
    )
    row = ReplayRow(
        msg=msg, original=[], replayed=[], drift="match",
        triage_decision=None, error="",
    )
    model = _ReplayModel()
    model.append(row)
    headers = list(_ReplayModel.HEADERS)
    ch_col = headers.index("Channel")
    idx = model.index(0, ch_col)
    assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "ch_a"
