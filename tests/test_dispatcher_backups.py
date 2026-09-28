"""An executable Rig backup is rollback data, never another installed hook."""
import os
import subprocess
from pathlib import Path
import pytest

RUNNER = Path(__file__).resolve().parents[1] / 'git-hooks/global-dispatcher/run-global-hooks'

@pytest.mark.parametrize('event', ['pre-commit', 'pre-push', 'post-commit'])
def test_dispatcher_does_not_execute_rig_backups(tmp_path, event):
    directory = tmp_path / 'fragments' / event
    directory.mkdir(parents=True)
    for name, body in {
        '10-current': 'echo current\n',
        '10-current.rig-bak-20260927-233226': 'echo obsolete; exit 71\n',
        '11-deleted.rig-bak-20260927-233226': 'echo obsolete; exit 72\n',
        '20-backup-audit': 'echo legitimate\n',
    }.items():
        fragment = directory / name
        fragment.write_text('#!/bin/sh\n' + body)
        fragment.chmod(0o755)
    env = dict(os.environ, GLOBAL_HOOKS_DIR=str(directory.parent), GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run([str(RUNNER), event], cwd=tmp_path, env=env,
                            input='', text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['current', 'legitimate']


def test_protected_push_advice_does_not_route_forgejo_users_to_github(tmp_path):
    hook = RUNNER.parent / 'global-hooks.d' / 'pre-push' / '10-protect-main'
    env = dict(os.environ, PUSH_MAIN_OK='0')
    refs = f"refs/heads/topic {'1' * 40} refs/heads/main {'2' * 40}\n"
    result = subprocess.run([str(hook), 'origin', 'https://git.hyperide.ai/ultrabricks/example.git'],
                            cwd=tmp_path, env=env, input=refs, text=True, capture_output=True, timeout=5)
    assert result.returncode == 1
    assert 'BLOCKED' in result.stderr
    assert 'gh pr create' not in result.stderr
    assert 'fj ship' in result.stderr
