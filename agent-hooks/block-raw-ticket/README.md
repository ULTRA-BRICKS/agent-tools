# block-raw-ticket

**Point:** `pre-bash` · **Fail policy:** `closed` · **Priority:** 10 (runs early)

When **task-cli is configured** (`task` on PATH — activated, and zero-config github-issues
counts as configured), this hook blocks a raw issue-tracker write that skips task-cli's
gates:

- `gh issue create` / `gh issue new` (and `close` / `edit` / `comment` / `delete` / …)
- `gh -R owner/repo issue create …`
- `linear issue create` / `update` / `delete` / …

Lets the sanctioned path through:

- `task new` / `task create` / `task change` / `task done` / `task list` / `task read`
- read-only `gh issue list` / `gh issue view`
- `gh pr …` (PRs are not tickets; ship still gates merges)

Inert when `task` is **not** on PATH (task-cli is not activated).

## Why an agent-hook (not a skill, not AGENTS.md)

"Use `task`, never raw `gh issue` / `linear`" was advice in the task skill. Advice cannot
stop an autonomous agent: omp was filing Linear issues directly, skipping acceptance
criteria, motivation, user-impact, and screenshots. A git-hook cannot help — the write
happens against GitHub/Linear, not at commit time. The only place to stop it is
**before the command runs**.

`require-ticket-before-commit` only looks at `git commit` messages; its own tests
document that `gh issue create` is allowed. This hook is the create-time counterpart.

## Fail-closed

`on_error: "closed"`. If the hook cannot inspect the command, it blocks rather than
allowing a tracker write through a broken guard.

Known residual (same class as other small pre-bash gates): a write hidden in
`bash -c 'gh issue create …'` is not peeled. The model-issued tool call is the threat
model, not a sandbox.

## Test

```bash
echo '{"args":{"command":"gh issue new --title x"}}' | ./block_raw_ticket.py; echo "exit=$?"
# task on PATH → {"decision":"block",…} exit=10
# task absent  → {"decision":"allow"} exit=0
```
