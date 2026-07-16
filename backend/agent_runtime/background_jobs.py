"""
background_jobs — Track background processes and notify the agent on completion.

Background processes reach this module two ways:

1. **Guard wrappers** — a build/download launched via the long_running_guard
   wrapper (tmux/screen/nohup with log + EXIT_CODE marker).
2. **Manual spawns** — the agent's own ``tmux new-session -d``, ``screen -dmS``
   or ``nohup … &`` scripts, detected by :func:`parse_manual_spawn`.

Both are registered by the bash tool after a successful spawn and handed off to
a persisted scheduler job (APScheduler + SQLite) via :func:`auto_watch` — no
``/detach`` needed. The scheduler polls the OS process on an interval; on
completion it feeds the result back into the agent (via handle_message) so the
agent follows up for the user, then self-deletes so the interval stops.

Using the scheduler (not an in-process thread) means tracking survives a server
restart: the poll schedule is reloaded from the DB on boot and keeps going.

The in-memory registry here is intentionally lightweight and transient — it
holds job identity + status for the Session State UI. All state needed to resume
polling lives in the schedule's action_config, so losing the registry on restart
only blanks the UI list; notifications still fire.
"""
from __future__ import annotations

import logging
import threading
import time
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

_logger = logging.getLogger(__name__)

# Poll cadence and a hard lifetime cap (also enforced via schedule max_runs).
_POLL_SECONDS = 30
_MAX_WATCH_SECONDS = 6 * 60 * 60          # 6 hours
_MAX_RUNS = _MAX_WATCH_SECONDS // _POLL_SECONDS  # backstop if self-cancel fails
_POLL_TIMEOUT = 15
_TAIL_LINES = 30

# owner_type for internal poll schedules — distinct so they stay out of the
# user-facing routines UI (which lists by a specific owner_type).
SCHEDULE_OWNER_TYPE = "background_job"


# Finished jobs kept per session for the Session State UI (oldest pruned).
_MAX_FINISHED_PER_SESSION = 10


@dataclass
class BackgroundJob:
    job_id: str
    session_id: str
    session_name: str
    log_file: str
    pid_file: str
    command: str
    started_at: float
    detached: bool = False
    schedule_id: Optional[str] = None
    kind: str = "wrapper"            # wrapper | tmux | screen | nohup
    pgrep_pattern: str = ""          # nohup fallback when no PID file
    status: str = "running"          # running | done | timeout
    exit_code: Optional[int] = None
    finished_at: Optional[float] = None


class BackgroundJobRegistry:
    """Thread-safe in-memory registry of background jobs (running + recent)."""

    def __init__(self):
        self._jobs: Dict[str, BackgroundJob] = {}
        self._guard = threading.Lock()
        self._counter = 0

    def register(self, session_id: str, session_name: str, log_file: str,
                 pid_file: str, command: str, kind: str = "wrapper",
                 pgrep_pattern: str = "") -> BackgroundJob:
        with self._guard:
            dedup_key = session_name or pgrep_pattern or command
            for j in self._jobs.values():
                if j.session_id == session_id and j.status == "running" and \
                        (j.session_name or j.pgrep_pattern or j.command) == dedup_key:
                    return j  # dedup: same spawn re-run
            self._counter += 1
            job = BackgroundJob(
                job_id=f"job{self._counter}",
                session_id=session_id,
                session_name=session_name,
                log_file=log_file,
                pid_file=pid_file,
                command=command,
                started_at=time.time(),
                kind=kind,
                pgrep_pattern=pgrep_pattern,
            )
            self._jobs[job.job_id] = job
            self._prune_finished(session_id)
            _logger.info("[bgjob] registered %s kind=%s session=%s sess=%s cmd=%r",
                         job.job_id, kind, session_id, session_name, command)
            return job

    def _prune_finished(self, session_id: str) -> None:
        """Drop oldest finished jobs beyond the per-session cap. Caller holds lock."""
        finished = sorted(
            [j for j in self._jobs.values()
             if j.session_id == session_id and j.status != "running"],
            key=lambda j: j.finished_at or 0,
        )
        excess = len(finished) - _MAX_FINISHED_PER_SESSION
        for j in finished[:max(0, excess)]:
            self._jobs.pop(j.job_id, None)

    def mark_finished(self, session_id: str, session_name: str, command: str,
                      status: str, exit_code: Optional[int]) -> None:
        """Record completion for UI display. No-op if the job is gone (restart)."""
        with self._guard:
            for j in self._jobs.values():
                if j.session_id == session_id and j.status == "running" and \
                        (j.session_name == session_name or j.command == command):
                    j.status = status
                    j.exit_code = exit_code
                    j.finished_at = time.time()
                    self._prune_finished(session_id)
                    return

    def active_for_session(self, session_id: str) -> List[BackgroundJob]:
        """Jobs in this session not yet detached (candidates for /detach)."""
        with self._guard:
            return [j for j in self._jobs.values()
                    if j.session_id == session_id and not j.detached]

    def list_for_session(self, session_id: str) -> List[BackgroundJob]:
        with self._guard:
            return [j for j in self._jobs.values() if j.session_id == session_id]

    def mark_detached(self, job_id: str, schedule_id: Optional[str]) -> None:
        with self._guard:
            j = self._jobs.get(job_id)
            if j:
                j.detached = True
                j.schedule_id = schedule_id


