"""Automatic update gating uses the selected uplink without an ICMP precheck."""

import os
import configparser
from pathlib import Path
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent


class AutoUpdateTests(unittest.TestCase):
    def test_opt_in_and_real_uplink_gate_updates(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            conf = root / 'mesh.conf'
            uplink = root / 'upstream'
            calls = root / 'calls'
            for name, text in {
                'ip': '#!/bin/sh\nprintf "%s\\n" "$TEST_ROUTE"\n',
                'update': '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_CALLS"\n',
                'ping': '#!/bin/sh\nexit 99\n',
            }.items():
                path = root / name; path.write_text(text); path.chmod(0o755)
            env = dict(os.environ, PATH=scratch + ':' + os.environ['PATH'],
                       MANET_MESH_CONF=str(conf), MANET_UPSTREAM_FILE=str(uplink),
                       MANET_TIME_RUN_DIR=scratch,
                       MANET_UPDATER=str(root / 'update'), TEST_CALLS=str(calls))
            for synced, enabled, iface, route, expected in [
                (False, 'y', 'end0', 'default via 1.2.3.4 dev end0', False),
                (True, 'n', 'end0', 'default via 1.2.3.4 dev end0', False),
                (True, 'y', 'br0', 'default via 1.2.3.4 dev br0', False),
                (True, 'y', 'end0', '', False),
                (True, 'y', 'end0', 'default via 1.2.3.4 dev end0', True),
            ]:
                with self.subTest(synced=synced, enabled=enabled, iface=iface, route=route):
                    if synced:
                        (root / 'initial_time_synced').touch()
                    calls.unlink(missing_ok=True)
                    conf.write_text(f'auto_update={enabled}\n'); uplink.write_text(iface)
                    env['TEST_ROUTE'] = route
                    result = subprocess.run(['bash', str(TOOLS / 'manet-auto-update.sh')], env=env,
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(calls.exists(), expected)
                    if not synced:
                        self.assertIn('deferred until clock sync', result.stdout)
                        self.assertEqual(len(result.stdout.splitlines()), 1)
                    if expected:
                        self.assertEqual(calls.read_text(), '--routine\n')


class AutoUpdateUnitTests(unittest.TestCase):
    def unit(self, name):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string((TOOLS.parent / 'systemd' / name).read_text())
        return parser

    def test_clock_path_queues_one_check_without_latching_routable_service(self):
        path = self.unit('manet-auto-update.path')
        self.assertEqual(path['Path']['PathExists'], '/run/initial_time_synced')
        self.assertEqual(path['Install']['WantedBy'], 'multi-user.target')
        trigger = self.unit(path['Path']['Unit'])
        self.assertEqual(trigger['Service']['Type'], 'oneshot')
        self.assertTrue(trigger['Service'].getboolean('RemainAfterExit'))
        self.assertEqual(trigger['Service']['ExecStart'],
                         '/usr/bin/systemctl start --no-block manet-auto-update.service')
        self.assertIn('manet-auto-update.service', trigger['Unit']['After'].split())
        service = self.unit('manet-auto-update.service')
        self.assertEqual(service['Service']['Type'], 'oneshot')
        self.assertFalse(service['Service'].getboolean('RemainAfterExit', fallback=False))

    def test_path_is_enabled_in_shipped_systemd_tree(self):
        path = TOOLS.parent / 'systemd' / 'manet-auto-update.path'
        link = path.parent / 'multi-user.target.wants' / path.name
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.readlink(), Path('../manet-auto-update.path'))
        self.assertTrue(link.is_file())

    def test_builders_ship_the_enabled_path(self):
        builders = TOOLS.parent / 'packaging'
        if not builders.is_dir():
            self.skipTest('local packaging tools are not tracked in this repository')
        for board in ('tools', 'cm4', 'rpi5', 'r3a'):
            with self.subTest(board=board):
                source = (builders / f'build-{board}-tarball.sh').read_text()
                self.assertRegex(source,
                                 r'install_tree "\$REPO_ROOT/MANET/systemd"\s+'
                                 r'"\$STAGE/etc/systemd/system"')
                self.assertIn('    manet-auto-update.path \\\n', source)

    def test_radio_setup_enables_clock_trigger(self):
        source = (TOOLS / 'radio-setup.sh').read_text()
        self.assertIn('systemctl enable --now manet-auto-update.path', source)
