"""Version-bump gate and screenshot gate (ship.sh lines 2061-2548).

The version-bump gate: a ship that changes shippable source (not docs / tests / CI) must move the
repo's declared version (skill: bump-version-on-release). The screenshot gate: optional uploader
for `--screenshot` files plus the "UI-touching PR needs an embedded image" check.
"""
from __future__ import annotations

import fnmatch
import glob
import os
import re

from shiplib.ctx import ShipExit

# POSIX [[:space:]] as grep/awk see it.
WS = " \t\n\v\f\r"
_WS_CLASS = "[" + WS + "]"

_REQUIREMENTS = ("requirements.txt", "requirements-*.txt", "constraints.txt", "constraints-*.txt")
_DOCS_PATHS = (
    "*.md", "*.mdx", "*.markdown", "*.rst", "*.txt", "*.adoc",
    "docs/*", "*/docs/*", "LICENSE", "LICENSE.*", "*/LICENSE", "NOTICE", "NOTICE.*", ".gitignore", ".gitattributes",
    ".github/*", "*/.github/*", ".gitlab/*", ".circleci/*", ".buildkite/*",
)
_CI_BASENAMES = (
    ".gitlab-ci.yml", ".gitlab-ci.yaml", ".travis.yml", ".travis.yaml", "azure-pipelines.yml", "azure-pipelines.yaml",
    "appveyor.yml", "appveyor.yaml", ".appveyor.yml", "Jenkinsfile", ".drone.yml", "bitbucket-pipelines.yml",
    "cloudbuild.yaml", "cloudbuild.yml",
)
_TEST_PATHS = (
    "test/*", "*/test/*", "tests/*", "*/tests/*", "__tests__/*", "*/__tests__/*",
    "test_*.py", "*/test_*.py", "*_test.py", "*/*_test.py",
    "*.test.ts", "*.test.tsx", "*.test.js", "*.test.jsx", "*.spec.ts", "*.spec.tsx", "*.spec.js", "*.spec.jsx",
    "*_test.go", "*/*_test.go",
)


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(value, p) for p in patterns)


def is_nonshippable_path(ctx, path: str) -> bool:
    """True iff `path` is docs / a test / CI-meta (bash rc 0); dependency manifests are shippable."""
    base = path.rsplit("/", 1)[-1]
    # A dependency MANIFEST named with a docs-ish extension is a SHIPPABLE change (a dep bump is a
    # release), so it is checked before the `.txt` docs bucket.
    if _matches_any(base, _REQUIREMENTS):
        return False
    if _matches_any(path, _DOCS_PATHS):
        return True
    if base in _CI_BASENAMES:
        return True
    # Exact root path only: _ship_config_load reads just the root file, so a nested same-named
    # file must not be swept into this exemption.
    if path == ".ship-config":
        return True
    return _matches_any(path, _TEST_PATHS)


def any_shippable_source(ctx, paths_text: str) -> bool:
    """True iff at least one newline-separated path in `paths_text` is shippable source."""
    found = False
    for p in paths_text.split("\n"):
        if p and not is_nonshippable_path(ctx, p):
            found = True
    return found


def version_decl_ere(ctx, path: str) -> str:
    """Regex (Python `re` form of the bash ERE) for a version-declaration line in `path`."""
    if path.endswith(".py"):
        # A module declares it only as an assignment of a string literal (`__version__ = "X"`,
        # also `__version__: str = "X"`); a `==` comparison is code.
        return "__version__" + _WS_CLASS + "*(:[^=]*)?=" + _WS_CLASS + "*[\"']"
    return '(version' + _WS_CLASS + '*=|"version"' + _WS_CLASS + '*:)'


def _grep_lines(lines: list[str], pattern: str) -> list[str]:
    rx = re.compile(pattern)
    return [ln for ln in lines if rx.search(ln)]


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as fh:
            return fh.read()
    except OSError:
        return None


def _file_has_line(path: str, pattern: str) -> bool:
    """`grep -Eq`: some line of the file matches."""
    text = _read_text(path)
    if text is None:
        return False
    return bool(_grep_lines(text.split("\n"), pattern))


