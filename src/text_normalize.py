"""Shared text normalisation used by deduplication AND trigger matching.

Extracted from `src/gui/services/profile_wizard.py` so the live listener's
`trigger_matcher` can apply the same normalisation as the offline wizard.
Both must produce identical normalised forms or operator-curated triggers
will silently fail to fire in production.

The pipeline collapses cosmetic + numeric variations so two messages with
the same intent but different prices / emoji decoration / Arabic diacritic
variants compare as equal:

  emoji strip → tashkeel strip → Arabic letter fold →
  digit runs to "<N>" → casefold → punctuation strip → whitespace collapse
"""
from __future__ import annotations

import re


_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FFFF"  # emoji blocks
    "☀-➿"          # symbols
    "　-〿"          # CJK punctuation
    "]"
)
_PUNCT_RE = re.compile(r"[^\w\s؀-ۿ]")  # keep word chars + Arabic
_WHITESPACE_RE = re.compile(r"\s+")

# Aggressive dedup helpers.
# Match runs of either ASCII digits or Arabic-Indic digits.
_DIGIT_RUN_RE = re.compile(r"[0-9٠-٩۰-۹]+")
# Tashkeel (Arabic diacritics): fatha, kasra, damma, shadda, sukun, tanween, etc.
_TASHKEEL_RE = re.compile(r"[ً-ْٰـ]")
# Letter folding map: alef variants, ya-with-dots, ta-marbuta, hamza on waw/ya.
_ARABIC_FOLD = str.maketrans({
    "أ": "ا",  # أ -> ا
    "إ": "ا",  # إ -> ا
    "آ": "ا",  # آ -> ا
    "ٱ": "ا",  # ٱ -> ا
    "ى": "ي",  # ى -> ي
    "ئ": "ي",  # ئ -> ي
    "ؤ": "و",  # ؤ -> و
    "ة": "ه",  # ة -> ه
})


def normalize(text: str) -> str:
    """Build a comparison key that collapses cosmetic + numeric variations.

    Steps applied in order: emoji strip → Arabic diacritics strip → letter
    folding → digit runs collapsed to "<N>" → lowercase → punctuation
    strip → whitespace collapse. Empty string in → empty string out.

    The same function MUST run on both sides of every comparison
    (operator trigger phrase and incoming message). The trigger matcher
    in the live listener and the wizard's deduplicator both call this.
    """
    if not text:
        return ""
    s = _EMOJI_RE.sub(" ", text)
    s = _TASHKEEL_RE.sub("", s)
    s = s.translate(_ARABIC_FOLD)
    s = _DIGIT_RUN_RE.sub("<N>", s)
    s = s.casefold()
    s = _PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s
