#!/bin/sh
# install-local-hooks.sh — wire ONE repo into the global git-hook dispatcher.
#
# THE PROBLEM IT SOLVES
#   A global `core.hooksPath = ~/.config/git/hooks` makes every global hook fire in
#   normal repos. But a repo with a LOCAL hook manager overrides core.hooksPath
#   (lefthook -> .git/hooks, husky -> .husky/_) or ships raw .git/hooks, and those
#   SHADOW the global hooks entirely. This script injects ONE call to the dispatcher
#   (`run-global-hooks <event>`) into whatever the repo already uses, so EVERY global
#   hook in ~/.config/git/global-hooks.d/<event>/ runs there too — now and forever,
#   with no further per-repo edits when new global hooks are added.
#
# WHY ONE LINE COVERS ALL FUTURE HOOKS
#   We never inject individual hooks (secret-scan etc.). We inject the dispatcher,
#   which enumerates global-hooks.d/<event>/ at runtime. Drop a new file there and it
#   runs in every wired repo automatically.
#
# IDEMPOTENT
#   Every injection is guarded by a marker comment (MARKER below). Re-running is a
#   no-op once present. Nothing is ever clobbered: lefthook gets a MERGED command,
#   husky/raw get an APPENDED guarded line, and an existing raw hook keeps its body.
#
# DEDUP (auto)
#   Before wiring, we scan the repo for a check it ALREADY runs locally that a global
#   fragment also provides — today: a secret scan (a `gitleaks` / `secret-scan` /
#   no-secrets command in lefthook.yml, .husky/*, raw hooks, or a tracked
#   git-hooks/no-secrets-scan). If found, we add that capability id to the repo's
#   tracked `.githooks-skip`, so the dispatcher skips its global copy here and the
#   check runs EXACTLY ONCE. See the DEDUP section in run-global-hooks.
#
# USAGE
#   install-local-hooks.sh [--commit] [--no-dedup] [REPO_DIR]   # default: repo of $PWD
#   Detects the manager automatically: lefthook (lefthook.yml) | husky (.husky/) |
#   raw (.git/hooks).
#     --commit    : after wiring, commit the TRACKED wiring changes (lefthook.yml,
#                   .husky/<event>, .githooks-skip) in the repo with a fixed message.
#                   Makes the wiring a reproducible, tracked step (no-op if nothing
#                   changed, or if the only changes are untracked .git/hooks files).
#     --no-dedup  : skip the auto secret-scan detection / .githooks-skip write.
#   For lefthook in a pull-only checkout, run on a branch and commit it (or use
#   --commit) — this script edits the tracked file in place.
#
# EXIT: 0 on success / already-wired; non-zero on a real failure.

set -eu

MARKER="global-git-hooks-dispatcher"          # idempotency marker (in injected text)
DISPATCHER='"${XDG_CONFIG_HOME:-$HOME/.config}/git/run-global-hooks"'
EVENTS="pre-commit commit-msg pre-push"

# --- option parsing (flags may appear before the optional REPO_DIR) ---------------
DO_COMMIT=0
DO_DEDUP=1
REPO_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --commit)   DO_COMMIT=1 ;;
    --no-dedup) DO_DEDUP=0 ;;
    --) shift; break ;;
    -*) echo "install-local-hooks: unknown flag $1" >&2; exit 2 ;;
    *)  REPO_ARG="$1" ;;
  esac
  shift
done
[ -n "$REPO_ARG" ] || REPO_ARG="${1:-}"

REPO="${REPO_ARG:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
[ -n "$REPO" ] || { echo "install-local-hooks: not in a git repo and no REPO_DIR given" >&2; exit 2; }
[ -e "$REPO/.git" ] || { echo "install-local-hooks: $REPO is not a git repo" >&2; exit 2; }
REPO="$(cd "$REPO" && pwd)"

log() { echo "install-local-hooks[$(basename "$REPO")]: $*"; }

# TOUCHED — newline-separated, repo-RELATIVE paths this run actually wrote. --commit
# stages ONLY these, so a pre-existing unrelated change to e.g. lefthook.yml or some
# OTHER file under .husky/ is never swept into the wiring commit.
TOUCHED=""
mark_touched() {  # mark_touched <repo-relative-path>
  case "
$TOUCHED" in *"
$1"*) return ;; esac     # already recorded
  TOUCHED="$TOUCHED
$1"
}

