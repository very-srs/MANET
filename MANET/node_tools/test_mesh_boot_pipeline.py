"""Run manager startup loops with real encoded records and a virtual clock."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import NodeInfo_pb2
from manet_ids import int_to_ipv4

TOOLS = Path(__file__).resolve().parent
OWN = '02:00:00:00:00:01'


class BootPipelineTests(unittest.TestCase):
    def run_manager(self, name, lobby=True, fail_publish='', missing_id=False):
        with tempfile.TemporaryDirectory(prefix='manet-boot-pipeline-') as scratch:
            root = Path(scratch)
            for directory in ('bin', 'run', 'etc', 'tools', 'net/br0'):
                (root / directory).mkdir(parents=True)
            (root / 'net/br0/address').write_text(OWN)
            (root / 'clock').write_text('0')
            env = dict(os.environ, TEST_ROOT=str(root), TEST_TOOLS=str(TOOLS),
                       TEST_FAIL_PUBLISH=fail_publish, TEST_MISSING_ID=str(int(missing_id)),
                       TEST_LOBBY='true' if lobby else 'false', MANET_TOOLS_DIR=str(TOOLS),
                       MANET_TIME_RUN_DIR=str(root / 'run'),
                       PATH=str(root / 'bin') + os.pathsep + str(Path(sys.executable).parent)
                            + os.pathsep + os.environ['PATH'])
            tool = f'#!{sys.executable}\n' + r'''
import base64, importlib.util, json, os, sys
from pathlib import Path
root = Path(os.environ['TEST_ROOT'])
tools = Path(os.environ['TEST_TOOLS'])
sys.path.insert(0, str(tools))
import NodeInfo_pb2
now = int((root / 'clock').read_text())
name = Path(sys.argv[0]).name
def event(kind, **extra):
    with (root / 'events').open('a') as out:
        out.write(json.dumps(dict(kind=kind, now=now, **extra)) + '\n')
if name == 'date':
    print(1000 + now if sys.argv[1:] == ['+%s'] else 'fixture-clock')
elif name == 'runuser':
    event('syncthing-id')
    if os.environ['TEST_MISSING_ID'] == '1' and not (root / 'id-retried').exists():
        (root / 'id-retried').touch()
        sys.exit(1)
    from manet_ids import bytes_to_syncthing_id
    print(bytes_to_syncthing_id(bytes(range(32))))
elif name == 'alfred':
    kind = sys.argv[2]
    payload = sys.stdin.read()
    if kind == os.environ['TEST_FAIL_PUBLISH'] and not (root / 'publish-retried').exists():
        (root / 'publish-retried').touch()
        event('publish-failed', record=kind)
        sys.exit(1)
    (root / ('published-' + kind)).write_text(payload)
    address = 0
    if kind == '67':
        ident = NodeInfo_pb2.NodeIdentity()
        ident.ParseFromString(base64.b64decode(payload))
        address = ident.ipv4_address
    event('publish', record=kind, address=address)
elif name == 'tick':
    event('sleep', seconds=int(sys.argv[1]))
    (root / 'clock').write_text(str(now + int(sys.argv[1])))
elif name == 'throughput':
    print(0)
elif name == 'allocate':
    spec = importlib.util.spec_from_file_location('startup', tools / 'mesh-ip-startup.py')
    startup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(startup)
    registry = root / 'registry'
    def command(*args, **kwargs):
        if args == ('builder',):
            published = all((root / ('published-' + k)).exists() for k in ('67', '68'))
            registry.write_text("NODE_020000000001_HOSTNAME='fixture'\n"
                                "NODE_020000000001_MAC_ADDRESSES='02:00:00:00:00:01'\n"
                                if published else '')
            return ''
        if args[0] == 'ip':
            return '[{"addr_info":[{"family":"inet6","scope":"link"}]}]'
        if args[0] == 'alfred':
            return '- mode: primary\n- interface: br0\n  - status: active\n'
        if args[0] == 'batctl':
            return '[]'
        raise AssertionError(args)
    startup.command = command
    startup.time.monotonic = lambda: now
    state_file = root / 'startup-state'
    state, ready, message = startup.check(startup.read_state(state_file), registry,
                                          'builder', root / 'net/br0/address')
    startup.save_state(state_file, state)
    event('discovery', since=state.get('since'), ready=ready, message=message)
    chunk = root / 'run/my_ipv4_chunk'
    if ready and not chunk.exists():
        chunk.write_text('0\n')
        event('allocated')
'''
            for command in ('date', 'runuser', 'alfred', 'tick', 'allocate', 'throughput'):
                path = root / 'bin' / command
                path.write_text(tool)
                path.chmod(0o755)
            source = (TOOLS / name).read_text()
            definitions = source.split('# === MAIN SETUP ===', 1)[0]
            loop = source.split('# === MAIN LOOP ===\n', 1)[1]
            def isolated(text):
                paths = {'/var/run/': '/run/', '/run/': '/run/', '/etc/': '/etc/',
                         '/sys/class/net/': '/net/', '/usr/local/bin/': '/tools/'}
                return re.sub('|'.join(re.escape(p) for p in paths),
                              lambda m: str(root) + paths[m[0]], text)
            overrides = r'''
MY_MAC=02:00:00:00:00:01
IP_MANAGER="$TEST_ROOT/bin/allocate"
ENCODER_PATH="$TEST_TOOLS/encoder.py"
RADIO_STATE_SYNC=''; CONFIG_SYNC=''; CONFIG_ROLLBACK=''
THROUGHPUT_MEAN="$TEST_ROOT/bin/throughput"
QUORUM_CHECKER=''; LIMP_MODE_MANAGER=''; CHANNEL_ELECTION=''; TOURGUIDE_MANAGER=''
log() { :; }
ensure_static_channels() { :; }
hostname() { echo fixture; }
collect_radio_mcs() { :; }
collect_interfaces_json() { echo '[]'; }
collect_ap_ssid() { :; }
detect_and_update_gateway_state() { :; }
is_ntp_time_source() { return 1; }
is_hosting_service() { return 1; }
is_hosting_mumble_service() { return 1; }
acs_clock_ready() { return 1; }
acs_configs_ready() { return 1; }
acs_agreement_busy() { return 0; }
should_perform_action() { return 1; }
should_perform_tourguide() { return 1; }
is_in_lobby() { echo "$TEST_LOBBY"; }
python3() {
    if [[ "$1" = */manet_node_ipv4.py ]]; then
        [ ! -s "$TEST_ROOT/run/my_ipv4_chunk" ] || echo 10.30.0.6
    else
        command python3 "$@"
    fi
}
sleep() {
    "$TEST_ROOT/bin/tick" "$1"
    [ ! -s "$TEST_ROOT/run/my_ipv4_chunk" ] || exit 0
    [ "$(cat "$TEST_ROOT/clock")" -lt 40 ] || exit 91
}
'''
            result = subprocess.run(['bash', '-c', isolated(definitions) + overrides + isolated(loop)],
                                    env=env, capture_output=True, text=True, timeout=20)
            events = [json.loads(line) for line in (root / 'events').read_text().splitlines()]
            self.assertEqual(result.returncode, 0, result.stderr + repr(events[:12]))
            identity = NodeInfo_pb2.NodeIdentity()
            identity.ParseFromString(base64.b64decode((root / 'published-67').read_text()))
            return events, identity

    def test_first_pass_starts_discovery_and_claim_publication_does_not_sleep(self):
        for name, lobby in (('node-manager-static.sh', True),
                            ('node-manager-acs.sh', True), ('node-manager-acs.sh', False)):
            with self.subTest(manager=name, lobby=lobby):
                events, identity = self.run_manager(name, lobby)
                first_check = next(e for e in events if e['kind'] == 'discovery')
                self.assertEqual(first_check['since'], 0)
                allocated = next(e for e in events if e['kind'] == 'allocated')
                self.assertEqual(allocated['now'], 10)  # full Alfred observation period
                claim = next(e for e in events if e['kind'] == 'publish' and e.get('address'))
                self.assertEqual(claim['now'], allocated['now'])
                self.assertEqual(events[-1]['seconds'], 15)
                self.assertEqual(sum(e['kind'] == 'syncthing-id' for e in events), 1)
                self.assertEqual(identity.ipv4_chunk, 0)
                self.assertEqual(int_to_ipv4(identity.ipv4_address), '10.30.0.6')

    def test_failed_publication_defers_window_and_missing_syncthing_id_is_retried(self):
        for name in ('node-manager-static.sh', 'node-manager-acs.sh'):
            for kind in ('67', '68'):
                with self.subTest(manager=name, failed_record=kind):
                    events, identity = self.run_manager(name, fail_publish=kind, missing_id=True)
                    checks = [e for e in events if e['kind'] == 'discovery']
                    self.assertIsNone(checks[0]['since'])
                    self.assertEqual(checks[1]['since'], 1)
                    allocated = next(e for e in events if e['kind'] == 'allocated')
                    self.assertEqual(allocated['now'], 11)
                    self.assertEqual(sum(e['kind'] == 'syncthing-id' for e in events), 2)
                    self.assertEqual(identity.syncthing_id, bytes(range(32)))


if __name__ == '__main__':
    unittest.main()
