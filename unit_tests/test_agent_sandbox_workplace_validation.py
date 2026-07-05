"""
Tests for sandbox_enabled enforcement against workplace type (issue #35).
"""

import unittest
from unittest.mock import patch

from routes.agents import _apply_sandbox_workplace_policy


class TestAgentSandboxWorkplaceValidation(unittest.TestCase):
    def _mock_get_workplace(self, workplace_id):
        workplaces = {
            'local-wp': {'id': 'local-wp', 'type': 'local'},
            'remote-wp': {'id': 'remote-wp', 'type': 'remote'},
            'tunnel-wp': {'id': 'tunnel-wp', 'type': 'tunnel'},
            'bwrap-wp': {'id': 'bwrap-wp', 'type': 'bwrap'},
        }
        return workplaces.get(workplace_id)

    @patch('routes.agents.db.get_workplace')
    def test_local_workplace_allows_sandbox_on(self, mock_get):
        mock_get.side_effect = self._mock_get_workplace
        data = {'sandbox_enabled': 1}
        _apply_sandbox_workplace_policy(data, 'local-wp')
        self.assertEqual(data['sandbox_enabled'], 1)

    @patch('routes.agents.db.get_workplace')
    def test_local_workplace_allows_sandbox_off(self, mock_get):
        mock_get.side_effect = self._mock_get_workplace
        data = {'sandbox_enabled': 0}
        _apply_sandbox_workplace_policy(data, 'local-wp')
        self.assertEqual(data['sandbox_enabled'], 0)

    @patch('routes.agents.db.get_workplace')
    def test_remote_workplace_forces_sandbox_off_when_enabling(self, mock_get):
        mock_get.side_effect = self._mock_get_workplace
        data = {'sandbox_enabled': 1}
        _apply_sandbox_workplace_policy(data, 'remote-wp')
        self.assertEqual(data['sandbox_enabled'], 0)

    @patch('routes.agents.db.get_workplace')
    def test_remote_workplace_forces_sandbox_off_when_already_disabled(self, mock_get):
        mock_get.side_effect = self._mock_get_workplace
        data = {'sandbox_enabled': 0}
        _apply_sandbox_workplace_policy(data, 'remote-wp')
        self.assertEqual(data['sandbox_enabled'], 0)

    @patch('routes.agents.db.get_workplace')
    def test_tunnel_workplace_forces_sandbox_off(self, mock_get):
        mock_get.side_effect = self._mock_get_workplace
        data = {'sandbox_enabled': 1}
        _apply_sandbox_workplace_policy(data, 'tunnel-wp')
        self.assertEqual(data['sandbox_enabled'], 0)

    @patch('backend.tools.lib.backends.bwrap_backend._availability_error')
    @patch('routes.agents.db.get_workplace')
    def test_bwrap_workplace_forces_sandbox_off(self, mock_get, mock_avail):
        mock_get.side_effect = self._mock_get_workplace
        mock_avail.return_value = None
        data = {'sandbox_enabled': 1}
        _apply_sandbox_workplace_policy(data, 'bwrap-wp')
        self.assertEqual(data['sandbox_enabled'], 0)

    @patch('backend.tools.lib.backends.bwrap_backend._availability_error')
    @patch('routes.agents.db.get_workplace')
    def test_bwrap_workplace_raises_when_host_incompatible(self, mock_get, mock_avail):
        mock_get.side_effect = self._mock_get_workplace
        mock_avail.return_value = 'bubblewrap is not installed.'
        data = {'sandbox_enabled': 1}
        with self.assertRaises(ValueError):
            _apply_sandbox_workplace_policy(data, 'bwrap-wp')

    @patch('routes.agents.db.get_workplace')
    def test_no_workplace_id_leaves_data_unchanged(self, mock_get):
        data = {'sandbox_enabled': 1}
        _apply_sandbox_workplace_policy(data, None)
        self.assertEqual(data['sandbox_enabled'], 1)
        mock_get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