# PRE_DIRTY — repo-relative paths that already had uncommitted changes (vs HEAD or the
# index) BEFORE we touched anything. --commit refuses to sweep these: it cannot split our
# wiring hunk from a pre-existing unrelated edit in the SAME file, so it leaves the whole
# repo for the user to commit by hand rather than silently bundling unrelated work.
PRE_DIRTY=""
snapshot_dirty() {
  # Union of worktree-modified, index-staged, AND untracked paths. We use --name-only /
  # --others (NOT porcelain) so rename lines like "R old -> new" don't get mis-parsed into
  # the OLD path — git emits clean target paths here. Untracked matters too: if a wiring
  # target (lefthook.yml / .githooks-skip / a custom hook) already existed UNTRACKED with
  # the user's content and we then modify it, --commit must refuse rather than `git add`
  # that pre-existing content into the wiring commit.
  PRE_DIRTY="$( { git -C "$REPO" diff --name-only 2>/dev/null
                  git -C "$REPO" diff --cached --name-only 2>/dev/null
                  git -C "$REPO" ls-files --others --exclude-standard 2>/dev/null; } \
                | sort -u || true)"
}
was_pre_dirty() {  # was_pre_dirty <repo-relative-path> -> 0 if it was already dirty
  [ -n "$PRE_DIRTY" ] || return 1
  printf '%s\n' "$PRE_DIRTY" | grep -Fxq -- "$1"
}

# --- manager detection ------------------------------------------------------------
# A repo can carry BOTH a lefthook.yml and .husky/ (one of them stale). Prefer whichever
# ACTUALLY OWNS the hooks: the installed manager points core.hooksPath at its own dir.
# Husky sets core.hooksPath=.husky/_ (newer) or .husky; lefthook installs into .git/hooks
# (default path) and leaves core.hooksPath unset/default. So:
#   - core.hooksPath under .husky        -> husky is active (even if a stale lefthook.yml exists)
#   - else lefthook config present       -> lefthook
#   - else .husky present                -> husky
#   - else                                -> raw
detect_manager() {
  _hp="$(git -C "$REPO" config --local core.hooksPath 2>/dev/null || true)"
  # A non-default core.hooksPath means SOME manager already owns the hook path. If it's
  # husky's dir, it's husky; ANY OTHER custom path (.githooks, ../shared, …) is raw — wire
  # it via raw_hooks_dir, not a stale lefthook.yml. Match husky EXACTLY (.husky or
  # .husky/_), not a glob that would also catch `.husky2`.
  case "$_hp" in
    ""|".git/hooks"|"$REPO/.git/hooks") : ;;            # default -> fall through to config
    .husky|.husky/_|"$REPO"/.husky|"$REPO"/.husky/_) echo husky; return ;;
    *) echo raw; return ;;                              # any other custom path -> raw
  esac
  if [ -f "$REPO/lefthook.yml" ] || [ -f "$REPO/lefthook.yaml" ] || [ -f "$REPO/.lefthook.yml" ]; then
    echo lefthook; return
  fi
  if [ -d "$REPO/.husky" ]; then echo husky; return; fi
  echo raw
}

# canon_dir <path> — echo the canonical absolute path of an existing directory (resolves
# `..`, symlinks). Falls back to the input unchanged if it can't be resolved.
canon_dir() {
  ( CDPATH='' cd -- "$1" 2>/dev/null && pwd -P ) || printf '%s\n' "$1"
}

# path_under <child-dir> <parent-dir> — 0 if the canonicalized child dir is the parent or
# strictly inside it. Used so a RELATIVE core.hooksPath like `../shared-hooks` (which a
# raw string-prefix check would wrongly accept) is correctly classified as OUTSIDE the repo.
path_under() {
  _c="$(canon_dir "$1")"; _p="$(canon_dir "$2")"
  case "$_c/" in "$_p/"*) return 0 ;; *) return 1 ;; esac
}

