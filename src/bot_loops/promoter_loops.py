"""Promoter + claim-sweeper loops moved out of ``src/bot.py``."""
from __future__ import annotations

import asyncio
import logging
import sqlite3

from telegram.ext import Application

from src.promoter import promote_due_actions, release_stale_claims


log = logging.getLogger("bot")


async def promotion_loop(app: Application):
    conn: sqlite3.Connection = app.bot_data["conn"]
    while True:
        try:
            n = promote_due_actions(conn)
            if n:
                log.info("promoted %s actions", n)
        except Exception as e:
            log.exception("promotion_loop error: %s", e)
        await asyncio.sleep(1.0)


async def claim_sweeper_loop(app: Application):
    """Periodically release claimed actions a crashed EA never confirmed."""
    conn: sqlite3.Connection = app.bot_data["conn"]
    while True:
        try:
            n = release_stale_claims(conn)  # default 300s
            if n:
                log.warning("released %s stale claim(s)", n)
        except Exception as e:
            log.exception("claim_sweeper_loop error: %s", e)
        await asyncio.sleep(15.0)
