# tests/test_api_manual.py
import pytest
from pydantic import ValidationError
from src.api_models import ManualOpenBody


def test_manual_open_body_minimal_market():
    b = ManualOpenBody(side="BUY", entry=4500.0, sl=4490.0, tp=4530.0, lot=0.05)
    assert b.symbol == "XAUUSD"
    assert b.pending is False
    assert b.comment == "manual"


def test_manual_open_body_rejects_nonpositive_lot():
    with pytest.raises(ValidationError):
        ManualOpenBody(side="BUY", entry=4500.0, sl=4490.0, tp=4530.0, lot=0.0)


def test_manual_open_body_rejects_bad_side():
    with pytest.raises(ValidationError):
        ManualOpenBody(side="LONG", entry=4500.0, sl=4490.0, tp=4530.0, lot=0.05)
