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
from mesh_config import validate_config, apply_local_to_conf

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
            env = dict(os.environ, MANET_CONFIG_WORKER="1",
                       MANET_RUN_DIR=scratch, MANET_MESH_CONF=str(conf),
                       MANET_WPA_DIR=str(wpa), MANET_APPLY_LOG=str(root / 'apply.log'),
                       MANET_CONFIG_WRITER=str(TOOLS / 'mesh-config-write.py'),
                       MANET_SUPPLICANT_HELPER=str(helper),
                       PATH=str(commands) + ':' + str(Path(sys.executable).parent) + ':' + os.environ['PATH'])
            from test_config_sandbox import isolate_config
            isolate_config(self, root, env)
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
        self.assertTrue(validate_config({'lan_ap_key': r'a\password'}, local=True)[0])


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

    def test_ap_name_refused_before_config_or_activation_changes(self):
        before = self.conf.read_bytes(), self.hostapd.read_bytes()
        for activate in (self.apply, lambda changes: apply_local_to_conf(changes, self.conf)):
            with self.subTest(activate=activate), patch.object(ap, 'restart_hostapd') as restart:
                with self.assertRaisesRegex(ValueError, 'AP SSID is set at flash'):
                    activate({'lan_ap_ssid': 'New', 'lan_ap_key': 'newpassword'})
                self.assertEqual((self.conf.read_bytes(), self.hostapd.read_bytes()), before)
                restart.assert_not_called()

    def test_success_preserves_suffix_backslashes_and_permissions(self):
        with patch.object(ap, 'restart_hostapd') as restart:
            self.apply({'lan_ap_key': r'a\password'})
        self.assertIn('ssid=Old-abcd\n', self.hostapd.read_text())
        self.assertIn('wpa_passphrase=a\\password\n', self.hostapd.read_text())
        self.assertEqual(self.conf.stat().st_mode & 0o777, 0o600)
        restart.assert_called_once()

    def test_restart_failure_restores_both_files_and_restarts_old_ap(self):
        before = self.conf.read_bytes(), self.hostapd.read_bytes()
        with patch.object(ap, 'restart_hostapd', side_effect=[subprocess.CalledProcessError(1, ['systemctl']), None]) as restart:
            with self.assertRaisesRegex(RuntimeError, 'previous settings restored'):
                self.apply({'lan_ap_key': 'newpassword'})
        self.assertEqual((self.conf.read_bytes(), self.hostapd.read_bytes()), before)
        self.assertEqual(restart.call_count, 2)

    def test_invalid_request_has_no_effect(self):
        before = self.conf.read_bytes(), self.hostapd.read_bytes()
        with patch.object(ap, 'restart_hostapd') as restart:
            with self.assertRaises(ValueError):
                self.apply({'lan_ap_key': 'bad'})
        self.assertEqual((self.conf.read_bytes(), self.hostapd.read_bytes()), before)
        restart.assert_not_called()


class FixedApSetupTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(); self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.conf = self.root / 'mesh.conf'
        self.conf.write_text('eud=auto\nlan_ap_ssid=EUD\n')
        self.hostapd = self.root / 'hostapd.conf'
        self.identity = self.root / 'ap-ssid'
        self.done = self.root / 'radio-setup.done'
        suffix = patch.object(ap, 'ap_suffix', return_value='5cc2')
        suffix.start(); self.addCleanup(suffix.stop)

    def setup(self):
        return ap.setup_ssid(self.conf, self.hostapd, self.identity, self.done)

    def test_flash_name_persists_across_retries_and_completed_setup(self):
        self.assertEqual(self.setup(), 'EUD-5cc2')
        self.assertEqual(self.identity.read_text(), 'EUD-5cc2\n')
        for complete in (False, True):
            if complete:
                self.done.touch()
            self.assertEqual(self.setup(), 'EUD-5cc2')
            self.conf.write_text('eud=auto\nlan_ap_ssid=Changed\n')
            with self.assertRaisesRegex(ValueError, 'AP SSID is set at flash'):
                self.setup()
            self.assertEqual(self.identity.read_text(), 'EUD-5cc2\n')
            self.conf.write_text('eud=auto\nlan_ap_ssid=EUD\n')

    def test_existing_hostapd_name_is_preserved_and_mismatch_refused(self):
        self.done.touch()
        self.hostapd.write_text('ssid=EUD-aa77\nwpa_passphrase=password\n')
        before = self.hostapd.read_bytes()
        self.assertEqual(self.setup(), 'EUD-aa77')
        self.identity.unlink()
        self.conf.write_text('lan_ap_ssid=Renamed\n')
        with self.assertRaisesRegex(ValueError, 'AP SSID is set at flash'):
            self.setup()
        self.assertFalse(self.identity.exists())
        self.assertEqual(self.hostapd.read_bytes(), before)

    def test_completed_node_without_original_identity_cannot_choose_a_name(self):
        self.done.touch()
        with self.assertRaisesRegex(ValueError, 'original AP name is unavailable'):
            self.setup()
        self.assertFalse(self.identity.exists())

    def test_wired_flash_without_ap_name_still_completes_and_freezes_empty_choice(self):
        self.conf.write_text('eud=wired\nlan_ap_ssid=\n')
        self.assertEqual(self.setup(), '')
        self.done.touch()
        self.assertEqual(self.setup(), '')
        self.conf.write_text('eud=wired\nlan_ap_ssid=New\n')
        with self.assertRaisesRegex(ValueError, 'AP SSID is set at flash'):
            self.setup()

    def test_setup_guard_exits_before_any_other_setup_side_effect(self):
        # Run the actual shell entry point through its preflight, redirecting
        # the helper into this disposable node. No real setup commands run.
        helper = self.root / 'manet_eud_ap.py'
        helper.write_text('import sys\nsys.path.insert(0, ' + repr(str(TOOLS)) + ')\n'
                          'from manet_eud_ap import setup_ssid\nprint(setup_ssid(' +
                          ','.join(repr(str(p)) for p in
                                   (self.conf, self.hostapd, self.identity, self.done)) + '))\n')
        (self.root / 'manet-common.sh').write_text('')
        self.done.touch()
        self.hostapd.write_text('ssid=Original-5cc2\n')
        source = (TOOLS / 'radio-setup.sh').read_text().split('# Append setup output')[0]
        result = subprocess.run(['bash', '-c', source + '\necho unsafe-setup'],
                                env=dict(os.environ, MANET_RADIO_SETUP_LOCKED='1',
                                         MANET_TOOLS_DIR=str(self.root)),
                                text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('AP SSID is set at flash', result.stderr)
        self.assertNotIn('unsafe-setup', result.stdout)

    def test_direct_config_writer_refuses_name_without_writing(self):
        before = self.conf.read_bytes()
        result = subprocess.run([sys.executable, str(TOOLS / 'mesh-config-write.py'),
                                 str(self.conf), 'lan_ap_ssid', 'New'],
                                text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('AP SSID is set at flash', result.stderr)
        self.assertEqual(self.conf.read_bytes(), before)

    def test_direct_mesh_apply_refuses_name_before_other_changes(self):
        (self.root / 'mesh_pending_config.json').write_text(json.dumps({
            'version': 'abcdef123456', 'config': {'lan_ap_ssid': 'New', 'mtx': 'y'}}))
        cat = self.root / 'systemd-cat'
        cat.write_text('#!/bin/sh\ncat >/dev/null\n'); cat.chmod(0o755)
        before = self.conf.read_bytes()
        result = subprocess.run(['bash', str(TOOLS / 'mesh-config-apply.sh')],
                                env=dict(os.environ, MANET_CONFIG_WORKER='1',
                                         MANET_RUN_DIR=str(self.root), MANET_MESH_CONF=str(self.conf),
                                         MANET_CONFIG_PENDING=str(self.root / 'mesh_pending_config.json'),
                                         MANET_CONFIG_WRITER=str(TOOLS / 'mesh-config-write.py'),
                                         MANET_APPLY_LOG=str(self.root / 'apply.log'),
                                         PATH=str(self.root) + ':' + os.environ['PATH']),
                                text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('AP SSID is set at flash', (self.root / 'apply.log').read_text())
        self.assertEqual(self.conf.read_bytes(), before)


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
