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
            REVIEW_RECORDS=str(self.records),
        )
        alfred = self.bin / 'alfred'
        alfred.write_text(
            '#!/bin/bash\n'
            'case "$1" in\n'
            '  -r) cat "$REVIEW_RECORDS/$2" ;;\n'
            '  -s) cat > "$REVIEW_RECORDS/published-$2" ;;\n'
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

    def add_node(self, mac=MAC, chunk=0, ip='10.30.0.6', age=0,
                 state='ACTIVE', identity=True):
        if identity:
            args = ['--hostname', 'mesh-' + mac[-2:], '--mac-addresses', mac,
                    '--ipv4-chunk', str(chunk)]
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

    def allocator(self, command):
        # Load only the existing function definitions, avoiding live interface
        # configuration. Exercise allocation against the registry's real output.
        source = (TOOLS / 'mesh-ip-manager.sh').read_text()
        functions = source.split('# --- Helper Functions ---\n', 1)[1]
        functions = functions.split('# --- Main Logic ---\n', 1)[0]
        env = dict(self.env, IPV4_NETWORK='10.30.0.0/28', CHUNK_SIZE='7',
                   SERVICES_RESERVED='5', CLAIMED_CHUNKS_FILE=str(self.claims))
        return subprocess.run(['bash', '-c', functions + '\n' + command],
                              env=env, capture_output=True, text=True, timeout=5)

    def test_chunk_zero_is_claimed_and_allocator_cannot_reuse_it(self):
        self.add_node()
        self.assertEqual(self.build_registry(), [f'0,{MAC}'])
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
                         [f'0,{MAC}', '0,02:00:00:00:00:02'])

    def test_nonzero_chunk_still_claimed(self):
        self.add_node(chunk=1, ip='10.30.0.13')
        self.assertEqual(self.build_registry(), [f'1,{MAC}'])

    def test_unknown_or_inactive_allocations_are_excluded(self):
        self.add_node(mac='02:00:00:00:00:02', ip='')
        self.add_node(mac='02:00:00:00:00:03', identity=False)
        self.add_node(mac='02:00:00:00:00:04', age=600)
        self.add_node(mac='02:00:00:00:00:05', state='SHUTTING_DOWN')
        self.assertEqual(self.build_registry(), [])

    def test_chunk_zero_survives_cached_identity(self):
        self.add_node()
        self.assertEqual(self.build_registry(), [f'0,{MAC}'])
        (self.records / '67').write_text('')
        self.assertEqual(self.build_registry(), [f'0,{MAC}'])

    def publish_identity(self, script, allocate=False):
        # Execute one real manager loop through identity publication only.
        # Redirect runtime files and hardware discovery into this fixture;
        # stub hardware/config operations, leaving the real encoder in place.
        source = (TOOLS / script).read_text().split('# === MAIN LOOP ===\n', 1)[1]
        stop = ('# === CHECK STATE: LOBBY OR DATA ===' if script.endswith('-acs.sh')
                else '# === PUBLISH TELEMETRY (Alfred type 68) ===')
        source = source.split(stop, 1)[0] + '\n    break\ndone\n'
        run = self.root / 'run'
        run.mkdir(exist_ok=True)
        source = source.replace('/var/run/', str(run) + '/')
        source = source.replace('/sys/class/net/', str(self.root / 'net') + '/')
        allocator = self.bin / 'allocate'
        allocator.write_text('#!/bin/bash\nprintf "1\\n" > ' +
                             shlex.quote(str(run / 'my_ipv4_chunk')) + '\n')
        allocator.chmod(0o755)
        prefix = ('log() { :; }\nensure_static_channels() { :; }\n'
                  'runuser() { :; }\nhostname() { printf "mesh-test\\n"; }\n'
                  'ip() { printf "    inet %s/28 scope global br0\\n" "$REVIEW_IPV4"; }\n')
        env = dict(self.env, ENCODER_PATH=str(TOOLS / 'encoder.py'), MY_MAC=MAC,
                   LAST_IDENTITY_PUBLISH='0', IDENTITY_PUBLISH_INTERVAL='270',
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

    def test_static_publish_reads_chunk_after_allocation(self):
        for script in ('node-manager-static.sh', 'node-manager.sh'):
            with self.subTest(script=script):
                marker = self.root / 'run/my_ipv4_chunk'
                marker.unlink(missing_ok=True)
                identity = self.publish_identity(script, allocate=True)
                self.assertEqual(identity.ipv4_chunk, 1)
                self.assertEqual(int_to_ipv4(identity.ipv4_address), '10.30.0.13')

    def test_managers_distinguish_chunk_zero_from_no_allocation(self):
        for script in ('node-manager-acs.sh', 'node-manager-static.sh', 'node-manager.sh'):
            with self.subTest(script=script):
                marker = self.root / 'run/my_ipv4_chunk'
                marker.unlink(missing_ok=True)
                identity = self.publish_identity(script)
                self.assertEqual(identity.ipv4_address, 0)
                marker.write_text('0\n')
                identity = self.publish_identity(script)
                self.assertEqual(identity.ipv4_chunk, 0)
                self.assertEqual(int_to_ipv4(identity.ipv4_address), '10.30.0.6')


if __name__ == '__main__':
    unittest.main()