# raw_hooks_dir — echo the directory raw .git/hooks injection uses for THIS repo,
# honouring a non-default local core.hooksPath (e.g. core.hooksPath=.githooks). Both
# inject_raw and the dedup detector resolve the dir through here so they never disagree.
raw_hooks_dir() {
  _hd="$REPO/.git/hooks"
  _hp="$(git -C "$REPO" config --local core.hooksPath 2>/dev/null || true)"
  case "$_hp" in
    ""|"$REPO/.git/hooks"|".git/hooks") : ;;
    /*) _hd="$_hp" ;;
    *)  _hd="$REPO/$_hp" ;;
  esac
  printf '%s\n' "$_hd"
}

# ----------------------------------------------------------------------------------
# lefthook: add (or merge) a `<event>.commands.global-git-hooks` entry that runs the
# dispatcher. We do NOT clobber the file — we only add commands that are missing,
# keyed by the marker. lefthook passes the commit-msg file path as {1}.
# ----------------------------------------------------------------------------------
inject_lefthook() {
  cfg="$REPO/lefthook.yml"; [ -f "$cfg" ] || cfg="$REPO/lefthook.yaml"; [ -f "$cfg" ] || cfg="$REPO/.lefthook.yml"
  if grep -q "$MARKER" "$cfg" 2>/dev/null; then
    log "lefthook already wired ($cfg) — no-op"; return 0
  fi
  log "wiring lefthook ($cfg)"
  # Append a self-contained block. lefthook merges multiple top-level <event> keys?
  # No — a YAML doc can't have duplicate keys. So we APPEND new command entries under
  # each event IF the event key exists, else create the event with our command. We do
  # this with a tiny awk that is YAML-indent aware for the common 2-space lefthook style.
  tmp="$(mktemp "${TMPDIR:-/tmp}/lefthook.XXXXXX")"
  # The dispatcher command for each event. The WHOLE shell command must be a single
  # YAML scalar, so we single-quote it: `run: '"$DISP" pre-commit'`. Inside a YAML
  # single-quoted scalar the `${...}` default and the embedded double-quotes are
  # literal — lefthook hands the string to `sh -c`, which then expands them. (An
  # unquoted `run: "..." pre-commit` is INVALID YAML — quoted scalar + trailing junk.)
  #
  # We pass every awk var as a SINGLE LINE — BSD/macOS awk rejects a literal newline
  # inside a `-v` value ("awk: newline in string"). awk emits the key line and the
  # run line as two separate prints.
  rgh='"${XDG_CONFIG_HOME:-$HOME/.config}/git/run-global-hooks"'
  keyline="    $MARKER:"
  # pre-push MUST carry `use_stdin: true`: lefthook only forwards the pushed-refs stdin
  # to a pre-push command with that option (https://lefthook.dev/configuration/use_stdin/).
  # Without it protect-main reads empty stdin and FAILS OPEN on a direct push to main.
  awk -v keyline="$keyline" \
      -v run_pc="      run: '$rgh pre-commit'" \
      -v run_cm="      run: '$rgh commit-msg {1}'" \
      -v run_pp="      run: '$rgh pre-push'" \
      -v stdin_pp="      use_stdin: true" '
    function emit(ev) {
      print keyline
      if (ev == "pre-commit") print run_pc
      else if (ev == "commit-msg") print run_cm
      else if (ev == "pre-push") { print run_pp; print stdin_pp }
    }
    BEGIN {
      split("pre-commit commit-msg pre-push", order, " ")
      for (k = 1; k <= 3; k++) { e = order[k]; hdr[e] = 0; cmds[e] = 0 }
    }
    # Record, per event: the header line index (hdr) and its `commands:` line index
    # (cmds, 0 if the block has no commands: key — e.g. a scripts:-only block).
    {
      lines[NR] = $0
      # Event header: `pre-commit:` optionally followed by whitespace and/or a `# comment`.
      # (A trailing comment like `pre-commit: # gate` is valid YAML; without allowing it we
      # would treat the block as absent and append a DUPLICATE top-level key.)
      if ($0 ~ /^(pre-commit|commit-msg|pre-push):[ \t]*(#.*)?$/) {
        ev = $0; sub(/:.*/, "", ev); cur_event = ev; hdr[ev] = NR
      } else if ($0 ~ /^[^ \t#]/) {
        cur_event = ""   # left the block (some other top-level key)
      }
      if (cur_event != "" && cmds[cur_event] == 0 && $0 ~ /^[ \t]+commands:[ \t]*$/) {
        cmds[cur_event] = NR
      }
    }
    END {
      # Decide the injection point for each present event.
      #  - has commands:  -> inject right AFTER the commands: line.
      #  - block but no commands: -> inject right AFTER the header, prefixing a commands: line.
      for (k = 1; k <= 3; k++) {
        e = order[k]
        if (cmds[e] > 0)      { after_cmds[cmds[e]] = e }
        else if (hdr[e] > 0)  { after_hdr[hdr[e]]  = e }
      }
      for (i = 1; i <= NR; i++) {
        print lines[i]
        if (i in after_cmds) emit(after_cmds[i])
        if (i in after_hdr)  { print "  commands:"; emit(after_hdr[i]) }
      }
      # Events with no block at all -> append a fresh one.
      for (k = 1; k <= 3; k++) {
        e = order[k]
        if (hdr[e] == 0) { print e ":"; print "  commands:"; emit(e) }
      }
    }
  ' "$cfg" > "$tmp"
  mv "$tmp" "$cfg"
  mark_touched "$(basename "$cfg")"
  log "lefthook wired — commit $cfg and run 'lefthook install' (or let it regenerate)"
}

