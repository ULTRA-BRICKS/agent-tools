---
name: strict-ticket-discipline
description: 'Use before any non-trivial change, filing a ticket, or closing one. Every change starts from a ticket (task-cli/GitHub Issue/Linear) written before work: concrete problem, evidence FIRST (screenshot/failing run/repro), user impact, acceptance criteria. Enforces no unchecked-box closes; checked boxes need visual proof.'
---

# Every non-trivial change starts from a ticket

Code without a ticket is a change nobody agreed to. The ticket is where the
*why* lives — the motivation, who it's for, and what "done" means. Write that
down **before** you touch code, not after. A commit that can't point at a ticket
is a commit whose intent exists only in a chat scrollback that's gone the moment
the session ends.

## The rule

Before starting a non-trivial change, open a ticket (`task` / GitHub Issue /
Linear) that states:

- **Motivation** — why this change, what problem it solves. Not "improve X" but
  the concrete failure or gap.
- **Acceptance criteria** — the checklist that makes it *done* and verifiable.
  If you can't write these, the change isn't understood yet.
- **User-impact** — who is affected and how. "Internal refactor, no user-facing
  change" is a valid answer — but it's an answer you wrote down, not skipped.

Then reference that ticket from the work: in the commit message (`Refs #123`,
`task:ABC-12`, a Linear `ENG-456`) and/or the PR. The `require-ticket-before-commit`
agent-hook checks for that reference at commit time.

**Write `Refs`, never `Closes`/`Fixes`/`Resolves` (or `closed`/`fixed`/`resolved`), before an
issue number or ticket code — in the PR title and body as well as commits.** Those are
GitHub's magic-close keywords, and Linear's GitHub integration honours the same words before
a ticket code: the ticket flips to Done the instant the PR merges, behind every gate below
(no criterion checked, no proof). `Refs` links the PR to the ticket without closing it; the
ticket closes only through `task done` after acceptance. `gh ship` refuses a PR carrying a
magic-close keyword (`--rewrite-magic-close` rewrites it to `Refs`), and runs
`task gate <code>` before merging — the merge is refused while any criterion is unchecked or
checked without a proof (see `ci/ship/README.md`).

## What a ticket must contain — the evidence-first standard

Motivation, acceptance, and user-impact are the floor. A ticket worth filing is
*self-explanatory to someone with no prior context* — the person who picks it up
(a teammate on a phone, another agent cold) should not have to re-derive a single
thing. A vague ticket gets rejected. Write these sections, **in this order**:

1. **What is concretely wrong** — the specific defect or gap, pinned with
   `file:line` or the exact user action that misbehaves. Not "X is suboptimal" —
   "X does Y when it should do Z".
2. **Evidence — capture what is broken, and how, FIRST, at creation time.** This is
   the rule that makes the difference. The moment you identify a problem worth a
   ticket, record the proof *before moving on to anything else* — never "I'll add a
   screenshot later":
   - **User-observable UI behavior** → a SCREENSHOT of the broken state, taken via
     Playwright or the project's Docker e2e harness. **Never `screencapture`** — a
     desktop grab depends on permissions and the active screen and breaks silently;
     drive the capture through the browser/Electron over CDP instead (this is the
     `visual-proof-cycle` capture, applied at ticket time). The "before" (broken
     state) is mandatory at creation; the "after" follows once the fix lands.
   - **CI / gate failure** → the failing run output or the finding count (e.g. the
     red Semgrep scan, the exact failing assertion).
   - **Pure code / logic** → the offending snippet (`file:line`) plus a failing
     repro that demonstrates the bug.
   - If a proof genuinely cannot exist yet (an unbuilt feature has no "after"), say
     so explicitly and capture the CURRENT / broken behavior instead. Being explicit
     about what is observable *now* is not the same as skipping evidence.
3. **Where this came from** — the origin: the session, audit, review, or brainstorm
   that surfaced it, plus the related ticket / PR / migration or decision that caused
   it. So no one has to reconstruct the trail.
4. **What happens if we don't do it** — the concrete consequence of leaving it
   unfixed, and **how it hurts the user** specifically. Not abstract "tech debt" —
   the actual broken thing the user hits.
5. **Acceptance criteria** — a checkbox list of verifiable conditions, each provable
   by a test or an observable behavior, and each naming its proof method (an e2e
   spec, a CI scan, a unit test). This is the acceptance checklist from "The rule"
   section above, sharpened: every item says *how* you'll prove it.
6. **Pseudocode** — when the fix shape is non-obvious, the minimal pseudocode of the
   change: the seam, the key branch, the data flow. Omit it only when the change is
   truly trivial.

This standard applies to Linear tickets, `TaskCreate` / `task` tasks, GitHub issues,
and any other tracked work item.

## The five documentation rules

