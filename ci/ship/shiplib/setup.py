"""ship step 1: argument parsing, knobs, core.bare guard, provider dispatch, --repo, PR state.

Port of ship.orig.sh lines 1-789 (the Windows python3 shim is handled by the ship.sh launcher).
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time

from shiplib.ctx import ENC, Result, ShipExit

USAGE = (
    "Usage: ship.sh <PR-number> [--repo <owner/repo>] [--skip-ci] [--dry-run] "
    "[--no-screenshot-ok <reason>] [--no-version-bump-ok <reason>] [--no-review-dwell-ok <reason>] "
    "[--resolve-addressed-threads] [--rewrite-magic-close] [--screenshot <path> [desc]]... "
    "[--known-flake <check-name>]... [--task-config <path>] [--subject <text>] [--body <text>] "
    "[--author-email <email>]"
)

DEFAULT_UI_PATH_REGEX = r"(^|/)(components|pages|views|ui|app|src/app)/|\.(tsx|jsx|vue|svelte)$"

_ABS_PATH_RE = re.compile(r"^(/|[A-Za-z]:/|[A-Za-z]:\\|\\\\)")


def caller_path(ctx, path: str) -> str:
    """A path argument as the caller meant it: absolute as given (POSIX, drive-letter, UNC), else
    relative to the caller's cwd."""
    if _ABS_PATH_RE.match(path):
        return path
    return f"{ctx.orig_pwd}/{path}"


def _starts_dash(s: str) -> bool:
    return s[:1] == "-"


def parse_args(ctx) -> None:
    """Parse ctx.argv into the ctx option attributes (bash `args=("$@")` loop)."""
    args = list(ctx.argv)
    n = len(args)
    i = 0
    while i < n:
        a = args[i]
        if a == "--skip-ci":
            ctx.skip_ci = True
        elif a == "--dry-run":
            ctx.dry_run = True
        elif a == "--resolve-addressed-threads":
            ctx.resolve_threads = True
        elif a == "--rewrite-magic-close":
            ctx.rewrite_magic_close = True
        elif a in ("--no-screenshot-ok", "--no-version-bump-ok", "--no-review-dwell-ok"):
            i += 1
            if not (i < n and not _starts_dash(args[i])):
                raise ctx.refuse(f"{a} needs a <reason>.")
            value = args[i]
            if not value:
                raise ctx.refuse(f"{a} reason empty.")
            if a == "--no-screenshot-ok":
                ctx.no_shot_ok = value
            elif a == "--no-version-bump-ok":
                ctx.no_vbump_ok = value
            else:
                ctx.no_dwell_ok = value
        elif a.startswith("--task-config="):
            ctx.task_config = a[len("--task-config="):]
            if not ctx.task_config:
                raise ctx.refuse("--task-config= needs a <path>.")
        elif a == "--task-config":
            i += 1
            if not (i < n and args[i] and not _starts_dash(args[i])):
                raise ctx.refuse("--task-config needs a <path>.")
            ctx.task_config = args[i]
        elif a == "--known-flake":
            i += 1
            if not (i < n and not _starts_dash(args[i])):
                raise ctx.refuse("--known-flake needs a <check-name>.")
            if not args[i]:
                raise ctx.refuse("--known-flake check-name empty.")
            ctx.known_flakes.append(args[i])
        elif a in ("--subject", "--body", "--author-email"):
            # The value is taken verbatim, even when it starts with "-" (a body may open with a list item).
            i += 1
            if not (i < n and args[i]):
                raise ctx.refuse(f"{a} needs a non-empty value.")
            ctx.merge_message_args += [a, args[i]]
            if a == "--author-email":
                ctx.merge_author_email_set = True
        elif a in ("--screenshot", "--shot"):
            i += 1
            if not (i < n and not _starts_dash(args[i])):
                raise ctx.refuse(f"{a} needs a <path>.")
            ctx.shot_paths.append(caller_path(ctx, args[i]))
            if i + 1 < n and not _starts_dash(args[i + 1]):
                # The next bare token is this shot's desc UNLESS it's the (still unset) PR number.
                nxt = args[i + 1]
                rest_bare = sum(1 for j in range(i + 2, n) if not _starts_dash(args[j]))
                if not ctx.pr and rest_bare == 0:
                    ctx.shot_descs.append("")
                else:
                    ctx.shot_descs.append(nxt)
                    i += 1
            else:
                ctx.shot_descs.append("")
        # The attached `=`/glued forms must precede the bare `--repo|-R` case.
        elif a.startswith("--repo="):
            ctx.repo_flag = a[len("--repo="):]
            if not ctx.repo_flag:
                raise ctx.refuse("--repo= needs an <owner/repo> value.")
        elif a.startswith("-R="):
            ctx.repo_flag = a[len("-R="):]
            if not ctx.repo_flag:
                raise ctx.refuse("-R= needs an <owner/repo> value.")
        elif a.startswith("-R") and len(a) > 2:
            ctx.repo_flag = a[2:]
        elif a in ("--repo", "-R"):
            i += 1
            if not (i < n and not _starts_dash(args[i])):
                raise ctx.refuse(f"{a} needs an <owner/repo> argument.")
            ctx.repo_flag = args[i]
        elif a.startswith("-"):
            raise ctx.refuse(f"Unknown flag: {a}\n{USAGE}")
        else:
            if ctx.pr:
                raise ctx.refuse(f"Multiple PR numbers ({ctx.pr}, {a}) — pass one.")
            ctx.pr = a
        i += 1
    if not ctx.pr:
        raise ctx.refuse(USAGE)
    # Validate --repo shape once: exactly owner/repo (a single slash, no extra path segments).
    if ctx.repo_flag:
        slashes = ctx.repo_flag.count("/")
        if slashes >= 2:
            raise ctx.refuse(
                f"Refusing: --repo value '{ctx.repo_flag}' looks like a URL segment — expected 'owner/repo'."
            )
        if slashes == 0:
            raise ctx.refuse(f"Refusing: --repo value '{ctx.repo_flag}' is not in owner/repo form.")


