"""Telegram inline-keyboard factories shared across bot subsystems.

Pulled out of ``src/bot.py`` so both the legacy ``notification_dispatcher``
+ ``recover_orphan_pending_actions`` (in ``src/bot_loops/``) and the
Step 7 ``OutboxTailer`` (in ``src/bot_outbox_tailer.py``) can import
without circling back through ``src/bot.py``.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def _kb_for_action(action_id: int, action_type: str) -> InlineKeyboardMarkup | None:
    if action_type == "ALERT":
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Cancel", callback_data=f"cancel:{action_id}"),
        InlineKeyboardButton("Execute now", callback_data=f"execute:{action_id}"),
    ]])


def _kb_for_relaunch(action_id: int) -> InlineKeyboardMarkup:
    """Keyboard offered for actions found in non-terminal state when the
    bot starts up — i.e. the previous session crashed before promoting /
    executing them. Operator decides whether to fire the action now or
    skip it (REVIEW.md P1 / Q2).
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Execute", callback_data=f"relexec:{action_id}"),
        InlineKeyboardButton("Ignore", callback_data=f"relskip:{action_id}"),
    ]])
