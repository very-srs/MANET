#!/usr/bin/env python3
"""Characterize interface status results and ordered hardware probes."""

from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import call, patch


TOOLS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    'interfaces_test_status', TOOLS / 'mesh-status.py')
status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)

IP_ADDR = json.loads('''[
  {"ifindex":1,"ifname":"lo","flags":["LOOPBACK","UP","LOWER_UP"],
   "operstate":"UNKNOWN","link_type":"loopback",
   "addr_info":[{"family":"inet","local":"127.0.0.1"}]},
  {"ifindex":2,"ifname":"end0","flags":["BROADCAST","MULTICAST","UP"],
   "operstate":"UP","link_type":"ether","master":"br0","addr_info":[]},
  {"ifindex":3,"ifname":"wlan0","flags":["BROADCAST","MULTICAST","UP"],
   "operstate":"UP","link_type":"ether","master":"bat0",
   "addr_info":[{"family":"inet6","local":"fe80::10"}]},
  {"ifindex":4,"ifname":"wlan1","flags":["BROADCAST","MULTICAST","UP"],
   "operstate":"UP","link_type":"ether","master":"bat0","addr_info":[]},
  {"ifindex":5,"ifname":"wlan2","flags":["BROADCAST","MULTICAST","UP"],
   "operstate":"UP","link_type":"ether","master":"bat0","addr_info":[]},
  {"ifindex":6,"ifname":"bat0","flags":["BROADCAST","MULTICAST","UP"],
   "operstate":"UNKNOWN","link_type":"ether","master":"br0",
   "addr_info":[]},
  {"ifindex":7,"ifname":"br0","flags":["BROADCAST","MULTICAST","UP"],
   "operstate":"UNKNOWN","link_type":"ether","addr_info":[
     {"family":"inet","local":"10.42.0.1"},
     {"family":"inet6","local":"fd42::1"},
     {"family":"inet6","local":"fe80::1"}]}]
''')

WLAN0 = '''\tInterface wlan0
\t\tifindex 3
\t\twdev 0x1
\t\taddr 02:00:00:00:00:10
\t\tssid manet
\t\ttype mesh point
\t\twiphy 0
\t\tchannel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz
\t\ttxpower 20.00 dBm
'''
WLAN1 = '''\tInterface wlan1
\t\tifindex 4
\t\twdev 0x100000001
\t\taddr 02:00:00:00:00:11
\t\tssid manet
\t\ttype mesh point
\t\twiphy 1
\t\tchannel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz
\t\ttxpower 23.00 dBm
'''
WLAN2 = '''\tInterface wlan2
\t\tifindex 5
\t\twdev 0x200000001
\t\taddr 02:00:00:00:00:12
\t\tssid manet-halow
\t\ttype mesh point
\t\twiphy 2
\t\tchannel 1 (2412 MHz), width: 20 MHz, center1: 2412 MHz
\t\ttxpower 15.00 dBm
'''
IW_DEV = 'phy#0\n' + WLAN0 + 'phy#1\n' + WLAN1 + 'phy#2\n' + WLAN2
IW_PHY = '''Wiphy phy0
\tBand 1:
\t\tFrequencies:
\t\t\t* 2412 MHz [1] (20.0 dBm)
\t\t\t* 2437 MHz [6] (20.0 dBm)
Wiphy phy1
\tBand 2:
\t\tFrequencies:
\t\t\t* 5180 MHz [36] (23.0 dBm)
\t\t\t* 5200 MHz [40] (23.0 dBm)
Wiphy phy2
\tBand 1:
\t\tFrequencies:
\t\t\t* 2412 MHz [1] (15.0 dBm)
'''
BATCTL = 'wlan0: active\nwlan1: active\nwlan2: active\n'
SYSTEMCTL = '''  wpa_supplicant@wlan0.service loaded active running WPA
  wpa_supplicant@wlan1.service loaded active running WPA
  wpa_supplicant-s1g-wlan2.service loaded active running WPA S1G
'''
SYSTEMCTL_CMD = [
    'systemctl', 'list-units', '--state=active', '--no-legend',
    'wpa_supplicant*', 'wpa_supplicant-s1g*',
]
MORSE_CMD = ['/usr/local/bin/morse_cli', '-i', 'wlan2', 'channel', '-j']
MORSE = '{"channel_frequency":907000,"channel_op_bw":2}\n'
CONF = 'eud=wired\nhalow_regulatory_domain=US\n'


