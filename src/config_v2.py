"""v2 multi-channel / multi-destination configuration.

Defines the seven first-class entities (Account, Profile, Channel, Destination,
Bot, Route, BotBinding) plus the ``ConfigV2`` container with lookup helpers and
JSON load/save.

See ``docs/plans/2026-05-23-multi-channel-routing.md`` for the architectural
context. This module is the foundation laid in Step 1; subsequent steps build
on top of it.

The on-disk format lives at ``%APPDATA%/CopyTrades/stacks_config.json`` (same
path as v1). A ``"version": 2`` field at the top distinguishes the formats so
the loader can route old files through the migration shim.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal


CONFIG_VERSION = 2

BindingScope = Literal["global", "destination", "channel", "route"]
_VALID_SCOPES: frozenset[str] = frozenset(("global", "destination", "channel", "route"))


@dataclass(frozen=True)
class Account:
    """One Telegram user account (one Telethon session, one phone number).

    ``phone`` is stored plaintext in ``stacks_config.json`` today. This is
    PII — accidental backups, support screenshots, or filesystem reads
    expose the operator's phone number. Use ``phone_display()`` whenever
    surfacing the phone to humans (GUI, logs, exports). Direct ``.phone``
    access is permitted only on paths that must round-trip the actual
    value back to Telegram (Telethon auth).

    Full encryption-at-rest for plaintext phones in v2 config is tracked
    in ``docs/plans/sunset-list.md`` as a deferred migration.
    """
    id: str
    name: str
    phone: str
    session_path: str
    service_name: str
    # Telethon API credentials — from my.telegram.org. Optional in v2
    # config (default 0/"" so existing rows still load); fully required
    # to actually run the listener. When set, services auto-sync them to
    # the destination DB's tg_api_id / tg_api_hash settings so the
    # listener has them on startup without the operator pasting twice.
    api_id: int = 0
    api_hash: str = ""

    def phone_display(self, *, redact: bool = True) -> str:
        """Human-friendly phone string with optional PII redaction.

        Default behavior redacts the middle digits — keeps country code +
        last four for operator recognition, hides the rest:
            "+9611234567" -> "+961***4567"
            "+15551234"   -> "+155***1234"
            "+xx"         -> "+xx"  (too short to redact meaningfully)
            ""            -> "<unset>"

        Pass ``redact=False`` to get the raw value (Telethon auth + sub-
        millisecond debug paths only — never for log lines or DM text).
        """
        raw = (self.phone or "").strip()
        if not raw:
            return "<unset>"
        if not redact:
            return raw
        digits_only = "".join(c for c in raw if c.isdigit())
        if len(digits_only) <= 6:
            # Phone too short to redact meaningfully without losing identifier
            # value; return as-is. Real Telegram numbers are 10+ digits.
            return raw
        # Preserve the leading + and country-code-ish prefix (first 3
        # significant digits) + the last 4. Mask everything in between.
        prefix_end = raw.find(digits_only[3])
        # prefix_end points at the START of the 4th digit; we want the
        # first 3 digits + leading non-digit chars (the '+' and any space).
        # Find by counting digits manually:
        seen = 0
        cut = 0
        for i, ch in enumerate(raw):
            if ch.isdigit():
                seen += 1
                if seen == 3:
                    cut = i + 1
                    break
        return f"{raw[:cut]}***{digits_only[-4:]}"


@dataclass(frozen=True)
class Profile:
    """AI prompt config for interpreting a channel's vocabulary.

    ``path`` points at the standalone profile JSON file (the vocabulary,
    examples, idempotency rules) — the profile content stays in its own
    file rather than being embedded here.
    """
    id: str
    name: str
    path: str
    language: str = ""
    symbol: str = ""


@dataclass(frozen=True)
class Channel:
    """A Telegram chat being watched.

    Owned by ``account_id`` (whose listener subscribes to it). Interpreted
    using ``profile_id``. ``halted`` is forward-compat for Step 15 — only
    Destination halt is honored in the current scope.
    """
    id: str
    name: str
    account_id: str
    chat_id: int
    profile_id: str
    enabled: bool = True
    halted: bool = False


@dataclass(frozen=True)
class Destination:
    """A trading endpoint: one MT5 terminal + one DB + one API process."""
    id: str
    name: str
    db_path: str
    api_host: str
    api_port: int
    service_name: str
    mt5_label: str = ""


@dataclass(frozen=True)
class Bot:
    """Operator-facing Telegram bot.

    The token itself is stored in each destination's ``settings`` table under
    ``token_setting_key`` (never in this config file) so the on-disk config
    can be plaintext without leaking secrets.
    """
    id: str
    name: str
    token_setting_key: str
    service_name: str


@dataclass(frozen=True)
class Route:
    """Edge: ``(channel_id, destination_id)``.

    ``sizing_multiplier`` is applied to lot sizes when the destination acts
    on a signal from this channel. ``halted`` is forward-compat for Step 15.

    Step 20 per-route rules (all optional; ``0`` / ``None`` / empty = disabled):
      - ``max_lots``: hard cap on lots regardless of multiplier (EA-enforced
        via the existing ``MaxLotsPerSignal`` pattern, propagated via the
        OPEN payload)
      - ``min_account_balance``: skip route if destination's MT5 balance is
        below this threshold (EA-enforced; the EA refuses with reason
        ``route_rule_balance_too_low``)
      - ``skip_if_drawdown_pct``: skip route if destination's drawdown
        exceeds this percentage (EA-enforced; reason
        ``route_rule_drawdown_too_high``)
      - ``allowed_action_types``: when non-empty, only these action types
        flow through this route (e.g. ``("OPEN",)`` for an "entries-only"
        route — API-enforced before persistence)
      - ``time_of_day_filter``: ``"HH:MM-HH:MM"`` UTC window; outside this
        window the route silently drops actions (API-enforced before
        persistence). Empty string disables.
    """
    id: str
    channel_id: str
    destination_id: str
    enabled: bool = True
    halted: bool = False
    sizing_multiplier: float = 1.0
    max_lots: float = 0.0
    min_account_balance: float = 0.0
    skip_if_drawdown_pct: float = 0.0
    allowed_action_types: tuple[str, ...] = ()
    time_of_day_filter: str = ""
    # Step 21: when set, the listener retries against this Destination's
    # API if the primary POST fails (timeout / network error / 5xx).
    # Empty = no failover (current behavior).
    fallback_destination_id: str = ""


@dataclass(frozen=True)
class BotBinding:
    """Edge: ``(bot_id, scope)``.

    Scope determines which events flow to this bot's outbox:
      - ``global``: every event from every destination
      - ``destination``: events on ``destination_id`` only
      - ``channel``: events whose ``source_channel_id == channel_id``
      - ``route``: events for ``route_id`` only

    Only ``scope=destination`` is wired in the dispatcher in the current
    scope (Step 7). The other scopes are reserved for Step 14.
    """
    id: str
    bot_id: str
    scope: BindingScope
    destination_id: str | None = None
    channel_id: str | None = None
    route_id: str | None = None

    def __post_init__(self) -> None:
        if self.scope not in _VALID_SCOPES:
            raise ValueError(
                f"BotBinding.scope must be one of {sorted(_VALID_SCOPES)}; got {self.scope!r}"
            )
        if self.scope == "destination" and not self.destination_id:
            raise ValueError("BotBinding scope=destination requires destination_id")
        if self.scope == "channel" and not self.channel_id:
            raise ValueError("BotBinding scope=channel requires channel_id")
        if self.scope == "route" and not self.route_id:
            raise ValueError("BotBinding scope=route requires route_id")


@dataclass(frozen=True)
class ConfigV2:
    """In-memory representation of the v2 config file.

    Tuples (not lists) so the whole object is hashable / immutable. Mutate
    via ``dataclasses.replace`` or by rebuilding tuples.
    """
    accounts: tuple[Account, ...] = ()
    profiles: tuple[Profile, ...] = ()
    channels: tuple[Channel, ...] = ()
    destinations: tuple[Destination, ...] = ()
    bots: tuple[Bot, ...] = ()
    routes: tuple[Route, ...] = ()
    bot_bindings: tuple[BotBinding, ...] = ()

    # ---- Single-entity lookups --------------------------------------------
    def account(self, account_id: str) -> Account | None:
        return next((a for a in self.accounts if a.id == account_id), None)

    def profile(self, profile_id: str) -> Profile | None:
        return next((p for p in self.profiles if p.id == profile_id), None)

    def channel(self, channel_id: str) -> Channel | None:
        return next((c for c in self.channels if c.id == channel_id), None)

    def destination(self, destination_id: str) -> Destination | None:
        return next((d for d in self.destinations if d.id == destination_id), None)

    def bot(self, bot_id: str) -> Bot | None:
        return next((b for b in self.bots if b.id == bot_id), None)

    def route(self, route_id: str) -> Route | None:
        return next((r for r in self.routes if r.id == route_id), None)

    def channel_by_chat_id(self, chat_id: int) -> Channel | None:
        """Used by the shared listener to route incoming messages."""
        return next((c for c in self.channels if c.chat_id == chat_id), None)

    def channel_name(self, channel_id: str) -> str:
        """Display name for a channel id, falling back to the id itself.

        Step 12: used by DM renderers + GUI filters to label which
        channel triggered an event. Returns the id when the channel
        isn't in the config (e.g. row tagged with a since-deleted
        channel) so the operator still sees a stable identifier.
        """
        c = self.channel(channel_id)
        return c.name if c is not None else channel_id

    # ---- Filtered lookups -------------------------------------------------
    def channels_for_account(self, account_id: str) -> tuple[Channel, ...]:
        return tuple(c for c in self.channels if c.account_id == account_id)

    def routes_for_channel(self, channel_id: str) -> tuple[Route, ...]:
        return tuple(r for r in self.routes if r.channel_id == channel_id)

    def routes_for_destination(self, destination_id: str) -> tuple[Route, ...]:
        return tuple(r for r in self.routes if r.destination_id == destination_id)

    def bindings_for_destination(self, destination_id: str) -> tuple[BotBinding, ...]:
        """Bindings that could match events flowing through this destination.

        Step 14 — all four scopes resolved here. The dispatcher does
        per-event filtering on top (e.g. a ``scope=channel`` binding only
        fires for events whose ``source_channel_id`` matches the binding's
        ``channel_id``); this method's job is to widen the candidate set
        as much as possible without missing potential matches.

        Resolution:
          - ``scope=global``: always included (bot wants everything)
          - ``scope=destination``: included when ``destination_id`` matches
          - ``scope=channel``: included when the channel has a Route
            targeting this destination (otherwise events from that channel
            never reach this DB anyway)
          - ``scope=route``: included when the route targets this destination

        The channel-scope check uses ``routes_for_channel`` to handle
        mirror routing (one channel → N destinations): a single
        channel-scoped binding flows DMs to every destination the
        channel mirrors through.
        """
        channel_ids_with_route_here = {
            r.channel_id for r in self.routes
            if r.destination_id == destination_id and r.enabled
        }
        route_ids_targeting_here = {
            r.id for r in self.routes
            if r.destination_id == destination_id and r.enabled
        }
        out: list[BotBinding] = []
        for b in self.bot_bindings:
            if b.scope == "global":
                out.append(b)
            elif b.scope == "destination" and b.destination_id == destination_id:
                out.append(b)
            elif b.scope == "channel" and b.channel_id in channel_ids_with_route_here:
                out.append(b)
            elif b.scope == "route" and b.route_id in route_ids_targeting_here:
                out.append(b)
        return tuple(out)

    def destinations_for_bot(self, bot_id: str) -> tuple["Destination", ...]:
        """Destinations whose ``bot_outbox`` this bot needs to tail.

        Step 14: a multi-scope bot may need to poll N destination DBs.
        Examples:
          - ``scope=global`` → every Destination
          - ``scope=destination`` → just that one
          - ``scope=channel`` → every Destination that channel mirrors to
          - ``scope=route`` → the Route's single Destination

        Returns the de-duplicated, order-stable set (matches
        ``self.destinations`` order). The bot process opens one DB
        connection per returned Destination and runs the tailer against
        all of them in parallel.
        """
        wanted_dest_ids: set[str] = set()
        bindings = [b for b in self.bot_bindings if b.bot_id == bot_id]
        if not bindings:
            return ()
        for b in bindings:
            if b.scope == "global":
                # Short-circuit: global covers every destination.
                return tuple(self.destinations)
            if b.scope == "destination" and b.destination_id:
                wanted_dest_ids.add(b.destination_id)
            elif b.scope == "channel" and b.channel_id:
                for r in self.routes:
                    if r.channel_id == b.channel_id and r.enabled:
                        wanted_dest_ids.add(r.destination_id)
            elif b.scope == "route" and b.route_id:
                route = self.route(b.route_id)
                if route is not None and route.enabled:
                    wanted_dest_ids.add(route.destination_id)
        return tuple(d for d in self.destinations if d.id in wanted_dest_ids)


def config_path() -> Path:
    """Location of the v2 (and v1) config file on disk."""
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / "CopyTrades" / "stacks_config.json"


def is_v2(path: Path | None = None) -> bool:
    """Quick check: does the file at ``path`` declare ``"version": 2``?"""
    p = path or config_path()
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return isinstance(data, dict) and data.get("version") == CONFIG_VERSION


def with_channel_halted(
    cfg: ConfigV2, channel_id: str, halted: bool,
) -> ConfigV2:
    """Pure transform: return a new ``ConfigV2`` with ``Channel.halted`` flipped.

    Step 15: halt is per-channel (orchestrator skips action emission for
    any message tagged with this channel_id, regardless of route). The
    operator-facing call is in v2_config_view; this function isolates
    the transform so it's trivially testable.

    Raises ValueError when the channel id isn't in the config.
    """
    target = cfg.channel(channel_id)
    if target is None:
        raise ValueError(f"Unknown channel: {channel_id}")
    new_channels = tuple(
        replace(c, halted=halted) if c.id == channel_id else c
        for c in cfg.channels
    )
    return replace(cfg, channels=new_channels)


def with_route_halted(
    cfg: ConfigV2, route_id: str, halted: bool,
) -> ConfigV2:
    """Pure transform: return a new ``ConfigV2`` with ``Route.halted`` flipped.

    Step 15: halt is also per-route — useful for mirror setups where one
    leg (e.g. demo destination) should pause without halting the channel
    (which would also halt the live leg). Channel halt and route halt are
    both checked at the API boundary; either one short-circuits.

    Raises ValueError when the route id isn't in the config.
    """
    target = cfg.route(route_id)
    if target is None:
        raise ValueError(f"Unknown route: {route_id}")
    new_routes = tuple(
        replace(r, halted=halted) if r.id == route_id else r
        for r in cfg.routes
    )
    return replace(cfg, routes=new_routes)


def with_account_added(
    cfg: ConfigV2, *,
    account_id: str,
    name: str,
    phone: str = "",
    session_path: str = "",
    service_name: str = "",
    api_id: int = 0,
    api_hash: str = "",
) -> ConfigV2:
    """Pure transform: append a new ``Account`` to ``cfg``.

    Operator-facing Telethon auth (logging in the phone number, accepting
    the SMS code, etc.) is handled by ``telegram_wizard`` after this
    transform persists. This transform just records the metadata.

    Raises ValueError on blank name/id or id collision.
    """
    if not account_id:
        raise ValueError("account_id is required")
    if not name:
        raise ValueError("Name is required")
    if cfg.account(account_id) is not None:
        raise ValueError(f"Account id collision: {account_id}")
    svc = service_name or f"CT-Listener-{account_id}"
    new_account = Account(
        id=account_id, name=name, phone=phone,
        session_path=session_path, service_name=svc,
        api_id=api_id, api_hash=api_hash,
    )
    return replace(cfg, accounts=cfg.accounts + (new_account,))


def with_profile_added(
    cfg: ConfigV2, *,
    profile_id: str,
    name: str,
    path: str,
    language: str = "",
    symbol: str = "",
) -> ConfigV2:
    """Pure transform: append a new ``Profile`` to ``cfg``.

    ``path`` points at the JSON file the AI prompt is loaded from.
    Caller is responsible for creating/populating that file separately;
    this transform just records the reference.

    Raises ValueError on blank fields or id collision.
    """
    if not profile_id:
        raise ValueError("profile_id is required")
    if not name:
        raise ValueError("Name is required")
    if not path:
        raise ValueError("Path is required (where the profile JSON lives)")
    if cfg.profile(profile_id) is not None:
        raise ValueError(f"Profile id collision: {profile_id}")
    new_profile = Profile(
        id=profile_id, name=name, path=path,
        language=language, symbol=symbol,
    )
    return replace(cfg, profiles=cfg.profiles + (new_profile,))


def with_destination_added(
    cfg: ConfigV2, *,
    destination_id: str,
    name: str,
    db_path: str,
    api_host: str = "127.0.0.1",
    api_port: int = 8765,
    service_name: str = "",
    mt5_label: str = "",
) -> ConfigV2:
    """Pure transform: append a new ``Destination`` to ``cfg``.

    The DB file itself isn't created here — the API process will
    ``init_schema`` against ``db_path`` on first start. Operator must
    point at a writable path (typically ``%APPDATA%\\CopyTrades\\<name>\\copytrades.db``).

    Validates:
      - id not blank, not colliding
      - name not blank
      - db_path not blank
      - api_port in [1, 65535]
      - api_port not already used by another destination (catches
        copy-paste mistakes that would race for the same socket)
    """
    if not destination_id:
        raise ValueError("destination_id is required")
    if not name:
        raise ValueError("Name is required")
    if not db_path:
        raise ValueError("db_path is required")
    if not (1 <= api_port <= 65535):
        raise ValueError(f"api_port must be 1..65535; got {api_port}")
    if cfg.destination(destination_id) is not None:
        raise ValueError(f"Destination id collision: {destination_id}")
    for existing in cfg.destinations:
        if existing.api_port == api_port and existing.api_host == api_host:
            raise ValueError(
                f"Destination {existing.id!r} already binds {api_host}:{api_port}; "
                "pick a different port to avoid a socket race on service start."
            )
    svc = service_name or f"CT-Api-{destination_id}"
    new_dest = Destination(
        id=destination_id, name=name, db_path=db_path,
        api_host=api_host, api_port=api_port,
        service_name=svc, mt5_label=mt5_label,
    )
    return replace(cfg, destinations=cfg.destinations + (new_dest,))


def with_bot_added(
    cfg: ConfigV2, *,
    bot_id: str,
    name: str,
    token_setting_key: str,
    service_name: str = "",
) -> ConfigV2:
    """Pure transform: append a new ``Bot`` to ``cfg``.

    The actual Telegram bot token is stored separately in each
    destination's ``settings`` table under ``token_setting_key`` (never
    in the v2 config file). This transform just records the reference;
    operator pastes the token via the Settings flow (or hand-inserts
    into the DB) afterwards.

    Validates: id/name/token_setting_key not blank, no id collision.
    """
    if not bot_id:
        raise ValueError("bot_id is required")
    if not name:
        raise ValueError("Name is required")
    if not token_setting_key:
        raise ValueError(
            "token_setting_key is required (the settings-table key under "
            "which each destination stores this bot's encrypted token)"
        )
    if cfg.bot(bot_id) is not None:
        raise ValueError(f"Bot id collision: {bot_id}")
    svc = service_name or f"CT-Bot-{bot_id}"
    new_bot = Bot(
        id=bot_id, name=name,
        token_setting_key=token_setting_key, service_name=svc,
    )
    return replace(cfg, bots=cfg.bots + (new_bot,))


def with_route_added(
    cfg: ConfigV2, *,
    channel_id: str,
    destination_id: str,
    sizing_multiplier: float = 1.0,
    route_id: str | None = None,
) -> ConfigV2:
    """Pure transform: return a new ``ConfigV2`` with one extra ``Route``.

    Step 16: backs the Routes Matrix GUI's cell-toggle (channel × destination
    grid → check a cell = create a route). When ``route_id`` is None, a
    deterministic id is derived from ``(channel_id, destination_id)`` so
    the matrix can toggle a cell off+on without spawning a new id each
    time (avoids visual id churn for operators reading stacks_config.json).

    Raises:
      - ValueError if channel_id or destination_id is unknown
      - ValueError if a Route already exists for this (channel, destination)
      - ValueError if sizing_multiplier is negative
    """
    if cfg.channel(channel_id) is None:
        raise ValueError(f"Unknown channel: {channel_id}")
    if cfg.destination(destination_id) is None:
        raise ValueError(f"Unknown destination: {destination_id}")
    if sizing_multiplier < 0:
        raise ValueError(
            f"sizing_multiplier must be >= 0; got {sizing_multiplier}"
        )
    # Reject duplicate (channel, destination) edges — the matrix model
    # treats each cell as 0-or-1 routes.
    for r in cfg.routes:
        if r.channel_id == channel_id and r.destination_id == destination_id:
            raise ValueError(
                f"Route already exists for ({channel_id} → {destination_id})"
            )
    rid = (route_id or f"route_{channel_id}__{destination_id}").strip()
    if cfg.route(rid) is not None:
        raise ValueError(f"Route id collision: {rid}")
    new_route = Route(
        id=rid, channel_id=channel_id, destination_id=destination_id,
        enabled=True, halted=False, sizing_multiplier=sizing_multiplier,
    )
    return replace(cfg, routes=cfg.routes + (new_route,))


def with_channel_removed(cfg: ConfigV2, channel_id: str) -> ConfigV2:
    """Remove a Channel + cascade any Routes/BotBindings referencing it.

    Bot bindings scoped to this channel are dropped. Routes whose
    ``channel_id`` matches are removed via ``with_route_removed`` so
    their own route-scoped bindings cascade too.
    """
    if cfg.channel(channel_id) is None:
        raise ValueError(f"Unknown channel: {channel_id}")
    # Cascade routes first (they cascade their own route-scoped bindings).
    cfg = replace(cfg)
    for r in [r for r in cfg.routes if r.channel_id == channel_id]:
        cfg = with_route_removed(cfg, r.id)
    new_channels = tuple(c for c in cfg.channels if c.id != channel_id)
    new_bindings = tuple(
        b for b in cfg.bot_bindings
        if not (b.scope == "channel" and b.channel_id == channel_id)
    )
    return replace(cfg, channels=new_channels, bot_bindings=new_bindings)


def with_account_removed(cfg: ConfigV2, account_id: str) -> ConfigV2:
    """Remove an Account + cascade every Channel that belongs to it."""
    if cfg.account(account_id) is None:
        raise ValueError(f"Unknown account: {account_id}")
    for ch in [ch for ch in cfg.channels if ch.account_id == account_id]:
        cfg = with_channel_removed(cfg, ch.id)
    new_accounts = tuple(a for a in cfg.accounts if a.id != account_id)
    return replace(cfg, accounts=new_accounts)


def with_profile_removed(cfg: ConfigV2, profile_id: str) -> ConfigV2:
    """Remove a Profile.

    Refuses if any Channel still references it — operator must reassign
    or delete those Channels first. Profiles are heavyweight (they own
    the AI prompt + worked examples); a silent cascade would surprise.
    """
    if cfg.profile(profile_id) is None:
        raise ValueError(f"Unknown profile: {profile_id}")
    referrers = [c.id for c in cfg.channels if c.profile_id == profile_id]
    if referrers:
        raise ValueError(
            f"Profile {profile_id} is still used by channel(s): "
            f"{', '.join(referrers)}. Reassign or remove those first."
        )
    new_profiles = tuple(p for p in cfg.profiles if p.id != profile_id)
    return replace(cfg, profiles=new_profiles)


def with_destination_removed(cfg: ConfigV2, destination_id: str) -> ConfigV2:
    """Remove a Destination + cascade any Routes/BotBindings hitting it."""
    if cfg.destination(destination_id) is None:
        raise ValueError(f"Unknown destination: {destination_id}")
    for r in [r for r in cfg.routes if r.destination_id == destination_id]:
        cfg = with_route_removed(cfg, r.id)
    new_dests = tuple(d for d in cfg.destinations if d.id != destination_id)
    new_bindings = tuple(
        b for b in cfg.bot_bindings
        if not (b.scope == "destination" and b.destination_id == destination_id)
    )
    return replace(cfg, destinations=new_dests, bot_bindings=new_bindings)


def with_bot_removed(cfg: ConfigV2, bot_id: str) -> ConfigV2:
    """Remove a Bot + every BotBinding owned by it."""
    if cfg.bot(bot_id) is None:
        raise ValueError(f"Unknown bot: {bot_id}")
    new_bots = tuple(b for b in cfg.bots if b.id != bot_id)
    new_bindings = tuple(b for b in cfg.bot_bindings if b.bot_id != bot_id)
    return replace(cfg, bots=new_bots, bot_bindings=new_bindings)


def with_route_removed(cfg: ConfigV2, route_id: str) -> ConfigV2:
    """Pure transform: return a new ``ConfigV2`` with ``Route`` removed.

    Step 16: backs the Routes Matrix cell-uncheck. Removes any
    ``BotBinding`` whose ``scope='route'`` references this route_id too
    (forward-compat for Step 14 — bindings can't survive a removed route).

    Raises ValueError when the route id isn't in the config.
    """
    if cfg.route(route_id) is None:
        raise ValueError(f"Unknown route: {route_id}")
    new_routes = tuple(r for r in cfg.routes if r.id != route_id)
    new_bindings = tuple(
        b for b in cfg.bot_bindings
        if not (b.scope == "route" and b.route_id == route_id)
    )
    return replace(cfg, routes=new_routes, bot_bindings=new_bindings)


def with_route_rules(
    cfg: ConfigV2,
    route_id: str,
    *,
    max_lots: float | None = None,
    min_account_balance: float | None = None,
    skip_if_drawdown_pct: float | None = None,
    allowed_action_types: tuple[str, ...] | None = None,
    time_of_day_filter: str | None = None,
) -> ConfigV2:
    """Pure transform: update Step-20 per-route rules.

    Pass ``None`` for any field to leave it unchanged; pass the field's
    "disabled" value (0 / empty tuple / empty string) to clear a rule.

    Validates:
      - max_lots / min_account_balance / skip_if_drawdown_pct >= 0
      - skip_if_drawdown_pct <= 100
      - allowed_action_types entries are non-empty strings
      - time_of_day_filter parses (delegates to ``_parse_time_window``)

    Raises ValueError on unknown route_id or invalid values.
    """
    target = cfg.route(route_id)
    if target is None:
        raise ValueError(f"Unknown route: {route_id}")
    if max_lots is not None and max_lots < 0:
        raise ValueError(f"max_lots must be >= 0; got {max_lots}")
    if min_account_balance is not None and min_account_balance < 0:
        raise ValueError(
            f"min_account_balance must be >= 0; got {min_account_balance}"
        )
    if skip_if_drawdown_pct is not None:
        if not (0.0 <= skip_if_drawdown_pct <= 100.0):
            raise ValueError(
                f"skip_if_drawdown_pct must be 0..100; got {skip_if_drawdown_pct}"
            )
    if allowed_action_types is not None:
        bad = [t for t in allowed_action_types if not (isinstance(t, str) and t)]
        if bad:
            raise ValueError(
                f"allowed_action_types entries must be non-empty strings; got {bad}"
            )
    if time_of_day_filter:
        # Validate by parsing; raises on bad shape.
        from src.route_rules import _parse_time_window
        _parse_time_window(time_of_day_filter)

    updates: dict = {}
    if max_lots is not None:
        updates["max_lots"] = max_lots
    if min_account_balance is not None:
        updates["min_account_balance"] = min_account_balance
    if skip_if_drawdown_pct is not None:
        updates["skip_if_drawdown_pct"] = skip_if_drawdown_pct
    if allowed_action_types is not None:
        updates["allowed_action_types"] = tuple(allowed_action_types)
    if time_of_day_filter is not None:
        updates["time_of_day_filter"] = time_of_day_filter
    new_routes = tuple(
        replace(r, **updates) if r.id == route_id else r
        for r in cfg.routes
    )
    return replace(cfg, routes=new_routes)


def with_route_failover(
    cfg: ConfigV2, route_id: str, fallback_destination_id: str,
) -> ConfigV2:
    """Pure transform: set the route's ``fallback_destination_id``.

    Step 21: pass an empty string to disable failover. Validates:
      - route exists
      - fallback destination exists (when non-empty)
      - fallback isn't the route's own destination (a circular fallback
        would just retry the same DB and gain nothing)

    Raises ValueError on any of the above.
    """
    target = cfg.route(route_id)
    if target is None:
        raise ValueError(f"Unknown route: {route_id}")
    if fallback_destination_id:
        if cfg.destination(fallback_destination_id) is None:
            raise ValueError(
                f"Unknown fallback destination: {fallback_destination_id}"
            )
        if fallback_destination_id == target.destination_id:
            raise ValueError(
                f"Route {route_id} fallback cannot equal its own destination "
                f"({target.destination_id}) — circular fallback adds no resilience."
            )
    new_routes = tuple(
        replace(r, fallback_destination_id=fallback_destination_id)
        if r.id == route_id else r
        for r in cfg.routes
    )
    return replace(cfg, routes=new_routes)


def with_route_sizing(
    cfg: ConfigV2, route_id: str, sizing_multiplier: float,
) -> ConfigV2:
    """Pure transform: update ``Route.sizing_multiplier`` in place.

    Step 16: backs inline sizing-multiplier edits in the matrix. The
    multiplier is applied per-leg at execution time, so changes take
    effect on the NEXT action POSTed by the listener (mtime-cached read
    on the API side).

    Raises:
      - ValueError if route_id is unknown
      - ValueError if sizing_multiplier is negative
    """
    if cfg.route(route_id) is None:
        raise ValueError(f"Unknown route: {route_id}")
    if sizing_multiplier < 0:
        raise ValueError(
            f"sizing_multiplier must be >= 0; got {sizing_multiplier}"
        )
    new_routes = tuple(
        replace(r, sizing_multiplier=sizing_multiplier)
        if r.id == route_id else r
        for r in cfg.routes
    )
    return replace(cfg, routes=new_routes)


def with_binding_added(
    cfg: ConfigV2, *,
    bot_id: str,
    scope: BindingScope,
    destination_id: str | None = None,
    channel_id: str | None = None,
    route_id: str | None = None,
    binding_id: str | None = None,
) -> ConfigV2:
    """Pure transform: append a new ``BotBinding`` to ``cfg``.

    Step 17: backs the Bot Bindings GUI's "Add binding" dialog. Validates:
      - bot_id exists
      - destination_id (when given) exists
      - channel_id (when given) exists
      - route_id (when given) exists
      - scope's required target field is set (delegated to ``BotBinding``)
      - no exact duplicate binding (same bot + scope + target combo)

    Deterministic id when ``binding_id`` is None:
      ``bind_<bot_id>__<scope>[_<target_id>]`` — toggle-off+on yields
      the same id (no churn in ``stacks_config.json``).
    """
    if cfg.bot(bot_id) is None:
        raise ValueError(f"Unknown bot: {bot_id}")
    if destination_id and cfg.destination(destination_id) is None:
        raise ValueError(f"Unknown destination: {destination_id}")
    if channel_id and cfg.channel(channel_id) is None:
        raise ValueError(f"Unknown channel: {channel_id}")
    if route_id and cfg.route(route_id) is None:
        raise ValueError(f"Unknown route: {route_id}")

    # Construct the new binding (BotBinding.__post_init__ enforces
    # scope-target consistency).
    target_id = destination_id or channel_id or route_id or ""
    bid = (binding_id
           or f"bind_{bot_id}__{scope}"
              + (f"_{target_id}" if target_id else "")).strip()
    if cfg.bot_bindings and any(b.id == bid for b in cfg.bot_bindings):
        raise ValueError(f"Binding id collision: {bid}")
    new_binding = BotBinding(
        id=bid, bot_id=bot_id, scope=scope,
        destination_id=destination_id,
        channel_id=channel_id,
        route_id=route_id,
    )
    # Reject exact-duplicate (same bot + scope + target). Two bindings
    # with the same effective targeting would just produce duplicate
    # DMs — almost always a misconfiguration, not intent.
    for b in cfg.bot_bindings:
        if (b.bot_id == bot_id and b.scope == scope
                and b.destination_id == destination_id
                and b.channel_id == channel_id
                and b.route_id == route_id):
            raise ValueError(
                f"Duplicate binding: bot={bot_id} scope={scope} "
                f"target={target_id or '(none)'}"
            )
    return replace(cfg, bot_bindings=cfg.bot_bindings + (new_binding,))


def with_binding_removed(cfg: ConfigV2, binding_id: str) -> ConfigV2:
    """Pure transform: remove the ``BotBinding`` with ``binding_id``.

    Raises ValueError when the id isn't present (caller is wrong about
    state, not a partial mutation).
    """
    if not any(b.id == binding_id for b in cfg.bot_bindings):
        raise ValueError(f"Unknown binding: {binding_id}")
    return replace(
        cfg,
        bot_bindings=tuple(b for b in cfg.bot_bindings if b.id != binding_id),
    )


def detect_binding_overlaps(cfg: ConfigV2) -> tuple[tuple[str, str, str], ...]:
    """Return tuples ``(destination_id, bot_id_a, bot_id_b)`` for each pair
    of distinct bots that would BOTH receive DMs on the same destination.

    Step 17: powers the GUI's overlap warning. Two distinct bots
    targeting the same destination almost always means "operator
    forgot to remove the old binding" or "operator typoed two bot ids
    for what should be one bot" — surface it so they can decide.

    Pairs are returned in deterministic order: (destination_id ascending,
    bot_id_a < bot_id_b). Bots with multiple bindings to the same
    destination via different scopes are NOT flagged (one bot getting
    multiple rows for one event is the dispatcher's expected behavior;
    the bot collapses them at delivery time).
    """
    out: list[tuple[str, str, str]] = []
    for dest in cfg.destinations:
        # Collect distinct bot_ids whose bindings cover this destination.
        bot_ids = sorted({
            b.bot_id for b in cfg.bindings_for_destination(dest.id)
        })
        for i, a in enumerate(bot_ids):
            for b in bot_ids[i + 1:]:
                out.append((dest.id, a, b))
    return tuple(out)


def load_v2(path: Path | None = None) -> ConfigV2 | None:
    """Load a v2 config from disk. Returns None if file missing or not v2."""
    p = path or config_path()
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("version") != CONFIG_VERSION:
        return None
    return _config_from_dict(data)


def save_v2(config: ConfigV2, path: Path | None = None) -> None:
    """Write a v2 config to disk. Creates parent directories.

    Also pushes entity values into per-destination DB settings (best-
    effort, no-op when DB files don't exist yet). This makes V2 Config
    self-sufficient — operator no longer needs to also paste tg_phone /
    tg_api_id / tg_api_hash / tg_watched_chat_id / ai_provider into the
    Tuning tab. Operator-supplied DB settings are never overwritten.
    """
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_serialize(config) + "\n", encoding="utf-8")
    try:
        from src.v2_db_sync import sync_v2_to_destination_dbs
        sync_v2_to_destination_dbs(config)
    except Exception:
        # Sync is best-effort — never block a successful config write.
        import logging
        logging.getLogger("config_v2").exception("v2 db sync failed")


def _serialize(config: ConfigV2) -> str:
    payload = {
        "version": CONFIG_VERSION,
        "accounts":     [asdict(a) for a in config.accounts],
        "profiles":     [asdict(p) for p in config.profiles],
        "channels":     [asdict(c) for c in config.channels],
        "destinations": [asdict(d) for d in config.destinations],
        "bots":         [asdict(b) for b in config.bots],
        "routes":       [asdict(r) for r in config.routes],
        "bot_bindings": [_binding_to_dict(b) for b in config.bot_bindings],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _binding_to_dict(binding: BotBinding) -> dict:
    """Strip None scope-target fields so the file stays minimal."""
    out = asdict(binding)
    for key in ("destination_id", "channel_id", "route_id"):
        if out.get(key) is None:
            out.pop(key, None)
    return out


def _route_from_dict(row: dict) -> "Route":
    """Coerce JSON-side list back to tuple for Route.allowed_action_types.

    Step 20: ``allowed_action_types`` is typed ``tuple[str, ...]`` for
    immutability but JSON serializes tuples as lists. Without this
    coercion, a saved+loaded Route would have a list there, breaking
    hashability and the immutability invariant the rest of the v2 code
    relies on.
    """
    row = dict(row)  # defensive copy
    allowed = row.get("allowed_action_types")
    if isinstance(allowed, list):
        row["allowed_action_types"] = tuple(str(x) for x in allowed)
    return Route(**row)


def _account_from_dict(row: dict) -> Account:
    """Build Account from a dict, tolerating missing optional fields and
    unknown keys (forward-compat with newer config writes)."""
    from dataclasses import fields as _fields
    allowed = {f.name for f in _fields(Account)}
    clean = {k: v for k, v in row.items() if k in allowed}
    return Account(**clean)


def _config_from_dict(data: dict) -> ConfigV2:
    return ConfigV2(
        accounts=tuple(_account_from_dict(a) for a in data.get("accounts", [])),
        profiles=tuple(Profile(**p) for p in data.get("profiles", [])),
        channels=tuple(Channel(**c) for c in data.get("channels", [])),
        destinations=tuple(Destination(**d) for d in data.get("destinations", [])),
        bots=tuple(Bot(**b) for b in data.get("bots", [])),
        routes=tuple(_route_from_dict(r) for r in data.get("routes", [])),
        bot_bindings=tuple(BotBinding(**b) for b in data.get("bot_bindings", [])),
    )
