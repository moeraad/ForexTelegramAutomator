"""Channel-agnostic message classifier used by the profile generator wizard.

This is the ONLY classifier that runs without a pre-existing channel
profile (chicken-and-egg: we're trying to *generate* the profile). It
maps a raw message string to one of the 14 buckets so we can later
derive vocabulary_table / worked_examples / triage_keep_triggers from
the grouped output.

Stays deliberately minimal — no idempotency rules, no state, no
shorthand decode. Just "what kind of message is this".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from string import Template

from src import config
from src.llm_provider import (
    AnthropicProvider,
    LLMProvider,
    OpenAIProvider,
)


_ACTION_TYPES = (
    "OPEN",
    "MOVE_SL_BE",
    "MOVE_SL",
    "CLOSE_PARTIAL",
    "CLOSE_FULL",
    "REOPEN_LAST",
    "REINFORCE",
    "TIGHTEN_SL",
    "OPEN_INSTANT",
    "ATTACH_SIGNAL",
    "MODIFY_TPS",
    "ALERT",
    "IGNORE",
    "UNKNOWN",
)

_TEMPLATE = Template("""You are a classifier for trading-signal channel messages.

Read the single message below and decide which ONE of these buckets it
belongs to. Output JSON ONLY — no prose.

BUCKETS:
- OPEN            : full new trade signal with entry zone + SL + at least one TP.
- MOVE_SL_BE      : "move stop to breakeven", "secure entry" (no explicit price).
- MOVE_SL         : "move stop to <price>" (with a price, either explicit or shorthand).
- CLOSE_PARTIAL   : "take half", "book partial profit".
- CLOSE_FULL      : "close the trade", "we got out".
- REOPEN_LAST     : "re-enter last trade if you're not in" / "available again".
- REINFORCE       : "reinforce BUY/SELL" (close current + reopen).
- TIGHTEN_SL      : "tighten stop", "bring stop closer".
- OPEN_INSTANT    : bare directional command like "BUY gold" / "SELL gold" with NO entry/SL/TP.
- ATTACH_SIGNAL   : structured entry/SL/TP arriving AFTER a bare directional, to attach to a naked position.
- MODIFY_TPS      : update only the take-profit list (no new SL or entry).
- ALERT           : ambiguous risk/warning that should ping the operator but not auto-trade.
- IGNORE          : commentary, encouragement, religious phrases, time/scheduling references, anything non-actionable.
- UNKNOWN         : cannot be confidently classified (use sparingly).

OUTPUT JSON SHAPE:
{
  "action_type": "<one bucket name>",
  "phrase": "<the short trigger phrase from the message, verbatim>",
  "reasoning": "<one short sentence>",
  "confidence": <float 0.0 - 1.0>
}

RULES:
- "phrase" must be a substring of the input message, the shortest piece
  that conveys the intent. For OPEN signals, use a representative
  fragment like "BUY @ 1234 SL 1200 TP 1300".
- Use IGNORE liberally for commentary. Better to drop than to misclassify.
- Use UNKNOWN only when the message is too garbled or off-topic to map.
${custom_rules_block}
MESSAGE:
${message}
""")


@dataclass(frozen=True)
class Classification:
    action_type: str
    phrase: str
    reasoning: str
    confidence: float


def _custom_rules_block() -> str:
    """Return the ``${custom_rules_block}`` substitution: either an empty
    string (no custom rules set) or a CUSTOM CHANNEL RULES section the
    classifier will treat as part of the prompt.
    """
    raw = (config.CLASSIFIER_CUSTOM_PROMPT or "").strip()
    if not raw:
        return ""
    return "\n\nCUSTOM CHANNEL RULES (operator-supplied, follow these):\n" + raw + "\n"


def build_discovery_provider() -> LLMProvider:
    """Classifier provider.

    Resolution order:
      1. classifier_provider DB setting (if non-empty)
      2. ai_provider DB setting
      3. fallback to anthropic

    Model resolved separately from classifier_anthropic_model /
    classifier_openai_model with sensible defaults.
    """
    raw = (config.CLASSIFIER_PROVIDER or "").strip().lower()
    if raw not in ("anthropic", "openai"):
        raw = (config.AI_PROVIDER or "anthropic").lower()
    if raw == "openai":
        model = config.CLASSIFIER_OPENAI_MODEL or "gpt-5-nano"
        return OpenAIProvider(model=model)
    model = config.CLASSIFIER_ANTHROPIC_MODEL or "claude-haiku-4-5-20251001"
    return AnthropicProvider(model=model)


_BATCH_TEMPLATE = Template("""You are a classifier for trading-signal channel messages.

