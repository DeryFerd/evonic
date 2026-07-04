"""
BwrapBackend — lightweight per-agent sandbox via bubblewrap (bwrap).

Used when sandbox_enabled=1 and SANDBOX_BACKEND=bwrap. Linux-only.

Each command runs in a fresh bubblewrap namespace sandbox: the host rootfs
is mounted read-only, the agent workspace is bound rw at /workspace, and a
persistent home lives at /home/agent (backed by <workspace>/.home on the
host). The agent gets its own hostname (UTS ns), PID namespace, IPC
namespace, and a private tmpfs /tmp — it "feels like its own OS" with
near-zero overhead: no daemon, no image, no standing container process.

Stateless process model: one bwrap invocation per run_bash/run_python.
State persists only via the workspace/home directories on disk. Documented
limitation: background processes do NOT survive between calls — when the
sandbox's PID-namespace init exits, the kernel kills every process in the
namespace, so tmux/screen/nohup workflows will not persist.

If unprivileged user namespaces are disabled on the host, bwrap fails with
a uid-map error; check `sysctl kernel.unprivileged_userns_clone` (Debian)
or `kernel.apparmor_restrict_unprivileged_userns` (Ubuntu 24.04+).
"""

import os
import re
import shutil
import subprocess
import sys
import time

from backend.tools.lib.exec_backend import truncate
from backend.tools.lib.process_tracker import process_tracker
from backend.tools.lib.backends.local_backend import LocalBackend, _MAX_OUTPUT_BYTES

try:
    from config import SANDBOX_WORKSPACE, SANDBOX_NETWORK, SANDBOX_BWRAP_ULIMIT_V_MB
except ImportError:
    SANDBOX_WORKSPACE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
    SANDBOX_NETWORK = 'bridge'
    SANDBOX_BWRAP_ULIMIT_V_MB = 0

# Directory containing the evonic helper package (bound into the sandbox).
_HELPERS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'runpy_helpers'))
# Helpers are bound at a top-level path: mount points inside read-only binds
# (/usr, /opt, …) cannot be created by bwrap, but the sandbox root is a tmpfs
# where top-level directories can.
_HELPERS_MOUNT_PARENT = '/evonic-helpers'
_HELPERS_MOUNT = f'{_HELPERS_MOUNT_PARENT}/evonic'

# PATH prefix prepended to every bash script so evonic/bin binaries take priority.
# The rg() wrapper fixes a stdin-inheritance bug: when `bash -s` reads from a pipe,
# child processes inherit that pipe as stdin and rg reads EOF instead of searching.
_EVONIC_BIN = f'{_HELPERS_MOUNT}/bin'
_PATH_PREFIX = (
    f'export PATH={_EVONIC_BIN}:$PATH\n'
    'rg() { if [ ! -t 0 ]; then command rg "$@" .; else command rg "$@"; fi; }\n'
    'export -f rg\n'
)

_HOME_MOUNT = '/home/agent'
_HOME_SUBDIR = '.home'  # host dir under the workspace backing /home/agent

_USERNS_HINT = (
    ' Hint: bubblewrap needs unprivileged user namespaces — check '
    '`sysctl kernel.unprivileged_userns_clone` (Debian) or '
    '`kernel.apparmor_restrict_unprivileged_userns` (Ubuntu 24.04+).'
)


def _availability_error() -> str:
    """Return an error message if bwrap cannot run on this host, else None."""
    if sys.platform != 'linux':
        return (f'bwrap sandbox backend is Linux-only (current platform: {sys.platform}). '
                f'Set SANDBOX_BACKEND=docker or disable the sandbox for this agent.')
    if shutil.which('bwrap') is None:
        return ('bubblewrap is not installed. Install it with: sudo apt install bubblewrap '
                '(Debian/Ubuntu) — or set SANDBOX_BACKEND=docker.')
    return None


def _sanitize_hostname(name: str) -> str:
    h = re.sub(r'[^a-zA-Z0-9-]+', '-', (name or '').lower()).strip('-')[:63]
    return h or 'agent'


