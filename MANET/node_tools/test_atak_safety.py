"""TAK server exclusions and independent phone/radio GPS evidence."""
import copy
from datetime import timedelta
import socket
import unittest
import struct
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

import manet_cot as cot
import test_atak_service as base
import test_atak_rewrite as rewrite
from test_tak_protocol import sa_fixture

atak = base.atak


def tcp_frame(port=8089, address=base.IP, mac=base.MAC, destination='203.0.113.8',
              flags=2, options=b'', ipv6=False):
    """Supply full TCP data so capture tests can prove payload is not retained."""
    tcp = struct.pack('!HHIIBBHHH', 40000, port, 1, 0, 0x50, flags, 5000, 0, 0)
    data = tcp + b'SECRET TLS OR PLAIN CONTENT' * 80
    ether = bytes.fromhex('020000000001' + mac.replace(':', ''))
    if ipv6:
        ip = struct.pack('!IHBB', 6 << 28, len(data), 6, 64) + socket.inet_pton(socket.AF_INET6, '2001:db8::2') + socket.inet_pton(socket.AF_INET6, '2001:db8::8')
        return ether + b'\x86\xdd' + ip + data
    ip = struct.pack('!BBHHHBBH4s4s', 0x45 + len(options) // 4, 0, 20 + len(options) + len(data),
                     1, 0x4000, 64, 6, 0, socket.inet_aton(address), socket.inet_aton(destination)) + options
    return ether + b'\x08\x00' + ip + data


class KernelTakPolicy:
    """Evaluate the emitted rule expressions with packet metadata and a clock."""
    def __init__(self):
        self.members = {}
        self.rules = [obj['rule'] for obj in atak.firewall_objects(('end0', 'wlan7')) if 'rule' in obj]

    def run(self, chain, packet, now, rewrite_members=()):
        self.members = {mac: end for mac, end in self.members.items() if now < end}
        def value(expr):
            if not isinstance(expr, dict):
                if expr == '@tak_phones':
                    return set(self.members)
                if expr == '@rewrite_phones':
                    return set(rewrite_members)
                return expr
            if 'set' in expr:
                return set(expr['set'])
            if 'concat' in expr:
                return tuple(value(v) for v in expr['concat'])
            if '&' in expr:
                return value(expr['&'][0]) & value(expr['&'][1])
            if 'meta' in expr:
                return packet.get(expr['meta']['key'])
            return packet.get((expr['payload']['protocol'], expr['payload']['field']))
        for rule in self.rules:
            if rule['family'] != 'bridge' or rule['chain'] != chain:
                continue
            for expr in rule['expr']:
                if 'match' in expr:
                    match = expr['match']
                    left, right = value(match['left']), value(match['right'])
                    op = match['op']
                    same = left in right if isinstance(right, set) else left == right
                    good = (same if op == '==' else not same if op == '!=' else
                            left is not None and (left <= right if op == '<=' else left >= right))
                    if not good:
                        break
                elif 'set' in expr:
                    update = expr['set']
                    assert update['op'] == 'update' and update['set'] == '@tak_phones'
                    self.members[value(update['elem'])] = now + 600
                elif 'drop' in expr:
                    return True
        return False

    def tcp(self, now, mac=base.MAC, port=8089, iface='end0', family='ip'):
        packet = {'iifname': iface, ('ether', 'saddr'): mac, ('ether', 'type'): family,
                  'l4proto': 'tcp', ('tcp', 'dport'): port, ('ip', 'frag-off'): 0}
        return self.run('prerouting', packet, now)

    def blocked(self, now, mac=base.MAC):
        packet = {'iifname': 'end0', 'oifname': 'bat0', 'l4proto': 'udp', 'length': 500,
                  ('ether', 'type'): 'ip', ('ether', 'saddr'): mac, ('ip', 'version'): 4,
                  ('ip', 'hdrlength'): 5, ('ip', 'frag-off'): 0, ('ip', 'length'): 500,
                  ('ip', 'daddr'): '239.2.3.1', ('udp', 'dport'): 6969, ('ip', 'saddr'): base.IP}
        return self.run('forward', packet, now, {('end0', mac, base.IP)})


