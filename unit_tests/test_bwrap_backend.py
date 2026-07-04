"""
Unit tests for backend/tools/lib/backends/bwrap_backend.py

All tests are pure-host (no bwrap execution), so they run on macOS too:
hostname sanitization, path mapping, bwrap argv construction, availability
guard, host-side file I/O round-trips, and registry selection via the
SANDBOX_BACKEND config.
"""

import os
import sys

from backend.tools.lib.backends import bwrap_backend
from backend.tools.lib.backends.bwrap_backend import BwrapBackend, _sanitize_hostname


# ---------------------------------------------------------------------------
# _sanitize_hostname
# ---------------------------------------------------------------------------

def test_sanitize_hostname_basic():
    assert _sanitize_hostname('My Agent') == 'my-agent'
    assert _sanitize_hostname('agent_01!') == 'agent-01'


def test_sanitize_hostname_unicode_and_empty():
    assert _sanitize_hostname('ägent ünïcode') == 'gent-n-code'
    assert _sanitize_hostname('') == 'agent'
    assert _sanitize_hostname('___') == 'agent'


def test_sanitize_hostname_truncates_to_63():
    assert len(_sanitize_hostname('a' * 100)) == 63


# ---------------------------------------------------------------------------
# resolve_path / _to_host
# ---------------------------------------------------------------------------

def test_resolve_path_maps_workspace(tmp_path):
    ws = str(tmp_path)
    b = BwrapBackend(session_id='s1', workspace=ws)
    assert b.resolve_path(os.path.join(ws, 'a', 'b.txt')) == '/workspace/a/b.txt'
    assert b.resolve_path('/etc/hosts') == '/etc/hosts'


def test_to_host_mappings(tmp_path):
    ws = str(tmp_path)
    b = BwrapBackend(session_id='s1', workspace=ws)
    assert b._to_host('/workspace/x/y.txt') == os.path.join(ws, 'x/y.txt')
    assert b._to_host('/workspace') == ws
    assert b._to_host('/home/agent/.npmrc') == os.path.join(ws, '.home', '.npmrc')
    assert b._to_host('/home/agent') == os.path.join(ws, '.home')
    assert b._to_host('/etc/hosts') == '/etc/hosts'


def test_file_io_round_trip(tmp_path):
    ws = str(tmp_path)
    b = BwrapBackend(session_id='s1', workspace=ws)
    assert b.write_file('/workspace/a.txt', 'hello') == {'ok': True}
    assert (tmp_path / 'a.txt').read_text() == 'hello'
    assert b.read_file('/workspace/a.txt') == {'content': 'hello'}
    assert b.file_exists('/workspace/a.txt') is True
    stat = b.file_stat('/workspace/a.txt')
    assert stat['exists'] is True and stat['size'] == 5

    # Home-view paths land in <ws>/.home on the host
    assert b.write_file('/home/agent/.persistent', 'hi') == {'ok': True}
    assert (tmp_path / '.home' / '.persistent').read_text() == 'hi'

    assert b.delete_file('/workspace/a.txt') == {'ok': True}
    assert not (tmp_path / 'a.txt').exists()


# ---------------------------------------------------------------------------
# _bwrap_argv
# ---------------------------------------------------------------------------

def test_bwrap_argv_core_flags(tmp_path):
    ws = str(tmp_path)
    b = BwrapBackend(session_id='s1', workspace=ws, agent_name='Test Agent')
    argv = b._bwrap_argv()
    for flag in ('--unshare-pid', '--unshare-uts', '--unshare-ipc',
                 '--unshare-user', '--die-with-parent'):
        assert flag in argv
    assert argv[argv.index('--hostname') + 1] == 'test-agent'
    ws_bind = argv.index('--bind')
    assert argv[ws_bind + 1:ws_bind + 3] == [ws, '/workspace']
    assert os.path.join(ws, '.home') in argv and '/home/agent' in argv
    assert bwrap_backend._HELPERS_DIR in argv
    assert argv[argv.index('--chdir') + 1] == '/workspace'


