"""One-shot DM helper for sending notifications to the bot owner.

Used by entry points (listener / api / bot-startup) to fire a single
Telegram message without instantiating the full python-telegram-bot
Application. Direct HTTP POST to the Bot API via stdlib so this module
has no dependency on the bot process being running and adds no new
deps to the project.

Best-effort: failures are logged at WARNING and swallowed. A startup
ping is informational; the rest of the system must run regardless.
"""
from __future__ import annotations

import json
import logging
import ssl
from urllib import error, request

import certifi

from src import config

log = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org"

# Use certifi's Mozilla CA bundle instead of the OS trust store. Some Windows
# hosts have antivirus / corporate-proxy software that injects a self-signed
# root CA and re-signs every HTTPS connection. urllib's default trust path
# then rejects the chain ("self-signed certificate in certificate chain").
# Telethon and the OpenAI SDK both already use certifi and work on the same
# boxes — match their behavior here.
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def notify_owner(text: str, *, timeout: float = 5.0) -> bool:
    """POST a single message to the configured bot owner.

    Returns True on HTTP 200, False on any failure (missing config,
    network error, non-2xx response). Never raises.
    """
    token = config.TG_BOT_TOKEN
    owner = config.TG_BOT_OWNER_USER_ID
    if not token or not owner:
        log.warning("notify_owner skipped: TG_BOT_TOKEN or TG_BOT_OWNER_USER_ID missing")
        return False
    url = f"{_TG_API}/bot{token}/sendMessage"
    body = json.dumps({"chat_id": owner, "text": text}).encode("utf-8")
    req = request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            return 200 <= resp.status < 300
    except (error.URLError, TimeoutError, OSError) as exc:
        log.warning("notify_owner failed: %s", exc)
        return False
