#!/bin/sh
# selfcheck.sh — standalone self-check for the global git-hook dispatcher, its
# per-repo wiring (install-local-hooks.sh) and the local-vs-global DEDUP.
#
# Accessed via: `sh git-hooks/global-dispatcher/selfcheck.sh` (or run it directly).
# No test framework needed: it builds throwaway repos + a fake gitleaks under a temp
# HOME, exercises the real scripts, and exits non-zero on the first failed assertion.
# It NEVER touches your real ~/.config/git or any real repo — everything lives under a
# fresh $TMP and the dispatcher is pointed at it via GLOBAL_HOOKS_DIR.
#
# What it proves
#   1. The dispatcher runs an enabled fragment (counter increments).
#   2. DEDUP via .githooks-skip skips the global fragment (counter does NOT increment).
#   3. DEDUP via `git config hooks.skipGlobal` and env GLOBAL_HOOKS_SKIP both work.
#   4. With a local secret-scan + the global one, the check fires EXACTLY ONCE
#      (the double-run is prevented): install-local-hooks.sh auto-writes .githooks-skip.
#   5. install-local-hooks.sh --commit commits the tracked wiring.

set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
DISPATCHER="$HERE/run-global-hooks"
INSTALLER="$HERE/install-local-hooks.sh"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/global-hooks-selfcheck.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT INT TERM

# ISOLATE git + XDG config so the user's real global config (hooks.skipGlobal,
# hooks.trustSkipFile, init.templateDir, core.hooksPath) can't influence the results.
# Everything the tests read lives under $TMP; the dispatcher is pointed at $GHD via
# GLOBAL_HOOKS_DIR. GIT_CONFIG_NOSYSTEM drops /etc/gitconfig; GIT_CONFIG_GLOBAL points the
# global file into $TMP; HOME/XDG keep any $HOME-relative lookups inside $TMP too.
export HOME="$TMP/home"
export XDG_CONFIG_HOME="$TMP/home/.config"
export GIT_CONFIG_GLOBAL="$TMP/home/.gitconfig"
export GIT_CONFIG_SYSTEM="$TMP/home/.gitconfig-system"
export GIT_CONFIG_NOSYSTEM=1
mkdir -p "$HOME" "$XDG_CONFIG_HOME"
: > "$GIT_CONFIG_GLOBAL"
# Unset anything that could redirect new repos' hooks during `git init` in the tests.
unset GLOBAL_HOOKS_SKIP GLOBAL_HOOKS_TRUST_SKIPFILE 2>/dev/null || true

fail=0
ok()   { echo "ok   $*"; }
bad()  { echo "FAIL $*" >&2; fail=$((fail + 1)); }
have() { if [ "$1" = "$2" ]; then ok "$3 ($1)"; else bad "$3 (got '$1' want '$2')"; fi; }
# assert: ok if the command SUCCEEDS; assert_not: ok if it FAILS.
assert()     { if "$@"; then ok "$LABEL"; else bad "$LABEL"; fi; }
assert_not() { if "$@"; then bad "$LABEL"; else ok "$LABEL"; fi; }

# A throwaway global-hooks.d with two counting fragments:
#   10-secret-scan  (id `secret-scan`)  — NOT protected, for the plain dedup mechanics.
#   15-prot-scan    (id `prot-scan`)    — PROTECTED, for the trust tests below.
GHD="$TMP/global-hooks.d"
mkdir -p "$GHD/pre-commit"
COUNTER="$TMP/counter"
: > "$COUNTER"
cat > "$GHD/pre-commit/10-secret-scan" <<EOF
#!/bin/sh
# global-hook-id: secret-scan
echo global-fragment-ran >> "$COUNTER"
exit 0
EOF
cat > "$GHD/pre-commit/15-prot-scan" <<EOF
#!/bin/sh
# global-hook-id: prot-scan
# global-hook-protected: true
echo prot-fragment-ran >> "$COUNTER.prot"
exit 0
EOF
chmod +x "$GHD/pre-commit/10-secret-scan" "$GHD/pre-commit/15-prot-scan"

count() { wc -l < "$COUNTER" | tr -d ' '; }
reset_counter() { : > "$COUNTER"; }
prot_count() { [ -f "$COUNTER.prot" ] && wc -l < "$COUNTER.prot" | tr -d ' ' || echo 0; }
reset_prot() { rm -f "$COUNTER.prot"; touch "$COUNTER.prot"; }

mk_repo() {  # mk_repo <dir>
  mkdir -p "$1"
  git -C "$1" init -q
  git -C "$1" config user.email t@t.t
  git -C "$1" config user.name  t
  ( cd "$1" && echo seed > seed.txt && git add seed.txt \
      && git -c core.hooksPath= commit -q --no-verify -m seed )
}

run_dispatch() {  # run_dispatch <repo>  — invoke the dispatcher from inside the repo
  ( cd "$1" && GLOBAL_HOOKS_DIR="$GHD" "$DISPATCHER" pre-commit </dev/null )
}

# --- 1. baseline: the fragment runs ----------------------------------------------
R1="$TMP/r1"; mk_repo "$R1"
reset_counter; run_dispatch "$R1"
have "$(count)" 1 "dispatcher runs the global fragment"

# --- 2. dedup via tracked .githooks-skip -----------------------------------------
R2="$TMP/r2"; mk_repo "$R2"
printf 'secret-scan\n' > "$R2/.githooks-skip"
reset_counter; run_dispatch "$R2"
have "$(count)" 0 "dedup via .githooks-skip skips the global fragment"

# id with a comment + whitespace is still parsed
printf '# note\n  secret-scan  \n' > "$R2/.githooks-skip"
reset_counter; run_dispatch "$R2"
have "$(count)" 0 "dedup tolerates comments/whitespace in .githooks-skip"

# a DIFFERENT id must NOT skip secret-scan (no false positive on partial match)
printf 'secret\n' > "$R2/.githooks-skip"
reset_counter; run_dispatch "$R2"
have "$(count)" 1 "partial id 'secret' does NOT skip 'secret-scan'"
rm -f "$R2/.githooks-skip"

# --- 3. dedup via git config and via env -----------------------------------------
R3="$TMP/r3"; mk_repo "$R3"
git -C "$R3" config hooks.skipGlobal secret-scan
reset_counter; run_dispatch "$R3"
have "$(count)" 0 "dedup via git config hooks.skipGlobal"
git -C "$R3" config --unset-all hooks.skipGlobal

reset_counter
( cd "$R3" && GLOBAL_HOOKS_DIR="$GHD" GLOBAL_HOOKS_SKIP="secret-scan:other" \
    "$DISPATCHER" pre-commit </dev/null )
have "$(count)" 0 "dedup via env GLOBAL_HOOKS_SKIP"

# --- 4. local + global UNPROTECTED secret-scan => the tracked file skips the global one.
# (Here the dispatcher's `10-secret-scan` is UNPROTECTED, so .githooks-skip alone skips it.
# The SHIPPED fragment is PROTECTED — for that, the user opts into trust; see test #4b.)
R4="$TMP/r4"; mk_repo "$R4"
cat > "$R4/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    no-secrets:
      run: gitleaks git --staged --no-banner
