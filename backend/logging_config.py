"""
Centralized logging configuration for Evonic.

Usage:
    # At startup (app.py, supervisor.py, cli):
    from backend.logging_config import configure
    configure()

    # In any module:
    from backend.logging_config import get_logger
    log = get_logger(__name__)
    log.info("message")

Format: [LEVEL] [module.path] message

Environment variables:
    EVONIC_LOG_LEVEL          — default log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    EVONIC_LOG_FILE           — path to log file (default: logs/evonic.log); empty = disable file
    EVONIC_LOG_MAX_BYTES      — max log file size before rotation (default: 5 MB)
    EVONIC_LOG_BACKUPS        — number of rotated backup files to keep (default: 3)
    EVONIC_LOG_QUIET          — comma-separated list of module names to silence globally (sets to ERROR level)
    EVONIC_LOG_CONSOLE_QUIET  — comma-separated list of fnmatch glob patterns; matching logs are hidden from console only (via Filter on StreamHandler)
    EVONIC_LOG_ROUTES         — semicolon-separated route entries; each entry is file_path:pattern1,pattern2
                                where patterns are fnmatch globs matched against logger names.
                                Default: logs/agent.log:backend.agent_runtime.*,backend.agent_state;
                                         logs/channels.log:backend.channels.*;
                                         logs/evaluator.log:evaluator.*
"""

import fnmatch
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

# ── Defaults ────────────────────────────────────────────────────────────────

_LOG_FORMAT = "[%(levelname)s] [%(name)s] %(message)s"
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_LOG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "evonic.log"
)
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_DEFAULT_BACKUPS = 3
_DEFAULT_LOG_ROUTES = (
    "logs/agent.log:backend.agent_runtime.*,backend.agent_state,backend.tools.agent_messaging;"
    "logs/channels.log:backend.channels.*;"
    "logs/evaluator.log:evaluator.*;"
    "logs/evomem.log:backend.agent_runtime.evomem_*"
)

_configured = False


class RouteFilter(logging.Filter):
    """Filter that accepts log records whose logger name matches any of the
    given fnmatch patterns (e.g. 'backend.agent_runtime.*', 'evaluator.*').
    """

    def __init__(self, patterns: list[str], name: str = ""):
        super().__init__(name)
        self.patterns = patterns

    def filter(self, record: logging.LogRecord) -> bool:
        return any(
            fnmatch.fnmatch(record.name, p) for p in self.patterns
        )


class ConsoleFilter(logging.Filter):
    """Filter that rejects log records whose logger name matches any of the
    given fnmatch glob patterns (e.g. 'apscheduler.scheduler').

    This is intended for the console handler only — file handlers are unaffected.
    """

    def __init__(self, patterns: list[str], name: str = ""):
        super().__init__(name)
        self.patterns = patterns

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(
            fnmatch.fnmatch(record.name, p) for p in self.patterns
        )


def _build_formatter() -> logging.Formatter:
    return logging.Formatter(_LOG_FORMAT)


