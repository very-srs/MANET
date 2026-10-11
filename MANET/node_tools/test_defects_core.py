"""Regressions for core telemetry, registry, consensus and service addresses."""

import base64
import contextlib
import hashlib
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import decoder
import manet_election_runtime
import manet_registry
import manet_registry_builder


TOOLS = Path(__file__).resolve().parent
OWN = '02:00:00:00:00:01'
PEER = '02:00:00:00:00:02'
SPEC = importlib.util.spec_from_file_location(
    'core_tourguide', TOOLS / 'mesh-tourguide-election.py')
TOURGUIDE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOURGUIDE)


class CoreHarness(unittest.TestCase):
    """Keep registry files and shell side effects inside temporary storage."""

    def setUp(self):
        """Create isolated files and command overrides."""
        scratch = tempfile.TemporaryDirectory(prefix='manet-core-')
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.registry = self.root / 'registry'
        self.uptime = self.root / 'uptime'
        self.uptime.write_text('1000.25 0.00\n')
        self.env = dict(os.environ, CORE_ROOT=str(self.root),
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        MESH_UPTIME_FILE=str(self.uptime))

    def stub(self, name, body):
        """Install one harmless executable in the fixture PATH."""
        path = self.bin / name
        path.write_text('#!/bin/bash\n' + body)
        path.chmod(0o755)
        return path

    def shell(self, source, env=None):
        """Execute real shell code with isolated inputs and a time limit."""
        return subprocess.run(['/bin/bash', '-c', source], text=True,
                              capture_output=True, env=env or self.env,
                              timeout=10)

    def encode(self, *args):
        """Use the shipped encoder CLI to create real NodeTelemetry bytes."""
        return subprocess.check_output(
            [sys.executable, str(TOOLS / 'encoder.py'), 'telemetry',
             '--timestamp', '1000', *args], text=True).strip()


class TourguideFrequencyTests(CoreHarness):
    """Wire-format frequency strings must remain eligible after decoding."""

    def registry_text(self, interfaces):
        """Serialize candidate interfaces using the real registry quoting."""
        value = decoder.shell_quote(json.dumps(interfaces))
        return ''.join(
            f"NODE_{mac.replace(':', '')}_INTERFACES_JSON='{value}'\n"
            for mac in (OWN, PEER))

    def test_telemetry_round_trip_elects_each_wifi_band(self):
        """Encode and decode all radios before exercising the real election."""
        interfaces = [dict(name=name, role='mesh', state='UP', freq_mhz=freq)
                      for name, freq in [('wlan0', '2437'), ('wlan1', 5180),
                                         ('wlan2', '907')]]
        payload = self.encode('--interfaces-json', json.dumps(interfaces))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            decoder.decode_telemetry(base64.b64decode(payload))
        rows = output.getvalue().splitlines()
        registry = ''.join(f"NODE_{mac.replace(':', '')}_{row}\n"
                           for mac in (OWN, PEER) for row in rows)
        decoded = manet_registry_builder.decode('telemetry', payload, OWN)
        self.assertEqual([i['freq_mhz'] for i in
                          json.loads(decoded['INTERFACES_JSON'])],
                         ['2437', '5180', '907'])
        for band in ('2.4', '5'):
            with self.subTest(band=band):
                self.assertEqual(TOURGUIDE.elect(OWN, [PEER], registry, band),
                                 OWN)

    def test_invalid_frequencies_and_ineligible_radios_do_not_hop(self):
        """Ignore malformed, non-finite, down and wrong-band interfaces."""
        for freq in ('bad', '', None, [], {}, 'nan', 'inf', '-inf', '1e999',
                     float('nan'), float('inf'), '907', 2500, 5900):
            with self.subTest(freq=freq):
                registry = self.registry_text([
                    dict(role='mesh', state='UP', freq_mhz=freq),
                    dict(role='ap', state='UP', freq_mhz='2437'),
                    dict(role='mesh', state='DOWN', freq_mhz='2437')])
                with self.assertRaisesRegex(ValueError, 'scheduled band'):
                    TOURGUIDE.elect(OWN, [PEER], registry, '2.4')
        for freq in (2437, 2437.5, '2437.5'):
            with self.subTest(valid=freq):
                registry = self.registry_text([
                    dict(role='mesh', state='UP', freq_mhz=freq)])
                self.assertEqual(TOURGUIDE.elect(OWN, [PEER], registry, '2.4'),
                                 OWN)


