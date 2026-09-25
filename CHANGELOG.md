# Changelog

## Unreleased

### Added

- `block-raw-ticket`: Forgejo/Gitea and raw-API coverage (UB-25). With task-cli armed, the
  guard now also blocks `fj issue` / `fj repo labels` writes, `tea issues` / `tea comment` /
  `tea label` writes, REST writes to `repos/<o>/<r>/(issues|labels)` via curl, wget, HTTPie
  or `gh api` on any host (github.com included), `gh api graphql` issue/label mutations, and
  inline Python/Node programs making those writes — through `bash -c`, `eval`, heredocs,
  `sudo`/`doas`/`xargs` and `uv run`-style runners. Explicit GET/HEAD stays a read; an
  unresolvable method counts as a write. New deny-by-default
  `RIG_HATCH_REQUEST_BLOCK_RAW_TICKET` hatch (one ask per command, `timeout_ms` 960000);
  unparseable writes and junk-title creates are never hatchable. Pre-tool text guard, not a
  firewall — residuals are listed in the hook README.
- `agenttools_hatch_escalation`: display-only `redact_for_display` masks credential values in
  the Telegram approval preview and the `overrides.log` audit line (redacted before
  truncation). Approval identity stays bound to the original command and cwd.

- `ci/ship`: Forgejo provider (UB-25). `gh ship` now ships a PR from a checkout whose `origin` is
  a Forgejo host instead of silently reading the wrong remote. A gh-shaped adapter
  (`ci/ship/forgejo_provider.py`) feeds the unchanged gates from the Forgejo API, pins the PR head
  and merges with `head_commit_id`, binds credentials to the origin host, and refuses every call
  it cannot back with Forgejo evidence. See `ci/ship/README.md` "Forgejo provider".
- `ci/ship`: `--task-config <path>` routes the ship's ticket through task-cli's standard
  `--config` for the acceptance gate and the post-merge notify (UB-25: a Tasks-app ticket shipped
  from a Forgejo repository). A missing file, and any acceptance result that cannot be evaluated,
  refuses the merge.

### Fixed

- `cc-hook-bridge`: one hard wall-clock budget (1100s) for all agent-hooks run on a single
  Claude Code tool call. Two hatch approval waits (960s each) in one pre-bash call used to
  exceed Claude Code's outer 1200s hook timeout, and a killed command hook fails OPEN. Each
  hook's timeout is now clamped to the remaining budget (fail-closed when clamped); once the
  budget is spent the remaining hooks are skipped and the call is blocked, overriding any
  earlier grant.
