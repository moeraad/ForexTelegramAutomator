"""Tests for v2 config schema and v1→v2 migration.

See ``docs/plans/2026-05-23-multi-channel-routing.md`` for the architectural
context. Step 1 acceptance criteria:

  - v2 round-trip: load → save → diff is empty
  - v1→v2 migration: existing config produces a coherent v2 with the right
    cross-references, accounts deduped by phone, .v1.bak created
  - Compat shim: stack_registry.discover_stacks() reads v2 and emits
    synthetic Stack objects that match what v1 produced
  - All migration paths idempotent (running twice is a no-op)
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

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
from src.migrations.config_v1_to_v2 import migrate, write_with_backup


# ---- Helpers ---------------------------------------------------------------


def _make_stack_db(path: Path, *, tg_settings: dict[str, str]) -> None:
    """Create a minimal per-stack SQLite DB with a settings table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        for k, v in tg_settings.items():
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, v))
        conn.commit()
    finally:
        conn.close()


def _sample_config() -> ConfigV2:
    return ConfigV2(
        accounts=(
            Account(id="acc_primary", name="Primary", phone="+9611234567",
                    session_path="C:/sessions/primary.session",
                    service_name="CT-Listener-acc_primary"),
        ),
        profiles=(
            Profile(id="prof_fe", name="Forex Engineer",
                    path="C:/profiles/fe.json", language="ar", symbol="XAUUSD"),
        ),
        channels=(
            Channel(id="ch_fe", name="Forex Engineer",
                    account_id="acc_primary", chat_id=-1001234567890,
                    profile_id="prof_fe"),
        ),
        destinations=(
            Destination(id="dest_fe", name="Forex Engineer",
                        db_path="C:/dbs/fe/copytrades.db",
                        api_host="127.0.0.1", api_port=8765,
                        service_name="CT-Api-fe"),
        ),
        bots=(
            Bot(id="bot_fe", name="FE Bot",
                token_setting_key="tg_bot_token",
                service_name="CT-Bot-fe"),
        ),
        routes=(
            Route(id="route_fe", channel_id="ch_fe",
                  destination_id="dest_fe"),
        ),
        bot_bindings=(
            BotBinding(id="bind_fe", bot_id="bot_fe",
                       scope="destination", destination_id="dest_fe"),
        ),
    )


# ---- v2 schema -------------------------------------------------------------


def test_round_trip_preserves_config(tmp_path: Path) -> None:
    original = _sample_config()
    cfg_path = tmp_path / "stacks_config.json"
    config_v2.save_v2(original, cfg_path)
    loaded = config_v2.load_v2(cfg_path)
    assert loaded == original


def test_save_writes_version_field(tmp_path: Path) -> None:
    cfg_path = tmp_path / "stacks_config.json"
    config_v2.save_v2(_sample_config(), cfg_path)
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["version"] == 2


def test_is_v2_true_for_versioned_file(tmp_path: Path) -> None:
    cfg_path = tmp_path / "stacks_config.json"
    config_v2.save_v2(_sample_config(), cfg_path)
    assert config_v2.is_v2(cfg_path) is True


def test_is_v2_false_for_v1_file(tmp_path: Path) -> None:
    cfg_path = tmp_path / "stacks_config.json"
    cfg_path.write_text(json.dumps({"stacks": []}), encoding="utf-8")
    assert config_v2.is_v2(cfg_path) is False


def test_is_v2_false_for_missing_file(tmp_path: Path) -> None:
    assert config_v2.is_v2(tmp_path / "missing.json") is False


def test_load_v2_returns_none_for_v1(tmp_path: Path) -> None:
    cfg_path = tmp_path / "stacks_config.json"
    cfg_path.write_text(json.dumps({"stacks": []}), encoding="utf-8")
    assert config_v2.load_v2(cfg_path) is None


# ---- Account.phone_display (Day-4 PII redaction) --------------------------


def test_phone_display_redacts_middle_digits():
    a = Account(id="x", name="x", phone="+9611234567",
                session_path="", service_name="")
    assert a.phone_display() == "+961***4567"


def test_phone_display_handles_us_format():
    a = Account(id="x", name="x", phone="+15551234567",
                session_path="", service_name="")
    assert a.phone_display() == "+155***4567"


def test_phone_display_returns_unset_when_empty():
    a = Account(id="x", name="x", phone="",
                session_path="", service_name="")
    assert a.phone_display() == "<unset>"


def test_phone_display_returns_unset_when_whitespace():
    a = Account(id="x", name="x", phone="   ",
                session_path="", service_name="")
    assert a.phone_display() == "<unset>"


