"""project_dir argv plumbing for bootstrap_v2_install helper.

Prior helper guessed project_dir = db_path.parent.parent.parent which
landed on %APPDATA%\\Roaming when DBs lived under CopyTrades/<dest>/ —
surfacing as "Neither bundled CopyTrades.exe nor a Python venv was
found" in the GUI. The caller now passes the real BASE_DIR explicitly.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.gui.helpers import bootstrap_v2_install


def test_helper_uses_explicit_argv_project_dir(tmp_path, monkeypatch):
    """argv[1] wins over the legacy spec.parent fallback."""
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps([
        {"service": "CT-Test-Api", "module": "src.api",
         "db_path": str(tmp_path / "fake.db"), "account_id": ""},
    ]), encoding="utf-8")

    real_project = tmp_path / "real_repo"
    real_project.mkdir()
    # Plant a venv stub so _resolve_runner returns it.
    venv_python = real_project / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    captured_project: dict = {}

    def _fake_install(name, runner, runner_args, project_dir, logs_dir, tag):
        captured_project["project_dir"] = project_dir
        captured_project["runner"] = runner
        return 0

    monkeypatch.setattr(bootstrap_v2_install, "_install_service", _fake_install)
    rc = bootstrap_v2_install.main([str(spec), str(real_project)])
    assert rc == 0
    assert captured_project["project_dir"] == real_project
    assert captured_project["runner"] == venv_python


def test_helper_falls_back_to_spec_parent_when_no_argv(tmp_path, monkeypatch):
    """Back-compat: missing argv[1] uses spec.parent (the previous default)."""
    spec_dir = tmp_path / "spec_dir"
    spec_dir.mkdir()
    spec = spec_dir / "spec.json"
    spec.write_text(json.dumps([
        {"service": "CT-Test-Api", "module": "src.api",
         "db_path": str(tmp_path / "fake.db"), "account_id": ""},
    ]), encoding="utf-8")
    venv_python = spec_dir / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    captured: dict = {}

    def _fake(name, runner, runner_args, project_dir, logs_dir, tag):
        captured["project_dir"] = project_dir
        return 0

    monkeypatch.setattr(bootstrap_v2_install, "_install_service", _fake)
    rc = bootstrap_v2_install.main([str(spec)])
    assert rc == 0
    assert captured["project_dir"] == spec_dir