- `omp-hook-bridge`: round-3 hardening of the `eval` tool's write-call secret-scan heuristic
  (agent-tools#614). Four further security-review findings closed:
  - A JS `\u{HEX}` Unicode code-point escape that widens to a literal backslash (e.g. `\u{5c}`)
    no longer gets re-interpreted as the start of a brand-new escape sequence by a subsequent
    decode pass — decoding is now single-pass, one escape token at a time, so `\u{5c}n` decodes
    to the two literal characters backslash-then-`n`, never an actual newline.
  - A raw-string path literal (`Path(r"...")`) is no longer escape-decoded before being compared
    against `block-secrets-write`'s path allowlist — Python's raw-string semantics mean no
    escape in it is ever interpreted at runtime, so decoding it could make a suffix-anchored
    allowlist pattern (e.g. `.example$`) match text the real write never targets.
  - `write_text`/`write_bytes`'s `data=` keyword argument is now recognized regardless of its
    position among other keyword arguments, matching pathlib's real by-name keyword resolution
    (`write_text(encoding="utf-8", data=...)` is now recognized, not just `data=` as the first
    token).
  - `Bun.write`/`fs.writeFile*`/`fs.appendFile*` calls whose first (path) argument can't be
    confidently resolved to a literal now fall through to the path-less content-scraping bucket,
    the same way `Path(...)`/`open(...)` already did, instead of silently dropping the write's
    content from every scan.
- `omp-hook-bridge`: closed a total blind spot in the `eval` tool's write-call secret-scan
  heuristic (agent-tools#614) — a bare, receiver-less call to the Eval tool's own documented
  global `write(path, content)` builtin matched none of the existing patterns (all of which
  require a `Path(`/`open(`/`Bun.`/`fs.`-prefixed or dotted `.write(...)` receiver call), so
  its content never reached `block-secrets-write` at all. A new `_EVAL_BUILTIN_WRITE` pattern
  recognizes the bare builtin call while excluding any dotted receiver (`f.write(...)`, already
  handled separately) and decoy identifiers like `mywrite(...)`.
- `omp-hook-bridge`: round-5 hardening of the `eval` tool's write-call secret-scan heuristic
  (agent-tools#614). Two further security-review findings closed:
  - The bare Eval-tool builtin `write(path, content)` pattern (`_EVAL_BUILTIN_WRITE`) required
    BOTH arguments to be a recognized string literal, unlike `Path(...)`/`open(...)`, which
    already fall through to a path-less content-scraping bucket whenever the path argument
    isn't a confidently-resolved literal — a computed/unresolvable first argument
    (`write(some_computed_path_var, "API_KEY=...")`) silently dropped the write's content from
    every scan instead. A new `_EVAL_BUILTIN_WRITE_FALLBACK` pattern (mirroring round 3's
    `Bun.write`/`fs.writeFile*` fallbacks) closes it.
  - `_EVAL_OPEN_WRITE`'s permissive `,[^)]*` mode/tail (round 2) could also swallow a NESTED
    call expression sitting in that tail position — `open("foo.example",
    open("src/secret.py","w").write("API_KEY=..."))` let the tail consume up to the inner
    `open(...)` call's own closing paren, so the match's `.write(` anchor landed on the nested
    call instead, attributing the real secret to the outer call's allowlist-friendly decoy path
    `foo.example` and never scanning it under its real path (or path-lessly) at all — a genuine
    `ALLOW_PATH` allowlist BYPASS, not merely an under-scan. The tail (shared with the
    `.writelines(...)` sibling pattern, which had the identical exposure) now rejects any
    position that begins an `identifier(...)`-shaped call, falling through to no-match instead
    — `open(...)`'s existing whole-string scan then finds and correctly attributes the nested
    call on its own.

- `omp-hook-bridge`: round-6 hardening of the `eval` tool's write-call secret-scan heuristic
  (agent-tools#614). Four further security-review findings closed:
  - `_EVAL_KWARG_VALUE` (what `_EVAL_OTHER_KWARG` recognizes as another keyword argument's value
    sitting before `data=`) only matched a quoted literal, a bare identifier, or a number — a
    CALL EXPRESSION there (`write_text(encoding=locale.getpreferredencoding(),
    data="API_KEY=...")`) matched none of its alternatives, so the whole call went entirely
    unrecognized (content silently dropped), contradicting round 3's "`data=` recognized
    regardless of position" fix. A new alternative recognizes a call expression with up to one
    level of nested parens.
  - `_EVAL_OPEN_WRITELINES_LIST`'s `[^\]]*` content scan stopped at the FIRST `]`, including one
    sitting INSIDE a quoted list item's own string body — `.writelines(["]", "API_KEY=..."])`
    truncated the captured content to just the opening quote of the first item, silently
    dropping the real secret in the second item. A new `_EVAL_WRITELINES_LIST_BODY` fragment
    treats each quoted chunk atomically so an embedded `]` inside quotes no longer ends the scan
    early.
  - Python's 8-digit code-point escape (`\UHHHHHHHH`, distinct from the JS `\u{HEX}` form and
    the 4-digit `\uHHHH` form) was not recognized by `_EVAL_ESCAPE_TOKEN` as its own token — it
    fell through to the generic `\.` catch-all, which consumes only the leading `\U`, leaving
    the 8 hex digits as untouched literal text (`\U00000041PI_KEY=...` scanned as literal text
    instead of decoding to `API_KEY=...`, a secret-scan bypass). A new alternative tokenizes the
    full 8-digit escape as one unit.
  - Round 5's `_EVAL_OPEN_MODE_TAIL` guard only rejected a tail beginning with the SHAPE
    `identifier(` — a decoy like `open("foo.example", [open][0]("src/secret.py","w").write(
    "API_KEY=..."))` (subscript-then-call, `[open][0](...)`) begins with `[`, not an identifier
    character, so it wasn't rejected and the secret was misattributed to the allowlist-friendly
    decoy path `foo.example` again — the identical bypass class as round 5's own finding, via a
    different call shape. The guard is widened to reject any `(` or `[` in the tail.
