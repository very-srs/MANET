"""Exercise the real LED scripts with fake GPIO, BATMAN and service commands."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent
PEER = '0c:bf:74:00:2b:f1'
OTHER = '02:00:00:00:00:02'
RADIO = '02:00:00:00:01:01'
SPEC = importlib.util.spec_from_file_location('mesh_neighbor_count', TOOLS / 'mesh-neighbor-count.py')
neighbor_counter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(neighbor_counter)


class LedTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='manet-led-test-')
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.config = self.root / 'led.conf'
        self.config.write_text('LED_ENABLED=1\n')
        self.events = self.root / 'events'
        self.table = self.root / 'peers'
        self.table.write_text('[]')
        self.registry = self.root / 'registry'
        self.registry.write_text(
            f"NODE_{PEER.replace(':', '')}_MAC_ADDRESSES='{PEER},{RADIO}'\n"
            f"NODE_{OTHER.replace(':', '')}_MAC_ADDRESSES='{OTHER}'\n")
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep
                        + str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'],
                        TEST_ROOT=str(self.root), MANET_LED_CONFIG=str(self.config),
                        MANET_LED_LOCK_FILE=str(self.root / 'led.lock'),
                        BATCTL_PATH=str(self.bin / 'batctl'),
                        REGISTRY_STATE_FILE=str(self.registry))
        self.command('gpioinfo', """
record('gpioinfo')
sys.exit(int(os.environ.get('TEST_GPIOINFO_RC', '0')))
""")
        self.command('gpioset', """
import fcntl, signal
# Emulate GPIO's exclusive ownership and process lifetime, not electrical state.
lock = (root / 'gpio-owner').open('w')
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    record('COLLISION')
    sys.exit(1)
if os.environ.get('TEST_GPIOSET_FAIL'):
    sys.exit(1)
record('set ' + ' '.join(sys.argv[1:]))
pidfile = root / ('holder-' + str(os.getpid()))
pidfile.touch()
def stop(sig, frame):
    pidfile.unlink()
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while True:
    signal.pause()
""")
        self.command('sleep', """
record('sleep ' + sys.argv[1])
# Keep subprocess startup deterministic while accelerating multi-second dwells.
time.sleep(0.06)
""")
        self.command('batctl', """
if sys.argv[1:] != ['meshif', 'bat0', 'neighbors_json']:
    record('WRONG_BATCTL_QUERY')
    sys.exit(99)
record('query')
step = root / 'queries'
n = int(step.read_text()) if step.exists() else 0
step.write_text(str(n + 1))
if os.environ.get('TEST_BOOT_SEQUENCE'):
    if n == 0:
        sys.exit(1)
    print('[]' if n == 1 else json.dumps([{'neigh_address': '0c:bf:74:00:2b:f1'}]))
else:
    print((root / 'peers').read_text())
    sys.exit(int(os.environ.get('TEST_BATCTL_RC', '0')))
""")
        self.command('systemctl', """
record('service')
if os.environ.get('TEST_SERVICE_DOWN'):
    sys.exit(1)
sys.exit(0)
""")
        self.command('ip', "sys.exit(0)\n")
        self.command('gpiomon', """
record('button ' + ' '.join(sys.argv[1:]))
marker = root / 'pressed'
if marker.exists():
    sys.exit(1)
marker.touch()
""")

    def command(self, name, body):
        path = self.bin / name
        path.write_text(f'#!{sys.executable}\n' + """
import os, sys, time, json
from pathlib import Path
root = Path(os.environ['TEST_ROOT'])
def record(text):
    with (root / 'events').open('a') as log:
        log.write(text + '\\n')
