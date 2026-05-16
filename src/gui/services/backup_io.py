"""Backup + restore of per-stack APPDATA state to/from a zip file.

What's included:
  - %APPDATA%/CopyTrades/stacks_config.json
  - %APPDATA%/CopyTrades/state.json
  - %APPDATA%/CopyTrades/<stack>/copytrades.db          (each stack)
  - %APPDATA%/CopyTrades/<stack>/copytrades.db-wal      (if present)
  - %APPDATA%/CopyTrades/<stack>/copytrades.db-shm      (if present)
  - %APPDATA%/CopyTrades/<stack>/profile.json           (each stack)
  - %APPDATA%/CopyTrades/<stack>/logs/trades.log        (high-value history)

What's NOT included:
  - logs/system.log (noisy, rotating, not needed for state restore)
  - logs/ai_calls.jsonl (rebuildable from new runs)
  - Per-machine DPAPI ciphertext only decrypts on the original
    machine + account. The backup carries it as-is, so restore on
    the same machine works fully; restore on a different machine
    requires re-running the setup wizard.

Restore semantics:
  - Validates the zip has stacks_config.json before touching anything.
  - Writes to a temp directory first, atomically renames into place.
  - The app must be restarted for the new files to be picked up
    (services hold open DB handles).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def _appdata_root() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / "CopyTrades"


@dataclass
class BackupResult:
    zip_path: Path
    stack_count: int
    total_bytes: int


@dataclass
class RestoreResult:
    success: bool
    stack_count: int
    warnings: list[str] = field(default_factory=list)
    error: str = ""


def make_backup(target_dir: Path | None = None) -> BackupResult:
    """Zip the current APPDATA tree into a dated archive in ``target_dir``.

    Defaults to ``%APPDATA%/CopyTrades/backups/``. Returns the zip path
    and basic stats.
    """
    root = _appdata_root()
    if not root.exists():
        raise FileNotFoundError(f"APPDATA root not found: {root}")
    if target_dir is None:
        target_dir = root / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    zip_path = target_dir / f"copytrades-backup-{ts}.zip"

    stacks_config = root / "stacks_config.json"
    state_file = root / "state.json"
    stack_count = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Top-level files.
        if stacks_config.exists():
            zf.write(stacks_config, arcname="stacks_config.json")
        if state_file.exists():
            zf.write(state_file, arcname="state.json")

        # Per-stack files.
        for stack_dir in sorted(root.iterdir()):
            if not stack_dir.is_dir() or stack_dir.name == "backups":
                continue
            stack_count += 1
            for fname in ("copytrades.db", "copytrades.db-wal",
                          "copytrades.db-shm", "profile.json"):
                src = stack_dir / fname
                if src.exists():
                    zf.write(src, arcname=f"{stack_dir.name}/{fname}")
            trades_log = stack_dir / "logs" / "trades.log"
            if trades_log.exists():
                zf.write(trades_log, arcname=f"{stack_dir.name}/logs/trades.log")

    return BackupResult(
        zip_path=zip_path,
        stack_count=stack_count,
        total_bytes=zip_path.stat().st_size,
    )


def restore_backup(zip_path: Path) -> RestoreResult:
    """Extract a backup zip into APPDATA. Atomic-ish: extracts into a
    temp dir first, then moves into place. Existing files are
    overwritten. Returns details of the operation.
    """
    if not zip_path.exists():
        return RestoreResult(success=False, stack_count=0,
                             error=f"backup file not found: {zip_path}")

    # Validate the zip has at least stacks_config.json.
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            if "stacks_config.json" not in names:
                return RestoreResult(
                    success=False, stack_count=0,
                    error="zip is missing stacks_config.json — not a valid CopyTrades backup",
                )
    except zipfile.BadZipFile as e:
        return RestoreResult(success=False, stack_count=0,
                             error=f"not a valid zip file: {e}")

    warnings: list[str] = []
    stack_count = 0
    root = _appdata_root()
    root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_path)

        # Count restored stacks before copying so we can report
        # accurately even if a stack is partially present.
        for entry in tmp_path.iterdir():
            if entry.is_dir():
                stack_count += 1

        for entry in tmp_path.iterdir():
            target = root / entry.name
            try:
                if entry.is_dir():
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.move(str(entry), str(target))
                else:
                    if target.exists():
                        target.unlink()
                    shutil.move(str(entry), str(target))
            except OSError as e:
                warnings.append(f"{entry.name}: {e}")

    return RestoreResult(
        success=True, stack_count=stack_count, warnings=warnings,
    )


def list_backups(target_dir: Path | None = None) -> list[Path]:
    """Return all backup zips in ``target_dir`` sorted newest-first."""
    if target_dir is None:
        target_dir = _appdata_root() / "backups"
    if not target_dir.exists():
        return []
    return sorted(
        target_dir.glob("copytrades-backup-*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