EOF
git -C "$R4" add lefthook.yml
git -C "$R4" -c core.hooksPath= commit -q --no-verify -m "add lefthook secret scan"

GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$R4" >/dev/null
LABEL="auto-dedup wrote 'secret-scan' to .githooks-skip"
assert grep -Fxq secret-scan "$R4/.githooks-skip"
# The local command stays; the GLOBAL (unprotected) copy is now skipped -> contributes 0.
reset_counter; run_dispatch "$R4"
have "$(count)" 0 "unprotected global secret-scan skipped by tracked .githooks-skip"

# --- 4b. the REAL headline: with the PROTECTED shipped secret-scan, the retrofit alone
# leaves it running (fail-safe), and the user opts into trust to get EXACTLY ONE run.
PSC="$TMP/pghd-secret"; mkdir -p "$PSC/pre-commit"
cat > "$PSC/pre-commit/10-secret-scan" <<EOF
#!/bin/sh
# global-hook-id: secret-scan
# global-hook-protected: true
echo ran >> "$TMP/r4-prot.log"
exit 0
EOF
chmod +x "$PSC/pre-commit/10-secret-scan"
rm -f "$TMP/r4-prot.log"; touch "$TMP/r4-prot.log"
# After retrofit (.githooks-skip has secret-scan) but NO trust: protected scan STILL runs.
( cd "$R4" && GLOBAL_HOOKS_DIR="$PSC" "$DISPATCHER" pre-commit </dev/null )
LABEL="protected secret-scan runs by default after retrofit (no trust) => local+global"
assert test "$(wc -l < "$TMP/r4-prot.log" | tr -d ' ')" = 1
# Opt into trust (git config — user-controlled) -> tracked file now skips it -> ONE scan.
rm -f "$TMP/r4-prot.log"; touch "$TMP/r4-prot.log"
git -C "$R4" config hooks.trustSkipFile true
( cd "$R4" && GLOBAL_HOOKS_DIR="$PSC" "$DISPATCHER" pre-commit </dev/null )
LABEL="with git-config trust opt-in, protected secret-scan dedups (exactly once)"
assert test "$(wc -l < "$TMP/r4-prot.log" | tr -d ' ')" = 0
git -C "$R4" config --unset hooks.trustSkipFile

# Idempotency: re-running the installer doesn't duplicate the skip id.
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$R4" >/dev/null
n="$(grep -Fxc secret-scan "$R4/.githooks-skip" || true)"
have "$n" 1 "re-run installer keeps a single 'secret-scan' skip entry"

# A repo WITHOUT a local secret scan must NOT get a skip entry (global still runs).
R5="$TMP/r5"; mk_repo "$R5"
cat > "$R5/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    lint:
      run: echo lint
EOF
git -C "$R5" add lefthook.yml
git -C "$R5" -c core.hooksPath= commit -q --no-verify -m "add lefthook lint"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$R5" >/dev/null
LABEL="no local secret scan => no .githooks-skip (global runs here)"
assert_not test -f "$R5/.githooks-skip"
# Lefthook only forwards the pushed-refs stdin to a pre-push command that sets
# `use_stdin: true`; without it protect-main reads empty stdin and FAILS OPEN.
LABEL="lefthook pre-push wiring dispatches the pre-push event"
assert sh -c "awk '/^[^ \t#]/{inblock = (\$0 ~ /^pre-push:/)} inblock && /run-global-hooks.*pre-push/{found=1} END{exit !found}' '$R5/lefthook.yml'"
LABEL="lefthook pre-push wiring carries use_stdin: true (refs reach protect-main)"
# bind the option to the DISPATCHER command: use_stdin must directly follow its run line
assert sh -c "grep -A1 -- \"run-global-hooks.* pre-push'\" '$R5/lefthook.yml' | grep -q 'use_stdin: true'"
# the SHIPPED template must carry the same guarantee (copy-template path, no injection)
LABEL="shipped lefthook template: pre-push block carries use_stdin: true"
assert sh -c "awk '/^[^ \t#]/{inblock = (\$0 ~ /^pre-push:/)} inblock && /use_stdin: true/{found=1} END{exit !found}' '$HERE/templates/lefthook.yml'"

# --- 5. --commit commits the tracked wiring --------------------------------------
R6="$TMP/r6"; mk_repo "$R6"
cat > "$R6/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    no-secrets:
      run: gitleaks git --staged
EOF
git -C "$R6" add lefthook.yml
git -C "$R6" -c core.hooksPath= commit -q --no-verify -m "base"
before="$(git -C "$R6" rev-parse HEAD)"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" --commit "$R6" >/dev/null
after="$(git -C "$R6" rev-parse HEAD)"
LABEL="--commit created a wiring commit"; assert test "$before" != "$after"
stat="$(git -C "$R6" show --stat --oneline HEAD)"
contains() { case "$2" in *"$1"*) return 0 ;; *) return 1 ;; esac; }
LABEL="wiring commit includes lefthook.yml"
assert contains lefthook.yml "$stat"
LABEL="wiring commit includes .githooks-skip"
assert contains .githooks-skip "$stat"
# Working tree clean afterwards (the wiring is committed, not left dirty).
LABEL="--commit leaves a clean working tree"
assert test -z "$(git -C "$R6" status --porcelain)"
# Re-run --commit is a no-op (idempotent).
before2="$(git -C "$R6" rev-parse HEAD)"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" --commit "$R6" >/dev/null
LABEL="re-run --commit is a no-op"; assert test "$before2" = "$(git -C "$R6" rev-parse HEAD)"

# --commit must NOT sweep an UNRELATED change into the wiring commit. Set up a fresh
# repo, make an unrelated edit, then wire+commit; the unrelated edit must remain
# uncommitted (still dirty) while only the wiring files are committed.
R7="$TMP/r7"; mk_repo "$R7"
cat > "$R7/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    no-secrets:
      run: gitleaks git --staged
EOF
echo unrelated > "$R7/unrelated.txt"
git -C "$R7" add lefthook.yml unrelated.txt
git -C "$R7" -c core.hooksPath= commit -q --no-verify -m "base + unrelated"
echo "edited-after-base" >> "$R7/unrelated.txt"     # an unrelated, uncommitted change
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" --commit "$R7" >/dev/null
LABEL="--commit does NOT sweep an unrelated file"
assert_not git -C "$R7" diff --quiet -- unrelated.txt   # still dirty => not committed
LABEL="wiring commit excludes the unrelated file"
assert_not sh -c "git -C '$R7' show --stat --oneline HEAD | grep -q unrelated.txt"

# --- 6. dedup must NOT trigger on a COMMENTED-OUT local secret scan ----------------
# A repo whose lefthook.yml only has a `# no-secrets:` commented block (like the shipped
# example template) runs no local scan, so the installer must NOT write .githooks-skip
# (doing so would wrongly disable the global secret scan).
R8="$TMP/r8"; mk_repo "$R8"
cat > "$R8/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    lint:
      run: echo lint
    # no-secrets:
    #   run: ./git-hooks/no-secrets-scan
