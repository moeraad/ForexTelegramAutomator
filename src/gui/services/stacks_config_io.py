"""Read / write %APPDATA%/CopyTrades/stacks_config.json."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StackEntry:
    name: str
    profile_path: str
    project_path: str
    db_path: str = ""
    service_names: list[str] = field(default_factory=list)

    def normalized(self) -> dict:
        out: dict = {
            "name": self.name,
            "profile_path": self.profile_path,
            "project_path": self.project_path,
        }
        if self.db_path:
            out["db_path"] = self.db_path
        if self.service_names and len(self.service_names) == 3:
            out["service_names"] = list(self.service_names)
        return out


def stacks_config_path() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / "CopyTrades" / "stacks_config.json"


def load_entries() -> list[StackEntry]:
    path = stacks_config_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    out: list[StackEntry] = []
    for entry in data.get("stacks", []):
        out.append(StackEntry(
            name=str(entry.get("name", "")),
            profile_path=str(entry.get("profile_path", "")),
            project_path=str(entry.get("project_path", "")),
            db_path=str(entry.get("db_path", "")),
            service_names=list(entry.get("service_names") or []),
        ))
    return out


def save_entries(entries: list[StackEntry]) -> None:
    path = stacks_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"stacks": [e.normalized() for e in entries]}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
