"""Machine-owner policy is configuration, not an ambient per-PR escape hatch."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

MODULE = Path(__file__).resolve().parents[1] / 'ci/ship/rig_external_review_policy.py'

@pytest.fixture
def policy():
    spec = importlib.util.spec_from_file_location('rig_external_review_policy', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.fixture
def config_path(tmp_path):
    root = tmp_path / 'machine' / 'rig'
    root.mkdir(parents=True, mode=0o700)
    # The policy refuses a group/other-writable config dir or parent. mkdir's mode is masked by
    # the process umask and `parents=True` creates `machine/` with the default mode, so under a
    # umask of 0002 (Ubuntu's user-private-group default, e.g. a Forgejo runner user) the tree
    # came out group-writable and every "policy applies" test read `unsafe_parent`. Pin it.
    for directory in (root.parent, root):
        directory.chmod(0o700)
    return root / 'config.yaml'

def write(path, required=False, owners=None):
    owners = ['alex-mextner'] if owners is None else owners
    path.write_text(json.dumps({'ci': {'items': {'ship': {'require_external_review': required, 'external_review_owners': owners}}}}))
    path.chmod(0o600)

@pytest.mark.parametrize('repo', ['alex-mextner/HyperCalendarBot', 'ALEX-MEXTNER/other-project'])
def test_machine_disable_matches_actual_target(policy, config_path, tmp_path, repo):
    write(config_path)
    assert policy.resolve(config_path, repo, tmp_path / 'checkout')['required'] is False

@pytest.mark.parametrize('repo', ['someone-else/project', 'github.example/alex-mextner/project', '../project', ''])
def test_no_invented_foreign_authority(policy, config_path, tmp_path, repo):
    write(config_path)
    assert policy.resolve(config_path, repo, tmp_path / 'checkout')['required'] is True

@pytest.mark.parametrize('required', [True, 'false', 0, None, [], {}])
def test_only_real_false_disables(policy, config_path, tmp_path, required):
    write(config_path, required)
    assert policy.resolve(config_path, 'alex-mextner/project', tmp_path / 'checkout')['required'] is True

@pytest.mark.parametrize('owners', [[], 'alex-mextner', ['*'], [7], ['alex-mextner/project']])
def test_explicit_well_formed_owner_list_required(policy, config_path, tmp_path, owners):
    write(config_path, owners=owners)
    assert policy.resolve(config_path, 'alex-mextner/project', tmp_path / 'checkout')['required'] is True

@pytest.mark.parametrize('content', ['broken: [', 'null', '[]', 'ci: false', 'ci: {items: {ship: false}}', 'ci: {items: {ship: {require_external_review: true, require_external_review: false, external_review_owners: [alex-mextner]}}}'])
def test_malformed_or_duplicate_keys_fail_closed(policy, config_path, tmp_path, content):
    config_path.write_text(content)
    assert policy.resolve(config_path, 'alex-mextner/project', tmp_path / 'checkout')['required'] is True

def test_missing_config_and_env_optout_stay_enabled(policy, config_path, tmp_path, monkeypatch):
    monkeypatch.setenv('SHIP_EXTERNAL_REVIEW', '0')
    assert policy.resolve(config_path, 'alex-mextner/project', tmp_path / 'checkout')['required'] is True

def test_untrusted_permissions_or_symlink_cannot_disable(policy, config_path, tmp_path):
    write(config_path)
    config_path.chmod(0o666)
    assert policy.resolve(config_path, 'alex-mextner/project', tmp_path / 'checkout')['required'] is True
    config_path.chmod(0o600)
    target = config_path.with_name('target.yaml')
    config_path.rename(target)
    config_path.symlink_to(target)
    assert policy.resolve(config_path, 'alex-mextner/project', tmp_path / 'checkout')['required'] is True

def test_config_inside_current_checkout_is_not_machine_authority(policy, config_path):
    write(config_path)
    assert policy.resolve(config_path, 'alex-mextner/project', config_path.parent)['required'] is True

def test_machine_scope_does_not_depend_on_current_checkout_owner(policy, config_path, tmp_path):
    write(config_path)
    assert policy.resolve(config_path, 'someone-else/project', tmp_path / 'alex-mextner')['required'] is True

def test_oversized_config_fails_closed(policy, config_path, tmp_path):
    config_path.write_text('x' * (policy.MAX_CONFIG_BYTES + 1))
    assert policy.resolve(config_path, 'alex-mextner/project', tmp_path / 'checkout')['required'] is True

def test_cli_provenance_and_ambient_env_is_not_a_bypass(config_path, tmp_path):
    write(config_path)
    env = dict(os.environ, XDG_CONFIG_HOME=str(config_path.parent.parent), SHIP_EXTERNAL_REVIEW='0')
    command = [sys.executable, '-I', str(MODULE), 'alex-mextner/project', str(tmp_path / 'checkout')]
    result = subprocess.run(command + ['--json'], capture_output=True, text=True, env=env)
    assert result.returncode == 0
    decision = json.loads(result.stdout)
    assert decision['required'] is False
    assert decision['source'] == str(config_path)
    write(config_path, True)
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    assert result.stdout.strip() == 'enabled'


def test_real_ship_policy_block_uses_actual_target(config_path, tmp_path):
    write(config_path)
    source = MODULE.with_name('ship.sh').read_text()
    block = source.split('# BEGIN RIG MACHINE EXTERNAL REVIEW POLICY', 1)[1].split('# END RIG MACHINE EXTERNAL REVIEW POLICY', 1)[0]
    env = dict(os.environ, XDG_CONFIG_HOME=str(config_path.parent.parent), SHIP_EXTERNAL_REVIEW='0', ROOT=str(tmp_path / 'checkout'), _SHIP_SELF_DIR=str(MODULE.parent))
    for target, expected in [('alex-mextner/project', '0:1'), ('foreign/project', '1:0'), ('', '1:0')]:
        script = 'set -eu\nEXTREV_ENABLED=1\ngh() { printf "%s" "$ACTUAL_TARGET"; }\n' + block + '\nprintf "%s:%s" "$EXTREV_ENABLED" "$EXTREV_RIG_SOURCE"'
        result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, env=dict(env, ACTUAL_TARGET=target))
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected


def test_writable_config_root_cannot_replace_trusted_rig_directory(policy, config_path, tmp_path):
    write(config_path)
    config_path.parent.parent.chmod(0o777)
    assert policy.resolve(config_path, 'alex-mextner/project', tmp_path / 'checkout')['required'] is True


def test_foreign_repo_is_passed_positionally_not_only_as_gh_repo_env(config_path, tmp_path):
    write(config_path)
    source = MODULE.with_name('ship.sh').read_text()
    block = source.split('# BEGIN RIG MACHINE EXTERNAL REVIEW POLICY', 1)[1].split('# END RIG MACHINE EXTERNAL REVIEW POLICY', 1)[0]
    # Real gh 2.92 repo view ignores GH_REPO without an explicit positional repo.
    fake = 'gh() { if [ "$3" = "foreign/project" ]; then echo foreign/project; else echo alex-mextner/local; fi; }\n'
    env = dict(os.environ, XDG_CONFIG_HOME=str(config_path.parent.parent), ROOT=str(tmp_path / 'checkout'), _SHIP_SELF_DIR=str(MODULE.parent), _EFFECTIVE_TARGET='foreign/project', _CWD_ORIGIN_REPO='alex-mextner/local', GH_REPO='foreign/project')
    result = subprocess.run(['bash', '-c', 'set -eu\nEXTREV_ENABLED=1\n' + fake + block + '\necho "$EXTREV_ENABLED"'], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '1'


def test_canonical_rig_path_contract(policy, tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    assert policy.global_config_path() == tmp_path / 'xdg' / 'rig' / 'config.yaml'
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    monkeypatch.setattr(policy.Path, 'home', lambda: tmp_path / 'home')
    assert policy.global_config_path() == tmp_path / 'home' / '.config' / 'rig' / 'config.yaml'


def test_real_jq_filter_excludes_non_github_hosts():
    import re
    import shutil
    jq = shutil.which('jq')
    if not jq:
        pytest.skip('jq CLI unavailable; live gh expression is checked during acceptance')
    source = MODULE.with_name('ship.sh').read_text()
    expression = re.search(r"--json nameWithOwner,url -q '([^']+)'", source).group(1)
    for url, expected in [('https://github.com/alex-mextner/project', 'alex-mextner/project'), ('https://github.example/alex-mextner/project', ''), ('https://github.com.evil/alex-mextner/project', ''), ('https://git.hyperide.ai/alex-mextner/project', '')]:
        result = subprocess.run([jq, '-r', expression], input=json.dumps({'nameWithOwner':'alex-mextner/project','url':url}), capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected


def test_preexisting_optout_does_not_query_github(tmp_path):
    source=MODULE.with_name('ship.sh').read_text()
    block=source.split('# BEGIN RIG MACHINE EXTERNAL REVIEW POLICY',1)[1].split('# END RIG MACHINE EXTERNAL REVIEW POLICY',1)[0]
    sentinel=tmp_path/'queried'
    script='set -eu\nEXTREV_ENABLED=0\ngh() { touch "$SENTINEL"; return 91; }\n'+block+'\nprintf "%s:%s" "$EXTREV_ENABLED" "$EXTREV_RIG_SOURCE"'
    result=subprocess.run(['bash','-c',script],capture_output=True,text=True,env=dict(os.environ,SENTINEL=str(sentinel)))
    assert result.returncode==0,result.stderr
    assert result.stdout=='0:0'
    assert not sentinel.exists()


def test_machine_branch_emits_distinct_audit_event():
    source=MODULE.with_name('ship.sh').read_text()
    begin=source.index('if [ "$EXTREV_ENABLED" = "0" ]; then',source.index('# END RIG MACHINE EXTERNAL REVIEW POLICY'))
    end=source.index('\nelse\n',begin)
    branch=source[begin:end]+'\nfi\n'
    script='set -eu\nEXTREV_ENABLED=0\nEXTREV_RIG_SOURCE=1\nEXTREV_RIG_TARGET=alex-mextner/project\n_external_review_audit_log() { printf "AUDIT:%s:%s\\n" "$1" "$2"; }\n'+branch
    result=subprocess.run(['bash','-c',script],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert 'AUDIT:external-review:disabled:rig-policy:alex-mextner/project' in result.stdout
    assert source.index('_external_review_audit_log()') < source.index('# BEGIN RIG MACHINE EXTERNAL REVIEW POLICY')
    assert source.index('_EFFECTIVE_TARGET="${REPO_FLAG:') < source.index('# BEGIN RIG MACHINE EXTERNAL REVIEW POLICY')


RIG_BLOCK_YAML = """\
version: 1
agent_hooks:
  enabled: true
