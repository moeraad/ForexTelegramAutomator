"""Interactive switcher for the listener account, watched channel, and bot.

Run from project root:  python scripts/switch_account.py

Walks you through swapping any subset of:
  - The Telethon user account (TG_API_ID / TG_API_HASH / TG_PHONE / TG_SESSION_NAME)
  - The watched channel (TG_WATCHED_CHAT_ID) — connects with the new
    credentials and lists every chat so you can pick by ID.
  - The control bot (TG_BOT_TOKEN)
  - The owner that receives DMs (TG_BOT_OWNER_USER_ID)

Safety:
  - Reads .env, asks before overwriting any key.
  - Snapshots the old .env to .env.bak.<UTC-timestamp> first.
  - Preserves comments, blank lines, and unrelated keys verbatim.
  - Never echoes secrets back to the screen after collection.
  - Stop launch.bat services first, otherwise the .session file is locked.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values
from telethon import TelegramClient

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
ENV_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / ".env.example"

# Keys this script knows how to swap. Order matters for prompting.
LISTENER_KEYS: tuple[str, ...] = (
    "TG_API_ID",
    "TG_API_HASH",
    "TG_PHONE",
    "TG_SESSION_NAME",
)
CHANNEL_KEY = "TG_WATCHED_CHAT_ID"
BOT_KEYS: tuple[str, ...] = (
    "TG_BOT_TOKEN",
    "TG_BOT_OWNER_USER_ID",
)
SECRET_KEYS: frozenset[str] = frozenset({"TG_API_HASH", "TG_BOT_TOKEN"})


def prompt(label: str, default: str = "", *, secret: bool = False) -> str:
    """Prompt with an optional default. Empty answer keeps the default."""
    if default:
        suffix = " [keep current ***]" if secret else f" [{default}]"
    else:
        suffix = ""
    while True:
        ans = input(f"  {label}{suffix}: ").strip()
        if ans:
            return ans
        if default:
            return default
        print("    (required - please enter a value)")


def prompt_yes_no(label: str, *, default_no: bool = True) -> bool:
    suffix = " [y/N]" if default_no else " [Y/n]"
    while True:
        ans = input(f"{label}{suffix} ").strip().lower()
        if not ans:
            return not default_no
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def load_existing_env() -> dict[str, str]:
    """Load current .env, falling back to .env.example for default keys."""
    if ENV_PATH.exists():
        return {k: (v or "") for k, v in dotenv_values(ENV_PATH).items()}
    if ENV_EXAMPLE_PATH.exists():
        print(f"No .env found - using {ENV_EXAMPLE_PATH.name} as a template.")
        return {k: (v or "") for k, v in dotenv_values(ENV_EXAMPLE_PATH).items()}
    return {}


def backup_env() -> Path | None:
    """Snapshot .env to .env.bak.<UTC-timestamp> for safety. Returns the path."""
    if not ENV_PATH.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = ENV_PATH.with_suffix(f".bak.{stamp}")
    backup.write_text(ENV_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return backup


def write_env(updates: dict[str, str]) -> None:
    """Rewrite .env applying `updates`.

    Preserves comments, blank lines, key order, and untouched keys verbatim.
    Adds new keys at the end of the file.
    """
    seen: set[str] = set()
    new_lines: list[str] = []

    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                new_lines.append(raw)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                new_lines.append(raw)

    appended = [k for k in updates if k not in seen]
    if appended:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append("# Added by scripts/switch_account.py")
        for k in appended:
            new_lines.append(f"{k}={updates[k]}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


async def list_dialogs_and_pick(
    api_id: str,
    api_hash: str,
    phone: str,
    session_name: str,
    current_chat_id: str,
) -> str:
    """Connect with the given creds, list chats, prompt for the channel ID.

    Triggers the Telethon login flow on first use (SMS code, optional 2FA).
    """
    print()
    print("Connecting to Telegram with the new account...")
    print("(If this is the first login, you'll be prompted for the SMS code.)")
    client = TelegramClient(session_name, int(api_id), api_hash)
    try:
        await client.start(phone=phone)
        print()
        print(f"{'ID':>18}  TYPE      NAME")
        print("-" * 60)
        async for d in client.iter_dialogs():
            kind = "channel" if d.is_channel else ("group" if d.is_group else "user")
            print(f"{d.id:>18}  {kind:<8}  {d.name}")
        print()
        return prompt(
            "Paste the channel ID for TG_WATCHED_CHAT_ID",
            default=current_chat_id,
        )
    finally:
        await client.disconnect()


async def main_async() -> int:
    print("=" * 60)
    print("  CopyTrades - switch Telegram account / channel / bot")
    print("=" * 60)
    print()
    print("Stop launch.bat first if it's running - otherwise the .session")
    print("file stays locked and Telethon can't log in.")
    print()
    if not prompt_yes_no("Services are stopped, ready to continue?", default_no=False):
        print("Aborted.")
        return 1

    current = load_existing_env()
    updates: dict[str, str] = {}

    # ---- Listener account ----
    print()
    swap_listener = prompt_yes_no(
        "Swap the listener (Telegram USER account)?", default_no=False
    )
    if swap_listener:
        print("\nGet new API credentials from https://my.telegram.org -> Apps")
        print("(Sign in there with the NEW account's phone number.)")
        for key in LISTENER_KEYS:
            updates[key] = prompt(
                key, current.get(key, ""), secret=key in SECRET_KEYS
            )

    # ---- Channel ID (requires the listener creds to be valid) ----
    print()
    swap_channel = prompt_yes_no(
        "Pick a new watched channel from the live dialog list?",
        default_no=False,
    )
    if swap_channel:
        api_id = updates.get("TG_API_ID", current.get("TG_API_ID", ""))
        api_hash = updates.get("TG_API_HASH", current.get("TG_API_HASH", ""))
        phone = updates.get("TG_PHONE", current.get("TG_PHONE", ""))
        session = updates.get(
            "TG_SESSION_NAME", current.get("TG_SESSION_NAME", "copytrades_session")
        )
        if not (api_id and api_hash and phone):
            print("Missing TG_API_ID / TG_API_HASH / TG_PHONE - cannot connect.")
            return 1
        try:
            chosen = await list_dialogs_and_pick(
                api_id, api_hash, phone, session, current.get(CHANNEL_KEY, "")
            )
            updates[CHANNEL_KEY] = chosen
        except Exception as exc:  # noqa: BLE001
            print(f"\nLogin / dialog fetch failed: {exc}")
            print("Fix the credentials and re-run.")
            return 1

    # ---- Bot ----
    print()
    swap_bot = prompt_yes_no("Swap the control bot or owner DM target?")
    if swap_bot:
        print(
            "\nNew bot token: DM @BotFather -> /newbot. "
            "Owner user ID: DM @userinfobot."
        )
        for key in BOT_KEYS:
            updates[key] = prompt(
                key, current.get(key, ""), secret=key in SECRET_KEYS
            )
        if updates.get("TG_BOT_OWNER_USER_ID") != current.get("TG_BOT_OWNER_USER_ID"):
            print()
            print("REMINDER: the new owner must /start the new bot in Telegram once,")
            print("otherwise the bot cannot deliver DMs to them.")

    if not updates:
        print("\nNothing to change. Done.")
        return 0

    # ---- Confirm + write ----
    print()
    print("About to update .env with the following keys:")
    for k in updates:
        shown = "***" if k in SECRET_KEYS else updates[k]
        print(f"  {k}={shown}")
    print()
    if not prompt_yes_no("Write these changes?", default_no=False):
        print("Aborted. .env unchanged.")
        return 1

    backup = backup_env()
    write_env(updates)
    print()
    print(f"OK - .env written. Backup: {backup.name if backup else '(no prior .env)'}")

    # ---- Optional cleanup of the old .session file ----
    old_session = current.get("TG_SESSION_NAME", "").strip()
    new_session = updates.get("TG_SESSION_NAME", old_session).strip()
    if swap_listener and old_session and old_session != new_session:
        old_path = Path(__file__).resolve().parent.parent / f"{old_session}.session"
        if old_path.exists():
            print()
            if prompt_yes_no(
                f"Delete the old session file '{old_path.name}'? "
                f"(It contains the previous account's login.)"
            ):
                try:
                    old_path.unlink()
                    journal = old_path.with_suffix(".session-journal")
                    if journal.exists():
                        journal.unlink()
                    print("Deleted.")
                except OSError as exc:
                    print(f"Could not delete: {exc}")

    print()
    print("Next steps:")
    print("  1. Restart services:  launch.bat")
    print("  2. Watch logs/listener.log for 'Listening on chat <new id>'.")
    print("  3. From the new owner account, /start the bot in Telegram.")
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(main_async()))
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(130)


if __name__ == "__main__":
    main()
