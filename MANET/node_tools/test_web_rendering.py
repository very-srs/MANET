"""Execute page renderers with hostile metadata and inspect their HTML output."""

import ctypes
import ctypes.util
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import manet_manage as manage
from manet_recovery_status import RECOVERY_JS
from manet_registry import parse_registry

TOOLS = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location('render_status', TOOLS / 'mesh-status.py')
status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)
PAYLOAD = "Test's radio \"'><img src=x onerror=alert(1)> & ');alert(2);//"


class Markup(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.tags, self.attributes, self.text = [], [], []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)

    def handle_data(self, text):
        self.text.append(text)


class JavaScript:
    """Use the system WebKit JavaScript engine without a network or browser."""
    def __init__(self):
        library = ctypes.util.find_library('javascriptcoregtk-4.1') or ctypes.util.find_library('javascriptcoregtk-4.0')
        if not library:
            raise unittest.SkipTest('WebKit JavaScriptCore is not installed')
        self.lib = ctypes.CDLL(library)
        pointer = ctypes.c_void_p
        for name, result, args in (
                ('JSGlobalContextCreate', pointer, [pointer]),
                ('JSGlobalContextRelease', None, [pointer]),
                ('JSStringCreateWithUTF8CString', pointer, [ctypes.c_char_p]),
                ('JSStringRelease', None, [pointer]),
                ('JSStringGetMaximumUTF8CStringSize', ctypes.c_size_t, [pointer]),
                ('JSStringGetUTF8CString', ctypes.c_size_t, [pointer, pointer, ctypes.c_size_t]),
                ('JSEvaluateScript', pointer, [pointer, pointer, pointer, pointer, ctypes.c_int, ctypes.POINTER(pointer)]),
                ('JSValueToStringCopy', pointer, [pointer, pointer, ctypes.POINTER(pointer)])):
            fn = getattr(self.lib, name)
            fn.restype, fn.argtypes = result, args
        self.context = self.lib.JSGlobalContextCreate(None)

    def close(self):
        self.lib.JSGlobalContextRelease(self.context)

    def evaluate(self, source):
        string = self.lib.JSStringCreateWithUTF8CString(source.encode())
        error = ctypes.c_void_p()
        try:
            result = self.lib.JSEvaluateScript(self.context, string, None, None, 1, ctypes.byref(error))
        finally:
            self.lib.JSStringRelease(string)
        value = self.lib.JSValueToStringCopy(self.context, error.value or result, None)
        try:
            size = self.lib.JSStringGetMaximumUTF8CStringSize(value)
            output = ctypes.create_string_buffer(size)
            self.lib.JSStringGetUTF8CString(value, output, size)
            text = output.value.decode()
        finally:
            self.lib.JSStringRelease(value)
        if error.value:
            raise AssertionError(text)
        return text


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.js = JavaScript()
        self.addCleanup(self.js.close)
        self.js.evaluate(RECOVERY_JS + '''
const elements = {};
const document = {
  getElementById: id => elements[id] || (elements[id] = {
    innerHTML: '', style: {}, appendChild: child => elements[id].innerHTML += child.innerHTML
  }),
  createElement: () => ({innerHTML: ''})
};
let _topo, DATA = null, LOCAL_DETAIL_HTML = '';
let EXPANDED_NODE_IDS = new Set(), PEER_LOADING_IDS = new Set(), PEER_DETAIL_CACHE = {};
const LINK_GREAT_MBPS = 30, LINK_OK_MBPS = 15, LINK_WARN_MBPS = 5, LINK_FULL_SCALE_MBPS = 50;
''')
        for source in (manage.JS, manage.CONFIG_TAB_JS, manage.VOICE_TAB_JS,
                       status.STATUS_HTML.split('<script>', 1)[1].split('</script>', 1)[0].replace('__RECOVERY_JS__', RECOVERY_JS).replace('__REFRESH__', '15000')):
            # Compile the complete script too, catching errors outside the tested renderers.
            self.js.evaluate('new Function(' + json.dumps(source) + '); "ok"')

    def functions(self, source, *names):
        for name in names:
            found = re.search(r'^(?:async )?function ' + name + r'\(.*?^}', source, re.M | re.S)
            self.assertIsNotNone(found, name)
            self.js.evaluate(found[0])

    def safe(self, markup):
        parsed = Markup(markup)
        self.assertNotIn('img', parsed.tags)
        self.assertNotIn('script', parsed.tags)
        self.assertNotIn('svg', parsed.tags)
        for key, value in parsed.attributes:
            self.assertFalse(key.startswith('on'), key)
        self.assertIn(PAYLOAD, ''.join(parsed.text) + ''.join(value or '' for _, value in parsed.attributes))
        return parsed

    def test_topology_radio_pairs_and_progress_keep_metadata_as_text(self):
        self.functions(manage.JS, 'renderTopology', 'buildIfaceControl', 'updatePairs',
                       'normalizeDbm', 'txPowerOptions', 'renderTxPowerSelect', 'isHalowIface',
                       'renderHalowPower', 'renderMeasureStats', 'fmtSecs', 'lastResultText',
                       'sessionMetric', 'metricText')
        node = dict(id=PAYLOAD, hostname=PAYLOAD, ip=PAYLOAD, battery=PAYLOAD,
                    is_me=True, is_gateway=True, gateway_iface=PAYLOAD,
                    interfaces={'wlan0': dict(active=True, channel=PAYLOAD, tx_mcs=PAYLOAD, rx_mcs=PAYLOAD)})
        peer = dict(node, is_me=False, id='peer')
        node['ip'] = '10.0.0.1'
        self.js.evaluate('_topo = ' + json.dumps({'nodes': [node, peer], 'my_ip': '10.0.0.1', 'my_hostname': PAYLOAD}) + '; renderTopology(); buildIfaceControl(); updatePairs();')
        for element in ('node-grid', 'iface-cards', 'pairs-grid'):
            parsed = self.safe(self.js.evaluate(f'elements[{json.dumps(element)}].innerHTML'))
            if element == 'pairs-grid':
                self.assertEqual(parsed.tags.count('input'), 1)
                self.assertIn(('data-src-name', PAYLOAD), parsed.attributes)
        data = dict(done=1, total=2, current=dict(src=PAYLOAD, dst=PAYLOAD),
                    last_result=dict(src=PAYLOAD, dst=PAYLOAD, error=PAYLOAD))
        self.js.evaluate('renderMeasureStats(' + json.dumps(data) + ')')
        self.safe(self.js.evaluate('elements["progress-stats"].innerHTML'))
        self.safe(self.js.evaluate('sessionMetric("TCP", ' + json.dumps({'avg': PAYLOAD, 'min': PAYLOAD, 'max': PAYLOAD}) + ', " Mbps")'))

    def test_status_local_peer_cached_details_atak_and_recovery_escape(self):
        self.functions(status.STATUS_HTML, 'renderNodeList', 'renderLocalPanel', 'renderPeerDrawer',
                       'battColor', 'atakPanel', 'linkClass', 'linkLabel', 'fmtMbps', 'linkPct', 'linkColor', 'fmtAge')
        iface = dict(name=PAYLOAD, role=PAYLOAD, health=PAYLOAD, state=PAYLOAD,
                     addrs=[PAYLOAD], tx_mcs=PAYLOAD, rx_mcs=PAYLOAD, detail=PAYLOAD, faults=[PAYLOAD])
        data = dict(hostname=PAYLOAD, ip=PAYLOAD, uptime=PAYLOAD, version=PAYLOAD, eud_mode=PAYLOAD,
                    ap_ssid=PAYLOAD, interfaces=[iface], euds=[dict(hostname=PAYLOAD, ip=PAYLOAD, mac=PAYLOAD)],
                    gps=dict(available=True, lat=PAYLOAD, lon=PAYLOAD, alt=PAYLOAD),
                    battery=dict(percentage=PAYLOAD, voltage_v=3.7, current_ma=PAYLOAD, power_w=PAYLOAD),
                    recovery=dict(summary=PAYLOAD, details=[dict(label=PAYLOAD, value=PAYLOAD)]),
                    atak=dict(enabled=True, position=dict(lat=PAYLOAD, lon=PAYLOAD, source=PAYLOAD),
                              phones=[dict(callsign=PAYLOAD, uid=PAYLOAD)], manual=dict(uid=PAYLOAD, id=PAYLOAD)))
        self.js.evaluate('const detail = ' + json.dumps(data))
        self.safe(self.js.evaluate('renderLocalPanel(detail)'))
        self.safe(self.js.evaluate('renderPeerDrawer(detail)'))
        node = dict(id='peer', hostname=PAYLOAD, ip=PAYLOAD, mac=PAYLOAD, state='ACTIVE',
                    uptime=PAYLOAD, cpu=PAYLOAD, battery=dict(percentage=PAYLOAD),
                    best_link=dict(iface=PAYLOAD, nexthop=PAYLOAD, mbps=3))
        self.js.evaluate('EXPANDED_NODE_IDS.add("peer"); PEER_DETAIL_CACHE.peer = renderPeerDrawer(detail); renderNodeList([' + json.dumps(node) + ']);')
        self.safe(self.js.evaluate('elements["node-list"].innerHTML'))

    def test_operator_names_render_as_text_and_clear_without_hiding_host(self):
        """Operator metadata is escaped in public and management node rows."""
        self.functions(status.STATUS_HTML, 'renderNodeList', 'battColor',
                       'linkClass', 'linkLabel', 'fmtMbps', 'linkPct',
                       'linkColor', 'fmtAge')
        self.functions(manage.JS, 'renderTopology')
        for name in (PAYLOAD, ''):
            node = dict(id='peer', hostname='mesh-abcd', operator=name,
                        ip='10.30.0.1', state='ACTIVE', is_me=False)
            self.js.evaluate('DATA = ' + json.dumps({'nodes': [node]}) +
                             '; _topo = DATA; renderNodeList(DATA.nodes);'
                             'renderTopology();')
            for element in ('node-list', 'node-grid'):
                markup = self.js.evaluate(
                    f'elements[{json.dumps(element)}].innerHTML')
                self.assertIn('mesh-abcd', markup)
                if name:
                    self.safe(markup)
                    self.assertIn('Operator:', markup)
                else:
                    self.assertNotIn('Operator:', markup)

    def test_ack_table_and_saved_sessions_escape_names_and_attributes(self):
        self.functions(manage.CONFIG_TAB_JS, 'renderStatus')
        self.js.evaluate('let formPopulated = true, STATUS = null; renderStatus(' + json.dumps({
            'nodes': [dict(hostname=PAYLOAD, ip=PAYLOAD)]}) + ')')
        self.safe(self.js.evaluate('elements["ack-tbody"].innerHTML'))
        self.functions(manage.JS, 'loadSessions', 'sessionMetric', 'metricText')
        self.js.evaluate('''
function U(path) { return '/manage' + path; }
let sessionsBusy = false;
const sessionData = ''' + json.dumps([{'label': PAYLOAD, 'tests': 1}]) + ''';
async function manageFetch() { return {ok: true, json: async () => sessionData}; }
loadSessions();
''')
        self.safe(self.js.evaluate('elements["sessions-list"].innerHTML'))

    def test_ap_name_is_readonly_full_broadcast_text_and_not_submitted(self):
        parsed = Markup(manage.CONFIG_TAB_HTML)
        self.assertIn(('id', 'f-lan-ap-ssid'), parsed.attributes)
        self.assertRegex(manage.CONFIG_TAB_HTML, r'<span[^>]*id="f-lan-ap-ssid"')
        self.functions(manage.CONFIG_TAB_JS, 'populateForm', 'readForm')
        self.js.evaluate('''
function setVal(id, v) { document.getElementById(id).value = v; }
function setText(id, v) { document.getElementById(id).textContent = v; }
function setChk(id, v) { document.getElementById(id).checked = v; }
function getVal(id) { return document.getElementById(id).value; }
function getChk(id) { return document.getElementById(id).checked; }
''')
        self.js.evaluate('populateForm(' + json.dumps({
            'lan_ap_ssid': 'prefix', 'ap_ssid': PAYLOAD + '-5cc2'}) + ')')
        self.assertEqual(self.js.evaluate('elements["f-lan-ap-ssid"].textContent'), PAYLOAD + '-5cc2')
        self.assertNotIn('lan_ap_ssid', json.loads(self.js.evaluate('JSON.stringify(readForm())')))

    def test_server_rendered_identity_is_escaped(self):
        with patch.object(manage, 'get_my_hostname', return_value=PAYLOAD), \
                patch.object(manage, 'get_my_ip', return_value=PAYLOAD):
            markup = manage.render_dashboard()
        parsed = Markup(markup)
        self.assertEqual(parsed.tags.count('img'), 1)  # Static logo only.
        self.assertIn(PAYLOAD, ''.join(parsed.text))
        self.assertNotIn('onerror', dict(parsed.attributes))

    def test_registry_keeps_display_punctuation_but_bounds_names(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / 'registry'
            for name in ("Test's radio", PAYLOAD, 'x' * 254, 'bad\x01name', '  '):
                quoted = name.replace("'", "'\\''")
                registry.write_text(f"NODE_aabbccddeeff_HOSTNAME='{quoted}'\n")
                value = parse_registry(registry)['aabbccddeeff']['HOSTNAME']
                self.assertEqual(value, name if name in ("Test's radio", PAYLOAD) else '')