def test_phone_display_does_not_mangle_short_phones():
    """Phones with ≤6 digits return as-is; redaction would obscure too much."""
    a = Account(id="x", name="x", phone="+12345",
                session_path="", service_name="")
    assert a.phone_display() == "+12345"


def test_phone_display_redact_false_returns_raw():
    """For Telethon auth and similar callsites that need the real value."""
    a = Account(id="x", name="x", phone="+9611234567",
                session_path="", service_name="")
    assert a.phone_display(redact=False) == "+9611234567"


def test_phone_display_handles_spaces_in_phone():
    """Phone with separators (operator-entered): output is canonicalized.

    Cut is at the boundary AFTER the 3rd digit, dropping any separator
    that came right after. So `+961 12 34 56 78` → `+961***5678`.
    """
    a = Account(id="x", name="x", phone="+961 12 34 56 78",
                session_path="", service_name="")
    assert a.phone_display() == "+961***5678"


def test_lookup_helpers(tmp_path: Path) -> None:
    cfg = _sample_config()
    assert cfg.account("acc_primary").phone == "+9611234567"
    assert cfg.channel("ch_fe").chat_id == -1001234567890
    assert cfg.channel_by_chat_id(-1001234567890).id == "ch_fe"
    assert cfg.channel_by_chat_id(999) is None
    assert cfg.routes_for_channel("ch_fe") == (cfg.routes[0],)
    assert cfg.routes_for_destination("dest_fe") == (cfg.routes[0],)
    assert cfg.bindings_for_destination("dest_fe") == (cfg.bot_bindings[0],)


def test_binding_global_scope_matches_every_destination() -> None:
    cfg = ConfigV2(
        destinations=(
            Destination(id="d1", name="d1", db_path="", api_host="", api_port=0, service_name=""),
            Destination(id="d2", name="d2", db_path="", api_host="", api_port=0, service_name=""),
        ),
        bot_bindings=(BotBinding(id="b", bot_id="bot_global", scope="global"),),
    )
    assert cfg.bindings_for_destination("d1") == cfg.bot_bindings
    assert cfg.bindings_for_destination("d2") == cfg.bot_bindings


def test_binding_validation_requires_target_for_scope() -> None:
    with pytest.raises(ValueError, match="destination"):
        BotBinding(id="b", bot_id="bot", scope="destination")
    with pytest.raises(ValueError, match="channel"):
        BotBinding(id="b", bot_id="bot", scope="channel")
    with pytest.raises(ValueError, match="route"):
        BotBinding(id="b", bot_id="bot", scope="route")
    with pytest.raises(ValueError, match="must be one of"):
        BotBinding(id="b", bot_id="bot", scope="bogus")  # type: ignore[arg-type]


def test_binding_global_scope_has_no_target_requirement() -> None:
    # Should not raise.
    BotBinding(id="b", bot_id="bot", scope="global")


def test_binding_serialization_omits_none_targets(tmp_path: Path) -> None:
    cfg = ConfigV2(
        bot_bindings=(BotBinding(id="b", bot_id="bot", scope="global"),),
    )
    cfg_path = tmp_path / "c.json"
    config_v2.save_v2(cfg, cfg_path)
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    binding = data["bot_bindings"][0]
    assert "destination_id" not in binding
    assert "channel_id" not in binding
    assert "route_id" not in binding


# ---- v1 -> v2 migration ----------------------------------------------------


def test_migrate_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert migrate(tmp_path / "missing.json") is None


def test_migrate_returns_none_for_already_v2_file(tmp_path: Path) -> None:
    cfg_path = tmp_path / "stacks_config.json"
    config_v2.save_v2(_sample_config(), cfg_path)
    assert migrate(cfg_path) is None


def test_migrate_empty_v1_file_yields_empty_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "stacks_config.json"
    cfg_path.write_text(json.dumps({"stacks": []}), encoding="utf-8")
    result = migrate(cfg_path)
    assert result == ConfigV2()


