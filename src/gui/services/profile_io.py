"""Load/save channel JSON with structured triggers round-trip.

Profiles may or may not have a ``triggers`` field. When present, the
derived prompt fields (vocabulary_table, worked_examples,
triage_keep_triggers, commentary_filter) are recomputed on save so the
JSON stays internally consistent. When absent, triggers are inferred
from the legacy fields so the Triggers tab is never empty on a
populated profile.
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from pathlib import Path

from src.config import BASE_DIR
from src.profile_render import render_prompt_fields


_BRACKET_TYPE_RE = re.compile(r"^\[([A-Z_]+)\]\s*$")
_BULLET_RE = re.compile(r"^[-•·]\s*(.+)$")
_EXAMPLE_RE = re.compile(
    r"Ex\d+\s*\(([A-Z_]+)\)\s*:\s*\"(.*?)\"\s*->",
    re.DOTALL,
)


_FIELD_ORDER = (
    "name",
    "description",
    "symbol",
    "language",
    "price_range_hint",
    "shorthand_decode_example",
    "header",
    "vocabulary_table",
    "compound_messages",
    "commentary_filter",
    "directional_command_flow",
    "worked_examples",
    "triage_keep_triggers",
    "triggers",
)


def profile_path(stack_name: str) -> Path:
    return BASE_DIR / "channels" / f"{stack_name}.json"


def load_profile(stack_name: str) -> OrderedDict:
    p = profile_path(stack_name)
    if not p.exists():
        return OrderedDict()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return OrderedDict()
    return OrderedDict(data)


def save_profile(stack_name: str, data: dict) -> Path:
    """Atomic-ish write with .bak backup. Re-renders derived fields if triggers present."""
    p = profile_path(stack_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            p.with_suffix(".json.bak").write_bytes(p.read_bytes())
        except OSError:
            pass

    triggers = data.get("triggers")
    if isinstance(triggers, list) and triggers:
        rendered = render_prompt_fields(triggers)
        for k, v in rendered.items():
            data[k] = v

    ordered = OrderedDict()
    for key in _FIELD_ORDER:
        if key in data:
            ordered[key] = data[key]
    for key, value in data.items():
        if key not in ordered:
            ordered[key] = value

    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return p


def normalize_triggers(raw) -> list[dict]:
    """Coerce loaded JSON 'triggers' into a clean list of dicts."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        at = str(entry.get("action_type") or "").strip().upper()
        if not at:
            continue
        phrase = str(entry.get("phrase") or "").strip()
        samples_raw = entry.get("samples") or []
        if isinstance(samples_raw, str):
            samples = [samples_raw]
        elif isinstance(samples_raw, list):
            samples = [str(s) for s in samples_raw if str(s).strip()]
        else:
            samples = []
        note = str(entry.get("note") or "").strip()
        out.append({
            "action_type": at,
            "phrase": phrase,
            "samples": samples,
            "note": note,
        })
    return out


def derive_triggers_from_legacy_fields(profile: dict) -> list[dict]:
    """Build a triggers list from vocabulary_table / worked_examples /
    commentary_filter when the profile has no structured ``triggers`` array.

    Recognises the format the generator wizard emits:

        VOCABULARY -> ACTION MAP:
          [MOVE_SL_BE]
            - secure entry
            - amnk d5olk

        Ex1 (MOVE_SL_BE): "..."
          -> [{"type":"MOVE_SL_BE"}]

        COMMENTARY FILTER (do NOT act on these):
          - religious phrase
    """
    out: list[dict] = []

    vocab = str(profile.get("vocabulary_table") or "")
    current_type: str | None = None
    for line in vocab.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = _BRACKET_TYPE_RE.match(stripped)
        if m:
            current_type = m.group(1).upper()
            continue
        bm = _BULLET_RE.match(stripped)
        if bm and current_type:
            phrase = bm.group(1).strip()
            if phrase:
                out.append({
                    "action_type": current_type,
                    "phrase": phrase,
                    "samples": [],
                    "note": "from vocabulary_table",
                })

    examples = str(profile.get("worked_examples") or "")
    for at, sample in _EXAMPLE_RE.findall(examples):
        sample = sample.strip()
        if not sample:
            continue
        out.append({
            "action_type": at.upper(),
            "phrase": "",
            "samples": [sample],
            "note": "from worked_examples",
        })

    commentary = str(profile.get("commentary_filter") or "")
    for line in commentary.splitlines():
        stripped = line.strip()
        bm = _BULLET_RE.match(stripped)
        if bm:
            phrase = bm.group(1).strip()
            if phrase:
                out.append({
                    "action_type": "IGNORE",
                    "phrase": phrase,
                    "samples": [],
                    "note": "from commentary_filter",
                })

    return out


def load_triggers(profile: dict) -> list[dict]:
    """Return the triggers list for a profile, falling back to legacy parsing."""
    triggers = normalize_triggers(profile.get("triggers", []))
    if triggers:
        return triggers
    return derive_triggers_from_legacy_fields(profile)
