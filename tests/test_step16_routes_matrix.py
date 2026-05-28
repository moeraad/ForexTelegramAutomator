"""Routes matrix tests (Step 16 of multi-channel plan).

Covers the pure transforms backing the matrix GUI:
  - ``with_route_added``: cell-toggle-on creates a Route
  - ``with_route_removed``: cell-toggle-off removes the Route (+ any
    scope=route bot bindings pointing at it)
  - ``with_route_sizing``: inline sizing-multiplier edit

Validation invariants:
  - Same (channel, destination) cannot be checked twice (one Route per cell)
  - Unknown channel/destination/route ids raise ValueError
  - Negative sizing_multiplier rejected
  - All transforms return NEW ConfigV2 instances (immutability)
  - Route id is deterministic when not specified (no churn across
    toggle-off + toggle-on)
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
    Route,
    with_route_added,
    with_route_removed,
    with_route_sizing,
)


def _cfg() -> ConfigV2:
    return ConfigV2(
        accounts=(Account(
            id="acc_primary", name="P", phone="",
            session_path="", service_name="CT-Listener-acc_primary",
        ),),
        channels=(
            Channel(id="ch_a", name="A", account_id="acc_primary",
                    chat_id=-1, profile_id="p"),
            Channel(id="ch_b", name="B", account_id="acc_primary",
                    chat_id=-2, profile_id="p"),
        ),
        destinations=(
            Destination(id="dest_x", name="X", db_path="/x.db",
                        api_host="127.0.0.1", api_port=8765,
                        service_name="CT-Api-X"),
            Destination(id="dest_y", name="Y", db_path="/y.db",
                        api_host="127.0.0.1", api_port=8766,
                        service_name="CT-Api-Y"),
        ),
        bots=(Bot(id="bot_x", name="B", token_setting_key="t",
                  service_name="CT-Bot-X"),),
        routes=(),
        bot_bindings=(),
    )


# ---- with_route_added -----------------------------------------------------


def test_with_route_added_creates_new_route():
    cfg = _cfg()
    new_cfg = with_route_added(
        cfg, channel_id="ch_a", destination_id="dest_x",
    )
    assert len(new_cfg.routes) == 1
    assert new_cfg.routes[0].channel_id == "ch_a"
    assert new_cfg.routes[0].destination_id == "dest_x"
    assert new_cfg.routes[0].sizing_multiplier == 1.0
    # Original cfg is untouched.
    assert len(cfg.routes) == 0


def test_with_route_added_uses_deterministic_id_by_default():
    """Toggle-off + toggle-on must produce the same route id so the
    operator's stacks_config.json doesn't churn route ids."""
    cfg = _cfg()
    cfg1 = with_route_added(cfg, channel_id="ch_a", destination_id="dest_x")
    rid_1 = cfg1.routes[0].id
    cfg2 = with_route_removed(cfg1, rid_1)
    cfg3 = with_route_added(cfg2, channel_id="ch_a", destination_id="dest_x")
    assert cfg3.routes[0].id == rid_1


def test_with_route_added_accepts_custom_sizing_multiplier():
    cfg = with_route_added(
        _cfg(), channel_id="ch_a", destination_id="dest_x",
        sizing_multiplier=0.5,
    )
    assert cfg.routes[0].sizing_multiplier == 0.5


def test_with_route_added_rejects_unknown_channel():
    with pytest.raises(ValueError, match="Unknown channel"):
        with_route_added(_cfg(), channel_id="ch_nope", destination_id="dest_x")


def test_with_route_added_rejects_unknown_destination():
    with pytest.raises(ValueError, match="Unknown destination"):
        with_route_added(_cfg(), channel_id="ch_a", destination_id="dest_nope")


def test_with_route_added_rejects_duplicate_cell():
    cfg = with_route_added(_cfg(), channel_id="ch_a", destination_id="dest_x")
    with pytest.raises(ValueError, match="Route already exists"):
        with_route_added(cfg, channel_id="ch_a", destination_id="dest_x")


def test_with_route_added_rejects_negative_sizing():
    with pytest.raises(ValueError, match="sizing_multiplier"):
        with_route_added(
            _cfg(), channel_id="ch_a", destination_id="dest_x",
            sizing_multiplier=-0.1,
        )


def test_with_route_added_accepts_explicit_route_id():
    cfg = with_route_added(
        _cfg(), channel_id="ch_a", destination_id="dest_x",
        route_id="custom_id",
    )
    assert cfg.routes[0].id == "custom_id"


def test_with_route_added_rejects_id_collision():
    cfg = with_route_added(
        _cfg(), channel_id="ch_a", destination_id="dest_x",
        route_id="dup",
    )
    with pytest.raises(ValueError, match="Route id collision"):
        with_route_added(
            cfg, channel_id="ch_b", destination_id="dest_x",
            route_id="dup",
        )


# ---- with_route_removed ---------------------------------------------------


def test_with_route_removed_drops_the_route():
    cfg = with_route_added(_cfg(), channel_id="ch_a", destination_id="dest_x")
    rid = cfg.routes[0].id
    cfg2 = with_route_removed(cfg, rid)
    assert len(cfg2.routes) == 0


def test_with_route_removed_leaves_other_routes_alone():
    cfg = with_route_added(_cfg(), channel_id="ch_a", destination_id="dest_x")
    cfg = with_route_added(cfg, channel_id="ch_b", destination_id="dest_y")
    target = cfg.routes[0].id
    cfg2 = with_route_removed(cfg, target)
    assert len(cfg2.routes) == 1
    assert cfg2.routes[0].id != target


def test_with_route_removed_raises_on_unknown_id():
    with pytest.raises(ValueError, match="Unknown route"):
        with_route_removed(_cfg(), "route_nope")


def test_with_route_removed_drops_scope_route_bindings():
    """Forward-compat for Step 14: a route-scoped binding can't outlive
    the route. ``with_route_removed`` cascades the binding cleanup so
    the operator doesn't end up with orphan dispatcher rules."""
    cfg = with_route_added(
        _cfg(), channel_id="ch_a", destination_id="dest_x",
        route_id="r_target",
    )
    cfg_with_binding = ConfigV2(
        accounts=cfg.accounts, channels=cfg.channels,
        destinations=cfg.destinations, bots=cfg.bots, routes=cfg.routes,
        bot_bindings=(
            BotBinding(id="b1", bot_id="bot_x", scope="route",
                       route_id="r_target"),
            BotBinding(id="b2", bot_id="bot_x", scope="destination",
                       destination_id="dest_x"),
        ),
    )
    cfg2 = with_route_removed(cfg_with_binding, "r_target")
    # scope=route binding is gone; scope=destination survives.
    binding_ids = [b.id for b in cfg2.bot_bindings]
    assert "b1" not in binding_ids
    assert "b2" in binding_ids


