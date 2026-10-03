#!/usr/bin/env python3
"""manet-uplink-dispatch.sh against a simulated node: wired-EUD protection."""

import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parent

SPEED_STUB = r'''#!/bin/bash
# measure: exit with $T/speed-rc/IFACE if present; metered names exit 2.
T="$REVIEW_ROOT"
echo "uplink-speed $*" >> "$T/calls"
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
  curl|nc) exit 1 ;;
  systemd-cat) cat >> "$T/journal" ;;
esac
exit 0
'''


class DispatchHarness(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        for name in ('bin', 'net', 'run', 'addr', 'gw', 'active', 'internet', 'networkd', 'bus/usb'):
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

    def dispatch(self, event='reconcile', iface=''):
        source = (TOOLS / 'manet-uplink-dispatch.sh').read_text()
        root = str(self.root)
        source = source.replace('/var/run/', '/run/')
        for old, new in (('/sys/class/net', root + '/net'), ('/run/', root + '/run/'),
                         ('/etc/mesh.conf', root + '/mesh.conf'),
                         ('/var/lib/ap_interface', root + '/ap_interface'),
                         ('/etc/systemd/network', root + '/networkd'),
                         ('/etc/radvd-mesh.conf', root + '/radvd-mesh.conf'),
                         ('/etc/radvd-gateway.conf', root + '/radvd-gateway.conf'),
                         ('/etc/radvd.conf', root + '/radvd.conf'),
                         ('/usr/local/bin/mesh-ip-manager.sh', root + '/bin/mesh-ip-manager')):
            source = source.replace(old, new)
        script = self.root / 'dispatch.sh'
        script.write_text(source)
        env = dict(os.environ, REVIEW_ROOT=root,
                   PATH=os.pathsep.join((str(self.root / 'bin'), os.environ['PATH'])),
                   MANET_ETH_DETECT_LOCK=str(self.eth_lock),
                   MANET_AP_MESH_HELPER=str(self.root / 'bin' / 'manet_ap_mesh.py'),
                   MANET_UPLINK_SPEED=str(self.root / 'bin' / 'uplink-speed'))
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

    def test_ethernet_without_the_speed_test_is_not_a_gateway(self):
        # Captive portal or filtered network: the probe passes, the test fails.
        self.uplink('end0')
        (self.root / 'speed-rc' / 'end0').write_text('1')
        calls = self.dispatch()
        self.assertFalse((self.root / 'run' / 'upstream_iface').exists())
        self.assertNotIn('uplink-speed announce end0', calls)
        self.assertIn('batctl gw_mode client', calls)
        self.assertIn('uplink-speed forget', calls)

    def test_a_standing_failure_is_not_logged_again(self):
        self.uplink('end0')
        journal = self.root / 'journal'
        for rc, logged in (('1', True), ('3', False)):
            with self.subTest(rc=rc):
                journal.unlink(missing_ok=True)
                (self.root / 'speed-rc' / 'end0').write_text(rc)
                self.dispatch()
                text = journal.read_text() if journal.exists() else ''
                self.assertEqual('not the speed test' in text, logged)
                self.assertFalse((self.root / 'run' / 'upstream_iface').exists())

    def test_metered_uplink_is_promoted_untested(self):
        self.uplink('usb0', usb=True)
        calls = self.dispatch()
        self.assertIn('uplink-speed announce usb0', calls)
        self.assertEqual((self.root / 'run' / 'upstream_iface').read_text().strip(), 'usb0')


if __name__ == '__main__':
    unittest.main()
