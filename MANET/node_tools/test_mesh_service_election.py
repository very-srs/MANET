#!/usr/bin/env python3
"""mesh-service-election.py: shared ranking for MediaMTX and Mumble."""

import importlib.util
import itertools
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('mesh_service_election', TOOLS / 'mesh-service-election.py')
election = importlib.util.module_from_spec(spec)
spec.loader.exec_module(election)

NOW = 10000.0
A, B, C = '02:00:00:00:00:01', '02:00:00:00:00:02', '02:00:00:00:00:03'


def node(mac, mbps, age=0, state=None, server=None, service='MEDIAMTX'):
    key = 'NODE_' + mac.replace(':', '')
    lines = [f"{key}_MAC_ADDRESS='{mac}'", f"{key}_MEAN_THROUGHPUT_MBPS='{mbps}'",
             f"{key}_OBSERVED_AT_UPTIME='{NOW - age:g}'"]
    if state:
        lines.append(f"{key}_NODE_STATE='{state}'")
    if server is not None:
        lines.append(f"{key}_IS_{service}_SERVER='{'true' if server else 'false'}'")
    return lines


def elect(*nodes, service='mediamtx'):
    return election.elect(service, '\n'.join(line for n in nodes for line in n), NOW)


class RankingTests(unittest.TestCase):
    def test_best_metric_wins_and_ties_go_to_the_lowest_mac(self):
        self.assertEqual(elect(node(A, 10), node(B, 50))[0], B)
        self.assertEqual(elect(node(B, 20), node(A, 20))[0], A)

    def test_incumbent_bias_keeps_a_healthy_host(self):
        winner, score, incumbent = elect(node(A, 45, server=True), node(B, 50))
        self.assertEqual((winner, score, incumbent), (A, 55, A))
        self.assertEqual(elect(node(A, 30, server=True), node(B, 50))[0], B)

    def test_each_service_reads_its_own_server_flag(self):
        mumble = node(A, 45, server=True, service='MUMBLE')
        self.assertEqual(elect(mumble, node(B, 50), service='mumble')[0], A)
        self.assertEqual(elect(mumble, node(B, 50), service='mediamtx')[0], B)


class EligibilityTests(unittest.TestCase):
    def test_shutting_down_and_stale_never_win_or_hold_bias(self):
        for state in ('SHUTTING_DOWN', 'STALE'):
            with self.subTest(state=state):
                winner, _, incumbent = elect(node(A, 0, state=state, server=True), node(B, 0))
                self.assertEqual((winner, incumbent), (B, None))

    def test_real_shutdown_tombstone(self):
        # What encoder.py telemetry --node-state SHUTTING_DOWN publishes:
        # throughput 0, server flags false (Codex 049).
        winner, _, _ = elect(node(A, 0.0, state='SHUTTING_DOWN', server=False), node(B, 0.0))
        self.assertEqual(winner, B)

    def test_observed_age_cutoff_is_300_seconds(self):
        self.assertEqual(elect(node(A, 90, age=300, server=True), node(B, 10))[0], A)
        self.assertEqual(elect(node(A, 90, age=301, server=True), node(B, 10))[0], B)

    def test_frozen_active_record_ages_out_regardless_of_its_label(self):
        winner, _, incumbent = elect(node(A, 90, age=400, state='ACTIVE', server=True), node(B, 10))
        self.assertEqual((winner, incumbent), (B, None))

    def test_future_or_invalid_observation_times_are_ineligible(self):
        for age in (-1, float('nan'), NOW + 1):
            with self.subTest(age=age):
                self.assertEqual(elect(node(A, 90, age=age), node(B, 10))[0], B)
        bad = [line.replace("'10000'", "'soon'") for line in node(A, 90)]
        self.assertEqual(elect(bad, node(B, 10))[0], B)

    def test_invalid_local_uptime_is_an_input_error(self):
        for uptime in (float('nan'), float('inf'), -1.0):
            with self.subTest(uptime=uptime), self.assertRaises(ValueError):
                election.elect('mediamtx', '\n'.join(node(A, 5)), uptime)

    def test_invalid_metrics_or_macs_are_ineligible(self):
        for metric in ('nan', 'inf', '-1', 'fast', ''):
            with self.subTest(metric=metric):
                self.assertEqual(elect(node(A, metric), node(B, 1))[0], B)
        bad = [line.replace(f"'{A}'", "'not-a-mac'") for line in node(A, 90)]
        self.assertEqual(elect(bad, node(B, 1))[0], B)

    def test_nobody_eligible(self):
        self.assertEqual(elect(), (None, None, None))
        self.assertEqual(elect(node(A, 5, state='STALE')), (None, None, None))


class DeterminismTests(unittest.TestCase):
    def test_multiple_incumbents_resolve_the_same_in_any_order(self):
        # Codex 054: after a partition merge two nodes can both advertise the
        # service; first-line-wins differed between nodes.
        nodes = [node(A, 40, server=True), node(B, 40, server=True), node(C, 45)]
        results = set()
        for order in itertools.permutations(nodes):
            lines = [line for n in order for line in n]
            results.add(elect(*order))
            # Field lines in reverse and interleaved order, not grouped by node.
            results.add(election.elect('mediamtx', '\n'.join(reversed(lines)), NOW))
            results.add(election.elect('mediamtx', '\n'.join(lines[::2] + lines[1::2]), NOW))
        self.assertEqual(results, {(A, 50, A)})

    def test_better_advertised_incumbent_takes_the_bias(self):
        winner, _, incumbent = elect(node(A, 30, server=True), node(B, 41, server=True), node(C, 50))
        self.assertEqual((winner, incumbent), (B, B))


class CommandTests(unittest.TestCase):
    def run_cli(self, registry_text, uptime='10000.5 1.0\n', args=('mediamtx',)):
        with tempfile.TemporaryDirectory() as scratch:
            registry = Path(scratch) / 'registry'
            registry.write_text(registry_text)
            uptime_file = Path(scratch) / 'uptime'
            uptime_file.write_text(uptime)
            return subprocess.run([sys.executable, str(TOOLS / 'mesh-service-election.py'), *args, str(registry)],
                                  capture_output=True, text=True, timeout=10,
                                  env=dict(os.environ, MESH_UPTIME_FILE=str(uptime_file)))

    def test_prints_winner_score_incumbent(self):
        result = self.run_cli('\n'.join(node(A, 45, server=True) + node(B, 50)))
        self.assertEqual((result.returncode, result.stdout), (0, f'{A} 55 {A}\n'))
        result = self.run_cli('')
        self.assertEqual(result.stdout, '- - -\n')

    def test_unreadable_inputs_print_nothing(self):
        for uptime in ('garbage', 'nan 1.0', '-5 1.0'):
            with self.subTest(uptime=uptime):
                result = self.run_cli('\n'.join(node(A, 45)), uptime=uptime)
                self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertEqual(self.run_cli('', args=('unknown',)).returncode, 2)

    def test_skipped_nodes_are_explained(self):
        result = self.run_cli('\n'.join(node(A, 45, state='SHUTTING_DOWN') + node(B, 5)))
        self.assertIn(f'Skipping {A}: shutting down', result.stderr)


if __name__ == '__main__':
    unittest.main()
