# format-on-write

**Point:** `post-write` · **Fail policy:** `open` · **Priority:** 60 (runs late) · **Never blocks**

After the agent writes or edits a file, this hook runs the **project's configured
formatter** on *just that one file*, in place. Formatting becomes a **harness** concern —
the agent's file writes come out formatted automatically — instead of a per-project /
per-extension manual step or a commit-time cleanup.

It is the generalization of "an `oxfmt --write` step in `lefthook.yml`" (or a
`package.json` `lint:fix`): the same idea, but lifted out of any one repo and made to run
on every write the agent does, in any project, for any of the supported languages.

## Why `post-write` (not `pre-write`)

A formatter rewrites a file **in place**, so the file has to exist first. `pre-write` only
sees the *proposed* bytes (and is for **blocking** a bad write before it lands). Formatting
is the opposite shape: let the write land, then clean it up. That needs a point that fires
*after* the file is on disk — `post-write`, added to the contract for exactly this class of
"react to a completed write" hooks. See `../README.md` → "Hook points used here".

## Detection table

Resolves the formatter by **(repo config, file extension)**, walking up from the written
file to the **git** repo root (the `.git` dir). The **first formatter the repo configures**
for that extension is the one used — availability alone never selects a tool. Once chosen,
a repo-**local** bin (`node_modules/.bin/<tool>`) is preferred over a global one on PATH
(same resolution `lefthook.yml` / npm scripts use). If the configured formatter is not
installed the hook is a **no-op** — it never falls through to another formatter. The table
is **fixed** (not project-configurable); an unsupported tool falls through to a no-op.

Why config, not availability: a repo that formats JS/TS with biome but keeps prettier for
Markdown (biome.json + a `.prettierrc` + a prettier devDependency) has
`node_modules/.bin/prettier` installed. Picking "the first installed tool" ran prettier on
every `.ts` write there, and its `trailingComma: "es5"` stripped the trailing commas biome's
default `"all"` requires — the repo's own lint then failed in CI (agent-tools#901).

> Repo config is read at the `.git` root, so JS formatters only fire **inside a git
> worktree**. Outside one (no `.git` up the tree) only the language-canonical formatters
> (`gofmt`, `rustfmt`, `black`) can apply.

"Configured" means, per tool:

- **biome** — a `biome.json`, `biome.jsonc`, `.biome.json` or `.biome.jsonc` at the root.
  For the JS/TS family, JSON and CSS; HTML only when the config opts in
  (`html.formatter.enabled: true`), as biome itself does; Markdown/YAML go on to
  oxfmt/prettier. A lint-only biome — the root config
  plainly turns the formatter off for the file's language (`javascript` / `json` / `css`
  `.formatter.enabled`, else the global `formatter.enabled`, is `false`) — leaves those
  files to oxfmt/prettier. When the effective setting needs biome's own resolution (a
  nested biome config between the file and the root, an `extends` chain, a `"root": false`
  config, or `overrides` that touch formatting), biome keeps the file: at worst it formats
  nothing, but a second formatter never takes over a file biome may own.
- **oxfmt** — a `.oxfmtrc.json(c)` or `oxfmt.config.(m)ts` at the root, or `oxfmt` as a
  direct `dependencies` / `devDependencies` entry in `package.json`.
- **prettier** — a `.prettierrc*` / `prettier.config.*` file at the root, a top-level
  `"prettier"` key in `package.json`, or `prettier` as a direct (dev)dependency. A transitive
  install that merely drops a bin into `node_modules/.bin`, or a mention elsewhere in
  `package.json` (`eslint-config-prettier`, a description), does not count.
- **ruff** — a `ruff.toml` / `.ruff.toml`, a `[tool.ruff*]` section in `pyproject.toml`, or a
  repo-local `node_modules/.bin/ruff`; a merely global ruff never takes over a Black repo.
- **black / gofmt / rustfmt** — the language's canonical formatter: used whenever installed.

| Extension | Formatters (first configured wins) | Invocation |
| --- | --- | --- |
| `.js .jsx .mjs .cjs .ts .tsx .mts .cts .json .jsonc .css .html` | **biome** → **oxfmt** → **prettier** | `biome format --write <f>` / `oxfmt --write --no-error-on-unmatched-pattern <f>` / `prettier --write <f>` |
| `.md .mdx .yaml .yml` | **oxfmt** → **prettier** | as above |
| `.py .pyi` | **ruff** → **black** | `ruff format <f>` / `black -q <f>` |
| `.go` | **gofmt** | `gofmt -w <f>` |
| `.rs` | **rustfmt** | `rustfmt <f>` |

If the extension isn't in the table, no formatter is configured, or the configured one is
not installed → **no-op, allow**. The hook never reformats the whole tree (it always passes
the single written file), and never installs a formatter.

## Escape hatch

```bash
NO_FORMAT_HOOK=1   # the hook no-ops immediately (allow), before touching the file
```

Use it when you want to inspect the agent's raw output, are mid-bisect, or a formatter is
misbehaving.

## Never blocks (fail-open, by design)

`on_error: "open"`, and the script has **no `block` path at all**. Every failure mode — no
formatter, tool missing on PATH, formatter non-zero (e.g. a half-written file with a syntax
error), timeout, unparsable event — resolves to `allow`. Formatting is a convenience; it
must never wedge the agent. The `pre-commit` git-hook format step remains the backstop that
*gates* on formatting.

Two timeouts bound a run: the script kills a formatter after **8s** (`RUN_TIMEOUT_S`), and
the descriptor's host-level `timeout_ms` (**10s**) is the hard ceiling — the inner bound is
deliberately shorter so a wedged formatter is caught by the script (→ allow) before the
host's `on_error` fires.

## Install

```bash
chmod +x format_on_write.py
# Set the descriptor's "cmd" to this file's absolute path, then drop the descriptor into
# your harness's post-write hook directory (map "post-write" to the harness's
# "file was written/edited" event — e.g. a PostToolUse matcher on the Write/Edit tools).
```

`rig apply` does all three (and rewrites the `cmd` placeholder to the absolute path).

## Test

Automated (hermetic — no network, no real formatters; a fake local bin proves the
local-bin preference and end-to-end run):

```bash
uv run --with pytest python -m pytest tests/test_format_on_write.py -q   # from repo root
```

Manual smoke (needs a real formatter on PATH/repo for case 1):

```bash
chmod +x format_on_write.py

# 1) formats a deliberately mis-formatted file (needs a formatter for that ext on PATH/repo)
tmp="$(mktemp -d)/messy.py"; printf 'x=1\ndef  f( a,b ):\n  return  a+b\n' > "$tmp"
echo "{\"args\":{\"path\":\"$tmp\"}}" | ./format_on_write.py 2>&1; echo "exit=$?"
cat "$tmp"   # → reformatted (if ruff/black is installed)

# 2) no-op when no formatter maps to the extension
tmp2="$(mktemp -d)/notes.txt"; echo "hi" > "$tmp2"
echo "{\"args\":{\"path\":\"$tmp2\"}}" | ./format_on_write.py 2>&1; echo "exit=$?"
# → "no formatter mapping for '.txt' — allow (no-op)"  decision allow  exit=0

# 3) escape hatch
NO_FORMAT_HOOK=1 sh -c "echo '{\"args\":{\"path\":\"$tmp\"}}' | ./format_on_write.py" 2>&1
# → "NO_FORMAT_HOOK=1 — skipping (allow)"
```

Every case prints `{"hook_api":"agents-hooks/v1","decision":"allow"}` and exits `0`.
