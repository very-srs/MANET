#!/usr/bin/env python3
"""manet-region.py: a region change reaches every radio config, not just mesh.conf."""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('manet_region', TOOLS / 'manet-region.py')
region = importlib.util.module_from_spec(spec)
spec.loader.exec_module(region)

US_S1G = '''country="US"
ctrl_interface=/var/run/wpa_supplicant_s1g
network={
    ssid="MESH"
    mode=5
    channel=10
    op_class=69
    country="US"
    s1g_prim_chwidth=1
    s1g_prim_1mhz_chan_index=1
}
'''


class RegionTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        env = mock.patch.dict(os.environ, MANET_REGION_ROOT=str(self.root))
        env.start()
        self.addCleanup(env.stop)
        self.put('etc/mesh.conf', 'mesh_ssid=MESH\nregulatory_domain=US\nhalow_regulatory_domain=US\n')
        self.put('etc/modprobe.d/cfg80211.conf', 'options cfg80211 ieee80211_regdom=US\n')
        self.put('etc/modprobe.d/morse.conf', 'options morse enable_mcast_whitelist=0 enable_mcast_rate_control=1\n'
                 'options morse country=US\noptions morse bcf=bcf_mf15457.bin\n')
        self.put('etc/default/crda', 'REGDOMAIN=US\n')
        self.put('etc/hostapd/hostapd.conf', 'interface=wlan1\ncountry_code=US\nchannel=36\n')
        self.put('etc/wpa_supplicant/wpa_supplicant-wlan0.conf', 'ctrl_interface=/var/run/wpa_supplicant\ncountry=US\nnetwork={\n}\n')
        self.put('etc/wpa_supplicant/wpa_supplicant-wlan0-lobby.conf', 'country=US\n')
        self.put('etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf', US_S1G)

    def put(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def read(self, name):
        return (self.root / name).read_text()

    def set_region(self, country):
        mesh = self.read('etc/mesh.conf')
        self.put('etc/mesh.conf', re.sub(r'^regulatory_domain=.*$', f'regulatory_domain={country}', mesh, flags=re.M))
        return region.apply()

    def test_eu_country_reaches_every_radio_file(self):
        country, halow, family_changed, _ = self.set_region('DE')
        self.assertEqual((country, halow, family_changed), ('DE', 'EU', True))
        self.assertIn('halow_regulatory_domain=EU', self.read('etc/mesh.conf'))
        self.assertEqual(self.read('etc/modprobe.d/cfg80211.conf'), 'options cfg80211 ieee80211_regdom=DE\n')
        morse = self.read('etc/modprobe.d/morse.conf')
        self.assertIn('options morse country=EU', morse)
        self.assertIn(region.EU_MORSE_OPTIONS, morse)
        self.assertIn('bcf=bcf_mf15457.bin', morse)          # board options survive
        self.assertNotIn('country=US', morse)
        self.assertEqual(self.read('etc/default/crda'), 'REGDOMAIN=DE\n')
        self.assertIn('country_code=DE', self.read('etc/hostapd/hostapd.conf'))
        self.assertIn('country=DE', self.read('etc/wpa_supplicant/wpa_supplicant-wlan0.conf'))
        self.assertIn('country=DE', self.read('etc/wpa_supplicant/wpa_supplicant-wlan0-lobby.conf'))

    def test_halow_moves_to_the_region_default_channel_across_plans(self):
        # US and EU share no HaLow channels, so the old one cannot be kept.
        self.set_region('FR')
        s1g = self.read('etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf')
        self.assertEqual(s1g.count('country="EU"'), 2)
        for line in ('channel=1', 'op_class=66', 's1g_prim_chwidth=0', 's1g_prim_1mhz_chan_index=0'):
            self.assertRegex(s1g, rf'(?m)^\s*{line}$')
        self.assertIn('ssid="MESH"', s1g)
        # And back again.
        self.set_region('US')
        s1g = self.read('etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf')
        for line in ('channel=10', 'op_class=69', 's1g_prim_chwidth=1', 's1g_prim_1mhz_chan_index=1'):
            self.assertRegex(s1g, rf'(?m)^\s*{line}$')
        self.assertNotIn(region.EU_MORSE_OPTIONS, self.read('etc/modprobe.d/morse.conf'))

    def test_operator_halow_channel_kept_within_the_same_plan(self):
        self.set_region('DE')
        s1g = self.read('etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf').replace('channel=1\n', 'channel=5\n')
        self.put('etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf', s1g)
        _, _, family_changed, _ = self.set_region('NL')
        self.assertFalse(family_changed)
        self.assertRegex(self.read('etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf'), r'(?m)^\s*channel=5$')
        self.assertIn('ieee80211_regdom=NL', self.read('etc/modprobe.d/cfg80211.conf'))

    def test_unchanged_region_writes_nothing(self):
        self.set_region('DE')
        _, _, _, changed = region.apply()
        self.assertEqual(changed, [])

    def test_invalid_region_is_refused_before_any_write(self):
        before = self.read('etc/modprobe.d/cfg80211.conf')
        with self.assertRaises(ValueError):
            self.set_region('D;E')
        self.assertEqual(self.read('etc/modprobe.d/cfg80211.conf'), before)

    def test_eu_country_list_matches_radio_setup(self):
        setup = (TOOLS / 'radio-setup.sh').read_text()
        lists = re.findall(r'^\s*((?:[A-Z]{2}\|)+[A-Z]{2})\)\s*$', setup, re.M)
        self.assertTrue(lists)
        for found in lists:
            self.assertEqual(set(found.split('|')), region.EU_HALOW_COUNTRIES)

    def test_defaults_match_radio_setup_templates(self):
        setup = (TOOLS / 'radio-setup.sh').read_text()
        for channel, op_class, chwidth, index in region.HALOW_DEFAULT_CHANNEL.values():
            self.assertRegex(setup, rf'channel={channel}\n\s*op_class={op_class}\n.*\n'
                                    rf'\s*s1g_prim_chwidth={chwidth}\n\s*s1g_prim_1mhz_chan_index={index}\n')

    def apply_package(self, config):
        run = self.root / 'run'
        run.mkdir(exist_ok=True)
        (run / 'mesh_pending_config.json').write_text(json.dumps({'version': 'v1', 'config': config}))
        stubs = self.root / 'bin'
        stubs.mkdir(exist_ok=True)
        for name in ('systemd-cat', 'systemctl'):
            (stubs / name).write_text('#!/bin/sh\ncat >/dev/null 2>&1; exit 0\n')
            (stubs / name).chmod(0o755)
        env = dict(os.environ, PATH=f'{stubs}:{os.environ["PATH"]}',
                   MANET_RUN_DIR=str(run), MANET_MESH_CONF=str(self.root / 'etc/mesh.conf'),
                   MANET_WPA_DIR=str(self.root / 'etc/wpa_supplicant'),
                   MANET_APPLY_LOG=str(self.root / 'apply.log'),
                   MANET_CONFIG_WRITER=str(TOOLS / 'mesh-config-write.py'))
        result = subprocess.run(['bash', str(TOOLS / 'mesh-config-apply.sh')], env=env,
                                capture_output=True, text=True, timeout=30)
        return result, (self.root / 'apply.log').read_text()

    def test_web_ui_region_change_rewrites_the_radio_files(self):
        # The path a Node config tab change takes: staged package, then apply.
        result, log = self.apply_package({'regulatory_domain': 'DE'})
        self.assertEqual(result.returncode, 0, result.stderr + log)
        self.assertIn('regulatory_domain=DE', self.read('etc/mesh.conf'))
        self.assertEqual(self.read('etc/modprobe.d/cfg80211.conf'), 'options cfg80211 ieee80211_regdom=DE\n')
        self.assertIn('options morse country=EU', self.read('etc/modprobe.d/morse.conf'))
        self.assertIn('country="EU"', self.read('etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf'))
        self.assertIn('takes effect at the next boot', log)
        self.assertNotIn('ERROR', log)

    def test_failed_region_write_is_reported_not_claimed(self):
        (self.root / 'etc/modprobe.d/morse.conf').unlink()
        (self.root / 'etc/modprobe.d').chmod(0o500)
        self.addCleanup((self.root / 'etc/modprobe.d').chmod, 0o755)
        if os.geteuid() == 0:
            self.skipTest('root ignores directory permissions')
        result, log = self.apply_package({'regulatory_domain': 'DE'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ERROR: regulatory domain saved but the radio files were not updated', log)
        self.assertNotIn('takes effect at the next boot', log)
        # Not recorded as applied; the pending package stays for inspection.
        self.assertFalse((self.root / 'run/mesh_applied_config_version').exists())
        self.assertTrue((self.root / 'run/mesh_pending_config.json').exists())
        # A new activation with the same region repairs it, although
        # mesh.conf already says DE.
        (self.root / 'etc/modprobe.d').chmod(0o755)
        result, log = self.apply_package({'regulatory_domain': 'DE'})
        self.assertEqual(result.returncode, 0, log)
        self.assertIn('options morse country=EU', self.read('etc/modprobe.d/morse.conf'))
        s1g = self.read('etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf')
        self.assertRegex(s1g, r'(?m)^\s*op_class=66$')

    def test_retry_after_partial_failure_still_moves_the_halow_plan(self):
        # a failure after an early marker write left channel 10 /
        # op_class 69 (US) under country EU on retry.
        real_write = region.write
        def failing(path, content):
            if path.name == 'cfg80211.conf':
                raise OSError('injected')
            real_write(path, content)
        with mock.patch.object(region, 'write', failing), self.assertRaises(OSError):
            self.set_region('DE')
        country, halow, family_changed, _ = region.apply()
        self.assertTrue(family_changed)
        s1g = self.read('etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf')
        self.assertRegex(s1g, r'(?m)^\s*channel=1$')
        self.assertRegex(s1g, r'(?m)^\s*op_class=66$')
        self.assertIn('halow_regulatory_domain=EU', self.read('etc/mesh.conf'))

    def test_marker_is_written_only_after_every_radio_file(self):
        real_write = region.write
        def failing(path, content):
            if path.name.endswith('-s1g.conf'):
                raise OSError('injected')
            real_write(path, content)
        with mock.patch.object(region, 'write', failing), self.assertRaises(OSError):
            self.set_region('DE')
        self.assertIn('halow_regulatory_domain=US', self.read('etc/mesh.conf'))

    def test_ssid_and_key_text_is_never_rewritten(self):
        s1g = US_S1G.replace('ssid="MESH"', 'ssid="country=US"\n    sae_password="abc country=US xyz"')
        self.put('etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf', s1g)
        self.put('etc/wpa_supplicant/wpa_supplicant-wlan0.conf',
                 'country=US\nnetwork={\n    ssid="country=US"\n    sae_password="x country=US"\n}\n')
        self.set_region('DE')
        s1g = self.read('etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf')
        self.assertIn('    ssid="country=US"\n', s1g)
        self.assertIn('    sae_password="abc country=US xyz"\n', s1g)
        self.assertEqual(s1g.count('country="EU"'), 2)
        wifi = self.read('etc/wpa_supplicant/wpa_supplicant-wlan0.conf')
        self.assertEqual(wifi, 'country=DE\nnetwork={\n    ssid="country=US"\n    sae_password="x country=US"\n}\n')

if __name__ == '__main__':
    unittest.main()
