"""
Tests for the 'bwrap' workplace type dispatch in WorkplaceManager.

BwrapBackend construction never raises (availability surfaces via status()),
so these tests patch BwrapBackend.status() to simulate available/unavailable
hosts without executing bwrap.
"""

import json
import unittest
from unittest.mock import patch

from backend.workplaces.manager import WorkplaceManager


def _bwrap_workplace_row(workplace_id='bwrap-wp', name='My Sandbox'):
    return {
        'id': workplace_id,
        'name': name,
        'type': 'bwrap',
        'config': json.dumps({'workspace_path': '/tmp/bwrap-ws'}),
        'status': 'disconnected',
    }


class TestWorkplaceManagerBwrap(unittest.TestCase):
    def setUp(self):
        self.manager = WorkplaceManager()

    def _patches(self, available=True):
        status = {'backend': 'bwrap', 'available': available}
        if not available:
            status['error'] = 'bwrap sandbox backend is Linux-only (current platform: test).'
        return (
            patch.object(WorkplaceManager, '_load_workplace', return_value=_bwrap_workplace_row()),
            patch.object(WorkplaceManager, '_set_status'),
            patch('backend.tools.lib.backends.bwrap_backend.BwrapBackend.status', return_value=status),
        )

    def test_get_backend_creates_and_caches_bwrap(self):
        load_p, status_p, avail_p = self._patches(available=True)
        with load_p, status_p as mock_set_status, avail_p:
            backend = self.manager.get_backend('bwrap-wp')
            from backend.tools.lib.backends.bwrap_backend import BwrapBackend
            self.assertIsInstance(backend, BwrapBackend)
            self.assertEqual(backend._hostname, 'my-sandbox')  # from workplace name
            self.assertIs(self.manager._backends[('bwrap-wp', False)], backend)
            mock_set_status.assert_called_with('bwrap-wp', 'connected')
            # Second call returns the cached instance
            self.assertIs(self.manager.get_backend('bwrap-wp'), backend)

    def test_get_backend_unavailable_raises_and_does_not_cache(self):
        load_p, status_p, avail_p = self._patches(available=False)
        with load_p, status_p as mock_set_status, avail_p:
            with self.assertRaises(RuntimeError) as ctx:
                self.manager.get_backend('bwrap-wp')
            self.assertIn('Linux-only', str(ctx.exception))
            self.assertNotIn(('bwrap-wp', False), self.manager._backends)
            mock_set_status.assert_called_with(
                'bwrap-wp', 'error',
                'bwrap sandbox backend is Linux-only (current platform: test).')

    def test_connect_ok_when_available(self):
        load_p, status_p, avail_p = self._patches(available=True)
        with load_p, status_p, avail_p:
            result = self.manager.connect('bwrap-wp')
            self.assertEqual(result, {'ok': True, 'status': 'connected'})
            self.assertIn(('bwrap-wp', False), self.manager._backends)

    def test_connect_error_when_unavailable(self):
        load_p, status_p, avail_p = self._patches(available=False)
        with load_p, status_p, avail_p:
            result = self.manager.connect('bwrap-wp')
            self.assertFalse(result['ok'])
            self.assertIn('Linux-only', result['error'])
            self.assertNotIn(('bwrap-wp', False), self.manager._backends)


if __name__ == '__main__':
    unittest.main()
