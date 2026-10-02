#!/usr/bin/env python3
"""AP <-> mesh transitions of the provisioned EUD radio, on a simulated node.

The Transition class is driven through its command/active/radio_info seams.
A small radio model stands behind them: services, interface type and
channel, bat0 membership, and failure switches. Hardware behavior (driver
mode changes, real supplicant timing, RF) is not established here.
"""

import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import manet_acs_agreement as agreement
import manet_ap_mesh
import manet_static_channels as static
from test_acs_agreement import status as acs_status


IFACE = 'wlan1'
OTHER = 'wlan0'
CAPS = ('Wiphy phy1\n\tSupported interface modes:\n\t\t * managed\n\t\t * AP\n'
        '\t\t * mesh point\n\tFrequencies:\n'
        '\t\t\t* 5180.0 MHz [36] (23.0 dBm)\n\t\t\t* 5200.0 MHz [40] (23.0 dBm)\n'
        '\t\t\t* 5220.0 MHz [44] (23.0 dBm)\n'
        '\t\t\t* 5260.0 MHz [52] (23.0 dBm) (radar detection)\n'
        '\t\t\t* 2412.0 MHz [1] (20.0 dBm)\n\t\t\t* 2462.0 MHz [11] (20.0 dBm)\n')


class Radio:
    """Everything the helper can observe or change, plus failure switches."""
    def __init__(self):
        self.active = {'hostapd.service', 'ap-txpower.service'}
        self.bat = set()
        self.kind = {IFACE: 'AP', OTHER: 'mesh point'}
        self.freq = {IFACE: 5745, OTHER: 2462}
        self.power = {IFACE: 5, OTHER: 23}
        self.ignore_power = False
        self.caps = CAPS
        self.fail_attach = False
        self.fail_supplicant = False
        self.wrong_channel = None
        self.fail_on = set()
        self.calls = []


class SimTransition(manet_ap_mesh.Transition):
    def __init__(self, radio, wpa):
        super().__init__()
        self.sim = radio
        self.wpa_dir = wpa

    def command(self, args, timeout=10):
        sim = self.sim
        sim.calls.append(' '.join(args))
        fail = subprocess.CalledProcessError(1, args)
        if ' '.join(args) in sim.fail_on:
            raise fail
        if args[:2] == ['systemctl', 'stop']:
            sim.active.discard(args[2])
        elif args[:2] in (['systemctl', 'start'], ['systemctl', 'restart']):
            service = args[2]
            if service.startswith('wpa_supplicant@'):
                iface = service.split('@')[1].split('.')[0]
                if sim.fail_supplicant:
                    raise fail
                conf = (self.wpa_dir / f'wpa_supplicant-{iface}.conf').read_text()
                freq = int(conf.split('frequency=')[1].split()[0])
                sim.freq[iface] = sim.wrong_channel or freq
            sim.active.add(service)
        elif args[:2] == ['batctl', 'if'] and len(args) == 2:
            return ''.join(f'{name}: active\n' for name in sorted(sim.bat))
        elif args[:3] == ['batctl', 'if', 'add']:
            if not sim.fail_attach:
                sim.bat.add(args[3])
        elif args[:3] == ['batctl', 'if', 'del']:
            sim.bat.discard(args[3])
        elif args[:2] == ['iw', 'dev'] and args[3:5] == ['set', 'type']:
            sim.kind[args[2]] = 'mesh point' if args[5] == 'mp' else args[5]
        elif args[:2] == ['iw', 'phy']:
            if args[3:5] == ['set', 'txpower'] and not sim.ignore_power:
                iface = IFACE if args[2] == 'phy1' else OTHER
                sim.power[iface] = float(args[6]) / 100 if args[5] == 'fixed' else 23
            return sim.caps
        elif args[0] == 'wpa_cli':
            if f'wpa_supplicant@{args[4]}.service' not in sim.active:
                raise fail
            return 'PONG\n'
        return ''

    def active(self, service):
        return service in self.sim.active

    def radio_info(self, iface):
        freq = self.sim.freq.get(iface)
        channel = (freq - 5000) // 5 if freq and freq > 5000 else ((freq - 2407) // 5 if freq else 0)
        return (f'Interface {iface}\n\ttype {self.sim.kind.get(iface, "managed")}\n\twiphy {1 if iface == IFACE else 0}\n'
                + f'\ttxpower {self.sim.power.get(iface, 21):.2f} dBm\n'
                + (f'\tchannel {channel} ({freq} MHz), width: 20 MHz\n' if freq else ''))