# ----------------------------------------------------------------------------------
# husky: insert ONE guarded dispatcher line into .husky/<event>, right AFTER the shebang
# (NOT appended). Husky v9 runs the file directly; an existing husky script may end in
# `exit 0`, which would make a trailing append dead code — so we insert near the top, the
# same way as raw hooks. The repo's own commands (lint-staged etc.) are preserved below.
# ----------------------------------------------------------------------------------
inject_husky() {
  wired_any=0
  for ev in $EVENTS; do
    f="$REPO/.husky/$ev"
    # Only wire events that already have a husky script, plus always-create pre-commit
    # AND pre-push: husky sets core.hooksPath, which bypasses the global composer, so a
    # husky repo with no .husky/pre-push would never dispatch the protected protect-main
    # fragment — direct pushes to main would fail open.
    case "$ev" in
      pre-commit|pre-push) ;;
      *) [ -f "$f" ] || continue ;;
    esac
    if [ -f "$f" ] && grep -q "$MARKER" "$f"; then
      log "husky $ev already wired — no-op"; wired_any=1; continue
    fi
    cmt="# $MARKER — runs every global hook for this event (see agent-tools git-hooks/global-dispatcher)"
    if [ "$ev" = "commit-msg" ]; then
      call="$DISPATCHER commit-msg \"\$1\" || exit \$?"
    else
      call="$DISPATCHER $ev \"\$@\" || exit \$?"
    fi
    if [ ! -f "$f" ]; then
      # Fresh husky hook (e.g. a repo that has .husky/ but no commit-msg yet).
      printf '#!/bin/sh\n%s\n%s\n' "$cmt" "$call" > "$f"
    else
      # Insert after the first line (the shebang) so a trailing `exit 0` can't shadow it.
      # If the file has no shebang, awk still inserts after line 1 — harmless, husky runs
      # it via sh either way. Pass the two lines as SEPARATE awk vars (no literal newline).
      tmp="$(mktemp "${TMPDIR:-/tmp}/huskyhook.XXXXXX")"
      awk -v cmt="$cmt" -v call="$call" 'NR==1{print; print cmt; print call; next} {print}' "$f" > "$tmp"
      mv "$tmp" "$f"
    fi
    chmod +x "$f"
    mark_touched ".husky/$ev"
    log "husky $ev wired ($f)"
    wired_any=1
  done
  [ "$wired_any" = 1 ] || log "husky: nothing to wire"
}

