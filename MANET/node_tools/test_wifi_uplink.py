"""Exercise built-in client ownership and channel following on a simulated PHY."""

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import unittest
from unittest import mock

import manet_wifi_uplink as wifi
import manet_acs_agreement as agreement
from test_acs_agreement import status as acs_status
from test_ap_mesh_transitions import Harness, SimTransition

TOOLS = Path(__file__).parent
PROFILE = dict(enabled=True, iface='wlan1', ssid='house', password='local-secret',
               bssid='02:00:00:00:01:01', security='wpa2', freq=5180)
SCAN = '''BSS 02:00:00:00:01:01(on wlan1)
\tfreq: 5180
\tsignal: -39.00 dBm
\tcapability: ESS Privacy (0x0011)
\tSSID: house
\tRSN:
\t\t * Authentication suites: PSK
BSS 02:00:00:00:01:02(on wlan1)
\tfreq: 5260
\tsignal: -45.00 dBm
\tcapability: ESS Privacy (0x0011)
\tSSID: <img onerror=bad>
\tRSN:
\t\t * Authentication suites: SAE
BSS 02:00:00:00:01:03(on wlan1)
\tfreq: 5200
\tsignal: -50.00 dBm
\tcapability: ESS Privacy (0x0011)
\tSSID: enterprise
\tRSN:
\t\t * Authentication suites: IEEE 802.1X
'''


class ClientModel(SimTransition):
    def __init__(self, radio, wpa):
        super().__init__(radio, wpa)
        self.associated = None
        self.ap_freq = None
        self.ap_state = ''
        self.ap_follow = False
        self.csa_works = True
        self.scan_fails = False
        self.unavailable = False
        self.dfs_available = False
        self.address = '192.0.2.10'
        self.client_enabled = True
        self.cac_at = None
        self.scan_busy = 0
        self.follower_supported = False
        self.ap_start_fails = False
        self.unexpected_cac = False

    def advance_dfs(self, seconds):
        """Model FCC scans blocked for the whole lifetime of a DFS AP."""
        if self.ap_state == 'DFS':
            if self.cac_at is None:
                self.cac_at = seconds
            if seconds - self.cac_at >= 60:
                self.ap_state = 'ENABLED'
        else:
            self.cac_at = None
        if self.associated is None and self.client_enabled:
            if self.ap_state and self.ap_freq == 5260 and not self.follower_supported:
                self.scan_busy += 1
            else:
                self.associated = 5260

    def command(self, args, timeout=10):
        self.sim.calls.append(' '.join(args))
        if args[:2] == ['iw', 'phy'] and args[-1] == 'info':
            band = '2.4' if args[2] == 'phy0' else '5'
            frequencies = [2412, 2437, 2462] if band == '2.4' else [5180, 5200, 5260, 5280]
            return '* mesh point\n' + '\n'.join(f'* {f}.0 MHz [{wifi.frequency_channel(f)}] (23 dBm)' +
                             (' (radar detection)\n  DFS state: ' +
                              ('unavailable' if self.unavailable else
                               'available' if self.dfs_available else 'usable')
                              if f in (5260, 5280) else '') for f in frequencies)
        if args[:2] == ['iw', 'phy'] and args[3:5] == ['interface', 'add']:
            vif = self.sysnet / wifi.AP_VIF
            (vif / 'phy80211').mkdir(parents=True, exist_ok=True)
            (vif / 'phy80211/name').write_text(args[2])
            address = (args[args.index('addr') + 1] if 'addr' in args
                       else (self.sysnet / 'wlan1/address').read_text())
            (vif / 'address').write_text(address)
            self.sim.kind[wifi.AP_VIF] = 'AP'
            return ''
        if args[:4] == ['ip', 'link', 'set', wifi.AP_VIF]:
            if args[4] == 'address':
                (self.sysnet / wifi.AP_VIF / 'address').write_text(args[5])
            return ''
        if args[:5] == ['ip', '-4', '-j', 'addr', 'show']:
            addresses = ([{'family': 'inet', 'local': self.address}]
                         if self.address else [])
            return json.dumps([{'addr_info': addresses}])
        if args == ['iw', 'dev', wifi.AP_VIF, 'del']:
            shutil.rmtree(self.sysnet / wifi.AP_VIF)
            return ''
        if args[:2] == ['iw', 'dev'] and args[3] == 'scan':
            if self.scan_fails:
                raise subprocess.CalledProcessError(1, args)
            self.assert_no_ap = 'hostapd.service' not in self.sim.active
            if args[2] == 'wlan0':
                return (SCAN.replace('5180', '2412').replace('5260', '2437')
                        .replace('5200', '2462'))
            return SCAN
        if args[:2] == ['iw', 'dev'] and args[3] == 'link':
            return f'Connected to {PROFILE["bssid"]}\n\tfreq: {self.associated}\n' if self.associated else 'Not connected.\n'
        if args == ['/usr/sbin/hostapd', '-v']:
            version = 'hostapd v' + (wifi.DFS_FOLLOW_MARKER if self.follower_supported else '2.10')
            raise subprocess.CalledProcessError(1, args, stderr=version)
        if args[0] == 'hostapd_cli':
            if 'status' in args:
                return f'state={self.ap_state}\nfreq={self.ap_freq or ""}\ndfs_follow_station={int(self.ap_follow)}\n'
            if 'chan_switch' in args:
                freq = int(args[args.index('chan_switch') + 2])
                if self.csa_works:
                    self.ap_freq = freq
                return 'OK\n'
        if args[0] == 'wpa_cli' and args[-1] in ('disconnect', 'reconnect'):
            self.client_enabled = args[-1] == 'reconnect'
            if args[-1] == 'disconnect':
                self.associated = None
            return 'OK\n'
        if args[0] == 'wpa_cli' and args[-1] == 'status':
            if self.associated is None:
                return 'wpa_state=SCANNING\n'
            profile = json.loads(Path(os.environ['MANET_WIFI_UPLINK_CONFIG']).read_text())
            return ('wpa_state=COMPLETED\nid_str=' + profile.get('save_id', '') +
                    '\nbssid=' + profile.get('bssid', '') + '\n')
        if args[:2] in (['systemctl', 'start'], ['systemctl', 'restart']) and args[2] == 'hostapd.service':
            if self.ap_start_fails:
                raise subprocess.CalledProcessError(1, args)
            self.ap_freq = 5000 + 5 * int(re.search(r'^channel=(\d+)', Path(os.environ['MANET_HOSTAPD_CONF']).read_text(), re.M)[1])
            follow = 'dfs_follow_station=1' in Path(os.environ['MANET_HOSTAPD_CONF']).read_text()
            self.ap_follow = follow
            self.ap_state = ('DFS' if self.ap_freq in (5260, 5280) and
                             (not follow or self.unexpected_cac) else 'ENABLED')
        if args[:3] == ['systemctl', 'stop', 'hostapd.service']:
            self.ap_state = ''
        # Avoid duplicate recording in the base simulation.
        self.sim.calls.pop()
        return super().command(args, timeout)


