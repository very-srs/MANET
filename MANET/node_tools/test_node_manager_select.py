#!/usr/bin/env python3
"""node-manager-select.sh: node-manager.sh follows acs= in mesh.conf."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent
SELECT = TOOLS / 'node-manager-select.sh'


class SelectTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.bin = Path(scratch.name) / 'bin'
        self.bin.mkdir()
        for name in ('node-manager-acs.sh', 'node-manager-static.sh'):
            (self.bin / name).write_text('#!/bin/sh\n')
            (self.bin / name).chmod(0o755)
        self.conf = Path(scratch.name) / 'mesh.conf'
        self.manager = self.bin / 'node-manager.sh'

    def select(self, conf):
        self.conf.write_text(conf)
        return subprocess.run(['bash', str(SELECT)], capture_output=True, text=True, timeout=10,
                              env=dict(os.environ, MANET_BIN_DIR=str(self.bin),
                                       MANET_MESH_CONF=str(self.conf)))

    def test_acs_on_links_the_acs_variant(self):
        for value in ('y', 'yes', 'Y', '1', 'true', 'TRUE '):
            with self.subTest(value=value):
                result = self.select(f'mesh_ssid=x\nacs={value}\n')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.manager.readlink(), Path('node-manager-acs.sh'))

    def test_anything_else_links_the_static_variant(self):
        for conf in ('acs=n\n', 'acs=no\n', 'acs=\n', 'mesh_ssid=x\n', ''):
            with self.subTest(conf=conf):
                self.assertEqual(self.select(conf).returncode, 0)
                self.assertEqual(self.manager.readlink(), Path('node-manager-static.sh'))

    def test_replaces_an_old_copied_file_with_a_link(self):
        self.manager.write_text('old copy\n')
        self.select('acs=y\n')
        self.assertTrue(self.manager.is_symlink())
        self.assertEqual(self.manager.readlink(), Path('node-manager-acs.sh'))

    def test_switching_back_and_forth(self):
        self.select('acs=y\n')
        result = self.select('acs=n\n')
        self.assertIn('node-manager.sh -> node-manager-static.sh', result.stdout)
        self.assertEqual(self.manager.readlink(), Path('node-manager-static.sh'))
        # Already right: quiet no-op.
        self.assertEqual(self.select('acs=n\n').stdout, '')

    def test_missing_variant_fails_and_keeps_the_current_link(self):
        self.select('acs=n\n')
        (self.bin / 'node-manager-acs.sh').unlink()
        result = self.select('acs=y\n')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.manager.readlink(), Path('node-manager-static.sh'))
        self.assertEqual([p.name for p in self.bin.iterdir() if p.name.startswith('.')], [])

    def test_concurrent_selections_all_succeed(self):
        # a shared temporary name made overlapping calls fail.
        self.conf.write_text('acs=y\n')
        env = dict(os.environ, MANET_BIN_DIR=str(self.bin), MANET_MESH_CONF=str(self.conf))
        for attempt in range(3):
            (self.bin / 'node-manager.sh').unlink(missing_ok=True)
            calls = [subprocess.Popen(['bash', str(SELECT)], env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE) for _ in range(15)]
            codes = [call.wait(timeout=20) for call in calls]
            for call in calls:
                call.stdout.close(); call.stderr.close()
            self.assertEqual(codes, [0] * 15)
            self.assertEqual(self.manager.readlink(), Path('node-manager-acs.sh'))
            self.assertEqual([p.name for p in self.bin.iterdir() if p.name.startswith('.')], [])

    def test_provisioning_templates_use_the_selector_after_mesh_conf(self):
        for name in ('firstrun.sh.template', 'rock3a-provision.sh.template'):
            text = (TOOLS.parent / 'provisioning' / name).read_text()
            self.assertNotIn('cp /usr/local/bin/node-manager-', text)
            self.assertLess(text.index('echo "acs=__AUTO_CHANNEL__" >> /etc/mesh.conf'),
                            text.index('/usr/local/bin/node-manager-select.sh'))

    def test_service_start_records_the_running_variant(self):
        run_dir = self.bin.parent / 'run'
        run_dir.mkdir()
        env = dict(os.environ, MANET_BIN_DIR=str(self.bin), MANET_MESH_CONF=str(self.conf),
                   MANET_RUN_DIR=str(run_dir))
        self.conf.write_text('acs=y\n')
        subprocess.run(['bash', str(SELECT)], env=env, check=True, timeout=10)
        self.assertFalse((run_dir / 'node-manager.running').exists())
        for _ in range(2):  # changed link, then already-right link
            subprocess.run(['bash', str(SELECT), '--service-start'], env=env, check=True, timeout=10)
            self.assertEqual((run_dir / 'node-manager.running').read_text(), 'node-manager-acs.sh\n')
        self.assertEqual(subprocess.run(['bash', str(SELECT), '--bogus'], env=env, timeout=10).returncode, 2)

    def test_service_runs_the_selector_before_every_start(self):
        dropin = (TOOLS.parent / 'systemd/node-manager.service.d/select.conf').read_text()
        self.assertIn('ExecStartPre=/usr/local/bin/node-manager-select.sh --service-start', dropin)
        self.assertFalse((TOOLS / 'node-manager.sh').exists())


if __name__ == '__main__':
    unittest.main()
