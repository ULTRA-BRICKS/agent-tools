#!/usr/bin/env python3
"""agents-hooks/v1 pre-bash hook — no sed/perl/awk (or `> file`) for editing source.

The hyperide rule (`~/work/hyperide/docs/rules/development.md`): "edits only via Edit/Write;
NO `sed`/`perl`/`awk` for editing files." A Bash in-place edit (`sed -i`, `perl -i`, `gawk -i
inplace`) or a redirect that OVERWRITES an existing tracked source file (`awk '...' f > f`,
`cmd > src.ts`) bypasses the Edit/Write tools — no diff to review, no formatter, no
file-state tracking. This gate BLOCKs that and points the agent at Edit/Write instead.

What is BLOCKED (decided from a PARSED command, never a raw substring):
  - an in-place stream editor on a tracked source file: `sed -i …`, `perl -i …`,
    `gawk -i inplace …` (also `--in-place`, GNU's `-i.bak`, perl's `-pi`/`-i.orig`, and `-i`
    clustered after other flags: `sed -Ei`/`-ri`/`-ni`). A GLOB operand (`sed -i … *.ts`) is
    expanded against the cwd and blocks if any match is a tracked source (the bulk-edit idiom).
  - a `> file` / `>> file` (or `>|` / `>& file`) redirect whose target is an EXISTING git-TRACKED
    SOURCE file (`awk '{…}' app.ts > app.ts`, `sed 's/a/b/' src/x.py > src/x.py`, `… >app.ts`)
  - `tee FILE` / `tee -a FILE` / `dd of=FILE` writing a tracked source file — the common
    pipe-to-a-file workaround once `>`/`sed -i` are blocked
  - the same wrapped in `bash -c '…'` / `sh -c '…'` — incl. the CLUSTERED form (`bash -ec`,
    `sh -lc`) — the inner script is re-parsed, nested too

What is ALLOWED (the rule is about EDITING, not reading or generating):
  - read-only filters: `sed -n '1,5p' f`, `awk '{print $1}' f | sort`, `grep -v x f` (no `-i`,
    no redirect to a tracked source file)
  - a redirect to /tmp, a brand-NEW file, or a NON-source file (`… > /tmp/x`, `… > out.log`,
    `… > notes.md`, `… > new_file.ts` that isn't tracked yet) — generating, not editing
  - `sed -i` etc. targeting an untracked / non-source path (scratch, build artifact)

Decided from the PARSED command, not a raw string match (codex found raw-string bypasses in
the #59 siblings — do NOT repeat them): shell COMMENTS are stripped ONCE up front (each
word-boundary `#` to its newline, so a multi-line command's later lines still parse and a `$(…)`
in a comment is never run), then the command is split on real separators, each segment is
tokenized with shlex, leading wrappers + a `VAR=val` assignment prefix are peeled, and
`-i`/redirect targets are read off the argv. So `echo "use sed -i"` (the words in a string), `ls #
sed -i later` (a comment), and `git commit -m 'switch to sed -i'` (a message) never trip it, while
`LANG=C sed -i … app.ts` (a common assignment prefix) IS caught.

A command SUBSTITUTION (`echo $(sed -i … f)` / backticks) is recursed into like `bash -c '…'`.

NOT covered (known scope boundary): non-editor file-overwrite idioms — `cp`/`mv`/`install` to a
tracked path, `patch < diff`, `ed -s`, `python -c "open('f','w')…"`; `find … -exec sed -i {} +` /
`… | xargs sed -i` (operands are `{}`/stdin, NOT in the command); a VARIABLE operand (`"$FILE"`,
value unknown statically); process substitution `<(…)`/`>(…)`; and `eval '…'`. The rule's spirit
still applies; this gate enforces the editors/redirects it names, not every write path (on_error=open).

Subagent-exempt? NO. The rule is about HOW any agent edits a file, orchestrator or subagent
alike — a subagent hand-editing with `sed -i` is exactly what this stops. (Contrast the
delegation gates, which govern the orchestrator only.)

External approval (deny-by-default): there is NO self-service bypass. For a genuine exception,
ASK the human, or request a one-time Telegram approval by setting
`RIG_HATCH_REQUEST_NO_SHELL_FILE_EDIT="<written justification>"` — the hook asks via a trusted
`tg-ctl` and allows ONLY on an explicit approval tap. A blank value or a bare `1`/`true` is
rejected (deny), no Telegram call is made. An agent can request, not self-grant — the human
decides.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command, the repo cwd in event.cwd
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": edit-hygiene discipline, not a security boundary — a crash (a malformed
command shlex can't tokenize, a git call that errors) must never wedge the ability to run a
command. The git-tracked lookup is timeout-bounded and fails toward ALLOW on the redirect
case (unknown tracked-ness → not provably an edit of a tracked source file).
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import re
import shlex
import subprocess  # noqa: S404 — `git ls-files` to tell a tracked source file from a new one
import sys
from pathlib import Path

# SYNC: duplicated in every hatch-using hook so each hook does not need
# a shared helper file under agent-hooks/. Edit every copy together;
# tests/test_hatch_import_hardening.py guards the shared behavior.
_HATCH_MODULE = "agenttools_hatch_escalation"


def _load_hatch_escalation():
    hatch_init = Path(__file__).resolve().parents[2] / "lib" / _HATCH_MODULE / "__init__.py"
    if not hatch_init.is_file():
        raise ImportError(f"cannot load hatch escalation helper from {hatch_init}")
    spec = importlib.util.spec_from_file_location(_HATCH_MODULE, hatch_init)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load hatch escalation helper from {hatch_init}")
    module = importlib.util.module_from_spec(spec)
    previous_modules = {
        name: sys.modules[name]
        for name in tuple(sys.modules)
        if name == _HATCH_MODULE or name.startswith(f"{_HATCH_MODULE}.")
    }
    for name in previous_modules:
        if name != _HATCH_MODULE:
            sys.modules.pop(name, None)
    sys.modules[_HATCH_MODULE] = module
    # Leave the repo-local module installed on success so later imports in this
    # hook process cannot regain a preloaded user/site package or submodule.
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        for name in tuple(sys.modules):
            if name == _HATCH_MODULE or name.startswith(f"{_HATCH_MODULE}."):
                sys.modules.pop(name, None)
        sys.modules.update(previous_modules)
        # A helper that calls sys.exit() at import must not make the hook exit 0 (allow);
        # convert it to an import failure after cleanup. Ctrl-C still propagates.
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise ImportError(f"cannot execute hatch escalation helper from {hatch_init}: {exc}") from exc
    return module


hatch_escalation = _load_hatch_escalation()

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# Per-`git ls-files` call bound, so a single slow/hanging git can't run away. Mirrors
# visual-proof-gate's inner-git timeout (#14). NOTE: this caps each call, not their SUM — a glob
# operand matching many existing-but-untracked source files runs one `git ls-files` per match (the
# `any(...)` doesn't short-circuit until a tracked one is hit), so total git time can exceed the
# descriptor's timeout_ms on a very slow git. That stays correct (the descriptor's own timeout fires
# and on_error=open → allow on the redirect case), it just isn't bounded by GIT_TIMEOUT_S alone.
GIT_TIMEOUT_S = 5

# Source-file extensions the rule protects. A redirect overwriting one of THESE (when tracked)
# is an edit; a redirect to a .log / .md / .json-data / no-extension file is generating output,
# not editing source. Kept broad but source-shaped: code, styles, markup, config-as-code.
SOURCE_EXT = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".py", ".rb", ".go", ".rs", ".java", ".kt", ".kts", ".swift", ".c", ".cc", ".cpp",
    ".h", ".hpp", ".cs", ".php", ".scala", ".clj", ".ex", ".exs", ".lua", ".sh", ".bash",
    ".zsh", ".css", ".scss", ".less", ".html", ".htm", ".xml", ".svg",
    ".yaml", ".yml", ".toml", ".sql", ".graphql", ".proto",
})

# Stream editors that can edit IN PLACE. The in-place flag is read off the PARSED argv below,
# never matched as a raw substring — `echo "sed -i"` / `# sed -i` must not trip it.
_INPLACE_EDITORS = frozenset({"sed", "perl", "awk", "gawk", "mawk"})
# The awk family: its `-i` is gawk's `--include` (load a library), in-place ONLY when the loaded
# library is `inplace` (`gawk -i inplace`). So `gawk -i json` is read-only — detected apart from
# sed/perl, whose `-i` is itself the in-place flag.
_AWK_FAMILY = frozenset({"awk", "gawk", "mawk"})
# Shells whose `-c <script>` argument we re-parse, so `bash -c 'sed -i … f'` is still caught.
_SHELL_RUNNERS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
# How deep a nested `bash -c "bash -c '…'"` is followed before we stop (a forged deep nest can't
# spin; a real one is rarely more than 2 deep).
_MAX_SHELL_DEPTH = 4
# Leading no-op wrappers that prefix the real command (same family the no-long-inline sibling
# peels). We skip the wrapper + its own args so `timeout 5 sed -i … f` / `env X=1 perl -i … f`
# are still seen. A wrapper with a POSITIONAL we don't model is left intact (false-negative is
# safer than a half-peel that hides the editor). `command`/`exec`/`builtin` are shell prefixes that
# run the next command verbatim (`command sed -i … f`) — peeling them exposes the real editor
# (codex: they were an undocumented bypass).
_WRAPPERS = frozenset({"timeout", "env", "nice", "time", "stdbuf", "nohup", "setsid", "unbuffer",
                       "command", "exec", "builtin"})


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"no-shell-file-edit: {msg}\n")


def _strip_comment(segment: str) -> str:
    """Drop a trailing shell comment from one segment, honoring quotes so a `#` INSIDE a quoted
    string (`sed 's/#foo/bar/' f`) is kept, while a real `# …` comment tail is removed. A `#`
    is a comment introducer only at a word boundary (start, or after whitespace)."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(segment):
        ch = segment[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"' and i + 1 < len(segment):
                out.append(segment[i + 1])
                i += 1
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "\\" and i + 1 < len(segment):
            out.append(ch)
            out.append(segment[i + 1])
            i += 1
        elif ch == "#" and (not out or out[-1].isspace()):
            break  # unquoted, word-boundary `#` → comment tail
        else:
            out.append(ch)
        i += 1
    return "".join(out).strip()


def _tokens(segment: str) -> list[str] | None:
    """shlex-tokenize one segment, or None if it won't parse (unbalanced quotes). A segment we
    can't tokenize is NOT silently dropped — main() fails OPEN for the whole command, but only
    after the readable segments are checked first.

    The `_strip_comment` here is a DEFENSIVE fallback, not the primary comment handling: by the
    time a segment reaches `_tokens`, `_matched_edit` has already run the `_strip_comments`
    whole-command pre-pass and `_split_segments` is itself comment-aware, so a top-level segment
    carries no live comment. It is kept (cheaply) so any future caller that hands `_tokens` a raw,
    un-pre-stripped segment still can't be tripped by a trailing `# …` — review flagged the three
    separate `#` readers as a desync risk, so this one is explicitly the redundant safety net.

    Redirect operators are normalized to standalone tokens BEFORE shlex (`f>g` / `f >g` →
    `f > g`), so a redirect target glued to the source word (`awk … app.ts>app.ts`) is parsed,
    not missed — the codex finding that the glued form slipped past `_redirect_targets`."""
    cleaned = _spaced_redirects(_strip_comment(segment))
    if not cleaned:
        return []
    try:
        return shlex.split(cleaned, comments=False)
    except ValueError:
        return None


def _spaced_redirects(segment: str) -> str:
    """Surround every UNQUOTED `>`/`>>` (with an optional leading fd digit) with spaces so shlex
    splits it off as its own token. Quote-aware: a `>` inside a quoted string (`grep 'a > b' f`)
    is left untouched. `>&` (fd dup) is normalized too; the path filter drops `/dev`/`&N` targets."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(segment):
        ch = segment[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == ">":
            # absorb a leading fd digit already emitted (`2>`), then `>` or `>>`.
            if out and out[-1].isdigit() and (len(out) < 2 or out[-2].isspace()):
                fd = out.pop()
            else:
                fd = ""
            op = ">>" if segment[i:i + 2] == ">>" else ">"
            out.append(" ")
            out.append(fd + op)
            out.append(" ")
            i += len(op)
            # drop a force-clobber marker immediately after the operator: bash's `>|` and zsh's `>!`
            # (the platform shell here is zsh, and zsh is in _SHELL_RUNNERS) both force-overwrite an
            # existing file — the `|`/`!` is not part of the target (review finding: `cat x >! app.ts`
            # left `!` as the target and waved app.ts through). The segment splitter already kept a
            # `>|`'s `|` out of a pipe split; a `>!`'s `!` was never a separator, so just skip it here.
            if i < len(segment) and segment[i] in ("|", "!"):
                i += 1
            # `>&word`: if word is a FILENAME (not a digit fd-dup `>&2` or a `>&-` close), it's a
            # stdout+stderr file redirect — keep the `&` off so the filename becomes the target.
            # `>&2`/`>&-` stay as a single `&…` token, which `_is_tracked_source` rejects.
            elif i < len(segment) and segment[i] == "&":
                nxt = segment[i + 1] if i + 1 < len(segment) else ""
                if nxt and not nxt.isdigit() and nxt != "-":
                    i += 1  # skip the `&`; the following filename is the redirect target
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# A leading shell VARIABLE ASSIGNMENT prefix (`LANG=C`, `LC_ALL=C`, `FOO=bar`) sits in front of a
# command without `env` and applies the var to that command. It is the most common legitimate
# prefix AND the simplest total bypass: `LANG=C sed -i … app.ts` would otherwise leave
# head="LANG=C" (an unrecognized command) and wave the in-place edit through (codex HIGH). We peel
# a run of these so the real runner is exposed. Anchored: NAME must be a valid shell identifier and
# the token must contain `=`, so a real argument like `s/a=b/c/` (no leading identifier=`) or a
# flag is never mistaken for an assignment.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _peel_wrappers(tokens: list[str]) -> list[str]:
    """Strip leading no-op wrappers (`timeout 5`, `env X=1`, `nice -n10`, `time`) and bare shell
    variable-assignment prefixes (`LANG=C`, `FOO=bar`) so the real runner sits at the head.
    Conservative: a recognized wrapper's flags + (for env) KEY=VALUE assignments + (for timeout)
    the one duration positional are skipped; an UNMODELLED positional stops the peel (we'd rather
    miss-and-allow than mis-parse)."""
    # Peel a leading run of bare `KEY=VALUE` assignments first (`LANG=C LC_ALL=C sed -i …`).
    j = 0
    while j < len(tokens) and _ASSIGNMENT.match(tokens[j]):
        j += 1
    if j:
        tokens = tokens[j:]
    while tokens and tokens[0] in _WRAPPERS:
        wrapper, rest = tokens[0], tokens[1:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok.startswith("-"):
                if "=" not in tok and tok in ("-s", "-k", "--signal", "--kill-after", "-n", "-u"):
                    i += 1
                i += 1
                continue
            if wrapper == "env" and "=" in tok and not tok.startswith("-"):
                i += 1
                continue
            # `timeout`'s positional is a DURATION (`5`, `5m`, `1.5h`) — consume it only when it
            # looks like one. A non-duration token here means the duration was omitted (an invalid
            # `timeout -s KILL sed …`); consuming it would eat the real command and let `sed -i`
            # through, so we leave it as the head (codex low-pri edge).
            if wrapper == "timeout" and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", tok):
                i += 1  # the duration positional
            break
        if i or rest:
            tokens = rest[i:] if i else rest
        else:
            break
    return tokens


# One-letter options that CONSUME the rest of their cluster as an argument, so a `i` appearing
# AFTER one of these is part of that argument, not a separate in-place flag (`sed -e s/i/x/` →
# the `i` is inside the script; `perl -Ilib` → `i` is inside the include path). Parsing the
# cluster up to the first such option is what tells a real `-i` apart from an `i` in an argument
# — the fix for the codex finding that `-Ei`/`-ri` slipped through and `-Ilib`/`-MMod` over-blocked.
# (awk-family is NOT here — its `-i` is handled by `_awk_has_inplace`, not the cluster walker.)
_ARG_TAKING_CLUSTER_OPT = {
    "sed": frozenset({"e", "f", "l"}),
    "perl": frozenset({"e", "E", "f", "I", "m", "M", "F", "C", "x"}),
}


def _cluster_has_inplace(editor: str, body: str) -> bool:
    """Walk a one-letter option cluster (the token text after a single leading `-`) left to right
    and report whether it carries an in-place `i`. Stops at the first arg-taking option, whose
    remaining text is that option's value — an `i` there is NOT the in-place flag. The `i` itself
    may carry a `.bak`-style extension (`-i.orig`, `-pi.bak`), which ends the cluster.
    """
    arg_taking = _ARG_TAKING_CLUSTER_OPT.get(editor, frozenset())
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "i":
            return True  # an in-place option reached before any arg-taking option
        if ch in arg_taking:
            return False  # the rest of the cluster is this option's argument
        if ch == ".":
            # a `.` before any `i` is not a valid in-place extension start → stop scanning.
            return False
        i += 1
    return False


def _awk_has_inplace(argv: list[str]) -> bool:
    """True if an awk-family argv loads gawk's `inplace` extension. gawk's `-i NAME` / `--include
    NAME` / `--load NAME` loads a library; it is in-place ONLY when NAME is `inplace` — so
    `gawk -i inplace …` blocks but `gawk -i json …` (codex finding) is read-only. Handles the
    value joined (`-iinplace`, `--include=inplace`) or as the next token (`-i inplace`).
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-i", "--include", "--load", "-l"):
            if i + 1 < len(argv) and argv[i + 1] == "inplace":
                return True
            i += 2
            continue
        if tok.startswith(("-i", "--include=", "--load=")):
            # the joined value must be EXACTLY `inplace`, not merely end with it — `gawk -isomeinplace`
            # / `--load=myinplace` loads a differently-named library and is read-only (codex).
            value = tok.split("=", 1)[1] if "=" in tok else tok[2:]
            if value == "inplace":
                return True
        i += 1
    return False


def _has_inplace_flag(editor: str, argv: list[str]) -> bool:
    """True if argv (the editor's own args) carries an in-place flag.

    awk family : delegated to `_awk_has_inplace` — `-i`/`--include`/`--load` is in-place only when
                 it loads the `inplace` extension (`gawk -i inplace`), never `gawk -i json`.
    sed / perl :
    - long form  : `--in-place`, `--in-place=.bak`
    - short form : a `-…` cluster whose first option, before any arg-taking option, is `i`
      (`-i`, `-i.bak`, `-Ei`, `-ri`, `-ni`, perl `-pi`, `-pi.orig`). `_cluster_has_inplace`
      decides, so `sed -e s/i/x/` (i inside the script), `perl -Ilib`/`-MList::Util` (i inside
      an include path / module name) do NOT count.
    A flag only counts when it is an ACTUAL argv token (parsed), not a substring of a string
    operand: `sed 's/-i//' f` has operand `s/-i//`, never an `-i` token → not in-place.
    """
    if editor in _AWK_FAMILY:
        return _awk_has_inplace(argv)
    for tok in argv:
        if not tok.startswith("-") or tok == "-" or tok == "--":
            continue
        if tok.startswith("--in-place"):
            return True
        if tok.startswith("--"):
            continue  # some other long option (`--regexp-extended`, `--posix`) → not in-place
        if _cluster_has_inplace(editor, tok[1:]):
            return True
    return False


def _redirect_targets(tokens: list[str]) -> list[str]:
    """Every file a `>`/`>>` (or `1>`/`2>>`) redirect in this segment writes to. `_spaced_redirects`
    has already split each operator into its own token (`f>g`/`f >g`/`f > g` all → `f > g`), so the
    target is simply the token AFTER each `[fd]>`/`[fd]>>` operator. A dangling operator at the end
    of the segment has no target. `>&2`-style fd dups present as `>` then a `&2` token → the path
    filter downstream drops the `&N`/`/dev` target."""
    targets: list[str] = []
    redir = re.compile(r"^\d*>>?$")
    for i, tok in enumerate(tokens):
        if redir.match(tok) and i + 1 < len(tokens):
            targets.append(tokens[i + 1])
    return targets


# Glob metacharacters that mean an operand is a pattern the shell expands at runtime, not a
# literal path (`sed -i … *.ts` / `src/app.*` / `foo[0-9].js`). The canonical bulk-edit idiom.
_GLOB_META = re.compile(r"[*?\[]")
# A brace group: the shell expands `{app,}.ts` → `app.ts .ts`, `src/{x,y}.py` → `src/x.py src/y.py`.
# Python's `glob` does NOT do brace expansion, so an operand with `{…}` slips past `_GLOB_META` and
# its literal-path check (`{app,}.ts` doesn't exist) → silent allow of a real edit (review finding:
# `sed -i … {app,}.ts` edits app.ts). We expand braces ourselves, then glob/track-check each result.
_BRACE = re.compile(r"\{([^{}]*)\}")


def _expand_braces(path: str) -> list[str]:
    """Expand shell brace groups in `path` into the concrete candidate paths the shell would produce
    (`{app,}.ts` → `['app.ts', '.ts']`, `src/{x,y}.py` → `['src/x.py', 'src/y.py']`). Comma-split
    only — numeric/char ranges (`{1..3}`) are left intact (rarer for filenames; an unexpanded range
    just won't match a tracked file, the safe direction). Bounded so a pathological nest can't blow
    up: once the candidate count crosses a cap, expansion stops and the partial set is returned."""
    results = [path]
    cap = 64
    while True:
        grew = False
        out: list[str] = []
        for cand in results:
            m = _BRACE.search(cand)
            if m and "," in m.group(1):
                pre, post = cand[: m.start()], cand[m.end():]
                out.extend(pre + opt + post for opt in m.group(1).split(","))
                grew = True
            else:
                out.append(cand)
        results = out
        if not grew or len(results) > cap:
            break
    return results


def _is_tracked_source(path: str, cwd: str) -> bool:
    """True when `path` resolves to a tracked source file. SHELL GLOBS (`*.ts`, `src/*.py`) and
    BRACE groups (`{app,}.ts`, `src/{x,y}.py`) are expanded against `cwd`, returning True if ANY
    resulting file is a tracked source — both are canonical bulk-edit idioms and must block when
    they would touch a tracked source (codex/review: globs slipped through `Path('*.ts').exists()`,
    and brace groups slipped through both checks). A pattern with NO tracked-source match does not
    block. A variable (`"$FILE"`) can't be resolved and is a documented boundary, not handled here."""
    if path.startswith("/dev/") or path.startswith("&") or path == "-":
        return False  # /dev sink (incl. /dev/null) or an `>&N` / `>&-` fd dup, never a file edit
    candidates = _expand_braces(path) if "{" in path else [path]
    return any(_glob_or_literal_is_tracked(cand, cwd) for cand in candidates)


def _glob_or_literal_is_tracked(path: str, cwd: str) -> bool:
    """A single brace-free operand: glob-expand it if it carries glob meta, else check it literally."""
    if _GLOB_META.search(path):
        base = Path(os.path.expanduser(path))
        pattern = str(base if base.is_absolute() else Path(cwd) / base)
        try:
            matches = glob.glob(pattern, recursive=True)
        except (OSError, ValueError):
            return False
        return any(_path_is_tracked_source(m, cwd) for m in matches)
    return _path_is_tracked_source(path, cwd)


def _path_is_tracked_source(path: str, cwd: str) -> bool:
    """True only when the LITERAL `path` is an EXISTING git-tracked file with a source extension. A
    new file (not yet tracked), a non-source file (.log/.md/.json-data), or a scratch path under
    /tmp are NOT — overwriting those via `>` is generating output, not editing source. Fails toward
    FALSE (allow) on any git error or an out-of-repo path, so the gate never over-blocks on
    uncertainty (on_error=open spirit).

    Scratch carve-out is GIT-TRACKEDNESS, not a /tmp string prefix: a real codebase can live under
    /tmp (CI runners, agent worktrees), so a `str.startswith('/tmp')` guard would wrongly exempt a
    genuine tracked source file there (codex finding — the earlier guard was both dead on macOS
    after `resolve()` AND over-broad). `git ls-files` already exempts every untracked /tmp scratch
    file, so tracked-ness is the sole, correct authority for the scratch case too.
    """
    p = Path(os.path.expanduser(path))
    abspath = p if p.is_absolute() else Path(cwd) / p
    try:
        norm = abspath.resolve()
    except (OSError, RuntimeError):
        norm = abspath
    if norm.suffix.lower() not in SOURCE_EXT:
        return False
    if not norm.exists():
        return False  # brand-new file → generating, not editing
    try:
        rc = subprocess.run(  # noqa: S603 — fixed argv, path passed as data after `--`
            ["git", "-C", str(norm.parent), "ls-files", "--error-unmatch", "--", norm.name],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return False  # git unavailable / errored → not provably tracked → allow
    return rc.returncode == 0


def _shell_c_script(argv: list[str]) -> str | None:
    """For a shell runner's args, return the `-c` command STRING, or None. `-c` may be a standalone
    token (`bash -c '…'`) OR clustered with other single-letter flags (`bash -ec`, `sh -lc`,
    `bash -xc` — codex: the exact-`-c` match let `bash -ec '…'` bypass the gate entirely). The
    string is the next positional token after the flag(s). A `--` long option (e.g. `--posix`)
    before the cluster is skipped; once a non-flag token is hit without having seen `c`, stop."""
    saw_c = False
    for i, tok in enumerate(argv):
        if saw_c:
            return tok  # the positional right after the flag carrying `c` is the command string
        if tok.startswith("--"):
            continue  # a long option (`--posix`, `--norc`) — not the `-c` cluster
        if tok.startswith("-") and len(tok) > 1:
            if "c" in tok[1:]:
                saw_c = True  # standalone `-c` or clustered `-ec`/`-lc`/`-xc`; script is next token
            continue
        return None  # a non-flag positional before any `c` → no `-c` script here
    return None


def _scan_segment(tokens: list[str], cwd: str, *, _depth: int = 0) -> str | None:
    """Return a short label of the file-edit this segment performs, or None.

    Two block cases: (1) an in-place stream editor on a tracked source file; (2) a `>`/`>>`
    redirect onto a tracked source file. A `bash -c '<script>'` (or clustered `-ec`/`-lc`) is
    recursed into (bounded depth), and the OUTER segment's own redirect is still checked —
    `bash -c '…' > tracked.ts` edits via the outer redirect."""
    tokens = _peel_wrappers(tokens)
    if not tokens:
        return None
    head = os.path.basename(tokens[0])

    # bash -c '<inner script>' → re-parse the inner script. Recurse up to _MAX_SHELL_DEPTH levels so
    # a NESTED `bash -c "bash -c '…'"` is still seen (codex: a single-level cap silently passed the
    # double nest); the cap stops a forged deep nest from spinning. We do NOT early-return here —
    # the outer `bash -c '…' > tracked.ts` redirect (case 2 below) must still be checked.
    if head in _SHELL_RUNNERS and _depth < _MAX_SHELL_DEPTH:
        script = _shell_c_script(tokens[1:])
        if script is not None:
            # Run the inner script through the FULL `_matched_edit` (segments + comment-strip +
            # command substitutions), not a bare segment loop — so `bash -c 'echo $(sed -i … f)'`
            # is caught like a top-level `$(…)` (codex: the manual loop missed substitutions inside
            # bash -c). When the inner shell re-executes, the `$(…)` IS expanded and the edit runs.
            hit, _sub_failed = _matched_edit(script, cwd, _depth=_depth + 1)
            if hit:
                return hit

    # case 1 — in-place editor on a tracked source file
    if head in _INPLACE_EDITORS and _has_inplace_flag(head, tokens[1:]):
        for operand in tokens[1:]:
            if operand.startswith("-") or operand == "inplace":
                continue  # `inplace` is gawk's extension NAME (operand of `-i`), not an edit target
            if _is_tracked_source(operand, cwd):
                return f"`{head} -i` edits the tracked source file `{operand}` in place"
        # in-place flag but no tracked-source operand (scratch / new / non-source) → allow.

    # case 1b — `tee`/`dd` writing a tracked source file: the canonical pipe-to-a-file workaround
    # once `>`/`sed -i` are blocked (codex: this is the most common bypass idiom). `tee FILE…` and
    # `tee -a FILE…` write each positional; `dd of=FILE` writes its `of=` target.
    if head == "tee":
        for operand in tokens[1:]:
            if operand.startswith("-"):
                continue  # -a/--append and friends, not a file
            if _is_tracked_source(operand, cwd):
                return f"`tee` writes the tracked source file `{operand}`"
    if head == "dd":
        for operand in tokens[1:]:
            if operand.startswith("of=") and _is_tracked_source(operand[3:], cwd):
                return f"`dd of=` writes the tracked source file `{operand[3:]}`"

    # case 2 — redirect writing a tracked source file (incl. the OUTER redirect of a bash -c)
    for target in _redirect_targets(tokens):
        if _is_tracked_source(target, cwd):
            return f"a redirect writes the tracked source file `{target}`"
    return None


def _split_segments(command: str) -> list[str]:
    """Split a command line on real shell separators (&&, ||, ;, |, newline). Quote-aware so a
    separator INSIDE a quoted string (`echo 'a|b'`) does not split. Redirect `>`/`>>` are NOT
    separators — they stay with their segment so `_redirect_targets` can read the target.

    COMMENT-aware: an unquoted, word-boundary `#` ends the command line — everything after is a
    shell comment, NOT more segments. This MUST happen during the split, not after: a `;`/`|`
    inside a comment (`echo hi # note; sed -i … f`) would otherwise split off the comment tail as
    a standalone command and falsely flag it as an edit the real shell never runs (codex)."""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    prev_space = True  # start-of-line counts as a word boundary for a leading `#`
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            prev_space = False
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command):
            buf.append(ch)
            buf.append(command[i + 1])
            prev_space = False
            i += 2
            continue
        if ch == "#" and prev_space:
            # unquoted word-boundary `#` → comment to the next newline ONLY (not the whole input):
            # a multi-line command's later lines must still be split (codex: a `break` here dropped
            # a real `sed -i` on a line after a commented one). `_matched_edit` already strips
            # top-level comments; this also covers an inner `bash -c` script that wasn't pre-stripped.
            nl = command.find("\n", i)
            if nl == -1:
                break
            i = nl  # the `\n` itself is handled as a separator on the next iteration
            prev_space = True
            continue
        two = command[i:i + 2]
        if two in ("&&", "||"):
            segments.append("".join(buf))
            buf = []
            prev_space = True
            i += 2
            continue
        # `>|` (force-clobber) and `>&` (stdout+stderr redirect) are NOT a pipe / background op —
        # keep the `|`/`&` with the segment so the redirect target is still seen (codex: `cmd >|
        # tracked.ts` / `cmd >& tracked.ts` must not split into a dangling `cmd >` + `tracked.ts`).
        # The trailing non-space char being `>` marks this case.
        if ch in ("|", "&") and "".join(buf).rstrip().endswith(">"):
            buf.append(ch)
            prev_space = False
            i += 1
            continue
        if ch in (";", "|", "\n", "&"):
            segments.append("".join(buf))
            buf = []
            prev_space = True  # a fresh segment begins → a leading `#` is a comment
            i += 1
            continue
        buf.append(ch)
        prev_space = ch.isspace()
        i += 1
    segments.append("".join(buf))
    return [s for s in segments if s.strip()]


def _command_subs(command: str) -> list[str]:
    """The bodies of every `$(…)` and `` `…` `` command substitution the shell would ACTUALLY run.
    A substitution runs a real sub-command whose output is interpolated — `echo $(sed -i … f)`
    actually edits `f` — so it must be scanned, consistently with `bash -c '…'` recursion.

    QUOTE-AWARE: the shell does NOT expand `$(…)`/backticks inside SINGLE quotes, so a literal
    `'… $(sed -i … f)'` (e.g. an `echo` of example text into a .md) is skipped — scanning it would
    falsely block (codex: this contradicted the 'parsed, not raw substring' principle). Inside
    DOUBLE quotes substitutions ARE expanded, so those are scanned. Nested `$(…)` is depth-counted;
    backticks don't nest."""
    bodies: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command):
            i += 2  # an escaped char (incl. `\$`, `\``) is literal, never a sub introducer
            continue
        if ch in ("'", '"') and quote is None:
            quote = ch
            i += 1
            continue
        if ch == '"' and quote == '"':
            quote = None
            i += 1
            continue
        # `$(…)` and backticks ARE expanded outside single quotes (incl. inside double quotes).
        if ch == "$" and i + 1 < len(command) and command[i + 1] == "(":
            depth = 1
            j = i + 2
            while j < len(command) and depth:
                if command[j] == "(":
                    depth += 1
                elif command[j] == ")":
                    depth -= 1
                j += 1
            bodies.append(command[i + 2:j - 1])
            i = j
            continue
        if ch == "`":
            j = command.find("`", i + 1)
            if j == -1:
                break
            bodies.append(command[i + 1:j])
            i = j + 1
            continue
        i += 1
    return bodies


