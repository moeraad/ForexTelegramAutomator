"""6-page setup wizard: AI + Bot + Telegram credentials + channel picker.

  Step 1  AI provider + API key
  Step 2  Telegram bot token (from @BotFather)
  Step 3  Telegram user API credentials (api_id, api_hash)
  Step 4  Phone number
  Step 5  Login code (and 2FA password if required)
  Step 6  Dialog picker — choose the channel to watch

Finish writes all critical settings into the stack's DB (via db_settings)
and saves the .session file at %APPDATA%/CopyTrades/sessions/<stack>.session.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from src import config, db_settings
from src.gui.services.stack_registry import Stack
from src.gui.services.telegram_session import (
    Dialog,
    Me,
    TelegramSessionService,
    load_remembered_credentials,
    save_remembered_credentials,
    session_path,
)


@dataclass
class _Collected:
    ai_provider: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    tg_bot_token: str = ""
    api_id: int = 0
    api_hash: str = ""
    phone: str = ""
    session_name: str = ""
    session_file: Path | None = None
    phone_code_hash: str = ""
    selected_chat_id: int = 0
    selected_chat_title: str = ""
    me: Me | None = None


_PAGE_WELCOME = 0
_PAGE_STACK_IDENTITY = 1
_PAGE_AI = 2
_PAGE_BOT = 3
_PAGE_CREDENTIALS = 4
_PAGE_PHONE = 5
_PAGE_CODE = 6
_PAGE_DIALOGS = 7
_PAGE_SERVICES = 8
_PAGE_DONE = 9


class TelegramWizard(QWizard):
    """Setup wizard. Stack-aware:
      - stack=None: full first-launch flow (welcome + stack identity + ...)
      - stack=existing: re-configure flow (skips welcome + identity)
    """

    def __init__(self, stack: Stack | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        title_suffix = stack.name if stack is not None else "setup"
        self.setWindowTitle(f"CopyTrades — {title_suffix}")
        self.resize(640, 540)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setOption(QWizard.WizardOption.NoDefaultButton, True)
        self.setOption(QWizard.WizardOption.HaveFinishButtonOnEarlyPages, False)
        # Promote Next + Finish to PrimaryPushButton so the wizard's
        # forward affordance matches every other primary save action
        # in the app (REVIEW.md §3 Component fit). Falls back silently
        # if qfluentwidgets isn't importable.
        try:
            from qfluentwidgets import PrimaryPushButton
            self.setButton(QWizard.WizardButton.NextButton, PrimaryPushButton("Next"))
            self.setButton(QWizard.WizardButton.FinishButton, PrimaryPushButton("Finish"))
        except Exception:
            pass

        self.stack: Stack | None = stack
        self.data = _Collected(session_name=stack.name if stack else "")
        self.data.session_file = stack.db_path if stack else None
        self.service = TelegramSessionService(self)
        self.service.error.connect(self._on_service_error)
        self.service.start_thread()

        self.setPage(_PAGE_WELCOME, _WelcomePage())
        self.setPage(_PAGE_STACK_IDENTITY, _StackIdentityPage())
        self.setPage(_PAGE_AI, _AIProviderPage())
        self.setPage(_PAGE_BOT, _BotTokenPage())
        self.setPage(_PAGE_CREDENTIALS, _CredentialsPage())
        self.setPage(_PAGE_PHONE, _PhonePage())
        self.setPage(_PAGE_CODE, _CodePage())
        self.setPage(_PAGE_DIALOGS, _DialogsPage())
        self.setPage(_PAGE_SERVICES, _ServicesInstallPage())
        self.setPage(_PAGE_DONE, _DonePage())

        self.setStartId(_PAGE_WELCOME if stack is None else _PAGE_AI)
        self.finished.connect(self._on_finished)

    def _on_finished(self, code: int) -> None:
        if code == int(QWizard.DialogCode.Accepted):
            self._persist()
        self.service.stop_thread()

    def _persist(self) -> None:
        d = self.data
        if self.stack is None:
            return  # nothing to persist if the user cancelled before identity page
        save_remembered_credentials(d.api_id, d.api_hash, d.phone)
        db_path = self.stack.db_path
        if not db_path.parent.exists():
            db_path.parent.mkdir(parents=True, exist_ok=True)
        session_token = "(db_blob)"  # session lives in DB now; this token is a marker only
        owner_id = int(d.me.id) if d.me is not None else 0
        writes: list[tuple[str, str, str]] = [
            ("ai_provider", d.ai_provider, "str"),
            ("tg_bot_token", d.tg_bot_token, "str"),
            ("tg_api_id", str(d.api_id), "str"),
            ("tg_api_hash", d.api_hash, "str"),
            ("tg_phone", d.phone, "str"),
            ("tg_session_name", session_token, "str"),
            ("tg_watched_chat_id", str(d.selected_chat_id), "str"),
            ("tg_bot_owner_user_id", str(owner_id), "str"),
        ]
        if d.anthropic_api_key:
            writes.append(("anthropic_api_key", d.anthropic_api_key, "str"))
        if d.openai_api_key:
            writes.append(("openai_api_key", d.openai_api_key, "str"))
        try:
            for key, value, _kind in writes:
                db_settings.set_str(db_path, key, value)
        except Exception as e:
            QMessageBox.critical(self, "Save failed", f"{type(e).__name__}: {e}")
            return
        config.invalidate_cache()
        QMessageBox.information(
            self,
            "Setup complete",
            f"Saved settings for {self.stack.name}.\n\n"
            f"Channel: {d.selected_chat_title} ({d.selected_chat_id})\n\n"
            "Open Settings to start the services.",
        )

    def _on_service_error(self, _kind: str, msg: str) -> None:
        page = self.currentPage()
        if isinstance(page, _BasePage):
            page.show_error(msg)


class _BasePage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self._error = QLabel("")
        self._error.setStyleSheet("color: #ef5350;")
        self._error.setWordWrap(True)
        self._error.setVisible(False)

    def show_error(self, msg: str) -> None:
        self._error.setText(msg)
        self._error.setVisible(True)

    def clear_error(self) -> None:
        self._error.setVisible(False)
        self._error.setText("")

    def wiz(self) -> "TelegramWizard":
        return self.wizard()  # type: ignore[return-value]


class _CredentialsPage(_BasePage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("API credentials")
        self.setSubTitle(
            "Get these from https://my.telegram.org → API development tools "
            "(takes 2 minutes, one-time)."
        )

        self._api_id = QLineEdit()
        self._api_id.setPlaceholderText("e.g. 12345678")
        self._api_hash = QLineEdit()
        self._api_hash.setPlaceholderText("32 hex chars")
        self._api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self._reveal = QPushButton("show")
        self._reveal.setCheckable(True)
        self._reveal.setFixedWidth(64)
        self._reveal.toggled.connect(self._toggle_reveal)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("API ID"))
        layout.addWidget(self._api_id)
        layout.addWidget(QLabel("API Hash"))
        hash_row = QHBoxLayout()
        hash_row.addWidget(self._api_hash, 1)
        hash_row.addWidget(self._reveal)
        layout.addLayout(hash_row)
        layout.addStretch()
        layout.addWidget(self._error)

    def _toggle_reveal(self, on: bool) -> None:
        self._api_hash.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        self._reveal.setText("hide" if on else "show")

    def initializePage(self) -> None:
        api_id, api_hash, _phone = load_remembered_credentials()
        if api_id is not None:
            self._api_id.setText(str(api_id))
        if api_hash:
            self._api_hash.setText(api_hash)
        self.clear_error()

    def validatePage(self) -> bool:
        self.clear_error()
        try:
            api_id = int(self._api_id.text().strip())
        except ValueError:
            self.show_error("API ID must be a number.")
            return False
        api_hash = self._api_hash.text().strip()
        if len(api_hash) < 16:
            self.show_error("API Hash looks too short. Re-check my.telegram.org.")
            return False
        w = self.wiz()
        w.data.api_id = api_id
        w.data.api_hash = api_hash
        return True


class _PhonePage(_BasePage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Phone number")
        self.setSubTitle("Telegram will send a login code to this number.")

        self._phone = QLineEdit()
        self._phone.setPlaceholderText("+<country><number>, e.g. +14155551234")
        self._session = QLineEdit()
        self._send_btn = QPushButton("Send code")
        self._send_btn.clicked.connect(self._on_send)
        self._busy = QLabel("")
        # Muted-role token tracks the active palette (REVIEW.md §3 Theming).
        self._busy.setProperty("role", "muted")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Phone number"))
        layout.addWidget(self._phone)
        layout.addWidget(QLabel("Session name"))
        layout.addWidget(self._session)
        layout.addWidget(self._send_btn)
        layout.addWidget(self._busy)
        layout.addStretch()
        layout.addWidget(self._error)

        self._sent = False

    def initializePage(self) -> None:
        _api_id, _api_hash, last_phone = load_remembered_credentials()
        if last_phone:
            self._phone.setText(last_phone)
        w = self.wiz()
        self._session.setText(w.data.session_name or w.stack.name)
        self.clear_error()
        self._sent = False
        self._busy.setText("")
        w.service.connected.connect(self._on_connected)
        w.service.code_sent.connect(self._on_code_sent)
        w.service.connect(w.data.api_id, w.data.api_hash, w.stack.db_path)

    def cleanupPage(self) -> None:
        w = self.wiz()
        try:
            w.service.connected.disconnect(self._on_connected)
            w.service.code_sent.disconnect(self._on_code_sent)
        except (TypeError, RuntimeError):
            pass

    def _on_connected(self, authorized: bool) -> None:
        if authorized:
            # Foot-gun fix (REVIEW.md §4.10): previously auto-advanced to
            # the channel picker as soon as the Telethon session reported
            # already-authorized. That bounced the operator forward at an
            # unpredictable moment — e.g. if they tabbed back to verify
            # their phone — and they could miss the channel choice
            # entirely. Now we just mark the page complete and let the
            # operator click Next themselves.
            self._busy.setText(
                "already logged in - click Next to continue to channel picker"
            )
            self._sent = True
            self.completeChanged.emit()

    def _on_send(self) -> None:
        self.clear_error()
        phone = self._phone.text().strip()
        if not phone.startswith("+") or len(phone) < 5:
            self.show_error("Format: +<country><number>, e.g. +14155551234")
            return
        w = self.wiz()
        w.data.phone = phone
        w.data.session_name = self._session.text().strip() or w.stack.name
        self._busy.setText("requesting code…")
        w.service.send_code(phone)

    def _on_code_sent(self, phone_code_hash: str) -> None:
        self._busy.setText("code sent — check your Telegram app")
        self.wiz().data.phone_code_hash = phone_code_hash
        self._sent = True
        self.completeChanged.emit()
        self.wiz().next()

    def isComplete(self) -> bool:
        return self._sent


class _CodePage(_BasePage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Login code")
        self.setSubTitle("Enter the code Telegram just sent to your app.")

        self._code = QLineEdit()
        self._code.setPlaceholderText("5-digit code")
        self._password = QLineEdit()
        self._password.setPlaceholderText("2FA password (only if Telegram asks)")
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_label = QLabel("Two-factor password")
        self._password_label.setVisible(False)
        self._password.setVisible(False)

        self._submit_btn = QPushButton("Sign in")
        self._submit_btn.clicked.connect(self._on_submit)
        self._busy = QLabel("")
        # Muted-role token tracks the active palette (REVIEW.md §3 Theming).
        self._busy.setProperty("role", "muted")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Code"))
        layout.addWidget(self._code)
        layout.addWidget(self._password_label)
        layout.addWidget(self._password)
        layout.addWidget(self._submit_btn)
        layout.addWidget(self._busy)
        layout.addStretch()
        layout.addWidget(self._error)

        self._signed_in = False
        self._password_mode = False

    def initializePage(self) -> None:
        self.clear_error()
        self._signed_in = False
        self._password_mode = False
        self._password_label.setVisible(False)
        self._password.setVisible(False)
        self._password.setText("")
        self._busy.setText("")
        w = self.wiz()
        w.service.signed_in.connect(self._on_signed_in)
        w.service.password_required.connect(self._on_password_required)

    def cleanupPage(self) -> None:
        w = self.wiz()
        try:
            w.service.signed_in.disconnect(self._on_signed_in)
            w.service.password_required.disconnect(self._on_password_required)
        except (TypeError, RuntimeError):
            pass

    def _on_submit(self) -> None:
        self.clear_error()
        w = self.wiz()
        if self._password_mode:
            pwd = self._password.text()
            if not pwd:
                self.show_error("Enter your 2FA password.")
                return
            self._busy.setText("verifying password…")
            w.service.sign_in_with_password(pwd)
            return
        code = self._code.text().strip()
        if not code:
            self.show_error("Enter the code.")
            return
        self._busy.setText("signing in…")
        w.service.sign_in(w.data.phone, code, w.data.phone_code_hash)

    def _on_password_required(self) -> None:
        self._password_mode = True
        self._password_label.setVisible(True)
        self._password.setVisible(True)
        self._password.setFocus()
        self._busy.setText("Telegram requires your two-factor password.")

    def _on_signed_in(self) -> None:
        self._busy.setText("signed in")
        self._signed_in = True
        self.completeChanged.emit()
        self.wiz().next()

    def isComplete(self) -> bool:
        return self._signed_in


class _DialogsPage(_BasePage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Pick a channel")
        self.setSubTitle("Choose the channel this stack should watch.")

        self._search = QLineEdit()
        self._search.setPlaceholderText("search title")
        self._search.textChanged.connect(self._apply_filter)
        self._list = QListView()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._model = QStandardItemModel()
        self._list.setModel(self._model)
        self._me_label = QLabel("")
        self._me_label.setStyleSheet("color: #787b86;")
        self._busy = QLabel("loading dialogs…")
        self._busy.setStyleSheet("color: #787b86;")

        layout = QVBoxLayout(self)
        layout.addWidget(self._me_label)
        layout.addWidget(self._search)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._busy)
        layout.addWidget(self._error)

        self._all_dialogs: list[Dialog] = []

    def initializePage(self) -> None:
        self.clear_error()
        self._model.clear()
        self._busy.setText("loading dialogs…")
        w = self.wiz()
        w.service.me_ready.connect(self._on_me)
        w.service.dialogs_ready.connect(self._on_dialogs)
        w.service.fetch_me()
        w.service.list_dialogs(limit=300)

    def cleanupPage(self) -> None:
        w = self.wiz()
        try:
            w.service.me_ready.disconnect(self._on_me)
            w.service.dialogs_ready.disconnect(self._on_dialogs)
        except (TypeError, RuntimeError):
            pass

    def _on_me(self, me: Me) -> None:
        self.wiz().data.me = me
        bits = [b for b in [me.first_name, me.last_name] if b]
        name = " ".join(bits) or "(no name)"
        suffix = f"  ·  @{me.username}" if me.username else ""
        self._me_label.setText(f"Logged in as: {name}  ·  {me.phone}{suffix}")

    def _on_dialogs(self, dialogs: list[Dialog]) -> None:
        self._all_dialogs = dialogs
        self._busy.setText(f"{len(dialogs)} dialogs")
        self._populate(dialogs)

    def _populate(self, dialogs: list[Dialog]) -> None:
        self._model.clear()
        for d in dialogs:
            tag = "channel" if d.is_broadcast else d.kind
            label = f"{d.title}    ·  {tag}"
            if d.unread_count:
                label += f"  ·  {d.unread_count} unread"
            item = QStandardItem(label)
            item.setEditable(False)
            item.setData(d, Qt.ItemDataRole.UserRole)
            self._model.appendRow(item)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        if not needle:
            self._populate(self._all_dialogs)
            return
        filtered = [d for d in self._all_dialogs if needle in d.title.lower()]
        self._populate(filtered)

    def validatePage(self) -> bool:
        self.clear_error()
        idx = self._list.currentIndex()
        if not idx.isValid():
            self.show_error("Pick a channel from the list.")
            return False
        item = self._model.itemFromIndex(idx)
        dlg: Dialog = item.data(Qt.ItemDataRole.UserRole)
        if dlg.kind not in ("channel", "supergroup"):
            ans = QMessageBox.question(
                self, "Not a channel",
                f"'{dlg.title}' is a {dlg.kind}, not a channel. "
                "The listener works best with broadcast channels. Continue anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return False
        w = self.wiz()
        w.data.selected_chat_id = dlg.id
        w.data.selected_chat_title = dlg.title
        return True


class _AIProviderPage(_BasePage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("AI provider")
        self.setSubTitle(
            "Pick which AI service interprets channel messages. "
            "Anthropic Sonnet 4.6 is the most accurate; OpenAI is cheaper."
        )

        from PySide6.QtWidgets import QRadioButton, QButtonGroup

        self._anthropic_radio = QRadioButton("Anthropic (Claude)")
        self._openai_radio = QRadioButton("OpenAI (gpt-5)")
        self._anthropic_radio.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self._anthropic_radio)
        group.addButton(self._openai_radio)

        self._key = QLineEdit()
        self._key.setPlaceholderText("paste API key")
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._reveal = QPushButton("show")
        self._reveal.setCheckable(True)
        self._reveal.setFixedWidth(64)
        self._reveal.toggled.connect(
            lambda on: (
                self._key.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password),
                self._reveal.setText("hide" if on else "show"),
            )
        )

        layout = QVBoxLayout(self)
        layout.addWidget(self._anthropic_radio)
        layout.addWidget(self._openai_radio)
        layout.addWidget(QLabel("API key"))
        row = QHBoxLayout()
        row.addWidget(self._key, 1)
        row.addWidget(self._reveal)
        layout.addLayout(row)
        layout.addStretch()
        layout.addWidget(self._error)

    def initializePage(self) -> None:
        w = self.wiz()
        from src import db_settings
        anth = db_settings.get_str(w.stack.db_path, "anthropic_api_key")
        openai = db_settings.get_str(w.stack.db_path, "openai_api_key")
        provider = db_settings.get_str(w.stack.db_path, "ai_provider")
        if provider == "openai" or (openai and not anth):
            self._openai_radio.setChecked(True)
            self._key.setText(openai)
        else:
            self._anthropic_radio.setChecked(True)
            self._key.setText(anth)
        self.clear_error()

    def validatePage(self) -> bool:
        self.clear_error()
        key = self._key.text().strip()
        if len(key) < 16:
            self.show_error("API key looks too short.")
            return False
        provider = "anthropic" if self._anthropic_radio.isChecked() else "openai"
        ok, msg = _validate_ai_key(provider, key)
        w = self.wiz()
        if not ok:
            if msg.startswith("NETWORK:"):
                from PySide6.QtWidgets import QMessageBox
                detail = msg[len("NETWORK:"):].strip()
                reply = QMessageBox.warning(
                    self,
                    f"Can't reach {provider}",
                    f"Couldn't verify the API key because of a network problem:\n\n"
                    f"  {detail}\n\n"
                    "Save the key anyway and continue? You can re-validate "
                    "later from the Setup wizard.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return False
            else:
                self.show_error(f"Provider rejected the key: {msg}")
                return False
        if provider == "anthropic":
            w.data.ai_provider = "anthropic"
            w.data.anthropic_api_key = key
            w.data.openai_api_key = ""
        else:
            w.data.ai_provider = "openai"
            w.data.openai_api_key = key
            w.data.anthropic_api_key = ""
        return True


def _validate_ai_key(provider: str, key: str) -> tuple[bool, str]:
    import httpx
    try:
        if provider == "anthropic":
            r = httpx.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                timeout=10.0,
            )
        else:
            r = httpx.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10.0,
            )
    except httpx.TimeoutException:
        return False, "NETWORK: timed out reaching provider"
    except httpx.ConnectError as e:
        return False, f"NETWORK: can't reach provider ({e})"
    except Exception as e:
        msg = str(e)
        if "11001" in msg or "getaddrinfo failed" in msg or "Name or service" in msg:
            return False, "NETWORK: DNS lookup failed"
        return False, f"NETWORK: {type(e).__name__}: {e}"
    if 200 <= r.status_code < 300:
        return True, "ok"
    if r.status_code == 401:
        return False, "key rejected (401)"
    if r.status_code == 403:
        return False, "key lacks permission (403)"
    return False, f"http {r.status_code}"


def _validate_bot_token(token: str) -> tuple[bool, str]:
    import httpx
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        r = httpx.get(url, timeout=10.0, follow_redirects=True)
    except httpx.TimeoutException:
        return False, "NETWORK: timed out reaching api.telegram.org"
    except httpx.ConnectError as e:
        return False, f"NETWORK: can't reach api.telegram.org ({e})"
    except Exception as e:
        msg = str(e)
        if "11001" in msg or "getaddrinfo failed" in msg or "Name or service" in msg:
            return False, "NETWORK: DNS lookup failed for api.telegram.org"
        return False, f"NETWORK: {type(e).__name__}: {e}"
    try:
        body = r.json()
    except Exception:
        body = {}
    description = body.get("description") or ""
    if r.status_code == 200 and body.get("ok"):
        username = body.get("result", {}).get("username", "ok")
        return True, f"@{username}"
    if r.status_code == 401:
        return False, (
            "Telegram says the token is unauthorized. Re-open @BotFather, "
            "send /mybots, pick this bot, then 'API Token' to copy the current "
            "token. If you regenerated it, the old one is dead."
        )
    if r.status_code == 404:
        return False, (
            "Telegram says the token is malformed (404 Not Found). The token "
            "must look like 1234567890:AAEAbc... — check for missing chars or "
            "extra whitespace."
        )
    if description:
        return False, f"Telegram replied {r.status_code}: {description}"
    snippet = (r.text or "")[:200].strip()
    return False, f"unexpected reply (http {r.status_code}){': ' + snippet if snippet else ''}"


class _BotTokenPage(_BasePage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Telegram bot token")
        self.setSubTitle(
            "Create a bot via @BotFather on Telegram (5 minutes) — it sends "
            "you a token like 1234567890:AAEAbcDef... Paste it below."
        )

        self._token = QLineEdit()
        self._token.setPlaceholderText("1234567890:AAEAbcDef...")
        self._token.setEchoMode(QLineEdit.EchoMode.Password)
        self._reveal = QPushButton("show")
        self._reveal.setCheckable(True)
        self._reveal.setFixedWidth(64)
        self._reveal.toggled.connect(
            lambda on: (
                self._token.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password),
                self._reveal.setText("hide" if on else "show"),
            )
        )

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Bot token"))
        row = QHBoxLayout()
        row.addWidget(self._token, 1)
        row.addWidget(self._reveal)
        layout.addLayout(row)
        layout.addStretch()
        layout.addWidget(self._error)

    def initializePage(self) -> None:
        from src import db_settings
        w = self.wiz()
        existing = db_settings.get_str(w.stack.db_path, "tg_bot_token")
        if existing:
            self._token.setText(existing)
        self.clear_error()

    def validatePage(self) -> bool:
        self.clear_error()
        token = self._token.text().strip()
        if ":" not in token or len(token) < 30:
            self.show_error("Bot token format looks wrong. Should be NNNN:abcdef...")
            return False
        ok, msg = _validate_bot_token(token)
        if not ok:
            if msg.startswith("NETWORK:"):
                from PySide6.QtWidgets import QMessageBox
                detail = msg[len("NETWORK:"):].strip()
                reply = QMessageBox.warning(
                    self,
                    "Can't reach Telegram",
                    f"Couldn't verify the token because of a network problem:\n\n"
                    f"  {detail}\n\n"
                    "This usually means no internet, a VPN/proxy is blocking "
                    "api.telegram.org, or DNS isn't resolving.\n\n"
                    "Save the token anyway and continue? You can re-validate "
                    "later from the Setup wizard.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return False
                self.wiz().data.tg_bot_token = token
                return True
            self.show_error(f"Telegram rejected the token: {msg}")
            return False
        self.wiz().data.tg_bot_token = token
        return True


# --- Async HTTP validator -------------------------------------------------

import httpx
from PySide6.QtCore import QThread, Signal


class _ApiValidator(QThread):
    """One-shot HTTP probe to validate an API key. Emits done(ok, message)."""

    done = Signal(bool, str)

    def __init__(self, url: str, headers: dict[str, str], timeout: float = 10.0) -> None:
        super().__init__()
        self._url = url
        self._headers = headers
        self._timeout = timeout

    def run(self) -> None:
        try:
            r = httpx.get(self._url, headers=self._headers, timeout=self._timeout)
            if 200 <= r.status_code < 300:
                self.done.emit(True, "ok")
            elif r.status_code == 401:
                self.done.emit(False, "key rejected (401)")
            elif r.status_code == 403:
                self.done.emit(False, "key lacks permission (403)")
            else:
                self.done.emit(False, f"http {r.status_code}")
        except httpx.TimeoutException:
            self.done.emit(False, "timeout — network or provider is unreachable")
        except Exception as e:
            self.done.emit(False, f"{type(e).__name__}: {e}")


# --- Welcome page --------------------------------------------------------


class _WelcomePage(_BasePage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Welcome to CopyTrades")
        self.setSubTitle("This wizard will take about 10 minutes.")
        body = QLabel(
            "<p>You'll need the following ready before continuing:</p>"
            "<ul>"
            "<li><b>An AI provider API key</b> — Anthropic (Claude) or OpenAI.</li>"
            "<li><b>A Telegram bot</b> created via @BotFather (get the token).</li>"
            "<li><b>Telegram API credentials</b> from https://my.telegram.org "
            "(api_id + api_hash).</li>"
            "<li><b>The phone number</b> of the Telegram account that will read "
            "the channel.</li>"
            "<li><b>MT5 installed</b> with the CopyTrades EA compiled "
            "(handled outside this wizard — last page has the checklist).</li>"
            "</ul>"
            "<p style='color:#787b86;'>Each step validates your input before "
            "moving on. You can re-run this wizard later from Settings → Setup "
            "wizard to change any value.</p>"
        )
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setOpenExternalLinks(True)
        layout = QVBoxLayout(self)
        layout.addWidget(body)
        layout.addStretch()


# --- Stack identity page -------------------------------------------------


class _StackIdentityPage(_BasePage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Stack identity")
        self.setSubTitle(
            "A stack is one running instance (one Telegram channel + one EA). "
            "Give it a name and pick the symbol the channel trades."
        )

        self._name = QLineEdit()
        self._name.setPlaceholderText("e.g. my-gold-stack")
        self._symbol = QLineEdit()
        self._symbol.setText("XAUUSD")
        self._symbol.setPlaceholderText("XAUUSD")

        info = QLabel(
            "<span style='color:#787b86;'>The stack folder will be created at "
            "<code>%APPDATA%/CopyTrades/&lt;name&gt;/</code> with its own DB "
            "and logs. A blank channel profile will be created at "
            "<code>channels/&lt;name&gt;.json</code> — fill in the AI prompt "
            "vocabulary later from the PROFILE view in the main window.</span>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)

        from PySide6.QtWidgets import QFormLayout
        form = QFormLayout()
        form.addRow("Stack name", self._name)
        form.addRow("Symbol", self._symbol)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(form)
        layout.addStretch()
        layout.addWidget(self._error)

    def validatePage(self) -> bool:
        self.clear_error()
        name = self._name.text().strip()
        symbol = self._symbol.text().strip().upper() or "XAUUSD"
        if not name:
            self.show_error("Stack name can't be empty.")
            return False
        if any(ch in name for ch in r'\/:*?"<>|'):
            self.show_error("Stack name can't contain \\ / : * ? \" < > |")
            return False
        from src.gui.services.stacks_config_io import StackEntry, load_entries, save_entries
        from src.gui.services.stack_registry import build_stack_for_new_entry
        existing = load_entries()
        if any(e.name == name for e in existing):
            self.show_error(f"A stack named '{name}' already exists.")
            return False
        _write_blank_channel_profile(name, symbol)
        new_stack = build_stack_for_new_entry(name, name)
        new_stack.db_path.parent.mkdir(parents=True, exist_ok=True)
        existing.append(StackEntry(
            name=new_stack.name,
            profile_path=str(new_stack.profile_path),
            project_path=str(new_stack.project_path),
            db_path="",
            service_names=list(new_stack.service_names),
        ))
        save_entries(existing)
        w = self.wiz()
        w.stack = new_stack
        w.data.session_name = new_stack.name
        w.data.session_file = new_stack.db_path
        from src import db, db_settings
        with db.connect(str(new_stack.db_path)) as conn:
            db.init_schema(conn)
        db_settings.set_str(new_stack.db_path, "channel_profile", name)
        return True


def _write_blank_channel_profile(name: str, symbol: str) -> None:
    """Create the stack's blank profile JSON next to its DB under APPDATA."""
    import json
    from src.gui.services.stack_registry import _default_profile_path
    profile_path = _default_profile_path(name)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    if profile_path.exists():
        return
    template = {
        "name": name,
        "description": "",
        "symbol": symbol,
        "language": "",
        "price_range_hint": "",
        "shorthand_decode_example": "",
        "header": "",
        "vocabulary_table": "",
        "compound_messages": "",
        "commentary_filter": "",
        "directional_command_flow": "",
        "worked_examples": "",
        "triage_keep_triggers": "",
    }
    profile_path.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")