def test_bwrap_argv_subagent_chdir(tmp_path):
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path), is_subagent=True)
    argv = b._bwrap_argv()
    assert argv[argv.index('--chdir') + 1] == '/workspace/.scratch'


def test_bwrap_argv_binds_resolv_conf_target(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path))
    real_realpath = os.path.realpath

    def fake_realpath(path, **kw):
        if path == '/etc/resolv.conf':
            return '/run/systemd/resolve/stub-resolv.conf'
        return real_realpath(path, **kw)

    monkeypatch.setattr(bwrap_backend.os.path, 'realpath', fake_realpath)
    argv = b._bwrap_argv()
    i = argv.index('/run/systemd/resolve/stub-resolv.conf')
    assert argv[i - 1] == '--ro-bind-try' and argv[i + 1] == argv[i]

    # Plain-file resolv.conf (no symlink) needs no extra bind
    monkeypatch.setattr(bwrap_backend.os.path, 'realpath',
                        lambda p, **kw: p if p == '/etc/resolv.conf' else real_realpath(p, **kw))
    assert '/run/systemd/resolve/stub-resolv.conf' not in b._bwrap_argv()


def test_bwrap_argv_unshare_net_follows_config(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path))
    monkeypatch.setattr(bwrap_backend, 'SANDBOX_NETWORK', 'bridge')
    assert '--unshare-net' not in b._bwrap_argv()
    monkeypatch.setattr(bwrap_backend, 'SANDBOX_NETWORK', 'none')
    assert '--unshare-net' in b._bwrap_argv()


# ---------------------------------------------------------------------------
# Availability guard
# ---------------------------------------------------------------------------

def test_availability_error_non_linux():
    if sys.platform == 'linux':
        return  # message depends on bwrap presence; covered by the guard test below
    err = bwrap_backend._availability_error()
    assert err is not None and 'Linux-only' in err


def test_run_bash_returns_error_when_unavailable(tmp_path, monkeypatch):
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path))
    monkeypatch.setattr(bwrap_backend, '_availability_error', lambda: 'bwrap missing')
    result = b.run_bash('echo hi', timeout=5, env={})
    assert result == {'error': 'bwrap missing', 'exit_code': -1, 'execution_time': 0}
    result = b.run_python('print(1)', timeout=5, env={})
    assert result['error'] == 'bwrap missing'


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def test_base_env_is_minimal(tmp_path, monkeypatch):
    monkeypatch.setenv('EVONIC_SECRET_TOKEN', 'leak-me-not')
    b = BwrapBackend(session_id='s1', workspace=str(tmp_path), agent_name='bot')
    env = b._base_env({'FOO': 'bar'})
    assert 'EVONIC_SECRET_TOKEN' not in env
    assert env['HOME'] == '/home/agent'
    assert env['HOSTNAME'] == 'bot'
    assert env['SCRATCH'] == '/workspace/.scratch'
    assert env['FOO'] == 'bar'


# ---------------------------------------------------------------------------
# Registry selection via SANDBOX_BACKEND
# ---------------------------------------------------------------------------

def test_registry_selects_bwrap(tmp_path, monkeypatch):
    import config
    from backend.tools.lib.exec_backend import registry
    monkeypatch.setattr(config, 'SANDBOX_BACKEND', 'bwrap')
    backend = registry.get_backend('sess-bwrap', {
        'sandbox_enabled': 1, 'workspace': str(tmp_path),
        'agent_id': 'a1', 'name': 'My Agent',
    })
    assert isinstance(backend, BwrapBackend)
    assert backend._hostname == 'my-agent'


def test_registry_defaults_to_docker(tmp_path, monkeypatch):
    import config
    from backend.tools.lib.exec_backend import registry
    from backend.tools.lib.backends.docker_backend import DockerBackend
    monkeypatch.setattr(config, 'SANDBOX_BACKEND', 'docker')
    backend = registry.get_backend('sess-docker', {
        'sandbox_enabled': 1, 'workspace': str(tmp_path), 'agent_id': 'a1',
    })
    assert isinstance(backend, DockerBackend)
