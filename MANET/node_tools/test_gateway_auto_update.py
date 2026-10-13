"""Exercise update triggers using the existing real gateway-loop harnesses."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_gateway_selection
import test_gateway_startup


class GatewayAutoUpdateTests(unittest.TestCase):
    def run_with_updates(self, harness, *, enabled='y', queue_status=0, **options):
        """Add opt-in and systemctl fixtures to an existing network simulation."""
        run = subprocess.run
        starts = []

        def instrument(command, **kwargs):
            script = Path(command[1])
            root = script.parent
            conf = root / 'mesh.conf'
            conf.write_text(f'auto_update={enabled}\n')
            script.write_text(script.read_text().replace('/etc/mesh.conf', str(conf)))
            stub = root / 'bin/systemctl'
            stub.write_text(f'#!{sys.executable}\n' +
                            'import json, os, sys\nfrom pathlib import Path\n'
                            "root = Path(os.environ['TEST_ROOT'])\n"
                            "with (root / 'update-starts').open('a') as out:\n"
                            "    out.write(json.dumps([int((root / 'clock').read_text()), "
                            "sys.argv[1:]]) + '\\n')\n"
                            f'sys.exit({queue_status})\n')
            stub.chmod(0o755)
            result = run(command, **kwargs)
            calls = root / 'update-starts'
            if calls.exists():
                starts.extend(json.loads(line) for line in calls.read_text().splitlines())
            return result

        with patch.object(subprocess, 'run', side_effect=instrument):
            result = harness.run_manager(**options)
        for _, args in starts:
            self.assertEqual(args, ['start', '--no-block', 'manet-auto-update.service'])
        return result, [now for now, _ in starts]

    def test_new_route_queues_once_only_when_enabled(self):
        for enabled in ('y', 'YES ', '1', 'true', 'n', ''):
            with self.subTest(enabled=enabled):
                (events, route), starts = self.run_with_updates(
                    test_gateway_startup.GatewayStartupTests(), enabled=enabled)
                self.assertTrue(route)
                self.assertEqual(sum(e['kind'] == 'replace' for e in events), 1)
                self.assertEqual(starts, [] if enabled in ('n', '') else [0])

    def test_failed_install_does_not_queue_until_success(self):
        (_, route), starts = self.run_with_updates(
            test_gateway_startup.GatewayStartupTests(), fail_install=True)
        self.assertTrue(route)
        self.assertEqual(starts, [1])

    def test_existing_route_and_source_refresh_do_not_queue(self):
        for current in ('default via 10.44.0.11 dev br0 src 10.44.0.6',
                        'default via 10.44.0.11 dev br0 src 10.44.0.2',
                        'default via 192.168.1.1 dev end0'):
            with self.subTest(current=current):
                _, starts = self.run_with_updates(
                    test_gateway_startup.GatewayStartupTests(), current=current, polls=2)
                self.assertEqual(starts, [])

    def test_gateway_switch_does_not_queue_but_loss_and_regain_does(self):
        for next_at, expected in ((20, [0]), (30, [0, 30])):
            with self.subTest(next_at=next_at):
                installs, starts = self.run_with_updates(
                    test_gateway_selection.GatewaySelectionTests(), until=50,
                    gateways={'a': (0, 20, 30.0, 100.0),
                              'b': (next_at, 9999, 10.0, 100.0)})
                self.assertEqual(installs, [(0, 'a'), (next_at, 'b')])
                self.assertEqual(starts, expected)

    def test_queue_failure_keeps_route_manager_running(self):
        (events, route), starts = self.run_with_updates(
            test_gateway_startup.GatewayStartupTests(), queue_status=1)
        self.assertTrue(route)
        self.assertEqual(starts, [0])
        self.assertEqual(sum(e['kind'] == 'sleep' for e in events), 5)


if __name__ == '__main__':
    unittest.main()
