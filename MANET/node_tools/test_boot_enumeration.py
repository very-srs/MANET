"""Boot discovery with controlled clocks and device observations; no hardware writes."""
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('wait_radios', TOOLS / 'manet-wait-radios.py')
radios = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(radios)


def observation(*names):
    return (tuple(f'phy#{i}' for i, _ in enumerate(names)),
            tuple((f'phy#{i}', name, 'driver', f'02:00:00:00:00:{i:02x}', i + 1)
                  for i, name in enumerate(names)))


class RadioEnumerationTests(unittest.TestCase):
    def setUp(self):
        self.now = 0

    def sleep(self, seconds):
        self.now += seconds

    def wait(self, reader):
        with patch.object(radios.time, 'monotonic', side_effect=lambda: self.now), \
                patch.object(radios.time, 'sleep', side_effect=self.sleep), \
                patch.object(radios, 'snapshot', side_effect=lambda remaining: reader(self.now)):
            return radios.wait_for_radios()

    def test_late_radio_resets_quiet_period(self):
        names = self.wait(lambda t: (observation('wlan0') if t < 8 else observation('wlan0', 'wlan1'), True))
        self.assertEqual(names, ('wlan0', 'wlan1'))
        self.assertEqual(self.now, 13)

    def test_same_count_replacement_is_not_stability(self):
        names = self.wait(lambda t: (observation('wlan0' if t < 8 else 'wlan2'), True))
        self.assertEqual(names, ('wlan2',))
        self.assertEqual(self.now, 13)

    def test_driver_binding_and_udev_must_finish(self):
        self.assertEqual(self.wait(lambda t: (observation('wlan0'), t >= 14)), ('wlan0',))
        self.assertEqual(self.now, 19)

    def test_first_phy_cannot_end_minimum_observation(self):
        self.wait(lambda t: (observation('wlan0'), True))
        self.assertEqual(self.now, 10)

    def test_wired_only_waits_until_deadline(self):
        self.assertEqual(self.wait(lambda t: (observation(), True)), ())
        self.assertEqual(self.now, 60)

    def test_churn_unbound_driver_and_disappearing_radio_fail_at_deadline(self):
        readers = [lambda t: (observation(f'wlan{int(t) % 2}'), True),
                   lambda t: (observation('wlan0'), False),
                   lambda t: (observation('wlan0') if t < 3 else observation(), True)]
        for reader in readers:
            with self.subTest(reader=reader):
                self.now = 0
                with self.assertRaises(TimeoutError):
                    self.wait(reader)
                self.assertEqual(self.now, 60)

    def test_query_errors_cannot_look_like_wired_only(self):
        def failed(t):
            self.now += 2
            raise subprocess.TimeoutExpired('iw', 2)
        with self.assertRaises(TimeoutError):
            self.wait(failed)
        self.assertEqual(self.now, 60)

    def test_snapshot_requires_mac_driver_and_every_phy_netdev(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / 'wlan0'
            (device / 'device').mkdir(parents=True)
            (device / 'address').write_text('02:00:00:00:00:01\n')
            (device / 'ifindex').write_text('3\n')
            (root / 'brcmfmac').mkdir()
            text = 'phy#0\n\tInterface wlan0\n'
            def command(args, **kwargs):
                self.assertLessEqual(kwargs['timeout'], 2)
                return subprocess.CompletedProcess(args, 0, stdout=text if args[0] == 'iw' else '')
            with patch.object(radios, 'NET', root), patch.object(radios.subprocess, 'run', side_effect=command):
                self.assertFalse(radios.snapshot(5)[1])
                (device / 'device/driver').symlink_to(root / 'brcmfmac')
                state, ready = radios.snapshot(5)
                self.assertTrue(ready)
                self.assertEqual(state[1][0][2], 'brcmfmac')
                text += 'phy#1\n'
                self.assertFalse(radios.snapshot(5)[1])

    def test_failed_wait_stops_setup_before_overwriting_roles(self):
        source = (TOOLS / 'radio-setup.sh').read_text()
        block = source[source.index('if ! python3 /usr/local/bin/manet-wait-radios.py; then'):]
        block = block[:block.index('\nfi') + 3]
        result = subprocess.run(['bash', '-c', '''
python3() { return 1; }
provision_fail() { echo failure; }
provision_state() { echo "$1"; }
''' + block + '\necho OVERWROTE_ROLES\n'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('incomplete', result.stdout)
        self.assertNotIn('OVERWROTE_ROLES', result.stdout)


class Cm4DiskTests(unittest.TestCase):
    def run_wait(self, scenario, ready_at=0):
        source = (TOOLS.parent / 'provisioning/linux-flasher.sh').read_text()
        function = re.search(r'^wait_for_cm4_disk\(\) \{\n.*?^\}', source, re.M | re.S)[0]
        mocks = r'''
SECONDS=0
sleep() { SECONDS=$((SECONDS + $1)); }
timeout() { shift; "$@"; }
lsblk() {
    [[ "$SCENARIO" != error ]] || return 1
    echo 'sda disk 1000 0'
    echo 'loop0 loop 99999 0'
    (( SECONDS >= READY_AT )) || return 0
    case "$SCENARIO" in
        ready) echo 'sdb disk 32000000000 0' ;;
        empty) echo 'sdb disk 0 0' ;;
        readonly) echo 'sdb disk 32000000000 1' ;;
        two) printf 'sdb disk 32000000000 0\nsdc disk 64000000000 0\n' ;;
        two_unready) printf 'sdb disk 32000000000 0\nsdc disk 0 0\n' ;;
        disappears) (( SECONDS != READY_AT )) || echo 'sdb disk 32000000000 0' ;;
    esac
    return 0
}
'''
        return subprocess.run(['bash', '-c', mocks + function + '\nwait_for_cm4_disk "sda"\n'],
                              capture_output=True, text=True, timeout=10,
                              env=dict(os.environ, SCENARIO=scenario, READY_AT=str(ready_at)))

    def test_disk_arriving_after_old_timeout_is_selected(self):
        result = self.run_wait('ready', 27)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'sdb\n')

    def test_existing_empty_readonly_and_disappearing_disks_are_not_selected(self):
        for scenario in ('none', 'empty', 'readonly', 'disappears'):
            with self.subTest(scenario=scenario):
                result = self.run_wait(scenario, 10)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('60 seconds', result.stderr)

    def test_ambiguous_disks_and_enumeration_error_fail(self):
        for scenario, status in [('two', 2), ('two_unready', 2), ('error', 3)]:
            result = self.run_wait(scenario)
            self.assertEqual(result.returncode, status)
            self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
