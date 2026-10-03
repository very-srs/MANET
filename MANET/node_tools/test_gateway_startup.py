"""Exercise the real gateway route loop without changing host networking."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent


class GatewayStartupTests(unittest.TestCase):
    def run_manager(self, *, ready_at=0, reachable_at=0, gateway=True, registry=True,
                    current='', local_gateway=False, fail_install=False,
                    jump_clock=False, polls=5, address='10.44.0.6'):
        with tempfile.TemporaryDirectory(prefix='manet-gateway-startup-') as scratch:
            root = Path(scratch)
            (root / 'bin').mkdir()
            (root / 'run').mkdir()
            (root / 'clock').write_text('0')
            (root / 'uptime').write_text('100.0 0.0\n')
            (root / 'route').write_text(current)
            if local_gateway:
                (root / 'run/mesh-gateway.state').touch()
            if registry:
                (root / 'run/mesh_node_registry').write_text(
                    "NODE_020000000010_MAC_ADDRESSES='02:00:00:00:00:10,02:00:00:00:01:10'\n"
                    "NODE_020000000010_IPV4_ADDRESS='10.44.0.11'\n")
            case = dict(ready_at=ready_at, reachable_at=reachable_at, gateway=gateway,
                        fail_install=fail_install, jump_clock=jump_clock, polls=polls,
                        address=address)
            (root / 'case').write_text(json.dumps(case))
            tool = f'#!{sys.executable}\n' + r'''
import json, os, sys
from pathlib import Path
root = Path(os.environ['TEST_ROOT'])
case = json.loads((root / 'case').read_text())
now = int((root / 'clock').read_text())
args = sys.argv[1:]
name = Path(sys.argv[0]).name
def event(kind, **extra):
    with (root / 'events').open('a') as out:
        out.write(json.dumps(dict(kind=kind, now=now, **extra)) + '\n')
if name == 'batctl':
    assert args == ['gwl', '-H', '-n'], args
    if case['gateway']:
        # batman's own "*" choice is ignored; the better score wins.
        print('* 02:00:00:00:09:10 (       20.0) 02:00:00:00:09:10 [     wlan0]: 10.0/2.0 MBit')
        print('  02:00:00:00:01:10 (       95.4) 02:00:00:00:01:10 [     wlan0]: 100.0/20.0 MBit')
elif name == 'ip':
    if args == ['route', 'show', 'default']:
        print((root / 'route').read_text())
    elif args[:3] == ['route', 'replace', 'default']:
        event('replace', args=args)
        if case['fail_install'] and not (root / 'retried').exists():
            (root / 'retried').touch()
            sys.exit(2)
        (root / 'route').write_text(' '.join(args[2:]))
    elif args == ['route', 'del', 'default', 'dev', 'br0']:
        event('delete')
        (root / 'route').write_text('')
    else:
        raise AssertionError(args)
elif name == 'primary.py':
    event('primary')
    if now >= case['ready_at']:
        print(case['address'])
elif name == 'ping':
    event('ping', target=args[-1])
    sys.exit(0 if now >= case['reachable_at'] else 1)
elif name == 'sleep':
    event('sleep', seconds=int(args[0]))
    count_file = root / 'polls'
    count = int(count_file.read_text()) + 1 if count_file.exists() else 1
    count_file.write_text(str(count))
    if count >= case['polls']:
        sys.exit(77)  # bounded fixture stop; production set -e exits here
    now += 90 if case['jump_clock'] and count == 1 else int(args[0])
    (root / 'clock').write_text(str(now))
    (root / 'uptime').write_text(str(100 + now) + '.0 0.0\n')
elif name == 'date':
    print('clock-after-NTP-step')  # does not determine the retry deadline
'''
            for name in ('batctl', 'ip', 'ping', 'sleep', 'date', 'primary.py'):
                path = root / 'bin' / name
                path.write_text(tool)
                path.chmod(0o755)
            source = (TOOLS / 'gateway-route-manager.sh').read_text()
            source = source.replace('/var/run/', str(root / 'run') + '/')
            source = source.replace('/proc/uptime', str(root / 'uptime'))
            source = source.replace('/usr/local/bin/manet_node_ipv4.py', str(root / 'bin/primary.py'))
            script = root / 'gateway.sh'
            script.write_text(source)
            env = dict(os.environ, TEST_ROOT=str(root), PATH=str(root / 'bin') + os.pathsep + os.environ['PATH'])
            result = subprocess.run(['bash', str(script)], env=env, capture_output=True,
                                    text=True, timeout=15)
            self.assertEqual(result.returncode, 77, result.stderr)
            events = [json.loads(line) for line in (root / 'events').read_text().splitlines()]
            return events, (root / 'route').read_text()

    def test_late_primary_gets_route_without_an_extra_ten_second_poll(self):
        events, route = self.run_manager(ready_at=3)
        install = next(e for e in events if e['kind'] == 'replace')
        self.assertEqual(install['now'], 3)
        self.assertEqual(install['args'], ['route', 'replace', 'default', 'via',
                                          '10.44.0.11', 'dev', 'br0', 'src', '10.44.0.6'])
        self.assertEqual([e['seconds'] for e in events if e['kind'] == 'sleep'], [1, 1, 1, 10, 10])
        self.assertEqual(sum(e['kind'] == 'replace' for e in events), 1)

    def test_no_primary_or_missing_registry_cannot_install_a_route(self):
        for options in ({'ready_at': 500}, {'registry': False}):
            with self.subTest(options=options):
                events, route = self.run_manager(**options)
                self.assertFalse(route)
                self.assertFalse(any(e['kind'] in ('replace', 'ping') for e in events))

    def test_failed_ping_and_route_install_retry_before_normal_polling(self):
        events, route = self.run_manager(reachable_at=2, fail_install=True)
        attempts = [e['now'] for e in events if e['kind'] == 'replace']
        self.assertEqual(attempts, [2, 3])
        self.assertIn('src 10.44.0.6', route)
        self.assertEqual([e['seconds'] for e in events if e['kind'] == 'sleep'], [1, 1, 1, 10, 10])

    def test_fast_polling_is_bounded_when_no_gateway_exists(self):
        events, route = self.run_manager(gateway=False, jump_clock=True, polls=3)
        self.assertEqual([e['seconds'] for e in events if e['kind'] == 'sleep'], [1, 10, 10])
        self.assertFalse(route)

    def test_local_uplink_is_preserved_even_before_its_gateway_marker(self):
        for marker in (False, True):
            with self.subTest(marker=marker):
                original = 'default via 192.168.69.1 dev end0 src 192.168.69.51'
                events, route = self.run_manager(current=original, local_gateway=marker, polls=2)
                self.assertEqual(route, original)
                self.assertTrue(all(e['kind'] == 'sleep' and e['seconds'] == 10 for e in events))

    def test_stale_mesh_route_is_removed_without_a_gateway(self):
        events, route = self.run_manager(gateway=False, current='default via 10.44.0.11 dev br0', polls=2)
        self.assertFalse(route)
        self.assertEqual(sum(e['kind'] == 'delete' for e in events), 1)

    def test_route_source_tracks_primary_reallocation_not_vips_or_prefix_matches(self):
        for old in ('10.44.0.2', '10.44.0.60'):
            with self.subTest(old_source=old):
                events, route = self.run_manager(current=f'default via 10.44.0.11 dev br0 src {old}', polls=2)
                self.assertEqual(sum(e['kind'] == 'replace' for e in events), 1)
                self.assertTrue(route.endswith('src 10.44.0.6'))


if __name__ == '__main__':
    unittest.main()
