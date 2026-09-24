"""NTP selection and lifecycle tests with fake chrony/systemctl and real files."""
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('mesh_time_sync', TOOLS / 'mesh-time-sync.py')
time_sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(time_sync)
OWN = '02:00:00:00:00:01'
PEER = '02:00:00:00:00:02'
THIRD = '02:00:00:00:00:03'
RADIO = '02:00:00:00:01:02'
NETWORK = ipaddress.IPv4Network('10.30.2.0/24')


def node(mac, ip, aliases='', ntp=True, state='ACTIVE'):
    fields = {'MAC_ADDRESSES': ','.join(filter(None, [mac, aliases])), 'IPV4_ADDRESS': ip,
              'IS_NTP_SERVER': str(ntp).lower(), 'NODE_STATE': state}
    return ''.join(f"NODE_{mac.replace(':', '')}_{key}='{value}'\n" for key, value in fields.items())


def route(mac, throughput=432, best=True, age=250):
    return {'orig_address': mac, 'throughput': throughput, 'best': best, 'last_seen_msecs': age}


def tracking(correction='0.00001', refid='0A1E0207', leap='Normal', stratum=2):
    return f'''Reference ID    : {refid} (test)
Stratum         : {stratum}
System time     : {correction} seconds slow of NTP time
Leap status     : {leap}
'''


def sources(address='10.30.2.7', mode='^', age='1', selected='*', reach='377', poll=6):
    return f'MS Name/IP address Stratum Poll Reach LastRx Last sample\n{mode}{selected} {address} 1 {poll} {reach} {age} +1us[+1us] +/- 1ms\n'


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.registry = node(OWN, '10.30.2.6') + node(PEER, '10.30.2.7', RADIO) + node(THIRD, '10.30.2.8')

    def pick(self, rows, registry=None):
        return time_sync.candidates(self.registry if registry is None else registry, rows, OWN, NETWORK)

    def test_selected_route_metric_maps_interface_to_canonical_identity(self):
        rows = [route(RADIO.upper(), 432), route(RADIO, 9999, best=False), route(THIRD, 500)]
        peers = self.pick(rows)
        self.assertEqual([p['mac'] for p in peers], [THIRD, PEER])
        self.assertEqual(peers[1]['metric'], 432)
        self.assertEqual(peers[1]['address'], '10.30.2.7')

    def test_ties_and_alternate_rows_do_not_depend_on_order(self):
        rows = [route(THIRD), route(RADIO), route(PEER, 200)]
        self.assertEqual(self.pick(rows), self.pick(rows[::-1]))
        self.assertEqual(len(self.pick(rows)), 2)
        self.assertEqual(self.pick(rows)[0]['mac'], PEER)

    def test_self_unadvertised_shutdown_and_old_routes_are_excluded(self):
        for registry in [node(PEER, '10.30.2.7', RADIO, ntp=False),
                         node(PEER, '10.30.2.7', RADIO, state='SHUTTING_DOWN')]:
            self.assertEqual(self.pick([route(RADIO)], registry), [])
        self.assertEqual(self.pick([route(OWN), route(RADIO, age=30001)]), [])

    def test_unknown_wall_clock_does_not_exclude_a_live_source(self):
        registry = node(PEER, '10.30.2.7', RADIO, state='STALE')
        registry += "NODE_020000000002_LAST_SEEN_TIMESTAMP='1'\n"
        self.assertEqual(self.pick([route(RADIO)], registry)[0]['mac'], PEER)

    def test_invalid_or_foreign_addresses_cannot_become_chrony_commands(self):
        for ip in ['Error: no IP', 'pool.ntp.org', '127.0.0.1', '10.30.2.255', '10.30.2.0', '10.30.3.7', '10.30.2.7; reboot']:
            self.assertEqual(self.pick([route(PEER)], node(PEER, ip)), [])

    def test_conflicting_aliases_or_allocations_cannot_select_an_identity(self):
        with self.assertRaises(ValueError):
            self.pick([route(RADIO)], self.registry + node('02:00:00:00:00:04', '10.30.2.9', RADIO))
        self.assertEqual(self.pick([route(PEER), route(THIRD)], node(PEER, '10.30.2.7') + node(THIRD, '10.30.2.7')), [])

    def test_malformed_query_is_an_error_and_invalid_metrics_do_not_rank(self):
        for rows in [{}, [{}], [{'orig_address': 'not-a-mac'}]]:
            with self.assertRaises(ValueError):
                self.pick(rows)
        for value in [True, None, '432', -1, 0, float('nan')]:
            self.assertEqual(self.pick([route(PEER, value)]), [])
        self.assertEqual(self.pick([{'orig_address': PEER, 'best': True, 'tq': 250}]), [])

    def test_registry_is_parsed_as_data_without_executing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            proof = Path(directory) / 'executed'
            data = node(PEER, '10.30.2.7') + f"echo injected > {proof}\n"
            self.assertEqual(len(self.pick([route(PEER)], data)), 1)
            self.assertFalse(proof.exists())


