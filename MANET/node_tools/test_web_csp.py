"""Check CSP headers, trusted script nonces and event dispatch without sockets."""

import html
from html.parser import HTMLParser
import http.client
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

import manet_manage as manage
from manet_recovery_status import RECOVERY_JS
import test_web_sessions as sessions
from test_web_rendering import JavaScript

status = sessions.status
TOOLS = Path(__file__).parent
INLINE_HANDLER = re.compile(r'\bon[a-z]+\s*=', re.I)
SCRIPT_URL = re.compile(r'javascript\s*:', re.I)
TABS = ('topology', 'radio', 'measure', 'sessions', 'uplink', 'voice', 'config')


class Page(HTMLParser):
    """Inspect real tags separately from text and JavaScript template data."""
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.web = sessions.WebSessionTests()
        self.web.setUp()
        self.addCleanup(self.web.doCleanups)
        self.request = self.web.request
        for name, value in (('get_my_hostname', 'mesh-test'),
                            ('get_my_ip', '10.30.2.1')):
            stub = patch.object(manage, name, return_value=value)
            stub.start()
            self.addCleanup(stub.stop)

    def check_html(self, response, script_count):
        """Check exact source restrictions and every trusted script tag."""
        self.assertIn('text/html', response.getheader('Content-Type'))
        policy = response.getheader('Content-Security-Policy')
        self.assertIsNotNone(policy)
        directives = dict(part.strip().split(' ', 1)
                          for part in policy.split(';') if part.strip())
        nonce = re.fullmatch(r"'nonce-([A-Za-z0-9_-]{43})'",
                             directives['script-src'])
        self.assertIsNotNone(nonce, policy)
        nonce = nonce[1]
        for key, value in {
                'default-src': "'self'", 'connect-src': "'self'",
                'img-src': "'self' data:", 'object-src': "'none'",
                'base-uri': "'none'", 'frame-ancestors': "'none'",
                'form-action': "'self'", 'script-src-attr': "'none'",
                'style-src': "'self' 'unsafe-inline'"}.items():
            self.assertEqual(directives[key], value)
        self.assertNotIn('unsafe-eval', policy)
        self.assertEqual(response.getheader('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(response.getheader('Referrer-Policy'), 'same-origin')
        markup = response.payload.decode()
        scripts = [attrs for tag, attrs in Page(markup).tags if tag == 'script']
        self.assertEqual(len(scripts), script_count)
        for attrs in scripts:
            self.assertEqual(attrs.get('nonce'), nonce)
            self.assertNotIn('src', attrs)
        self.assertIsNone(INLINE_HANDLER.search(markup))
        self.assertIsNone(SCRIPT_URL.search(markup))
        self.assertEqual(int(response.getheader('Content-Length')),
                         len(response.payload))
        return nonce

    def test_every_page_alias_and_tab_gets_a_fresh_matching_nonce(self):
        _, token = self.web.login()
        cases = [(path, '', 1, 200) for path in ('/', '/index.html')]
        cases += [(path, token, 1, 200)
                  for path in ('/manage/', '/manage/index.html')]
        cases += [('/manage/?theme=dark#' + tab, token, 1, 200) for tab in TABS]
        cases += [(path, '', 2, 401) for path in
                  ('/manage/', '/manage/index.html', '/manage/login',
                   '/auth/perf-login', '/manage/missing')]
        nonces = []
        for path, cookie, scripts, code in cases * 2:
            with self.subTest(path=path, authenticated=bool(cookie)):
                response = self.request('GET', path, cookie=cookie)
                self.assertEqual(response.status, code)
                nonces.append(self.check_html(response, scripts))
                self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        self.assertEqual(len(nonces), len(set(nonces)))

    def test_login_errors_missing_password_and_http_errors_are_protected(self):
        for path in ('/manage/login', '/auth/perf-login'):
            body = urlencode({'password': 'wrong', 'next': '/manage/'}).encode()
            response = self.request('POST', path, body)
            self.assertEqual(response.status, 401)
            self.check_html(response, 2)
        self.web.write_conf('')
        self.check_html(self.request('GET', '/manage/login'), 2)
        for method, body, code in (('OPTIONS', b'', 501),
                                    ('POST', b'x' * 65537, 413)):
            response = self.request(method, '/', body)
            self.assertEqual(response.status, code)
            self.check_html(response, 0)

    def test_two_html_responses_on_one_connection_do_not_reuse_a_nonce(self):
        raw = (b'GET / HTTP/1.1\r\nHost: manet\r\n\r\n'
               b'GET /manage/login HTTP/1.1\r\nHost: manet\r\n'
               b'Connection: close\r\n\r\n')
        connection = sessions.MemoryConnection(raw)
        with patch.object(status.MeshHandler, 'protocol_version', 'HTTP/1.1'):
            status.MeshHandler(connection, ('127.0.0.1', 12345),
                               SimpleNamespace())
        responses = bytes(connection.output).split(b'HTTP/1.1 ')[1:]
        nonces = []
        self.assertEqual(len(responses), 2)
        for data, scripts in zip(responses, (1, 2)):
            response = http.client.HTTPResponse(
                sessions.MemoryConnection(b'HTTP/1.1 ' + data))
            response.begin()
            response.payload = response.read()
            nonces.append(self.check_html(response, scripts))
        self.assertNotEqual(*nonces)

    def test_admin_status_keeps_config_secrets_and_response_shape(self):
        """CSP must not redact or extend the authenticated admin API."""
        _, token = self.web.login()
        with patch.object(status, 'parse_registry', return_value={}), \
                patch.object(status, 'get_pending_config', return_value=None), \
                patch.object(status, 'authenticated_config_acks', return_value={}), \
                patch.object(status, 'config_application_status',
                             return_value=None), \
                patch.object(status, 'read_rollback_state',
                             return_value={'armed': False}):
            expected = status.assemble_admin_status()
            response = self.request('GET', '/api/admin/status', cookie=token)
        data = json.loads(response.payload)
        data.pop('my_hostname')
        self.assertEqual(data, expected)
        current = data['current_config']
        self.assertEqual(current['admin_password'], self.web.password)
        self.assertEqual(current['mesh_key'], 'radio-password')

    def test_no_inline_handlers_or_script_urls_in_sources_or_generated_pages(self):
        """Scan template source too: HTMLParser treats script bodies as text."""
        for name in ('mesh-status.py', 'manet_manage.py'):
            source = (TOOLS / name).read_text()
            self.assertIsNone(INLINE_HANDLER.search(source), name)
            self.assertIsNone(SCRIPT_URL.search(html.unescape(source)), name)
        self.assertIsNone(INLINE_HANDLER.search(RECOVERY_JS))
        pages = (status.render_status_page(), manage.render_dashboard(),
                 status.render_perf_auth_page())
        for markup in pages:
            for tag, attrs in Page(markup).tags:
                for key, value in attrs.items():
                    self.assertFalse(key.startswith('on'), (tag, key))
                    if key in ('src', 'href', 'action'):
                        value = re.sub(r'[\x00-\x20]', '', value or '')
                        self.assertFalse(value.lower().startswith('javascript:'))
                        self.assertFalse(urlsplit(value).netloc, value)
            # No CDN, font, map tile, or other external browser URL in JS/CSS.
            self.assertNotRegex(markup, r'https?://|["\']//[a-zA-Z0-9]')


class EventTests(unittest.TestCase):
    def setUp(self):
        self.js = JavaScript()
        self.addCleanup(self.js.close)
        self.js.evaluate('''
const calls = [];
const window = {location: {href: ''}};
const document = {documentElement: {dataset: {theme: 'dark'}}};
''')

    def load_dispatch(self, source, name, functions):
        """Run the real dispatch function against observable action spies."""
        for function in functions:
            self.js.evaluate(f'function {function}(...args) {{ '
                             f'calls.push([{json.dumps(function)}, ...args]); }}')
        match = re.search(r'^function ' + name + r'\(.*?^}', source, re.M | re.S)
        self.assertIsNotNone(match)
        self.js.evaluate(match[0])

    def click(self, function, dataset, disabled=False):
        """Simulate a click on a child of a dynamically generated control."""
        source = f'''calls.length = 0;
{function}({{target: {{closest: () => ({{
  dataset: {json.dumps(dataset)}, disabled: {json.dumps(disabled)}
}})}}}});
JSON.stringify(calls);'''
        return json.loads(self.js.evaluate(source))

    def test_manage_dispatch_preserves_actions_arguments_and_disabled_controls(self):
        cases = [
            ('hide-overlay', 'hideOverlay', {}, []),
            ('theme', 'toggleTheme', {}, []),
            ('apply-halow', 'applyHalow', {}, []),
            ('apply-2g', 'apply2G', {}, []), ('apply-5g', 'apply5G', {}, []),
            ('start-measurement', 'startMeasurement', {}, []),
            ('apply-uplink', 'applyUsbWifiUplink', {}, []),
            ('stage-config', 'stageChanges', {}, []),
            ('apply-config', 'applyChanges', {}, [False]),
            ('force-config', 'applyChanges', {}, [True]),
            ('show-force', 'showForceModal', {}, []),
            ('close-force', 'closeForceModal', {}, []),
            ('cancel-config', 'cancelPending', {}, []),
        ]
        cases += [('show-tab', 'showTab', {'tab': tab}, [tab]) for tab in TABS]
        for state in ('up', 'down'):
            for iface in ('wlan0', 'wlan1', 'wlan2'):
                cases.append(('toggle-all', 'toggleAll',
                              {'iface': iface, 'state': state}, [iface, state]))
        literal = "Test's radio \"'><img src=x> & ');alert(1);//"
        for state in ('up', 'down'):
            cases.append(('toggle-interface', 'toggleIface',
                          {'ip': literal, 'id': literal, 'iface': 'wlan1',
                           'state': state}, [literal, literal, 'wlan1', state]))
        cases += [('set-power', 'setTxPower',
                   {'ip': literal, 'id': literal, 'iface': 'wlan0'},
                   [literal, literal, 'wlan0']),
                  ('delete-session', 'deleteSession', {'label': literal},
                   [literal])]
        cases += [('voice-step', 'voiceStepChannel', {'step': str(step)}, [step])
                  for step in (-1, 1)]
        cases += [('voice-codec', 'voiceSetCodec', {'codec': codec}, [codec])
                  for codec in ('lyra', 'opus')]
        self.load_dispatch(manage.JS, 'handleManageClick',
                           {case[1] for case in cases})
        for action, function, data, expected in cases:
            with self.subTest(action=action, data=data):
                data = dict(data, action=action)
                self.assertEqual(self.click('handleManageClick', data),
                                 [[function, *expected]])
                self.assertEqual(self.click('handleManageClick', data, True), [])
        self.click('handleManageClick', {'action': 'overview'})
        self.assertEqual(self.js.evaluate('window.location.href'), '/?theme=dark')
        self.click('handleManageClick', {'action': 'logout'})
        self.assertEqual(self.js.evaluate('window.location.href'), '/manage/logout')
        self.assertEqual(self.click('handleManageClick', {'action': 'unknown'}), [])

    def test_status_dispatch_preserves_atak_control_elements(self):
        self.load_dispatch(status.STATUS_HTML, 'handleStatusClick',
                           ('goPerfDashboard', 'toggleTheme',
                            'setAtakController', 'releaseAtakPoint'))
        for action, function in (('manage', 'goPerfDashboard'),
                                 ('theme', 'toggleTheme'),
                                 ('atak-controller', 'setAtakController'),
                                 ('atak-release', 'releaseAtakPoint')):
            data = {'action': action, 'atakUid': "phone's uid", 'manualId': '123'}
            expected = [function]
            if action.startswith('atak-'):
                expected.append({'dataset': data, 'disabled': False})
            self.assertEqual(self.click('handleStatusClick', data), [expected])

    def test_generated_talk_group_buttons_register_callable_listeners(self):
        """Each new DOM button must select its own channel without an attribute."""
        self.js.evaluate('''
const VOICE_TG_MAX = 32;
const grid = {buttons: [], childElementCount: 0, appendChild(button) {
  this.buttons.push(button); this.childElementCount++;
}};
document.getElementById = () => grid;
document.createElement = () => ({dataset: {}, listeners: {},
  addEventListener(event, fn) { this.listeners[event] = fn; }
});
''')
        self.load_dispatch(manage.VOICE_TAB_JS, 'voiceTgBuildGrid',
                           ('voiceSetChannel',))
        result = self.js.evaluate('''
voiceTgBuildGrid(); voiceTgBuildGrid();
grid.buttons.forEach(button => button.listeners.click());
JSON.stringify(calls);
''')
        self.assertEqual(json.loads(result),
                         [['voiceSetChannel', n] for n in range(1, 33)])

