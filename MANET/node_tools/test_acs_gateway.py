#!/usr/bin/env python3
"""ACS in auto EUD mode: gateways serve the AP and never tourguide."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_acs as acs
from test_acs import OWN, PEER, THIRD, RADIO, node, runtime, tourguide
import test_acs_halow_tourguide as halow
import test_ap_mesh_transitions as transitions
from test_ap_mesh_transitions import IFACE


def gateway(mac):
    return f"NODE_{mac.replace(':', '')}_IS_GATEWAY='true'\n"


class GatewayElectionTests(unittest.TestCase):
    def test_gateway_is_never_elected(self):
        # OWN has the oldest turn and would win without its gateway flag.
        registry = node(OWN, 0) + gateway(OWN) + node(PEER, 10, aliases=RADIO) + node(THIRD, 20)
        for own, peers in [(OWN, [RADIO, THIRD]), (PEER, [OWN, THIRD]), (THIRD, [OWN, RADIO])]:
            self.assertEqual(tourguide.elect(own, peers, registry, '2.4'), PEER)

    def test_mesh_of_only_gateways_elects_nobody(self):
        registry = ''.join(node(mac, n) + gateway(mac) for n, mac in enumerate((OWN, PEER, THIRD)))
        self.assertIsNone(tourguide.elect(OWN, [PEER, THIRD], registry, '2.4'))

    def test_false_gateway_flag_keeps_node_eligible(self):
        registry = (node(OWN, 0) + f"NODE_{OWN.replace(':', '')}_IS_GATEWAY='false'\n"
                    + node(PEER, 10))
        self.assertEqual(tourguide.elect(OWN, [PEER], registry, '2.4'), OWN)


class GatewayTourguideTests(halow.HaLowTourguideTests):
    """Only the gateway case; the inherited HaLow tests run in their module."""

    def test_local_gateway_state_skips_the_hop(self):
        (self.roles / 'halow_if').unlink()
        self.registry.write_text(node(OWN, 0) + node(PEER, 20, aliases=RADIO))
        (self.root / 'peers').write_text(json.dumps([{'orig_address': RADIO}]))
        state = self.root / 'mesh-gateway.state'
        state.touch()
        self.env['MANET_GATEWAY_STATE'] = str(state)
        result = self.run_tourguide()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'sent-75').exists())
        commands = self.root / 'commands'
        self.assertNotIn('reconfigure', commands.read_text() if commands.exists() else '')
        # Control: the same node hops once it is no longer a gateway.
        state.unlink()
        result = self.run_tourguide()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'sent-75').exists(), result.stderr)


class ParkedRadioTests(acs.AcsHarness):
    def setUp(self):
        super().setUp()
        self.configure(2437, None)
        # wlan1 was the 5 GHz mesh radio until the gateway gave it to the AP.
        (self.roles / 'ap_interface').write_text('wlan1\n')
        (self.roles / 'ap_mesh_band').write_text('5\n')
        self.parked = self.wpa / 'wpa_supplicant-wlan1.conf'
        self.parked.write_text('mesh_max_inactivity=10\nnetwork={\nfrequency=5180\n}\n')
        env = patch.dict(os.environ, self.env)
        env.start(); self.addCleanup(env.stop)
        self.runner = runtime.Runtime()

    def test_tick_syncs_parked_config_from_a_learned_destination(self):
        self.runner.state['destination'] = {'plan': {'channels': {'2.4': 2437, '5': 5745}}}
        with patch.object(self.runner, '_tick'):
            self.runner.tick(0)
        self.assertIn('frequency=5745', self.parked.read_text())

    def test_clockless_tick_keeps_the_recovered_parked_channel(self):
        self.parked.write_text(self.parked.read_text().replace('5180', '5745'))
        self.runner.state['destination'] = {'plan': {'channels': {'2.4': 2437, '5': 5200}}}
        with patch.object(self.runner, '_tick'), patch.object(runtime, 'clock_ready', return_value=False):
            self.runner.tick(0)
        self.assertIn('frequency=5745', self.parked.read_text())

    def test_clock_arriving_during_a_tick_does_not_sync_the_old_plan(self):
        self.parked.write_text(self.parked.read_text().replace('5180', '5745'))
        self.runner.state['destination'] = {'plan': {'channels': {'2.4': 2437, '5': 5200}}}
        with patch.object(self.runner, '_tick'), \
                patch.object(runtime, 'clock_ready', side_effect=[False, True, True]):
            self.runner.tick(0)
        self.assertIn('frequency=5745', self.parked.read_text())

    def test_invalid_band_frequency_is_never_parked(self):
        for channels in ({'5': 5500}, {'5': 5745.0}, {'5': None}, None, ['5']):
            self.runner.sync_parked(channels)
        self.assertIn('frequency=5180', self.parked.read_text())

    def test_agreed_plan_updates_parked_config_without_touching_the_radio(self):
        self.runner.apply({'2.4': 2437, '5': 5745})
        self.assertIn('frequency=5745', self.parked.read_text())
        commands = (self.root / 'commands').read_text() if (self.root / 'commands').exists() else ''
        self.assertNotIn('wlan1', commands)

    def test_discovery_rotation_leaves_parked_config_alone(self):
        self.runner.apply({'2.4': 2437}, discovering=True)
        self.assertIn('frequency=5180', self.parked.read_text())

    def test_radio_holding_its_mesh_role_is_not_treated_as_parked(self):
        (self.roles / 'mesh_5_if').write_text('wlan1\n')
        self.assertIsNone(self.runner.parked())

    def test_without_ap_role_nothing_is_parked(self):
        (self.roles / 'ap_interface').unlink()
        self.assertIsNone(self.runner.parked())


class ParkedRejoinTests(transitions.AgreementTests):
    """Only the parked-config cases; the inherited agreement tests run once."""

    def test_parked_config_is_not_authority_even_with_peers_on_it(self):
        # Unsigned telemetry agreeing with a saved file must not choose the
        # channel. Only an authenticated destination or the anchor does.
        (self.wpa / f'wpa_supplicant-{IFACE}.conf').write_text('network={\n    frequency=5220\n}\n')
        self.peer_registry(44)
        result = self.transition().to_mesh()
        self.assertEqual((result['frequency'], result['source']), (5180, 'anchor'))

    def test_unconfirmed_parked_config_falls_back_to_anchor(self):
        (self.wpa / f'wpa_supplicant-{IFACE}.conf').write_text('network={\n    frequency=5220\n}\n')
        result = self.transition().to_mesh()
        self.assertEqual((result['frequency'], result['source']), (5180, 'anchor'))

    def test_live_destination_still_wins_over_parked_config(self):
        (self.wpa / f'wpa_supplicant-{IFACE}.conf').write_text('network={\n    frequency=5745\n}\n')
        self.keep(self.destination({'2.4': 2462, '5': 5220}))
        result = self.transition().to_mesh()
        self.assertEqual((result['frequency'], result['source']), (5220, 'acs-agreement'))


class UnplugHookTests(unittest.TestCase):
    """networkd-dispatcher's off hook applies the same auto-mode rule."""
    def unplug(self, conf, usb_uplink=False, usb_eud_carrier=None):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = self.root = Path(scratch.name)
        for directory in ('var/run', 'var/lib', 'etc', 'sys/class/net/end0',
                          'sys/class/net/usb0', 'usr/local/bin'):
            (root / directory).mkdir(parents=True)
        (root / 'sys/class/net/end0/carrier').write_text('0\n')
        (root / 'sys/class/net/usb0/carrier').write_text('1\n')
        (root / 'var/run/upstream_iface').write_text('usb0\n' if usb_uplink else 'end0\n')
        (root / 'sys/class/net/br0').mkdir()
        if usb_eud_carrier is not None:
            (root / 'sys/class/net/usb1').mkdir()
            (root / 'sys/class/net/usb1/carrier').write_text(usb_eud_carrier + '\n')
            os.symlink('../br0', root / 'sys/class/net/usb1/master')
        (root / 'etc/mesh.conf').write_text(conf)
        (root / 'var/lib/ap_interface').write_text('wlan1\n')
        (root / 'var/run/mesh-gateway.state').touch()
        source = (acs.TOOLS.parent / 'networkd-dispatcher/off').read_text()
        source = re.sub(r'/(?:etc|run|var|sys|usr/local/bin)/', lambda m: str(root) + m[0], source)
        stubs = """
python3() { echo "python3 ${1##*/} ${*:2}"; }
systemctl() { echo "systemctl $*"; }
ip() { :; }; networkctl() { :; }; nft() { :; }; batctl() { :; }
systemd-cat() { cat >/dev/null; }
"""
        result = subprocess.run(['bash', '-c', stubs + source], capture_output=True, text=True,
                                timeout=20, env=dict(os.environ, IFACE='end0'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((root / 'var/run/mesh-gateway.state').exists(), usb_uplink)
        return result.stdout

    def test_acs_returns_ap_radio_to_mesh_when_no_uplink_remains(self):
        out = self.unplug('eud=auto\nacs=y\n')
        self.assertIn('python3 manet_ap_mesh.py mesh', out)
        self.assertNotIn('systemctl start hostapd.service', out)

    def test_surviving_usb_uplink_keeps_gateway_and_serves_ap(self):
        # A wired EUD on end0 had kept the radio in mesh; it is now gone.
        out = self.unplug('eud=auto\nacs=y\n', usb_uplink=True)
        self.assertNotIn('manet_ap_mesh.py', out)
        self.assertIn('systemctl start hostapd.service', out)
        self.assertNotIn('restart gateway-route-manager', out)

    def test_remaining_wired_eud_keeps_radio_in_mesh_on_usb_gateway(self):
        out = self.unplug('eud=auto\nacs=y\n', usb_uplink=True, usb_eud_carrier='1')
        self.assertIn('python3 manet_ap_mesh.py mesh', out)
        self.assertNotIn('systemctl start hostapd.service', out)

    def test_unplugged_second_eud_port_does_not_block_the_ap(self):
        out = self.unplug('eud=auto\nacs=y\n', usb_uplink=True, usb_eud_carrier='0')
        self.assertIn('systemctl start hostapd.service', out)

    def test_static_channels_keep_the_ap(self):
        out = self.unplug('eud=auto\nacs=n\n')
        self.assertIn('systemctl start hostapd.service', out)
        self.assertNotIn('manet_ap_mesh.py mesh', out)

    def test_wireless_mode_keeps_the_ap_with_acs_set(self):
        out = self.unplug('eud=wireless\nacs=y\n')
        self.assertIn('systemctl start hostapd.service', out)


class PrepareApGuardTests(transitions.Harness):
    """hostapd's ExecStartPre enforces the gateway-only AP rule."""
    def setUp(self):
        super().setUp()
        self.state = self.root / 'run' / 'mesh-gateway.state'
        env = patch.dict(os.environ, MANET_GATEWAY_STATE=str(self.state))
        env.start(); self.addCleanup(env.stop)
        self.write('mesh_5_if', IFACE)
        self.write('mesh_if', f'{transitions.OTHER}\n{IFACE}')

    def auto(self, acs):
        with (self.root / 'mesh.conf').open('a') as conf:
            conf.write(f'eud=auto\nacs={acs}\n')

    def test_non_gateway_refuses_ap_and_keeps_mesh_role(self):
        self.auto('y')
        with self.assertRaisesRegex(ValueError, 'only a gateway'):
            self.transition().prepare_ap()
        self.assertEqual(self.read('mesh_5_if'), IFACE)

    def test_gateway_may_serve_ap(self):
        self.auto('y')
        self.state.touch()
        self.assertEqual(self.transition().prepare_ap()['mode'], 'ap')
        self.assertEqual(self.read('mesh_5_if'), '')

    def test_demotion_during_lock_wait_is_seen(self):
        self.auto('y')
        self.state.touch()
        real = transitions.manet_ap_mesh.locked

        @contextmanager
        def demoted_while_waiting(path):
            self.state.unlink(missing_ok=True)
            with real(path):
                yield
        with patch.object(transitions.manet_ap_mesh, 'locked', demoted_while_waiting):
            with self.assertRaisesRegex(ValueError, 'only a gateway'):
                self.transition().prepare_ap()
        self.assertEqual(self.read('mesh_5_if'), IFACE)

    def test_static_auto_mode_is_unchanged(self):
        self.auto('n')
        self.assertEqual(self.transition().prepare_ap()['mode'], 'ap')


def run_inherited_tests_once():
    """Disable inherited tests so each runs once, in its own module."""
    for base, case in ((transitions.AgreementTests, ParkedRejoinTests),
                       (halow.HaLowTourguideTests, GatewayTourguideTests)):
        for name in [n for n in dir(base) if n.startswith('test_')]:
            setattr(case, name, None)


run_inherited_tests_once()


if __name__ == '__main__':
    unittest.main()
