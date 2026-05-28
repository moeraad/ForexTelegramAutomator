"""One-shot migration from v1 ``stacks_config.json`` to the v2 schema.

The v1 format was a flat list of "stacks", each conflating a Telegram channel,
a profile, an MT5 destination, and a bot into one entity. v2 decomposes this
into seven first-class entities (see ``src/config_v2.py``).

This migration:

  1. Reads the v1 file
  2. For each stack, reads its per-stack DB's ``settings`` table to recover
     Telegram credentials (phone, chat_id, bot token reference, session name)
  3. Deduplicates Accounts by phone — two v1 stacks sharing a phone become
     ONE Account with TWO Channels in v2 (which is exactly what the user
     wanted for the "1 account, N channels" use case)
  4. Emits 1 Profile + 1 Channel + 1 Destination + 1 Bot + 1 Route +
     1 BotBinding per v1 stack, with all cross-references wired
  5. Returns a ``ConfigV2`` object — the caller decides when to write to disk
     and back up the v1 file

The function is **idempotent**: if the file at ``config_path`` is already v2,
or doesn't exist, returns ``None``.
"""
from __future__ import annotations

import json
import re
import sqlite3
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
    is_v2,
)


_TG_SETTINGS_KEYS = (
    "tg_api_id",
    "tg_api_hash",
    "tg_phone",
    "tg_session_name",
    "tg_watched_chat_id",
    "tg_bot_token",
)


def migrate(config_path: Path) -> ConfigV2 | None:
    """Read v1 config at ``config_path``, return derived ConfigV2.

    Returns ``None`` if the file is absent, unreadable, or already v2.
    Does NOT write to disk — the caller is responsible for persistence
    and backups.
    """
    if not config_path.exists():
        return None
    if is_v2(config_path):
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    v1_stacks = data.get("stacks", [])
    if not v1_stacks:
        # An empty v1 file becomes an empty v2 config — still a valid result.
        return ConfigV2()

    accounts: list[Account] = []
    profiles: list[Profile] = []
    channels: list[Channel] = []
    destinations: list[Destination] = []
    bots: list[Bot] = []
    routes: list[Route] = []
    bindings: list[BotBinding] = []

    # Phone-keyed Account dedup. Two stacks with the same phone share one
    # Account (one Telethon session) but contribute two Channels.
    account_by_phone: dict[str, Account] = {}
    fallback_account_index = 0

    for entry in v1_stacks:
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        slug = _slugify(name)
        project_path = entry.get("project_path", "")
        profile_path = entry.get("profile_path", "")
        db_path = entry.get("db_path") or ""
        service_names = entry.get("service_names") or []
        # service_names is the v1 (api, bot, listener) tuple. After migration,
        # listener moves to the per-Account service (CT-Listener-<acc>), so we
        # drop service_names[2] and reuse [0] for Destination, [1] for Bot.
        api_service  = service_names[0] if len(service_names) > 0 else f"CT-{slug.upper()}-Api"
        bot_service  = service_names[1] if len(service_names) > 1 else f"CT-{slug.upper()}-Bot"

        # Read TG creds from the stack's DB. Missing/unreadable DB is OK —
        # placeholder values let the operator finish configuration via GUI.
        tg = _read_tg_settings(Path(db_path)) if db_path else {}
        phone = tg.get("tg_phone", "").strip()
        session_name = tg.get("tg_session_name", "").strip()
        chat_id_raw = tg.get("tg_watched_chat_id", "").strip()
        chat_id = int(chat_id_raw) if chat_id_raw.lstrip("-").isdigit() else 0

        # Account: dedup by phone. If no phone, give this stack its own
        # placeholder account so the migration doesn't lose data.
        if phone:
            account = account_by_phone.get(phone)
            if account is None:
                account = _build_account(
                    account_id=f"acc_{_phone_slug(phone)}",
                    name=f"Account {phone}",
                    phone=phone,
                    session_name=session_name,
                    project_path=project_path,
                )
                account_by_phone[phone] = account
                accounts.append(account)
        else:
            fallback_account_index += 1
            account = _build_account(
                account_id=f"acc_unconfigured_{fallback_account_index}",
                name=f"Unconfigured account ({name})",
                phone="",
                session_name=session_name,
                project_path=project_path,
            )
            accounts.append(account)

        profile = Profile(
            id=f"prof_{slug}",
            name=name,
            path=str(profile_path),
        )
        profiles.append(profile)

        channel = Channel(
            id=f"ch_{slug}",
            name=name,
            account_id=account.id,
            chat_id=chat_id,
            profile_id=profile.id,
            enabled=True,
        )
        channels.append(channel)

        destination = Destination(
            id=f"dest_{slug}",
            name=name,
            db_path=str(db_path),
            api_host="127.0.0.1",
            api_port=0,  # actual port lives in the per-stack DB; resolved at runtime
            service_name=api_service,
        )
        destinations.append(destination)

        bot = Bot(
            id=f"bot_{slug}",
            name=f"{name} Bot",
            # Token setting key inside the destination's settings table
            # matches the v1 key name so existing rows are reused as-is.
            token_setting_key="tg_bot_token",
            service_name=bot_service,
        )
        bots.append(bot)

        route = Route(
            id=f"route_{slug}",
            channel_id=channel.id,
            destination_id=destination.id,
            enabled=True,
            sizing_multiplier=1.0,
        )
        routes.append(route)

        binding = BotBinding(
            id=f"bind_{slug}",
            bot_id=bot.id,
            scope="destination",
            destination_id=destination.id,
        )
        bindings.append(binding)

    return ConfigV2(
        accounts=tuple(accounts),
        profiles=tuple(profiles),
        channels=tuple(channels),
        destinations=tuple(destinations),
        bots=tuple(bots),
        routes=tuple(routes),
        bot_bindings=tuple(bindings),
    )