# ----------------------------------------------------------------------------------
# raw .git/hooks: chain the dispatcher onto each event hook without clobbering an
# existing script. The dispatcher call is INSERTED right after the shebang, NOT
# appended — an existing hook may end in `exit 0` / `set -e` exit, which would make a
# trailing append dead code. Running the dispatcher first also means the secret-scan
# fires before the repo's (possibly slow) lint/test gate.
# ----------------------------------------------------------------------------------
inject_raw() {
  hooks_dir="$(raw_hooks_dir)"        # honours a non-default local core.hooksPath
  mkdir -p "$hooks_dir"
  for ev in $EVENTS; do
    f="$hooks_dir/$ev"
    if [ -f "$f" ] && grep -q "$MARKER" "$f"; then
      log "raw $ev already wired — no-op"; continue
    fi
    # Build the guarded call line for this event (two physical lines: comment + call).
    cmt="# $MARKER — runs every global hook for this event (agent-tools git-hooks/global-dispatcher)"
    if [ "$ev" = "commit-msg" ]; then
      call="$DISPATCHER commit-msg \"\$1\" || exit \$?"
    else
      call="$DISPATCHER $ev \"\$@\" || exit \$?"
    fi
    if [ ! -f "$f" ]; then
      # Fresh hook: shebang + dispatcher call.
      printf '#!/bin/sh\n%s\n%s\nexit 0\n' "$cmt" "$call" > "$f"
    else
      # Existing hook: insert the block right after the shebang (first line) so it
      # runs even if the body later `exit`s. awk preserves the rest verbatim.
      # Pass the two lines as SEPARATE awk vars — `-v` forbids literal newlines.
      tmp="$(mktemp "${TMPDIR:-/tmp}/rawhook.XXXXXX")"
      awk -v cmt="$cmt" -v call="$call" 'NR==1{print; print cmt; print call; next} {print}' "$f" > "$tmp"
      mv "$tmp" "$f"
    fi
    chmod +x "$f"
    # Mark for --commit ONLY if the hook truly lives in the worktree and OUTSIDE .git (a
    # tracked custom core.hooksPath like .githooks/). We CANONICALIZE the dir first so a
    # relative `core.hooksPath=../shared-hooks` (string-prefixed by "$REPO/" but actually
    # OUTSIDE the repo) and `.git/hooks` are both correctly excluded. Files git can't track
    # are never committable, so they stay out of TOUCHED.
    _hdir_real="$(canon_dir "$hooks_dir")"
    if path_under "$hooks_dir" "$REPO" && ! path_under "$hooks_dir" "$REPO/.git"; then
      mark_touched "${_hdir_real#"$(canon_dir "$REPO")"/}/$ev"
    fi
    log "raw $ev wired ($f)"
  done
}

# ----------------------------------------------------------------------------------
# DEDUP: detect a local secret-scan and record the capability id in .githooks-skip so
# the dispatcher skips its GLOBAL copy here. Deterministic and CONSERVATIVE:
#   - The global secret-scan fragment is a PRE-COMMIT hook, so we only look at the
#     repo's PRE-COMMIT hook (lefthook `pre-commit:` block, .husky/pre-commit, the raw
#     pre-commit). A local secret scan wired ONLY to pre-push must NOT disable the global
#     pre-commit scan.
#   - We only count an ACTIVE COMMAND INVOCATION: comments are stripped, and the scanner
#     must appear at a COMMAND POSITION (start of the command or right after a shell
#     separator |, &&, ;, ( ) — so `fail_text: gitleaks missing`, `run: echo install
#     gitleaks`, or a `# gitleaks in CI` note do NOT count. We also drop the dispatcher
#     line (it carries the MARKER).
# A false positive here would wrongly DISABLE the global secret scan, so we err toward
# NOT skipping. The git-hooks/no-secrets-scan helper alone never counts — only an active
# hook line that invokes it does.
# ----------------------------------------------------------------------------------
SKIPFILE="$REPO/.githooks-skip"

# Regex: the scanner at a COMMAND position. (^|sep)ws[path/]<tool>(ws|EOL). An optional
# path prefix matches `/usr/bin/gitleaks`, `./bin/gitleaks`, etc. For gitleaks we require a
# FOLLOWING token (subcommand/flag) so a bare trailing `... gitleaks` arg doesn't match;
# the helper scripts match as a path/command token. (Env-assignment prefixes like
# `GITLEAKS_CONFIG=x gitleaks …` are stripped in scan_text_for_secret before this runs.)
SECRET_CMD_RE='(^|[|&;(]|&&|\|\|)[[:space:]]*([^[:space:]|&;(]*/)?(gitleaks[[:space:]]|[^[:space:]]*no-secrets-scan([[:space:]]|$)|[^[:space:]]*secret-scan([[:space:]]|$))'

# A possibly-NON-BLOCKING invocation must NOT count as a real local gate — if we treated it
# as one and wrote .githooks-skip, we could skip the real (blocking) global scan and end up
# with NO blocking scan. The hook's exit status is the status of its LAST command, so any
# shell control operator AFTER the scanner can mask its failure. We can't statically know
# which masks and which re-blocks (`|| exit 1` blocks, `|| echo` doesn't; `; foo` and a
# pipeline `| tee` mask; `gitleaks … &` backgrounds it so the hook never blocks on it), so
# we conservatively DROP any line that puts a control operator after the command: a pipe `|`
# (and `||`), `&` (background and `&&`), or sequencing `;`. A real single-command gate
# (`gitleaks git --staged`, optionally with redirects) has none of these and still counts.
# The cost is purely fail-SAFE: a masked-but-actually-blocking form just won't dedup, so
# the global scan also runs — one extra scan, never a disabled one.
NEUTRALIZED_RE='[|&;]'

