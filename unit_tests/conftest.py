"""
Pytest configuration and fixtures for unit tests.
Uses a separate test database to avoid polluting production data.
"""

import pytest
import os
import sys
import tempfile
import shutil
import threading as _threading

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Force daemon threads ────────────────────────────────────────────────────
# AgentRuntime starts non-daemon worker threads which prevent pytest from
# exiting after all tests complete.  Override Thread.__init__ so every thread
# created during the test session is a daemon thread, allowing clean exit.
_orig_thread_init = _threading.Thread.__init__


def _force_daemon_init(self, group=None, target=None, name=None,
                       args=(), kwargs=None, *, daemon=None):
    _orig_thread_init(self, group=group, target=target, name=name,
                      args=args, kwargs=kwargs, daemon=True)


_threading.Thread.__init__ = _force_daemon_init


def pytest_sessionfinish(session, exitstatus):
    """Force-exit to skip AgentRuntime atexit handlers that wait 30s for workers."""
    import os
    import sys
    sys.stdout.flush()
    sys.stderr.flush()
    # os._exit(int(exitstatus))  # disabled for debugging


@pytest.fixture(autouse=True)
def use_test_database(monkeypatch, tmp_path):
    """
    Automatically use a temporary test database for all tests.
    This prevents unit tests from polluting the production database.
    """
    # Create a temporary database file
    test_db_path = str(tmp_path / "test_evonic.db")
    
    # Patch the database path before importing db
    from models import db as db_module
    
    # Store original path
    original_path = db_module.db.db_path
    
    # Set test database path and clear cached connection so _connect() uses the new path
    db_module.db.db_path = test_db_path
    db_module.db._tlocal = _threading.local()

    # Reinitialize tables in test database
    db_module.db._init_tables()
    
    yield

    # Restore original path and reset connection cache
    db_module.db._tlocal = _threading.local()
    db_module.db.db_path = original_path


@pytest.fixture(autouse=True)
def ensure_super_agent(use_test_database):
    """Create a super agent in the test DB so Flask API routes pass the setup check."""
    from models.db import db
    if not db.has_super_agent():
        db.create_agent({
            'id': 'test_super_agent',
            'name': 'Test Super Agent',
            'system_prompt': '',
            'is_super': True,
        })
