"""Telethon worker: phone-login flow + dialog browse, exposed via Qt signals.

Owns one TelegramClient on a dedicated thread running its own asyncio
event loop. All public methods enqueue an async operation; the worker
runs it to completion and emits the result via a Qt signal.

Hard constraint: Telethon is not thread-safe — every coroutine runs in
the worker's loop, never on the UI thread.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import Channel, Chat, User


@dataclass(frozen=True)
class Dialog:
    id: int
    title: str
    kind: str
    unread_count: int
    is_megagroup: bool
    is_broadcast: bool


@dataclass(frozen=True)
class Me:
    id: int
    first_name: str
    last_name: str
    phone: str
    username: str


def sessions_dir() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    d = Path(appdata) / "CopyTrades" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def session_path(stack_name: str) -> Path:
    return sessions_dir() / f"{stack_name}.session"


def telegram_settings_path() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / "CopyTrades" / "telegram.json"


def load_remembered_credentials() -> tuple[int | None, str, str]:
    p = telegram_settings_path()
    if not p.exists():
        return None, "", ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, "", ""
    api_id = data.get("api_id")
    return (
        int(api_id) if isinstance(api_id, int) else None,
        str(data.get("api_hash", "")),
        str(data.get("last_phone", "")),
    )


def save_remembered_credentials(api_id: int, api_hash: str, phone: str) -> None:
    p = telegram_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {"api_id": api_id, "api_hash": api_hash, "last_phone": phone},
            indent=2,
        ),
        encoding="utf-8",
    )


def _classify_dialog(entity) -> tuple[str, bool, bool]:
    if isinstance(entity, Channel):
        if entity.megagroup:
            return "supergroup", True, False
        if entity.broadcast:
            return "channel", False, True
        return "channel", False, False
    if isinstance(entity, Chat):
        return "group", False, False
    if isinstance(entity, User):
        return ("bot" if entity.bot else "user"), False, False
    return "unknown", False, False


class TelegramSessionService(QObject):
    """Qt-signal facade over a Telethon client.

    Signals fire on the Qt thread via Qt's auto cross-thread queuing.
    """

    connected = Signal(bool)
    code_sent = Signal(str)
    signed_in = Signal()
    password_required = Signal()
    signed_out = Signal()
    me_ready = Signal(object)
    dialogs_ready = Signal(list)
    error = Signal(str, str)
    disconnected = Signal()
    # Step "Add Account with Telethon" — returns the in-memory StringSession
    # blob to the caller so it can persist it wherever it wants (per-account
    # file, destination DB, etc.) without coupling this service to v2 config.
    session_snapshot = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: TelegramClient | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._db_path: Path | None = None

    def start_thread(self) -> None:
        if self._thread is not None:
            return
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._thread_main, daemon=False, name="telegram-worker"
        )
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop_thread(self) -> None:
        if self._loop is None or self._thread is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        try:
            fut.result(timeout=5.0)
        except Exception:
            pass
        self._thread.join(timeout=3.0)
        self._thread = None
        self._loop = None
        self._client = None

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    async def _shutdown(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        loop = asyncio.get_event_loop()
        loop.stop()

    def connect(
        self, api_id: int, api_hash: str,
        db_path: Path | None = None, session_blob: str = "",
    ) -> None:
        """Connect / re-connect the Telethon client.

        ``db_path`` is optional: when provided, ``_persist_session_blob``
        writes the StringSession into that DB's ``settings.tg_session_blob``
        after a successful sign-in (the stack-setup wizard's path). When
        None, the Add-Account dialog flow uses ``snapshot_session`` instead
        to grab the blob and save it wherever it wants.

        ``session_blob`` is optional: when given, ``_connect`` instantiates
        the Telethon client with this StringSession directly — used by
        the Add Channel dialog's channel-picker flow to re-attach an
        already-authed account without touching a DB.
        """
        self._db_path = db_path
        self._session_blob_override = session_blob
        self._submit(self._connect(api_id, api_hash))

    def snapshot_session(self) -> None:
        """Grab the current StringSession blob and emit ``session_snapshot``.

        Used by the Add-Account dialog after sign-in. Emits an empty
        string when not connected / not authorized so the caller's slot
        can show a friendly error rather than fall into a None.
        """
        self._submit(self._snapshot_session())

    def send_code(self, phone: str) -> None:
        self._submit(self._send_code(phone))

    def sign_in(self, phone: str, code: str, phone_code_hash: str) -> None:
        self._submit(self._sign_in(phone, code, phone_code_hash))

    def sign_in_with_password(self, password: str) -> None:
        self._submit(self._sign_in_with_password(password))

    def fetch_me(self) -> None:
        self._submit(self._fetch_me())

    def list_dialogs(self, limit: int = 200) -> None:
        self._submit(self._list_dialogs(limit))

    def sign_out(self) -> None:
        self._submit(self._sign_out())

    def _submit(self, coro) -> None:
        if self._loop is None:
            self.error.emit("not_started", "telegram worker thread not started")
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _connect(self, api_id: int, api_hash: str) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        try:
            from src import db_settings
            # Caller-supplied blob (Add Channel re-attach flow) wins;
            # otherwise read from db_path (wizard flow); otherwise empty
            # (Add Account flow, where sign-in produces the first blob).
            existing = getattr(self, "_session_blob_override", "") or ""
            if not existing and self._db_path is not None and self._db_path.exists():
                existing = db_settings.get_str(self._db_path, "tg_session_blob", "")
            self._client = TelegramClient(StringSession(existing), api_id, api_hash)
            await self._client.connect()
            authorized = await self._client.is_user_authorized()
            self.connected.emit(bool(authorized))
        except ApiIdInvalidError:
            self.error.emit("api_id_invalid", "API ID / Hash is invalid. Check my.telegram.org.")
        except Exception as e:
            self.error.emit("connect_failed", f"{type(e).__name__}: {e}")

    async def _send_code(self, phone: str) -> None:
        if self._client is None:
            self.error.emit("not_connected", "call connect first")
            return
        try:
            result = await self._client.send_code_request(phone)
            self.code_sent.emit(result.phone_code_hash)
        except PhoneNumberInvalidError:
            self.error.emit("phone_invalid", "Phone number format is invalid. Use +<country><number>.")
        except FloodWaitError as e:
            self.error.emit("flood", f"Telegram is rate-limiting. Wait {e.seconds}s and try again.")
        except Exception as e:
            self.error.emit("send_code_failed", f"{type(e).__name__}: {e}")

    def _persist_session_blob(self) -> None:
        if self._client is None or self._db_path is None:
            return
        try:
            from src import db_settings
            blob = self._client.session.save()
            if blob:
                db_settings.set_str(self._db_path, "tg_session_blob", blob)
        except Exception:
            pass

    async def _sign_in(self, phone: str, code: str, phone_code_hash: str) -> None:
        if self._client is None:
            self.error.emit("not_connected", "call connect first")
            return
        try:
            await self._client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            self._persist_session_blob()
            self.signed_in.emit()
        except SessionPasswordNeededError:
            self.password_required.emit()
        except PhoneCodeInvalidError:
            self.error.emit("code_invalid", "Login code is incorrect.")
        except PhoneCodeExpiredError:
            self.error.emit("code_expired", "Login code has expired. Request a new one.")
        except Exception as e:
            self.error.emit("sign_in_failed", f"{type(e).__name__}: {e}")

    async def _sign_in_with_password(self, password: str) -> None:
        if self._client is None:
            self.error.emit("not_connected", "call connect first")
            return
        try:
            await self._client.sign_in(password=password)
            self._persist_session_blob()
            self.signed_in.emit()
        except Exception as e:
            self.error.emit("password_failed", f"{type(e).__name__}: {e}")

    async def _fetch_me(self) -> None:
        if self._client is None:
            self.error.emit("not_connected", "call connect first")
            return
        try:
            u = await self._client.get_me()
            if u is None:
                self.error.emit("not_authorized", "no logged-in user")
                return
            me = Me(
                id=int(u.id),
                first_name=str(u.first_name or ""),
                last_name=str(u.last_name or ""),
                phone="+" + str(u.phone) if u.phone else "",
                username=str(u.username or ""),
            )
            self.me_ready.emit(me)
        except Exception as e:
            self.error.emit("me_failed", f"{type(e).__name__}: {e}")

    async def _list_dialogs(self, limit: int) -> None:
        if self._client is None:
            self.error.emit("not_connected", "call connect first")
            return
        try:
            out: list[Dialog] = []
            async for dlg in self._client.iter_dialogs(limit=limit):
                kind, is_megagroup, is_broadcast = _classify_dialog(dlg.entity)
                out.append(Dialog(
                    id=int(dlg.id),
                    title=str(dlg.name or "(no title)"),
                    kind=kind,
                    unread_count=int(dlg.unread_count or 0),
                    is_megagroup=is_megagroup,
                    is_broadcast=is_broadcast,
                ))
            self.dialogs_ready.emit(out)
        except FloodWaitError as e:
            self.error.emit("flood", f"Telegram is rate-limiting. Wait {e.seconds}s and try again.")
        except Exception as e:
            self.error.emit("dialogs_failed", f"{type(e).__name__}: {e}")

    async def _snapshot_session(self) -> None:
        """Worker-thread side: serialize the current StringSession + emit."""
        if self._client is None:
            self.session_snapshot.emit("")
            return
        try:
            blob = self._client.session.save() or ""
            self.session_snapshot.emit(blob)
        except Exception as e:
            self.error.emit("snapshot_failed", f"{type(e).__name__}: {e}")
            self.session_snapshot.emit("")

    async def _sign_out(self) -> None:
        if self._client is None:
            self.signed_out.emit()
            return
        try:
            await self._client.log_out()
        except Exception:
            pass
        try:
            await self._client.disconnect()
        except Exception:
            pass
        self._client = None
        if self._db_path is not None and self._db_path.exists():
            try:
                from src import db_settings
                db_settings.set_str(self._db_path, "tg_session_blob", "")
            except Exception:
                pass
        self.signed_out.emit()
