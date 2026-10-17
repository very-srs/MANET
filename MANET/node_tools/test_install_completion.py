"""Regressions found on a freshly provisioned CM4 and its first normal boot."""
import json
import os
from pathlib import Path
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
    """Check setup locking, completion and first-boot service ordering."""
    def setup_lock_prefix(self, root):
        """Run the real lock prefix beside its installed shell libraries."""
        for name in ('manet-common.sh', 'manet-runtime-client.sh'):
            (root / name).write_text((TOOLS / name).read_text())
        source = (TOOLS / 'radio-setup.sh').read_text()
        return source[:source.index('# Refuse a changed flash-time AP name')].replace(
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
        self.assertIn('start manet-cpu-policy.service', result.stdout)
        for unit in ('mesh-clone-identity', 'mesh-boot-lobby', 'mesh-shutdown'):
            self.assertNotIn('start ' + unit, result.stdout)

    def test_setup_retires_old_cpu_unit_before_applying_new_policy(self):
        source = (TOOLS / 'radio-setup.sh').read_text()
        begin = source.index('# Replace the old CPU policy')
        end = source.index('cat << EOF', begin)
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            unit = root / 'cpu-powersave.service'
            unit.write_text('old unit')
            link = root / 'multi-user.target.wants' / unit.name
            link.parent.mkdir()
            link.symlink_to('../' + unit.name)
            body = '''
systemctl() { echo "$*"; }
provision_try() { shift; "$@"; }
''' + source[begin:end].replace('/etc/systemd/system', str(root))
            result = subprocess.run(['bash', '-c', body], capture_output=True,
                                    text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(unit.exists())
            self.assertFalse(link.is_symlink())
            self.assertLess(result.stdout.index('disable --now cpu-powersave.service'),
                            result.stdout.index('daemon-reload'))
            self.assertLess(result.stdout.index('daemon-reload'),
                            result.stdout.index('enable --now manet-cpu-policy.service'))


class CpuPolicyTests(unittest.TestCase):
    """Apply the installed script to temporary CPU and device-tree files."""

    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='manet-cpu-policy-')
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.cpu = self.root / 'cpu'
        self.cpu.mkdir()
        self.model = self.root / 'model'
        self.model.write_bytes(b'Raspberry Pi Compute Module 4 Rev 1.0\0')

    def make_policy(self, name='policy0', minimum=600000, maximum=1500000,
                    frequencies='1500000 600000 800000 1000000',
                    governors='conservative ondemand powersave schedutil'):
        policy = self.cpu / 'cpufreq' / name
        policy.mkdir(parents=True)
        values = {'cpuinfo_min_freq': minimum, 'cpuinfo_max_freq': maximum,
                  'scaling_max_freq': minimum, 'scaling_governor': 'powersave',
                  'scaling_available_governors': governors}
        if frequencies is not None:
            values['scaling_available_frequencies'] = frequencies
        for filename, value in values.items():
            (policy / filename).write_text(str(value) + '\n')
        return policy

    def run_policy(self, *args):
        env = dict(os.environ, MANET_SYS_CPU=str(self.cpu),
                   MANET_CPU_MODEL_FILE=str(self.model))
        return subprocess.run(['bash', str(TOOLS / 'manet-cpu-policy.sh'), *args],
                              env=env, capture_output=True, text=True, timeout=5)

    def assert_policy(self, policy, governor, cap):
        self.assertEqual((policy / 'scaling_governor').read_text().strip(), governor)
        self.assertEqual((policy / 'scaling_max_freq').read_text().strip(), str(cap))

    def test_cm4_uses_schedutil_and_800mhz_on_every_policy(self):
        policies = [self.make_policy(name) for name in ('policy0', 'policy4')]
        result = self.run_policy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        for policy in policies:
            self.assert_policy(policy, 'schedutil', 800000)
            self.assertIn('board="Raspberry Pi Compute Module 4 Rev 1.0" '
                          f'{policy.name} governor=schedutil cap=800000 kHz',
                          result.stdout)

    def test_cm4_uses_highest_available_frequency_below_ceiling(self):
        policy = self.make_policy(frequencies='1000000 600000 900000 750000')
        result = self.run_policy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_policy(policy, 'schedutil', 750000)

    def test_cm4_never_caps_below_cpuinfo_minimum(self):
        policy = self.make_policy(minimum=900000, frequencies='600000 900000 1500000')
        result = self.run_policy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_policy(policy, 'schedutil', 900000)

    def test_cm4_without_frequency_table_uses_ceiling(self):
        policy = self.make_policy(frequencies=None)
        result = self.run_policy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_policy(policy, 'schedutil', 800000)

    def test_other_boards_restore_their_reported_maximum(self):
        policies = [self.make_policy('policy0', minimum=408000, maximum=1800000),
                    self.make_policy('policy4', minimum=600000, maximum=2400000)]
        for model in ('Radxa ROCK 3A', 'Raspberry Pi 5 Model B Rev 1.0',
                      'Another board', None):
            with self.subTest(model=model):
                if model is None:
                    self.model.unlink()
                else:
                    self.model.write_bytes(model.encode() + b'\0')
                for policy in policies:
                    (policy / 'scaling_max_freq').write_text('816000\n')
                    (policy / 'scaling_governor').write_text('powersave\n')
                result = self.run_policy()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, '')
                self.assert_policy(policies[0], 'schedutil', 1800000)
                self.assert_policy(policies[1], 'schedutil', 2400000)

    def test_offline_cpus_come_online_even_without_cpufreq(self):
        for cpu in range(4):
            path = self.cpu / f'cpu{cpu}'
            path.mkdir()
            (path / 'online').write_text('1\n' if cpu < 2 else '0\n')
        for cpufreq in (False, True):
            with self.subTest(cpufreq=cpufreq):
                if cpufreq:
                    self.make_policy()
                for cpu in (2, 3):
                    (self.cpu / f'cpu{cpu}/online').write_text('0\n')
                result = self.run_policy()
                self.assertEqual(result.returncode, 0, result.stderr)
                for cpu in range(4):
                    self.assertEqual((self.cpu / f'cpu{cpu}/online').read_text(), '1\n')

    def test_missing_hotplug_controls_and_cpufreq_are_supported(self):
        for cpufreq in (False, True):
            with self.subTest(cpufreq=cpufreq):
                if cpufreq:
                    policy = self.make_policy()
                result = self.run_policy()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('no CPU hotplug controls', result.stdout)
                if cpufreq:
                    self.assert_policy(policy, 'schedutil', 800000)
                else:
                    self.assertIn('no cpufreq policies', result.stdout)

    def test_missing_schedutil_falls_back_to_ondemand_then_kernel_choice(self):
        policy = self.make_policy()
        for governors, expected in [('powersave ondemand', 'ondemand'),
                                    ('powersave performance', 'powersave'),
                                    ('powersave schedutil-other', 'powersave')]:
            with self.subTest(governors=governors):
                (policy / 'scaling_available_governors').write_text(governors + '\n')
                (policy / 'scaling_governor').write_text('powersave\n')
                result = self.run_policy()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_policy(policy, expected, 800000)

    def test_failed_write_is_reported_and_other_policies_still_apply(self):
        broken = self.make_policy()
        maximum = broken / 'scaling_max_freq'
        maximum.unlink()
        maximum.mkdir()
        working = self.make_policy('policy4')
        result = self.run_policy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ERROR: CPU policy: policy0: cannot set frequency ceiling',
                      result.stderr)
        self.assert_policy(working, 'schedutil', 800000)

    def test_unexpected_arguments_do_not_change_policy(self):
        policy = self.make_policy()
        result = self.run_policy('--unknown')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('usage: manet-cpu-policy.sh', result.stderr)
        self.assert_policy(policy, 'powersave', 600000)



if __name__ == '__main__':
    unittest.main()