def validate_task_config(ctx) -> None:
    """Resolve --task-config against the caller's cwd and prove it is routing-only and readable NOW."""
    if not ctx.task_config:
        return
    ctx.task_config = caller_path(ctx, ctx.task_config)
    if not os.path.isfile(ctx.task_config) or not os.access(ctx.task_config, os.R_OK):
        raise ctx.refuse(
            f"Refusing: --task-config '{ctx.task_config}' is not a readable file — the ticket cannot be "
            "routed, so its acceptance cannot be proven."
        )
    # Routing only: a task-cli config can also carry enforcement settings (e.g. switching the
    # acceptance gate off), which would make a caller-supplied file a self-service bypass.
    rc = ctx.cmd(
        [ctx.python, "-I", os.path.join(ctx.self_dir, "task_config_guard.py"), ctx.task_config],
        stdout="inherit",
    ).rc
    if rc != 0:
        raise ctx.refuse(
            "Refusing: --task-config must be a routing-only task-cli config (backend + its routing "
            "block); see ci/ship/task_config_guard.py."
        )
    ctx.task_config_args = ["--config", ctx.task_config]


# --------------------------------------------------------------------------- core.bare guard

def die_core_bare(ctx, dir: str, label: str, fix_cmd: str):
    """Print the core.bare corruption diagnostic and end the run (exit 1)."""
    ctx.err(f"Refusing: {label} at {dir} has core.bare=true but is a WORKING checkout (corruption).")
    ctx.err("  Every git op there fails 'fatal: this operation must be run in a work tree',")
    ctx.err("  which would break ship's main-refresh mid-merge. Fix it, then re-run ship:")
    ctx.err(f"    {fix_cmd}")
    ctx.err("  (or run `rig doctor --fix` to detect + repair it).")
    raise ShipExit(1)


def shell_squote(s: str) -> str:
    """Single-quote `s` for copy-paste into a shell, escaping embedded quotes via the '\\'' idiom."""
    return "'" + s.replace("'", "'\\''") + "'"


def _git_out(ctx, *args: str) -> str:
    """`$(git ... 2>/dev/null)`: stdout with trailing newlines stripped, errors ignored."""
    return ctx.git(*args, stderr="devnull").out.rstrip("\n")


