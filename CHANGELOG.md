# Changelog

## Unreleased

### Added

- `lib/agenttools_gpu_slots`: a machine-wide GPU-model slot pool shared by every agent and
  harness (agent-tools#817). `RIG_GPU_SLOTS` (default 2) `flock` lock files in
  `~/.local/state/agent-tools/gpu-slots`; the kernel frees a slot the instant its holder dies,
  SIGKILL included. `python3 <agent-tools>/lib/agenttools_gpu_slots run -- <cmd>` waits for a
  slot (default up to 30 min, exit 75 on timeout) and then execs the model command holding it;
  `status` lists the holders. stt-cli acquires slots with the same protocol.

- `agent-hooks/package-store-guard` (pre-bash, every harness): blocks wiping a shared package
  store (`bun pm cache rm`, deleting/moving `~/.bun/install/cache` or an ancestor, `uv cache
  clean`, `uv cache prune --all`/`--ci`, deleting `~/.cache/uv`), creating a symlink named
  `node_modules`, and `npm install`/`npm ci`/`pnpm install`/`yarn` where the nearest lockfile is
  `bun.lock`/`bun.lockb`. Each block names the replacement (`bun install --frozen-lockfile`,
  `rig worktree gc`). It also blocks installing or starting the banned container VM runtimes
  (`colima start`, `limactl start`, `brew install colima|lima|podman|orbstack|docker-desktop`,
  `nix run nixpkgs#colima`), pointing to Apple `container` / `mocker` and the native
  fake-gcs-server backend, and `lefthook install`/`husky`/`simple-git-hooks` while
  `core.hooksPath` points outside the repo (a postinstall `lefthook install` replaced the global
  pre-commit composer on 2026-09-26). Its `blurb.md` adds the SessionStart rules that disk
  cleanup never wipes package-manager caches or stores, that hook managers stay repo-local, and
  that colima-style VMs stay off the machine.

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

- `agent-hooks/heavy-op-memory-gate` now sees wired-memory exhaustion and GPU model loads
  (agent-tools#817). On 2026-09-29 thirteen concurrent `whisper-cli` runs wired 95.7% of a 24 GB
  Mac into GPU buffers and kernel-panicked it while the jetsam level, the hook's only signal,
  stayed "normal". The hook now also blocks when the wired share of RAM is above
  `RIG_HEAVY_OP_MAX_WIRED_PCT` (default 75) or free + inactive + speculative + purgeable memory
  is below `RIG_HEAVY_OP_MIN_AVAILABLE_MB` (default 512), read from `vm_stat` (macOS) or
  `/proc/meminfo` (Linux). `stt`, `whisper-cli`, `whisper-server`, `llama-cli`, `llama-server`,
  the `mlx_lm`/`mlx_whisper` entry points, and any command handed a `.bin`/`.gguf`/`.ggml`/
  `.safetensors` model file are heavy, matched per simple command (loop bodies, `bash -c`
  scripts, and `<(…)`/`$(…)`/backtick substitutions included). Such a load is refused at once
  while every GPU slot is taken, unless it is queued through the slot wrapper.

- `ci/ship`: the version-bump gate reads dynamic Python versions. A `pyproject.toml` with
  `[project] dynamic = ["version"]` and setuptools `version = { attr = "pkg.__version__" }` or
  hatch `[tool.hatch.version] path` now resolves to the module that holds `__version__`, and the
  PR-diff and base-tip reads accept `__version__ = "X"` (also in a module pinned via
  `SHIP_VERSION_FILES`). Before, the setuptools form was always refused as UNCHANGED (qa-cli
  PR #23 had to ship with `--no-version-bump-ok`), and the hatch form skipped the gate entirely,
  so its source PRs merged without a bump. A dynamic source ship cannot read (a module outside
  the flat/`src/` layouts, an attribute other than `__version__`, a computed `__version__` with
  no literal `"X"`, a setuptools `file`) now refuses with the reason and a pointer to
  `SHIP_VERSION_FILES`, instead of reading the pyproject's own `version = { attr = ... }` line,
  where renaming the attr looked like a bump (auto-detected: a root package.json that declares
  the version still counts first, for polyglot repos). The resolved file must be a regular
  file inside the checkout: a PR's `path = "../.."`, absolute path or symlink cannot make ship
  stat or read files outside it. In a module only an assignment of a string literal counts (a
  `__version__ == "X"` comparison is not a bump), the value is read from that literal (a digit
  in an annotation such as `__version__: T1 = "X"` is not the version), and a PR that moves the
  version module and bumps it is matched by the file's new path. A comment inside the
  `dynamic = [...]` array (e.g. one holding a `]`) no longer hides the dynamic version and skips
  the gate. A custom hatch `pattern`, or both a setuptools attr and a hatch path, refuses as
  unreadable. Known limit: the gate matches lines, not Python, so a `__version__ = "X"` line in
  a docstring reads like the real one (parser-based reading: agent-tools#812).
- `ci/ship`: the version-bump gate no longer exits silently (exit 1, no message) when the base
  branch tip has no version value in the version file, e.g. a PR that adds its first version
  module; the base-tip check now passes as documented.
- `git-hooks/global-dispatcher/install-local-hooks.sh`: the dedup scanner strips `VAR=value`
  prefixes with one portable substitution instead of a GNU-only `sed ':a; …; ta'` loop. BSD sed
  (macOS) warned "unused label" and stripped nothing, so an env-prefixed local scan such as
  `GITLEAKS_CONFIG=x gitleaks git --staged` read as "no local secret scan" (agent-tools#810).
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