def repo_file_inside_root(ctx, rel: str) -> bool:
    """True iff `rel` is a regular, non-symlink file physically inside ctx.root (no abs/`..`/symlinked dir)."""
    p = "/" + rel + "/"
    if "//" in p or "/../" in p:
        return False
    try:
        root = os.path.realpath(ctx.root)
        if not os.path.isdir(root):
            return False
        dirname = os.path.dirname(rel) or "."
        dpath = os.path.join(ctx.root, dirname)
        if not os.path.isdir(dpath):
            return False
        d = os.path.realpath(dpath)
    except OSError:
        return False
    if not (d + "/").startswith(root + "/"):
        return False
    full = os.path.join(ctx.root, rel)
    return os.path.isfile(full) and not os.path.islink(full)


def _strval(s: str) -> str:
    """The string after the first `=`, in double or single quotes, else ''."""
    s = re.sub("^[^=]*=" + _WS_CLASS + "*", "", s, count=1)
    q = s[:1]
    if q != '"' and q != "'":
        return ""
    s = s[1:]
    i = s.find(q)
    return s[:i] if i >= 0 else ""


def _scan_pyproject(text: str) -> str:
    """Port of the awk program: the dynamic-version spec line ('' when the version is static)."""
    sec = ""
    indyn = False
    dyn = declared = pattern = False
    attr = path = ""
    ws = _WS_CLASS
    re_dblbr = re.compile("^" + ws + r"*\[\[")
    re_sec = re.compile("^" + ws + r"*\[[A-Za-z0-9_. -]+\]" + ws + "*(#.*)?$")
    re_dynamic = re.compile("^" + ws + "*dynamic" + ws + "*=")
    re_ver_dot = re.compile("^" + ws + "*version" + ws + "*[.=]")
    re_alpha = re.compile("^" + ws + "*[A-Za-z]")
    re_ver_brace = re.compile("^" + ws + "*version" + ws + "*=" + ws + "*[{]")
    re_ver_attr = re.compile("^" + ws + r"*version\.attr" + ws + "*=")
    re_attr = re.compile("^" + ws + "*attr" + ws + "*=")
    re_path = re.compile("^" + ws + "*path" + ws + "*=")
    re_pattern = re.compile("^" + ws + "*pattern" + ws + "*=")
    re_attr_in = re.compile("(^|[" + WS + ",])attr" + ws + "*=")
    for line in text.split("\n"):
        if re_dblbr.search(line):
            sec = ""
            indyn = False
            continue
        if re_sec.search(line):
            sec = re.sub("^" + ws + r"*\[" + ws + "*", "", line, count=1)
            sec = re.sub(ws + r"*\].*$", "", sec, count=1)
            indyn = False
            continue
        if sec == "project" and re_dynamic.search(line):
            indyn = True
        if indyn:
            # Drop a comment first: its `]` would end the array early, its "version" is not one.
            nc = re.sub("#.*", "", line, count=1)
            if '"version"' in nc or "'version'" in nc:
                dyn = True
            if "]" in nc:
                indyn = False
        if sec == "tool.setuptools.dynamic" and re_ver_dot.search(line):
            declared = True
        if sec == "tool.setuptools.dynamic.version" and re_alpha.search(line):
            declared = True
        if sec == "tool.setuptools.dynamic" and re_ver_brace.search(line):
            s = re.sub("^[^{]*[{]", "", line, count=1)
            m = re_attr_in.search(s)
            if m:
                attr = _strval(s[m.start():])
        if sec == "tool.setuptools.dynamic" and re_ver_attr.search(line):
            attr = _strval(line)
        if sec == "tool.setuptools.dynamic.version" and re_attr.search(line):
            attr = _strval(line)
        if sec == "tool.hatch.version" and re_path.search(line):
            path = _strval(line)
        if sec == "tool.hatch.version" and re_pattern.search(line):
            pattern = True
    if not dyn:
        return ""
    if attr != "" and path != "":
        return "both"
    if attr != "":
        return "attr " + attr
    if path != "":
        return ("pattern " if pattern else "path ") + path
    if declared:
        return "setuptools"
    return ""


