"""Elevated one-shot: register every v2 NSSM service from a spec file.

argv: <spec_json_path>

The spec is a JSON list of entries; each entry describes one service:

    [
      {"service": "CT-X-Api",        "module": "src.api",
       "db_path": "C:\\\\…\\\\copytrades.db", "account_id": ""},
      {"service": "CT-Main-Bot",     "module": "src.bot",
       "db_path": "C:\\\\…\\\\copytrades.db", "account_id": ""},
      {"service": "CT-Listener-acc_a","module": "src.shared_listener",
       "db_path": "C:\\\\…\\\\copytrades.db", "account_id": "acc_a"}
    ]

Phase-3 v2 cleanup: replaces the rigid 3-tuple ``bootstrap_services_install``
that assumed one stack = one (API, Bot, Listener) trio. This version
handles any topology: multiple bots binding to one destination, one bot
serving N destinations, multiple accounts each with their own listener.

Each entry is install-or-update idempotent — calling twice doesn't
re-create services that already exist with the right ``AppParameters``.
Duplicate ``service`` names in the spec are deduplicated (first wins);
the caller is responsible for assembling a coherent list.

Wire-compat: the existing ``bootstrap_services_install`` helper is kept
for one-stack legacy callers (Settings view's per-stack "Install
services" button). Phase-4 will retire it.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.gui.helpers._helper_log import error, info

# Re-use the existing helper's _install_service + _resolve_runner +
# _service_args + _quote so we don't duplicate the NSSM-set chain.
from src.gui.helpers.bootstrap_services_install import (
    _install_service,
    _resolve_runner,
    _service_args,
)


def _parse_spec(path: Path) -> list[dict] | None:
    """Read + validate the JSON spec. Returns None on any IO/format error."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        error(
            "CopyTrades - v2 services install",
            f"could not read spec file:\n  {path}\n\n{e}",
        )
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        error(
            "CopyTrades - v2 services install",
            f"spec file is not valid JSON ({path}):\n\n{e}",
        )
        return None
    if not isinstance(data, list):
        error(
            "CopyTrades - v2 services install",
            "spec must be a JSON list of service entries; got "
            f"{type(data).__name__}",
        )
        return None
    return data


def _dedup_spec(entries: list[dict]) -> list[dict]:
    """First-wins dedup by ``service`` name.

    A shared bot bound to two destinations might appear in the spec
    twice (once per destination's enumeration). Both entries should
    install the SAME service — dedup so we don't ``nssm install`` it
    twice in a row (the second call's ``AppParameters`` would clobber
    the first's, which is harmless but noisy in logs).
    """
    seen: set[str] = set()
    out: list[dict] = []
    for e in entries:
        svc = (e.get("service") or "").strip()
        if not svc or svc in seen:
            continue
        seen.add(svc)
        out.append(e)
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 1:
        error(
            "CopyTrades - v2 services install",
            "helper invoked with no spec file; expected argv: "
            "<spec_json_path> [<project_dir>]",
        )
        return 2
    spec_path = Path(argv[0])
    entries = _parse_spec(spec_path)
    if entries is None:
        return 3
    entries = _dedup_spec(entries)
    if not entries:
        info("v2 install: no services to register (spec was empty)")
        return 0

    # Project_dir resolution priority:
    #   1. Explicit argv[1] from the caller (BASE_DIR / install root) —
    #      this is the source of truth; older heuristics guessed wrong
    #      for APPDATA-based DB layouts.
    #   2. Fallback: spec file's own parent (always a real directory).
    # The previous parent-chain-from-db_path heuristic landed on
    # %APPDATA%\Roaming when the DB lived under CopyTrades/<dest>/, which
    # has no .venv and surfaced as the "no venv found" install error.
    if len(argv) >= 2 and argv[1]:
        project_dir = Path(argv[1])
    else:
        project_dir = spec_path.parent
    runner, mode, err_msg = _resolve_runner(project_dir)
    if runner is None:
        error("CopyTrades - v2 services install", err_msg or "runner not found")
        return 4
    info(f"runner: {runner} (mode={mode})")

    failures: list[str] = []
    for e in entries:
        svc = (e.get("service") or "").strip()
        module = (e.get("module") or "").strip()
        db_path = (e.get("db_path") or "").strip() or None
        account_id = (e.get("account_id") or "").strip() or None
        if not svc or not module:
            failures.append(f"(skipped invalid entry: service={svc!r} module={module!r})")
            continue
        args = _service_args(mode, module, db_path, account_id=account_id)
        # Logs live next to the DB the service binds to (when given);
        # this matches the per-service log routing the runtime expects.
        logs_dir = Path(db_path).parent / "logs" if db_path else project_dir / "logs"
        tag = module.split(".")[-1].replace("_", "-")
        rc = _install_service(svc, runner, args, project_dir, logs_dir, tag=tag)
        if rc != 0:
            failures.append(f"{svc}: install rc={rc}")

    if failures:
        error(
            "CopyTrades - v2 services install",
            "Some services could not be registered:\n\n" + "\n".join(failures),
        )
        return 5
    info(f"v2 install: registered {len(entries)} service(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
