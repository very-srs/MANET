"""Check node shell interpreters, service sandbox baselines and manuals."""
import ast
from collections import defaultdict
import configparser
import re
import tempfile
from pathlib import Path
import shutil
import subprocess
import unittest
import warnings

MANET = Path(__file__).resolve().parent.parent
MAN_COMMANDS = (
    'radio-setup.sh', 'node-update.sh', 'ethernet-autodetect.sh',
    'usb-wifi-uplink.sh', 'manet-user-scripts.sh', 'manet-provision-status.sh',
    'manet-power-status.sh', 'verify-bridge.sh', 'manet-region.py',
    'manet-os-cleanup.py', 'manet-mesh-census.py', 'manet-cpu-sample.py',
    'mesh-neighbor-count.py', 'halow-mcs-summary.py', 'mac-to-ip.sh',
)
HARDENED = (
    'mesh-status.service.d/20-hardening.conf',
    'mesh-voice.service', 'mesh-channel-agreement.service',
    'gps-reader.service', 'button-monitor.service',
    'led-boot.service', 'manet-led-status.service', 'sae-watchdog.service',
    'batman-enslave-watch.service', 'ap-txpower.service',
    'manet-mesh-power.service', 'manet-dhcp-isolation.service',
    'manet-cpu-policy.service', 'manet-uplink-dns.service',
)


class CodingConventionTests(unittest.TestCase):
    def test_operator_commands_have_manual_pages(self):
        for name in MAN_COMMANDS:
            with self.subTest(command=name):
                self.assertTrue((MANET / 'node_tools' / name).is_file())
                page = MANET / 'man' / 'man8' / (name + '.8')
                self.assertTrue(page.is_file(), str(page))
                source = page.read_text()
                self.assertIn(f'.Dt {name.upper()} 8\n', source)
                self.assertIn(f'.Nm {name}\n', source)
                for section in ('NAME', 'SYNOPSIS', 'DESCRIPTION',
                                'EXIT STATUS'):
                    self.assertIn(f'.Sh {section}\n', source)

    def test_manual_pages_parse(self):
        if shutil.which('mandoc'):
            command = ['mandoc', '-T', 'lint']
        elif shutil.which('groff'):
            command = ['groff', '-mandoc', '-ww', '-z']
        else:
            self.skipTest('manual-page lint requires mandoc or groff')
        pages = sorted((MANET / 'man').rglob('*.8'))
        self.assertTrue(pages, 'no section 8 manual pages found')
        for page in pages:
            with self.subTest(page=page.relative_to(MANET)):
                result = subprocess.run(
                    command + [str(page)], capture_output=True,
                    text=True, timeout=10)
                self.assertEqual(result.returncode, 0,
                                 result.stdout + result.stderr)
                self.assertEqual(result.stderr, '')

    def test_python_compiles_without_warnings(self):
        # Invalid escapes warn on the nodes' Python 3.13 and will become
        # errors. Node tools run without -W, so the test enforces it.
        for path in sorted((MANET / 'node_tools').rglob('*.py')):
            with self.subTest(path=path.relative_to(MANET)):
                source = path.read_text(encoding='utf-8')
                with warnings.catch_warnings():
                    warnings.simplefilter('error')
                    compile(source, str(path), 'exec')

    def test_node_shell_interpreters(self):
        for directory in ('node_tools', 'networkd-dispatcher', 'udev'):
            for path in sorted((MANET / directory).rglob('*')):
                if not path.is_file():
                    continue
                with path.open('rb') as stream:
                    first = stream.readline().rstrip(b'\r\n')
                shell = (path.suffix == '.sh' or
                         first in (b'#!/bin/sh', b'#!/bin/bash') or
                         first.startswith(b'#!/usr/bin/env bash') or
                         first.startswith(b'#!/usr/bin/env sh'))
                if not shell:
                    continue
                with self.subTest(path=path.relative_to(MANET)):
                    self.assertIn(first, (b'#!/bin/bash', b'#!/bin/sh'))
                    if first == b'#!/bin/sh':
                        result = subprocess.run(
                            ['dash', '-n', str(path)], capture_output=True,
                            text=True, timeout=5)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        if shutil.which('checkbashisms'):
                            result = subprocess.run(
                                ['checkbashisms', str(path)],
                                capture_output=True, text=True, timeout=5)
                            self.assertEqual(result.returncode, 0,
                                             result.stderr)

    def test_hardened_services_keep_the_baseline(self):
        for name in HARDENED:
            with self.subTest(unit=name):
                config = configparser.ConfigParser(
                    strict=False, interpolation=None)
                with (MANET / 'systemd' / name).open() as stream:
                    config.read_file(stream)
                self.assertEqual(config['Service']['NoNewPrivileges'], 'yes')
                self.assertEqual(config['Service']['ProtectSystem'], 'strict')

    def test_wpa_cli_callers_can_bind_their_reply_socket(self):
        # wpa_cli binds a client socket in the host's /tmp and the supplicant
        # replies there, so a read-only or private /tmp makes every call fail.
        config = configparser.ConfigParser(strict=False, interpolation=None)
        with (MANET / 'systemd' / 'mesh-channel-agreement.service').open() as stream:
            config.read_file(stream)
        service = config['Service']
        self.assertIn('/tmp', service['ReadWritePaths'].split())
        self.assertNotEqual(service.get('PrivateTmp', 'no'), 'yes')
        for name in ('manet_ap_mesh.py', 'manet_supplicant.py', 'mesh-channel-agreement.py'):
            self.assertIn('wpa_cli', (MANET / 'node_tools' / name).read_text())


