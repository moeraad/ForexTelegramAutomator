"""Elevated one-shot: register CT-<NAME>-Api/Bot/Listener via nssm install.

argv: <stack_name> <project_path> <api_svc> <bot_svc> <listener_svc> [<db_path>]

Service-runtime resolution:
  - Frozen (installed) GUI: NSSM runs `<install>\\CopyTrades.exe --service api|bot|listener`.
    No Python or venv needed on the target machine -- the same bundled
    exe serves as both the GUI and the three backends. See gui_launcher.py.
  - Dev mode (running from source under a venv): NSSM runs
    `<project>\\.venv\\Scripts\\python.exe -m src.api|bot|listener` as before.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from src.gui.helpers._helper_log import error, info

_CREATE_NO_WINDOW = 0x08000000


def _nssm_set(name: str, key: str, *values: str) -> None:
    subprocess.run(
        ["nssm", "set", name, key, *values],
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )


def _service_exists(name: str) -> bool:
    proc = subprocess.run(
        ["sc", "query", name],
        capture_output=True,
        text=True,
        creationflags=_CREATE_NO_WINDOW,
    )
    return proc.returncode == 0 and ("SERVICE_NAME" in proc.stdout or "STATE" in proc.stdout)


def _install_service(
    name: str,
    runner: Path,
    runner_args: list[str],
    project_dir: Path,
    tag: str,
) -> int:
    """Register `name` with NSSM to run `runner` with `runner_args`.

    `runner` is either the bundled CopyTrades.exe (frozen install) or
    the venv's python.exe (dev). `runner_args` is the argv tail
    (`--service api` / `-m src.api --db-path ...`).
    `tag` is the short module name used to label NSSM stdout/stderr logs.
    """
    # Two-step registration: `nssm install` with the application path
    # only, then `nssm set ... AppParameters "<quoted string>"`. NSSM's
    # `install` command joins inline argv args with spaces and stores
    # them WITHOUT re-quoting, which breaks any --db-path containing a
    # space (e.g. "Forex Engineer\\copytrades.db" gets truncated at the
    # space when NSSM later splits AppParameters at launch time). Going
    # through `nssm set AppParameters` with our own _quote() preserves
    # the path exactly.
    if not _service_exists(name):
        install_cmd = ["nssm", "install", name, str(runner)]
        inst = subprocess.run(
            install_cmd,
            capture_output=True,
            text=True,
            creationflags=_CREATE_NO_WINDOW,
        )
        if inst.returncode != 0:
            error(
                "CopyTrades — nssm install",
                f"nssm install {name} failed:\n{inst.stdout}{inst.stderr}",
            )
            return inst.returncode
    _nssm_set(name, "Application", str(runner))
    _nssm_set(name, "AppParameters", " ".join(_quote(a) for a in runner_args))
    info(f"registered service {name}")
    logs_dir = project_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    _nssm_set(name, "AppDirectory", str(project_dir))
    _nssm_set(name, "AppStdout", str(logs_dir / f"nssm-{tag}.out.log"))
    _nssm_set(name, "AppStderr", str(logs_dir / f"nssm-{tag}.err.log"))
    _nssm_set(name, "AppRotateFiles", "1")
    _nssm_set(name, "AppRotateBytes", "10485760")
    _nssm_set(name, "Start", "SERVICE_AUTO_START")
    _nssm_set(name, "AppExit", "Default", "Restart")
    _nssm_set(name, "AppRestartDelay", "5000")
    info(f"installed service {name} → {runner.name} {' '.join(runner_args)}")
    return 0


def _quote(arg: str) -> str:
    """Wrap an arg in double quotes if it contains whitespace. Matches
    how NSSM expects AppParameters when set via `nssm set`."""
    if " " in arg or "\t" in arg:
        return f'"{arg}"'
    return arg


def _resolve_runner(
    project_dir: Path,
) -> tuple[Path | None, str, str | None]:
    """Pick the runner to register with NSSM.

    Returns (runner_path, mode, error_message).

    Frozen install (preferred): the same `CopyTrades.exe` running this
    helper IS the bundled exe; reuse it for the services with
    `--service <name>` argv. Detection: sys.frozen + sys.executable
    points at a non-Python exe.

    Dev mode: `<project>\\.venv\\Scripts\\python.exe`. Errors out with a
    clear message when neither exists, so the operator knows what's
    missing on a fresh install.
    """
    if getattr(sys, "frozen", False):
        bundle_exe = Path(sys.executable).resolve()
        if bundle_exe.exists():
            return bundle_exe, "frozen", None
    venv_python = project_dir / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return venv_python, "venv", None
    return (
        None,
        "missing",
        (
            "Neither the bundled CopyTrades.exe nor a Python venv was "
            f"found.\n\nExpected (dev mode):\n  {venv_python}\n\n"
            "If you installed via CopyTradesSetup-*.exe, restart the GUI "
            "from the Start Menu shortcut -- the installer drops "
            "CopyTrades.exe alongside this helper so services can run "
            "without Python.\n\n"
            "If you're running from source, create the venv with:\n"
            "  python -m venv .venv\n"
            "  .venv\\Scripts\\activate\n"
            "  pip install -e .\n\n"
            "Or update stacks_config.json to point project_path at a "
            "folder that contains .venv."
        ),
    )


def _service_args(mode: str, module: str, db_path: str | None) -> list[str]:
    """Construct argv tail for the chosen runner."""
    target = module.split(".")[-1]  # 'src.api' -> 'api'
    if mode == "frozen":
        args = ["--service", target]
    else:
        args = ["-m", module]
    if db_path:
        args.extend(["--db-path", db_path])
    return args


def main(argv: list[str]) -> int:
    if len(argv) < 5:
        error(
            "CopyTrades — services install",
            f"helper invoked with {len(argv)} args, need at least 5:\n"
            "<stack_name> <project_path> <api_svc> <bot_svc> <listener_svc> [<db_path>]",
        )
        return 2
    _, project_path, api_svc, bot_svc, listener_svc = argv[:5]
    db_path = argv[5] if len(argv) >= 6 else None
    project_dir = Path(project_path)

    runner, mode, err_msg = _resolve_runner(project_dir)
    if runner is None:
        error("CopyTrades — services install", err_msg or "unknown error")
        return 3
    info(f"runner: {runner} (mode={mode})")

    for svc, module in (
        (api_svc, "src.api"),
        (bot_svc, "src.bot"),
        (listener_svc, "src.listener"),
    ):
        args = _service_args(mode, module, db_path)
        rc = _install_service(svc, runner, args, project_dir, tag=module.split(".")[-1])
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
