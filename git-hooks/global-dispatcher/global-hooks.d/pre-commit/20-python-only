#!/usr/bin/env python3
# global-hook-id: python-only
# global-hook-protected: true
"""python-only: rig-ecosystem tools are written in Python.

Owner decision 2026-06-14 ("Okay, we're moving the tools to Python"), restated 2026-10-17
("all rig-ecosystem tools must be in Python"); see AGENTS.md "Python-only tools" and
agent-tools#945. This one stdlib file is both carriers of the rule:

  * the global pre-commit fragment: global-hooks.d/pre-commit/20-python-only is a
    byte-identical copy of this file (a regular file, so any install method works; a test
    keeps the two equal). With no arguments it checks the STAGED diff;
  * the CI slot ci/python-only: ``--base <ref> --head <sha> --origin <url>`` checks the
    PR's base...head diff (the workflow runs the trusted base-branch copy).

Scope: a repo whose origin sits in an owner namespace and whose name ends in ``-cli`` or is
listed in REPOS. Product repos (apps, sites, bots) are out of scope and pass silently.

Flagged: added/changed files in a non-Python language by extension, scripts whose shebang
runs node/bun/deno/ruby/..., and shell scripts longer than SHELL_LINE_LIMIT lines.
Deletions pass, and so does an unchanged move of a file that was already non-compliant where
it was. A path is exempt only through an Allow entry with a reason; only code whose host
forces another language belongs there (omp/opencode/pi extensions, browser assets, the
oxlint plugin API, ...). The registry lives here, centrally, so an exemption or a warn-mode
grant is an agent-tools PR, never a self-service edit in the repo being checked.

Exit: 0 pass / warn / out of scope; 1 blocked; 2 git error or an invalid registry.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import PurePosixPath

DECISION = (
    "Owner decision 2026-06-14, restated 2026-10-17: every rig-ecosystem tool is written in "
    "Python\n  (\"Okay, we're moving the tools to Python\"; \"all rig-ecosystem tools must be "
    "in Python\")."
)
POLICY_REF = 'agent-tools AGENTS.md "Python-only tools"; https://git.hyperide.ai/ultrabricks/agent-tools/issues/945'
REGISTRY_REF = "agent-tools ci/python-only/python_only.py (REPOS)"

SHELL_LINE_LIMIT = 40

OWNER_NAMESPACES = frozenset(
    {"git.hyperide.ai/ultrabricks", "github.com/alex-mextner", "github.com/ultrabricks"}
)

NON_PYTHON_EXT = {
    ".ts": "TypeScript", ".tsx": "TypeScript", ".mts": "TypeScript", ".cts": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".vue": "JavaScript component", ".svelte": "JavaScript component", ".astro": "JavaScript component",
    ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".swift": "Swift", ".kt": "Kotlin", ".kts": "Kotlin",
    ".java": "Java", ".scala": "Scala", ".clj": "Clojure", ".lua": "Lua", ".pl": "Perl", ".pm": "Perl",
    ".php": "PHP", ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".cxx": "C++", ".hpp": "C++",
    ".hh": "C++", ".m": "Objective-C", ".mm": "Objective-C", ".cs": "C#", ".fs": "F#",
    ".zig": "Zig", ".nim": "Nim", ".dart": "Dart", ".ex": "Elixir", ".exs": "Elixir",
    ".erl": "Erlang", ".hs": "Haskell", ".jl": "Julia", ".r": "R", ".ml": "OCaml",
    ".ps1": "PowerShell", ".psm1": "PowerShell", ".awk": "AWK", ".tcl": "Tcl", ".groovy": "Groovy",
}
SHELL_EXT = frozenset({".sh", ".bash", ".zsh", ".ksh", ".fish"})
SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "mksh", "fish"})
OTHER_INTERPRETERS = {
    "node": "JavaScript", "nodejs": "JavaScript", "bun": "JavaScript/TypeScript (Bun)",
    "deno": "JavaScript/TypeScript (Deno)", "tsx": "TypeScript", "ts-node": "TypeScript",
    "npx": "JavaScript/TypeScript (npx)", "bunx": "JavaScript/TypeScript (bunx)",
    "pnpx": "JavaScript/TypeScript (pnpx)", "ruby": "Ruby", "perl": "Perl", "php": "PHP",
    "lua": "Lua", "osascript": "AppleScript", "pwsh": "PowerShell", "powershell": "PowerShell",
    "awk": "AWK", "gawk": "AWK", "mawk": "AWK", "tclsh": "Tcl", "groovy": "Groovy", "Rscript": "R",
}
# env options that take the next token as their operand (GNU and BSD env).
_ENV_SHORT_WITH_OPERAND = frozenset("uCPa")
_ENV_LONG_WITH_OPERAND = frozenset({"--unset", "--chdir", "--argv0"})


@dataclass(frozen=True)
class Allow:
    glob: str
    reason: str


@dataclass(frozen=True)
class RepoPolicy:
    name: str
    mode: str = "block"
    port_ticket: str = ""
    allow: tuple[Allow, ...] = field(default_factory=tuple)


_BROWSER = "browser code: the browser only runs JavaScript"
_OMP_HOST = "runs inside the omp extension host, which loads JavaScript/TypeScript"
_OMP_HOST_TESTS = "tests of the code that runs inside the omp extension host"

# Repos by name. A repo in an owner namespace whose name ends in -cli is in scope even when it
# is not listed here (block, no exemptions); list it only to add exemptions or a warn grant.
REPOS: dict[str, RepoPolicy] = {
    p.name: p
    for p in (
        RepoPolicy(
            "agent-tools",
            allow=(
                Allow("lib/omp_hook_bridge/extension.ts", "omp extension: omp loads extensions as TypeScript"),
                Allow("lib/opencode_hook_bridge/plugin.js", "opencode plugin: opencode loads plugins as JavaScript"),
                Allow("pi-extensions/**", "pi extensions and their tests: pi loads extensions as TypeScript"),
                Allow("tools/oxlint/**", "oxlint JS plugin: oxlint's plugin API is JavaScript/TypeScript"),
                Allow("oxlint.config.ts", "oxlint config: oxlint reads its config as TypeScript"),
                Allow("linters/oxc/oxlint.config.ts", "oxlint config template shipped to TS repos"),
                Allow("skills/by-stack/**", "code samples inside stack-specific skills for projects in that language"),
            ),
        ),
        RepoPolicy("text-lint"),
        RepoPolicy("claude-accounts"),
        RepoPolicy(
            "rig-cli",
            allow=(
                # Today the template is a string inside riglib/omp_guard.py; this keeps it legal
                # if it moves into its own file.
                Allow(
                    "riglib/**/rig-permissions-guard*.ts",
                    "omp extension template (rendered to ~/.omp/agent/extensions): omp loads extensions as TypeScript",
                ),
            ),
        ),
        RepoPolicy(
            "tg-cli",
            mode="warn",
            port_ticket="https://git.hyperide.ai/ultrabricks/tg-cli/issues/768",
            allow=(
                Allow("extensions/omp-live.ts", "omp extension loader: omp loads extensions as TypeScript"),
                Allow("features/tg-ctl/omp-live-extension.mjs", _OMP_HOST),
                Allow("features/tg-ctl/omp-live-runtime.mjs", _OMP_HOST),
                Allow("tests/omp-live-extension.test.mjs", _OMP_HOST_TESTS),
                Allow("tests/omp-live-runtime.test.mjs", _OMP_HOST_TESTS),
                Allow("tests/omp-live-limits.test.mjs", _OMP_HOST_TESTS),
                Allow("tests/omp-live-reload.test.mjs", _OMP_HOST_TESTS),
                Allow("tests/omp-live-bounded-read.test.mjs", _OMP_HOST_TESTS),
                Allow("tests/omp-live-fixture.mjs", _OMP_HOST_TESTS),
            ),
        ),
        RepoPolicy(
            "review-cli",
            allow=(
                Allow("reviewlib/dashboard/assets/**", _BROWSER),
                Allow("reviewlib/specweb/static/**", _BROWSER),
                Allow("tests/dashboard_app.test.js", "tests of the dashboard browser code"),
                Allow("tests/specweb_sw.test.js", "tests of the specweb service worker"),
                Allow("reviewlib/qa/ext_runner/**", "runs inside the VS Code extension host"),
                Allow("tests/fixtures/qa/**", "fixture projects standing in for the code under QA"),
            ),
        ),
        RepoPolicy(
            "3d-cli",
            allow=(
                Allow("lib/web/static/**", _BROWSER),
                Allow("integrations/homeassistant/www/**", "Home Assistant Lovelace cards run in the browser"),
                Allow("integrations/homeassistant/tests/**/browser_checks.js", "evaluated inside the browser page"),
            ),
        ),
        RepoPolicy(
            "stt-cli",
            allow=(
                Allow("stt_cli/helper_app/**", "macOS helper: the Speech/AVFoundation APIs it uses are Swift-only"),
                Allow("site/**", "the project's website (browser assets and its static build), not tool code"),
            ),
        ),
    )
}

# The exact files `rig apply` vendors from agent-tools ci/ into other repos (exact paths, not a
# ci/** glob, so a repo cannot hide its own shell tool under ci/). They are edited at their
# source in agent-tools, where this exemption deliberately does not apply. A test keeps this
# list equal to what agent-tools ships.
VENDORED_FILES: tuple[str, ...] = (
    ".github/scripts/checklist-gate.mjs",
    "ci/ai-review/ai-review.sh",
    "ci/copilot-findings/copilot-findings.sh",
    "ci/dependency-review/dep-audit.sh",
    "ci/leftover-grep/leftover-grep.sh",
    "ci/license-policy/license-audit.sh",
    "ci/review-threads/review-threads.sh",
    "ci/sast/sast.sh",
    "ci/screenshots/require-screenshots.sh",
    "ci/secret-scan/secret-scan.sh",
    "ci/trivy/trivy-scan.sh",
)
VENDORED_ALLOW: tuple[Allow, ...] = tuple(
    Allow(path, "CI gate script vendored by rig apply from agent-tools ci/") for path in VENDORED_FILES
)
VENDOR_SOURCE_REPO = "agent-tools"

# Host-forced code recognisable by its layout alone, exempt in every in-scope repo. Kept to
# the installed GNOME layout: a bare `*@*` directory would also match `pkg@1.2/` or `@scope/`.
HOST_FORCED_ALLOW: tuple[Allow, ...] = (
    Allow(
        "**/gnome-shell/extensions/*@*/**",
        "GNOME Shell extension (gnome-shell/extensions/<uuid>/): GNOME Shell loads extensions as JavaScript",
    ),
    # rig apply writes these into any repo that enables linters.rules (riglib/plan.py):
    # generated output at fixed paths, owned by rig, not the repo's tool code.
    Allow("oxlint.config.ts", "oxlint config generated by rig apply: oxlint reads its config as TypeScript"),
    Allow(
        "tools/oxlint/anti-slop/**",
        "anti-slop oxlint plugin vendored by rig apply: oxlint's plugin API is JavaScript/TypeScript",
    ),
)

# Known hosts that ssh aliases decorate (`github.com-work`, `git.hyperide.ai_alt`).
_OWNER_HOSTS = tuple(sorted({ns.split("/")[0] for ns in OWNER_NAMESPACES}))


def validate_registry(repos: dict[str, RepoPolicy] | None = None) -> list[str]:
    problems: list[str] = []
    for name, policy in (REPOS if repos is None else repos).items():
        if policy.mode not in ("block", "warn"):
            problems.append(f"{name}: mode must be block or warn, got {policy.mode!r}")
        if policy.mode == "warn" and not policy.port_ticket.startswith("https://"):
            problems.append(f"{name}: warn mode needs a port_ticket URL")
        for entry in policy.allow:
            if not entry.reason.strip():
                problems.append(f"{name}: allow {entry.glob!r} has no reason")
    return problems


_SCP_RE = re.compile(r"^(?:[\w.-]+@)?([\w.-]+):(.+)$")


def repo_identity(url: str) -> tuple[str, str] | None:
    """(host/owner, name) of a remote URL, lowercased; None for local paths and junk."""
    url = url.strip()
    m = re.match(r"^[a-z][a-z0-9+.-]*://(?:[^@/]*@)?([^/:]+)(?::\d+)?/(.+)$", url, re.I)
    if m is None and "://" not in url:
        m = _SCP_RE.match(url)  # [user@]host:owner/name — the user may come from ~/.ssh/config
    if m is None:
        return None
    host, path = m.group(1).lower(), m.group(2)
    for known in _OWNER_HOSTS:  # an ~/.ssh/config alias like github.com-work is still github.com
        if host.startswith((known + "-", known + "_")):
            host = known
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    name = parts[-1].lower()
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return f"{host}/{parts[-2]}".lower(), name


def policy_for(origin: str) -> RepoPolicy | None:
    ident = repo_identity(origin)
    if ident is None or ident[0] not in OWNER_NAMESPACES:
        return None
    name = ident[1]
    if name in REPOS:
        return REPOS[name]
    if name.endswith("-cli"):
        return RepoPolicy(name)
    return None


def glob_match(glob: str, path: str) -> bool:
    """`**/` spans zero or more directories, `**` any text, `*` and `?` one path segment."""
    out = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.fullmatch("".join(out), path) is not None


def _program(token: str) -> str:
    return re.sub(r"[0-9.]+$", "", os.path.basename(token))


def _interpreter(first_line: bytes) -> str | None:
    if first_line.startswith(b"\xef\xbb\xbf"):  # UTF-8 BOM
        first_line = first_line[3:]
    if not first_line.startswith(b"#!"):
        return None
    # `env -S "node --flag"` quotes its command; the quotes are not part of the program name.
    tokens = [t.strip("\"'") for t in first_line[2:].decode("utf-8", "replace").split()]
    if not tokens:
        return None
    prog = _program(tokens[0])
    if prog != "env":
        return prog
    # env runs the first operand after its options: skip option operands (`env -u VAR`,
    # `-C dir`, BSD `-P path`) and VAR=value assignments. `-S` splits the rest of the line,
    # which the whitespace split above already did; an attached split string (`-Sbash`,
    # `--split-string=bash`) is the command itself, so it goes back into the queue.
    queue = tokens[1:]
    while queue:
        token = queue.pop(0)
        if token == "--":
            continue
        if token.startswith("--"):
            name, eq, value = token.partition("=")
            if name == "--split-string" and eq:
                queue[:0] = [t.strip("\"'") for t in value.split()]
            elif name in _ENV_LONG_WITH_OPERAND and not eq and queue:
                queue.pop(0)
            continue
        if token.startswith("-") and len(token) > 1:
            for i, ch in enumerate(token[1:], start=2):
                if ch == "S":
                    queue[:0] = [t.strip("\"'") for t in token[i:].split()]  # [] if none attached
                    break
                if ch in _ENV_SHORT_WITH_OPERAND:
                    if i == len(token) and queue:  # operand is the next token, not glued (`-uVAR`)
                        queue.pop(0)
                    break
            continue
        if "=" in token:
            continue
        return _program(token)
    return None


def _line_count(data: bytes) -> int:
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _suffix(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def needs_content(path: str) -> bool:
    """Whether classify() looks past the path (shebang, line count) for this file."""
    return _suffix(path) not in NON_PYTHON_EXT


def classify(path: str, data: bytes) -> str | None:
    """Why `path` breaks the rule, or None when it is fine."""
    suffix = _suffix(path)
    if suffix in NON_PYTHON_EXT:
        return f"{NON_PYTHON_EXT[suffix]} source"
    # Every other name (none, .py, .txt, .command, ...) is judged by its shebang: a `.py`
    # that runs under node is not Python.
    interp = _interpreter(data.split(b"\n", 1)[0])
    if interp in OTHER_INTERPRETERS:
        return f"{OTHER_INTERPRETERS[interp]} script"
    if suffix in SHELL_EXT or interp in SHELL_INTERPRETERS:
        lines = _line_count(data)
        if lines > SHELL_LINE_LIMIT:
            return f"shell script, {lines} lines (limit {SHELL_LINE_LIMIT})"
    return None


def allowed(policy: RepoPolicy, path: str) -> bool:
    entries = policy.allow + HOST_FORCED_ALLOW
    if policy.name != VENDOR_SOURCE_REPO:
        entries += VENDORED_ALLOW
    return any(glob_match(entry.glob, path) for entry in entries)


class GitError(Exception):
    pass


def _git(*args: str) -> bytes:
    proc = subprocess.run(["git", *args], capture_output=True)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


@dataclass(frozen=True)
class Change:
    path: str
    blob: str
    renamed_from: str | None = None  # set for a rename with unchanged content (R100)


_NOT_A_FILE_MODES = ("120000", "160000")  # symlink, submodule (gitlink)


def parse_raw(raw: bytes) -> list[Change]:
    """Changed files worth checking from `git diff --raw -z -M` output."""
    fields = raw.split(b"\0")
    changes: list[Change] = []
    i = 0
    while i < len(fields) and fields[i].startswith(b":"):
        old_mode, new_mode, old_blob, new_blob, status = fields[i][1:].decode().split()
        old_path = fields[i + 1].decode("utf-8", "surrogateescape")
        if status[0] in ("R", "C"):  # rename/copy rows carry a source and a destination path
            path = fields[i + 2].decode("utf-8", "surrogateescape")
            i += 3
        else:
            path = old_path
            i += 2
        if status[0] == "D" or new_mode in _NOT_A_FILE_MODES:
            continue
        # A symlink or gitlink turning into a regular file is new code even with the same blob id.
        was_file = old_mode not in _NOT_A_FILE_MODES
        if status[0] in ("M", "T") and old_blob == new_blob and was_file:  # mode-only change
            continue
        changes.append(Change(path, new_blob, old_path if status == "R100" and was_file else None))
    return changes


# Full object ids (an abbreviated one can turn ambiguous in a big repo), NUL-separated paths.
_RAW_DIFF = ("--raw", "-z", "-M", "--no-abbrev", "--no-ext-diff", "--no-color")


def staged_changes() -> list[Change]:
    changes = parse_raw(_git("diff", "--cached", *_RAW_DIFF))
    merge_head = _git("rev-parse", "--git-path", "MERGE_HEAD").decode().strip()
    if changes and os.path.isfile(merge_head):
        # Resolving a merge: whatever either parent already carries is not new here.
        differs = set(
            _git("diff", "--cached", "--name-only", "-z", "--no-renames", "MERGE_HEAD")
            .decode("utf-8", "surrogateescape")
            .split("\0")
        )
        changes = [c for c in changes if c.path in differs]
    return changes


def range_changes(base: str, head: str) -> list[Change]:
    return parse_raw(_git("diff", *_RAW_DIFF, f"{base}...{head}"))


def read_blobs(shas: list[str]) -> dict[str, bytes]:
    """Blob contents by sha, through one `git cat-file --batch` process."""
    if not shas:
        return {}
    proc = subprocess.run(
        ["git", "cat-file", "--batch"], input="".join(f"{s}\n" for s in shas).encode(), capture_output=True
    )
    if proc.returncode != 0:
        raise GitError(f"git cat-file --batch: {proc.stderr.decode('utf-8', 'replace').strip()}")
    out, pos, blobs = proc.stdout, 0, {}
    try:
        for sha in shas:
            end = out.index(b"\n", pos)
            header = out[pos:end].split()
            if len(header) != 3:
                raise GitError(f"git cat-file --batch: cannot read {sha}: {out[pos:end].decode('utf-8', 'replace')}")
            size = int(header[2])
            blobs[sha] = out[end + 1 : end + 1 + size]
            pos = end + 1 + size + 1
    except ValueError as exc:
        raise GitError(f"git cat-file --batch: unreadable output ({exc})") from exc
    return blobs


def _needs_blob(change: Change) -> bool:
    return needs_content(change.path) or (
        change.renamed_from is not None and needs_content(change.renamed_from)
    )


def violations(policy: RepoPolicy, changes: list[Change]) -> list[tuple[str, str]]:
    candidates = [c for c in changes if not allowed(policy, c.path)]
    blobs = read_blobs(sorted({c.blob for c in candidates if _needs_blob(c)}))
    found = []
    for change in candidates:
        data = blobs[change.blob] if _needs_blob(change) else b""
        why = classify(change.path, data)
        if not why:
            continue
        old = change.renamed_from
        if old is not None and not allowed(policy, old) and classify(old, data):
            continue  # moved unchanged, and already non-compliant where it was: no new code
        found.append((change.path, why))
    return found


def report(policy: RepoPolicy, found: list[tuple[str, str]]) -> str:
    if policy.mode == "warn":
        head = (
            f"python-only: WARNING — non-Python source in {policy.name} (warn mode until its "
            f"Python port lands: {policy.port_ticket}). Not blocking."
        )
    else:
        head = f"python-only: BLOCKED — non-Python source in the rig-ecosystem tool repo {policy.name}."
    lines = [head, f"  {DECISION}"]
    lines += [f"  - {path} — {why}" for path, why in found]
    lines += [
        f"  Fix: write it in Python. Shell glue up to {SHELL_LINE_LIMIT} lines is fine.",
        "  Host-forced code only (an omp/opencode/pi extension, browser assets, a linter plugin",
        f"  API) gets an exemption: an Allow entry with a reason in {REGISTRY_REF}, via an agent-tools PR.",
        f"  Policy: {POLICY_REF}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python-only", description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", help="check base...head instead of the staged diff (CI)")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--origin", help="repo URL that decides scope (default: remote.origin.url)")
    args = parser.parse_args(argv)

    problems = validate_registry()
    if problems:
        print("python-only: invalid registry:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    # Deciding scope runs in every repo on the machine (the fragment is global), so a surprise
    # there must not block an out-of-scope commit: fail open until a policy applies.
    try:
        if subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True).returncode != 0:
            return 0
        origin = args.origin
        if origin is None:
            # `remote get-url` applies url.<x>.insteadOf rewrites; no origin means out of scope.
            got = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True)
            origin = got.stdout.decode("utf-8", "replace").strip() if got.returncode == 0 else ""
        policy = policy_for(origin)
    except Exception as exc:  # noqa: BLE001 — see above: never block a commit we cannot place
        print(f"python-only: skipped, cannot determine the repo's scope ({exc})", file=sys.stderr)
        return 0
    if policy is None:
        return 0
    try:
        changes = range_changes(args.base, args.head) if args.base else staged_changes()
        found = violations(policy, changes)
    except GitError as exc:
        print(f"python-only: {exc}", file=sys.stderr)
        return 2
    if not found:
        return 0
    print(report(policy, found), file=sys.stderr)
    return 0 if policy.mode == "warn" else 1


if __name__ == "__main__":
    sys.exit(main())
