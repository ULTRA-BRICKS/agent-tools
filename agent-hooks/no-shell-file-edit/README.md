# no-shell-file-edit

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 36

Enforces the hyperide rule (`~/work/hyperide/docs/rules/development.md`): **"edits only via
Edit/Write; no `sed`/`perl`/`awk` for editing files."** A shell in-place edit or a redirect
that overwrites a tracked source file bypasses the Edit/Write tools — no reviewable diff, no
formatter run, no file-state tracking. This gate **hard-blocks** that case and points the
agent at Edit/Write.

## Blocked (each decided from a PARSED command — never a raw substring)

- an **in-place stream editor on a tracked source file**: `sed -i …`, `perl -i …`,
  `gawk -i inplace …` (also `--in-place`, GNU's `-i.bak`, perl's `-pi` / `-i.orig` clusters, and
  `-i` clustered after other flags: `sed -Ei` / `-ri` / `-ni`)
- a **`> file` / `>> file` redirect onto a tracked source file** — including the operator glued to
  a word (`… app.ts>app.ts`), bash's `>|` and zsh's `>!` force-clobber, and `>& file`:
  `awk '{…}' app.ts > app.ts`, `sed 's/a/b/' src/x.py > src/x.py`, `grep -v x f.js > f.js`
- a **`tee FILE` / `tee -a FILE` / `dd of=FILE`** writing a tracked source file — the common
  pipe-to-a-file workaround once `>` / `sed -i` are blocked
- the same wrapped in **`bash -c '…'` / `sh -c '…'`** — the inner script is re-parsed and scanned
  (nested `bash -c "bash -c '…'"` too, up to a small depth cap), and the **outer** redirect of a
  `bash -c '…' > app.ts` is checked as well

"Tracked source file" = an **existing git-tracked** file with a **source extension**
(`.ts/.tsx/.js/.py/.go/.rs/.css/.html/.yaml/.sql/…`). Generating a *new* file, or overwriting a
non-source file (`.log`, `.md`, a data dump), is **not** editing source — see Allowed.

Leading **no-op wrappers** are peeled before scanning, so a wrapped edit is still caught:
`timeout 5 sed -i … f`, `env X=1 perl -i … f`, `nice -n10 gawk -i inplace … f`.

**`gawk -i` is in-place only with the `inplace` extension.** gawk's `-i NAME` loads a library;
`gawk -i inplace …` edits in place (blocked), but `gawk -i json …` / `-i readfile …` just loads
that extension and is read-only (allowed).

## Allowed (the rule is about EDITING, not reading or generating)

- **read-only filters** — no `-i`, no redirect to a tracked source file:
  `sed -n '1,5p' f`, `awk '{print $1}' f | sort`, `grep -v x f`
- a **redirect to /tmp, a brand-new file, or a non-source file**:
  `… > /tmp/x`, `… > out.log`, `… > notes.md`, `… > new_file.ts` (not tracked yet)
- `sed -i` / `perl -i` targeting an **untracked / non-source path** (scratch, build artifact)

The scratch exemption is **git-trackedness**, not a `/tmp` string prefix: a real codebase can
live under `/tmp` (CI runners, agent worktrees), so a tracked source file there still blocks; an
untracked `/tmp` scratch file is exempt because `git ls-files` doesn't know it, wherever it sits.

A **leading `VAR=val` assignment prefix** (`LANG=C` / `LC_ALL=C` / `FOO=bar …`) is peeled before
matching, so `LANG=C sed -i … app.ts` is still caught — it is both the most common legitimate
prefix and the simplest bypass otherwise (the assignment would leave an unrecognized command head).

## Known scope boundary (other write idioms)

This gate covers the shell editors the hyperide rule names (`sed`/`perl`/`awk` in-place), the
redirect family, the `tee`/`dd` pipe-to-a-file workaround, **command substitutions**
(`echo $(sed -i … app.ts)` is recursed into like `bash -c '…'`), and **shell globs + brace
expansion** — a glob operand (`sed -i … *.ts`, `src/app.*`) is expanded against the cwd, and a
brace group (`{app,}.ts`, `src/{x,y}.py`) is expanded into its candidate paths first (Python's
glob doesn't do braces); either blocks if any resulting file is a tracked source (the canonical
bulk-edit idioms). **No-op prefixes** `command` / `exec` /
`builtin` are peeled like `timeout`/`env`. A **heredoc body** is treated as data, not commands, so
a `sed -i` *inside* a heredoc (generating a script/doc) is not falsely blocked. It does **not**
police every conceivable way to overwrite a file from the shell:

