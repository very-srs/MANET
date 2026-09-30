"""Unauthenticated envelopes cannot force unbounded KDF work across processes."""

import base64
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from cryptography.exceptions import InvalidTag
import manet_admin as admin
from manet_admin_limits import ReceiveBusy, ReceiveLimits, BURST


class AdminLimitTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(); self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.conf = self.root / 'mesh.conf'; self.conf.write_text('admin_password=secret-password\n')
        self.sender = admin.AdminTransport(self.conf, self.root / 'sender')
        self.receiver = admin.AdminTransport(self.conf, self.root / 'receiver')
        self.now = 100.0
        self.receiver.receive_limits.clock = lambda: self.now
        self.payload = {'kind': 'acs_probe'}

    def test_bad_envelope_cache_survives_receiver_restart_without_poisoning_salt(self):
        good = self.sender.seal_challenge(76, self.payload)
        bad = dict(good)
        bad['ciphertext'] = base64.b64encode(b'0' * 32).decode()
        with self.assertRaises(InvalidTag):
            self.receiver.open_challenge(76, bad)
        receiver = admin.AdminTransport(self.conf, self.root / 'receiver')
        receiver.receive_limits.clock = lambda: self.now
        with patch.object(admin, '_derive_key', wraps=admin._derive_key) as derive:
            with self.assertRaises(InvalidTag):
                receiver.open_challenge(76, bad)
            derive.assert_not_called()
        self.assertEqual(receiver.open_challenge(76, good).payload, self.payload)

    def test_password_change_invalidates_negative_cache(self):
        good = self.sender.seal_challenge(76, self.payload)
        self.conf.write_text('admin_password=wrong-password\n')
        with self.assertRaises(InvalidTag):
            self.receiver.open_challenge(76, good)
        self.conf.write_text('admin_password=secret-password\n')
        self.assertEqual(self.receiver.open_challenge(76, good).payload, self.payload)

    def test_global_budget_is_shared_and_replenishes_without_wall_clock(self):
        directory = self.root / 'limits'
        first = ReceiveLimits(directory, clock=lambda: self.now)
        second = ReceiveLimits(directory, clock=lambda: self.now)
        for _ in range(BURST):
            with first.derivation():
                pass
        with self.assertRaises(ReceiveBusy):
            with second.derivation():
                self.fail('budget should be exhausted')
        self.now += 1
        with patch('time.time', return_value=-100000):
            with second.derivation():
                pass

    def test_only_one_cross_process_derivation_can_hold_memory_budget(self):
        first = ReceiveLimits(self.root / 'limits')
        second = ReceiveLimits(self.root / 'limits', lock_timeout=0)
        with first.derivation():
            with self.assertRaises(ReceiveBusy):
                with second.derivation():
                    self.fail('parallel KDF must not run')

    def test_cached_keys_do_not_consume_derivation_budget(self):
        envelope = self.sender.seal_challenge(76, self.payload)
        with patch.object(self.receiver.receive_limits, 'derivation', side_effect=AssertionError('cache miss')):
            self.assertEqual(self.receiver.open_challenge(76, envelope).payload, self.payload)

    def test_authenticated_salt_survives_receiver_restart_without_spending_budget(self):
        envelope = self.sender.seal_challenge(76, self.payload)
        self.receiver.open_challenge(76, envelope)
        directory = self.receiver.receive_limits.directory
        (directory / 'budget.json').write_text(json.dumps({'when': self.now, 'tokens': 0}))
        with admin._KEY_LOCK:
            admin._KEYS.clear()  # Simulate a new receiver process.
        receiver = admin.AdminTransport(self.conf, self.root / 'receiver')
        receiver.receive_limits.clock = lambda: self.now
        self.assertEqual(receiver.open_challenge(76, envelope).payload, self.payload)
        self.assertEqual(json.loads((directory / 'budget.json').read_text())['tokens'], 0)
        self.assertNotIn('secret-password', ''.join(p.read_text() for p in directory.glob('*.json')))

    def test_derivation_does_not_block_negative_cache_reads(self):
        first = ReceiveLimits(self.root / 'limits')
        second = ReceiveLimits(self.root / 'limits')
        with first.derivation():
            self.assertFalse(second.rejected('unrelated'))
