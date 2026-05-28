"""Qt smoke tests for the Telethon-aware Add Account dialog.

These tests don't go through real Telethon — they stub the
``TelegramSessionService`` interactions to confirm the dialog wires
its phases (basics → awaiting_code → done) and persists the session
blob to a per-account file on save.
"""
from __future__ import annotations

from pathlib import Path

from src import config_v2
from src.config_v2 import ConfigV2


def test_dialog_constructs_in_basics_phase(
    qapp, qtbot, monkeypatch, tmp_path: Path,
):
    """Initial state: basics fields enabled, code/password ROWS HIDDEN, OK off.

    Code and 2FA rows are deliberately not part of the form on first
    render — they reveal only when Telegram needs them (Step "phased UI").
    """
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    # Stub the worker thread so the test doesn't actually fire up Telethon.
    from src.gui.services import telegram_session

    class _StubService:
        def __init__(self, parent=None):
            from PySide6.QtCore import QObject, Signal
            class _S(QObject):
                connected = Signal(bool)
                code_sent = Signal(str)
                signed_in = Signal()
                password_required = Signal()
                session_snapshot = Signal(str)
                me_ready = Signal(object)
                error = Signal(str, str)
            self._s = _S()
            self.connected = self._s.connected
            self.code_sent = self._s.code_sent
            self.signed_in = self._s.signed_in
            self.password_required = self._s.password_required
            self.session_snapshot = self._s.session_snapshot
            self.me_ready = self._s.me_ready
            self.error = self._s.error
            self.calls: list = []

        def start_thread(self): pass
        def stop_thread(self): pass
        def connect(self, api_id, api_hash, db_path=None):
            self.calls.append(("connect", api_id, api_hash))
        def send_code(self, phone): self.calls.append(("send_code", phone))
        def sign_in(self, phone, code, h): self.calls.append(("sign_in", code))
        def sign_in_with_password(self, p): self.calls.append(("password", p))
        def fetch_me(self): self.calls.append(("fetch_me",))
        def snapshot_session(self): self.calls.append(("snapshot",))

    monkeypatch.setattr(
        telegram_session, "TelegramSessionService", _StubService,
    )

    from src.gui.views.v2_config_view import _AddAccountDialog
    from PySide6.QtWidgets import QDialogButtonBox
    dlg = _AddAccountDialog()
    qtbot.addWidget(dlg)
    dlg.show()  # setRowVisible only reflects in widget.isVisible() once shown
    qapp.processEvents()
    assert dlg._name.isEnabled()
    assert dlg._phone.isEnabled()
    # Code + 2FA rows are HIDDEN until Telegram asks for them.
    assert not dlg._code.isVisible()
    assert not dlg._password.isVisible()
    ok_btn = dlg._btns.button(QDialogButtonBox.StandardButton.Ok)
    assert not ok_btn.isEnabled()
    dlg.close()


def test_dialog_reveals_code_row_when_code_sent(
    qapp, qtbot, monkeypatch, tmp_path: Path,
):
    """`_on_code_sent` reveals the Code row + leaves 2FA hidden."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from src.gui.services import telegram_session

    class _StubService:
        def __init__(self, parent=None):
            from PySide6.QtCore import QObject, Signal
            class _S(QObject):
                connected = Signal(bool); code_sent = Signal(str)
                signed_in = Signal(); password_required = Signal()
                session_snapshot = Signal(str); me_ready = Signal(object)
                error = Signal(str, str)
            self._s = _S()
            for sig in ("connected", "code_sent", "signed_in",
                        "password_required", "session_snapshot",
                        "me_ready", "error"):
                setattr(self, sig, getattr(self._s, sig))
        def start_thread(self): pass
        def stop_thread(self): pass

    monkeypatch.setattr(
        telegram_session, "TelegramSessionService", _StubService,
    )

    from src.gui.views.v2_config_view import _AddAccountDialog
    dlg = _AddAccountDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    qapp.processEvents()
    assert not dlg._code.isVisible()

    dlg._on_code_sent("hash_abc123")
    qapp.processEvents()
    assert dlg._code.isVisible()
    assert dlg._code.isEnabled()
    # 2FA still hidden — Telegram hasn't asked yet.
    assert not dlg._password.isVisible()
    dlg.close()


def test_dialog_reveals_password_row_when_required(
    qapp, qtbot, monkeypatch, tmp_path: Path,
):
    """`_on_password_required` reveals the 2FA password row."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from src.gui.services import telegram_session

    class _StubService:
        def __init__(self, parent=None):
            from PySide6.QtCore import QObject, Signal
            class _S(QObject):
                connected = Signal(bool); code_sent = Signal(str)
                signed_in = Signal(); password_required = Signal()
                session_snapshot = Signal(str); me_ready = Signal(object)
                error = Signal(str, str)
            self._s = _S()
            for sig in ("connected", "code_sent", "signed_in",
                        "password_required", "session_snapshot",
                        "me_ready", "error"):
                setattr(self, sig, getattr(self._s, sig))
        def start_thread(self): pass
        def stop_thread(self): pass

    monkeypatch.setattr(
        telegram_session, "TelegramSessionService", _StubService,
    )

    from src.gui.views.v2_config_view import _AddAccountDialog
    dlg = _AddAccountDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    qapp.processEvents()
    assert not dlg._password.isVisible()

    dlg._on_password_required()
    qapp.processEvents()
    assert dlg._password.isVisible()
    assert dlg._password.isEnabled()
    dlg.close()


