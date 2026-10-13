"""Automatic checks require opt-in, clock sync and a direct or mesh route."""

import os
import json
import sys
import configparser
from pathlib import Path
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent


class AutoUpdateTests(unittest.TestCase):
    def run_wrapper(self, *, iface='br0', route='default via 10.44.0.1 dev br0',
                    enabled='y', synced=True, delay='0', after_route=None,
                    after_enabled=None):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / 'mesh.conf').write_text(f'auto_update={enabled}\n')
            (root / 'route').write_text(route)
            if iface is not None:
                (root / 'upstream').write_text(iface)
            if synced:
                (root / 'initial_time_synced').touch()
            stub = f'#!{sys.executable}\n' + r'''
import json, os, sys
from pathlib import Path
root = Path(os.environ['TEST_ROOT'])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / 'calls').open('a') as out:
    out.write(json.dumps([name, *args]) + '\n')
if name == 'ip':
    assert args[:4] == ['-4', 'route', 'show', 'default'], args
    assert len(args) == 4 or (len(args) == 6 and args[4] == 'dev'), args
    for line in (root / 'route').read_text().splitlines():
        if len(args) == 4 or f' dev {args[5]} ' in f' {line} ':
            print(line)
elif name == 'sleep':
    if 'TEST_AFTER_ROUTE' in os.environ:
        (root / 'route').write_text(os.environ['TEST_AFTER_ROUTE'])
    if 'TEST_AFTER_ENABLED' in os.environ:
        (root / 'mesh.conf').write_text('auto_update=' + os.environ['TEST_AFTER_ENABLED'])
elif name == 'ping':
    sys.exit(99)
'''
            for name in ('ip', 'sleep', 'update', 'ping'):
                path = root / name
                path.write_text(stub)
                path.chmod(0o755)
            env = dict(os.environ, PATH=scratch + ':' + os.environ['PATH'],
                       TEST_ROOT=scratch, MANET_MESH_CONF=str(root / 'mesh.conf'),
                       MANET_UPSTREAM_FILE=str(root / 'upstream'),
                       MANET_TIME_RUN_DIR=scratch, MANET_UPDATER=str(root / 'update'))
            env.pop('MANET_AUTO_UPDATE_DELAY', None)
            if delay is not None:
                env['MANET_AUTO_UPDATE_DELAY'] = delay
            if after_route is not None:
                env['TEST_AFTER_ROUTE'] = after_route
            if after_enabled is not None:
                env['TEST_AFTER_ENABLED'] = after_enabled
            result = subprocess.run(['bash', str(TOOLS / 'manet-auto-update.sh')], env=env,
                                    capture_output=True, text=True, timeout=5)
            calls = root / 'calls'
            events = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
            self.assertNotIn('ping', [e[0] for e in events])
            return result, events

    def test_opt_in_clock_and_default_route_gate_updates(self):
        cases = [
            (False, 'y', 'end0', 'default via 1.2.3.4 dev end0', False),
            (False, 'y', 'br0', 'default via 1.2.3.4 dev br0', False),
            (True, 'n', 'end0', 'default via 1.2.3.4 dev end0', False),
            (True, 'n', 'br0', 'default via 1.2.3.4 dev br0', False),
            (True, 'y', 'br0', 'default via 1.2.3.4 dev br0', True),
            (True, 'y', None, 'default via 1.2.3.4 dev br0 src 10.44.0.6', True),
            (True, 'y', 'end0', 'default via 1.2.3.4 dev br0', True),
            (True, 'y', 'br0', '', False),
            (True, 'y', 'br0', 'default dev br0 scope link', False),
            (True, 'y', 'br0', 'default via 1.2.3.4 dev br01', False),
            (True, 'y', 'br0', 'default via fe80::1 dev br0', False),
            (True, 'y', 'bat0', 'default via 1.2.3.4 dev bat0', False),
            (True, 'y', 'lo', 'default via 1.2.3.4 dev lo', False),
            (True, 'y', 'br1', 'default via 1.2.3.4 dev br1', False),
            (True, 'y', 'bad iface', 'default via 1.2.3.4 dev end0', False),
            (True, 'y', 'end0', '', False),
            (True, 'y', 'end0', 'default via 1.2.3.4 dev end0', True),
            (True, 'YeS  ', 'usb0', 'default dev usb0 scope link', True),
        ]
        for synced, enabled, iface, route, expected in cases:
            with self.subTest(synced=synced, enabled=enabled, iface=iface, route=route):
                result, events = self.run_wrapper(synced=synced, enabled=enabled,
                                                  iface=iface, route=route)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(['update', '--routine'] in events, expected)
                if not synced:
                    self.assertEqual(result.stdout, 'MANET routine update check deferred until clock sync\n')
                    self.assertFalse(any(e[0] == 'sleep' for e in events))
                if enabled == 'n':
                    self.assertEqual(events, [])

    def test_only_mesh_path_waits_and_direct_path_takes_precedence(self):
        for iface in ('end0', 'usb0', 'wlan2'):
            with self.subTest(iface=iface):
                result, events = self.run_wrapper(iface=iface, delay='900', route=
                    f'default via 1.2.3.4 dev {iface}\ndefault via 10.44.0.1 dev br0')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(['update', '--routine'], events)
                self.assertFalse(any(e[0] == 'sleep' for e in events))
        for delay in ('0', '900', None):
            with self.subTest(delay=delay):
                result, events = self.run_wrapper(delay=delay)
                self.assertEqual(result.returncode, 0, result.stderr)
                sleeps = [e for e in events if e[0] == 'sleep']
                self.assertEqual(len(sleeps), 1)
                actual = int(sleeps[0][1])
                self.assertTrue(0 <= actual <= 900)
                if delay is not None:
                    self.assertEqual(actual, int(delay))
                self.assertLess(events.index(sleeps[0]), events.index(['update', '--routine']))

    def test_invalid_delay_cannot_exceed_the_service_budget(self):
        for delay in ('-1', '901', '10000', '1m', '1.5'):
            with self.subTest(delay=delay):
                result, events = self.run_wrapper(delay=delay)
                self.assertEqual(result.returncode, 1)
                self.assertFalse(any(e[0] in ('sleep', 'update') for e in events))

    def test_route_loss_or_opt_out_during_jitter_cancels_update(self):
        for changes in ({'after_route': ''}, {'after_enabled': 'n'}):
            with self.subTest(changes=changes):
                result, events = self.run_wrapper(delay='900', **changes)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(['sleep', '900'], events)
                self.assertNotIn(['update', '--routine'], events)


