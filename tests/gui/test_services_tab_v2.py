"""V2-native Services tab.

Tab is entity-grouped (Accounts / Destinations / Bots) with per-row
controls + bulk actions. Tests cover:
  - _collect_rows reads v2 config + groups by role
  - empty / missing v2 config surfaces an explanatory error
  - mocked NSSM state drives the badge text on each row
  - the tab itself builds cleanly off the tmp_stack v1 fallback path
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# Auto-migration of the tmp_stack v1 config to v2 happens lazily, so
# many tests need an explicit v2 config dropped at the APPDATA path.


def _write_v2_config(appdata: Path, *, accounts=(), profiles=(), destinations=(),
                     bots=(), channels=(), routes=(), bindings=()):
    cfg = {
        "version": 2,
        "accounts": list(accounts),
        "profiles": list(profiles),
        "destinations": list(destinations),
        "bots": list(bots),
        "channels": list(channels),
        "routes": list(routes),
        "bot_bindings": list(bindings),
    }
    (appdata / "CopyTrades").mkdir(parents=True, exist_ok=True)
    (appdata / "CopyTrades" / "stacks_config.json").write_text(
        json.dumps(cfg, indent=2), encoding="utf-8",
    )


# ---- _collect_rows -----------------------------------------------------


def test_collect_rows_returns_error_when_no_v2_config(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_v2"))
    # Clear any cached config_v2 state.

    from src.gui.views._services_tab_v2 import _collect_rows
    accts, dests, bots, err = _collect_rows()
    assert accts == [] and dests == [] and bots == []
    assert "v2 config not found" in err


def test_collect_rows_groups_by_role(tmp_path, monkeypatch):
    db_path = tmp_path / "x.db"
    _write_v2_config(
        tmp_path,
        accounts=[{"id": "acc_a", "name": "Forex Eng", "phone": "",
                   "session_path": "", "service_name": "CT-Listener-acc_a"}],
        profiles=[{"id": "prof_a", "name": "P", "path": "/p.json"}],
        destinations=[{"id": "dest_a", "name": "Main MT5",
                       "service_name": "CT-A-Api", "db_path": str(db_path),
                       "api_host": "127.0.0.1", "api_port": 8765}],
        bots=[{"id": "bot_a", "name": "Main Bot", "token_setting_key": "tg_bot_token",
               "service_name": "CT-A-Bot"}],
        channels=[{"id": "ch_a", "name": "Ch A", "account_id": "acc_a",
                   "profile_id": "prof_a", "chat_id": -1, "enabled": True}],
        routes=[{"id": "r_a", "channel_id": "ch_a",
                 "destination_id": "dest_a", "enabled": True}],
        bindings=[{"id": "bd_a", "bot_id": "bot_a", "scope": "destination",
                   "destination_id": "dest_a"}],
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from src.gui.views._services_tab_v2 import _collect_rows
    accts, dests, bots, err = _collect_rows()
    assert err == ""
    assert [a.service_name for a in accts] == ["CT-Listener-acc_a"]
    assert [d.service_name for d in dests] == ["CT-A-Api"]
    assert [b.service_name for b in bots] == ["CT-A-Bot"]
    assert "Main MT5" in dests[0].entity_name
    assert "8765" in dests[0].subtitle
    assert "Main MT5" in bots[0].subtitle  # bot -> destination wiring


def test_collect_rows_two_channels_one_account_two_dests(tmp_path, monkeypatch):
    """The user's actual topology: 1 account, 2 channels, 2 destinations,
    2 bots. Should produce 5 rows (1 + 2 + 2)."""
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    _write_v2_config(
        tmp_path,
        accounts=[{"id": "acc_main", "name": "Main TG", "phone": "",
                   "session_path": "", "service_name": "CT-Listener-acc_main"}],
        profiles=[
            {"id": "prof_a", "name": "Pa", "path": "/pa.json"},
            {"id": "prof_b", "name": "Pb", "path": "/pb.json"},
        ],
        destinations=[
            {"id": "dest_a", "name": "MT5-A",
             "service_name": "CT-A-Api", "db_path": str(db_a),
             "api_host": "127.0.0.1", "api_port": 8765},
            {"id": "dest_b", "name": "MT5-B",
             "service_name": "CT-B-Api", "db_path": str(db_b),
             "api_host": "127.0.0.1", "api_port": 8766},
        ],
        bots=[
            {"id": "bot_a", "name": "Bot-A", "token_setting_key": "tg_bot_token",
             "service_name": "CT-A-Bot"},
            {"id": "bot_b", "name": "Bot-B", "token_setting_key": "tg_bot_token",
             "service_name": "CT-B-Bot"},
        ],
        channels=[
            {"id": "ch_a", "name": "Ch A", "account_id": "acc_main",
             "profile_id": "prof_a", "chat_id": -1, "enabled": True},
            {"id": "ch_b", "name": "Ch B", "account_id": "acc_main",
             "profile_id": "prof_b", "chat_id": -2, "enabled": True},
        ],
        routes=[
            {"id": "r_a", "channel_id": "ch_a",
             "destination_id": "dest_a", "enabled": True},
            {"id": "r_b", "channel_id": "ch_b",
             "destination_id": "dest_b", "enabled": True},
        ],
        bindings=[
            {"id": "bd_a", "bot_id": "bot_a", "scope": "destination",
             "destination_id": "dest_a"},
            {"id": "bd_b", "bot_id": "bot_b", "scope": "destination",
             "destination_id": "dest_b"},
        ],
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from src.gui.views._services_tab_v2 import _collect_rows
    accts, dests, bots, err = _collect_rows()
    assert err == ""
    assert len(accts) == 1, [a.service_name for a in accts]
    assert len(dests) == 2, [d.service_name for d in dests]
    assert len(bots) == 2, [b.service_name for b in bots]
    # Total of 5 services — matches the topology.
    assert len(accts) + len(dests) + len(bots) == 5


def test_collect_rows_skips_destinations_with_no_routes(tmp_path, monkeypatch):
    """Orphan destinations are NOT installable — spec excludes them, so
    the rows collector should excludes them too (otherwise the operator
    would see a row whose Install button no-ops)."""
    db_a = tmp_path / "a.db"
    _write_v2_config(
        tmp_path,
        destinations=[{"id": "dest_a", "name": "Orphan",
                       "service_name": "CT-A-Api", "db_path": str(db_a),
                       "api_host": "127.0.0.1", "api_port": 8765}],
        # no routes
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))

    from src.gui.views._services_tab_v2 import _collect_rows
    _, dests, _, _ = _collect_rows()
    assert dests == []


# ---- _ServiceRowWidget state badge ------------------------------------


def test_row_widget_renders_state_badges(qapp, monkeypatch):
    """RUNNING / STOPPED / NOT INSTALLED render the right badge text."""
    from src.gui.views._services_tab_v2 import (
        _ServiceRow,
        _ServiceRowWidget,
    )
    from src.gui.services import nssm_client

    row = _ServiceRow(
        role="Destination", entity_name="MT5-A",
        service_name="CT-A-Api", subtitle="port 8765",
        spec_entry={"service": "CT-A-Api", "module": "src.api",
                    "db_path": "/x.db", "account_id": ""},
    )
    w = _ServiceRowWidget(row, on_action=lambda *_: None)

    # NOT INSTALLED.
    monkeypatch.setattr(nssm_client, "service_exists", lambda _n: False)
    monkeypatch.setattr(nssm_client, "service_running", lambda _n: False)
    w.refresh_state()
    assert "NOT INSTALLED" in w._state.text()

    # STOPPED.
    monkeypatch.setattr(nssm_client, "service_exists", lambda _n: True)
    monkeypatch.setattr(nssm_client, "service_running", lambda _n: False)
    w.refresh_state()
    assert "STOPPED" in w._state.text()

    # RUNNING.
    monkeypatch.setattr(nssm_client, "service_exists", lambda _n: True)
    monkeypatch.setattr(nssm_client, "service_running", lambda _n: True)
    w.refresh_state()
    assert "RUNNING" in w._state.text()


# ---- ServicesTabV2 ----------------------------------------------------


def test_services_tab_v2_builds_without_v2_config(qapp, tmp_stack, monkeypatch):
    """The legacy v1 fallback path (tmp_stack uses StackEntry) still
    auto-migrates lazily, but the tab should construct without crashing
    even before that happens."""
    monkeypatch.setattr(
        "src.gui.services.nssm_client.service_exists", lambda _n: False,
    )
    monkeypatch.setattr(
        "src.gui.services.nssm_client.service_running", lambda _n: False,
    )
    from src.gui.views._services_tab_v2 import ServicesTabV2
    tab = ServicesTabV2(tmp_stack)
    # Should have built three section cards.
    assert tab._accounts_section is not None
    assert tab._destinations_section is not None
    assert tab._bots_section is not None


def test_services_tab_v2_topology_label_reflects_counts(
    qapp, tmp_path, monkeypatch,
):
    db_a = tmp_path / "a.db"
    _write_v2_config(
        tmp_path,
        accounts=[{"id": "acc_a", "name": "A", "phone": "", "session_path": "",
                   "service_name": "CT-Listener-acc_a"}],
        profiles=[{"id": "prof_a", "name": "P", "path": "/p.json"}],
        destinations=[{"id": "dest_a", "name": "D",
                       "service_name": "CT-A-Api", "db_path": str(db_a),
                       "api_host": "127.0.0.1", "api_port": 8765}],
        bots=[{"id": "bot_a", "name": "B", "token_setting_key": "tg_bot_token",
               "service_name": "CT-A-Bot"}],
        channels=[{"id": "ch_a", "name": "Ch A", "account_id": "acc_a",
                   "profile_id": "prof_a", "chat_id": -1, "enabled": True}],
        routes=[{"id": "r_a", "channel_id": "ch_a",
                 "destination_id": "dest_a", "enabled": True}],
        bindings=[{"id": "bd_a", "bot_id": "bot_a", "scope": "destination",
                   "destination_id": "dest_a"}],
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(
        "src.gui.services.nssm_client.service_exists", lambda _n: False,
    )
    monkeypatch.setattr(
        "src.gui.services.nssm_client.service_running", lambda _n: False,
    )

    # Build a synthetic Stack pointing at the v2 db; not used during
    # _refresh besides being held by the constructor.
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _S:
        name: str = "D"
        profile_path: Path = Path("/p.json")
        project_path: Path = Path("/proj")
        db_path: Path = db_a
        api_host: str = "127.0.0.1"
        api_port: int = 8765
        service_names: tuple = ("CT-A-Api",)
        primary_api_service: str = "CT-A-Api"
        primary_bot_service: str = "CT-A-Bot"
        primary_listener_service: str = "CT-Listener-acc_a"

        @property
        def api_url(self) -> str:
            return "http://127.0.0.1:8765"

    from src.gui.views._services_tab_v2 import ServicesTabV2
    tab = ServicesTabV2(_S())
    # Topology should mention 1 account / 1 destination / 1 bot.
    text = tab._topology.text()
    assert "1</b> account" in text
    assert "1</b> destination" in text
    assert "1</b> bot" in text
