"""The `linters/prose/` carrier `rig lint text` reads must stay loadable and self-consistent."""

from __future__ import annotations

import configparser
import functools
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROSE = ROOT / "linters" / "prose"
DICT_FILES = sorted((PROSE / "dict").glob("*.json"))
REQUIRED_RULE_KEYS = {"id", "pattern", "flags", "replacement", "why", "examples"}


def _vale_ini() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # rule names are case-sensitive (`write-good.E-Prime`)
    # vale.ini's top-level keys have no section; configparser needs one to parse them.
    parser.read_string("[root]\n" + (PROSE / "vale.ini").read_text(encoding="utf-8"))
    return parser


def _chat_dict_gate():
    """The chat-dictionary-gate hook, the reference reader of the DICT.json rule format.

    Reuses the module when test_chat_dictionary_gate.py already loaded it under the same name.
    Registered in sys.modules before exec: the hook's dataclasses resolve through it.
    """
    name = "chat_dict_gate"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "agent-hooks" / "chat-dictionary-gate" / "chat_dict_gate.py")
    if spec is None or spec.loader is None:
        pytest.fail("cannot load agent-hooks/chat-dictionary-gate/chat_dict_gate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _compile(rule: dict) -> re.Pattern[str]:
    """Compile with the gate's own flag mapping, so the tested regex is the one that runs."""
    return re.compile(rule["pattern"], _chat_dict_gate()._map_flags(rule["flags"], rule["id"]))


@functools.lru_cache(maxsize=None)
def _load_pack(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_carrier_files_exist():
    for name in ("vale.ini", "languagetool.json", "prompt.md", "README.md", "dict/NOTICE"):
        assert (PROSE / name).is_file(), name
    assert DICT_FILES, "expected at least one dictionary pack in linters/prose/dict/"


def test_vale_packages_are_pinned_release_urls_for_exactly_the_enabled_styles():
    ini = _vale_ini()
    packages = [p.strip() for p in ini["root"]["Packages"].split(",")]
    assert packages
    for url in packages:
        assert re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/releases/download/v[\d.]+/[\w.-]+\.zip", url), url
    # Each package zip is named after the style it installs (ai-tells.zip -> styles/ai-tells).
    stems = {url.rsplit("/", 1)[1].removesuffix(".zip") for url in packages}
    assert stems == {s.strip() for s in ini["*.md"]["BasedOnStyles"].split(",")}


def test_vale_toggles_only_disable_rules_of_enabled_styles():
    ini = _vale_ini()
    styles = {s.strip() for s in ini["*.md"]["BasedOnStyles"].split(",")}
    toggles = {k: v for k, v in ini["*.md"].items() if k != "BasedOnStyles"}
    assert toggles, "expected at least one disabled rule"
    for key, value in toggles.items():
        assert key.split(".", 1)[0] in styles, key
        assert value == "NO", key


def test_languagetool_disable_lists_match_the_declared_languages():
    data = json.loads((PROSE / "languagetool.json").read_text(encoding="utf-8"))
    assert set(data["languages"]) == {"en", "ru"}
    assert set(data["disable"]) == set(data["languages"].values())
    for code, ids in data["disable"].items():
        assert ids and all(re.fullmatch(r"[A-Z0-9_]+", i) for i in ids), code


def test_prompt_has_both_placeholders_and_the_draft_markers():
    prompt = (PROSE / "prompt.md").read_text(encoding="utf-8")
    assert prompt.count("{draft}") == 1 and prompt.count("{findings}") == 1
    assert "FINDINGS" in prompt and "REVISED DRAFT" in prompt
    assert "<<<DRAFT" in prompt and "DRAFT>>>" in prompt


@pytest.mark.parametrize("path", DICT_FILES, ids=lambda p: p.name)
def test_dictionary_pack_rules_are_complete_and_unique(path: Path):
    data = _load_pack(path)
    assert data["version"] == 1
    assert data["rules"], f"{path.name}: expected at least one rule"
    ids = [rule["id"] for rule in data["rules"]]
    assert len(ids) == len(set(ids)), "duplicate rule id"
    for rule in data["rules"]:
        assert REQUIRED_RULE_KEYS <= rule.keys(), rule["id"]
        for key in ("pattern", "replacement", "why"):
            assert isinstance(rule[key], str) and rule[key].strip(), f"{rule['id']}: {key} must be a non-empty string"
        examples = rule["examples"]
        assert isinstance(examples, dict), rule["id"]
        for kind in ("hit", "miss"):
            assert isinstance(examples.get(kind), list) and examples[kind], f"{rule['id']}: {kind} must be a non-empty list"
            assert all(isinstance(text, str) for text in examples[kind]), f"{rule['id']}: {kind} must hold strings"


@pytest.mark.parametrize(
    "rule",
    [rule for path in DICT_FILES for rule in _load_pack(path)["rules"]],
    ids=lambda r: r["id"],
)
def test_dictionary_rule_compiles_and_matches_its_examples(rule: dict):
    pattern = _compile(rule)
    for text in rule["examples"]["hit"]:
        assert pattern.search(text), f"{rule['id']} should match: {text}"
    for text in rule["examples"]["miss"]:
        assert not pattern.search(text), f"{rule['id']} should not match: {text}"
