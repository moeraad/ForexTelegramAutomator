"""Discover available CopyTrades stacks (channel profile + project + ports)."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from src.config import API_HOST, API_PORT, BASE_DIR


# Port fallbacks when a stack's project .env does not specify API_PORT.
# These mirror the values used in services/install_services.bat descriptions
# (Arabic stack on 8765, SMC stack on 8766).
_PORT_FALLBACKS: dict[str, int] = {
    "fxengineer-gold": 8765,
    "SMC": 8766,
}


@dataclass(frozen=True)
class Stack:
    name: str
    profile_path: Path
    project_path: Path
    db_path: Path
    api_host: str
    api_port: int
    service_names: tuple[str, str, str]

    @property
    def api_url(self) -> str:
        return f"http://{self.api_host}:{self.api_port}"


def _name_upper(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", name).upper()


def _derive_service_names(name: str) -> tuple[str, str, str]:
    # Auto-derived. install_services.bat used CT-SMC-* and CT-AR-* as
    # operator-picked aliases; stacks_config.json overrides if needed.
    n = _name_upper(name)
    return (f"CT-{n}-Api", f"CT-{n}-Bot", f"CT-{n}-Listener")


def _parse_env_file(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _default_db_path(stack_name: str) -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return Path(appdata) / "CopyTrades" / stack_name / "copytrades.db"


def _default_profile_path(stack_name: str) -> Path:
    """Channel profile lives next to the stack's DB under APPDATA."""
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return Path(appdata) / "CopyTrades" / stack_name / "profile.json"


def _stacks_config_path() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / "CopyTrades" / "stacks_config.json"


def _read_api_settings_from_stack_db(db_path: Path) -> tuple[str | None, int | None]:
    if not db_path.exists():
        return None, None
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            rows = dict(conn.execute(
                "SELECT key, value FROM settings WHERE key IN ('api_host','api_port')"
            ).fetchall())
        finally:
            conn.close()
    except sqlite3.Error:
        return None, None
    host = rows.get("api_host") or None
    port_raw = rows.get("api_port") or None
    port = int(port_raw) if port_raw and port_raw.isdigit() else None
    return host, port


def _build_stack(
    name: str,
    profile_path: Path,
    project_path: Path,
    service_names: tuple[str, str, str] | None,
    db_path_override: Path | None = None,
) -> Stack:
    db_path = db_path_override or _default_db_path(name)
    db_host, db_port = _read_api_settings_from_stack_db(db_path)
    env = _parse_env_file(project_path / ".env")
    host = db_host or env.get("API_HOST") or "127.0.0.1"
    if db_port:
        port = db_port
    else:
        port_raw = env.get("API_PORT")
        port = int(port_raw) if port_raw else _PORT_FALLBACKS.get(name, 8765)
    services = service_names or _derive_service_names(name)
    return Stack(
        name=name,
        profile_path=profile_path,
        project_path=project_path,
        db_path=db_path,
        api_host=host,
        api_port=port,
        service_names=services,
    )


def _discover_from_config(cfg_path: Path) -> list[Stack]:
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    stacks: list[Stack] = []
    for entry in data.get("stacks", []):
        name = entry["name"]
        profile_path = Path(entry["profile_path"])
        project_path = Path(entry.get("project_path", BASE_DIR))
        sn = entry.get("service_names")
        services: tuple[str, str, str] | None
        services = tuple(sn) if sn and len(sn) == 3 else None  # type: ignore[assignment]
        db_override = Path(entry["db_path"]) if entry.get("db_path") else None
        stacks.append(_build_stack(name, profile_path, project_path, services, db_override))
    return stacks


def _discover_from_channels_dir() -> list[Stack]:
    channels_dir = BASE_DIR / "channels"
    stacks: list[Stack] = []
    if not channels_dir.exists():
        return stacks
    for profile in sorted(channels_dir.glob("*.json")):
        if profile.stem.endswith("_draft"):
            continue
        stacks.append(_build_stack(profile.stem, profile, BASE_DIR, None))
    return stacks


def discover_stacks() -> list[Stack]:
    cfg = _stacks_config_path()
    if cfg.exists():
        return _discover_from_config(cfg)
    return []


def available_profiles() -> list[str]:
    """Names of channel profile templates available for new stacks."""
    channels_dir = BASE_DIR / "channels"
    if not channels_dir.exists():
        return []
    out: list[str] = []
    for profile in sorted(channels_dir.glob("*.json")):
        if profile.stem.endswith("_draft"):
            continue
        out.append(profile.stem)
    return out


def build_stack_for_new_entry(name: str, profile_name: str) -> Stack:
    """Construct a Stack object for a freshly-added stacks_config entry.

    The profile JSON lives next to the stack's DB under APPDATA so all
    per-stack files share one folder. The ``profile_name`` arg is the
    profile identifier kept for parity with older callers; new stacks
    just use the stack name.
    """
    profile_path = _default_profile_path(name)
    return _build_stack(name, profile_path, BASE_DIR, None)
