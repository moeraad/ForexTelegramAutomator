"""DETAIL panel: per-action reasoning + quality + fill, or synth state when empty."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.gui.models.actions_model import ActionRow, _age_text
from src.gui.services.db_subscriber import DBSubscriber


_VERDICT_COLOR = {
    "recommend": "#26a69a",
    "acceptable": "#ff9800",
    "caution": "#ff7043",
    "avoid": "#ef5350",
}


def _section_title(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        "color: #787b86; font-size: 11px; font-weight: 700; "
        "letter-spacing: 1px; padding-top: 12px;"
    )
    return lbl


def _body(text: str, css: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lbl.setStyleSheet(f"color: #d1d4dc; padding: 4px 0; {css}")
    lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
    return lbl


class DetailPanel(QWidget):
    def __init__(self, subscriber: DBSubscriber, db_path: Path) -> None:
        super().__init__()
        self._subscriber = subscriber
        self._db_path = db_path
        self._open_positions: list[sqlite3.Row] = []
        self._last_open: sqlite3.Row | None = None
        self._watching: list[sqlite3.Row] = []
        self._current_action: ActionRow | None = None

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(16, 12, 16, 12)
        self._content_layout.setSpacing(2)
        self._content_layout.addStretch()
        self._scroll.setWidget(self._content)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._scroll)

        self._subscribe()
        self._render_synth_state()

    def rebind(self, db_path: Path) -> None:
        self._db_path = db_path
        self._open_positions = []
        self._last_open = None
        self._watching = []
        self._render_current()

    def set_action(self, action: ActionRow | None) -> None:
        self._current_action = action
        self._render_current()

    def _render_current(self) -> None:
        if self._current_action is None:
            self._render_synth_state()
        else:
            self._render_action(self._current_action)

    def _subscribe(self) -> None:
        self._subscriber.subscribe(
            "detail_open_positions",
            "SELECT id, mt5_ticket, side, volume, original_volume, entry_price, "
            "sl, tp, opened_at, partial_close_count, sl_moved_at "
            "FROM positions WHERE status='open' ORDER BY opened_at DESC",
        )
        self._subscriber.subscribe(
            "detail_last_open",
            "SELECT id, payload_json, status, created_at "
            "FROM actions WHERE action_type='OPEN' AND status='executed' "
            "ORDER BY id DESC LIMIT 1",
        )
        self._subscriber.subscribe(
            "detail_watching",
            "SELECT id, payload_json, created_at FROM actions "
            "WHERE action_type='OPEN' AND status='watching' "
            "ORDER BY id DESC LIMIT 5",
        )
        self._subscriber.query_changed.connect(self._on_query_changed)

    def _on_query_changed(self, key: str, rows: list[sqlite3.Row]) -> None:
        if key == "detail_open_positions":
            self._open_positions = rows
        elif key == "detail_last_open":
            self._last_open = rows[0] if rows else None
        elif key == "detail_watching":
            self._watching = rows
        else:
            return
        if self._current_action is None:
            self._render_synth_state()

    def _clear(self) -> None:
        while self._content_layout.count() > 0:
            item = self._content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _add(self, w: QWidget) -> None:
        self._content_layout.addWidget(w)

    def _add_kv(self, key: str, value: str) -> None:
        row = QWidget()
        h = QVBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(0)
        k = QLabel(key.upper())
        k.setStyleSheet("color: #787b86; font-size: 10px; letter-spacing: 1px;")
        v = QLabel(value)
        v.setStyleSheet("color: #d1d4dc;")
        v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.setWordWrap(True)
        h.addWidget(k)
        h.addWidget(v)
        self._add(row)

    def _render_action(self, action: ActionRow) -> None:
        self._clear()

        score_text = ""
        if action.is_open and action.quality_score is not None:
            verdict = action.quality_verdict or ""
            color = _VERDICT_COLOR.get(verdict, "#787b86")
            score_text = (
                f"<span style='color:{color}; font-weight:700; font-size:18px;'>"
                f"{action.quality_score}</span>"
                f"<span style='color:#787b86;'>/100</span>"
                f"&nbsp;<span style='color:{color};'>{verdict}</span>"
            )

        title = QLabel(
            f"<span style='font-size:14px; font-weight:700; color:#d1d4dc;'>"
            f"#{action.id}  ·  {action.type_display}</span>"
            f"&nbsp;&nbsp;<span style='color:#787b86;'>{action.status}</span>"
            + (f"&nbsp;&nbsp;{score_text}" if score_text else "")
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        self._add(title)

        meta = QLabel(
            f"<span style='color:#787b86;'>{action.created_at}  ·  age {_age_text(action.created_at)}</span>"
        )
        meta.setTextFormat(Qt.TextFormat.RichText)
        self._add(meta)

        msg = self._lookup_source_message(action)
        if msg:
            self._add(_section_title("SOURCE MESSAGE"))
            sender = msg.get("sender") or "unknown"
            self._add_kv("from", f"{sender}  ·  {msg.get('received_at', '')}")
            body = _body(
                msg.get("text") or "(empty)",
                "background: rgba(255,152,0,0.10); padding: 8px; border-left: 3px solid #ff9800;",
            )
            # The source message is Arabic (XAUUSD signals from an
            # Arabic-language channel). Right-align + RTL layout so the
            # operator reads it as the channel rendered it
            # (REVIEW.md §3 RTL).
            body.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            body.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            self._add(body)

        if action.is_open:
            self._render_open_specifics(action)
        elif action.action_type == "ALERT":
            level = action.payload.get("level", "info")
            text = action.payload.get("text", "")
            self._add(_section_title(f"ALERT  ·  {level}"))
            self._add(_body(text or "(empty)"))
        else:
            self._add(_section_title("PAYLOAD"))
            for k, v in action.payload.items():
                if k == "evaluation":
                    continue
                self._add_kv(k, str(v))

        self._render_position_for_action(action)
        self._content_layout.addStretch()

    def _render_open_specifics(self, action: ActionRow) -> None:
        p = action.payload
        self._add(_section_title("SIGNAL"))
        side = str(p.get("side", "?")).upper()
        entry_low = p.get("entry_low")
        entry_high = p.get("entry_high")
        sl = p.get("sl")
        tps = p.get("tps") or []
        pending = bool(p.get("pending"))
        is_naked = entry_low is None and sl is None and not tps
        if is_naked:
            self._add_kv("side", f"{side}  (NAKED — awaiting ATTACH_SIGNAL)")
        else:
            zone = (
                f"{entry_low}" if entry_low == entry_high
                else f"{entry_low} – {entry_high}"
            )
            self._add_kv("side", f"{side}  {'(PENDING LIMIT)' if pending else '(MARKET)'}")
            self._add_kv("entry", str(zone))
            self._add_kv("sl", str(sl))
            self._add_kv("tps", ", ".join(str(t) for t in tps) or "—")
        if p.get("comment"):
            self._add_kv("comment", str(p["comment"]))

        evaluation = p.get("evaluation") or {}
        if evaluation:
            self._add(_section_title("AI REASONING"))
            summary = evaluation.get("summary") or evaluation.get("key_factor") or ""
            self._add(_body(summary))
            kf = evaluation.get("key_factor")
            if kf and kf != summary:
                self._add_kv("key factor", kf)
            factors = evaluation.get("factors") or {}
            if isinstance(factors, dict) and factors:
                self._add(_section_title("FACTORS"))
                for code, text in factors.items():
                    self._add_kv(code, str(text))

    def _render_position_for_action(self, action: ActionRow) -> None:
        if not self._db_path.exists():
            return
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT mt5_ticket, side, volume, original_volume, entry_price, "
                "sl, tp, exit_price, realized_pnl, status, opened_at, closed_at, close_reason "
                "FROM positions WHERE action_id=? LIMIT 1",
                (action.id,),
            ).fetchone()
        if row is None:
            return
        self._add(_section_title("POSITION"))
        self._add_kv("ticket", str(row["mt5_ticket"]))
        self._add_kv("status", str(row["status"]))
        self._add_kv(
            "fill",
            f"{row['entry_price']}  ({row['side']} {row['volume']} of {row['original_volume']})",
        )
        self._add_kv("sl / tp", f"{row['sl']} / {row['tp']}")
        if row["status"] == "closed":
            self._add_kv("exit", f"{row['exit_price']}  ·  reason: {row['close_reason'] or '—'}")
            pnl = row["realized_pnl"]
            self._add_kv("realized pnl", f"${pnl:.2f}" if pnl is not None else "—")
            self._add_kv("opened / closed", f"{row['opened_at']}  →  {row['closed_at']}")
        else:
            self._add_kv("opened", str(row["opened_at"]))

    def _lookup_source_message(self, action: ActionRow) -> dict | None:
        if not self._db_path.exists():
            return None
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT a.source_msg_id, m.text, m.sender, m.received_at "
                "FROM actions a LEFT JOIN messages m ON m.id = a.source_msg_id "
                "WHERE a.id=?",
                (action.id,),
            ).fetchone()
        if row is None or row["text"] is None:
            return None
        return dict(row)

    def _render_synth_state(self) -> None:
        self._clear()
        title = QLabel(
            "<span style='font-size:14px; font-weight:700; color:#d1d4dc;'>LIVE STATE</span>"
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        self._add(title)
        self._add(_body("Select an action on the left for full detail.", "color: #787b86;"))

        self._add(_section_title("OPEN POSITIONS"))
        if not self._open_positions:
            self._add(_body("(none)", "color: #787b86;"))
        else:
            for p in self._open_positions:
                line = (
                    f"#{p['mt5_ticket']}  {p['side']}  {p['volume']:.2f}"
                    f"  @  {p['entry_price']}  ·  SL {p['sl']}  ·  TP {p['tp']}"
                )
                partials = p["partial_close_count"] or 0
                if partials:
                    line += f"  ·  partials taken: {partials}"
                if p["sl_moved_at"]:
                    line += "  ·  SL moved"
                self._add(_body(line))

        self._add(_section_title("LAST EXECUTED OPEN"))
        if self._last_open is None:
            self._add(_body("(none in this DB)", "color: #787b86;"))
        else:
            try:
                p = json.loads(self._last_open["payload_json"] or "{}")
            except json.JSONDecodeError:
                p = {}
            evaluation = p.get("evaluation") or {}
            score = evaluation.get("score")
            tps = p.get("tps") or [None]
            line = (
                f"#{self._last_open['id']}  ·  {p.get('side', '?')}  "
                f"@  {p.get('entry_low')}  ·  SL {p.get('sl')}  ·  "
                f"TP {tps[0]}  ·  age {_age_text(self._last_open['created_at'])}"
            )
            if score is not None:
                line += f"  ·  quality {score}"
            self._add(_body(line))

        self._add(_section_title("OPEN WATCHING (pending limits)"))
        if not self._watching:
            self._add(_body("(none)", "color: #787b86;"))
        else:
            for w in self._watching:
                try:
                    p = json.loads(w["payload_json"] or "{}")
                except json.JSONDecodeError:
                    p = {}
                self._add(_body(
                    f"#{w['id']}  ·  {p.get('side', '?')}  @  {p.get('entry_low')}"
                    f"  ·  SL {p.get('sl')}  ·  age {_age_text(w['created_at'])}"
                ))

        self._content_layout.addStretch()