def core_bare_fix_cmd(ctx, dir: str) -> str:
    """The `core.bare false` command that actually repairs DIR (worktree-scoped when core.bare=true lives there)."""
    q = shell_squote(dir)
    # Read both flags through git's --bool parser so a value written as 1/yes/on is matched.
    if (
        _git_out(ctx, "-C", dir, "config", "--bool", "extensions.worktreeConfig") == "true"
        and _git_out(ctx, "-C", dir, "config", "--worktree", "--bool", "--get", "core.bare") == "true"
    ):
        return f"git -C {q} config --worktree core.bare false"
    return f"git -C {q} config core.bare false"


def abort_if_core_bare(ctx, dir: str, label: str) -> None:
    """Abort when DIR is a working-checkout layout (`.git` at its root) that git reports as bare."""
    if not os.path.exists(f"{dir}/.git"):
        return  # no working-checkout layout (genuine bare / not a checkout) — not our class
    bare = ctx.git("-C", dir, "rev-parse", "--is-bare-repository", stderr="devnull")
    if (bare.out.rstrip("\n") if bare.rc == 0 else "false") != "true":
        return
    die_core_bare(ctx, dir, label, core_bare_fix_cmd(ctx, dir))


def _physical_cwd() -> str:
    try:
        return os.path.realpath(os.getcwd())
    except OSError:
        return os.environ.get("PWD", "")


def cwd_has_ancestor_dotgit() -> bool:
    """True iff some ancestor of the physical cwd (inclusive) holds a `.git` entry."""
    d = _physical_cwd()
    if not d.startswith("/"):
        return False  # need an absolute path to walk to / safely
    while True:
        if os.path.exists(f"{d}/.git"):
            return True
        if d == "/":
            return False
        d = os.path.dirname(d)


def abort_if_cwd_core_bare(ctx) -> None:
    """Abort when the cwd (possibly a subdir) is inside a corrupt core.bare=true working checkout."""
    bare = ctx.git("rev-parse", "--is-bare-repository", stderr="devnull")
    if (bare.out.rstrip("\n") if bare.rc == 0 else "false") != "true":
        return
    # A genuine bare repo (or a subdir of one) is inside its git dir and has no ancestor `.git`:
    # core.bare=true is legitimate there. Default a rev-parse hiccup to "false" so the guard keeps firing.
    inside = ctx.git("rev-parse", "--is-inside-git-dir", stderr="devnull")
    if (inside.out.rstrip("\n") if inside.rc == 0 else "false") == "true":
        return
    if not cwd_has_ancestor_dotgit():
        return
    here = _physical_cwd()
    die_core_bare(ctx, here, "current checkout", core_bare_fix_cmd(ctx, here))


# --------------------------------------------------------------------------- provider dispatch

def ship_origin_host(h: str) -> str:
    """The lowercase HOST of `scheme://[user@]host[:port]/path` or scp-style `[user@]host:path`, else ""."""
    if "://" in h:
        h = h.split("://", 1)[1]
        h = h.split("/", 1)[0]
        h = h.rsplit("@", 1)[-1]
        h = h.split(":", 1)[0]
    elif ":" in h:
        h = h.split(":", 1)[0]
        h = h.rsplit("@", 1)[-1]
    else:
        h = ""
    return "".join(c.lower() if "A" <= c <= "Z" else c for c in h)  # LC_ALL=C tr: ASCII only


def provider_wants_forgejo_probe(ctx) -> bool:
    """True when origin must be probed as a Forgejo instance (bash return 0)."""
    provider = ctx.env.get("SHIP_PROVIDER", "") or "auto"
    if provider == "github":
        return False
    if provider == "forgejo":
        return True
    if provider != "auto":
        raise ctx.refuse(f"Refusing: SHIP_PROVIDER='{provider}' — expected github, forgejo or auto.")
    raw = ctx.ship_origin_url_raw
    if raw == "" or raw.startswith(("/", "./", "../", "file://")):
        return False
    if ship_origin_host(raw) in ("github.com", "www.github.com", "ssh.github.com"):
        return False
    return True


