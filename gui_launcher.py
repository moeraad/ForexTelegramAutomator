"""PyInstaller entry script — delegates to src.gui.__main__:main."""
from __future__ import annotations

import sys

from src.gui.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