def dynamic_version_file(ctx, pyproject: str) -> tuple[int, str]:
    """(rc, text): rc 0 text=module path; rc 1 static/VCS-derived (no text); rc 2 text=why unreadable."""
    d = ""
    if "/" in pyproject:
        d = pyproject.rsplit("/", 1)[0] + "/"
    text = _read_text(os.path.join(ctx.root, pyproject))
    if text is None:
        return 1, ""
    spec = _scan_pyproject(text)
    val = spec.split(" ", 1)[1] if " " in spec else spec
    cand = why = ""
    if spec == "":
        return 1, ""
    if spec.startswith("path ") and len(spec) > len("path "):
        cand = d + (val[2:] if val.startswith("./") else val)
        # One reason for every failure, so the refusal says nothing about files outside.
        if not repo_file_inside_root(ctx, cand):
            why = "not a regular file inside the repo"
    elif spec.startswith("attr ") and spec.endswith(".__version__") and len(spec) > len("attr ") + len(".__version__"):
        mod = val[: -len(".__version__")].replace(".", "/")
        why = "module not found"
        # Flat layout first, then the src layout.
        for cand in (d + mod + "/__init__.py", d + mod + ".py", d + "src/" + mod + "/__init__.py", d + "src/" + mod + ".py"):
            if repo_file_inside_root(ctx, cand):
                why = ""
                break
    elif spec.startswith("attr "):
        why = "ship reads only __version__"
    elif spec.startswith("pattern "):
        # A hatch `pattern` may name another variable than `__version__`.
        return 2, '%s: path "%s" (custom hatch pattern)' % (pyproject, val)
    elif spec == "both":
        return 2, "%s: both a setuptools attr and a hatch path (ambiguous)" % pyproject
    else:
        return 2, "%s: [tool.setuptools.dynamic] version (not an attr)" % pyproject
    # The module must spell the version out: a computed `__version__` never shows a value in a
    # diff, so every ship would read UNCHANGED. Only a `.py` file is read as a module.
    if not why:
        if cand.endswith(".py"):
            if not _file_has_line(os.path.join(ctx.root, cand), "^" + _WS_CLASS + "*" + version_decl_ere(ctx, cand) + "v?[0-9]"):
                why = 'no literal __version__ = "X" in ' + cand
        else:
            why = "not a .py module"
    if not why:
        return 0, cand
    return 2, '%s: %s "%s" (%s)' % (pyproject, spec.split(" ", 1)[0], val, why)


def locate_version_file(ctx) -> tuple[int, str]:
    """(rc, text): rc 0 text=version file path; rc 1 none; rc 2 text=the unreadable dynamic source."""
    unreadable = ""
    pinned = ctx.env.get("SHIP_VERSION_FILES", "")
    if pinned:
        # Unquoted `for f in $SHIP_VERSION_FILES`: word-split plus pathname expansion in the root.
        words: list[str] = []
        for w in pinned.split():
            if any(c in w for c in "*?["):
                hits = sorted(glob.glob(w, root_dir=ctx.root))
                words.extend(hits or [w])
            else:
                words.append(w)
        for f in words:
            if not os.path.isfile(os.path.join(ctx.root, f)):
                continue
            if f == "pyproject.toml" or f.endswith("/pyproject.toml"):
                # rc 2 is final (the pin is refused, not skipped for a later entry); rc 1: static.
                rc, out = dynamic_version_file(ctx, f)
                if rc != 1:
                    return rc, out
            return 0, f
        return 1, ""
    pyproject = os.path.join(ctx.root, "pyproject.toml")
    if os.path.isfile(pyproject):
        rc, out = dynamic_version_file(ctx, "pyproject.toml")
        if rc == 0:
            return 0, out
        if rc == 2:
            unreadable = out  # a polyglot repo may version through package.json instead
        elif _file_has_line(pyproject, "^" + _WS_CLASS + "*version" + _WS_CLASS + "*=" + _WS_CLASS + "*[\"']"):
            return 0, "pyproject.toml"
    package_json = os.path.join(ctx.root, "package.json")
    if os.path.isfile(package_json) and _file_has_line(package_json, '"version"' + _WS_CLASS + "*:"):
        return 0, "package.json"
    if unreadable:
        return 2, unreadable
    return 1, ""


