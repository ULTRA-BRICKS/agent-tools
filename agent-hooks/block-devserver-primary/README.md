# block-devserver-primary

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 40

Blocks launching a dev server / dev-watch process (`npm run dev`, `vite`, `next dev`, ...)
while the effective working directory sits on an **enrolled** repo's **default branch**.

## The gap this closes

`worktree-only-writes` (pre-write) already denies an Edit/Write on the default branch, and
`pin-primary-worktree` (pre-bash) already denies a `git checkout`/`switch` off it. Neither sees
a dev server starting — it's not an Edit/Write tool call and not a `git` command. Claude Code's
own built-in worktree-isolation Bash guard is also git-focused: verified live, a plain
`cd <shared-checkout> && npm run dev` (zero `git` anywhere) sails through it untouched.

**Reproduced (2026-08-28, internal task #191):** hyperide's canvas preview generator
(`lib/preview-generator`) overwrites `client/App.tsx` and the git-tracked
`client/__canvas_preview__.tsx` as a side effect of the dev server running. One "quick live
verification" pass against the shared checkout corrupted both files with 2000+ generated
lines — no `Edit`/`Write` tool call and no `git` command anywhere in that session for any
existing hook to catch.

## What's detected

A fixed, well-known list of dev-server launch shapes: `npm`/`yarn`/`pnpm run dev|start|preview|
serve` (and the bare `npm start` / `yarn dev` forms, plus `npm exec`/`pnpm dlx` as the
package-manager equivalent of `npx`), `bun run dev|start` / `bun dev|start`, `npx`/`bunx
vite|next dev|astro dev`, the bare binaries `vite`, `next dev`, `astro dev`, `webpack serve` /
`webpack-dev-server`. A directory-changing flag (`--prefix`/`-C`/`--dir`/`--cwd`) is resolved as
a ONE-SHOT cwd override for that one launch (`npm --prefix <shared-checkout> run dev` from a
feature worktree is still caught, without ever changing the tracked `cd` state for a later
segment). A literal leading `cd <dir> &&` prefix in the same command is tracked (also via `;`)
so `cd /path/to/shared-checkout && npm run dev` is caught even when the agent's own shell `cwd`
is elsewhere; a literal `\`-newline continuation is folded first so a wrapped command isn't
split into two unclassifiable pieces. Read-only info flags (`--help`/`-h`/`--version`/`-v`) are
excluded so `vite --version` doesn't trip the gate.

## What's deliberately NOT covered

**Generic file mutation via Bash (`>`, `tee`, `sed -i`, `rm`) is a DIFFERENT hook's job, already
done — not a gap this hook needs to fill.** The sibling `no-shell-file-edit` hook (also
`pre-bash`) already hard-blocks an in-place edit or redirect onto a tracked source file
REPO-WIDE, on every branch, unconditional on `worktree_only` enrollment — see its own README
for the full parsed-not-raw-matched coverage. A worktree-scoped reimplementation here would be
redundant, not additive. A bare `git commit` directly in the primary checkout is likewise
handled by design elsewhere: `require-review-before-commit` / `require-ticket-before-commit`
gate every commit regardless of location, and an unpushed commit on the primary checkout's
`main` is still caught at push time by the pre-push `protect-main` git-hook — `worktree-only-
writes`'s own docstring documents this split (pre-write blocks the AUTHORING, pre-push blocks
the PUSH). What was genuinely uncovered by anything — this repo's own hooks OR Claude Code's
built-in worktree-isolation Bash guard (confirmed live to be git-command-focused) — was a
dev-server LAUNCH: nothing recognized `npm run dev` at all, on any branch, in any checkout.
That's the one gap this hook exists to close, with a narrow, fixed, low-false-positive
signature rather than a general "any Bash mutation" detector.

## Per-repo opt-in

Shares the **same** knob as the two sibling hooks — one feature, one flag:

1. `RIG_WORKTREE_ONLY=1` (force on) / `=0` (force off).
2. the repo's committed `rig.yaml` → `agent_hooks.worktree_only: true`.
3. default OFF.

## No self-service bypass — external approval only

Deny-by-default, same pattern as the sibling hooks: no env-var self-grant. A genuine one-off
need requests a one-time Telegram approval with a written justification in
`RIG_HATCH_REQUEST_BLOCK_DEVSERVER_PRIMARY`; a bare `1`/`true`/`yes`/`on` is rejected outright
(no Telegram round-trip). Approved → allow; declined, timed out, or unset → block.

A late approval is not wasted: if the human approves after the ask window expires, the tg-ctl daemon keeps it as a single-use `approved:<id>` grant bound to this exact operation (RIG_HATCH_REQUEST_BLOCK_DEVSERVER_PRIMARY="approved:<id>" inline for a retry of the SAME command in the SAME directory; for any hook — pre-write included — simply retrying the identical operation lets the shared lib consume the grant automatically via its pull, which requires this session's route identity to match the original ask — if the retry still denies, ask the human via `tg --tag decision`). A changed command/edit or a different directory is denied and does not consume the grant. See the shared lib docstring (`lib/agenttools_hatch_escalation/`) for the full Late-Approval Grant Protocol (agent-tools#742).

## Known scope limits (heuristic, not a sandbox)

- Only a literal leading `cd <dir> &&` prefix in the SAME command is tracked; a `cd` inside a
  subshell, a function, or a later segment reached via `;`/`|` after the first is not re-tracked
  beyond the simple running-cwd update this hook does per segment. A bare `cd` (→ `$HOME`) or
  `cd -` (→ `$OLDPWD`, a value this hook never sees) are both left UNTRACKED — the running cwd
  stays at whatever it was, so a segment after either shape is judged against a possibly-stale
  directory rather than the real one.
- **A `cd` target (or a directory flag's value) must be a LITERAL path.** `cd "$SHARED_CHECKOUT"
  && npm run dev` or `cd $(dirname "$REPO")/hyperide && npm run dev` are not expanded — the
  hook has no shell to evaluate `$VAR`/`$(...)` against, so the literal, unresolvable string is
  used as the cwd, `git -C` on it fails, and the gate fails open (allow). This is the easiest
  non-literal evasion of the exact incident shape; same limitation for `bash -c "npm run dev"`
  (an opaque single argument, not decomposed) and `npm --prefix "$VAR" run dev`.
- The detected command list is fixed and will miss an unlisted tool (a bespoke dev-server
  wrapper script, an uncommon framework CLI) or an unusually-named package.json script that
  doesn't share a `dev`/`start`/`preview`/`serve` prefix before its first `:` (a colon-namespaced
  variant like `dev:client` IS matched). Extend `_classify_devserver_segment` /
  `_is_dev_script`'s known-script set if a new incident surfaces one.
- Cannot see writes made by a process this hook already ALLOWED to start (e.g. one already
  running from a previous Bash call, or one launched from an un-enrolled/feature-branch
  checkout that later gets switched). This hook only gates the launch command itself.
- **`npx`/`bunx` flag-skipping doesn't distinguish a valueless flag from one that takes a
  value.** `npx -p cowsay vite` reads `cowsay` (the `-p` package-name value) as the tool, misses
  `vite` entirely, and allows the command. Same for `npx -p webpack webpack serve`. A full argv
  parser for every possible `npx` flag was judged not worth the complexity for a discipline
  gate; the direct-binary and `npm run <script>` forms are unaffected.
- **Heredoc bodies are not exempted from chain-splitting.** `_split_chain` has no heredoc
  awareness, so a line inside a `cat > file <<'EOF' ...
  EOF` body that happens to read `npm run dev` (e.g. documentation being written into a
  README/script) is split out as its own segment and classified like a real launch — a false
  BLOCK on an enrolled default-branch repo. `pin-primary-worktree` shares the same limitation
  for `git checkout` text; here it's more likely to bite because `npm run dev` / `vite` are
  common strings inside documentation and scripts. Not yet worked around — if this causes a
  real false positive, either special-case `<<`/`<<-` markers in `_split_chain` or accept the
  Telegram hatch as the escape hatch.
- A line continuation is folded before chain-splitting (see `_normalize_line_continuations`),
  PARITY-checked (Codex review, PR agent-tools#469, round 3) — an ODD run of backslashes
  immediately before a newline is a real continuation and folds; an EVEN run collapses to that
  many literal backslashes with the newline left as a REAL separator (two backslashes before a
  newline is one literal `\` in a real shell, not a continuation). Any other multi-line shape
  (heredocs above, an unescaped newline meant as free-form text inside an unclosed quote that
  `shlex` itself already rejects) is unaffected.
- ~~Shell comments and backslash-escaped chain-operator characters were not recognized.~~ —
  **fixed** (Codex review, PR agent-tools#469, round 3). `_split_chain` now tracks an unquoted,
  unescaped `#` at the start of a word as opening a shell comment (consumed verbatim to the next
  real newline — nothing inside it, including what looks like a chain operator, is real syntax),
  and treats a backslash-escaped `;`/`|`/`&`/`&&`/`||` as the LITERAL character it is in a real
  shell, not a real operator. Both were real bypasses: `: # comment ; cd /feature` + newline +
  `npm run dev`, and `echo \; cd /feature; npm run dev`, both previously tracked a `cd` that
  never actually executes and could wrongly ALLOW a launch that really happens in the original,
  still-protected checkout.
