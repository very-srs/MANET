#!/usr/bin/env python3
"""Pin manage radio JSON and command contracts before sharing the parsers."""

from contextlib import ExitStack
import io
import json
import subprocess
import unittest
from unittest.mock import Mock, call, patch

import manet_manage as manage
import manet_radio as radio

# Representative CM4 iw output. Morse exposes a compatibility Wi-Fi channel.
IW = {
    'wlan0': '''Interface wlan0
    ifindex 3
    wdev 0x1
    addr 02:00:00:00:00:10
    type mesh point
    wiphy 0
    channel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz
    txpower 20.00 dBm
''',
    'wlan1': '''Interface wlan1
    ifindex 4
    wdev 0x100000001
    addr 02:00:00:00:00:11
    type mesh point
    wiphy 1
    channel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz
    txpower 23.00 dBm
''',
    'wlan2': '''Interface wlan2
    ifindex 5
    wdev 0x200000001
    addr 02:00:00:00:00:12
    type mesh point
    wiphy 2
    channel 1 (2412 MHz), width: 20 MHz, center1: 2412 MHz
    txpower 24.00 dBm
''',
}
PHY = '''Wiphy phy0
    Band 1:
        Frequencies:
            * 2412 MHz [1] (20.0 dBm)
            * 2437 MHz [6] (30.0 dBm)
Wiphy phy1
    Band 2:
        Frequencies:
            * 5180 MHz [36] (23.0 dBm)
            * 5260 MHz [52] (disabled)
            * 5745 MHz [149] (30.0 dBm)
Wiphy phy2
    Band 1:
        Frequencies:
            * 2412 MHz [1] (24.0 dBm)
'''
CONF_PATHS = (
    '/etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf',
    '/etc/wpa_supplicant/wpa_supplicant_s1g-wlan2.conf',
)
MORSE_ARGS = (
    ['-i', 'wlan2', 'channel', '-j'],
    ['-i', 'wlan2', 'channel', '--json'],
    ['channel', '-i', 'wlan2', '-j'],
    ['-i', 'wlan2', 'channel'],
    ['channel', '-i', 'wlan2'],
)
WIDTHS = (
    (1, 902500, '1', '902.5', '1MHz', '68'),
    (2, 907000, '10', '907', '2MHz', '69'),
    (4, 906000, '8', '906', '4MHz', '70'),
    (8, 908000, '12', '908', '8MHz', '71'),
)


def power_info(channel, frequency, power, cap):
    """Spell out the complete iw result without invoking production helpers."""
    return {
        'channel': channel, 'freq_mhz': frequency, 'txpower_dbm': power,
        'txpower_cap_dbm': cap,
        'txpower_options_dbm': [str(n) for n in range(int(cap), 0, -1)],
    }


def iw_probes(iface):
    """Include the second info query used to discover the power cap."""
    return [(['iw', 'dev', iface, 'info'], 5, IW[iface]),
            (['iw', 'dev', iface, 'info'], 5, IW[iface]),
            (['iw', 'phy'], 5, PHY)]


def morse_probes(reply, absolute=True):
    """List every fallback syntax in its existing order."""
    binaries = ('/usr/local/bin/morse_cli', 'morse_cli')
    if not absolute:
        binaries = binaries[1:]
    return [([binary, *args], 3, reply)
            for binary in binaries for args in MORSE_ARGS]


