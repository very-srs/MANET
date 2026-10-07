"""R4 service integration: real cores/wire/solver, fake radio and clocks only."""
import asyncio
import base64
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
import encoder
import manet_locate as locate
import manet_ranging as ranging
import manet_spoof as spoof
import NodeInfo_pb2 as node_pb
import ranging_pb2 as pb


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


svc = module('manet_positioning_r4', 'manet-positioning.py')
atak = module('manet_atak_r4_consumer', 'manet-atak.py')


def status(now, *, has_fix=True, devices=None):
    return {'schema': 2, 'boot_id': 'boot', 'written_boot': now.boot,
            'clock': 'monotonic_raw', 'has_fix': has_fix,
            'devices': ['/dev/ttyGPS'] if devices is None else devices,
            'first_fix_mono': now.raw - 10 if has_fix else None,
            'time_ok': has_fix, 'events': [],
            'sample': {'mode': 3 if has_fix else 1, 'lat': 39.7, 'lon': -105.,
                       'mono': now.raw - .2, 'alt_hae': None,
                       'alt_msl': 1600, 'eph': .001, 'epv': .001, 'hdop': .001}}


class InputTests(unittest.TestCase):
    def test_receiver_raw_clock_mapping_and_unknown_height_accuracy(self):
        now = svc.Now(100, 1234, 9999)
        doc = status(now)
        receiver = svc.receiver_document(doc, now, 'boot')
        self.assertAlmostEqual(receiver.observed, 99.8)
        self.assertIsNone(receiver.fix['hae'])
        s = svc.Service(svc.Config('P', True), svc.HelperRadio(), lambda: now, 'boot')
        s.update(receiver)
        p = s.position_snapshot()
        self.assertFalse(p.HasField('altitude_cm'))
        self.assertFalse(p.HasField('fix_time_unix_ms'))
        self.assertEqual(p.horizontal_uncertainty_cm, 500)
        self.assertAlmostEqual(s.position_document()['radius_m'], 5 * locate.CE95)

    def test_reject_wrong_boot_future_stale_and_nonfinite(self):
        now = svc.Now(100, 200, 300)
        for field, value in [('boot_id', 'other'), ('clock', 'monotonic'),
                             ('written_boot', 301), ('written_boot', 294),
                             ('written_boot', float('nan'))]:
            doc = status(now)
            doc[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                svc.receiver_document(doc, now, 'boot')
        for age in (-1, 5, 100):
            doc = status(now)
            doc['sample']['mono'] = now.raw - age
            self.assertIsNone(svc.receiver_document(doc, now, 'boot').fix)

    def test_cold_start_and_lost_fix(self):
        now = [svc.Now(0, 20, 0)]
        s = svc.Service(svc.Config('P', True), svc.HelperRadio(), lambda: now[0], 'boot')
        s.update(svc.Receiver(devices=True))
        self.assertFalse(s.fallback_due())
        now[0] = svc.Now(179, 199, 179)
        self.assertFalse(s.fallback_due())
        now[0] = svc.Now(180, 200, 180)
        self.assertTrue(s.fallback_due())
        now[0] = svc.Now(0, 20, 0)
        for receiver in [svc.Receiver(devices=False), svc.Receiver(jammed=True),
                         svc.Receiver(had_fix=True)]:
            s.update(receiver)
            self.assertTrue(s.fallback_due())

    def test_cold_start_neighbour_timeout_and_reset(self):
        sim = svc.Simulation()
        self.addCleanup(sim.close)
        sim.inputs()
        s = sim.services['P']
        s.clock = lambda: svc.Now(sim.now, sim.now + 321, sim.now - 200)
        s.update(svc.Receiver(devices=True))
        self.assertFalse(s.fallback_due())
        sim.now += 59
        self.assertFalse(s.fallback_due())
        sim.now += 1
        self.assertTrue(s.fallback_due())
        s.update(svc.Receiver(devices=True), {})
        self.assertFalse(s.fallback_due())

    def test_config_opt_in_policy_switches_and_sim_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mesh.conf'
            path.write_text('atak=y\n')
            self.assertFalse(svc.Config.load(path).enabled)
            path.write_text('positioning=y\npositioning_node_id=P\npositioning_phone_best_guess=y\npositioning_mark_priority=newer\n')
            c = svc.Config.load(path)
            self.assertTrue(c.enabled and c.phone_best_guess)
            self.assertEqual(c.mark_priority, 'newer')
        self.assertFalse(svc.Config('P').phone_best_guess)
        self.assertEqual(svc.Config('P').mark_priority, 'manual')
        with self.assertRaises(ValueError):
            svc.make_radio(svc.Config('P', True, 'simulated'))
        with patch.object(svc.radio_api, 'RadioAdapter', side_effect=AssertionError('privilege crossing')):
            radio = svc.make_radio(svc.Config('P', True))
            self.assertFalse(radio.can_range)
            with self.assertRaises(svc.radio_api.AssociationUnavailable):
                radio.arm_responder(None, b'S' * 16, 20)


class DiscoveryTests(unittest.TestCase):
    primary, station = '02:00:00:00:00:01', '02:00:00:00:05:01'

    @staticmethod
    def record(mac, message):
        return '{ "' + mac + '", "' + base64.b64encode(message.SerializeToString()).decode() + '" },\n'

    def setUp(self):
        self.discovery = svc.Discovery(svc.Config('P', True))
        self.identity = self.record(self.primary, node_pb.NodeIdentity(
            mac_addresses=[bytes.fromhex(self.station.replace(':', ''))]))
        self.t = node_pb.NodeTelemetry(last_seen_timestamp=1)
        self.t.location.CopyFrom(node_pb.NodeTelemetry.GpsLocation(
            latitude_e7=397000000, longitude_e7=-1050000000, source=1,
            uncertainty_m=2.448, valid=True, quality='good', anchor_eligible=True))
        self.bat = f'{self.station} 0.100s [wlan1]\n'
        self.iw = f'Station {self.station} (on wlan1)\n\tsignal: -60 [-60] dBm\n'

    def parse(self, when=100, **kwargs):
        return self.discovery.parse(self.identity, self.record(self.primary, self.t),
                                    kwargs.get('bat', self.bat), kwargs.get('iw', self.iw), when)

    def test_identity_alias_and_intersection_only(self):
        n = self.parse()[self.primary]
        self.assertEqual(n.peer.mac.hex(':'), self.station)
        self.assertAlmostEqual(n.anchor.h_sigma_m, 1, places=5)
        self.assertIsNone(n.anchor.hae)
        self.assertFalse(self.parse(bat=self.bat.replace('wlan1', 'wlan2')))
        self.assertFalse(self.parse(iw=''))
        # Alfred geography is never inserted into a consistency monitor.
        s = svc.Service(svc.Config('P', True), svc.HelperRadio(), lambda: svc.Now(100, 100, 100), 'boot')
        s.update(svc.Receiver(), self.parse(), mesh=True)
        self.assertNotIn(self.primary, s.monitor.samples)

    def test_unchanged_telemetry_ages_without_trusting_wall_clock(self):
        self.assertTrue(self.parse(100))
        self.assertTrue(self.parse(200))
        self.assertFalse(self.parse(341))
        self.t.last_seen_timestamp = 2  # Deliberately remote clock in 1970.
        self.assertTrue(self.parse(342))

    def test_good_ranged_only_and_transitive_loop_rejection(self):
        p = self.t.location
        p.source, p.generation = p.RANGED, 1
        p.used_node_ids.append('A')
        self.assertIsNotNone(self.parse()[self.primary].anchor)
        p.quality = 'best_guess'
        self.assertIsNone(self.parse()[self.primary].anchor)
        p.quality = 'good'
        p.used_node_ids.append('P')
        self.assertIsNone(self.parse()[self.primary].anchor)

    def test_missing_provenance_and_malformed_alfred_are_not_anchors(self):
        self.t.location.ClearField('source')
        self.assertIsNone(self.parse()[self.primary].anchor)
        self.assertFalse(svc.alfred_records('{ "bad", "AAAA" }', node_pb.NodeTelemetry))
        self.assertFalse(svc.alfred_records('{ "' + self.primary + '", "====" }', node_pb.NodeTelemetry))


class ServiceIntegrationTests(unittest.TestCase):
    def rig(self, count=4):
        sim = svc.Simulation(count)
        self.addCleanup(sim.close)
        return sim

    def test_three_gnss_anchors_position_and_no_guess_reuse(self):
        sim = self.rig()
        sim.advance(12)
        s = sim.services['P']
        doc = s.position_document()
        self.assertEqual(doc['quality'], 'best_guess')
        self.assertEqual(doc['anchors_used'], ['A0', 'A1', 'A2'])
        self.assertLess(sim.summary()['gpsless']['error_m'], .1)
        self.assertEqual(doc['generation'], 1)
        self.assertEqual(doc['ancestry'], doc['anchors_used'])
        self.assertGreater(doc['radius_m'], 0)
        self.assertFalse(doc['anchor_eligible'])
        self.assertFalse(s.position_snapshot().valid)
        self.assertEqual(sum(e['status'] == 'ok' for e in s.events), 3)
        self.assertTrue(all(other.radio.ack == 'original' for other in sim.services.values()))

    def test_independent_boot_clocks_never_cross_the_wire(self):
        sim = svc.Simulation(clock_offsets={'P': 3000, 'A0': 40000, 'A1': 500, 'A2': 17000})
        self.addCleanup(sim.close)
        sim.advance(12)
        self.assertEqual(sim.services['P'].position_document()['quality'], 'best_guess')
        self.assertLess(sim.summary()['gpsless']['error_m'], .1)
        sim.advance(80)
        self.assertFalse(any(s.monitor.last_failure for s in sim.services.values()))

    def test_quarantined_anchor_refuses_position_even_with_stale_alfred_hint(self):
        sim = self.rig()
        sim.inputs()
        p, a = sim.services['P'], sim.services['A0']
        self.assertTrue(p.neighbours['A0'].eligible_anchor)
        a.monitor.last_failure['A0'] = sim.now
        snapshot = a.position_snapshot()
        self.assertFalse(snapshot.valid)
        self.assertEqual(snapshot.source, pb.Position.GNSS)
        # The raw observation remains available to subsequent consistency checks.
        self.assertIsNotNone(svc.Service.gnss_sample(snapshot, sim.now))
        p.job = 'solve'
        p.sessions[b'S' * 16] = sim.now, 'initiator'
        p.handle_result(ranging.Result(sim.peers['A0'], b'S' * 16, 'ok',
            ranging.Estimate(60, 1, tuple((n, 1) for n in range(50)), 50, 0),
            initiator_position=pb.Position(valid=False), responder_position=snapshot))
        self.assertFalse(p.observations)

    def test_stable_solutions_back_off_and_empty_search_is_bounded(self):
        sim = self.rig()
        sim.advance(12)
        p = sim.services['P']
        observations = []
        for node_id in ('A0', 'A1', 'A2'):
            anchor = p.neighbours[node_id].anchor
            observations.append(locate.Observation(anchor, 60, sim.now))
        for _ in range(5):
            p.job, p.observations = 'solve', observations
            p.finish_job()
        self.assertEqual(p.backoff, 60)
        self.assertEqual(p.next_solve, sim.now + 60)
        p.update(p.receiver, {})
        p.next_solve = sim.now
        p.schedule()
        self.assertEqual(p.next_solve, sim.now + 60)
        self.assertIsNone(p.core.active)

    def test_background_spoof_and_responder_claim_both_quarantine(self):
        sim = self.rig()
        sim.advance(100)
        self.assertFalse(any(s.monitor.last_failure for s in sim.services.values()))
        # A0 initiates; A1 learns exclusively through the authenticated Result
        # claim. Keep the GPS-less node out of this controlled exchange.
        for s in sim.services.values():
            s.core.cancel()
            s.job = None
            s.next_solve = s.next_audit = sim.now + 1000
        sim.messages.clear()
        x, y, z = sim.truth['A0']
        sim.claimed['A0'] = (x + 120, y, z)
        sim.inputs()
        a, b = sim.services['A0'], sim.services['A1']
        a.core._peers.clear()
        b.core._peers.clear()
        a.job = 'audit'
        a.core.start([sim.peers['A1']])
        sim.advance(5)
        for s in (a, b):
            self.assertIn(s.state(), spoof.DISTRUSTED)
            self.assertFalse(s.trusted())
            self.assertFalse(s.spoof_document()['time_ok'])
            self.assertGreater(s.spoof_document()['fault_epoch'], 0)
        claim = [e for e in b.events if e['status'] == 'peer_claim'][-1]
        self.assertTrue(claim['failed'])
        self.assertTrue(b.monitor.edges[frozenset(('A0', 'A1'))]['peer_claim'])
        self.assertFalse(b.spoof_document()['ranges_agree'])
        self.assertIsNone(b.result)

    def test_spoof_is_discovered_without_forcing_an_exchange(self):
        result = svc.simulate(4)
        self.assertIsNotNone(result['spoof_detection_s'])
        self.assertLessEqual(result['spoof_detection_s'], 120)
        self.assertIn(result['attack']['states']['A0'], spoof.DISTRUSTED)
        self.assertTrue(result['attack']['failed_peer_claims'])
        self.assertTrue(all(s not in spoof.DISTRUSTED for s in result['benign']['states'].values()))

    def test_stale_ready_fix_is_not_a_solver_observation(self):
        sim = self.rig()
        sim.inputs()
        s = sim.services['P']
        p = sim.services['A0'].position_snapshot()
        p.fix_age_ms = 6000
        s.sessions[b'S' * 16] = (sim.now, 'initiator')
        s.job = 'solve'
        result = ranging.Result(sim.peers['A0'], b'S' * 16, 'ok',
            ranging.Estimate(60, 1, tuple((n, 1) for n in range(50)), 50, 0),
            initiator_position=pb.Position(valid=False), responder_position=p)
        s.handle_result(result)
        self.assertFalse(s.observations)

    def test_canonical_burst_snapshots_not_newer_or_alfred_positions(self):
        sim = self.rig()
        sim.inputs()
        s = sim.services['A0']
        a, b = s.position_snapshot(), sim.services['A1'].position_snapshot()
        # Alter the *current* fix, not the snapshots; the pair must still pass.
        sim.claimed['A0'] = (500, 0, 1600)
        sim.inputs()
        result = ranging.Result(sim.peers['A1'], b'S' * 16, 'ok',
                                initiator_position=a, responder_position=b)
        edge = s.feed_monitor(result, sim.now, 103.923)
        self.assertFalse(edge['disagrees'])
        self.assertAlmostEqual(s.monitor.samples['A0'][-1]['lon'], s.receiver.fix['lon'])

    def test_mesh_loss_cancels_and_restores_responder_ack(self):
        sim = self.rig()
        sim.advance(.2)
        armed = next(s for s in sim.services.values() if s.radio.armed)
        armed.update(armed.receiver, mesh=False)
        armed.tick()
        self.assertIsNone(armed.core.active)
        self.assertFalse(armed.radio.armed)
        self.assertEqual(armed.radio.ack, 'original')

    def test_restart_does_not_clear_quarantine_and_boot_epochs_are_local(self):
        sim = self.rig()
        sim.inputs()
        s = sim.services['A0']
        s.monitor.last_failure['A0'] = sim.now - 1
        old = s.spoof_document()
        checkpoint = json.loads(json.dumps(s.checkpoint()))
        clock = lambda: svc.Now(sim.now + 800, sim.now + 1234, sim.now + 100)
        restored = svc.Service(s.config, svc.HelperRadio(), clock, s.boot_id, checkpoint=checkpoint)
        doc = restored.spoof_document()
        self.assertEqual(doc['state'], spoof.INCONSISTENT)
        self.assertEqual(doc['fault_epoch'], old['fault_epoch'])
        self.assertEqual(doc['producer_id'], old['producer_id'])
        self.assertAlmostEqual(doc['quarantine_remaining_s'], 299)
        sim.now += 301
        self.assertEqual(restored.spoof_document()['state'], spoof.NO_FIX)
        new_boot = svc.Service(s.config, svc.HelperRadio(), clock, 'new-boot', checkpoint=checkpoint)
        self.assertFalse(new_boot.monitor.last_failure)

    def test_good_only_ranged_anchor_with_age_and_generation(self):
        sim = self.rig()
        s = sim.services['P']
        frame = sim.frame
        observations = []
        for i, (x, y) in enumerate([(40, 0), (0, 40), (-40, 0), (0, -40)]):
            p = frame.position(x, y, 1600)
            observations.append(locate.Observation(locate.Anchor(str(i), p['lat'], p['lon'], 1,
                1600, 1), 40, sim.now))
        s.result = locate.solve('P', observations, at_mono=sim.now)
        s.result_at = sim.now
        self.assertEqual(s.result['quality'], 'good')
        self.assertTrue(s.position_document()['anchor_eligible'])
        p = s.position_snapshot()
        self.assertEqual(p.source, pb.Position.RANGED)
        self.assertEqual(p.generation, 1)
        self.assertEqual(list(p.used_node_ids), ['0', '1', '2', '3'])
        sim.now += 16
        self.assertFalse(s.position_document()['anchor_eligible'])
        self.assertFalse(s.position_snapshot().valid)
        sim.now += 90
        self.assertEqual(s.position_document()['quality'], 'none')

    def test_quarantine_survives_no_fix_and_clean_peer_claim(self):
        sim = self.rig()
        sim.inputs()
        s = sim.services['A0']
        s.monitor.last_failure['A0'] = sim.now
        a, b = sim.services['A1'].position_snapshot(), s.position_snapshot()
        result = ranging.Result(sim.peers['A1'], b'S' * 16, 'peer_claim',
                                initiator_position=a, responder_position=b)
        s.feed_monitor(result, sim.now + .01, 103.923)
        s.update(svc.Receiver())
        self.assertEqual(s.state(), spoof.INCONSISTENT)
        self.assertFalse(s.spoof_document()['ranges_agree'])

    def test_clean_claim_is_not_independent_agreement_or_a_veto_on_local_pass(self):
        sim = self.rig()
        sim.inputs()
        s = sim.services['A0']
        claim = ranging.Result(sim.peers['A2'], b'S' * 16, 'peer_claim',
            initiator_position=sim.services['A2'].position_snapshot(),
            responder_position=s.position_snapshot())
        s.feed_monitor(claim, sim.now, 103.923)
        self.assertFalse(s.spoof_document()['ranges_agree'])
        local = ranging.Result(sim.peers['A1'], b'T' * 16, 'ok',
            initiator_position=s.position_snapshot(),
            responder_position=sim.services['A1'].position_snapshot())
        s.feed_monitor(local, sim.now, 103.923)
        self.assertTrue(s.spoof_document()['ranges_agree'])

    def test_position_shapes_age_without_mutating_solver_result(self):
        sim = self.rig()
        s = sim.services['P']
        p = sim.frame.position(60, 0, 1600)
        s.result = locate.solve('P', [locate.Observation(locate.Anchor('A', p['lat'], p['lon'],
                                    1, 1600, 1), 60, sim.now)], at_mono=sim.now)
        s.result_at = sim.now
        before = copy.deepcopy(s.result)
        first = s.position_document()
        sim.now += 5
        later = s.position_document()
        self.assertEqual(later['quality'], 'ring')
        self.assertAlmostEqual(later['rings'][0]['outer_radius_m'] - first['rings'][0]['outer_radius_m'], 7.5)
        self.assertEqual(s.result, before)

    def test_existing_atak_inputs_accept_the_actual_spoof_publication(self):
        sim = self.rig()
        sim.inputs()
        s = sim.services['A0']
        s.monitor.last_failure['A0'] = sim.now
        now = sim.clock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gps = status(now)
            gps['boot_id'] = s.boot_id
            svc.atomic_json(root / 'gps_status.json', gps)
            svc.atomic_json(root / 'manet-spoof.json', s.spoof_document())
            memory = {'fault_epoch': 0}
            gps, detail = atak.Inputs(s.boot_id, root).read(atak.Now(
                now.mono, now.raw, now.boot, datetime(2026, 10, 7, tzinfo=timezone.utc)), memory)
            self.assertEqual(gps.state, spoof.INCONSISTENT)
            self.assertGreater(memory['fault_epoch'], 0)
            self.assertEqual(memory['monitor_token'][1], s.fault_epoch)


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config, self.path = self.root / 'mesh.conf', self.root / 'position.json'
        self.config.write_text('positioning=y\n')
        self.doc = {'schema': 1, 'boot_id': 'boot', 'written_boot': 100,
                    'gnss_state': spoof.UNCHECKED,
                    'quality': 'best_guess', 'point': {'lat': 39.7, 'lon': -105., 'hae': None},
                    'radius_m': 12, 'age_s': 1, 'source': 'ranged', 'generation': 1,
                    'ancestry': ['A', 'B', 'C'], 'anchor_eligible': False}

    def encode(self):
        svc.atomic_json(self.path, self.doc)
        return self.read_output()

    def read_output(self, *, raw_fix=True):
        t = node_pb.NodeTelemetry()
        if raw_fix:
            t.location.CopyFrom(node_pb.NodeTelemetry.GpsLocation(
                latitude_e7=100, longitude_e7=-250, altitude_m=1601))
        encoder.apply_positioning_location(t, self.path, self.config, now_boot=100, boot_id='boot')
        return t

    def assert_unchecked_gnss(self, t):
        self.assertTrue(t.HasField('location'))
        p = t.location
        self.assertEqual((p.latitude_e7, p.longitude_e7, p.altitude_m), (100, -250, 1601))
        self.assertEqual(p.source, p.GNSS)
        self.assertTrue(p.HasField('valid'))
        self.assertFalse(p.valid)
        self.assertEqual(p.quality, 'unchecked')
        self.assertFalse(p.anchor_eligible)
        # No ranged ancestry, invented uncertainty, HAE, or solution age.
        self.assertEqual({f.name for f, _ in p.ListFields()},
                         {'latitude_e7', 'longitude_e7', 'altitude_m', 'source', 'valid', 'quality'})

    def test_ranged_metadata_and_legacy_wire_compatibility(self):
        t = self.encode()
        p = t.location
        self.assertEqual(p.source, p.RANGED)
        self.assertEqual(p.uncertainty_m, 12)
        self.assertFalse(p.anchor_eligible)
        self.assertFalse(p.HasField('hae_m'))
        # Build an isolated pre-R4 descriptor: no dependency on a /tmp snapshot.
        old = descriptor_pb2.FileDescriptorProto()
        old.ParseFromString(node_pb.DESCRIPTOR.serialized_pb)
        location = next(m for m in old.message_type if m.name == 'NodeTelemetry').nested_type[0]
        del location.field[3:]
        del location.enum_type[:]
        del location.oneof_decl[:]
        pool = descriptor_pool.DescriptorPool()
        pool.Add(old)
        old_cls = message_factory.MessageFactory(pool).GetPrototype(pool.FindMessageTypeByName('NodeTelemetry'))
        decoded = old_cls.FromString(t.SerializeToString())
        self.assertEqual(decoded.location.latitude_e7, 397000000)
        self.assertEqual(decoded.location.longitude_e7, -1050000000)
        self.assertEqual(node_pb.NodeTelemetry.FromString(decoded.SerializeToString()).location.source, p.RANGED)

    def test_missing_service_keeps_unchecked_gnss(self):
        self.assert_unchecked_gnss(self.read_output())

    def test_stale_wrong_boot_or_invalid_output_keeps_unchecked_gnss(self):
        for key, value in [('boot_id', 'other'), ('written_boot', 95), ('written_boot', 101),
                           ('quality', 'ring'), ('age_s', 90), ('radius_m', -1),
                           ('generation', 0), ('ancestry', []), ('point', None),
                           ('gnss_state', 'unknown'), ('gnss_state', None)]:
            old = self.doc[key]
            self.doc[key] = value
            with self.subTest(key=key, value=value):
                self.assert_unchecked_gnss(self.encode())
            self.doc[key] = old

    def test_malformed_json_keeps_unchecked_gnss(self):
        for raw in ('{', '[]', 'null', '{"schema":1}', 'x' * 131073):
            with self.subTest(raw=raw[:30]):
                self.path.write_text(raw)
                self.assert_unchecked_gnss(self.read_output())

    def test_fresh_explicit_suspect_suppresses_raw_gnss(self):
        sim = svc.Simulation()
        self.addCleanup(sim.close)
        sim.inputs()
        s = sim.services['A0']
        s.monitor.last_failure['A0'] = sim.now
        doc = s.position_document()
        self.assertEqual(doc['gnss_state'], spoof.INCONSISTENT)
        self.doc = dict(doc, boot_id='boot', written_boot=100)
        for state in spoof.DISTRUSTED:
            self.doc['gnss_state'] = state
            self.assertFalse(self.encode().HasField('location'))
        # A fresh verdict can suppress GNSS while still publishing a ranged fix.
        self.doc.update(quality='good', source='ranged',
                        point={'lat': 39.7, 'lon': -105., 'hae': None}, radius_m=12,
                        generation=1, ancestry=['A', 'B', 'C'], age_s=1)
        self.assertEqual(self.encode().location.source, node_pb.NodeTelemetry.GpsLocation.RANGED)

    def test_stale_suspect_or_nonverdict_does_not_suppress_gnss(self):
        self.doc.update(quality='none', source='none', point=None,
                        gnss_state=spoof.SUSPECTED, written_boot=95)
        self.assert_unchecked_gnss(self.encode())
        self.doc.update(written_boot=100, boot_id='other')
        self.assert_unchecked_gnss(self.encode())
        self.doc.update(boot_id='boot', gnss_state=spoof.NO_FIX)
        self.assert_unchecked_gnss(self.encode())
        self.doc['gnss_state'] = spoof.UNCHECKED
        self.assert_unchecked_gnss(self.encode())

    def test_no_raw_fix_does_not_invent_fallback(self):
        self.assertFalse(self.read_output(raw_fix=False).HasField('location'))
        self.path.write_text('{')
        self.assertFalse(self.read_output(raw_fix=False).HasField('location'))

    def test_disabled_config_keeps_existing_encoder_behavior(self):
        self.config.write_text('positioning=n\n')
        self.assertEqual(self.encode().location.latitude_e7, 100)

    def test_disabled_cli_matches_pre_r4_bytes_for_identical_inputs(self):
        # Frozen stdout from encoder.py at 3b2ee1aaec55b5374827f549f65ca78877127d7b,
        # before R4. Covers all telemetry groups, GNSS, an equator fix and no fix.
        common = [
            '--timestamp', '1791331200', '--mean-throughput-mbps', '42.5',
            '--is-internet-gateway', '--gateway-iface', 'end0',
            '--is-mumble-server', '--is-ntp-server', '--is-tak-server', '--is-mediamtx-server',
            '--uptime-seconds', '1234', '--battery-percentage', '57', '--cpu-load-average', '0.25',
            '--atak-user', 'Ada', '--data-channel-2-4', '2412', '--data-channel-5-0', '5180',
            '--channel-report-json', '{"results":[{"channel":5180,"noise_floor":-94,"bss_count":2,"busy_pct":0}]}',
            '--halow-tx-mcs', '2', '--halow-rx-mcs', '3', '--halow-mcs-peer', '02:00:00:00:00:01',
            '--wifi-24-tx-mcs', '4', '--wifi-24-rx-mcs', '5', '--wifi-5-tx-mcs', '6', '--wifi-5-rx-mcs', '7',
            '--interfaces-json', '[{"name":"wlan1","role":"mesh","state":"UP","ipv4":["10.43.0.1"],"freq_mhz":"5180"}]',
            '--eud-mode', 'wireless', '--ap-ssid', 'Test mesh', '--eud-count', '2', '--is-in-limp-mode',
            '--last-tourguide-timestamp', '1791331000', '--last-tourguide-radio', 'wlan1',
            '--partition-size', '8', '--node-state', 'SHUTTING_DOWN', '--config-ack-version', 'abcd']
        fixtures = [
            ([], 'DQAAKkIQARoEZW5kMFABWAFgAWgBoAHSCagBObUBAACAPsIBA0FkYfAB7BL4AbwoggIMCgoIvCgQuwEYAiAAigIBMpICATOaAgYCAAAAAAGiAgE0qgIBNbICATa6AgE3wgIXCgV3bGFuMRACGAEiBAEAKwpCBDUxODDoAgHyAglUZXN0IG1lc2j4AgKQA4CXltYGmAMBoAO4lZbWBqoDBXdsYW4xsAMBugMEYWJjZMADCA=='),
            (['--latitude', '39.7', '--longitude', '-105', '--altitude', '1600.6'], 'DQAAKkIQARoEZW5kMFABWAFgAWgBoAHSCagBObUBAACAProBDQ1AvakXFYBFasEYghnCAQNBZGHwAewS+AG8KIICDAoKCLwoELsBGAIgAIoCATKSAgEzmgIGAgAAAAABogIBNKoCATWyAgE2ugIBN8ICFwoFd2xhbjEQAhgBIgQBACsKQgQ1MTgw6AIB8gIJVGVzdCBtZXNo+AICkAOAl5bWBpgDAaADuJWW1gaqAwV3bGFuMbADAboDBGFiY2TAAwg='),
            (['--latitude', '0', '--longitude', '10', '--altitude', '-3.5'], 'DQAAKkIQARoEZW5kMFABWAFgAWgBoAHSCagBObUBAACAProBBxUA4fUFGAfCAQNBZGHwAewS+AG8KIICDAoKCLwoELsBGAIgAIoCATKSAgEzmgIGAgAAAAABogIBNKoCATWyAgE2ugIBN8ICFwoFd2xhbjEQAhgBIgQBACsKQgQ1MTgw6AIB8gIJVGVzdCBtZXNo+AICkAOAl5bWBpgDAaADuJWW1gaqAwV3bGFuMbADAboDBGFiY2TAAwg=')]
        # Even an existing positioning result must have no effect when disabled.
        svc.atomic_json(self.path, self.doc)
        for setting in ('positioning=n\n', '# default n\n'):
            self.config.write_text(setting)
            for coords, expected in fixtures:
                with self.subTest(config=setting, coords=coords):
                    actual = subprocess.check_output([
                        sys.executable, '-B', str(Path(encoder.__file__)), 'telemetry', *common,
                        *coords, '--mesh-config', str(self.config), '--position-file', str(self.path)],
                        timeout=10)
                    self.assertEqual(actual, expected.encode('ascii'))

    def test_gnss_zero_coordinate_and_hae_have_presence(self):
        self.doc.update(source='gnss', quality='good', generation=0, ancestry=[], anchor_eligible=True,
                        point={'lat': 0., 'lon': 0., 'hae': 0.})
        p = self.encode().location
        self.assertTrue(p.valid and p.anchor_eligible)
        self.assertTrue(p.HasField('hae_m'))
        self.assertEqual(p.hae_m, 0)


class RuntimeTests(unittest.TestCase):
    def test_bounded_async_discovery_command_reads_all_chunks(self):
        result = asyncio.run(svc.command([sys.executable, '-c',
            'import sys; sys.stdout.write("x" * 60000); sys.stdout.flush(); sys.stdout.write("y" * 60000)']))
        self.assertEqual(result, 'x' * 60000 + 'y' * 60000)
        with self.assertRaises(ValueError):
            asyncio.run(svc.command([sys.executable, '-c', 'print("x" * 300000)']))

    def test_disabled_cli_never_opens_node_or_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'mesh.conf'
            config.write_text('positioning=n\n')
            result = subprocess.run([sys.executable, '-B', str(Path(svc.__file__)), '--config', str(config)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
