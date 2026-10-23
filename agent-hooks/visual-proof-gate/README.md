# visual-proof-gate

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 46

Fires on a `git commit`. If the staged change touches **user-visible** files, it **blocks**
unless a fresh "I looked at a screenshot" marker exists. Enforces `visual-proof-cycle`:
capture the rendered result, read the capture back, verify it — **then** commit. A "done"
claim on a UI change with no screenshot you actually looked at is the failure this stops.

## What counts as user-visible (staged file inspection)

The gate shells out to `git -C <cwd> diff --cached --name-only` and flags a staged file when
it **can render** (agent-tools#973) and is not a test or story:

- **never visible** (checked after the `public/`/`assets/` rule below): `*.test.*` (incl. `*.bun.test.ts`), `*.spec.*`,
  `*.stories.*`, `*.story.*`, anything under `__tests__/`, `__mocks__/`, `__fixtures__/`, and a
  `test/` or `tests/` directory that is NOT below a routing/asset root (`app/ pages/ public/
  assets/ views/ components/ ui/`): `src/app/test/page.tsx` is a real `/test` route and stays
  gated. A commit that touches a component AND its test is still gated by the component. (A
  stories-only commit is not gated; the CI story screenshots are its proof.)
- `.tsx` / `.jsx` (anywhere) and `.js` / `.mjs` / `.cjs` (only under the directories below):
  visible only if the **staged, HEAD or worktree content contains JSX markup** (the worktree copy of a STAGED file is included because `git commit -a`/`<pathspec>` commits it; a file that is not staged at all is never listed, as before) (so
  removing the last element of a component still counts). The check is a heuristic that errs
  toward "visible": generics like `useQuery<Foo>` and comparisons are told apart from tags by the
  token before the `<`, anything ambiguous counts as markup. A `.tsx` React Query hook with no
  markup renders nothing and passes. Content git cannot read back counts as visible. All
  contents are read with one `git cat-file --batch`.
- other extensions matching `\.(vue|svelte|css|scss|less|html|svg|png|jpg|jpeg|gif|webp)$`;
- **everything** under `public/` or `assets/` (served verbatim: copy in a `.json`, manifests),
  even test-named files; a style-in-code file (`*.css.ts`, `*.styled.ts`, `*.styles.ts`) and
  locale/i18n message JSON (`locales/ i18n/ messages/ translations/`);
- a `.md` under `pages/` (a rendered route);
- a path under `components/`, `ui/`, `pages/`, `app/`, `views/` **and** an extension that can
  render (`astro`, `mdx`, `htm`, `ico`, `avif`, `bmp`, `tif(f)`, `woff(2)`, `ttf`, `otf`, `eot`,
  `mp4`, `webm`). A plain `.ts`/`.js`/`.json` under `src/app/` is not visible.

**Known boundary (not detected, by design of the #973 ask that plain `.ts` must not block):** code
that renders without JSX markup: Angular inline templates and Lit `html\`` in `.ts`,
`React.createElement`/`h()` calls, and a route that only re-exports another component. Commit
those with a real capture; the gate cannot see them from the path or from JSX syntax. Renames are
listed as delete + add (`--no-renames`), so turning a component into a markup-free hook still
counts via its HEAD content.

If **no** user-visible file is staged → **allow** (nothing to prove). If git can't be queried
(not a repo, timeout, error), the lister returns nothing and the gate **fails open**.

## Order of operations: the marker and the commit are separate calls

This is a `pre-bash` hook: it judges the command line BEFORE any part of it runs. So
`visual_proof_gate.py --write-marker && git commit ...` in ONE bash call is blocked: when the
hook inspects it no marker exists yet, and the `--write-marker` inside the same command never got
to run. Write the marker (or `dev shot`) in one call, then commit in the next. This is inherent
to a pre-execution hook (and the reason the gate cannot be satisfied by a self-attesting
one-liner); it is pinned by `test_marker_written_in_the_same_command_does_not_satisfy_the_gate`.

## The marker contract (how it knows a screenshot was looked at)

**agent-tools#475**: the marker used to be a bare `touch` under `~/.cache/agent-tools/visual-proof/`
— ANY fresh file there, of ANY content, satisfied the gate for EVERY repo on the machine. An
agent screenshotting repo A silently satisfied repo B's unrelated commit for up to an hour, and
a junk file dropped there by anything else worked just as well as a real screenshot. Fixed:
markers are now checked for the repo they claim to be about and (for the primary path) the
exact staged diff AND capture file they claim to cover.

**PRIMARY** — `dev shot '<url>' --out /tmp/shot.png` (dev-cli; requires a `dev-cli` build that
has the `shot` subcommand — if your installed `dev` predates it, use the FALLBACK below). It
captures, measures the capture for blankness, and — only on a passing verdict — writes a JSON
attestation (`attest-<ts>-<digest>.json`) recording the repo's resolved git toplevel, the
sha256 of `git diff --cached` at capture time, whether the worktree was dirty at capture time,
and the sha256 of the capture file itself. This gate re-resolves the repo and staged-diff hash
at commit time, requires `worktree_dirty` to have been recorded as `false`, and re-hashes the
capture file on disk right now to confirm it still matches — so passing needs a real capture
file, not just the two `git`-derived facts a forger could type by hand. A record failing any
one of these does not satisfy the gate.
>
> **`--out` MUST point OUTSIDE the repo** (e.g. `/tmp/shot.png`), not `shot.png` inside it. A
> capture written INSIDE the repo becomes an untracked file the instant it's created — `git
> status` sees it, `worktree_dirty` is recorded `true`, and the gate then rejects the very
> attestation `dev shot` just wrote (it requires `worktree_dirty: false`). This is not a bug in
> either tool individually; it's what "the capture output is itself worktree state" plus "we
> require a provably clean worktree at capture time" adds up to. Writing outside the repo
> avoids the conflict entirely.
>
> Known, accepted residual gap: `staged_sha256` and `worktree_dirty` are both computed by
> dev-cli AFTER the capture, not atomically with it — a concurrent edit to the same file
> between "browser renders" and "dev-cli hashes the index" can, in principle, let a screenshot
> of version A get attested as covering version B's staged diff with a clean worktree. Closing
> that needs dev-cli to snapshot/verify the binding immediately around the capture itself; this
> gate only validates what dev-cli hands it, and can't detect a race it never observes. Not
> fixed here — it's the producer's concern, not the consumer's, and is a narrower, harder-to-hit
> gap than the machine-global/content-blind hole this fix closes.

**FALLBACK** (no URL to shoot — docs-only visual change, a generated image, a schematic per
`visual-proof-cycle`) — after actually reviewing a capture some other way:

```bash
python3 agent-hooks/visual-proof-gate/visual_proof_gate.py --write-marker
```

writes a marker whose content is the resolved repo toplevel — repo-scoped and content-checked,
but NOT bound to the staged diff (there is no attestation to draw that hash from), so it is
strictly weaker than the primary path. A bare `touch` no longer satisfies the gate at all.

Both marker kinds are FRESH-windowed (`VISUAL_PROOF_WINDOW_S`, default `3600`s). Configure the
dir with `VISUAL_PROOF_DIR`.

> The env-configured marker dir is read at import time; CC re-invokes the script per call, so
> each call picks up the current env — this is fine, not a footgun.

## Block, not warn (doctrine)

Doctrine says "block a commit that changes UI with no attached screenshot", so this is a
straight block on the first occurrence — but it is **satisfiable** (touch the marker after you
review the capture).

## Not subagent-exempt

A subagent committing UI work must also have looked at the result, so this gate does **not**
exempt subagents (unlike the orchestration gates 1–3).

## No self-service bypass — request a Telegram approval instead

There is **no** env var or inline sentinel an agent can set on its own command to skip this
gate (a self-grant is security theater). If you have a genuine reason to commit UI work with
no fresh screenshot marker, **ASK the human**, or request a **one-time Telegram approval**:

```bash
RIG_HATCH_REQUEST_VISUAL_PROOF_GATE="deleting a dead component, nothing to render" git commit -m x
```

This is a **pre-bash** hook, so the inline prefix is honored: the hook parses the leading
`RIG_HATCH_REQUEST_VISUAL_PROOF_GATE=…` assignment out of the command string the event carries
(a pre-bash hook runs in its own process *before* the shell evaluates the `VAR=x cmd` prefix, so
the value never reaches its `os.environ`). Exporting the var into the harness environment works
too and takes precedence over an inline value.

The request routes to the human over Telegram (`tg-ctl ask`) and the commit is allowed **only**
on their approval. It is **deny-by-default**: a blank value or a bare `1`/`true` (no real
justification) is rejected without even sending the message, and any nonzero/timeout/error
verdict denies. This replaces the old `ALLOW_NO_VISUAL_PROOF` / `# visual-proof-ok:` self-service
hatch (removed — an agent setting its own bypass is not a control).

## Fail-open, on purpose

`on_error: "open"`. Process discipline, not a security boundary. The `git diff --cached`
subprocess is timeout-bounded (5s) and fails open — a broken stat must never wedge committing.

## Test

Capture the hook's exit on its OWN line right after the pipe (so it's the hook's exit, not
`echo`'s):

```bash
chmod +x visual_proof_gate.py
# in a repo with a staged .tsx and no fresh marker:
echo '{"cwd":"'"$PWD"'","args":{"command":"git commit -m x"}}' | ./visual_proof_gate.py
rc=$?; echo "exit=$rc"   # → exit=10 (block)

# write a correctly-scoped marker after looking at a screenshot → allow (fallback path; a bare
# `touch` no longer works — see "The marker contract" above). No argument needed when run FROM
# the repo (defaults to the current directory) — deliberately not `--write-marker "$PWD"`: a
# bare `$VAR` trips Claude Code's worktree-isolation Bash guard (anthropics/claude-code#88776,
# see PR #433's investigation), so the block message and this recipe both avoid it.
./visual_proof_gate.py --write-marker
echo '{"cwd":"'"$PWD"'","args":{"command":"git commit -m x"}}' | ./visual_proof_gate.py
rc=$?; echo "exit=$rc"   # → exit=0 (marker fresh and repo-scoped → allow)
```
