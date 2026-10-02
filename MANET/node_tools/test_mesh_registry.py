#!/usr/bin/env python3
"""Exercise chunk claims through the real Alfred encoder, decoder and registry."""

import base64
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

import NodeInfo_pb2
from manet_ids import int_to_ipv4


TOOLS = Path(__file__).resolve().parent
MAC = '02:00:00:00:00:01'
A6 = (10 << 24) + (30 << 16) + 6   # 10.30.0.6 as an integer


class ChunkClaimsTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.records = self.root / 'records'
        self.records.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.registry = self.root / 'registry'
        self.claims = self.root / 'claims'
        self.env = dict(
            os.environ,
            PATH=os.pathsep.join((str(self.bin), str(Path(sys.executable).parent),
                                 str(TOOLS), os.environ.get('PATH', ''))),
            MESH_REGISTRY_FILE=str(self.registry),
            MESH_CLAIMED_CHUNKS_FILE=str(self.claims),
            MESH_DECODER_PATH=str(TOOLS / 'decoder.py'),
            MESH_REGISTRY_STALE_AFTER='300',
            MESH_REGISTRY_OBSERVED_FILE=str(self.root / 'observed' / 'observed.tsv'),
            MESH_UPTIME_FILE=str(self.root / 'uptime'),
            REVIEW_RECORDS=str(self.records),
        )
        self.set_uptime(1000)
        alfred = self.bin / 'alfred'
        alfred.write_text(
            '#!/bin/bash\n'
            'case "$1" in\n'
            '  -r) cat "$REVIEW_RECORDS/$2" ;;\n'
            '  -s) if [ -f "$REVIEW_RECORDS/fail-publish-once" ]; then\n'
            '        rm "$REVIEW_RECORDS/fail-publish-once"; cat >/dev/null; exit 1\n'
            '      fi\n'
            '      cat > "$REVIEW_RECORDS/published-$2" ;;\n'
            '  *) exit 1 ;;\n'
            'esac\n'
        )
        alfred.chmod(0o755)
        for kind in (67, 68):
            (self.records / str(kind)).write_text('')

    def encode(self, kind, *args):
        return subprocess.check_output(
            [sys.executable, str(TOOLS / 'encoder.py'), kind, *args], text=True,
        )

    def set_uptime(self, seconds):
        (self.root / 'uptime').write_text(f'{seconds}.25 {seconds * 3}.50\n')

    def add_node(self, mac=MAC, chunk=0, ip='10.30.0.6', age=0,
                 state='ACTIVE', identity=True, size=7):
        if identity:
            args = ['--hostname', 'mesh-' + mac[-2:], '--mac-addresses', mac,
                    '--ipv4-chunk', str(chunk), '--ipv4-chunk-size', str(size)]
            if ip:
                args += ['--ipv4-address', ip]
            payload = self.encode('identity', *args)
            with (self.records / '67').open('a') as record:
                record.write(f'{{ "{mac}", "{payload}" }},\n')
        payload = self.encode('telemetry', '--timestamp', str(int(time.time()) - age),
                              '--node-state', state)
        with (self.records / '68').open('a') as record:
            record.write(f'{{ "{mac}", "{payload}" }},\n')

    def build_registry(self):
        result = subprocess.run(
            ['bash', str(TOOLS / 'mesh-registry-builder.sh')], env=self.env,
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.claims.read_text().splitlines()

    def allocator(self, command, network='10.30.0.0/28', chunk_size=7):
        # Load only the existing function definitions, avoiding live interface
        # configuration. Exercise allocation against the registry's real output.
        source = (TOOLS / 'mesh-ip-manager.sh').read_text()
        functions = source.split('# --- Helper Functions ---\n', 1)[1]
        functions = functions.split('# --- Main Logic ---\n', 1)[0]
        env = dict(self.env, IPV4_NETWORK=network, CHUNK_SIZE=str(chunk_size),
                   SERVICES_RESERVED='5', CLAIMED_CHUNKS_FILE=str(self.claims))
        return subprocess.run(['bash', '-c', functions + '\n' + command],
                              env=env, capture_output=True, text=True, timeout=5)

    def test_chunk_zero_is_claimed_and_allocator_cannot_reuse_it(self):
        self.add_node()
        self.assertEqual(self.build_registry(), [f'0,{MAC},{A6},7'])
        # A /28 has room for only one seven-address chunk after reservations.
        result = self.allocator('get_random_chunk')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('No available chunks', result.stderr)

    def test_chunk_zero_starts_after_reserved_service_addresses(self):
        result = self.allocator('get_chunk_ips 0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(),
                         '10.30.0.6:10.30.0.7:10.30.0.8:10.30.0.12')

    def test_both_owners_of_chunk_zero_reach_the_conflict_list(self):
        self.add_node()
        self.add_node(mac='02:00:00:00:00:02')
        self.assertEqual(self.build_registry(),
                         [f'0,{MAC},{A6},7', f'0,02:00:00:00:00:02,{A6},7'])

    def test_nonzero_chunk_still_claimed(self):
        self.add_node(chunk=1, ip='10.30.0.13')
        self.assertEqual(self.build_registry(), [f'1,{MAC},{A6 + 7},7'])

    def test_unknown_or_inactive_allocations_are_excluded(self):
        self.add_node(mac='02:00:00:00:00:02', ip='')
        self.add_node(mac='02:00:00:00:00:03', identity=False)
        self.add_node(mac='02:00:00:00:00:05', state='SHUTTING_DOWN')
        self.assertEqual(self.build_registry(), [])

    def test_chunk_zero_survives_cached_identity(self):
        self.add_node()
        self.assertEqual(self.build_registry(), [f'0,{MAC},{A6},7'])
        (self.records / '67').write_text('')
        self.assertEqual(self.build_registry(), [f'0,{MAC},{A6},7'])

    def test_failed_alfred_read_preserves_previous_claims_and_registry(self):
        self.add_node()
        self.build_registry()
        previous = self.registry.read_bytes(), self.claims.read_bytes()
        for kind in (67, 68):
            with self.subTest(kind=kind):
                record = self.records / str(kind)
                contents = record.read_text()
                record.unlink()
                result = subprocess.run(['bash', str(TOOLS / 'mesh-registry-builder.sh')],
                                        env=self.env, capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.registry.read_bytes(), self.claims.read_bytes()), previous)
                record.write_text(contents)

    def test_saved_chunk_recovery_respects_peer_claims(self):
        source = (TOOLS / 'mesh-ip-manager.sh').read_text()
        selection = source.split('# --- State Machine ---\n', 1)[1].split('        # Get chunk IPs', 1)[0]
        selection += 'printf "%s\\n" "$PROPOSED_CHUNK"\n;;\nesac\n'
        setup = ('IPV4_NETWORK=10.30.0.0/27\nIPV4_STATE=UNCONFIGURED\n'
                 'PERSISTENT_CHUNK=0\nPERSISTENT_IPV4=10.30.0.6\n'
                 'mac_is_local() { [ "$1" = "' + MAC + '" ]; }\n'
                 'load_claims\n')
        # The /27 has three chunks. The saved chunk and one alternative are
        # taken, leaving exactly chunk 2. Own cached advertisements are ignored.
        self.claims.write_text(f'0,02:00:00:00:00:02,{A6},7\n'
                               f'1,02:00:00:00:00:03,{A6 + 7},7\n')
        result = self.allocator(setup + selection)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '2')
        self.claims.write_text(f'0,{MAC},{A6},7\n')
        result = self.allocator(setup + selection)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '0')

    def registry_value(self, mac, key):
        prefix = 'NODE_' + mac.replace(':', '') + '_' + key + "='"
        for line in self.registry.read_text().splitlines():
            if line.startswith(prefix):
                return line[len(prefix):-1]
        return None

    def test_sender_clock_does_not_decide_freshness(self):
        # A node without an RTC can boot days behind; it is still alive.
        self.add_node(age=100000)
        self.add_node(mac='02:00:00:00:00:02', ip='10.30.0.13', chunk=1, age=-100000)
        self.assertEqual(self.build_registry(),
                         [f'0,{MAC},{A6},7', f'1,02:00:00:00:00:02,{A6 + 7},7'])
        self.assertEqual(self.registry_value(MAC, 'NODE_STATE'), 'ACTIVE')
        self.assertEqual(self.registry_value(MAC, 'OBSERVED_AGE_SECONDS'), '0')

    def test_unchanged_payload_ages_on_local_clock_and_change_refreshes(self):
        self.add_node()
        self.build_registry()
        self.set_uptime(1250)
        self.assertEqual(self.build_registry(), [f'0,{MAC},{A6},7'])
        self.assertEqual(self.registry_value(MAC, 'OBSERVED_AGE_SECONDS'), '250')
        self.set_uptime(1301)
        self.assertEqual(self.build_registry(), [])
        self.assertEqual(self.registry_value(MAC, 'NODE_STATE'), 'STALE')
        # Any republish changes the payload (it carries a timestamp).
        (self.records / '68').write_text('')
        self.add_node(age=5)
        self.assertEqual(self.build_registry(), [f'0,{MAC},{A6},7'])
        self.assertEqual(self.registry_value(MAC, 'NODE_STATE'), 'ACTIVE')

    def test_reappearing_identical_record_keeps_its_age(self):
        self.add_node()
        self.build_registry()
        saved = {kind: (self.records / str(kind)).read_text() for kind in (67, 68)}
        for kind in (67, 68):
            (self.records / str(kind)).write_text('')
        self.set_uptime(1100)
        self.assertEqual(self.build_registry(), [])
        for kind, text in saved.items():
            (self.records / str(kind)).write_text(text)
        self.set_uptime(1400)
        self.assertEqual(self.build_registry(), [])
        self.assertEqual(self.registry_value(MAC, 'OBSERVED_AGE_SECONDS'), '400')
        # Tombstones are forgotten once well past Alfred's own expiry.
        for kind in (67, 68):
            (self.records / str(kind)).write_text('')
        self.set_uptime(2400)
        self.build_registry()
        self.assertNotIn(MAC, (self.root / 'observed' / 'observed.tsv').read_text())

    def test_failed_read_keeps_observations(self):
        self.add_node()
        self.build_registry()
        observed = self.root / 'observed' / 'observed.tsv'
        before = observed.read_text()
        (self.records / '68').unlink()
        self.set_uptime(1200)
        result = subprocess.run(['bash', str(TOOLS / 'mesh-registry-builder.sh')],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(observed.read_text(), before)

    def test_mixed_block_sizes_are_compared_as_address_ranges(self):
        # Peer provisioned for 5 EUDs holds 10.30.0.13-19. With our 3-address
        # blocks in a /27, local chunks 2, 3 and 4 overlap it.
        self.claims.write_text(f'1,02:00:00:00:00:02,{A6 + 7},7\n')
        script = ('for i in 0 1 2 3 4 5 6 7; do chunk_claimed_by_peer $i || printf "%s " $i; done\n'
                  'range_claimed_by_peer $(( ' + str(A6) + ' + 9 )) $(( ' + str(A6) + ' + 11 ))\n')
        result = self.allocator(script, network='10.30.0.0/27', chunk_size=3)
        self.assertEqual(result.stdout.split(), ['0', '1', '5', '6', '7', '02:00:00:00:00:02'],
                         result.stderr)
        for _ in range(20):
            result = self.allocator('get_random_chunk', network='10.30.0.0/27', chunk_size=3)
            self.assertIn(result.stdout.strip(), {'0', '1', '5', '6', '7'}, result.stderr)

    def test_claim_without_block_size_defers_allocation(self):
        # Guessing our own size would recreate the mixed-size overlap.
        source = (TOOLS / 'mesh-ip-manager.sh').read_text()
        selection = source.split('# --- State Machine ---\n', 1)[1].split('        # Get chunk IPs', 1)[0]
        selection += 'printf "%s\\n" "$PROPOSED_CHUNK"\n;;\nesac\n'
        setup = 'IPV4_STATE=UNCONFIGURED\nload_claims\n'
        for line in (f'1,02:00:00:00:00:02,{A6 + 7},0', '1,02:00:00:00:00:02'):
            with self.subTest(line=line):
                self.claims.write_text(line + '\n')
                result = self.allocator(setup + selection, network='10.30.0.0/27')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('Deferring allocation', result.stderr)
        # Its known primary still counts as a conflict for an existing block.
        self.claims.write_text(f'1,02:00:00:00:00:02,{A6 + 7},0\n')
        result = self.allocator(f'load_claims; range_claimed_by_peer {A6 + 6} {A6 + 12}',
                                network='10.30.0.0/27')
        self.assertEqual(result.stdout.strip(), '02:00:00:00:00:02')

    def test_implausible_claims_are_incomplete_not_ignored(self):
        source = (TOOLS / 'mesh-ip-manager.sh').read_text()
        selection = source.split('# --- State Machine ---\n', 1)[1].split('        # Get chunk IPs', 1)[0]
        selection += 'printf "%s\\n" "$PROPOSED_CHUNK"\n;;\nesac\n'
        for line in (f'1,02:00:00:00:00:02,{A6 + 7},256', '1,02:00:00:00:00:02,,7',
                     '1,02:00:00:00:00:02,99999999999,7'):
            with self.subTest(line=line):
                self.claims.write_text(line + '\n')
                result = self.allocator('IPV4_STATE=UNCONFIGURED\nload_claims\n' + selection,
                                        network='10.30.0.0/27')
                self.assertEqual(result.stdout, '', result.stderr)
                self.assertIn('Deferring allocation', result.stderr)

    def test_out_of_range_advertised_address_becomes_incomplete_claim(self):
        # The decoder only ever emits dotted quads; simulate a corrupted cache.
        self.add_node()
        self.build_registry()
        text = self.registry.read_text().replace("IPV4_ADDRESS='10.30.0.6'", "IPV4_ADDRESS='10.30.0.999'")
        self.registry.write_text(text)
        (self.records / '67').write_text('')
        self.assertEqual(self.build_registry(), [f'0,{MAC},,7'])

    def test_large_network_allocation_is_one_pass(self):
        # A /16 with 3-address blocks has over 21000 candidate chunks.
        self.claims.write_text(''.join(f'{i},02:00:00:00:01:{i:02x},{A6 + 30 * i},12\n'
                                       for i in range(40)))
        started = time.monotonic()
        result = self.allocator('get_random_chunk', network='10.30.0.0/16', chunk_size=3)
        self.assertLess(time.monotonic() - started, 5, 'allocation should not spawn per chunk')
        chunk = int(result.stdout.strip())
        start = A6 + chunk * 3
        for i in range(40):
            peer = A6 + 30 * i
            self.assertFalse(start <= peer + 11 and peer <= start + 2, (chunk, i))

    def test_published_block_size_round_trips(self):
        self.add_node(size=12)
        self.build_registry()
        self.assertEqual(self.registry_value(MAC, 'IPV4_CHUNK_SIZE'), '12')

    def publish_identity(self, script, allocate=False, recent=False, fail_first=False):
        # Execute one real manager loop through identity publication only.
        # Redirect runtime files and hardware discovery into this fixture;
        # stub hardware/config operations, leaving the real encoder in place.
        source = (TOOLS / script).read_text().split('# === MAIN LOOP ===\n', 1)[1]
        stop = ('# === CHECK STATE: LOBBY OR DATA ===' if script.endswith('-acs.sh')
                else '# === PUBLISH TELEMETRY (Alfred type 68) ===')
        source = source.split(stop, 1)[0]
        if fail_first:
            (self.records / 'fail-publish-once').touch()
            source += '\nif [ "${REVIEW_PASS:-0}" = 0 ]; then REVIEW_PASS=1; continue; fi\n'
        source += '\n    break\ndone\n'
        run = self.root / 'run'
        run.mkdir(exist_ok=True)
        source = source.replace('/var/run/', str(run) + '/')
        source = source.replace('/sys/class/net/', str(self.root / 'net') + '/')
        allocator = self.bin / 'allocate'
        allocator.write_text('#!/bin/bash\nprintf "1\\n" > ' +
                             shlex.quote(str(run / 'my_ipv4_chunk')) + '\n')
        allocator.chmod(0o755)
        # timeout invokes an executable, so a shell function cannot isolate
        # this lookup from the host's runuser/Syncthing installation.
        runuser = self.bin / 'runuser'
        runuser.write_text('#!/bin/bash\nexit 0\n')
        runuser.chmod(0o755)
        prefix = ('log() { :; }\nensure_static_channels() { :; }\n'
                  'hostname() { printf "mesh-test\\n"; }\n'
                  'python3() { if [ "$1" = /usr/local/bin/manet_node_ipv4.py ]; then '
                  'printf "%s\\n" "$REVIEW_IPV4"; else command python3 "$@"; fi; }\n'
                  'ip() { printf "    inet %s/28 scope global br0\\n" "$REVIEW_IPV4"; }\n')
        env = dict(self.env, ENCODER_PATH=str(TOOLS / 'encoder.py'), MY_MAC=MAC,
                   LAST_IDENTITY_PUBLISH=str(int(time.time())) if recent else '0',
                   LAST_IDENTITY_ALLOCATION=':', IDENTITY_PUBLISH_INTERVAL='270',
                   ALFRED_IDENTITY_TYPE='67', CONTROL_IFACE='br0',
                   RADIO_STATE_SYNC='', CONFIG_SYNC='', CONFIG_ROLLBACK='',
                   REGISTRY_BUILDER='', IP_MANAGER=str(allocator) if allocate else '',
                   REVIEW_IPV4='10.30.0.13' if allocate else '10.30.0.6')
        result = subprocess.run(['bash', '-c', prefix + source], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        identity = NodeInfo_pb2.NodeIdentity()
        identity.ParseFromString(base64.b64decode(
            (self.records / 'published-67').read_text()))
        return identity

    def test_static_publish_reads_chunk_after_reallocation(self):
        for script in ('node-manager-static.sh',):
            with self.subTest(script=script):
                marker = self.root / 'run/my_ipv4_chunk'
                marker.parent.mkdir(exist_ok=True)
                marker.write_text('0\n')
                identity = self.publish_identity(script, allocate=True)
                self.assertEqual(identity.ipv4_chunk, 1)
                self.assertEqual(int_to_ipv4(identity.ipv4_address), '10.30.0.13')

    def test_managers_distinguish_chunk_zero_from_no_allocation(self):
        for script in ('node-manager-acs.sh', 'node-manager-static.sh'):
            with self.subTest(script=script):
                marker = self.root / 'run/my_ipv4_chunk'
                marker.unlink(missing_ok=True)
                identity = self.publish_identity(script)
                self.assertEqual(identity.ipv4_address, 0)
                marker.write_text('0\n')
                identity = self.publish_identity(script)
                self.assertEqual(identity.ipv4_chunk, 0)
                self.assertEqual(int_to_ipv4(identity.ipv4_address), '10.30.0.6')


    def test_allocation_change_publishes_before_keepalive_and_retries_failure(self):
        marker = self.root / 'run/my_ipv4_chunk'
        marker.parent.mkdir()
        marker.write_text('0\n')
        for script in ('node-manager-acs.sh', 'node-manager-static.sh'):
            for fail_first in (False, True):
                with self.subTest(script=script, fail_first=fail_first):
                    (self.records / 'published-67').unlink(missing_ok=True)
                    identity = self.publish_identity(script, recent=True, fail_first=fail_first)
                    self.assertEqual(int_to_ipv4(identity.ipv4_address), '10.30.0.6')


if __name__ == '__main__':
    unittest.main()
