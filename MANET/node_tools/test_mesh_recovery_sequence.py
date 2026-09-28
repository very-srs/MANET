"""Independent ACS runners sharing a lossy, partitionable Alfred transport."""
from contextlib import contextmanager
import json
import os
import time
import unittest
from unittest.mock import patch

from test_acs import AcsHarness, OWN, PEER, THIRD, node, runtime
from test_acs_bootstrap import frame
import manet_acs_agreement as protocol
from manet_recovery_status import recovery_status


class MeshRecoverySequenceTests(unittest.TestCase):
    def setUp(self):
        self.macs = [OWN, PEER, THIRD]
        self.nodes = []
        self.runners = []
        self.connected = True
        self.drop = set()
        self.now = int(time.time()) // 180 * 180 + 25
        for index, mac in enumerate(self.macs):
            harness = AcsHarness()
            harness.setUp()
            self.addCleanup(harness.doCleanups)
            harness.configure(2437, None)
            harness.env['MANET_TIME_RUN_DIR'] = str(harness.root)
            harness.env['TEST_NOW'] = str(self.now)
            (harness.root / 'net/br0/address').write_text(mac)
            (harness.root / 'boot').write_text(chr(ord('a') + index) * 32)
            (harness.roles / 'halow_if').write_text('wlan2')
            harness.reports([[{'channel': 2437, 'noise_floor': -95, 'busy_pct': 95},
                              {'channel': 2462, 'noise_floor': -95, 'busy_pct': 0}]] * 3, self.macs)
            with harness.registry.open('a') as stream:
                stream.write(''.join(node(peer) for peer in self.macs))
            harness.command('alfred', '''
kind = sys.argv[-1]
root = Path(os.environ['TEST_ROOT'])
if sys.argv[1] == '-r':
    path = root / ('wire-' + kind)
    print(path.read_text() if path.exists() else '')
else:
    data = sys.stdin.read()
    (root / ('sent-' + kind)).write_text(data)
    with (root / 'traffic').open('a') as out:
        out.write(kind + ' ' + str(len(data.encode())) + '\\n')
''')
            self.nodes.append(harness)
            with patch.dict(os.environ, harness.env):
                self.runners.append(runtime.Runtime())

    @contextmanager
    def environment(self, index, when):
        with patch.dict(os.environ, self.nodes[index].env), \
                patch.object(runtime.time, 'time', return_value=when), \
                patch.object(runtime.time, 'time_ns', return_value=when * 10**9), \
                patch.object(runtime.rendezvous, 'halow_ready', return_value=self.connected):
            yield

    def step(self, when):
        # Delivery is a snapshot, so the loop order cannot shortcut replication.
        mail = {}
        for sender, harness in enumerate(self.nodes):
            for kind in (74, 76, 77):
                path = harness.root / f'sent-{kind}'
                if path.exists():
                    mail[sender, kind] = json.loads(path.read_text())
        for receiver, harness in enumerate(self.nodes):
            peers = [mac for mac in self.macs if mac != self.macs[receiver]] if self.connected else []
            (harness.root / 'peers').write_text(json.dumps([
                {'orig_address': mac, 'hard_ifname': 'wlan2', 'best': True} for mac in peers]))
            for kind in (74, 76, 77):
                received = ''.join(frame(self.macs[sender], envelope)
                    for (sender, msg_type), envelope in mail.items()
                    if msg_type == kind and sender != receiver and self.connected
                    and (sender, receiver, kind) not in self.drop)
                (harness.root / f'wire-{kind}').write_text(received)
            with self.environment(receiver, when):
                self.runners[receiver].tick(when)

    def start_round(self):
        for runner in self.runners:
            runner.request_path.write_text(json.dumps({'round': self.now // 180}))
        for offset in (0, 5, 10, 15, 20):
            self.step(self.now + offset)

    def frequency(self, index):
        return self.runners[index].interfaces()['2.4'][2]

    def restart(self, index, when):
        with self.environment(index, when):
            self.runners[index] = runtime.Runtime()

    def test_simultaneous_start_dropped_ack_restart_and_straggler_recovery(self):
        # The third node hears no agreement traffic. Its missing vote cannot
        # stop the other two, and its cached identity stays in the denominator.
        self.drop = {(sender, 2, 74) for sender in (0, 1)} | {(2, receiver, 74) for receiver in (0, 1)}
        self.start_round()
        plan = self.runners[0].state['protocol']['plan']
        self.assertEqual(len(plan['participants']), 3)
        for when in range(self.now + 25, plan['deadline'] + 1, 5):
            self.step(when)
        self.assertEqual(len(self.runners[0].state['protocol']['commit']['approvals']), 2)
        self.step(plan['deadline'] + 5)
        self.restart(1, plan['deadline'] + 6)
        for when in range(plan['deadline'] + 10, plan['activate_at'] + 1, 5):
            self.step(when)
        self.assertEqual([self.frequency(i) for i in range(3)], [2462, 2462, 2437])
        self.drop.clear()
        self.step(plan['activate_at'] + 35)
        self.step(plan['activate_at'] + 40)
        self.assertEqual([self.frequency(i) for i in range(3)], [2462] * 3)
        with self.environment(2, plan['activate_at'] + 40):
            report = recovery_status({'acs': 'y'}, {}, 'third')
        self.assertIn('via HaLow', report['summary'])
        changes = (self.nodes[2].root / 'manet-last-channel-change.json').read_text()
        self.assertIn('Returning to the connected mesh channel plan', changes)

    def test_complete_link_loss_then_reconnection_recovers_a_stray_channel(self):
        self.start_round()
        plan = self.runners[0].state['protocol']['plan']
        for when in range(self.now + 25, plan['activate_at'] + 1, 5):
            self.step(when)
        self.assertEqual([self.frequency(i) for i in range(3)], [2462] * 3)
        self.connected = False
        self.nodes[2].configure(2437, None)
        self.restart(2, plan['activate_at'] + 35)
        self.step(plan['activate_at'] + 40)
        self.assertEqual(self.frequency(2), 2437)
        self.connected = True
        for offset in (70, 75, 80):
            self.step(plan['activate_at'] + offset)
        self.assertEqual([self.frequency(i) for i in range(3)], [2462] * 3)

    def test_missing_majority_times_out_and_stable_ticks_bound_announcements(self):
        self.drop = {(sender, receiver, 74) for sender in (1, 2) for receiver in (0,)}
        self.start_round()
        plan = self.runners[0].state['protocol']['plan']
        for when in range(self.now + 25, plan['activate_at'] + 1, 5):
            self.step(when)
        self.assertNotIn('commit', self.runners[0].state['protocol'])
        self.assertEqual([self.frequency(i) for i in range(3)], [2437] * 3)
        # Once the round is idle, repeated one-second ticks must not turn
        # the existing five-second advertisement into a per-tick stream.
        start = plan['activate_at'] + 10
        self.step(start)
        before = [len((node.root / 'traffic').read_text().splitlines()) for node in self.nodes]
        for offset in range(1, 11):
            self.step(start + offset)
        after = [len((node.root / 'traffic').read_text().splitlines()) for node in self.nodes]
        self.assertEqual([a - b for a, b in zip(after, before)], [2, 2, 2])


if __name__ == '__main__':
    unittest.main()
