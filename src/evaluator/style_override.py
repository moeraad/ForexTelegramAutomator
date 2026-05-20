"""Per-signal trading-style override detection from message text.

When a channel mixes signal types (some scalps, some swings) the message
usually says so explicitly. Detect those keywords cheaply with a regex
pass — no LLM call needed for the common case. The detection is opt-in
per stack via the `trading_style_ai_override` setting.

The taxonomy is intentionally conservative: only override when the
message DECLARES a style. Ambiguous wording falls back to the stack
default; better to use the wrong rubric occasionally than to mis-infer
intent and reduce trust in the override mechanism.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.trading_style import TradingStyle


@dataclass(frozen=True)
class StyleOverride:
    """Result of a style-override probe.

    `style` is None when the message doesn't declare one — caller uses
    the stack default. When non-None, `matched_phrase` carries the
    substring that triggered the override (for forensic logging).
    """
    style: TradingStyle | None
    matched_phrase: str = ""


# Keyword tables. Arabic forms come from the Forex Engineer channel's
# observed vocabulary; English forms cover any channel that mixes in
# trader jargon. Patterns use `\b`-ish word boundaries via lookbehind/
# lookahead so "scalper" doesn't match "alps" etc. Arabic is matched
# case-insensitively but the Arabic alphabet is naturally case-free.
#
# Order matters: more-specific styles checked first so "long swing"
# doesn't accidentally hit "position trade" if both keywords appear.

_PATTERNS: list[tuple[TradingStyle, list[str]]] = [
    (TradingStyle.SCALP, [
        r"\bscalp(?:ing|er)?\b",
        r"\bquick\s+trade\b",
        r"\bfast\s+(?:trade|in\s+and\s+out)\b",
        r"اسكالب",
        r"سكالب",
        r"صفقة\s+سريعة",
        r"سريعة\s+فقط",
    ]),
    (TradingStyle.SWING, [
        r"\bswing(?:\s+trade)?\b",
        r"\bmulti[-\s]?day\b",
        r"\bhold\s+overnight\b",
        r"صفقة\s+طويلة",
        r"مدى\s+طويل(?:\s+نوعا\s+ما)?",
        r"عدة\s+ايام",
    ]),
    (TradingStyle.POSITION, [
        r"\bposition\s+trade\b",
        r"\blong[-\s]term\s+(?:position|hold)\b",
        r"\binvestment\s+horizon\b",
        r"استثمار\s+طويل\s+المدى",
        r"اسبوع",  # "weeks" — used in position context
        r"\bweeks?\s+to\s+months?\b",
    ]),
    (TradingStyle.INTRADAY, [
        # Explicit intraday is rare but worth catching when channels say
        # "close before NY end" or similar. Listed LAST so its less-
        # specific patterns don't shadow swing/position.
        r"\bintraday\b",
        r"\bday\s+trade\b",
        r"close\s+before\s+(?:NY\s+)?(?:close|end)",
        r"خلال\s+اليوم",
        r"اليوم\s+فقط",
    ]),
]


# Pre-compile the patterns. Cache at module-import time; live calls
# only run already-compiled regexes.
_COMPILED: list[tuple[TradingStyle, list[re.Pattern[str]]]] = [
    (style, [re.compile(p, re.IGNORECASE) for p in patterns])
    for style, patterns in _PATTERNS
]


def detect(message: str) -> StyleOverride:
    """Scan the message for style-declaring keywords. Returns the first
    style whose pattern matches, or `StyleOverride(None)` when nothing
    matches.

    Matching is greedy at the per-style level (any pattern in the style's
    list wins) and ordered across styles (scalp > swing > position >
    intraday). This bias is deliberate: shorter-hold declarations are
    more specific signals when present, while "intraday" is the
    default we fall back to anyway.

    No allocations beyond the regex match objects. Safe to call on
    every incoming message.
    """
    if not message:
        return StyleOverride(None)
    for style, patterns in _COMPILED:
        for pat in patterns:
            m = pat.search(message)
            if m:
                return StyleOverride(style=style, matched_phrase=m.group(0))
    return StyleOverride(None)


def resolve(
    stack_default: TradingStyle | str,
    message: str,
    ai_override_enabled: bool,
) -> tuple[TradingStyle, str]:
    """Compose the override probe with the stack default.

    Returns `(effective_style, source)` where source is one of:
      - "stack_default"            -> no override or override disabled
      - "ai_override:<phrase>"     -> message declared a different style

    `stack_default` accepts the enum or its string tag (the settings
    layer stores strings). Unknown strings fall back to the package
    DEFAULT_STYLE inside `get_spec` — but we don't catch that here; let
    the caller validate the setting at boot.
    """
    if isinstance(stack_default, str):
        stack_default = TradingStyle(stack_default)
    if not ai_override_enabled:
        return stack_default, "stack_default"
    probe = detect(message)
    if probe.style is None or probe.style == stack_default:
        return stack_default, "stack_default"
    return probe.style, f"ai_override:{probe.matched_phrase}"
