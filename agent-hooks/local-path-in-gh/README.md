# local-path-in-gh

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 40

Blocks a `gh` / `task` command that **writes text to GitHub** (or to a tracker through task-cli)
when that text names a **local filesystem path**. A path such as a tmp-dir file is unreadable
by everyone on GitHub and gone after a reboot, so as a proof or a reference it is fake.
([agent-tools#1002](https://git.hyperide.ai/ultrabricks/agent-tools/issues/1002).)

## The incident

An acceptance criterion on a ticket was "proven" with a local tmp-dir path rendered as an
image link. The acceptance gate (`task check --proof`, `gh ship`) only asks that a proof is
_attached_; it cannot tell that nobody but the capturing machine can open it. The owner called
this unacceptable and ordered a mechanical guard.

## What is gated

Parsed with shlex, chain-aware (`ls /tmp && gh pr comment 1 --body ok` is fine — only the text
the `gh`/`task` call itself writes is judged):

| Command                                                                                                                                                                                                                   | Text that is scanned                                                                                                                                                                                                                                                                                                                       |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `gh pr` / `gh issue` `create`, `edit`, `comment`, `review`, `close`, `reopen`, `merge`                                                                                                                                    | `--title/-t`, `--body/-b`, `--subject`, `--comment/-c` (close/reopen), and the **contents** of `--body-file/-F`                                                                                                                                                                                                                            |
| `gh release create` / `edit`                                                                                                                                                                                              | `--title/-t`, `--notes/-n`, the contents of `--notes-file/-F`                                                                                                                                                                                                                                                                              |
| `gh api` **writes** (explicit POST/PUT/PATCH/DELETE, or the implied POST of any `-f/-F/--field/--raw-field/--input`) to an `issues` / `pulls` / `comments` / `reviews` / `releases` / `discussions` endpoint or `graphql` | every `-f/-F` field value, `-F key=@file` contents, `--input` contents. **The endpoint is never scanned.** A GET, or another endpoint (`contents`, `user`, …), is ignored                                                                                                                                                                  |
| `task new` / `create` / `change` / `check` / `attach` / `done` / `comment` / `status` / `accept`                                                                                                                                                          | the positional title of `new`/`create`, `--title --what --acceptance --why --impact --if-not-done --from-message --deployed --post-merge-acceptance --skip-*`, `--force` reasons (new/create/check), comment text, `--body-file` contents, and the **references** `--proof`/`--screenshot`/`attach` files. `-C/--cwd --config --repo --backend` are not text and are never scanned |
| `tg` / `tg report` | message text, stdin bodies and `--title --impact --verify-result --evidence --summary`. `--photo`/`--file` upload a local file on purpose and are never scanned |

Body files are read (`--body-file x.md`, `-F x.md`, `-F body=@x.md`, `--input x.json`). Stdin
bodies (`--body-file -`) are read from a heredoc, `< file`, a here-string, or an immediate
`cat file |` / `echo … |` / `printf … |` producer. `--body "$(cat file)"` is judged by the file's
content. A file written **earlier in the same command** (`cat > b.md <<EOF … EOF && gh pr
comment 1 --body-file b.md`, `echo … > b.md`, `… | tee b.md`) is judged by what the command
will put in it, not by a stale copy on disk. `bash -c '…'`, `sh -c`, `eval` strings are
re-parsed (depth 3).

### What counts as a local path

Anywhere in the text, outside a URL:

- `/tmp`, `/var/tmp`, `/var/folders`, `/private/tmp`, `/dev/shm`, `/run/user`, `/root`,
  `/home/<user>`, `/Users/<user>` — a `<placeholder>`, `{placeholder}` or `${USER}` in the
  account segment is a generic example, not a path, and passes.
- `~/…`, `$HOME`, `${HOME}`, `$TMPDIR`, Windows `C:\…` / `C:/…`, `%USERPROFILE%` / `%TEMP%` /
  `%APPDATA%` …, and `file://` URLs.
- The **literal value of the current home directory at runtime** (from `$HOME`, `~`, and the
  account database). It is printed as `<home>`; any other `/home/<name>` or `/Users/<name>` in
  a printed token has its account segment replaced by `<user>`. Nothing in this hook, its
  message, its tests or its docs names a specific account.
- Markdown / HTML link and image targets (`![x](…)`, `[x](…)`, `[x]: …`, `src=`, `href=`) that
  are not `http(s):`, `#anchor`, `mailto:`, `tel:`, or a relative path. An absolute `/x` target is always flagged: on GitHub it is a
  site-root link, never repo-relative.
- For a task-cli **reference** (`--proof`, `--screenshot`, `attach` files) _any_ absolute local
  path counts, not just the well-known roots.

A token only starts a path at a boundary, so these are **not** paths: `src/foo.cpp`,
`scripts/x.sh`, `./relative/file`, `../up/x`, `docs/home/x.md`, `tests/tmp/data`,
`https://x/tmp/y`, `user@host:/p`, `3/4`, `and/or`, `~5 minutes`, `$HOMEBREW`.

### Fenced code blocks: no exemption

Deliberately simple and predictable: a path inside a fenced block is still a path that will be
posted, and the remediation is to paste the _content_ (scrubbed of paths), not a pointer to a
file. A command example should use repo-relative paths or `<path>` placeholders.

## The block message

````
local-path-in-gh BLOCKED.
TRIGGER: gh pr create --body: `/tmp/proofs/check.txt` (local link target).
WHY: a local path means nothing to anyone reading GitHub — it cannot be opened there and is gone after a reboot — so as a proof or a reference it is fake.
DO THIS: attach the content itself, never the name of a local file.
  - text / log / command output: paste an excerpt (or the whole file) as a fenced block ```text ... ``` (```json, ```diff, ```cpp ... for code) or inside <details><summary>log</summary> ... </details>; scrub paths out of what you paste (use <tmp>, <home> or a repo-relative path).
  - image / binary: upload it and link the https URL (push it to the repo's assets or pr-assets branch, `gh gist create`, `gh release upload`, or drag-drop it into the GitHub UI), then write ![name](https://...).
  - command or file references: use repo-relative paths (src/foo.cpp, scripts/x.sh) or a <path> placeholder.
EXCEPTION: deny-by-default, no self-service bypass — RIG_HATCH_REQUEST_LOCAL_PATH_IN_GH="<justification>" <command> asks for a one-time Telegram approval (a bare 1 is rejected).
````

The TRIGGER line names the command, the exact flag (`--body`, `--body-file <file> (contents)`,
`-f body=`, `heredoc`, `--proof`, …) and up to three offending tokens (`+N more`). For a task-cli
finding an extra line explains how a proof is attached (an https URL, or a working-directory
relative path to an existing image that task uploads; `--force '<reason>'` for a non-image proof).

## Bypass: the hatch only

Deny-by-default. The single bypass is a one-time Telegram approval:

```
RIG_HATCH_REQUEST_LOCAL_PATH_IN_GH="<written justification>" gh pr comment 1 --body "…"
```

A blank value, a bare `1`/`true`/`yes`, or a single command-name token is rejected with no
Telegram call. There is no config toggle, no flag, and no other environment variable. One hatch
round-trip covers a whole chained command: findings from every gated segment are gathered
first, then one approval is requested. Every attempt is audited by the shared
`agenttools_hatch_escalation` lib (`overrides.log`).

Setting the variable as a process-environment export or as the inline prefix both work; the
inline prefix is the sanctioned form (see the hatch note in the repo `AGENTS.md`).

## Install

Nothing to wire: an `agent-hooks/<name>/` directory with a `<id>.<point>.json` descriptor is
discovered by `rig apply commit` (`agent_hooks.all: true`, the default) and installed for every
harness bridge rig provisions (Claude Code, Codex, opencode, omp). `cmd` in the shipped
descriptor is the `/ABSOLUTE/PATH/TO/...` placeholder `rig` substitutes.

## Known limits (heuristic, not a sandbox)

- A command substitution that **builds** the text (`--body "$(some-script)"`) is not run or
  inspected, and a piped producer other than `cat`/`echo`/`printf` is opaque. `$(cat file)` is
  expanded.
- A body file is read when the hook runs; a writer that changes it between the hook and `gh`
  opening it (a race) is not caught. `--body-file <(cat f)` / `<(printf …)` is judged by the
  producer's output for `cat`/`echo`/`printf`; any other process substitution is opaque.
- A body file that does not exist at hook time and is not written earlier in the same command
  is skipped (gh itself fails on it).
- A path split across shell variables (`"$D/x"`) is invisible; only literal text is judged.
- `gh` flags are matched by the table in the script; an unknown future text flag is not scanned.
- A tracker write that goes through another client (raw `curl`, an SDK) is the business of
  [`block-raw-ticket`](../block-raw-ticket/README.md), not this hook.
