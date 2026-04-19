import json
from src.ai_logger import log_call


def test_log_call_appends_jsonl(tmp_path):
    log_path = tmp_path / "ai.jsonl"
    log_call(log_path, {
        "prompt": "hello",
        "response": "world",
        "latency_ms": 42,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 7,
    })
    log_call(log_path, {"prompt": "again", "response": "x"})
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["prompt"] == "hello"
    assert "ts" in rec
