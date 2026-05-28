"""Compose v2 entities from the legacy wizard's collected fields.

The TelegramWizard pre-v2 cleanup wrote a v1 StackEntry which then
got auto-migrated to v2 entities on next launch. This helper skips the
round-trip — the wizard now creates v2 entities directly.

Two entry points:

  - ``compose_wizard_entities`` runs at Stack-identity-page validate
    time. It creates Account/Profile/Destination/Bot entities (no
    Channel yet — the Telethon dialog list comes later).
  - ``finalize_wizard_channel`` runs on wizard Finish. It adds the
    Channel + Route + BotBinding entities once the operator has
    picked the channel from Telethon.

The wizard's existing ``_persist`` keeps writing per-stack DB settings
(tg_api_id, tg_api_hash, tg_session_blob, etc.) — those belong on the
destination DB and the listener already reads them from there.
"""
from __future__ import annotations

import re
from pathlib import Path

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


def _slug(name: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s or fallback


def compose_wizard_entities(
    existing: ConfigV2 | None,
    *,
    stack_name: str,
    symbol: str,
    db_path: Path,
    api_host: str,
    api_port: int,
    profile_path: Path,
    service_names: tuple[str, ...],
) -> ConfigV2:
    """Add Account/Profile/Destination/Bot entities for a new wizard run.

    The Stack-identity page calls this. Account + Bot are placeholders
    until the wizard's later pages fill in api_id/hash/session + bot
    token; both have stable ids derived from the stack_name so the
    Finish-page _persist can find and update them.

    Idempotent: caller already checked for destination-name collision,
    but this still does duplicate-id guards so partial wizard runs
    don't blow up on retry.
    """
    base = existing or ConfigV2()
    slug = _slug(stack_name, "stack")
    acc_id = f"acc_{slug}"
    prof_id = f"prof_{slug}"
    dest_id = f"dest_{slug}"
    bot_id = f"bot_{slug}"

    # Service names from the synthetic Stack triple (api, bot, listener).
    api_svc = service_names[0] if len(service_names) > 0 else f"CT-{slug.upper()}-Api"
    bot_svc = service_names[1] if len(service_names) > 1 else f"CT-{slug.upper()}-Bot"
    listener_svc = (
        service_names[2] if len(service_names) > 2
        else f"CT-Listener-{acc_id}"
    )

    accounts = tuple(base.accounts)
    if base.account(acc_id) is None:
        accounts = accounts + (Account(
            id=acc_id, name=stack_name, phone="",
            session_path="", service_name=listener_svc,
        ),)

    profiles = tuple(base.profiles)
    if base.profile(prof_id) is None:
        profiles = profiles + (Profile(
            id=prof_id, name=stack_name,
            path=str(profile_path),
            language="", symbol=symbol or "XAUUSD",
        ),)

    destinations = tuple(base.destinations)
    if base.destination(dest_id) is None:
        destinations = destinations + (Destination(
            id=dest_id, name=stack_name, db_path=str(db_path),
            api_host=api_host, api_port=int(api_port),
            service_name=api_svc, mt5_label="",
        ),)

    bots = tuple(base.bots)
    if base.bot(bot_id) is None:
        bots = bots + (Bot(
            id=bot_id, name=stack_name,
            # The wizard's _persist writes the token to the dest DB
            # under "tg_bot_token" (legacy key). The OutboxTailer's
            # token resolver follows this key for back-compat with
            # wizard-installed stacks.
            token_setting_key="tg_bot_token",
            service_name=bot_svc,
        ),)

    return ConfigV2(
        accounts=accounts, profiles=profiles, channels=base.channels,
        destinations=destinations, bots=bots, routes=base.routes,
        bot_bindings=base.bot_bindings,
    )


def finalize_wizard_channel(
    existing: ConfigV2,
    *,
    stack_name: str,
    chat_id: int,
    account_phone: str = "",
) -> ConfigV2:
    """On Finish, add Channel + Route + BotBinding wiring the entities
    created by compose_wizard_entities.

    Also backfills ``Account.phone`` once Telethon login captured it
    (compose_wizard_entities couldn't know it ahead of the SMS-code
    pages).
    """
    slug = _slug(stack_name, "stack")
    acc_id = f"acc_{slug}"
    prof_id = f"prof_{slug}"
    dest_id = f"dest_{slug}"
    bot_id = f"bot_{slug}"
    ch_id = f"ch_{slug}"
    route_id = f"route_{slug}"
    bind_id = f"bind_{slug}"

    # Backfill account phone if compose ran before Telethon login captured it.
    from dataclasses import replace as _replace
    new_accounts = tuple(
        _replace(a, phone=account_phone)
        if a.id == acc_id and account_phone and not a.phone
        else a
        for a in existing.accounts
    )

    channels = tuple(existing.channels)
    if existing.channel(ch_id) is None and chat_id:
        channels = channels + (Channel(
            id=ch_id, name=stack_name, account_id=acc_id,
            chat_id=int(chat_id), profile_id=prof_id, enabled=True,
        ),)

    routes = tuple(existing.routes)
    if existing.route(route_id) is None and chat_id:
        routes = routes + (Route(
            id=route_id, channel_id=ch_id, destination_id=dest_id,
        ),)

    bindings = tuple(existing.bot_bindings)
    if all(b.id != bind_id for b in bindings):
        bindings = bindings + (BotBinding(
            id=bind_id, bot_id=bot_id, scope="destination",
            destination_id=dest_id,
        ),)

    return ConfigV2(
        accounts=new_accounts, profiles=existing.profiles,
        channels=channels, destinations=existing.destinations,
        bots=existing.bots, routes=routes, bot_bindings=bindings,
    )
