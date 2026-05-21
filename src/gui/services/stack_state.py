"""Read/write %APPDATA%/CopyTrades/state.json (last-used stack, geometry)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path


from dataclasses import field


@dataclass(frozen=True)
class AppState:
    last_stack: str | None = None
    window_geometry: str | None = None
    last_active_view: str = "live"
    theme: str = "dark"
    # Per-stack persisted UI state. Currently only the Actions panel's
    # filter selections live here, keyed by stack name. The value is a
    # plain dict so adding new persisted fields doesn't require a
    # migration — unknown keys are tolerated by callers.
    actions_filters: dict = field(default_factory=dict)


def state_path() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / "CopyTrades" / "state.json"


def load_state() -> AppState:
    path = state_path()
    if not path.exists():
        return AppState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppState()
    af = data.get("actions_filters")
    if not isinstance(af, dict):
        af = {}
    return AppState(
        last_stack=data.get("last_stack"),
        window_geometry=data.get("window_geometry"),
        last_active_view=data.get("last_active_view", "live"),
        theme=data.get("theme", "dark"),
        actions_filters=af,
    )


def save_state(state: AppState) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
