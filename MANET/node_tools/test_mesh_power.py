#!/usr/bin/env python3
"""manet-mesh-power.sh: 30 dBm on every mesh radio, hardware sets the limit."""

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent
SCRIPT = TOOLS / 'manet-mesh-power.sh'

# Stub iw: radios described in $TEST_RADIOS (JSON), calls appended to $TEST_CALLS.
# "reports" is a list of readings consumed one per info call (last one sticks).
# "becomes_ap_after" turns the radio into an AP after that many info calls.
IW = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state_path = Path(os.environ['TEST_RADIOS'])
radios = json.loads(state_path.read_text())
with open(os.environ['TEST_CALLS'], 'a') as log:
    log.write('iw ' + ' '.join(sys.argv[1:]) + '\n')
iface = sys.argv[2]
if sys.argv[1] == 'phy':
    iface = next((name for name, radio in radios.items()
                  if radio['phy'] == sys.argv[2]), None)
radio = radios.get(iface)
if radio is None:
    sys.exit(237)
if radio.get('hang') in (sys.argv[3], 'all'):
    import time
    time.sleep(60)
if sys.argv[3:5] == ['set', 'txpower']:
    if sys.argv[1] != 'phy':
        sys.exit(238)
    sys.exit(1 if radio.get('refuse') else 0)
radio['infos'] = radio.get('infos', 0) + 1
if radio.get('becomes_ap_after') and radio['infos'] > radio['becomes_ap_after']:
    radio['type'] = 'AP'
reports = radio.get('reports', ['30.00'])
value = reports.pop(0) if len(reports) > 1 else reports[0]
radio['reports'] = reports
state_path.write_text(json.dumps(radios))
print(f'Interface {iface}\n\ttype {radio.get("type", "mesh point")}')
if value:
    print(f'\ttxpower {value} dBm')