# Exceptions are bound to files so a new copy cannot inherit an exemption.
DUPLICATE_NAME_ALLOWLIST = {
    # UNRELATED: service, voice and ranging configuration schemas.
    ('python', 'Config'): frozenset({
            'manet-atak.py',
            'manet-positioning.py',
            'manet_ranging.py',
            'manet_ranging_radio.py',
            'mesh-voice.py',
    }),
    # UNRELATED: positioning peers versus rendezvous discovery state.
    ('python', 'Discovery'): frozenset({
            'manet-positioning.py',
            'manet_rendezvous.py',
    }),
    # Open work: ATAK and positioning carry different clock records.
    ('python', 'Now'): frozenset({
            'manet-atak.py',
            'manet-positioning.py',
    }),
    # Open work: ATAK publisher and positioning state machine.
    ('python', 'Service'): frozenset({
            'manet-atak.py',
            'manet-positioning.py',
    }),
    # Open work: ATAK masked payloads versus DHCP IPv6 matches.
    ('python', '_canonical_expr'): frozenset({
            'manet-atak.py',
            'manet-dhcp-isolation.py',
    }),
    # UNRELATED: numeric conversion versus a boolean predicate.
    ('python', '_finite'): frozenset({
            'encoder.py',
            'manet_locate.py',
    }),
    # UNRELATED: ACS voting versus IP startup deadlines.
    ('python', 'advance'): frozenset({
            'manet_acs_agreement.py',
            'mesh-ip-startup.py',
    }),
    # Open work: ATAK and positioning durability and size limits.
    ('python', 'atomic_json'): frozenset({
            'manet-atak.py',
            'manet-positioning.py',
    }),
    # UNRELATED contracts: registry cache/mode versus clock fsync.
    ('python', 'atomic_text'): frozenset({
            'manet_registry_builder.py',
            'mesh-time-sync.py',
    }),
    # UNRELATED contracts: DHCP existing-file chown versus durable I/O.
    ('python', 'atomic_write'): frozenset({
            'manet-dhcp-isolation.py',
            'manet_config_io.py',
    }),
    # UNRELATED: nft rules, service convergence and IP readiness.
    ('python', 'check'): frozenset({
            'manet-dhcp-isolation.py',
            'manet_election_runtime.py',
            'mesh-ip-startup.py',
    }),
    # UNRELATED: MCS station summaries versus interface telemetry.
    ('python', 'collect'): frozenset({
            'halow-mcs-summary.py',
            'manet_interfaces.py',
    }),
    # UNRELATED: subprocess arguments, failures and async contracts.
    ('python', 'command'): frozenset({
            'manet-os-cleanup.py',
            'manet-positioning.py',
            'manet_interfaces.py',
            'mesh-channel-agreement.py',
            'mesh-ip-startup.py',
    }),
    # UNRELATED: tagged state, Ethernet, ranging and Alfred formats.
    ('python', 'decode'): frozenset({
            'manet-atak.py',
            'manet-mesh-census.py',
            'manet_ranging.py',
            'manet_registry_builder.py',
    }),
    # STANDALONE firmware before coldplug; ACS hashes canonical JSON.
    ('python', 'digest'): frozenset({
            'manet-mt7916-firmware.py',
            'manet_acs_agreement.py',
    }),
    # UNRELATED: throughput service ranking versus tourguide fairness.
    ('python', 'elect'): frozenset({
            'mesh-service-election.py',
            'mesh-tourguide-election.py',
    }),
    # UNRELATED: shell assignments versus base64 protobuf output.
    ('python', 'emit'): frozenset({
            'decoder.py',
            'encoder.py',
    }),
    # Open work: ATAK tagged state versus ranging protobuf encoding.
    ('python', 'encode'): frozenset({
            'manet-atak.py',
            'manet_ranging.py',
    }),
    # UNRELATED: supplied country mapping versus local radio config.
    ('python', 'halow_region'): frozenset({
            'manet-region.py',
            'manet_radio.py',
    }),
    # STANDALONE firmware directory lock; AP deadline/file locking.
    ('python', 'locked'): frozenset({
            'manet-mt7916-firmware.py',
            'manet_ap_mesh.py',
    }),
    # UNRELATED: per-program tags, sinks and error handling.
    ('python', 'log'): frozenset({
            'manet-atak.py',
            'mesh-config-sync.py',
            'mesh-radio-state.py',
            'mesh-voice.py',
    }),
    # UNRELATED CLI entry points; firmware/updater also run standalone.
    ('python', 'main'): frozenset({
            'decoder.py',
            'encoder.py',
            'gps-reader.py',
            'halow-mcs-summary.py',
            'manet-atak.py',
            'manet-cpu-sample.py',
            'manet-dhcp-isolation.py',
            'manet-mesh-census.py',
            'manet-mt7916-firmware.py',
            'manet-positioning.py',
            'manet-radio-names.py',
            'manet-region.py',
            'manet-wait-radios.py',
            'manet-uplink-dns.py',
            'manet_ap_mesh.py',
            'manet_hosts.py',
            'manet_ip_runtime.py',
            'manet_rendezvous.py',
            'manet_static_channels.py',
            'mesh-channel-agreement.py',
            'mesh-config-sync.py',
            'mesh-ip-startup.py',
            'mesh-neighbor-count.py',
            'mesh-radio-state.py',
            'mesh-service-election.py',
            'mesh-time-sync.py',
            'mesh-tourguide-election.py',
            'mesh-voice.py',
            'node-update.py',
    }),
    # Open work: ATAK operator support versus DHCP equality expressions.
    ('python', 'match'): frozenset({
            'manet-atak.py',
            'manet-dhcp-isolation.py',
    }),
    # Open work: ATAK/positioning numeric types; election accepts text.
    ('python', 'number'): frozenset({
            'manet-atak.py',
            'manet-positioning.py',
            'mesh-service-election.py',
    }),
    # UNRELATED: config and radio authenticated payload contracts.
    ('python', 'publish_ack'): frozenset({
            'mesh-config-sync.py',
            'mesh-radio-state.py',
    }),
    # UNRELATED contracts: shapes, limits, exceptions or command I/O.
    ('python', 'read_json'): frozenset({
            'manet-atak.py',
            'manet-mesh-census.py',
            'manet-positioning.py',
            'manet_ap_mesh.py',
            'manet_config_io.py',
            'mesh-channel-agreement.py',
            'mesh-radio-state.py',
    }),
    # UNRELATED: radio naming versus address/service reconciliation.
    ('python', 'reconcile'): frozenset({
            'manet-radio-names.py',
            'manet_ip_runtime.py',
    }),
    # UNRELATED: APT/nft execution and subprocess wrapper contracts.
    ('python', 'run'): frozenset({
            'manet-dhcp-isolation.py',
            'manet-os-cleanup.py',
            'mesh-config-sync.py',
            'mesh-radio-state.py',
    }),
    # UNRELATED: service/VIP state versus chrony synchronization.
    ('python', 'settled'): frozenset({
            'manet_election_runtime.py',
            'mesh-time-sync.py',
    }),
    # UNRELATED: cgroup counters, enumeration and ACS channel state.
    ('python', 'snapshot'): frozenset({
            'manet-cpu-sample.py',
            'manet-wait-radios.py',
            'manet_acs_agreement.py',
    }),
    # STANDALONE: saved updater recovery and pre-coldplug firmware.
    ('python', 'sync_directory'): frozenset({
            'manet-mt7916-firmware.py',
            'node-update.py',
    }),
    # UNRELATED: config and radio transaction/activation workflows.
    ('python', 'sync_once'): frozenset({
            'mesh-config-sync.py',
            'mesh-radio-state.py',
    }),
    # UNRELATED: APT cleanup versus ACS channel/time validation.
    ('python', 'validate_plan'): frozenset({
            'manet-os-cleanup.py',
            'manet_acs_agreement.py',
    }),
    # UNRELATED: GPS bool/error handling versus private radio state.
    ('python', 'write_json'): frozenset({
            'gps-reader.py',
            'mesh-radio-state.py',
    }),
    # UNRELATED: pending JSON versus key/value config with a default.
    ('shell', 'cfg_get'): frozenset({
            'mesh-config-apply.sh',
            'usb-wifi-uplink.sh',
    }),
    # UNRELATED: accumulate power errors versus terminate peer query.
    ('shell', 'fail'): frozenset({
            'manet-mesh-power.sh',
            'mesh-peer-count.sh',
    }),
    # UNRELATED: per-program tags, sinks and error handling.
    ('shell', 'log'): frozenset({
            'batman-enslave-watch.sh',
            'channel-election.sh',
            'ethernet-autodetect.sh',
            'gateway-route-manager.sh',
            'limp-mode-manager.sh',
            'manet-ui-firewall.sh',
            'manet-uplink-dispatch.sh',
            'manet-uplink-speed.sh',
            'mediamtx-election.sh',
            'mesh-clone-identity.sh',
            'mesh-config-apply.sh',
            'mesh-config-rollback.sh',
            'mesh-ip-manager.sh',
            'mesh-shutdown.sh',
            'mumble-election.sh',
            'node-manager-acs.sh',
            'node-manager-static.sh',
            'quorum-checker.sh',
            'sae-watchdog.sh',
            'ssh-recovery.sh',
            'syncthing-peer-manager.sh',
            'tourguide-manager.sh',
            'usb-ethernet-watch.sh',
            'usb-wifi-halow-recovery.sh',
            'usb-wifi-uplink.sh',
    }),
    # UNRELATED: independent CLI arguments and exit behavior.
    ('shell', 'usage'): frozenset({
            'batman-if-setup.sh',
            'channel-election.sh',
            'ethernet-autodetect.sh',
            'mac-to-ip.sh',
            'manet-cpu-policy.sh',
            'manet-ipcalc.sh',
            'manet-power-status.sh',
            'manet-uplink-dispatch.sh',
            'manet-uplink-speed.sh',
            'manet-user-scripts.sh',
            'mesh-config-apply.sh',
            'mesh-config-rollback.sh',
            'node-manager-select.sh',
            'usb-wifi-uplink.sh',
    }),
}