# scan_text_for_secret — read COMMAND lines on stdin (one shell command per line; for
# lefthook these are already-extracted `run:` values), strip comments, drop our marker
# line, strip leading shell VAR=value env-assignment prefixes (so `GITLEAKS_CONFIG=x
# gitleaks …` is seen as a gitleaks command), drop neutralized lines; return 0 if an
# ACTIVE, BLOCKING command invokes the scanner (per SECRET_CMD_RE above).
# PORTABILITY: one substitution with a repeated group strips every leading assignment. No
# `:label; …; tlabel` loop: BSD sed (macOS) reads a label to the end of the line, warns
# "unused label" and passes every line through unchanged, so an env-prefixed scanner
# (`GITLEAKS_CONFIG=x gitleaks …`) read as "no local secret scan" there.
scan_text_for_secret() {
  sed 's/#.*$//' \
    | grep -vF "$MARKER" \
    | sed -E 's/^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)+//' \
    | grep -Ev "$NEUTRALIZED_RE" \
    | grep -Eq "$SECRET_CMD_RE"
}

# lefthook_precommit_runs <cfg> — print ONLY the COMMAND text of `run:` values inside the
# top-level `pre-commit:` block. We must look at `run:` values only (not `fail_text:`,
# `glob:`, names, …) — a separator-bearing metadata string like
# `fail_text: "oops; gitleaks git --staged"` is NOT an executed command and must not count.
# Handles inline `run: cmd` and block scalars (`run: |` / `run: >` with deeper-indented
# body lines). Other keys at the command's indent end a block scalar.
lefthook_precommit_runs() {
  awk '
    function indent(s,   n){ n=0; while (substr(s,n+1,1)==" "||substr(s,n+1,1)=="\t") n++; return n }
    # Enter / leave the pre-commit: top-level block.
    /^pre-commit:[ \t]*(#.*)?$/ { inblk=1; next }
    /^[^ \t#]/                  { inblk=0; inrun=0 }
    inblk!=1 { next }
    {
      ind = indent($0)
      if (inrun) {
        # We are inside a `run: |`/`>` block scalar. Body lines are indented deeper than
        # the run: key; a line at <= run-key indent (and non-blank) ends the scalar.
        if ($0 ~ /^[[:space:]]*$/) { print ""; next }
        if (ind > runind) { sub(/^[[:space:]]+/, "", $0); print; next }
        inrun = 0   # fall through to re-examine this line as a normal key
      }
      # An inline `run: value` or a block-scalar opener `run: |` / `run: >`.
      if ($0 ~ /^[[:space:]]*run:[[:space:]]*[|>][+-]?[[:space:]]*$/) {
        runind = ind; inrun = 1; next
      }
      if ($0 ~ /^[[:space:]]*run:[[:space:]]*/) {
        v = $0; sub(/^[[:space:]]*run:[[:space:]]*/, "", v)
        sub(/^["'"'"']/, "", v); sub(/["'"'"']$/, "", v)   # unquote a simple scalar
        print v; next
      }
    }
  ' "$1" 2>/dev/null
}

repo_has_local_secret_scan() {  # repo_has_local_secret_scan <active-manager>
  # Scan ONLY the ACTIVE manager's pre-commit gate (the one git actually runs). A stale
  # inactive .husky/pre-commit in a lefthook repo, or vice-versa, must NOT count — it
  # doesn't run, so it provides no local scan, and counting it would wrongly disable the
  # global one. The dispatcher secret-scan is PRE-COMMIT only.
  case "$1" in
    lefthook)
      # Only the ACTIVE config (the first existing, in the same precedence inject_lefthook
      # uses) — a stale lefthook.yaml must not trigger when lefthook.yml is the live one.
      _cfg="$REPO/lefthook.yml"
      [ -f "$_cfg" ] || _cfg="$REPO/lefthook.yaml"
      [ -f "$_cfg" ] || _cfg="$REPO/.lefthook.yml"
      [ -f "$_cfg" ] || return 1
      lefthook_precommit_runs "$_cfg" | scan_text_for_secret && return 0 ;;
    husky)
      [ -f "$REPO/.husky/pre-commit" ] && \
        scan_text_for_secret < "$REPO/.husky/pre-commit" && return 0 ;;
    raw)
      _rawpc="$(raw_hooks_dir)/pre-commit"
      [ -f "$_rawpc" ] && scan_text_for_secret < "$_rawpc" && return 0 ;;
  esac
  return 1
}