EOF
git -C "$R8" add lefthook.yml
git -C "$R8" -c core.hooksPath= commit -q --no-verify -m "lint + commented no-secrets"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$R8" >/dev/null
LABEL="commented-out no-secrets does NOT write .githooks-skip"
assert_not test -f "$R8/.githooks-skip"

# Presence of git-hooks/no-secrets-scan ALONE (not invoked by any active hook) must NOT
# trigger dedup either.
R8b="$TMP/r8b"; mk_repo "$R8b"
mkdir -p "$R8b/git-hooks"; printf '#!/bin/sh\ngitleaks git --staged\n' > "$R8b/git-hooks/no-secrets-scan"
cat > "$R8b/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    lint:
      run: echo lint
EOF
git -C "$R8b" add -A
git -C "$R8b" -c core.hooksPath= commit -q --no-verify -m base
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$R8b" >/dev/null
LABEL="orphan no-secrets-scan helper does NOT write .githooks-skip"
assert_not test -f "$R8b/.githooks-skip"

# --- 7. --commit REFUSES when a file it would touch is already dirty ---------------
# Pre-existing UNCOMMITTED edit to lefthook.yml itself: the installer must not bundle it.
R9="$TMP/r9"; mk_repo "$R9"
cat > "$R9/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    no-secrets:
      run: gitleaks git --staged
EOF
git -C "$R9" add lefthook.yml
git -C "$R9" -c core.hooksPath= commit -q --no-verify -m base
printf '\n# an unrelated hand edit before wiring\n' >> "$R9/lefthook.yml"   # pre-dirty
head9="$(git -C "$R9" rev-parse HEAD)"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" --commit "$R9" >/dev/null
LABEL="--commit refuses when a touched file was already dirty"
assert test "$head9" = "$(git -C "$R9" rev-parse HEAD)"   # no commit created

# --- 8. raw dedup honours a custom core.hooksPath ----------------------------------
# A repo with core.hooksPath=.githooks and a local gitleaks hook there must get a
# .githooks-skip (the detector resolves the SAME dir inject_raw uses).
R10="$TMP/r10"; mk_repo "$R10"
git -C "$R10" config core.hooksPath .githooks
mkdir -p "$R10/.githooks"
printf '#!/bin/sh\ngitleaks git --staged\n' > "$R10/.githooks/pre-commit"
chmod +x "$R10/.githooks/pre-commit"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$R10" >/dev/null
LABEL="custom core.hooksPath gitleaks hook => .githooks-skip written"
assert grep -Fxq secret-scan "$R10/.githooks-skip"
# ...and with --commit, the tracked custom hook file (not under .git) is committed too.
R10c="$TMP/r10c"; mk_repo "$R10c"
git -C "$R10c" config core.hooksPath .githooks
mkdir -p "$R10c/.githooks"
printf '#!/bin/sh\ngitleaks git --staged\n' > "$R10c/.githooks/pre-commit"
chmod +x "$R10c/.githooks/pre-commit"
git -C "$R10c" add -A; git -C "$R10c" -c core.hooksPath= commit -q --no-verify -m base
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" --commit "$R10c" >/dev/null
LABEL="--commit commits the tracked custom-hooksPath pre-commit hook"
assert sh -c "git -C '$R10c' show --stat --oneline HEAD | grep -q '.githooks/pre-commit'"
LABEL="--commit leaves custom-hooksPath repo clean"
assert test -z "$(git -C "$R10c" status --porcelain)"

# --- 9. pre-push-ONLY local gitleaks must NOT disable the global pre-commit scan ----
# The global secret-scan fragment is pre-commit; a repo whose only local gitleaks runs on
# pre-push must still get the global pre-commit scan (no .githooks-skip written).
R11="$TMP/r11"; mk_repo "$R11"
cat > "$R11/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    lint:
      run: echo lint
pre-push:
  commands:
    no-secrets:
      run: gitleaks git --staged
EOF
git -C "$R11" add lefthook.yml
git -C "$R11" -c core.hooksPath= commit -q --no-verify -m "pre-push gitleaks only"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$R11" >/dev/null
LABEL="pre-push-only gitleaks does NOT write .githooks-skip"
assert_not test -f "$R11/.githooks-skip"

# --- 10. an INLINE comment mentioning gitleaks must NOT trigger dedup --------------
R12="$TMP/r12"; mk_repo "$R12"
cat > "$R12/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    lint:
      run: echo lint # gitleaks handled in CI
EOF
git -C "$R12" add lefthook.yml
git -C "$R12" -c core.hooksPath= commit -q --no-verify -m "inline comment"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$R12" >/dev/null
LABEL="inline-comment 'gitleaks' does NOT write .githooks-skip"
assert_not test -f "$R12/.githooks-skip"

# --- 11. 'gitleaks' as a NON-command token (arg / fail_text) must NOT trigger -------
R13="$TMP/r13"; mk_repo "$R13"
cat > "$R13/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    note:
      run: echo install gitleaks
      fail_text: gitleaks missing
EOF
git -C "$R13" add lefthook.yml
git -C "$R13" -c core.hooksPath= commit -q --no-verify -m "non-command gitleaks tokens"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$R13" >/dev/null
LABEL="non-command 'gitleaks' tokens do NOT write .githooks-skip"
assert_not test -f "$R13/.githooks-skip"

# --- 12. a real gitleaks command after a shell control operator is CONSERVATIVELY not
# deduped (its exit status could be masked by the operator). A single-command gitleaks on
# its OWN line (e.g. a block-scalar body line, see test #33b) IS deduped — that's the real
# blocking gate. Here `echo scanning && gitleaks …` has `&&` on the line, so fail-safe: no
# dedup (the global scan also runs; never a disabled one).
R13b="$TMP/r13b"; mk_repo "$R13b"
cat > "$R13b/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    sec:
      run: echo scanning && gitleaks git --staged
EOF
git -C "$R13b" add lefthook.yml
git -C "$R13b" -c core.hooksPath= commit -q --no-verify -m "gitleaks after &&"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$R13b" >/dev/null
LABEL="gitleaks after '&&' is CONSERVATIVELY not deduped (fail-safe: global scan runs)"
assert_not test -f "$R13b/.githooks-skip"

# --- 13. a pre-commit header with a trailing comment is recognized -----------------
R14="$TMP/r14"; mk_repo "$R14"
printf 'pre-commit:   # the pre-commit gate\n  commands:\n    sec:\n      run: gitleaks git --staged\n' > "$R14/lefthook.yml"
git -C "$R14" add lefthook.yml
git -C "$R14" -c core.hooksPath= commit -q --no-verify -m "pre-commit header w/ comment"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$R14" >/dev/null
LABEL="pre-commit header with trailing comment still dedups"
assert grep -Fxq secret-scan "$R14/.githooks-skip"

# --- 14. .githooks-skip with NO trailing newline appends cleanly -------------------
R15="$TMP/r15"; mk_repo "$R15"
cat > "$R15/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    sec:
      run: gitleaks git --staged