class UplinkHarness(Harness):
    def setUp(self):
        super().setUp()
        self.env2 = mock.patch.dict(os.environ, {
            'MANET_WIFI_UPLINK_CONFIG': str(self.root / 'etc/wifi.json'),
            'MANET_NETWORKD_DIR': str(self.root / 'network'),
            'MANET_HOSTAPD_CONF': str(self.root / 'hostapd.conf')})
        self.env2.start()
        self.addCleanup(self.env2.stop)
        self.mode('wireless')
        self.write('halow_if', 'wlan2')
        self.write('no_mesh_if', '')
        self.radio.bat.add('wlan2')
        (self.root / 'hostapd.conf').write_text('interface=wlan1\nssid=EUD\nwpa_passphrase=eud-secret\nchannel=36\n')
        (self.root / 'drivers/mt7915e').mkdir(parents=True)
        for n in (0, 1):
            base = self.root / f'net/wlan{n}'
            (base / 'device').mkdir()
            (base / 'device/driver').symlink_to(self.root / 'drivers/mt7915e')
            (base / 'phy80211').mkdir()
            (base / 'phy80211/name').write_text(f'phy{n}')
            (base / 'phy80211/macaddress').write_text(
                f'00:0a:52:0d:d1:{22 + n:02x}\n')
            (base / 'address').write_text(f'00:0a:52:0d:d1:{22 + n:02x}\n')
            (base / 'flags').write_text('0x1')
            (self.wpa / f'wpa_supplicant-wlan{n}.conf').write_text(f'network={{\nfrequency={2412 if n == 0 else 5180}\n}}\n')
        self.model = ClientModel(self.radio, self.wpa)
        self.model.gateway = self.root / 'gateway.state'
        self.uplink = wifi.WifiUplink()
        self.uplink.radio = self.model
        self.cap = self.root / 'dfs_follow_ap_v1'
        self.cap_patch = mock.patch.object(wifi, 'DFS_FOLLOW_CAP', self.cap)
        self.cap_patch.start()
        self.addCleanup(self.cap_patch.stop)
        self.policy = mock.patch.object(self.uplink, 'refresh_policy')
        self.policy.start()
        self.addCleanup(self.policy.stop)

    def mode(self, eud):
        conf = self.root / 'mesh.conf'
        source = re.sub(r'^eud=.*\n', '', conf.read_text(), flags=re.M)
        conf.write_text(source + f'eud={eud}\n')

    def choose(self, profile=None):
        value = profile or PROFILE
        self.uplink.store(self.uplink.scan_path, {'iface': value['iface'], 'at': time.clock_gettime(time.CLOCK_BOOTTIME), 'networks': [dict(value, selectable=True)]})
        return self.uplink.set_profile(value)

    def enable_follower(self):
        self.cap.write_text('Y\n')
        self.model.follower_supported = True


