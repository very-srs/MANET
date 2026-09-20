#!/usr/bin/env python3
"""Cold-boot discovery with controlled time and no live network operations."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('mesh_ip_startup', TOOLS / 'mesh-ip-startup.py')
startup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(startup)
OWN = '02:00:00:00:00:01'
PEER = '02:00:00:00:00:02'
RADIO = '02:00:00:00:01:02'
READY_ADDRESS = [{'addr_info': [{'family': 'inet6', 'scope': 'link', 'local': 'fe80::1'}]}]
READY_ALFRED = '- mode: primary\n- interface: br0\n\t- status: active\n'


def node(mac, aliases='', identity=True, ip=''):
    prefix = 'NODE_' + mac.replace(':', '')
    return (f"{prefix}_HOSTNAME='{('mesh-' + mac[-2:]) if identity else ''}'\n"
            f"{prefix}_MAC_ADDRESSES='{mac}{',' + aliases if aliases else ''}'\n"
            f"{prefix}_IPV4_CHUNK='0'\n{prefix}_IPV4_ADDRESS='{ip}'\n")


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        root = Path(self.scratch.name)
        self.registry = root / 'registry'
        self.registry.write_text(node(OWN))
        self.mac = root / 'mac'
        self.mac.write_text(OWN)
        self.builder = '/test/registry-builder'
        self.responses = {
            (self.builder,): '',
            ('ip', '-j', '-6', 'addr', 'show', 'dev', 'br0'): json.dumps(READY_ADDRESS),
            ('alfred', '-S'): READY_ALFRED,
            ('batctl', 'meshif', 'bat0', 'originators_json'): '[]',
        }
        self.calls = []

    def run_command(self, *args, **kwargs):
        self.calls.append(args)
        response = self.responses[args]
        if isinstance(response, Exception):
            raise response
        return response

    def check(self, now, state=None):
        with patch.object(startup, 'command', side_effect=self.run_command), \
                patch.object(startup.time, 'monotonic', return_value=now):
            return startup.check(state or {}, self.registry, self.builder, self.mac)

    def peers(self, *macs):
        self.responses[('batctl', 'meshif', 'bat0', 'originators_json')] = json.dumps(
            [{'orig_address': mac} for mac in macs])

    def test_solo_waits_one_alfred_period_and_restarts_after_reboot(self):
        state, ready, _ = self.check(100)
        self.assertFalse(ready)
        state, ready, _ = self.check(109, state)
        self.assertFalse(ready)
        state, ready, _ = self.check(110, state)
        self.assertTrue(ready)
        # /run is empty next boot even if /etc/mesh_ipv4_state survives.
        _, ready, _ = self.check(500, {})
        self.assertFalse(ready)

    def test_empty_registry_waits_for_own_publication(self):
        self.registry.write_text('')
        state, ready, message = self.check(1000)
        self.assertFalse(ready)
        self.assertEqual(state, {})
        self.assertIn('our initial', message)

    def test_simultaneous_boots_do_not_require_peer_ipv4(self):
        self.peers(RADIO, RADIO)  # two routes to one originator
        self.registry.write_text(node(OWN) + node(PEER, aliases=RADIO))
        state, ready, _ = self.check(0)
        self.assertFalse(ready)
        state, ready, _ = self.check(10, state)
        self.assertTrue(ready)
        self.assertEqual(state['peers'], [RADIO])

    def test_late_peer_with_records_does_not_restart_the_window(self):
        state, _, _ = self.check(0)
        self.peers(RADIO)
        self.registry.write_text(node(OWN) + node(PEER, aliases=RADIO, ip='10.30.0.6'))
        state, ready, _ = self.check(9, state)
        self.assertFalse(ready)
        self.assertEqual(state['since'], 0)
        _, ready, _ = self.check(10, state)
        self.assertTrue(ready)

    def test_missing_records_wait_only_until_the_original_deadline(self):
        self.peers(PEER)
        self.registry.write_text(node(OWN) + node(PEER, identity=False))
        state, _, _ = self.check(0)
        state, ready, message = self.check(10, state)
        self.assertFalse(ready)
        self.assertIn(PEER, message)
        state, ready, _ = self.check(19, state)
        self.assertFalse(ready)
        state, ready, message = self.check(20, state)
        self.assertTrue(ready)
        self.assertIn('deadline reached', message)
        self.assertIn(PEER, message)
        self.assertEqual(state['since'], 0)

    def test_receiving_missing_records_can_finish_before_deadline(self):
        self.peers(PEER)
        state, _, _ = self.check(0)
        state, ready, _ = self.check(10, state)
        self.assertFalse(ready)
        self.registry.write_text(node(OWN) + node(PEER))
        _, ready, _ = self.check(12, state)
        self.assertTrue(ready)

    def test_continuous_peer_churn_cannot_extend_deadline(self):
        state, _, _ = self.check(0)
        for now in (5, 10, 15, 19, 20):
            # Each sample sees a different peer with no Alfred records.
            mac = f'02:00:00:00:01:{now:02x}'
            self.peers(mac)
            state, ready, message = self.check(now, state)
            self.assertEqual(state['since'], 0)
            self.assertEqual(ready, now == 20)
        self.assertIn(mac, message)

    def test_departing_peer_does_not_restart_or_leave_a_missing_requirement(self):
        self.peers(PEER)
        state, _, _ = self.check(0)
        state, ready, _ = self.check(10, state)
        self.assertFalse(ready)
        self.peers()
        _, ready, _ = self.check(11, state)
        self.assertTrue(ready)

    def test_unusable_ipv6_blocks_allocation_without_resetting_elapsed_time(self):
        state, _, _ = self.check(0)
        key = ('ip', '-j', '-6', 'addr', 'show', 'dev', 'br0')
        for flag in ('tentative', 'dadfailed'):
            with self.subTest(flag=flag):
                address = dict(READY_ADDRESS[0]['addr_info'][0], **{flag: True})
                self.responses[key] = json.dumps([{'addr_info': [address]}])
                pending, ready, _ = self.check(30, state)
                self.assertFalse(ready)
                self.assertEqual(pending, state)
        self.responses[key] = json.dumps(READY_ADDRESS)
        _, ready, _ = self.check(31, pending)
        self.assertTrue(ready)

    def test_alfred_must_be_primary_and_active_on_br0(self):
        for status in (READY_ALFRED.replace('primary', 'secondary'),
                       READY_ALFRED.replace('active', 'inactive'),
                       READY_ALFRED.replace('br0', 'bat0'), ''):
            with self.subTest(status=status):
                self.responses[('alfred', '-S')] = status
                state, ready, _ = self.check(100)
                self.assertFalse(ready)
                self.assertEqual(state, {})

    def test_deadline_cannot_bypass_local_alfred_or_own_publication(self):
        state, _, _ = self.check(0)
        self.responses[('alfred', '-S')] = READY_ALFRED.replace('active', 'inactive')
        pending, ready, _ = self.check(30, state)
        self.assertFalse(ready)
        self.assertEqual(pending, state)
        self.responses[('alfred', '-S')] = READY_ALFRED
        self.registry.write_text('')
        pending, ready, _ = self.check(31, pending)
        self.assertFalse(ready)
        self.assertEqual(pending, state)
        self.registry.write_text(node(OWN))
        _, ready, _ = self.check(32, pending)
        self.assertTrue(ready)

    def test_query_errors_and_invalid_originators_do_not_mean_solo(self):
        key = ('batctl', 'meshif', 'bat0', 'originators_json')
        for response in (subprocess.CalledProcessError(1, 'batctl'), '{}',
                         '[{"orig_address": "bad"}]'):
            with self.subTest(response=response):
                self.responses[key] = response
                with self.assertRaises((subprocess.SubprocessError, ValueError)):
                    self.check(100)

    def test_registry_is_refreshed_even_after_discovery_completed(self):
        _, ready, _ = self.check(100, {'complete': True})
        self.assertTrue(ready)
        self.assertEqual(self.calls, [(self.builder,)])
        self.responses[(self.builder,)] = subprocess.CalledProcessError(1, self.builder)
        with self.assertRaises(subprocess.SubprocessError):
            self.check(101, {'complete': True})

    def test_failed_read_preserves_saved_wait_and_returns_pending_past_deadline(self):
        state_path = Path(self.scratch.name) / 'state'
        for state in ({'since': 0, 'peers': []}, {'complete': True}):
            with self.subTest(state=state):
                state_path.write_text(json.dumps(state))
                with patch.dict('os.environ', {'MESH_IP_STARTUP_STATE': str(state_path)}), \
                        patch.object(startup.time, 'monotonic', return_value=30), \
                        patch.object(startup, 'check', side_effect=subprocess.CalledProcessError(1, 'alfred')):
                    self.assertEqual(startup.main(), 1)
                self.assertEqual(json.loads(state_path.read_text()), state)

    def test_successful_read_after_transient_failure_uses_original_deadline(self):
        state, _, _ = self.check(0)
        self.responses[(self.builder,)] = subprocess.CalledProcessError(1, self.builder)
        with self.assertRaises(subprocess.SubprocessError):
            self.check(5, state)
        self.responses[(self.builder,)] = ''
        _, ready, _ = self.check(10, state)
        self.assertTrue(ready)

    def test_registry_strings_are_parsed_without_execution(self):
        marker = Path(self.scratch.name) / 'should-not-exist'
        payload = node(OWN).replace('mesh-01', f'$(touch {marker})')
        self.assertEqual(startup.registry_macs(payload), {OWN})
        self.assertFalse(marker.exists())

    def test_pending_discovery_prevents_any_allocator_side_effects(self):
        # Execute the real main entry point. Reaching any hardware/config
        # command after the guard would leave a marker and fail this test.
        source = (TOOLS / 'mesh-ip-manager.sh').read_text().split('# --- Main Logic ---\n', 1)[1]
        marker = Path(self.scratch.name) / 'hardware-touched'
        helper = Path(self.scratch.name) / 'pending.py'
        helper.write_text('raise SystemExit(1)\n')
        prefix = ('log() { :; }\n'
                  'cat() { touch "$TEST_MARKER"; }\n'
                  'ip() { touch "$TEST_MARKER"; }\n'
                  'cleanup_control_aliases() { touch "$TEST_MARKER"; }\n')
        result = subprocess.run(['bash', '-c', prefix + source], capture_output=True,
                                text=True, env={'PATH': '/usr/bin:/bin',
                                                'STARTUP_HELPER': str(helper),
                                                'TEST_MARKER': str(marker)}, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
