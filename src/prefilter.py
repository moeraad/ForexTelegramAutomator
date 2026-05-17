"""Universal, language-agnostic pre-filter that runs BEFORE the LLM stages.

Two cheap deterministic gates:

  1. should_drop_by_symbol — drops messages that ONLY reference instruments
     the operator has listed as "other_instruments" in the channel profile,
     and DO NOT reference any "symbol_aliases" for the target instrument.
     Channel-specific vocabulary lives entirely in profile.json (the
     `symbol_aliases` and `other_instruments` lists); the function itself
     has NO hardcoded language keywords.

  2. looks_like_ad — detects ad-shaped messages using only universal
     signals: URL count, total length, percent-with-currency patterns. No
     language keywords. Works on Arabic XM promos and Russian VIP ads
     the same way.

Both gates default-to-keep when their config is empty or signals are
inconclusive — false positives are much worse than false negatives at
this stage (the LLM triage downstream is the real filter).

These same functions run in BOTH the live orchestrator and the wizard
pipeline, so a channel's pre-filter behavior is identical offline and
online.
"""
from __future__ import annotations

import re


# Three regex constants compiled at module load. All are deliberately
# generic — they trigger on shape, not on any specific token.

_URL_RE = re.compile(
    r"https?://\S+|t\.me/\S+|wa\.me/\S+|whatsapp\.com/\S+",
    re.IGNORECASE,
)

# Matches "30%" / "25 %" / "$5 to $5,000" / "€500" / "5%cashback" style
# fragments — the visual signature of promos. We use it AND high URL
# density to confirm an ad, never alone.
_PCT_OR_CURRENCY_RE = re.compile(
    r"(?:\b\d{1,3}\s*%)|(?:[$€£¥]\s*\d)|(?:\b\d+[,.]\d+\s*[$€£¥])",
)


def _has_alias(text: str, aliases: list[str]) -> bool:
    """Case-insensitive substring match for any of the aliases."""
    if not aliases:
        return False
    text_l = text.lower()
    return any(a and a.lower() in text_l for a in aliases)


def should_drop_by_symbol(text: str, profile: dict) -> tuple[bool, str]:
    """Return (drop, reason). Drops only when the message references ONLY
    listed `other_instruments` and NONE of the configured `symbol_aliases`.

    Config-driven: both lists come from profile.json and may be empty. With
    empty `other_instruments` the function is a permanent no-op (safe
    default for first-run profiles before the operator populates the
    lists). With empty `symbol_aliases` AND non-empty `other_instruments`
    we still need at least one other-instrument hit to drop — otherwise
    pure analytical messages with no instrument mentions wouldn't trigger.

    No hardcoded language tokens. Adding a new channel (any language) means
    updating profile.json, not editing this code.
    """
    aliases = profile.get("symbol_aliases") or []
    others = profile.get("other_instruments") or []
    if not others:
        return (False, "")
    if _has_alias(text, aliases):
        return (False, "")  # any target-symbol mention saves the message
    if _has_alias(text, others):
        return (True, "symbol-mismatch")
    return (False, "")


def looks_like_ad(text: str) -> bool:
    """Heuristic ad-shape detector. Universal signals only.

    Returns True for:
      - 3+ URLs in any message > 200 chars (typical promo with referral
        + Telegram link + WhatsApp link)
      - Long messages (>800 chars) with 2+ URLs AND a percent-or-currency
        pattern (typical sponsored deposit-bonus ad)

    Does NOT trigger on:
      - Short messages even with URLs (TradingView chart link in a real
        signal stays sub-200 chars)
      - Long market-analysis posts with no URLs
      - Single URL with promo wording (LLM triage handles those — too
        risky to filter heuristically without language knowledge)
    """
    if not text:
        return False
    url_count = len(_URL_RE.findall(text))
    length = len(text)
    if url_count >= 3 and length > 200:
        return True
    if length > 800 and url_count >= 2 and _PCT_OR_CURRENCY_RE.search(text):
        return True
    return False
