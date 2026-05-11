import json
import os
import pytest
from pathlib import Path
from src.ai import AIClient

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "messages.jsonl"

pytestmark = pytest.mark.skipif(
    not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")),
    reason="No LLM API key set; skipping live AI tests."
)


def _load_fixtures():
    with open(FIXTURE_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.mark.parametrize("fx", _load_fixtures(), ids=lambda fx: fx["id"])
def test_replay(fx):
    client = AIClient()
    recent = "\n".join(fx["prior_chat"])
    res = client.call(recent, fx["open_positions_block"], fx["new_message"])
    actual_types = sorted(a.type for a in res.response.actions)
    expected_types = sorted(fx["expected_action_types"])
    assert actual_types == expected_types, (
        f"types mismatch in {fx['id']}: got {actual_types}, expected {expected_types}\n"
        f"reasoning: {res.response.reasoning}"
    )
    for atype, must in fx.get("must_contain", {}).items():
        match = next((a for a in res.response.actions if a.type == atype), None)
        assert match is not None, f"{atype} missing in {fx['id']}"
        for k, v in must.items():
            actual_v = getattr(match, k)
            assert actual_v == v, (
                f"{fx['id']} {atype}.{k}: got {actual_v}, expected {v}"
            )
