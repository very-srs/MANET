"""Exercise the Wi-Fi menu, literal scan names and local API transport."""

import json
import re
import subprocess
import unittest
from unittest.mock import patch

import manet_manage as manage
import manet_wifi_uplink as wifi
from test_web_rendering import JavaScript, Markup


def network(**values):
    """Return one selectable access point with explicit scan identity."""
    return dict(dict(ssid='House', bssid='02:00:00:00:00:01', freq=5180,
                     channel=36, signal=-45, security='wpa2', selectable=True,
                     dfs=False, reason=''), **values)


class ScanNameTests(unittest.TestCase):
    def scan(self, name, extra=''):
        """Parse an iw capture, including the no-space hidden-name variant."""
        return wifi.scan_records('BSS 02:00:00:00:00:01(on wlan1)\n'
                                 '\tfreq: 5180\n\tsignal: -45.0 dBm\n'
                                 '\tSSID:' + name + '\n' + extra)[0]

    def test_hotspot_filter_uses_literal_current_prefix_and_lowercase_suffix(self):
        for prefix in ('EUD', 'EUD.+', 'EUD-5cc2', 'Neighbour'):
            cases = [(prefix + '-5cc2', True), (prefix + '-aa77', True),
                     (prefix + '-5CC2', False), (prefix + '-5cc', False),
                     (prefix + '-5cc22', False), (prefix + '-zzzz', False),
                     (prefix, False), (prefix + '-5cc2-more', False),
                     ('X' + prefix + '-5cc2', False), (prefix + '2-5cc2', False),
                     (' ' + prefix + '-5cc2', False), (prefix + '-5cc2 ', False)]
            for name, hidden in cases:
                with self.subTest(prefix=prefix, name=name), patch.object(
                        wifi, 'load_kv_file', return_value={'lan_ap_ssid': prefix}):
                    rows = wifi.scan_records('BSS 02:00:00:00:00:01\n\tfreq: 5180\n'
                                             '\tsignal: -40\n\tSSID: ' + name + '\n')
                    self.assertEqual([r['ssid'] for r in rows], [] if hidden else [name])
            with patch.object(wifi, 'load_kv_file', return_value={'lan_ap_ssid': prefix}):
                rows = wifi.scan_records('BSS 02:00:00:00:00:01\n\tfreq: 5180\n'
                                         '\tsignal: -40\n\tSSID: ' + prefix + r'-\x35cc2' + '\n')
                self.assertEqual(rows, [])


    def test_literal_utf8_quotes_html_backslashes_and_edge_spaces(self):
        for wire, expected in [(' "<b> &\'"', '"<b> &\'"'),
                               (r' \xf0\x9f\x93\xb6', '📶'),
                               (r' \x20name\x20', ' name '),
                               (r' literal\x5cx41', r'literal\x41')]:
            with self.subTest(wire=wire):
                row = self.scan(wire)
                self.assertEqual(row['ssid'], expected)
                self.assertEqual(row['ssid_hex'], expected.encode().hex())
                self.assertTrue(row['selectable'])

    def test_hidden_non_utf8_and_controls_remain_visible_with_reasons(self):
        for wire, reason in [('', 'Hidden'), (' ', 'Hidden'),
                             (r' \x00\x00', 'Hidden'),
                             (r' bad\xff', 'encoding'), (r' bad\x0a', 'control')]:
            with self.subTest(wire=wire):
                row = self.scan(wire)
                self.assertFalse(row['selectable'])
                self.assertIn(reason, row['reason'])
                if 'bad' in wire:
                    self.assertEqual(row['ssid'], wire[1:])

    def test_enterprise_has_plain_reason(self):
        row = self.scan(' Office', '\tRSN:\n\t Authentication suites: IEEE 802.1X\n')
        self.assertFalse(row['selectable'])
        self.assertIn('sign-in', row['reason'])

    def test_api_rejects_removed_or_missing_radio_without_running_helpers(self):
        with patch.object(manage.subprocess, 'run') as run:
            for action in ('scan', 'scan-plan', 'set'):
                for iface in ('usb', 'wlan5', None):
                    with self.subTest(action=action, iface=iface), self.assertRaises(ValueError):
                        manage.builtin_wifi_request(action, {'iface': iface, 'enabled': False})
            run.assert_not_called()
        with patch.object(manage, 'builtin_wifi_request', return_value={'saved_networks': []}):
            self.assertEqual(manage.wifi_uplink_status(),
                             {'builtin': {'saved_networks': []}, 'saved_networks': []})

    def test_scan_api_only_passes_explicit_interruption_acknowledgement(self):
        response = subprocess.CompletedProcess([], 0, '{"networks":[]}', '')
        with patch.object(manage.subprocess, 'run', return_value=response) as run:
            manage.builtin_wifi_request('scan-plan', {'iface': 'wlan1'})
            self.assertEqual(run.call_args.args[0][-2:],
                             ['scan-plan', 'wlan1'])
            for value in (False, 'true', None):
                manage.builtin_wifi_request('scan', {
                    'iface': 'wlan1', 'allow_ap_stop': value})
                self.assertNotIn('--allow-ap-stop', run.call_args.args[0])
            manage.builtin_wifi_request('scan', {
                'iface': 'wlan1', 'allow_ap_stop': True})
            self.assertIn('--allow-ap-stop', run.call_args.args[0])