class RegistryQuotingTests(CoreHarness):
    """Read exactly the quoting emitted by both registry writers."""

    def test_decoder_writer_preserves_apostrophes_and_literal_shell(self):
        """Treat quote breaks and shell-looking text as literal data."""
        for value in ("Mike's AP", "''", "ends'", "'starts", '',
                      "back\\slash's $(false); `false` = x\nnext line"):
            with self.subTest(value=value):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    decoder.emit('NODE_020000000001_AP_SSID', value)
                self.registry.write_text(output.getvalue())
                self.assertEqual(manet_registry.parse_registry(self.registry)
                                 ['020000000001']['AP_SSID'], value)

    def test_builder_round_trip_preserves_ap_ssid(self):
        """Build a real registry from encoded Alfred telemetry."""
        ssid = "Mike's AP"
        payload = self.encode('--ap-ssid', ssid)
        snapshots = [subprocess.CompletedProcess([], 0, stdout=''),
                     subprocess.CompletedProcess(
                         [], 0, stdout=f'{{ "{OWN}", "{payload}" }},\n')]
        with patch.dict(os.environ, self.env), patch.object(
                manet_registry_builder.subprocess, 'run',
                side_effect=snapshots):
            manet_registry_builder._build(
                self.registry, self.root / 'claims', self.root / 'observed')
        self.assertEqual(manet_registry.parse_registry(self.registry)
                         ['020000000001']['AP_SSID'], ssid)

    def test_unquoted_or_trailing_shell_syntax_is_not_a_registry_value(self):
        """Reject assignments outside the writers' exact quoting grammar."""
        self.registry.write_text(
            "NODE_020000000001_AP_SSID='valid'\n"
            "NODE_020000000001_AP_SSID='partial'; false\n"
            "NODE_020000000001_HOSTNAME=unquoted\n"
            "# NODE_020000000001_AP_SSID='comment'\n")
        self.assertEqual(manet_registry.parse_registry(self.registry),
                         {'020000000001': {'id': '020000000001',
                                           'AP_SSID': 'valid'}})