class TakKernelTests(unittest.TestCase):
    def test_both_families_ports_and_renewal_while_userspace_dead(self):
        for family in ('ip', 'ip6'):
            for port in (8087, 8089):
                kernel = KernelTakPolicy()
                self.assertTrue(kernel.blocked(0))
                self.assertFalse(kernel.tcp(1, port=port, family=family))
                self.assertFalse(kernel.blocked(1))  # No userspace call or set removal.
                self.assertTrue(kernel.blocked(1, '02:00:00:00:00:03'))
                kernel.tcp(600, port=port, family=family)
                self.assertFalse(kernel.blocked(1199.99))
                self.assertTrue(kernel.blocked(1200))

    def test_only_local_ports_and_server_destinations_record_macs(self):
        for iface, port, expected in (('end0', 8089, True), ('wlan7', 8087, True),
                                      ('bat0', 8089, False), ('wlan0', 8089, False), ('end0', 443, False)):
            kernel = KernelTakPolicy()
            kernel.tcp(100, iface=iface, port=port)
            self.assertEqual(base.MAC in kernel.members, expected)

    def test_no_tcp_packet_wakes_userspace_even_with_large_payload(self):
        for ipv6 in (False, True):
            for port in (8087, 8089, 443):
                for flags in (2, 0x10, 0x18):
                    self.assertEqual(base.run_mesh_bpf(tcp_frame(port=port, flags=flags, ipv6=ipv6)), 0)
        self.assertEqual(base.run_mesh_bpf(tcp_frame(options=b'\x01' * 12)), 0)
        self.assertEqual(base.run_mesh_bpf(tcp_frame() + b'x' * 20000), 0)
        self.assertEqual(base.run_mesh_bpf(base.mesh_frame(b'example')), atak.MAX_MESH_FRAME)

    def test_policy_rejects_changed_set_scope_timeout_or_exemption(self):
        original = atak.firewall_objects(('end0', 'wlan7'))
        self.assertTrue(atak.valid_firewall({'nftables': original}, ('end0', 'wlan7')))
        for change in ({'timeout': 10}, {'flags': ['timeout']}, {'type': 'ipv4_addr'}, {'size': 4096}):
            bad = copy.deepcopy(original)
            next(x['set'] for x in bad if x.get('set', {}).get('name') == 'tak_phones').update(change)
            self.assertFalse(atak.valid_firewall({'nftables': bad}, ('end0', 'wlan7')))
        bad = copy.deepcopy(original)
        bad[-1]['rule']['expr'] = [expr for expr in bad[-1]['rule']['expr']
                                  if expr.get('match', {}).get('right') != '@tak_phones']
        self.assertFalse(atak.valid_firewall({'nftables': bad}, ('end0', 'wlan7')))
        for i, item in enumerate(original):
            if any('set' in expr for expr in item.get('rule', {}).get('expr', [])):
                for j in range(len(item['rule']['expr'])):
                    if item['rule']['expr'][j].get('match', {}).get('left') == atak.meta('l4proto'):
                        continue  # nft readback omits the implied TCP match.
                    bad = copy.deepcopy(original)
                    del bad[i]['rule']['expr'][j]
                    self.assertFalse(atak.valid_firewall({'nftables': bad}, ('end0', 'wlan7')))
        good = copy.deepcopy(original)
        obj = next(x['set'] for x in good if x.get('set', {}).get('name') == 'tak_phones')
        obj['flags'].reverse()
        obj['elem'] = [{'elem': {'val': base.MAC, 'expires': 599.5}}]
        self.assertTrue(atak.valid_firewall({'nftables': good}, ('end0', 'wlan7')))


def attr(kind, data):
    return struct.pack('=HH', len(data) + 4, kind) + data + b'\0' * (-len(data) % 4)


def nlmsg(kind, seq, body, flags=2, pid=0):
    return struct.pack('=IHHII', 16 + len(body), kind, flags, seq, pid) + body


