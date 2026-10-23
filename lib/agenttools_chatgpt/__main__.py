"""Module entry point; forwards to the installed CLI implementation."""

from .cli import main

raise SystemExit(main())
