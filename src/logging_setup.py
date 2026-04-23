"""Shared logging configuration.

Each entrypoint (bot, listener, api) calls `configure_logging(name)` once at
startup. Output is tee'd to stderr AND to `logs/<name>.log` (rotated at 10MB
x 5 backups) so forensic history survives console closures and crashes.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from src.config import LOGS_DIR

_FORMAT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5


def configure_logging(name: str, level: int = logging.INFO) -> logging.Logger:
    """Configure the root logger with stderr + rotating file handler.

    Idempotent: repeated calls in the same process re-use existing handlers
    so test runners that import entrypoints multiple times don't duplicate
    output.
    """
    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(_FORMAT)
    log_path = LOGS_DIR / f"{name}.log"

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

    return logging.getLogger(name)


def http_logger(name: str = "api_http") -> logging.Logger:
    """Isolated logger for per-request HTTP access logs.

    Kept separate from the main app log so `logs/api_http.log` is a clean,
    greppable access trail (one line per request, no mixed app-log noise).
    Does not propagate to root.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fh = RotatingFileHandler(
        LOGS_DIR / f"{name}.log",
        maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(fh)
    return logger
