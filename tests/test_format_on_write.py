"""Tests for the format-on-write agent-hook.

Hermetic: no network, no real formatters. The "does it run a formatter" cases install a
tiny fake executable into a temp repo's node_modules/.bin (so the local-bin preference is
exercised) or monkeypatch the detection table; everything resolves to `allow`.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_format_on_write.py -q
    # or, if pytest is installed:  python -m pytest tests/test_format_on_write.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import stat
import sys
from pathlib import Path

import pytest

# Import the hook module by path (it lives outside any package, next to its descriptor).
_HOOK = (
    Path(__file__).resolve().parents[1]
    / "agent-hooks"
    / "format-on-write"
    / "format_on_write.py"
)
_spec = importlib.util.spec_from_file_location("format_on_write", _HOOK)
assert _spec and _spec.loader
fow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fow)


def _run(event, monkeypatch, env: dict | None = None) -> tuple[str, str, int]:
    """Drive main() with `event` on stdin; return (stdout, stderr, exit_code)."""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    code = fow.main()
    return out.getvalue(), err.getvalue(), code


def _assert_allow(out: str, code: int) -> None:
    assert code == 0
    payload = json.loads(out)
    assert payload == {"hook_api": fow.HOOK_API, "decision": "allow"}
    assert out.endswith("\n"), "protocol line must be newline-terminated"


def _mk_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _install_fake_local_bin(root: Path, tool: str, body: str = "exit 0\n") -> Path:
    binp = root / "node_modules" / ".bin" / tool
    binp.parent.mkdir(parents=True, exist_ok=True)
    binp.write_text("#!/bin/sh\n" + body)
    binp.chmod(binp.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binp


# ── escape hatch ─────────────────────────────────────────────────────────────────────────


def test_escape_hatch_skips_before_touching_anything(tmp_path, monkeypatch):
    f = tmp_path / "a.go"
    f.write_text("package main\nfunc  main(){}\n")
    before = f.read_text()
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch, {"NO_FORMAT_HOOK": "1"})
    _assert_allow(out, code)
    assert "NO_FORMAT_HOOK=1" in err
    assert f.read_text() == before  # untouched


# ── always-allow failure modes ─────────────────────────────────────────────────────────────


def test_non_dict_event_allows(tmp_path, monkeypatch):
    out, err, code = _run([1, 2, 3], monkeypatch)  # type: ignore[arg-type]
    _assert_allow(out, code)
    assert "not a JSON object" in err


def test_missing_file_allows(tmp_path, monkeypatch):
    out, err, code = _run({"args": {"path": str(tmp_path / "ghost.go")}}, monkeypatch)
    _assert_allow(out, code)
    assert "no written file" in err


def test_unmapped_extension_is_noop(tmp_path, monkeypatch):
    f = tmp_path / "notes.txt"
    f.write_text("hi\n")
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert "no formatter mapping for '.txt'" in err


def test_no_path_in_event_allows(tmp_path, monkeypatch):
    out, err, code = _run({"args": {}}, monkeypatch)
    _assert_allow(out, code)


# ── path resolution ─────────────────────────────────────────────────────────────────────────


def test_resolve_path_prefers_keys_in_order(tmp_path):
    p = fow.resolve_path({"args": {"path": "/x/a.ts", "file_path": "/y/b.ts"}})
    assert p == Path("/x/a.ts")
    p2 = fow.resolve_path({"args": {"filePath": "/z/c.ts"}})
    assert p2 == Path("/z/c.ts")
    p3 = fow.resolve_path({"path": "/top/d.ts"})
    assert p3 == Path("/top/d.ts")


def test_resolve_relative_path_against_cwd():
    p = fow.resolve_path({"args": {"path": "src/a.ts"}, "cwd": "/proj"})
    assert p == Path("/proj/src/a.ts")


def test_resolve_path_none_when_blank_or_non_string():
    assert fow.resolve_path({"args": {"path": ""}}) is None
    assert fow.resolve_path({"args": {"path": 123}}) is None


# ── detection table ─────────────────────────────────────────────────────────────────────────


def test_table_js_ts_candidate_order():
    # biome only where it can format; then oxfmt, then prettier
    assert [f.tool for f in fow.TABLE[".ts"]] == ["biome", "oxfmt", "prettier"]
    assert [f.tool for f in fow.TABLE[".md"]] == ["oxfmt", "prettier"]
    assert [f.tool for f in fow.TABLE[".html"]] == ["biome", "oxfmt", "prettier"]


def test_table_python_candidate_order():
    cands = fow.TABLE[".py"]
    # ruff first, then black
    assert [f.tool for f in cands] == ["ruff", "black"]
    assert cands[0].argv("ruff", "x.py") == ["ruff", "format", "x.py"]
    assert cands[1].argv("black", "x.py") == ["black", "-q", "x.py"]


def test_ruff_global_requires_config(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    monkeypatch.setattr(fow, "has_global", lambda tool: tool in ("ruff", "black"))
    # no ruff config -> a globally-installed ruff is NOT selected (a Black-configured repo must
    # not be silently reformatted by ruff just because ruff is on PATH).
    fmt, cmd = fow.select_formatter(root, root / "a.py")
    assert (fmt.tool, cmd) == ("black", "black")
    # a ruff.toml makes ruff the configured formatter -> global ruff is now used.
    (root / "ruff.toml").write_text("line-length = 100\n")
    fmt, cmd = fow.select_formatter(root, root / "a.py")
    assert (fmt.tool, cmd) == ("ruff", "ruff")


def test_ruff_config_via_pyproject_tool_section(tmp_path):
    root = _mk_repo(tmp_path)
    (root / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["E"]\n')
    assert fow._ruff_configured(root) is True
    # a pyproject with only [tool.black] does NOT count as ruff config
    (root / "pyproject.toml").write_text("[tool.black]\nline-length = 88\n")
    assert fow._ruff_configured(root) is False


def test_table_go_and_rust():
    assert fow.TABLE[".go"][0].argv("gofmt", "x.go") == ["gofmt", "-w", "x.go"]
    assert fow.TABLE[".rs"][0].argv("rustfmt", "x.rs") == ["rustfmt", "x.rs"]


def test_local_bin_preferred_over_global(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    (root / ".oxfmtrc.json").write_text("{}")
    binp = _install_fake_local_bin(root, "oxfmt")
    # pretend a global oxfmt ALSO exists — local must still win
    monkeypatch.setattr(fow, "has_global", lambda tool: True)
    assert fow.select_formatter(root, root / "a.ts")[1] == str(binp)


def test_configured_formatter_falls_back_to_global(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    (root / "package.json").write_text('{"devDependencies":{"prettier":"^3"}}')
    # configured but NOT installed → selected, with no runnable command
    monkeypatch.setattr(fow, "has_global", lambda tool: False)
    fmt, cmd = fow.select_formatter(root, root / "a.ts")
    assert (fmt.tool, cmd) == ("prettier", None)
    # configured AND on PATH → the bare global name
    monkeypatch.setattr(fow, "has_global", lambda tool: True)
    assert fow.select_formatter(root, root / "a.ts")[1] == "prettier"


def test_no_configured_formatter_is_noop(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    f = root / "a.ts"
    f.write_text("const x=1\n")
    # neither local bins nor globals exist
    monkeypatch.setattr(fow, "has_global", lambda tool: False)
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert "no configured/available formatter" in err


# ── end-to-end with a fake local formatter ─────────────────────────────────────────────────


def test_runs_local_formatter_on_written_file(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    (root / ".oxfmtrc.json").write_text("{}")
    src = root / "src"
    src.mkdir()
    f = src / "a.ts"
    f.write_text("const   x=1\n")
    # fake oxfmt: rewrite the (last-arg) file to a marker so we can prove it ran
    _install_fake_local_bin(
        root, "oxfmt", body='eval "f=\\${$#}"\nprintf MARKER > "$f"\nexit 0\n'
    )
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert "formatted a.ts with oxfmt" in err
    assert f.read_text() == "MARKER"  # the local bin was the one invoked


def test_formatter_nonzero_exit_still_allows(tmp_path, monkeypatch):
    root = _mk_repo(tmp_path)
    (root / ".oxfmtrc.json").write_text("{}")
    f = root / "a.ts"
    f.write_text("const x=1\n")
    _install_fake_local_bin(root, "oxfmt", body='echo "boom" >&2\nexit 2\n')
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)  # never blocks, even on a formatter error
    assert "exited 2" in err


# ── the repo's OWN formatter config decides, never mere availability (agent-tools#901) ─────


def _install_marking_bin(root: Path, tool: str) -> Path:
    """A fake local formatter that overwrites the (last-arg) file with its own name."""
    return _install_fake_local_bin(
        root, tool, body=f'eval "f=\\${{$#}}"\nprintf {tool.upper()} > "$f"\nexit 0\n'
    )


def _mk_biome_and_prettier_repo(tmp_path: Path) -> Path:
    """The conloca shape: biome owns JS/TS/JSON/CSS, prettier (devDependency + .prettierrc
    with trailingComma es5) owns the rest — and BOTH bins sit in node_modules/.bin."""
    root = _mk_repo(tmp_path)
    (root / "biome.json").write_text("{}\n")
    (root / ".prettierrc").write_text('{"trailingComma": "es5"}\n')
    (root / "package.json").write_text(
        '{"devDependencies":{"@biomejs/biome":"^2","prettier":"^3"}}'
    )
    _install_marking_bin(root, "biome")
    _install_marking_bin(root, "prettier")
    return root


@pytest.mark.parametrize("name", ["a.ts", "a.tsx", "a.js", "a.json", "a.css"])
def test_biome_repo_formats_js_ts_with_biome_not_prettier(tmp_path, monkeypatch, name):
    root = _mk_biome_and_prettier_repo(tmp_path)
    monkeypatch.setattr(fow, "has_global", lambda tool: False)
    f = root / name
    f.write_text("x\n")
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert f.read_text() == "BIOME", err


@pytest.mark.parametrize("name", ["README.md", "ci.yaml", "page.html"])
def test_biome_repo_routes_files_biome_cannot_format_to_prettier(tmp_path, monkeypatch, name):
    root = _mk_biome_and_prettier_repo(tmp_path)
    monkeypatch.setattr(fow, "has_global", lambda tool: False)
    f = root / name
    f.write_text("x\n")
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert f.read_text() == "PRETTIER", err


@pytest.mark.parametrize("tool", ["prettier", "oxfmt", "biome"])
def test_unconfigured_local_bin_alone_is_noop(tmp_path, monkeypatch, tool):
    # A transitive dependency drops node_modules/.bin/<tool> into repos that never chose it.
    root = _mk_repo(tmp_path)
    _install_marking_bin(root, tool)
    monkeypatch.setattr(fow, "has_global", lambda t: True)
    f = root / "a.ts"
    f.write_text("x\n")
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert f.read_text() == "x\n"
    assert "no configured/available formatter" in err


def test_configured_but_missing_formatter_never_falls_through(tmp_path, monkeypatch):
    # biome is the repo's JS/TS formatter but is not installed: prettier (configured for
    # Markdown, installed) must NOT step in and reformat the .ts file in its own style.
    root = _mk_repo(tmp_path)
    (root / "biome.json").write_text("{}\n")
    (root / ".prettierrc").write_text("{}\n")
    _install_marking_bin(root, "prettier")
    monkeypatch.setattr(fow, "has_global", lambda tool: False)
    f = root / "a.ts"
    f.write_text("x\n")
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert f.read_text() == "x\n"
    assert "biome" in err and "not installed" in err


@pytest.mark.parametrize(
    "setup",
    [
        {".prettierrc": "{}"},
        {".prettierrc.json": "{}"},
        {".prettierrc.yaml": "semi: false\n"},
        {"prettier.config.js": "export default {}\n"},
        {".prettierrc.mjs": "export default {}\n"},
        {"package.json": '{"prettier": {"semi": false}}'},
        {"package.json": '{"devDependencies": {"prettier": "^3"}}'},
        {"package.json": '{"dependencies": {"prettier": "^3"}}'},
    ],
)
def test_prettier_configured_signals(tmp_path, setup):
    root = _mk_repo(tmp_path)
    for name, body in setup.items():
        (root / name).write_text(body)
    assert fow._prettier_configured(root) is True


@pytest.mark.parametrize(
    "pkg",
    [
        '{"name": "not-prettier-at-all"}',
        '{"devDependencies": {"eslint-config-prettier": "^9"}}',
        "not json",
        "[]",
    ],
)
def test_prettier_not_configured_by_a_mere_mention(tmp_path, pkg):
    root = _mk_repo(tmp_path)
    (root / "package.json").write_text(pkg)
    assert fow._prettier_configured(root) is False


@pytest.mark.parametrize(
    "setup",
    [
        {".oxfmtrc.json": "{}"},
        {".oxfmtrc.jsonc": "{}"},
        {"oxfmt.config.ts": "export default {}\n"},
        {"oxfmt.config.mts": "export default {}\n"},
        {"package.json": '{"devDependencies": {"oxfmt": "^0.1"}}'},
    ],
)
def test_oxfmt_configured_signals(tmp_path, setup):
    root = _mk_repo(tmp_path)
    for name, body in setup.items():
        (root / name).write_text(body)
    assert fow._oxfmt_configured(root) is True


@pytest.mark.parametrize("name", ["biome.json", "biome.jsonc", ".biome.json", ".biome.jsonc"])
def test_biome_configured_by_its_config_file(tmp_path, name):
    root = _mk_repo(tmp_path)
    assert fow._biome_formats(root, root / "a.ts") is False
    (root / name).write_text("{}\n")
    assert fow._biome_formats(root, root / "a.ts") is True


_SCHEMA = '"$schema": "https://biomejs.dev/schemas/2.5.12/schema.json"'


@pytest.mark.parametrize(
    ("config", "formats"),
    [
        # lint-only biome: the formatter is switched off globally
        ('{%s, "formatter": {"enabled": false}}' % _SCHEMA,
         {".ts": False, ".json": False, ".css": False}),
        # JSONC: comments (incl. a "//" inside a string) and trailing commas still parse
        ('{\n  // lint only\n  %s, /* no fmt */\n  "formatter": {"enabled": false,},\n}\n' % _SCHEMA,
         {".ts": False, ".json": False}),
        # per-language switches
        ('{"javascript": {"formatter": {"enabled": false}}}',
         {".ts": False, ".tsx": False, ".mjs": False, ".json": True, ".css": True}),
        ('{"json": {"formatter": {"enabled": false}}}',
         {".ts": True, ".json": False, ".jsonc": False}),
        ('{"css": {"formatter": {"enabled": false}}}', {".css": False, ".ts": True}),
        # the language section wins over the global switch, as in biome
        ('{"formatter": {"enabled": false}, "javascript": {"formatter": {"enabled": true}}}',
         {".ts": True, ".json": False}),
        ('{"formatter": {"enabled": true}, "css": {"formatter": {"enabled": false}}}',
         {".css": False, ".ts": True}),
        # explicitly enabled / unrelated keys → biome formats
        ('{"formatter": {"enabled": true, "indentWidth": 2}}', {".ts": True, ".css": True}),
        # unreadable config: biome is still the repo's tool (biome itself reports the error)
        ("{not json", {".ts": True, ".html": False}),
        # HTML is opt-in in biome: biome's only when the config turns it on
        ("{}", {".html": False, ".ts": True}),
        ('{"html": {"formatter": {"enabled": true}}}', {".html": True}),
        ('{"html": {"formatter": {"enabled": false}}}', {".html": False}),
        ('{"overrides": [{"includes": ["web/**"], "html": {"formatter": {"enabled": true}}}]}',
         {".html": True}),
        # an inherited config may enable HTML: biome keeps it
        ('{"extends": ["./shared.json"]}', {".html": True, ".ts": True}),
        # a global formatter override does not enable opt-in HTML
        ('{"overrides": [{"includes": ["**"], "formatter": {"indentWidth": 4}}]}',
         {".html": False, ".ts": True}),
        # settings only biome can resolve keep the file with biome — at worst it formats
        # nothing, but prettier never takes over a file biome may own
        ('{"formatter": {"enabled": false}, "overrides": '
         '[{"includes": ["src/**"], "formatter": {"enabled": true}}]}',
         {".ts": True, ".css": True}),
        ('{"javascript": {"formatter": {"enabled": false}}, "overrides": '
         '[{"includes": ["src/**"], "javascript": {"formatter": {"enabled": true}}}]}',
         {".ts": True, ".json": True}),
        ('{"extends": ["./shared.json"], "formatter": {"enabled": false}}', {".ts": True}),
        ('{"root": false, "formatter": {"enabled": false}}', {".ts": True}),
        # overrides that only tune the linter do not shield a lint-only config
        ('{"formatter": {"enabled": false}, "overrides": '
         '[{"includes": ["**"], "linter": {"enabled": true}}]}',
         {".ts": False}),
    ],
)
def test_biome_respects_its_formatter_switches(tmp_path, config, formats):
    root = _mk_repo(tmp_path)
    (root / "biome.jsonc").write_text(config)
    assert {ext: fow._biome_formats(root, root / f"a{ext}") for ext in formats} == formats


def test_nested_biome_config_keeps_files_below_it_with_biome(tmp_path):
    # a package-level biome config may re-enable what the lint-only root switched off
    root = _mk_repo(tmp_path)
    (root / "biome.json").write_text('{"formatter": {"enabled": false}}')
    pkg = root / "packages" / "app"
    (pkg / "src").mkdir(parents=True)
    (pkg / "biome.json").write_text('{"extends": "//", "formatter": {"enabled": true}}')
    assert fow._biome_formats(root, pkg / "src" / "a.ts") is True
    # files outside that package still follow the root's lint-only config
    assert fow._biome_formats(root, root / "tools" / "b.ts") is False
    # the nested config may opt HTML in too; outside it HTML stays off (opt-in)
    assert fow._biome_formats(root, pkg / "src" / "index.html") is True
    assert fow._biome_formats(root, root / "tools" / "index.html") is False


def test_lint_only_biome_repo_lets_prettier_format_ts(tmp_path, monkeypatch):
    root = _mk_biome_and_prettier_repo(tmp_path)
    (root / "biome.json").write_text('{"formatter": {"enabled": false}}')
    monkeypatch.setattr(fow, "has_global", lambda tool: False)
    f = root / "a.ts"
    f.write_text("x\n")
    out, err, code = _run({"args": {"path": str(f)}}, monkeypatch)
    _assert_allow(out, code)
    assert f.read_text() == "PRETTIER", err


def test_outside_a_repo_nothing_js_is_configured():
    assert fow._biome_formats(None, Path("/nowhere/a.ts")) is False
    assert fow._prettier_configured(None) is False
    assert fow._oxfmt_configured(None) is False


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