# --- Services install page -----------------------------------------------


class _ServicesInstallPage(_BasePage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Install Windows services")
        self.setSubTitle(
            "CopyTrades runs three background services (API, Bot, Listener) "
            "via NSSM. Windows will prompt for admin rights once."
        )
        self._status = QLabel("Click Install to register and start the services.")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("padding: 8px;")
        self._install_btn = QPushButton("Install + start services")
        self._install_btn.clicked.connect(self._on_install)
        self._busy = QLabel("")
        # Muted-role token tracks the active palette (REVIEW.md §3 Theming).
        self._busy.setProperty("role", "muted")
        self._done = False
        self._mgr = None

        layout = QVBoxLayout(self)
        layout.addWidget(self._status)
        layout.addWidget(self._install_btn)
        layout.addWidget(self._busy)
        layout.addStretch()
        layout.addWidget(self._error)

    def initializePage(self) -> None:
        self.clear_error()
        self._done = False
        self._busy.setText("")
        w = self.wiz()
        if w.stack is None:
            self.show_error("Internal error: stack not set.")
            return
        from src.gui.services import nssm_client
        running = sum(1 for s in w.stack.service_names if nssm_client.service_running(s))
        if running == len(w.stack.service_names):
            self._status.setText("All services are already running. Click Next.")
            self._install_btn.setEnabled(False)
            self._done = True
            self.completeChanged.emit()
            return
        exists = sum(1 for s in w.stack.service_names if nssm_client.service_exists(s))
        if exists < len(w.stack.service_names):
            self._status.setText(
                f"Stack: {w.stack.name}\nServices to register: "
                + "\n  • ".join([""] + list(w.stack.service_names))
            )
        else:
            self._status.setText(
                f"All services are registered but not running.\n"
                "Click Install + start to bring them up."
            )

    def _on_install(self) -> None:
        from src.gui.services.bootstrap import BootstrapManager
        w = self.wiz()
        if w.stack is None:
            return
        self._install_btn.setEnabled(False)
        self._busy.setText("running NSSM install + start (UAC prompts may appear)…")
        self._mgr = BootstrapManager([w.stack], parent=self)
        self._mgr.all_completed.connect(self._on_done)
        self._mgr.step_failed.connect(self._on_step_failed)
        self._mgr.start()

    def _on_done(self) -> None:
        self._busy.setText("services running ✓")
        self._done = True
        self.completeChanged.emit()

    def _on_step_failed(self, stack: str, step: str, err: str) -> None:
        self.show_error(f"{stack} · {step} · {err}")

    def isComplete(self) -> bool:
        return self._done


# --- Done page -----------------------------------------------------------


class _DonePage(_BasePage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("You're set up — one last step in MT5")
        self.setSubTitle("Finish the EA side and you're live.")

        self._summary = QLabel()
        self._summary.setTextFormat(Qt.TextFormat.RichText)
        self._summary.setWordWrap(True)
        self._summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        from PySide6.QtWidgets import QApplication
        self._copy_btn = QPushButton("Copy API URL to clipboard")
        self._copy_btn.clicked.connect(self._copy_url)

        self._copied = QLabel("")
        self._copied.setStyleSheet("color: #26a69a;")

        layout = QVBoxLayout(self)
        layout.addWidget(self._summary)
        row = QHBoxLayout()
        row.addWidget(self._copy_btn)
        row.addWidget(self._copied)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()

    def initializePage(self) -> None:
        w = self.wiz()
        from src import db_settings
        port = db_settings.get_int(w.stack.db_path, "api_port", 8765) if w.stack else 8765
        self._api_url = f"http://127.0.0.1:{port}"
        self._summary.setText(
            "<h3>MT5 EA checklist</h3>"
            "<ol>"
            "<li>Open <b>MetaEditor</b> (F4 from MT5) and compile "
            "<code>ea/CopyTrades.mq5</code> with F7.</li>"
            "<li>In MT5: drag <b>CopyTrades</b> from the Navigator onto any chart.</li>"
            "<li>In MT5 → <b>Tools → Options → Expert Advisors</b>: tick "
            "<b>Allow WebRequest for listed URL</b> and add:</li>"
            "</ol>"
            f"<p style='font-family:Consolas,monospace; background:#1e222d; "
            f"padding:8px; border-left:3px solid #26a69a;'>{self._api_url}</p>"
            "<ol start='4'>"
            "<li>Enable <b>AutoTrading</b> on the MT5 toolbar.</li>"
            "<li>Watch the EA's Experts tab — first GET on "
            f"<code>{self._api_url}/actions?status=sent</code> means it's wired up.</li>"
            "</ol>"
            "<p style='color:#787b86;'>You can re-open this wizard anytime from "
            "Settings → Setup wizard.</p>"
        )

    def _copy_url(self) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self._api_url)
        self._copied.setText("copied")