class BuiltinUplinkTests(UplinkHarness):

    def test_own_hotspots_removed_from_scan_storage_and_old_cache(self):
        conf = self.root / 'mesh.conf'
        conf.write_text(conf.read_text() + 'lan_ap_ssid=EUD\n')
        for iface in ('wlan0', 'wlan1'):
            with self.subTest(iface=iface), mock.patch(__name__ + '.SCAN',
                    SCAN.replace('house', 'EUD-5cc2')):
                result = self.uplink.discover(iface, allow_ap_stop=True)
                self.assertNotIn('EUD-5cc2', [r['ssid'] for r in result['networks']])
                stored = json.loads(self.uplink.scan_path.read_text())
                self.assertNotIn('EUD-5cc2', [r['ssid'] for r in stored['networks']])
        self.uplink.store(self.uplink.scan_path, {
            'iface': 'wlan1', 'at': time.clock_gettime(time.CLOCK_BOOTTIME),
            'networks': [dict(PROFILE, ssid='EUD-5cc2'), PROFILE]})
        self.assertEqual([r['ssid'] for r in self.uplink.saved_scan()['networks']], ['house'])

    def test_own_hotspot_connect_rejected_even_with_fresh_selection(self):
        conf = self.root / 'mesh.conf'
        conf.write_text(conf.read_text() + 'lan_ap_ssid=EUD\n')
        with self.assertRaisesRegex(ValueError, "one of this mesh's own hotspots"):
            self.choose(dict(PROFILE, ssid='EUD-5cc2'))
        self.assertFalse(self.uplink.config.exists())
        self.assertNotIn('manet-wifi-client@wlan1.service', self.radio.active)

    def test_existing_own_hotspot_is_disconnected_and_disabled(self):
        self.choose(dict(PROFILE, ssid='EUD-5cc2'))
        conf = self.root / 'mesh.conf'
        conf.write_text(conf.read_text() + 'lan_ap_ssid=EUD\n')
        self.model.associated = 5180
        self.uplink.reconcile()
        result = self.uplink.read_status()
        self.assertFalse(result['enabled'])
        self.assertFalse(result['connected'])
        self.assertFalse(result['gateway'])
        self.assertIn("one of this mesh's own hotspots", result['error'])
        self.assertFalse(self.uplink.profile()['enabled'])
        self.assertFalse(self.uplink.saved.exists())
        self.assertNotIn('manet-wifi-client@wlan1.service', self.radio.active)
        self.assertIn('ip addr flush dev wlan1', self.radio.calls)
        self.uplink.refresh_policy.assert_called()
        self.uplink.reconcile()
        self.assertIn("one of this mesh's own hotspots", self.uplink.read_status()['error'])

    def test_cli_rejects_own_hotspot_without_a_scan_or_hardware_change(self):
        conf = self.root / 'mesh.conf'
        conf.write_text(conf.read_text() + 'lan_ap_ssid=EUD\n')
        import sys
        result = subprocess.run([sys.executable, str(TOOLS / 'manet_wifi_uplink.py'), 'set'],
                                input=json.dumps(dict(PROFILE, ssid='EUD-5cc2')),
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("one of this mesh's own hotspots", json.loads(result.stdout)['error'])
        self.assertFalse(self.uplink.config.exists())

    def test_rejected_profile_stays_blocked_if_cleanup_needs_retry(self):
        self.choose(dict(PROFILE, ssid='EUD-5cc2'))
        conf = self.root / 'mesh.conf'
        conf.write_text(conf.read_text() + 'lan_ap_ssid=EUD\n')
        with mock.patch.object(self.uplink, 'release', side_effect=RuntimeError('busy')):
            with self.assertRaises(RuntimeError):
                self.uplink.reconcile()
        self.assertFalse(self.uplink.profile()['enabled'])
        self.assertFalse(wifi.uplink_profile_allowed('wlan1'))
        self.uplink.reconcile()
        self.assertNotIn('manet-wifi-client@wlan1.service', self.radio.active)


    def test_shared_client_owns_phy_and_secrets_stay_private(self):
        result = self.choose()
        self.assertEqual(result['state'], 'connecting')
        self.assertEqual(self.read('wifi_uplink_if'), 'wlan1')
        self.assertEqual(self.read('ap_interface'), 'manetap')
        self.assertNotIn('wlan1', self.read('mesh_if'))
        self.assertNotIn('wlan1', self.read('no_mesh_if'))
        self.assertNotIn('wlan1', self.radio.bat)
        self.assertIn('manet-wifi-client@wlan1.service', self.radio.active)
        for path in [self.uplink.config, self.wpa / 'wpa_supplicant-wlan1-uplink.conf', self.uplink.saved]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(PROFILE['password'], json.dumps(result))
        self.assertNotIn(PROFILE['password'], '\n'.join(self.radio.calls))
        self.assertNotIn(PROFILE['password'], (self.wpa / 'wpa_supplicant-wlan1-uplink.conf').read_text())

    def test_wired_mode_either_band_and_restore_mesh(self):
        self.mode('wired')
        self.write('ap_interface', '')
        self.write('ap_mesh_band', '')
        self.write('mesh_if', 'wlan0\nwlan1')
        self.write('mesh_5_if', 'wlan1')
        self.radio.bat.add('wlan1')
        for iface, freq in [('wlan0', 2412), ('wlan1', 5180)]:
            self.choose(dict(PROFILE, iface=iface, freq=freq))
            self.assertNotIn(iface, self.read('mesh_if'))
            self.assertFalse((self.root / 'net/manetap').exists())
            self.uplink.set_profile({'enabled': False})
            self.assertIn(iface, self.read('mesh_if'))
            self.assertIn(iface, self.radio.bat)

    def test_file_modes_are_correct_before_atomic_publication(self):
        public = [self.uplink.network / '05-wlan1-wifi-uplink.network',
                  self.uplink.network / '05-manetap.network',
                  self.lib / 'wifi_uplink_if']
        private = [self.uplink.config, self.uplink.saved,
                   self.wpa / 'wpa_supplicant-wlan1-uplink.conf']
        expected = {path: 0o644 for path in public}
        expected.update({path: 0o600 for path in private})
        observed = []
        replace = os.replace

        def record_mode(source, destination):
            """Inspect the inode networkd will see at the rename boundary."""
            path = Path(destination)
            if path in expected:
                observed.append((path, Path(source).stat().st_mode & 0o777))
            return replace(source, destination)

        with mock.patch.object(os, 'replace', side_effect=record_mode):
            self.choose()
            # Rewrites must also fix modes left by an earlier activation.
            for path in expected:
                path.chmod(0o600 if path in public else 0o644)
            self.uplink.store(self.uplink.config, PROFILE)
            self.uplink.store(self.uplink.saved,
                              json.loads(self.uplink.saved.read_text()))
            self.uplink.activate(PROFILE)
        self.assertEqual({path for path, _ in observed}, set(expected))
        for path, mode in observed:
            with self.subTest(path=path.name, published_mode=oct(mode)):
                self.assertEqual(mode, expected[path])
        for path, mode in expected.items():
            with self.subTest(final=path.name):
                self.assertEqual(path.stat().st_mode & 0o777, mode)

    def test_association_waits_for_address_in_status(self):
        for iface, freq in [('wlan0', 2412), ('wlan1', 5180)]:
            with self.subTest(iface=iface):
                self.choose(dict(PROFILE, iface=iface, freq=freq))
                self.model.associated = freq
                self.model.address = ''
                self.uplink.reconcile()
                self.uplink.reconcile()
                state = self.uplink.read_status()
                self.assertEqual(state['state'], 'waiting-address')
                self.assertTrue(state['connected'])
                self.assertEqual(state['ip'], '')
                if iface == 'wlan1':
                    self.uplink.prepare_shared_ap()
                    self.assertIn('hostapd.service', self.radio.active)
                self.model.address = '192.0.2.10'
                self.assertEqual(self.uplink.read_status()['state'],
                                 'connected')
                self.uplink.reconcile()
                self.assertEqual(self.uplink.read_status()['state'],
                                 'connected')
                self.model.address = ''
                self.assertEqual(self.uplink.read_status()['state'],
                                 'waiting-address')
                self.uplink.set_profile({'enabled': False})

    def test_disconnect_during_busy_agreement_restores_mesh(self):
        self.conf(acs='y')
        self.mode('wireless')
        self.choose(dict(PROFILE, iface='wlan0', freq=2412))
        busy = self.root / 'run/manet-acs-busy'
        busy.write_text(f'{"ab" * 16} 160.5\n')
        with mock.patch.object(wifi.time, 'clock_gettime', return_value=100.5):
            result = self.uplink.set_profile({'enabled': False})
        self.assertEqual(result['state'], 'disabled')
        self.assertFalse(self.uplink.profile()['enabled'])
        self.assertIsNone(self.read('wifi_uplink_if'))
        self.assertEqual(self.read('mesh_24_if'), 'wlan0')
        self.assertIn('wlan0', self.radio.bat)
        self.assertEqual(self.radio.freq['wlan0'], 2412)
        self.assertIn('wpa_supplicant@wlan0.service', self.radio.active)
        self.assertNotIn('manet-wifi-client@wlan0.service', self.radio.active)
        self.assertFalse(self.uplink.saved.exists())
        self.assertFalse((self.wpa / 'wpa_supplicant-wlan0-uplink.conf')
                         .exists())
        self.assertFalse((self.uplink.network /
                          '05-wlan0-wifi-uplink.network').exists())
        self.assertTrue(busy.exists())
        self.assertIn('networkctl reconfigure wlan0', self.radio.calls)

    def test_busy_disconnect_uses_existing_commitment(self):
        self.conf(acs='y')
        self.mode('wired')
        self.write('ap_interface', '')
        self.write('ap_mesh_band', '')
        (self.wpa / 'wpa_supplicant-wlan0.conf').write_text(
            'network={\nfrequency=2462\n}\n')
        (self.root / 'run/initial_time_synced').touch()
        created = (int(time.time()) // 180 - 1) * 180 + 45
        members = {'00:0a:52:0d:d1:16':
                   acs_status('a', current={'2.4': 2462})}
        plan = agreement.make_plan(members, {'2.4': 2462, '5': 5200},
                                   False, created, 'e' * 32)
        destination = {'plan': plan, 'commit': {
            'plan': agreement.digest(plan),
            'approvals': {m: st['boot'] for m, st in members.items()},
            'issued_at': plan['deadline']}}
        state = self.root / 'acs/agreement.json'
        state.write_text(json.dumps({'clock_boot': 'ab' * 16,
                                     'destination': destination}))
        before = state.read_bytes()
        self.choose()
        (self.root / 'run/manet-acs-busy').write_text(f'{"ab" * 16} 160.5\n')
        with mock.patch.object(wifi.time, 'clock_gettime', return_value=100.5):
            self.uplink.set_profile({'enabled': False})
        self.assertEqual(self.radio.freq['wlan1'], 5200)
        self.assertEqual(self.model.selection['source'], 'acs-agreement')
        self.assertEqual(state.read_bytes(), before)

    def test_shared_ap_mac_is_local_unique_and_stable_before_start(self):
        vif = self.model.sysnet / wifi.AP_VIF
        for first in ('00', '02'):
            with self.subTest(first=first):
                parent = self.model.sysnet / 'wlan1'
                source = first + ':0a:52:0d:d1:17'
                (parent / 'address').write_text(source)
                (parent / 'phy80211/macaddress').write_text(source)
                self.choose()
                address = (vif / 'address').read_text().strip()
                self.assertNotEqual(address, source)
                self.assertEqual(int(address[:2], 16) & 3, 2)
                command = 'iw phy phy1 interface add manetap type __ap addr '
                self.assertIn(command + address, self.radio.calls)
                self.uplink.set_profile({'enabled': False})
                self.choose()
                self.assertEqual((vif / 'address').read_text().strip(), address)
                self.uplink.set_profile({'enabled': False})

    def test_shared_ap_mac_uses_permanent_phy_address(self):
        (self.model.sysnet / 'wlan1/address').write_text('02:00:00:00:10:00')
        self.choose()
        vif = self.model.sysnet / wifi.AP_VIF
        self.assertEqual((vif / 'address').read_text(), '02:0a:52:0d:d1:17')

    def test_existing_shared_ap_mac_is_repaired_before_start(self):
        self.choose()
        vif = self.model.sysnet / wifi.AP_VIF
        (vif / 'address').write_text(
            (self.model.sysnet / 'wlan1/address').read_text())
        self.radio.calls.clear()
        self.uplink.activate(PROFILE)
        address = (vif / 'address').read_text().strip()
        self.assertNotEqual(address, '00:0a:52:0d:d1:17')
        self.assertLess(self.radio.calls.index('ip link set manetap down'),
                        self.radio.calls.index(
                            'ip link set manetap address ' + address))
        self.assertNotIn('ip link set manetap up', self.radio.calls)

    def test_shared_ap_mac_avoids_all_local_interfaces(self):
        occupied = ['02:0a:52:0d:d1:17', '02:0a:52:0d:d1:18',
                    '02:0a:52:0d:d1:19']
        for name, address in zip(('br0', 'bat0', 'dummy0'), occupied):
            path = self.model.sysnet / name
            path.mkdir()
            (path / 'address').write_text(address.upper() + '\n')
        self.choose()
        vif = self.model.sysnet / wifi.AP_VIF
        address = (vif / 'address').read_text().strip()
        others = {path.read_text().strip().lower()
                  for path in self.model.sysnet.glob('*/address')
                  if path.parent != vif}
        self.assertNotIn(address, others)
        self.assertEqual(int(address[:2], 16) & 3, 2)
        self.uplink.set_profile({'enabled': False})
        self.choose()
        self.assertEqual((vif / 'address').read_text().strip(), address)

    def test_wifi_eud_24_requires_surviving_halow(self):
        self.radio.bat.discard('wlan2')
        with self.assertRaises(RuntimeError):
            self.choose(dict(PROFILE, iface='wlan0', freq=2412))
        self.assertFalse(self.uplink.profile()['enabled'])
        self.assertIsNone(self.read('wifi_uplink_if'))

    def test_eud_mode_change_requires_disconnect_before_role_rebuild(self):
        import manet_eud_ap
        self.choose()
        before = (self.root / 'mesh.conf').read_bytes()
        with self.assertRaisesRegex(ValueError, 'Disconnect'):
            manet_eud_ap.apply_local({'eud': 'wired'}, self.root / 'mesh.conf',
                                    self.uplink.hostapd, self.lib / 'ap_interface')
        self.assertEqual((self.root / 'mesh.conf').read_bytes(), before)

    def test_disconnect_preserves_local_ap_credentials(self):
        self.choose()
        source = self.uplink.hostapd.read_text().replace('ssid=EUD', 'ssid=changed')
        self.uplink.hostapd.write_text(source)
        self.uplink.set_profile({'enabled': False})
        self.assertEqual(self.read('ap_interface'), 'wlan1')
        self.assertFalse((self.root / 'net/manetap').exists())
        self.assertIn('ssid=changed', self.uplink.hostapd.read_text())
        self.assertFalse((self.wpa / 'wpa_supplicant-wlan1-uplink.conf').exists())

    def test_scan_stops_ap_and_restores_after_success_or_error(self):
        self.model.ap_state, self.model.ap_freq = 'ENABLED', 5180
        rows = self.uplink.discover('wlan1')['networks']
        self.assertTrue(self.model.assert_no_ap)
        self.assertEqual([row['security'] for row in rows], ['wpa2', 'sae', 'unsupported'])
        self.assertTrue(rows[1]['dfs'])
        self.assertFalse(rows[2]['selectable'])
        self.assertIn('hostapd.service', self.radio.active)
        self.model.scan_fails = True
        with self.assertRaises(RuntimeError):
            self.uplink.discover('wlan1')
        self.assertIn('hostapd.service', self.radio.active)

    def test_shared_scan_keeps_associated_ap_and_live_status(self):
        for freq in (5180, 5260):
            with self.subTest(freq=freq):
                self.enable_follower()
                self.choose(dict(PROFILE, freq=freq))
                self.model.associated = freq
                self.uplink.reconcile()
                self.radio.calls.clear()
                result = self.uplink.discover('wlan1')
                self.assertTrue(result['networks'])
                self.assertFalse(self.model.assert_no_ap)
                self.assertNotIn('systemctl stop hostapd.service',
                                 self.radio.calls)
                self.assertEqual(self.uplink.read_status()['state'],
                                 'connected')

    def test_scan_keeps_results_when_ap_restore_fails(self):
        self.model.ap_start_fails = True
        result = self.uplink.discover('wlan1')
        self.assertTrue(result['networks'])
        self.assertIn('hotspot', result['warning'])
        self.assertEqual(self.uplink.read_status()['scan']['networks'],
                         result['networks'])

    def test_mesh_scan_restores_live_channel_and_retries_separately(self):
        self.radio.freq['wlan0'] = 2462
        with mock.patch.object(self.model, 'start_mesh',
                               side_effect=RuntimeError('restore lag')):
            result = self.uplink.discover('wlan0')
        self.assertTrue(result['networks'])
        self.assertEqual(result['networks'][0]['freq'], 2412)
        self.assertIn('restore lag', result['warning'])
        saved = json.loads(self.uplink.scan_path.read_text())['networks']
        self.assertEqual([row['bssid'] for row in saved],
                         [row['bssid'] for row in result['networks']])
        self.assertIn('frequency=2462',
                      (self.wpa / 'wpa_supplicant-wlan0.conf').read_text())
        self.uplink.reconcile()
        self.assertIn('wlan0', self.radio.bat)
        self.assertEqual(self.radio.freq['wlan0'], 2462)
        self.assertNotIn('warning', self.uplink.read_status()['scan'])

    def test_scan_disruption_requires_ack_and_rechecks_live_state(self):
        self.assertTrue(self.uplink.scan_plan('wlan1')['interrupts_ap'])
        self.radio.calls.clear()
        with self.assertRaisesRegex(ValueError, 'disconnect'):
            self.uplink.discover('wlan1', allow_ap_stop=False)
        self.assertNotIn('systemctl stop hostapd.service', self.radio.calls)

    def test_saved_scan_age_does_not_renew_on_status_reads(self):
        self.uplink.discover('wlan1')
        at = json.loads(self.uplink.scan_path.read_text())['at']
        with mock.patch.object(wifi.time, 'clock_gettime',
                               return_value=at + 301):
            scan = self.uplink.read_status()['scan']
        self.assertAlmostEqual(scan['age_seconds'], 301)
        self.assertFalse(scan['fresh'])
        self.assertTrue(scan['networks'])

    def test_watch_during_scan_keeps_connection_status(self):
        self.choose()
        self.model.associated = 5180
        self.uplink.reconcile()
        before = self.uplink.status_path.read_bytes()
        with mock.patch.dict(os.environ, {
                'MANET_RADIO_SETUP_LOCK': str(self.root / 'setup.lock')}), \
                mock.patch('manet_ap_mesh.LOCK_TIMEOUT', .01), \
                wifi.locked(self.uplink.lock):
            self.uplink.watch_once()
        self.assertEqual(self.uplink.status_path.read_bytes(), before)

    def test_scan_preflight_cannot_authorize_later_hotspot_drop(self):
        self.choose()
        self.model.associated = 5180
        self.uplink.reconcile()
        self.assertFalse(self.uplink.scan_plan('wlan1')['interrupts_ap'])
        self.model.associated = None
        self.radio.calls.clear()
        with self.assertRaisesRegex(ValueError, 'disconnect'):
            self.uplink.discover('wlan1', allow_ap_stop=False)
        self.assertNotIn('systemctl stop hostapd.service', self.radio.calls)

    def test_pending_ap_repair_is_checked_before_scan_interruption(self):
        self.radio.active.discard('hostapd.service')
        self.uplink.store(self.uplink.repair_path,
                          {'iface': 'wlan1', 'kind': 'ap'})
        with self.assertRaisesRegex(ValueError, 'disconnect'):
            self.uplink.discover('wlan1', allow_ap_stop=False)
        self.assertIn('hostapd.service', self.radio.active)
        self.assertNotIn('systemctl stop hostapd.service', self.radio.calls)

    def test_restore_warning_does_not_turn_working_client_into_error(self):
        self.choose()
        self.model.associated = 5180
        self.uplink.reconcile()
        with mock.patch.object(self.model, 'start_mesh',
                               side_effect=RuntimeError('restore lag')):
            self.uplink.discover('wlan0')
            self.uplink.reconcile()
        state = self.uplink.read_status()
        self.assertEqual(state['state'], 'connected')
        self.assertNotIn('error', state)
        self.assertIn('restore lag', state['scan']['warning'])

    def test_failed_scan_retains_driver_reason_and_prior_results(self):
        self.uplink.discover('wlan1')
        old = self.uplink.scan_path.read_bytes()
        original = self.model.command

        def fail_scan(args, timeout=10):
            """Inject the driver's busy diagnostic only at the scan call."""
            if args[:2] == ['iw', 'dev'] and 'scan' in args:
                raise subprocess.CalledProcessError(
                    1, args, stderr='command failed: Device busy (-16)')
            return original(args, timeout)

        with mock.patch.object(self.model, 'command', side_effect=fail_scan):
            with self.assertRaisesRegex(RuntimeError, 'Device busy'):
                self.uplink.discover('wlan1')
        self.assertEqual(self.uplink.scan_path.read_bytes(), old)
        self.assertIn('hostapd.service', self.radio.active)

    def test_scan_teardown_failure_restarts_prior_mesh_owner(self):
        self.radio.fail_on.add('batctl if del wlan0')
        with self.assertRaises(RuntimeError):
            self.uplink.discover('wlan0')
        self.assertIn('wpa_supplicant@wlan0.service', self.radio.active)
        self.assertIn('wlan0', self.radio.bat)

    def test_scan_restores_mesh_attachment_on_failure(self):
        self.model.scan_fails = True
        with self.assertRaises(RuntimeError):
            self.uplink.discover('wlan0')
        self.assertIn('wlan0', self.radio.bat)
        self.assertEqual(self.read('mesh_24_if'), 'wlan0')

    def test_channel_change_uses_csa_then_verifies(self):
        self.choose()
        self.model.associated = 5180
        self.uplink.reconcile()
        self.assertEqual(self.model.ap_freq, 5180)
        self.model.associated = 5200
        self.uplink.reconcile()
        self.assertEqual(self.model.ap_freq, 5200)
        self.assertTrue(any('chan_switch 5 5200' in c for c in self.radio.calls))
        self.radio.calls.clear()
        self.uplink.reconcile()
        self.assertEqual(self.uplink.read_status()['state'], 'connected')
        self.assertFalse(any('systemctl stop' in c for c in self.radio.calls))

    def test_csa_accepted_without_effect_falls_back_to_restart(self):
        self.choose()
        self.model.associated = 5180
        self.uplink.reconcile()
        self.uplink.reconcile()
        self.model.csa_works = False
        self.model.associated = 5200
        self.uplink.reconcile()
        self.uplink.reconcile()
        self.assertEqual(self.model.ap_freq, 5200)
        self.assertIn('systemctl stop hostapd.service', self.radio.calls)

    def test_dfs_follower_starts_after_association_without_cac(self):
        self.enable_follower()
        self.choose(dict(PROFILE, freq=5260))
        self.radio.calls.clear()
        self.uplink.reconcile()
        self.assertFalse(self.model.ap_state)
        self.model.associated = 5260
        for _ in range(100):
            self.uplink.reconcile()
        self.assertEqual(self.model.ap_state, 'ENABLED')
        self.assertEqual(self.model.associated, 5260)
        self.assertTrue(self.uplink.read_status()['dfs_follow'])
        self.assertEqual(self.radio.calls.count('systemctl start hostapd.service'), 1)
        self.assertFalse(any(c.endswith(' disconnect') for c in self.radio.calls))
        self.assertIn('dfs_follow_station=1', self.uplink.hostapd.read_text())
        self.assertIn('ieee80211h=1', self.uplink.hostapd.read_text())

    def test_dfs_follower_loss_reconnect_cycle_releases_ap(self):
        self.enable_follower()
        self.choose(dict(PROFILE, freq=5260))
        for _ in range(3):
            self.model.associated = 5260
            self.uplink.reconcile()
            self.uplink.reconcile()
            self.assertEqual(self.model.ap_state, 'ENABLED')
            self.model.associated = None
            self.radio.calls.clear()
            self.uplink.reconcile()
            self.assertFalse(self.model.ap_state)
            calls = self.radio.calls
            self.assertLess(calls.index('systemctl stop hostapd.service'),
                            next(i for i, c in enumerate(calls) if c.endswith(' reconnect')))
            self.model.advance_dfs(3)
            self.assertEqual(self.model.scan_busy, 0)
        self.uplink.reconcile()
        self.assertEqual(self.model.ap_state, 'ENABLED')

    def test_dfs_follower_csa_and_radar_master_move_restart_on_new_channel(self):
        self.enable_follower()
        self.choose()
        for freq in (5180, 5260, 5280, 5200, 5260):
            self.model.associated = freq
            self.uplink.reconcile()
            self.uplink.reconcile()
            self.assertEqual(self.model.ap_freq, freq)
            self.assertEqual(self.model.ap_state, 'ENABLED')
            self.assertEqual('dfs_follow_station=1' in self.uplink.hostapd.read_text(),
                             freq in (5260, 5280))
        self.assertFalse(any(c.endswith(' disconnect') for c in self.radio.calls))
        self.model.unavailable = True
        self.uplink.reconcile()
        self.assertFalse(self.model.ap_state)
        self.assertEqual(self.uplink.read_status()['state'], 'ap-channel-unavailable')

    def test_dfs_follower_replaces_old_ap_using_running_mode_not_config(self):
        self.enable_follower()
        self.choose(dict(PROFILE, freq=5260))
        self.model.associated = 5260
        self.model.ap_state = 'ENABLED'
        self.model.ap_freq = 5260
        self.model.ap_follow = False
        self.radio.active.add('hostapd.service')
        # A config rewrite alone does not prove the running AP uses the mode.
        self.uplink.write_ap_channel(5260, dfs_follow=True)
        self.radio.calls.clear()
        self.uplink.reconcile()
        self.assertIn('systemctl stop hostapd.service', self.radio.calls)
        self.assertIn('systemctl start hostapd.service', self.radio.calls)
        self.assertTrue(self.model.ap_follow)

    def test_dfs_follower_requires_both_loaded_kernel_and_hostapd(self):
        self.choose(dict(PROFILE, freq=5260))
        self.model.associated = 5260
        for cap, host in ((None, True), ('N', True), ('Y', False), ('garbage', True)):
            with self.subTest(cap=cap, host=host):
                self.cap.unlink(missing_ok=True)
                if cap is not None:
                    self.cap.write_text(cap)
                self.model.follower_supported = host
                self.uplink.reconcile()
                self.assertFalse(self.model.ap_state)
                self.assertEqual(self.uplink.read_status()['ap_reason'], wifi.DFS_AP_REASON)
                with self.assertRaisesRegex(ValueError, 'EUD hotspot off'):
                    self.uplink.prepare_shared_ap()

    def test_dfs_follower_preparation_uses_live_state_and_restores_normal_config(self):
        self.enable_follower()
        self.choose()
        self.uplink.publish('connected', freq=5180)
        self.model.associated = 5260
        self.uplink.prepare_shared_ap()
        self.assertIn('channel=52', self.uplink.hostapd.read_text())
        self.assertIn('dfs_follow_station=1', self.uplink.hostapd.read_text())
        self.model.associated = None
        with self.assertRaisesRegex(ValueError, 'Waiting for upstream'):
            self.uplink.prepare_shared_ap()
        self.model.associated = 5200
        self.uplink.prepare_shared_ap()
        self.assertNotIn('dfs_follow_station', self.uplink.hostapd.read_text())
        self.assertIn('ieee80211h=1', self.uplink.hostapd.read_text())
        self.uplink.set_profile({'enabled': False})
        self.assertNotIn('dfs_follow_station', self.uplink.hostapd.read_text())

    def test_dfs_follower_failed_starts_and_unexpected_cac_are_bounded(self):
        self.enable_follower()
        self.choose(dict(PROFILE, freq=5260))
        self.model.associated = 5260
        self.model.ap_start_fails = True
        self.radio.calls.clear()
        for _ in range(20):
            try:
                self.uplink.reconcile()
            except RuntimeError:
                pass
        self.assertEqual(self.radio.calls.count('systemctl start hostapd.service'), 3)
        self.assertEqual(self.uplink.read_status()['ap_blocked'], 'dfs-start-failed')
        with self.assertRaisesRegex(ValueError, 'startup failed'):
            self.uplink.prepare_shared_ap()
        # Observed disassociation clears the latch; the next association retries.
        self.model.associated = None
        self.uplink.reconcile()
        self.model.associated = 5260
        self.model.ap_start_fails = False
        self.model.unexpected_cac = True
        self.uplink.reconcile()
        self.assertEqual(self.model.ap_state, 'DFS')
        self.uplink.reconcile()
        self.assertFalse(self.model.ap_state)
        self.assertEqual(self.uplink.read_status()['ap_blocked'], 'dfs-start-failed')
        for _ in range(20):
            self.uplink.reconcile()
        self.assertFalse(self.model.ap_state)

    def test_dfs_follower_scan_and_auto_policy_restore_after_live_check(self):
        self.enable_follower()
        self.mode('auto')
        self.model.gateway.touch()
        self.choose(dict(PROFILE, freq=5260))
        self.model.associated = 5260
        self.uplink.reconcile()
        self.uplink.discover('wlan1')
        self.assertEqual(self.model.ap_state, 'ENABLED')
        with mock.patch.object(self.uplink, 'ap_wanted', return_value=False):
            self.uplink.reconcile()
        self.assertFalse(self.model.ap_state)
        self.uplink.reconcile()
        self.assertEqual(self.model.ap_state, 'ENABLED')

    def test_dfs_upstream_does_not_loop_cac_and_reconnect(self):
        """Reproduce repeated 60 s CAC, 20 s blocked scan, reconnect cycles."""
        self.mode('auto')
        self.model.gateway.touch()
        self.choose(dict(PROFILE, freq=5260))
        self.radio.calls.clear()
        with mock.patch.object(wifi.time, 'monotonic') as clock:
            for seconds in range(3, 301, 3):
                clock.return_value = seconds
                self.model.advance_dfs(seconds)
                self.uplink.reconcile()
        starts = self.radio.calls.count('systemctl start hostapd.service')
        self.assertEqual(starts, 0,
                         f'{starts} CAC starts, {self.model.scan_busy} '
                         'reconnect scans blocked by the shared DFS AP')
        self.assertEqual(self.model.scan_busy, 0)
        self.assertEqual(self.model.associated, 5260)
        self.assertNotIn('hostapd.service', self.radio.active)
        status = self.uplink.read_status()
        self.assertEqual(status['state'], 'connected')
        self.assertTrue(status['connected'])
        self.assertEqual(status['ap_blocked'], 'dfs-upstream')
        self.assertIn('EUD hotspot off', status['ap_reason'])
        self.assertFalse(any(c.endswith('disconnect')
                             for c in self.radio.calls))

    def test_existing_dfs_ap_is_released_without_reconnect_grace(self):
        """A watcher restart releases CAC and post-CAC scan deadlocks."""
        self.choose(dict(PROFILE, freq=5260))
        for ap_state, prior in (('DFS', 'cac'),
                                ('ENABLED', 'ap-reconnect'),
                                ('ENABLED', 'connected')):
            with self.subTest(ap=ap_state, prior=prior):
                self.model.associated = None
                self.model.client_enabled = False
                self.model.ap_state = ap_state
                self.model.ap_freq = 5260
                self.radio.active.add('hostapd.service')
                self.uplink.publish(prior, freq=5260,
                                    reconnect_until=time.monotonic() + 20)
                self.radio.calls.clear()
                self.uplink.reconcile()
                self.assertNotIn('hostapd.service', self.radio.active)
                stop = self.radio.calls.index('systemctl stop hostapd.service')
                reconnect = next(i for i, c in enumerate(self.radio.calls)
                                 if c.endswith('reconnect'))
                self.assertLess(stop, reconnect)
                self.model.advance_dfs(3)
                self.uplink.reconcile()
                self.assertEqual(self.model.associated, 5260)
                self.assertEqual(self.uplink.read_status()['ap_blocked'],
                                 'dfs-upstream')
                self.assertNotIn('systemctl start hostapd.service',
                                 self.radio.calls)

    def test_dfs_preparation_rechecks_live_channel_and_association(self):
        """Policy starts cannot trust an earlier non-DFS watcher result."""
        self.choose()
        self.uplink.publish('connected', freq=5180)
        before = self.uplink.hostapd.read_bytes()
        self.model.associated = 5260
        with self.assertRaisesRegex(ValueError, 'EUD hotspot off'):
            self.uplink.prepare_shared_ap()
        self.model.associated = None
        with self.assertRaisesRegex(ValueError, 'Waiting for upstream'):
            self.uplink.prepare_shared_ap()
        self.assertEqual(self.uplink.hostapd.read_bytes(), before)

    def test_dfs_available_channel_still_blocks_shared_ap(self):
        """A completed CAC does not remove the FCC scanning restriction."""
        self.choose(dict(PROFILE, freq=5260))
        self.model.associated = 5260
        self.model.dfs_available = True
        self.radio.calls.clear()
        self.uplink.reconcile()
        self.assertEqual(self.uplink.read_status()['ap_blocked'],
                         'dfs-upstream')
        with self.assertRaisesRegex(ValueError, 'EUD hotspot off'):
            self.uplink.prepare_shared_ap()
        self.assertNotIn('systemctl start hostapd.service', self.radio.calls)

    def test_dfs_uplink_keeps_address_and_gateway_status(self):
        """Hotspot suppression is separate from DHCP and gateway selection."""
        self.choose(dict(PROFILE, freq=5260))
        self.model.associated = 5260
        self.model.address = ''
        self.uplink.reconcile()
        self.assertEqual(self.uplink.read_status()['state'], 'waiting-address')
        self.model.address = '192.0.2.10'
        self.model.gateway.touch()
        for upstream in ('wlan1', 'end0', 'enx1234', 'usb0'):
            with self.subTest(upstream=upstream):
                (self.model.run / 'upstream_iface').write_text(upstream + '\n')
                self.uplink.reconcile()
                status = self.uplink.read_status()
                self.assertEqual(status['state'], 'connected')
                self.assertEqual(status['gateway'], upstream == 'wlan1')
                self.assertIn('DFS', status['ap_reason'])
                self.assertEqual(self.model.associated, 5260)

    def test_dfs_channel_move_disables_ap_and_non_dfs_restores_it(self):
        """Keep the station across DFS moves and resume channel following."""
        self.choose()
        self.model.associated = 5180
        self.uplink.reconcile()
        self.assertIn('hostapd.service', self.radio.active)
        self.radio.calls.clear()
        self.model.associated = 5260
        self.uplink.reconcile()
        self.assertNotIn('hostapd.service', self.radio.active)
        self.assertEqual(self.model.associated, 5260)
        self.assertFalse(any('chan_switch' in c for c in self.radio.calls))
        self.model.associated = 5200
        self.uplink.reconcile()
        self.uplink.reconcile()
        self.assertEqual(self.model.ap_freq, 5200)
        self.assertEqual(self.uplink.read_status()['state'], 'connected')
        self.assertNotIn('ap_reason', self.uplink.read_status())

    def test_dfs_scan_does_not_restore_blocking_ap(self):
        """A scan can release an old DFS AP but must not restart its CAC."""
        self.choose(dict(PROFILE, freq=5260))
        self.model.associated = 5260
        for failure in (False, True):
            with self.subTest(scan_failure=failure):
                self.model.ap_state, self.model.ap_freq = 'DFS', 5260
                self.radio.active.add('hostapd.service')
                self.model.scan_fails = failure
                self.radio.calls.clear()
                if failure:
                    with self.assertRaises(RuntimeError):
                        self.uplink.discover('wlan1')
                else:
                    self.uplink.discover('wlan1')
                self.assertNotIn('hostapd.service', self.radio.active)
                self.assertNotIn('systemctl start hostapd.service',
                                 self.radio.calls)

    def test_dfs_wired_uplink_does_not_change_an_independent_ap(self):
        """DFS suppression only applies to an AP sharing the client PHY."""
        self.mode('wired')
        self.choose(dict(PROFILE, freq=5260))
        self.model.associated = 5260
        self.radio.calls.clear()
        self.uplink.reconcile()
        self.assertEqual(self.uplink.read_status()['state'], 'connected')
        self.assertNotIn('ap_blocked', self.uplink.read_status())
        self.assertFalse(any('hostapd' in c for c in self.radio.calls))

    def test_auto_policy_does_not_restart_dfs_ap(self):
        """Wired-EUD policy changes cannot lift the DFS sharing restriction."""
        self.mode('auto')
        self.choose(dict(PROFILE, freq=5260))
        self.model.associated = 5260
        self.radio.calls.clear()
        for wanted in (True, False, True):
            with mock.patch.object(self.uplink, 'ap_wanted',
                                   return_value=wanted):
                self.uplink.reconcile()
            with self.assertRaisesRegex(ValueError, 'EUD hotspot off'):
                self.uplink.prepare_shared_ap()
        self.assertNotIn('systemctl start hostapd.service', self.radio.calls)
        self.assertEqual(self.model.associated, 5260)

    def test_existing_dfs_ap_with_connected_sta_is_stopped(self):
        """Even an already connected DFS pair must leave future scans free."""
        self.choose(dict(PROFILE, freq=5260))
        self.model.ap_state, self.model.ap_freq = 'ENABLED', 5260
        self.radio.active.add('hostapd.service')
        self.model.associated = 5260
        self.uplink.reconcile()
        self.assertEqual(self.model.associated, 5260)
        self.assertNotIn('hostapd.service', self.radio.active)

    def test_non_dfs_shared_scan_restores_ap(self):
        """Scanning retains the existing non-DFS AP restoration behavior."""
        self.choose()
        self.model.associated = 5180
        self.uplink.reconcile()
        for failure in (False, True):
            self.model.scan_fails = failure
            if failure:
                with self.assertRaises(RuntimeError):
                    self.uplink.discover('wlan1')
            else:
                self.uplink.discover('wlan1')
            self.assertIn('hostapd.service', self.radio.active)
            self.assertEqual(self.model.ap_freq, 5180)
            self.uplink.reconcile()

    def test_radar_unavailable_does_not_restart_ap_on_upstream_channel(self):
        self.choose()
        self.model.associated = 5260
        self.model.unavailable = True
        self.radio.calls.clear()
        self.uplink.reconcile()
        self.assertEqual(self.uplink.read_status()['state'], 'ap-channel-unavailable')
        self.assertNotIn('systemctl start hostapd.service', self.radio.calls)

    def test_lost_client_releases_stale_ap_channel(self):
        self.choose()
        self.model.associated = 5180
        self.uplink.reconcile()
        self.model.associated = None
        self.uplink.reconcile()
        self.assertNotIn('hostapd.service', self.radio.active)
        self.assertEqual(self.uplink.read_status()['state'], 'connecting')

    def test_setup_reserves_client_before_surviving_mesh_is_ready(self):
        self.uplink.store(self.uplink.config, PROFILE)
        self.radio.bat.clear()
        self.uplink.reserve_profile()
        self.assertEqual(self.read('wifi_uplink_if'), 'wlan1')
        self.assertNotIn('wlan1', self.read('mesh_if'))
        self.assertNotIn('manet-wifi-client@wlan1.service', self.radio.active)
        self.radio.bat.add('wlan2')
        self.uplink.reconcile()
        self.assertIn('manet-wifi-client@wlan1.service', self.radio.active)

    def test_boot_recreates_virtual_ap_without_overwriting_restore_record(self):
        self.choose()
        original = self.uplink.saved.read_bytes()
        shutil.rmtree(self.root / 'net/manetap')
        self.radio.active.discard('manet-wifi-client@wlan1.service')
        self.uplink.reconcile()
        self.assertTrue((self.root / 'net/manetap').exists())
        self.assertEqual(self.uplink.saved.read_bytes(), original)

    def test_failed_dhcp_setup_restores_prior_mesh_ownership(self):
        self.radio.fail_on.add('networkctl reconfigure wlan0')
        with self.assertRaises(RuntimeError):
            self.choose(dict(PROFILE, iface='wlan0', freq=2412))
        self.assertIn('wlan0', self.read('mesh_if'))
        self.assertNotIn('manet-wifi-client@wlan0.service', self.radio.active)

    def test_registry_reports_client_without_upstream_ssid(self):
        import manet_interfaces
        self.choose()
        raw = json.dumps([{'ifname': 'wlan1', 'operstate': 'UP', 'addr_info': []}])
        def query(*args):
            if args == ('ip', '-j', 'addr'):
                return raw
            if args == ('iw', 'dev'):
                return '\tInterface wlan1\n\t\ttype managed\n\t\tssid house\n\t\tchannel 36 (5180 MHz)\n'
            return ''
        with mock.patch.object(manet_interfaces, 'command', side_effect=query):
            result = manet_interfaces.collect(self.model.sysnet, self.lib / 'no_mesh_if')
        self.assertEqual(result[0]['role'], 'uplink')
        self.assertNotIn('house', json.dumps(result))
        self.assertNotIn(PROFILE['password'], json.dumps(result))

    def test_mesh_roles_stay_excluded_if_stale_files_reappear(self):
        self.choose()
        self.write('mesh_5_if', 'wlan1')
        command = f'. "{TOOLS}/mesh-acs-common.sh"; load_mesh_roles; printf "%s" "$WPA_IFACE_5_0"'
        result = subprocess.run(['bash', '-c', command], capture_output=True, text=True)
        self.assertEqual(result.stdout, '')
        self.uplink.reconcile()
        self.assertEqual(self.read('mesh_5_if'), '')

    def test_stale_scan_and_wrong_band_fail_before_mutation(self):
        self.uplink.store(self.uplink.scan_path, {'iface': 'wlan1', 'at': time.clock_gettime(time.CLOCK_BOOTTIME)-301, 'networks': [PROFILE]})
        with self.assertRaises(ValueError):
            self.uplink.set_profile(PROFILE)
        self.assertFalse(self.uplink.saved.exists())
        with self.assertRaises(ValueError):
            wifi.validate_profile(dict(PROFILE, freq=2412))
        self.assertFalse(self.uplink.config.exists())

    def test_disabled_scan_selection_is_rejected_before_changing_roles(self):
        self.uplink.store(self.uplink.scan_path, {
            'iface': 'wlan1', 'at': time.clock_gettime(time.CLOCK_BOOTTIME),
            'networks': [dict(PROFILE, selectable=False)]})
        self.radio.calls.clear()
        with self.assertRaisesRegex(ValueError, 'fresh list'):
            self.uplink.set_profile(PROFILE)
        self.assertFalse(self.uplink.config.exists())
        self.assertFalse(self.radio.calls)

    def test_mesh_guard_and_power_policy_cannot_take_client(self):
        self.choose()
        self.assertFalse(self.model.ap_power()['changed'])
        result = subprocess.run(['bash', str(TOOLS / 'manet-ap-guard.sh'), 'wlan1'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.model.to_mesh()['mode'], 'uplink')
        self.assertEqual(self.read('wifi_uplink_if'), 'wlan1')


class ProfileTests(unittest.TestCase):
    def test_literal_credentials_and_following_frequency_list(self):
        profile = wifi.validate_profile(dict(PROFILE, ssid='name"\\test', password='$(touch /tmp/not-created)'))
        output = wifi.client_config(profile, [5180, 5200, 5260, 5280], 'US')
        self.assertIn('freq_list=5180 5200 5260', output)
        self.assertIn('ssid=' + profile['ssid'].encode().hex(), output)
        self.assertNotIn(profile['password'], output)
        with self.assertRaises(ValueError):
            wifi.validate_profile(dict(PROFILE, ssid='a\nnetwork={'))

    def test_api_uses_stdin_and_never_coordinates_credentials(self):
        import manet_manage as manage
        response = subprocess.CompletedProcess([], 0, '{"state":"connecting"}', '')
        with mock.patch.object(manage.subprocess, 'run', return_value=response) as run:
            manage.builtin_wifi_request('set', PROFILE)
            args, kwargs = run.call_args
            self.assertNotIn(PROFILE['password'], ' '.join(args[0]))
            self.assertEqual(json.loads(kwargs['input']), PROFILE)
        with mock.patch.object(manage, 'send_alfred_object') as publish:
            result = manage.coordinate_radio_change({'uplink_wifi': PROFILE})
            self.assertFalse(result['ok'])
            publish.assert_not_called()

    def test_receiver_rejects_credentials_before_ack(self):
        spec = importlib.util.spec_from_file_location('radio_state_wifi_test', TOOLS / 'mesh-radio-state.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ok, why = module.validate_pkg({'kind': 'radio_state', 'version': 'a', 'uplink_wifi': PROFILE})
        self.assertFalse(ok)
        self.assertIn('node-local', why)



class WatcherRewakeTests(unittest.TestCase):
    """A watcher that exits as new work arrives is started once more."""

    class Uplink:
        def __init__(self, pending):
            self.pending = pending

        def watch_pending(self):
            return self.pending

    def rewake(self, pending, **environ):
        with mock.patch.object(wifi.subprocess, 'run') as run:
            restarted = wifi.rewake(self.Uplink(pending), environ)
        return restarted, run.call_args_list

    def test_clean_exit_with_pending_work_queues_a_start(self):
        restarted, calls = self.rewake(True, EXIT_CODE='exited', EXIT_STATUS='0')
        self.assertTrue(restarted)
        self.assertEqual(calls[0].args[0],
                         ['systemctl', '--no-block', 'start', 'manet-wifi-uplink.service'])

    def test_idle_exit_stop_request_or_crash_does_not_restart(self):
        for pending, environ in ((False, dict(EXIT_CODE='exited', EXIT_STATUS='0')),
                                 (True, dict(EXIT_CODE='killed', EXIT_STATUS='TERM')),
                                 (True, dict(EXIT_CODE='exited', EXIT_STATUS='1')),
                                 (True, {})):
            with self.subTest(pending=pending, environ=environ):
                self.assertEqual(self.rewake(pending, **environ), (False, []))

    def test_unit_rewakes_after_the_watcher_exits(self):
        unit = (Path(__file__).parents[1] / 'systemd/manet-wifi-uplink.service').read_text()
        self.assertIn('ExecStopPost=/usr/local/bin/manet_wifi_uplink.py rewake\n', unit)


if __name__ == '__main__':
    unittest.main()