def diff_section_of(ctx, file: str) -> list[str]:
    """The lines of `file`'s hunks in ctx.pr_diff, picked by the exact `+++ b/<path>` header line."""
    target = "+++ b/" + file
    insec = False
    inhdr = False
    out: list[str] = []
    for line in getattr(ctx, "pr_diff", "").split("\n"):
        if line.startswith("diff --git "):
            insec = False
            inhdr = True
            continue
        if inhdr and line.startswith("+++ "):
            insec = line == target
            continue
        if inhdr and line.startswith("@@"):
            inhdr = False
        if insec:
            out.append(line)
    return out


def version_value(ctx, lines: list[str]) -> str:
    """The value on the first declaration line of `lines` (quoted literal after `=` when present)."""
    if not lines:
        return ""
    first = re.sub("^[^=]*=" + _WS_CLASS + "*([\"'])", r"\1", lines[0], count=1)
    m = re.search(r"[0-9][0-9A-Za-z.+_-]*", first)
    return m.group(0) if m else ""


def version_line_bumped(ctx, file: str) -> bool:
    """True iff the PR diff changes the VALUE of `file`'s version line (uses ctx.pr_diff)."""
    ere = version_decl_ere(ctx, file)
    section = diff_section_of(ctx, file)
    newv = version_value(ctx, _grep_lines(section, r"^\+" + _WS_CLASS + "*" + ere))
    oldv = version_value(ctx, _grep_lines(section, "^-" + _WS_CLASS + "*" + ere))
    if not newv:
        return False  # no new version value declared -> not bumped
    return newv != oldv  # value unchanged (cosmetic-only edit) -> not bumped


def pr_new_version_value(ctx, file: str) -> str:
    """The PR's NEW version value (the `+` line of `file`'s diff section); '' when none."""
    ere = version_decl_ere(ctx, file)
    return version_value(ctx, _grep_lines(diff_section_of(ctx, file), r"^\+" + _WS_CLASS + "*" + ere))


def base_tip_version_value(ctx, file: str) -> str:
    """The version value of `file` at the tip of the PR's base branch, '' when it cannot be read.

    The PR diff is relative to the merge-base, so a sibling release can already hold the number
    (agent-tools#592); a missing ref/file/value yields '' (the check passes)."""
    ere = version_decl_ere(ctx, file)
    r = ctx.gh("pr", "view", ctx.pr, "--json", "baseRefName", "-q", ".baseRefName", stderr="devnull")
    base = r.out.rstrip("\n") if r.rc == 0 else ""
    if base == "" or base.startswith("-") or re.search(r"[^A-Za-z0-9._/-]", base):
        base = ctx.default_branch
    ref = "origin/" + base
    ctx.cmd(["git", "-C", ctx.root, "fetch", "-q", "origin", "%s:refs/remotes/origin/%s" % (base, base)], stdout="devnull", stderr="devnull")
    if ctx.cmd(["git", "-C", ctx.root, "rev-parse", "--verify", "--quiet", ref], stdout="devnull", stderr="devnull").rc != 0:
        return ""
    shown = ctx.cmd(["git", "-C", ctx.root, "show", "%s:%s" % (ref, file)], stderr="devnull")
    if shown.rc != 0:
        return ""
    return version_value(ctx, _grep_lines(shown.out.split("\n"), "^" + _WS_CLASS + "*" + ere))


