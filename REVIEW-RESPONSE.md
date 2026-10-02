# GL-264 — SAST runner false-green fix: review response

Linear issue: GL-264 (`9ab1ccdd-2332-43b7-bf09-7ed736322cbf`). This file is an untracked
work artifact for the parent to consume — it is **not** staged and should not be committed.

## The defect

`ci/sast/sast.sh` gated on `if semgrep …; then … exit 0; fi` and then read `rc=$?` in the
fall-through. POSIX (and both bash and dash) define the exit status of an `if` whose condition
is false and which has no `else` branch as **0**. So after semgrep failed, `rc=$?` captured the
`if` statement's status (0), not semgrep's. In block tier the runner printed `FAIL` and then ran
`exit "$rc"` = `exit 0` — a false green. A real finding (semgrep exit 1) or a scanner
infrastructure failure (semgrep exit 2) both passed CI as clean.

This is the same class of bug the sibling `ci/trivy/trivy-scan.sh` runner already fixed; the fix
here mirrors that runner's `set +e; scanner; rc=$?; set -e` pattern.

## The fix (smallest correct change, scoped to SAST)

`ci/sast/sast.sh` — replace the `if semgrep …; then` gate with a direct invocation that captures
semgrep's own exit code inside a `set +e` / `set -e` bracket, then branch on `rc`:

- `rc == 0` → PASS, exit 0.
- `SAST_WARN=1` → warn tier, exit 0 **unconditionally** (documented "never fail the build"
  policy — preserved, and now honored for scanner errors too, not only findings).
- otherwise → block tier, `exit "$rc"` (propagates 1 for findings, 2 for scanner failure).

The intentional warn-tier policy was preserved, not changed. No permission bypass, no broad
ignore, no change outside the SAST runner. `workflow.yml` uses the `semgrep/semgrep-action`
GitHub action and is unaffected by the shell bug.

## Tests added

`tests/test_sast_gate.py` — stdlib-only (`subprocess` + a POSIX `semgrep` stub on PATH), invoking
the real shell runner. Mirrors `tests/test_trivy_gate.py`. Covers:

1. clean scan (exit 0) → runner PASS/exit 0;
2. finding (exit 1) in block mode → runner FAIL/exit 1 (regression);
3. scanner infrastructure failure (exit 2) in block mode → runner FAIL/exit 2 (regression);
4. `SAST_WARN=1` never fails on a finding (exit 0);
5. `SAST_WARN=1` never fails on a scanner error (exit 0);
6. default argv (`scan`, `--config auto`, `--error`, target last);
7. config/target/extra argument construction preserved;
8. script executable + shellcheck-clean (shellcheck ran, passed).

## Exact commands run

RED (against the buggy runner — 2 block-tier tests fail, log `/tmp/gl264/red.log`):

```
uv run --with pytest python -m pytest tests/test_sast_gate.py -v
# -> 2 failed, 6 passed; both failures print "[sast] FAIL ... (exit 0)" then exit 0
```

GREEN (after the fix — log `/tmp/gl264/green.log`):

```
uv run --with pytest python -m pytest tests/test_sast_gate.py -v
# -> 8 passed in 12.26s ; shellcheck available and clean
```

Fable 5.1 review (staged diff; log `/tmp/gl264/review.log`):

```
git add ci/sast/sast.sh tests/test_sast_gate.py
review diff --staged --task GL-264 -m claude:claude-fable-5-1 --timeout 900 --effort high < /dev/null
# -> [ok] on all three roles (correctness, performance, quality); iteration 21,
#    session sess-20260924T193831_048582. Board padded to claude:claude-fable-5-1
#    across all three roles (only that model was reachable, per -m).
```

## Review outcome

PASS — `[ok]` from all three roles. Correctness confirmed the bug and that the rewrite captures
the scanner's own status and propagates 1/2/127/130 to `exit "$rc"`. No blocking findings.

### Non-blocking quality suggestions (deferred — NOT applied, to keep the change minimal & scoped)

The parent may pick these up separately; none is a correctness/security defect and none was in
scope for this minimal false-green fix:

- Block-tier FAIL message still says "Fix them, or use an inline // nosemgrep" for a scanner
  error (exit 2), where that advice does not apply. (Warn-tier message already says "findings
  and/or a scanner error".) Could branch the message on `rc`.
- `set +e`/`set -e` toggle vs. the `rc=0; cmd || rc=$?` idiom — kept the toggle deliberately for
  parity with `ci/trivy/trivy-scan.sh`.
- GL-264 explanation appears in a few places (runner comment + test docstrings) — could be
  reduced to one canonical spot.
- Tier tests could be parametrized; argv assertions are substring matches; the shellcheck test
  passes (rather than skips) when shellcheck is absent; tests invoke `sh SCRIPT` rather than the
  shebang; PATH is replaced rather than prepended.
- No test exercises positional args passed to the script itself flowing through `"$@"`.