EOF
printf 'othercheck' > "$R15/.githooks-skip"   # NOTE: no trailing newline
git -C "$R15" add -A; git -C "$R15" -c core.hooksPath= commit -q --no-verify -m base
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$R15" >/dev/null
LABEL="append to no-final-newline .githooks-skip keeps 'othercheck' intact"
assert grep -Fxq othercheck "$R15/.githooks-skip"
LABEL="append to no-final-newline .githooks-skip adds 'secret-scan' on its own line"
assert grep -Fxq secret-scan "$R15/.githooks-skip"

# --- 15. a STAGED RENAME of lefthook.yml is seen as pre-dirty by --commit -----------
# git status porcelain emits "R old -> new"; snapshot_dirty must see the NEW path so
# --commit refuses to bundle the rename. (Regression guard for porcelain mis-parsing.)
R16="$TMP/r16"; mk_repo "$R16"
cat > "$R16/oldname.yml" <<'EOF'
pre-commit:
  commands:
    sec:
      run: gitleaks git --staged
EOF
git -C "$R16" add oldname.yml
git -C "$R16" -c core.hooksPath= commit -q --no-verify -m base
git -C "$R16" mv oldname.yml lefthook.yml      # staged rename -> lefthook.yml is dirty
head16="$(git -C "$R16" rev-parse HEAD)"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" --commit "$R16" >/dev/null 2>&1 || true
LABEL="--commit refuses to bundle a staged rename of lefthook.yml"
assert test "$head16" = "$(git -C "$R16" rev-parse HEAD)"

# --- 16. TRUST: a PROTECTED fragment is NOT disabled by a tracked .githooks-skip ----
# alone (a cloned repo can't silently turn off the secret scan), and is NOT disabled by the
# env var either (a repo's lefthook `env:` block can inject env). Only git config (genuinely
# user-controlled) or an explicit trust opt-in disables it.
RP="$TMP/rprot"; mk_repo "$RP"
printf 'prot-scan\n' > "$RP/.githooks-skip"      # tracked file (untrusted by default)
reset_prot; ( cd "$RP" && GLOBAL_HOOKS_DIR="$GHD" "$DISPATCHER" pre-commit </dev/null )
LABEL="protected fragment RUNS despite tracked .githooks-skip (no trust)"
assert test "$(prot_count)" = 1
# env does NOT disable a protected fragment (repo-injectable, untrusted).
reset_prot; ( cd "$RP" && GLOBAL_HOOKS_DIR="$GHD" GLOBAL_HOOKS_SKIP="prot-scan" "$DISPATCHER" pre-commit </dev/null )
LABEL="protected fragment is NOT skipped via env GLOBAL_HOOKS_SKIP (repo-injectable)"
assert test "$(prot_count)" = 1
# repo git config DOES disable (genuinely user-controlled; a cloned repo can't set it).
git -C "$RP" config --add hooks.skipGlobal prot-scan
reset_prot; ( cd "$RP" && GLOBAL_HOOKS_DIR="$GHD" "$DISPATCHER" pre-commit </dev/null )
LABEL="protected fragment is skipped via git config hooks.skipGlobal"
assert test "$(prot_count)" = 0
git -C "$RP" config --unset-all hooks.skipGlobal
# opting into trust via git config (NOT env) honors the tracked file for protected ids.
git -C "$RP" config hooks.trustSkipFile true
reset_prot; ( cd "$RP" && GLOBAL_HOOKS_DIR="$GHD" "$DISPATCHER" pre-commit </dev/null )
LABEL="protected fragment IS skipped by tracked file when git-config trust opted in"
assert test "$(prot_count)" = 0
# but an ENV trust flag is NOT honored (repo-injectable) — protection holds.
git -C "$RP" config --unset hooks.trustSkipFile
reset_prot; ( cd "$RP" && GLOBAL_HOOKS_DIR="$GHD" GLOBAL_HOOKS_TRUST_SKIPFILE=1 "$DISPATCHER" pre-commit </dev/null )
LABEL="env GLOBAL_HOOKS_TRUST_SKIPFILE does NOT trust the file for a protected id"
assert test "$(prot_count)" = 1
# an UNPROTECTED fragment is still skipped by env AND by the tracked file alone.
reset_counter; ( cd "$RP" && GLOBAL_HOOKS_DIR="$GHD" GLOBAL_HOOKS_SKIP="secret-scan" "$DISPATCHER" pre-commit </dev/null )
LABEL="unprotected fragment is skipped via env GLOBAL_HOOKS_SKIP"
assert test "$(count)" = 0
printf 'secret-scan\n' > "$RP/.githooks-skip"
reset_counter; ( cd "$RP" && GLOBAL_HOOKS_DIR="$GHD" "$DISPATCHER" pre-commit </dev/null )
LABEL="unprotected fragment still skipped by tracked file alone"
assert test "$(count)" = 0

# --- 17. installer writes ONLY the tracked .githooks-skip — it never auto-enables a
# trusted skip for the PROTECTED secret-scan. So a real protected scan is NOT disabled by
# the retrofit alone (fail-safe); the user must opt in explicitly.
RP2="$TMP/rprot2"; mk_repo "$RP2"
cat > "$RP2/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    no-secrets:
      run: gitleaks git --staged
EOF
git -C "$RP2" add lefthook.yml
git -C "$RP2" -c core.hooksPath= commit -q --no-verify -m base
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$RP2" >/dev/null
LABEL="installer writes secret-scan to tracked .githooks-skip"
assert grep -Fxq secret-scan "$RP2/.githooks-skip"
LABEL="installer does NOT auto-write trusted git config for secret-scan"
assert_not sh -c "git -C '$RP2' config --get-all hooks.skipGlobal | grep -Fxq secret-scan"
# A protected scan in $GHD with the SAME id would still run here by default (fail-safe).
cp "$GHD/pre-commit/15-prot-scan" "$RP2/expect-prot"   # sanity: protected helper exists
# Build a protected fragment whose id matches what the installer wrote, prove it still runs.
PGHD="$TMP/pghd"; mkdir -p "$PGHD/pre-commit"
cat > "$PGHD/pre-commit/10-secret-scan" <<EOF
#!/bin/sh
# global-hook-id: secret-scan
# global-hook-protected: true
echo ran >> "$TMP/prot-default.log"
exit 0
EOF
chmod +x "$PGHD/pre-commit/10-secret-scan"; rm -f "$TMP/prot-default.log"; touch "$TMP/prot-default.log"
( cd "$RP2" && GLOBAL_HOOKS_DIR="$PGHD" "$DISPATCHER" pre-commit </dev/null )
LABEL="protected secret-scan still RUNS after retrofit (no trust opt-in)"
assert test "$(wc -l < "$TMP/prot-default.log" | tr -d ' ')" = 1

# --- 18. mixed-manager: a STALE inactive .husky/pre-commit must NOT trigger dedup ---
# lefthook is the ACTIVE manager (lefthook.yml present => detected first); its pre-commit
# has no scan. A leftover .husky/pre-commit with gitleaks does NOT run, so it must not
# disable the global secret scan.
RM="$TMP/rmixed"; mk_repo "$RM"
cat > "$RM/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    lint:
      run: echo lint
