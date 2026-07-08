"""agenttools_dev - project-scoped development command helpers.

Accessed via: the installed ``dev`` console script, or ``python -m agenttools_dev``.

Assumptions: importing this package must stay stdlib-only; commands that need rig.yaml
or process inspection load their dependencies and platform helpers inside the command
path.
"""

from __future__ import annotations

__version__ = "0.1.1"

__all__ = ["__version__"]
