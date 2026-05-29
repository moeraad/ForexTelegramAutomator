"""Pydantic request/response models for ``src/api.py``.

Pulled out of api.py during the Day-1 cleanup pass so api.py focuses
on endpoint wiring + middleware + the startup orchestration in run().

Every model here is part of the wire contract — the EA, the listener,
the GUI all serialize/deserialize against these shapes. Don't change
field names without considering callers; new fields should be optional
with a default so older clients keep working.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LegResult(BaseModel):
    mt5_ticket: int
    snapshot: dict


class ResultBody(BaseModel):
    # Pydantic Literal rejects any other status string at parse time
    # (returns 422). Without this, the schema's CHECK constraint was the
    # last line of defense against an EA bug or attacker pushing the
    # action into an unconstrained state.
    #
    # 'watching' is for the Phase-5 pending-limit flow: EA places a
    # broker-side BuyLimit/SellLimit on an OPEN action with pending=true,
    # then POSTs status='watching'. The action stays in that state until
    # either the limit fires (EA POSTs 'executed' with a position
    # snapshot) or a CANCEL_PENDING flips it to 'rejected' server-side.
    status: Literal["executed", "failed", "rejected", "watching"]
    mt5_ticket: int | None = None
    error: str | None = None
    snapshot: dict | None = None
    legs: list[LegResult] | None = None


class CloseBody(BaseModel):
    reason: str = ""
    # Realized-P&L extension (Step 3 of AI_EVALUATOR_ROADMAP). Both
    # optional — older EA builds posting just `reason` keep working.
    # exit_price = price of the closing deal; realized_pnl = total
    # broker-reported profit on the position (sum across all exit deals).
    exit_price: float | None = None
    realized_pnl: float | None = None


class IncomingMessageBody(BaseModel):
    """Listener-posted body for POST /incoming_message (Step 4 of multi-channel plan).

    The listener no longer writes to the messages table directly. It POSTs
    here; the API process inserts the message, runs the orchestrator in a
    background task (so the HTTP response is fast), and returns 202.

    Fields:
        channel_id: v2 Channel.id from stacks_config.json. Used to resolve
            the ProfileContext for prompt rendering (Step 12 deferred). In
            single-stack mode the API ignores this and uses its process-level
            ProfileContext — but the field is required so the wire contract
            is forward-compatible.
        tg_chat_id: numeric Telegram chat id (negative for channels). Stored
            in the messages table verbatim — unchanged from v1.
        tg_message_id: Telegram message id within the chat. Combined with
            tg_chat_id in the UNIQUE(chat_id, tg_message_id) constraint
            for dedup.
        text: message body.
        sender: display name of the sender.
        received_at: ISO-8601 UTC timestamp the listener observed the
            message. Used for ordering in audit views; the actual row
            received_at is set by the API insert.
        is_backfill: True for replay paths (post-reconnect catchup).
            Threaded to the orchestrator so it can flag the action rows.
        sizing_multiplier: Route.sizing_multiplier. Step 11 (mirror
            routing, deferred) will use this; today every route is 1.0.
    """
    channel_id: str
    tg_chat_id: int
    tg_message_id: int
    text: str
    sender: str = ""
    received_at: str = ""
    is_backfill: bool = False
    # Step 11 mirror routing: identifies which Route this leg of the
    # fan-out came from so the receiving destination can tag its action
    # rows with route_id (lets the audit view in Step 19 reconstruct
    # which leg of the mirror produced which trade). Defaults to "" so
    # pre-Step-11 listeners still pass; the orchestrator stores NULL
    # in that case (matches pre-v2 rows).
    route_id: str = ""
    sizing_multiplier: float = Field(default=1.0, ge=0.0)
    # Step 21 failover destinations: set by the listener when this POST
    # is a fallback retry after the primary destination's POST failed.
    # Empty string when this is a primary POST. The orchestrator
    # propagates it into the OPEN payload as 'failover_from' so the
    # operator sees which destination took over.
    failover_from_destination_id: str = ""
    # Telegram reply_to_msg_id when this message replies to a prior one
    # in the same chat. The orchestrator persists it on the messages row
    # so state_summary can prepend the parent message text to the
    # SYSTEM STATE block — lets the AI resolve "cancel that order" etc.
    # None on normal (non-reply) messages.
    reply_to_tg_message_id: int | None = None


class PositionUpdateBody(BaseModel):
    volume: float | None = None
    sl: float | None = None
    tp: float | None = None
    # Used by the EA on partial closes: the broker-reported P&L of THIS
    # deal alone, added to the position's running realized_pnl. None means
    # "leave realized_pnl alone" — old EA builds keep working.
    realized_pnl_delta: float | None = None


class AttachSignalBody(BaseModel):
    """EA-posted body for /positions/{ticket}/attach_signal.

    Called after the EA successfully modifies the broker-side SL/TP on a
    naked position. Clears is_naked, sets sl/tp on the row. Final-TP only —
    the broker only holds one TP; the full tps[] ladder lives on the
    ATTACH_SIGNAL action payload and is consumed by the EA's RegisterPlan
    for the staged exit.
    """
    sl: float
    tp: float


class MarketPriceBody(BaseModel):
    """Heartbeat from the EA so the AI prompt has a current price for
    two-digit SL shorthand decoding (e.g. "ستوبك 56" -> 4856 only if we
    know gold is around 4850).
    """
    symbol: str = "XAUUSD"
    bid: float
    ask: float


class MarketSnapshotStateBody(BaseModel):
    """Posted by the EA at OnInit (and whenever ``EnableMarketSnapshot``
    flips) so the GUI's services-bar can tell "snapshot intentionally
    off" apart from "snapshot stale". Without this sentinel the pill
    goes red after 3 minutes whenever the operator disables snapshots.
    """
    symbol: str = "XAUUSD"
    enabled: bool


class TimeframeOhlcBody(BaseModel):
    """OHLC + ATR for one timeframe in the market snapshot.

    sma50/sma200 are optional — only the D1 block populates them today
    (the directional evaluator uses D1 trend filters). Older EA builds
    that POST without these fields keep working.
    """
    open: float
    high: float
    low: float
    close: float
    atr14: float
    sma50: float | None = None
    sma200: float | None = None


class MarketSnapshotBody(BaseModel):
    """Multi-timeframe OHLC snapshot pushed by the EA roughly every minute.
    Consumed by the directional-bias evaluator (see src/ai_evaluator.py).

    The original m15/h1/h4 fields are required (older EA builds keep
    working). The directional-rubric extensions (d1, d1_prev, adr20,
    adx_h1, h1_recent_closes) are optional so an EA upgrade can roll out
    independently from the API server. The evaluator falls back to
    `data_quality=reduced` when the new fields are absent.
    """
    symbol: str = "XAUUSD"
    m15: TimeframeOhlcBody
    h1: TimeframeOhlcBody
    h4: TimeframeOhlcBody
    # Directional-rubric extensions (added 2026-05-09).
    d1: TimeframeOhlcBody | None = None         # current day's OHLC + SMA50/200
    d1_prev: TimeframeOhlcBody | None = None    # yesterday's OHLC (for gap/continuation context)
    adr20: float | None = None                  # rolling avg of last 20 D1 ranges
    adx_h1: float | None = None                 # ADX(14) on H1 — trend vs chop
    h1_recent_closes: list[float] | None = None # last ~5 H1 closes (oldest first), for structure


class PositionRecoverBody(BaseModel):
    """EA-posted snapshot for POST /positions/recover.

    Sent by the reverse-reconciliation pass when the EA holds a live broker
    position (matching our magic number + symbol) that the DB has no open
    row for. The API inserts an `open` row flagged `recovered=1` (OR IGNORE,
    so it never resurrects a `closed` row or duplicates an existing one) and
    DMs the operator. sl/tp are optional — a naked/just-opened position may
    not carry them yet.
    """
    mt5_ticket: int
    symbol: str = "XAUUSD"
    side: Literal["BUY", "SELL"]
    volume: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    sl: float | None = None
    tp: float | None = None


class AlertBody(BaseModel):
    """Operator-visible alert posted by the EA when something needs human
    attention (e.g. a staged partial-close gave up after PartialMaxRetries).
    Inserted into actions as an ALERT row; the bot's notification_dispatcher
    DMs the owner because it already handles ALERT rows.
    """
    level: str = "warning"  # "info" | "warning" | "critical"
    text: str