The evidence-first standard above is *what to write*. These five rules are the
*enforced shape* of that writing — they make a sloppy ticket rejectable instead of
relying on goodwill. The mechanical checks are being added to the ticket CLI (task-cli)
as the companion change to this skill; once wired, task-cli runs them at
create / update / close / check time. Until then — and on any harness without that CLI —
this skill is the discipline you hold yourself to, and the
`require-ticket-before-commit` agent-hook still enforces the commit-side half of rule 1
(a commit must reference a ticket). Rules 1–4 are
**mechanically checkable** (a link is present, every box is checked, each box has an
attached proof, the criterion count is ≥2). Rule 5 is qualitative: the CLI can enforce
its *presence and minimum substance* (a non-empty user-impact that isn't a one-line
title-restate), but whether the prose is genuinely plain-language and full-context is a
review judgment — treat rule 5 as a guideline the reviewer holds, not a hard machine
verdict. Four rules carry a deliberate `--force "<reason>"` escape, for two different
reasons: for rule 1 it clears a *false positive* (the matcher fired on text that isn't
really an entity); for rules 3, 4, and 5 it records a *legitimate exception* where the
check fired correctly but there's an honest reason to proceed (a criterion with no visual
surface; a genuinely atomic single-criterion change; a genuinely one-line internal-only
user-impact). Rule 2 has no `--force`; instead a criterion is explicitly struck with a
recorded reason. Either way the reason is mandatory and recorded, so every escape is
auditable, not silent.

1. **Every related entity is a LINK, never a bare reference.** Every ticket, PR,
   commit, or repo a ticket body mentions must be a real clickable link — a full URL or
   a markdown link — never bare plain text. The tooling flags anything in the body
   that *reads as* a related-entity reference — a ticket key, an issue/PR number, a commit
   SHA, a repo — when it is not already a link, and blocks until each bare one is wrapped in
   a link. Text already wrapped as a markdown link or a bare URL is left alone, so a
   correctly-linked entity never re-triggers. An entity-shaped token anywhere else still
   flags — *including inside backticks*, so a bare `` `HYP-789` `` is not a silent loophole.
   The match deliberately errs toward flagging, so genuine false matches happen — a version
   or build id that reads like a SHA, a slash-word (`and/or`, a `path/like` token) that reads
   like a repo, an acronym that reads like a ticket key, a file path caught as an `org/repo`.
   `--force "<reason>"` clears exactly those — with a recorded reason, never by hiding the
   token in code — and the reason names which one it is. **Why:** a bare `HYP-789` in a ticket body is a dead
   end — the reader has to guess the tracker, copy the string, and search. A link is one
   tap, and it carries live state (open / closed / merged) the bare id can never show.

2. **A ticket cannot be CLOSED while any acceptance-criterion box is unchecked.**
   Closing *is* the assertion "every acceptance criterion is met." An unchecked box
   makes that assertion false, so the close is refused until the box is checked (or the
   criterion is explicitly struck with a recorded reason). **Why:** a ticket closed with
   unchecked boxes silently drops scope — the unmet criterion vanishes the moment the
   ticket closes, and nobody notices until the user hits the gap.

3. **A box cannot be CHECKED without an attached visual proof.** Checking an
   acceptance box is a claim that the criterion is *verifiably* met; the claim needs a
   **visual** artifact — a screenshot, a screen recording, or rendered output — attached
   to or linked from that criterion. `--force "<reason>"` covers the case where a visual
   proof is genuinely impossible or impractical (a pure-logic criterion proven by a named
   passing test, an infra change with no rendered surface), and the reason must name the
   alternative proof — so a non-visual proof like a named test passes *through the
   escape*, not the default path. **Why:** a checked box with no proof is just an assertion, and
   assertions regress. This is `visual-proof-cycle` / `adversarial-verification` applied
   per criterion: prove it with an artifact, not "correct by construction".

4. **A ticket must have at least 2 acceptance criteria.** One criterion is almost
   always an under-specified ticket that restates its own title as a single "make it
   work" box. Two or more forces you to split *done* into separately verifiable
   conditions — the happy path **and** its boundary, the new behavior **and** the thing
   it must not break. `--force "<reason>"` covers the rare genuinely atomic change with
   one honest criterion. **Why:** a single criterion hides the edges; the bug always
   lives in the edge the one-liner never named.

5. **User-impact is plain-language, detailed, full-context.** Write the user-impact
   for someone only *weakly* familiar with the product. Describe the consequence in the
   user's-world terms — what they see, do, or lose — not in internal jargon, component
   names, or ticket-speak. Give the full context: what the user was trying to do, what
   happens instead, and why it matters to them. The CLI checks that the section is
   present and substantive (not empty, not a one-line restate of the title); `--force
   "<reason>"` covers a flagged-but-acceptable case (e.g. an internal-only change whose
   honest impact really is one line). The quality itself — *is this actually
   plain-language?* — is the reviewer's call. **Why:** the user-impact is read by the
   people who set priority and who do *not* live in the code — a PM, a support lead, the
   CTO on a phone. Jargon tells them nothing; a concrete scenario tells them everything.

