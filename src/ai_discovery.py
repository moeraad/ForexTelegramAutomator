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
from dataclasses import dataclass, field, replace
from string import Template

from src import config
from src.llm_provider import (
    AnthropicProvider,
    LLMProvider,
    OpenAIProvider,
)


# All buckets the classifier may return. Two additions beyond the live
# interpreter's action vocabulary:
#   - CONTEXT: analysis posts, status updates, looking-for-opportunity
#     messages. Not noise (operator may want to know) but not actionable.
#   - UNKNOWN: classifier confidence too low to commit to a bucket. Routed
#     to the review queue.
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
    "CANCEL_PENDING",  # was missing — caused "delete limit" messages to be misclassified as CLOSE_FULL
    "CONTEXT",         # analysis-only / status / looking-for-opportunity
    "ALERT",
    "IGNORE",
    "UNKNOWN",
)

_TEMPLATE = Template("""You are a classifier for trading-signal channel messages.

Read the single message below and decide which bucket(s) it belongs to.
Output JSON ONLY — no prose, no markdown fences.

# DECISION TREE — apply in order

Step 1: Is this trading-related at all?
  - Pure greetings, religious phrases standing alone, motivational quotes,
    emoji-only reactions, off-topic chat, channel ads, sponsored deposit
    bonuses, contest announcements, subscription pitches → IGNORE
  - Channel boasts ("VIP results this week", running-profit screenshots,
    "TP hit / SL hit" auto-close announcements) → IGNORE
  - Otherwise → continue to Step 2

Step 2: Is it about a trade ACTION or just COMMENTARY?
  - Pure analysis (chart annotations, "I see support at X", "I'm watching
    Y", "frame 4H: …"), forward-looking opinions without instruction,
    status updates ("10 pips to target"), "looking for opportunity" /
    "waiting for setup" → CONTEXT
  - An explicit trade instruction (open / close / move SL / partial /
    cancel / reinforce / tighten) → continue to Step 3

Step 3: Which action(s)?
  Most messages map to a single bucket. COMPOUND messages map to multiple
  (in execution order). Examples below.

# BUCKETS

OPEN          full new signal: entry (single price OR zone) + SL + ≥1 TP.
              `pending=true`  when wording is "limit" / "stop" / "wait for
                              pullback" / "wait for breakout"
              `pending=false` when wording is "now" / "market" / immediate
                              fill expected
MOVE_SL_BE    "move stop to breakeven", "secure entry", "use BE" (no price)
MOVE_SL       "move stop to <price>" — explicit price OR shorthand digits
CLOSE_PARTIAL "take half", "book partial profit", "half lot close"
CLOSE_FULL    "close the trade", "exit now", "we got out"
REOPEN_LAST   "re-enter last trade if not in" / "available again"
REINFORCE     explicit "reinforce / add to BUY/SELL" (close current + reopen)
TIGHTEN_SL    "tighten stop", "bring stop closer"
OPEN_INSTANT  bare directional ("buy gold" / "sell gold") with NO prices
ATTACH_SIGNAL structured entry+SL+TP arriving AFTER an OPEN_INSTANT, to
              wire SL/TPs to an already-open naked position
MODIFY_TPS    update only the TP ladder (no new SL or entry)
CANCEL_PENDING "delete limit", "cancel pending order", "remove the order"
CONTEXT       analysis / status / "looking for opportunity"
ALERT         risk warning the operator should see but not auto-trade
IGNORE        commentary, ads, greetings, TP/SL hit announcements
UNKNOWN       too garbled / off-topic to classify

# OUTPUT SHAPE

{
  "action_types": ["<bucket>", ...],
  "pending":  true | false | null,
  "phrase":   "<verbatim shortest fragment conveying intent>",
  "reasoning":"<one short sentence>",
  "confidence": <float 0.0 - 1.0>
}

- `action_types` is ALWAYS an array. Single-bucket → one entry. Compound
  ("half close, use BE") → multiple entries in execution order.
- `pending` is meaningful only when "OPEN" is in `action_types`; otherwise
  return null.
- "phrase" must be a substring of the input message.
- Set confidence below 0.7 only when you genuinely don't know — those
  go to the operator review queue.

# WORKED EXAMPLES (universal — channel-agnostic patterns)

Ex1 — Structured OPEN with zone, market fill:
  IN:  "GOLD BUY @ 4544-4542  TP1 4557  TP2 4564  TP3 4580  SL 4535"
  OUT: {"action_types":["OPEN"],"pending":false,"phrase":"GOLD BUY @ 4544-4542 SL 4535","reasoning":"structured buy zone with SL and 3 TPs","confidence":0.97}

Ex2 — Single-price OPEN, market fill:
  IN:  "Xauusd buy now @ 4593  SL 4590  Tp 4613"
  OUT: {"action_types":["OPEN"],"pending":false,"phrase":"buy now @ 4593 SL 4590 Tp 4613","reasoning":"single-price market buy","confidence":0.96}

Ex3 — Pending limit OPEN:
  IN:  "Xauusd buy limit 4670  SL 4662  Tp 4707"
  OUT: {"action_types":["OPEN"],"pending":true,"phrase":"buy limit 4670 SL 4662 Tp 4707","reasoning":"buy limit pending below current","confidence":0.96}

Ex4 — Pending stop OPEN (breakout):
  IN:  "Buy Stop 4750  SL 4720  TP 4800  — only if breaks resistance"
  OUT: {"action_types":["OPEN"],"pending":true,"phrase":"Buy Stop 4750 SL 4720 TP 4800","reasoning":"breakout buy stop pending","confidence":0.93}

Ex5 — Bare directional, no prices:
  IN:  "buy gold now"
  OUT: {"action_types":["OPEN_INSTANT"],"pending":null,"phrase":"buy gold now","reasoning":"directional only, no entry/SL/TP","confidence":0.92}

Ex6 — Compound partial + BE:
  IN:  "half close, use BE"
  OUT: {"action_types":["CLOSE_PARTIAL","MOVE_SL_BE"],"pending":null,"phrase":"half close use BE","reasoning":"compound: book half and move SL to entry","confidence":0.95}

Ex7 — Cancel pending:
  IN:  "Please delete pending order"
  OUT: {"action_types":["CANCEL_PENDING"],"pending":null,"phrase":"delete pending order","reasoning":"cancel a watching limit","confidence":0.96}

Ex8 — Analysis only, no command:
  IN:  "Gold daily frame: I see support at 4500, I expect ascent toward 4700"
  OUT: {"action_types":["CONTEXT"],"pending":null,"phrase":"support at 4500, ascent toward 4700","reasoning":"forward-looking analysis, no instruction","confidence":0.9}

Ex9 — Auto-close announcement:
  IN:  "TP1 ✅ +150 pips locked"
  OUT: {"action_types":["IGNORE"],"pending":null,"phrase":"TP1 hit","reasoning":"TP hit auto-closes at broker; informational","confidence":0.95}

Ex10 — Ad / promo:
  IN:  "🔥 Cashback offer — sign up at https://example.com/promo  30% bonus  https://t.me/somesupport"
  OUT: {"action_types":["IGNORE"],"pending":null,"phrase":"Cashback offer","reasoning":"sponsored promo with referral links","confidence":0.97}

Ex11 — Move SL to specific price:
  IN:  "move stop to 4650"
  OUT: {"action_types":["MOVE_SL"],"pending":null,"phrase":"move stop to 4650","reasoning":"explicit SL move","confidence":0.95}

Ex12 — Garbled / ambiguous:
  IN:  "still working"
  OUT: {"action_types":["UNKNOWN"],"pending":null,"phrase":"still working","reasoning":"no clear intent","confidence":0.4}

# MESSAGE

${message}
""")