class PickerTests(unittest.TestCase):
    def setUp(self):
        self.js = JavaScript()
        self.addCleanup(self.js.close)
        self.js.evaluate('''
const elements = {};
function element(id) { return elements[id] || (elements[id] = {
  value: '', hidden: false, disabled: false, innerHTML: '', textContent: '', children: [],
  dataset: {}, replaceChildren(...children) { this.children = children; this.innerHTML = ''; },
  append(...children) { this.children.push(...children); }, add(option) { this.children.push(option); }, scrollIntoView() {}, focus() {}
}); }
function Option(text, value) { this.text = text; this.value = value; }
const document = {getElementById: element, querySelectorAll: () => [],
  createElement: () => ({textContent: '', className: ''})};
const calls = [];
let reply = {ok: true, networks: [], iface: 'wlan1'}, fail = false;
function U(path) { return '/manage' + path; }
async function manageFetch(url, options) {
  calls.push({url, options});
  if (url.endsWith('/scan-plan')) {
    return {ok: true, json: async () => ({ok: true, interrupts_ap: false})};
  }
  if (fail) throw new Error('scan failed');
  return {ok: true, json: async () => reply};
}
element('uplink-wifi-radio').value = 'wlan1';
''')
        esc = re.search(r'^function esc\(.*?^}', manage.JS, re.M | re.S)
        # esc is supplied by the common recovery JavaScript.
        from manet_recovery_status import RECOVERY_JS
        self.js.evaluate(RECOVERY_JS if esc is None else esc[0])
        source = manage.JS.split('// Wi-Fi uplink picker.', 1)[1].split('// Measurements tab', 1)[0]
        self.js.evaluate('// Wi-Fi uplink picker.' + source)

    def value(self, expression):
        """Read a JS value without losing booleans or nested objects."""
        return json.loads(self.js.evaluate('JSON.stringify(' + expression + ')'))

    def prepare(self, rows=None):
        """Install a fresh list independently of asynchronous scan transport."""
        self.js.evaluate('uplinkNetworks = ' + json.dumps(rows or [network()]) + ';'
                         "uplinkScanIface = 'wlan1'; uplinkScanDevice = 'wlan1'; uplinkScannedAt = Date.now();")

    def test_radio_selector_handles_one_two_and_no_builtin_radios(self):
        for choices in ([{'iface': 'wlan1', 'band': '5'}],
                        [{'iface': 'wlan0', 'band': '2.4'}, {'iface': 'wlan1', 'band': '5'}], []):
            self.js.evaluate('uplinkInitialized=false; reply=' + json.dumps({
                'builtin': {'enabled': False, 'choices': choices}}) + '; loadWifiUplink();')
            self.assertEqual(self.value("element('uplink-wifi-radio').children.map(o => o.value)"),
                             [choice['iface'] for choice in choices])
            self.assertEqual(self.value("element('uplink-wifi-radio').value"),
                             choices[0]['iface'] if choices else '')
            self.assertEqual(self.value("element('uplink-wifi-radio').disabled"), len(choices) < 2)
            self.assertEqual(self.value("element('btn-uplink-wifi-scan').disabled"), not choices)
        self.assertIn('No built-in radio', self.value("element('uplink-wifi-status').textContent"))

    def test_grouping_keeps_strongest_bssid_but_preserves_operator_choices(self):
        rows = [network(signal=-80), network(bssid='02:00:00:00:00:02', signal=-30),
                network(freq=2412), network(security='open'), network(dfs=True),
                network(selectable=False, reason='Channel unavailable'),
                network(ssid='', bssid='02:00:00:00:00:03'),
                network(ssid='', bssid='02:00:00:00:00:04')]
        result = self.value('groupUplinkNetworks(' + json.dumps(rows) + ')')
        self.assertEqual(len(result), 7)
        self.assertEqual(result[0]['bssid'], '02:00:00:00:00:02')
        self.assertEqual(result[0]['signal'], -30)
        self.assertEqual([r['signal'] for r in result], sorted([r['signal'] for r in result], reverse=True))

    def test_unknown_server_reason_is_literal_and_status_has_one_period(self):
        reason = '<img src=x onerror=bad> restore failed'
        self.assertIn(reason, self.value('uplinkError(new Error(' +
                                        json.dumps(reason) + '))'))
        self.js.evaluate('uplinkInitialized = true; reply = ' + json.dumps({
            'builtin': {'enabled': True, 'error': 'failed'}}) + '; loadWifiUplink();')
        self.assertNotIn('..', self.value(
            "element('uplink-wifi-status').textContent"))

    def test_reconnect_recovers_scan_age_warning_and_preserves_input(self):
        scan = dict(iface='wlan1', at=42, age_seconds=200,
                    networks=[network()], warning='Mesh restore pending')
        status = {'builtin': {'enabled': False, 'scan': scan}}
        self.js.evaluate('uplinkInitialized = true; reply = ' +
                         json.dumps(status) + '; loadWifiUplink();')
        self.assertEqual(len(self.value('uplinkNetworks')), 1)
        self.assertGreaterEqual(self.value('Date.now()-uplinkScannedAt'),
                                200000)
        self.assertIn('pending', self.value(
            "element('uplink-wifi-warning').textContent"))
        self.assertTrue(self.value("element('uplink-wifi-intro').hidden"))
        self.js.evaluate("selectUplinkNetwork(0); "
                         "element('uplink-wifi-password').value = 'keep'; "
                         'loadWifiUplink();')
        self.assertEqual(self.value("element('uplink-wifi-password').value"),
                         'keep')
        self.js.evaluate('uplinkScannedAt -= 101000; updateUplinkControls();')
        self.assertTrue(self.value("element('btn-uplink-wifi-apply').disabled"))

    def test_blocked_profile_reason_is_visible_without_a_network_row(self):
        self.prepare()
        reason = "This network is one of this mesh's own hotspots and cannot be used as an uplink"
        blocked = dict(enabled=False, iface='wlan1', ssid='EUD-5cc2',
                       state='blocked', error=reason, connected=False)
        self.js.evaluate('reply=' + json.dumps({'builtin': blocked})
                         + '; loadWifiUplink();')
        self.assertIn(reason, self.value("element('uplink-wifi-status').textContent"))
        self.assertNotIn('..', self.value("element('uplink-wifi-status').textContent"))
        self.assertTrue(self.value("element('uplink-wifi-current').hidden"))
        self.assertEqual(self.value('uplinkError(' + json.dumps(reason) + ')'), reason)

    def test_status_completes_a_scan_whose_http_response_is_still_waiting(self):
        status = {'builtin': {'enabled': False, 'scan': {
            'iface': 'wlan1', 'at': 55, 'age_seconds': 20,
            'networks': [network()]}}}
        self.js.evaluate('uplinkInitialized=true; reply=' +
                         json.dumps(status) + ''';
manageFetch = async (url, options) => {
  calls.push({url, options});
  if (url.endsWith('/scan-plan')) return {
    ok:true, json:async()=>({ok:true,interrupts_ap:false})};
  if (url.endsWith('/scan')) return new Promise(()=>{});
  return {ok:true,json:async()=>reply};
};
scanWifiUplink();
''')
        self.assertTrue(self.value('uplinkBusy'))
        self.js.evaluate('loadWifiUplink();')
        self.assertFalse(self.value('uplinkBusy'))
        self.assertEqual(len(self.value('uplinkNetworks')), 1)
        self.assertIn('networks', self.value(
            "element('uplink-wifi-feedback').textContent"))

    def test_rows_render_names_as_literal_text_with_disabled_reasons_and_locks(self):
        for name in ['"<img src=x onerror=alert(1)> &\'📶', r'bad\xff', ' ' * 2, '']:
            row = network(ssid=name, selectable=False, reason='Unsupported name <script>')
            markup = Markup(self.js.evaluate('uplinkNetworkMarkup(' + json.dumps(row) + ', 0)'))
            self.assertNotIn('img', markup.tags)
            self.assertNotIn('script', markup.tags)
            self.assertIn(name or 'Hidden network', ''.join(markup.text))
            self.assertIn(('disabled', None), markup.attributes)
            self.assertIn('Unsupported name <script>', ''.join(markup.text))
            self.assertIn('🔒', ''.join(markup.text))
        markup = Markup(self.js.evaluate('uplinkNetworkMarkup(' + json.dumps(network(security='open')) + ', 0)'))
        self.assertNotIn('🔒', ''.join(markup.text))
        self.assertIn('Open network', ''.join(markup.text))

    def test_selection_shows_only_needed_fields_and_clears_previous_password(self):
        self.prepare([network(), network(security='open'), network(selectable=False)])
        self.js.evaluate('updateUplinkControls();')
        self.assertTrue(self.value("element('uplink-wifi-connect').hidden"))
        self.js.evaluate('selectUplinkNetwork(0);')
        self.assertFalse(self.value("element('uplink-wifi-password-row').hidden"))
        self.assertFalse(self.value("element('btn-uplink-wifi-apply').disabled"))
        self.js.evaluate("element('uplink-wifi-password').value = 'secret'; selectUplinkNetwork(1);")
        self.assertTrue(self.value("element('uplink-wifi-password-row').hidden"))
        self.assertEqual(self.value("element('uplink-wifi-password').value"), '')
        self.js.evaluate('selectUplinkNetwork(2);')
        self.assertEqual(self.value('uplinkSelected'), 1)

    def test_connected_network_pinned_as_text_and_status_poll_preserves_input(self):
        self.prepare()
        status = dict(enabled=True, iface='wlan1', ssid='<b>📶</b>', connected=True,
                      state='connected', ip='192.0.2.1', freq=5180, gateway=False)
        self.js.evaluate('uplinkInitialized = true; reply = ' + json.dumps({'builtin': status}) + ';'
                         "selectUplinkNetwork(0); element('uplink-wifi-password').value = 'keep me'; loadWifiUplink();")
        self.assertEqual(self.value("element('uplink-wifi-current').children[0].textContent"), '<b>📶</b>')
        self.assertIn('not the current internet', self.value("element('uplink-wifi-current').children[1].textContent"))
        self.assertEqual(self.value("element('uplink-wifi-password').value"), 'keep me')
        self.assertEqual(self.value('uplinkSelected'), 0)

    def test_stale_scan_and_radio_switch_prevent_connecting(self):
        self.prepare()
        self.js.evaluate('selectUplinkNetwork(0); uplinkScannedAt -= 300001; applyWifiUplink();')
        self.assertEqual(self.value('calls'), [])
        self.assertTrue(self.value("element('btn-uplink-wifi-apply').disabled"))
        self.assertIn('out of date', self.value("element('uplink-wifi-feedback').textContent"))
        self.js.evaluate("element('uplink-wifi-radio').value = 'wlan0'; updateUplinkChoice(); applyWifiUplink();")
        self.assertEqual(self.value('calls'), [])
        self.assertEqual(self.value('uplinkSelected'), -1)
        self.assertEqual(self.value('uplinkNetworks'), [])

    def test_scan_failure_clears_old_choices_and_empty_scan_explains_retry(self):
        self.prepare()
        self.js.evaluate('selectUplinkNetwork(0); fail = true; scanWifiUplink();')
        self.assertEqual(self.value('uplinkNetworks'), [])
        self.assertEqual(self.value('uplinkSelected'), -1)
        self.assertFalse(self.value('uplinkBusy'))
        self.assertIn('try again', self.value("element('uplink-wifi-feedback').textContent"))
        self.js.evaluate('fail = false; scanWifiUplink();')
        self.assertIn('No networks', self.value("element('uplink-wifi-feedback').textContent"))

    def test_open_selection_posts_no_old_password_or_credentials_in_url(self):
        self.prepare([network(security='open')])
        self.js.evaluate("selectUplinkNetwork(0); element('uplink-wifi-password').value = 'old secret'; applyWifiUplink();")
        sent = self.value('calls[0]')
        self.assertEqual(sent['url'], '/manage/api/uplink/wifi')
        self.assertEqual(sent['options']['method'], 'POST')
        request = json.loads(sent['options']['body'])
        self.assertEqual(request['password'], '')
        self.assertEqual(request['ssid'], 'House')
        self.assertEqual(request['bssid'], network()['bssid'])
        self.assertEqual(request['scan_iface'], 'wlan1')

    def test_saved_badge_lookup_and_password_override(self):
        self.prepare([network(saved=True)])
        self.js.evaluate('uplinkInitialized = true;')
        markup = Markup(self.js.evaluate('uplinkNetworkMarkup(uplinkNetworks[0], 0)'))
        self.assertIn('Saved', ''.join(markup.text))
        self.assertIn('svg', markup.tags)
        self.js.evaluate('selectUplinkNetwork(0);')
        self.assertTrue(self.value("element('uplink-wifi-password-row').hidden"))
        self.assertFalse(self.value("element('uplink-wifi-saved-password').hidden"))
        self.js.evaluate('applyWifiUplink();')
        request = json.loads(self.value('calls[0].options.body'))
        self.assertTrue(request['use_saved'])
        self.assertEqual(request['password'], '')
        self.js.evaluate("selectUplinkNetwork(0); useDifferentUplinkPassword(); element('uplink-wifi-password').value='replacement';")
        self.assertFalse(self.value("element('uplink-wifi-password-row').hidden"))
        self.js.evaluate('reply={builtin:{},saved_networks:[{ssid:"House",security:"wpa2"}]}; loadWifiUplink();')
        self.assertEqual(self.value("element('uplink-wifi-password').value"), 'replacement')
        self.js.evaluate('calls.length=0; applyWifiUplink();')
        request = json.loads(self.value('calls[0].options.body'))
        self.assertFalse(request['use_saved'])
        self.assertEqual(request['password'], 'replacement')

    def test_forget_refreshes_selection_without_disconnect_and_escapes_names(self):
        self.prepare([network(saved=True)])
        names = [{'ssid': 'House', 'security': 'wpa2'},
                 {'ssid': '<img src=x onerror=bad>', 'security': 'sae'}]
        self.js.evaluate('updateSavedNetworks(' + json.dumps(names) + '); selectUplinkNetwork(0);')
        markup = Markup(self.value("element('uplink-wifi-saved-list').innerHTML"))
        self.assertNotIn('img', markup.tags)
        self.assertIn(names[1]['ssid'], ''.join(markup.text))
        self.js.evaluate('reply={ok:true,saved_networks:[]}; forgetUplinkNetwork(0);')
        sent = self.value('calls[0]')
        self.assertEqual(sent['url'], '/manage/api/uplink/wifi/forget')
        self.assertEqual(json.loads(sent['options']['body']), names[0])
        self.assertFalse(self.value('uplinkNetworks[0].saved'))
        self.assertFalse(self.value("element('uplink-wifi-password-row').hidden"))
        self.assertIn('stays connected', self.value("element('uplink-wifi-saved-feedback').textContent"))

    def test_open_network_never_uses_a_saved_password_or_shows_badge(self):
        self.prepare([network(security='open', saved=True)])
        markup = Markup(self.js.evaluate('uplinkNetworkMarkup(uplinkNetworks[0], 0)'))
        self.assertNotIn('Saved', ''.join(markup.text))
        self.js.evaluate('selectUplinkNetwork(0); applyWifiUplink();')
        self.assertFalse(json.loads(self.value('calls[0].options.body'))['use_saved'])

    def test_mobile_rules_and_markup_keep_scroll_touch_and_label_contracts(self):
        self.assertIn('max-height:420px;overflow-y:auto', manage.CSS)
        self.assertIn('min-height:44px;font-size:16px', manage.CSS)
        self.assertIn('grid-template-columns:minmax(0,1fr) auto', manage.CSS)
        self.assertIn('overflow-wrap:anywhere;white-space:pre-wrap', manage.CSS)
        self.assertIn('#tab-uplink [hidden]{display:none}', manage.CSS)
        with patch.object(manage, 'get_my_hostname', return_value='test'), \
                patch.object(manage, 'get_my_ip', return_value='192.0.2.1'):
            html = manage.render_dashboard()
        self.assertNotIn('<select id="uplink-wifi-networks"', html)
        self.assertIn('aria-labelledby="uplink-wifi-heading"', html)
        self.assertIn('for="uplink-wifi-password"', html)
        self.assertLess(html.index('id="uplink-wifi-current"'), html.index('id="uplink-wifi-results"'))


if __name__ == '__main__':
    unittest.main()
