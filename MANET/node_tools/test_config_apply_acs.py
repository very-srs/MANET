#!/usr/bin/env python3
"""mesh-config-apply.sh, acs key: the running manager must match the selection.

Runs the real apply script and the real node-manager-select.sh. systemctl is
a stub standing in for node-manager.service: a restart runs the drop-in's
ExecStartPre (selector --service-start) and can be made to fail, leaving the
previous variant running, as in Codex 065.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent

SYSTEMCTL = r'''#!/bin/bash
echo "systemctl $*" >> "$TEST_CALLS"
case "$1" in
    is-active) [ -f "$TEST_ROOT/active" ] ;;
    restart)
        if [ "$2" = node-manager.service ]; then
            [ -f "$TEST_ROOT/restart-fails" ] && exit 1
            "$TEST_SELECT" --service-start >/dev/null && touch "$TEST_ROOT/active"
        fi ;;
    *) exit 0 ;;
esac
'''


class AcsApplyTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.run_dir = self.root / 'run'
        self.run_dir.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.tools = self.root / 'tools'
        self.tools.mkdir()
        for name in ('node-manager-acs.sh', 'node-manager-static.sh'):
            (self.tools / name).write_text('#!/bin/sh\n')
            (self.tools / name).chmod(0o755)
        self.wpa = self.root / 'wpa'
        self.wpa.mkdir()
        (self.wpa / 'wpa_supplicant-wlan0.conf').write_text('network={\n    ssid="old"\n}\n')
        self.conf = self.root / 'mesh.conf'
        self.calls = self.root / 'calls'
        (self.bin / 'systemctl').write_text(SYSTEMCTL)
        (self.bin / 'systemctl').chmod(0o755)
        (self.bin / 'systemd-cat').write_text('#!/bin/sh\ncat >/dev/null\n')
        (self.bin / 'systemd-cat').chmod(0o755)
        # mesh-config-apply.sh runs the supplicant helper with python3.
        (self.bin / 'supplicant').write_text('import os, sys\n'
                                             'open(os.environ["TEST_CALLS"], "a").write("supplicant " + " ".join(sys.argv[1:]) + "\\n")\n')
        (self.bin / 'supplicant').chmod(0o755)
        self.env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}',
                        MANET_RUN_DIR=str(self.run_dir), MANET_MESH_CONF=str(self.conf),
                        MANET_WPA_DIR=str(self.wpa), MANET_BIN_DIR=str(self.tools),
                        MANET_APPLY_LOG=str(self.root / 'apply.log'),
                        MANET_CONFIG_WRITER=str(TOOLS / 'mesh-config-write.py'),
                        MANET_SUPPLICANT_HELPER=str(self.bin / 'supplicant'),
                        NODE_MANAGER_SELECT=str(TOOLS / 'node-manager-select.sh'),
                        TEST_SELECT=str(TOOLS / 'node-manager-select.sh'),
                        TEST_CALLS=str(self.calls), TEST_ROOT=str(self.root))

    def manager_running(self, acs):
        """Boot state: mesh.conf, link and the running variant all agree."""
        self.conf.write_text(f'mesh_ssid=old\nacs={acs}\n')
        subprocess.run(['bash', str(TOOLS / 'node-manager-select.sh'), '--service-start'],
                       env=self.env, check=True, capture_output=True, timeout=10)
        (self.root / 'active').touch()

    def running(self):
        return (self.run_dir / 'node-manager.running').read_text().strip()

    def apply(self, version, **config):
        (self.run_dir / 'mesh_pending_config.json').write_text(
            json.dumps({'version': version, 'config': config}))
        self.calls.unlink(missing_ok=True)
        (self.root / 'apply.log').unlink(missing_ok=True)
        result = subprocess.run(['bash', str(TOOLS / 'mesh-config-apply.sh')], env=self.env,
                                capture_output=True, text=True, timeout=30,
                                stdin=subprocess.DEVNULL)
        calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        return result, calls, (self.root / 'apply.log').read_text()

    def applied(self):
        path = self.run_dir / 'mesh_applied_config_version'
        return path.read_text().strip() if path.exists() else None

    def test_change_restarts_and_verifies_before_recording(self):
        self.manager_running('n')
        result, calls, log = self.apply('v1', acs='y')
        self.assertEqual(result.returncode, 0, log)
        self.assertIn('systemctl restart node-manager.service', calls)
        self.assertEqual(self.running(), 'node-manager-acs.sh')
        self.assertEqual(self.applied(), 'v1')
        self.assertLess(log.index('node-manager running node-manager-acs.sh'),
                        log.index('Config apply complete'))

    def test_failed_restart_then_same_value_retry_recovers(self):
        # Codex 065: restart fails, the static variant keeps running.
        self.manager_running('n')
        (self.root / 'restart-fails').touch()
        result, calls, log = self.apply('v1', acs='y')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('node-manager did not restart', log)
        self.assertIsNone(self.applied())
        self.assertTrue((self.run_dir / 'mesh_pending_config.json').exists())
        self.assertEqual(self.running(), 'node-manager-static.sh')
        # A NEW activation with the same value must not call this healthy.
        result, calls, log = self.apply('v2', acs='y')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('systemctl restart node-manager.service', calls)
        self.assertIsNone(self.applied())
        (self.root / 'restart-fails').unlink()
        result, calls, log = self.apply('v3', acs='y')
        self.assertEqual(result.returncode, 0, log)
        self.assertEqual((self.running(), self.applied()), ('node-manager-acs.sh', 'v3'))

    def test_mixed_package_finishes_every_step_before_the_restart(self):
        # Codex 064: the restart used to cut short the rest of the package.
        self.manager_running('n')
        result, calls, log = self.apply('v1', acs='y', mesh_ssid='new')
        self.assertEqual(result.returncode, 0, log)
        self.assertIn('ssid="new"', (self.wpa / 'wpa_supplicant-wlan0.conf').read_text())
        self.assertLess(calls.index('supplicant restart'),
                        calls.index('systemctl restart node-manager.service'))
        self.assertEqual(self.applied(), 'v1')

    def test_selector_failure_fails_the_apply(self):
        self.manager_running('n')
        (self.tools / 'node-manager-acs.sh').unlink()
        result, calls, log = self.apply('v1', acs='y')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cannot select the node manager', log)
        self.assertFalse([c for c in calls if 'restart' in c])
        self.assertIsNone(self.applied())

    def test_same_value_on_a_healthy_node_is_a_no_op(self):
        self.manager_running('y')
        result, calls, log = self.apply('v1', acs='y')
        self.assertEqual(result.returncode, 0, log)
        self.assertFalse([c for c in calls if 'restart' in c])
        self.assertEqual(self.applied(), 'v1')

    def test_same_value_repairs_a_stopped_manager(self):
        self.manager_running('y')
        (self.root / 'active').unlink()
        result, calls, log = self.apply('v1', acs='y')
        self.assertEqual(result.returncode, 0, log)
        self.assertIn('systemctl restart node-manager.service', calls)

    def test_sync_runs_the_apply_in_its_own_unit(self):
        source = (TOOLS / 'mesh-config-sync.py').read_text()
        self.assertIn('"systemd-run", "--unit=mesh-config-apply", "--wait"', source)
        self.assertIn('r = run(APPLY_COMMAND, timeout=180)', source)
        self.assertIn('"--property=TimeoutStartSec=180"', source)


if __name__ == '__main__':
    unittest.main()