""" + body)
        path.chmod(0o755)

    def run_script(self, name):
        return subprocess.run(['bash', str(TOOLS / name)], env=self.env,
                              capture_output=True, text=True, timeout=20)

    def history(self):
        return self.events.read_text() if self.events.exists() else ''

    def states(self):
        return [tuple(part.split('=')[1] for part in line.split()[-3:])
                for line in self.history().splitlines() if line.startswith('set ')]

    def assert_released(self):
        self.assertEqual(list(self.root.glob('holder-*')), [])
        with (self.root / 'led.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertNotIn('COLLISION', self.history())

    def test_unconfigured_harness_never_touches_gpio_or_queries_peers(self):
        self.config.unlink()
        for script in ('led-boot.sh', 'led-info.sh', 'button-monitor.sh'):
            with self.subTest(script=script):
                result = self.run_script(script)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.history(), '')

    def test_missing_gpio_exits_without_looping(self):
        self.env['TEST_GPIOINFO_RC'] = '1'
        for script in ('led-boot.sh', 'led-info.sh', 'button-monitor.sh'):
            result = self.run_script(script)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.history().splitlines(), ['gpioinfo'] * 3)

    def test_empty_table_shows_only_red_then_off(self):
        result = self.run_script('led-info.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.states(), [('1', '0', '0'), ('0', '0', '0')])
        self.assertIn('peer count = 0', result.stdout)
        self.assert_released()

    def test_one_blink_per_direct_node_across_radios_and_mac_case(self):
        for peers, count in [
            ([{'neigh_address': PEER, 'hard_ifname': 'halow0'}], 1),
            ([{'neigh_address': PEER, 'hard_ifname': 'halow0', 'best': True},
              {'neigh_address': PEER.upper(), 'hard_ifname': 'wlan0'},
              {'neigh_address': RADIO, 'hard_ifname': 'wlan1'}], 1),
            ([{'neigh_address': PEER, 'hard_ifname': 'halow0'},
              {'neigh_address': OTHER, 'hard_ifname': 'end0'}], 2),
        ]:
            with self.subTest(count=count):
                self.events.unlink(missing_ok=True)
                self.table.write_text(json.dumps(peers))
                result = self.run_script('led-info.sh')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.states().count(('0', '1', '0')), count)
                self.assertNotIn(('1', '0', '0'), self.states())
                self.assertNotIn('WRONG_BATCTL_QUERY', self.history())
                self.assert_released()

    def test_registry_only_nodes_and_multihop_routes_do_not_extend_blink_count(self):
        # A large registry and a large originator table do not make these
        # remote nodes neighbors. Only the live direct table controls the LED.
        self.registry.write_text(self.registry.read_text() + ''.join(
            f"NODE_02000000{n:04x}_MAC_ADDRESSES='02:00:00:00:{n >> 8:02x}:{n & 255:02x}'\n"
            for n in range(10, 110)))
        self.command('batctl', """
if sys.argv[1:] == ['meshif', 'bat0', 'neighbors_json']:
    print(json.dumps([{'neigh_address': '0c:bf:74:00:2b:f1', 'hard_ifname': 'halow0'}]))
elif sys.argv[1:] == ['meshif', 'bat0', 'originators_json']:
    record('UNWANTED_ORIGINATOR_QUERY')
    print(json.dumps([{'orig_address': '02:00:00:00:00:%02x' % n,
                      'neigh_address': '0c:bf:74:00:2b:f1'} for n in range(10, 110)]))
else:
    sys.exit(99)
