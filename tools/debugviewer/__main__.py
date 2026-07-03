"""Entry point: ``python3 -m tools.debugviewer``."""
from __future__ import annotations

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
