#!/usr/bin/env python3
"""Service elections: start gating, cadence, overlap lock and convergence."""

import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parent
SELF = '02:00:00:00:00:01'
PEER = '02:00:00:00:00:02'
MANAGERS = ('node-manager-static.sh', 'node-manager-acs.sh', 'node-manager.sh')


class Fixture(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.run_dir = self.root / 'run'
        self.run_dir.mkdir()
        self.env = dict(os.environ, PATH=os.pathsep.join(
            (str(self.bin), str(TOOLS), str(Path(sys.executable).parent), os.environ['PATH'])))

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/bash\n' + body)
        path.chmod(0o755)


class ElectionGateTests(Fixture):
    """The managers' run_service_elections, extracted from each script."""

    def setUp(self):
        super().setUp()
        self.elections = self.root / 'elections'
        self.elections.mkdir()
        record = self.elections / 'test-election.sh'
        record.write_text('#!/bin/bash\necho run >> "$REVIEW_LOG"\n')
        record.chmod(0o755)
        (self.elections / 'channel-election.sh').write_text(record.read_text())
        (self.elections / 'channel-election.sh').chmod(0o755)
        self.log = self.root / 'runs'
        self.uptime = self.root / 'uptime'

    def function(self, script):
        source = (TOOLS / script).read_text()
        body = re.search(r'^ELECTION_INTERVAL=.*?^run_service_elections\(\) \{\n.*?^\}\n',
                         source, re.M | re.S)[0]
        return body.replace('/var/run/', str(self.run_dir) + '/')

    def passes(self, script, uptimes):
        calls = ''.join(f'echo "{u}.40 1.0" > "$MESH_UPTIME_FILE"\nrun_service_elections\n'
                        for u in uptimes)
        env = dict(self.env, ELECTION_DIR=str(self.elections), REVIEW_LOG=str(self.log),
                   MESH_UPTIME_FILE=str(self.uptime))
        result = subprocess.run(['bash', '-c', self.function(script) + calls + 'wait\n'],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0

    def test_no_elections_before_an_allocation(self):
        for script in MANAGERS:
            with self.subTest(script=script):
                (self.run_dir / 'my_ipv4_chunk').unlink(missing_ok=True)
                self.log.unlink(missing_ok=True)
                self.assertEqual(self.passes(script, [10, 11, 12, 40]), 0)

    def test_steady_cadence_after_allocation(self):
        # Allocation follows either complete discovery or its bounded deadline
        # (both covered by test_mesh_ip_startup); the gate only needs the claim.
        (self.run_dir / 'my_ipv4_chunk').write_text('0\n')
        for script in MANAGERS:
            with self.subTest(script=script):
                self.log.unlink(missing_ok=True)
                # One-second startup passes: first runs, the rest wait 15 s.
                # channel-election is never started from here.
                self.assertEqual(self.passes(script, [100, 101, 102, 114, 115, 116, 130]), 3)


class MediaMtxElectionTests(Fixture):
    VIP = '10.30.0.2'

    def setUp(self):
        super().setUp()
        self.addresses = self.root / 'addresses'
        self.addresses.write_text('')
        self.services = self.root / 'services'
        self.registry = self.root / 'registry'
        (self.root / 'mesh.conf').write_text('ipv4_network=10.30.0.0/24\n')
        (self.root / 'mac').write_text(SELF + '\n')
        (self.root / 'uptime').write_text('10000.50 1.00\n')
        self.env['MESH_UPTIME_FILE'] = str(self.root / 'uptime')
        self.stub('systemd-cat', 'cat > /dev/null\n')
        self.stub('mtx-ip', 'echo fd00:1:2:3::64/128\n')
        # ip: remember added/removed addresses and show them back.
        self.stub('ip', f'''
case "$*" in
  *"addr show"*) while read -r a; do case "$a" in *:*) echo "    inet6 $a/128";; *) echo "    inet $a/24";; esac; done < "{self.addresses}" ;;
  *"addr add"*) a="${{3%/*}}"; echo "$a" >> "{self.addresses}" ;;
  *"addr del"*) a="${{3%/*}}"; grep -vxF "$a" "{self.addresses}" > "{self.addresses}.new"; mv "{self.addresses}.new" "{self.addresses}" ;;
esac
''')
        self.stub('systemctl', f'''
case "$1" in
  is-active) grep -qx active "{self.services}" 2>/dev/null ;;
  restart|start) echo active > "{self.services}" ;;
  stop) echo inactive > "{self.services}" ;;
  *) exit 0 ;;
esac
''')
        self.stub('arping', 'exit 0\n')

    def node(self, mac, mbps, age=0, server=False):
        key = 'NODE_' + mac.replace(':', '')
        return (f"{key}_MAC_ADDRESS='{mac}'\n{key}_MEAN_THROUGHPUT_MBPS='{mbps}'\n"
                f"{key}_OBSERVED_AT_UPTIME='{10000 - age}'\n"
                f"{key}_LAST_SEEN_TIMESTAMP='100'\n"
                f"{key}_IS_MEDIAMTX_SERVER='{'true' if server else 'false'}'\n")

    def elect(self, lock=None):
        source = (TOOLS / 'mediamtx-election.sh').read_text()
        for old, new in (('/var/run/mesh_node_registry', str(self.registry)),
                         ('/sys/class/net/${CONTROL_IFACE}/address', str(self.root / 'mac')),
                         ('/usr/local/bin/mtx-ip.sh', str(self.bin / 'mtx-ip')),
                         ('/etc/mesh.conf', str(self.root / 'mesh.conf')),
                         ('/var/run/mediamtx-election.lock', lock or str(self.root / 'lock'))):
            source = source.replace(old, new)
        return subprocess.run(['bash', '-c', source], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def holds_vip(self):
        return self.VIP in self.addresses.read_text().split()

    def test_overlapping_run_exits_without_touching_state(self):
        lock = self.root / 'held.lock'
        self.registry.write_text(self.node(SELF, 50))
        with open(lock, 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.elect(lock=str(lock))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.holds_vip())

    def test_lone_winner_yields_once_registry_shows_the_incumbent(self):
        # A node seeing only itself wins. Once the registry converges and shows
        # a better-connected incumbent, it withdraws the VIP and service.
        self.registry.write_text(self.node(SELF, 50))
        self.assertEqual(self.elect().returncode, 0)
        self.assertTrue(self.holds_vip())
        self.registry.write_text(self.node(SELF, 50) + self.node(PEER, 45, server=True))
        self.assertEqual(self.elect().returncode, 0)
        self.assertFalse(self.holds_vip())
        self.assertEqual(self.services.read_text().strip(), 'inactive')

    def test_candidate_freshness_is_local_observation(self):
        # The peer's sender clock (LAST_SEEN_TIMESTAMP='100') is ignored; only
        # an observed age beyond the threshold disqualifies it.
        self.registry.write_text(self.node(SELF, 10) + self.node(PEER, 90))
        self.elect()
        self.assertFalse(self.holds_vip())
        self.registry.write_text(self.node(SELF, 10) + self.node(PEER, 90, age=601))
        self.elect()
        self.assertTrue(self.holds_vip())


if __name__ == '__main__':
    unittest.main()
