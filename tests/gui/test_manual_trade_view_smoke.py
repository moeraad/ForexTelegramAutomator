# tests/gui/test_manual_trade_view_smoke.py
import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_chart_panel_instantiates_and_sets_candles(qapp):
    from src.gui.views.manual_trade_chart import ChartPanel
    panel = ChartPanel()
    bars = [
        {"t": "2026-06-09T10:00:00+00:00", "o": 4500, "h": 4505, "l": 4498, "c": 4502, "v": 10},
        {"t": "2026-06-09T10:15:00+00:00", "o": 4502, "h": 4508, "l": 4501, "c": 4507, "v": 12},
    ]
    panel.set_candles(bars)          # must not raise
    panel.set_live_price(4506.0)     # must not raise
    panel.arm_order_lines(entry=4503.0, line_a=4530.0, line_b=4490.0)
    entry, a, b = panel.line_values()
    assert entry == 4503.0
    assert {a, b} == {4530.0, 4490.0}


def test_chart_panel_zoom_controls_do_not_raise(qapp):
    from src.gui.views.manual_trade_chart import ChartPanel
    panel = ChartPanel()
    bars = [
        {"t": f"2026-06-09T10:{m:02d}:00+00:00", "o": 4500 + i, "h": 4505 + i,
         "l": 4498 + i, "c": 4502 + i, "v": 10}
        for i, m in enumerate(range(0, 50))
    ]
    panel.set_candles(bars)
    # Every zoom affordance must be callable without raising.
    panel.zoom_in()
    panel.zoom_out()
    panel.show_latest()
    panel.reset_view()
    # Calling the controls before any data arrived must also be safe.
    empty = ChartPanel()
    empty.zoom_in()
    empty.reset_view()
    empty.show_latest()


def _fake_stack(tmp_path):
    from src.db import connect, init_schema
    db = tmp_path / "stack.db"
    conn = connect(str(db))
    init_schema(conn)
    conn.execute("INSERT INTO settings(key,value) VALUES('account_balance','1000')")
    conn.commit()
    conn.close()

    class _Stack:
        db_path = db
        api_url = "http://127.0.0.1:8766"
        name = "TEST"
    return _Stack()


def test_manual_trade_view_instantiates(qapp, tmp_path):
    from src.gui.views.manual_trade_view import ManualTradeView
    view = ManualTradeView(_fake_stack(tmp_path))
    assert view is not None
    # recompute with no armed lines must not raise
    view._recompute()


def test_manual_trade_view_computes_lot_when_armed(qapp, tmp_path):
    from src.gui.views.manual_trade_view import ManualTradeView
    view = ManualTradeView(_fake_stack(tmp_path))
    view._chart.arm_order_lines(entry=4500.0, line_a=4530.0, line_b=4490.0)
    view._side = "BUY"
    view._recompute()
    # balance 1000, default lot_per_100 0.01 -> base 0.10, well above min
    assert view._last_sizing is not None
    assert view._last_sizing.final_lot > 0