def test_dialog_apply_writes_session_file_and_appends_account(
    qapp, qtbot, monkeypatch, tmp_path: Path,
):
    """Simulate a successful sign-in by directly setting the dialog's
    captured blob; apply() must write the file + return a ConfigV2 with
    the new Account whose session_path points at the file."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    # Stub the service so dialog construction doesn't start a real thread.
    from src.gui.services import telegram_session

    class _StubService:
        def __init__(self, parent=None):
            from PySide6.QtCore import QObject, Signal
            class _S(QObject):
                connected = Signal(bool); code_sent = Signal(str)
                signed_in = Signal(); password_required = Signal()
                session_snapshot = Signal(str); me_ready = Signal(object)
                error = Signal(str, str)
            self._s = _S()
            for sig in ("connected", "code_sent", "signed_in",
                        "password_required", "session_snapshot",
                        "me_ready", "error"):
                setattr(self, sig, getattr(self._s, sig))
        def start_thread(self): pass
        def stop_thread(self): pass

    monkeypatch.setattr(
        telegram_session, "TelegramSessionService", _StubService,
    )

    from src.gui.views.v2_config_view import _AddAccountDialog
    dlg = _AddAccountDialog()
    qtbot.addWidget(dlg)
    qapp.processEvents()
    dlg._name.setText("Primary")
    dlg._phone.setText("+14155551234")
    dlg._session_blob = "STUB_BLOB_FROM_TELETHON"

    new_cfg = dlg.apply(ConfigV2())

    # Account row appended.
    assert len(new_cfg.accounts) == 1
    acc = new_cfg.accounts[0]
    assert acc.name == "Primary"
    assert acc.phone == "+14155551234"
    # session_path points at a real file containing the blob.
    sess_file = Path(acc.session_path)
    assert sess_file.exists()
    assert sess_file.read_text(encoding="utf-8") == "STUB_BLOB_FROM_TELETHON"
    # File lives under the expected APPDATA subdirectory.
    expected_dir = tmp_path / "appdata" / "CopyTrades" / "accounts"
    assert sess_file.parent == expected_dir

    dlg.close()


def test_dialog_apply_raises_when_session_not_captured(
    qapp, qtbot, monkeypatch, tmp_path: Path,
):
    """If the operator clicks OK before signing in (shouldn't be possible
    — OK is disabled — but defensive guard anyway), apply raises."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from src.gui.services import telegram_session

    class _StubService:
        def __init__(self, parent=None):
            from PySide6.QtCore import QObject, Signal
            class _S(QObject):
                connected = Signal(bool); code_sent = Signal(str)
                signed_in = Signal(); password_required = Signal()
                session_snapshot = Signal(str); me_ready = Signal(object)
                error = Signal(str, str)
            self._s = _S()
            for sig in ("connected", "code_sent", "signed_in",
                        "password_required", "session_snapshot",
                        "me_ready", "error"):
                setattr(self, sig, getattr(self._s, sig))
        def start_thread(self): pass
        def stop_thread(self): pass

    monkeypatch.setattr(
        telegram_session, "TelegramSessionService", _StubService,
    )
    from src.gui.views.v2_config_view import _AddAccountDialog
    import pytest
    dlg = _AddAccountDialog()
    qtbot.addWidget(dlg)
    qapp.processEvents()
    dlg._name.setText("Primary")
    with pytest.raises(ValueError, match="did not complete"):
        dlg.apply(ConfigV2())
    dlg.close()
