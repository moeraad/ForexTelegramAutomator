"""Elevated one-shot: register CT-<NAME>-Api/Bot/Listener via nssm install.

argv: <stack_name> <project_path> <api_svc> <bot_svc> <listener_svc> [<db_path>]
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
    python_exe: Path,
    module: str,
    project_dir: Path,
    db_path: str | None,
) -> int:
    app_params = f'-m {module}'
    if db_path:
        app_params += f' --db-path "{db_path}"'
    if _service_exists(name):
        _nssm_set(name, "Application", str(python_exe))
        _nssm_set(name, "AppParameters", app_params)
        info(f"updated service {name} (already installed)")
    else:
        install_cmd = ["nssm", "install", name, str(python_exe), "-m", module]
        if db_path:
            install_cmd.extend(["--db-path", db_path])
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
    logs_dir = project_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    tag = module.split(".")[-1]
    _nssm_set(name, "AppDirectory", str(project_dir))
    _nssm_set(name, "AppStdout", str(logs_dir / f"nssm-{tag}.out.log"))
    _nssm_set(name, "AppStderr", str(logs_dir / f"nssm-{tag}.err.log"))
    _nssm_set(name, "AppRotateFiles", "1")
    _nssm_set(name, "AppRotateBytes", "10485760")
    _nssm_set(name, "Start", "SERVICE_AUTO_START")
    _nssm_set(name, "AppExit", "Default", "Restart")
    _nssm_set(name, "AppRestartDelay", "5000")
    info(f"installed service {name} → {module}")
    return 0


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
    python_exe = project_dir / ".venv" / "Scripts" / "python.exe"
    if not python_exe.exists():
        error(
            "CopyTrades — services install",
            f"This stack has no Python venv yet.\n\n"
            f"Expected: {python_exe}\n\n"
            f"Create one by running in that folder:\n"
            f"  python -m venv .venv\n"
            f"  .venv\\Scripts\\activate\n"
            f"  pip install -e .\n\n"
            "Or update stacks_config.json to point project_path at a folder that has .venv.",
        )
        return 3
    for svc, module in (
        (api_svc, "src.api"),
        (bot_svc, "src.bot"),
        (listener_svc, "src.listener"),
    ):
        rc = _install_service(svc, python_exe, module, project_dir, db_path)
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