def set_reply(seq, members, *, name='tak_phones'):
    elements = b''
    for mac, remaining in members:
        element = attr(1, attr(1, bytes.fromhex(mac.replace(':', ''))))
        element += attr(8, b'') + attr(5, struct.pack('!Q', remaining))
        elements += attr(1, element)
    body = b'\x07\0\0\x01' + attr(1, b'manet_atak\0') + attr(2, name.encode() + b'\0') + attr(3, elements)
    return nlmsg(0xa0c, seq, body)


class TakNetlinkTests(unittest.TestCase):
    def setUp(self):
        self.clock = base.FakeClock()
        self.events = atak.NetworkEvents(Mock(side_effect=base.FakeSocket), lambda: self.clock.now().mono)
        self.addCleanup(self.events.close)

    def inject(self, data, groups=0):
        self.events.sockets[1].incoming.append((data, [], 0, (0, groups)))
        return self.events.changed()

    def done(self):
        return self.inject(nlmsg(3, self.events.tak_pending, struct.pack('=i', 0)))

    def test_real_getsetelem_request_multipart_expiration_and_empty_dump(self):
        now = self.clock.now().mono
        self.events.read_tak(now)
        raw, dest = self.events.sockets[1].sent[-1]
        size, kind, flags, seq, pid = struct.unpack_from('=IHHII', raw)
        self.assertEqual((size, kind, flags, dest), (len(raw), 0xa0d, 0x301, (0, 0)))
        self.assertIn(b'tak_phones\0', raw)
        self.assertFalse(self.inject(set_reply(seq, [(base.MAC, 600000)])))
        self.assertIsNone(self.events.tak_members(now))  # Partial data is never authoritative.
        self.assertFalse(self.done())
        self.assertEqual(self.events.tak_members(now), {base.MAC: now + 600})
        self.events.read_tak(now + .5)
        self.assertEqual(len(self.events.sockets[1].sent), 1)
        self.clock.advance(5)
        self.events.read_tak(now + 5)
        self.assertFalse(self.done())  # Empty dump also terminates normally.
        self.assertEqual(self.events.tak_members(now + 5), {})
        self.assertIsNone(self.events.tak_members(now + 10))

    def test_missing_error_truncated_interrupted_duplicate_and_wrong_set_fail(self):
        for case in ('missing', 'error', 'short', 'interrupted', 'duplicate', 'wrong', 'overflow'):
            self.setUp()
            self.events.read_tak(100)
            seq = self.events.tak_pending
            with self.subTest(case=case), self.assertRaises(OSError):
                if case == 'missing':
                    self.events.read_tak(102)
                elif case == 'error':
                    self.inject(nlmsg(2, seq, struct.pack('=i', -2) + b'\0' * 16))
                elif case == 'short':
                    self.inject(nlmsg(0xa0c, seq, b'\x07\0\0\0' + b'\x01'))
                elif case == 'interrupted':
                    self.inject(nlmsg(3, seq, struct.pack('=i', 0), 2 | 16))
                elif case == 'duplicate':
                    self.inject(set_reply(seq, [(base.MAC, 1), (base.MAC, 1)]))
                elif case == 'wrong':
                    self.inject(set_reply(seq, [], name='rewrite_phones'))
                else:
                    self.events.tak_bytes = 128 * 1024
                    self.done()

    def test_external_notification_with_matching_sequence_still_invalidates(self):
        self.events.read_tak(100)
        self.assertTrue(self.inject(set_reply(self.events.tak_pending, []), groups=64))
        self.assertIsNotNone(self.events.tak_pending)
        self.assertFalse(self.done())

    def test_dump_waits_for_our_rewrite_ack(self):
        self.events.rewrite_members({('end0', base.IP, base.MAC)})
        self.events.read_tak(100)
        self.assertIsNone(self.events.tak_pending)
        self.assertEqual(len(self.events.sockets[1].sent), 1)