def row(name, role='other', health='ok', detail='', faults=(), addrs=(),
        state='UP', channel='', freq_mhz='', txpower_dbm='',
        txpower_cap_dbm='', txpower_options_dbm=(), halow_bw='',
        halow_source=''):
    """Spell out every public field, including empty defaults."""
    return {
        'name': name, 'role': role, 'health': health, 'detail': detail,
        'faults': list(faults), 'addrs': list(addrs), 'state': state,
        'channel': channel, 'freq_mhz': freq_mhz,
        'txpower_dbm': txpower_dbm, 'txpower_cap_dbm': txpower_cap_dbm,
        'txpower_options_dbm': list(txpower_options_dbm),
        'halow_bw': halow_bw, 'halow_source': halow_source,
    }


class InterfaceTests(unittest.TestCase):
    def collect(self, probes, files=None, expected_reads=()):
        """Run real parsers against scripted commands and in-memory files."""
        files = {} if files is None else files
        replies = iter(item[2] for item in probes)
        events = []

        def run(args, **kwargs):
            events.append(('run', args))
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            if isinstance(reply, subprocess.CompletedProcess):
                return reply
            return subprocess.CompletedProcess(args, 0, reply, '')

        def read(path, *args, **kwargs):
            events.append(('open', path))
            value = files.get(path, FileNotFoundError(path))
            if isinstance(value, Exception):
                raise value
            return io.StringIO(value)

        with patch.object(status.subprocess, 'run', side_effect=run) as proc:
            with patch('builtins.open', side_effect=read) as opened:
                with patch('manet_radio.os.path.exists', return_value=True):
                    result = status.get_interfaces()
        self.assertEqual(proc.call_args_list, [
            call(args, capture_output=True, text=True, timeout=timeout)
            for args, timeout, _ in probes
        ])
        self.assertEqual(opened.call_args_list,
                         [call(path) for path in expected_reads])
        self.events = events
        return result

    def assert_rows(self, actual, expected):
        self.assertEqual(actual, expected)
        self.assertEqual(json.dumps(actual), json.dumps(expected))

    def cm4_probes(self):
        probes = [(['ip', '-j', 'addr'], 5, json.dumps(IP_ADDR)),
                  (['iw', 'dev'], 5, IW_DEV),
                  (['iw', 'phy'], 5, IW_PHY)]
        for name, info, driver in (
            ('wlan0', WLAN0, 'mt7915e'),
            ('wlan1', WLAN1, 'mt7915e'),
            ('wlan2', WLAN2, 'morse_usb'),
        ):
            probes.extend([
                (['ethtool', '-i', name], 3,
                 f'driver: {driver}\nversion: 6.18.0\nbus-info: pci\n'),
                (['iw', 'dev', name, 'info'], 5, info),
                (['iw', 'phy'], 5, IW_PHY),
            ])
        return probes + [
            (MORSE_CMD, 3, MORSE),
            (['batctl', 'if'], 5, BATCTL),
            (SYSTEMCTL_CMD, 5, SYSTEMCTL),
            (['ip', 'route', 'show', 'dev', 'end0'], 3,
             '10.42.0.0/16 proto kernel scope link src 10.42.0.1\n'),
        ]

    def cm4_expected(self):
        return [
            row('bat0', 'bat', 'info', 'BATMAN-ADV mesh bridge'),
            row('wlan0', 'mesh', detail='2.4 GHz: ch6 [manet]',
                channel='6', freq_mhz='2437', txpower_dbm='20.00',
                txpower_cap_dbm='20',
                txpower_options_dbm=[str(n) for n in range(20, 0, -1)]),
            row('wlan1', 'mesh', detail='5 GHz: ch36 [manet]',
                channel='36', freq_mhz='5180', txpower_dbm='23.00',
                txpower_cap_dbm='23',
                txpower_options_dbm=[str(n) for n in range(23, 0, -1)]),
            row('wlan2', 'mesh', detail='HaLow: ch10 [manet-halow]',
                channel='10', freq_mhz='907', txpower_dbm='15.00',
                txpower_cap_dbm='15',
                txpower_options_dbm=[str(n) for n in range(15, 0, -1)],
                halow_bw='2MHz', halow_source='morse'),
            row('end0', 'eud-bridge', detail='Ethernet: EUD connection'),
            row('br0', 'bridge', 'info', 'L2 bridge (mesh + EUD)',
                addrs=['10.42.0.1', 'fd42::1']),
        ]

    def test_cm4_layout_and_unknown_bridge_states(self):
        actual = self.collect(
            self.cm4_probes(),
            {'/etc/mesh.conf': CONF, '/var/lib/no_mesh_if': ''},
            ['/etc/mesh.conf', '/etc/mesh.conf', '/var/lib/no_mesh_if'])
        self.assert_rows(actual, self.cm4_expected())
        self.assertEqual(self.events[-7:], [
            ('run', MORSE_CMD), ('open', '/etc/mesh.conf'),
            ('run', ['batctl', 'if']), ('run', SYSTEMCTL_CMD),
            ('open', '/etc/mesh.conf'), ('open', '/var/lib/no_mesh_if'),
            ('run', ['ip', 'route', 'show', 'dev', 'end0']),
        ])

    def test_dual_band_phy_prefers_24_in_either_band_order(self):
        for bands in ('\tBand 1:\n\tBand 2:\n',
                      '\tBand 2:\n\tBand 1:\n'):
            with self.subTest(bands=bands):
                actual = self.collect([
                    (['ip', '-j', 'addr'], 5, json.dumps([IP_ADDR[3]])),
                    (['iw', 'dev'], 5, WLAN1),
                    (['iw', 'phy'], 5, 'Wiphy phy1\n' + bands),
                    (['ethtool', '-i', 'wlan1'], 3, 'driver: mt7915e\n'),
                    (['iw', 'dev', 'wlan1', 'info'], 5, WLAN1),
                    (['iw', 'phy'], 5, 'Wiphy phy1\n' + bands),
                    (['batctl', 'if'], 5, 'wlan1: active\n'),
                    (SYSTEMCTL_CMD, 5, SYSTEMCTL),
                ], expected_reads=['/etc/mesh.conf', '/var/lib/no_mesh_if'])
                self.assert_rows(actual, [row(
                    'wlan1', 'mesh', detail='2.4 GHz: ch36 [manet]',
                    channel='36', freq_mhz='5180', txpower_dbm='23.00',
                    txpower_cap_dbm='23',
                    txpower_options_dbm=[str(n) for n in range(23, 0, -1)])])

    def test_no_mesh_file_exempts_ap_from_mesh_health_checks(self):
        for iw_type in ('AP', 'managed'):
            with self.subTest(iw_type=iw_type):
                info = WLAN0.replace('mesh point', iw_type)
                info = info.replace('ssid manet', 'ssid EUD Access')
                raw = dict(IP_ADDR[2], master='br0')
                actual = self.collect([
                    (['ip', '-j', 'addr'], 5, json.dumps([raw])),
                    (['iw', 'dev'], 5, info),
                    (['iw', 'phy'], 5, IW_PHY),
                    (['ethtool', '-i', 'wlan0'], 3, 'driver: mt7915e\n'),
                    (['iw', 'dev', 'wlan0', 'info'], 5, info),
                    (['iw', 'phy'], 5, IW_PHY),
                    (['batctl', 'if'], 5, ''),
                    (SYSTEMCTL_CMD, 5, ''),
                ], {'/etc/mesh.conf': 'eud=wireless\n',
                    '/var/lib/no_mesh_if': '\n  wlan0  \n\nwlan0\n'},
                    ['/etc/mesh.conf', '/var/lib/no_mesh_if'])
                self.assert_rows(actual, [row(
                    'wlan0', 'ap', detail='EUD AP: EUD Access',
                    channel='6', freq_mhz='2437', txpower_dbm='20.00',
                    txpower_cap_dbm='20',
                    txpower_options_dbm=[str(n) for n in range(20, 0, -1)])])

    def test_unenslaved_down_mesh_sorts_first_within_role(self):
        for state, health, first_fault in (
            ('DOWN', 'fault', 'wlan1 is DOWN'),
            ('UP', 'warn', 'Not active in bat0'),
        ):
            with self.subTest(state=state):
                raw = deepcopy(IP_ADDR)
                raw[3].pop('master')
                raw[3]['operstate'] = state
                probes = self.cm4_probes()
                probes[0] = (['ip', '-j', 'addr'], 5, json.dumps(raw))
                probes[-3] = (['batctl', 'if'], 5,
                              'wlan0: active\nwlan2: active\n')
                probes[-2] = (SYSTEMCTL_CMD, 5, SYSTEMCTL.replace(
                    '  wpa_supplicant@wlan1.service loaded active running '
                    'WPA\n', ''))
                expected = self.cm4_expected()
                faulted = expected.pop(2)
                faulted.update(health=health, state=state, faults=[
                    first_fault, 'wpa_supplicant not running for wlan1'])
                expected.insert(1, faulted)
                actual = self.collect(
                    probes, {'/etc/mesh.conf': CONF},
                    ['/etc/mesh.conf', '/etc/mesh.conf',
                     '/var/lib/no_mesh_if'])
                self.assert_rows(actual, expected)

    def test_ip_probe_errors_and_invalid_json_return_immediately(self):
        for reply in (
            OSError('ip unavailable'),
            subprocess.TimeoutExpired(['ip', '-j', 'addr'], 5),
            subprocess.CalledProcessError(1, ['ip', '-j', 'addr']),
            subprocess.CompletedProcess(['ip', '-j', 'addr'], 1, '', 'fail'),
            'not json', '',
        ):
            with self.subTest(reply=repr(reply)):
                actual = self.collect([(['ip', '-j', 'addr'], 5, reply)])
                self.assert_rows(actual, [])

    def test_all_optional_probes_fail_or_time_out(self):
        raw = [dict(item, master='br0') for item in IP_ADDR]
        for failure in (OSError('unavailable'),
                        subprocess.TimeoutExpired('probe', 5)):
            with self.subTest(failure=type(failure).__name__):
                actual = self.collect([
                    (['ip', '-j', 'addr'], 5, json.dumps(raw)),
                    (['iw', 'dev'], 5, failure),
                    (['iw', 'phy'], 5, failure),
                    (['batctl', 'if'], 5, failure),
                    (SYSTEMCTL_CMD, 5, failure),
                    (['ip', 'route', 'show', 'dev', 'end0'], 3, failure),
                ], expected_reads=['/etc/mesh.conf', '/var/lib/no_mesh_if'])
                self.assert_rows(actual, [
                    row('bat0', 'bat', 'warn', 'BATMAN-ADV mesh bridge',
                        faults=['No interfaces enslaved to bat0']),
                    *[row(name, 'mesh', 'warn', 'Mesh radio', faults=[
                        'Not active in bat0',
                        f'wpa_supplicant not running for {name}'])
                      for name in ('wlan0', 'wlan1', 'wlan2')],
                    row('end0', 'eud-bridge',
                        detail='Ethernet: EUD connection'),
                    row('br0', 'bridge', 'info', 'L2 bridge (mesh + EUD)',
                        addrs=['10.42.0.1', 'fd42::1']),
                ])

    def test_driver_and_power_probe_failures_keep_radio_rows(self):
        for failure in (OSError('unavailable'),
                        subprocess.TimeoutExpired('probe', 3)):
            with self.subTest(failure=type(failure).__name__):
                actual = self.collect([
                    (['ip', '-j', 'addr'], 5, json.dumps([IP_ADDR[2]])),
                    (['iw', 'dev'], 5, WLAN0),
                    (['iw', 'phy'], 5, IW_PHY),
                    (['ethtool', '-i', 'wlan0'], 3, failure),
                    (['iw', 'dev', 'wlan0', 'info'], 5, failure),
                    (['batctl', 'if'], 5, BATCTL),
                    (SYSTEMCTL_CMD, 5, SYSTEMCTL),
                ], expected_reads=['/etc/mesh.conf', '/var/lib/no_mesh_if'])
                self.assert_rows(actual, [row(
                    'wlan0', 'mesh', detail='2.4 GHz: ch6 [manet]',
                    channel='6', freq_mhz='2437', txpower_dbm='20.00')])

    def test_morse_probe_failures_fall_back_to_supplicant_config(self):
        config_path = '/etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf'
        for failure in (OSError('unavailable'),
                        subprocess.TimeoutExpired('morse_cli', 3)):
            with self.subTest(failure=type(failure).__name__):
                probes = self.cm4_probes()
                morse_probes = []
                for binary in ('/usr/local/bin/morse_cli', 'morse_cli'):
                    for args in (
                        ['-i', 'wlan2', 'channel', '-j'],
                        ['-i', 'wlan2', 'channel', '--json'],
                        ['channel', '-i', 'wlan2', '-j'],
                        ['-i', 'wlan2', 'channel'],
                        ['channel', '-i', 'wlan2'],
                    ):
                        morse_probes.append(([binary] + args, 3, failure))
                probes[12:13] = morse_probes
                expected = self.cm4_expected()
                expected[3].update(halow_source='config', freq_mhz='2412')
                actual = self.collect(
                    probes, {'/etc/mesh.conf': CONF,
                             config_path: 'network={\n channel=10\n}\n'},
                    [config_path, '/etc/mesh.conf', '/etc/mesh.conf',
                     '/var/lib/no_mesh_if'])
                self.assert_rows(actual, expected)

    def test_phy_failure_leaves_band_unknown_and_keeps_later_probes(self):
        for failure in (OSError('unavailable'),
                        subprocess.TimeoutExpired('iw phy', 5)):
            with self.subTest(failure=type(failure).__name__):
                actual = self.collect([
                    (['ip', '-j', 'addr'], 5, json.dumps([IP_ADDR[3]])),
                    (['iw', 'dev'], 5, WLAN1),
                    (['iw', 'phy'], 5, failure),
                    (['ethtool', '-i', 'wlan1'], 3, 'driver: mt7915e\n'),
                    (['iw', 'dev', 'wlan1', 'info'], 5, WLAN1),
                    (['iw', 'phy'], 5, failure),
                    (['batctl', 'if'], 5, BATCTL),
                    (SYSTEMCTL_CMD, 5, SYSTEMCTL),
                ], expected_reads=['/etc/mesh.conf', '/var/lib/no_mesh_if'])
                self.assert_rows(actual, [row(
                    'wlan1', 'mesh', detail='Mesh radio [manet]',
                    channel='36', freq_mhz='5180', txpower_dbm='23.00')])

    def test_ip_master_without_batctl_membership_remains_other_wireless(self):
        actual = self.collect([
            (['ip', '-j', 'addr'], 5, json.dumps([IP_ADDR[2]])),
            (['iw', 'dev'], 5, ''), (['iw', 'phy'], 5, ''),
            (['batctl', 'if'], 5, ''), (SYSTEMCTL_CMD, 5, ''),
        ], expected_reads=['/etc/mesh.conf', '/var/lib/no_mesh_if'])
        self.assert_rows(actual, [row(
            'wlan0', health='warn', detail='Wireless',
            faults=['Not in bat0 and not an AP: check wpa_supplicant'])])

    def test_health_branches_and_stable_role_order(self):
        raw = [
            {'ifname': 'eth0', 'operstate': 'UP'},
            {'ifname': 'wlan0', 'operstate': 'UP'},
            {'ifname': 'wlan1', 'operstate': 'UP'},
            {'ifname': 'wlan2', 'operstate': 'UP'},
            {'ifname': 'wlan3', 'operstate': 'DOWN'},
            {'ifname': 'bat0', 'operstate': 'DOWN'},
            {'ifname': 'br0', 'operstate': 'UNKNOWN', 'flags': []},
            {'ifname': 'ens1', 'operstate': 'DOWN'},
            {'ifname': 'halow0', 'operstate': 'UP'},
            {'ifname': 'mlan0', 'operstate': 'DOWN'},
            {'ifname': 'tun0'},
        ]
        iw = ('\tInterface wlan2\n\t\ttype AP\n'
              '\tInterface wlan3\n\t\ttype AP\n\t\tssid Field AP\n')
        actual = self.collect([
            (['ip', '-j', 'addr'], 5, json.dumps(raw)),
            (['iw', 'dev'], 5, iw), (['iw', 'phy'], 5, ''),
            (['ethtool', '-i', 'wlan2'], 3, ''),
            (['iw', 'dev', 'wlan2', 'info'], 5, ''),
            (['ethtool', '-i', 'wlan3'], 3, ''),
            (['iw', 'dev', 'wlan3', 'info'], 5, ''),
            (['batctl', 'if'], 5, 'wlan0: active\nwlan1: inactive\n'),
            (SYSTEMCTL_CMD, 5, SYSTEMCTL),
            (['ip', 'route', 'show', 'dev', 'eth0'], 3,
             'default via 192.168.1.1 proto dhcp metric 100\n'),
            (['ip', 'route', 'show', 'dev', 'ens1'], 3, ''),
        ], expected_reads=['/etc/mesh.conf', '/var/lib/no_mesh_if'])
        self.assert_rows(actual, [
            row('bat0', 'bat', 'fault', 'BATMAN-ADV mesh bridge',
                faults=['bat0 is DOWN'], state='DOWN'),
            row('wlan1', 'mesh', 'fault', 'Mesh radio',
                faults=['Inactive in bat0 (wpa_supplicant issue?)']),
            row('wlan0', 'mesh', detail='Mesh radio'),
            row('wlan3', 'ap', 'fault', 'EUD AP: Field AP',
                faults=['wlan3 AP is DOWN'], state='DOWN'),
            row('wlan2', 'ap', 'warn', 'EUD AP: ',
                faults=['AP has no SSID (hostapd issue?)']),
            row('eth0', 'gateway', detail='Ethernet: Internet gateway'),
            row('br0', 'bridge', 'warn', 'L2 bridge (mesh + EUD)',
                faults=['No IP assigned'], state='DOWN'),
            row('mlan0', health='fault', detail='Wireless', state='DOWN',
                faults=['mlan0 is DOWN: not participating in mesh']),
            row('halow0', health='warn', detail='Wireless',
                faults=['Not in bat0 and not an AP: check wpa_supplicant']),
            row('tun0', state='UNKNOWN'),
            row('ens1', health='info', detail='Ethernet (no cable)',
                state='DOWN'),
        ])


class VersionTests(unittest.TestCase):
    def test_reads_release_from_first_line(self):
        with patch('builtins.open', return_value=io.StringIO('0.571\n10/2026\n')):
            self.assertEqual(status.get_manet_version(), '0.571')

    def test_missing_file_reads_empty(self):
        with patch('builtins.open', side_effect=FileNotFoundError):
            self.assertEqual(status.get_manet_version(), '')


if __name__ == '__main__':
    unittest.main()
