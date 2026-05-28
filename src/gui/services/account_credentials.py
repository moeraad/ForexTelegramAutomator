"""Look up Telethon credentials for a v2 Account.

Two paths the credentials might live on, checked in order:

  1. Per-account sidecar files written by the "Add Account" dialog:
     ``%APPDATA%/CopyTrades/accounts/<id>.session.txt`` (StringSession blob)
     ``%APPDATA%/CopyTrades/accounts/<id>.creds.json``  (api_id + api_hash)

  2. Legacy single-stack path: the first destination DB the account's
     channels route to, with keys ``tg_session_blob`` + ``tg_api_id``
     + ``tg_api_hash`` set by the wizard.

The helper returns a ``AccountCreds`` dataclass with all three fields,
or ``None`` when nothing usable was found. Used by the Add Channel
dialog's "Pick channel" flow + (planned) listener fallback when the
global ``config.TG_API_ID/HASH`` isn't appropriate per-account.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from src.config_v2 import Account, ConfigV2


@dataclass(frozen=True)
class AccountCreds:
    api_id: int
    api_hash: str
    session_blob: str


def _accounts_dir() -> Path:
    appdata = Path(os.environ.get("APPDATA", str(Path.home())))
    return appdata / "CopyTrades" / "accounts"


def _sidecar_creds(account_id: str) -> tuple[int | None, str]:
    """Read api_id + api_hash from the per-account sidecar JSON.

    Returns ``(None, "")`` on any failure (sidecar missing, JSON
    malformed, fields absent). Caller falls through to the legacy
    destination-DB path.
    """
    try:
        f = _accounts_dir() / f"{account_id}.creds.json"
        if not f.exists():
            return None, ""
        data = json.loads(f.read_text(encoding="utf-8"))
        api_id = int(data.get("api_id") or 0)
        api_hash = str(data.get("api_hash") or "")
        if api_id and api_hash:
            return api_id, api_hash
        return None, ""
    except Exception:
        return None, ""


def _sidecar_session(account: Account) -> str:
    """Read the StringSession blob from ``account.session_path``."""
    if not account.session_path:
        return ""
    try:
        p = Path(account.session_path)
        if not p.exists() or not p.is_file():
            return ""
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _normalize_phone(raw: str) -> str:
    """Strip non-digits so phone-matching survives formatting drift.

    The wizard stores ``+9611234567`` while ``stacks_config.json`` might
    have it as ``+961 1234567`` or ``961-1234567`` after a hand-edit.
    Normalising to digits-only makes the match robust.
    """
    return "".join(c for c in (raw or "") if c.isdigit())


def _destination_db_for_account(
    cfg: ConfigV2, account: Account,
) -> Path | None:
    """Return the first destination DB whose Telethon creds match this account.

    Resolution order (each pass returns the first match):

      1. Destinations the account's channels ALREADY route to (cheapest,
         and the wizard's natural target for migrated single-stacks).

      2. ANY destination DB in the config whose ``tg_phone`` setting
         matches ``account.phone`` (digits-only). Critical for the Add
         Channel dialog: at that moment the account may have no channels
         yet, so step 1 is empty — but the wizard already wrote creds
         to some destination DB.

      3. ANY destination DB with a non-empty ``tg_session_blob`` (last
         resort for single-account installs where ``tg_phone`` wasn't
         populated).
    """
    # Pass 1: explicit channel-route linkage.
    for ch in cfg.channels_for_account(account.id):
        for route in cfg.routes_for_channel(ch.id):
            dest = cfg.destination(route.destination_id)
            if dest is not None and dest.db_path:
                p = Path(dest.db_path)
                if p.exists():
                    return p

    from src import db_settings
    acct_phone = _normalize_phone(account.phone)

    # Pass 2: phone match across all destinations.
    if acct_phone:
        for dest in cfg.destinations:
            if not dest.db_path:
                continue
            p = Path(dest.db_path)
            if not p.exists():
                continue
            try:
                db_phone = _normalize_phone(
                    db_settings.get_str(p, "tg_phone", "")
                )
            except Exception:
                continue
            if db_phone and db_phone == acct_phone:
                return p

    # Pass 3: any destination DB that has SOME session blob.
    for dest in cfg.destinations:
        if not dest.db_path:
            continue
        p = Path(dest.db_path)
        if not p.exists():
            continue
        try:
            if db_settings.get_str(p, "tg_session_blob", ""):
                return p
        except Exception:
            continue

    return None


def _db_creds(db_path: Path) -> tuple[int | None, str, str]:
    """Read api_id / api_hash / session_blob from a destination DB."""
    try:
        from src import db_settings
        api_id_raw = db_settings.get_str(db_path, "tg_api_id", "")
        api_hash = db_settings.get_str(db_path, "tg_api_hash", "")
        session = db_settings.get_str(db_path, "tg_session_blob", "")
        api_id: int | None = None
        if api_id_raw:
            try:
                api_id = int(api_id_raw)
            except ValueError:
                api_id = None
        return api_id, api_hash, session
    except Exception:
        return None, "", ""


def load_account_credentials(
    cfg: ConfigV2, account: Account,
) -> AccountCreds | None:
    """Return ``AccountCreds`` for the account, or None when incomplete.

    Sidecar files win over destination-DB values; the operator's freshly
    added account doesn't depend on any wiring being in place yet.
    """
    api_id, api_hash = _sidecar_creds(account.id)
    session_blob = _sidecar_session(account)
    if api_id and api_hash and session_blob:
        return AccountCreds(
            api_id=api_id, api_hash=api_hash, session_blob=session_blob,
        )
    # Fall back to legacy destination-DB credentials.
    db = _destination_db_for_account(cfg, account)
    if db is None:
        return None
    db_api_id, db_api_hash, db_session = _db_creds(db)
    api_id = api_id or db_api_id
    api_hash = api_hash or db_api_hash
    session_blob = session_blob or db_session
    if api_id and api_hash and session_blob:
        return AccountCreds(
            api_id=api_id, api_hash=api_hash, session_blob=session_blob,
        )
    return None