class RewriteNotificationTests(base.Harness):
    """Drive real dump/write encoding and guard invalidation on an admitted AP."""
    def setUp(self):
        super().setUp()
        self.fixture = base.APAdmissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        net = self.fixture.net
        (net / 'end0/carrier').write_text('0')
        (net / 'bat0').mkdir()
        (net / 'bat0/master').symlink_to('../br0')
        (net / 'bat0/carrier').write_text('1')
        (net / 'br0/brif/bat0').mkdir()
        (net / 'br0/brif/bat0/state').write_text('3')
        for name, index in (('br0', 2), ('end0', 3), ('bat0', 5), ('wlan7', 7)):
            (net / name / 'ifindex').write_text(str(index))
        self.fixture.fdb = [{'mac': base.MAC, 'ifname': 'wlan7', 'master': 'br0', 'flags': [], 'state': ''}]
        self.guard = self.fixture.cached_guard()
        self.events = atak.NetworkEvents(base.FakeSocket, lambda: self.clock.now().mono)
        self.addCleanup(self.events.close)
        self.guard.events = self.events
        self.nft = self.events.sockets[1]
        self.portid = 32100
        self.nft.getsockname = lambda: (self.portid, 64)
        self.proof = self.guard.current()
        self.network = atak.Network(base.FakeSocket, self.guard.current)
        self.addCleanup(self.network.close)
        self.network.events = self.events
        self.network.configure(self.proof)
        self.network.sync_mesh_ports(self.proof.local_ports)
        self.receive(iface='wlan7')
        self.assertTrue(self.service.has_live_lease(self.clock.now()))
        self.tick(rewrite.FIX)
        self.network.update_tak(self.service, self.clock.now(), request=True)  # New phone status.
        self.guard.runner.reset_mock()  # Exclude initial proof/admission commands.
        self.outbound = 0
        self.dumps = self.renewals = 0

    receive = rewrite.RewriteNetworkTests.receive

    def enqueue(self, packet, group=64):
        self.nft.incoming.append((packet, [], 0, (0, group)))

    def generation(self, seq, pid=None):
        return nlmsg(0xa0f, seq, b'\0\0\0\x01' + attr(1, struct.pack('!I', 1)),
                     flags=0, pid=self.portid if pid is None else pid)

    def kernel_replies(self, *, acks_first=False):
        """Return dump completion, element multicast, NEWGEN and ordinary ACKs."""
        for wire, _ in self.nft.sent[self.outbound:]:
            batch_seq = None
            notifications, acks = [], []
            while wire:
                length, kind, flags, seq, _ = struct.unpack_from('=IHHII', wire)
                body = wire[16:length]
                if kind == 0xa0d:
                    self.dumps += 1
                    self.enqueue(nlmsg(3, seq, struct.pack('=i', 0), pid=self.portid), 0)
                elif kind in (0xa0c, 0xa0e):
                    if batch_seq is None:
                        batch_seq = seq
                    # Linux emits per-element notifications with sequence zero.
                    element = attr(1, attr(1, b'\0' * 28))
                    names = b'\x07\0\0\x01' + attr(1, b'manet_atak\0') + attr(2, b'rewrite_phones\0')
                    notifications.append(nlmsg(kind, 0, names + attr(3, attr(1, element)),
                                               flags=0, pid=self.portid))
                elif kind == 17:
                    self.renewals += 1
                    # nfnetlink strips BEGIN before passing the original skb
                    # to commit, so NEWGEN uses the first operation's sequence.
                    self.last_batch_seq = batch_seq
                    notifications.append(self.generation(batch_seq))
                if flags & 4:
                    acks.append(nlmsg(2, seq, struct.pack('=i', 0) + wire[:16],
                                      flags=0, pid=self.portid))
                wire = wire[(length + 3) & ~3:]
            if acks_first:
                for ack in acks:
                    self.enqueue(ack, 0)
                # Drain ACKs separately: later notifications still belong to us.
                self.guard.current()
            for packet in notifications:
                self.enqueue(packet)
            if not acks_first:
                for ack in acks:
                    self.enqueue(ack, 0)
        self.outbound = len(self.nft.sent)

    def cycle(self, *, acks_first=False):
        now = self.clock.now()
        self.proof = self.guard.current(refresh=self.service.has_live_lease(now))
        self.network.configure(self.proof)
        self.network.update_tak(self.service, now)
        self.kernel_replies()
        self.guard.current()
        self.network.update_tak(self.service, now)
        self.network.handle([], now, self.service)
        self.tick(rewrite.FIX)
        self.network.refresh_rewrite(self.service, now)
        self.kernel_replies(acks_first=acks_first)
        self.guard.current()
        self.assertTrue(self.network.rewrite_members)
        self.assertFalse(self.events.rewrite_acks)

    def test_dump_renewal_own_multicast_then_external_newgen(self):
        self.cycle()
        self.clock.advance(1)
        self.cycle(acks_first=True)
        self.assertEqual((self.dumps, self.renewals), (1, 2))
        self.guard.runner.assert_not_called()
        # Same sequence and group but another originating port MUST invalidate.
        self.enqueue(self.generation(self.last_batch_seq, self.portid + 1))
        self.guard.current()
        self.assertEqual(self.guard.runner.call_count, 4)

    def test_present_phone_trusted_fix_sixty_seconds_zero_readback_commands(self):
        for step in range(300):
            self.clock.seconds = step / 5
            if step and step % 25 == 0:
                self.receive(iface='wlan7')
            self.cycle(acks_first=bool(step % 2))
        self.assertEqual((self.dumps, self.renewals), (12, 60))
        self.assertTrue(self.service.has_live_lease(self.clock.now()))
        self.assertIsNotNone(self.service.rewrite_fix)
        self.guard.runner.assert_not_called()
        self.assertLessEqual(len(self.events.rewrite_notifications), 32)

    def test_external_unknown_and_dump_sequence_notifications_still_invalidate(self):
        self.cycle()
        old_seq = self.last_batch_seq
        dump_seq = next(seq for wire, _ in self.nft.sent
                        for _, kind, _, seq, _ in [struct.unpack_from('=IHHII', wire)]
                        if kind == 0xa0d)
        names = b'\x07\0\0\x01' + attr(1, b'manet_atak\0')
        for packet, group in (
                (self.generation(old_seq, 0), 64),
                (self.generation(old_seq, self.portid + 1), 64),
                (self.generation(999999), 64),
                (self.generation(dump_seq), 64),
                (self.generation(old_seq), 0),
                (nlmsg(0xa06, old_seq, names, flags=0, pid=self.portid), 64),
                (nlmsg(0xa0c, 0, names + attr(2, b'tak_phones\0') + attr(3, b''), flags=0, pid=self.portid), 64),
                (nlmsg(0xa0c, 0, names + attr(2, b'rewrite_phones\0') + attr(3, b''), flags=0, pid=self.portid + 1), 64),
                (nlmsg(0xa0c, dump_seq, names + attr(2, b'rewrite_phones\0') + attr(3, b''), flags=0, pid=self.portid), 64),
                (nlmsg(0xa0f, old_seq, b'\0\0\0\x01\x01', flags=0, pid=self.portid), 64)):
            with self.subTest(packet=packet, group=group):
                self.guard.runner.reset_mock()
                self.enqueue(packet, group)
                self.guard.current()
                self.assertEqual(self.guard.runner.call_count, 4)
        # Expired sequence history cannot exempt an unrelated later event.
        for _ in range(9):
            self.clock.advance(1)
            self.cycle()
        self.guard.runner.reset_mock()
        self.enqueue(self.generation(old_seq))
        self.guard.current()
        self.assertEqual(self.guard.runner.call_count, 4)

    def test_idle_present_phone_has_no_timer_reads_and_status_change_creates_demand(self):
        for _ in range(60):
            self.cycle()
            self.clock.advance(1)
        self.assertEqual(self.dumps, 1)
        self.assertEqual(self.renewals, 60)
        self.guard.runner.assert_not_called()
        self.network.update_tak(self.service, self.clock.now(), request=True)
        self.cycle()
        self.assertEqual(self.dumps, 2)
        self.network.update_tak(self.service, self.clock.now(), request=True)
        self.cycle()
        self.assertEqual(self.dumps, 2)  # Several status changes share one cache.

    def test_sparse_sa_waits_for_fresh_dump_then_rewrites_same_frame(self):
        self.cycle()
        for _ in range(3):
            for second in range(20):
                self.clock.advance(1)
                self.cycle()
            before = self.dumps
            self.receive(iface='wlan7')
            self.assertEqual(len(self.network.mesh_pending), 1)
            self.cycle()
            self.assertEqual(self.dumps, before + 1)
            self.assertFalse(self.network.mesh_pending)
            packet = atak.mesh_sa_frame(self.network.raw_sender.sent[-1][0])[2]
            self.assertEqual(base.tak.decode_mesh_sa(packet).lat, rewrite.FIX.lat)

    def test_missing_demand_reply_times_out_and_teardown_forwards_original(self):
        self.cycle()
        self.clock.advance(5)
        self.tick(rewrite.FIX)
        frame = self.receive(iface='wlan7')
        self.assertEqual(len(self.network.mesh_pending), 1)
        self.clock.advance(2)
        with self.assertRaisesRegex(OSError, 'timed out'):
            self.network.update_tak(self.service, self.clock.now())
        sender = self.network.raw_sender
        self.network.close()
        self.assertEqual(sender.sent[-1][0], frame)

    def test_sa_expiring_while_waiting_for_dump_is_forwarded_original(self):
        self.cycle()
        self.clock.advance(5)
        self.tick(rewrite.FIX)
        root = ET.fromstring(sa_fixture(source='GPS', stamp=self.phone_time())[1])
        root.set('stale', cot._stamp(self.phone_time() + timedelta(seconds=1)))
        frame = self.receive(ET.tostring(root), iface='wlan7')
        self.assertEqual(len(self.network.mesh_pending), 1)
        self.clock.advance(1)
        self.kernel_replies()
        self.guard.current()
        self.network.update_tak(self.service, self.clock.now())
        self.network.handle([], self.clock.now(), self.service)
        self.assertEqual(self.network.raw_sender.sent[-1][0], frame)

    def test_pending_frame_uses_original_after_path_or_fix_loss(self):
        for loss in ('path', 'fix'):
            with self.subTest(loss=loss):
                self.setUp()
                self.cycle()
                self.clock.advance(5)
                self.tick(rewrite.FIX)
                frame = self.receive(iface='wlan7')
                self.kernel_replies()
                self.guard.current()
                self.network.update_tak(self.service, self.clock.now())
                if loss == 'path':
                    self.network.configure(None)
                else:
                    self.service.rewrite_fix = None
                self.network.handle([], self.clock.now(), self.service)
                self.assertEqual(self.network.raw_sender.sent[-1][0], frame)

    def test_own_notifications_do_not_replace_ack_or_hide_error(self):
        self.cycle()
        self.events.rewrite_members({('wlan7', base.IP, base.MAC)})
        pending = set(self.events.rewrite_acks)
        self.enqueue(self.generation(min(pending)))
        self.guard.current()
        self.assertEqual(self.events.rewrite_acks, pending)
        self.guard.runner.assert_not_called()
        with self.assertRaisesRegex(OSError, 'acknowledgement'):
            self.events.rewrite_members(set())
        self.enqueue(nlmsg(2, min(pending), struct.pack('=i', -1) + b'\0' * 16,
                           flags=0, pid=self.portid), 0)
        with self.assertRaisesRegex(atak.GuardFailure, 'transaction failed'):
            self.guard.current()