def test_migrate_single_stack(tmp_path: Path) -> None:
    db_path = tmp_path / "fe" / "copytrades.db"
    _make_stack_db(db_path, tg_settings={
        "tg_phone": "+9611234567",
        "tg_session_name": "copytrades_session",
        "tg_watched_chat_id": "-1001234567890",
        "tg_bot_token": "123:ABC",
    })
    cfg_path = tmp_path / "stacks_config.json"
    cfg_path.write_text(json.dumps({
        "stacks": [{
            "name": "Forex Engineer",
            "profile_path": str(tmp_path / "fe" / "profile.json"),
            "project_path": str(tmp_path / "project"),
            "db_path": str(db_path),
            "service_names": [
                "CT-FOREXENGINEER-Api",
                "CT-FOREXENGINEER-Bot",
                "CT-FOREXENGINEER-Listener",
            ],
        }],
    }), encoding="utf-8")

    result = migrate(cfg_path)
    assert result is not None
    assert len(result.accounts) == 1
    assert len(result.channels) == 1
    assert len(result.destinations) == 1
    assert len(result.bots) == 1
    assert len(result.routes) == 1
    assert len(result.bot_bindings) == 1
    account = result.accounts[0]
    assert account.phone == "+9611234567"
    assert account.session_path.endswith("copytrades_session.session")
    channel = result.channels[0]
    assert channel.chat_id == -1001234567890
    assert channel.account_id == account.id
    dest = result.destinations[0]
    assert dest.service_name == "CT-FOREXENGINEER-Api"
    assert dest.db_path == str(db_path)
    bot = result.bots[0]
    assert bot.service_name == "CT-FOREXENGINEER-Bot"
    assert bot.token_setting_key == "tg_bot_token"
    route = result.routes[0]
    assert route.channel_id == channel.id
    assert route.destination_id == dest.id
    binding = result.bot_bindings[0]
    assert binding.bot_id == bot.id
    assert binding.scope == "destination"
    assert binding.destination_id == dest.id


def test_migrate_dedups_accounts_by_phone(tmp_path: Path) -> None:
    """Two v1 stacks sharing a TG_PHONE become one Account + two Channels."""
    db1 = tmp_path / "s1" / "copytrades.db"
    db2 = tmp_path / "s2" / "copytrades.db"
    shared_phone = "+9611111111"
    _make_stack_db(db1, tg_settings={
        "tg_phone": shared_phone,
        "tg_session_name": "session1",
        "tg_watched_chat_id": "-1001",
    })
    _make_stack_db(db2, tg_settings={
        "tg_phone": shared_phone,
        "tg_session_name": "session2",
        "tg_watched_chat_id": "-1002",
    })
    cfg_path = tmp_path / "stacks_config.json"
    cfg_path.write_text(json.dumps({"stacks": [
        {"name": "S1", "profile_path": "", "project_path": "",
         "db_path": str(db1), "service_names": ["CT-S1-Api", "CT-S1-Bot", "CT-S1-Listener"]},
        {"name": "S2", "profile_path": "", "project_path": "",
         "db_path": str(db2), "service_names": ["CT-S2-Api", "CT-S2-Bot", "CT-S2-Listener"]},
    ]}), encoding="utf-8")

    result = migrate(cfg_path)
    assert result is not None
    assert len(result.accounts) == 1, "phone-shared stacks should collapse to one Account"
    assert len(result.channels) == 2
    assert result.channels[0].account_id == result.channels[1].account_id == result.accounts[0].id
    assert result.channels[0].chat_id == -1001
    assert result.channels[1].chat_id == -1002


def test_migrate_handles_missing_db_with_placeholder(tmp_path: Path) -> None:
    """Stack with missing DB gets a placeholder account so migration doesn't fail."""
    cfg_path = tmp_path / "stacks_config.json"
    cfg_path.write_text(json.dumps({"stacks": [
        {"name": "Broken", "profile_path": "", "project_path": "",
         "db_path": str(tmp_path / "nonexistent.db")},
    ]}), encoding="utf-8")

    result = migrate(cfg_path)
    assert result is not None
    assert len(result.accounts) == 1
    assert result.accounts[0].phone == ""
    assert result.accounts[0].id.startswith("acc_unconfigured")


def test_write_with_backup_creates_v1_bak(tmp_path: Path) -> None:
    cfg_path = tmp_path / "stacks_config.json"
    cfg_path.write_text(json.dumps({"stacks": []}), encoding="utf-8")
    config = ConfigV2()
    write_with_backup(config, cfg_path)
    backup = cfg_path.with_suffix(cfg_path.suffix + ".v1.bak")
    assert backup.exists()
    assert config_v2.is_v2(cfg_path)


