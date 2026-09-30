"""Manual Wi-Fi channel transactions and ACS ownership, without radios."""

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import manet_radio as radio
import manet_supplicant as supplicant


class WifiChannelApplyTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(); self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.conf = self.root / 'mesh.conf'; self.conf.write_text('acs=n\n')
        self.plan = self.root / 'static-channels.json'
        self.lock = self.root / 'channel.lock'
        for name, content in [('mesh_24_if', ''), ('mesh_5_if', 'wlan0\n'), ('mesh_if', 'wlan0\n')]:
            (self.root / name).write_text(content)
        self.paths = [self.root / f'wpa_supplicant-wlan0{suffix}.conf' for suffix in ('', '-lobby')]
        for path in self.paths:
            path.write_text('network={\n    frequency=5180\n    ssid="test"\n}\n')
            path.chmod(0o600)
        env = patch.dict(os.environ, MANET_MESH_CONF=str(self.conf), MANET_STATIC_CHANNELS=str(self.plan),
                         MANET_IFACE_STATE_DIR=str(self.root), MANET_WPA_DIR=str(self.root),
                         MANET_ACS_LOCK_FILE=str(self.lock))
        env.start(); self.addCleanup(env.stop)

    def test_manual_change_persists_both_configs_and_plan_under_lock(self):
        def restart(**kwargs):
            self.assertEqual(kwargs, {'only': {'wlan0'}})
            with self.lock.open('a') as competing:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch.object(supplicant, 'restart_configured', side_effect=restart):
            result = radio.apply_wifi_channel('5', 149)
        self.assertTrue(result['ok'])
        self.assertEqual(result['freq'], 5745)
        self.assertEqual(json.loads(self.plan.read_text()), {'2.4': 2412, '5': 5745})
        for path in self.paths:
            self.assertIn('    frequency=5745\n', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_restart_failure_restores_plan_and_both_configs(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.plan.write_text('{"2.4":2437,"5":5180}\n')
                previous = {path: path.read_bytes() for path in self.paths + ([self.plan] if existing else [])}
                with patch.object(supplicant, 'restart_configured', side_effect=[RuntimeError('failed'), None]) as restart:
                    with self.assertRaisesRegex(RuntimeError, 'previous channel restored'):
                        radio.apply_wifi_channel('5', 149)
                self.assertEqual(restart.call_count, 2)
                for path, contents in previous.items():
                    self.assertEqual(path.read_bytes(), contents)
                self.assertEqual(self.plan.exists(), existing)

    def test_acs_and_lock_contention_reject_without_changing_anything(self):
        before = [path.read_bytes() for path in self.paths]
        with patch.object(supplicant, 'restart_configured') as restart:
            self.conf.write_text('acs=y\n')
            with self.assertRaisesRegex(ValueError, 'managed by ACS'):
                radio.apply_wifi_channel('5', 149)
            self.conf.write_text('acs=n\n')
            with self.lock.open('a') as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                with patch.object(radio, 'CHANNEL_LOCK_TIMEOUT', 0), self.assertRaisesRegex(RuntimeError, 'in progress'):
                    radio.apply_wifi_channel('5', 149)
            restart.assert_not_called()
        self.assertFalse(self.plan.exists())
        self.assertEqual([path.read_bytes() for path in self.paths], before)

    def test_incompatible_band_or_malformed_plan_never_reaches_restart(self):
        with patch.object(supplicant, 'restart_configured') as restart:
            with self.assertRaises(ValueError):
                radio.apply_wifi_channel('5', 6)
            self.plan.write_text('{')
            with self.assertRaises(ValueError):
                radio.apply_wifi_channel('5', 149)
            restart.assert_not_called()
        self.assertEqual(self.plan.read_text(), '{')

    def test_receiver_rejects_manual_channel_before_ack_in_acs_mode(self):
        path = Path(__file__).with_name('mesh-radio-state.py')
        spec = importlib.util.spec_from_file_location('radio_receiver_review', path)
        receiver = importlib.util.module_from_spec(spec); spec.loader.exec_module(receiver)
        self.conf.write_text('acs=y\n')
        ok, why = receiver.validate_pkg({'kind': 'radio_state', 'version': 'review',
                                        'wifi_channel': {'band': '5', 'channel': 149}})
        self.assertFalse(ok)
        self.assertIn('managed by ACS', why)

    def test_node_without_band_saves_the_plan_without_touching_radios(self):
        (self.root / 'mesh_5_if').write_text('')
        with patch.object(supplicant, 'restart_configured') as restart, patch.object(radio, 'set_iface_txpower_verified') as power:
            result = radio.apply_wifi_channel('5', 149, 20)
        self.assertTrue(result['ok'])
        self.assertEqual(result['iface'], '')
        self.assertEqual(json.loads(self.plan.read_text())['5'], 5745)
        restart.assert_not_called(); power.assert_not_called()

    def test_power_failure_restores_channel_and_old_power(self):
        before = [path.read_bytes() for path in self.paths]
        with patch.object(supplicant, 'restart_configured') as restart, \
                patch.object(radio, 'read_iface_txpower_dbm', return_value='20'), \
                patch.object(radio, 'get_iface_txpower_cap', return_value=''), \
                patch.object(radio, 'set_iface_txpower_verified', side_effect=[RuntimeError('power failed'), ('20', '20')]) as power:
            with self.assertRaisesRegex(RuntimeError, 'previous channel restored'):
                radio.apply_wifi_channel('5', 149, 18)
        self.assertEqual([path.read_bytes() for path in self.paths], before)
        self.assertFalse(self.plan.exists())
        self.assertEqual(restart.call_count, 2)
        self.assertEqual(power.call_args.args, ('wlan0', '20'))
