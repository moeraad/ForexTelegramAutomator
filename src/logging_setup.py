"""Centralized logging — three files in logs/ optimized for debugging.

  logs/system.log     — process events, errors, warnings, HTTP errors only.
                        All 3 long-running processes share this file.
  logs/trades.log     — domain timeline: action lifecycle + position events.
                        Structured single-line format for grep.
  logs/ai_calls.jsonl — every AI call with usage + raw response. Managed
                        separately by src.ai.log_call (NDJSON, not stdlib).

Every long-running entrypoint calls configure_logging(name) once at startup.
Domain modules import trades_log() to emit lifecycle events.

Noise suppression: httpx and Telethon's network/mtproto loggers default to
INFO and drown out the signal with one line per request/connect. Demoted
to WARNING here so system.log stays useful.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from src.config import LOGS_DIR

_FORMAT_SYSTEM = "%(asctime)s [%(name)s] %(levelname)s %(message)s"
_FORMAT_TRADES = "%(asctime)s %(message)s"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5

# Loggers whose default INFO output is pure noise for this app. httpx logs one
# line per OpenAI call; Telethon's network layer logs every reconnect probe.
# We already capture the same information in ai_calls.jsonl and listener
# warnings, so demote these to WARNING.
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "telethon.network.mtprotosender",
    "telethon.network.connection",
    "telethon.network.connection.tcpfull",
    "telethon.crypto",
    "telethon.extensions.messagepacker",
)


def configure_logging(name: str, level: int = logging.INFO) -> logging.Logger:
    """Route the root logger to stderr + logs/system.log.

    Idempotent: repeated calls in the same process re-use existing handlers
    so test runners that import entrypoints multiple times don't duplicate
    output.

    The `name` argument is the logger returned to the caller (used as the
    `[name]` tag in log lines); the file destination is always system.log
    regardless of which process called us.
    """
    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(_FORMAT_SYSTEM)
    log_path = LOGS_DIR / "system.log"

    has_stream = any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, RotatingFileHandler)
        for h in root.handlers
    )
    has_file = any(
        isinstance(h, RotatingFileHandler)
        and getattr(h, "baseFilename", "") == str(log_path)
        for h in root.handlers
    )

    if not has_stream:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)

    if not has_file:
        fh = RotatingFileHandler(
            log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)

    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger(name)


def trades_log() -> logging.Logger:
    """Domain-event logger writing to logs/trades.log.

    Use for lifecycle and position events. Callers emit a single structured
    line per event so the file is grep-friendly:

        trades = trades_log()
        trades.info("action_inserted msg_id=42 action_id=101 type=OPEN status=pending")
        trades.info("position_opened ticket=555 side=BUY vol=0.10 entry=4862")
        trades.info("position_closed ticket=555 reason=tp")

    Does NOT propagate to root, so events don't double-log into system.log.
    Idempotent across repeated calls.
    """
    logger = logging.getLogger("trades")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fh = RotatingFileHandler(
        LOGS_DIR / "trades.log",
        maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter(_FORMAT_TRADES))
    logger.addHandler(fh)
    return logger
