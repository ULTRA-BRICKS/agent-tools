"""Entry point for ``python3 <agent-tools>/lib/agenttools_gpu_slots ...`` (no install needed) and
``python3 -m agenttools_gpu_slots ...``."""

from __future__ import annotations

import sys
from pathlib import Path

if not __package__:
    # Run as a directory script: put lib/ on the path so the package imports itself normally.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agenttools_gpu_slots import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