### Good vs bad (the `HYP-789` ticket)

**Bad** — bare references, unprovable boxes, one criterion, jargon impact:

> **Related:** Blocked by HYP-789, see #4321, regressed by a1b2c3d, all in acme/api.
> **Acceptance:** [ ] reconciler no longer skips a tick.
> **User-impact:** Debounce race in the canvas reducer drops the trailing patch → stale vnode.

Four bare references the matcher flags (`HYP-789`, `#4321`, `a1b2c3d`, `acme/api`) — none
is stored as an explicit link (platform auto-linking, where it happens at all, is not
enough — rule 1 wants the link written in); a single box with no proof and no way to
verify; an impact line only the author understands.

**Good** — links, ≥2 provable boxes with proof, plain-language impact:

> **Related:** Blocked by [HYP-789](https://linear.app/acme/issue/HYP-789); see
> [#4321](https://github.com/acme/api/pull/4321); regressed by
> [a1b2c3d](https://github.com/acme/api/commit/a1b2c3d), all in
> [acme/api](https://github.com/acme/api).
> **Acceptance:**
> - [x] Typing fast then immediately clicking Save persists the last keystroke — proof: a before-and-after screenshot from e2e `save-debounce.spec.ts`.
> - [x] An existing slow-typed save still works unchanged — no rendered surface; proven by the named regression run `save-existing.spec.ts`.
> **User-impact:** When you edit a component and hit Save quickly, the last edit
> sometimes doesn't get saved. You see no error — the file just silently keeps the old
> content, so you lose work and don't find out until later, when you reopen the file and
> your change is gone.

The `--force "<reason>"` escapes are **arguments to the ticket CLI's commands**
(create / update / close / check) — the tool records the reason on the action. You do not write the literal
`--force …` string into the ticket body. The second box above has a test, not a visual,
as its proof, so it was checked *through* that escape — the body states the proof, the
CLI records the `--force` reason.

## What counts as "non-trivial"

Use a ticket for: features, bugfixes, refactors, behavior changes, anything that
ships to a user or another consumer. **Skip** the ticket for genuinely trivial
chores — a typo fix, a comment, a lockfile bump, a formatting-only commit. The
hook treats `chore:`/`docs:`/`style:` commits and `wip`/fixup commits as exempt
by default (see its README), so the gate doesn't punish the small stuff.

## Why before, not after

A ticket written *after* the code is a description of what you built, not a
contract for what you should build. Writing acceptance criteria first is the same
discipline as `tdd-red-first`: stating the target before the implementation keeps
the implementation honest. It also lets someone push back *before* the work is
sunk, not after.

This pairs with `deferred-findings-tracking` (a finding you defer becomes a
ticket too — and it carries the same evidence-first standard) and
`promise-durable-action` (an intention without a durable record evaporates). The
ticket is that durable record for planned work. Capturing the broken-state proof at
ticket time is `visual-proof-cycle` applied early; trusting that proof rather than
your own assertion is `adversarial-verification`.

## Do not

- **Do NOT ask "should I capture a screenshot / evidence?"** — capture it. Do it as
  the work surfaces the problem, not as a follow-up question.
- **Do NOT file a one-liner "fix X" and move on.** If it is worth a ticket, it is
  worth the sections above.
- **Do NOT claim a proof exists when it does not** — be explicit about what is
  observable now versus only after the fix.
- **Do NOT name a local file as a proof** — never a tmp-dir path, a home-dir path, `~/…`,
  `$HOME`, or a `file://` URL in a PR, issue, comment, review, release note, or a `task`
  field/`--proof`. Nobody reading GitHub can open it and it is gone after a reboot, so the
  box is "proven" by nothing. A proof is **attached content or an http(s) link**: paste the
  output as a fenced block (```` ```text ````) or inside `<details>`, upload the image and link its https URL, or
  pass `task check --proof` an https URL / an existing image it uploads. Command examples use
  repo-relative paths or `<path>` placeholders. The `local-path-in-gh` agent-hook blocks the
  write mechanically (bypass: one-time Telegram hatch `RIG_HATCH_REQUEST_LOCAL_PATH_IN_GH`).

## Common mistakes

- **Retroactive ticket** — filing the issue right before the commit, copying the
  diff into it. That's a receipt, not a spec. Write it first.
- **"I'll add the screenshot later"** — the evidence is the part that decays
  fastest. The broken state you can reproduce now may be gone after the fix or after
  the environment moves on. Capture it at creation time.
- **Empty acceptance criteria** — "make it work" is not a criterion. List the
  observable conditions that prove it's done, each with its proof method.
- **Ticket reference only in chat** — the reference must be in the commit/PR, not
  just something you told the reviewer. Chat scrollback doesn't survive.
- **Over-ticketing trivia** — a one-character typo fix doesn't need a ticket.
  Reserve the discipline for changes someone would want to have agreed to.
