import time
from dataclasses import dataclass
from typing import Any

from src import config
from src.llm_provider import (
    AnthropicProvider,
    LLMProvider,
    build_interpreter_provider,
    reasoning_level,
)
from src.validators import AIResponse, parse_ai_response


SYSTEM_PROMPT = """You are a signal interpreter for a forex Telegram channel that posts gold (XAUUSD) trade ideas. You read incoming messages plus the current state of open positions and decide what trading actions to emit.

MESSAGE TRIAGE FLOW — apply these tiers in order to every incoming message:

Tier 1 — IGNORE (category="ignore"):
  Promotional, motivational, greetings, ads, emoji-only reactions, channel
  announcements unrelated to gold, general chit-chat. Emit ZERO actions.

Tier 2 — CONTEXT (category="context"):
  The message carries gold-relevant information but is NOT a trade instruction:
  market commentary, bias, zones being watched, news awareness, post-trade
  reflections, TP-hit confirmations. Emit exactly ONE ALERT with level="info"
  whose `text` distills the memorable fact in ≤200 characters, starting with
  the tag "[context] ". This preserves the fact in the recent-chat window so
  future messages can reason against it.

Tier 3 — SIGNAL (category="signal"):
  A COMPLETE new trade (entry + SL + at least one TP + inferable side) OR a
  clear management instruction (move SL, partial close, close all). Emit the
  appropriate OPEN / MODIFY / CLOSE / CLOSE_ALL action(s).

Tier 4 — PARTIAL SIGNAL (category="partial_signal"):
  The message looks trade-ish (prices, direction cues, "enter now", etc.) but
  at least one of entry / SL / TP / side is missing or ambiguous. THIS IS
  WHERE YOU THINK HARDEST. Use the reasoning budget fully:
    a. Apply the PRICE DECODING rules below to expand any shorthand.
    b. Cross-reference OPEN POSITIONS and RECENT CHAT — the missing piece may
       already be established (e.g. SL stated in a previous message, or
       direction implied by an open position the user is managing).
    c. If after reasoning you can fully reconstruct a complete trade, PROMOTE
       to Tier 3 and emit the OPEN/MODIFY/CLOSE as if it were Tier 3. Set
       category="signal".
    d. If it remains incomplete, emit exactly ONE ALERT with level="warning"
       whose `text` lists precisely what is missing, starting with
       "[partial] " (e.g. "[partial] entries 4794-4795 inferred but SL not
       given; side ambiguous").

OUTPUT FORMAT:
You MUST output a single JSON object and nothing else. Schema:
{
  "category": "ignore" | "context" | "signal" | "partial_signal",
  "actions": [ ... zero or more action objects ... ],
  "reasoning": "short explanation: which tier, why, and any key inferences"
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

PRICE DECODING (CRITICAL — read carefully):
- This channel posts gold (XAUUSD) signals. As of 2026, gold trades roughly in the 4000-5500 USD/oz range. Do NOT assume sub-3000 prices from training-data priors.
- Messages frequently quote entries and targets as ONLY the last two digits, e.g. "من 95-94" (from 95-94), "TP 85", "الأهداف 85 ثم 65" (targets 85 then 65). You MUST expand these to full 4-digit prices.
- To expand shorthand, use any explicit 4-digit price in the SAME message (SL, TP, entry) as the anchor for the thousands+hundreds digits. Messages almost always contain at least one explicit 4-digit SL or TP.
- Direction constraint for expansion:
    * SELL: entries sit BELOW the SL; TPs sit further below the entries.
    * BUY:  entries sit ABOVE the SL; TPs sit further above the entries.
  Pick the thousands+hundreds digits that place each shorthand value on the correct side of the explicit anchor.
- NEVER rewrite an explicit 4-digit number. If the message says "ستوب 4808" / "SL 4808", emit sl=4808 verbatim. Do not shift it to 2908, 3808, 5808, etc.
- Side inference: if the word BUY/SELL / شراء / بيع is absent, derive the side from the SL vs entry relationship (SL above entry → SELL; SL below entry → BUY).
- WORKED EXAMPLE:
    Message: "نبدأ الصفقة (0.01) من 95-94. ستوب 4808. الأهداف 85 أولاً ثم 65 ثانياً"
    Anchor: SL=4808 (explicit, keep as-is). SL above entries → SELL.
    Entries shorthand "95-94" → choose 47xx (below 4808) → 4794, 4795.
    TP "85" → below entries → 4785.  TP "65" → further below → 4765.
    Output: side=SELL, entry_low=4794, entry_high=4795, sl=4808, tps=[4785, 4765].

DECISION RULES (apply within the tier logic above):
1. OPEN requires entry + SL + ≥1 TP + side (inferred if needed). If any of
   those is genuinely missing after deep reasoning, stay in Tier 4.
2. Position management (e.g. "move SL to BE", "take partial at TP1",
   "close half") → MODIFY or CLOSE by mt5_ticket from OPEN POSITIONS. If you
   cannot identify the target ticket → Tier 4 ALERT.
3. "Close all gold", "exit everything", "out now" → CLOSE_ALL (Tier 3).
4. NEVER emit OPEN for a signal already represented:
   a. Overlapping entry zone + same symbol+side in OPEN POSITIONS.
   b. Overlapping entry zone + same symbol+side in PENDING OPEN SIGNALS —
      these are limits already queued; re-issuing creates duplicates.
   c. The channel is RE-POSTING or REFERENCING/QUOTING an earlier signal
      from RECENT CHAT ("as mentioned", "update on the gold buy",
      "still valid", quoted blocks, forwards). Treat as Tier 2 CONTEXT
      (category="context") with a short ALERT note.
5. Symbol is always XAUUSD. A non-gold instrument → Tier 2 CONTEXT with a
   single ALERT level="info" like "[context] non-gold instrument <X>
   mentioned; skipped" so the user still has visibility.

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
        thinking_enabled: bool | None = None,
        thinking_budget_tokens: int | None = None,
        provider: LLMProvider | None = None,
    ):
        # Resolution order:
        #   1. explicit `provider=` (new-style injection)
        #   2. legacy `client=` (raw SDK client) → wrap as AnthropicProvider so
        #      existing tests that pass an anthropic-shaped MagicMock still work
        #   3. build from config (AI_PROVIDER switch)
        if provider is not None:
            self._provider: LLMProvider = provider
        elif client is not None:
            self._provider = AnthropicProvider(client=client, model=model)
        else:
            self._provider = build_interpreter_provider(model=model)
        self._max_retries = max_retries
        self._retry_sleep = retry_sleep
        self._thinking_enabled = (
            config.AI_THINKING_ENABLED if thinking_enabled is None else thinking_enabled
        )
        self._thinking_budget = (
            config.AI_THINKING_BUDGET_TOKENS
            if thinking_budget_tokens is None
            else thinking_budget_tokens
        )

    def call(
        self,
        recent_chat: str,
        open_positions_block: str,
        new_message: str,
    ) -> AICallResult:
        cached_prefix = f"RECENT CHAT (last messages, oldest first):\n{recent_chat}"
        volatile_suffix = f"{open_positions_block}\n\nNEW MESSAGE:\n{new_message}"
        level = reasoning_level(self._thinking_enabled, self._thinking_budget)
        output_budget = 1024
        last_err: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                result = self._provider.interpret(
                    system_prompt=SYSTEM_PROMPT,
                    cached_prefix=cached_prefix,
                    volatile_suffix=volatile_suffix,
                    max_output_tokens=output_budget,
                    reasoning_level=level,
                )
                parsed = parse_ai_response(result.raw_text)
                return AICallResult(
                    response=parsed,
                    raw_text=result.raw_text,
                    usage=result.usage,
                    latency_ms=result.latency_ms,
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