# Singleton
background_jobs = BackgroundJobRegistry()


def parse_wrapper_script(script: str) -> Optional[dict]:
    """Extract job identity from a long_running_guard wrapper script.

    Returns dict(session_name, log_file, pid_file, command) when the script is a
    generated wrapper (starts with the bypass marker), else None.
    """
    from backend.tools.lib.long_running_guard import BYPASS_MARKER

    if not script.lstrip().startswith(BYPASS_MARKER):
        return None

    def _grab(pattern: str) -> str:
        m = re.search(pattern, script)
        return m.group(1) if m else ""

    session_name = _grab(r'SESS="([^"]+)"')
    if not session_name:
        return None
    log_file = _grab(r'LOG_FILE="([^"]+)"')
    pid_file = _grab(r'PID_FILE="([^"]+)"')

    cmd_m = re.search(r"SCRIPT_CMD='\{ (.*?); \}; EC=\$\?", script, re.DOTALL)
    command = cmd_m.group(1).strip() if cmd_m else session_name
    command = command.replace("'\\''", "'")  # undo wrapper's quote escaping

    return {
        "session_name": session_name,
        "log_file": log_file,
        "pid_file": pid_file,
        "command": command,
    }


_TMUX_SPAWN_RE = re.compile(r'\btmux\s+(?:new-session|new)\b([^\n;|&]*)')
_SCREEN_SPAWN_RE = re.compile(r'\bscreen\s+([^\n;|&]*)')
_NOHUP_LINE_RE = re.compile(r'^\s*nohup\s+(.+)$', re.MULTILINE)
_PID_CAPTURE_RE = re.compile(r'echo\s+\$!\s*>>?\s*(\S+)')


def parse_manual_spawn(script: str) -> Optional[dict]:
    """Detect an agent-authored background spawn (tmux/screen/nohup).

    Returns dict(kind, session_name, log_file, pid_file, pgrep_pattern, command)
    for the first spawn found, or None. Guard wrapper scripts (BYPASS_MARKER)
    are excluded — those go through :func:`parse_wrapper_script`.
    """
    from backend.tools.lib.long_running_guard import BYPASS_MARKER

    if script.lstrip().startswith(BYPASS_MARKER):
        return None

    # Join backslash-newline continuations so multi-line spawn commands are
    # captured in full — the spawn regexes stop at a literal newline.
    script = re.sub(r'\\\n\s*', ' ', script)

    # tmux new-session -d -s NAME 'cmd'
    m = _TMUX_SPAWN_RE.search(script)
    if m:
        seg = m.group(1)
        detached = re.search(r'\s-[A-Za-z]*d', ' ' + seg)
        name_m = re.search(r'-[A-Za-z]*s\s+["\']?([^\s"\';|&]+)', seg)
        if detached and name_m:
            return {
                "kind": "tmux",
                "session_name": name_m.group(1),
                "log_file": "",
                "pid_file": "",
                "pgrep_pattern": "",
                "command": m.group(0).strip()[:1000],
            }

    # screen -dmS NAME cmd  (or -d -m -S NAME)
    m = _SCREEN_SPAWN_RE.search(script)
    if m:
        seg = m.group(1)
        name_m = re.search(r'-(?:[A-Za-z]*S)\s+["\']?([^\s"\';|&]+)', seg)
        detached = '-dmS' in seg or ('-d' in seg and '-m' in seg)
        if detached and name_m:
            return {
                "kind": "screen",
                "session_name": name_m.group(1),
                "log_file": "",
                "pid_file": "",
                "pgrep_pattern": "",
                "command": m.group(0).strip()[:1000],
            }

    # nohup CMD [> log 2>&1] &  [echo $! > pidfile]
    m = _NOHUP_LINE_RE.search(script)
    if m:
        line = re.sub(r'2>&1', '', m.group(1))
        # Backgrounded = a lone '&' (not '&&'); may sit mid-line before
        # e.g. 'echo $! > pidfile'. Foreground nohup blocks the bash call
        # itself, so there is nothing to watch.
        amp = re.search(r'(?<!&)&(?!&)', line)
        if not amp:
            return None
        before_amp = line[:amp.start()]
        # Command text: everything up to the first redirect
        command = re.split(r'\s[12]?>>?', before_amp)[0].strip()
        if not command:
            return None
        # stdout redirect target (ignore stderr-only redirects like 2>file)
        line_wo_stderr = re.sub(r'2>>?\s*\S+', '', before_amp)
        log_m = re.search(r'>>?\s*([^\s&]+)', line_wo_stderr)
        pid_m = _PID_CAPTURE_RE.search(script)
        return {
            "kind": "nohup",
            "session_name": "",
            "log_file": log_m.group(1) if log_m else "",
            "pid_file": pid_m.group(1) if pid_m else "",
            "pgrep_pattern": "" if pid_m else command[:80].strip('"\''),
            "command": ("nohup " + command)[:1000],
        }

    return None


