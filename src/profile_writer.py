"""Apply an accepted learning suggestion into the channel profile.json.

The SOLE writer of learned rules into the live config. Each rule_kind maps to
an existing profile field that a live layer already reads:
  noise / context_drop -> noise_patterns (triage prompt)
  keep_trigger         -> triage_keep_triggers (triage prompt)
  action_trigger       -> triggers[] (trigger_matcher)
Writes atomically (temp + os.replace) so a crash mid-write can't corrupt the
profile. Newline-delimited text fields get the phrase appended de-duplicated.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from src.suggestion_store import Suggestion


def _append_line(existing: str, phrase: str) -> str:
    lines = [l for l in (existing or "").splitlines() if l.strip()]
    if phrase not in lines:
        lines.append(phrase)
    return "\n".join(lines)


def apply_suggestion(profile_path: Path, suggestion: Suggestion) -> None:
    profile_path = Path(profile_path)
    data = json.loads(profile_path.read_text(encoding="utf-8"))
    kind = suggestion.rule_kind
    payload = suggestion.payload
    phrase = str(payload.get("phrase") or "").strip()

    if kind in ("noise", "context_drop"):
        if not phrase:
            raise ValueError("noise suggestion missing phrase")
        data["noise_patterns"] = _append_line(data.get("noise_patterns", ""), phrase)
    elif kind == "keep_trigger":
        if not phrase:
            raise ValueError("keep_trigger suggestion missing phrase")
        data["triage_keep_triggers"] = _append_line(
            data.get("triage_keep_triggers", ""), phrase)
    elif kind == "action_trigger":
        action_types = payload.get("action_types")
        samples = payload.get("samples")
        if not phrase or not action_types or not samples:
            raise ValueError("action_trigger payload incomplete")
        triggers = data.get("triggers") or []
        triggers.append({
            "phrase": phrase,
            "action_types": list(action_types),
            "samples": list(samples),
            "context_tokens": [],
        })
        data["triggers"] = triggers
    else:
        raise ValueError(f"unknown rule_kind: {kind}")

    tmp = profile_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, profile_path)
