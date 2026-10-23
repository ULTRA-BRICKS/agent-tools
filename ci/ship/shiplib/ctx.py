"""Shared run state and subprocess helpers for `gh ship` (the Python port of ship.sh).

ship.sh used to be one 4.6k-line bash script whose top-level variables were shared by every gate.
Here the same variables live on one `Ctx` object that every gate module receives.

Naming rules (the port contract every module follows):

* a bash global `FOO_BAR` or `_FOO_BAR` is `ctx.foo_bar` (lower-case, leading underscores dropped);
* a bash function `_foo_bar` / `foo_bar` is the Python function `foo_bar` (leading underscores
  dropped) in the module that owns the code segment it was defined in;
* a bash 0/1 flag is a `bool`; a bash array is a `list[str]`; an unset/empty string is `""`
  (never None), so the bash `[ -n "$X" ]` test is `if ctx.x:`;
* a bash `export X=...` is `ctx.env["X"] = ...` (`ctx.env` is what every child process inherits)
  and every `${SHIP_X:-}` read is `ctx.env.get("SHIP_X", "")` (never `os.environ`);
* bash `set -e` aborting the script on an unguarded failing command is `raise ShipExit(rc)`;
  `exit N` is `raise ShipExit(N)`; a bash `exit` inside `$(...)`/a subshell only ends that subshell,
  so port it as a return value, not a ShipExit;
* messages keep their exact wording and their stream (tests assert on them): `ctx.out` is stdout,
  `ctx.err` is stderr.

State a gate needs that is not declared below is assigned as a new attribute on `ctx` by the module
that owns it, with a one-line comment where it is first set; always read such an attribute through
`getattr(ctx, "name", default)` unless the owning step has certainly run.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Sequence

ENC = {"encoding": "utf-8", "errors": "surrogateescape"}


class ShipExit(Exception):
    """End the run with this exit status (bash `exit N`, or `set -e` aborting)."""

    def __init__(self, code: int = 1):
        super().__init__(code)
        self.code = code


@dataclass
class Result:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


class Ctx:
    def __init__(self, self_dir: str, argv: list[str] | None = None):
        self.argv = argv or []  # the command line after `ship.sh`
        self.self_dir = self_dir  # `_SHIP_SELF_DIR`: realpath of ci/ship
        self.env: dict[str, str] = dict(os.environ)
        self.orig_pwd = os.path.realpath(os.getcwd())
        # The launcher exports the interpreter it found (a real one even when python3 is the
        # Windows Store stub); tests shim `python3` on PATH, which this default keeps honouring.
        self.python = self.env.get("SHIP_REAL_PYTHON") or "python3"

        # --- command line (setup.py fills these in) ---
        self.pr = ""
        self.skip_ci = False
        self.dry_run = False
        self.no_shot_ok = ""
        self.no_vbump_ok = ""
        self.no_dwell_ok = ""
        self.repo_flag = ""
        self.rewrite_magic_close = False
        self.resolve_threads = False
        self.task_config = ""
        self.task_config_args: list[str] = []
        self.shot_paths: list[str] = []
        self.shot_descs: list[str] = []
        self.known_flakes: list[str] = []
        self.merge_message_args: list[str] = []
        self.merge_author_email_set = False

        # --- derived once, early (setup.py) ---
        self.root = ""  # git rev-parse --show-toplevel
        self.default_branch = "main"  # SHIP_DEFAULT_BRANCH: only the FALLBACK base (see cleanup.py)
        self.merge_method = "squash"
        self.ui_path_regex = ""
        self.main_checkout = ""
        self.provider_resolved = "github"
        self.foreign_repo_invoke = False
        self.branch = ""
        self.state = ""
        self.mergeable = ""
        self.cross_repo = ""
        self.merge_state = ""

        # --- evidence handed from gate to gate ---
        self.kf_audit_pending = ""
        self.ci_evidence = ""
        self.quorum_evidence = ""
        self.task_code = self.env.get("TASK_CODE", "")  # bash: an exported TASK_CODE is inherited
        self.acceptance_gate_code = ""
        self.acceptance_gate_fully_accepted = ""

        self._exit_handlers: list[Callable[[int], None]] = []

    # ---- output ---------------------------------------------------------------------------
    def out(self, msg: str = "", end: str = "\n") -> None:
        sys.stdout.write(msg + end)
        sys.stdout.flush()

    def err(self, msg: str = "", end: str = "\n") -> None:
        sys.stderr.write(msg + end)
        sys.stderr.flush()

    def refuse(self, msg: str, code: int = 1) -> "ShipExit":
        """Print `msg` to stderr and end the run: `raise ctx.refuse("Refusing: ...")`."""
        self.err(msg)
        return ShipExit(code)

    # ---- EXIT trap ------------------------------------------------------------------------
    def on_exit(self, handler: Callable[[int], None]) -> None:
        """Register what bash did with `trap '...' EXIT`; handlers get the exit status."""
        self._exit_handlers.append(handler)

    def run_exit_handlers(self, code: int) -> None:
        for handler in reversed(self._exit_handlers):
            handler(code)

    # ---- subprocesses ---------------------------------------------------------------------
    def cmd(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        input: str | None = None,
        stdout: str = "capture",
        stderr: str = "inherit",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Result:
        """Run a command. stdout: capture|inherit|devnull. stderr: capture|inherit|devnull|stdout.
        A command that cannot be started returns rc 127 (bash's "command not found")."""
        modes = {"capture": subprocess.PIPE, "inherit": None, "devnull": subprocess.DEVNULL, "stdout": subprocess.STDOUT}
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            proc = subprocess.run(
                list(argv),
                cwd=cwd,
                input=input,
                stdout=modes[stdout],
                stderr=modes[stderr],
                env=self.env if env is None else env,
                timeout=timeout,
                **ENC,
            )
        except FileNotFoundError as exc:
            return Result(127, "", str(exc))
        except subprocess.TimeoutExpired:
            return Result(124, "", "timed out")
        rc = proc.returncode if proc.returncode >= 0 else 128 - proc.returncode  # shell convention: 128+signal
        return Result(rc, proc.stdout or "", proc.stderr or "")

    def git(self, *args: str, **kw) -> Result:
        return self.cmd(["git", *args], **kw)

    def gh(self, *args: str, **kw) -> Result:
        """`gh ...`; on a Forgejo origin the call is routed through forgejo_provider.py exactly like
        the bash `gh()` function shadow (setup.forgejo_gh keeps the per-run evidence ledger)."""
        if self.provider_resolved == "forgejo":
            from shiplib.setup import forgejo_gh

            return forgejo_gh(self, list(args), **kw)
        return self.cmd(["gh", *args], **kw)

    def run(self, argv: Sequence[str], *, check: bool = True, **kw) -> int:
        """bash `run`: under --dry-run print `[dry-run] <argv>` instead of executing, else execute
        with inherited stdio. `check=True` is `set -e`: a failure ends the run with that status."""
        if self.dry_run:
            self.out("[dry-run] " + " ".join(argv))
            return 0
        rc = self.cmd(argv, stdout="inherit", **kw).rc
        if check and rc != 0:
            raise ShipExit(rc)
        return rc