EOF
mkdir -p "$RM/.husky"
printf '#!/bin/sh\ngitleaks git --staged\n' > "$RM/.husky/pre-commit"  # STALE / inactive
git -C "$RM" add -A
git -C "$RM" -c core.hooksPath= commit -q --no-verify -m "lefthook active + stale husky"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$RM" >/dev/null
LABEL="stale inactive .husky/pre-commit does NOT write .githooks-skip (lefthook active)"
assert_not test -f "$RM/.githooks-skip"
LABEL="stale inactive .husky/pre-commit does NOT set git config skipGlobal"
assert_not sh -c "git -C '$RM' config --get-all hooks.skipGlobal | grep -Fxq secret-scan"

# --- 19. a NEUTRALIZED local scan (`gitleaks ... || true`) must NOT trigger dedup ---
RN="$TMP/rneutral"; mk_repo "$RN"
cat > "$RN/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    no-secrets:
      run: gitleaks git --staged || true
EOF
git -C "$RN" add lefthook.yml
git -C "$RN" -c core.hooksPath= commit -q --no-verify -m "neutralized gitleaks"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$RN" >/dev/null
LABEL="neutralized 'gitleaks ... || true' does NOT write .githooks-skip"
assert_not test -f "$RN/.githooks-skip"

# --- 20. a RELATIVE core.hooksPath OUTSIDE the repo is not swept by --commit --------
# core.hooksPath=../shared-hooks resolves outside $REPO; --commit must NOT mark/stage it
# (the raw string prefix "$REPO/" would wrongly accept it without canonicalization).
SHARED="$TMP/shared-hooks"; mkdir -p "$SHARED"
RH="$TMP/rrelhp"; mk_repo "$RH"
git -C "$RH" config core.hooksPath ../shared-hooks
printf '#!/bin/sh\ngitleaks git --staged\n' > "$SHARED/pre-commit"; chmod +x "$SHARED/pre-commit"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" --commit "$RH" >/dev/null 2>&1 || true
# It may write .githooks-skip (a scan exists in the resolved dir) — that's in-repo and OK.
# The point: the OUT-OF-REPO hook file must NOT be in the wiring commit, and the commit (if
# any) must not reference ../shared-hooks.
LABEL="--commit does not reference an out-of-repo ../shared-hooks file"
assert_not sh -c "git -C '$RH' show --stat --oneline HEAD 2>/dev/null | grep -q 'shared-hooks'"

# --- 21. installer REFUSES to write through a symlinked .githooks-skip --------------
RSY="$TMP/rsymlink"; mk_repo "$RSY"
cat > "$RSY/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    no-secrets:
      run: gitleaks git --staged
EOF
echo target-content > "$TMP/outside-target"
ln -s "$TMP/outside-target" "$RSY/.githooks-skip"   # symlink pointing OUTSIDE the repo
lefthook_before="$(cat "$RSY/lefthook.yml")"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$RSY" >/dev/null 2>&1 || true
LABEL="installer does NOT write through a symlinked .githooks-skip"
assert_not grep -q secret-scan "$TMP/outside-target"
# PREFLIGHT: the symlink is detected BEFORE any hook file is touched -> no partial install.
LABEL="symlinked .githooks-skip aborts install BEFORE mutating lefthook.yml (no partial install)"
assert test "$lefthook_before" = "$(cat "$RSY/lefthook.yml")"

# --- 22. dispatcher IGNORES a symlinked .githooks-skip (even with trust on) ---------
RSY2="$TMP/rsymlink2"; mk_repo "$RSY2"
printf 'prot-scan\n' > "$TMP/evil-skip"
ln -s "$TMP/evil-skip" "$RSY2/.githooks-skip"
git -C "$RSY2" config hooks.trustSkipFile true   # even WITH trust, a symlink is ignored
reset_prot
( cd "$RSY2" && GLOBAL_HOOKS_DIR="$GHD" "$DISPATCHER" pre-commit </dev/null 2>/dev/null )
LABEL="dispatcher ignores a symlinked .githooks-skip (protected fragment still runs)"
assert test "$(prot_count)" = 1

# --- 23. a STALE lefthook.yaml is ignored when lefthook.yml is the active config ----
RYA="$TMP/ryaml"; mk_repo "$RYA"
cat > "$RYA/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    lint:
      run: echo lint
EOF
cat > "$RYA/lefthook.yaml" <<'EOF'
pre-commit:
  commands:
    sec:
      run: gitleaks git --staged
EOF
git -C "$RYA" add -A
git -C "$RYA" -c core.hooksPath= commit -q --no-verify -m "yml active, yaml stale"
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$RYA" >/dev/null
LABEL="stale lefthook.yaml does NOT trigger dedup when lefthook.yml is active"
assert_not test -f "$RYA/.githooks-skip"

# --- 24. idempotency: an existing entry with an INLINE comment is not duplicated ----
RID="$TMP/ridem"; mk_repo "$RID"
cat > "$RID/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    no-secrets:
      run: gitleaks git --staged
EOF
printf 'secret-scan # local gitleaks\n' > "$RID/.githooks-skip"   # already present w/ comment
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$RID" >/dev/null
LABEL="existing 'secret-scan # comment' entry is not duplicated on install"
assert test "$(sed -e 's/#.*//' -e 's/[[:space:]]*//g' "$RID/.githooks-skip" | grep -c '^secret-scan$')" = 1

# --- 25. a repo's lefthook env: block cannot disable the PROTECTED secret scan -------
# Simulate the attack: the env var is set (as a repo's `env:` would when the hook runs)
# AND a tracked .githooks-skip ships it. Neither must disable a protected fragment.
REV="$TMP/renvattack"; mk_repo "$REV"
printf 'prot-scan\n' > "$REV/.githooks-skip"
reset_prot
( cd "$REV" && GLOBAL_HOOKS_DIR="$GHD" GLOBAL_HOOKS_SKIP="prot-scan" "$DISPATCHER" pre-commit </dev/null )
LABEL="repo-injected env + tracked file still cannot disable a protected fragment"
assert test "$(prot_count)" = 1

# --- 26. injection: a `pre-commit: # comment` header is NOT duplicated --------------
# The injector must recognize the existing event header (with a trailing comment) and
# merge into it, not append a second top-level `pre-commit:` key (invalid YAML).
RINJ="$TMP/rinject"; mk_repo "$RINJ"
printf 'pre-commit:   # the gate\n  commands:\n    lint:\n      run: echo lint\n' > "$RINJ/lefthook.yml"
git -C "$RINJ" add lefthook.yml
git -C "$RINJ" -c core.hooksPath= commit -q --no-verify -m base
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$RINJ" >/dev/null
LABEL="injection into 'pre-commit: # comment' adds no duplicate top-level key"
assert test "$(grep -c '^pre-commit:' "$RINJ/lefthook.yml")" = 1
LABEL="injection merged the dispatcher command under the existing pre-commit block"
assert grep -q 'global-git-hooks-dispatcher' "$RINJ/lefthook.yml"
# The result is still valid YAML (parse it if python3/yaml is available; else skip-quietly).
if command -v python3 >/dev/null 2>&1 && python3 -c 'import yaml' 2>/dev/null; then
  LABEL="injected lefthook.yml is valid YAML"
  assert python3 -c "import yaml,sys; yaml.safe_load(open('$RINJ/lefthook.yml'))"
