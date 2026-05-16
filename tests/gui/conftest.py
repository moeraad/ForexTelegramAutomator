"""Shared fixtures for GUI smoke tests.

``tmp_stack`` builds a self-contained APPDATA-style folder with a
freshly-schemed DB + blank profile so views can be constructed without
touching the operator's real data.

``apply_default_theme`` runs once per session to make sure the QSS
template is applied before any view is built (otherwise palette tokens
leak through as raw ``${bg}`` strings).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass(frozen=True)
class _TmpStack:
    """Minimal Stack-shaped object that view constructors can accept."""
    name: str
    profile_path: Path
    project_path: Path
    db_path: Path
    api_host: str
    api_port: int
    service_names: tuple[str, str, str]

    @property
    def api_url(self) -> str:
        return f"http://{self.api_host}:{self.api_port}"


@pytest.fixture(scope="session", autouse=True)
def _qapp_session(qapp):
    """Force qapp into existence + apply our QSS template once."""
    from src.gui.theme import apply_theme
    apply_theme(qapp, "dark")
    yield qapp


@pytest.fixture
def tmp_stack(tmp_path: Path, monkeypatch) -> _TmpStack:
    appdata = tmp_path / "appdata"
    copytrades_root = appdata / "CopyTrades"
    stack_dir = copytrades_root / "TestStack"
    stack_dir.mkdir(parents=True, exist_ok=True)
    (stack_dir / "logs").mkdir(exist_ok=True)

    db_path = stack_dir / "copytrades.db"
    profile_path = stack_dir / "profile.json"

    # Initialize DB schema so settings/actions queries don't blow up.
    from src import db
    with db.connect(str(db_path)) as conn:
        db.init_schema(conn)

    # A minimal channel profile so PROFILE view can load it.
    profile_path.write_text(json.dumps({
        "name": "TestStack",
        "description": "smoke test stack",
        "symbol": "XAUUSD",
        "language": "en",
        "price_range_hint": "",
        "shorthand_decode_example": "",
        "header": "Test header.",
        "vocabulary_table": "VOCABULARY -> ACTION MAP:\n  [MOVE_SL_BE]\n    - secure entry",
        "compound_messages": "",
        "commentary_filter": "",
        "directional_command_flow": "",
        "worked_examples": "Ex1 (MOVE_SL_BE): \"secure entry\"\n  -> [{\"type\":\"MOVE_SL_BE\"}]",
        "triage_keep_triggers": "TRIGGERS: secure entry",
    }, indent=2), encoding="utf-8")

    # Point APPDATA so any code that reads it sees the tmp dir.
    monkeypatch.setenv("APPDATA", str(appdata))

    # Stacks_config registers TestStack.
    cfg = {
        "stacks": [{
            "name": "TestStack",
            "profile_path": str(profile_path),
            "project_path": str(tmp_path),
            "db_path": str(db_path),
            "service_names": ["CT-TEST-Api", "CT-TEST-Bot", "CT-TEST-Listener"],
        }]
    }
    (copytrades_root / "stacks_config.json").write_text(
        json.dumps(cfg, indent=2), encoding="utf-8",
    )

    return _TmpStack(
        name="TestStack",
        profile_path=profile_path,
        project_path=tmp_path,
        db_path=db_path,
        api_host="127.0.0.1",
        api_port=8765,
        service_names=("CT-TEST-Api", "CT-TEST-Bot", "CT-TEST-Listener"),
    )