def _strip_comments(command: str) -> str:
    """The command with every unquoted shell COMMENT (each word-boundary `#` to the next newline)
    removed, the newline kept. ONE shared pre-pass so segment-splitting and command-substitution
    extraction agree on what is a comment — the three earlier readers diverged (`_split_segments`
    broke the whole line, `_command_subs` ignored `#` entirely), which both bypassed a multi-line
    edit AND falsely blocked a `$(…)`/backtick sitting in a comment (codex)."""
    out: list[str] = []
    quote: str | None = None
    prev_space = True
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"' and i + 1 < len(command):
                out.append(command[i + 1])
                i += 1
            prev_space = False
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
            prev_space = False
        elif ch == "\\" and i + 1 < len(command):
            out.append(ch)
            out.append(command[i + 1])
            i += 1
            prev_space = False
        elif ch == "#" and prev_space:
            nl = command.find("\n", i)
            if nl == -1:
                break  # comment runs to end of input
            i = nl  # skip to the newline; the loop re-appends it next iteration
            prev_space = True
            continue
        else:
            out.append(ch)
            prev_space = ch.isspace()
        i += 1
    return "".join(out)


_HEREDOC_OPENER = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _heredoc_terminator(line: str) -> str | None:
    """The terminator WORD of a heredoc opener on `line`, or None. A `<<WORD` counts ONLY when it
    sits in an UNQUOTED, NON-COMMENT position: a `<<` inside a quoted string (`echo "report <<
    TODO"`), a commit message (`git commit -m "fix << EOF"`), or a shell COMMENT (`echo ok # tmpl
    <<EOF`) is literal text, NOT a heredoc — so the rest of a multi-line command must keep being
    scanned (codex/review: the bare regex matched a `<<` in a quote/comment and dropped a real
    `sed -i` on a later line, the exact #59 raw-substring bypass this gate closes).

    Quote- AND comment-aware like the rest of the parser: the scan ignores any `<<` inside quotes,
    STOPS at an unquoted word-boundary `#` (everything after is a comment the shell never reads as a
    heredoc opener), and matches the opener regex only from the first real `<<`. Comment-stripping
    runs AFTER heredoc-stripping in `_matched_edit`, so a heredoc whose body contains a `#` is
    unaffected — its body is removed here before any comment pass touches it."""
    quote: str | None = None
    prev_space = True  # start-of-line is a word boundary for a leading `#`
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"' and i + 1 < len(line):
                i += 1
            prev_space = False
        elif ch in ("'", '"'):
            quote = ch
            prev_space = False
        elif ch == "\\" and i + 1 < len(line):
            i += 1
            prev_space = False
        elif ch == "#" and prev_space:
            return None  # an unquoted, word-boundary `#` starts a comment → no heredoc opener after
        elif ch == "<" and line[i:i + 2] == "<<":
            m = _HEREDOC_OPENER.match(line, i)
            if m:
                return m.group(2)
            # a `<<` not followed by a WORD (e.g. `a << b` arithmetic-ish) is not an opener; keep
            # scanning past it so a later real `cmd <<EOF` on the same line is still found.
            i += 1
            prev_space = False
        else:
            prev_space = ch.isspace()
        i += 1
    return None


