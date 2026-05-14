"""Read / write KEY=VALUE entries in .env files, preserving comments and order."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


_SECRET_HINT_TOKENS = ("TOKEN", "KEY", "PASSWORD", "SECRET", "HASH")


@dataclass
class EnvLine:
    raw: str
    key: str
    value: str

    @property
    def is_kv(self) -> bool:
        return bool(self.key)


def is_secret(key: str) -> bool:
    upper = key.upper()
    return any(token in upper for token in _SECRET_HINT_TOKENS)


def parse_env(path: Path) -> list[EnvLine]:
    if not path.exists():
        return []
    out: list[EnvLine] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(EnvLine(raw=raw, key="", value=""))
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        out.append(EnvLine(raw=raw, key=key, value=value))
    return out


def _maybe_quote(value: str) -> str:
    if value == "":
        return ""
    if any(ch.isspace() for ch in value) or "#" in value:
        return '"' + value.replace('"', '\\"') + '"'
    return value


def write_env(path: Path, lines: list[EnvLine], edits: dict[str, str]) -> None:
    out_lines: list[str] = []
    seen: set[str] = set()
    for ln in lines:
        if ln.is_kv and ln.key in edits:
            new_value = edits[ln.key]
            out_lines.append(f"{ln.key}={_maybe_quote(new_value)}")
            seen.add(ln.key)
        else:
            out_lines.append(ln.raw)
    for key, val in edits.items():
        if key in seen:
            continue
        out_lines.append(f"{key}={_maybe_quote(val)}")
    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
