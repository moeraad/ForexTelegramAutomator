"""Bot bindings transforms tests (Step 17 of multi-channel plan).

Covers the pure transforms backing the Bot Bindings GUI:
  - ``with_binding_added``: scope-target validation, uniqueness,
    reference checks, deterministic id
  - ``with_binding_removed``: surgical removal, raises on unknown id
  - ``detect_binding_overlaps``: pairs of distinct bots covering the
    same destination (the GUI warning)
"""
from __future__ import annotations

import pytest

from src.config_v2 import (
    Account,
    Bot,
    BotBinding,
    Channel,
    ConfigV2,
    Destination,
    Profile,
    Route,
    detect_binding_overlaps,
    with_binding_added,
    with_binding_removed,
)


def _cfg() -> ConfigV2:
    return ConfigV2(
        accounts=(Account(id="acc_a", name="A", phone="",
                          session_path="", service_name="CT-Listener-acc_a"),),
        profiles=(Profile(id="prof", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(
            Channel(id="ch_a", name="A", account_id="acc_a",
                    chat_id=-1, profile_id="prof"),
            Channel(id="ch_b", name="B", account_id="acc_a",
                    chat_id=-2, profile_id="prof"),
        ),
        destinations=(
            Destination(id="dest_x", name="X", db_path="/x.db",
                        api_host="127.0.0.1", api_port=8765,
                        service_name="CT-Api-X"),
            Destination(id="dest_y", name="Y", db_path="/y.db",
                        api_host="127.0.0.1", api_port=8766,
                        service_name="CT-Api-Y"),
        ),
        bots=(
            Bot(id="bot_main", name="Main", token_setting_key="t1",
                service_name="CT-Bot-Main"),
            Bot(id="bot_alert", name="Alert", token_setting_key="t2",
                service_name="CT-Bot-Alert"),
        ),
        routes=(
            Route(id="r_ax", channel_id="ch_a", destination_id="dest_x"),
            Route(id="r_ay", channel_id="ch_a", destination_id="dest_y"),
        ),
        bot_bindings=(),
    )


# ---- with_binding_added --------------------------------------------------


def test_with_binding_added_global_scope_appends():
    cfg = with_binding_added(_cfg(), bot_id="bot_main", scope="global")
    assert len(cfg.bot_bindings) == 1
    b = cfg.bot_bindings[0]
    assert b.bot_id == "bot_main"
    assert b.scope == "global"
    assert b.destination_id is None
    assert b.channel_id is None
    assert b.route_id is None


def test_with_binding_added_destination_scope_requires_destination_id():
    """BotBinding.__post_init__ raises on missing destination_id."""
    with pytest.raises(ValueError, match="destination_id"):
        with_binding_added(_cfg(), bot_id="bot_main", scope="destination")


def test_with_binding_added_destination_scope_appends():
    cfg = with_binding_added(
        _cfg(), bot_id="bot_main", scope="destination",
        destination_id="dest_x",
    )
    assert cfg.bot_bindings[0].destination_id == "dest_x"


def test_with_binding_added_channel_scope_appends():
    cfg = with_binding_added(
        _cfg(), bot_id="bot_alert", scope="channel", channel_id="ch_a",
    )
    assert cfg.bot_bindings[0].channel_id == "ch_a"


def test_with_binding_added_route_scope_appends():
    cfg = with_binding_added(
        _cfg(), bot_id="bot_alert", scope="route", route_id="r_ax",
    )
    assert cfg.bot_bindings[0].route_id == "r_ax"


def test_with_binding_added_rejects_unknown_bot():
    with pytest.raises(ValueError, match="Unknown bot"):
        with_binding_added(_cfg(), bot_id="bot_nope", scope="global")


def test_with_binding_added_rejects_unknown_destination():
    with pytest.raises(ValueError, match="Unknown destination"):
        with_binding_added(
            _cfg(), bot_id="bot_main", scope="destination",
            destination_id="dest_nope",
        )


def test_with_binding_added_rejects_unknown_channel():
    with pytest.raises(ValueError, match="Unknown channel"):
        with_binding_added(
            _cfg(), bot_id="bot_alert", scope="channel",
            channel_id="ch_nope",
        )


def test_with_binding_added_rejects_unknown_route():
    with pytest.raises(ValueError, match="Unknown route"):
        with_binding_added(
            _cfg(), bot_id="bot_alert", scope="route", route_id="r_nope",
        )


def test_with_binding_added_rejects_exact_duplicate():
    """Two identical bindings would just produce duplicate DMs — almost
    always misconfiguration. Either the deterministic-id collision OR
    the explicit duplicate-detection catches it (both safe outcomes)."""
    cfg = with_binding_added(
        _cfg(), bot_id="bot_alert", scope="channel", channel_id="ch_a",
    )
    with pytest.raises(ValueError, match="Duplicate binding|Binding id collision"):
        with_binding_added(
            cfg, bot_id="bot_alert", scope="channel", channel_id="ch_a",
        )


def test_with_binding_added_explicit_id_triggers_duplicate_detection():
    """When the operator passes an explicit (different) binding_id but the
    bot+scope+target combo already exists, the duplicate-content check fires."""
    cfg = with_binding_added(
        _cfg(), bot_id="bot_alert", scope="channel", channel_id="ch_a",
        binding_id="custom_1",
    )
    with pytest.raises(ValueError, match="Duplicate binding"):
        with_binding_added(
            cfg, bot_id="bot_alert", scope="channel", channel_id="ch_a",
            binding_id="custom_2",  # different id, same content
        )


def test_with_binding_added_uses_deterministic_id():
    """Toggle-off+on yields the same binding id — operator's stacks_config.json
    doesn't churn ids."""
    cfg1 = with_binding_added(
        _cfg(), bot_id="bot_main", scope="destination",
        destination_id="dest_x",
    )
    id1 = cfg1.bot_bindings[0].id
    cfg2 = with_binding_removed(cfg1, id1)
    cfg3 = with_binding_added(
        cfg2, bot_id="bot_main", scope="destination",
        destination_id="dest_x",
    )
    assert cfg3.bot_bindings[0].id == id1


def test_with_binding_added_accepts_explicit_id():
    cfg = with_binding_added(
        _cfg(), bot_id="bot_main", scope="global", binding_id="custom_bind",
    )
    assert cfg.bot_bindings[0].id == "custom_bind"


def test_with_binding_added_rejects_id_collision():
    cfg = with_binding_added(
        _cfg(), bot_id="bot_main", scope="global", binding_id="dup",
    )
    with pytest.raises(ValueError, match="Binding id collision"):
        with_binding_added(
            cfg, bot_id="bot_alert", scope="global", binding_id="dup",
        )


# ---- with_binding_removed ------------------------------------------------


def test_with_binding_removed_drops_target():
    cfg = with_binding_added(_cfg(), bot_id="bot_main", scope="global")
    bid = cfg.bot_bindings[0].id
    cfg2 = with_binding_removed(cfg, bid)
    assert len(cfg2.bot_bindings) == 0


def test_with_binding_removed_leaves_others_alone():
    cfg = with_binding_added(_cfg(), bot_id="bot_main", scope="global")
    cfg = with_binding_added(
        cfg, bot_id="bot_alert", scope="channel", channel_id="ch_a",
    )
    target = cfg.bot_bindings[0].id
    cfg2 = with_binding_removed(cfg, target)
    assert len(cfg2.bot_bindings) == 1
    assert cfg2.bot_bindings[0].id != target


def test_with_binding_removed_raises_on_unknown():
    with pytest.raises(ValueError, match="Unknown binding"):
        with_binding_removed(_cfg(), "bind_nope")


# ---- detect_binding_overlaps ---------------------------------------------


def test_overlaps_empty_when_no_bindings():
    assert detect_binding_overlaps(_cfg()) == ()


def test_overlaps_empty_when_single_bot():
    cfg = with_binding_added(_cfg(), bot_id="bot_main", scope="global")
    assert detect_binding_overlaps(cfg) == ()


def test_overlaps_detects_two_bots_covering_same_destination():
    """bot_main scope=destination dest_x, bot_alert scope=global
    → both cover dest_x and dest_y."""
    cfg = with_binding_added(
        _cfg(), bot_id="bot_main", scope="destination",
        destination_id="dest_x",
    )
    cfg = with_binding_added(cfg, bot_id="bot_alert", scope="global")
    overlaps = detect_binding_overlaps(cfg)
    # dest_x: bot_alert + bot_main; dest_y: bot_alert only (no overlap).
    assert overlaps == (("dest_x", "bot_alert", "bot_main"),)


def test_overlaps_detects_channel_and_destination_on_same_dest():
    """bot_main scope=destination dest_x + bot_alert scope=channel ch_a
    (which routes to dest_x) → overlap on dest_x.
    But ch_a also routes to dest_y → bot_alert solo on dest_y."""
    cfg = with_binding_added(
        _cfg(), bot_id="bot_main", scope="destination",
        destination_id="dest_x",
    )
    cfg = with_binding_added(
        cfg, bot_id="bot_alert", scope="channel", channel_id="ch_a",
    )
    overlaps = detect_binding_overlaps(cfg)
    # dest_x: bot_main + bot_alert (via channel). dest_y: bot_alert alone.
    assert overlaps == (("dest_x", "bot_alert", "bot_main"),)


def test_overlaps_does_not_flag_same_bot_multiple_bindings():
    """One bot with two bindings to the same dest (e.g., global + dest)
    is NOT an overlap — the bot collapses duplicate rows at delivery time.
    Only DISTINCT bots count as overlaps."""
    cfg = with_binding_added(_cfg(), bot_id="bot_main", scope="global")
    cfg = with_binding_added(
        cfg, bot_id="bot_main", scope="destination",
        destination_id="dest_x",
    )
    assert detect_binding_overlaps(cfg) == ()
