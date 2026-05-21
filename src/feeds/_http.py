"""Shared HTTPS configuration for the feed workers.

All feed modules (cot, news_scan, etf_flows, macro) hit public HTTPS
endpoints. They previously relied on `ssl.create_default_context()`
which consults the OS cert store. On a Windows machine running the
PyInstaller-bundled `CopyTrades.exe` under an NSSM service the OS
store often isn't reachable, producing:

    [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate

Routing through `certifi`'s bundled CA pack avoids that — certifi
ships with curated root certs and the path is stable regardless of
process identity (LocalSystem / service / interactive user).

Import + use:

    from src.feeds._http import SSL_CONTEXT
    with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as r:
        ...

Falls back to `None` (i.e. urllib's own default context) when certifi
isn't installable — keeps the dev environment working even if the
operator skipped `pip install certifi`. urllib then uses the OS store,
which is fine on most non-frozen runs.
"""
from __future__ import annotations

import logging
import ssl

log = logging.getLogger(__name__)

SSL_CONTEXT: ssl.SSLContext | None
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    log.warning(
        "feeds._http: certifi not available; falling back to system cert store. "
        "Install certifi to fix SSL_VERIFY errors on Windows service hosts."
    )
    SSL_CONTEXT = None
except Exception as e:  # noqa: BLE001 — any context-build failure should not
    # break feed imports. Log and fall back; per-feed try/except handles
    # the actual cert errors anyway.
    log.warning("feeds._http: failed to build certifi SSL context: %s", e)
    SSL_CONTEXT = None