def shell_function_names(source):
    """Find declarations outside shell strings, comments and here-documents."""
    tokens = re.compile(
        r"(?P<doc>(?<!<)<<(?P<tabs>-?)\s*"
        r"(?P<quote>['\"]?)(?P<delimiter>[\w-]+)(?P=quote))"
        r"|\$\(\(.*?\)\)|'[^']*'|\"(?:\\.|[^\"\\])*\""
        r"|`(?:\\.|[^`\\])*`|\\[^\n]|(?<![\w$}])#[^\n]*|(?P<line>\n)",
        re.S)
    parts, pending = [], []
    position = 0
    while match := tokens.search(source, position):
        parts.append(source[position:match.start()])
        parts.append('\n' if match['line'] else ' ')
        position = match.end()
        if match['doc']:
            pending.append((match['delimiter'], bool(match['tabs'])))
        if match['line']:
            # Skip data before lexing it; a quote inside Python or generated
            # config must not swallow declarations after the terminator.
            for delimiter, tabs in pending:
                while position < len(source):
                    end = source.find('\n', position)
                    end = len(source) if end < 0 else end + 1
                    line = source[position:end].rstrip('\r\n')
                    position = end
                    if (line.lstrip('\t') if tabs else line) == delimiter:
                        break
            pending.clear()
    parts.append(source[position:])
    text = ''.join(parts)
    declarations = re.finditer(
        r'(?:^|[;{}\n])[ \t]*'
        r'(?:function[ \t]+([\w.-]+)(?:\s*\(\s*\))?'
        r'|([\w.-]+)\s*\(\s*\))\s*(?:\{|\()', text)
    return {match[1] or match[2] for match in declarations}