def _version_bump_gate(ctx) -> None:
    if ctx.env.get("SHIP_SKIP_VERSION_BUMP", "0") == "1" and not ctx.no_vbump_ok:
        ctx.no_vbump_ok = "SHIP_SKIP_VERSION_BUMP=1 (env)"
    if ctx.no_vbump_ok:
        ctx.out("[ship] version-bump gate OVERRIDDEN — reason: %s" % ctx.no_vbump_ok)
        return
    pr = ctx.pr
    files = ctx.gh("pr", "diff", pr, "--name-only", stderr="devnull")
    if files.rc != 0:
        raise ctx.refuse("Refusing: could not list changed files for PR #%s — cannot evaluate the version-bump gate (override with --no-version-bump-ok <reason> if intended)." % pr)
    if not any_shippable_source(ctx, files.out.rstrip("\n") + "\n"):
        ctx.out("[ship] version-bump gate: PR #%s changes no shippable source (docs/tests/CI only) — no bump required." % pr)
        return
    # Shippable source changed -> this ship is a release. The version field must have moved.
    vfile_rc, vfile = locate_version_file(ctx)
    vfile = vfile.rstrip("\n")
    if vfile_rc == 2:
        # A dynamic version ship cannot read: refuse rather than skip, or a repo in another
        # layout would merge source changes unchecked.
        ctx.err("Refusing: PR #%s changes shippable source, but ship cannot read its dynamic version: %s." % (pr, vfile))
        ctx.err("  The gate follows a module's `__version__ = \"X\"`: a setuptools attr in a flat or src/ layout, or a hatch path to a .py file inside the repo.")
        ctx.err("  Pin the file that holds __version__: SHIP_VERSION_FILES=<path/to/module.py>, then re-run ship.")
        ctx.err("  If this is genuinely NOT a release (docs-only / pure test or CI / a revert), override: --no-version-bump-ok <reason> (or SHIP_SKIP_VERSION_BUMP=1).")
        raise ShipExit(1)
    if vfile_rc != 0:
        ctx.out("[ship] version-bump gate: no version file (pyproject.toml/package.json) found at repo root — skipping (set SHIP_VERSION_FILES for a non-standard layout).")
        return
    diff = ctx.gh("pr", "diff", pr, stderr="devnull")
    if diff.rc != 0:
        raise ctx.refuse("Refusing: could not read the diff for PR #%s — cannot evaluate the version-bump gate (override with --no-version-bump-ok <reason> if intended)." % pr)
    ctx.pr_diff = diff.out.rstrip("\n")  # bash PR_DIFF (command-substitution strips trailing newlines)
    if not version_line_bumped(ctx, vfile):
        ctx.err("Refusing: PR #%s changes shippable source but the version in %s is UNCHANGED — this ship is a release, so bump the version (skill: bump-version-on-release)." % (pr, vfile))
        ctx.err("  Semver: patch for a fix, minor for a feature, major for a breaking change. Edit %s's version field, push, re-run ship." % vfile)
        ctx.err("  If this is genuinely NOT a release (docs-only / pure test or CI / a revert), override: --no-version-bump-ok <reason> (or SHIP_SKIP_VERSION_BUMP=1).")
        raise ShipExit(1)
    # Bumped relative to the merge-base is not enough: the base branch may ALREADY carry that
    # value (a sibling PR released it first).
    vb_new = pr_new_version_value(ctx, vfile)
    vb_base = base_tip_version_value(ctx, vfile)
    if vb_new and vb_base and vb_new == vb_base:
        ctx.err("Refusing: PR #%s bumps %s to %s, but the base branch tip ALREADY has version %s — another release took that number, so merging would change no version (the PR diff is relative to the merge-base, not to the current base)." % (pr, vfile, vb_new, vb_base))
        ctx.err("  Bump above %s (rebase or edit %s's version field), push, re-run ship." % (vb_base, vfile))
        ctx.err("  If this is genuinely NOT a release, override: --no-version-bump-ok <reason> (or SHIP_SKIP_VERSION_BUMP=1).")
        raise ShipExit(1)
    ctx.out("[ship] version-bump gate OK — %s version is bumped in PR #%s." % (vfile, pr))


# --- screenshot gate --------------------------------------------------------------------

_POSIX_CLASSES = {
    "alpha": "a-zA-Z", "digit": "0-9", "alnum": "a-zA-Z0-9", "upper": "A-Z", "lower": "a-z",
    "space": " \\t\\n\\r\\f\\v", "blank": " \\t", "xdigit": "0-9A-Fa-f", "punct": "!-/:-@\\[-`{-~",
    "print": "\\x20-\\x7e", "graph": "\\x21-\\x7e", "cntrl": "\\x00-\\x1f\\x7f",
}


