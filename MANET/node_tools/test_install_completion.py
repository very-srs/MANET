"""Regressions found on a freshly provisioned CM4 and its first normal boot."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import manet_node_ipv4 as address

TOOLS = Path(__file__).resolve().parent


class NodeAddressTests(unittest.TestCase):
    def setUp(self):
        self.state = {'PERSISTENT_IPV4': '10.30.2.136', 'PERSISTENT_CHUNK': '26',
                      'PERSISTENT_NETWORK': '10.30.2.0/24'}
        self.config = {'ipv4_network': '10.30.2.0/24'}

    def test_service_vip_and_eud_gateway_never_replace_the_assigned_primary(self):
        for ips in (['10.30.2.2', '10.30.2.136', '10.30.2.137'],
                    ['10.30.2.137', '10.30.2.2', '10.30.2.136']):
            self.assertEqual(address.primary_ipv4(self.state, self.config, ips), '10.30.2.136')

    def test_remembered_allocation_is_not_published_before_it_is_installed(self):
        for ips in ([], ['10.30.2.2'], ['10.30.2.137']):
            self.assertEqual(address.primary_ipv4(self.state, self.config, ips), '')

    def test_changed_network_invalidates_the_old_allocation(self):
        self.assertEqual(address.primary_ipv4(self.state, {'ipv4_network': '10.40.0.0/24'},
                                             ['10.30.2.136']), '')

    def test_missing_invalid_or_service_reserved_state_is_not_an_allocation(self):
        for state in ({}, dict(self.state, PERSISTENT_CHUNK=''),
                      dict(self.state, PERSISTENT_IPV4='10.30.2.2'),
                      dict(self.state, PERSISTENT_IPV4='10.30.2.255'),
                      dict(self.state, PERSISTENT_IPV4='not-an-ip')):
            self.assertEqual(address.primary_ipv4(state, self.config,
                                                 ['10.30.2.136', '10.30.2.2', '10.30.2.255']), '')

    def test_chunk_zero_is_valid(self):
        state = dict(self.state, PERSISTENT_CHUNK='0', PERSISTENT_IPV4='10.30.2.6')
        self.assertEqual(address.primary_ipv4(state, self.config, ['10.30.2.6']), '10.30.2.6')

    def test_interface_query_failure_is_not_an_empty_interface(self):
        with patch.object(address, 'read_values', side_effect=[self.state, self.config]), \
                patch.object(address.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ip', 3)):
            with self.assertRaises(subprocess.TimeoutExpired):
                address.current_ipv4()

    def test_real_address_reader_ignores_list_order(self):
        response = subprocess.CompletedProcess([], 0, json.dumps([{'addr_info': [
            {'family': 'inet', 'local': ip} for ip in ['10.30.2.2', '10.30.2.136']]}]))
        with patch.object(address, 'read_values', side_effect=[self.state, self.config]), \
                patch.object(address.subprocess, 'run', return_value=response):
            self.assertEqual(address.current_ipv4(), '10.30.2.136')


class CompletionTests(unittest.TestCase):
    def setup_lock_prefix(self, root):
        source = (TOOLS / 'radio-setup.sh').read_text()
        return source[:source.index('# Append setup output')].replace(
            '/run/lock/manet-radio-setup.lock', str(root / 'setup.lock'))

    def test_overlapping_setup_does_not_enter_or_clear_state_and_retry_keeps_exit_code(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            script = root / 'setup.sh'
            script.write_text(self.setup_lock_prefix(root) + '''
echo entered >> "$1"
if [[ ${2:-} == hold ]]; then
    touch "$1.ready"
    while [[ ! -e "$1.release" ]]; do sleep 0.02; done
fi
exit "${3:-0}"
''')
            state = root / 'state'
            env = dict(os.environ)
            env.pop('MANET_RADIO_SETUP_LOCKED', None)
            first = subprocess.Popen(['bash', str(script), str(state), 'hold', '7'], env=env)
            try:
                deadline = time.monotonic() + 5
                while not (root / 'state.ready').exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((root / 'state.ready').exists())
                second = subprocess.run(['bash', str(script), str(state)], env=env, timeout=2)
                self.assertEqual(second.returncode, 0)
                self.assertEqual(state.read_text(), 'entered\n')
                (root / 'state.release').touch()
                self.assertEqual(first.wait(timeout=3), 7)
                retry = subprocess.run(['bash', str(script), str(state)], env=env, timeout=2)
                self.assertEqual(retry.returncode, 0)
                self.assertEqual(state.read_text(), 'entered\nentered\n')
            finally:
                (root / 'state.release').touch()
                first.wait(timeout=3)

    def test_background_child_cannot_keep_setup_locked(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            script = root / 'setup.sh'
            script.write_text(self.setup_lock_prefix(root) + '''
sleep 30 >/dev/null 2>&1 &
echo $! > "$1"
''')
            child_file = root / 'child'
            env = dict(os.environ)
            env.pop('MANET_RADIO_SETUP_LOCKED', None)
            try:
                subprocess.run(['bash', str(script), str(child_file)], env=env, check=True, timeout=3)
                result = subprocess.run(['flock', '-n', str(root / 'setup.lock'), 'true'], timeout=2)
                self.assertEqual(result.returncode, 0)
            finally:
                if child_file.exists():
                    os.kill(int(child_file.read_text()), 15)

    def test_rename_reuses_first_boot_unit_and_only_reboots_when_enabled(self):
        source = (TOOLS / 'radio-setup.sh').read_text()
        start = source.index('if [ "$needs_rerun" -eq 1 ]; then')
        end = source.index('# Mesh (SAE) supplicant config', start)
        with tempfile.TemporaryDirectory() as scratch:
            marker = Path(scratch) / 'reboot-pending'
            branch = source[start:end].replace('/var/lib/radio-setup-reboot-pending', str(marker))
            for succeeds in (True, False):
                marker.unlink(missing_ok=True)
                body = 'needs_rerun=1\n' + '''
systemctl() { echo "systemctl $*"; return ''' + ('0' if succeeds else '1') + '''; }
provision_try() { shift; "$@"; }
''' + branch
                result = subprocess.run(['bash', '-c', body], capture_output=True, text=True)
                self.assertIn('systemctl enable radio-setup-run-once.service', result.stdout)
                self.assertNotIn('radio-setup-rerun.service', result.stdout)
                self.assertEqual(marker.exists(), succeeds)

    def test_rename_reboot_does_not_fall_through_to_completion(self):
        source = (TOOLS / 'radio-setup.sh').read_text()
        start = source.index('if [ "$needs_rerun" -eq 1 ] && [ -f /var/lib/radio-setup-reboot-pending ]; then')
        end = source.index('# Start the services enabled above', start)
        with tempfile.TemporaryDirectory() as scratch:
            marker = Path(scratch) / 'reboot-pending'
            marker.touch()
            body = '''
needs_rerun=1
stage_rename_roles() { :; }
sleep() { :; }
reboot() { echo REBOOT; }
provision_try() { shift; "$@"; }
''' + source[start:end].replace('/var/lib/radio-setup-reboot-pending', str(marker)) + '\necho COMPLETED\n'
            result = subprocess.run(['bash', '-c', body], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('REBOOT', result.stdout)
            self.assertNotIn('COMPLETED', result.stdout)

    def test_runtime_services_start_before_success_and_failure_is_recorded(self):
        source = (TOOLS / 'radio-setup.sh').read_text()
        begin = source.index('# Start the services enabled above on this boot too.')
        end = source.index('# === DID THIS ACTUALLY WORK? ===', begin)
        self.assertLess(end, source.index('provision_state complete'))
        body = '''
systemctl() { echo "$*"; [ "$*" != 'start mesh-status.service' ]; }
provision_try() { local what="$1"; shift; "$@" || echo "FAIL:$what"; }
''' + source[begin:end]
        result = subprocess.run(['bash', '-c', body], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('FAIL:service start failed: mesh-status.service', result.stdout)
        self.assertIn('start batman-enslave-watch.service', result.stdout)
        for unit in ('mesh-clone-identity', 'mesh-boot-lobby', 'mesh-shutdown'):
            self.assertNotIn('start ' + unit, result.stdout)

    def test_cpu_frequency_control_works_without_hotplug_and_restores_board_maximum(self):
        source = (TOOLS / 'radio-setup.sh').read_text()
        unit = source.split('cat << EOF > /etc/systemd/system/cpu-powersave.service\n', 1)[1].split('\nEOF', 1)[0]
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            policy = root / 'cpufreq/policy0'
            policy.mkdir(parents=True)
            (policy / 'cpuinfo_max_freq').write_text('1500000\n')
            for present in (False, True):
                with self.subTest(hotplug=present):
                    if present:
                        for cpu in (2, 3):
                            (root / f'cpu{cpu}').mkdir()
                            (root / f'cpu{cpu}/online').write_text('1\n')
                    for directive, governor, maximum in [('ExecStart', 'powersave', '1008000'),
                                                          ('ExecStop', 'ondemand', '1500000')]:
                        for line in unit.splitlines():
                            if line.startswith(directive + '='):
                                args = shlex.split(line.split('=', 1)[1].replace('/sys/devices/system/cpu', str(root)))
                                result = subprocess.run(args, capture_output=True, text=True)
                                self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual((policy / 'scaling_governor').read_text().strip(), governor)
                        self.assertEqual((policy / 'scaling_max_freq').read_text().strip(), maximum)
                    if present:
                        self.assertEqual((root / 'cpu2/online').read_text().strip(), '1')



if __name__ == '__main__':
    unittest.main()
