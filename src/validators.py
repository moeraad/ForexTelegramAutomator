import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Literal, Union
from pydantic import BaseModel, Field, field_validator
from src.config import SUPPORTED_SYMBOLS

_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class OpenAction(BaseModel):
    type: Literal["OPEN"] = "OPEN"
    symbol: str
    side: Literal["BUY", "SELL"]
    entry_low: float
    entry_high: float
    tps: list[float] = Field(min_length=1)
    sl: float
    comment: str = ""

    @field_validator("symbol")
    @classmethod
    def supported(cls, v: str) -> str:
        if v not in SUPPORTED_SYMBOLS:
            raise ValueError(f"unsupported symbol {v}")
        return v


class ModifyAction(BaseModel):
    type: Literal["MODIFY"] = "MODIFY"
    mt5_ticket: int
    new_sl: float | None = None
    new_tp: float | None = None


class CloseAction(BaseModel):
    type: Literal["CLOSE"] = "CLOSE"
    mt5_ticket: int
    reason: str = ""


class CloseAllAction(BaseModel):
    type: Literal["CLOSE_ALL"] = "CLOSE_ALL"
    symbol: str
    reason: str = ""


class AlertAction(BaseModel):
    type: Literal["ALERT"] = "ALERT"
    level: Literal["info", "warning"] = "info"
    text: str


# ---- Phase 2: management actions for the singleton open position --------
#
# The system runs in single-position mode (one XAUUSD position max). These
# actions don't carry mt5_ticket — the EA infers it from the singleton.
# Param ranges are enforced at parse time by Pydantic Field constraints;
# state guards (is there an open position? is SL already at BE? has the
# partial already fired?) are the EA's responsibility, not the validator's,
# because state can change between insert and execution.

class MoveSlBeAction(BaseModel):
    """Move SL to entry price (Break-Even) on the open position."""
    type: Literal["MOVE_SL_BE"] = "MOVE_SL_BE"


class MoveSlAction(BaseModel):
    """Move SL to a specific price on the open position."""
    type: Literal["MOVE_SL"] = "MOVE_SL"
    price: float = Field(gt=0)


class ClosePartialAction(BaseModel):
    """Close a fraction of the position's ORIGINAL volume (default 50%)."""
    type: Literal["CLOSE_PARTIAL"] = "CLOSE_PARTIAL"
    fraction: float = Field(default=0.5, gt=0.0, lt=1.0)


class CloseFullAction(BaseModel):
    """Close the entire open position. EA-side no-op if none open."""
    type: Literal["CLOSE_FULL"] = "CLOSE_FULL"


class ReopenLastAction(BaseModel):
    """Reopen the last-closed position (within `within_hours`) at market.

    EA fetches the original signal params from /positions/last_closed and
    runs the regular OPEN flow with them. Skipped if a position is already
    open or no recent close exists.
    """
    type: Literal["REOPEN_LAST"] = "REOPEN_LAST"
    within_hours: int = Field(default=24, gt=0, le=168)


class ReinforceAction(BaseModel):
    """Close current position (regardless of PnL) and immediately reopen
    the same direction using the last-closed position's params. The `side`
    field is informational — the EA uses the prior position's side as the
    source of truth, but the AI is asked to emit `side` so the operator
    log shows what the channel intended.
    """
    type: Literal["REINFORCE"] = "REINFORCE"
    side: Literal["BUY", "SELL"]


class TightenSlAction(BaseModel):
    """Reduce SL distance by `by_fraction`. EA computes new_sl as
    entry +/- (entry - current_sl) * (1 - by_fraction) per side.
    """
    type: Literal["TIGHTEN_SL"] = "TIGHTEN_SL"
    by_fraction: float = Field(default=0.5, gt=0.0, lt=1.0)


Action = Union[
    OpenAction, ModifyAction, CloseAction, CloseAllAction, AlertAction,
    MoveSlBeAction, MoveSlAction, ClosePartialAction, CloseFullAction,
    ReopenLastAction, ReinforceAction, TightenSlAction,
]

