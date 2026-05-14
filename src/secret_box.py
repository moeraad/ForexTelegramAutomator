"""Windows DPAPI wrappers for encrypting secrets at rest.

Uses CRYPTPROTECT_LOCAL_MACHINE so the secrets can be decrypted by any
account on this machine — required because services run under
LocalSystem, not the interactive user. Tradeoff: any admin on the box
can decrypt (acceptable since admin already owns the user account too).
Plain-string in, base64-string out so the ciphertext can sit in a
SQLite TEXT column.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


_CRYPTPROTECT_UI_FORBIDDEN = 0x01
_CRYPTPROTECT_LOCAL_MACHINE = 0x04
_DPAPI_FLAGS = _CRYPTPROTECT_UI_FORBIDDEN | _CRYPTPROTECT_LOCAL_MACHINE

_crypt32 = ctypes.windll.crypt32
_kernel32 = ctypes.windll.kernel32


def _blob_from(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))


def _blob_to_bytes(blob: _DATA_BLOB) -> bytes:
    if not blob.cbData:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free(blob: _DATA_BLOB) -> None:
    if blob.pbData:
        _kernel32.LocalFree(blob.pbData)


def encrypt(plaintext: str) -> str:
    """Return base64 ciphertext usable in SQLite TEXT columns."""
    if not plaintext:
        return ""
    src = _blob_from(plaintext.encode("utf-8"))
    out = _DATA_BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(src),
        "copytrades-secret",
        None,
        None,
        None,
        _DPAPI_FLAGS,
        ctypes.byref(out),
    )
    if not ok:
        raise OSError(f"CryptProtectData failed: {ctypes.get_last_error()}")
    try:
        return base64.b64encode(_blob_to_bytes(out)).decode("ascii")
    finally:
        _free(out)


def decrypt(ciphertext: str) -> str:
    """Inverse of encrypt(). Returns '' for empty input."""
    if not ciphertext:
        return ""
    raw = base64.b64decode(ciphertext.encode("ascii"))
    src = _blob_from(raw)
    out = _DATA_BLOB()
    description = ctypes.c_wchar_p()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(src),
        ctypes.byref(description),
        None,
        None,
        None,
        _DPAPI_FLAGS,
        ctypes.byref(out),
    )
    if not ok:
        raise OSError(f"CryptUnprotectData failed: {ctypes.get_last_error()}")
    try:
        return _blob_to_bytes(out).decode("utf-8")
    finally:
        _free(out)
        if description:
            _kernel32.LocalFree(description)