def test_write_with_backup_no_backup_when_no_v1(tmp_path: Path) -> None:
    cfg_path = tmp_path / "stacks_config.json"
    write_with_backup(ConfigV2(), cfg_path)
    backup = cfg_path.with_suffix(cfg_path.suffix + ".v1.bak")
    assert not backup.exists()
    assert config_v2.is_v2(cfg_path)


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Running migrate() on a freshly-migrated file is a no-op."""
    cfg_path = tmp_path / "stacks_config.json"
    cfg_path.write_text(json.dumps({"stacks": []}), encoding="utf-8")
    config = migrate(cfg_path)
    write_with_backup(config, cfg_path)
    # Second migration call should be a no-op.
    assert migrate(cfg_path) is None


# ---- Compat shim in stack_registry -----------------------------------------


def test_stack_registry_auto_migrates_v1(tmp_path: Path, monkeypatch) -> None:
    """discover_stacks() reads a v1 file, migrates it, returns synthetic Stacks."""
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))

    db_path = appdata / "CopyTrades" / "Forex Engineer" / "copytrades.db"
    _make_stack_db(db_path, tg_settings={
        "tg_phone": "+96100000",
        "tg_session_name": "sess",
        "tg_watched_chat_id": "-1001",
        "api_host": "127.0.0.1",
        "api_port": "8765",
    })
    cfg_path = appdata / "CopyTrades" / "stacks_config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"stacks": [{
        "name": "Forex Engineer",
        "profile_path": str(db_path.parent / "profile.json"),
        "project_path": str(tmp_path / "project"),
        "db_path": str(db_path),
        "service_names": ["CT-FE-Api", "CT-FE-Bot", "CT-FE-Listener"],
    }]}), encoding="utf-8")

    # Re-import under the patched env so the module-level paths resolve fresh.
    import importlib

    from src.gui.services import stack_registry
    importlib.reload(stack_registry)
    stacks = stack_registry.discover_stacks()

    assert len(stacks) == 1
    s = stacks[0]
    assert s.name == "Forex Engineer"
    assert str(s.db_path) == str(db_path)
    assert s.service_names == ("CT-FE-Api", "CT-FE-Bot", "CT-Listener-acc_96100000")
    assert s.api_port == 8765
    # File was migrated in place.
    assert config_v2.is_v2(cfg_path)
    assert (cfg_path.with_suffix(cfg_path.suffix + ".v1.bak")).exists()


def test_stack_registry_reads_v2_directly(tmp_path: Path, monkeypatch) -> None:
    """discover_stacks() on an already-v2 file derives synthetic Stacks
    without re-running migration."""
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))

    db_path = appdata / "CopyTrades" / "dest_a" / "copytrades.db"
    _make_stack_db(db_path, tg_settings={
        "api_host": "127.0.0.1",
        "api_port": "8765",
    })
    cfg_path = appdata / "CopyTrades" / "stacks_config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    config = ConfigV2(
        accounts=(Account(id="acc_x", name="A", phone="+961X",
                          session_path="", service_name="CT-Listener-acc_x"),),
        profiles=(Profile(id="prof_a", name="A", path=""),),
        channels=(Channel(id="ch_a", name="A", account_id="acc_x",
                          chat_id=-1, profile_id="prof_a"),),
        destinations=(Destination(id="dest_a", name="Dest A",
                                  db_path=str(db_path), api_host="",
                                  api_port=0, service_name="CT-Api-A"),),
        bots=(Bot(id="bot_a", name="Bot A", token_setting_key="tg_bot_token",
                  service_name="CT-Bot-A"),),
        routes=(Route(id="r_a", channel_id="ch_a", destination_id="dest_a"),),
        bot_bindings=(BotBinding(id="bind_a", bot_id="bot_a", scope="destination",
                                 destination_id="dest_a"),),
    )
    config_v2.save_v2(config, cfg_path)

    import importlib

    from src.gui.services import stack_registry
    importlib.reload(stack_registry)
    stacks = stack_registry.discover_stacks()

    assert len(stacks) == 1
    s = stacks[0]
    assert s.name == "Dest A"
    assert s.service_names == ("CT-Api-A", "CT-Bot-A", "CT-Listener-acc_x")
    # No backup file written — already v2.
    backup = cfg_path.with_suffix(cfg_path.suffix + ".v1.bak")
    assert not backup.exists()


def test_stack_registry_skips_disabled_routes(tmp_path: Path, monkeypatch) -> None:
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    cfg_path = appdata / "CopyTrades" / "stacks_config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    config = ConfigV2(
        accounts=(Account(id="a", name="a", phone="", session_path="", service_name="s"),),
        profiles=(Profile(id="p", name="p", path=""),),
        channels=(Channel(id="c", name="c", account_id="a", chat_id=1, profile_id="p"),),
        destinations=(Destination(id="d", name="D", db_path="",
                                  api_host="", api_port=0, service_name="CT-Api-D"),),
        bots=(),
        routes=(Route(id="r", channel_id="c", destination_id="d", enabled=False),),
        bot_bindings=(),
    )
    config_v2.save_v2(config, cfg_path)
    import importlib

    from src.gui.services import stack_registry
    importlib.reload(stack_registry)
    assert stack_registry.discover_stacks() == []