def build_manual_status_script(kind: str, session_name: str, pid_file: str,
                               pgrep_pattern: str) -> str:
    """RUNNING/DONE poll snippet for a manually spawned background process."""
    if kind == "tmux":
        return (f'tmux has-session -t {session_name} 2>/dev/null '
                f'&& echo "RUNNING" || echo "DONE"')
    if kind == "screen":
        return (f'screen -list 2>/dev/null | grep -q {session_name} '
                f'&& echo "RUNNING" || echo "DONE"')
    if pid_file:
        return (f'[ -f {pid_file} ] && kill -0 $(cat {pid_file}) 2>/dev/null '
                f'&& echo "RUNNING" || echo "DONE"')
    esc = pgrep_pattern.replace("'", "'\\''")
    return (f"pgrep -f -- '{esc}' >/dev/null 2>&1 "
            f'&& echo "RUNNING" || echo "DONE"')


def auto_watch(job: BackgroundJob, agent_id: str,
               external_user_id: Optional[str],
               channel_id: Optional[str]) -> Optional[str]:
    """Start the completion watcher for a freshly registered job.

    Idempotent: returns the existing schedule_id if the job is already watched.
    """
    if job.detached:
        return job.schedule_id
    schedule_id = create_detach_schedule(job, agent_id, external_user_id, channel_id)
    if schedule_id:
        background_jobs.mark_detached(job.job_id, schedule_id)
    return schedule_id


def create_detach_schedule(job: BackgroundJob, agent_id: str,
                           external_user_id: Optional[str],
                           channel_id: Optional[str]) -> Optional[str]:
    """Create a persisted scheduler job that polls `job` to completion.

    Returns the schedule_id, or None on failure.
    """
    from backend.scheduler import scheduler

    action_config = {
        "session_name": job.session_name,
        "log_file": job.log_file,
        "pid_file": job.pid_file,
        "command": job.command,
        "kind": job.kind,
        "pgrep_pattern": job.pgrep_pattern,
        "session_id": job.session_id,
        "agent_id": agent_id,
        "external_user_id": external_user_id,
        "channel_id": channel_id,
        "deadline_ts": job.started_at + _MAX_WATCH_SECONDS,
    }
    try:
        sched = scheduler.create_schedule(
            name=f"bgjob:{job.command[:40]}",
            owner_type=SCHEDULE_OWNER_TYPE,
            owner_id=agent_id,
            trigger_type="interval",
            trigger_config={"seconds": _POLL_SECONDS},
            action_type="poll_background_job",
            action_config=action_config,
            max_runs=_MAX_RUNS,
            metadata={"session_id": job.session_id},
        )
        return sched.get("id") if sched else None
    except Exception as e:
        _logger.warning("[bgjob] failed to create poll schedule: %s", e)
        return None