def _strip_heredocs(command: str) -> str:
    """Drop heredoc BODIES (`cmd <<['"]?WORD['"]? … \\n …body… \\n WORD`). A heredoc body is DATA fed
    to the command's stdin, NOT shell commands — so a `sed -i 's/a/b/' app.ts` line *inside* a
    heredoc (e.g. generating a script or doc) must not be scanned as a real edit (codex false
    positive). The `<<WORD` line itself is kept (its command may still edit); only the body lines up
    to the terminator are removed. `<<-` (tab-stripped) is handled the same. The opener is detected
    quote-aware (`_heredoc_terminator`), so a `<<` inside a quoted string / commit message is NOT
    treated as a heredoc and the following lines keep being scanned. If no terminator line is found,
    every line after the opener is dropped as the (unterminated) body — which matches the shell:
    it likewise never runs the body of an unclosed heredoc. Never crashes."""
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        term = _heredoc_terminator(line)
        if term is not None:
            i += 1
            while i < len(lines) and lines[i].strip() != term:
                i += 1  # skip body lines (the data)
            if i < len(lines):
                out.append(lines[i])  # keep the terminator line
        i += 1
    return "\n".join(out)


def _matched_edit(command: str, cwd: str, *, _depth: int = 0) -> tuple[str | None, bool]:
    """(label, parse_failed). Scan every segment; first edit-of-tracked-source wins. parse_failed
    is True if ANY segment couldn't be tokenized (unbalanced quotes) AND nothing matched — the
    caller fails OPEN in that case rather than guessing. Command substitutions (`$(…)`/backticks)
    are recursed into (bounded depth), so `echo $(sed -i … f)` is caught like `bash -c '…'`.

    Heredoc bodies are dropped, then comments stripped ONCE up front, so segment-splitting and
    substitution-extraction agree on what is a command vs. data (codex: a `#` in one reader bypassed
    a multi-line edit, another falsely scanned a `$(…)` in a comment, and a heredoc body was scanned
    as commands)."""
    command = _strip_comments(_strip_heredocs(command))
    parse_failed = False
    for seg in _split_segments(command):
        toks = _tokens(seg)
        if toks is None:
            parse_failed = True
            continue
        # Thread `_depth` into the segment scanner — otherwise its `bash -c` recursion always starts
        # from 0 and the `_MAX_SHELL_DEPTH` cap never fires, so a deeply nested `bash -c "bash -c …"`
        # recurses to a RecursionError instead of stopping at the cap (codex: the cap was a no-op).
        if hit := _scan_segment(toks, cwd, _depth=_depth):
            return hit, False
    if _depth < _MAX_SHELL_DEPTH:
        for body in _command_subs(command):
            hit, sub_failed = _matched_edit(body, cwd, _depth=_depth + 1)
            if hit:
                return hit, False
            parse_failed = parse_failed or sub_failed
    return None, parse_failed


