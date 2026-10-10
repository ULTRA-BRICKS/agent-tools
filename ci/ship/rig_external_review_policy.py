#!/usr/bin/env python3
"""Read the trusted Rig machine policy for ship's external-review gate.

Only the exact configured target owner may opt out. This is independent of
review verdicts and never disables CI, local tests, permissions or thread checks.

ship.sh runs this under `python3 -I`, so the only project code it loads is the
stdlib-only hatch lib, by FIXED path (never a by-name import a PYTHONPATH could
hijack): its `path_trust_problem` permission predicate and its PyYAML-free
`machine_config_mapping` reader. `-I` hides user-site packages, which is where
PyYAML usually lives, so depending on PyYAML here made every owner's policy
silently inert.

When a policy file exists but is not applied, one line naming the reason goes
to stderr (ship.sh passes it through); stdout stays the bare decision.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys

MAX_CONFIG_BYTES = 262144
OWNER = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z')
REPOSITORY = re.compile(r'([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+)\Z')
HATCH_LIB = Path(__file__).resolve().parents[2] / 'lib' / 'agenttools_hatch_escalation' / '__init__.py'
# Reasons that mean "no policy to apply" or "applied as written" — nothing for the shipper to fix.
QUIET_REASONS = frozenset({'not_configured', 'invalid_target', 'required_or_owner_not_opted_out', 'explicit_machine_owner_policy'})


def global_config_path() -> Path:
    """Same canonical location as riglib.config.global_config_path()."""
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'rig/config.yaml'


def machine_trust():
    """The hatch lib, loaded from its fixed path under a private module name (registered in
    sys.modules first: its dataclasses resolve their module through it)."""
    name = '_rig_policy_machine_trust'
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HATCH_LIB)
    if spec is None or spec.loader is None:
        raise ImportError(f'cannot load {HATCH_LIB}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def resolve(path: Path, repository: str, checkout_root: Path) -> dict:
    result = {'required': True, 'source': str(path), 'repository': repository, 'reason': 'not_configured'}
    match = REPOSITORY.fullmatch(repository)
    if not match or not OWNER.fullmatch(match[1]) or match[2] in ('.', '..'):
        result['reason'] = 'invalid_target'
        return result
    try:
        # A missing file is "no policy" whatever its location; one that exists inside the
        # checkout is reported (not_machine_config), since the shipper put it there to apply.
        if not os.path.lexists(path):
            return result
        if not path.is_absolute() or path.resolve().is_relative_to(checkout_root.resolve()):
            result['reason'] = 'not_machine_config'
            return result
        # Loaded here, by fixed path, never as a top-level `import yaml`-style dependency:
        # `python3 -I` hides user-site packages, which is exactly how this reader broke before.
        trust = machine_trust()
        for directory in (path.parent, path.parent.parent):
            problem = trust.path_trust_problem(directory, directory.stat())
            if problem:
                result.update(reason='unsafe_parent', detail=problem)
                return result
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            problem = None if stat.S_ISREG(info.st_mode) else f'{path} is not a regular file'
            problem = problem or trust.path_trust_problem(path, info)
            if problem:
                result.update(reason='unsafe_config', detail=problem)
                return result
            if info.st_size > MAX_CONFIG_BYTES:
                result['reason'] = 'oversized_config'
                return result
            with os.fdopen(os.dup(fd), 'rb') as stream:
                raw = stream.read(MAX_CONFIG_BYTES + 1)
            if len(raw) > MAX_CONFIG_BYTES:
                result['reason'] = 'oversized_config'
                return result
        finally:
            os.close(fd)
        try:
            value = trust.machine_config_mapping(raw.decode('utf-8'), ('ci', 'items', 'ship'))
        except trust.MachineConfigError as exc:
            # The reader's messages carry line numbers and key names, never values.
            result.update(reason='invalid_config', detail=str(exc))
            return result
        value = {} if value is None else value
        required = value.get('require_external_review', True)
        owners = value.get('external_review_owners', [])
        if type(required) is not bool or not isinstance(owners, list):
            result['reason'] = 'invalid_policy'
            return result
        if not all(isinstance(owner, str) and OWNER.fullmatch(owner) for owner in owners):
            result['reason'] = 'invalid_owner_scope'
            return result
        if not required and match[1].casefold() in {owner.casefold() for owner in owners}:
            result.update(required=False, reason='explicit_machine_owner_policy')
        else:
            result['reason'] = 'required_or_owner_not_opted_out'
    except Exception as exc:
        # Missing helper, undecodable bytes, symlink or permission failure is absence of
        # authority, never a bypass. Never log arbitrary config values — only the error class.
        result.update(reason='unreadable_or_invalid_config', detail=type(exc).__name__)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository')
    parser.add_argument('checkout_root', type=Path)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    decision = resolve(global_config_path(), args.repository, args.checkout_root)
    if decision['reason'] not in QUIET_REASONS:
        detail = f": {decision['detail']}" if decision.get('detail') else ''
        print(
            f"[ship] Rig machine policy {decision['source']} not applied ({decision['reason']}{detail}); "
            'the external-review gate stays enabled.',
            file=sys.stderr,
        )
    print(json.dumps(decision) if args.json else ('enabled' if decision['required'] else 'disabled'))


if __name__ == '__main__':
    main()
