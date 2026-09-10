# Changelog

## Unreleased

### Fixed

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
