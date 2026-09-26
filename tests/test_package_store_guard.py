"""Tests for agent-hooks/package-store-guard — shared package stores and store-backed installs.

The cruxes (each red if the command were allowed):
- ``bun pm cache rm`` and ``rm -rf ~/.bun/install/cache`` wipe the bun global store every
  worktree's node_modules links into (2026-09-15 incident);
- ``ln -s <main>/node_modules node_modules`` lets ``bun install`` write through into the main
  checkout (hypercalendarbot, 2026-09-09);
- ``npm install`` in a bun project makes a full real copy of node_modules.
And the allow side: npm in an npm-only subproject, global installs, ordinary rm/ln.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "package-store-guard"
    / "package_store_guard.py"
)
_DESCRIPTOR = _HOOK.parent / "package-store-guard.pre-bash.json"

_spec = importlib.util.spec_from_file_location("package_store_guard", _HOOK)
assert _spec and _spec.loader
psg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(psg)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    (h / ".bun" / "install" / "cache" / "links").mkdir(parents=True)
    (h / ".cache" / "uv").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    for var in ("BUN_INSTALL", "BUN_INSTALL_CACHE_DIR", "UV_CACHE_DIR", "XDG_CACHE_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    return h


@pytest.fixture
def bun_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text("{}\n", encoding="utf-8")
    (repo / "bun.lock").write_text("{}\n", encoding="utf-8")
    return repo


def _run(command: str, monkeypatch, cwd: Path) -> tuple[dict, int]:
    out, err = io.StringIO(), io.StringIO()
    event = {"hook_api": "agents-hooks/v1", "point": "pre-bash", "args": {"command": command}, "cwd": str(cwd)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = psg.main()
    return json.loads(out.getvalue()), code


def _blocked(command: str, monkeypatch, cwd: Path) -> str:
    payload, code = _run(command, monkeypatch, cwd)
    assert code == psg.BLOCK_EXIT_CODE, (command, payload)
    assert payload["decision"] == "block"
    return payload["message"]


def _allowed(command: str, monkeypatch, cwd: Path) -> None:
    payload, code = _run(command, monkeypatch, cwd)
    assert code == 0, (command, payload)
    assert payload["decision"] == "allow"


# ── store wipes ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "bun pm cache rm",
        "bun pm cache rm --verbose",
        "cd /tmp && bun pm cache rm",
        "bash -c 'bun pm cache rm'",
        "rm -rf ~/.bun/install/cache",
        "rm -rf ~/.bun/install/cache/",
        "rm -rf ~/.bun/install/cache/links",
        "rm -rf ~/.bun/install/cache/*",
        "rm -rf ~/.bun",
        "rm -rf $HOME/.bun/install/cache",
        "rm -rf ${HOME}/.bun/install/cache",
        'rm -rf "$(bun pm cache)"',
        "rm -rf $(bun pm cache)",
        "sudo rm -rf ~/.bun/install/cache",
        "du -sh ~/.bun && rm -r -f ~/.bun/install/cache",
        "trash ~/.bun/install/cache",
        "mv ~/.bun/install/cache /tmp/old-bun-cache",
        "find ~/.bun/install/cache -type d -name 'links' -exec rm -rf {} +",
        "find ~/.bun/install/cache/links -mindepth 1 -delete",
        "ssh mac 'rm -rf ~/.bun/install/cache'",
    ],
)
def test_bun_store_wipes_block(command, home, tmp_path, monkeypatch):
    msg = _blocked(command, monkeypatch, tmp_path)
    assert "shared package store" in msg
    assert "rig worktree gc" in msg
    assert "bun install --frozen-lockfile" in msg


def test_relative_rm_resolves_against_cd(home, monkeypatch, tmp_path):
    _blocked("cd ~/.bun/install && rm -rf cache", monkeypatch, tmp_path)
    _blocked("rm -rf install/cache", monkeypatch, home / ".bun")


def test_bun_install_cache_dir_env_is_protected(home, tmp_path, monkeypatch):
    custom = tmp_path / "custom-bun-cache"
    monkeypatch.setenv("BUN_INSTALL_CACHE_DIR", str(custom))
    _blocked(f"rm -rf {custom}", monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "uv cache clean",
        "uv cache clean requests",
        "uv --quiet cache clean",
        "uv --cache-dir /x cache clean",
        "uv cache prune --all",
        "uv cache prune --ci",
        "rm -rf ~/.cache/uv",
        "rm -rf ~/.cache/*",
        "rm -rf ~/.cache",
    ],
)
def test_uv_cache_wipes_block(command, home, tmp_path, monkeypatch):
    msg = _blocked(command, monkeypatch, tmp_path)
    assert "uv" in msg


def test_uv_cache_dir_env_is_protected(home, tmp_path, monkeypatch):
    custom = tmp_path / "uvc"
    monkeypatch.setenv("UV_CACHE_DIR", str(custom))
    _blocked(f"rm -rf {custom}", monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "bun pm cache",
        "bun install --frozen-lockfile",
        "uv cache dir",
        "uv cache prune",
        "uv sync",
        "rm -rf node_modules",
        "rm -rf ~/.bun/bin/old-bun",
        "rm -rf ~/.cache/pip",
        "rm -rf ~/.cache/pip*",
        "rm -rf /tmp/scratch",
        "find ./build -name '*.o' -delete",
        "find ~/.bun/install/cache -name '*.tgz' -print",
        "find ~/.bun/install/cache/links -type l ! -exec test -e {} \\; -print 2>/dev/null | wc -l",
        "ls ~/.bun/install/cache | head",
        "du -sh ~/.bun/install/cache",
        "mv /tmp/a ~/.cache/uv-notes",
        "echo 'rm -rf ~/.bun/install/cache' > note.txt",
        "git commit -m 'never run bun pm cache rm'",
    ],
)
def test_store_neighbours_allowed(command, home, tmp_path, monkeypatch):
    _allowed(command, monkeypatch, tmp_path)


def test_unparseable_command_still_blocks_literal_store_wipe(home, tmp_path, monkeypatch):
    _blocked("bun pm cache rm; echo 'unterminated", monkeypatch, tmp_path)
    _allowed("echo 'unterminated", monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        # heredoc bodies are data (commit messages, docs), even with an apostrophe in them
        "git commit -F - <<'EOF'\nfeat: guard\n\nrm -rf ~/.bun/install/cache is what we block, don't.\nbun pm cache rm\nEOF",
        "cat > notes.md <<EOF\nln -s /main/node_modules node_modules\nnpm install\nEOF\necho done",
        "cat <<-EOF\n\tuv cache clean\n\tEOF",
        # a quoted multi-line argument stays one token
        'git commit -m "feat: guard\n\nbun pm cache rm is blocked now"',
        # comments never run
        "echo hi # rm -rf ~/.bun/install/cache",
    ],
)
def test_data_that_mentions_blocked_commands_is_allowed(command, home, bun_repo, monkeypatch):
    _allowed(command, monkeypatch, bun_repo)


@pytest.mark.parametrize(
    "command",
    [
        "cat <<EOF > x\nhello\nEOF\nbun pm cache rm",
        "echo start\nrm -rf ~/.bun/install/cache",
        "rm -rf \\\n  ~/.bun/install/cache",
        "echo hi # comment\nbun pm cache rm",
    ],
)
def test_commands_after_heredocs_comments_and_continuations_still_block(command, home, tmp_path, monkeypatch):
    _blocked(command, monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "ln -s /main/node_modules node_modules >/dev/null 2>&1",
        "ln -sfn /main/node_modules node_modules 2>/dev/null || true",
        "rm -rf ~/.bun/install/cache 2>/dev/null",
    ],
)
def test_redirections_do_not_hide_operands(command, home, tmp_path, monkeypatch):
    _blocked(command, monkeypatch, tmp_path)


def test_redirect_target_is_not_an_operand(home, tmp_path, monkeypatch):
    _allowed("ln -s ../pkg node_modules/pkg > node_modules", monkeypatch, tmp_path)


# ── review hardening: indirection, wrappers, find options, pipelines ─────────


@pytest.mark.parametrize(
    "command",
    [
        "find -L ~/.bun/install/cache/links -xtype l -delete",
        "find -H ~/.bun/install/cache -delete",
        "find -f ~/.bun/install/cache -delete",
        "find -O3 ~/.bun/install/cache/links -mindepth 1 -exec rm -rf {} +",
        'X=$(bun pm cache); rm -rf "$X"',
        'X="$(bun pm cache)" && rm -rf "$X/links"',
        "CACHE=~/.bun/install/cache && rm -rf $CACHE",
        'export C="$(uv cache dir)"; rm -rf "$C"',
        'rm -rf "${UV_CACHE_DIR:-$HOME/.cache/uv}"',
        "ls ~/.bun/install/cache/links | xargs rm -rf",
        "find ~/.bun/install/cache -name '*' -print0 | xargs -0 rm -f",
        "find ~/.bun/install/cache/links -type l ! -exec test -e {} \\; -print0 | xargs -0 rm -f",
        "find ~/.bun/install/cache/links -type l -exec rm {} \\;",
        "busybox rm -rf ~/.bun/install/cache",
        "fish -c 'bun pm cache rm'",
        "env -S 'rm -rf ~/.bun/install/cache'",
        "env --split-string='bun pm cache rm'",
        "env -C ~/.bun/install rm -rf cache",
        "sudo -D ~/.bun/install rm -rf cache",
        "rm -rf -- ~/.bun/install/cache",
        "rmdir ~/.bun/install/cache",
        "unlink ~/.bun/install/cache",
        "rm -rf ~/.bun/install/cache '",
    ],
)
def test_indirect_store_wipes_block(command, home, tmp_path, monkeypatch):
    _blocked(command, monkeypatch, tmp_path)


def test_bun_install_env_root_is_protected(home, tmp_path, monkeypatch):
    monkeypatch.setenv("BUN_INSTALL", str(tmp_path / "bunhome"))
    _blocked(f"rm -rf {tmp_path / 'bunhome' / 'install' / 'cache'}", monkeypatch, tmp_path)


@pytest.mark.skipif(sys.platform not in {"darwin", "win32"}, reason="case-insensitive filesystems only")
def test_case_variants_block_on_case_insensitive_filesystems(home, tmp_path, monkeypatch):
    _blocked("rm -rf ~/.BUN/install/cache", monkeypatch, tmp_path)
    _blocked("rm -rf ~/.cache/UV", monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "find ~/.bun/install/cache -name '*.tgz' -exec sha256sum {} +",
        "X=$(bun pm cache); du -sh \"$X\"",
        "du -sh ~/.bun/install/cache; ls | xargs rm -f",
        "ls ~/.bun/install/cache | xargs -n1 echo",
        "env -C /tmp ls",
        "FOO=bar npm run build",
    ],
)
def test_indirection_neighbours_allowed(command, home, tmp_path, monkeypatch):
    _allowed(command, monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command,verb",
    [
        ("npm uninstall lodash", "npm uninstall"),
        ("npm rm lodash", "npm rm"),
        ("npm prune", "npm prune"),
        ("pnpm remove zod", "pnpm remove"),
        ("pnpm -w install", "pnpm install"),
        ("yarn remove zod", "yarn remove"),
        ("npm --loglevel warn install", "npm install"),
        ("npm ci '", "npm ci"),
    ],
)
def test_more_foreign_pm_verbs_block_with_the_right_verb(command, verb, bun_repo, home, monkeypatch):
    msg = _blocked(command, monkeypatch, bun_repo)
    assert f"`{verb}`" in msg


@pytest.mark.parametrize(
    "command",
    [
        # a here-doc fed to a shell is a script, not data
        "bash <<'EOF'\nbun pm cache rm\nEOF",
        "sh <<EOF\nrm -rf ~/.bun/install/cache\nEOF",
        "sudo bash -s <<'EOF'\nuv cache clean\nEOF",
        "ssh mac <<'EOF'\nbun pm cache rm\nEOF",
        "ssh -o BatchMode=yes mac <<'EOF'\nrm -rf ~/.bun/install/cache\nEOF",
        # a quoted or commented `<<EOF` opens no here-doc
        "echo 'tip: <<EOF starts a heredoc'\nbun pm cache rm",
        "echo hi # <<EOF\nbun pm cache rm",
        # command substitutions and inline assignments
        "FOO=$(bun pm cache rm)",
        "X=$(bun pm cache) rm -rf ~/.bun/install/cache",
        'C="$(uv cache dir)" rm -rf ~/.cache/uv',
        # bash -c operands bind to $0/$1/$@
        "bash -c 'rm -rf \"$1\"' _ ~/.bun/install/cache",
        "bash -c '\"$@\"' _ bun pm cache rm",
        # an unfiltered find from an ancestor deletes everything below it
        "find ~/.bun -delete",
        "find ~/.cache -delete",
        "cd ~/.bun && find . -delete",
    ],
)
def test_round_two_store_bypasses_block(command, home, tmp_path, monkeypatch):
    _blocked(command, monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'ln -s \"$1\" \"$2\"' _ /main/node_modules node_modules",
        "cp -s /main/node_modules node_modules",
        "cp -rs /main/node_modules node_modules",
        "cp --symbolic-link /main/node_modules node_modules",
    ],
)
def test_round_two_symlink_bypasses_block(command, home, tmp_path, monkeypatch):
    assert "symlink node_modules" in _blocked(command, monkeypatch, tmp_path)


def test_round_two_pm_bypasses_block(bun_repo, home, monkeypatch):
    _blocked("bash -c '\"$@\"' _ npm install", monkeypatch, bun_repo)
    _blocked("X=$(bun pm cache) npm install", monkeypatch, bun_repo)


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'echo hi' <<EOF\nbun pm cache rm\nEOF",
        "python3 - <<'EOF'\nprint('bun pm cache rm')\nEOF",
        "bash script.sh <<EOF\nrm -rf ~/.bun/install/cache\nEOF",
        "cat <<'EOF' | tee notes.md\nbun pm cache rm\nEOF",
        "cp -r src dst",
        "cp -s a b",
        "find /tmp/scratch -type f -delete",
        "X=$(ls); echo $X",
    ],
)
def test_round_two_neighbours_allowed(command, home, tmp_path, monkeypatch):
    _allowed(command, monkeypatch, tmp_path)


@pytest.mark.skipif(sys.platform not in {"darwin", "win32"}, reason="case-insensitive filesystems only")
def test_node_modules_case_variant_blocks_on_case_insensitive_filesystems(home, tmp_path, monkeypatch):
    _blocked("ln -s /main/node_modules Node_Modules", monkeypatch, tmp_path)


@pytest.mark.parametrize("event", [{"args": ["rm", "-rf", "x"]}, ["not", "a", "dict"], {"args": "bun pm cache rm"}])
def test_malformed_event_shapes_allow_instead_of_crashing(event, monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert psg.main() == 0
    assert json.loads(out.getvalue())["decision"] == "allow"


def test_ln_to_an_existing_dir_links_inside_it(home, tmp_path, monkeypatch):
    (tmp_path / "wt").mkdir()
    _blocked("ln -s /main/node_modules wt", monkeypatch, tmp_path)
    _allowed("ln -s ../repo/node_modules nm-alias", monkeypatch, tmp_path)
    _blocked("ln -s ../node_modules node_modules '", monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        # command substitutions run wherever they appear
        "echo $(bun pm cache rm)",
        'touch "$(rm -rf ~/.bun/install/cache)"',
        "$(uv cache clean)",
        "echo `bun pm cache rm`",
        # a here-doc piped into a shell is a script
        "cat <<'EOF' | bash\nrm -rf ~/.bun/install/cache\nEOF",
        "cat <<EOF | sh -s\nuv cache clean\nEOF",
        # CRLF line endings still close the here-doc
        "cat <<EOF > x\r\nhello\r\nEOF\r\nrm -rf ~/.bun/install/cache",
        # filters do not make a delete walk above the store safe
        "find ~/.bun -type d -delete",
        "find ~/.bun -name cache -exec rm -rf {} +",
        "find ~/.bun -prune -delete",
        "find ~ -name '*' -delete",
        "find ~ -name '*.orig' -delete",
    ],
)
def test_round_three_store_bypasses_block(command, home, tmp_path, monkeypatch):
    _blocked(command, monkeypatch, tmp_path)


def test_renaming_a_symlink_into_node_modules_blocks(home, tmp_path, monkeypatch):
    _blocked("ln -s /main/node_modules .nm && mv .nm node_modules", monkeypatch, tmp_path)
    os.symlink(tmp_path, tmp_path / ".nm")
    _blocked("mv .nm node_modules", monkeypatch, tmp_path)
    _blocked("cp -P .nm node_modules", monkeypatch, tmp_path)
    _blocked('X=node_modules; ln -s /main/nm "$X"', monkeypatch, tmp_path)
    (tmp_path / "plain").mkdir()
    _allowed("mv plain node_modules", monkeypatch, tmp_path)  # a real directory, not a link


def test_directory_stack_keeps_relative_paths_honest(home, tmp_path, monkeypatch):
    _allowed("pushd ~/.bun/install && popd && rm -rf cache", monkeypatch, tmp_path)
    _allowed("cd ~/.bun/install; cd -; rm -rf cache", monkeypatch, tmp_path)
    _blocked("pushd /tmp && pushd ~/.bun/install && rm -rf cache", monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        # wrappers with valued flags in front of a shell reading a here-doc
        "sudo -u root bash <<'EOF'\nbun pm cache rm\nEOF",
        "env -C ~/.bun/install bash <<'EOF'\nrm -rf cache\nEOF",
        "timeout 5m bash <<'EOF'\nrm -rf ~/.bun/install/cache\nEOF",
        "nice -n 5 sh <<EOF\nuv cache clean\nEOF",
        # an unquoted substitution with a suffix is still one assignment word
        'X=$(bun pm cache)/links; rm -rf "$X"',
        "X=`bun pm cache`; rm -rf $X",
        # find without a start path walks `.`
        "cd ~/.bun/install/cache && find -delete",
        "cd ~/.bun/install/cache && find -print0 | xargs -0 rm -f",
        # -exec tails are commands too
        "find . -name '*.tgz' -exec rm -rf ~/.bun/install/cache {} +",
        "find /tmp -exec bash -c 'rm -rf ~/.bun/install/cache' \\;",
        # the uv CLI through python -m
        "python -m uv cache clean",
        "python3 -I -m uv cache prune --all",
    ],
)
def test_round_five_bypasses_block(command, home, tmp_path, monkeypatch):
    _blocked(command, monkeypatch, tmp_path)


@pytest.mark.skipif(sys.platform not in {"darwin", "win32"}, reason="case-insensitive command lookup only")
@pytest.mark.parametrize("command", ["Bun pm cache rm", "UV cache clean", "RM -rf ~/.bun/install/cache", "COLIMA start"])
def test_command_names_are_case_insensitive_where_lookup_is(command, home, tmp_path, monkeypatch):
    _blocked(command, monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "find . -name '*.o' -exec test -f {} \\;",
        "python -m pytest -q",
        "python3 script.py uv cache clean",
        "X=$(date +%s); echo $X",
        "echo \"$(git rev-parse HEAD); done\"",
    ],
)
def test_round_five_neighbours_allowed(command, home, tmp_path, monkeypatch):
    _allowed(command, monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "cat <<BUILD-LOG-1\nsome text\nBUILD-LOG-1\nbun pm cache rm",
        'find "$(bun pm cache)/links" -mindepth 1 -delete',
        "find $(uv cache dir) -delete",
        "ls $(bun pm cache) | xargs rm -rf",
        "bun --cwd /x pm cache rm",
        "bun pm cache clean",
        "bun pm cache clear",
        "colima --profile ci --arch aarch64 start",
        "rm -rf ~/.bun/install/cache __psg_subst_0__",
        "bun pm cache rm __psg_subst_abc_1__",
        "exec -a foo rm -rf ~/.bun/install/cache",
        "env -a foo rm -rf ~/.bun/install/cache",
        "env --argv0 foo bun pm cache rm",
        "xcrun rm -rf ~/.bun/install/cache",
        "bash <<< 'rm -rf ~/.bun/install/cache'",
        "bash -s <<< 'bun pm cache rm'",
        "open -a Docker",
        "open -a OrbStack",
        "open -a 'Docker Desktop'",
        "open -a /Applications/OrbStack.app",
    ],
)
def test_round_six_bypasses_block(command, home, tmp_path, monkeypatch):
    _blocked(command, monkeypatch, tmp_path)


def test_npm_dash_c_redirects_the_lockfile_lookup(bun_repo, home, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _blocked(f"npm -C {bun_repo} install", monkeypatch, elsewhere)


@pytest.mark.parametrize(
    "command",
    [
        "cat <<'NOTES-2'\nrm -rf ~/.bun/install/cache\nNOTES-2",
        "grep x <<< 'rm -rf ~/.bun/install/cache'",
        "bash -c 'echo hi' <<< 'bun pm cache rm'",
        "open -a Safari https://example.com",
        "open .",
    ],
)
def test_round_six_neighbours_allowed(command, home, tmp_path, monkeypatch):
    _allowed(command, monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "cp -s /main/node_modules .nm && mv .nm node_modules",
        "mkdir sub && ln -s /main/node_modules sub",
        "rm -rf ~/.{bun/install/cache,cache/uv}",
        "rm -rf ~/.bun/install/{cache,links}",
        "find ~/.bun/install/{cache,tmp} -delete",
        # oh-my-pi's .omp/commands/review-prs.md form
        'MAIN="$(git rev-parse --show-toplevel)"\nWT="$(pwd)"\nln -snf "$MAIN/target"        "$WT/target"\nln -snf "$MAIN/node_modules"  "$WT/node_modules"',
    ],
)
def test_round_seven_bypasses_block(command, home, tmp_path, monkeypatch):
    _blocked(command, monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    ["npx npm install", "corepack npm ci", "npm exec -- npm install", "pnpm dlx npm install", "npx -y npm@10 ci"],
)
def test_package_runners_launching_npm_block_in_bun_repo(command, bun_repo, home, monkeypatch):
    _blocked(command, monkeypatch, bun_repo)


@pytest.mark.parametrize("command", ["npx tsc --noEmit", "bunx --bun vite", "corepack enable", "rm -rf ~/{a,b}/tmp"])
def test_package_runner_neighbours_allowed(command, bun_repo, home, monkeypatch):
    _allowed(command, monkeypatch, bun_repo)


def test_expand_braces():
    assert psg.expand_braces("a{b,c}d") == ["abd", "acd"]
    assert psg.expand_braces("x{1,{2,3}}") == ["x1", "x2", "x3"]
    assert psg.expand_braces("{nocomma}") == ["{nocomma}"]


def _repo_with_hooks_path(tmp_path: Path, hooks_path: str | None) -> Path:
    repo = tmp_path / "hookrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    if hooks_path is not None:
        subprocess.run(["git", "config", "core.hooksPath", hooks_path], cwd=repo, check=True)
    return repo


@pytest.mark.parametrize(
    "command",
    ["lefthook install", "npx lefthook install", "bunx lefthook add pre-commit", "husky", "npx husky install", "husky init", "npx simple-git-hooks"],
)
def test_hook_managers_blocked_with_a_global_hooks_path(command, home, tmp_path, monkeypatch):
    global_hooks = tmp_path / "global-git-hooks"
    global_hooks.mkdir()
    repo = _repo_with_hooks_path(tmp_path, str(global_hooks))
    msg = _blocked(command, monkeypatch, repo)
    assert "core.hooksPath" in msg and "GIT_CONFIG_KEY_0=core.hooksPath" in msg


def test_hook_managers_allowed_with_a_repo_local_hooks_path(home, tmp_path, monkeypatch):
    _allowed("lefthook install", monkeypatch, _repo_with_hooks_path(tmp_path, None))
    other = tmp_path / "other"
    other.mkdir()
    repo = _repo_with_hooks_path(other, ".husky/_")
    _allowed("npx husky install", monkeypatch, repo)
    outside = tmp_path / "third"
    outside.mkdir()
    repo = _repo_with_hooks_path(outside, str(tmp_path / "global-git-hooks"))
    _allowed(
        'GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0="$(git rev-parse --absolute-git-dir)/hooks" lefthook install',
        monkeypatch,
        repo,
    )
    _allowed("lefthook run pre-commit", monkeypatch, repo)


@pytest.mark.parametrize(
    "command",
    [
        "GIT_CONFIG_KEY_0=core.hooksPath lefthook install",  # no COUNT: git ignores the key
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/elsewhere/hooks lefthook install",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=\"$(cat /tmp/p)\" lefthook install",
        "bun x lefthook install",
        "npx -p lefthook -c 'lefthook install'",
    ],
)
def test_hook_manager_overrides_must_really_stay_in_the_repo(command, home, tmp_path, monkeypatch):
    global_hooks = tmp_path / "global-git-hooks"
    global_hooks.mkdir()
    _blocked(command, monkeypatch, _repo_with_hooks_path(tmp_path, str(global_hooks)))


@pytest.mark.parametrize("command", ["husky --help", "simple-git-hooks --version", "lefthook -h"])
def test_hook_manager_info_flags_allowed(command, home, tmp_path, monkeypatch):
    global_hooks = tmp_path / "global-git-hooks"
    global_hooks.mkdir()
    _allowed(command, monkeypatch, _repo_with_hooks_path(tmp_path, str(global_hooks)))


@pytest.mark.parametrize(
    "command",
    ["npx -c 'rm -rf ~/.bun/install/cache'", "npx --call='bun pm cache rm'", "npm exec -c 'uv cache clean'"],
)
def test_package_runner_call_scripts_are_inspected(command, home, tmp_path, monkeypatch):
    _blocked(command, monkeypatch, tmp_path)


def test_bun_x_is_a_package_runner(bun_repo, home, monkeypatch):
    _blocked("bun x npm install", monkeypatch, bun_repo)


def test_vm_verb_is_the_subcommand_not_a_flag_value(home, tmp_path, monkeypatch):
    _allowed("colima --profile stop status", monkeypatch, tmp_path)
    _allowed("colima --verbose status", monkeypatch, tmp_path)
    _blocked("colima --verbose start", monkeypatch, tmp_path)
    _blocked("colima -p work start --cpu 4", monkeypatch, tmp_path)
    _blocked("open -b com.docker.docker", monkeypatch, tmp_path)


def test_a_variable_command_word_is_its_value(home, tmp_path, monkeypatch):
    _blocked("C=rm; $C -rf ~/.bun/install/cache", monkeypatch, tmp_path)
    _blocked('W="bun pm"; $W cache rm', monkeypatch, tmp_path)


def test_hook_manager_follows_git_dir_and_exported_overrides(home, tmp_path, monkeypatch):
    global_hooks = tmp_path / "global-git-hooks"
    global_hooks.mkdir()
    repo = _repo_with_hooks_path(tmp_path, str(global_hooks))
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    _blocked(f"GIT_DIR={repo}/.git lefthook install", monkeypatch, elsewhere)
    _blocked(f"env GIT_DIR={repo}/.git lefthook install", monkeypatch, elsewhere)
    _allowed(
        'export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath '
        'GIT_CONFIG_VALUE_0="$(git rev-parse --absolute-git-dir)/hooks"; lefthook install',
        monkeypatch,
        repo,
    )
    # a repo-local-looking path that is (or becomes) a link out of the repo is not repo-local
    _blocked(
        f"ln -sfn {global_hooks} .git/hook-link && GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath "
        "GIT_CONFIG_VALUE_0=.git/hook-link lefthook install",
        monkeypatch,
        repo,
    )


def test_mv_into_the_store_is_not_a_removal(home, tmp_path, monkeypatch):
    _allowed("mv -t ~/.bun/install/cache /tmp/foo", monkeypatch, tmp_path)
    _blocked("mv -t /tmp ~/.bun/install/cache", monkeypatch, tmp_path)


# ── node_modules symlinks ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "ln -s /Users/ultra/xp/oh-my-pi/node_modules node_modules",
        "ln -sfn /Users/ultra/xp/oh-my-pi/node_modules node_modules",
        "ln -snf ../../node_modules ./node_modules",
        "ln -sf ../../node_modules node_modules/",
        "ln --symbolic /main/node_modules node_modules",
        "ln -s /main/node_modules .",
        "ln -s /main/node_modules",
        "ln -s -t . /main/node_modules",
        "cd .worktrees/x && ln -s ../../node_modules node_modules && trap 'rm node_modules' EXIT",
        "test -e node_modules || ln -s /Users/ultra/xp/hypercalendarbot/node_modules node_modules",
    ],
)
def test_node_modules_symlink_blocks(command, home, tmp_path, monkeypatch):
    msg = _blocked(command, monkeypatch, tmp_path)
    assert "symlink node_modules" in msg
    assert "bun install --frozen-lockfile" in msg


@pytest.mark.parametrize(
    "command",
    [
        "ln -s ../pkg node_modules/pkg",
        "ln -s ../pkg node_modules/",
        "ln -s /main/node_modules/.bin/tsc ./tsc",
        "ln /a/file hardlink",
        "ln -s ~/.config/foo.toml foo.toml",
    ],
)
def test_other_symlinks_allowed(command, home, tmp_path, monkeypatch):
    _allowed(command, monkeypatch, tmp_path)


# ── npm / pnpm / yarn in bun projects ────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "npm install",
        "npm i",
        "npm ci",
        "npm i -D typescript",
        "npm install --save-dev vitest",
        "npm --loglevel warn install",
        "pnpm install",
        "pnpm i --frozen-lockfile",
        "pnpm add zod",
        "yarn",
        "yarn install",
        "yarn add zod",
        "timeout 600 npm ci",
        "NODE_ENV=development npm install",
    ],
)
def test_foreign_pm_install_in_bun_repo_blocks(command, bun_repo, home, monkeypatch):
    msg = _blocked(command, monkeypatch, bun_repo)
    assert "bun.lock" in msg
    assert "bun install --frozen-lockfile" in msg


def test_npm_install_in_bun_subdir_without_own_lockfile_blocks(bun_repo, home, monkeypatch):
    sub = bun_repo / "packages" / "web"
    sub.mkdir(parents=True)
    (sub / "package.json").write_text("{}\n", encoding="utf-8")
    _blocked("npm install", monkeypatch, sub)


def test_cd_and_prefix_redirect_the_lockfile_lookup(bun_repo, home, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _blocked(f"cd {bun_repo} && npm install", monkeypatch, elsewhere)
    _blocked(f"npm install --prefix {bun_repo}", monkeypatch, elsewhere)
    _blocked(f"npm install --prefix={bun_repo}", monkeypatch, elsewhere)
    _blocked(f"pnpm --dir {bun_repo} install", monkeypatch, elsewhere)
    _blocked(f"pnpm -C {bun_repo} install", monkeypatch, elsewhere)
    _blocked(f"yarn --cwd {bun_repo} install", monkeypatch, elsewhere)


def test_bun_lockb_counts_as_bun(tmp_path, home, monkeypatch):
    repo = tmp_path / "legacy"
    repo.mkdir()
    (repo / "bun.lockb").write_bytes(b"\0")
    _blocked("npm ci", monkeypatch, repo)


def test_bun_lock_beside_package_lock_still_blocks(bun_repo, home, monkeypatch):
    (bun_repo / "package-lock.json").write_text("{}\n", encoding="utf-8")
    _blocked("npm ci", monkeypatch, bun_repo)


def test_npm_only_subproject_inside_bun_repo_allowed(bun_repo, home, monkeypatch):
    sub = bun_repo / "tools" / "legacy-npm"
    sub.mkdir(parents=True)
    (sub / "package.json").write_text("{}\n", encoding="utf-8")
    (sub / "package-lock.json").write_text("{}\n", encoding="utf-8")
    _allowed("npm ci", monkeypatch, sub)
    _allowed("npm install", monkeypatch, sub)


@pytest.mark.parametrize(
    "command",
    [
        "npm i -g @anthropic-ai/claude-code",
        "npm install --global pnpm",
        "npm install --location=global pnpm",
        "npm run build",
        "npm test",
        "npm --version",
        "npx tsc --noEmit",
        "yarn test",
        "yarn --version",
        "yarn run lint",
        "pnpm run build",
        "pnpm dlx create-vite",
        "bun install --frozen-lockfile",
        "bun add zod",
    ],
)
def test_non_install_pm_commands_allowed_in_bun_repo(command, bun_repo, home, monkeypatch):
    _allowed(command, monkeypatch, bun_repo)


def test_npm_install_without_any_lockfile_allowed(tmp_path, home, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    _allowed("npm install", monkeypatch, plain)


def test_npm_only_repo_allowed(tmp_path, home, monkeypatch):
    repo = tmp_path / "npm-repo"
    repo.mkdir()
    (repo / "package-lock.json").write_text("{}\n", encoding="utf-8")
    _allowed("npm ci", monkeypatch, repo)


# ── protocol ──────────────────────────────────────────────────────────────────


def test_bad_event_json_allows(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert psg.main() == 0
    assert json.loads(out.getvalue())["decision"] == "allow"


def test_internal_error_allows(home, tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(psg, "inspect_command", _boom)
    _allowed("npm install", monkeypatch, tmp_path)


def test_script_exit_code_is_protocol_block(home, tmp_path):
    """Run the executable the way a bridge does: exit 10 + a block JSON on stdout."""
    event = {"args": {"command": "bun pm cache rm"}, "cwd": str(tmp_path)}
    res = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
        timeout=30,
    )
    assert res.returncode == 10, res.stderr
    assert json.loads(res.stdout)["decision"] == "block"


def test_descriptor_shape():
    spec = json.loads(_DESCRIPTOR.read_text(encoding="utf-8"))
    assert spec["id"] == "package-store-guard"
    assert spec["point"] == "pre-bash"
    assert spec["cmd"].endswith("/agent-hooks/package-store-guard/package_store_guard.py")
    assert spec["on_error"] == "closed"


def test_script_is_executable():
    if os.name == "nt":
        pytest.skip("POSIX mode bits")
    assert os.access(_HOOK, os.X_OK)


# ── banned container VM runtimes (colima & co.) ─────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "colima start",
        "colima start --cpu 4 --memory 8",
        "colima --profile ci start",
        "colima restart",
        "colima install",
        "limactl start",
        "limactl start default",
        "brew install colima",
        "brew install lima",
        "brew install podman",
        "brew install --cask orbstack",
        "brew install --cask docker-desktop",
        "brew install --cask docker",
        "brew reinstall colima",
        "HOMEBREW_NO_AUTO_UPDATE=1 brew install colima docker",
        "nix run nixpkgs#colima",
        "nix run nixpkgs#colima -- start",
        "nix shell nixpkgs#lima",
        "nix profile install nixpkgs#podman",
        "nix-env -iA nixpkgs.colima",
        "nix-shell -p colima",
        "bash -c 'colima start'",
        "ssh mac 'colima start'",
        "podman machine init",
        "podman machine start",
        "podman --log-level debug machine start podman-machine-default",
    ],
)
def test_vm_runtime_installs_and_starts_block(command, home, tmp_path, monkeypatch):
    msg = _blocked(command, monkeypatch, tmp_path)
    assert "banned" in msg
    assert "Apple `container`" in msg and "mocker" in msg
    assert "fake-gcs-server" in msg


@pytest.mark.parametrize(
    "command",
    [
        "colima status",
        "colima stop",
        "colima delete --force",
        "colima list",
        "limactl list",
        "limactl stop default",
        "brew install jq",
        "brew install docker",
        "brew uninstall colima",
        "brew list",
        "nix run nixpkgs#hello",
        "nix-shell -p jq",
        "container system start",
        "mocker compose up -d",
        "echo done # then: colima start",
        "echo hi # $(bun pm cache rm)",
        "podman machine stop",
        "podman machine rm -f",
        "podman machine list",
        "podman ps",
    ],
)
def test_vm_runtime_neighbours_allowed(command, home, tmp_path, monkeypatch):
    _allowed(command, monkeypatch, tmp_path)


def test_every_vm_start_verb_head_has_its_subcommand_list():
    assert set(psg._VM_SUBCOMMANDS) == set(psg._VM_START_VERBS)


@pytest.mark.parametrize(
    "command",
    [
        "cat <(bun pm cache rm)",
        "wc -l <(find ~/.bun -delete)",
        "echo x | tee >(uv cache clean)",
        "diff <(ls) <(rm -rf ~/.bun/install/cache)",
    ],
)
def test_process_substitution_bodies_are_inspected(command, home, tmp_path, monkeypatch):
    _blocked(command, monkeypatch, tmp_path)


@pytest.mark.parametrize("command", ["diff <(ls a) <(ls b)", 'echo "<(bun pm cache rm)"', "sort < input.txt"])
def test_process_substitution_neighbours_allowed(command, home, tmp_path, monkeypatch):
    _allowed(command, monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "uvx lefthook install",
        "uvx --from lefthook==1.9 lefthook install",
        "uv tool run lefthook install",
        "uv --quiet run lefthook add pre-commit",
        "bun run lefthook install",
        "bun lefthook install",
        "yarn lefthook install",
        "yarn run husky install",
        "pnpm run husky",
        "pnpm husky init",
        "npm run -- simple-git-hooks",
        "pnpm -C . lefthook install",
        "yarn --cwd . lefthook install",
        "uv --project x run lefthook add pre-commit",
        "lefthook install -v",
    ],
)
def test_every_package_runner_reaches_the_hook_manager_check(command, home, tmp_path, monkeypatch):
    global_hooks = tmp_path / "global-git-hooks"
    global_hooks.mkdir()
    _blocked(command, monkeypatch, _repo_with_hooks_path(tmp_path, str(global_hooks)))


@pytest.mark.parametrize("command", ["bun run build", "npm run test -- -x", "uv run pytest -q", "yarn add -D lefthook"])
def test_runner_scripts_that_are_not_hook_managers_stay_allowed(command, home, tmp_path, monkeypatch):
    global_hooks = tmp_path / "global-git-hooks"
    global_hooks.mkdir()
    _allowed(command, monkeypatch, _repo_with_hooks_path(tmp_path, str(global_hooks)))


def test_git_is_asked_once_per_query_per_command(home, tmp_path, monkeypatch):
    """Several hook-manager segments must not multiply git calls past the descriptor timeout."""
    repo = _repo_with_hooks_path(tmp_path, None)  # repo-local: every segment is checked, all allowed
    calls: list[list[str]] = []
    real_run = psg.subprocess.run

    def counting_run(cmd, *a, **k):
        calls.append(cmd)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(psg.subprocess, "run", counting_run)
    assert psg.inspect_command("lefthook install && lefthook add pre-push && npx husky init", str(repo)) is None
    git_calls = [c for c in calls if c and c[0] == "git"]
    assert 0 < len(git_calls) <= 2, git_calls
    # a spent budget asks git nothing: a stalled git cannot outlast the descriptor's timeout
    monkeypatch.setattr(psg, "_GIT_BUDGET_S", 0.0)
    calls.clear()
    psg.inspect_command("lefthook install", str(repo))
    assert not [c for c in calls if c and c[0] == "git"]


def test_a_dotfiles_repo_at_home_does_not_make_the_global_hooks_dir_local(home, monkeypatch):
    """A work tree at $HOME contains ~/.config/git/hooks, rig's global dispatcher dir: a hook
    manager installing there still replaces the composer for every repo."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    global_hooks = home / ".config" / "git" / "hooks"
    global_hooks.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=home, check=True)
    subprocess.run(["git", "config", "core.hooksPath", str(global_hooks)], cwd=home, check=True)
    _blocked("lefthook install", monkeypatch, home)
    subprocess.run(["git", "config", "core.hooksPath", ".husky/_"], cwd=home, check=True)
    _allowed("npx husky install", monkeypatch, home)