- `cp /tmp/new app.ts`, `mv … app.ts`, `install … app.ts`, `patch app.ts < x.diff`, `ed -s app.ts`,
  `python -c "open('app.ts','w')…"` / `node -e "…writeFileSync('app.ts'…)"` — a smaller risk (an
  agent reaching for `sed -i` rarely reaches for `cp` to edit), and telling a legit rename from an
  overwrite is its own task.
- **`find … -exec sed -i {} +`** and **`… | xargs sed -i`** — the bulk-edit idioms whose file
  operands are `{}` (a find placeholder) or arrive on stdin, so they are *not present in the
  command* and tracked-ness can't be resolved without running it. Recognizing the bare `sed -i`
  there would block legit `find … -exec sed -i … /tmp/{}` too.
- a **variable operand** (`sed -i … "$FILE"`) — the value isn't known statically, so it can't be
  resolved; **process substitution** `… <(sed -i … app.ts)` / `>(…)`; and **`eval '…'`** are not
  recursed into (exotic). Use Edit/Write (or the Agent tool for a real multi-file codemod).
- **`sudo`** is not peeled, so `sudo sed -i … app.ts` / `… | sudo tee app.ts` pass — editing a
  tracked source as root is rare in an agent loop, and `sudo`'s own option grammar (`-u user`, `-E`,
  `--`) is its own parse. Like find/xargs, it's named here rather than chased; the rule still applies.
- an **awk-internal redirect** (`awk '{print > "app.ts"}' in.txt`) — the `>` lives *inside* awk's
  single-quoted program, which the shell never sees as a redirect and the gate's quote-aware
  redirect splitter deliberately doesn't reach into. Recognizing it would mean parsing each editor's
  own language; exotic, named here rather than chased.
- an in-place editor's **option-argument** is not distinguished from the edit target: every
  non-flag operand of a blocked-editor invocation is checked for tracked-ness, so the contrived
  `sed -i -f lib.py /tmp/scratch` (where `lib.py` is sed's `-f` *script file*, not the target)
  would flag `lib.py`. Modeling each editor's arg-taking options well is its own task; in practice
  the script-file is rarely a tracked source you'd be surprised to see flagged.
- an edit wrapped in **≥5 levels of `bash -c '…'`** is not reached: shell-runner recursion is
  capped at `_MAX_SHELL_DEPTH` (4) so a forged deep nest can't spin into a `RecursionError` /
  fail-open. A real edit is rarely more than 1–2 `bash -c` deep, so the cap costs nothing in
  practice; a deliberately deep nest is an exotic bypass that, like find/xargs, this gate does not
  chase (the same `on_error: open` edit-hygiene posture).

The rule's spirit — *edit via Edit/Write* — still applies to all of these; this gate just isn't the
enforcer for them. `on_error: open` reinforces that this is edit-hygiene, not a security boundary.

## Not a raw-string match (why the bypasses are closed)

The #59 siblings were patched after codex found **raw-string** bypasses (a flag spotted inside a
string or comment). This gate avoids the whole class: the command is **split on real shell
separators** (quote-aware), each segment is **shlex-tokenized**, a trailing **shell comment is
dropped**, and the in-place flag / redirect target are read **off the argv** — not grepped from
the line. So none of these trip it:

```bash
echo "use sed -i to patch"          # the words live inside a string operand
ls -la  # remember: sed -i later     # a comment tail, dropped before parsing
git commit -m 'switch off sed -i'    # the flag is inside the commit MESSAGE
sed 's/-i//' file.ts                  # `-i` is part of the s/// operand, not a flag
```

The in-place flag counts only when it is an actual argv token; the redirect target only blocks
when `git ls-files --error-unmatch` confirms it is tracked **and** the extension is source.

## Not subagent-exempt

Unlike the delegation gates (which govern the orchestrator only), this rule is about **how any
agent edits a file** — a subagent hand-editing with `sed -i` is exactly what it stops. There is
no `agent_id` carve-out here.

## Why a hard block (not warn-first)