_ACTION_BY_TYPE = {
    "OPEN": OpenAction,
    "MODIFY": ModifyAction,
    "CLOSE": CloseAction,
    "CLOSE_ALL": CloseAllAction,
    "ALERT": AlertAction,
    "MOVE_SL_BE": MoveSlBeAction,
    "MOVE_SL": MoveSlAction,
    "CLOSE_PARTIAL": ClosePartialAction,
    "CLOSE_FULL": CloseFullAction,
    "REOPEN_LAST": ReopenLastAction,
    "REINFORCE": ReinforceAction,
    "TIGHTEN_SL": TightenSlAction,
}


Category = Literal["ignore", "context", "signal", "partial_signal"]


class AIResponse(BaseModel):
    actions: list[Action]
    reasoning: str = ""
    category: Category | None = None


def _extract_json(raw: str) -> str:
    """Tolerate markdown fences or conversational preambles around the JSON."""
    m = _FENCE_PATTERN.search(raw)
    if m:
        return m.group(1)
    start = raw.find("{")
    if start < 0:
        return raw
    depth = 0
    for i in range(start, len(raw)):
        c = raw[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return raw[start:i + 1]
    return raw[start:]


def parse_ai_response(raw: str) -> AIResponse:
    try:
        data = json.loads(_extract_json(raw))
    except json.JSONDecodeError as e:
        preview = raw[:200].replace("\n", " ") if raw else "<empty>"
        raise ValueError(f"invalid JSON: {e}; raw_preview={preview!r}")
    actions = []
    for a in data.get("actions", []):
        t = a.get("type")
        cls = _ACTION_BY_TYPE.get(t)
        if cls is None:
            raise ValueError(f"unknown action type: {t}")
        actions.append(cls(**a))
    return AIResponse(
        actions=actions,
        reasoning=data.get("reasoning", ""),
        category=data.get("category"),
    )


@dataclass
class ValidationResult:
    ok: bool
    error: str = ""


def _ticket_open(conn: sqlite3.Connection, ticket: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM positions WHERE mt5_ticket=? AND status='open'",
        (ticket,),
    ).fetchone()
    return row is not None


def _has_overlapping_open_position(
    conn: sqlite3.Connection, symbol: str, side: str,
    entry_low: float, entry_high: float,
) -> bool:
    rows = conn.execute(
        "SELECT entry_price FROM positions "
        "WHERE symbol=? AND side=? AND status='open'",
        (symbol, side),
    ).fetchall()
    for r in rows:
        ep = r["entry_price"]
        if ep is None:
            continue
        if entry_low <= ep <= entry_high:
            return True
    return False


def validate_action(action: Action, conn: sqlite3.Connection) -> ValidationResult:
    if isinstance(action, OpenAction):
        if _has_overlapping_open_position(
            conn, action.symbol, action.side, action.entry_low, action.entry_high
        ):
            return ValidationResult(False, "duplicate: overlaps existing open position")
        return ValidationResult(True)
    if isinstance(action, (CloseAction, ModifyAction)):
        if not _ticket_open(conn, action.mt5_ticket):
            return ValidationResult(False, f"unknown or closed ticket {action.mt5_ticket}")
        return ValidationResult(True)
    if isinstance(action, CloseAllAction):
        if action.symbol not in SUPPORTED_SYMBOLS:
            return ValidationResult(False, f"unsupported symbol {action.symbol}")
        return ValidationResult(True)
    # Phase 2 management actions: parse-time Field constraints handle
    # numeric ranges; state-existence checks (open position present?
    # last-closed within window?) are deferred to the EA so they aren't
    # racing with execution. Pass through unconditionally here.
    if isinstance(action, (
        MoveSlBeAction, MoveSlAction, ClosePartialAction, CloseFullAction,
        ReopenLastAction, ReinforceAction, TightenSlAction,
    )):
        return ValidationResult(True)
    # AlertAction
    return ValidationResult(True)
