import time
from dataclasses import dataclass
from typing import Any

import anthropic

from src.validators import AIResponse, parse_ai_response


SYSTEM_PROMPT = """You are a signal interpreter for a forex Telegram channel that posts gold (XAUUSD) trade ideas. You read incoming messages plus the current state of open positions and decide what trading actions to emit.

OUTPUT FORMAT:
You MUST output a single JSON object and nothing else. Schema:
{
  "actions": [ ... zero or more action objects ... ],
  "reasoning": "short explanation of why you chose these actions"
}

ACTION TYPES:

OPEN — a new trade signal:
  {"type":"OPEN","symbol":"XAUUSD","side":"BUY"|"SELL",
   "entry_low":<float>,"entry_high":<float>,
   "tps":[<float>,...],"sl":<float>,"comment":"short tag"}

MODIFY — change SL/TP on an existing position. Reference an mt5_ticket from OPEN POSITIONS:
  {"type":"MODIFY","mt5_ticket":<int>,"new_sl":<float|null>,"new_tp":<float|null>}

CLOSE — close one specific position by mt5_ticket:
  {"type":"CLOSE","mt5_ticket":<int>,"reason":"<text>"}

CLOSE_ALL — close every open position for the given symbol:
  {"type":"CLOSE_ALL","symbol":"XAUUSD","reason":"<text>"}

ALERT — info only, no trade:
  {"type":"ALERT","level":"info"|"warning","text":"<text>"}

DECISION RULES:
1. Emit OPEN only when the message is a CLEAR new trade with at least an entry, an SL, and one TP. Vague analysis or commentary → no OPEN.
2. If the message references an existing position (e.g. "move SL to BE", "take partial at TP1", "close half"), emit MODIFY or CLOSE with the right mt5_ticket from OPEN POSITIONS. If you can't tell which position, emit ALERT.
3. "Close all gold", "exit everything", "out now" → CLOSE_ALL.
4. News warnings ("NFP coming, be careful"), opinions, market commentary → ALERT only.
5. NEVER emit OPEN for a signal that is already represented in OPEN POSITIONS (entry zone overlaps, same side).
6. If you are uncertain, emit ALERT and explain in `reasoning`. Do NOT emit speculative trades.
7. Symbol is always XAUUSD. If a non-gold instrument is mentioned, emit ALERT, do not OPEN.

Be precise. Output JSON ONLY."""


def build_messages(
    recent_chat: str,
    open_positions_block: str,
    new_message: str,
) -> list[dict[str, Any]]:
    """Build the messages list for the Anthropic API.

    First block (recent chat) is marked with cache_control so that
    repeated calls reuse the cached prefix. The second block holds the
    volatile open positions + new message and is NOT cached.
    """
    cached_block = {
        "type": "text",
        "text": f"RECENT CHAT (last messages, oldest first):\n{recent_chat}",
        "cache_control": {"type": "ephemeral"},
    }
    volatile_block = {
        "type": "text",
        "text": f"{open_positions_block}\n\nNEW MESSAGE:\n{new_message}",
    }
    return [{"role": "user", "content": [cached_block, volatile_block]}]


@dataclass
class AICallResult:
    response: AIResponse
    raw_text: str
    usage: dict[str, int]
    latency_ms: int


class AIClient:
    def __init__(
        self,
        client: Any = None,
        model: str = "claude-sonnet-4-6",
        max_retries: int = 3,
        retry_sleep: float = 1.5,
    ):
        self._client = client if client is not None else anthropic.Anthropic()
        self._model = model
        self._max_retries = max_retries
        self._retry_sleep = retry_sleep

    def call(
        self,
        recent_chat: str,
        open_positions_block: str,
        new_message: str,
    ) -> AICallResult:
        messages = build_messages(recent_chat, open_positions_block, new_message)
        system = [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        last_err: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                start = time.monotonic()
                resp = self._client.messages.create(
                    model=self._model,
                    max_tokens=1024,
                    system=system,
                    messages=messages,
                )
                raw_text = resp.content[0].text
                parsed = parse_ai_response(raw_text)
                usage = {
                    "input_tokens": getattr(resp.usage, "input_tokens", 0),
                    "output_tokens": getattr(resp.usage, "output_tokens", 0),
                    "cache_read_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
                    "cache_creation_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
                }
                latency_ms = int((time.monotonic() - start) * 1000)
                return AICallResult(
                    response=parsed,
                    raw_text=raw_text,
                    usage=usage,
                    latency_ms=latency_ms,
                )
            except Exception as e:
                last_err = e
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_sleep * (2 ** attempt))
        raise RuntimeError(f"AI call failed after {self._max_retries} attempts: {last_err}")


def call_ai(
    recent_chat: str,
    open_positions_block: str,
    new_message: str,
) -> AICallResult:
    return AIClient().call(recent_chat, open_positions_block, new_message)