'''


class MeshPowerTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.roles = self.root / 'lib'
        self.roles.mkdir()
        self.net = self.root / 'net'
        self.net.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.bin / 'iw').write_text(IW)
        (self.bin / 'iw').chmod(0o755)
        (self.bin / 'sleep').write_text('#!/bin/sh\necho "sleep $*" >> "$TEST_CALLS"\n')
        (self.bin / 'sleep').chmod(0o755)
        self.radios_file = self.root / 'radios.json'
        self.calls_file = self.root / 'calls'
        self.lock = self.root / 'channel-election.lock'
        self.radios = {}
        self.env = dict(os.environ,
                        PATH=f'{self.bin}:{os.environ["PATH"]}',
                        MANET_IFACE_STATE_DIR=str(self.roles), MANET_SYS_NET=str(self.net),
                        MANET_ACS_LOCK_FILE=str(self.lock), MANET_LOCK_WAIT='1',
                        MANET_IW_TIMEOUT='1',
                        TEST_RADIOS=str(self.radios_file), TEST_CALLS=str(self.calls_file))

    def radio(self, name, phy, up=True, **fields):
        (self.net / name / 'phy80211').mkdir(parents=True)
        (self.net / name / 'phy80211/name').write_text(phy + '\n')
        (self.net / name / 'flags').write_text('0x1003\n' if up else '0x1002\n')
        self.radios[name] = dict(fields, phy=phy)

    def roles_are(self, mesh='', halow=''):
        (self.roles / 'mesh_if').write_text(mesh + '\n')
        (self.roles / 'halow_if').write_text(halow + '\n')

    def run_script(self):
        self.radios_file.write_text(json.dumps(self.radios))
        result = subprocess.run(['bash', str(SCRIPT)], env=self.env, capture_output=True,
                                text=True, timeout=30)
        calls = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        return result, calls

    def test_every_mesh_radio_is_asked_for_30_dbm(self):
        self.radio('wlan0', 'phy0')
        self.radio('wlan1', 'phy1')
        self.radio('wlan2', 'phy2', reports=['24.00'])
        self.roles_are(mesh='wlan0 wlan1', halow='wlan2')
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        for phy in ('phy0', 'phy1', 'phy2'):
            self.assertIn(f'iw phy {phy} set txpower fixed 3000', calls)
        self.assertIn('wlan0: requested 30 dBm; driver reports 30.00 dBm', result.stdout)
        # A lower hardware report is logged, not an error.
        self.assertIn('wlan2: requested 30 dBm; driver reports 24.00 dBm', result.stdout)
        self.assertFalse([c for c in calls if 'auto' in c or 'modprobe' in c or 'rmmod' in c])

    def test_access_point_is_left_alone(self):
        self.radio('wlan0', 'phy0')
        self.radio('wlan1', 'phy1', type='AP')
        self.roles_are(mesh='wlan0 wlan1')
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('wlan1: serving as the EUD AP', result.stdout)
        self.assertFalse([c for c in calls if c.startswith('iw phy phy1 set')])

    def test_holds_the_channel_lock_used_by_ap_transitions(self):
        # the role check and the request happen under the lock, so
        # a transition holding it cannot interleave. While it is held, wait
        # and give up rather than act on a stale check.
        self.radio('wlan1', 'phy1')
        self.roles_are(mesh='wlan1')
        with open(self.lock, 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result, calls = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn('radio transition busy', result.stderr)
        self.assertEqual(calls, [])

    def test_roles_are_read_under_the_lock(self):
        source = SCRIPT.read_text()
        self.assertLess(source.index('flock -w'), source.index('for role in mesh_if halow_if'))

    def test_shared_phy_with_another_active_interface_is_refused(self):
        self.radio('wlan0', 'phy0')
        self.radio('ap0', 'phy0', type='AP')
        self.roles_are(mesh='wlan0')
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn('phy0 also serves ap0', result.stderr)
        self.assertFalse([c for c in calls if 'set txpower' in c])

    def test_shared_phy_with_a_down_interface_is_allowed(self):
        self.radio('wlan0', 'phy0')
        self.radio('spare0', 'phy0', up=False)
        self.roles_are(mesh='wlan0')
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('iw phy phy0 set txpower fixed 3000', calls)

    def test_hung_iw_fails_bounded_and_releases_the_lock(self):
        # unbounded iw calls held the channel lock indefinitely.
        for hang, writes in (('info', 0), ('set', 1)):
            with self.subTest(hang=hang):
                self.calls_file.unlink(missing_ok=True)
                self.radios = {}
                for path in self.net.iterdir():
                    __import__('shutil').rmtree(path)
                self.radio('wlan2', 'phy2', hang=hang)
                self.roles_are(halow='wlan2')
                result, calls = self.run_script()
                self.assertEqual(result.returncode, 1)
                self.assertEqual(len([c for c in calls if 'set txpower' in c]), writes)
                with open(self.lock, 'a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_unidentified_phy_is_refused_without_a_write(self):
        self.radio('wlan2', 'phy2')
        (self.net / 'wlan2/phy80211/name').unlink()
        self.roles_are(halow='wlan2')
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn('cannot identify its PHY', result.stderr)
        self.assertFalse([c for c in calls if 'set txpower' in c])

    def test_waits_for_power_to_be_reported(self):
        self.radio('wlan2', 'phy2', reports=['30.00', '', '', '30.00'])
        self.roles_are(halow='wlan2')
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count('iw dev wlan2 info'), 4)
        self.assertEqual(calls.count('sleep 1'), 2)

    def test_no_power_report_is_an_error(self):
        self.radio('wlan2', 'phy2', reports=[''])
        self.roles_are(halow='wlan2')
        result, _ = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn('no transmit power reported', result.stderr)

    def test_failed_request_is_an_error_not_ignored(self):
        self.radio('wlan2', 'phy2', refuse=True)
        self.roles_are(halow='wlan2')
        result, _ = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn('wlan2: power request refused', result.stderr)

    def test_interface_named_twice_is_set_once(self):
        self.radio('wlan0', 'phy0')
        self.roles_are(mesh='wlan0', halow='wlan0')
        _, calls = self.run_script()
        self.assertEqual(calls.count('iw phy phy0 set txpower fixed 3000'), 1)

    def test_missing_interface_reported_others_still_applied(self):
        self.radio('wlan3', 'phy3')
        self.roles_are(halow='wlan2 wlan3')
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn('wlan2: interface not present', result.stderr)
        self.assertIn('iw phy phy3 set txpower fixed 3000', calls)

    def test_no_roles_is_a_no_op(self):
        result, calls = self.run_script()
        self.assertEqual((result.returncode, calls), (0, []))
        self.roles_are()
        result, calls = self.run_script()
        self.assertEqual((result.returncode, calls), (0, []))

    def test_invalid_role_name_rejected(self):
        (self.roles / 'mesh_if').write_text('wlan0;reboot\n')
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, [])

    def test_unit_has_no_ignored_failures_or_fixed_names(self):
        unit = (TOOLS.parent / 'systemd' / 'manet-mesh-power.service').read_text()
        self.assertIn('ExecStart=/usr/local/bin/manet-mesh-power.sh', unit)
        self.assertNotIn('ExecStart=-', unit)
        self.assertNotIn('wlan', unit)
        self.assertIn('WantedBy=multi-user.target', unit)


if __name__ == '__main__':
    unittest.main()
