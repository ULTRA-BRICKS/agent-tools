"""Regression tests for task-cli#140 — dep-audit.sh must only FAIL a PR for an advisory that
is NEW relative to the PR's base branch, never for one already on the base branch.

Background: dep-audit.sh (ci/dependency-review/dep-audit.sh) previously audited only the
current (PR head) tree and failed on ANY advisory at/above DEP_AUDIT_LEVEL, with no notion of
"was this already here before the PR". A real incident (hyperide/hyper-saas#780, a docs-only
AGENTS.md consolidation with zero dependency changes) failed this gate on 6 pre-existing
high-severity transitive advisories (browserslist, fast-uri) that predated the PR — a
systemic false positive that blocks EVERY future PR in EVERY repo carrying this shared CI
slot, forever, once any pre-existing high+ transitive advisory exists (a near-certainty over
time in any real lockfile).

The fix: dep-audit.sh now accepts an optional second positional argument, BASE_DIR — the base
branch's checked-out tree. When given, each JS-ecosystem branch (bun/npm/pnpm/yarn — the ones
with a `--json` output whose severity-tagged advisory records dep-audit.sh can parse via jq)
runs the SAME audit against BASE_DIR and diffs by the GitHub Advisory Database's numeric id
(the "id"/"source" field — the same id space across bun/npm/pnpm, verified against a live
browserslist advisory). Only advisories NEW relative to BASE_DIR fail the check; advisories
present in both are printed as a visible informational warning but never fail it. With no
BASE_DIR (the default — old callers, push events with no PR to diff against) behavior is
UNCHANGED: any advisory at/above DEP_AUDIT_LEVEL fails, exactly as before this fix.

We drive the real dep-audit.sh against a FAKE `bun` binary on PATH (a tiny shell stub that
reads a JSON fixture file + a paired exit-code file from its cwd) rather than the real `bun
audit`, which would need network access to a live advisory feed and would be non-deterministic
across time (advisories get published/patched). The stub's `--json` output shape mirrors real
`bun audit --json` byte-for-byte (verified manually against a live vulnerable fixture:
browserslist 4.16.6 -> GHSA-c83g-rgw3-j3cx, id 1153171) so the parsing path under test is the
real one, only the advisory DB lookup is faked.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEP_AUDIT = REPO_ROOT / "ci" / "dependency-review" / "dep-audit.sh"

# A real GitHub Advisory Database id/severity pair (browserslist <=4.28.6, high) — used as the
# "pre-existing" advisory in every scenario below. A second, disjoint fake id stands in for a
# genuinely NEW advisory a PR could introduce.
PRE_EXISTING_ID = 1153171
PRE_EXISTING_SEVERITY = "high"
NEW_ID = 9999999
NEW_SEVERITY = "high"


def _bun_advisory_json(*entries: tuple[int, str]) -> str:
    """Build a `bun audit --json` shaped blob: {"<pkg>": [{"id":N,"severity":"S",...}, ...]}."""
    if not entries:
        return "{}"
    items = ",".join(
        '{{"id":{id},"url":"https://github.com/advisories/GHSA-fake-{id}",'
        '"title":"fake advisory {id}","severity":"{sev}","vulnerable_versions":"<=0.0.0"}}'.format(
            id=aid, sev=sev
        )
        for aid, sev in entries
    )
    return '{{"fake-pkg":[{items}]}}'.format(items=items)


def _write_stub_bun(bin_dir: Path) -> None:
    """A fake `bun` that only implements `audit [...] [--json]`. It reads files from its
    CURRENT WORKING DIRECTORY (not from argv — dep-audit.sh cd's into AUDIT_DIR/BASE_DIR before
    invoking the audit command, exactly like the real workflow does):
      - audit-fixture.json     — printed verbatim for `--json` invocations (default: "{}")
      - audit-fixture.rc       — bun's exit code for the PLAIN (non --json) invocation
                                 (default: 0)
      - audit-fixture.json-rc  — bun's exit code for the `--json` invocation specifically
                                 (default: same as audit-fixture.rc). Real bun/npm/pnpm always
                                 mirror the plain invocation's exit code here — this knob exists
                                 to simulate a tool whose --json mode reports success/failure
                                 differently from its own plain mode (see
                                 test_json_schema_mismatch_does_not_swallow_a_real_failure).
    This mirrors real bun audit: the JSON payload lists every advisory it knows about
    regardless of --audit-level (verified live), and --audit-level only affects the exit code.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "bun"
    stub.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'if [ "${1:-}" != "audit" ]; then\n'
        '  echo "stub-bun: unsupported invocation: $*" >&2\n'
        "  exit 2\n"
        "fi\n"
        "shift\n"
        "json=0\n"
        'for a in "$@"; do\n'
        '  [ "$a" = "--json" ] && json=1\n'
        "done\n"
        'rc=0; [ -f audit-fixture.rc ] && rc="$(cat audit-fixture.rc)"\n'
        'if [ "$json" = "1" ]; then\n'
        '  if [ -f audit-fixture.json ]; then cat audit-fixture.json; else echo "{}"; fi\n'
        '  [ -f audit-fixture.json-rc ] && rc="$(cat audit-fixture.json-rc)"\n'
        "else\n"
        '  echo "stub-bun: pretty output (rc=$rc)"\n'
        "fi\n"
        'exit "$rc"\n'
    )
    os.chmod(stub, 0o755)


