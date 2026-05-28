"""Per-account session-file fallback tests.

The Add-Account dialog writes the StringSession blob to
``account.session_path``. The listener reads it as a fallback when the
destination DB has no ``tg_session_blob`` yet.
"""
from __future__ import annotations

from pathlib import Path

from src.config_v2 import Account
from src.shared_listener import _read_session_from_account_file


def _account(session_path: str = "") -> Account:
    return Account(
        id="acc_a", name="A", phone="+1",
        session_path=session_path,
        service_name="CT-Listener-acc_a",
    )


def test_returns_blob_when_file_exists(tmp_path: Path):
    sess = tmp_path / "acc_a.session.txt"
    sess.write_text("BLOBBYBLOB", encoding="utf-8")
    blob = _read_session_from_account_file(_account(str(sess)))
    assert blob == "BLOBBYBLOB"


def test_returns_empty_when_session_path_blank():
    assert _read_session_from_account_file(_account("")) == ""


def test_returns_empty_when_file_missing(tmp_path: Path):
    missing = tmp_path / "no-such-file.session.txt"
    assert _read_session_from_account_file(_account(str(missing))) == ""


def test_returns_empty_when_path_is_directory(tmp_path: Path):
    # Pointing at a directory shouldn't crash — return empty.
    assert _read_session_from_account_file(_account(str(tmp_path))) == ""


def test_strips_trailing_whitespace(tmp_path: Path):
    """Operator might accidentally save with a trailing newline; strip it
    so the StringSession constructor doesn't barf."""
    sess = tmp_path / "acc_a.session.txt"
    sess.write_text("  BLOB  \n\n", encoding="utf-8")
    assert _read_session_from_account_file(_account(str(sess))) == "BLOB"
