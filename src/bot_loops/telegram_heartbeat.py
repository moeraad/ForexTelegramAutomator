"""Telegram heartbeat loop moved out of ``src/bot.py``."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

from telegram.ext import Application


log = logging.getLogger("bot")


async def telegram_heartbeat_loop(app: Application):
    """Probes Telegram every 30s via bot.get_me() and writes
    settings.bot_telegram_ok_at on success. The GUI's service-bar reads
    this timestamp to colour the Bot pill — green = recent success,
    amber = stale, red = failing or missing.

    Failure modes (all surface to the pill, none crash the bot):
      - DNS broken -> NetworkError -> heartbeat not updated -> pill goes amber/red
      - Telegram backend slow -> same
      - Bot token revoked -> Unauthorized -> heartbeat not updated -> red
    """
    from src.db import set_setting
    conn: sqlite3.Connection = app.bot_data["conn"]
    while True:
        try:
            await app.bot.get_me()
            set_setting(
                conn, "bot_telegram_ok_at",
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:  # noqa: BLE001
            # Don't update the timestamp — staleness IS the signal.
            log.debug("telegram_heartbeat: %s: %s", type(e).__name__, e)
        await asyncio.sleep(30.0)