fi

# --- 27. detection recognizes a PATH-QUALIFIED gitleaks invocation ------------------
RPQ="$TMP/rpathq"; mk_repo "$RPQ"
cat > "$RPQ/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    sec:
      run: ./bin/gitleaks git --staged
EOF
git -C "$RPQ" add lefthook.yml
git -C "$RPQ" -c core.hooksPath= commit -q --no-verify -m base
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$RPQ" >/dev/null
LABEL="path-qualified './bin/gitleaks' is detected as a local scan"
assert grep -Fxq secret-scan "$RPQ/.githooks-skip"

# --- 28. detection recognizes an ENV-PREFIXED gitleaks invocation -------------------
RENV="$TMP/renvpfx"; mk_repo "$RENV"
cat > "$RENV/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    sec:
      run: GITLEAKS_CONFIG=cfg.toml gitleaks git --staged
EOF
git -C "$RENV" add lefthook.yml
git -C "$RENV" -c core.hooksPath= commit -q --no-verify -m base
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$RENV" >/dev/null
LABEL="env-prefixed 'VAR=x gitleaks' is detected as a local scan"
assert grep -Fxq secret-scan "$RENV/.githooks-skip"

# --- 29. manager detection: husky is active (core.hooksPath=.husky) despite stale
# lefthook.yml — installer wires husky, not the inactive lefthook config.
RHA="$TMP/rhuskyactive"; mk_repo "$RHA"
mkdir -p "$RHA/.husky/_"
git -C "$RHA" config core.hooksPath .husky/_
cat > "$RHA/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    lint:
      run: echo lint
EOF
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$RHA" >/dev/null
LABEL="husky-active repo wires .husky/pre-commit (not the stale lefthook.yml)"
assert test -f "$RHA/.husky/pre-commit"
# Husky's core.hooksPath bypasses the global composer, so pre-push must ALWAYS be
# created (like pre-commit) — otherwise protect-main never fires in a husky repo.
LABEL="husky-active repo always creates .husky/pre-push (protect-main coverage)"
assert test -f "$RHA/.husky/pre-push"
LABEL=".husky/pre-push dispatches the pre-push event"
assert grep -q 'pre-push "\$@"' "$RHA/.husky/pre-push"
LABEL="husky-active repo does NOT inject the dispatcher into the stale lefthook.yml"
assert_not grep -q 'global-git-hooks-dispatcher' "$RHA/lefthook.yml"

# --- 30. env-injected git config (GIT_CONFIG_COUNT) cannot forge a protected skip ----
# A repo's lefthook env: could set GIT_CONFIG_COUNT/KEY/VALUE to spoof a trusted config.
# The dispatcher reads trusted config with that mechanism stripped -> protection holds.
REC="$TMP/rcfgenv"; mk_repo "$REC"
reset_prot
( cd "$REC" && GLOBAL_HOOKS_DIR="$GHD" \
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=hooks.skipGlobal GIT_CONFIG_VALUE_0=prot-scan \
    "$DISPATCHER" pre-commit </dev/null )
LABEL="env-injected GIT_CONFIG hooks.skipGlobal cannot skip a protected fragment"
assert test "$(prot_count)" = 1
reset_prot
printf 'prot-scan\n' > "$REC/.githooks-skip"
( cd "$REC" && GLOBAL_HOOKS_DIR="$GHD" \
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=hooks.trustSkipFile GIT_CONFIG_VALUE_0=true \
    "$DISPATCHER" pre-commit </dev/null )
LABEL="env-injected GIT_CONFIG hooks.trustSkipFile cannot trust the file for a protected id"
assert test "$(prot_count)" = 1
# GIT_CONFIG_PARAMETERS (the serialized `-c k=v` channel) is also stripped.
reset_prot
( cd "$REC" && GLOBAL_HOOKS_DIR="$GHD" \
    GIT_CONFIG_PARAMETERS="'hooks.trustSkipFile=true'" \
    "$DISPATCHER" pre-commit </dev/null )
LABEL="env-injected GIT_CONFIG_PARAMETERS cannot trust the file for a protected id"
assert test "$(prot_count)" = 1

# --- 31. NON-BLOCKING local scans (`; true`, `|| echo`) do NOT trigger dedup --------
# These include true non-blocking forms AND `|| exit 1` (which DOES block) — we drop every
# `|| …` line conservatively, so even the blocking one does not dedup. That is fail-SAFE:
# the global scan also runs (one extra scan), it is never disabled. (Worst case for the
# user: a redundant scan — easily fixed by an explicit `git config hooks.skipGlobal`.)
i=0
for nb in 'gitleaks git --staged; true' 'gitleaks git --staged || echo warning' 'gitleaks git --staged || :' 'gitleaks git --staged || exit 1' 'gitleaks git --staged; echo ok' 'gitleaks git --staged | tee scan.log' 'gitleaks git --staged &'; do
  i=$((i + 1)); RNB="$TMP/rnb$i"; mk_repo "$RNB"
  { echo 'pre-commit:'; echo '  commands:'; echo '    sec:'; echo "      run: $nb"; } > "$RNB/lefthook.yml"
  git -C "$RNB" add lefthook.yml
  git -C "$RNB" -c core.hooksPath= commit -q --no-verify -m base
  GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$RNB" >/dev/null
  LABEL="conservative: '$nb' does NOT write .githooks-skip (fail-safe: global scan still runs)"
  assert_not test -f "$RNB/.githooks-skip"
done

# --- 32. manager detection: '.husky2' is NOT husky; a custom path falls back to raw --
RH2="$TMP/rhusky2"; mk_repo "$RH2"
git -C "$RH2" config core.hooksPath .husky2
cat > "$RH2/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    sec:
      run: gitleaks git --staged
EOF
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$RH2" >/dev/null
# core.hooksPath=.husky2 is a CUSTOM path => raw. The dispatcher is wired into .husky2/
# pre-commit (raw), NOT injected into the stale lefthook.yml.
LABEL="'.husky2' custom path is treated as raw (wires .husky2/pre-commit)"
assert test -f "$RH2/.husky2/pre-commit"
LABEL="'.husky2' custom path does NOT inject into the stale lefthook.yml"
assert_not grep -q 'global-git-hooks-dispatcher' "$RH2/lefthook.yml"

# --- 33. lefthook detection looks at `run:` VALUES only, not metadata ----------------
# A separator-bearing string in `fail_text:` (not an executed command) must NOT count.
RFT="$TMP/rfailtext"; mk_repo "$RFT"
cat > "$RFT/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    lint:
      run: echo lint
      fail_text: "install failed; gitleaks git --staged"
