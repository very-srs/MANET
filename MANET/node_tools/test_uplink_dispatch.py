#!/usr/bin/env python3
"""manet-uplink-dispatch.sh against a simulated node: wired-EUD protection."""

import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


TOOLS = Path(__file__).resolve().parent

SPEED_STUB = r'''#!/bin/bash
# measure: exit with $T/speed-rc/IFACE if present; metered names exit 2.
T="$REVIEW_ROOT"
echo "uplink-speed $*" >> "$T/calls"
if [ "$1" = is-ethernet ]; then
  case "$2" in usb*) exit 1 ;; esac
fi
if [ "$1" = measure ]; then
  [ -f "$T/speed-rc/$2" ] && exit "$(cat "$T/speed-rc/$2")"
  case "$2" in usb*) exit 2 ;; esac
  echo 50.0
fi
exit 0
'''

STUB = r'''#!/bin/bash
T="$REVIEW_ROOT"
echo "$(basename "$0") $*" >> "$T/calls"
case "$(basename "$0")" in
  ip)
    case "$*" in
      "-4 -o addr show dev "*) f="$T/addr/${@: -1}"; [ -f "$f" ] && echo "2: ${@: -1} inet $(cat "$f")/24 brd x scope global" ;;
      "route show default dev "*) f="$T/gw/${@: -1}"; [ -f "$f" ] && echo "default via $(cat "$f") dev ${@: -1}" ;;
      "route show default") for f in "$T"/gw/*; do [ -f "$f" ] && echo "default via $(cat "$f") dev $(basename "$f")"; done ;;
      "link show "*) i="${@: -1}"; [ -L "$T/net/$i/master" ] && echo "3: $i: <UP> master $(basename "$(readlink "$T/net/$i/master")")" || echo "3: $i: <UP>" ;;
      "link set "*" nomaster") rm -f "$T/net/$3/master" ;;
      "link set "*" master br0") ln -sfn ../br0 "$T/net/$3/master" ;;
    esac ;;
  systemctl)
    case "$1" in
      is-active) [ -f "$T/active/${@: -1}" ]; exit $? ;;
      is-enabled) echo enabled ;;
    esac ;;
  ping) for a in "$@"; do [ -f "$T/internet/$a" ] && exit 0; done; exit 1 ;;
  curl)
    while [ $# -gt 0 ]; do
      if [ "$1" = --interface ]; then iface="$2"; break; fi
      shift
    done
    [ -f "$T/http-code/$iface" ] && cat "$T/http-code/$iface" && exit 0
    [ -f "$T/internet/$iface" ] && printf 204 && exit 0
    exit 1 ;;
  nc) exit 1 ;;
  systemd-cat) cat >> "$T/journal" ;;
esac
exit 0
'''