# ---- with_route_sizing ----------------------------------------------------


def test_with_route_sizing_updates_multiplier():
    cfg = with_route_added(_cfg(), channel_id="ch_a", destination_id="dest_x")
    rid = cfg.routes[0].id
    cfg2 = with_route_sizing(cfg, rid, 0.25)
    assert cfg2.routes[0].sizing_multiplier == 0.25
    # Original cfg untouched (immutability).
    assert cfg.routes[0].sizing_multiplier == 1.0


def test_with_route_sizing_accepts_zero():
    """Zero is allowed: operator setting sizing to 0 effectively disables
    the route execution-side (lots become 0) without removing the row.
    Useful for temporary pause without losing the route's id."""
    cfg = with_route_added(_cfg(), channel_id="ch_a", destination_id="dest_x")
    cfg2 = with_route_sizing(cfg, cfg.routes[0].id, 0.0)
    assert cfg2.routes[0].sizing_multiplier == 0.0


def test_with_route_sizing_rejects_negative():
    cfg = with_route_added(_cfg(), channel_id="ch_a", destination_id="dest_x")
    with pytest.raises(ValueError, match="sizing_multiplier"):
        with_route_sizing(cfg, cfg.routes[0].id, -1.0)


def test_with_route_sizing_raises_on_unknown_id():
    with pytest.raises(ValueError, match="Unknown route"):
        with_route_sizing(_cfg(), "route_nope", 0.5)


# ---- Composed scenarios mirroring the matrix UI --------------------------


def test_matrix_check_uncheck_check_roundtrips():
    """Operator clicks a cell on (route created) → off (route gone) → on
    (route re-created). The end state must equal the first 'on' state
    (modulo route order)."""
    cfg = _cfg()
    cfg1 = with_route_added(cfg, channel_id="ch_a", destination_id="dest_x")
    cfg2 = with_route_removed(cfg1, cfg1.routes[0].id)
    cfg3 = with_route_added(cfg2, channel_id="ch_a", destination_id="dest_x")
    assert len(cfg3.routes) == 1
    assert cfg3.routes[0].channel_id == "ch_a"
    assert cfg3.routes[0].destination_id == "dest_x"
    # Deterministic id stayed stable across toggle-off+on.
    assert cfg3.routes[0].id == cfg1.routes[0].id


def test_matrix_full_mesh_creation():
    """All cells checked: 2 channels × 2 destinations = 4 routes."""
    cfg = _cfg()
    cfg = with_route_added(cfg, channel_id="ch_a", destination_id="dest_x")
    cfg = with_route_added(cfg, channel_id="ch_a", destination_id="dest_y")
    cfg = with_route_added(cfg, channel_id="ch_b", destination_id="dest_x")
    cfg = with_route_added(cfg, channel_id="ch_b", destination_id="dest_y")
    assert len(cfg.routes) == 4
    # Each cell has its own route id.
    assert len({r.id for r in cfg.routes}) == 4


def test_matrix_aggregate_two_channels_one_destination():
    """Aggregate topology: ch_a and ch_b both route to dest_x; dest_y
    has nothing. Matches the Step 12 acceptance scenario."""
    cfg = _cfg()
    cfg = with_route_added(cfg, channel_id="ch_a", destination_id="dest_x")
    cfg = with_route_added(cfg, channel_id="ch_b", destination_id="dest_x")
    routes_to_x = [r for r in cfg.routes if r.destination_id == "dest_x"]
    assert len(routes_to_x) == 2
    assert {r.channel_id for r in routes_to_x} == {"ch_a", "ch_b"}


def test_matrix_mirror_one_channel_to_two_destinations():
    """Mirror topology: ch_a routes to BOTH dest_x and dest_y."""
    cfg = _cfg()
    cfg = with_route_added(cfg, channel_id="ch_a", destination_id="dest_x")
    cfg = with_route_added(cfg, channel_id="ch_a", destination_id="dest_y")
    routes_from_a = [r for r in cfg.routes if r.channel_id == "ch_a"]
    assert len(routes_from_a) == 2
    assert {r.destination_id for r in routes_from_a} == {"dest_x", "dest_y"}