class LimpConsensusTests(CoreHarness):
    """The numerator and denominator must describe the same fresh nodes."""

    def test_stale_votes_cannot_create_or_hold_consensus(self):
        """Exercise entry and exit with stale, missing and invalid ages."""
        self.stub('systemd-cat', 'cat >> "$CORE_ROOT/journal"\n')
        self.stub('iw', 'exit 0\n')
        state = self.root / 'limp.state'
        env = dict(self.env, MESH_REGISTRY_FILE=str(self.registry),
                   MANET_LIMP_STATE_FILE=str(state),
                   MANET_ACS_LOCK_FILE=str(self.root / 'lock'))
        rows = []
        for i, (age, vote) in enumerate([
                ('999', 'true'), ('401', 'false'), ('400', 'true'),
                ('100', 'true'), (None, 'true'), ('bad', 'true')]):
            prefix = f'NODE_0200000000{i:02x}'
            fields = [f"{prefix}_IS_IN_LIMP_MODE='{vote}'"]
            if age is not None:
                fields.append(f"{prefix}_OBSERVED_AT_UPTIME='{age}'")
            rows.extend(fields if i % 2 else reversed(fields))
        self.registry.write_text('\n'.join(rows) + '\n')
        source = (TOOLS / 'limp-mode-manager.sh').read_text()
        source = source.replace('/var/lib/', str(self.root) + '/')
        for already_limp in (False, True):
            with self.subTest(already_limp=already_limp):
                state.unlink(missing_ok=True)
                if already_limp:
                    state.write_text('600\n')
                result = self.shell(source, env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(state.exists())
                self.assertIn('1/2 (.50)', (self.root / 'journal').read_text())
        state.unlink(missing_ok=True)
        self.registry.write_text(self.registry.read_text().replace(
            "IS_IN_LIMP_MODE='false'", "IS_IN_LIMP_MODE='true'"))
        self.assertEqual(self.shell(source, env).returncode, 0)
        self.assertEqual(state.read_text(), '1000\n')


class QuorumFreshnessTests(CoreHarness):
    """Shutdown deductions use the same fresh observations as active count."""

    def test_stale_shutdowns_cannot_hide_six_node_quorum_failure(self):
        self.stub('systemd-cat', 'cat >> "$CORE_ROOT/journal"\n')
        batctl = self.stub('batctl', '''
printf '%s\n' '[{"orig_address":"02:00:00:00:00:02"}]'
''')
        env = dict(self.env, REGISTRY_STATE_FILE=str(self.registry),
                   BATCTL_PATH=str(batctl))
        active = ''.join(
            f"NODE_{i:012x}_OBSERVED_AT_UPTIME='999'\n"
            f"NODE_{i:012x}_NODE_STATE='ACTIVE'\n" for i in range(6))
        for age, expected, shutdowns in ((None, 1, 0), ('399', 1, 0),
                                         ('400', 1, 0), ('401', 1, 4),
                                         ('bad', 1, 0)):
            for reverse in (False, True):
                with self.subTest(observed=age, reverse=reverse):
                    rows = []
                    for i in range(6, 10):
                        fields = [f"NODE_{i:012x}_NODE_STATE='SHUTTING_DOWN'"]
                        if age is not None:
                            fields.append(f"NODE_{i:012x}_OBSERVED_AT_UPTIME='{age}'")
                        rows.extend(reversed(fields) if reverse else fields)
                    self.registry.write_text(active + '\n'.join(rows) + '\n')
                    result = subprocess.run(
                        ['bash', str(TOOLS / 'quorum-checker.sh')], env=env,
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertIn(f'Shutdown={shutdowns}\n',
                                  (self.root / 'journal').read_text())
                    (self.root / 'journal').unlink()


class StaticPublishTests(CoreHarness):
    """A failed send leaves telemetry due on the next manager pass."""

    def test_failed_publish_retries_then_success_starts_interval(self):
        """Execute the real telemetry block through failure and recovery."""
        encoder = self.stub('encoder', "printf '%s' telemetry\n")
        self.stub('alfred', '''cat >/dev/null
count=$(cat "$CORE_ROOT/sends" 2>/dev/null || echo 0)
count=$((count + 1))
printf '%s\n' "$count" > "$CORE_ROOT/sends"
[ "$count" -gt 1 ]
''')
        (self.root / 'my_ipv4_chunk').write_text('0\n')
        (self.root / 'loadavg').write_text('0.10 0.10 0.10\n')
        source = (TOOLS / 'node-manager-static.sh').read_text().split(
            '# === PUBLISH TELEMETRY (Alfred type 68) ===', 1)[1].split(
            '# Bootstrap only after', 1)[0]
        for prefix in ('/var/run/', '/proc/'):
            source = source.replace(prefix, str(self.root) + '/')
        setup = '''
log() { :; }
detect_and_update_gateway_state() { :; }
is_ntp_time_source() { return 1; }
is_hosting_service() { return 1; }
is_hosting_mumble_service() { return 1; }
collect_radio_mcs() { :; }
collect_interfaces_json() { printf '[]'; }
collect_ap_ssid() { :; }
LAST_PUBLISH_TIME=100
for MONO in 1000 1015 1030; do
'''
        env = dict(self.env, ENCODER_PATH=str(encoder), THROUGHPUT_MEAN='true',
                   PUBLISH_INTERVAL='180', NOW='1000', ALFRED_DATA_TYPE='68',
                   GPS_STATUS_FILE=str(self.root / 'missing-gps'))
        result = self.shell(setup + source + '''
printf '%s\n' "$LAST_PUBLISH_TIME"
done
''', env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['100', '1015', '1015'])
        self.assertEqual((self.root / 'sends').read_text(), '2\n')


class ServiceVipTests(CoreHarness):
    """Every service-address consumer must use the same hash algorithm."""

    def helper(self, script, hashes):
        """Run the actual helper with controlled installed hash commands."""
        for command in ('grep', 'awk', 'sed', 'cut', 'md5sum', 'sha256sum'):
            target = self.bin / command
            target.unlink(missing_ok=True)
            if command not in ('md5sum', 'sha256sum') or command in hashes:
                target.symlink_to(shutil.which(command))
        config = self.root / 'radvd.conf'
        config.write_text('prefix fd01:ed20:ecb4::/48 {};\n')
        source = (TOOLS / script).read_text().replace(
            '/etc/radvd-mesh.conf', str(config))
        env = dict(self.env, PATH=str(self.bin))
        return self.shell(source, env), source

    def test_hash_availability_cannot_select_another_vip(self):
        """Require MD5 and refuse a SHA-only address derivation."""
        for script, service, base in [('mtx-ip.sh', 'mediamtx', 100),
                                      ('mumble-ip.sh', 'mumble', 400)]:
            for hashes in (('md5sum', 'sha256sum'), ('md5sum',),
                           ('sha256sum',)):
                with self.subTest(script=script, hashes=hashes):
                    result, _ = self.helper(script, hashes)
                    if 'md5sum' not in hashes:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(result.stdout, '')
                        self.assertIn('md5sum', result.stderr)
                        continue
                    digest = hashlib.md5(
                        f'fd01:ed20:ecb4:0000-{service}'.encode()).digest()
                    expected = f'fd01:ed20:ecb4:0000::{base + digest[0]:x}/128'
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), expected)

    def test_python_vip_consumer_reuses_shell_derivation(self):
        """Exercise the cached Python caller against the real shell helper."""
        result, source = self.helper('mtx-ip.sh', ('md5sum', 'sha256sum'))
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.bin / 'bash').symlink_to('/bin/bash')
        (self.root / 'mtx-ip.sh').write_text(source)
        manet_election_runtime.ipv6_vip.cache_clear()
        self.addCleanup(manet_election_runtime.ipv6_vip.cache_clear)
        with patch.object(manet_election_runtime, 'TOOLS', self.root), \
                patch.dict(os.environ, PATH=str(self.bin)):
            vip = manet_election_runtime.ipv6_vip(b'test prefix', 'test helper')
        self.assertEqual(vip, str(ipaddress.IPv6Interface(
            result.stdout.strip()).ip))


if __name__ == '__main__':
    unittest.main()
