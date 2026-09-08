# workflow-danger-gate

**Point:** `pre-workflow` · **Fail policy:** `closed` · **Priority:** 10

Inspects a Claude Code **Workflow** script before it runs and **blocks** if the
executable JS would do something the bash hooks already refuse:

- a raw `gh pr merge` (merges go through `gh ship`)
- `git reset --hard`
- `git push --force` / `git push -f` (`--force-with-lease` is allowed)
- `git clean -f`
- `sudo rm`
- `--no-verify`
- `screencapture`

## Why Workflow is still in `permissions.allow`

Ultracode (CC's multi-agent orchestration) needs the Workflow tool auto-approved
so it is not prompted on every spawn. That allow is **not** a blank cheque.

```yaml
permissions:
  allow:
    # Ultracode needs Workflow auto-approved so it is not prompted on every
    # spawn. This is NOT a blank cheque: workflow-danger-gate still inspects
    # the script and denies destructive commands (raw gh pr merge, hard
    # reset, force-push, sudo rm, --no-verify, screencapture). JS comments
    # and agent() prompts that mention those forms stay allowed.
    - Workflow
```

## Comments and ultracode prompts are allowed

A `//` or `/* */` comment that mentions a denied command does **not** block —
operators must be able to write comments explaining the permission split.
An `agent({ prompt: "never gh pr merge; use gh ship" })` dispatch (the
ultracode path) is also allowed: only the `prompt:` string is ignored, so a
bun `$`...`` / `exec(...)` sitting next to that prompt still blocks. The
same command inside a bun `$`...`` template or `exec(...)` **is** blocked.
Clustered short flags (`git clean -df`, `git push -uf`) and `git -C <dir>`
wrappers match the same way the bash hooks do.

## Fail-closed

`on_error: "closed"`. A missing/unreadable script, or a payload we cannot
parse, is **denied**. A Workflow we cannot inspect must not auto-run under
the harness allow.

## Carrier / two-repo split

The mapping `PreToolUse` + tool `Workflow` → `pre-workflow` lives in
`lib/cc_hook_bridge`. For the hook to fire in a live Claude Code session,
rig-cli must also register a `Workflow` PreToolUse matcher in
`hook_bridge_entries` (same split as `pre-skill` / `Skill`). Until that
matcher ships, the descriptor is catalog-complete and the unit tests prove
the gate; CC will not invoke it yet.

## Test

```bash
uv run --with pytest python -m pytest tests/test_workflow_danger_gate.py -q
```
