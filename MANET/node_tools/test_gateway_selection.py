"""gateway-route-manager.sh picks its own gateway and switches only for a
noticeable gain. Runs the real loop against a simulated clock."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent
GATEWAYS = {'a': ('02:00:00:00:00:0a', '10.44.0.10'),
            'b': ('02:00:00:00:00:0b', '10.44.0.11'),
            'c': ('02:00:00:00:00:0c', '10.44.0.12')}

TOOL = f'#!{sys.executable}\n' + r'''
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
def active(windows):
    return any(start <= now < end for start, end in windows)
if name == 'batctl':
    lines = []
    for gw, (start, end, path, bw) in case['gateways'].items():
        if start <= now < end:
            mac = case['macs'][gw]
            lines.append(f'  {mac} ({path:>11.1f}) {mac} [     wlan0]: {bw:.1f}/2.0 MBit')
    print('\n'.join(lines))
elif name == 'ip':
    if args == ['route', 'show', 'default']:
        print((root / 'route').read_text())
    elif args[:3] == ['route', 'replace', 'default']:
        event('replace', via=args[4])
        (root / 'route').write_text(' '.join(args[2:]))
    elif args == ['route', 'del', 'default', 'dev', 'br0']:
        event('delete')
        (root / 'route').write_text('')
elif name == 'primary.py':
    print('10.44.0.6')
elif name == 'ping':
    sys.exit(1 if active(case['down'].get(args[-1], [])) else 0)
elif name == 'sleep':
    if now >= case['until']:
        sys.exit(77)
    now += int(args[0])
    (root / 'clock').write_text(str(now))
    (root / 'uptime').write_text(f'{1000 + now}.0 0.0\n')
elif name == 'date':
    print('date')
'''


class GatewaySelectionTests(unittest.TestCase):
    def run_manager(self, gateways, until=900, down=None, route=''):
        """gateways: {name: (start, end, path_mbps, announced_down_mbps)}.
        Returns [(time, gateway name)] for every route install."""
        with tempfile.TemporaryDirectory(prefix='manet-gw-select-') as scratch:
            root = Path(scratch)
            (root / 'bin').mkdir()
            (root / 'run').mkdir()
            (root / 'clock').write_text('0')
            (root / 'uptime').write_text('1000.0 0.0\n')
            (root / 'route').write_text(route)
            (root / 'run/mesh_node_registry').write_text(''.join(
                f"NODE_{mac.replace(':', '')}_MAC_ADDRESSES='{mac}'\n"
                f"NODE_{mac.replace(':', '')}_IPV4_ADDRESS='{ip}'\n" for mac, ip in GATEWAYS.values()))
            (root / 'case').write_text(json.dumps(dict(
                gateways=gateways, until=until,
                macs={g: mac for g, (mac, _) in GATEWAYS.items()},
                down={GATEWAYS[g][1]: w for g, w in (down or {}).items()})))
            for name in ('batctl', 'ip', 'ping', 'sleep', 'date', 'primary.py'):
                (root / 'bin' / name).write_text(TOOL)
                (root / 'bin' / name).chmod(0o755)
            source = (TOOLS / 'gateway-route-manager.sh').read_text()
            source = source.replace('/var/run/', str(root / 'run') + '/')
            source = source.replace('/proc/uptime', str(root / 'uptime'))
            source = source.replace('/usr/local/bin/manet_node_ipv4.py', str(root / 'bin/primary.py'))
            (root / 'gateway.sh').write_text(source)
            env = dict(os.environ, TEST_ROOT=str(root),
                       PATH=str(root / 'bin') + os.pathsep + os.environ['PATH'])
            result = subprocess.run(['bash', str(root / 'gateway.sh')], env=env,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 77, result.stderr)
            by_ip = {ip: g for g, (_, ip) in GATEWAYS.items()}
            events = [json.loads(line) for line in (root / 'events').read_text().splitlines()] \
                if (root / 'events').exists() else []
            self.log = result.stderr
            return [(e['now'], by_ip[e['via']]) for e in events if e['kind'] == 'replace']

    def test_picks_the_best_bottleneck_not_the_fastest_path(self):
        # a: fast mesh path to a slow uplink; b: slower path to a fast one.
        installs = self.run_manager({'a': (0, 9999, 95.0, 10.0), 'b': (0, 9999, 30.0, 50.0)}, until=60)
        self.assertEqual(installs, [(0, 'b')])

    def test_a_small_gain_never_switches(self):
        installs = self.run_manager({'a': (0, 9999, 20.0, 100.0), 'b': (100, 9999, 28.0, 100.0)})
        self.assertEqual(installs, [(0, 'a')])

    def test_a_tiny_absolute_gain_never_switches(self):
        # 1.0 -> 2.5 is 2.5x but only 1.5 Mbit/s: not noticeable.
        installs = self.run_manager({'a': (0, 9999, 1.0, 100.0), 'b': (100, 9999, 2.5, 100.0)})
        self.assertEqual(installs, [(0, 'a')])

    def test_a_noticeable_gain_switches_after_hold_and_sustain(self):
        installs = self.run_manager({'a': (0, 9999, 10.0, 100.0), 'b': (20, 9999, 30.0, 100.0)})
        self.assertEqual(installs[0], (0, 'a'))
        self.assertEqual([g for _, g in installs[1:]], ['b'])
        # Not within 300 s of the first choice, and only after 60 s sustained.
        self.assertTrue(360 <= installs[1][0] <= 380, installs)
        self.assertIn('Switching gateway from 02:00:00:00:00:0a', self.log)

    def test_a_brief_gain_does_not_switch(self):
        installs = self.run_manager({'a': (0, 9999, 10.0, 100.0), 'b': (400, 440, 30.0, 100.0)})
        self.assertEqual(installs, [(0, 'a')])

    def test_a_vanished_gateway_is_replaced_at_once(self):
        installs = self.run_manager({'a': (0, 200, 30.0, 100.0), 'b': (0, 9999, 10.0, 100.0)}, until=300)
        self.assertEqual(installs, [(0, 'a'), (200, 'b')])

    def test_an_unanswering_gateway_is_replaced_after_two_polls(self):
        installs = self.run_manager({'a': (0, 9999, 30.0, 100.0), 'b': (0, 9999, 10.0, 100.0)},
                                    until=300, down={'a': [[150, 9999]]})
        self.assertEqual([g for _, g in installs], ['a', 'b'])
        self.assertTrue(150 < installs[1][0] <= 170, installs)

    def test_restart_keeps_the_gateway_already_in_use(self):
        route = 'default via 10.44.0.11 dev br0 src 10.44.0.6'
        installs = self.run_manager({'a': (0, 9999, 14.0, 100.0), 'b': (0, 9999, 10.0, 100.0)},
                                    route=route, until=600)
        self.assertEqual(installs, [])

    def test_restart_still_switches_for_a_noticeable_gain(self):
        route = 'default via 10.44.0.11 dev br0 src 10.44.0.6'
        installs = self.run_manager({'a': (0, 9999, 40.0, 100.0), 'b': (0, 9999, 10.0, 100.0)},
                                    route=route, until=200)
        self.assertEqual([g for _, g in installs], ['a'])
        self.assertTrue(60 <= installs[0][0] <= 80, installs)


if __name__ == '__main__':
    unittest.main()
