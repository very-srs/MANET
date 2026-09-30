"""Automatic update gating uses the selected uplink without an ICMP precheck."""

import os
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
                       MANET_UPDATER=str(root / 'update'), TEST_CALLS=str(calls))
            for enabled, iface, route, expected in [
                ('n', 'end0', 'default via 1.2.3.4 dev end0', False),
                ('y', 'br0', 'default via 1.2.3.4 dev br0', False),
                ('y', 'end0', '', False),
                ('y', 'end0', 'default via 1.2.3.4 dev end0', True),
            ]:
                with self.subTest(enabled=enabled, iface=iface, route=route):
                    calls.unlink(missing_ok=True)
                    conf.write_text(f'auto_update={enabled}\n'); uplink.write_text(iface)
                    env['TEST_ROUTE'] = route
                    result = subprocess.run(['bash', str(TOOLS / 'manet-auto-update.sh')], env=env,
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(calls.exists(), expected)
                    if expected:
                        self.assertEqual(calls.read_text(), '--routine\n')