class TakServerTests(base.Harness):
    def setUp(self):
        super().setUp()
        self.proof = atak.Proof(base.LOCAL, {base.IP: base.MAC}, 'generation',
            interfaces={base.IP: 'end0'}, local_ports={'end0': ('wired', '3')}, mesh_path=('2', '5'))
        self.kernel = KernelTakPolicy()
        self.network = atak.Network(Mock(side_effect=base.FakeSocket))
        self.addCleanup(self.network.close)
        self.network.configure(self.proof)
        self.network.sync_mesh_ports(self.proof.local_ports)
        self.network.events = Mock(tak_pending=None)
        self.network.events.tak_members.side_effect = lambda now: dict(self.kernel.members)
        self.receive()
        self.tick(rewrite.FIX)
        self.network.refresh_rewrite(self.service, self.clock.now())

    receive = rewrite.RewriteNetworkTests.receive

    def test_kernel_exempts_before_userspace_learns_and_pending_frame_is_original(self):
        self.kernel.tcp(self.clock.now().mono)
        self.assertFalse(self.kernel.blocked(self.clock.now().mono))
        self.assertTrue(self.network.rewrite_members)
        self.network.update_tak(self.service, self.clock.now())
        frame = self.receive()  # May have been captured just before the MAC was added.
        self.assertEqual(self.network.raw_sender.sent[-1][0], frame)
        self.network.refresh_rewrite(self.service, self.clock.now())
        self.assertFalse(self.network.rewrite_members)
        self.assertTrue(self.tick(rewrite.FIX)['phones'][0]['tak_server'])
        self.clock.advance(3.01)
        frame = self.receive()  # Pending queue still gets originals after lease expiry.
        self.assertEqual(self.network.raw_sender.sent[-1][0], frame)
        self.network.handle([], self.clock.now(), self.service)
        count = len(self.network.raw_sender.sent)
        self.receive()
        self.assertEqual(len(self.network.raw_sender.sent), count)

    def test_cached_snapshot_race_is_not_misrepresented_as_atomic(self):
        # A new kernel insertion after a read cannot retroactively change an
        # already captured frame. Native forwarding opens immediately, while
        # userspace may still send a replacement until the next bounded read.
        self.kernel.tcp(self.clock.now().mono)
        self.assertFalse(self.kernel.blocked(self.clock.now().mono))
        self.clock.advance(.2)
        self.network.events.tak_members.side_effect = None
        self.network.events.tak_members.return_value = {}  # Snapshot still predates insertion.
        self.receive()
        self.assertEqual(base.tak.decode_mesh_sa(atak.mesh_sa_frame(self.network.raw_sender.sent[-1][0])[2]).lat, rewrite.FIX.lat)
        self.clock.advance(5)
        self.network.events.tak_members.return_value = dict(self.kernel.members)
        self.network.update_tak(self.service, self.clock.now())
        frame = self.receive()
        self.assertEqual(self.network.raw_sender.sent[-1][0], frame)

    def test_other_phone_still_rewrites_and_hold_expiry_restores_member(self):
        rewrite.RewriteNetworkTests.test_guest_on_wifi_is_rewritten_without_receiving_a_local_copy(self)
        self.kernel.tcp(self.clock.now().mono)
        self.network.refresh_rewrite(self.service, self.clock.now())
        self.assertEqual(self.network.rewrite_members, {('wlan7', '192.0.2.3', '02:00:00:00:00:03')})
        self.clock.advance(599.9)
        self.receive()
        self.tick(rewrite.FIX)
        self.network.refresh_rewrite(self.service, self.clock.now())
        self.assertFalse(self.network.rewrite_members)
        self.clock.advance(.1)
        self.kernel.blocked(self.clock.now().mono)  # Prune expired kernel element.
        self.receive()
        self.tick(rewrite.FIX)
        self.network.refresh_rewrite(self.service, self.clock.now())
        self.assertIn(('end0', base.IP, base.MAC), self.network.rewrite_members)

    def test_expired_snapshot_defers_frame_and_reuses_existing_native_safe_lease(self):
        self.network.events.tak_members.return_value = None
        self.network.events.tak_members.side_effect = None
        self.network.refresh_rewrite(self.service, self.clock.now())
        self.assertTrue(self.network.rewrite_members)
        self.clock.advance(1)
        frame = self.receive()
        self.assertEqual(self.network.mesh_pending['end0'][0], frame)
        self.assertNotIn(self.network.mesh_sockets['end0'][1], self.network.readers())
        self.network.events.read_tak.assert_called_with(self.clock.now().mono)
        self.network.events.tak_members.return_value = {base.MAC: self.clock.now().mono + 600}
        self.network.update_tak(self.service, self.clock.now())
        self.network.handle([], self.clock.now(), self.service)
        self.assertEqual(self.network.raw_sender.sent[-1][0], frame)
        self.assertFalse(self.network.mesh_pending)

    def test_pending_dump_defers_generation_change_and_capture_loss_uses_three_seconds(self):
        self.network.events.tak_pending = 123
        self.network.events.rewrite_members.reset_mock()
        self.clock.advance(1)
        self.network.refresh_rewrite(self.service, self.clock.now())
        self.network.events.rewrite_members.assert_not_called()
        self.network.events.tak_pending = None
        self.network.mesh_sockets['end0'][1].dropped = 1
        frame = self.receive()
        self.assertEqual(self.network.raw_sender.sent[-1][0], frame)
        self.assertEqual(self.network.rewrite_suspended_until, self.clock.now().mono + 3)
        self.network.refresh_rewrite(self.service, self.clock.now())
        self.assertFalse(self.network.rewrite_members)