def forgejo_init_state(ctx) -> None:
    """Create the per-run Forgejo evidence ledger (refuse if impossible) and register its EXIT cleanup."""
    state_dir = ""
    try:
        state_dir = tempfile.mkdtemp(prefix="ship-forgejo.", dir=ctx.env.get("TMPDIR") or "/tmp")
    except OSError:
        state_dir = ""
    ctx.ship_forgejo_state_dir = state_dir
    ok = bool(state_dir)
    if ok:
        try:
            open(f"{state_dir}/fatal.log", "w").close()
            open(f"{state_dir}/calls.log", "w").close()
            with open(f"{state_dir}/ready", "w") as fh:
                fh.write("ready\n")
        except OSError:
            ok = False
    if not ok:
        raise ctx.refuse(
            f"Refusing: could not create the Forgejo provider's per-run evidence ledger under "
            f"{ctx.env.get('TMPDIR') or '/tmp'} — without it a swallowed provider failure would be invisible."
        )
    ctx.on_exit(lambda code: forgejo_cleanup(ctx, code))


def _count_lines(path: str, accept: tuple[str, ...]) -> int:
    try:
        with open(path, encoding=ENC["encoding"], errors=ENC["errors"]) as fh:
            return sum(1 for line in fh.read().split("\n") if line in accept)
    except OSError:
        return 0


def forgejo_cleanup(ctx, code: int) -> None:
    """EXIT handler: let started calls record their outcome (bounded), summarize on refusal, remove the ledger."""
    d = getattr(ctx, "ship_forgejo_state_dir", "")
    if not d or not os.path.isdir(d):
        return
    i = 0
    while True:
        started = _count_lines(f"{d}/calls.log", ("start",))
        finished = _count_lines(f"{d}/calls.log", ("ok", "fail"))
        if started <= finished or i >= 100:
            break
        time.sleep(0.1)
        i += 1
    if code != 0:
        failed = _count_lines(f"{d}/calls.log", ("fail",))
        tail = f", {started - finished} still running at exit" if started > finished else ""
        ctx.err(
            f"[ship] forgejo evidence ledger: {started} call(s) started, {finished - failed} completed, "
            f"{failed} failed{tail}."
        )
        try:
            with open(f"{d}/fatal.log", encoding=ENC["encoding"], errors=ENC["errors"]) as fh:
                fatal = fh.read()
        except OSError:
            fatal = ""
        if fatal:
            lines = fatal.split("\n")
            if lines[-1] == "":
                lines.pop()
            for line in lines:
                ctx.err(f"[ship]   - {line}")
    shutil.rmtree(d, ignore_errors=True)


def _append(path: str, text: str) -> bool:
    try:
        with open(path, "a", encoding=ENC["encoding"], errors=ENC["errors"]) as fh:
            fh.write(text)
        return True
    except OSError:
        return False


def _mark_broken(d: str) -> None:
    _append(f"{d}/broken", "broken\n")


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def forgejo_gh(ctx, args: list[str], **kw) -> Result:
    """The bash `gh()` shadow for a Forgejo origin: run forgejo_provider.py `gh ARGS` and keep the evidence
    ledger (start/ok/fail per call, fatal.log reasons). Returns Result(rc, stdout, stderr)."""
    d = ctx.ship_forgejo_state_dir
    if not _append(f"{d}/calls.log", "start\n"):
        _mark_broken(d)
        ctx.err("[ship] forgejo provider: cannot write the evidence ledger — refusing this gh call.")
        return Result(1)
    before = _file_size(f"{d}/fatal.log")
    # Stdout is held until the outcome is in the ledger (the provider's output is captured whole).
    mode = kw.pop("stdout", "capture")
    res = ctx.cmd([ctx.python, "-I", ctx.ship_forgejo_py, "gh", *args], stdout="capture", **kw)
    rc = res.rc
    out = res.out
    if rc != 0:
        if not _append(f"{d}/calls.log", "fail\n"):
            _mark_broken(d)
        # The provider records its own detailed reason; write a line only when it could not.
        after = _file_size(f"{d}/fatal.log")
        if after <= before or before < 0:
            head = "".join(f"{a} " for a in args[:3]).replace("\n", " ")
            if not _append(f"{d}/fatal.log", f"gh {head}: exit {rc} (no reason recorded by the provider)\n"):
                _mark_broken(d)
    else:
        if not _append(f"{d}/calls.log", "ok\n"):
            _mark_broken(d)
    if mode == "inherit":
        ctx.out(out, end="")
        out = ""
    elif mode == "devnull":
        out = ""
    return Result(rc, out, res.err)


