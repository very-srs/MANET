#!/usr/bin/env python3
"""Stored static channel plan and the static manager's enforcement of it."""

import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import manet_static_channels as plan_mod


TOOLS = Path(__file__).resolve().parent


class PlanTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name) / 'etc' / 'static-channels.json'

    def test_missing_file_is_the_lobby_defaults(self):
        self.assertEqual(plan_mod.load(self.path), {'2.4': 2412, '5': 5180})

    def test_save_is_persistent_and_returns_the_plan(self):
        self.assertEqual(plan_mod.save('5', 5745, self.path), {'2.4': 2412, '5': 5745})
        self.assertEqual(plan_mod.save('2.4', 2437, self.path), {'2.4': 2437, '5': 5745})
        self.assertEqual(json.loads(self.path.read_text()), {'2.4': 2437, '5': 5745})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_invalid_frequencies_are_refused(self):
        for band, freq in (('2.4', 5180), ('5', 2412), ('2.4', 2484), ('5', 5190),
                           ('5', '5180'), ('5', True), ('6', 5955), ('2.4', 2412.0)):
            with self.subTest(band=band, freq=freq):
                with self.assertRaises(plan_mod.StaticChannelError):
                    plan_mod.save(band, freq, self.path)
        self.assertFalse(self.path.exists())

    def test_malformed_plan_is_an_error_not_a_fallback(self):
        self.path.parent.mkdir(parents=True)
        for text in ('', '{', '[]', '{"2.4": 2412}', '{"2.4": 2412, "5": 5190}',
                     '{"2.4": 2412, "5": 5180, "6": 5955}', '{"2.4": "2412", "5": 5180}'):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaises(plan_mod.StaticChannelError):
                    plan_mod.load(self.path)
                with self.assertRaises(plan_mod.StaticChannelError):
                    plan_mod.save('5', 5745, self.path)
                self.assertEqual(self.path.read_text(), text)

    def test_band_lookup(self):
        self.assertEqual(plan_mod.band_for_frequency(2462), '2.4')
        self.assertEqual(plan_mod.band_for_frequency(5825), '5')
        self.assertIsNone(plan_mod.band_for_frequency(5190))

    def test_cli(self):
        env = dict(os.environ, MANET_STATIC_CHANNELS=str(self.path))
        run = lambda *args: subprocess.run([sys.executable, str(TOOLS / 'manet_static_channels.py'), *args],
                                           env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(run('get', '5').stdout, '5180\n')
        self.assertEqual(run('set', '5', '5200').returncode, 0)
        self.assertEqual(run('get', '5').stdout, '5200\n')
        self.assertEqual(json.loads(run('show').stdout), {'2.4': 2412, '5': 5200})
        self.assertEqual(run('set', '5', '2412').returncode, 1)
        self.assertEqual(run('get', '6').returncode, 2)
        self.path.write_text('{')
        result = run('get', '2.4')
        self.assertEqual(result.returncode, 1)
        self.assertIn('not valid JSON', result.stderr)


class EnforcementTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.wpa = self.root / 'wpa'
        self.wpa.mkdir()
        self.plan = self.root / 'static-channels.json'
        self.lock = self.root / 'channel.lock'
        self.restarts = self.root / 'restarts'
        (self.root / 'mesh_24_if').write_text('wlan0\n')
        (self.root / 'mesh_5_if').write_text('wlan1\n')
        for iface, freq in (('wlan0', 2412), ('wlan1', 5180)):
            for suffix in ('', '-lobby'):
                (self.wpa / f'wpa_supplicant-{iface}{suffix}.conf').write_text(
                    f'network={{\n    frequency={freq}\n}}\n')

    def enforce(self):
        source = (TOOLS / 'node-manager-static.sh').read_text()
        header = re.search(r'^STATIC_CHANNELS=.*?^CHANNEL_LOCK_FILE=.*?\n', source, re.M | re.S)[0]
        functions = ''.join(re.search(rf'^{name}\(\) \{{\n.*?^\}}\n', source, re.M | re.S)[0]
                            for name in ('get_current_freq', 'load_mesh_wpa_confs',
                                         'ensure_static_iface_channel', 'load_static_plan',
                                         'ensure_static_channels'))

        prefix = ('log() { echo "$1" >&2; }\nradio_iface_enabled() { return 0; }\n'
                  'sleep() { :; }\n'
                  f'systemctl() {{ echo "$2" >> "{self.restarts}"; }}\n')
        env = dict(os.environ, MANET_STATIC_CHANNELS=str(self.plan),
                   MANET_STATIC_CHANNELS_TOOL=str(TOOLS / 'manet_static_channels.py'),
                   MANET_ACS_LOCK_FILE=str(self.lock), MANET_WPA_DIR=str(self.wpa),
                   MANET_IFACE_STATE_DIR=str(self.root))
        return subprocess.run(['bash', '-c', prefix + header + functions + 'ensure_static_channels\n'],
                              env=env, capture_output=True, text=True, timeout=10)

    def freq(self, name):
        return re.search(r'frequency=(\d+)', (self.wpa / name).read_text())[1]

    def restarted(self):
        return self.restarts.read_text().split() if self.restarts.exists() else []

    def test_stored_plan_survives_enforcement_and_reaches_lobby(self):
        # The operator's saved change is the plan; enforcement no longer
        # reverts it to constants, and the boot (lobby) copy follows it.
        plan_mod.save('5', 5745, self.plan)
        (self.wpa / 'wpa_supplicant-wlan1.conf').write_text('network={\n    frequency=5745\n}\n')
        result = self.enforce()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.freq('wpa_supplicant-wlan1.conf'), '5745')
        self.assertEqual(self.freq('wpa_supplicant-wlan1-lobby.conf'), '5745')
        self.assertEqual(self.restarted(), [])

    def test_boot_copy_of_old_channel_is_corrected(self):
        plan_mod.save('2.4', 2462, self.plan)
        result = self.enforce()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.freq('wpa_supplicant-wlan0.conf'), '2462')
        self.assertEqual(self.freq('wpa_supplicant-wlan0-lobby.conf'), '2462')
        self.assertEqual(self.restarted(), ['wpa_supplicant@wlan0.service'])

    def test_single_5ghz_mesh_radio_gets_the_5ghz_plan(self):
        # mesh_if would list it first; the role files say it is 5 GHz.
        (self.root / 'mesh_24_if').write_text('')
        (self.root / 'mesh_5_if').write_text('wlan0\n')
        (self.wpa / 'wpa_supplicant-wlan0.conf').write_text('network={\n    frequency=5180\n}\n')
        plan_mod.save('5', 5200, self.plan)
        result = self.enforce()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.freq('wpa_supplicant-wlan0.conf'), '5200')

    def test_plan_is_read_only_under_the_channel_lock(self):
        # A plan changed while the lock is held must not be enforced.
        with open(self.lock, 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.plan.write_text('{')
            result = self.enforce()
        self.assertNotIn('unusable', result.stderr)

    def test_malformed_plan_leaves_radios_alone(self):
        self.plan.write_text('{"2.4": 2462}')
        (self.wpa / 'wpa_supplicant-wlan0.conf').write_text('network={\n    frequency=2437\n}\n')
        result = self.enforce()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('static channel plan unusable', result.stderr)
        self.assertEqual(self.freq('wpa_supplicant-wlan0.conf'), '2437')
        self.assertEqual(self.restarted(), [])

    def test_waits_for_a_channel_change_in_progress(self):
        plan_mod.save('2.4', 2462, self.plan)
        with open(self.lock, 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.enforce()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.freq('wpa_supplicant-wlan0.conf'), '2412')
        self.assertEqual(self.restarted(), [])


if __name__ == '__main__':
    unittest.main()