# skipfile_has_id <id> — 0 if the id is already an effective entry. Uses the SAME parse as
# the dispatcher (strip '#' comments + surrounding whitespace) so `secret-scan # local` is
# recognized and we don't append a duplicate on re-run.
skipfile_has_id() {
  [ -f "$SKIPFILE" ] || return 1
  sed -e 's/#.*//' -e 's/[[:space:]]*$//' -e 's/^[[:space:]]*//' "$SKIPFILE" 2>/dev/null \
    | grep -v '^$' | grep -Fxq -- "$1"
}

# add_skip_id <id> — idempotently add a capability id to the tracked .githooks-skip.
add_skip_id() {
  _id="$1"
  # SECURITY: never write through a symlink — a cloned repo could point .githooks-skip
  # outside the worktree. A pre-existing symlink here is a hard error, not something we
  # silently follow or clobber.
  if [ -L "$SKIPFILE" ]; then
    log "dedup: refusing — .githooks-skip is a symlink (remove it; must be a regular file)"
    return 1
  fi
  if [ -e "$SKIPFILE" ] && [ ! -f "$SKIPFILE" ]; then
    log "dedup: refusing — .githooks-skip exists but is not a regular file"; return 1
  fi
  if skipfile_has_id "$_id"; then
    log "dedup: '$_id' already in .githooks-skip — no-op"; return 0
  fi
  # A write IS now required — verify it will succeed BEFORE we touch the file (and note:
  # injection ran first; the early preflight() already rejected symlink/non-regular so an
  # un-writable file/root is the only remaining failure here).
  if [ -e "$SKIPFILE" ]; then
    [ -w "$SKIPFILE" ] || { log "dedup: refusing — .githooks-skip is not writable"; return 1; }
  else
    [ -w "$REPO" ] || { log "dedup: refusing — cannot create .githooks-skip (repo root not writable)"; return 1; }
  fi
  if [ ! -f "$SKIPFILE" ]; then
    {
      echo "# .githooks-skip — capability ids the GLOBAL dispatcher must NOT run here,"
      echo "# because this repo already runs them locally (one check, run once). One id"
      echo "# per line; '#' starts a comment. See agent-tools git-hooks/global-dispatcher."
    } > "$SKIPFILE"
  elif [ -n "$(tail -c1 "$SKIPFILE" 2>/dev/null)" ]; then
    # Existing file whose last byte is NOT a newline -> our append would join onto that
    # line (e.g. `othersecret-scan`). Add the missing terminator first.
    echo "" >> "$SKIPFILE"
  fi
  echo "$_id" >> "$SKIPFILE"
  mark_touched ".githooks-skip"
  # NOTE: whether the GLOBAL copy is actually skipped depends on the fragment (a PROTECTED
  # id like secret-scan needs a trust opt-in — dedup_secret_scan explains that). So state
  # only what we DID here (recorded the id), not an unconditional "skipped".
  log "dedup: recorded '$_id' in .githooks-skip"
}

dedup_secret_scan() {
  [ "$DO_DEDUP" = 1 ] || { log "dedup: disabled (--no-dedup)"; return 0; }
  if repo_has_local_secret_scan "$1"; then
    log "dedup: repo runs a secret scan locally (pre-commit, $1)"
    add_skip_id "secret-scan"
    # secret-scan is a PROTECTED (security) capability: the dispatcher honors the tracked
    # .githooks-skip for it ONLY when you opt into trust — so by DEFAULT the global scan
    # still runs here (fail-safe) even after this retrofit. We deliberately do NOT
    # auto-write a trusted `git config hooks.skipGlobal` here: that would silently disable
    # a security gate based on a heuristic match (and would go stale if the local hook is
    # later removed). To actually dedup a protected scan, opt in EXPLICITLY, once:
    #   git config --global hooks.trustSkipFile true     (honor tracked .githooks-skip), or
    #   git -C <repo> config --add hooks.skipGlobal secret-scan   (per-repo, user-chosen).
    log "dedup: '.githooks-skip' written (reproducible). secret-scan is PROTECTED, so the"
    log "       global scan still runs until you opt in: 'git config --global"
    log "       hooks.trustSkipFile true' OR 'git -C \"$REPO\" config --add hooks.skipGlobal secret-scan'."
  else
    log "dedup: no local secret scan detected — global secret-scan will run here"
  fi
}