class DisagreementTests(base.Harness):
    def observe(self, metres=0, source='GPS', wire=0):
        lat = 40 + metres / 111195.08
        packet = sa_fixture(source=source, point=(lat, -105), stamp=self.phone_time())[wire]
        self.service.ingest(base.tak.decode_mesh_sa(packet), base.IP, self.proof, self.clock.now())
        return self.tick(cot.Fix(40, -105, 'gnss'))

    def notices(self):
        return [root for root, _ in self.tx.sent if root.get('type') == 'b-t-f' and
                'Radio and phone GPS disagree' in ''.join(root.itertext())]

    def test_above_below_threshold_one_chat_log_no_audio_or_trust_change(self):
        with patch.object(atak, 'log') as log:
            for wire in (0, 1):
                self.clock.advance(1)
                self.assertIsNone(self.observe(49, wire=wire)['phones'][0]['gps_disagreement_m'])
            self.assertFalse(self.notices())
            state = (self.service.position._distrusted, self.service.position._overridden,
                     self.service.position._position_confirmed, self.service.position._fault_epoch)
            for second in range(10):
                self.clock.advance(1)
                status = self.observe(51)
                self.assertEqual(status['phones'][0]['gps_disagreement_m'], 51)
            self.assertEqual(len(self.notices()), 1)
            self.assertEqual(log.call_count, 1)
            self.assertFalse(self.service.audio)
            self.assertEqual(state, (self.service.position._distrusted, self.service.position._overridden,
                                    self.service.position._position_confirmed, self.service.position._fault_epoch))
            self.assertTrue(status['position_trusted'])

    def test_thirty_seconds_close_agreement_rearms_but_gap_and_deadband_do_not(self):
        self.observe(100)
        for elapsed, distance in ((1, 0), (29, 0), (1, 40), (1, 0), (29, 0), (1, 100)):
            self.clock.advance(elapsed)
            self.observe(distance)
        self.assertEqual(len(self.notices()), 1)
        self.clock.advance(1)
        self.observe(0)
        for _ in range(30):
            self.clock.advance(1)
            self.observe(0)
        self.clock.advance(1)
        self.observe(100)
        self.assertEqual(len(self.notices()), 2)

    def test_no_comparison_without_phone_gps_fresh_radio_and_trust(self):
        self.observe(100)
        count = len(self.notices())
        self.clock.advance(1)
        self.assertIsNone(self.observe(100, source='MANET:node')['phones'][0]['gps_disagreement_m'])
        self.clock.advance(1)
        self.observe(100)
        self.tick(cot.Fix(40, -105, 'gnss'), state='GNSS_SUSPECTED')
        self.assertTrue(self.service.position._distrusted)
        self.clock.advance(1)
        self.observe(0)
        self.clock.advance(31)
        self.observe(0)
        self.assertTrue(self.service.position._distrusted)
        self.assertIsNone(self.service.disagreements['synthetic-phone']['distance'])
        self.assertEqual(len(self.notices()), count)

    def test_absence_and_expired_gps_clear_note_without_rearming_episode(self):
        self.observe(100)
        self.clock.advance(31)
        self.tick(cot.Fix(40, -105, 'gnss'), proof=False)
        self.assertIsNone(self.service.disagreements['synthetic-phone']['distance'])
        self.clock.advance(50)
        self.tick(cot.Fix(40, -105, 'gnss'))
        self.assertIsNone(self.service.disagreements['synthetic-phone']['distance'])
        self.observe(100)
        self.assertEqual(len(self.notices()), 1)

    def test_send_failure_does_not_repeat(self):
        self.tx.fail_ports.add(4242)
        self.observe(100)
        self.tx.fail_ports.clear()
        self.clock.advance(1)
        self.observe(100)
        self.assertFalse(self.notices())
        self.assertFalse(self.service.audio)