class DispatchHarness(unittest.TestCase):
    """Run uplink events against an isolated node filesystem."""
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        for name in ('bin', 'net', 'run', 'addr', 'gw', 'active', 'internet',
                     'networkd', 'bus/usb', 'http-code'):
            (self.root / name).mkdir(parents=True)
        for tool in ('ip', 'systemctl', 'batctl', 'networkctl', 'nft', 'ping', 'curl', 'nc',
                     'systemd-cat', 'mesh-ip-manager'):
            path = self.root / 'bin' / tool
            path.write_text(STUB)
            path.chmod(0o755)
        speed = self.root / 'bin' / 'uplink-speed'
        speed.write_text(SPEED_STUB)
        speed.chmod(0o755)
        (self.root / 'speed-rc').mkdir()
        helper = self.root / 'bin' / 'manet_ap_mesh.py'
        helper.write_text('import os, sys\nroot = os.environ["REVIEW_ROOT"]\n'
                          'open(root + "/calls", "a").write("ap-mesh-helper " + " ".join(sys.argv[1:]) + "\\n")\n'
                          'reply = root + "/helper-reply"\n'
                          'text = open(reply).read() if os.path.exists(reply) else "0|{}"\n'
                          'code, _, body = text.partition("|")\n'
                          'print(body, file=sys.stderr if code != "0" else sys.stdout)\n'
                          'sys.exit(int(code))\n')
        helper.chmod(0o755)
        self.calls = self.root / 'calls'
        self.eth_lock = self.root / 'run' / 'ethernet-autodetect.lock'
        (self.root / 'radvd-mesh.conf').write_text('mesh\n')
        (self.root / 'radvd-gateway.conf').write_text('gateway\n')
        self.iface('br0')
        self.iface('wlan1')
        (self.root / 'ap_interface').write_text('wlan1\n')
        self.mode('auto')

    def iface(self, name, carrier=True, bridged=False, usb=False):
        path = self.root / 'net' / name
        path.mkdir(exist_ok=True)
        (path / 'carrier').write_text('1\n' if carrier else '0\n')
        if bridged:
            os.symlink('../br0', path / 'master')
        if usb:
            (path / 'device').mkdir()
            os.symlink('../../../bus/usb', path / 'device' / 'subsystem')

    def mode(self, eud):
        (self.root / 'mesh.conf').write_text(f'eud={eud}\nipv4_network=10.30.2.0/24\n')

    def eth_state(self, mode):
        (self.root / 'run' / 'ethernet_detection_state').write_text(
            f'ETH_MODE={mode}\nETH_BRIDGE=br0\n')

    def dispatch(self, event='reconcile', iface='', extra_env=None):
        """Run one isolated uplink event with its shared shell libraries."""
        source = (TOOLS / 'manet-uplink-dispatch.sh').read_text()
        common = (TOOLS / 'manet-common.sh').read_text()
        root = str(self.root)
        source = source.replace('/var/run/', '/run/')
        common = common.replace('/var/run/', '/run/')
        for old, new in (('/sys/class/net', root + '/net'), ('/run/', root + '/run/'),
                         ('/etc/mesh.conf', root + '/mesh.conf'),
                         ('/var/lib/ap_interface', root + '/ap_interface'),
                         ('/etc/systemd/network', root + '/networkd'),
                         ('/etc/radvd-mesh.conf', root + '/radvd-mesh.conf'),
                         ('/etc/radvd-gateway.conf', root + '/radvd-gateway.conf'),
                         ('/etc/radvd.conf', root + '/radvd.conf'),
                         ('/usr/local/bin/mesh-ip-manager.sh', root + '/bin/mesh-ip-manager')):
            source = source.replace(old, new)
            common = common.replace(old, new)
        (self.root / 'manet-common.sh').write_text(common)
        (self.root / 'manet-runtime-client.sh').write_text(
            (TOOLS / 'manet-runtime-client.sh').read_text())
        script = self.root / 'dispatch.sh'
        script.write_text(source)
        env = dict(os.environ, REVIEW_ROOT=root, MANET_TOOLS_DIR=root,
                   PATH=os.pathsep.join((str(self.root / 'bin'), os.environ['PATH'])),
                   MANET_ETH_DETECT_LOCK=str(self.eth_lock),
                   MANET_AP_MESH_HELPER=str(self.root / 'bin' / 'manet_ap_mesh.py'),
                   MANET_UPLINK_SPEED=str(self.root / 'bin' / 'uplink-speed'))
        env.update(extra_env or {})
        result = subprocess.run(['bash', str(script), event, iface], env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def assert_eud_port_untouched(self, calls):
        touched = [c for c in calls if c.startswith(('ip link set end0', 'ip addr flush dev end0',
                                                      'networkctl reconfigure end0'))]
        self.assertEqual(touched, [])
        self.assertTrue((self.root / 'net' / 'end0' / 'master').is_symlink())
        self.assertFalse((self.root / 'networkd' / '20-end0.network').exists())


class WiredEudTests(DispatchHarness):
    def wired_eud(self):
        self.iface('end0', bridged=True)
        self.eth_state('WIRED_EUD')
        (self.root / 'run' / 'upstream_iface').write_text('end0\n')

    def test_reconcile_leaves_wired_eud_port_and_record_alone(self):
        self.wired_eud()
        calls = self.dispatch()
        self.assert_eud_port_untouched(calls)
        self.assertIn('ETH_MODE=WIRED_EUD',
                      (self.root / 'run' / 'ethernet_detection_state').read_text())
        # Not a demotion: nothing restarted just because the record exists.
        self.assertNotIn('systemctl restart gateway-route-manager.service', calls)

    def test_auto_mode_with_wired_eud_returns_ap_radio_to_mesh(self):
        self.wired_eud()
        calls = self.dispatch()
        self.assertIn('ap-mesh-helper mesh', calls)
        self.assertFalse([c for c in calls if 'start' in c and 'hostapd' in c])

    def test_auto_mode_without_wired_eud_keeps_ap(self):
        self.iface('end0', carrier=False)
        calls = self.dispatch()
        self.assertIn('systemctl start hostapd.service', calls)
        self.assertNotIn('ap-mesh-helper mesh', calls)
        # hostapd bridges the AP (bridge=br0); the dispatcher never does.
        self.assertFalse([c for c in calls if c.startswith('ip link set wlan1')])

    def test_wired_mode_always_returns_ap_radio_to_mesh(self):
        self.mode('wired')
        self.iface('end0', carrier=False)
        calls = self.dispatch()
        self.assertIn('ap-mesh-helper mesh', calls)
        self.assertFalse([c for c in calls if 'hostapd' in c and 'start' in c])

    def test_wireless_mode_keeps_ap_even_with_wired_eud(self):
        self.mode('wireless')
        self.wired_eud()
        calls = self.dispatch()
        self.assertIn('systemctl start hostapd.service', calls)
        self.assertNotIn('ap-mesh-helper mesh', calls)
        self.assert_eud_port_untouched(calls)

    def test_teardown_event_does_not_detach_wired_eud_port(self):
        self.wired_eud()
        calls = self.dispatch('no-carrier', 'usb0')
        self.assert_eud_port_untouched(calls)

    def test_usb_uplink_coexists_with_wired_eud(self):
        self.wired_eud()
        self.iface('usb0', usb=True)
        (self.root / 'addr' / 'usb0').write_text('192.168.42.5')
        (self.root / 'gw' / 'usb0').write_text('192.168.42.1')
        (self.root / 'internet' / 'usb0').touch()
        calls = self.dispatch('routable', 'usb0')
        self.assert_eud_port_untouched(calls)
        self.assertIn('usb0', (self.root / 'run' / 'upstream_iface').read_text())
        self.assertIn('ETH_MODE=WIRED_EUD',
                      (self.root / 'run' / 'ethernet_detection_state').read_text())
        self.assertIn('ap-mesh-helper mesh', calls)

    def test_skips_pass_while_ethernet_autodetect_runs(self):
        self.iface('end0')
        with open(self.eth_lock, 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            calls = self.dispatch()
        self.assertEqual(calls, [])

    def test_unbridged_end0_is_still_probed_as_uplink(self):
        # Protection is for bridged ports only; a plain end0 is a candidate.
        self.iface('end0')
        (self.root / 'addr' / 'end0').write_text('192.168.1.20')
        (self.root / 'gw' / 'end0').write_text('192.168.1.1')
        (self.root / 'internet' / 'end0').touch()
        self.dispatch()
        self.assertEqual((self.root / 'run' / 'upstream_iface').read_text().strip(), 'end0')

    def test_failed_ap_start_is_reported_and_not_bridged(self):
        self.iface('end0', carrier=False)
        stub = self.root / 'bin' / 'systemctl'
        stub.write_text(stub.read_text().replace(
            '    case "$1" in', '    [ "$1 $2" = "start hostapd.service" ] && exit 1\n    case "$1" in', 1))
        calls = self.dispatch()
        self.assertIn('systemctl start hostapd.service', calls)
        self.assertNotIn('systemctl start dnsmasq.service', calls)

    def helper_log(self, reply):
        if not (self.root / 'net' / 'end0').exists():
            self.wired_eud()
        (self.root / 'helper-reply').write_text(reply)
        self.dispatch()
        return [c for c in self.calls.read_text().splitlines() if c.startswith('systemd-cat')], \
            (self.root / 'journal').read_text() if (self.root / 'journal').exists() else ''

    def test_helper_results_reach_the_log_only_when_they_matter(self):
        _, quiet = self.helper_log('0|{"mode": "mesh", "changed": false, "registry": "confirmed"}')
        self.assertNotIn('AP radio', quiet)
        (self.root / 'journal').unlink(missing_ok=True)
        _, changed = self.helper_log('0|{"mode": "mesh", "changed": true, "frequency": 5200}')
        self.assertIn('"frequency": 5200', changed)
        (self.root / 'journal').unlink(missing_ok=True)
        _, conflict = self.helper_log('0|{"changed": false, "registry": "conflict", "peer_frequency": 5220}')
        self.assertIn('"peer_frequency": 5220', conflict)
        (self.root / 'journal').unlink(missing_ok=True)
        _, failed = self.helper_log('1|Radio transition busy; retry shortly')
        self.assertIn('Radio transition busy', failed)

    def test_missing_helper_is_logged_not_fatal(self):
        self.wired_eud()
        (self.root / 'bin' / 'manet_ap_mesh.py').unlink()
        calls = self.dispatch()
        self.assertFalse([c for c in calls if 'hostapd' in c and 'start' in c])

    def test_empty_helper_failure_reports_exit_status(self):
        """Do not turn an unreported failure into a guessed lock diagnosis."""
        _, failed = self.helper_log('1|')
        self.assertIn('helper exited 1 without a diagnostic', failed)
        self.assertNotIn('Radio transition busy', failed)



class ProvisioningUplinkTests(DispatchHarness):
    """Bootstrap qualified DNS without starting unfinished radio services."""

    def setUp(self):
        """Connect end0 and publish DNS through the actual selection helper."""
        super().setUp()
        self.iface('end0')
        (self.root / 'addr/end0').write_text('192.168.1.20')
        (self.root / 'gw/end0').write_text('192.168.1.1')
        (self.root / 'internet/end0').touch()
        (self.root / 'net/end0/ifindex').write_text('2')
        (self.root / 'sys/class').mkdir(parents=True)
        (self.root / 'sys/class/net').symlink_to(self.root / 'net')
        leases = self.root / 'run/systemd/netif/leases'
        leases.mkdir(parents=True)
        (leases / '2').write_text('ADDRESS=192.168.1.20\nDNS=192.168.1.1\n')
        self.dns = (self.root /
                    'run/systemd/resolved.conf.d/80-manet-uplink.conf')
        systemctl = self.root / 'bin/systemctl'
        systemctl.write_text(STUB.replace('  systemctl)\n', '''  systemctl)
    if [ "$*" = 'start manet-uplink-dns.service' ]; then
      exec "$TEST_PYTHON" "$TEST_DNS" --root "$T" reconcile
    fi
'''))
        self.env = {'TEST_PYTHON': sys.executable,
                    'TEST_DNS': str(TOOLS / 'manet-uplink-dns.py')}

    def assert_no_radio_actions(self, calls):
        """NAT and DNS may change, but radio and manager startup must wait."""
        self.assertFalse(any('hostapd' in c or 'ap-mesh-helper' in c or
                             'mesh-ip-manager' in c or 'radvd' in c or
                             'gateway-route-manager' in c for c in calls))
        self.assertNotIn('uplink-speed measure end0', calls)
        self.assertNotIn('uplink-speed announce end0', calls)

    def test_restricted_dns_bootstrap_then_normal_reconcile_finishes(self):
        """Candidate lookup qualifies HTTP before DHCP DNS becomes global."""
        curl = self.root / 'bin/curl'
        curl.write_text(STUB.replace('  curl)\n', '''  curl)
    if [[ "$*" != *--resolve* ]] &&
        [ ! -f "$T/run/systemd/resolved.conf.d/80-manet-uplink.conf" ]; then
      exit 6
    fi
'''))
        lookup = self.root / 'bin/candidate-dns'
        lookup.write_text('''#!/bin/bash
echo "candidate-dns $*" >> "$REVIEW_ROOT/calls"
printf '%s\\n' 198.51.100.10
''')
        lookup.chmod(0o755)
        self.env['MANET_UPLINK_DNS'] = str(lookup)
        calls = self.dispatch('provision', extra_env=self.env)
        self.assertIn('DNS=192.168.1.1\n', self.dns.read_text())
        lookup_index = next(i for i, c in enumerate(calls)
                            if c.startswith('candidate-dns resolve end0'))
        resolved_probe = next(i for i, c in enumerate(calls)
                              if c.startswith('curl ') and '--resolve' in c)
        publish = calls.index('systemctl start manet-uplink-dns.service')
        self.assertLess(lookup_index, resolved_probe)
        self.assertLess(resolved_probe, publish)
        self.assertIn('--interface end0', calls[resolved_probe])
        self.assertIn('nft add rule ip nat postrouting '
                      'oifname end0 masquerade', calls)
        self.assertIn('metric 100', '\n'.join(calls))
        self.assertIn('UPLINK_MODE=provisioning',
                      (self.root / 'run/manet-uplink.env').read_text())
        self.assert_no_radio_actions(calls)
        self.calls.unlink()
        calls = self.dispatch(extra_env=self.env)
        self.assertIn('systemctl start hostapd.service', calls)
        self.assertIn('systemctl restart gateway-route-manager.service', calls)
        self.assertIn('uplink-speed measure end0', calls)
        self.assertIn('UPLINK_MODE=gateway',
                      (self.root / 'run/manet-uplink.env').read_text())

    def test_setup_keeps_full_promotion_without_repeating_radio_actions(self):
        """A setup retry repairs DNS even when the runtime promoted already."""
        self.dispatch(extra_env=self.env)
        self.calls.unlink()
        calls = self.dispatch('provision', extra_env=self.env)
        self.assert_no_radio_actions(calls)
        self.assertNotIn('nft flush chain inet filter input', calls)
        self.assertIn('systemctl start manet-uplink-dns.service', calls)
        self.assertIn('UPLINK_MODE=gateway',
                      (self.root / 'run/manet-uplink.env').read_text())

    def test_setup_ignores_unfinished_ap_roles_in_every_eud_mode(self):
        """Rename boots may retain live or pinned AP names without configs."""
        for mode in ('auto', 'wired', 'wireless'):
            for ap in ('wlan1', 'wlx_pending_rename', ''):
                with self.subTest(mode=mode, ap=ap):
                    self.mode(mode)
                    (self.root / 'ap_interface').write_text(ap)
                    calls = self.dispatch('provision', extra_env=self.env)
                    self.assert_no_radio_actions(calls)

    def test_setup_failure_withdraws_dns_without_starting_radios(self):
        """Portal or dead uplink revokes an earlier setup qualification."""
        for code in ('200', '302', ''):
            with self.subTest(code=code):
                (self.root / 'http-code/end0').unlink(missing_ok=True)
                self.dispatch('provision', extra_env=self.env)
                self.calls.unlink()
                (self.root / 'http-code/end0').write_text(code)
                calls = self.dispatch('provision', extra_env=self.env)
                self.assert_no_radio_actions(calls)
                marker = self.root / 'run/mesh-gateway.state'
                self.assertFalse(marker.exists())
                self.assertNotIn('DNS=192.168.1.1', self.dns.read_text())

    def test_setup_does_not_probe_a_wired_eud(self):
        """A connected EUD must keep its bridge and never supply setup DNS."""
        (self.root / 'net/end0/master').symlink_to('../br0')
        self.eth_state('WIRED_EUD')
        calls = self.dispatch('provision', extra_env=self.env)
        self.assert_eud_port_untouched(calls)
        self.assert_no_radio_actions(calls)
        self.assertFalse(self.dns.exists())

    def test_setup_waits_for_active_dispatch_or_ethernet_detection(self):
        """Unlike runtime events, setup waits for the serialized owner."""
        for path in (self.eth_lock,
                     self.root / 'run/manet-uplink-dispatch.lock'):
            with self.subTest(lock=path.name), path.open('w') as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                timer = threading.Timer(0.5, fcntl.flock,
                                        args=(held, fcntl.LOCK_UN))
                timer.start()
                try:
                    self.dispatch('provision', extra_env=self.env)
                finally:
                    timer.join()
                self.assertIn('DNS=192.168.1.1\n', self.dns.read_text())
                self.dns.unlink()


class SpeedGateTests(DispatchHarness):
    def uplink(self, name, usb=False):
        self.iface(name, usb=usb)
        (self.root / 'addr' / name).write_text('192.168.1.20')
        (self.root / 'gw' / name).write_text('192.168.1.1')
        (self.root / 'internet' / name).touch()

    def test_ethernet_gateway_announces_its_measured_speed(self):
        self.uplink('end0')
        calls = self.dispatch()
        self.assertIn('uplink-speed measure end0', calls)
        self.assertIn('uplink-speed announce end0', calls)
        self.assertNotIn('batctl gw_mode server', calls)
        self.assertEqual((self.root / 'run' / 'upstream_iface').read_text().strip(), 'end0')

    def test_unknown_speed_is_promoted_and_retried_without_duplicate_logs(self):
        """TLS failure and its cooldown do not demote a connected Ethernet."""
        self.uplink('end0')
        for rc in ('1', '3', '0'):
            (self.root / 'speed-rc' / 'end0').write_text(rc)
            calls = self.dispatch()
            self.assertTrue((self.root / 'run/mesh-gateway.state').exists())
            self.assertIn('uplink-speed announce end0', calls)
        journal = (self.root / 'journal').read_text()
        self.assertEqual(journal.count('Promoted end0'), 1)
        self.assertNotIn('Demoted', journal)

    def test_captive_portal_cannot_qualify_with_icmp(self):
        """An ICMP reply and HTTP 200/302 are insufficient for Ethernet."""
        self.uplink('end0')
        for code in ('200', '302'):
            (self.root / 'http-code/end0').write_text(code)
            calls = self.dispatch()
            self.assertFalse((self.root / 'run/mesh-gateway.state').exists())
            self.assertNotIn('uplink-speed measure end0', calls)
            self.assertFalse(any(c.startswith('ping ') for c in calls))
            probes = [c for c in calls if c.startswith('curl ')]
            self.assertTrue(probes)
            self.assertTrue(all('--interface end0' in c for c in probes))

    def test_demotion_withdraws_direct_route_and_restarts_mesh_selection(self):
        """Losing the 204 path withdraws metric 100 and restores a client."""
        self.uplink('end0')
        self.dispatch()
        (self.root / 'internet/end0').unlink()
        self.calls.unlink()
        calls = self.dispatch()
        self.assertFalse((self.root / 'run/mesh-gateway.state').exists())
        self.assertIn('ip route del default dev end0 metric 100', calls)
        self.assertIn('systemctl restart gateway-route-manager.service', calls)
        self.assertIn('batctl gw_mode client', calls)
        self.dispatch()
        journal = (self.root / 'journal').read_text()
        self.assertEqual(journal.count('Demoted MANET gateway'), 1)

    def test_metered_uplink_is_promoted_untested(self):
        self.uplink('usb0', usb=True)
        calls = self.dispatch()
        self.assertIn('uplink-speed announce usb0', calls)
        self.assertEqual((self.root / 'run' / 'upstream_iface').read_text().strip(), 'usb0')


class AcsAutoModeTests(SpeedGateTests):
    """With ACS in auto mode, only a gateway serves the AP."""
    def setUp(self):
        super().setUp()
        with (self.root / 'mesh.conf').open('a') as conf:
            conf.write('acs=y\n')

    def test_gateway_serves_the_ap(self):
        self.uplink('end0')
        calls = self.dispatch()
        self.assertTrue((self.root / 'run' / 'mesh-gateway.state').exists())
        self.assertIn('systemctl start hostapd.service', calls)
        self.assertNotIn('ap-mesh-helper mesh', calls)

    def test_node_without_uplink_returns_ap_radio_to_mesh(self):
        self.iface('end0', carrier=False)
        calls = self.dispatch()
        self.assertIn('ap-mesh-helper mesh', calls)
        self.assertFalse([c for c in calls if 'hostapd' in c and 'start' in c])

    def test_losing_the_uplink_returns_ap_radio_to_mesh(self):
        self.uplink('end0')
        self.dispatch()
        (self.root / 'internet' / 'end0').unlink()
        (self.root / 'speed-rc' / 'end0').write_text('1')
        self.calls.unlink()
        calls = self.dispatch()
        self.assertFalse((self.root / 'run' / 'mesh-gateway.state').exists())
        self.assertIn('ap-mesh-helper mesh', calls)

    def test_gateway_with_wired_eud_keeps_radio_in_mesh(self):
        self.uplink('usb0', usb=True)
        self.iface('end0', bridged=True)
        self.eth_state('WIRED_EUD')
        calls = self.dispatch()
        self.assertIn('ap-mesh-helper mesh', calls)
        self.assertFalse([c for c in calls if 'hostapd' in c and 'start' in c])

    def test_static_channels_keep_the_ap_without_uplink(self):
        self.mode('auto')
        self.iface('end0', carrier=False)
        calls = self.dispatch()
        self.assertIn('systemctl start hostapd.service', calls)


for _name in [n for n in dir(SpeedGateTests) if n.startswith('test_')]:
    setattr(AcsAutoModeTests, _name, None)
del _name


if __name__ == '__main__':
    unittest.main()