def configure(
    level: Optional[str] = None,
    log_file: Optional[str] = None,
    max_bytes: Optional[int] = None,
    backups: Optional[int] = None,
    console: bool = True,
) -> None:
    """Configure root logger once at application startup.

    Args:
        level: Log level string, read from EVONIC_LOG_LEVEL if None.
        log_file: Path to rotating log file, read from EVONIC_LOG_FILE if None.
            Pass empty string to disable file output.
        max_bytes: Max size before rotation, read from EVONIC_LOG_MAX_BYTES if None.
        backups: Number of backup files, read from EVONIC_LOG_BACKUPS if None.
        console: Whether to attach a StreamHandler to stdout.
    """
    global _configured
    if _configured:
        # Idempotent — but re-apply settings that depend on env vars.
        # This handles the case where configure() was called before load_dotenv()
        # (e.g., from the CLI entry point) and the env var is now available.
        if console:
            root = logging.getLogger()
            for h in root.handlers:
                # Only apply ConsoleFilter to the console handler (stdout),
                # not to file handlers (RotatingFileHandler is a StreamHandler
                # subclass but writes to a file, not stdout).
                if (
                    isinstance(h, logging.StreamHandler)
                    and h.stream is sys.stdout
                ):
                    console_quiet = os.environ.get("EVONIC_LOG_CONSOLE_QUIET", "")
                    if console_quiet:
                        patterns = [p.strip() for p in console_quiet.split(",") if p.strip()]
                        if patterns and not any(
                            isinstance(f, ConsoleFilter) for f in h.filters
                        ):
                            h.addFilter(ConsoleFilter(patterns))
        # Re-apply EVONIC_LOG_QUIET (logger-level quiet) in case it was missed.
        quiet = os.environ.get("EVONIC_LOG_QUIET", "").split(",")
        for name in quiet:
            name = name.strip()
            if name:
                logging.getLogger(name).setLevel(logging.ERROR)
        return

    # Resolve env vars / defaults
    level = level or os.environ.get("EVONIC_LOG_LEVEL", _DEFAULT_LOG_LEVEL).upper()
    log_file = log_file if log_file is not None else os.environ.get("EVONIC_LOG_FILE", _DEFAULT_LOG_FILE)
    max_bytes = max_bytes or int(os.environ.get("EVONIC_LOG_MAX_BYTES", _DEFAULT_MAX_BYTES))
    backups = backups if backups is not None else int(os.environ.get("EVONIC_LOG_BACKUPS", _DEFAULT_BACKUPS))
    # Bounds check
    if max_bytes < 1:
        max_bytes = _DEFAULT_MAX_BYTES
    if max_bytes > 1_073_741_824:  # 1 GiB
        max_bytes = 1_073_741_824
    if backups is not None and (backups < 1 or backups > 100):
        backups = _DEFAULT_BACKUPS

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))

    # Silence noisy third-party modules
    quiet = os.environ.get("EVONIC_LOG_QUIET", "").split(",")
    for name in quiet:
        name = name.strip()
        if name:
            logging.getLogger(name).setLevel(logging.ERROR)

    formatter = _build_formatter()

    # Console handler (stdout)
    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        # Console-only filter: hide specific modules from stdout
        console_quiet = os.environ.get("EVONIC_LOG_CONSOLE_QUIET", "")
        if console_quiet:
            patterns = [p.strip() for p in console_quiet.split(",") if p.strip()]
            if patterns:
                ch.addFilter(ConsoleFilter(patterns))
        root.addHandler(ch)

    # File handler with rotation
    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)

    # --- Per-module log routing (EVONIC_LOG_ROUTES) ---
    # Format: file_path:pattern1,pattern2;file_path:pattern3,...
    # Routes matching module logs to dedicated files via filtered handlers.
    routes_raw = os.environ.get("EVONIC_LOG_ROUTES", _DEFAULT_LOG_ROUTES)
    if routes_raw:
        for entry in routes_raw.split(";"):
            entry = entry.strip()
            if not entry or ":" not in entry:
                continue
            # Split on first colon only — file path may be absolute (e.g. /var/log/...)
            file_path, patterns_str = entry.split(":", 1)
            file_path = file_path.strip()
            patterns = [p.strip() for p in patterns_str.split(",") if p.strip()]
            if not file_path or not patterns:
                continue
            # Create a dedicated rotating handler with a filter
            route_dir = os.path.dirname(file_path)
            if route_dir:
                os.makedirs(route_dir, exist_ok=True)
            rh = RotatingFileHandler(
                file_path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
            )
            rh.setFormatter(formatter)
            rh.addFilter(RouteFilter(patterns))
            root.addHandler(rh)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger, automatically configured on first call.

    If logging has not been configured yet, configure() is called with defaults.
    This ensures modules get a working logger even if nobody called configure().
    """
    global _configured
    if not _configured:
        configure()
    return logging.getLogger(name)
