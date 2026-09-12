"""Entry point: ``python -m engine --strategy trend --mode paper``."""

from __future__ import annotations

import sys

from engine.cli import main

if __name__ == "__main__":
    sys.exit(main())