def _dispatch_provider(ctx) -> None:
    ctx.provider_resolved = "github"
    ctx.ship_forgejo_py = os.path.join(ctx.self_dir, "forgejo_provider.py")
    origin = ctx.git("remote", "get-url", "origin", stderr="devnull")
    ctx.ship_origin_url_raw = origin.out.rstrip("\n") if origin.rc == 0 else ""
    if not provider_wants_forgejo_probe(ctx):
        return
    if shutil.which("python3", path=ctx.env.get("PATH")) is None:
        raise ctx.refuse("Refusing: python3 is required for the Forgejo provider.")
    detect = ctx.cmd(
        [ctx.python, "-I", ctx.ship_forgejo_py, "detect"],
        env={**ctx.env, "SHIP_FORGEJO_ORIGIN_URL": ctx.ship_origin_url_raw},
    )
    if detect.rc != 0:
        raise ctx.refuse(
            "Refusing: origin is not github.com and was not confirmed as a Forgejo instance (see above) — "
            "refusing rather than letting gh pick another remote. For GitHub Enterprise set SHIP_PROVIDER=github."
        )
    ctx.ship_forgejo_id = detect.out.rstrip("\n")
    if ctx.repo_flag or ctx.env.get("GH_SHIP_REPO", "") or ctx.env.get("GH_REPO", ""):
        raise ctx.refuse(
            "Refusing: --repo / GH_SHIP_REPO / GH_REPO are GitHub-only; for a Forgejo PR run ship from that "
            "repository's checkout."
        )
    if ctx.merge_author_email_set:
        raise ctx.refuse("Refusing: --author-email is GitHub-only; the Forgejo merge API takes no author.")
    ctx.provider_resolved = "forgejo"
    forgejo_init_state(ctx)
    ctx.env["SHIP_FORGEJO_STATE_DIR"] = ctx.ship_forgejo_state_dir
    ctx.env["SHIP_FORGEJO_ORIGIN_URL"] = ctx.ship_origin_url_raw
    ctx.out(f"[ship] provider: {ctx.ship_forgejo_id} (gh calls routed through ci/ship/forgejo_provider.py)")


# --------------------------------------------------------------------------- cross-repo

_ORIGIN_SED_1 = re.compile(r".*github\.com[:/](.*)\.git$", re.S)
_ORIGIN_SED_2 = re.compile(r".*github\.com[:/](.*)$", re.S)


def _derive_cwd_origin_repo(ctx) -> None:
    # `git remote get-url` exits non-zero with NO origin; assign first, transform second.
    res = ctx.git("remote", "get-url", "origin", stderr="devnull")
    ctx.cwd_origin_url = res.out.rstrip("\n") if res.rc == 0 else ""
    repo = _ORIGIN_SED_1.sub(r"\1", ctx.cwd_origin_url, count=1)
    repo = _ORIGIN_SED_2.sub(r"\1", repo, count=1)
    if "@" in repo or ":" in repo or repo == "" or repo.count("/") >= 2:
        repo = ""
    ctx.cwd_origin_repo = repo


def _resolve_main_checkout(ctx) -> None:
    configured = ctx.env.get("SHIP_MAIN_CHECKOUT", "")
    if configured:
        ctx.main_checkout = configured
        return
    # `awk '{print $2; exit}'`: the second whitespace-delimited field of the first `worktree ` line.
    res = ctx.git("worktree", "list", "--porcelain")
    if res.rc != 0:
        raise ShipExit(res.rc)
    path = ""
    for line in res.out.split("\n"):
        if line.startswith("worktree "):
            fields = re.split(r"[ \t]+", line)
            path = fields[1] if len(fields) > 1 else ""
            break
    ctx.main_checkout = path


