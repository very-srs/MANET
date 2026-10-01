#!/usr/bin/env python3
"""manet-halow-power.py: auto power on HaLow roles only, read back, never reload."""

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('manet_halow_power', TOOLS / 'manet-halow-power.py')
halow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(halow)


class Radio:
    def __init__(self, reports=('21.00',)):
        self.calls = []
        self.reports = list(reports)
        self.fail_set = False

    def __call__(self, args):
        self.calls.append(' '.join(args))
        if args[3:5] == ['set', 'txpower']:
            if self.fail_set:
                raise subprocess.CalledProcessError(1, args)
            return ''
        if args[-1] == 'info':
            value = self.reports.pop(0) if len(self.reports) > 1 else self.reports[0]
            return f'Interface {args[2]}\n\ttype mesh point\n' + (f'\ttxpower {value} dBm\n' if value else '')
        raise AssertionError(f'unexpected command {args}')


class HalowPowerTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.roles = self.root / 'lib'
        self.roles.mkdir()
        self.net = self.root / 'net'
        self.env = mock.patch.dict(os.environ, MANET_IFACE_STATE_DIR=str(self.roles),
                                   MANET_SYS_NET=str(self.net))
        self.env.start()
        self.addCleanup(self.env.stop)

    def radio(self, name, driver='morse_usb'):
        drivers = self.root / 'drivers' / driver
        drivers.mkdir(parents=True, exist_ok=True)
        device = self.net / name / 'device'
        device.mkdir(parents=True)
        (device / 'driver').symlink_to(drivers)

    def test_requests_auto_and_logs_reported_power(self):
        (self.roles / 'halow_if').write_text('wlan2\n')
        self.radio('wlan2')
        radio = Radio()
        with mock.patch('builtins.print') as printed:
            halow.main(radio, sleep=lambda s: None)
        self.assertEqual(radio.calls[0], 'iw dev wlan2 set txpower auto')
        self.assertIn('iw dev wlan2 info', radio.calls)
        self.assertIn('driver reports 21.00 dBm', printed.call_args.args[0])
        # Never a fixed ceiling, never a module reload.
        self.assertFalse([c for c in radio.calls if 'fixed' in c or 'modprobe' in c or 'rmmod' in c])

    def test_waits_for_power_to_be_reported(self):
        (self.roles / 'halow_if').write_text('wlan2\n')
        self.radio('wlan2')
        radio = Radio(reports=('', '', '21.00'))
        halow.main(radio, sleep=lambda s: None)
        self.assertEqual(radio.calls.count('iw dev wlan2 info'), 3)

    def test_no_power_report_is_an_error(self):
        (self.roles / 'halow_if').write_text('wlan2\n')
        self.radio('wlan2')
        with self.assertRaisesRegex(RuntimeError, 'no transmit power reported'):
            halow.main(Radio(reports=('',)), sleep=lambda s: None)

    def test_failed_request_is_an_error_not_ignored(self):
        (self.roles / 'halow_if').write_text('wlan2\n')
        self.radio('wlan2')
        radio = Radio()
        radio.fail_set = True
        with self.assertRaises(RuntimeError):
            halow.main(radio, sleep=lambda s: None)

    def test_only_halow_roles_are_touched(self):
        # Mesh and AP radios are not this unit's business, whatever exists.
        (self.roles / 'halow_if').write_text('wlan2\n')
        (self.roles / 'mesh_if').write_text('wlan0 wlan1\n')
        for name, driver in (('wlan0', 'mt7915e'), ('wlan1', 'mt7915e'), ('wlan2', 'morse_usb')):
            self.radio(name, driver)
        radio = Radio()
        halow.main(radio, sleep=lambda s: None)
        self.assertEqual({c.split()[2] for c in radio.calls}, {'wlan2'})

    def test_non_morse_interface_in_halow_role_is_refused(self):
        # A stale role naming a Wi-Fi radio must not change its power.
        (self.roles / 'halow_if').write_text('wlan0\n')
        self.radio('wlan0', 'mt7915e')
        radio = Radio()
        with self.assertRaisesRegex(RuntimeError, 'not a Morse HaLow radio'):
            halow.main(radio, sleep=lambda s: None)
        self.assertEqual(radio.calls, [])

    def test_missing_interface_reported_others_still_applied(self):
        (self.roles / 'halow_if').write_text('wlan2 wlan3\n')
        self.radio('wlan3')
        radio = Radio()
        with self.assertRaisesRegex(RuntimeError, 'wlan2: interface not present'):
            halow.main(radio, sleep=lambda s: None)
        self.assertIn('iw dev wlan3 set txpower auto', radio.calls)

    def test_no_halow_role_is_a_no_op(self):
        radio = Radio()
        halow.main(radio, sleep=lambda s: None)
        (self.roles / 'halow_if').write_text('')
        halow.main(radio, sleep=lambda s: None)
        self.assertEqual(radio.calls, [])

    def test_invalid_role_name_rejected(self):
        (self.roles / 'halow_if').write_text('wlan2;reboot\n')
        with self.assertRaises(ValueError):
            halow.main(Radio(), sleep=lambda s: None)

    def test_unit_has_no_ignored_failures_or_fixed_names(self):
        unit = (TOOLS.parent / 'systemd' / 'manet-halow-power.service').read_text()
        self.assertIn('ExecStart=/usr/local/bin/manet-halow-power.py', unit)
        self.assertNotIn('ExecStart=-', unit)
        self.assertNotIn('wlan', unit)
        self.assertIn('WantedBy=multi-user.target', unit)


if __name__ == '__main__':
    unittest.main()