def _make_tree(dir_: Path, *, advisories: tuple[tuple[int, str], ...], rc: int) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "bun.lock").write_text("# fake lockfile\n")
    (dir_ / "audit-fixture.json").write_text(_bun_advisory_json(*advisories))
    (dir_ / "audit-fixture.rc").write_text(str(rc))


def _run(cwd: Path, *args: str, path: str) -> tuple[int, str]:
    env = dict(os.environ, PATH=path)
    proc = subprocess.run(
        ["bash", str(DEP_AUDIT), *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr


@pytest.fixture()
def bun_path(tmp_path: Path) -> str:
    bin_dir = tmp_path / "fakebin"
    _write_stub_bun(bin_dir)
    return f"{bin_dir}:/usr/bin:/bin"


def _npm_advisory_json(*entries: tuple[int, str]) -> str:
    """Build an `npm audit --json` shaped blob — deliberately a DIFFERENT shape from bun's:
    nested under "vulnerabilities" -> "<pkg>" -> "via", using "source" (not "id") for the
    advisory id, one level deeper than bun's flat array. Matches real npm audit --json,
    verified live against browserslist 4.16.6 (source 1153171 / GHSA-c83g-rgw3-j3cx)."""
    if not entries:
        return '{"auditReportVersion":2,"vulnerabilities":{}}'
    via = ",".join(
        '{{"source":{id},"name":"fake-pkg","severity":"{sev}",'
        '"url":"https://github.com/advisories/GHSA-fake-{id}"}}'.format(id=aid, sev=sev)
        for aid, sev in entries
    )
    return (
        '{{"auditReportVersion":2,"vulnerabilities":{{"fake-pkg":'
        '{{"name":"fake-pkg","severity":"{sev}","via":[{via}]}}}}}}'
    ).format(sev=entries[0][1], via=via)


def _write_stub_npm(bin_dir: Path) -> None:
    """A fake `npm` implementing only `audit [...] [--json]`, same fixture-file contract as
    _write_stub_bun (see its docstring) but reading npm-prefixed fixture files so a single
    tmp_path/bin_dir can host both stubs without collision."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "npm"
    stub.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'if [ "${1:-}" != "audit" ]; then\n'
        '  echo "stub-npm: unsupported invocation: $*" >&2\n'
        "  exit 2\n"
        "fi\n"
        "shift\n"
        "json=0\n"
        'for a in "$@"; do\n'
        '  [ "$a" = "--json" ] && json=1\n'
        "done\n"
        'rc=0; [ -f audit-fixture.rc ] && rc="$(cat audit-fixture.rc)"\n'
        'if [ "$json" = "1" ]; then\n'
        '  if [ -f audit-fixture.json ]; then cat audit-fixture.json; else echo "{}"; fi\n'
        '  [ -f audit-fixture.json-rc ] && rc="$(cat audit-fixture.json-rc)"\n'
        "else\n"
        '  echo "stub-npm: pretty output (rc=$rc)"\n'
        "fi\n"
        'exit "$rc"\n'
    )
    os.chmod(stub, 0o755)


@pytest.fixture()
def npm_path(tmp_path: Path) -> str:
    bin_dir = tmp_path / "fakebin-npm"
    _write_stub_npm(bin_dir)
    return f"{bin_dir}:/usr/bin:/bin"


def _require_jq() -> None:
    if subprocess.run(["sh", "-c", "command -v jq"], capture_output=True).returncode != 0:
        pytest.skip("jq not installed — baseline diffing is skipped by design without it")


def test_pre_existing_vuln_alone_passes_but_is_reported(tmp_path: Path, bun_path: str) -> None:
    """The exact task-cli#140 / hyper-saas#780 scenario: a PR that changes zero dependencies,
    where the base branch already carries a high-severity transitive advisory. The check must
    PASS (exit 0) — the PR did not introduce this — but the advisory must still be VISIBLE as
    an informational warning, not silently dropped."""
    _require_jq()
    base = tmp_path / "base"
    head = tmp_path / "head"
    _make_tree(base, advisories=((PRE_EXISTING_ID, PRE_EXISTING_SEVERITY),), rc=1)
    _make_tree(head, advisories=((PRE_EXISTING_ID, PRE_EXISTING_SEVERITY),), rc=1)

    rc, out = _run(tmp_path, str(head), str(base), path=bun_path)

    assert rc == 0, out
    assert str(PRE_EXISTING_ID) in out
    assert "pre-existing" in out.lower()
    assert "not" in out.lower() and "fail" in out.lower()  # "NOT failing this check" wording


def test_new_vuln_relative_to_base_still_fails(tmp_path: Path, bun_path: str) -> None:
    """A PR that introduces a genuinely NEW vulnerable dependency (not present on the base
    branch) must still FAIL the check — the scoping fix must not neuter real detections."""
    _require_jq()
    base = tmp_path / "base"
    head = tmp_path / "head"
    _make_tree(base, advisories=((PRE_EXISTING_ID, PRE_EXISTING_SEVERITY),), rc=1)
    _make_tree(
        head,
        advisories=((PRE_EXISTING_ID, PRE_EXISTING_SEVERITY), (NEW_ID, NEW_SEVERITY)),
        rc=1,
    )

    rc, out = _run(tmp_path, str(head), str(base), path=bun_path)

    assert rc == 1, out
    assert str(NEW_ID) in out
    assert "new" in out.lower()
    # the pre-existing one is still reported, just not as the reason for failure
    assert str(PRE_EXISTING_ID) in out


def test_no_base_dir_preserves_old_whole_tree_behavior(tmp_path: Path, bun_path: str) -> None:
    """With NO second argument (old callers, or a push event with no PR to diff against),
    behavior is unchanged: ANY advisory at/above DEP_AUDIT_LEVEL fails the check, exactly like
    before this fix."""
    _require_jq()
    head = tmp_path / "head"
    _make_tree(head, advisories=((PRE_EXISTING_ID, PRE_EXISTING_SEVERITY),), rc=1)

    rc, out = _run(tmp_path, str(head), path=bun_path)  # no BASE_DIR arg

    assert rc == 1, out
    assert str(PRE_EXISTING_ID) in out


def test_clean_tree_passes_with_baseline_present(tmp_path: Path, bun_path: str) -> None:
    """Sanity: zero advisories anywhere (current or base) is a clean pass, baseline diffing
    present or not."""
    _require_jq()
    base = tmp_path / "base"
    head = tmp_path / "head"
    _make_tree(base, advisories=(), rc=0)
    _make_tree(head, advisories=(), rc=0)

    rc, out = _run(tmp_path, str(head), str(base), path=bun_path)

    assert rc == 0, out


def test_base_dir_missing_lockfile_falls_back_to_fail_on_all(tmp_path: Path, bun_path: str) -> None:
    """If BASE_DIR exists but has no matching lockfile (baseline unresolvable for this
    ecosystem), fail OPEN in the safe direction: audit the current tree as before (fail on any
    advisory), rather than crashing or silently passing everything."""
    _require_jq()
    base = tmp_path / "base"
    base.mkdir()  # exists, but no bun.lock inside — baseline unavailable for bun
    head = tmp_path / "head"
    _make_tree(head, advisories=((PRE_EXISTING_ID, PRE_EXISTING_SEVERITY),), rc=1)

    rc, out = _run(tmp_path, str(head), str(base), path=bun_path)

    assert rc == 1, out
    assert "no baseline" in out.lower() or "no matching lockfile" in out.lower()


def test_base_dir_nonexistent_does_not_crash(tmp_path: Path, bun_path: str) -> None:
    """A BASE_DIR that doesn't exist at all (deleted base branch, bad ref) must not crash the
    script — fail open to the old whole-tree behavior with a visible note."""
    _require_jq()
    head = tmp_path / "head"
    _make_tree(head, advisories=((PRE_EXISTING_ID, PRE_EXISTING_SEVERITY),), rc=1)

    rc, out = _run(tmp_path, str(head), str(tmp_path / "does-not-exist-base"), path=bun_path)

    assert rc == 1, out
    assert "no baseline" in out.lower() or "no matching lockfile" in out.lower() or "does not exist" in out.lower()


def test_json_schema_mismatch_does_not_swallow_a_real_failure(tmp_path: Path, bun_path: str) -> None:
    """Regression for a review finding on agent-tools#603: an ecosystem whose `--json` mode (a)
    uses a key schema ADVISORY_ID_JQ_FILTER doesn't recognize (e.g. different casing than the
    verified bun/npm/pnpm shape) AND (b) reports success (exit 0) on that `--json` invocation
    independently of its own plain-mode judgment — jq then parses the JSON fine and returns zero
    ids WITHOUT erroring. json_advisory_ids's own internal check (tool errored AND ids empty)
    does NOT catch this, because the --json run itself didn't error (rc 0). Without a check
    against the PLAIN invocation's exit code too, report_advisory_diff would see zero current
    ids and silently report a clean pass — even though the tool's own plain-mode exit code
    (rc=1, a REAL vulnerability) said otherwise. This must fail closed instead."""
    _require_jq()
    head = tmp_path / "head"
    head.mkdir()
    (head / "bun.lock").write_text("# fake lockfile\n")
    # Plain mode: rc=1 (a real vulnerability, per the tool's own judgment). --json mode: rc=0
    # (this ecosystem's JSON mode reports success independently) AND uses "ID"/"Severity"
    # (wrong case) instead of the "id"/"severity" schema the jq filter matches — exactly the
    # combination a future/unverified ecosystem could introduce.
    (head / "audit-fixture.json").write_text(
        '{"fake-pkg":[{"ID":9999999,"Severity":"high"}]}'
    )
    (head / "audit-fixture.rc").write_text("1")
    (head / "audit-fixture.json-rc").write_text("0")

    rc, out = _run(tmp_path, str(head), path=bun_path)  # no BASE_DIR — simplest reproduction

    assert rc == 1, out
    assert "shape didn't match" in out.lower() or "didn't parse" in out.lower()


def test_relative_base_dir_resolves_against_original_cwd(tmp_path: Path, bun_path: str) -> None:
    """Regression for a review finding on agent-tools#603: dep-audit.sh resolves a RELATIVE
    BASE_DIR to an absolute path before it `cd`s into AUDIT_DIR, specifically so a relative
    BASE_DIR doesn't get resolved against the wrong directory. Every other test in this file
    passes absolute paths (tmp_path-derived), which never exercises that resolution branch at
    all -- this test invokes the script with both arguments as bare relative subdirectory
    names (the documented usage in the README's Quick start), from a shared parent cwd, and
    asserts the baseline diff still works correctly (same pre-existing-advisory-passes
    scenario as test_pre_existing_vuln_alone_passes_but_is_reported, just with relative args)."""
    _require_jq()
    base = tmp_path / "base"
    head = tmp_path / "head"
    _make_tree(base, advisories=((PRE_EXISTING_ID, PRE_EXISTING_SEVERITY),), rc=1)
    _make_tree(head, advisories=((PRE_EXISTING_ID, PRE_EXISTING_SEVERITY),), rc=1)

    rc, out = _run(tmp_path, "head", "base", path=bun_path)  # relative, not str(head)/str(base)

    assert rc == 0, out
    assert str(PRE_EXISTING_ID) in out
    assert "pre-existing" in out.lower()


def test_npm_json_shape_is_parsed_correctly(tmp_path: Path, npm_path: str) -> None:
    """Regression for a review finding on agent-tools#603: every other test in this file only
    stubs bun's flat `{"pkg":[{"id":...}]}` --json shape. npm's real --json output nests
    advisories one level deeper, under "vulnerabilities" -> "<pkg>" -> "via", and uses "source"
    instead of "id" (verified live against browserslist 4.16.6, GHSA-c83g-rgw3-j3cx). This
    locks in that ADVISORY_ID_JQ_FILTER's recursive `..` walk actually extracts ids from that
    real shape, not just bun's -- exercising the same pre-existing-vs-new scenario as the bun
    tests above, but through the npm branch (package.json, no bun.lock)."""
    _require_jq()
    base = tmp_path / "base"
    head = tmp_path / "head"
    for d in (base, head):
        d.mkdir(parents=True, exist_ok=True)
        (d / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n')
        (d / "package-lock.json").write_text('{"name":"demo","lockfileVersion":3}\n')
        (d / "audit-fixture.json").write_text(
            _npm_advisory_json((PRE_EXISTING_ID, PRE_EXISTING_SEVERITY))
        )
        (d / "audit-fixture.rc").write_text("1")

    rc, out = _run(tmp_path, str(head), str(base), path=npm_path)

    assert rc == 0, out
    assert str(PRE_EXISTING_ID) in out
    assert "pre-existing" in out.lower()
