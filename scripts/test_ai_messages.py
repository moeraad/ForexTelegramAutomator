"""Manual probe: does the AI correctly parse shorthand Arabic gold signals?

Run:
    python scripts/test_ai_messages.py

Requires ANTHROPIC_API_KEY in the environment (or .env).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ai import AIClient  # noqa: E402
from src import config  # noqa: E402


MESSAGE_1 = (
    "بسم الله نبدأ الصفقة الأولى (0.01) من 95-94\n"
    "ستوب 4808\n"
    "والأهداف 85 أولاً ثم 65 ثانياً - و ثالثاً لوقتها"
)

MESSAGE_2 = (
    "TP1 ✅\n"
    "ربح 110 نقطة من مستويات 4794\n"
    "ربح 120 نقطة من مستويات 4795\n"
    "الحمدلله"
)

EXPECTED_1 = {
    "side": "SELL",
    "entry_low": 4794,
    "entry_high": 4795,
    "sl": 4808,
    "tps_contains": [4785, 4765],
}


def _divider(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _run(ai: AIClient, recent_chat: str, open_positions: str,
         new_message: str, expected: dict | None) -> None:
    print(f"\nNEW MESSAGE:\n{new_message}\n")
    result = ai.call(recent_chat, open_positions, new_message)
    print(f"latency_ms={result.latency_ms}  usage={result.usage}")
    print("\nRAW RESPONSE:")
    print(result.raw_text)
    print("\nPARSED ACTIONS:")
    for a in result.response.actions:
        print(f"  - {a.model_dump()}")
    print(f"\nREASONING: {result.response.reasoning}")

    if expected:
        print("\nCHECKS:")
        opens = [a for a in result.response.actions
                 if a.__class__.__name__ == "OpenAction"]
        if not opens:
            print("  [FAIL] no OPEN action emitted")
            return
        op = opens[0]
        d = op.model_dump()
        ok = True
        for key in ("side", "entry_low", "entry_high", "sl"):
            want = expected[key]
            got = d[key]
            mark = "OK" if got == want else "FAIL"
            if got != want:
                ok = False
            print(f"  [{mark}] {key}: got={got} want={want}")
        tps_want = expected["tps_contains"]
        tps_got = d["tps"]
        if all(tp in tps_got for tp in tps_want):
            print(f"  [OK] tps contains {tps_want}: got={tps_got}")
        else:
            ok = False
            print(f"  [FAIL] tps missing some of {tps_want}: got={tps_got}")
        print(f"\nSUMMARY: {'PASS' if ok else 'FAIL'}")


def main() -> None:
    if not config.ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY is not set. Aborting.")
        sys.exit(1)

    ai = AIClient(model=config.ANTHROPIC_MODEL)

    _divider("TEST 1 — Arabic shorthand SELL signal (95-94 / 4808 / 85 / 65)")
    _run(
        ai,
        recent_chat="(no prior messages)",
        open_positions="OPEN POSITIONS:\n  (none)",
        new_message=MESSAGE_1,
        expected=EXPECTED_1,
    )

    _divider("TEST 2 — TP1 confirmation referencing the prior signal")
    prior_chat = f"[earlier] FX Yusuf: {MESSAGE_1}"
    open_positions = (
        "OPEN POSITIONS:\n"
        "  Signal #1:\n"
        "    ticket=70001 SELL XAUUSD vol=0.01 entry=4794.50 sl=4808.00 tp=4785.00\n\n"
        "PENDING OPEN SIGNALS:\n  (none)"
    )
    _run(
        ai,
        recent_chat=prior_chat,
        open_positions=open_positions,
        new_message=MESSAGE_2,
        expected=None,
    )


if __name__ == "__main__":
    main()
