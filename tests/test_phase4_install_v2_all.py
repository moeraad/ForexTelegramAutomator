"""Phase-4 v2 cleanup: install_v2_services_all() end-to-end.

Doesn't actually elevate — mocks ``run_elevated_python`` so we can
assert the spec content + the right helper module name being targeted.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from src import config_v2
from src.config_v2 import (
    Account,
    Bot,
    BotBinding,
    Channel,
    ConfigV2,
    Destination,
    Profile,
    Route,
)


def _wire_appdata_with_full_cfg(monkeypatch, tmp_path: Path) -> ConfigV2:
    """Write a minimal v2 config + APPDATA path so load_v2 finds it."""
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    db = appdata / "CopyTrades" / "dest_x" / "copytrades.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    cfg = ConfigV2(
        accounts=(Account(id="acc_a", name="A", phone="+1",
                          session_path="", service_name="CT-Listener-acc_a"),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(id="ch_a", name="A", account_id="acc_a",
                          chat_id=-1, profile_id="p"),),
        destinations=(Destination(
            id="dest_x", name="X", db_path=str(db),
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-X-Api",
        ),),
        bots=(Bot(id="bot_main", name="Main",
                  token_setting_key="t", service_name="CT-Main-Bot"),),
        routes=(Route(id="r", channel_id="ch_a",
                      destination_id="dest_x"),),
        bot_bindings=(BotBinding(id="bind", bot_id="bot_main",
                                 scope="destination",
                                 destination_id="dest_x"),),
    )
    config_v2.save_v2(cfg)
    return cfg


def test_install_all_returns_false_when_v2_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))
    from src.gui.services.bootstrap import install_v2_services_all
    ok, msg = install_v2_services_all()
    assert ok is False
    assert "v2 config" in msg.lower()


def test_install_all_returns_false_when_spec_empty(monkeypatch, tmp_path: Path):
    """A config with destinations but no enabled routes → no spec → fail."""
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    cfg = ConfigV2(
        destinations=(Destination(
            id="dest_orphan", name="Orphan", db_path="/x.db",
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-Orphan-Api",
        ),),
    )
    config_v2.save_v2(cfg)
    from src.gui.services.bootstrap import install_v2_services_all
    ok, msg = install_v2_services_all()
    assert ok is False
    assert "installable" in msg.lower() or "destination" in msg.lower()


def test_install_all_writes_spec_and_elevates(monkeypatch, tmp_path: Path):
    """Spec contents reach the helper via a TEMP json file."""
    _wire_appdata_with_full_cfg(monkeypatch, tmp_path)
    captured_spec: list = []

    def _fake_elevate(module: str, args: list[str]) -> bool:
        assert module == "src.gui.helpers.bootstrap_v2_install"
        # Two args: [spec_path, project_dir]. project_dir argv was
        # added 2026-05-25 so the helper finds .venv on installs whose
        # APPDATA-based DB paths don't share the project root.
        assert len(args) == 2, args
        spec_path = Path(args[0])
        assert spec_path.exists(), "BootstrapManager must write the spec before elevating"
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        captured_spec.extend(spec)
        return True

    with patch("src.gui.services.bootstrap.run_elevated_python",
               side_effect=_fake_elevate):
        from src.gui.services.bootstrap import install_v2_services_all
        ok, msg = install_v2_services_all()

    assert ok is True
    assert "service(s)" in msg
    # Spec contains api + bot + listener entries.
    services = {e["service"] for e in captured_spec}
    assert "CT-X-Api" in services
    assert "CT-Main-Bot" in services
    assert "CT-Listener-acc_a" in services


def test_per_stack_spec_filters_to_destination_subset(
    monkeypatch, tmp_path: Path,
):
    """BootstrapManager._derive_per_stack_spec should ONLY emit entries
    whose service names appear in this stack's service_names tuple."""
    _wire_appdata_with_full_cfg(monkeypatch, tmp_path)
    from src.gui.services.bootstrap import BootstrapManager, _StackWorker
    from src.gui.services.stack_registry import _stacks_from_v2
    stacks = _stacks_from_v2(config_v2.load_v2(config_v2.config_path()))
    assert len(stacks) == 1
    worker = _StackWorker(stacks[0])
    spec = worker._derive_per_stack_spec()
    # Should contain api + bot + listener (3 entries).
    services = {e["service"] for e in spec}
    assert services == {"CT-X-Api", "CT-Main-Bot", "CT-Listener-acc_a"}


def test_per_stack_spec_returns_empty_when_v2_missing(monkeypatch, tmp_path: Path):
    """Legacy v1 install path: no v2 config → empty spec → caller uses
    the legacy 3-tuple helper as a fallback."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))
    from src.gui.services.bootstrap import _StackWorker
    from src.gui.services.stack_registry import Stack
    stack = Stack(
        name="legacy", profile_path=Path("/p"), project_path=Path("/proj"),
        db_path=Path("/legacy.db"), api_host="127.0.0.1", api_port=8765,
        service_names=("CT-L-Api", "CT-L-Bot", "CT-L-Listener"),
        primary_api_service="CT-L-Api",
        primary_bot_service="CT-L-Bot",
        primary_listener_service="CT-L-Listener",
    )
    worker = _StackWorker(stack)
    assert worker._derive_per_stack_spec() == []


def test_install_all_returns_false_when_elevation_cancelled(
    monkeypatch, tmp_path: Path,
):
    _wire_appdata_with_full_cfg(monkeypatch, tmp_path)
    with patch("src.gui.services.bootstrap.run_elevated_python",
               return_value=False):
        from src.gui.services.bootstrap import install_v2_services_all
        ok, msg = install_v2_services_all()
    assert ok is False
    assert "elevation" in msg.lower()
    # Spec path should appear in the error so operator can re-run manually.
    assert "ct_v2_spec_" in msg
