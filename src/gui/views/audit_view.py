"""Cross-destination Audit view (Step 19 of multi-channel plan).

Search by Telegram message_id or text substring; results show the full
trace per destination — message → actions → DMs — so the operator can
answer "did channel A's mirror actually fire on dest B?" at a glance.

The view is intentionally read-only: the underlying aggregator opens
each destination DB in SQLite's URI ``mode=ro`` form so an audit query
can't accidentally mutate production rows.

Performance posture: aggregator runs cold on each Search click; no
pool, no cache. Reasoning: audit is operator-rare (<100 queries/day
across all operators), and a stale cache would mislead the operator
during incident triage — exactly when freshness matters most.
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.gui.services.audit_aggregator import (
    ActionTrace,
    DestinationTrace,
    DmTrace,
    MessageTrace,
    destinations_from_v2_config,
    search_trace,
)
from src.gui.services.stack_registry import Stack


class AuditView(QWidget):
    """Search-and-trace UI on top of ``audit_aggregator.search_trace``."""

    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._build_ui()

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        # Cleared results; operator triggers a fresh search after rebind.
        self._tree.clear()
        self._summary.setText(
            "<span style='color:#787b86;'>Stack rebind — re-run search.</span>"
        )

    # ---- UI scaffolding ---------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel(
            "<span style='font-size:16px; font-weight:700;'>AUDIT</span>"
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(title)
        hint = QLabel(
            "<span style='color:#787b86;'>cross-destination message tracer  ·  "
            "search by tg_message_id (exact) or text (substring)</span>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(hint)
        header.addStretch()
        layout.addLayout(header)

        # Search row: tg_message_id (int) + text (string) + limit + Search.
        controls = QHBoxLayout()
        controls.addWidget(QLabel("tg_message_id:"))
        self._tg_msg_id = QSpinBox()
        self._tg_msg_id.setRange(0, 2_147_483_647)
        self._tg_msg_id.setValue(0)
        self._tg_msg_id.setSpecialValueText("(any)")
        self._tg_msg_id.setMinimumWidth(140)
        controls.addWidget(self._tg_msg_id)

        controls.addWidget(QLabel("text:"))
        self._text_query = QLineEdit()
        self._text_query.setPlaceholderText("substring (case-sensitive)")
        controls.addWidget(self._text_query, 1)

        controls.addWidget(QLabel("limit/dest:"))
        self._limit = QSpinBox()
        self._limit.setRange(1, 200)
        self._limit.setValue(25)
        controls.addWidget(self._limit)

        from src.gui._button_helpers import make_icon_button
        self._search_btn = make_icon_button(
            "SEARCH", "Run search",
            variant="primary", fallback_text="Search",
        )
        # QToolButton doesn't have setDefault — only QPushButton does.
        # Skipped when running on the icon-button path; Enter key still
        # triggers via QLineEdit returnPressed if wired by caller.
        if hasattr(self._search_btn, "setDefault"):
            self._search_btn.setDefault(True)
        self._search_btn.clicked.connect(self._run_search)
        controls.addWidget(self._search_btn)

        layout.addLayout(controls)

        self._summary = QLabel(
            "<span style='color:#787b86;'>Enter a tg_message_id or text "
            "substring and click Search. Empty query returns recent "
            "messages per destination.</span>"
        )
        layout.addWidget(self._summary)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            "id / time", "type / status", "details",
        ])
        self._tree.setAlternatingRowColors(True)
        hdr = self._tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        # `details` column stretches to fill viewport — search results
        # are wider than narrow id/type columns benefit from.
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setStretchLastSection(True)
        self._tree.setColumnWidth(0, 220)
        self._tree.setColumnWidth(1, 160)
        layout.addWidget(self._tree, 1)

        # Enter in the text box triggers Search.
        self._text_query.returnPressed.connect(self._run_search)

    # ---- Search ----------------------------------------------------------

    def _run_search(self) -> None:
        tg_msg_id_raw = self._tg_msg_id.value()
        tg_msg_id = tg_msg_id_raw if tg_msg_id_raw > 0 else None
        text_query = self._text_query.text().strip()

        destinations = destinations_from_v2_config()
        if not destinations:
            self._summary.setText(
                "<span style='color:#ff9800;'>No v2 destinations configured "
                "yet — open Settings to migrate.</span>"
            )
            self._tree.clear()
            return

        traces = search_trace(
            tg_message_id=tg_msg_id,
            text_query=text_query,
            destinations=destinations,
            limit_per_destination=self._limit.value(),
        )

        total_messages = sum(len(t.messages) for t in traces)
        criteria_parts = []
        if tg_msg_id is not None:
            criteria_parts.append(f"tg_message_id={tg_msg_id}")
        if text_query:
            criteria_parts.append(f"text~{text_query!r}")
        if not criteria_parts:
            criteria_parts.append("recent")
        self._summary.setText(
            f"<span style='color:#787b86;'>"
            f"search [{' AND '.join(criteria_parts)}]  →  "
            f"{total_messages} message(s) across "
            f"{len(traces)} destination(s)"
            "</span>"
        )

        self._render(traces)

    # ---- Tree render -----------------------------------------------------

    def _render(self, traces: tuple[DestinationTrace, ...]) -> None:
        self._tree.clear()
        for t in traces:
            top = QTreeWidgetItem([
                f"📁 {t.destination_name}  ({t.destination_id})",
                f"{len(t.messages)} match" if not t.error else "ERROR",
                t.error if t.error else t.db_path,
            ])
            self._tree.addTopLevelItem(top)
            if t.error:
                continue
            for msg in t.messages:
                top.addChild(_build_message_node(msg))
            top.setExpanded(True)


def _build_message_node(msg: MessageTrace) -> QTreeWidgetItem:
    """One messages row + its action children."""
    chan_label = msg.source_channel_id or "(no channel tag)"
    text_preview = (msg.text or "").replace("\n", " ")
    if len(text_preview) > 80:
        text_preview = text_preview[:77] + "..."
    label_left = f"📨 tg#{msg.tg_message_id}  {_short_time(msg.received_at)}"
    label_mid = f"chat={msg.chat_id}  ch={chan_label}"
    backfill_tag = "  [BACKFILL]" if msg.is_backfill else ""
    msg_node = QTreeWidgetItem([
        label_left, label_mid + backfill_tag, text_preview,
    ])
    for act in msg.actions:
        msg_node.addChild(_build_action_node(act))
    return msg_node


def _build_action_node(act: ActionTrace) -> QTreeWidgetItem:
    """One actions row + its DM children."""
    route_label = act.route_id or "(no route tag)"
    payload_preview = _short_payload(act.payload)
    node = QTreeWidgetItem([
        f"⚡ action#{act.id}  {_short_time(act.created_at)}",
        f"{act.action_type}  {act.status}",
        f"route={route_label}  {payload_preview}",
    ])
    if act.ea_response:
        node.addChild(QTreeWidgetItem([
            "↳ ea_response", "", _truncate(act.ea_response, 120),
        ]))
    if act.executed_at:
        node.addChild(QTreeWidgetItem([
            "↳ executed_at", "", act.executed_at,
        ]))
    for dm in act.dms:
        node.addChild(_build_dm_node(dm))
    return node


def _build_dm_node(dm: DmTrace) -> QTreeWidgetItem:
    """One bot_outbox row (a DM event)."""
    status = "DELIVERED" if dm.delivered_at else "PENDING"
    delivered_label = f"  delivered={_short_time(dm.delivered_at)}" if dm.delivered_at else ""
    return QTreeWidgetItem([
        f"💬 dm#{dm.id}  {_short_time(dm.created_at)}",
        f"{dm.event_type}  {status}",
        f"bot={dm.bot_id}{delivered_label}",
    ])


def _short_time(iso: str | None) -> str:
    """Render an ISO-8601 timestamp compactly. Returns '' for falsy input."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return iso[:19] if isinstance(iso, str) else ""


def _short_payload(payload: dict) -> str:
    """Compact payload preview for the tree's details column."""
    if not payload:
        return ""
    parts = []
    for k in ("symbol", "side", "fraction", "price", "level", "text"):
        if k in payload:
            v = payload[k]
            if isinstance(v, str) and len(v) > 40:
                v = v[:37] + "..."
            parts.append(f"{k}={v}")
    return "  ".join(parts) if parts else _truncate(str(payload), 80)


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: limit - 3] + "..."