@dataclass(frozen=True)
class Classification:
    """A single message's bucket assignment.

    `action_types` is a tuple to support COMPOUND messages — one channel
    message may carry multiple actions ("half close use BE" is both
    CLOSE_PARTIAL and MOVE_SL_BE). The classifier may return one or many.

    `pending` is meaningful only when "OPEN" is in `action_types`:
      True  → broker-side pending order ("buy limit", "sell limit")
      False → market entry ("buy now", "sell now")
      None  → not an OPEN, or pending intent not derivable from text
    """
    action_types: tuple[str, ...]
    phrase: str
    reasoning: str
    confidence: float
    pending: bool | None = None

    @property
    def action_type(self) -> str:
        """Backwards-compat shim: returns first action_type, or UNKNOWN if empty.

        Existing call sites that read `c.action_type` keep working until they
        migrate to iterate `c.action_types` for full compound semantics.
        """
        return self.action_types[0] if self.action_types else "UNKNOWN"


def build_discovery_provider() -> LLMProvider:
    """Classifier provider — inherits the live AI provider, defaults to the
    cheap tier for the selected vendor.

    The dedicated classifier_* tuning settings (provider override, batch
    size, concurrency, custom prompt) were removed when the wizard
    moved to a triage-first pipeline. The classifier only runs on
    triage-keep survivors now, so the volume is small enough that
    sensible vendor-cheap defaults are good enough.
    """
    raw = (config.AI_PROVIDER or "anthropic").lower()
    if raw == "openai":
        return OpenAIProvider(model="gpt-5-nano")
    return AnthropicProvider(model="claude-haiku-4-5-20251001")