def _resolve_pr_state(ctx) -> None:
    res = ctx.gh(
        "pr", "view", ctx.pr,
        "--json", "headRefName,state,mergeable,isCrossRepository,mergeStateStatus",
        "-q", "[.headRefName,.state,.mergeable,.isCrossRepository,.mergeStateStatus]|@tsv",
    )
    # `read -r` hitting EOF (gh failed / no newline-terminated line) returns 1 and set -e aborts.
    if "\n" not in res.out:
        raise ShipExit(1)
    line = res.out.split("\n", 1)[0]
    # IFS is space/tab/newline, so runs of tabs collapse and empty fields shift left; the last
    # variable takes the remainder.
    fields = re.split(r"[ \t]+", line.strip(" \t"), maxsplit=4)
    fields += [""] * (5 - len(fields))
    ctx.branch, ctx.state, ctx.mergeable, ctx.cross_repo, ctx.merge_state = fields
    ctx.merge_state = ctx.merge_state.rstrip(" \t")
    if not ctx.branch:
        raise ctx.refuse(f"Could not resolve PR #{ctx.pr}")
    if ctx.state != "OPEN":
        raise ctx.refuse(f"Refusing: PR #{ctx.pr} is {ctx.state}, not OPEN.")
    if ctx.mergeable == "CONFLICTING":
        raise ctx.refuse(f"Refusing: PR #{ctx.pr} is CONFLICTING — resolve the conflict first.")
    if ctx.merge_state == "BEHIND":
        raise ctx.refuse(
            f"Refusing: PR #{ctx.pr} head is BEHIND its base. Update it "
            f"(gh pr update-branch {ctx.pr} --rebase), wait for CI, re-run."
        )


# --------------------------------------------------------------------------- run

def run(ctx) -> None:
    # Helper-script paths, derived ONLY from this script's own location (fail closed if copied out).
    ctx.ship_hatch_py = os.path.join(ctx.self_dir, "review_quorum_hatch.py")
    ctx.ship_skip_ci_hatch_py = os.path.join(ctx.self_dir, "skip_ci_hatch.py")
    ctx.ship_external_review_hatch_py = os.path.join(ctx.self_dir, "external_review_hatch.py")
    ctx.ship_forgejo_py = os.path.join(ctx.self_dir, "forgejo_provider.py")
    ctx.usage = USAGE

    ctx.resolve_threads = ctx.env.get("SHIP_RESOLVE_ADDRESSED_THREADS", "") in ("1", "true", "yes")
    parse_args(ctx)
    validate_task_config(ctx)

    ctx.default_branch = ctx.env.get("SHIP_DEFAULT_BRANCH", "") or "main"
    ctx.merge_method = ctx.env.get("SHIP_MERGE_METHOD", "") or "squash"
    ctx.ui_path_regex = ctx.env.get("SHIP_UI_PATH_REGEX", DEFAULT_UI_PATH_REGEX)  # unset-only default

    # Guard the CURRENT directory FIRST: `rev-parse --show-toplevel` dies opaquely under core.bare=true.
    abort_if_cwd_core_bare(ctx)

    top = ctx.git("rev-parse", "--show-toplevel")
    if top.rc != 0:
        raise ShipExit(top.rc)
    ctx.root = top.out.rstrip("\n")
    os.chdir(ctx.root)

    _dispatch_provider(ctx)

    # Under Forgejo the `gh` shell function shadows the binary, so the presence check always passes.
    if ctx.provider_resolved != "forgejo" and shutil.which("gh", path=ctx.env.get("PATH")) is None:
        raise ctx.refuse("gh CLI not found")

    _derive_cwd_origin_repo(ctx)

    # Thread --repo through EVERY gh call via GH_REPO (GH_SHIP_REPO is a test/override hook).
    if ctx.repo_flag:
        ctx.env["GH_REPO"] = ctx.repo_flag
    elif ctx.env.get("GH_SHIP_REPO", ""):
        ctx.env["GH_REPO"] = ctx.env["GH_SHIP_REPO"]

    # Foreign-ness comes from the EFFECTIVE gh target (--repo, else GH_SHIP_REPO, else ambient GH_REPO).
    ctx.foreign_repo_invoke = False
    ctx.effective_target = ctx.repo_flag or ctx.env.get("GH_SHIP_REPO", "") or ctx.env.get("GH_REPO", "")
    if ctx.effective_target and ctx.effective_target != ctx.cwd_origin_repo:
        ctx.foreign_repo_invoke = True

    _resolve_main_checkout(ctx)
    abort_if_core_bare(ctx, ctx.main_checkout, "main checkout")

    _resolve_pr_state(ctx)
