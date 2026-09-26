"""install-local-hooks.sh's secret-scan dedup scanner must work with BSD sed (macOS).

BSD sed reads a `:label` to the END of its line, so the GNU one-liner `sed ':a; s/…//; ta'` is
just one label there: sed warns "unused label" and passes every line through unchanged. The
dedup scanner then never stripped `VAR=value` prefixes on macOS, so a local scan such as
`GITLEAKS_CONFIG=x gitleaks git --staged` read as "no local secret scan detected" (found while
wiring summary-bot's husky hooks).

The scanner is run here exactly as the script defines it (its own regexes and function body,
read from the file), through a `sed` shim that enforces BSD's label rule on any platform, and on
macOS also through the real /usr/bin/sed. A static check keeps the GNU-only label form out of
every git-hooks shell script.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "git-hooks" / "global-dispatcher" / "install-local-hooks.sh"
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh + sed")

# BSD sed: `:a; s/x/y/; ta` defines ONE label named "a; s/x/y/; ta" and no command, so it warns
# "unused label" and copies its input unchanged (exit 0) — measured with macOS /usr/bin/sed. The
# shim does the same for any `:label` program that continues after `;` on its line, and
# otherwise runs the real sed.
_BSD_LABEL_SHIM = """#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    :*\\;*) echo "sed: 1: \\"$arg\\": unused label" >&2; exec cat ;;
  esac
done
exec "$REAL_SED" "$@"
"""

DETECTED = [
    "gitleaks git --staged",
    "GITLEAKS_CONFIG=.gitleaks.toml gitleaks git --staged",
    "A=1 B=2 gitleaks protect --staged",
    "  FOO=bar ./bin/secret-scan",
    "npx lint-staged\ngitleaks git --staged",
]
NOT_DETECTED = [
    "echo hi",
    "gitleaks git --staged || true",
    "# gitleaks git --staged",
    "GITLEAKS=1 echo gitleaks",
    "gitleaks git --staged | tee out.log",
]


def _scanner_source() -> str:
    """The script's own MARKER / SECRET_CMD_RE / NEUTRALIZED_RE and scan_text_for_secret()."""
    text = SCRIPT.read_text(encoding="utf-8")
    parts = []
    for name in ("MARKER", "SECRET_CMD_RE", "NEUTRALIZED_RE"):
        m = re.search(rf"^{name}=.*$", text, re.M)
        assert m, f"{name} not found in {SCRIPT.name}"
        parts.append(m.group(0))
    m = re.search(r"^scan_text_for_secret\(\) \{\n.*?^\}\n", text, re.M | re.S)
    assert m, "scan_text_for_secret() not found"
    parts.append(m.group(0))
    return "\n".join(parts) + "\nscan_text_for_secret\n"


def _sed_dir(tmp_path: Path, mode: str) -> tuple[Path | None, dict[str, str]]:
    env = dict(os.environ)
    if mode == "system":
        return None, env
    bindir = tmp_path / f"sed-{mode}"
    bindir.mkdir()
    sed = bindir / "sed"
    if mode == "bsd-rules":
        env["REAL_SED"] = shutil.which("sed") or "/usr/bin/sed"
        sed.write_text(_BSD_LABEL_SHIM, encoding="utf-8")
        sed.chmod(0o755)
    else:  # "macos": the real BSD sed
        sed.symlink_to("/usr/bin/sed")
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}"
    return bindir, env


def _scan(text: str, tmp_path: Path, mode: str) -> subprocess.CompletedProcess[str]:
    driver = tmp_path / "scan.sh"
    driver.write_text(_scanner_source(), encoding="utf-8")
    _, env = _sed_dir(tmp_path, mode)
    return subprocess.run(["sh", str(driver)], input=text + "\n", text=True, env=env, capture_output=True, timeout=30)


MODES = ["system", "bsd-rules"] + (["macos"] if sys.platform == "darwin" and Path("/usr/bin/sed").exists() else [])


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("hook", DETECTED)
def test_a_local_secret_scan_is_detected_under_every_sed(hook, mode, tmp_path):
    res = _scan(hook, tmp_path, mode)
    assert res.returncode == 0, (mode, hook, res.stderr)
    assert "unused label" not in res.stderr


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("hook", NOT_DETECTED)
def test_non_blocking_or_absent_scans_are_not_counted(hook, mode, tmp_path):
    res = _scan(hook, tmp_path, mode)
    assert res.returncode == 1, (mode, hook, res.stderr)
    assert "unused label" not in res.stderr


@pytest.mark.parametrize("mode", ["bsd-rules"] + (["macos"] if "macos" in MODES else []))
def test_the_gnu_label_one_liner_is_a_no_op_under_bsd_rules(mode, tmp_path):
    """Keeps the emulation honest: the old scanner line strips nothing under it, as on macOS."""
    _, env = _sed_dir(tmp_path, mode)
    res = subprocess.run(
        ["sh", "-c", "echo 'A=1 gitleaks git' | sed -E ':a; s/^[[:space:]]*[A-Za-z_]+=[^[:space:]]*[[:space:]]+//; ta'"],
        text=True, env=env, capture_output=True, timeout=30,
    )
    assert "unused label" in res.stderr
    assert res.stdout.strip() == "A=1 gitleaks git"


def test_no_git_hooks_script_uses_a_gnu_only_sed_label():
    """`sed ':a; …; ta'` / `sed -e ':a;…'` works only in GNU sed; the global hooks run on macOS."""
    label_one_liner = re.compile(r"""\bsed\b[^|\n]*?['"]\s*:[A-Za-z_][A-Za-z0-9_]*\s*;""")
    offenders = []
    for path in sorted((ROOT / "git-hooks").rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not (path.suffix == ".sh" or text.startswith("#!")):
            continue
        offenders += [f"{path.relative_to(ROOT)}:{n}" for n, line in enumerate(text.splitlines(), 1) if label_one_liner.search(line)]
    assert not offenders, offenders
