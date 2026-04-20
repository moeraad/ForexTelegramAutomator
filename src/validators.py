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


Action = Union[OpenAction, ModifyAction, CloseAction, CloseAllAction, AlertAction]

_ACTION_BY_TYPE = {
    "OPEN": OpenAction,
    "MODIFY": ModifyAction,
    "CLOSE": CloseAction,
    "CLOSE_ALL": CloseAllAction,
    "ALERT": AlertAction,
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
    # AlertAction
    return ValidationResult(True)
