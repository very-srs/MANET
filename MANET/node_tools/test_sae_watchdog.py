#!/usr/bin/env python3
"""sae-watchdog.sh waits for an enabled mesh interface instead of exiting.

An exit made Restart=always re-run it every few seconds during first boot,
and each start pulled in batman-enslave, which started supplicants that had
no config yet.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent

# Each sleep is one wait. The Nth sleep runs $TEST_ROOT/onsleep-N if present.
SLEEP = r'''#!/bin/bash
n=$(( $(cat "$TEST_ROOT/sleeps" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$TEST_ROOT/sleeps"
[ -x "$TEST_ROOT/onsleep-$n" ] && "$TEST_ROOT/onsleep-$n"
[ "$n" -lt 10 ] || exit 1
'''
JOURNALCTL = '#!/bin/sh\necho "journalctl $*" >> "$TEST_ROOT/calls"\n'


class SaeWatchdogTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name, body in (('sleep', SLEEP), ('journalctl', JOURNALCTL)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.mesh_if = self.root / 'mesh_if'
        self.state = self.root / 'radio_state.json'
        self.env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}',
                        TEST_ROOT=str(self.root), MANET_MESH_IF_FILE=str(self.mesh_if),
                        MANET_RADIO_STATE_FILE=str(self.state))

    def on_sleep(self, n, command):
        hook = self.root / f'onsleep-{n}'
        hook.write_text(f'#!/bin/sh\n{command}\n')
        hook.chmod(0o755)

    def run_watchdog(self):
        result = subprocess.run(['bash', str(TOOLS / 'sae-watchdog.sh')], env=self.env,
                                capture_output=True, text=True, timeout=30)
        calls = (self.root / 'calls').read_text().splitlines() if (self.root / 'calls').exists() else []
        sleeps = int((self.root / 'sleeps').read_text()) if (self.root / 'sleeps').exists() else 0
        return result, calls, sleeps

    def test_waits_for_roles_then_monitors(self):
        self.on_sleep(2, f'echo wlan0 > {self.mesh_if}')
        result, calls, sleeps = self.run_watchdog()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sleeps, 2)
        self.assertEqual(result.stdout.count('No enabled mesh interfaces yet; waiting'), 1)
        self.assertIn('monitoring: wlan0', result.stdout)
        self.assertEqual(calls, ['journalctl -fu wpa_supplicant@wlan0.service --output=cat'])

    def test_radio_turned_off_keeps_it_waiting_not_exiting(self):
        self.mesh_if.write_text('wlan0\n')
        self.state.write_text(json.dumps({'desired': {'wlan0': 'down'}}))
        self.on_sleep(3, f"echo '{{\"desired\": {{\"wlan0\": \"up\"}}}}' > {self.state}")
        result, calls, sleeps = self.run_watchdog()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sleeps, 3)
        self.assertEqual(len(calls), 1)

    def test_roles_present_start_immediately(self):
        self.mesh_if.write_text('wlan0 wlan1\n')
        result, calls, sleeps = self.run_watchdog()
        self.assertEqual((result.returncode, sleeps), (0, 0))
        self.assertEqual(calls, ['journalctl -fu wpa_supplicant@wlan0.service '
                                 '-fu wpa_supplicant@wlan1.service --output=cat'])


if __name__ == '__main__':
    unittest.main()
