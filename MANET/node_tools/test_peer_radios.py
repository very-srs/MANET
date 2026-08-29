#!/usr/bin/env python3
"""Topology chips for peers must show radio values even when INTERFACES_JSON is []."""

import unittest

from manet_peer_radios import freq_mhz_to_channel, peer_radio_interfaces


class FreqToChannelTests(unittest.TestCase):
    def test_2_4ghz(self):
        self.assertEqual(freq_mhz_to_channel('2412'), '1')
        self.assertEqual(freq_mhz_to_channel(2437), '6')

    def test_5ghz(self):
        self.assertEqual(freq_mhz_to_channel('5180'), '36')
        self.assertEqual(freq_mhz_to_channel(5500), '100')

    def test_empty(self):
        self.assertEqual(freq_mhz_to_channel(''), '')
        self.assertEqual(freq_mhz_to_channel(None), '')


class PeerRadioInterfacesTests(unittest.TestCase):
    def test_empty_json_uses_registry_mcs_and_data_channels(self):
        nd = {
            'INTERFACES_JSON': '[]',
            'DATA_CHANNEL_2_4': '2412',
            'DATA_CHANNEL_5_0': '5180',
            'WIFI_24_TX_MCS': 'MCS7 N2',
            'WIFI_24_RX_MCS': 'MCS5 N2',
            'WIFI_5_TX_MCS': '',
            'WIFI_5_RX_MCS': 'MCS8 N2',
            'HALOW_TX_MCS': 'MCS7 S1G',
            'HALOW_RX_MCS': 'MCS7 S1G',
        }
        ifaces = peer_radio_interfaces(nd)
        self.assertEqual(set(ifaces), {'wlan0', 'wlan1', 'wlan2'})

        self.assertTrue(ifaces['wlan0']['active'])
        self.assertEqual(ifaces['wlan0']['channel'], '1')
        self.assertEqual(ifaces['wlan0']['freq_mhz'], '2412')
        self.assertEqual(ifaces['wlan0']['tx_mcs'], 'MCS7 N2')
        self.assertEqual(ifaces['wlan0']['rx_mcs'], 'MCS5 N2')

        # 5 GHz is often the AP, not a mesh radio — do not guess it ON.
        self.assertFalse(ifaces['wlan1']['active'])
        self.assertEqual(ifaces['wlan1']['channel'], '36')
        self.assertEqual(ifaces['wlan1']['rx_mcs'], 'MCS8 N2')

        self.assertTrue(ifaces['wlan2']['active'])
        self.assertEqual(ifaces['wlan2']['tx_mcs'], 'MCS7 S1G')

    def test_published_json_wins_for_role_state_and_channel(self):
        nd = {
            'INTERFACES_JSON': (
                '[{"name":"wlan0","role":"mesh","state":"UP","channel":"6",'
                '"freq_mhz":"2437","txpower_dbm":"20","tx_mcs":"from-json",'
                '"rx_mcs":"from-json"},'
                '{"name":"wlan1","role":"ap","state":"UP","channel":"36",'
                '"freq_mhz":"5180","tx_mcs":"","rx_mcs":"MCS8 N2"},'
                '{"name":"wlan2","role":"mesh","state":"UP","channel":"1",'
                '"freq_mhz":"863.5","halow_bw":"1","tx_mcs":"MCS7 S1G",'
                '"rx_mcs":"MCS7 S1G"}]'
            ),
            'DATA_CHANNEL_2_4': '2412',
            'WIFI_24_TX_MCS': 'ignored',
            'WIFI_5_RX_MCS': 'ignored',
            'HALOW_TX_MCS': 'ignored',
        }
        ifaces = peer_radio_interfaces(nd)
        self.assertTrue(ifaces['wlan0']['active'])
        self.assertEqual(ifaces['wlan0']['channel'], '6')
        self.assertEqual(ifaces['wlan0']['tx_mcs'], 'from-json')
        self.assertFalse(ifaces['wlan1']['active'])
        self.assertEqual(ifaces['wlan1']['channel'], '36')
        self.assertTrue(ifaces['wlan2']['active'])
        self.assertEqual(ifaces['wlan2']['channel'], '1')
        self.assertEqual(ifaces['wlan2']['freq_mhz'], '863.5')
        self.assertEqual(ifaces['wlan2']['halow_bw'], '1')


class EncoderInterfacesRoundtripTests(unittest.TestCase):
    def test_channel_fields_survive_encode_decode(self):
        import json
        import os
        import subprocess
        import sys

        here = os.path.dirname(os.path.abspath(__file__))
        payload = subprocess.check_output(
            [sys.executable, os.path.join(here, 'encoder.py'), 'telemetry',
             '--timestamp', '1',
             '--interfaces-json',
             '[{"name":"wlan2","role":"mesh","state":"UP","channel":"1",'
             '"freq_mhz":"863.5","txpower_dbm":"15","halow_bw":"1",'
             '"tx_mcs":"MCS9 N1 SGI","rx_mcs":"MCS9 N1 SGI"}]'],
            cwd=here,
        ).decode()
        decoded = subprocess.check_output(
            [sys.executable, os.path.join(here, 'decoder.py'), 'telemetry', payload],
            cwd=here,
        ).decode()
        ifaces = None
        for line in decoded.splitlines():
            if line.startswith("INTERFACES_JSON="):
                raw = line.split('=', 1)[1]
                if raw.startswith("'") and raw.endswith("'"):
                    raw = raw[1:-1].replace("'\\''", "'")
                ifaces = json.loads(raw)
        self.assertIsNotNone(ifaces)
        wlan2 = ifaces[0]
        self.assertEqual(wlan2['name'], 'wlan2')
        self.assertEqual(wlan2['channel'], '1')
        self.assertEqual(wlan2['freq_mhz'], '863.5')
        self.assertEqual(wlan2['txpower_dbm'], '15')
        self.assertEqual(wlan2['halow_bw'], '1')


class TelemetryInterfacesTests(unittest.TestCase):
    def test_keeps_radio_ifaces_and_drops_the_rest(self):
        from manet_peer_radios import interfaces_for_telemetry
        dumped = interfaces_for_telemetry([
            {'name': 'bat0', 'role': 'bat', 'state': 'UP', 'addrs': ['10.0.0.1']},
            {
                'name': 'wlan0', 'role': 'mesh', 'state': 'UP',
                'addrs': ['fe80::1'], 'channel': '1', 'freq_mhz': '2412',
                'txpower_dbm': '20', 'tx_mcs': 'MCS7', 'rx_mcs': 'MCS5',
            },
            {
                'name': 'wlan1', 'role': 'ap', 'state': 'UP',
                'channel': '36', 'freq_mhz': '5180',
            },
            {'name': 'end0', 'role': 'other', 'state': 'UP'},
        ])
        names = [i['name'] for i in dumped]
        self.assertEqual(names, ['wlan0', 'wlan1'])
        self.assertEqual(dumped[0]['role'], 'mesh')
        self.assertEqual(dumped[0]['ipv4'], ['fe80::1'])
        self.assertEqual(dumped[0]['channel'], '1')
        self.assertEqual(dumped[0]['freq_mhz'], '2412')
        self.assertEqual(dumped[0]['txpower_dbm'], '20')
        self.assertEqual(dumped[1]['role'], 'ap')


if __name__ == '__main__':
    unittest.main()