Below are ${count} numbered messages. Classify EACH into ONE of these buckets:
OPEN | MOVE_SL_BE | MOVE_SL | CLOSE_PARTIAL | CLOSE_FULL | REOPEN_LAST | REINFORCE | TIGHTEN_SL | OPEN_INSTANT | ATTACH_SIGNAL | MODIFY_TPS | ALERT | IGNORE | UNKNOWN

Output a JSON ARRAY of exactly ${count} objects, in the same order as the inputs.
Each object: {"action_type":"<bucket>","phrase":"<short verbatim trigger from this message>","reasoning":"<one short sentence>","confidence":<0.0-1.0>}

RULES:
- Output the array ONLY, no prose, no code fences.
- The array must have EXACTLY ${count} items, even if some are IGNORE or UNKNOWN.
- "phrase" must be a substring of the input message, the shortest piece that conveys intent.
- Use IGNORE liberally for commentary; UNKNOWN only when too garbled to classify.
${custom_rules_block}
MESSAGES:
${messages}
""")


def classify_batch(messages: list[str], provider: LLMProvider) -> list[Classification]:
    """Classify a list of messages in ONE provider call.

    On any failure (parse error, wrong array length, malformed item), falls
    back to per-message ``classify`` so the caller never has to retry.
    """
    if not messages:
        return []
    numbered = "\n\n".join(
        f"{i + 1}. {m.strip()}" for i, m in enumerate(messages)
    )
    prompt = _BATCH_TEMPLATE.substitute(
        count=len(messages),
        messages=numbered,
        custom_rules_block=_custom_rules_block(),
    )
    try:
        result = provider.triage(
            system_prompt="Reply with strict JSON only. No code fences.",
            user_content=prompt,
            max_output_tokens=min(4000, 200 * len(messages) + 200),
        )
        text = (result.raw_text or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        data = json.loads(text)
        if not isinstance(data, list) or len(data) != len(messages):
            raise ValueError(
                f"expected array of {len(messages)} items, got "
                f"{type(data).__name__} of length "
                f"{len(data) if isinstance(data, list) else 'n/a'}"
            )
        out: list[Classification] = []
        for item in data:
            if not isinstance(item, dict):
                raise ValueError(f"non-dict array item: {item!r}")
            at = str(item.get("action_type", "UNKNOWN")).upper()
            if at not in _ACTION_TYPES:
                at = "UNKNOWN"
            out.append(Classification(
                action_type=at,
                phrase=str(item.get("phrase", "")).strip(),
                reasoning=str(item.get("reasoning", "")).strip(),
                confidence=float(item.get("confidence", 0.0) or 0.0),
            ))
        return out
    except Exception:
        # Per-message fallback so a single malformed batch doesn't lose data.
        return [classify(m, provider) for m in messages]


def classify(message: str, provider: LLMProvider) -> Classification:
    """Run the discovery prompt on one message; return the parsed bucket."""
    prompt = _TEMPLATE.substitute(
        message=message.strip(),
        custom_rules_block=_custom_rules_block(),
    )
    result = provider.triage(
        system_prompt="Reply with strict JSON only. No code fences.",
        user_content=prompt,
        max_output_tokens=400,
    )
    text = (result.raw_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return Classification("UNKNOWN", "", f"parse failed: {text[:80]}", 0.0)
    at = str(data.get("action_type", "UNKNOWN")).upper()
    if at not in _ACTION_TYPES:
        at = "UNKNOWN"
    return Classification(
        action_type=at,
        phrase=str(data.get("phrase", "")).strip(),
        reasoning=str(data.get("reasoning", "")).strip(),
        confidence=float(data.get("confidence", 0.0) or 0.0),
    )