EOF
git -C "$RFT" add lefthook.yml
git -C "$RFT" -c core.hooksPath= commit -q --no-verify -m base
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$RFT" >/dev/null
LABEL="separator-bearing fail_text 'gitleaks' does NOT write .githooks-skip"
assert_not test -f "$RFT/.githooks-skip"

# ...but a real gitleaks command in a `run: |` BLOCK SCALAR is still detected.
RBS="$TMP/rblockscalar"; mk_repo "$RBS"
cat > "$RBS/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    sec:
      run: |
        echo scanning
        gitleaks git --staged
EOF
git -C "$RBS" add lefthook.yml
git -C "$RBS" -c core.hooksPath= commit -q --no-verify -m base
GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$RBS" >/dev/null
LABEL="gitleaks inside a 'run: |' block scalar IS detected"
assert grep -Fxq secret-scan "$RBS/.githooks-skip"

# --- 34. read-only .githooks-skip: only fails when a WRITE is actually needed ----------
# (a) read-only file that ALREADY has the id -> NO write needed -> install SUCCEEDS.
ROA="$TMP/rro_have"; mk_repo "$ROA"
cat > "$ROA/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    no-secrets:
      run: gitleaks git --staged
EOF
printf 'secret-scan\n' > "$ROA/.githooks-skip"
chmod 0444 "$ROA/.githooks-skip"
rc_a=0; GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$ROA" >/dev/null 2>&1 || rc_a=$?
LABEL="read-only .githooks-skip that ALREADY has the id => install succeeds (no write needed)"
assert test "$rc_a" = 0
chmod 0644 "$ROA/.githooks-skip" 2>/dev/null || true
# (b) read-only file MISSING the id -> a write IS needed -> install exits non-zero.
ROB="$TMP/rro_need"; mk_repo "$ROB"
cat > "$ROB/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    no-secrets:
      run: gitleaks git --staged
EOF
printf 'othercheck\n' > "$ROB/.githooks-skip"
chmod 0444 "$ROB/.githooks-skip"
rc_b=0; GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$ROB" >/dev/null 2>&1 || rc_b=$?
LABEL="read-only .githooks-skip that NEEDS the id => install exits non-zero"
assert test "$rc_b" != 0
chmod 0644 "$ROB/.githooks-skip" 2>/dev/null || true
# (c) a repo with NO local scan + read-only .githooks-skip -> no dedup write attempted ->
# install succeeds (the over-eager preflight that failed this case is gone).
RON="$TMP/rro_noscan"; mk_repo "$RON"
cat > "$RON/lefthook.yml" <<'EOF'
pre-commit:
  commands:
    lint:
      run: echo lint
EOF
printf 'othercheck\n' > "$RON/.githooks-skip"
chmod 0444 "$RON/.githooks-skip"
rc_c=0; GLOBAL_HOOKS_DIR="$GHD" "$INSTALLER" "$RON" >/dev/null 2>&1 || rc_c=$?
LABEL="no-local-scan repo with read-only .githooks-skip => install succeeds (no dedup write)"
assert test "$rc_c" = 0
chmod 0644 "$RON/.githooks-skip" 2>/dev/null || true

# --- 35. protect-main: the SHIPPED pre-push fragment blocks direct pushes to main ---
# Uses the real fragment + the real composers (core.hooksPath) end-to-end through a
# real `git push` to a local bare origin, so the stdin ref-forwarding chain
# (git -> composer spool -> dispatcher spool -> fragment `read`) is what is proven.
GHD_PP="$TMP/ghd-prepush"
mkdir -p "$GHD_PP/pre-push"
cp "$HERE/global-hooks.d/pre-push/10-protect-main" "$GHD_PP/pre-push/"
chmod +x "$GHD_PP/pre-push/10-protect-main"
# Keep the override log inside $TMP — explicit path, NOT $HOME-derived, so a future
# reshuffle of the HOME sandbox can never point this at the user's real audit log.
export XDG_CACHE_HOME="$TMP/home/.cache"
OVLOG="$XDG_CACHE_HOME/agent-tools/overrides.log"
RPM="$TMP/rpm"; mk_repo "$RPM"
git -C "$RPM" branch -M main
git init -q --bare "$TMP/rpm-origin.git"
git -C "$RPM" remote add origin "$TMP/rpm-origin.git"
git -C "$RPM" config core.hooksPath "$HERE/hooks"
pp_push() { ( cd "$RPM" && GLOBAL_HOOKS_DIR="$GHD_PP" git push "$@" >/dev/null 2>&1 ); }

LABEL="protect-main allows the first root commit to an empty remote main"
assert pp_push origin main
( cd "$RPM" && echo second >> seed.txt && git add seed.txt \
    && git -c core.hooksPath= commit -q --no-verify -m second )
LABEL="protect-main blocks a second direct push to main"
assert_not pp_push origin main
LABEL="protect-main allows a feature-branch push"
assert pp_push origin main:feat-x

# A missing main branch is not enough: bootstrap is allowed only when the ENTIRE remote is empty.
# Seed another branch, then prove creating main directly is still blocked.
RPM_NONEMPTY="$TMP/rpm-nonempty"; mk_repo "$RPM_NONEMPTY"
git -C "$RPM_NONEMPTY" branch -M main
git init -q --bare "$TMP/rpm-nonempty-origin.git"
git -C "$RPM_NONEMPTY" remote add origin "$TMP/rpm-nonempty-origin.git"
git -C "$RPM_NONEMPTY" push -q origin main:seed
git -C "$RPM_NONEMPTY" config core.hooksPath "$HERE/hooks"
ne_push() { ( cd "$RPM_NONEMPTY" && GLOBAL_HOOKS_DIR="$GHD_PP" git push "$@" >/dev/null 2>&1 ); }
LABEL="protect-main blocks creating main when remote already has another ref"
assert_not ne_push origin main

# Likewise, an empty remote does not waive history: only a single root commit is bootstrap.
RPM_HISTORY="$TMP/rpm-history"; mk_repo "$RPM_HISTORY"
git -C "$RPM_HISTORY" branch -M main
( cd "$RPM_HISTORY" && echo second >> seed.txt && git add seed.txt \
    && git -c core.hooksPath= commit -q --no-verify -m second )
git init -q --bare "$TMP/rpm-history-origin.git"
git -C "$RPM_HISTORY" remote add origin "$TMP/rpm-history-origin.git"
git -C "$RPM_HISTORY" config core.hooksPath "$HERE/hooks"
hist_push() { ( cd "$RPM_HISTORY" && GLOBAL_HOOKS_DIR="$GHD_PP" git push "$@" >/dev/null 2>&1 ); }
LABEL="protect-main blocks multi-commit first push to an empty remote main"
assert_not hist_push origin main

rm -f "$OVLOG"
LABEL="PUSH_MAIN_OK=1 allows the push to main"
assert env PUSH_MAIN_OK=1 PUSH_MAIN_REASON=selfcheck sh -c \
  'cd "$1" && GLOBAL_HOOKS_DIR="$2" git push origin main >/dev/null 2>&1' _ "$RPM" "$GHD_PP"
