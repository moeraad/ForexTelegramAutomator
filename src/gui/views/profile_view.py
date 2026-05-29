"""PROFILE view: structured editor for channels/<name>.json.

Each top-level field becomes a labeled section. Short fields use QLineEdit,
long fields use a monospace QPlainTextEdit. Save writes the JSON back to
disk; Reload Listener restarts the listener service so it picks up the new
prompt without manual intervention.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.gui.services import nssm_client
from src.gui.services.playground import PlaygroundResult, format_actions, run_playground
from src.gui.services.stack_registry import Stack
from src.gui.theme import current_palette


_SHORT_FIELDS = ("name", "symbol", "language")
_PROSE_FIELDS = ("description", "header", "compound_messages")
# Triggers-derived prompt fragments — `vocabulary_table`,
# `commentary_filter`, `worked_examples`, `triage_keep_triggers` — are
# baked from `triggers` by profile_io.render(). Editing them inline
# would be overwritten on the next render, so the Editor used to show
# read-only views. With the dedicated Triggers tab (source of truth)
# and the Prompts tab (rendered preview), those read-only views were
# the weakest of three surfaces and added a "why is there an 'Edit
# elsewhere' button here?" footgun. Drop them from the Editor entirely
# — `_EDITOR_HIDDEN` filters them out on load.
_DERIVED_FIELDS = (
    "vocabulary_table",
    "commentary_filter",
    "worked_examples",
    "triage_keep_triggers",
)
_EDITOR_HIDDEN = frozenset({"triggers", *_DERIVED_FIELDS})
_LANGUAGES = ("ar", "en", "fr", "es", "ru", "tr", "de", "pt")
_FIELD_ORDER = (
    "name",
    "description",
    "symbol",
    "language",
    "shorthand_decode_example",
    "header",
    "vocabulary_table",
    "compound_messages",
    "commentary_filter",
    "directional_command_flow",
    "worked_examples",
    "triage_keep_triggers",
)
_REQUIRED = ("name", "symbol", "header", "vocabulary_table", "worked_examples")

# Logical groupings for the Editor — drives the SettingCardGroup layout
# so the Editor uses the same Fluent shell as Settings → Tuning. Each
# tuple is (group_title, (field_key, ...)). Fields present in the data
# but not listed here fall through to "_PROFILE_EDITOR_FALLBACK_GROUP".
_PROFILE_EDITOR_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Identity",        ("name", "symbol", "language")),
    ("Prompt content",  ("description", "shorthand_decode_example", "header",
                         "compound_messages", "directional_command_flow")),
    # `vocabulary_table`, `commentary_filter`, `worked_examples`, and
    # `triage_keep_triggers` are intentionally absent here — they're
    # derived from the Triggers tab. See _EDITOR_HIDDEN above.
)
_PROFILE_EDITOR_FALLBACK_GROUP = "Other"

# Per-key metadata: icon + one-line subtitle for the card. Anything not
# listed falls back to (FluentIcon.EDIT, "") so the row still renders.
# Subtitle reads as the operator's tooltip-at-a-glance hint.
def _profile_field_meta(key: str):
    # Import inside the function so module import time doesn't cost the
    # qfluentwidgets icon set when this file is just being parsed by
    # tests that don't render UI.
    from qfluentwidgets import FluentIcon
    table: dict[str, tuple[object, str]] = {
        "name":           (FluentIcon.TAG,      "Profile name shown in the AI prompt header."),
        "description":    (FluentIcon.INFO,     "One-paragraph description of the channel character."),
        "symbol":         (FluentIcon.PIE_SINGLE, "Trading symbol (single-symbol invariant — XAUUSD only)."),
        "language":       (FluentIcon.LANGUAGE, "Source-channel language code (drives language-specific rules)."),
        "shorthand_decode_example": (FluentIcon.CODE, "Example of two-digit shorthand expansion anchored on market mid."),
        "header":         (FluentIcon.DOCUMENT, "Top of the SYSTEM prompt — sets persona and invariants."),
        "compound_messages": (FluentIcon.MESSAGE, "Compound-emit rules (e.g. MOVE_SL_BE + CLOSE_PARTIAL)."),
        "directional_command_flow": (FluentIcon.SCROLL, "How directional commands map to OPEN actions."),
    }
    return table.get(key, (FluentIcon.EDIT, ""))


def _is_long_field(key: str) -> bool:
    return key not in _SHORT_FIELDS


# Per-key sizing for the editor inside an ExpandSettingCard's expanded
# panel. Prose fields (description) read better short; mono blobs
# (header, vocabulary_table) need more room. Derived widgets (tables,
# chip flows) need the most.
def _editor_min_height(key: str) -> int:
    if key == "header":
        return 200
    if key == "description":
        return 80
    if key in _PROSE_FIELDS:
        return 120
    return 160


def _editor_max_height(key: str) -> int:
    if key in ("header", "compound_messages", "directional_command_flow"):
        return 360
    return 240


def _mono_font() -> QFont:
    f = QFont("Consolas")
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSize(10)
    return f


def _prose_font() -> QFont:
    f = QFont()
    f.setPointSize(10)
    return f


class ProfileView(QWidget):
    """Profile editor — Phase-2 cleanup: now per-Profile, not per-Stack.

    The Editor tab + Save / Revert / Export buttons operate on the
    profile picked in the top-left dropdown, not on whatever profile
    the current Stack happens to own. Operators editing aggregate-
    routing setups (where one destination has channels using N
    different profiles) can pick any profile without switching stacks.

    The sub-tabs (Triggers, Unmatched, Playground) stay stack-scoped
    because they reference per-destination DB rows (matcher hit counts,
    unmatched signal pool, latest-action replay). Switching profile via
    the picker keeps them on the current stack.
    """

    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        # Phase-2: separate "which profile are we editing?" from "which
        # stack are we on?". Initial value follows the stack so existing
        # behavior is unchanged until the operator changes the picker.
        self._active_profile_name: str = stack.name
        self._original: OrderedDict[str, str] = OrderedDict()
        self._editors: dict[str, QWidget] = {}
        self._dirty = False
        self._build_ui()
        self.rebind(stack)

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        # On stack change, reset the picker to the new stack's profile —
        # that's the operator's most likely intent ("show me THIS
        # stack's profile"). They can re-pick another one if they want.
        self._active_profile_name = stack.name
        self._refresh_picker_choices()
        self._load_from_disk()
        if hasattr(self, "_triggers_tab"):
            self._triggers_tab.rebind(stack)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top = QHBoxLayout()
        self._title = QLabel()
        self._title.setTextFormat(Qt.TextFormat.RichText)
        from src.gui.panels._a11y import mark_heading
        mark_heading(self._title, "Profile")
        top.addWidget(self._title)
        self._dirty_marker = QLabel("")
        self._dirty_marker.setStyleSheet("color: #ff9800;")
        top.addWidget(self._dirty_marker)
        top.addStretch()
        layout.addLayout(top)

        # Profile-wide ribbon — common to all profile tabs, sits
        # immediately under the title row. Three groups:
        #   File       — Save / Revert / Export
        #   Listener   — Reload listener service (applies saved profile)
        #   Generate   — Build a fresh profile from channel history
        from src.gui.panels.ribbon_bar import RibbonAction, RibbonBar, RibbonGroup
        ribbon = RibbonBar([
            RibbonGroup("File", [
                RibbonAction("SAVE", "Save",
                             "Commit profile edits to disk",
                             variant="success", callback=self._save),
                RibbonAction("CANCEL", "Revert",
                             "Discard pending edits and re-read from disk",
                             variant="warning", callback=self._load_from_disk),
                RibbonAction("SHARE", "Export",
                             "Save the profile JSON to another location",
                             callback=self._export),
            ]),
            RibbonGroup("Listener", [
                RibbonAction("UPDATE", "Reload",
                             "nssm restart <listener service> · applies the saved profile",
                             variant="warning", callback=self._reload_listener),
            ]),
            RibbonGroup("Generate", [
                RibbonAction("ROBOT", "From history",
                             "Fetch recent channel messages, classify each via AI, "
                             "and derive a profile JSON from the results.",
                             variant="success", callback=self._open_generator_wizard),
            ]),
        ])
        layout.addWidget(ribbon)

        # Phase-2: profile picker — operator picks WHICH profile to edit
        # instead of being implicitly bound to the current stack's profile.
        # Hidden by _refresh_picker_choices when the active destination has
        # ≤1 profile (1:1 topology) — switching destinations via the
        # top-left picker already disambiguates. Visible for aggregate /
        # mesh routing where one destination serves multiple channels
        # with different profiles.
        self._picker_holder = QWidget()
        picker_row = QHBoxLayout(self._picker_holder)
        picker_row.setContentsMargins(0, 0, 0, 0)
        picker_row.addWidget(QLabel("Profile:"))
        self._picker = QComboBox()
        self._picker.setMinimumWidth(280)
        self._picker.currentIndexChanged.connect(self._on_picker_changed)
        picker_row.addWidget(self._picker)
        picker_row.addStretch()
        layout.addWidget(self._picker_holder)

        self._meta_label = QLabel()
        self._meta_label.setStyleSheet(f"color: {current_palette().text_muted}; padding-bottom: 4px;")
        layout.addWidget(self._meta_label)

        self._tabs = QTabWidget()
        self._editor_tab = QWidget()
        editor_layout = QVBoxLayout(self._editor_tab)
        editor_layout.setContentsMargins(0, 8, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(12)
        self._scroll.setWidget(self._content)
        editor_layout.addWidget(self._scroll)
        self._tabs.addTab(self._editor_tab, "Editor")

        from src.gui.views.triggers_view import TriggersView
        self._triggers_tab = TriggersView(
            self._stack, get_profile_name=lambda: self._active_profile_name,
        )
        self._tabs.addTab(self._triggers_tab, "Triggers")

        from src.gui.views.unmatched_view import UnmatchedView
        self._unmatched_tab = UnmatchedView(
            self._stack,
            get_profile_name=lambda: self._active_profile_name,
        )
        # Promotion writes through profile_io, so the Triggers tab must
        # reload from disk to surface the new row. Without this, the
        # operator promotes, switches tabs, and sees stale in-memory
        # state — the on-disk profile is correct but TriggersView's
        # `self._triggers` is not.
        self._unmatched_tab.trigger_promoted.connect(
            self._triggers_tab._load_from_disk
        )
        self._tabs.addTab(self._unmatched_tab, "Unmatched")

        self._playground = _PlaygroundTab(
            lambda: self._stack,
            get_profile_name=lambda: self._active_profile_name,
        )
        self._tabs.addTab(self._playground, "Playground")
        layout.addWidget(self._tabs, 1)

    # ---- Phase 2: profile picker ----------------------------------------

    def _discover_profiles(self) -> list[str]:
        """Return every profile name (stem) discoverable on disk.

        Sources (deduped):
          - ``%APPDATA%/CopyTrades/<name>/profile.json`` — per-stack files
            written by the wizard
          - ``<repo>/channels/<name>.json`` — operator-edited templates
        """
        import os
        from src.gui.services.stack_registry import BASE_DIR
        names: set[str] = set()
        try:
            appdata = Path(os.environ.get("APPDATA", str(Path.home())))
            root = appdata / "CopyTrades"
            if root.exists():
                for sub in root.iterdir():
                    if sub.is_dir() and (sub / "profile.json").exists():
                        names.add(sub.name)
        except Exception:
            pass
        try:
            ch_dir = BASE_DIR / "channels"
            if ch_dir.exists():
                for p in ch_dir.glob("*.json"):
                    if p.stem.endswith("_draft"):
                        continue
                    names.add(p.stem)
        except Exception:
            pass
        return sorted(names)

    def _refresh_picker_choices(self) -> None:
        """Repopulate the picker; preserve the active selection when possible.

        Picker visibility:
          - ≤1 profile reachable from the active destination → HIDDEN.
            Top-left picker already disambiguates (1:1 topology).
          - 2+ profiles reachable → VISIBLE so the operator can switch
            within the destination (aggregate/mesh routing).
        """
        if not hasattr(self, "_picker"):
            return
        scoped = self._profiles_for_active_destination()
        choices = scoped if scoped else self._discover_profiles()
        # Make sure the active name is in the list even if it doesn't
        # exist on disk yet (e.g., brand-new stack before first save).
        if self._active_profile_name and self._active_profile_name not in choices:
            choices.append(self._active_profile_name)
            choices.sort()
        self._picker.blockSignals(True)
        self._picker.clear()
        for name in choices:
            self._picker.addItem(name, name)
        idx = self._picker.findData(self._active_profile_name)
        if idx >= 0:
            self._picker.setCurrentIndex(idx)
        self._picker.blockSignals(False)
        # Hide the whole picker row when there's nothing to switch between.
        if hasattr(self, "_picker_holder"):
            self._picker_holder.setVisible(len(choices) > 1)

    def _profiles_for_active_destination(self) -> list[str]:
        """Profile names reachable through routes hitting the active dest.

        Returns [] when v2 config can't be read — caller falls back to
        the global profile list so the picker still works on legacy
        single-stack installs.
        """
        try:
            from src import config_v2
            cfg = config_v2.load_v2(config_v2.config_path())
            if cfg is None:
                return []
        except Exception:
            return []
        # Find the destination matching the active stack (by name).
        dest = next(
            (d for d in cfg.destinations if d.name == self._stack.name),
            None,
        )
        if dest is None:
            return []
        names: list[str] = []
        seen: set[str] = set()
        for r in cfg.routes:
            if r.destination_id != dest.id or not r.enabled:
                continue
            ch = cfg.channel(r.channel_id)
            if ch is None or not ch.enabled:
                continue
            prof = cfg.profile(ch.profile_id)
            if prof is None or prof.name in seen:
                continue
            seen.add(prof.name)
            names.append(prof.name)
        names.sort()
        return names

    def _on_picker_changed(self, _idx: int) -> None:
        picked = self._picker.currentData()
        if not picked or picked == self._active_profile_name:
            return
        # Operator picked a different profile — warn about unsaved edits.
        if self._dirty:
            ans = QMessageBox.question(
                self, "Switch profile",
                "Unsaved edits in this profile will be lost. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                # Snap the picker back.
                self._picker.blockSignals(True)
                self._picker.setCurrentIndex(
                    self._picker.findData(self._active_profile_name)
                )
                self._picker.blockSignals(False)
                return
        self._active_profile_name = picked
        self._load_from_disk()
        # Tell sub-tabs to re-read from the new profile (TriggersView
        # owns the writable triggers list; UnmatchedView reads dest-DB
        # but appends to the active profile on promote).
        if hasattr(self, "_triggers_tab") and hasattr(self._triggers_tab, "reload"):
            try:
                self._triggers_tab.reload()
            except Exception:
                pass

    def _active_profile_path(self) -> Path:
        """Resolve the active profile's JSON path.

        Priority:
          1. v2 Profile entity matching ``_active_profile_name`` (path
             is operator-set in V2 Config).
             - If that absolute path doesn't exist on this machine
               (config was copied between PCs / installs / users), fall
               back to the same-basename file under the runtime bundle
               (``BASE_DIR/channels/<basename>``) or APPDATA.
          2. Legacy lookup via ``profile_io.profile_path`` (APPDATA
             stack folder + ``channels/<name>.json`` fallback).

        Decoupled from ``self._stack.profile_path`` so the picker can
        switch independently of the current stack.
        """
        try:
            from src import config_v2
            cfg = config_v2.load_v2(config_v2.config_path())
            if cfg is not None:
                for p in cfg.profiles:
                    if p.name == self._active_profile_name and p.path:
                        recorded = Path(p.path)
                        if recorded.exists():
                            return recorded
                        # Recorded path is stale (different machine /
                        # install / user) — match the basename against
                        # locations that actually exist on THIS machine.
                        return self._resolve_profile_by_basename(
                            recorded.name,
                        ) or recorded
        except Exception:
            pass
        from src.gui.services import profile_io
        return profile_io.profile_path(self._active_profile_name)

    def _resolve_profile_by_basename(self, basename: str) -> Path | None:
        """Find ``basename`` under the runtime bundle / APPDATA channels.

        Used when the v2 Profile.path was recorded on a different
        machine and references a path that doesn't exist here.
        """
        import os
        from src.config import BASE_DIR
        candidates = [
            BASE_DIR / "channels" / basename,
            Path(os.environ.get("APPDATA", "")) / "CopyTrades" / "channels" / basename
            if os.environ.get("APPDATA") else None,
        ]
        for c in candidates:
            if c is not None and c.exists():
                return c
        return None

    def _load_from_disk(self) -> None:
        path = self._active_profile_path()
        _tm = current_palette().text_muted
        self._title.setText(
            f"<span style='font-size:16px; font-weight:700;'>PROFILE</span>"
            f"&nbsp;&nbsp;<span style='color:{_tm};'>{self._active_profile_name}</span>"
        )
        self._meta_label.setText(f"file: {path}")
        if not path.exists():
            self._render_error(f"profile file not found: {path}")
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            self._render_error(f"profile JSON is invalid: {e}")
            return
        if not isinstance(data, dict):
            self._render_error("profile JSON must be an object at the top level")
            return
        ordered: OrderedDict[str, str] = OrderedDict()
        seen = set(_EDITOR_HIDDEN)
        for key in _FIELD_ORDER:
            if key in data and key not in _EDITOR_HIDDEN:
                ordered[key] = str(data[key]) if data[key] is not None else ""
                seen.add(key)
        for key, value in data.items():
            if key in seen:
                continue
            ordered[key] = str(value) if value is not None else ""
        self._original = ordered
        self._render_form(ordered)
        self._set_dirty(False)

    def _render_error(self, message: str) -> None:
        self._clear_content()
        lbl = QLabel(message)
        lbl.setStyleSheet("color: #ef5350; padding: 32px;")
        lbl.setWordWrap(True)
        self._content_layout.addWidget(lbl)
        self._content_layout.addStretch()

    def _clear_content(self) -> None:
        while self._content_layout.count() > 0:
            item = self._content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._editors.clear()

    def _render_form(self, data: OrderedDict[str, str]) -> None:
        self._clear_content()
        from src.gui.panels._setting_cards import make_setting_card
        from qfluentwidgets import SettingCardGroup

        # Partition the keys we have into the predefined sections;
        # anything unrecognised tails into a fallback group so unknown
        # JSON fields still appear in the UI.
        remaining = OrderedDict(data)
        section_map: dict[str, list[str]] = {}
        section_order: list[str] = []
        for title, keys in _PROFILE_EDITOR_SECTIONS:
            buckets = [k for k in keys if k in remaining]
            if buckets:
                section_map[title] = buckets
                section_order.append(title)
                for k in buckets:
                    remaining.pop(k, None)
        if remaining:
            section_map[_PROFILE_EDITOR_FALLBACK_GROUP] = list(remaining.keys())
            section_order.append(_PROFILE_EDITOR_FALLBACK_GROUP)

        for title in section_order:
            group = SettingCardGroup(title)
            for key in section_map[title]:
                value = data[key]
                widget, kind = self._build_editor_widget(key, value, None)
                if widget is None:
                    continue
                icon, subtitle = _profile_field_meta(key)
                # Use a humanised title for the card while keeping the
                # uppercase legend visible to operators familiar with the
                # raw JSON keys.
                pretty = key.replace("_", " ").title()
                card_title = f"{pretty}  ·  {key}"
                card = make_setting_card(
                    icon=icon,
                    title=card_title,
                    subtitle=subtitle,
                    widget=widget,
                    kind=kind,
                    expand_min_height=_editor_min_height(key),
                    expand_max_height=_editor_max_height(key),
                )
                group.addSettingCard(card)
            self._content_layout.addWidget(group)
        self._content_layout.addStretch()

    def _build_editor_widget(self, key: str, value: str, triggers):
        """Construct the editor widget for one profile key, register it
        in ``self._editors``, and wire dirty-tracking. Returns
        ``(widget, kind)`` where ``kind`` is ``"compact"`` for short
        single-line controls and ``"expand"`` for everything else.

        Note: derived prompt-fragment keys (vocabulary_table etc.) are
        filtered out via ``_EDITOR_HIDDEN`` before reaching this method —
        the Triggers tab is their source of truth.
        """
        if key == "language":
            combo = QComboBox()
            combo.setEditable(True)
            combo.setMinimumWidth(180)
            for lang in _LANGUAGES:
                combo.addItem(lang)
            if value:
                idx = combo.findText(value)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                else:
                    combo.setEditText(value)
            combo.currentTextChanged.connect(self._on_changed)
            self._editors[key] = combo
            return combo, "compact"
        if key in _SHORT_FIELDS:
            line = QLineEdit(value)
            line.textChanged.connect(self._on_changed)
            self._editors[key] = line
            return line, "compact"
        edit = QPlainTextEdit()
        if key in _PROSE_FIELDS:
            edit.setFont(_prose_font())
        else:
            edit.setFont(_mono_font())
        edit.setPlainText(value)
        edit.textChanged.connect(self._on_changed)
        self._editors[key] = edit
        return edit, "expand"

    def _jump_to_triggers_tab(self) -> None:
        for idx in range(self._tabs.count()):
            if self._tabs.tabText(idx).strip().lower() == "triggers":
                self._tabs.setCurrentIndex(idx)
                return

    def _on_changed(self) -> None:
        self._set_dirty(self._current() != self._original)

    def _current(self) -> OrderedDict[str, str]:
        out: OrderedDict[str, str] = OrderedDict()
        for key in self._original.keys():
            widget = self._editors.get(key)
            if isinstance(widget, QLineEdit):
                out[key] = widget.text()
            elif isinstance(widget, QComboBox):
                out[key] = widget.currentText()
            elif isinstance(widget, QPlainTextEdit):
                out[key] = widget.toPlainText()
            else:
                # Derived widgets: keep the existing rendered value (it'll be
                # recomputed on save from the triggers array anyway).
                out[key] = self._original[key]
        return out

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self._dirty_marker.setText("●  unsaved" if dirty else "")

    def _validate(self, data: OrderedDict[str, str]) -> str | None:
        for key in _REQUIRED:
            if key not in data or not data[key].strip():
                return f"required field '{key}' is empty"
        return None

    def _save(self) -> None:
        data = self._current()
        err = self._validate(data)
        if err:
            QMessageBox.warning(self, "Cannot save", err)
            return
        from src.gui.services import profile_io
        # Merge editor-changes onto the on-disk JSON so the triggers array
        # (which the editor does NOT show) survives + drives derived fields.
        # Phase-2: uses the picker's active profile, not the stack's name.
        on_disk = profile_io.load_profile(self._active_profile_name)
        merged = dict(on_disk)
        for k, v in data.items():
            if k in _DERIVED_FIELDS:
                continue
            merged[k] = v
        try:
            path = profile_io.save_profile(self._active_profile_name, merged)
        except OSError as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return
        self._original = data
        self._set_dirty(False)
        # Day-2 cleanup: with mtime-based ProfileContext invalidation
        # (src/profile_context.py:get_profile_context), the listener and
        # API processes pick up the new prompt automatically on the very
        # next message — no service restart required. The "Reload
        # Listener" button is still useful when the operator wants to
        # force-restart for other reasons (e.g. after a code update).
        QMessageBox.information(
            self, "Saved",
            f"Wrote {path}\n\n"
            "✓ Live: the next incoming message will use the new prompt "
            "(no service restart needed).\n\n"
            "Use 'Reload Listener' only if you also need to bounce the "
            "Telethon session.",
        )

    def _export(self) -> None:
        suggested = f"{self._active_profile_name}_profile_backup.json"
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Export profile", suggested, "JSON (*.json)"
        )
        if not path_str:
            return
        try:
            Path(path_str).write_text(
                json.dumps(dict(self._current()), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            QMessageBox.information(self, "Exported", f"Wrote {path_str}")
        except OSError as e:
            QMessageBox.critical(self, "Export failed", str(e))

    def _open_generator_wizard(self) -> None:
        if self._dirty:
            ans = QMessageBox.question(
                self, "Unsaved changes",
                "You have unsaved profile changes. Discard them and run the "
                "generator (it overwrites this file)?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        from src.gui.windows.profile_generator_wizard import ProfileGeneratorWizard
        wiz = ProfileGeneratorWizard(self._stack, self)
        if wiz.exec():
            self._load_from_disk()
            # The Triggers tab caches its own in-memory copy of the
            # profile's triggers; without rebind it sits stale after
            # the wizard finishes and the operator has to click Revert
            # (or restart the GUI) to see freshly-generated rows.
            self._triggers_tab.rebind(self._stack)

    def _reload_listener(self) -> None:
        if self._dirty:
            ans = QMessageBox.question(
                self, "Unsaved changes",
                "You have unsaved profile changes. Save them before reloading?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if ans == QMessageBox.StandardButton.Cancel:
                return
            if ans == QMessageBox.StandardButton.Save:
                self._save()
                if self._dirty:
                    return
        # Phase-1: service_names is variable-length now; use the
        # primary headline instead of indexing.
        listener_svc = self._stack.primary_listener_service or (
            self._stack.service_names[2]
            if len(self._stack.service_names) >= 3 else ""
        )
        ok, msg = nssm_client.nssm_restart(listener_svc)
        if ok:
            QMessageBox.information(
                self, "Listener reloaded",
                f"{listener_svc} restarted.\nThe new prompt will apply to the next incoming message.",
            )
        else:
            QMessageBox.warning(
                self, "Restart failed",
                f"Could not restart {listener_svc}.\n\n{msg or 'nssm command failed'}",
            )


class _PlaygroundRunner(QThread):
    finished_with = Signal(object)

    def __init__(
        self, stack: Stack, message: str,
        profile_name: str | None = None,
    ) -> None:
        super().__init__()
        self._stack = stack
        self._message = message
        self._profile_name = profile_name

    def run(self) -> None:
        result = run_playground(
            self._stack,
            self._message,
            provider_override=None,
            interpreter_model_override=None,
            profile_name=self._profile_name,
        )
        self.finished_with.emit(result)


class _PlaygroundTab(QWidget):
    def __init__(self, get_stack, get_profile_name=None):
        """``get_profile_name`` (post-v2 gap fix) is a callback returning
        the parent ProfileView's active profile name. When given, the
        playground uses that profile's SYSTEM_PROMPT + TRIAGE_SYSTEM_PROMPT
        instead of the stack's default. Operator editing an aggregate-
        routing config sees the playground reflect their edits.
        """
        super().__init__()
        self._get_stack = get_stack
        self._get_profile_name = get_profile_name
        self._runner: _PlaygroundRunner | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        _tm2 = current_palette().text_muted
        notice = QLabel(
            f"<span style='color:{_tm2};'>"
            "Live AI call against the active channel's state (no DB writes). "
            "Save profile changes before running so the prompt picks them up. "
            "Costs real tokens."
            "</span>"
        )
        notice.setTextFormat(Qt.TextFormat.RichText)
        notice.setWordWrap(True)
        layout.addWidget(notice)

        layout.addWidget(QLabel("MESSAGE  ·  paste a Telegram message verbatim"))
        self._input = QPlainTextEdit()
        self._input.setFont(_mono_font())
        self._input.setMinimumHeight(110)
        self._input.setPlaceholderText("e.g.  Xauusd buy limit.4708.21\\nSL 4705.17\\nTp 4736.10")
        layout.addWidget(self._input)

        action_row = QHBoxLayout()
        self._run_btn = QPushButton("Run  ·  triage → interpret")
        self._run_btn.setProperty("variant", "primary")
        self._run_btn.clicked.connect(self._on_run)
        action_row.addWidget(self._run_btn)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {current_palette().text_muted}; padding-left: 12px;")
        action_row.addWidget(self._status)
        action_row.addStretch()
        layout.addLayout(action_row)

        results = QSplitter(Qt.Orientation.Horizontal)
        self._left_panel = QPlainTextEdit()
        self._left_panel.setReadOnly(True)
        self._left_panel.setFont(_mono_font())
        self._left_panel.setPlaceholderText("Triage + context appear here after Run.")
        self._right_panel = QPlainTextEdit()
        self._right_panel.setReadOnly(True)
        self._right_panel.setFont(_mono_font())
        self._right_panel.setPlaceholderText("Interpreter response appears here after Run.")
        results.addWidget(self._left_panel)
        results.addWidget(self._right_panel)
        results.setStretchFactor(0, 1)
        results.setStretchFactor(1, 2)
        layout.addWidget(results, 1)

    def _on_run(self) -> None:
        if self._runner is not None and self._runner.isRunning():
            return
        message = self._input.toPlainText().strip()
        if not message:
            QMessageBox.information(self, "Empty message", "Paste a message before Run.")
            return
        stack = self._get_stack()
        self._run_btn.setEnabled(False)
        self._status.setText("running…")
        self._left_panel.setPlainText("")
        self._right_panel.setPlainText("")
        profile_name = (
            self._get_profile_name() if self._get_profile_name else None
        )
        self._runner = _PlaygroundRunner(stack, message, profile_name=profile_name)
        self._runner.finished_with.connect(self._on_finished)
        self._runner.finished.connect(self._on_thread_done)
        from src.gui.services.thread_registry import register
        register(self._runner, stop_fn=self._runner.quit)
        self._runner.start()

    def _on_thread_done(self) -> None:
        self._run_btn.setEnabled(True)
        self._runner = None

    def _on_finished(self, result: PlaygroundResult) -> None:
        ctx = result.context
        left = [
            f"PROFILE       {ctx.profile}",
            f"PROVIDER      {ctx.provider}",
            f"INTERPRETER   {ctx.interpreter_model}",
            f"TRIAGE MODEL  {ctx.triage_model}",
            f"OPEN POSNS    {ctx.open_count}",
            f"ELAPSED       {ctx.elapsed_total_ms} ms",
        ]
        if result.triage is not None:
            left += [
                "",
                "TRIAGE",
                f"  decision: {result.triage.decision}",
                f"  latency:  {result.triage.latency_ms} ms",
                f"  usage:    {result.triage.usage}",
                "  raw:",
                "    " + (result.triage.raw_text or "").replace("\n", "\n    "),
            ]
        left += ["", "STATE BLOCK (preview):", ctx.open_positions_block_preview]
        self._left_panel.setPlainText("\n".join(left))

        if result.error:
            self._right_panel.setPlainText(f"ERROR\n  {result.error}")
            self._status.setText("error")
            return

        if result.triage is None:
            self._right_panel.setPlainText("(no triage outcome)")
            self._status.setText("aborted")
            return

        if result.triage.decision != "keep":
            self._right_panel.setPlainText(
                f"INTERPRETER SKIPPED\n  triage decision: {result.triage.decision}"
            )
            self._status.setText(f"triage={result.triage.decision}")
            return

        if result.interpret is None:
            self._right_panel.setPlainText("(no interpreter outcome — see ERROR on the left)")
            self._status.setText("interpreter missing")
            return

        i = result.interpret
        out = [
            "INTERPRETER",
            f"  latency: {i.latency_ms} ms",
            f"  usage:   {i.usage}",
            "",
            f"ACTIONS  ({len(i.actions)})",
            format_actions(i.actions),
            "",
            "REASONING",
            i.reasoning or "(none)",
        ]
        self._right_panel.setPlainText("\n".join(out))
        self._status.setText(
            f"ok  ·  {len(i.actions)} action(s)  ·  {i.latency_ms} ms"
        )
