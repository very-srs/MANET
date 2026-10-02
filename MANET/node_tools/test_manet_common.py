#!/usr/bin/env python3
"""manet-common.sh and the jq reads that replaced inline Python in shell."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

TOOLS = Path(__file__).resolve().parent


class RadioEnabledTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.state = Path(scratch.name) / 'mesh_radio_state.json'

    def enabled(self, iface):
        script = f'. "{TOOLS}/manet-common.sh"; radio_iface_enabled "$1"'
        return subprocess.run(['bash', '-c', script, 'test', iface], timeout=10,
                              env=dict(os.environ, MANET_RADIO_STATE_FILE=str(self.state))).returncode == 0

    def test_only_an_explicit_down_disables(self):
        self.state.write_text(json.dumps({'desired': {'wlan0': 'down', 'wlan1': 'up', 'wlan2': False}}))
        self.assertFalse(self.enabled('wlan0'))
        self.assertTrue(self.enabled('wlan1'))
        self.assertTrue(self.enabled('wlan2'))
        self.assertTrue(self.enabled('wlan3'))

    def test_missing_or_broken_state_means_enabled(self):
        self.assertTrue(self.enabled('wlan0'))
        for text in ('not json', '[]', '{"desired": []}', '{"desired": "x"}', ''):
            with self.subTest(text=text):
                self.state.write_text(text)
                self.assertTrue(self.enabled('wlan0'))

    def test_only_the_exact_string_down_disables(self):
        # Codex 055: shell capture would strip the newline from "down\n".
        for value in ('down\n', ' down', 'DOWN', 'down ', ['down']):
            with self.subTest(value=value):
                self.state.write_text(json.dumps({'desired': {'wlan0': value}}))
                self.assertTrue(self.enabled('wlan0'))

    def test_down_followed_by_junk_or_more_documents_is_malformed(self):
        # Codex 056/059: only one whole valid document can disable a radio.
        down = json.dumps({'desired': {'wlan0': 'down'}})
        for text in (down + ' trailing junk', '{}\n' + down, down + '\n' + down, down + '\n{}'):
            with self.subTest(text=text):
                self.state.write_text(text)
                self.assertTrue(self.enabled('wlan0'))
        self.state.write_text(down + '\n')
        self.assertFalse(self.enabled('wlan0'))

    def test_empty_name_is_not_enabled(self):
        self.assertFalse(self.enabled(''))

    def test_no_shell_script_embeds_this_check_in_python(self):
        for script in TOOLS.glob('*.sh'):
            self.assertNotIn("get('desired', {})", script.read_text(), script.name)


class ManagerJsonReadTests(unittest.TestCase):
    """The GPS and battery reads, extracted unchanged from both managers."""

    def snippet(self, manager, start, end):
        text = (TOOLS / manager).read_text()
        a = text.index(start)
        return text[a:text.index(end, a)]

    def gps(self, manager, data):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        self.addCleanup(os.unlink, f.name)
        block = self.snippet(manager, 'GPS_LAT=""; GPS_LON=""; GPS_ALT=""', '[ -n "$GPS_LAT" ]')
        script = (f'GPS_STATUS_FILE={f.name}\nGPS_FIX_MAX_AGE=60\n' + block +
                  'echo "$GPS_LAT|$GPS_LON|$GPS_ALT"')
        return subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=10).stdout.strip()

    def test_fresh_fix_is_published_and_stale_or_partial_is_not(self):
        now = time.time()
        fix = {'has_fix': True, 'timestamp': now, 'latitude': 39.7392, 'longitude': -104.9903, 'altitude': 1609.3}
        for manager in ('node-manager-static.sh', 'node-manager-acs.sh'):
            with self.subTest(manager=manager):
                self.assertEqual(self.gps(manager, fix), '39.7392|-104.9903|1609.3')
                self.assertEqual(self.gps(manager, dict(fix, timestamp=now - 120)), '||')
                self.assertEqual(self.gps(manager, dict(fix, has_fix=False)), '||')
                self.assertEqual(self.gps(manager, {k: v for k, v in fix.items() if k != 'altitude'}), '||')
                self.assertEqual(self.gps(manager, 'not json'), '||')
                # Values are data, never shell: nothing is eval'd any more.
                self.assertEqual(self.gps(manager, dict(fix, latitude='$(touch /tmp/x)')), '||')

    def test_battery_percentage(self):
        for manager in ('node-manager-static.sh', 'node-manager-acs.sh'):
            line = next(l for l in (TOOLS / manager).read_text().splitlines() if 'BATT_PCT=$(' in l)
            self.assertIn("jq -r '.percentage // empty' /run/battery_status.json", line)


if __name__ == '__main__':
    unittest.main()