class Harness(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        for name in ('lib', 'wpa', 'run', 'acs', 'net/wlan1', 'net/wlan0'):
            (self.root / name).mkdir(parents=True)
        (self.root / 'net/wlan1/address').write_text('02:00:00:00:00:11\n')
        (self.root / 'net/wlan0/address').write_text('02:00:00:00:00:10\n')
        self.lib = self.root / 'lib'
        self.wpa = self.root / 'wpa'
        self.plan = self.root / 'static-channels.json'
        self.registry = self.root / 'registry'
        self.registry.write_text('')
        (self.root / 'boot').write_text('ab' * 16 + '\n')
        self.env = mock.patch.dict(os.environ, {
            'MANET_IFACE_STATE_DIR': str(self.lib), 'MANET_WPA_DIR': str(self.wpa),
            'MANET_ACS_RUN_DIR': str(self.root / 'run'), 'MANET_ACS_STATE_DIR': str(self.root / 'acs'),
            'MANET_MESH_CONF': str(self.root / 'mesh.conf'), 'MANET_SYS_NET': str(self.root / 'net'),
            'MANET_ACS_LOCK_FILE': str(self.root / 'run/channel.lock'),
            'MANET_AP_ROLE_LOCK': str(self.root / 'run/role.lock'),
            'MANET_RADIO_STATE_FILE': str(self.lib / 'mesh_radio_state.json'),
            'MANET_BOOT_ID_FILE': str(self.root / 'boot'),
            'MANET_STATIC_CHANNELS': str(self.plan), 'REGISTRY_FILE': str(self.registry),
            'MANET_TIME_RUN_DIR': str(self.root / 'run')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conf(acs='n')
        # Provisioned: wlan0 meshes on 2.4, wlan1 was the 5 GHz mesh radio
        # before setup assigned it to the EUD AP and cleared its active roles.
        self.write('ap_interface', IFACE)
        self.write('ap_mesh_band', '5')
        self.write('mesh_if', OTHER)
        self.write('mesh_24_if', OTHER)
        self.write('mesh_5_if', '')
        # The original bug: a supplicant file left from long ago.
        stale = 'network={\n    ssid="old"\n    frequency=5745\n    sae_password="old-key-1"\n}\n'
        (self.wpa / f'wpa_supplicant-{IFACE}.conf').write_text(stale)
        (self.wpa / f'wpa_supplicant-{IFACE}-lobby.conf').write_text(stale)
        self.radio = Radio()
        self.radio.active.add(f'wpa_supplicant@{OTHER}.service')
        self.radio.bat.add(OTHER)

    def conf(self, acs='n', ssid='mesh-net', key='current-key-1'):
        (self.root / 'mesh.conf').write_text(
            f'mesh_ssid={ssid}\nmesh_key={key}\nregulatory_domain=US\nacs={acs}\n'
            'max_euds_per_node=5\n')

    def write(self, name, value):
        (self.lib / name).write_text(value + '\n' if value else '')

    def read(self, name):
        path = self.lib / name
        return path.read_text().strip() if path.exists() else None

    def transition(self):
        return SimTransition(self.radio, self.wpa)

    def live(self):
        return (self.wpa / f'wpa_supplicant-{IFACE}.conf').read_text()

    def peer_registry(self, channel):
        interfaces = json.dumps([{'name': 'wlan1', 'role': 'mesh', 'state': 'UP',
                                  'channel': str(channel), 'freq_mhz': ''}])
        import time
        at = int(time.clock_gettime(time.CLOCK_BOOTTIME)) - 5
        lines = []
        for n in (1, 2):
            key = f'NODE_0200000001{n:02x}'
            lines += [f"{key}_MAC_ADDRESS='02:00:00:00:01:{n:02x}'",
                      f"{key}_NODE_STATE='ACTIVE'", f"{key}_OBSERVED_AT_UPTIME='{at}'",
                      f"{key}_INTERFACES_JSON='{interfaces}'"]
        self.registry.write_text('\n'.join(lines) + '\n')


class PowerOwnershipTests(Harness):
    def test_post_start_logs_cap_failure_without_failing_hostapd(self):
        transition = mock.Mock()
        transition.ap_power.side_effect = RuntimeError('cap did not stick')
        with mock.patch.object(manet_ap_mesh, 'Transition', return_value=transition), \
                mock.patch.object(sys, 'argv', ['helper', 'ap-power-post-start']), \
                mock.patch.object(sys, 'stderr', new_callable=io.StringIO) as warning, \
                mock.patch.object(sys, 'stdout', new_callable=io.StringIO):
            manet_ap_mesh.main()
            self.assertIn('WARNING: AP TX power cap failed', warning.getvalue())
        with mock.patch.object(manet_ap_mesh, 'Transition', return_value=transition), \
                mock.patch.object(sys, 'argv', ['helper', 'ap-power']), \
                self.assertRaisesRegex(RuntimeError, 'cap did not stick'):
            manet_ap_mesh.main()

    def test_ap_power_requires_both_ap_type_and_no_active_mesh_role(self):
        for kind, members, expected in [('AP', OTHER, True), ('mesh point', OTHER, False),
                                        ('managed', OTHER, False), ('AP', OTHER + ' ' + IFACE, False)]:
            with self.subTest(kind=kind, members=members):
                self.radio.kind[IFACE] = kind
                self.radio.power[IFACE] = 23
                self.write('mesh_if', members)
                self.radio.calls.clear()
                self.assertEqual(self.transition().ap_power()['changed'], expected)
                self.assertEqual('iw phy phy1 set txpower fixed 500' in self.radio.calls, expected)

    def test_stale_ap_role_cannot_prepare_or_cap_halow(self):
        for evidence in ('role', 'driver'):
            with self.subTest(evidence=evidence):
                self.write('halow_if', IFACE if evidence == 'role' else '')
                if evidence == 'driver':
                    device = self.root / 'net' / IFACE / 'device'
                    device.mkdir()
                    (device / 'driver').symlink_to(self.root / 'morse_usb')
                self.radio.calls.clear()
                for action in ('prepare_ap', 'ap_power', 'to_mesh'):
                    with self.assertRaisesRegex(ValueError, 'HaLow'):
                        getattr(self.transition(), action)()
                self.assertFalse(self.radio.calls)

    def test_successful_setter_without_effect_is_an_error(self):
        self.radio.ignore_power = True
        self.radio.power[IFACE] = 23
        with mock.patch.object(manet_ap_mesh.time, 'sleep'), \
                self.assertRaisesRegex(RuntimeError, 'TX power'):
            self.transition().ap_power()

    def test_lower_regulatory_ceiling_is_accepted(self):
        self.radio.ignore_power = True
        self.radio.power[IFACE] = 4
        self.assertFalse(self.transition().ap_power()['changed'])
        self.assertFalse(self.radio.calls)

    def test_ap_cap_is_idempotent_and_heals_a_later_power_reset(self):
        self.assertFalse(self.transition().ap_power()['changed'])
        self.assertFalse(self.radio.calls)
        self.radio.power[IFACE] = 23
        self.assertTrue(self.transition().ap_power()['changed'])
        self.assertEqual(self.radio.power[IFACE], 5)
        self.radio.calls.clear()
        self.assertFalse(self.transition().ap_power()['changed'])
        self.assertFalse(self.radio.calls)

    def test_phy_write_refuses_another_active_interface_on_the_same_radio(self):
        self.radio.power[IFACE] = 23
        directory = self.root / 'net' / OTHER
        (directory / 'phy80211').mkdir()
        (directory / 'phy80211/name').write_text('phy1\n')
        (directory / 'flags').write_text('0x1003\n')
        with self.assertRaisesRegex(ValueError, 'also serves'):
            self.transition().ap_power()
        self.assertEqual(self.radio.calls, [])
        (directory / 'flags').write_text('0x1002\n')
        self.assertTrue(self.transition().ap_power()['changed'])


class ToMeshTests(Harness):
    def test_returning_radio_follows_current_plan_not_stale_file(self):
        static.save('5', 5200, self.plan)
        result = self.transition().to_mesh()
        self.assertEqual(result['mode'], 'mesh')
        self.assertEqual(self.radio.freq[IFACE], 5200)
        self.assertIn('frequency=5200', self.live())
        self.assertIn('ssid="mesh-net"', self.live())
        self.assertIn('sae_password="current-key-1"', self.live())
        self.assertEqual(self.read('mesh_5_if'), IFACE)
        self.assertEqual(self.read('mesh_if').split(), [OTHER, IFACE])
        self.assertIn(IFACE, self.radio.bat)
        self.assertNotIn('hostapd.service', self.radio.active)
        self.assertEqual(self.read('mesh_24_if'), OTHER)
        # The AP's PHY-wide 5 dBm cap must not follow the radio into the mesh.
        calls = self.radio.calls
        self.assertIn('iw phy phy1 set txpower fixed 3000', calls)
        self.assertLess(calls.index('iw phy phy1 set txpower fixed 3000'),
                        calls.index(f'systemctl restart wpa_supplicant@{IFACE}.service'))

    def test_registry_evidence_does_not_persist_a_static_plan(self):
        # Telemetry is unauthenticated: mesh membership alone must not change
        # the stored plan. It may only corroborate or question it.
        static.save('5', 5200, self.plan)
        self.peer_registry(44)
        self.transition().to_mesh()
        self.assertEqual(static.load(self.plan)['5'], 5200)
        self.assertEqual(self.radio.freq[IFACE], 5200)

    def test_second_call_is_a_no_op(self):
        static.save('5', 5200, self.plan)
        self.transition().to_mesh()
        self.radio.calls.clear()
        result = self.transition().to_mesh()
        self.assertFalse(result['changed'])
        # Stopping an already inactive hostapd is harmless; anything that
        # touches the meshing radio is not.
        self.assertFalse([c for c in self.radio.calls
                          if c.startswith(('systemctl restart', 'batctl if add', 'batctl if del',
                                           'iw dev', 'ip link'))
                          or (c.startswith('systemctl stop') and 'hostapd' not in c)])

    def rollback_case(self):
        before = {name: self.read(name) for name in ('mesh_if', 'mesh_24_if', 'mesh_5_if')}
        live = self.live()
        with self.assertRaises(RuntimeError):
            self.transition().to_mesh()
        self.assertEqual({name: self.read(name) for name in before}, before)
        self.assertEqual(self.live(), live)
        self.assertIn('hostapd.service', self.radio.active)
        self.assertNotIn(IFACE, self.radio.bat)
        self.assertNotIn(f'wpa_supplicant@{IFACE}.service', self.radio.active)

    def test_rollback_when_supplicant_fails(self):
        self.radio.fail_supplicant = True
        self.rollback_case()

    def test_rollback_when_bat0_attach_fails(self):
        self.radio.fail_attach = True
        self.rollback_case()

    def test_rollback_when_radio_lands_on_wrong_channel(self):
        self.radio.wrong_channel = 5220
        with mock.patch.object(manet_ap_mesh, 'READY_TIMEOUT', 0.3):
            self.rollback_case()

    def test_ap_only_radio_is_never_meshed(self):
        self.write('ap_mesh_band', '')
        result = self.transition().to_mesh()
        self.assertEqual(result['mode'], 'off')
        self.assertIsNone(self.read('mesh_5_if') or None)
        self.assertNotIn(IFACE, self.read('mesh_if').split())
        self.assertNotIn(IFACE, self.radio.bat)
        self.assertFalse([c for c in self.radio.calls if 'wpa_supplicant@wlan1' in c
                          and c.startswith(('systemctl start', 'systemctl restart'))])

    def test_disabled_mesh_candidate_takes_role_but_stays_down(self):
        (self.lib / 'mesh_radio_state.json').write_text(json.dumps({'desired': {IFACE: 'down'}}))
        result = self.transition().to_mesh()
        self.assertEqual(result['mode'], 'mesh-disabled')
        self.assertEqual(self.read('mesh_5_if'), IFACE)
        self.assertNotIn(IFACE, self.radio.bat)
        self.assertNotIn(f'wpa_supplicant@{IFACE}.service', self.radio.active)

    def test_missing_capability_metadata_refuses_without_touching_ap(self):
        (self.lib / 'ap_mesh_band').unlink()
        with self.assertRaises((ValueError, RuntimeError)):
            self.transition().to_mesh()
        self.assertIn('hostapd.service', self.radio.active)
        self.assertEqual(self.read('mesh_5_if'), '')

    def test_empty_role_files_are_handled(self):
        for name in ('mesh_if', 'mesh_24_if', 'mesh_5_if'):
            self.write(name, '')
        static.save('5', 5180, self.plan)
        result = self.transition().to_mesh()
        self.assertEqual(result['mode'], 'mesh')
        self.assertEqual(self.read('mesh_if').split(), [IFACE])

    def test_band_owned_by_another_radio_refuses(self):
        self.write('mesh_5_if', 'wlan3')
        with self.assertRaises(RuntimeError):
            self.transition().to_mesh()
        self.assertEqual(self.read('mesh_5_if'), 'wlan3')
        self.assertIn('hostapd.service', self.radio.active)

    def test_forbidden_plan_frequency_keeps_ap(self):
        static.save('5', 5260, self.plan)      # radar channel on this PHY
        with self.assertRaises((ValueError, RuntimeError)):
            self.transition().to_mesh()
        self.assertIn('hostapd.service', self.radio.active)

    def test_malformed_static_plan_keeps_ap(self):
        self.plan.write_text('{')
        with self.assertRaises((ValueError, RuntimeError)):
            self.transition().to_mesh()
        self.assertIn('hostapd.service', self.radio.active)

    def test_acs_without_live_destination_uses_anchor(self):
        self.conf(acs='y')
        result = self.transition().to_mesh()
        self.assertEqual(result['frequency'], 5180)
        self.assertEqual(self.radio.freq[IFACE], 5180)
        self.assertIn('frequency=5180', (self.wpa / f'wpa_supplicant-{IFACE}-lobby.conf').read_text())

    def test_acs_ignores_registry_without_agreement(self):
        # Peers on 5220 in telemetry do not authorize an ACS move.
        self.conf(acs='y')
        self.peer_registry(44)
        result = self.transition().to_mesh()
        self.assertEqual(self.radio.freq[IFACE], 5180)
        self.assertEqual(result['source'], 'anchor')


class AgreementTests(Harness):
    BOOT = 'ab' * 16

    def setUp(self):
        super().setUp()
        self.conf(acs='y')
        (self.root / 'run' / 'initial_time_synced').touch()
        # The other radio is stable on the agreed 2.4 GHz channel.
        (self.wpa / f'wpa_supplicant-{OTHER}.conf').write_text('network={\n    frequency=2462\n}\n')

    def destination(self, channels):
        import time
        created = (int(time.time()) // 180 - 1) * 180 + 45   # activated already
        members = {'02:00:00:00:00:10': acs_status('a', current={'2.4': 2462}),
                   '02:00:00:00:01:01': acs_status('b', current={'2.4': 2462})}
        plan = agreement.make_plan(members, channels, False, created, 'e' * 32)
        commit = {'plan': agreement.digest(plan),
                  'approvals': {m: st['boot'] for m, st in members.items()},
                  'issued_at': plan['deadline']}
        return {'plan': plan, 'commit': commit}

    def keep(self, destination, boot=None):
        (self.root / 'acs' / 'agreement.json').write_text(json.dumps(
            {'clock_boot': boot or self.BOOT, 'destination': destination}))

    def test_committed_destination_for_this_boot_is_used(self):
        self.keep(self.destination({'2.4': 2462, '5': 5220}))
        result = self.transition().to_mesh()
        self.assertEqual((result['frequency'], result['source']), (5220, 'acs-agreement'))
        self.assertEqual(self.radio.freq[IFACE], 5220)
        # The boot copy stays on the shared anchor for recovery.
        self.assertIn('frequency=5180', (self.wpa / f'wpa_supplicant-{IFACE}-lobby.conf').read_text())

    def test_unstable_other_radio_means_no_live_destination(self):
        (self.wpa / f'wpa_supplicant-{OTHER}.conf').unlink()
        self.keep(self.destination({'2.4': 2462, '5': 5220}))
        self.assertEqual(self.transition().to_mesh()['frequency'], 5180)

    def test_destination_from_an_earlier_boot_is_ignored(self):
        self.keep(self.destination({'2.4': 2462, '5': 5220}), boot='cd' * 16)
        result = self.transition().to_mesh()
        self.assertEqual((result['frequency'], result['source']), (5180, 'anchor'))

    def test_destination_without_this_band_uses_anchor(self):
        self.keep(self.destination({'2.4': 2462}))
        self.assertEqual(self.transition().to_mesh()['frequency'], 5180)

    def assert_ap_kept(self):
        before = {name: self.read(name) for name in ('mesh_if', 'mesh_24_if', 'mesh_5_if')}
        with self.assertRaises(RuntimeError):
            self.transition().to_mesh()
        self.assertIn('hostapd.service', self.radio.active)
        self.assertEqual({name: self.read(name) for name in before}, before)
        self.assertNotIn(IFACE, self.radio.bat)

    def test_agreement_in_progress_keeps_serving_ap(self):
        import time
        (self.root / 'run' / 'manet-acs-busy').write_text(str(int(time.time()) + 60))
        self.assert_ap_kept()
        self.assertNotIn('systemctl stop hostapd.service', self.radio.calls)

    def test_held_channel_lock_keeps_serving_ap(self):
        # A tourguide visit holds this lock for its whole duration.
        with open(self.root / 'run' / 'channel.lock', 'a') as held, \
                mock.patch.object(manet_ap_mesh, 'LOCK_TIMEOUT', 0.2):
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assert_ap_kept()


class RegistryValidationTests(Harness):
    def test_peers_on_the_plan_confirm_it(self):
        static.save('5', 5200, self.plan)
        self.peer_registry(40)
        result = self.transition().to_mesh()
        self.assertEqual((result['frequency'], result['registry'], result['peers']),
                         (5200, 'confirmed', 2))

    def test_peers_elsewhere_are_reported_not_followed(self):
        static.save('5', 5200, self.plan)
        self.peer_registry(44)
        result = self.transition().to_mesh()
        self.assertEqual((result['registry'], result['peer_frequency']), ('conflict', 5220))
        self.assertEqual(self.radio.freq[IFACE], 5200)
        self.assertEqual(static.load(self.plan)['5'], 5200)


class PrepareApTests(Harness):
    def meshing(self):
        static.save('5', 5200, self.plan)
        self.transition().to_mesh()
        self.radio.active.discard('hostapd.service')

    def test_prepare_ap_withdraws_roles_and_detaches(self):
        self.meshing()
        result = self.transition().prepare_ap()
        self.assertEqual(result['mode'], 'ap')
        self.assertEqual(self.read('mesh_5_if'), '')
        self.assertEqual(self.read('mesh_if').split(), [OTHER])
        self.assertEqual(self.read('mesh_24_if'), OTHER)
        self.assertNotIn(IFACE, self.radio.bat)
        self.assertNotIn(f'wpa_supplicant@{IFACE}.service', self.radio.active)
        self.assertEqual(self.radio.kind[IFACE], 'managed')

    def test_prepare_ap_failure_restores_previous_mesh(self):
        self.meshing()
        self.radio.fail_on.add(f'iw dev {IFACE} set type managed')
        with self.assertRaises(subprocess.CalledProcessError):
            self.transition().prepare_ap()
        self.assertEqual(self.read('mesh_5_if'), IFACE)
        self.assertIn(IFACE, self.read('mesh_if').split())
        self.assertIn(IFACE, self.radio.bat)
        self.assertEqual((self.radio.kind[IFACE], self.radio.freq[IFACE]), ('mesh point', 5200))

    def test_round_trip_returns_on_the_plan_changed_while_ap(self):
        self.meshing()
        self.transition().prepare_ap()
        self.radio.active.add('hostapd.service')
        # An authenticated static change arrives while the radio is the AP;
        # absent-band nodes save the plan without touching the radio.
        static.save('5', 5220, self.plan)
        self.transition().to_mesh()
        self.assertEqual(self.radio.freq[IFACE], 5220)


if __name__ == '__main__':
    unittest.main()
