"""Regression checks for literal writes, validation and failed AP activation."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import manet_eud_ap as ap
import manet_supplicant as supplicant
from manet_config_io import supplicant_string
from mesh_config import validate_config

TOOLS = Path(__file__).resolve().parent


class ConfigSafetyTests(unittest.TestCase):
    def test_supplicant_encoding_preserves_exact_bytes(self):
        for value in ('abcdefgh', r'a\password', 'é' * 8, 'abc"defgh', 'abcd1234'):
            encoded = supplicant_string(value)
            # wpa_config_parse_string: ordinary quotes delimit literal bytes;
            # otherwise the field is decoded as hexadecimal, not escaped text.
            decoded = encoded[1:-1].encode() if encoded.startswith('"') else bytes.fromhex(encoded)
            self.assertEqual(decoded, value.encode())

    def test_full_apply_updates_standard_lobby_and_halow_configs(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            wpa = root / 'wpa'; wpa.mkdir()
            conf = root / 'mesh.conf'
            conf.write_text('mesh_ssid=old\nmesh_key=previous-password\n')
            fixtures = ['wpa_supplicant-wlan0.conf', 'wpa_supplicant-wlan0-lobby.conf',
                        'wpa_supplicant-wlan2-s1g.conf']
            for name in fixtures:
                (wpa / name).write_text('network={\n    ssid="old"\n    sae_password="previous-password"\n}\n')
            key = r'new\password'
            uplink = wpa / 'wpa_supplicant-wlan4-uplink.conf'
            uplink.write_text('network={\n    ssid="hotspot"\n    psk="uplink-password"\n}\n')
            uplink_before = uplink.read_bytes()
            (root / 'mesh_pending_config.json').write_text(json.dumps({
                'version': 'abcdef123456', 'config': {'mesh_ssid': 'new', 'mesh_key': key}}))
            helper = root / 'restart.py'; helper.write_text('pass\n')
            commands = root / 'bin'; commands.mkdir()
            cat = commands / 'systemd-cat'; cat.write_text('#!/bin/sh\ncat >/dev/null\n'); cat.chmod(0o755)
            env = dict(os.environ, MANET_RUN_DIR=scratch, MANET_MESH_CONF=str(conf),
                       MANET_WPA_DIR=str(wpa), MANET_APPLY_LOG=str(root / 'apply.log'),
                       MANET_CONFIG_WRITER=str(TOOLS / 'mesh-config-write.py'),
                       MANET_SUPPLICANT_HELPER=str(helper),
                       PATH=str(commands) + ':' + str(Path(sys.executable).parent) + ':' + os.environ['PATH'])
            result = subprocess.run(['bash', str(TOOLS / 'mesh-config-apply.sh')], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(uplink.read_bytes(), uplink_before)
            for name in fixtures:
                text = (wpa / name).read_text()
                self.assertIn(f'sae_password="{key}"', text)
                self.assertIn('ssid="new"', text)
            self.assertNotIn(key, (root / 'apply.log').read_text())

    def test_invalid_settings_rejected_including_bytes_capacity_and_immutable_width(self):
        bad = [{'unknown': 'value'}, {'mesh_ssid': 'é' * 17}, {'lan_ap_ssid': 'a' * 28},
               {'lan_ap_key': 'short'}, {'lan_ap_ssid': 'ok\nadmin_password=bad'},
               {'ipv4_network': '10.30.2.0/30'}, {'ipv4_network': '10.30.2.1/24'},
               {'max_euds_per_node': '2'}, {'lan_ap_key': 'é' * 10}, {'mtx': True}]
        for config in bad:
            with self.subTest(config=config):
                self.assertFalse(validate_config(config, {'max_euds_per_node': '1'}, local=True)[0])
        self.assertFalse(validate_config({'ipv4_network': '10.30.2.0/28'}, {'max_euds_per_node': '12'})[0])
        self.assertTrue(validate_config({'lan_ap_ssid': 'é' * 13, 'lan_ap_key': r'a\password'}, local=True)[0])


class ApActivationTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(); self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.conf = self.root / 'mesh.conf'; self.conf.write_text('lan_ap_ssid=Old\nlan_ap_key=oldpassword\n')
        self.conf.chmod(0o600)
        self.hostapd = self.root / 'hostapd.conf'
        self.hostapd.write_text('ssid=Old-abcd\nwpa_passphrase=oldpassword\ninterface=wlan1\n')
        self.role = self.root / 'ap_interface'; self.role.write_text('wlan1\n')
        p = patch.object(ap, 'ap_suffix', return_value='abcd'); p.start(); self.addCleanup(p.stop)

    def apply(self, changes):
        ap.apply_local(changes, self.conf, self.hostapd, self.role)

    def test_success_preserves_suffix_backslashes_and_permissions(self):
        with patch.object(ap, 'restart_hostapd') as restart:
            self.apply({'lan_ap_ssid': r'New\AP', 'lan_ap_key': r'a\password'})
        self.assertIn('ssid=New\\AP-abcd\n', self.hostapd.read_text())
        self.assertIn('wpa_passphrase=a\\password\n', self.hostapd.read_text())
        self.assertEqual(self.conf.stat().st_mode & 0o777, 0o600)
        restart.assert_called_once()

    def test_restart_failure_restores_both_files_and_restarts_old_ap(self):
        before = self.conf.read_bytes(), self.hostapd.read_bytes()
        with patch.object(ap, 'restart_hostapd', side_effect=[subprocess.CalledProcessError(1, ['systemctl']), None]) as restart:
            with self.assertRaisesRegex(RuntimeError, 'previous settings restored'):
                self.apply({'lan_ap_ssid': 'New'})
        self.assertEqual((self.conf.read_bytes(), self.hostapd.read_bytes()), before)
        self.assertEqual(restart.call_count, 2)

    def test_invalid_request_has_no_effect(self):
        before = self.conf.read_bytes(), self.hostapd.read_bytes()
        with patch.object(ap, 'restart_hostapd') as restart:
            with self.assertRaises(ValueError):
                self.apply({'lan_ap_key': 'bad'})
        self.assertEqual((self.conf.read_bytes(), self.hostapd.read_bytes()), before)
        restart.assert_not_called()


class SupplicantRestartTests(unittest.TestCase):
    def test_roles_disabled_radios_and_failed_service(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / 'mesh_if').write_text('wlan0\nwlan1\n')
            (root / 'halow_if').write_text('wlan2\n')
            (root / 'mesh_radio_state.json').write_text('{"desired":{"wlan1":"down"}}')
            with patch.object(supplicant.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'PONG\n')) as run:
                supplicant.restart_configured(root)
            args = [call.args[0] for call in run.call_args_list]
            self.assertIn(['systemctl', 'restart', 'wpa_supplicant-s1g-wlan2.service'], args)
            self.assertFalse(any('wpa_supplicant@wlan2.service' in a or 'wpa_supplicant@wlan1.service' in a for a in args))
            with patch.object(supplicant.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['systemctl'])):
                with self.assertRaises(RuntimeError):
                    supplicant.restart_configured(root)