# ----------------------------------------------------------------------------------
# COMMIT: stage and commit ONLY the exact files THIS run wrote (the $TOUCHED list) —
# never a whole directory, never a file we didn't change — so a pre-existing unrelated
# edit is not swept into the wiring commit. Raw .git/hooks files are not in $TOUCHED
# (they live under .git, not tracked) so --commit is a no-op for raw repos; we say so.
# Idempotent: a re-run touches nothing => nothing staged => no commit.
#
# SAFETY: if a touched file was ALREADY dirty before we ran (pre-existing unrelated edit
# or staged change in the SAME file), we cannot split our wiring hunk from it, so we
# REFUSE --commit and tell the user to commit by hand. Returns non-zero ONLY on a real
# git failure (so the script's "non-zero on real failure" contract holds); "nothing to
# commit" and "refused (dirty)" are deliberate no-ops that return 0.
# ----------------------------------------------------------------------------------
commit_wiring() {
  [ "$DO_COMMIT" = 1 ] || return 0
  # Build the argv from $TOUCHED (newline-separated, repo-relative). Only keep paths
  # that (a) exist and (b) are inside the work tree, i.e. excludes .git/hooks raw files.
  set --
  _OLDIFS="$IFS"; IFS='
'
  for p in $TOUCHED; do
    [ -n "$p" ] || continue
    [ -e "$REPO/$p" ] || continue
    if was_pre_dirty "$p"; then
      IFS="$_OLDIFS"
      log "commit: '$p' had pre-existing uncommitted changes — refusing --commit so an"
      log "        unrelated edit isn't bundled. Commit the wiring by hand."
      return 0
    fi
    set -- "$@" "$p"
  done
  IFS="$_OLDIFS"
  if [ $# -eq 0 ]; then
    log "commit: nothing tracked to commit (raw .git/hooks aren't tracked) — no-op"; return 0
  fi
  # Stage exactly those paths. A real `git add` failure is fatal (don't mask it).
  if ! git -C "$REPO" add -- "$@"; then
    log "commit: 'git add' failed"; return 1
  fi
  if git -C "$REPO" diff --cached --quiet -- "$@"; then
    log "commit: tracked wiring already committed / unchanged — no-op"; return 0
  fi
  msg="chore(hooks): wire global git-hook dispatcher (+ dedup) [$MARKER]"
  # --no-verify: this commit only touches hook wiring; running the very hooks we are
  # installing against their own wiring commit would be circular. Path-scoped commit so
  # only the staged wiring paths are recorded even if the index had other entries. A real
  # commit failure (something IS staged but git rejects it) is fatal.
  if git -C "$REPO" commit --no-verify -m "$msg" -- "$@" >/dev/null; then
    log "commit: committed wiring ($*)"
  else
    log "commit: 'git commit' failed"; return 1
  fi
}

# PREFLIGHT: a cheap SECURITY guard that runs BEFORE any hook file is mutated — a symlinked
# or non-regular .githooks-skip is suspicious (a cloned repo could point it outside the
# worktree) and is rejected up front regardless of whether a write turns out to be needed,
# so we never partially wire a repo just to discover the skip file is hostile. The
# WRITABILITY of the file is NOT checked here: it only matters if a write is actually
# required, so add_skip_id checks it on the write path (after skipfile_has_id decides).
preflight() {
  [ "$DO_DEDUP" = 1 ] || return 0
  if [ -L "$SKIPFILE" ]; then
    echo "install-local-hooks: refusing — $SKIPFILE is a symlink (must be a regular file). Nothing changed." >&2
    return 1
  fi
  if [ -e "$SKIPFILE" ] && [ ! -f "$SKIPFILE" ]; then
    echo "install-local-hooks: refusing — $SKIPFILE is not a regular file. Nothing changed." >&2
    return 1
  fi
  return 0
}
preflight || exit 1

# Snapshot which paths are already dirty BEFORE we touch anything (used by --commit to
# refuse sweeping a pre-existing unrelated edit). Cheap and harmless when --commit is off.
snapshot_dirty

manager="$(detect_manager)"
log "detected manager: $manager"
case "$manager" in
  lefthook) inject_lefthook ;;
  husky)    inject_husky ;;
  raw)      inject_raw ;;
esac
dedup_secret_scan "$manager"
commit_wiring
log "done"
