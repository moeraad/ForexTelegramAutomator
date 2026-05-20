"""Render derived channel-profile prompt fields from a structured triggers list.

The triggers list is the authoritative mapping of phrase -> action_type.
vocabulary_table, worked_examples, triage_keep_triggers, and
commentary_filter are computed views over it.
"""
from __future__ import annotations

from collections import defaultdict


_VOCAB_SKIP = ("IGNORE", "UNKNOWN", "ALERT")
_EXAMPLES_SKIP = ("IGNORE", "UNKNOWN")
_TRIGGERS_SKIP = ("IGNORE", "UNKNOWN")


def render_prompt_fields(triggers: list[dict]) -> dict[str, str]:
    """Build the four derived prompt fields from a triggers list.

    Each trigger may carry either the legacy ``action_type: "..."`` or
    the newer compound ``action_types: [...]`` (Profile Generator
    wizard output). Compound entries are grouped into EVERY bucket
    they participate in so a single message hitting `[OPEN, MOVE_SL_BE]`
    contributes a vocabulary example to both buckets.
    """
    grouped: dict[str, list[dict]] = defaultdict(list)
    for t in triggers:
        # Compound shape wins; fall back to legacy singular. Without
        # this both `vocabulary_table` and `worked_examples` would
        # silently render empty for any wizard-saved profile because
        # every trigger would group under "UNKNOWN" (filtered out).
        ats_raw = t.get("action_types")
        if isinstance(ats_raw, list) and ats_raw:
            buckets = [str(a).upper() for a in ats_raw if a]
        else:
            buckets = [str(t.get("action_type") or "UNKNOWN").upper()]
        for at in buckets:
            grouped[at].append(t)

    vocab_lines = ["VOCABULARY -> ACTION MAP:"]
    for at in sorted(grouped.keys()):
        if at in _VOCAB_SKIP:
            continue
        phrases = sorted({(t.get("phrase") or "").strip() for t in grouped[at]} - {""})
        if not phrases:
            continue
        vocab_lines.append(f"  [{at}]")
        for p in phrases[:30]:
            vocab_lines.append(f"    - {p}")

    example_lines: list[str] = []
    idx = 1
    for at in sorted(grouped.keys()):
        if at in _EXAMPLES_SKIP:
            continue
        examples_for_type = grouped[at][:2]
        for t in examples_for_type:
            samples = t.get("samples") or []
            if not samples:
                continue
            msg = str(samples[0]).replace("\n", " ").strip()
            if not msg:
                continue
            example_lines.append(
                f"Ex{idx} ({at}): \"{msg[:160]}\"\n"
                f"  -> [{{\"type\":\"{at}\"}}]"
            )
            idx += 1

    trigger_phrases: list[str] = []
    for at, items in grouped.items():
        if at in _TRIGGERS_SKIP:
            continue
        trigger_phrases.extend((t.get("phrase") or "").strip() for t in items)
    trigger_phrases = sorted({p for p in trigger_phrases if p})[:60]

    ignore_phrases = sorted(
        {(t.get("phrase") or "").strip() for t in grouped.get("IGNORE", [])}
        - {""}
    )[:50]

    return {
        "vocabulary_table": "\n".join(vocab_lines),
        "worked_examples": "\n\n".join(example_lines),
        "triage_keep_triggers": (
            "ALWAYS-KEEP TRIGGERS (high-signal phrases):\n  "
            + " | ".join(trigger_phrases)
        ),
        "commentary_filter": (
            "COMMENTARY FILTER (do NOT act on these):\n  "
            + "\n  ".join(f"- {p}" for p in ignore_phrases)
        ),
    }
