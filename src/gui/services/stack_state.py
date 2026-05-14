"""Read/write %APPDATA%/CopyTrades/state.json (last-used stack, geometry)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass(frozen=True)
class AppState:
    last_stack: str | None = None
    window_geometry: str | None = None
    last_active_view: str = "live"


def state_path() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / "CopyTrades" / "state.json"


def load_state() -> AppState:
    path = state_path()
    if not path.exists():
        return AppState()
    data = json.loads(path.read_text(encoding="utf-8"))
    return AppState(
        last_stack=data.get("last_stack"),
        window_geometry=data.get("window_geometry"),
        last_active_view=data.get("last_active_view", "live"),
    )


def save_state(state: AppState) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
