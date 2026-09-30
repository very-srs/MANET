#!/usr/bin/env python3
"""Channel choice for a radio rejoining the mesh, from registry evidence."""

import json
from pathlib import Path
import tempfile
import time
import unittest

from manet_rejoin_channel import check_frequency, load_registry, peer_frequencies


OWN = ['02:00:00:00:00:01', '02:00:00:00:00:a1']


def boot():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def peer(n, interfaces, age=10, state='ACTIVE', raw=None):
    mac = f'02:00:00:00:01:{n:02x}'
    return {'MAC_ADDRESS': mac, 'MAC_ADDRESSES': mac + ',02:00:00:00:02:%02x' % n,
            'NODE_STATE': state, 'OBSERVED_AT_UPTIME': str(int(boot() - age)),
            'INTERFACES_JSON': raw if raw is not None else json.dumps(interfaces)}


def radio(channel=None, freq='', role='mesh', state='UP', name='wlan1'):
    return {'name': name, 'role': role, 'state': state,
            'channel': '' if channel is None else str(channel), 'freq_mhz': str(freq)}


class EvidenceTests(unittest.TestCase):
    def nodes(self, *peers):
        return {f'n{i}': p for i, p in enumerate(peers)}

    def test_counts_distinct_fresh_peers_per_frequency(self):
        nodes = self.nodes(peer(1, [radio(44)]), peer(2, [radio(44)]), peer(3, [radio(36)]))
        self.assertEqual(peer_frequencies('5', nodes, OWN), {5220: 2, 5180: 1})

    def test_frequency_field_is_used_when_published(self):
        nodes = self.nodes(peer(1, [radio(freq='5745')]))
        self.assertEqual(peer_frequencies('5', nodes, OWN), {5745: 1})

    def test_ineligible_evidence_is_ignored(self):
        ignored = self.nodes(
            peer(1, [radio(44)], age=400),                    # stale by our boot clock
            peer(2, [radio(44)], state='SHUTTING_DOWN'),
            peer(3, [radio(44, role='ap')]),                  # peer's own AP
            peer(4, [radio(44, state='DOWN')]),
            peer(5, [radio(6, name='wlan0')]),                # other band
            peer(7, [], raw='{not json'),
            peer(8, [], raw='{"role": "mesh"}'),
            {**peer(9, [radio(44)]), 'MAC_ADDRESSES': OWN[1]},  # our own record
            {**peer(10, [radio(44)]), 'OBSERVED_AT_UPTIME': ''},
        )
        self.assertEqual(peer_frequencies('5', ignored, OWN), {})

    def test_malformed_numbers_are_rejected_not_truncated(self):
        for bad in ('inf', 'nan', '5180.5', '-5180', '1e4', '5180 MHz', '9' * 12):
            with self.subTest(bad=bad):
                nodes = self.nodes(peer(1, [radio(freq=bad)]), peer(2, [radio(channel=bad)]))
                self.assertEqual(peer_frequencies('5', nodes, OWN), {})

    def test_own_record_recognized_by_registry_key_without_aliases(self):
        own = {**peer(1, [radio(44)]), 'MAC_ADDRESS': '', 'MAC_ADDRESSES': ''}
        self.assertEqual(peer_frequencies('5', {'020000000001': own}, OWN), {})
        self.assertEqual(peer_frequencies('5', {'020000000199': own}, OWN), {5220: 1})

    def test_one_peer_counts_once_even_with_two_radios_on_the_band(self):
        nodes = self.nodes(peer(1, [radio(44), radio(44, name='wlan3')]), peer(2, [radio(36)]),
                           peer(3, [radio(36)]))
        self.assertEqual(peer_frequencies('5', nodes, OWN), {5220: 1, 5180: 2})

    def test_2ghz_band(self):
        nodes = self.nodes(peer(1, [radio(11, name='wlan0')]))
        self.assertEqual(peer_frequencies('2.4', nodes, OWN), {2462: 1})

    def test_recently_touring_peer_is_not_evidence(self):
        touring = {**peer(1, [radio(36)]), 'LAST_SEEN_TIMESTAMP': '1000',
                   'LAST_TOURGUIDE_TIMESTAMP': '900'}
        long_ago = {**peer(2, [radio(44)]), 'LAST_SEEN_TIMESTAMP': '5000',
                    'LAST_TOURGUIDE_TIMESTAMP': '900'}
        self.assertEqual(peer_frequencies('5', self.nodes(touring, long_ago), OWN), {5220: 1})


class CheckTests(unittest.TestCase):
    def nodes(self, *peers):
        return {f'n{i}': p for i, p in enumerate(peers)}

    def test_plan_confirmed_by_peers(self):
        nodes = self.nodes(peer(1, [radio(44)]), peer(2, [radio(44)]), peer(3, [radio(36)]))
        self.assertEqual(check_frequency('5', 5220, nodes, OWN), ('confirmed', 2, 5180))

    def test_plan_contradicted_by_more_peers(self):
        nodes = self.nodes(peer(1, [radio(44)]), peer(2, [radio(44)]), peer(3, [radio(36)]))
        self.assertEqual(check_frequency('5', 5180, nodes, OWN), ('conflict', 1, 5220))

    def test_equal_split_is_not_a_conflict(self):
        nodes = self.nodes(peer(1, [radio(44)]), peer(2, [radio(36)]))
        self.assertEqual(check_frequency('5', 5180, nodes, OWN)[0], 'confirmed')

    def test_no_peers_on_band(self):
        self.assertEqual(check_frequency('5', 5180, {}, OWN), ('unknown', 0, None))
        nodes = self.nodes(peer(1, [radio(44)]))
        self.assertEqual(check_frequency('5', 5745, nodes, OWN), ('conflict', 0, 5220))


class RegistryFileTests(unittest.TestCase):
    def test_reads_builder_output_including_escaped_quotes(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'registry'
            path.write_text("# header\n"
                            "NODE_020000000101_HOSTNAME='bob'\\''s'\n"
                            "NODE_020000000101_INTERFACES_JSON='[{\"role\": \"mesh\"}]'\n"
                            "garbage line\n")
            nodes = load_registry(path)
        self.assertEqual(nodes['020000000101']['HOSTNAME'], "bob's")
        self.assertEqual(json.loads(nodes['020000000101']['INTERFACES_JSON']), [{'role': 'mesh'}])

    def test_missing_file_is_empty(self):
        self.assertEqual(load_registry('/nonexistent/registry'), {})


if __name__ == '__main__':
    unittest.main()