""")
        result = self.run_script('led-info.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.states().count(('0', '1', '0')), 1)
        self.assertIn('peer count = 1', result.stdout)
        self.assertNotIn('UNWANTED_ORIGINATOR_QUERY', self.history())
        self.assert_released()

    def test_zero_and_single_neighbor_need_no_registry(self):
        self.registry.unlink()
        for rows, count in [([], 0), ([{'neigh_address': PEER}], 1)]:
            with self.subTest(count=count):
                self.table.write_text(json.dumps(rows))
                result = self.run_script('led-info.sh')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'peer count = {count}', result.stdout)
                self.assert_released()

    def test_unresolved_or_ambiguous_aliases_still_show_connected(self):
        self.table.write_text(json.dumps([{'neigh_address': PEER}, {'neigh_address': RADIO}]))
        for registry in [None, '',
                         f"NODE_0cbf74002bf1_MAC_ADDRESSES='{PEER},{RADIO}'\n"
                         f"NODE_020000000002_MAC_ADDRESSES='{RADIO}'\n",
                         "NODE_0cbf74002bf1_MAC_ADDRESSES='unclosed\n"]:
            with self.subTest(registry=registry):
                self.events.unlink(missing_ok=True)
                if registry is None:
                    self.registry.unlink(missing_ok=True)
                else:
                    self.registry.write_text(registry)
                result = self.run_script('led-info.sh')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('connected; neighbor count pending', result.stdout)
                self.assertEqual(self.states(), [('0', '1', '0'), ('0', '0', '0')])
                self.assertIn('sleep 3', self.history())
                self.assertNotIn('peer count =', result.stdout)
                self.assert_released()

    def test_boot_accepts_confirmed_connection_before_registry_arrives(self):
        self.registry.unlink()
        self.table.write_text(json.dumps([{'neigh_address': PEER}, {'neigh_address': RADIO}]))
        result = self.run_script('led-boot.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('direct connection confirmed; identities pending', result.stdout)
        self.assertIn('MESH_FORMING', result.stdout)
        self.assertIn('IDLE', result.stdout)
        self.assertEqual((self.root / 'queries').read_text(), '1')
        self.assert_released()

    def test_direct_query_is_bounded_and_timeout_is_not_zero(self):
        with patch.object(neighbor_counter.subprocess, 'run',
                          side_effect=subprocess.TimeoutExpired('batctl', 5)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                neighbor_counter.neighbor_addresses('/test/batctl')
        self.assertEqual(run.call_args.args[0], ['/test/batctl', 'meshif', 'bat0', 'neighbors_json'])
        self.assertEqual(run.call_args.kwargs['timeout'], 5)

    def test_helper_launch_error_is_not_a_connection(self):
        self.command('python3', 'sys.exit(2)\n')
        result = self.run_script('led-info.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('peer count unavailable', result.stdout)
        self.assertEqual(self.states(), [('1', '1', '0'), ('0', '0', '0')])
        self.assert_released()

    def test_failed_or_malformed_query_shows_unknown_not_zero(self):
        for table, status in [('[]', '1'), ('not-json', '0'), ('[{}]', '0')]:
            with self.subTest(table=table, status=status):
                self.events.unlink(missing_ok=True)
                self.table.write_text(table)
                self.env['TEST_BATCTL_RC'] = status
                result = self.run_script('led-info.sh')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.states(), [('1', '1', '0'), ('0', '0', '0')])
                self.assertIn('unavailable', result.stdout)
                self.assertNotIn('peer count = 0', result.stdout)
                self.assert_released()

    def test_boot_retries_unknown_and_zero_before_announcing_formation(self):
        self.env['TEST_BOOT_SEQUENCE'] = '1'
        result = self.run_script('led-boot.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'queries').read_text(), '3')
        self.assertLess(result.stdout.index('unavailable'), result.stdout.index('peer poll = 0'))
        self.assertLess(result.stdout.index('peer poll = 0'), result.stdout.index('MESH_FORMING'))
        self.assertIn(('1', '1', '0'), self.states())
        self.assertIn('IDLE', result.stdout)
        self.assert_released()

    def test_gpio_request_failure_stops_both_displays(self):
        self.env['TEST_GPIOSET_FAIL'] = '1'
        for script in ('led-boot.sh', 'led-info.sh'):
            with self.subTest(script=script):
                result = self.run_script(script)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('GPIO request failed', result.stderr)
                self.assertNotIn('MESH_FORMING', result.stdout)
                self.assert_released()

    def launch(self, script):
        proc = subprocess.Popen(['bash', str(TOOLS / script)], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        def cleanup():
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
        self.addCleanup(cleanup)
        return proc

    def wait_for(self, predicate, proc):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            if proc.poll() is not None:
                out, err = proc.communicate()
                self.fail(f'Process exited early: {out} {err}')
            time.sleep(0.01)
        self.fail('Timed out waiting for LED activity')

    def test_term_reaps_gpio_holder_and_releases_lock(self):
        self.env['TEST_SERVICE_DOWN'] = '1'
        proc = self.launch('led-boot.sh')
        self.wait_for(lambda: bool(list(self.root.glob('holder-*'))), proc)
        proc.terminate()
        out, err = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 143, out + err)
        self.assert_released()

    def test_button_sequence_can_run_during_boot_without_gpio_collisions(self):
        self.env['TEST_SERVICE_DOWN'] = '1'
        proc = self.launch('led-boot.sh')
        self.wait_for(lambda: bool(list(self.root.glob('holder-*'))), proc)
        self.table.write_text(json.dumps([{'neigh_address': PEER}]))
        result = self.run_script('led-info.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('peer count = 1', result.stdout)
        self.assertEqual(self.states().count(('0', '1', '0')), 1)
        proc.terminate()
        proc.communicate(timeout=5)
        self.assert_released()

    def test_button_press_runs_real_info_then_monitor_exits_on_gpio_error(self):
        self.table.write_text(json.dumps([{'neigh_address': PEER}]))
        result = self.run_script('button-monitor.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.states().count(('0', '1', '0')), 1)
        self.assertIn('--debounce-period 50ms', self.history())
        self.assertIn('gpiomon failed', result.stdout)
        self.assert_released()


class OnboardLedTests(unittest.TestCase):
    def test_provisioning_verdicts_and_legacy_marker(self):
        with tempfile.TemporaryDirectory(prefix='manet-onboard-led-test-') as folder:
            root = Path(folder)
            state = root / 'state'
            done = root / 'done'
            for led in ('PWR', 'ACT'):
                (root / led).mkdir()
                (root / led / 'max_brightness').write_text('255')
            for verdict, marker, red, green in [
                ('complete', False, ('none', '0'), ('heartbeat', '7')),
                ('incomplete', False, ('heartbeat', '7'), ('none', '255')),
                ('running', False, ('timer', '7'), ('timer', '7')),
                ('', False, ('timer', '7'), ('timer', '7')),
                ('', True, ('none', '0'), ('heartbeat', '7')),
            ]:
                with self.subTest(verdict=verdict, marker=marker):
                    state.write_text('STATE=' + verdict + '\n')
                    if marker:
                        done.touch()
                    else:
                        done.unlink(missing_ok=True)
                    for led in ('PWR', 'ACT'):
                        (root / led / 'trigger').write_text('timer')
                        (root / led / 'brightness').write_text('7')
                    env = dict(os.environ, MANET_LED_DIR=str(root),
                               MANET_PROVISION_STATE=str(state), MANET_PROVISION_DONE=str(done))
                    result = subprocess.run(['bash', str(TOOLS / 'manet-led-status.sh')],
                                            env=env, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    for led, expected in [('PWR', red), ('ACT', green)]:
                        self.assertEqual(tuple((root / led / field).read_text().strip()
                                               for field in ('trigger', 'brightness')), expected)


if __name__ == '__main__':
    unittest.main()
