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
