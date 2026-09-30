"""Frozen registry files and sender clock errors cannot keep peers fresh."""

import unittest
from unittest.mock import patch

import manet_registry as registry
import manet_manage as manage


class FreshnessTests(unittest.TestCase):
    def test_frozen_registry_ages_even_if_sender_or_wall_clock_is_wrong(self):
        node = {'OBSERVED_AT_UPTIME': '100', 'NODE_STATE': 'ACTIVE',
                'OBSERVED_AGE_SECONDS': '0', 'LAST_SEEN_TIMESTAMP': '9999999999'}
        with patch.object(registry.time, 'time', return_value=-1):
            with patch.object(registry.time, 'clock_gettime', return_value=400):
                self.assertEqual(registry.node_state(node), 'ACTIVE')
            with patch.object(registry.time, 'clock_gettime', return_value=401):
                self.assertEqual(registry.node_state(node), 'STALE')

    def test_missing_invalid_and_shutdown_records_are_not_active(self):
        with patch.object(registry.time, 'clock_gettime', return_value=100):
            for value in (None, 'nan', 'inf', 'bad', '101'):
                self.assertEqual(registry.node_state({'OBSERVED_AT_UPTIME': value}), 'STALE')
            self.assertEqual(registry.node_state({'OBSERVED_AT_UPTIME': '100',
                                                 'NODE_STATE': 'SHUTTING_DOWN'}), 'SHUTTING_DOWN')

    def test_radio_ack_membership_ages_after_failed_registry_rebuild(self):
        nodes = {'a': {'HOSTNAME': 'fresh', 'OBSERVED_AT_UPTIME': '300'},
                 'b': {'HOSTNAME': 'gone', 'OBSERVED_AT_UPTIME': '0', 'NODE_STATE': 'ACTIVE'}}
        with patch.object(manage.subprocess, 'run', side_effect=OSError('unavailable')), \
                patch.object(manage, 'parse_registry', return_value=nodes), \
                patch.object(manage, 'get_my_hostname', return_value='self'), \
                patch.object(registry.time, 'clock_gettime', return_value=400):
            self.assertEqual(manage.radio_expected_hosts(), ['fresh', 'self'])