class BwrapBackend(LocalBackend):
    """Executes bash/python inside a fresh bubblewrap namespace sandbox.

    Subclasses LocalBackend (with run_as_user=None) to reuse its
    _poll_proc / _run_bash_streaming machinery and its host-side (non-sudo)
    file I/O — the workspace is a plain host directory, so file operations
    only need the sandbox-view → host path reverse mapping in _to_host().
    """

    def __init__(self, session_id: str = '', workspace: str = None,
                 agent_id: str = '', agent_name: str = '', is_subagent: bool = False):
        super().__init__(session_id=session_id, workspace=workspace, run_as_user=None)
        self._agent_id = agent_id
        self._hostname = _sanitize_hostname(agent_name or agent_id)
        self._is_subagent = is_subagent
        self._dirs_ready = False

    # ------------------------------------------------------------------
    # Sandbox construction
    # ------------------------------------------------------------------

    def _ensure_dirs(self):
        if self._dirs_ready:
            return
        ws = self._cwd()
        os.makedirs(os.path.join(ws, _HOME_SUBDIR), exist_ok=True)
        os.makedirs(os.path.join(ws, '.scratch'), exist_ok=True)
        self._dirs_ready = True

    def _bwrap_argv(self) -> list:
        ws = self._cwd()
        argv = [
            'bwrap',
            '--ro-bind', '/usr', '/usr',
            '--ro-bind', '/etc', '/etc',
            '--ro-bind-try', '/opt', '/opt',
        ]
        # Merged-usr distros (Debian/Ubuntu) have /bin -> usr/bin symlinks;
        # recreate them as symlinks, fall back to ro-binds on split-usr hosts.
        for d in ('/bin', '/sbin', '/lib', '/lib64'):
            if os.path.islink(d):
                argv += ['--symlink', 'usr' + d, d]
            elif os.path.isdir(d):
                argv += ['--ro-bind', d, d]
        argv += [
            '--proc', '/proc',
            '--dev', '/dev',
            '--tmpfs', '/tmp',
            '--bind', ws, '/workspace',
            '--bind', os.path.join(ws, _HOME_SUBDIR), _HOME_MOUNT,
            '--ro-bind', _HELPERS_DIR, _HELPERS_MOUNT,
            '--unshare-pid', '--unshare-uts', '--unshare-ipc', '--unshare-user',
            '--hostname', self._hostname,
            '--die-with-parent',
            '--chdir', '/workspace/.scratch' if self._is_subagent else '/workspace',
        ]
        if SANDBOX_NETWORK == 'none':
            argv += ['--unshare-net']
        return argv

    def _base_env(self, env: dict) -> dict:
        # Deliberately NOT inherited from os.environ — the sandbox must not
        # see server secrets. Minimal explicit environment only.
        run_env = {
            'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            'HOME': _HOME_MOUNT,
            'USER': 'agent',
            'LOGNAME': 'agent',
            'HOSTNAME': self._hostname,
            'TMPDIR': '/tmp',
            'LANG': 'C.UTF-8',
            'TERM': 'dumb',
            'SCRATCH': '/workspace/.scratch',
        }
        run_env.update(env)
        return run_env

    @staticmethod
    def _bash_prefix() -> str:
        prefix = _PATH_PREFIX
        if SANDBOX_BWRAP_ULIMIT_V_MB > 0:
            prefix = f'ulimit -v {SANDBOX_BWRAP_ULIMIT_V_MB * 1024} 2>/dev/null\n' + prefix
        return prefix

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _exec(self, cmd: list, input_data: str, timeout: int, run_env: dict) -> dict:
        t0 = time.time()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=self._cwd(), env=run_env,
            start_new_session=True,
        )
        process_tracker.register(self._session_id, proc, proc.pid,
                                 kill_method='killpg')
        try:
            stdout, stderr, reason = self._poll_proc(proc, input_data, timeout, t0)
            if stdout is None:
                elapsed = round(time.time() - t0, 3)
                if reason == 'timeout':
                    return {
                        'error': f'Execution timed out after {timeout}s',
                        'exit_code': -1,
                        'execution_time': elapsed,
                    }
                was_user_stop = not process_tracker.is_registered(self._session_id)
                if was_user_stop:
                    return {
                        'error': 'Execution stopped by user',
                        'exit_code': -9,
                        'execution_time': elapsed,
                    }
                sig = -proc.returncode if proc.returncode else 'unknown'
                return {
                    'error': f'Process killed by signal {sig}. This may happen when a command requires interactive input that cannot be provided in this environment.',
                    'exit_code': proc.returncode or -9,
                    'execution_time': elapsed,
                }
        finally:
            process_tracker.unregister(self._session_id)
        elapsed = round(time.time() - t0, 3)
        if proc.returncode != 0 and 'bwrap:' in (stderr or ''):
            stderr += _USERNS_HINT
        return {
            'stdout': truncate(stdout, _MAX_OUTPUT_BYTES),
            'stderr': truncate(stderr, _MAX_OUTPUT_BYTES),
            'exit_code': proc.returncode,
            'execution_time': elapsed,
        }

    def run_bash(self, script: str, timeout: int, env: dict, on_output=None) -> dict:
        err = _availability_error()
        if err:
            return {'error': err, 'exit_code': -1, 'execution_time': 0}
        self._ensure_dirs()
        run_env = self._base_env(env)
        prefixed = self._bash_prefix() + script
        cmd = self._bwrap_argv() + ['bash', '-s']
        if on_output is not None:
            return self._run_bash_streaming(cmd, prefixed, timeout, run_env, time.time(), on_output)
        return self._exec(cmd, prefixed, timeout, run_env)

    def run_python(self, code: str, timeout: int, env: dict) -> dict:
        err = _availability_error()
        if err:
            return {'error': err, 'exit_code': -1, 'execution_time': 0}
        self._ensure_dirs()
        run_env = self._base_env(env)
        run_env['PYTHONPATH'] = _HELPERS_MOUNT_PARENT
        if SANDBOX_BWRAP_ULIMIT_V_MB > 0:
            inner = ['bash', '-c',
                     f'ulimit -v {SANDBOX_BWRAP_ULIMIT_V_MB * 1024} 2>/dev/null; exec python3 -']
        else:
            inner = ['python3', '-']
        cmd = self._bwrap_argv() + inner
        return self._exec(cmd, code, timeout, run_env)

    # ------------------------------------------------------------------
    # Path resolution & file I/O
    # ------------------------------------------------------------------

    def resolve_path(self, path: str) -> str:
        """Convert a host filesystem path to the sandbox's /workspace view."""
        effective = self._cwd()
        if path.startswith(effective):
            return '/workspace' + path[len(effective):]
        return path

    def _to_host(self, path: str) -> str:
        """Reverse-map a sandbox-view path back to the host filesystem.

        File tools call resolve_path() first and hand us the sandbox view;
        the workspace and home are plain host directories, so file I/O runs
        host-side (LocalBackend non-sudo paths) on the mapped location.
        Other paths (e.g. /tmp — a per-invocation tmpfs) pass through as-is.
        """
        ws = self._cwd()
        if path == '/workspace' or path.startswith('/workspace/'):
            return ws + path[len('/workspace'):]
        if path == _HOME_MOUNT or path.startswith(_HOME_MOUNT + '/'):
            return os.path.join(ws, _HOME_SUBDIR) + path[len(_HOME_MOUNT):]
        return path

    def file_exists(self, path: str) -> bool:
        return super().file_exists(self._to_host(path))

    def file_stat(self, path: str) -> dict:
        return super().file_stat(self._to_host(path))

    def read_file(self, path: str) -> dict:
        return super().read_file(self._to_host(path))

    def write_file(self, path: str, content: str, create_dirs: bool = True) -> dict:
        return super().write_file(self._to_host(path), content, create_dirs)

    def make_dirs(self, path: str) -> dict:
        return super().make_dirs(self._to_host(path))

    def cat_file_bytes(self, path: str) -> dict:
        return super().cat_file_bytes(self._to_host(path))

    def delete_file(self, path: str) -> dict:
        return super().delete_file(self._to_host(path))

    def write_file_bytes(self, path: str, data: bytes, create_dirs: bool = True) -> dict:
        return super().write_file_bytes(self._to_host(path), data, create_dirs)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def destroy(self) -> dict:
        return {'result': 'ok',
                'detail': 'bwrap backend is stateless — nothing to destroy. Agent home persists in the workspace.'}

    def status(self) -> dict:
        err = _availability_error()
        info = {
            'backend': 'bwrap',
            'workspace': self._cwd(),
            'hostname': self._hostname,
            'available': err is None,
            'detail': f'stateless; one sandbox per command; {_HOME_MOUNT} persists at <workspace>/{_HOME_SUBDIR}',
        }
        if err:
            info['error'] = err
        return info
