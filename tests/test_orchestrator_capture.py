"""The orchestrator appends a learning_samples row on the interpret path."""
from __future__ import annotations

from dataclasses import dataclass

from src import learning_store, orchestrator
from src.db import connect, init_schema


@dataclass
class _Resp:
    category: str
    actions: list


@dataclass
class _Result:
    response: _Resp
    raw_text: str = "{}"
    latency_ms: int = 1
    usage: dict = None

    def __post_init__(self):
        if self.usage is None:
            object.__setattr__(self, "usage", {})


class _FakeAI:
    def __init__(self, category, actions):
        self._r = _Result(_Resp(category, actions))

    def call(self, *a, **k):
        return self._r


def test_interpret_verdict_is_captured(tmp_path, monkeypatch):
    monkeypatch.setenv("COPYTRADES_DISABLE_EVALUATOR", "1")
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    ai = _FakeAI("ignore", [])  # noise that triage let through
    orchestrator.process_message(
        conn, ai, tg_message_id=1, chat_id=99, sender="x",
        text="مبروك على الارباح", ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=0, source_channel_id="chX", route_id="rX",
    )
    rows = learning_store.recent(conn, "chX", 10)
    assert len(rows) == 1
    assert rows[0].category == "ignore"
    assert rows[0].text == "مبروك على الارباح"
