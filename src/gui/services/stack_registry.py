"""Discover available CopyTrades stacks (channel profile + project + ports)."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from src.config import API_HOST, API_PORT, BASE_DIR


# Port fallbacks when a stack's project .env does not specify API_PORT.
# These mirror the values used in services/install_services.bat descriptions
# (Arabic stack on 8765, SMC stack on 8766).
_PORT_FALLBACKS: dict[str, int] = {
    "fxengineer-gold": 8765,
    "SMC": 8766,
}


@dataclass(frozen=True)
class Stack:
    """Per-destination scope handle the GUI views bind to.

    Phase-1 cleanup of the v2 cleanup pass:

      - One Stack per Destination (was: one per Route, which produced
        duplicate Stacks named the same when a destination served N
        channels via mirror / aggregate).
      - ``service_names`` is now a variable-length tuple containing
        EVERY service touching this destination: 1 API + N bot
        services (from BotBindings) + N listener services (from
        Accounts whose channels route here). De-duplicated, order:
        ``[api, *bots, *listeners]``.
      - ``primary_api_service`` / ``primary_bot_service`` /
        ``primary_listener_service`` are derived for UI rows that need
        a single "headline" service per role (services_bar icons).
        Multi-bot / multi-account setups use the first scope=destination
        binding + first channel's account.
    """
    name: str
    profile_path: Path
    project_path: Path
    db_path: Path
    api_host: str
    api_port: int
    service_names: tuple[str, ...]
    primary_api_service: str = ""
    primary_bot_service: str = ""
    primary_listener_service: str = ""

    @property
    def api_url(self) -> str:
        return f"http://{self.api_host}:{self.api_port}"


def _name_upper(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", name).upper()


def _derive_service_names(name: str) -> tuple[str, str, str]:
    # Auto-derived. install_services.bat used CT-SMC-* and CT-AR-* as
    # operator-picked aliases; stacks_config.json overrides if needed.
    n = _name_upper(name)
    return (f"CT-{n}-Api", f"CT-{n}-Bot", f"CT-{n}-Listener")


def _parse_env_file(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _default_db_path(stack_name: str) -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return Path(appdata) / "CopyTrades" / stack_name / "copytrades.db"


def _default_profile_path(stack_name: str) -> Path:
    """Channel profile lives next to the stack's DB under APPDATA."""
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return Path(appdata) / "CopyTrades" / stack_name / "profile.json"


def _stacks_config_path() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / "CopyTrades" / "stacks_config.json"


def _read_api_settings_from_stack_db(db_path: Path) -> tuple[str | None, int | None]:
    if not db_path.exists():
        return None, None
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            rows = dict(conn.execute(
                "SELECT key, value FROM settings WHERE key IN ('api_host','api_port')"
            ).fetchall())
        finally:
            conn.close()
    except sqlite3.Error:
        return None, None
    host = rows.get("api_host") or None
    port_raw = rows.get("api_port") or None
    port = int(port_raw) if port_raw and port_raw.isdigit() else None
    return host, port


def _build_stack(
    name: str,
    profile_path: Path,
    project_path: Path,
    service_names: tuple[str, ...] | None,
    db_path_override: Path | None = None,
) -> Stack:
    db_path = db_path_override or _default_db_path(name)
    db_host, db_port = _read_api_settings_from_stack_db(db_path)
    env = _parse_env_file(project_path / ".env")
    host = db_host or env.get("API_HOST") or "127.0.0.1"
    if db_port:
        port = db_port
    else:
        port_raw = env.get("API_PORT")
        port = int(port_raw) if port_raw else _PORT_FALLBACKS.get(name, 8765)
    services = service_names or _derive_service_names(name)
    # Legacy v1 / discover_from_config path: services is the 3-tuple
    # (api, bot, listener). Map to primary_* for UI rows.
    primary_api = services[0] if len(services) > 0 else ""
    primary_bot = services[1] if len(services) > 1 else ""
    primary_listener = services[2] if len(services) > 2 else ""
    return Stack(
        name=name,
        profile_path=profile_path,
        project_path=project_path,
        db_path=db_path,
        api_host=host,
        api_port=port,
        service_names=tuple(services),
        primary_api_service=primary_api,
        primary_bot_service=primary_bot,
        primary_listener_service=primary_listener,
    )


def _discover_from_config(cfg_path: Path) -> list[Stack]:
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    stacks: list[Stack] = []
    for entry in data.get("stacks", []):
        name = entry["name"]
        profile_path = Path(entry["profile_path"])
        project_path = Path(entry.get("project_path", BASE_DIR))
        sn = entry.get("service_names")
        services: tuple[str, str, str] | None
        services = tuple(sn) if sn and len(sn) == 3 else None  # type: ignore[assignment]
        db_override = Path(entry["db_path"]) if entry.get("db_path") else None
        stacks.append(_build_stack(name, profile_path, project_path, services, db_override))
    return stacks


def _discover_from_channels_dir() -> list[Stack]:
    channels_dir = BASE_DIR / "channels"
    stacks: list[Stack] = []
    if not channels_dir.exists():
        return stacks
    for profile in sorted(channels_dir.glob("*.json")):
        if profile.stem.endswith("_draft"):
            continue
        stacks.append(_build_stack(profile.stem, profile, BASE_DIR, None))
    return stacks