class ManageRadioTests(unittest.TestCase):
    """Exercise real radio parsers at the manage API boundary."""

    def probe(self, probes, invoke, files=None, reads=(), absolute=True):
        """Replay captures and assert every command, option and file read."""
        replies = iter(item[2] for item in probes)
        files = files or {}

        def run(args, **kwargs):
            """Return the next capture or simulate a failed probe."""
            value = next(replies)
            if isinstance(value, Exception):
                raise value
            if isinstance(value, subprocess.CompletedProcess):
                return value
            return subprocess.CompletedProcess(args, 0, value, '')

        def read(path, *args, **kwargs):
            """Supply only the fixture's explicit configuration files."""
            if path not in files:
                raise FileNotFoundError(path)
            return io.StringIO(files[path])

        with ExitStack() as stack:
            proc = stack.enter_context(patch.object(
                manage.subprocess, 'run', side_effect=run))
            opened = stack.enter_context(patch(
                'builtins.open', side_effect=read))
            stack.enter_context(patch.object(
                radio, 'halow_region', return_value='US'))
            stack.enter_context(patch.object(
                manage.os.path, 'exists', return_value=absolute))
            actual = invoke()
        self.assertEqual(proc.call_args_list, [
            call(args, capture_output=True, text=True, timeout=timeout)
            for args, timeout, _ in probes
        ])
        self.assertEqual(opened.call_args_list, [call(p) for p in reads])
        return actual

    def topology(self):
        """Build the real route response with stable non-radio observations."""
        values = {
            'parse_registry': {'self': {'HOSTNAME': 'cm4',
                                       'IPV4_ADDRESS': '10.42.0.1',
                                       'BATTERY_PERCENTAGE': '80'}},
            'get_my_hostname': 'cm4', 'get_my_ip': '10.42.0.1',
            'load_kv_file': {'acs': 'y'},
            'get_bat0_active_ifaces': ['wlan0', 'wlan1', 'wlan2'],
            'get_local_uptime': '1h07m', 'has_internet': True,
            'halow_channel_options': {'region': 'US'},
            'recovery_status': {'state': 'idle'},
        }
        with ExitStack() as stack:
            for name, value in values.items():
                stack.enter_context(patch.object(
                    manage, name, return_value=value))
            stack.enter_context(patch.object(
                manage.time, 'time', return_value=1720000000))
            stack.enter_context(patch.object(
                manage.STATUS_CACHE, 'get',
                side_effect=lambda key, builder: builder()))
            handler = object.__new__(manage.ManageRoutes)
            handler.path = '/api/topology'
            handler.send_json = Mock()
            handler.manage_do_GET()
            self.assertEqual(handler.send_json.call_count, 1)
            args, kwargs = handler.send_json.call_args
            self.assertEqual(kwargs, {})
            self.assertEqual(len(args), 1)
            return json.loads(json.dumps(args[0]))

    def expected_topology(self, interfaces):
        """Pin the full JSON object, including defaults on each interface."""
        return {
            'nodes': [{
                'id': 'self', 'hostname': 'cm4', 'ip': '10.42.0.1',
                'is_me': True, 'is_gateway': False, 'gateway_iface': '',
                'battery': '80', 'uptime': '1h07m',
                'interfaces': {
                    name: {'active': True, **info, 'tx_mcs': '', 'rx_mcs': ''}
                    for name, info in interfaces.items()
                },
            }],
            'my_hostname': 'cm4', 'my_ip': '10.42.0.1', 'internet': True,
            'halow_plan': {'region': 'US'}, 'acs_enabled': True,
            'recovery': {'state': 'idle'}, 'timestamp': 1720000000,
        }

    def test_mt7916_bands_and_phy_caps(self):
        """Keep live power separate from the PHY's maximum offered power."""
        for iface, expected in (
            ('wlan0', power_info('6', '2437', '20.00', '30')),
            ('wlan1', power_info('36', '5180', '23.00', '30')),
        ):
            with self.subTest(iface=iface):
                actual = self.probe(iw_probes(iface),
                                    lambda: manage.get_iw_info(iface))
                self.assertEqual(actual, expected)

    def test_morse_widths_and_full_topology_json(self):
        """Pin runtime widths in direct reads and the manage route JSON."""
        for width, khz, channel, mhz, formatted, _ in WIDTHS:
            with self.subTest(width=width):
                capture = json.dumps({'channel_frequency': khz,
                                      'channel_op_bw': width}) + '\n'
                halow = dict(power_info(channel, mhz, '24.00', '24'),
                             halow_bw=formatted, halow_source='morse')
                probes = iw_probes('wlan2') + [
                    (['/usr/local/bin/morse_cli', *MORSE_ARGS[0]], 3,
                     capture)]
                self.assertEqual(self.probe(
                    probes, lambda: manage.get_iw_info('wlan2')), halow)
                probes = iw_probes('wlan0') + iw_probes('wlan1') + probes
                self.assertEqual(self.probe(probes, self.topology),
                                 self.expected_topology({
                                     'wlan0': power_info(
                                         '6', '2437', '20.00', '30'),
                                     'wlan1': power_info(
                                         '36', '5180', '23.00', '30'),
                                     'wlan2': halow,
                                 }))

    def test_morse_json_noise_units_and_text_fallback(self):
        """Keep nested JSON, frequency units and stderr text parsing."""
        for frequency in (908, 908000, 908000000):
            for width, formatted in ((8, '8MHz'), (8000, '8MHz'),
                                     (8000000, '8MHz'), ('8mhz', '8MHz')):
                with self.subTest(frequency=frequency, width=width):
                    capture = 'Morse channel:\n' + json.dumps({
                        'result': [{'channel_frequency': frequency,
                                    'channel_op_bw': width}]}) + '\nDone\n'
                    actual = self.probe(iw_probes('wlan2') + [
                        (['/usr/local/bin/morse_cli', *MORSE_ARGS[0]], 3,
                         capture)], lambda: manage.get_iw_info('wlan2'))
                    self.assertEqual(actual, dict(
                        power_info('12', '908', '24.00', '24'),
                        halow_bw=formatted, halow_source='morse'))
        probes = iw_probes('wlan2')
        for index, args in enumerate(MORSE_ARGS[:4]):
            reply = subprocess.CompletedProcess([], 1, '', '')
            if index == 3:
                reply.stderr = ('channel_frequency = 906000\n'
                                'channel_op_bw = 4\nchannel_index = 99\n')
            probes.append((['/usr/local/bin/morse_cli', *args], 3, reply))
        actual = self.probe(probes, lambda: manage.get_iw_info('wlan2'))
        self.assertEqual(actual, dict(
            power_info('8', '906', '24.00', '24'),
            halow_bw='4MHz', halow_source='morse'))

    def test_missing_morse_and_timeouts_keep_config_fields(self):
        """Use operating channel width and retain the configured op_class."""
        for failure in (FileNotFoundError('morse_cli'),
                        subprocess.TimeoutExpired('morse_cli', 3)):
            for width, _, channel, _, _, op_class in WIDTHS:
                with self.subTest(failure=type(failure), width=width):
                    files = {CONF_PATHS[0]: (
                        'network={\n    channel=' + channel + '\n'
                        '    op_class=' + op_class + '\n'
                        '    s1g_prim_chwidth=1\n}\n')}
                    halow = dict(power_info(channel, '2412', '24.00', '24'),
                                 op_class=op_class, halow_bw=f'{width}MHz',
                                 halow_source='config')
                    probes = iw_probes('wlan2') + morse_probes(failure)
                    self.assertEqual(self.probe(
                        probes, lambda: manage.get_iw_info('wlan2'),
                        files, CONF_PATHS[:1]), halow)
                    probes = iw_probes('wlan0') + iw_probes('wlan1') + probes
                    self.assertEqual(self.probe(
                        probes, self.topology, files, CONF_PATHS[:1]),
                        self.expected_topology({
                            'wlan0': power_info('6', '2437', '20.00', '30'),
                            'wlan1': power_info('36', '5180', '23.00', '30'),
                            'wlan2': halow,
                        }))

    def test_missing_absolute_binary_and_second_config_path(self):
        """Skip an absent absolute CLI and try the second supplicant name."""
        probes = iw_probes('wlan2') + morse_probes(
            FileNotFoundError('morse_cli'), absolute=False)
        actual = self.probe(
            probes, lambda: manage.get_iw_info('wlan2'),
            {CONF_PATHS[1]: 'channel=12\nop_class=71\n'},
            CONF_PATHS, absolute=False)
        self.assertEqual(actual, dict(
            power_info('12', '2412', '24.00', '24'),
            op_class='71', halow_bw='8MHz', halow_source='config'))

    def test_shared_owner_config_fallback(self):
        """Keep the configured operating class in the shared helper result."""
        for width, _, channel, _, _, op_class in WIDTHS:
            with self.subTest(width=width):
                actual = self.probe(
                    morse_probes(FileNotFoundError('morse_cli')),
                    radio.get_halow_driver_info,
                    {CONF_PATHS[0]: f'channel={channel}\n'
                     f'op_class={op_class}\n'}, CONF_PATHS[:1])
                self.assertEqual(actual, {
                    'channel': channel, 'halow_bw': f'{width}MHz',
                    'halow_source': 'config', 'op_class': op_class,
                })
        actual = self.probe(
            morse_probes(FileNotFoundError('morse_cli')),
            radio.get_halow_driver_info,
            {CONF_PATHS[0]: 'op_class=71\n'}, CONF_PATHS[:1])
        self.assertEqual(actual, {'op_class': '71', 'halow_source': 'config'})

    def test_missing_tools_and_timeouts_keep_empty_radio_objects(self):
        """Failures leave the full topology available with empty choices."""
        for failure in (FileNotFoundError('iw'),
                        subprocess.TimeoutExpired('iw', 5)):
            with self.subTest(failure=type(failure)):
                probes = []
                for iface in IW:
                    local = [(['iw', 'dev', iface, 'info'], 5, failure)] * 2
                    if iface == 'wlan2':
                        local += morse_probes(failure)
                    reads = CONF_PATHS if iface == 'wlan2' else ()
                    self.assertEqual(self.probe(
                        local, lambda: manage.get_iw_info(iface),
                        reads=reads), {'txpower_options_dbm': []})
                    probes += local
                self.assertEqual(self.probe(
                    probes, self.topology, reads=CONF_PATHS),
                    self.expected_topology({
                        iface: {'txpower_options_dbm': []} for iface in IW
                    }))

    def test_power_fallbacks_and_failed_phy(self):
        """Pin wdev fallback, absent PHY caps, nonzero iw and timeout cases."""
        cases = (
            (IW['wlan1'].replace('    wiphy 1\n', ''), PHY, '30'),
            (IW['wlan1'], 'Wiphy phy1\n', '23'),
            (IW['wlan1'], subprocess.TimeoutExpired('iw phy', 5), ''),
        )
        for capture, phy, cap in cases:
            with self.subTest(cap=cap):
                probes = [(['iw', 'dev', 'wlan1', 'info'], 5, capture)] * 2
                probes.append((['iw', 'phy'], 5, phy))
                expected = {'channel': '36', 'freq_mhz': '5180',
                            'txpower_dbm': '23.00', 'txpower_options_dbm': []}
                if cap:
                    expected = power_info('36', '5180', '23.00', cap)
                self.assertEqual(self.probe(
                    probes, lambda: manage.get_iw_info('wlan1')), expected)
        actual = self.probe([
            (['iw', 'dev', 'wlan1', 'info'], 5, IW['wlan1']),
            (['iw', 'dev', 'wlan1', 'info'], 5,
             subprocess.CompletedProcess([], 1, '', 'No such device')),
        ], lambda: manage.get_iw_info('wlan1'))
        self.assertEqual(actual, {
            'channel': '36', 'freq_mhz': '5180', 'txpower_dbm': '23.00',
            'txpower_options_dbm': [],
        })

    def test_manage_helper_patches_intercept_direct_callers(self):
        """Keep manage's direct helper bindings patchable after extraction."""
        with patch.object(manage, 'get_iface_txpower_cap',
                          return_value='17.5') as cap, \
                patch.object(manage, 'txpower_choices_from_cap',
                             return_value=['17']) as choices, \
                patch.object(manage, 'get_halow_driver_info',
                             return_value={'channel': '99'}) as halow:
            actual = self.probe(
                [(['iw', 'dev', 'wlan2', 'info'], 5, '')],
                lambda: manage.get_iw_info('wlan2'))
        cap.assert_called_once_with('wlan2')
        choices.assert_called_once_with('17.5')
        halow.assert_called_once_with('wlan2')
        self.assertEqual(actual, {'txpower_cap_dbm': '17.5',
                                  'txpower_options_dbm': ['17'],
                                  'channel': '99'})


if __name__ == '__main__':
    unittest.main()
