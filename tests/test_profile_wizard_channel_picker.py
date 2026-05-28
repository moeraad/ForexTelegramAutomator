"""Profile-generator wizard: channel-picker for multi-channel destinations.

Verifies the intro page populates the picker correctly + the worker
honors WizardParameters.chat_id over the legacy fallback.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

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
from src.db import connect, init_schema
from src.gui.services.profile_wizard import (
    ProfileWizardWorker,
    WizardParameters,
)


def _seed_v2_with_n_channels(
    monkeypatch, tmp_path: Path, *, channel_count: int,
) -> tuple[Path, list[Channel]]:
    """Write a v2 config with N channels all routing to one destination."""
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    db_path = appdata / "CopyTrades" / "dest_x" / "copytrades.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(db_path))
    init_schema(conn)

    channels = tuple(
        Channel(id=f"ch_{i}", name=f"Channel {i}", account_id="acc_a",
                chat_id=-(1000 + i), profile_id="p")
        for i in range(channel_count)
    )
    routes = tuple(
        Route(id=f"r_{i}", channel_id=ch.id, destination_id="dest_x")
        for i, ch in enumerate(channels)
    )
    cfg = ConfigV2(
        accounts=(Account(id="acc_a", name="A", phone="+1",
                          session_path="", service_name="CT-Listener-acc_a"),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=channels,
        destinations=(Destination(
            id="dest_x", name="X", db_path=str(db_path),
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-X-Api",
        ),),
        bots=(Bot(id="b", name="B", token_setting_key="t",
                  service_name="CT-B-Bot"),),
        routes=routes,
        bot_bindings=(BotBinding(id="bind", bot_id="b",
                                 scope="destination",
                                 destination_id="dest_x"),),
    )
    config_v2.save_v2(cfg)
    return db_path, list(channels)


# ---- _IntroPage picker behavior ----------------------------------------


def _build_intro_page(qapp, qtbot, db_path: Path):
    """Construct the wizard's intro page + a stub Stack pointing at db_path."""
    from src.gui.windows.profile_generator_wizard import _IntroPage
    from src.gui.services.stack_registry import Stack
    stack = Stack(
        name="X", profile_path=Path("/p"), project_path=Path("/proj"),
        db_path=db_path, api_host="127.0.0.1", api_port=8765,
        service_names=("CT-X-Api",),
        primary_api_service="CT-X-Api",
    )
    page = _IntroPage()
    qtbot.addWidget(page)
    # Fake wizard owner so initializePage can find ``stack``.
    stub = MagicMock()
    stub.stack = stack
    page.wizard = lambda: stub
    return page


def test_picker_hidden_when_single_channel(qapp, qtbot, monkeypatch, tmp_path):
    db_path, _ = _seed_v2_with_n_channels(monkeypatch, tmp_path, channel_count=1)
    page = _build_intro_page(qapp, qtbot, db_path)
    page.show()
    page.initializePage()
    qapp.processEvents()
    # Combo populated (1 entry) but row hidden — operator has no choice to make.
    assert page._channel.count() == 1
    assert not page._channel.isVisible()
    page.close()


def test_picker_visible_when_multiple_channels(
    qapp, qtbot, monkeypatch, tmp_path,
):
    db_path, channels = _seed_v2_with_n_channels(
        monkeypatch, tmp_path, channel_count=3,
    )
    page = _build_intro_page(qapp, qtbot, db_path)
    page.show()
    page.initializePage()
    qapp.processEvents()
    assert page._channel.count() == 3
    assert page._channel.isVisible()
    # Combo entries show channel NAMES (not just ids).
    labels = [page._channel.itemText(i) for i in range(page._channel.count())]
    for ch in channels:
        assert any(ch.name in label for label in labels)
    page.close()


def test_picker_hidden_when_no_channels(qapp, qtbot, monkeypatch, tmp_path):
    db_path, _ = _seed_v2_with_n_channels(monkeypatch, tmp_path, channel_count=0)
    page = _build_intro_page(qapp, qtbot, db_path)
    page.show()
    page.initializePage()
    qapp.processEvents()
    assert page._channel.count() == 0
    assert not page._channel.isVisible()
    page.close()