A shell file-edit is unambiguous: it edits a tracked source file outside Edit/Write, every time.
There is no borderline case to warn about — so this blocks on the first occurrence,
deny-by-default, with an external Telegram approval for the rare deliberate exception.

## No self-service bypass — external Telegram approval only

There is **no** env-var or inline escape hatch any more. The old `ALLOW_SHELL_FILE_EDIT=1` +
`ALLOW_SHELL_FILE_EDIT_REASON` env and the `# shell-file-edit-ok:` inline sentinel let the very
agent this gate constrains grant itself an exception — security theater, not a permission gate.
Both were removed.

The block is now **deny-by-default**. For a genuine exception (a vetted bulk codemod, a
generated file), ASK the human, or request a one-time Telegram approval with a written
justification:

```bash
RIG_HATCH_REQUEST_NO_SHELL_FILE_EDIT="bulk codemod across 200 files, vetted" \
  sed -i 's/v1/v2/' config.yaml
```

This is a **pre-bash** hook, so the inline prefix is honored: the hook parses the leading
`RIG_HATCH_REQUEST_NO_SHELL_FILE_EDIT=…` assignment out of the command string the event carries
(a pre-bash hook runs in its own process *before* the shell evaluates the `VAR=x cmd` prefix, so
the value never reaches its `os.environ`). Exporting the var into the harness environment works
too and takes precedence over an inline value.

If the env var is unset, no Telegram call is made and the command simply blocks. If it is
present but blank, whitespace-only, or a bare flag value (`1`/`true`/`yes`/`on`), the hook does
not contact Telegram and denies — a bare `1` is not a justification. A real justification runs
`tg-ctl ask` through a trusted absolute path (never ambient `PATH`) with the question as a JSON
ButtonRequest on stdin; only a reply on stdout whose decision is explicitly `allow` allows — an
empty or unparseable reply, an explicit deny, any nonzero exit, launch error, or timeout denies
(`tg-ctl ask` exits 0 regardless of outcome, so a clean exit alone is never approval). An agent
can *request*, not self-grant.

## Fail-open, on purpose

`on_error: "open"`. Edit-hygiene discipline, not a security boundary — a crash (a command shlex
can't tokenize, a `git` call that errors) must never wedge the ability to run a command. The
`git ls-files` lookup is timeout-bounded and fails toward **allow** on the redirect case
(unknown tracked-ness → not provably an edit of a tracked source file).

## Test

Capture the hook's exit on its OWN line right after the pipe (so it's the hook's exit, not
`echo`'s). Run from inside a git repo where `app.ts` is a tracked file:

```bash
chmod +x no_shell_file_edit.py
CWD=$(pwd)
echo "{\"args\":{\"command\":\"sed -i 's/a/b/' app.ts\"},\"cwd\":\"$CWD\"}" | ./no_shell_file_edit.py; echo " exit=$?"   # → exit=10 (block)
echo "{\"args\":{\"command\":\"awk '{print}' app.ts > app.ts\"},\"cwd\":\"$CWD\"}" | ./no_shell_file_edit.py; echo " exit=$?"  # → exit=10 (block)
echo "{\"args\":{\"command\":\"sed -n '1,5p' app.ts\"},\"cwd\":\"$CWD\"}" | ./no_shell_file_edit.py; echo " exit=$?"   # → exit=0 (read-only filter)
echo "{\"args\":{\"command\":\"awk '{print}' app.ts > /tmp/out.ts\"},\"cwd\":\"$CWD\"}" | ./no_shell_file_edit.py; echo " exit=$?"  # → exit=0 (/tmp)
echo "{\"args\":{\"command\":\"echo \\\"use sed -i\\\"\"},\"cwd\":\"$CWD\"}" | ./no_shell_file_edit.py; echo " exit=$?"  # → exit=0 (string, not a flag)
# deny-by-default: a bare RIG_HATCH_REQUEST value (no written justification) still blocks
RIG_HATCH_REQUEST_NO_SHELL_FILE_EDIT=1 \
  bash -c "echo '{\"args\":{\"command\":\"sed -i s/a/b/ app.ts\"},\"cwd\":\"$CWD\"}' | ./no_shell_file_edit.py"; echo " exit=$?"  # → exit=10 (bare 1 rejected)
```