class AutoUpdateUnitTests(unittest.TestCase):
    def unit(self, name):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string((TOOLS.parent / 'systemd' / name).read_text())
        return parser

    def test_clock_path_queues_one_check_without_latching_routable_service(self):
        path = self.unit('manet-auto-update.path')
        self.assertEqual(path['Path']['PathExists'], '/run/initial_time_synced')
        self.assertEqual(path['Install']['WantedBy'], 'multi-user.target')
        trigger = self.unit(path['Path']['Unit'])
        self.assertEqual(trigger['Service']['Type'], 'oneshot')
        self.assertTrue(trigger['Service'].getboolean('RemainAfterExit'))
        self.assertEqual(trigger['Service']['ExecStart'],
                         '/usr/bin/systemctl start --no-block manet-auto-update.service')
        self.assertIn('manet-auto-update.service', trigger['Unit']['After'].split())
        service = self.unit('manet-auto-update.service')
        self.assertEqual(service['Service']['Type'], 'oneshot')
        self.assertFalse(service['Service'].getboolean('RemainAfterExit', fallback=False))
        self.assertEqual(service['Service']['TimeoutStartSec'], '45min')

    def test_daily_timer_queues_the_same_service_with_jitter_and_catchup(self):
        timer = self.unit('manet-auto-update.timer')
        self.assertEqual(timer['Timer']['OnCalendar'], 'daily')
        self.assertEqual(timer['Timer']['RandomizedDelaySec'], '2h')
        self.assertTrue(timer['Timer'].getboolean('Persistent'))
        self.assertEqual(timer['Timer']['Unit'], 'manet-auto-update.service')
        self.assertEqual(timer['Install']['WantedBy'], 'timers.target')

    def test_timer_is_enabled_in_shipped_systemd_tree(self):
        link = TOOLS.parent / 'systemd/timers.target.wants/manet-auto-update.timer'
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.readlink(), Path('../manet-auto-update.timer'))
        self.assertTrue(link.is_file())

    def test_path_is_enabled_in_shipped_systemd_tree(self):
        path = TOOLS.parent / 'systemd' / 'manet-auto-update.path'
        link = path.parent / 'multi-user.target.wants' / path.name
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.readlink(), Path('../manet-auto-update.path'))
        self.assertTrue(link.is_file())

    def test_builders_ship_the_enabled_path(self):
        builders = TOOLS.parent / 'packaging'
        if not builders.is_dir():
            self.skipTest('local packaging tools are not tracked in this repository')
        for board in ('tools', 'cm4', 'rpi5', 'r3a'):
            with self.subTest(board=board):
                source = (builders / f'build-{board}-tarball.sh').read_text()
                self.assertRegex(source,
                                 r'install_tree "\$REPO_ROOT/MANET/systemd"\s+'
                                 r'"\$STAGE/etc/systemd/system"')
                self.assertIn('    manet-auto-update.path \\\n', source)
                self.assertIn('ln -sf ../manet-auto-update.timer', source)
                self.assertIn('$STAGE/etc/systemd/system/timers.target.wants/manet-auto-update.timer', source)

    def test_radio_setup_enables_clock_trigger(self):
        source = (TOOLS / 'radio-setup.sh').read_text()
        self.assertIn('systemctl enable --now manet-auto-update.path', source)
        self.assertIn('systemctl enable --now manet-auto-update.timer', source)

    def test_updater_requires_and_enables_timer(self):
        # Use the updater's existing archive/activation harness.
        import test_node_update
        case = test_node_update.UpdateTests()
        case.setUp()
        try:
            self.assertIn('etc/systemd/system/manet-auto-update.timer', test_node_update.update.REQUIRED)
            case.updater.update()
            self.assertIn('systemctl enable --now manet-auto-update.timer', case.history())
        finally:
            case.doCleanups()
