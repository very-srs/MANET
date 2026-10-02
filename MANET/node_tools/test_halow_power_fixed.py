#!/usr/bin/env python3
"""HaLow power is fixed at radio start: every live path refuses it, and a
Wi-Fi reduction that does not take is a failure."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import manet_manage
import manet_radio as radio

TOOLS = Path(__file__).resolve().parent


class Fixture(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.roles = Path(scratch.name)
        (self.roles / 'halow_if').write_text('wlan2\n')
        env = mock.patch.dict(os.environ, MANET_IFACE_STATE_DIR=str(self.roles),
                              MANET_SYS_NET=str(self.roles / 'net'))
        env.start()
        self.addCleanup(env.stop)
        self.run_cmd = mock.patch.object(radio.subprocess, 'run',
                                         side_effect=AssertionError('no command may run'))
        self.run_cmd.start()
        self.addCleanup(self.run_cmd.stop)


class BackendTests(Fixture):
    def test_halow_detection_by_role_and_by_driver(self):
        self.assertTrue(radio.is_halow_iface('wlan2'))
        self.assertFalse(radio.is_halow_iface('wlan0'))
        driver = self.roles / 'drivers/morse_usb'
        driver.mkdir(parents=True)
        (self.roles / 'net/wlan5/device').mkdir(parents=True)
        (self.roles / 'net/wlan5/device/driver').symlink_to(driver)
        self.assertTrue(radio.is_halow_iface('wlan5'))

    def test_apply_txpower_refuses_halow_without_running_anything(self):
        result = radio.apply_txpower('wlan2', 20)
        self.assertFalse(result['ok'])
        self.assertIn('needs a reboot', result['error'])

    def test_channel_change_with_power_is_refused_before_any_write(self):
        with mock.patch('builtins.open', side_effect=AssertionError('no file may be opened')):
            result = radio.apply_halow_channel(10, '2MHz', 20)
        self.assertFalse(result['ok'])
        self.assertIn('needs a reboot', result['error'])

    def test_receiver_refuses_halow_power_before_ack(self):
        spec = importlib.util.spec_from_file_location('radio_receiver_power', TOOLS / 'mesh-radio-state.py')
        receiver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(receiver)
        with mock.patch.object(receiver, 'is_halow_iface', side_effect=lambda i: i == 'wlan2'), \
                mock.patch.object(receiver, 'target_matches', return_value=True):
            ok, why = receiver.validate_pkg({'kind': 'radio_state', 'version': 'v',
                                            'txpower': {'wlan2': 20}})
            self.assertFalse(ok)
            self.assertIn('needs a reboot', why)
            ok, why = receiver.validate_pkg({'kind': 'radio_state', 'version': 'v',
                                            'halow_channel': {'channel': 10, 'bw': '2MHz', 'dbm': 20}})
            self.assertFalse(ok)
            self.assertIn('needs a reboot', why)


class ReadbackTests(unittest.TestCase):
    def verified(self, requested, reading):
        with mock.patch.object(radio.subprocess, 'run'), \
                mock.patch.object(radio, 'read_iface_txpower_dbm', return_value=reading), \
                mock.patch.object(radio.time, 'sleep'):
            return radio.set_iface_txpower_verified('wlan0', requested)

    def test_lower_report_on_a_high_request_is_accepted(self):
        self.assertEqual(self.verified(30, '24'), ('30', '24'))

    def test_reduction_that_does_not_take_fails(self):
        # Codex 072: requested 5, still 24 used to be reported as success.
        with self.assertRaisesRegex(RuntimeError, 'above the requested 5 dBm'):
            self.verified(5, '24')

    def test_exact_reading(self):
        self.assertEqual(self.verified(5, '5'), ('5', '5'))


class ManageApiTests(Fixture):
    def post(self, path, body):
        handler = object.__new__(manet_manage.ManageRoutes)
        handler.path = path
        handler.read_body = lambda: json.dumps(body).encode()
        handler.send_json = mock.Mock()
        with mock.patch.object(manet_manage, 'coordinate_radio_change') as staged:
            handler.manage_do_POST()
        return handler.send_json.call_args.args[0], staged

    def test_txpower_api_refuses_halow_before_staging(self):
        result, staged = self.post('/api/txpower', {'node_ip': 'all', 'iface': 'wlan2', 'dbm': 20})
        self.assertFalse(result['ok'])
        staged.assert_not_called()
        result, staged = self.post('/api/txpower', {'node_ip': 'all', 'iface': 'wlan0', 'dbm': 20})
        staged.assert_called_once()

    def test_halow_channel_api_refuses_power_and_stages_without_it(self):
        result, staged = self.post('/api/halow/channel', {'channel': 10, 'bw': '2MHz', 'dbm': 20})
        self.assertFalse(result['ok'])
        staged.assert_not_called()
        result, staged = self.post('/api/halow/channel', {'channel': 10, 'bw': '2MHz'})
        staged.assert_called_once_with({'halow_channel': {'channel': 10, 'bw': '2MHz'}})

    def test_page_offers_no_halow_power_control(self):
        page = (TOOLS / 'manet_manage.py').read_text()
        self.assertNotIn('txpwr-all-wlan2', page)
        self.assertIn('isHalowIface(iface, info) ? renderHalowPower(info)', page)


if __name__ == '__main__':
    unittest.main()
