"""Replay historical Telegram messages through the current AI to detect drift.

For each message in the chosen range:
1. Look up what action(s) actually got inserted (join actions.source_msg_id)
2. Run playground triage + interpreter using current prompts (no DB writes)
3. Compare action_type / side / decision to flag drift

Sequential per provider rate limits. Costs real tokens.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from src.gui.services.playground import run_playground
from src.gui.services.stack_registry import Stack


@dataclass(frozen=True)
class HistMessage:
    id: int
    text: str
    received_at: str
    sender: str
    # v2 per-channel attribution (Step 11). Empty on legacy / pre-tagging
    # messages — view renders "—".
    source_channel_id: str = ""


@dataclass(frozen=True)
class OriginalAction:
    id: int
    action_type: str
    status: str
    side: str
    payload: dict


@dataclass(frozen=True)
class ReplayedAction:
    action_type: str
    side: str
    payload: dict


@dataclass(frozen=True)
class ReplayRow:
    msg: HistMessage
    original: list[OriginalAction]
    triage_decision: str
    replayed: list[ReplayedAction]
    error: str | None
    drift: str
    raw_reasoning: str = ""
    raw_actions_json: str = ""
    cost_estimate: float = 0.0


def _since_iso(days: int | None) -> str | None:
    if days is None:
        return None
    if days == 0:
        now = datetime.now(timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def load_messages(db_path: Path, days: int | None, limit: int = 200) -> list[HistMessage]:
    if not db_path.exists():
        return []
    sql = (
        "SELECT id, text, received_at, sender, source_channel_id "
        "FROM messages WHERE 1=1"
    )
    params: tuple = ()
    since = _since_iso(days)
    if since is not None:
        sql += " AND received_at >= ?"
        params = (since,)
    sql += " ORDER BY id DESC LIMIT ?"
    params = params + (limit,)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    out: list[HistMessage] = []
    for r in rows:
        try:
            sci = r["source_channel_id"]
        except (IndexError, KeyError):
            sci = None
        out.append(HistMessage(
            id=int(r["id"]),
            text=str(r["text"] or ""),
            received_at=str(r["received_at"] or ""),
            sender=str(r["sender"] or ""),
            source_channel_id=str(sci) if sci is not None else "",
        ))
    return out


def load_original_actions(db_path: Path, msg_id: int) -> list[OriginalAction]:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, action_type, status, payload_json FROM actions "
            "WHERE source_msg_id=? ORDER BY id",
            (msg_id,),
        ).fetchall()
    out: list[OriginalAction] = []
    for r in rows:
        try:
            p = json.loads(r["payload_json"] or "{}")
        except json.JSONDecodeError:
            p = {}
        out.append(OriginalAction(
            id=int(r["id"]),
            action_type=str(r["action_type"]),
            status=str(r["status"]),
            side=str(p.get("side", "")).upper() if isinstance(p, dict) else "",
            payload=p if isinstance(p, dict) else {},
        ))
    return out


def _summarize_replayed(actions: list[dict]) -> list[ReplayedAction]:
    out: list[ReplayedAction] = []
    for a in actions:
        out.append(ReplayedAction(
            action_type=str(a.get("type") or a.get("action_type") or ""),
            side=str(a.get("side", "")).upper(),
            payload=a,
        ))
    return out


def detect_drift(
    original: list[OriginalAction],
    triage_decision: str,
    replayed: list[ReplayedAction],
) -> str:
    if not original and not replayed:
        return "none"
    if (not original) and replayed:
        return "decision"
    if original and (triage_decision == "ignore" or not replayed):
        return "decision"
    if len(original) != len(replayed):
        return "count"
    for o, r in zip(original, replayed):
        if o.action_type != r.action_type:
            return "type"
        if o.side and r.side and o.side != r.side:
            return "side"
    return "none"


class ReplayRunner(QThread):
    progress = Signal(int, int)
    row_ready = Signal(object)
    completed = Signal(int)
    failed_with = Signal(str)

    def __init__(
        self,
        stack: Stack,
        messages: list[HistMessage],
        provider_override: str | None,
        model_override: str | None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._stack = stack
        self._messages = messages
        self._provider = provider_override
        self._model = model_override
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        total = len(self._messages)
        drifted = 0
        try:
            for i, msg in enumerate(self._messages):
                if self._cancel:
                    break
                row = self._process(msg)
                if row.drift != "none":
                    drifted += 1
                self.row_ready.emit(row)
                self.progress.emit(i + 1, total)
            self.completed.emit(drifted)
        except Exception as e:
            self.failed_with.emit(f"{type(e).__name__}: {e}")

    def _process(self, msg: HistMessage) -> ReplayRow:
        original = load_original_actions(self._stack.db_path, msg.id)
        result = run_playground(
            self._stack,
            msg.text,
            provider_override=self._provider or None,
            interpreter_model_override=self._model or None,
        )
        triage_decision = result.triage.decision if result.triage else "(error)"
        replayed_raw = result.interpret.actions if result.interpret else []
        replayed = _summarize_replayed(replayed_raw)
        drift = detect_drift(original, triage_decision, replayed)
        cost = 0.0
        if result.interpret:
            u = result.interpret.usage
            cost += (
                u.get("input_tokens", 0) * 3.0
                + u.get("output_tokens", 0) * 15.0
                + u.get("cache_read_tokens", 0) * 0.30
            ) / 1_000_000.0
        if result.triage:
            u = result.triage.usage
            cost += (u.get("input_tokens", 0) * 1.0 + u.get("output_tokens", 0) * 5.0) / 1_000_000.0
        return ReplayRow(
            msg=msg,
            original=original,
            triage_decision=triage_decision,
            replayed=replayed,
            error=result.error,
            drift=drift,
            raw_reasoning=result.interpret.reasoning if result.interpret else "",
            raw_actions_json=json.dumps(replayed_raw, indent=2, ensure_ascii=False),
            cost_estimate=cost,
        )