# The harness's NATIVE file-edit tool, named in the block message so the agent is pointed at a
# tool it actually has: Claude Code has Edit/Write; Codex has `apply_patch` (a codex session told
# to "use the Edit tool" has no such tool and re-plans around the refusal). Keyed on the
# TOP-LEVEL `event["harness"]` only — a module literal each bridge stamps, never `args` (see
# orchestrator-stays-thin's trust-boundary note) — and only ever changes WORDING, never the
# decision, so a spoofed value buys nothing.
_NATIVE_EDIT_TOOLS: dict[str, tuple[str, str]] = {
    # harness → (tools named in the rule text, the one-line "use X" instruction)
    "codex": ("the apply_patch tool", "Use apply_patch (a single patch that adds or updates the file)."),
}
_DEFAULT_EDIT_TOOLS = ("the Edit/Write tools", "Use the Edit tool (or Write to replace the whole file).")


def _native_edit_tools(event: dict) -> tuple[str, str]:
    harness = event.get("harness")
    key = harness.strip() if isinstance(harness, str) else ""
    return _NATIVE_EDIT_TOOLS.get(key, _DEFAULT_EDIT_TOOLS)


def _block_message(matched: str, tools: tuple[str, str]) -> str:
    tools_name, use_line = tools
    return (
        f"Edit files with {tools_name}, not the shell: {matched}. The hyperide rule "
        "is 'edits only via Edit/Write; no sed/perl/awk for editing files' — a shell in-place "
        "edit (`sed -i`/`perl -i`/`gawk -i inplace`) or a `> file` redirect onto a tracked "
        f"source file leaves no reviewable diff, skips the formatter, and desyncs file-state "
        f"tracking. {use_line} Read-only "
        "sed/awk/grep pipelines and writes to /tmp or new files are fine. There is NO "
        "self-service bypass. For a genuine exception, ASK the human, or request a one-time "
        "Telegram approval by setting "
        "RIG_HATCH_REQUEST_NO_SHELL_FILE_EDIT=\"<written justification>\" "
        "(deny-by-default; a bare 1 is rejected)."
    )


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)
    cwd = event.get("cwd") or args.get("cwd") or os.getcwd()

    try:
        matched, parse_failed = _matched_edit(command, str(cwd))
    except Exception as exc:  # noqa: BLE001 — edit-hygiene discipline, not a security boundary
        warn(f"scan error ({exc}) — allowing (fail-open)")
        emit("allow")
        return 0

    if matched is None:
        if parse_failed:
            warn("a segment did not tokenize cleanly — allowing (fail-open)")
        emit("allow")
        return 0

    block_message = _block_message(matched, _native_edit_tools(event))

    ctx = {"hook": "no-shell-file-edit", "command": command}
    hatch = hatch_escalation.request_hatch_approval(
        "no-shell-file-edit", ctx, cwd=str(cwd), command=command
    )
    if hatch.should_stop:
        if hatch.approved:
            warn(f"no-shell-file-edit allowed via hatch escalation ({hatch.reason})")
            emit("allow", f"allowed via hatch escalation ({hatch.reason})")
            return 0
        emit("block", f"hatch escalation denied: {hatch.reason}\n{block_message}")
        return BLOCK_EXIT_CODE

    emit("block", block_message)
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
