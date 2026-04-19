"""List all your Telegram dialogs so you can pick TG_WATCHED_CHAT_ID.

Run once from project root:  python scripts/find_channel_id.py

Prompts for your Telegram login code on first run, caches session.
"""
import asyncio
import os
import sys
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()


async def main() -> None:
    api_id = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    phone = os.getenv("TG_PHONE", "").strip()
    session = os.getenv("TG_SESSION_NAME", "copytrades_session")

    missing = [k for k, v in {"TG_API_ID": api_id, "TG_API_HASH": api_hash, "TG_PHONE": phone}.items() if not v]
    if missing:
        print(f"Missing in .env: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    client = TelegramClient(session, int(api_id), api_hash)
    await client.start(phone=phone)
    print(f"{'ID':>18}  TYPE      NAME")
    print("-" * 60)
    async for d in client.iter_dialogs():
        kind = "channel" if d.is_channel else ("group" if d.is_group else "user")
        print(f"{d.id:>18}  {kind:<8}  {d.name}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