ci:
  items:
    ship:
      external_review_owners:
      - alex-mextner
      require_external_review: false
"""


def test_block_yaml_policy_applies_without_pyyaml(policy, config_path, tmp_path, monkeypatch):
    """ship runs this under `python3 -I`, which hides the user-site PyYAML most machines have —
    the rig-written block YAML must still apply with no `yaml` module importable at all."""
    monkeypatch.setitem(sys.modules, 'yaml', None)
    config_path.write_text(RIG_BLOCK_YAML)
    config_path.chmod(0o600)
    decision = policy.resolve(config_path, 'alex-mextner/project', tmp_path / 'checkout')
    assert decision['required'] is False, decision


def test_group_writable_parent_is_trusted_only_for_a_private_group(policy, config_path, tmp_path, monkeypatch):
    """Ubuntu's default umask 002 leaves ~/.config/rig group-writable for the user's PRIVATE group."""
    write(config_path)
    config_path.parent.chmod(0o770)
    trust = policy.machine_trust()
    monkeypatch.setattr(trust, '_is_private_group', lambda gid: True)
    assert policy.resolve(config_path, 'alex-mextner/project', tmp_path / 'checkout')['required'] is False
    monkeypatch.setattr(trust, '_is_private_group', lambda gid: False)
    decision = policy.resolve(config_path, 'alex-mextner/project', tmp_path / 'checkout')
    assert decision['required'] is True
    assert decision['reason'] == 'unsafe_parent'
    assert 'group-writable' in decision['detail']


def test_cli_names_why_an_existing_policy_was_not_applied(config_path, tmp_path):
    write(config_path)
    config_path.chmod(0o666)
    env = dict(os.environ, XDG_CONFIG_HOME=str(config_path.parent.parent))
    command = [sys.executable, '-I', str(MODULE), 'alex-mextner/project', str(tmp_path / 'checkout')]
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    assert result.stdout.strip() == 'enabled'
    assert f'Rig machine policy {config_path} not applied (unsafe_config: {config_path} is world-writable)' in result.stderr


def test_cli_is_quiet_when_no_policy_exists(config_path, tmp_path):
    env = dict(os.environ, XDG_CONFIG_HOME=str(config_path.parent.parent))
    command = [sys.executable, '-I', str(MODULE), 'alex-mextner/project', str(tmp_path / 'checkout')]
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    assert (result.stdout.strip(), result.stderr) == ('enabled', '')


def test_ship_block_passes_the_rejection_reason_through(config_path, tmp_path):
    write(config_path)
    config_path.chmod(0o666)
    source = MODULE.with_name('ship.sh').read_text()
    block = source.split('# BEGIN RIG MACHINE EXTERNAL REVIEW POLICY', 1)[1].split('# END RIG MACHINE EXTERNAL REVIEW POLICY', 1)[0]
    env = dict(os.environ, XDG_CONFIG_HOME=str(config_path.parent.parent), ROOT=str(tmp_path / 'checkout'), _SHIP_SELF_DIR=str(MODULE.parent))
    script = 'set -eu\nEXTREV_ENABLED=1\ngh() { echo alex-mextner/project; }\n' + block + '\necho "$EXTREV_ENABLED"'
    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '1'
    assert 'not applied (unsafe_config' in result.stderr