def write_with_backup(config: ConfigV2, config_path: Path) -> Path:
    """Persist v2 config to ``config_path``, moving any v1 file to ``.v1.bak``.

    Returns the backup path (or the original path if no backup was needed).
    """
    from src.config_v2 import save_v2

    backup_path = config_path.with_suffix(config_path.suffix + ".v1.bak")
    if config_path.exists() and not is_v2(config_path):
        # Overwrite an older .v1.bak rather than fail — re-running the
        # migration is a supported operation.
        if backup_path.exists():
            backup_path.unlink()
        config_path.rename(backup_path)
    save_v2(config, config_path)
    return backup_path if backup_path.exists() else config_path


def _build_account(
    *,
    account_id: str,
    name: str,
    phone: str,
    session_name: str,
    project_path: str,
) -> Account:
    # Telethon resolves session names relative to cwd; record an absolute
    # path derived from project_path + session_name so the new shared
    # listener (Step 5) can find the existing session file without
    # depending on cwd.
    if session_name and project_path:
        session_path = str(Path(project_path) / f"{session_name}.session")
    else:
        session_path = ""
    return Account(
        id=account_id,
        name=name,
        phone=phone,
        session_path=session_path,
        service_name=f"CT-Listener-{account_id}",
    )


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """'Forex Engineer' -> 'forex_engineer'."""
    out = _SLUG_RE.sub("_", name.lower()).strip("_")
    return out or "unnamed"


def _phone_slug(phone: str) -> str:
    """'+9611234567' -> '9611234567' (digits only, leading + stripped)."""
    digits = re.sub(r"\D", "", phone)
    return digits or "unknown"


def _read_tg_settings(db_path: Path) -> dict[str, str]:
    """Read tg_* keys from a stack's settings table. Missing DB returns {}."""
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            placeholders = ",".join("?" * len(_TG_SETTINGS_KEYS))
            rows = conn.execute(
                f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
                _TG_SETTINGS_KEYS,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {k: (v or "") for k, v in rows}