def duplicate_production_names(directory):
    """Collect duplicate Python top-level names and shell declarations."""
    names = defaultdict(set)
    for path in sorted(directory.rglob('*')):
        if (not path.is_file()
                or 'testdata' in path.relative_to(directory).parts):
            continue
        relative = str(path.relative_to(directory))
        if (path.suffix == '.py' and not path.name.startswith('test_')
                and not path.name.endswith('_pb2.py')):
            for node in ast.parse(path.read_text()).body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                    names['python', node.name].add(relative)
        elif path.suffix == '.sh':
            for name in shell_function_names(path.read_text()):
                names['shell', name].add(relative)
    return {key: files for key, files in names.items() if len(files) > 1}


class DuplicateHelperTests(unittest.TestCase):
    """Reject new copies without exempting unrelated files by name alone."""

    def test_production_names_have_one_owner(self):
        """Require each remaining duplicate to match its reviewed file set."""
        duplicates = duplicate_production_names(MANET / 'node_tools')
        unexpected = {
            key: sorted(files) for key, files in duplicates.items()
            if files != DUPLICATE_NAME_ALLOWLIST.get(key)
        }
        self.assertEqual(unexpected, {},
                         'Duplicate production names need a shared owner')
        self.assertEqual(set(DUPLICATE_NAME_ALLOWLIST) - set(duplicates), set(),
                         'Remove stale duplicate-name exemptions')

    def test_python_scope_and_generated_exclusions(self):
        """Ignore methods/tests/generated files; include async and classes."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'a.py').write_text(
                'def helper(): pass\nclass Record:\n'
                '    def method(self): pass\nasync def worker(): pass\n')
            (root / 'b.py').write_text(
                'def helper(): pass\nclass Record:\n'
                '    def method(self): pass\nasync def worker(): pass\n')
            for name in ('test_helpers.py', 'fixture_pb2.py'):
                (root / name).write_text('def helper(): pass\n')
            (root / 'testdata').mkdir()
            (root / 'testdata/fixture.py').write_text('def helper(): pass\n')
            self.assertEqual(duplicate_production_names(root), {
                ('python', name): {'a.py', 'b.py'}
                for name in ('helper', 'Record', 'worker')})

    def test_shell_declaration_forms_and_data(self):
        """Include keyword, multiline and nested functions, excluding data."""
        source = '''#!/bin/bash
# commented() { :; }
printf '%s' 'quoted() { :; }'
printf '%s' "<<FAKE"
cat <<'PYTHON'
embedded() { :; }
unmatched='
PYTHON
value=$((1 << 24))
one() { :; }
function two { :; }
function three() { :; }
four()
{
    nested() { :; }
}
five() ( : )
'''
        self.assertEqual(shell_function_names(source),
                         {'one', 'two', 'three', 'four', 'nested', 'five'})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('a.sh', 'b.sh'):
                (root / name).write_text(source)
            self.assertEqual(duplicate_production_names(root), {
                ('shell', name): {'a.sh', 'b.sh'} for name in
                ('one', 'two', 'three', 'four', 'nested', 'five')})

    def test_group_one_helper_is_never_allowlisted(self):
        """Detect a copied radio helper in the management module."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = 'def _format_halow_bw(value):\n    return value\n'
            for name in ('manet_radio.py', 'manet_manage.py'):
                (root / name).write_text(source)
            duplicates = duplicate_production_names(root)
            self.assertEqual(duplicates, {
                ('python', '_format_halow_bw'):
                    {'manet_radio.py', 'manet_manage.py'}})
            self.assertNotIn(('python', '_format_halow_bw'),
                             DUPLICATE_NAME_ALLOWLIST)

    def test_flat_installed_imports_and_shell_sources(self):
        """Load changed callers from a flat bin with no checkout on sys.path."""
        import os
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = root / 'usr/local/bin'
            installed.mkdir(parents=True)
            for path in (MANET / 'node_tools').iterdir():
                if (path.is_file() and path.suffix in ('.py', '.sh')
                        and not path.name.startswith('test_')):
                    shutil.copy2(path, installed / path.name)
            modules = ('manet_manage', 'mesh-status', 'manet_recovery_status',
                       'manet_registry_builder', 'manet_config_io',
                       'manet_registry', 'manet_node_info', 'manet_radio',
                       'mesh-radio-state')
            runner = installed / 'import-check.py'
            runner.write_text(
                'import runpy\nfrom pathlib import Path\n' +
                'for name in ' + repr(modules) + ':\n'
                '    runpy.run_path(str(Path(__file__).with_name(name + '
                '".py")), run_name="installed_check")\n')
            env = dict(os.environ)
            env.pop('PYTHONPATH', None)
            env.pop('MANET_TOOLS_DIR', None)
            result = subprocess.run([sys.executable, '-s', str(runner)],
                                    cwd=root, env=env, capture_output=True,
                                    text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            callers = {
                'node-manager-acs.sh': 'run_service_elections',
                'node-manager-static.sh': 'get_current_freq',
                'manet-ipcalc.sh': 'int_to_ip',
                'mesh-ip-manager.sh': 'int_to_ip',
                'batman-if-setup.sh': 'iface_driver',
                'radio-setup.sh': 'iface_supports_mesh',
                'ethernet-autodetect.sh': 'internet_probe',
                'manet-uplink-dispatch.sh': 'is_usb_wifi_uplink_iface',
                'usb-wifi-uplink.sh': 'iface_driver',
                'gateway-route-manager.sh': 'uptime_now',
                'mesh-config-rollback.sh': 'uptime_now',
            }
            for name, helper in callers.items():
                lines = iter((installed / name).read_text().splitlines())
                imports = []
                for line in lines:
                    if not line.startswith('. '):
                        continue
                    imports.append(line)
                    while line.endswith(('\\', '||')):
                        line = next(lines)
                        imports.append(line)
                probe = installed / 'source-check.sh'
                probe.write_text('#!/bin/bash\n' + '\n'.join(imports) +
                                 '\ndeclare -F ' + helper + ' >/dev/null\n')
                result = subprocess.run(['bash', str(probe)], cwd=root,
                                        env=env, capture_output=True,
                                        text=True, timeout=5)
                self.assertEqual(result.returncode, 0, name + result.stderr)


if __name__ == '__main__':
    unittest.main()