def test_params_includes_chat_id_from_picker(qapp, qtbot, monkeypatch, tmp_path):
    db_path, channels = _seed_v2_with_n_channels(
        monkeypatch, tmp_path, channel_count=2,
    )
    page = _build_intro_page(qapp, qtbot, db_path)
    page.show()
    page.initializePage()
    qapp.processEvents()
    # Default: first channel selected.
    p = page.params()
    assert p.chat_id == channels[0].chat_id
    assert p.channel_name == channels[0].name
    # Pick the second.
    page._channel.setCurrentIndex(1)
    qapp.processEvents()
    p = page.params()
    assert p.chat_id == channels[1].chat_id
    page.close()


def test_params_returns_zero_chat_id_when_no_channels(
    qapp, qtbot, monkeypatch, tmp_path,
):
    """0-channel case: params.chat_id=0 → worker falls back to legacy."""
    db_path, _ = _seed_v2_with_n_channels(monkeypatch, tmp_path, channel_count=0)
    page = _build_intro_page(qapp, qtbot, db_path)
    page.show()
    page.initializePage()
    qapp.processEvents()
    p = page.params()
    assert p.chat_id == 0
    page.close()


# ---- Worker honors params.chat_id ---------------------------------------


def test_worker_uses_params_chat_id_over_legacy(monkeypatch, tmp_path):
    """params.chat_id wins over db_settings tg_watched_chat_id."""
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    from src import db_settings
    db_settings.set_str(db_path, "tg_watched_chat_id", "-9999")  # legacy

    from src.gui.services.stack_registry import Stack
    stack = Stack(
        name="X", profile_path=Path("/p"), project_path=Path("/proj"),
        db_path=db_path, api_host="127.0.0.1", api_port=8765,
        service_names=("CT-X-Api",),
        primary_api_service="CT-X-Api",
    )

    params = WizardParameters(chat_id=-12345, channel_name="Picked")
    worker = ProfileWizardWorker(stack, params)

    captured = {}

    async def _stub_fetch(db_path_arg, chat_id, *a, **kw):
        captured["chat_id"] = chat_id
        return []

    with patch("src.gui.services.profile_wizard._fetch_history",
               new=_stub_fetch):
        worker._do_pipeline()

    assert captured["chat_id"] == -12345  # picker value, NOT -9999 from legacy


def test_worker_falls_back_to_legacy_when_params_chat_id_zero(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    from src import db_settings
    db_settings.set_str(db_path, "tg_watched_chat_id", "-77777")  # legacy

    from src.gui.services.stack_registry import Stack
    stack = Stack(
        name="X", profile_path=Path("/p"), project_path=Path("/proj"),
        db_path=db_path, api_host="127.0.0.1", api_port=8765,
        service_names=("CT-X-Api",),
        primary_api_service="CT-X-Api",
    )

    params = WizardParameters(chat_id=0)  # explicit fallback request
    worker = ProfileWizardWorker(stack, params)

    captured = {}

    async def _stub_fetch(db_path_arg, chat_id, *a, **kw):
        captured["chat_id"] = chat_id
        return []

    with patch("src.gui.services.profile_wizard._fetch_history",
               new=_stub_fetch):
        worker._do_pipeline()

    assert captured["chat_id"] == -77777  # legacy value used as fallback


def test_worker_raises_when_no_chat_id_anywhere(monkeypatch, tmp_path):
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    # No tg_watched_chat_id seeded.

    from src.gui.services.stack_registry import Stack
    stack = Stack(
        name="X", profile_path=Path("/p"), project_path=Path("/proj"),
        db_path=db_path, api_host="127.0.0.1", api_port=8765,
        service_names=("CT-X-Api",),
        primary_api_service="CT-X-Api",
    )

    params = WizardParameters(chat_id=0)
    worker = ProfileWizardWorker(stack, params)
    with pytest.raises(RuntimeError, match="No channel selected"):
        worker._do_pipeline()
