"""Entry point: `python -m src.gui`  ·  or `CopyTrades.exe`."""
from __future__ import annotations

import argparse
import os
import sys

from src.logging_setup import configure_logging


_HELPERS = {
    "bootstrap_nssm_install": "src.gui.helpers.bootstrap_nssm_install",
    "bootstrap_services_install": "src.gui.helpers.bootstrap_services_install",
    "bootstrap_services_uninstall": "src.gui.helpers.bootstrap_services_uninstall",
}


def _run_helper(name: str, rest: list[str]) -> int:
    if name not in _HELPERS:
        sys.stderr.write(f"unknown helper: {name}\n")
        return 2
    import importlib
    mod = importlib.import_module(_HELPERS[name])
    if hasattr(mod, "main"):
        if name in ("bootstrap_services_install", "bootstrap_services_uninstall"):
            return int(mod.main(rest))
        return int(mod.main())
    sys.stderr.write(f"helper {name} has no main()\n")
    return 3


def main() -> int:
    parser = argparse.ArgumentParser(prog="copytrades-gui", add_help=False)
    parser.add_argument("--pick", action="store_true")
    parser.add_argument("--helper", default="")
    parser.add_argument("-h", "--help", action="help")
    args, rest = parser.parse_known_args()

    if args.helper:
        configure_logging("gui-helper")
        return _run_helper(args.helper, rest)

    if args.pick:
        os.environ["GUI_FORCE_PICKER"] = "1"

    configure_logging("gui")
    from src.gui.app import run
    return run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