_BATCH_TEMPLATE = Template("""You are a classifier for trading-signal channel messages.

Below are ${count} numbered messages. Classify EACH into one or more buckets:
OPEN | MOVE_SL_BE | MOVE_SL | CLOSE_PARTIAL | CLOSE_FULL | REOPEN_LAST | REINFORCE | TIGHTEN_SL | OPEN_INSTANT | ATTACH_SIGNAL | MODIFY_TPS | CANCEL_PENDING | CONTEXT | ALERT | IGNORE | UNKNOWN

# DECISION FLOW (per message)
1. Greetings / ads / promos / contests / "TP hit" / "SL hit" / running-profit
   screenshots / channel boasts → IGNORE
2. Pure analysis / forward-looking commentary / "looking for opportunity" /
   status updates ("10 pips to target") → CONTEXT
3. Explicit trade instruction → pick action bucket(s)

# BUCKET QUICK REFERENCE
OPEN: structured entry (price or zone) + SL + ≥1 TP. Pending bool:
  - "limit"/"stop"/"wait for pullback"/"breakout" → pending=true
  - "now"/"market"/zone fill expected            → pending=false
OPEN_INSTANT: bare "buy gold" / "sell gold" with NO prices
MOVE_SL_BE: "secure entry", "use BE", "move to breakeven"
MOVE_SL: "move stop to <price>" (explicit number)
TIGHTEN_SL: "tighten stop", "bring stop closer"
CLOSE_PARTIAL: "half close", "book half", "take partial"
CLOSE_FULL: "close trade", "exit now"
REOPEN_LAST: "re-enter last", "available again"
REINFORCE: explicit "reinforce", "add to position"
MODIFY_TPS: replace TP list only
CANCEL_PENDING: "delete limit", "cancel pending order"
ATTACH_SIGNAL: structured signal AFTER a prior OPEN_INSTANT (rare)

# COMPOUND
"half close, use BE" → ["CLOSE_PARTIAL", "MOVE_SL_BE"]
"close + reopen same direction" via REINFORCE alone, not [CLOSE_FULL, OPEN].

# OUTPUT
JSON ARRAY of EXACTLY ${count} objects, same order as inputs. No prose, no fences.
Each: {"action_types":["..."],"pending":true|false|null,"phrase":"...","reasoning":"...","confidence":0.0-1.0}

# MESSAGES
${messages}
""")


def _parse_action_types(item: dict) -> tuple[str, ...]:
    """Tolerant: accepts new shape `action_types: [...]` OR old `action_type: "X"`.

    Unknown bucket names get coerced to UNKNOWN. Empty / missing → ("UNKNOWN",).
    Duplicate entries are deduplicated while preserving first-occurrence order.
    """
    raw = item.get("action_types")
    if raw is None:
        raw_single = item.get("action_type")
        raw = [raw_single] if raw_single else []
    if not isinstance(raw, list):
        raw = [raw]
    seen: set[str] = set()
    out: list[str] = []
    for v in raw:
        if v is None:
            continue
        s = str(v).strip().upper()
        if not s:
            continue
        if s not in _ACTION_TYPES:
            s = "UNKNOWN"
        if s not in seen:
            seen.add(s)
            out.append(s)
    return tuple(out) if out else ("UNKNOWN",)


def _parse_pending(item: dict, action_types: tuple[str, ...]) -> bool | None:
    """`pending` is only meaningful when OPEN is in the bucket set."""
    if "OPEN" not in action_types:
        return None
    v = item.get("pending")
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1"):
            return True
        if s in ("false", "no", "0"):
            return False
    return None


# Confidence floor: classifier outputs below this are forced to UNKNOWN
# and surfaced in the wizard's review queue for the operator to triage.
# 0.7 chosen empirically — below this Haiku/gpt-5-nano tend to hedge with
# a generic bucket guess rather than admit they don't know.
CONFIDENCE_FLOOR = 0.7


def _apply_confidence_floor(c: Classification) -> Classification:
    """Force low-confidence classifications to UNKNOWN with annotated reasoning.

    Operates AFTER the LLM returns; the model never knows about the floor.
    Annotation prefix `low_confidence(<score>):` is the wizard UI's hook
    for the amber-highlight on review rows.
    """
    if c.confidence >= CONFIDENCE_FLOOR:
        return c
    if c.action_types == ("UNKNOWN",):
        return c  # already routed correctly; don't double-annotate
    return replace(
        c,
        action_types=("UNKNOWN",),
        reasoning=f"low_confidence({c.confidence:.2f}): {c.reasoning}",
    )


def _classification_from_item(item: dict) -> Classification:
    action_types = _parse_action_types(item)
    pending = _parse_pending(item, action_types)
    c = Classification(
        action_types=action_types,
        phrase=str(item.get("phrase", "")).strip(),
        reasoning=str(item.get("reasoning", "")).strip(),
        confidence=float(item.get("confidence", 0.0) or 0.0),
        pending=pending,
    )
    return _apply_confidence_floor(c)


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
            out.append(_classification_from_item(item))
        return out
    except Exception:
        # Per-message fallback so a single malformed batch doesn't lose data.
        return [classify(m, provider) for m in messages]


def classify(message: str, provider: LLMProvider) -> Classification:
    """Run the discovery prompt on one message; return the parsed bucket."""
    prompt = _TEMPLATE.substitute(
        message=message.strip(),
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
        return Classification(
            action_types=("UNKNOWN",),
            phrase="",
            reasoning=f"parse failed: {text[:80]}",
            confidence=0.0,
        )
    return _classification_from_item(data)