def ere_to_python(ere: str) -> str:
    """Translate a POSIX ERE (grep -E) to a Python `re` pattern: bracket expressions (backslash is
    literal inside them, [:class:] names), GNU `\\<` `\\>` word edges, and non-special escapes."""
    out: list[str] = []
    i, n = 0, len(ere)
    while i < n:
        c = ere[i]
        if c == "\\" and i + 1 < n:
            nxt = ere[i + 1]
            i += 2
            if nxt in "<>":
                out.append(r"\b")
            elif nxt == "`":
                out.append(r"\A")
            elif nxt == "'":
                out.append(r"\Z")
            elif nxt in "wWsSbB" or nxt.isdigit():
                out.append("\\" + nxt)
            elif nxt.isalpha():
                out.append(re.escape(nxt))
            else:
                out.append("\\" + nxt)
            continue
        if c == "\\":
            out.append(r"\\")
            i += 1
            continue
        if c != "[":
            out.append(c)
            i += 1
            continue
        # bracket expression
        j = i + 1
        neg = False
        if j < n and ere[j] == "^":
            neg = True
            j += 1
        items: list[str] = []
        first = True
        closed = False
        while j < n:
            ch = ere[j]
            if ch == "]" and not first:
                closed = True
                j += 1
                break
            first = False
            if ch == "[" and j + 1 < n and ere[j + 1] in ":.=":
                kind = ere[j + 1]
                end = ere.find(kind + "]", j + 2)
                if end >= 0:
                    name = ere[j + 2:end]
                    if kind == ":":
                        items.append(_POSIX_CLASSES.get(name, ""))
                    else:
                        items.append(re.escape(name))
                    j = end + 2
                    continue
            if ch in "\\[]^&|~":
                items.append("\\" + ch)
            else:
                items.append(ch)
            j += 1
        if not closed:
            out.append(r"\[")
            i += 1
            continue
        out.append("[" + ("^" if neg else "") + "".join(items) + "]")
        i = j
    return "".join(out)


def _basename(path: str) -> str:
    """coreutils `basename`."""
    stripped = path.rstrip("/")
    if stripped == "":
        return "/" if path else ""
    return stripped.rsplit("/", 1)[-1]


def upload_png_compose_cmd(ctx, template: str, replacement: str) -> str:
    """The upload template with `replacement` spliced in for each `{FILE}`, `"{FILE}"` or `'{FILE}'`.

    One left-to-right pass over the ORIGINAL template, never rescanning emitted text: a path that
    itself contains `{FILE}` must not be re-matched. A quoted form is replaced WHOLE (quotes
    included) because the replacement already carries its own `%q` quoting."""
    haystack = template
    out = ""
    dq, sq, bare = '"{FILE}"', "'{FILE}'", "{FILE}"
    while True:
        if bare not in haystack:
            out += haystack
            break
        prefix_dq = haystack.split(dq, 1)[0] if dq in haystack else haystack
        prefix_sq = haystack.split(sq, 1)[0] if sq in haystack else haystack
        prefix_bare = haystack.split(bare, 1)[0]
        best_prefix, needle = prefix_dq, dq
        if len(prefix_sq) < len(best_prefix):
            best_prefix, needle = prefix_sq, sq
        if len(prefix_bare) < len(best_prefix):
            best_prefix, needle = prefix_bare, bare
        out += best_prefix + replacement
        haystack = haystack.split(needle, 1)[1]
    return out


def upload_png(ctx, png: str) -> str | None:
    """Upload `png` via SHIP_IMAGE_UPLOAD_CMD; the URL, or None (bash rc 1: no uploader / failed).

    HYP-1260: the untrusted path is shell-quoted with `printf %q` (a single inert token) and
    spliced into the trusted template before the shell evaluates it, so it cannot break out of
    its quoting while the template keeps full shell semantics (pipes, redirects, env prefixes)."""
    tmpl = ctx.env.get("SHIP_IMAGE_UPLOAD_CMD", "")
    if not tmpl:
        return None
    if ctx.dry_run:
        return "https://example.invalid/dry-run/" + _basename(png)
    q = ctx.cmd(["bash", "-c", 'printf %q "$1"', "bash", png], stderr="devnull")
    quoted_png = q.out
    # Strip trailing newline(s) from the template: a stray one (from an unguarded `$(cat file)`)
    # would become an embedded statement separator once the path is appended.
    tmpl = tmpl.rstrip("\n")
    # Placeholder detection asks the TEMPLATE, not the composed output: a path equal to the
    # literal `{FILE}` would slip past an output-equality check.
    if "{FILE}" in tmpl:
        cmd = upload_png_compose_cmd(ctx, tmpl, quoted_png)
    else:
        cmd = tmpl + " " + quoted_png
    # The script ran `eval` under `set -uo pipefail` (errexit is not inherited by `$(...)`).
    r = ctx.cmd(["bash", "-u", "-o", "pipefail", "-c", cmd], cwd=ctx.root, stderr="devnull")
    urls = re.findall(r"https?://[^ \n]+", r.out.rstrip("\n"))
    if not urls:
        return None
    return urls[-1]


