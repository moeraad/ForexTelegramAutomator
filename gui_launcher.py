"""PyInstaller entry script.

Branches on argv BEFORE importing any heavy module so the same bundled
exe can serve as both the GUI and the three backend services:

    CopyTrades.exe                 → launch the GUI (default)
    CopyTrades.exe --service api   → run src.api.run()
    CopyTrades.exe --service bot   → run src.bot.main()
    CopyTrades.exe --service listener → asyncio.run(src.listener.main())

The argv check happens before any `from src.gui` import so the service
processes never pay the PySide6 / qfluentwidgets memory cost (~100 MB
each). bootstrap_services_install.py registers each NSSM service to
run this exe with the matching `--service <name>` parameter when the
GUI is running as a frozen bundle, so the operator doesn't need a
Python venv on the target machine.

Crash-capture: PyInstaller's `console=False` strips stdio handles in
windowed mode, so an import failure during service startup would
otherwise vanish into the void (no log, no NSSM stderr capture, just
SERVICE_PAUSED). _write_service_crashlog() catches any unhandled
exception during the service path and writes a full traceback to a
known path so the operator can debug the cause.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _crashlog_dir() -> Path:
    """Pick a writable folder for the bootstrap traceback file.

    Prefers %APPDATA%\\CopyTrades (the GUI's data root) so the operator
    finds the log next to copytrades.db. Falls back to %LOCALAPPDATA%
    then %TEMP% if the preferred path is unwritable.
    """
    for env_key in ("APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        base = os.environ.get(env_key)
        if not base:
            continue
        d = Path(base) / "CopyTrades"
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            continue
    return Path.cwd()


def _write_service_crashlog(target: str, exc: BaseException) -> None:
    """Persist an unhandled service-bootstrap exception to disk so the
    operator can read it even when NSSM has nothing to capture.
    """
    import traceback
    out = _crashlog_dir() / f"service-{target}-crash.log"
    try:
        with out.open("a", encoding="utf-8") as f:
            f.write("=" * 72 + "\n")
            f.write(f"timestamp:   {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"target:      {target}\n")
            f.write(f"executable:  {sys.executable}\n")
            f.write(f"frozen:      {getattr(sys, 'frozen', False)!r}\n")
            f.write(f"argv:        {sys.argv!r}\n")
            f.write(f"cwd:         {os.getcwd()}\n")
            f.write(f"python:      {sys.version}\n")
            f.write("traceback:\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
            f.write("\n")
    except Exception:
        pass


def _wire_safe_stdio() -> None:
    """When PyInstaller builds with console=False (windowed mode), the
    bundled exe has no console attached, so `sys.stdout` / `sys.stderr`
    are None. Any `logging.StreamHandler()` (which defaults to stderr)
    would then raise AttributeError on the first log line.

    Route both streams to NUL so the service still starts. File log
    output is unaffected -- `configure_logging` adds a RotatingFileHandler
    that writes to logs/<name>.log regardless of the stderr handle.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", buffering=1)


def _dispatch_service(target: str) -> int:
    """Run one of the background services in-process. Returns an
    exit code; callers should propagate it via sys.exit().

    Targets:
      - api / bot: per-destination services (one each per stack).
      - listener: legacy per-stack listener. Kept for back-compat with
        old NSSM installs that pre-date Step 8 of the multi-channel plan.
      - shared-listener: v2 per-account listener. Started by the Step 8
        install helper for new and migrated stacks.
    """
    if target == "api":
        from src.api import run as api_run
        api_run()
        return 0
    if target == "bot":
        from src.bot import main as bot_main
        bot_main()
        return 0
    if target == "listener":
        import asyncio
        from src.listener import main as listener_main
        asyncio.run(listener_main())
        return 0
    if target == "shared-listener":
        import asyncio
        from src.shared_listener import main as shared_listener_main
        asyncio.run(shared_listener_main())
        return 0
    print(
        f"--service expects one of: api, bot, listener, shared-listener "
        f"(got: {target!r})",
        file=sys.stderr,
    )
    return 2


def _service_target(argv: list[str]) -> str | None:
    """Return the requested service name, or None if the GUI should run."""
    try:
        idx = argv.index("--service")
    except ValueError:
        return None
    if idx + 1 >= len(argv):
        return None
    return argv[idx + 1].strip().lower()


if __name__ == "__main__":
    _target = _service_target(sys.argv)
    if _target is not None:
        _wire_safe_stdio()
        try:
            sys.exit(_dispatch_service(_target))
        except BaseException as _exc:  # noqa: BLE001 - top-level crash net
            _write_service_crashlog(_target, _exc)
            # Exit non-zero so NSSM treats it as a failure (which it
            # already does -- we just want the rc to be explicit).
            sys.exit(1)
    # GUI path — keep the import here so service processes never load
    # PySide6 / qfluentwidgets.
    from src.gui.__main__ import main
    sys.exit(main())