def discover_stacks() -> list[Stack]:
    """Discover stacks for the GUI.

    Compat shim for the v2 multi-channel architecture (see
    ``docs/plans/2026-05-23-multi-channel-routing.md``, Step 1):

    1. If the config file is v1, auto-migrate to v2 in-place, backing up
       the v1 file as ``stacks_config.json.v1.bak``. One-shot, idempotent.
    2. Always read from v2 after that and derive synthetic Stack objects
       (one per Route) so existing GUI views continue to work unchanged
       until Step 9 reworks them around the new entity model.
    """
    cfg = _stacks_config_path()
    if not cfg.exists():
        return []
    from src import config_v2
    if not config_v2.is_v2(cfg):
        from src.migrations.config_v1_to_v2 import migrate, write_with_backup
        migrated = migrate(cfg)
        if migrated is not None:
            write_with_backup(migrated, cfg)
    config = config_v2.load_v2(cfg)
    if config is None:
        # Shouldn't happen: either we just migrated or it was already v2.
        # Fall back to the v1 reader so the GUI doesn't crash on a corrupt
        # config; a corrupt config is the operator's problem to fix.
        return _discover_from_config(cfg)
    return _stacks_from_v2(config)


def _stacks_from_v2(config) -> list[Stack]:
    """Derive Stack scope-handles from a v2 ConfigV2.

    Phase-1 cleanup: ONE Stack per Destination (was: per Route). When a
    destination serves N channels via mirror/aggregate, the resulting
    Stack lists ALL bots + listeners that touch it, but appears in the
    header picker only once. Destinations with zero enabled routes are
    skipped (no point showing them — no live traffic).
    """
    stacks: list[Stack] = []
    for dest in config.destinations:
        enabled_routes = [
            r for r in config.routes_for_destination(dest.id) if r.enabled
        ]
        if not enabled_routes:
            continue

        # Collect every channel routing here, every bot bound to it,
        # every account whose channels touch it.
        channels = [
            ch for r in enabled_routes
            if (ch := config.channel(r.channel_id)) is not None
        ]
        account_ids = {ch.account_id for ch in channels}
        accounts = [
            a for a in config.accounts if a.id in account_ids
        ]
        # ``bindings_for_destination`` already widens to channel/route
        # scopes that hit this dest — exactly what we want for "every
        # bot serving this destination."
        bound_bot_ids = {b.bot_id for b in config.bindings_for_destination(dest.id)}
        bots = [b for b in config.bots if b.id in bound_bot_ids]

        # Headline (primary) services for single-role UI rows. Each
        # entity's blank service_name falls back to a name DERIVED FROM
        # ITS OWN name — not the destination's — so a bot named "B"
        # with blank service_name becomes "CT-B-Bot" (not "CT-X-Bot").
        dest_derived = _derive_service_names(dest.name)
        primary_api = dest.service_name or dest_derived[0]

        def _bot_service(b) -> str:
            return b.service_name or _derive_service_names(b.name or dest.name)[1]

        def _acct_service(a) -> str:
            return a.service_name or _derive_service_names(a.name or dest.name)[2]

        # Primary bot: prefer scope=destination binding; fall back to first bot.
        primary_bot = next(
            (b for b in bots
             if any(bd.scope == "destination" and bd.bot_id == b.id
                    for bd in config.bot_bindings
                    if bd.destination_id == dest.id)),
            None,
        ) or (bots[0] if bots else None)
        primary_bot_service = (
            _bot_service(primary_bot) if primary_bot is not None
            else dest_derived[1]
        )
        primary_listener_service = (
            _acct_service(accounts[0]) if accounts else dest_derived[2]
        )

        # Full service list = api + every bot + every account, dedup +
        # blank-stripped (the uninstall helper filters blanks anyway,
        # cleaner to drop them here too).
        all_services: list[str] = [primary_api]
        for b in bots:
            sn = _bot_service(b)
            if sn and sn not in all_services:
                all_services.append(sn)
        for a in accounts:
            sn = _acct_service(a)
            if sn and sn not in all_services:
                all_services.append(sn)
        service_names: tuple[str, ...] = tuple(all_services)

        # Per-destination DB + API binding (unchanged).
        db_path = Path(dest.db_path) if dest.db_path else _default_db_path(dest.name)
        db_host, db_port = _read_api_settings_from_stack_db(db_path)
        host = db_host or dest.api_host or "127.0.0.1"
        port = db_port or dest.api_port or _PORT_FALLBACKS.get(dest.name, 8765)

        # Profile path: pick the first channel's profile so the per-stack
        # Profile view has SOMETHING to show on this destination. Multi-
        # profile destinations (aggregate routing) will eventually move
        # to the Profile view's own picker (Phase 3 of the v2 cleanup).
        primary_profile = (
            config.profile(channels[0].profile_id) if channels else None
        )
        profile_path = (
            Path(primary_profile.path) if primary_profile and primary_profile.path
            else _default_profile_path(dest.name)
        )
        project_path = BASE_DIR

        stacks.append(Stack(
            name=dest.name,
            profile_path=profile_path,
            project_path=project_path,
            db_path=db_path,
            api_host=host,
            api_port=port,
            service_names=service_names,
            primary_api_service=primary_api,
            primary_bot_service=primary_bot_service,
            primary_listener_service=primary_listener_service,
        ))
    return stacks


def available_profiles() -> list[str]:
    """Names of channel profile templates available for new stacks."""
    channels_dir = BASE_DIR / "channels"
    if not channels_dir.exists():
        return []
    out: list[str] = []
    for profile in sorted(channels_dir.glob("*.json")):
        if profile.stem.endswith("_draft"):
            continue
        out.append(profile.stem)
    return out


def build_stack_for_new_entry(name: str, profile_name: str) -> Stack:
    """Construct a Stack object for a freshly-added stacks_config entry.

    The profile JSON lives next to the stack's DB under APPDATA so all
    per-stack files share one folder. The ``profile_name`` arg is the
    profile identifier kept for parity with older callers; new stacks
    just use the stack name.
    """
    profile_path = _default_profile_path(name)
    return _build_stack(name, profile_path, BASE_DIR, None)
