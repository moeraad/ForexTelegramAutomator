"""Tests for Step 8 of multi-channel plan: per-account listener install
+ legacy per-stack listener uninstall migration.

Verifies:

  - bootstrap_services_install._service_args picks the right --service
    target for src.shared_listener (hyphenated 'shared-listener') and
    src.listener (legacy 'listener')
  - bootstrap_services_install.main installs the listener slot with
    module src.shared_listener (NOT src.listener)
  - bootstrap_services_uninstall.main accepts any number of names ≥1
  - gui_launcher._dispatch_service routes 'shared-listener' to
    shared_listener.main()

NSSM / sc.exe calls are mocked — these are unit tests, not integration.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import pytest

from src.gui.helpers import bootstrap_services_install, bootstrap_services_uninstall


# ---- _service_args ---------------------------------------------------------


def test_service_args_frozen_shared_listener_uses_hyphenated_target():
    args = bootstrap_services_install._service_args(
        "frozen", "src.shared_listener", db_path="C:/x.db",
    )
    assert args == ["--service", "shared-listener", "--db-path", "C:/x.db"]


def test_service_args_frozen_legacy_listener_unchanged():
    args = bootstrap_services_install._service_args(
        "frozen", "src.listener", db_path=None,
    )
    assert args == ["--service", "listener"]


def test_service_args_frozen_api_bot_unchanged():
    assert bootstrap_services_install._service_args(
        "frozen", "src.api", None,
    ) == ["--service", "api"]
    assert bootstrap_services_install._service_args(
        "frozen", "src.bot", "X",
    ) == ["--service", "bot", "--db-path", "X"]


def test_service_args_venv_uses_module_form():
    args = bootstrap_services_install._service_args(
        "venv", "src.shared_listener", db_path="X",
    )
    assert args == ["-m", "src.shared_listener", "--db-path", "X"]


# ---- bootstrap_services_install.main wires shared_listener -----------------


def test_install_main_uses_shared_listener_for_third_slot(tmp_path: Path, monkeypatch):
    """Calls to _install_service should pass module=src.shared_listener
    for the third (listener) slot — not src.listener."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".venv" / "Scripts").mkdir(parents=True)
    (project / ".venv" / "Scripts" / "python.exe").touch()

    db = tmp_path / "stack" / "copytrades.db"
    db.parent.mkdir(parents=True)
    db.touch()

    captured: list[dict] = []

    def fake_install_service(name, runner, runner_args, project_dir, logs_dir, tag):
        captured.append({
            "name": name, "args": runner_args, "tag": tag,
        })
        return 0

    monkeypatch.setattr(bootstrap_services_install, "_install_service",
                        fake_install_service)

    rc = bootstrap_services_install.main([
        "TestStack", str(project),
        "CT-TEST-Api", "CT-TEST-Bot", "CT-Listener-acc_test",
        str(db),
    ])
    assert rc == 0
    assert len(captured) == 3
    api_call = captured[0]
    bot_call = captured[1]
    listener_call = captured[2]
    assert api_call["name"] == "CT-TEST-Api"
    assert "-m" in api_call["args"] and "src.api" in api_call["args"]
    assert bot_call["name"] == "CT-TEST-Bot"
    assert "src.bot" in bot_call["args"]
    # Critical: listener slot runs shared_listener, not listener.
    assert listener_call["name"] == "CT-Listener-acc_test"
    assert "src.shared_listener" in listener_call["args"]
    assert "src.listener" not in listener_call["args"]
    # Log tag should be hyphenated (matches the --service flag form).
    assert listener_call["tag"] == "shared-listener"


# ---- bootstrap_services_uninstall accepts variable args --------------------


def test_uninstall_main_accepts_one_name(monkeypatch):
    monkeypatch.setattr(bootstrap_services_uninstall, "_remove_one",
                        lambda n: (True, "removed"))
    assert bootstrap_services_uninstall.main(["CT-FOO-Listener"]) == 0


def test_uninstall_main_accepts_many_names(monkeypatch):
    removed: list[str] = []
    def fake(n):
        removed.append(n)
        return (True, "removed")
    monkeypatch.setattr(bootstrap_services_uninstall, "_remove_one", fake)
    rc = bootstrap_services_uninstall.main(
        ["CT-A-Listener", "CT-B-Listener", "CT-C-Listener"],
    )
    assert rc == 0
    assert removed == ["CT-A-Listener", "CT-B-Listener", "CT-C-Listener"]


def test_uninstall_main_rejects_empty():
    rc = bootstrap_services_uninstall.main([])
    assert rc == 2


def test_uninstall_main_skips_blank_args(monkeypatch):
    removed: list[str] = []
    def fake(n):
        removed.append(n)
        return (True, "x")
    monkeypatch.setattr(bootstrap_services_uninstall, "_remove_one", fake)
    bootstrap_services_uninstall.main(["", "  ", "CT-X-Listener", ""])
    assert removed == ["CT-X-Listener"]


def test_uninstall_main_propagates_failure(monkeypatch):
    monkeypatch.setattr(bootstrap_services_uninstall, "_remove_one",
                        lambda n: (False, "permission denied"))
    monkeypatch.setattr(bootstrap_services_uninstall, "error",
                        lambda *a, **kw: None)  # don't pop GUI dialog in tests
    rc = bootstrap_services_uninstall.main(["CT-X-Listener"])
    assert rc == 4


# ---- gui_launcher routes shared-listener -----------------------------------


def test_gui_launcher_dispatches_shared_listener(monkeypatch):
    """The --service shared-listener target must call src.shared_listener.main."""
    import gui_launcher

    called = {"shared_listener": 0}

    async def fake_main():
        called["shared_listener"] += 1

    import src.shared_listener
    monkeypatch.setattr(src.shared_listener, "main", fake_main)

    rc = gui_launcher._dispatch_service("shared-listener")
    assert rc == 0
    assert called["shared_listener"] == 1


def test_gui_launcher_rejects_unknown_target():
    import gui_launcher
    assert gui_launcher._dispatch_service("bogus") == 2
