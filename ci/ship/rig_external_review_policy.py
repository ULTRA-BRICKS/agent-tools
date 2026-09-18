#!/usr/bin/env python3
"""Read the trusted Rig machine policy without importing any project code.

Only the exact configured target owner may opt out. This is independent of
review verdicts and never disables CI, local tests, permissions or thread checks.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat

MAX_CONFIG_BYTES = 262144
OWNER = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z')
REPOSITORY = re.compile(r'([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+)\Z')


def global_config_path() -> Path:
    """Same canonical location as riglib.config.global_config_path()."""
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'rig/config.yaml'


def resolve(path: Path, repository: str, checkout_root: Path) -> dict:
    result = {'required': True, 'source': str(path), 'repository': repository, 'reason': 'not_configured'}
    match = REPOSITORY.fullmatch(repository)
    if not match or not OWNER.fullmatch(match[1]) or match[2] in ('.', '..'):
        result['reason'] = 'invalid_target'
        return result
    try:
        if not path.is_absolute() or path.resolve().is_relative_to(checkout_root.resolve()):
            result['reason'] = 'not_machine_config'
            return result
        for directory in (path.parent, path.parent.parent):
            parent = directory.stat()
            if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
                result['reason'] = 'unsafe_parent'
                return result
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                result['reason'] = 'unsafe_config'
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
        import yaml

        class UniqueSafeLoader(yaml.SafeLoader):
            def construct_mapping(self, node, deep=False):
                self.flatten_mapping(node)
                seen = set()
                for key_node, _ in node.value:
                    key = self.construct_object(key_node, deep=deep)
                    if key in seen:
                        raise ValueError('Duplicate mapping key')
                    seen.add(key)
                return super().construct_mapping(node, deep=deep)

        value = yaml.load(raw, Loader=UniqueSafeLoader)
        for key in ('ci', 'items', 'ship'):
            if not isinstance(value, dict):
                result['reason'] = 'invalid_config'
                return result
            value = value.get(key, {})
        if not isinstance(value, dict):
            result['reason'] = 'invalid_config'
            return result
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
    except Exception:
        # Missing dependency/file, malformed YAML, symlink or permission failure
        # is absence of authority, never a bypass. Never log arbitrary YAML values.
        result['reason'] = 'unreadable_or_invalid_config'
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository')
    parser.add_argument('checkout_root', type=Path)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    decision = resolve(global_config_path(), args.repository, args.checkout_root)
    print(json.dumps(decision) if args.json else ('enabled' if decision['required'] else 'disabled'))


if __name__ == '__main__':
    main()
