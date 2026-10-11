"""Exercise mesh configuration, binary selection and archive staging."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from manet_ap_mesh import Transition

TOOLS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    'mesh_supplicant', TOOLS / 'manet-mesh-supplicant.py')
mesh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mesh)


class MeshSupplicantTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.config = self.root / 'wpa_supplicant-wlan0.conf'
        self.config.write_text('country=US\nnetwork={\n    mode=5\n}\n')
        self.config.chmod(0o600)

    def test_migrate_live_lobby_and_halow_preserves_modes_and_tuning(self):
        lobby = self.root / 'wpa_supplicant-wlan0-lobby.conf'
        halow = self.root / 'wpa_supplicant-wlan2-s1g.conf'
        uplink = self.root / 'wpa_supplicant-wlan4-uplink.conf'
        tuned = self.root / 'wpa_supplicant-wlan1.conf'
        for path in (lobby, halow):
            path.write_bytes(self.config.read_bytes())
        uplink.write_text('network={\n    mode=0\n}\n')
        tuned.write_text('mesh_max_inactivity=17\n' + self.config.read_text())
        before = {path: path.read_bytes() for path in (uplink, tuned)}
        mesh.prepare_all(self.root)
        for path, seconds in ((self.config, 10), (lobby, 10), (halow, 300)):
            self.assertTrue(path.read_text().startswith(
                f'mesh_max_inactivity={seconds}\n'))
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        before = {path: path.read_bytes() for path in self.root.iterdir()}
        mesh.prepare_all(self.root)
        after = {p: p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(before, after)

    def test_launch_selects_manet_and_warns_on_missing_binary(self):
        for present in (True, False):
            with self.subTest(present=present):
                stderr = io.StringIO()
                with patch.object(mesh, 'WPA_DIR', self.root), \
                     patch.object(mesh.os, 'access', return_value=present), \
                     patch.object(mesh.os, 'execv') as execute, \
                     contextlib.redirect_stderr(stderr):
                    mesh.launch('wlan0')
                selected = mesh.MANET_BINARY if present else mesh.DEBIAN_BINARY
                binary = str(selected)
                execute.assert_called_once_with(binary, [binary, '-i', 'wlan0',
                    '-c', str(self.config), '-D', 'nl80211'])
                self.assertEqual('WARNING' in stderr.getvalue(), not present)
                self.assertTrue(self.config.read_text().startswith(
                    'mesh_max_inactivity=10\n'))

    def test_dropin_replaces_exec_and_retains_ap_guard(self):
        directory = TOOLS.parent / 'systemd/wpa_supplicant@.service.d'
        dropin = (directory / '20-manet-binary.conf').read_text()
        self.assertIn('ExecStart=\nExecStart=/usr/local/bin/'
                      'manet-mesh-supplicant.py %I\n', dropin)
        self.assertIn('ExecCondition=/usr/local/bin/manet-ap-guard.sh %i',
                      (directory / '10-manet-ap-guard.conf').read_text())
        self.assertIn('ExecStart=/usr/sbin/wpa_supplicant -c ',
                      (TOOLS / 'usb-wifi-uplink.sh').read_text())

    def test_ap_transition_renders_global_inactivity(self):
        result = Transition().render_config(
            {'mesh_ssid': 'test', 'mesh_key': 'test-password'}, 2412)
        self.assertIn('mesh_max_inactivity=10\n', result.split('network={')[0])
        self.assertNotIn('mesh_max_inactivity', result.split('network={')[1])

    def test_radio_setup_mesh_generators_emit_global_defaults(self):
        source = (TOOLS / 'radio-setup.sh').read_text()
        blocks = re.findall(r'cat <<-? ?EOF > /etc/wpa_supplicant/[^\n]+\n'
                            r'(.*?)\nEOF', source, re.S)
        self.assertEqual(len(blocks), 3)
        for index, body in enumerate(blocks):
            path = self.root / f'generated-{index}.conf'
            # Execute the actual heredoc with shell variables as setup does.
            command = ('cat <<EOF > ' + shlex.quote(str(path))
                       + '\n' + body + '\nEOF\n')
            subprocess.run(['bash', '-c', command], check=True,
                           env=dict(os.environ, CFG80211_REGDOM='US',
                                    HALOW_REGULATORY_DOMAIN='DE', freq='2412',
                                    MESH_NAME='test', KEY='test-password',
                                    mesh_ssid='test', mesh_key='test-password'))
            text = path.read_text()
            seconds = 10 if index == 0 else 300
            self.assertIn(f'mesh_max_inactivity={seconds}\n',
                          text.split('network={')[0])
            self.assertNotIn('mesh_max_inactivity', text.split('network={')[1])

    def test_builders_stage_wifi_binaries_and_exclude_halow_candidate(self):
        packaging = TOOLS.parent / 'packaging'
        if not packaging.exists():
            self.skipTest('Local packaging tooling is not in this checkout')
        repo = self.root / 'repo'
        binaries = repo / 'MANET/binaries_arm64'
        binaries.mkdir(parents=True)
        for name in ('wpa_supplicant_manet', 'wpa_cli_manet',
                     'wpa_supplicant_s1g_candidate'):
            (binaries / name).write_bytes(b'fixture-' + name.encode())
        stage = self.root / 'stage'
        command = '. "$1"; stage_mesh_supplicant "$2" "$3"'
        args = ['bash', '-c', command, 'test',
                str(packaging / 'lib-supplicant.sh'), str(stage), str(repo)]
        subprocess.run(args, check=True)
        for name in ('wpa_supplicant_manet', 'wpa_cli_manet'):
            installed = stage / 'usr/sbin' / name
            self.assertEqual(installed.read_bytes(),
                             (binaries / name).read_bytes())
            self.assertEqual(installed.stat().st_mode & 0o777, 0o755)
        candidate = stage / 'usr/sbin/wpa_supplicant_s1g_candidate'
        self.assertFalse(candidate.exists())
        for name in ('build-tools-tarball.sh', 'build-cm4-tarball.sh',
                     'build-r3a-tarball.sh', 'build-rpi5-tarball.sh'):
            self.assertIn('stage_mesh_supplicant "$STAGE" "$REPO_ROOT"',
                          (packaging / name).read_text())
        (binaries / 'wpa_supplicant_manet').unlink()
        result = subprocess.run(args, check=True, capture_output=True,
                                text=True)
        self.assertIn('WARNING: wpa_supplicant_manet is missing', result.stderr)
