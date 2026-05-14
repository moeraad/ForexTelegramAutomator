"""Retry-with-timeout probe of an API stack's /health endpoint."""
from __future__ import annotations

import time
import urllib.error
import urllib.request


def ping_once(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def ping_with_retry(
    url: str,
    total_timeout: float = 30.0,
    interval: float = 1.0,
    per_request_timeout: float = 2.0,
) -> bool:
    deadline = time.time() + total_timeout
    while time.time() < deadline:
        if ping_once(url, timeout=per_request_timeout):
            return True
        time.sleep(interval)
    return False
