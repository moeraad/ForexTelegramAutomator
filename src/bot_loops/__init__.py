"""Background loops the Bot process spawns at startup.

Pulled out of the monolithic ``src/bot.py`` into one file per concern
so the bot module stays focused on command handlers + `post_init`
wiring. Each loop is an ``async def`` taking the python-telegram-bot
``Application`` and runs forever (with try/except per tick).

Wiring lives in ``src.bot.post_init`` which imports from here and
calls ``_supervise(asyncio.create_task(...))`` on each.
"""
from src.bot_loops.notification import (
    notification_dispatcher,
    position_close_notifier,
)
from src.bot_loops.promoter_loops import (
    claim_sweeper_loop,
    promotion_loop,
)
from src.bot_loops.cost_guard_loop import cost_guard_loop
from src.bot_loops.telegram_heartbeat import telegram_heartbeat_loop
from src.bot_loops.feeds import (
    calendar_feed_loop,
    cot_feed_loop,
    etf_flows_feed_loop,
    macro_feed_loop,
    news_scan_feed_loop,
)
from src.bot_loops.orphan_recovery import recover_orphan_pending_actions

__all__ = [
    "notification_dispatcher",
    "position_close_notifier",
    "promotion_loop",
    "claim_sweeper_loop",
    "cost_guard_loop",
    "telegram_heartbeat_loop",
    "macro_feed_loop",
    "cot_feed_loop",
    "etf_flows_feed_loop",
    "calendar_feed_loop",
    "news_scan_feed_loop",
    "recover_orphan_pending_actions",
]