class ClockTests(unittest.TestCase):
    def test_remaining_correction_and_reference_are_checked(self):
        self.assertTrue(time_sync.settled(tracking()))
        for text in [tracking('0.101'), tracking('-0.2'), tracking('nan'), tracking('inf'),
                     tracking(refid='00000000'), tracking(refid='7F7F0101'), tracking(leap='Not synchronised'),
                     tracking(stratum=0), tracking(stratum=16), '200 OK']:
            self.assertFalse(time_sync.settled(text), text)

    def test_selected_source_must_be_recent_and_reachable(self):
        self.assertEqual(time_sync.selected_source(sources())['address'], '10.30.2.7')
        for report in [sources(selected='?'), sources(reach='0'), sources(age='5m'), sources(age='-'), 'bad']:
            self.assertIsNone(time_sync.selected_source(report))
        self.assertEqual(time_sync.selected_source(sources('GPS', '#', poll=4))['mode'], '#')
        self.assertIsNone(time_sync.selected_source(sources('GPS', '#', age='60', poll=4)))

    def test_source_profiles_use_configured_mesh_and_bind_internet_to_uplink(self):
        network = ipaddress.IPv4Network('10.44.0.0/16')
        gps = time_sync.chrony_config(network, gps=True)
        self.assertIn('refclock SHM 0 refid GPS', gps)
        self.assertIn('allow 10.44.0.0/16', gps)
        self.assertNotIn('pool ', gps)
        self.assertNotIn('local stratum', gps)
        internet = time_sync.chrony_config(network, uplink='usb0')
        self.assertIn('pool pool.ntp.org iburst maxsources 2', internet)
        self.assertIn('bindacqdevice usb0', internet)
        client = time_sync.chrony_config(network, peer='10.44.0.7')
        self.assertIn('server 10.44.0.7 iburst', client)
        self.assertIn('deny all', client)
        self.assertIn('corrtimeratio 1', client)
        self.assertIn('leapsecmode slew', client)
        self.assertNotIn('pool ', client)
        with self.assertRaises(ValueError):
            time_sync.chrony_config(network, uplink='usb0\nserver malicious')


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='manet-time-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        (self.root / 'net/br0').mkdir(parents=True)
        (self.root / 'net/br0/address').write_text(OWN)
        (self.root / 'mesh.conf').write_text('ipv4_network=10.30.2.0/24\n')
        (self.root / 'registry').write_text(node(OWN, '10.30.2.6', ntp=False) + node(PEER, '10.30.2.7', RADIO) + node(THIRD, '10.30.2.8'))
        (self.root / 'routes').write_text(json.dumps([route(RADIO, 900), route(THIRD, 432)]))
        (self.root / 'tracking').write_text(tracking(leap='Not synchronised'))
        (self.root / 'sources').write_text(sources(selected='?'))
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        MANET_TIME_RUN_DIR=str(self.root), MANET_MESH_CONF=str(self.root / 'mesh.conf'),
                        MANET_CHRONY_CONF=str(self.root / 'chrony.conf'), MANET_SYS_NET=str(self.root / 'net'),
                        REGISTRY_STATE_FILE=str(self.root / 'registry'), BATCTL_PATH=str(self.bin / 'batctl'),
                        TEST_ROOT=str(self.root))
        self.command('systemctl', '''
active = root / 'active'
if (root / 'systemctl-fail').exists():
    sys.exit(1)
if sys.argv[1] == 'is-active':
    sys.exit(0 if active.exists() else 3)
if sys.argv[1] in ('start', 'restart'):
    active.touch()
if sys.argv[1] == 'stop':
    active.unlink(missing_ok=True)
''')
        self.command('chronyc', '''
if (root / 'chronyc-fail').exists():
    sys.exit(1)
if sys.argv[1:] == ['makestep', '0.1', '0']:
    if (root / 'step-disable-fail').exists():
        sys.exit(1)
    sys.exit(0)
assert sys.argv[1] == '-n'
print((root / sys.argv[2]).read_text())
''')
        self.command('batctl', '''
assert sys.argv[1:] == ['meshif', 'bat0', 'originators_json']
if (root / 'batctl-fail').exists():
    sys.exit(1)
print((root / 'routes').read_text())
''')
        self.patcher = patch.dict(os.environ, self.env)
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        self.service = time_sync.TimeService()
        self.clock = 1000

    def command(self, name, body):
        path = self.bin / name
        path.write_text(f'''#!{sys.executable}
import os, sys
from pathlib import Path
root = Path(os.environ['TEST_ROOT'])
with (root / 'commands').open('a') as out:
    out.write({name!r} + ' ' + ' '.join(sys.argv[1:]) + '\\n')
''' + body)
        path.chmod(0o755)

    def step(self, advance=0):
        self.clock += advance
        with patch.object(time_sync.time, 'monotonic', return_value=self.clock), patch.object(time_sync.time, 'time', return_value=2000):
            return self.service.step()

    def good(self, address='10.30.2.7', mode='^', correction='0.00001', age='1'):
        (self.root / 'tracking').write_text(tracking(correction=correction))
        (self.root / 'sources').write_text(sources(address, mode, age))

    def gps(self, timestamp=2000, fix=True):
        (self.root / 'gps_status.json').write_text(json.dumps({'has_fix': fix, 'timestamp': timestamp}))

    def uplink(self):
        (self.root / 'mesh-gateway.state').touch()
        (self.root / 'upstream_iface').write_text('usb0')
        (self.root / 'net/usb0').mkdir(exist_ok=True)
        (self.root / 'net/usb0/carrier').write_text('1')

    def history(self):
        return (self.root / 'commands').read_text() if (self.root / 'commands').exists() else ''

    def test_real_wrapper_and_service_candidate_path_never_uses_text_table(self):
        result = subprocess.run(['bash', str(TOOLS / 'one-shot-time-sync.sh'), '--once'], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('server 10.30.2.7 iburst', (self.root / 'chrony.conf').read_text())
        self.assertIn('batctl meshif bat0 originators_json', self.history())
        self.assertFalse(self.service.marker.exists())

    def test_success_stops_polling_and_keeps_local_role_checks(self):
        self.step(); self.good(); self.step(5)
        self.assertTrue(self.service.marker.exists())
        self.assertFalse((self.root / 'active').exists())
        self.assertNotIn('server ', (self.root / 'chrony.conf').read_text())
        (self.root / 'commands').write_text('')
        self.step(30)
        self.assertNotIn('batctl', self.history())
        self.assertNotIn('chronyc', self.history())
        self.assertNotIn('restart', self.history())
        self.assertFalse((self.root / 'mesh-ntp.state').exists())

    def test_refresh_waits_six_hours_and_survives_process_restart(self):
        self.step(); self.good(); self.step(5)
        due = self.service.state['refresh_at']
        self.assertGreaterEqual(due, self.clock + 6 * 60 * 60)
        self.assertLessEqual(due, self.clock + 6 * 60 * 60 + 600)
        self.service = time_sync.TimeService()
        (self.root / 'commands').write_text('')
        self.clock = due - 1
        self.step()
        self.assertNotIn('batctl', self.history())
        self.assertNotIn('chronyc', self.history())
        self.step(1)
        self.assertIn('pending', self.service.state)
        self.assertIn('server 10.30.2.7 iburst', (self.root / 'chrony.conf').read_text())
        self.assertNotIn('makestep', (self.root / 'chrony.conf').read_text())
        self.step(5)
        self.assertNotIn('pending', self.service.state)
        self.assertFalse((self.root / 'active').exists())
        self.assertGreater(self.service.state['refresh_at'], due + 6 * 60 * 60)

    def test_refresh_schedule_is_unaffected_by_wall_clock_jumps(self):
        self.step(); self.good(); self.step(5)
        due = self.service.state['refresh_at']
        with patch.object(time_sync.time, 'monotonic', return_value=due - 1):
            for wall in [-100000, 10000000000]:
                with patch.object(time_sync.time, 'time', return_value=wall):
                    self.service.step()
                    self.assertNotIn('pending', self.service.state)
                    self.assertEqual(self.service.state['refresh_at'], due)

    def test_missing_or_failed_refresh_preserves_holdover_and_backoff(self):
        self.step(); self.good(); self.step(5)
        due = self.service.state['refresh_at']
        (self.root / 'routes').write_text('[]')
        self.clock = due
        self.step()
        self.assertTrue(self.service.marker.exists())
        self.assertEqual(self.service.state['refresh_at'], due)
        self.assertFalse((self.root / 'active').exists())
        (self.root / 'routes').write_text(json.dumps([route(RADIO), route(THIRD, 100)]))
        (self.root / 'tracking').write_text(tracking(correction='2.0'))
        self.step(30); self.step(5)
        self.assertIn('pending', self.service.state)
        self.step(90)
        self.assertTrue(self.service.marker.exists())
        self.assertEqual(self.service.state['refresh_at'], due)
        self.assertFalse((self.root / 'active').exists())
        self.step(30)
        self.assertEqual(self.service.state['pending']['mac'], THIRD)
        self.assertNotIn('makestep', (self.root / 'chrony.conf').read_text())

    def test_local_source_extends_holdover_without_peer_polls(self):
        self.gps(); self.good('GPS', '#'); self.step()
        first_due = self.service.state['refresh_at']
        self.step(24 * 60 * 60)
        last_due = self.service.state['refresh_at']
        self.assertGreater(last_due, first_due)
        self.assertNotIn('batctl', self.history())
        self.gps(fix=False); self.step(15)
        self.assertEqual(self.service.state['refresh_at'], last_due)
        self.assertNotIn('pending', self.service.state)
        self.assertFalse((self.root / 'active').exists())

    def test_startup_steps_are_disarmed_live_and_on_later_role_changes(self):
        self.gps(); self.good('GPS', '#'); self.step()
        self.assertIn('chronyc makestep 0.1 0', self.history())
        self.assertNotIn('makestep', (self.root / 'chrony.conf').read_text())
        self.assertEqual(self.history().count('systemctl restart chrony.service'), 1)
        self.service = time_sync.TimeService()
        self.uplink(); self.step(15)
        self.assertIn('bindacqdevice usb0', (self.root / 'chrony.conf').read_text())
        self.assertNotIn('makestep', (self.root / 'chrony.conf').read_text())
        self.assertEqual(self.history().count('chronyc makestep 0.1 0'), 1)

    def test_missing_refresh_state_does_not_grant_a_new_holdover_window(self):
        self.step(); self.good(); self.step(5)
        self.service.state_path.unlink()
        self.service = time_sync.TimeService()
        self.step()
        self.assertIn('pending', self.service.state)
        self.assertNotIn('makestep', (self.root / 'chrony.conf').read_text())

    def test_failed_step_disarming_cannot_claim_initial_sync(self):
        self.gps(); self.good('GPS', '#')
        (self.root / 'step-disable-fail').touch()
        with self.assertRaises(subprocess.CalledProcessError):
            self.step()
        self.assertFalse(self.service.marker.exists())
        self.assertFalse((self.root / 'mesh-ntp-gps.state').exists())
        (self.root / 'step-disable-fail').unlink()
        self.step(15)
        self.assertTrue(self.service.marker.exists())
        self.assertTrue((self.root / 'mesh-ntp-gps.state').exists())

    def test_refreshes_continue_for_multiple_days_without_continuous_polling(self):
        self.step(); self.good(); self.step(5)
        start = self.clock
        jitter = self.service.state['refresh_jitter']
        for _ in range(8):
            self.service = time_sync.TimeService()
            self.clock = self.service.state['refresh_at'] - 1
            (self.root / 'commands').write_text('')
            self.step()
            self.assertNotIn('batctl', self.history())
            self.assertNotIn('chronyc', self.history())
            self.step(1); self.step(5)
            self.assertNotIn('pending', self.service.state)
            self.assertFalse((self.root / 'active').exists())
            self.assertNotIn('makestep', self.history())
            self.assertEqual(self.service.state['refresh_jitter'], jitter)
        self.assertGreaterEqual(self.clock - start, 48 * 60 * 60)

    def test_no_server_at_boot_is_retried_when_one_appears(self):
        (self.root / 'routes').write_text('[]')
        self.assertEqual(self.step(), 30)
        self.assertNotIn('restart', self.history())
        (self.root / 'routes').write_text(json.dumps([route(RADIO)]))
        self.step(30)
        self.assertEqual(self.service.state['pending']['address'], '10.30.2.7')

    def test_failed_query_does_not_start_chrony_or_claim_success(self):
        (self.root / 'batctl-fail').touch()
        self.assertEqual(self.step(), 30)
        self.assertFalse(self.service.marker.exists())
        self.assertNotIn('restart', self.history())

    def test_timeout_stops_failed_peer_and_tries_another(self):
        self.step()
        self.step(90)
        self.assertFalse(self.service.marker.exists())
        self.assertNotIn('pending', self.service.state)
        self.assertFalse((self.root / 'active').exists())
        self.step(30)
        self.assertEqual(self.service.state['pending']['mac'], THIRD)

    def test_restart_preserves_deadline_and_cooldown(self):
        self.step()
        deadline = self.service.state['pending']['deadline']
        self.service = time_sync.TimeService()
        self.step(30)
        self.assertEqual(self.service.state['pending']['deadline'], deadline)
        self.step(60)
        self.service = time_sync.TimeService()
        self.step(30)
        self.assertEqual(self.service.state['pending']['mac'], THIRD)

    def test_wall_clock_jump_does_not_extend_attempt(self):
        self.step()
        with patch.object(time_sync.time, 'monotonic', return_value=1091), patch.object(time_sync.time, 'time', return_value=-100000):
            self.service.step()
        self.assertNotIn('pending', self.service.state)
        self.assertFalse(self.service.marker.exists())

    def test_unsettled_wrong_or_old_source_cannot_complete(self):
        self.step()
        for address, correction, age in [('10.30.2.7', '0.5', '1'), ('10.30.2.8', '0', '1'), ('10.30.2.7', '0', '60')]:
            self.good(address, correction=correction, age=age)
            self.step(1)
            self.assertFalse(self.service.marker.exists())
            self.assertTrue((self.root / 'active').exists())

    def test_good_sample_after_timeout_is_not_accepted(self):
        self.step(); self.good(); self.step(91)
        self.assertFalse(self.service.marker.exists())
        self.assertFalse((self.root / 'active').exists())

    def test_gps_fix_alone_is_not_a_server_advertisement(self):
        self.gps(); self.step()
        self.assertTrue((self.root / 'active').exists())
        self.assertFalse((self.root / 'mesh-ntp-gps.state').exists())
        self.assertFalse(self.service.marker.exists())
        self.good('GPS', '#'); self.step(15)
        self.assertTrue((self.root / 'mesh-ntp-gps.state').exists())
        self.assertTrue(self.service.marker.exists())
        self.assertNotIn('pool ', (self.root / 'chrony.conf').read_text())
        self.assertNotIn('batctl', self.history())

    def test_gps_source_loss_withdraws_flag_and_stops_mesh_polling(self):
        self.gps(); self.good('GPS', '#'); self.step()
        self.gps(fix=False); self.step(15)
        self.assertFalse((self.root / 'mesh-ntp-gps.state').exists())
        self.assertFalse((self.root / 'active').exists())
        self.assertNotIn('server ', (self.root / 'chrony.conf').read_text())

    def test_future_or_stale_gps_status_is_not_a_local_time_source(self):
        for timestamp in [1900, 2010, float('nan')]:
            self.gps(timestamp=timestamp)
            with patch.object(time_sync.time, 'time', return_value=2000):
                self.assertEqual(self.service.roles(), (False, ''))

    def test_current_uplink_marker_starts_internet_sync_and_verified_advertisement(self):
        self.uplink(); self.step()
        self.assertIn('bindacqdevice usb0', (self.root / 'chrony.conf').read_text())
        self.assertFalse((self.root / 'mesh-ntp.state').exists())
        self.good('192.0.2.1'); self.step(15)
        self.assertTrue((self.root / 'mesh-ntp.state').exists())
        self.assertTrue((self.root / 'active').exists())
        self.assertNotIn('batctl', self.history())

    def test_uplink_loss_preserves_live_gps_chrony(self):
        self.uplink(); self.gps(); self.good('GPS', '#'); self.step()
        (self.root / 'net/usb0/carrier').write_text('0')
        (self.root / 'commands').write_text('')
        self.step(15)
        self.assertNotIn('pool ', (self.root / 'chrony.conf').read_text())
        self.assertTrue((self.root / 'active').exists())
        self.assertTrue((self.root / 'mesh-ntp-gps.state').exists())
        self.assertNotIn('systemctl stop', self.history())

    def test_gps_or_uplink_appearing_during_peer_attempt_does_not_get_stopped(self):
        for new_role in [self.gps, self.uplink]:
            with self.subTest(role=new_role.__name__):
                self.service.state = {}
                self.service.marker.unlink(missing_ok=True)
                (self.root / 'gps_status.json').unlink(missing_ok=True)
                (self.root / 'mesh-gateway.state').unlink(missing_ok=True)
                self.step(); new_role(); self.good('GPS' if new_role == self.gps else '192.0.2.1', '#' if new_role == self.gps else '^')
                (self.root / 'commands').write_text('')
                self.step(2)
                self.assertNotIn('pending', self.service.state)
                self.assertTrue((self.root / 'active').exists())
                self.assertNotIn('systemctl stop', self.history())
                self.assertNotIn('server 10.30.2.7', (self.root / 'chrony.conf').read_text())

    def test_local_source_is_rechecked_without_restarting_or_announcing(self):
        self.gps(); self.good('GPS', '#'); self.step()
        (self.root / 'commands').write_text('')
        self.step(15); self.step(15)
        self.assertNotIn('restart', self.history())
        self.assertNotIn('alfred', self.history())
        (self.root / 'chronyc-fail').touch()
        with self.assertRaises(subprocess.CalledProcessError):
            self.step(15)
        self.assertFalse((self.root / 'mesh-ntp-gps.state').exists())

    def test_failed_start_retries_even_after_configuration_was_written(self):
        self.gps(); (self.root / 'systemctl-fail').touch()
        with self.assertRaises(subprocess.CalledProcessError):
            self.step()
        self.assertFalse((self.root / 'mesh-ntp-gps.state').exists())
        (self.root / 'systemctl-fail').unlink(); self.good('GPS', '#'); self.step(15)
        self.assertEqual(self.history().count('systemctl restart chrony.service'), 2)
        self.assertTrue((self.root / 'mesh-ntp-gps.state').exists())


class IntegrationTests(unittest.TestCase):
    def test_provisioned_units_match_shipped_service(self):
        unit = (TOOLS.parent / 'systemd/one-shot-time-sync.service').read_text()
        for name in ('firstrun.sh.template', 'rock3a-provision.sh.template'):
            text = (TOOLS.parent / 'provisioning' / name).read_text()
            emitted = text.split("cat <<'EOF' > /etc/systemd/system/one-shot-time-sync.service\n", 1)[1].split('\nEOF', 1)[0] + '\n'
            self.assertEqual(emitted, unit)
        self.assertIn('Type=simple', unit)
        self.assertIn('PartOf=node-manager.service', unit)

    def test_managers_publish_existing_flag_without_owning_chrony(self):
        for name in ('node-manager-acs.sh', 'node-manager-static.sh', 'node-manager.sh'):
            text = (TOOLS / name).read_text()
            self.assertIn('IS_NTP_FLAG=$(is_ntp_time_source', text)
            self.assertNotIn('update_gps_time_source', text)
            self.assertNotIn('chrony.service', text)
        self.assertEqual((TOOLS / 'node-manager.sh').read_bytes(), (TOOLS / 'node-manager-static.sh').read_bytes())

    def test_legacy_ethernet_paths_cannot_stop_gps_chrony(self):
        for path in [TOOLS / 'ethernet-autodetect.sh', TOOLS.parent / 'networkd-dispatcher/off']:
            text = path.read_text()
            self.assertNotRegex(text, r'(?m)^\s*systemctl stop chrony')
            self.assertNotRegex(text, r'(?m)^\s*cp /etc/chrony')


if __name__ == '__main__':
    unittest.main()
