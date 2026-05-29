"""Regression: stack.profile_path must resolve to the LIVE db-adjacent
profile.json the runtime reads, not the (often stale) v2 Profile.path.

Root cause of the CLL "Profile not found" bug: _stacks_from_v2 used
Path(primary_profile.path) verbatim, which points at the legacy bundled
channels/<name>.json location the runtime ignores.
"""
from __future__ import annotations

from src.config_v2 import (
    Account,
    Channel,
    ConfigV2,
    Destination,
    Profile,
    Route,
)
from src.gui.services.stack_registry import (
    _resolve_live_profile_path,
    _stacks_from_v2,
)


def _cfg(db_path: str, configured_profile_path: str) -> ConfigV2:
    return ConfigV2(
        accounts=(Account(id="a", name="A", phone="", session_path="",
                          service_name="CT-Listener-a"),),
        profiles=(Profile(id="p", name="P", path=configured_profile_path,
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(id="c", name="C", account_id="a",
                          chat_id=-1, profile_id="p"),),
        destinations=(Destination(
            id="dest_x", name="X", db_path=db_path,
            api_host="127.0.0.1", api_port=8765, service_name="CT-X-Api",
        ),),
        routes=(Route(id="r", channel_id="c", destination_id="dest_x"),),
    )


def test_stack_profile_path_prefers_db_adjacent_live_file(tmp_path):
    dest_dir = tmp_path / "X"; dest_dir.mkdir()
    db_path = dest_dir / "copytrades.db"
    live = dest_dir / "profile.json"; live.write_text("{}", encoding="utf-8")
    stale = tmp_path / "_internal" / "channels" / "x.json"
    stale.parent.mkdir(parents=True); stale.write_text("{}", encoding="utf-8")

    stacks = _stacks_from_v2(_cfg(str(db_path), str(stale)))
    assert len(stacks) == 1
    # The live db-adjacent file wins over the (stale-but-present) configured path.
    assert stacks[0].profile_path == live


def test_stack_profile_path_falls_back_to_live_location_when_absent(tmp_path):
    # Neither the live nor configured file exists → return the live location
    # anyway, so a freshly-created profile lands where the runtime looks.
    dest_dir = tmp_path / "X"; dest_dir.mkdir()
    db_path = dest_dir / "copytrades.db"
    stacks = _stacks_from_v2(_cfg(str(db_path), "/nonexistent/legacy.json"))
    assert stacks[0].profile_path == dest_dir / "profile.json"


def test_resolve_live_profile_path_helper(tmp_path):
    d = tmp_path / "X"; d.mkdir()
    db = d / "copytrades.db"
    # db-adjacent exists → wins
    live = d / "profile.json"; live.write_text("{}", encoding="utf-8")
    legacy = tmp_path / "legacy.json"; legacy.write_text("{}", encoding="utf-8")
    assert _resolve_live_profile_path(db, str(legacy)) == live
    # remove live → legacy fallback
    live.unlink()
    assert _resolve_live_profile_path(db, str(legacy)) == legacy
    # neither → live default
    legacy.unlink()
    assert _resolve_live_profile_path(db, str(legacy)) == d / "profile.json"