def _screenshot_gate(ctx) -> None:
    ctx.posted_image = False  # bash POSTED_IMAGE: set only when a screenshot was really uploaded + embedded
    if ctx.shot_paths:
        body = "### Visual proof"
        for idx, png in enumerate(ctx.shot_paths):
            desc = ctx.shot_descs[idx] if idx < len(ctx.shot_descs) else ""
            if not desc:
                desc = _basename(png)
            url = None
            if os.path.isfile(png):
                url = upload_png(ctx, png)
            if not os.path.isfile(png):
                ctx.err("[ship] WARNING: screenshot not found: %s (does NOT satisfy the gate)." % png)
                body = body + "\n\n" + "**" + desc + "**" + "\n" + "_Visual proof MISSING — file not found: `" + png + "`_"
            elif url:
                ctx.out("[ship] uploaded '%s' -> %s" % (desc, url))
                body = body + "\n\n" + "**" + desc + "**" + "\n" + "![" + desc + "](" + url + ")"
                ctx.posted_image = True
            else:
                # A path note is NOT an embedded image, so it must not satisfy the UI gate.
                ctx.err("[ship] NOTE: no uploader (SHIP_IMAGE_UPLOAD_CMD) or upload failed for '%s' — embedding a path note (does NOT satisfy the gate)." % desc)
                body = body + "\n\n" + "**" + desc + "**" + "\n" + "_Local path: `" + png + "` — upload manually._"
        ctx.out("[ship] posting visual-proof comment to PR #%s ..." % ctx.pr)
        # bash `run gh ...`: dry-run echo, else the (Forgejo-aware) gh, set -e on failure.
        argv = ["gh", "pr", "comment", ctx.pr, "--body", body]
        if ctx.dry_run:
            ctx.out("[dry-run] " + " ".join(argv))
        else:
            rc = ctx.gh(*argv[1:], stdout="inherit").rc
            if rc != 0:
                raise ShipExit(rc)

    ui_touching = False
    if ctx.ui_path_regex:
        files = ctx.gh("pr", "diff", ctx.pr, "--name-only", stderr="devnull")
        if files.rc == 0:
            try:
                rx = re.compile(ere_to_python(ctx.ui_path_regex))
            except re.error:
                rx = None  # grep exits 2 on a bad regex: the `&&` list just does not set the flag
            if rx is not None and any(rx.search(ln) for ln in files.out.rstrip("\n").split("\n")):
                ui_touching = True
        else:
            raise ctx.refuse("Refusing: could not list changed files for PR #%s — cannot evaluate the screenshot gate." % ctx.pr)
    ctx.ui_touching = ui_touching
    if not ui_touching:
        return
    has_image = False
    # Only a really-uploaded image, or one already embedded in the PR, counts.
    if ctx.posted_image:
        has_image = True
    else:
        r = ctx.gh("pr", "view", ctx.pr, "--json", "body,comments", "-q", '(.body // "") + "\\n" + ([.comments[].body] | join("\\n"))', stderr="devnull")
        pr_text = r.out.rstrip("\n") if r.rc == 0 else ""
        rx_img = re.compile(r"!\[[^\]]*\]\(https?://|<img |user-attachments/")
        if any(rx_img.search(ln) for ln in pr_text.split("\n")):
            has_image = True
    if not has_image:
        if ctx.no_shot_ok:
            ctx.out("[ship] UI screenshot gate OVERRIDDEN — reason: %s" % ctx.no_shot_ok)
        else:
            ctx.err("Refusing: PR #%s touches UI but has NO embedded screenshot. Pass --screenshot <path> [desc]" % ctx.pr)
            ctx.err("  (with SHIP_IMAGE_UPLOAD_CMD set), or --no-screenshot-ok <reason> to override.")
            raise ShipExit(1)


def run(ctx) -> None:
    _version_bump_gate(ctx)
    _screenshot_gate(ctx)