def run_poll_action(action_config: dict) -> dict:
    """Poll one background job (called by the scheduler each interval tick).

    Returns {'done': bool, 'state': str}. When done, the agent has been notified
    and the caller (scheduler) self-cancels the schedule.
    """
    from backend.tools.lib.exec_backend import registry
    from backend.tools.lib.long_running_guard import build_status_scripts
    from models.db import db

    session_id = action_config["session_id"]
    session_name = action_config["session_name"]
    log_file = action_config["log_file"]
    pid_file = action_config["pid_file"]
    kind = action_config.get("kind") or "wrapper"
    pgrep_pattern = action_config.get("pgrep_pattern") or ""
    deadline_ts = action_config.get("deadline_ts") or 0

    if kind == "wrapper":
        scripts = build_status_scripts(session_name, log_file, pid_file)
        check_status_script = scripts["check_status_script"]
    else:
        scripts = None
        check_status_script = build_manual_status_script(
            kind, session_name, pid_file, pgrep_pattern)
    agent = db.get_agent(action_config["agent_id"]) or {}

    try:
        backend = registry.get_backend(session_id, agent)
    except Exception as e:
        _logger.warning("[bgjob] backend resolve failed: %s", e)
        return {"done": False, "state": "backend_error"}

    timed_out = deadline_ts and time.time() > deadline_ts

    if not timed_out:
        try:
            res = backend.run_bash(check_status_script, _POLL_TIMEOUT, {})
            out = (res.get("stdout") or "")
        except Exception as e:
            _logger.warning("[bgjob] status poll failed: %s", e)
            return {"done": False, "state": "poll_error"}
        if "DONE" not in out:
            return {"done": False, "state": "running"}

    # Completed (or timed out) — gather exit code + log tail, then notify.
    # Exit code is only knowable for guard wrappers (EXIT_CODE marker in log).
    exit_code: Optional[int] = None
    tail = ""
    if not timed_out and scripts is not None:
        try:
            ec = backend.run_bash(scripts["check_exit_code_script"], _POLL_TIMEOUT, {})
            ec_out = (ec.get("stdout") or "").strip()
            if ec_out.isdigit():
                exit_code = int(ec_out)
        except Exception:
            pass
    if log_file:
        try:
            tr = backend.run_bash(f"tail -n {_TAIL_LINES} {log_file}", _POLL_TIMEOUT, {})
            tail = (tr.get("stdout") or "")
        except Exception:
            pass

    status = "timeout" if timed_out else "done"
    background_jobs.mark_finished(session_id, session_name,
                                  action_config.get("command", ""),
                                  status=status, exit_code=exit_code)
    _trigger_agent_summary(action_config, status=status,
                           exit_code=exit_code, tail=tail)
    return {"done": True, "state": status}


def _trigger_agent_summary(action_config: dict, status: str,
                           exit_code: Optional[int], tail: str) -> None:
    """Feed the finished job back into the agent so it summarizes for the user.

    Routes through handle_message (same path scheduled prompts use) so the
    agent's reply is delivered via the normal pipeline (web SSE + channel).
    """
    command = action_config.get("command", "the background job")
    log_file = action_config.get("log_file", "")
    kind = action_config.get("kind") or "wrapper"
    tail = (tail or "").strip()

    if status == "done":
        if exit_code is None and kind != "wrapper":
            outcome = ("finished (exit code unknown — not available for "
                       "manually spawned processes)")
        elif exit_code in (0, None):
            outcome = "finished successfully (exit code 0)"
        else:
            outcome = f"finished with FAILURE (exit code {exit_code})"
    else:
        outcome = "is still running past the watch limit; monitoring was stopped"

    trigger = (
        "[SYSTEM] A background process you started has finished — there is no "
        "user message to answer; proactively report the outcome. The background "
        "tracking schedule has already been removed automatically, so no cleanup "
        "is needed on your part.\n\n"
        f"Command: `{command}`\n"
        f"Outcome: {outcome}\n"
    )
    if log_file:
        trigger += f"Log file: {log_file}\n"
    if tail:
        trigger += f"\nLast output:\n```\n{tail[-1500:]}\n```\n"
    trigger += (
        "\nFollow up appropriately: verify or summarize the result for the user "
        "concisely and naturally. If it failed, note the likely cause from the "
        "output and suggest a next step."
    )

    try:
        from backend.agent_runtime import agent_runtime
        agent_runtime.handle_message(
            agent_id=action_config["agent_id"],
            external_user_id=action_config.get("external_user_id") or "__system__",
            message=trigger,
            channel_id=action_config.get("channel_id"),
            metadata={"background_job_trigger": True},
        )
    except Exception as e:
        _logger.warning("[bgjob] summary trigger failed: %s", e)