have "$(wc -l < "$OVLOG" | tr -d ' ')" 1 "override appended exactly ONE overrides.log line"
# NOTE: match the repo by basename — `git rev-parse --show-toplevel` returns the
# RESOLVED path (macOS: /var/folders -> /private/var/folders), not $RPM verbatim.
LABEL="overrides.log line carries repo + ref + reason"
assert grep -q "/$(basename "$RPM") ref=refs/heads/main reason=selfcheck" "$OVLOG"

# dispatch-once: a template-shim local pre-push (as written by init.templateDir) already
# calls the dispatcher; the composer must NOT dispatch a second time (no duplicate audit
# lines, no double secret scans).
cp "$HERE/template/hooks/pre-push" "$RPM/.git/hooks/pre-push"
chmod +x "$RPM/.git/hooks/pre-push"
# the template shim resolves the runner via XDG_CONFIG_HOME — point it at the real one
mkdir -p "$XDG_CONFIG_HOME/git"
cp "$HERE/run-global-hooks" "$XDG_CONFIG_HOME/git/run-global-hooks"
chmod +x "$XDG_CONFIG_HOME/git/run-global-hooks"
( cd "$RPM" && echo pm >> seed.txt && git add seed.txt \
    && git -c core.hooksPath= commit -q --no-verify -m pm )
rm -f "$OVLOG"
LABEL="template-shim repo: override push still allowed"
assert env PUSH_MAIN_OK=1 PUSH_MAIN_REASON=once sh -c \
  'cd "$1" && GLOBAL_HOOKS_DIR="$2" git push origin main >/dev/null 2>&1' _ "$RPM" "$GHD_PP"
have "$(wc -l < "$OVLOG" | tr -d ' ')" 1 "dispatch-once: template-shim repo logs ONE line, not two"

# a local hook that only MENTIONS run-global-hooks in a comment does NOT count as
# dispatching — the composer must still run the dispatcher (fragment still blocks).
cat > "$RPM/.git/hooks/pre-push" <<'EOF'
#!/bin/sh
# run-global-hooks is handled elsewhere (comment only — no dispatch here)
exit 0
EOF
chmod +x "$RPM/.git/hooks/pre-push"
( cd "$RPM" && echo pm2 >> seed.txt && git add seed.txt \
    && git -c core.hooksPath= commit -q --no-verify -m pm2 )
LABEL="comment-only run-global-hooks mention: composer still dispatches (push blocked)"
assert_not pp_push origin main

# fail-open guard: a shim that REFERENCES the runner but whose runner path is missing
# executes nothing — the dispatch marker stays untouched, so the composer must STILL
# run its own dispatcher (fragment blocks). A static text-match heuristic would have
# been fooled here into skipping every global gate.
cat > "$RPM/.git/hooks/pre-push" <<EOF
#!/bin/sh
DISP="$TMP/does-not-exist/run-global-hooks"
[ -x "\$DISP" ] && { "\$DISP" pre-push "\$@" || exit \$?; }
exit 0
EOF
chmod +x "$RPM/.git/hooks/pre-push"
LABEL="shim with MISSING runner: composer still dispatches (push blocked, no fail-open)"
assert_not pp_push origin main

# deletion of main passes the fragment — the server owns that gate (git refuses
# deleting the current branch by default; relaxed here to isolate the fragment).
git -C "$TMP/rpm-origin.git" config receive.denyDeleteCurrent ignore
LABEL="protect-main lets a DELETION of main pass (server-side owns that gate)"
assert pp_push origin :main

# multi-ref: a push hitting BOTH protected branches must audit each ref, and a blocked
# multi-ref push must name each ref in the refusal.
rm -f "$OVLOG"
LABEL="multi-ref override push (main + master) allowed"
assert env PUSH_MAIN_OK=1 PUSH_MAIN_REASON=multi sh -c \
  'cd "$1" && GLOBAL_HOOKS_DIR="$2" git push origin main main:master >/dev/null 2>&1' _ "$RPM" "$GHD_PP"
have "$(wc -l < "$OVLOG" | tr -d ' ')" 2 "multi-ref override logs one audit line PER protected ref"
LABEL="master audit line present in overrides.log"
assert grep -q "ref=refs/heads/master reason=multi" "$OVLOG"

( cd "$RPM" && echo mr >> seed.txt && git add seed.txt \
    && git -c core.hooksPath= commit -q --no-verify -m mr )
BLOCK_OUT="$( cd "$RPM" && GLOBAL_HOOKS_DIR="$GHD_PP" git push origin main main:master 2>&1 || true )"
LABEL="blocked multi-ref message names BOTH protected branches"
assert sh -c 'printf %s "$1" | grep -q "refs/heads/main" && printf %s "$1" | grep -q "refs/heads/master"' _ "$BLOCK_OUT"
LABEL="blocked message points to the PR flow"
assert sh -c 'printf %s "$1" | grep -q "Land it via a PR"' _ "$BLOCK_OUT"

# --- 36. protect-main END-TO-END through a HUSKY-managed repo -----------------------
# Husky sets core.hooksPath to its own dir, bypassing the global composer entirely —
# coverage comes from the installer ALWAYS creating .husky/pre-push. Prove the whole
# chain live, not just the file's existence: git push -> husky shim -> .husky/pre-push
# -> dispatcher stdin spool -> protect-main `read` (the husky twin of the lefthook
# `use_stdin: true` guarantee — args carry remote/url, the REFS arrive on stdin).
RHPP="$TMP/rhuskypp"; mk_repo "$RHPP"
git -C "$RHPP" branch -M main
mkdir -p "$RHPP/.husky/_"
git -C "$RHPP" config core.hooksPath .husky/_
# fake husky v9 shim: run the same-named user hook, args + stdin pass through
cat > "$RHPP/.husky/_/pre-push" <<'EOF'
#!/bin/sh
h="$(dirname "$0")/../pre-push"
[ -f "$h" ] && exec sh "$h" "$@"
exit 0
EOF
chmod +x "$RHPP/.husky/_/pre-push"
GLOBAL_HOOKS_DIR="$GHD_PP" "$INSTALLER" "$RHPP" >/dev/null
git init -q --bare "$TMP/rhuskypp-origin.git"
git -C "$RHPP" remote add origin "$TMP/rhuskypp-origin.git"
hpp_push() { ( cd "$RHPP" && GLOBAL_HOOKS_DIR="$GHD_PP" git push "$@" >/dev/null 2>&1 ); }
LABEL="husky repo: first root commit to empty remote main passes through protect-main"
assert hpp_push origin main
( cd "$RHPP" && echo second >> seed.txt && git add seed.txt \
    && git -c core.hooksPath= commit -q --no-verify -m second )
LABEL="husky repo: second direct push to main is BLOCKED (refs reach protect-main via stdin)"
assert_not hpp_push origin main
LABEL="husky repo: feature-branch push passes"
assert hpp_push origin main:feat-h

echo
if [ "$fail" -eq 0 ]; then
  echo "selfcheck: ALL PASS"
  exit 0
else
  echo "selfcheck: $fail FAILED" >&2
  exit 1
fi
