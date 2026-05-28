"""Per-profile prompt rendering — closes the Prompts/Playground gap.

Pre-fix: prompt_inspector.render() and run_playground() always pulled
the system prompt from ``config.CHANNEL_PROFILE`` (a module-level
global). Aggregate-routing setups (one destination, N channels with
different profiles) could only inspect/playground ONE profile.

Post-fix: both APIs accept ``profile_name=`` and load THAT profile's
JSON via profile_io. Tests assert each profile renders distinct content.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.db import connect, init_schema


def _seed_profile(channels_dir: Path, name: str, *,
                  header: str = "H", symbol: str = "XAUUSD",
                  language: str = "en") -> Path:
    """Write a minimal but renderable profile JSON."""
    p = channels_dir / f"{name}.json"
    p.write_text(
        json.dumps({
            "name": name, "symbol": symbol, "language": language,
            "header": header,
            "vocabulary_table": "vt", "compound_messages": "cm",
            "commentary_filter": "cf", "directional_command_flow": "dcf",
            "worked_examples": "we", "shorthand_decode_example": "sde",
            "promo_indicators": "", "noise_patterns": "",
            "triage_keep_triggers": "",
        }),
        encoding="utf-8",
    )
    return p


@pytest.fixture
def two_profiles(monkeypatch, tmp_path: Path):
    base = tmp_path / "base"
    (base / "channels").mkdir(parents=True)
    monkeypatch.setattr("src.gui.services.stack_registry.BASE_DIR", base)
    monkeypatch.setattr("src.gui.services.profile_io.BASE_DIR", base)
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))
    _seed_profile(base / "channels", "alpha",
                  header="ALPHA HEADER 12345", symbol="XAUUSD")
    _seed_profile(base / "channels", "beta",
                  header="BETA HEADER 67890", symbol="EURUSD")
    return base


# ---- prompt_inspector.render(profile_name=...) -------------------------


def test_render_interpreter_uses_picked_profile(two_profiles, tmp_path: Path):
    from src.prompt_inspector import render
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    rp_alpha = render("interpreter", db_path, mode="demo",
                      profile_name="alpha")
    rp_beta = render("interpreter", db_path, mode="demo",
                     profile_name="beta")
    assert "ALPHA HEADER 12345" in rp_alpha.system_prompt
    assert "BETA HEADER 67890" in rp_beta.system_prompt
    # Cross-check: neither contains the other's header.
    assert "BETA HEADER" not in rp_alpha.system_prompt
    assert "ALPHA HEADER" not in rp_beta.system_prompt


def test_render_triage_uses_picked_profile(two_profiles, tmp_path: Path):
    """Triage prompt is a different rendering of the same profile JSON,
    but should still vary per profile (different symbol/language/etc)."""
    from src.prompt_inspector import render
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    rp_alpha = render("triage", db_path, mode="demo", profile_name="alpha")
    rp_beta = render("triage", db_path, mode="demo", profile_name="beta")
    # Triage prompts reference the symbol — XAUUSD vs EURUSD.
    assert "XAUUSD" in rp_alpha.system_prompt
    assert "EURUSD" in rp_beta.system_prompt


def test_render_falls_back_to_global_when_profile_name_none(
    two_profiles, tmp_path: Path, monkeypatch,
):
    """Backward compat: legacy callers (no profile_name) keep working."""
    # Make ai._render_system_prompt fall through silently (no global
    # CHANNEL_PROFILE configured for the test).
    from src.prompt_inspector import render
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    rp = render("interpreter", db_path, mode="demo", profile_name=None)
    # Doesn't crash; system_prompt is either empty or the fallback text.
    assert isinstance(rp.system_prompt, str)


def test_render_unknown_profile_returns_empty(two_profiles, tmp_path: Path):
    """A profile_name that doesn't exist on disk → empty system prompt,
    no crash."""
    from src.prompt_inspector import render
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    rp = render("interpreter", db_path, mode="demo",
                profile_name="nonexistent")
    assert isinstance(rp.system_prompt, str)
    # The custom headers don't leak through.
    assert "ALPHA HEADER" not in rp.system_prompt
    assert "BETA HEADER" not in rp.system_prompt


def test_evaluator_renderer_ignores_profile_name(two_profiles, tmp_path: Path):
    """Evaluator prompt is profile-agnostic; passing profile_name is a no-op."""
    from src.prompt_inspector import render
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    rp_a = render("evaluator", db_path, mode="demo", profile_name="alpha")
    rp_b = render("evaluator", db_path, mode="demo", profile_name="beta")
    assert rp_a.system_prompt == rp_b.system_prompt


# ---- _render_system_for_profile / _render_triage_for_profile helpers ---


def test_helper_returns_empty_for_unknown_profile(two_profiles, tmp_path: Path):
    from src.prompt_inspector import (
        _render_system_for_profile,
        _render_triage_for_profile,
    )
    assert _render_system_for_profile("does-not-exist") == ""
    assert _render_triage_for_profile("does-not-exist") == ""


def test_helper_renders_named_profile(two_profiles, tmp_path: Path):
    from src.prompt_inspector import _render_system_for_profile
    out = _render_system_for_profile("alpha")
    assert "ALPHA HEADER 12345" in out