- **A COMMAND WRAPPER — `nohup`, `env`, `sudo`, `time`, `nice`, `setsid`, `exec`, `command`, `bash
  -c`/`sh -c` — is not unwrapped before classification.** `nohup npm run dev &` (the classic
  "leave the dev server running and detach" shape — arguably the MOST realistic way an agent
  actually launches one) has head token `nohup`, not `npm`, so it is NOT recognized and is
  ALLOWED. Same for `env npm run dev`, `sudo -u dev npm run dev`, `bash -c "npm run dev"`. The
  sibling `no-shell-file-edit` hook already unwraps `bash -c`/`sh -c` and peels `VAR=` prefixes
  for its own detection — this hook only does the `VAR=` peel (`_strip_leading_assignments`),
  not the wrapper unwrapping. Tracked as a follow-up
  ([agent-tools#463](https://github.com/alex-mextner/agent-tools/issues/463)) rather than folded
  into this PR: unwrapping needs its own care (recursion depth cap, `bash -c` re-tokenizing the
  inner script, `env`'s own `VAR=val` args vs. its target command) and deserves review on its
  own rather than inflating this PR further; the RIG_HATCH_REQUEST_BLOCK_DEVSERVER_PRIMARY
  escape hatch and simply not doing this in a shared checkout remain the mitigations until then.
- ~~`_split_chain` doesn't track which operator precedes a `cd` segment~~ — **fixed** (Codex
  review, PR agent-tools#469, two rounds). `main()` now tracks a SET of possible `effective_cwd`
  states, not a single one. A `cd` reached via `&&`/`||` (whose actual execution depends on an
  exit code this hook cannot evaluate) BRANCHES the set into both possible outcomes (the `cd`
  ran / didn't run) — a later dev-server segment is blocked if EITHER possible state is gated.
  A `cd` piped into/out of another command or backgrounded (`cd X | cat`, `cd X &`) runs in a
  subshell whose directory change never reaches the parent shell at all — the set is left
  unchanged regardless of what precedes it. Only a `cd` that is BOTH unconditionally reached
  (first segment, or after `;`/newline/`&`) AND not itself isolated in a subshell is trusted
  deterministically. See `_apply_cd_to_candidates`'s docstring for the two asymmetric bypass
  shapes this closes (one per round): `false && cd /feature; npm run dev` from a protected
  checkout (needs "the cd didn't run" to correctly BLOCK) and `true && cd /protected-main;
  npm run dev` from a feature worktree (needs "the cd DID run" to correctly BLOCK) — a single
  fixed assumption in either direction gets one of the two wrong; tracking both keeps both
  correct. `_split_chain` is no longer a byte-identical copy of `pin-primary-worktree`'s — see
  that function's own module comment.
- ~~The candidate-cap could silently drop a gated branch once full.~~ — **fixed** (Codex review,
  PR agent-tools#469, round 3). A candidate is only ever dropped for being over
  `_MAX_CANDIDATE_CWDS` once it's CONFIRMED SAFE (`_resolve_gate` returns `None`) — a new
  candidate that resolves to a gate is always added regardless of the cap, at the cost of a
  small, bounded amount of possible over-cap growth (see `_apply_cd_to_candidates`'s own
  docstring). The original version dropped whichever target arrived once the cap was full,
  including one that was itself gated — order-dependent, not correctness-dependent.
- ~~`npm run`'s value-taking flags (`--workspace`/`-w`, `--script-shell`) weren't consumed.~~ —
  **fixed** (Codex review, PR agent-tools#469, round 3). `_peel_run_flags` (shared by
  `_classify_pm_segment`'s `run` branch) now consumes both the valueless `_RUN_FLAGS` and the
  value-taking `_RUN_VALUE_FLAGS`, split or `=`-joined, before testing for the script name —
  `npm run --workspace web dev` previously stopped at `web` and missed `dev` entirely.
- **The one-line form of this hook's OWN recommended remediation is over-blocked.** `MESSAGE`
  tells an agent to run `git worktree add ../wt-<feature> ...` and then, as a SEPARATE command,
  `cd ../wt-<feature> && <command>` — that two-step form works fine (the `cd` there is the first
  segment of its own command, unconditionally trusted). But the compressed one-liner
  `git worktree add ../wt -b f origin/main && cd ../wt && npm run dev` is BLOCKED: `cd ../wt` is
  reached via `&&` (conditional, from `git worktree add`'s success), so the candidate set
  branches to `{original, ../wt}`, and since `original` is still gated, the whole command is
  blocked even though the launch really would happen in the new worktree. Over-blocking, the
  documented safe direction — run the worktree creation and the `cd … && <launch>` as two
  separate commands (exactly as `MESSAGE` already spells it), or use the Telegram hatch.
- **A subshell — `(npm run dev)`, `$(cd /shared && npm run dev)` — is missed ENTIRELY, not just
  "not re-tracked".** The opening `(` glues to the head token (`(npm`) and the closing `)` glues
  to the last token (`dev)`), so neither `_resolve_cd_target` nor `_classify_devserver_segment`
  recognizes either half — the whole construct is invisible to this hook, not merely
  stale-tracked. (An earlier draft of this note undersold this as a tracking-staleness issue;
  it's a full miss.)

## Test

```bash
python -m pytest -q tests/test_block_devserver_primary.py
```
