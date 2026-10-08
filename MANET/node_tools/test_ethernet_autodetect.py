#!/usr/bin/env python3
"""ethernet-autodetect.sh re-detection decisions, run whole in a fake root.

Bridging, flushing or reconfiguring end0 makes networkd report "Gained
carrier", and networkd-dispatcher runs the detector again. Only a real link
transition (the kernel's carrier_changes counter) may restart detection.
"""
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import unittest


TOOLS = Path(__file__).resolve().parent

IP_STUB = r'''#!/bin/bash
T="$REVIEW_ROOT"
echo "ip $*" >> "$T/calls"
case "$*" in
  "-4 addr show dev end0") [ -f "$T/addr" ] && echo "    inet $(cat "$T/addr")/24 brd x scope global end0" ;;
  "route show dev end0") [ -f "$T/route" ] && echo "default via 192.168.69.1 proto dhcp" ;;
  "link show end0") if [ -L "$T/sys/class/net/end0/master" ]; then echo "2: end0: <UP> master br0"; else echo "2: end0: <UP>"; fi ;;
  "link set end0 nomaster") rm -f "$T/sys/class/net/end0/master" ;;
  "link set end0 master br0") ln -sfn ../br0 "$T/sys/class/net/end0/master" ;;
esac
exit 0
'''


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


class AutodetectHarness(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        for directory in ('bin', 'var/run', 'var/log', 'var/lib', 'etc/systemd/network',
                          'etc/networkd-dispatcher/off.d', 'sys/class/net/end0',
                          'sys/class/net/br0', 'usr/local/bin'):
            (self.root / directory).mkdir(parents=True)
        self.stub('ip', IP_STUB)
        for name in ('networkctl', 'systemctl', 'batctl', 'nft', 'iw', 'sysctl', 'ping', 'curl',
                     'nc', 'sleep'):
            self.stub(name, '#!/bin/bash\necho "%s $*" >> "$REVIEW_ROOT/calls"\n'
                            '[ "$1" = is-active ] && exit 3\n'
                            '[ "$(basename "$0")" = ping ] && exit 1\nexit 0\n' % name)
        self.stub('systemd-cat', '#!/bin/bash\ncat >> "$REVIEW_ROOT/journal"\n')
        # Passive capture: replay canned frames (tcpdump -e format), if any.
        self.stub('tcpdump', '#!/bin/bash\necho "tcpdump $*" >> "$REVIEW_ROOT/calls"\n'
                  'cat "$REVIEW_ROOT/frames" 2>/dev/null\nexit 0\n')
        self.stub('manet-uplink-speed.sh', SPEED_STUB, where='usr/local/bin')
        (self.root / 'speed-rc').mkdir()
        self.stub('mesh-ip-manager.sh', '#!/bin/bash\necho "mesh-ip-manager.sh $*" >> "$REVIEW_ROOT/calls"\n',
                  where='usr/local/bin')
        # Invoked as `python3 manet_ap_mesh.py mesh`, so the stub is Python.
        self.stub('manet_ap_mesh.py', 'import os, sys\nopen(os.environ["REVIEW_ROOT"] + "/calls", "a")'
                  '.write("manet_ap_mesh.py " + " ".join(sys.argv[1:]) + "\\n")\n',
                  where='usr/local/bin')
        # The detector's nested unplug cleanup: record that it ran.
        self.stub('50-gateway-disable', '#!/bin/bash\necho "off-hook" >> "$REVIEW_ROOT/calls"\n',
                  where='etc/networkd-dispatcher/off.d')
        (self.root / 'etc/mesh.conf').write_text('eud=auto\n')
        (self.root / 'var/lib/ap_interface').write_text('wlan1\n')
        (self.root / 'etc/radvd-mesh.conf').write_text('mesh\n')
        self.link(carrier=1, changes=3)

    def stub(self, name, body, where='bin'):
        path = self.root / where / name
        path.write_text(body)
        path.chmod(0o755)

    def link(self, carrier=1, changes=3, bridged=False):
        end0 = self.root / 'sys/class/net/end0'
        (end0 / 'carrier').write_text(f'{carrier}\n')
        (end0 / 'carrier_changes').write_text(f'{changes}\n')
        (end0 / 'address').write_text(OWN + '\n')
        master = end0 / 'master'
        if bridged and not master.is_symlink():
            master.symlink_to('../br0')
        if not bridged and master.is_symlink():
            master.unlink()

    def wired_eud_on_record(self, generation):
        self.link(carrier=1, changes=3, bridged=True)
        (self.root / 'var/run/ethernet_detection_state').write_text(
            'ETH_MODE=WIRED_EUD\nETH_BRIDGE=br0\n')
        (self.root / 'var/run/eth-carrier-generation').write_text(f'end0 wired-eud {generation}\n')

    def run_detector(self):
        source = (TOOLS / 'ethernet-autodetect.sh').read_text()
        source = re.sub(r'/(?:etc|var|sys|usr/local/bin|root)/',
                        lambda m: str(self.root) + m[0], source)
        script = self.root / 'detector.sh'
        script.write_text(source)
        env = dict(os.environ, REVIEW_ROOT=str(self.root),
                   PATH=os.pathsep.join((str(self.root / 'bin'), os.environ['PATH'])))
        result = subprocess.run(['bash', str(script), '--hotplug'], env=env,
                                capture_output=True, text=True, timeout=60)
        calls = (self.root / 'calls').read_text().splitlines() if (self.root / 'calls').exists() else []
        journal = (self.root / 'journal').read_text() if (self.root / 'journal').exists() else ''
        return result, calls, journal

    def generation_record(self):
        path = self.root / 'var/run/eth-carrier-generation'
        return path.read_text().split() if path.exists() else None


class WiredEudTests(AutodetectHarness):
    def test_same_link_event_does_not_detach_wired_eud(self):
        # A reboot loop: each run's own reconfigure
        # re-triggered the detector. Same carrier generation -> no-op.
        self.wired_eud_on_record(3)
        result, calls, journal = self.run_detector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Existing wired-EUD state is current', journal)
        self.assertNotIn('ip link set end0 nomaster', calls)
        self.assertNotIn('ip addr flush dev end0', calls)
        self.assertTrue((self.root / 'sys/class/net/end0/master').is_symlink())

    def test_real_link_change_redetects_and_records_new_generation(self):
        # Cable pulled and replugged (or the EUD power-cycled) between events,
        # even if the unplug event itself was coalesced away.
        self.wired_eud_on_record(3)
        self.link(carrier=1, changes=5, bridged=True)
        result, calls, journal = self.run_detector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('detecting role', journal)
        self.assertIn('ip link set end0 nomaster', calls)
        self.assertIn('Wired EUD configuration complete', journal)
        self.assertEqual(self.generation_record(), ['end0', 'wired-eud', '5'])
        self.assertIn('manet_ap_mesh.py mesh', calls)

    def test_record_without_bridge_membership_redetects(self):
        self.wired_eud_on_record(3)
        self.link(carrier=1, changes=3, bridged=False)
        _, calls, journal = self.run_detector()
        self.assertIn('detecting role', journal)
        self.assertNotIn('Existing wired-EUD state is current', journal)

    def test_first_detection_records_generation(self):
        result, _, journal = self.run_detector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Wired EUD configuration complete', journal)
        self.assertEqual(self.generation_record(), ['end0', 'wired-eud', '3'])
        # A second event on the same link is then a no-op: the loop is broken.
        (self.root / 'calls').unlink()
        _, calls, journal = self.run_detector()
        self.assertIn('Existing wired-EUD state is current', journal)
        self.assertNotIn('ip link set end0 nomaster', calls)

    def test_link_change_during_detection_is_not_absorbed(self):
        # The cable is swapped while the 20 s DHCP probe runs.
        # The decision is recorded against the generation seen at the start,
        # so the event queued by the swap still re-detects.
        counter = self.root / 'sys/class/net/end0/carrier_changes'
        self.stub('networkctl', '#!/bin/bash\necho "networkctl $*" >> "$REVIEW_ROOT/calls"\n'
                  '[ "$1" = reconfigure ] && echo 7 > "%s"\nexit 0\n' % counter)
        result, _, journal = self.run_detector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.generation_record(), ['end0', 'wired-eud', '3'])
        self.assertIn('Link changed on end0 during detection', journal)
        (self.root / 'calls').unlink()
        _, calls, journal = self.run_detector()
        self.assertNotIn('Existing wired-EUD state is current', journal)
        self.assertIn('ip link set end0 nomaster', calls)

    def test_unplug_forgets_the_generation(self):
        self.wired_eud_on_record(3)
        self.link(carrier=0, changes=4, bridged=True)
        result, calls, _ = self.run_detector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('off-hook', calls)
        self.assertIsNone(self.generation_record())

    def test_missing_counter_falls_back_to_detection(self):
        self.wired_eud_on_record(3)
        (self.root / 'sys/class/net/end0/carrier_changes').unlink()
        _, _, journal = self.run_detector()
        self.assertIn('detecting role', journal)


OWN = '88:a2:9e:32:ce:6a'
LAPTOP = '54:b2:03:96:50:d5'
ROUTER = '54:b2:03:ef:8f:93'


def frame(src, dst, rest):
    return f'19:28:39.250424 {src} > {dst}, {rest}\n'


DHCP_REQUEST = frame(LAPTOP, 'ff:ff:ff:ff:ff:ff', 'ethertype IPv4 (0x0800), length 342: '
                     '0.0.0.0.68 > 255.255.255.255.67: BOOTP/DHCP, Request from 54:b2:03:96:50:d5, length 300')
LAPTOP_MDNS = frame(LAPTOP, '01:00:5e:00:00:fb', 'ethertype IPv4 (0x0800), length 80: '
                    '169.254.3.4.5353 > 224.0.0.251.5353: 0 PTR (QM)?')


class LinkEvidenceTests(AutodetectHarness):
    """Decide what the cable leads to from what the far end sends."""

    def frames(self, *lines):
        (self.root / 'frames').write_text(''.join(lines))

    def assert_not_bridged(self, journal, reason):
        self.assertIn(f'a network is attached ({reason}) but gave no DHCP lease', journal)
        self.assertFalse((self.root / 'sys/class/net/end0/master').is_symlink())
        self.assertEqual(self.generation_record(), ['end0', 'network', '3'])
        self.assertNotIn('Wired EUD configuration complete', journal)

    def test_lone_device_asking_for_an_address_is_decided_early(self):
        self.frames(DHCP_REQUEST, LAPTOP_MDNS)
        result, calls, journal = self.run_detector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Wired EUD detected on end0 after 5 s (eud)', journal)
        self.assertIn('Wired EUD configuration complete', journal)
        self.assertEqual(calls.count('sleep 1'), 5)

    def test_router_advertisement_means_a_network(self):
        self.frames(DHCP_REQUEST, frame(ROUTER, '33:33:00:00:00:01',
                    'ethertype IPv6 (0x86dd), length 118: fe80::1 > ff02::1: ICMP6, router advertisement, length 64'))
        _, _, journal = self.run_detector()
        self.assert_not_bridged(journal, 'router-advertisement')

    def test_switch_protocol_frames_mean_a_network(self):
        for dst, rest in (('01:80:c2:00:00:00', '802.3, length 39: LLC, dsap STP (0x42) Individual, ssap STP (0x42) Command, ctrl 0x03: STP 802.1d, Config'),
                          ('01:80:c2:00:00:0e', 'ethertype LLDP (0x88cc), length 60: LLDP, length 46'),
                          ('01:00:0c:cc:cc:cc', '802.3, length 60: LLC, dsap SNAP (0xaa) Individual, ssap SNAP (0xaa) Command, ctrl 0x03: oui Cisco (0x00000c), pid CDP (0x2000): CDPv2, ttl: 180s')):
            with self.subTest(dst=dst):
                (self.root / 'var/run/eth-carrier-generation').unlink(missing_ok=True)
                (self.root / 'journal').unlink(missing_ok=True)
                self.frames(DHCP_REQUEST, frame('00:11:22:33:44:55', dst, rest))
                _, _, journal = self.run_detector()
                self.assert_not_bridged(journal, 'switch-protocol')

    def test_several_devices_mean_a_network(self):
        # A static-addressed LAN: no DHCP anywhere, but more than one host.
        self.frames(frame(LAPTOP, 'ff:ff:ff:ff:ff:ff', 'ethertype ARP (0x0806), length 60: Request who-has 192.168.1.1'),
                    frame(ROUTER, 'ff:ff:ff:ff:ff:ff', 'ethertype ARP (0x0806), length 60: Request who-has 192.168.1.9'))
        _, _, journal = self.run_detector()
        self.assert_not_bridged(journal, 'multiple-devices')

    def test_late_dhcp_server_reply_means_a_network(self):
        self.frames(frame(ROUTER, 'ff:ff:ff:ff:ff:ff', 'ethertype IPv4 (0x0800), length 342: '
                          '192.168.1.1.67 > 255.255.255.255.68: BOOTP/DHCP, Reply, length 300'))
        _, _, journal = self.run_detector()
        self.assert_not_bridged(journal, 'dhcp-server')

    def test_own_frames_are_not_a_second_device(self):
        self.frames(DHCP_REQUEST, frame(OWN, 'ff:ff:ff:ff:ff:ff', 'ethertype ARP (0x0806), length 60: Request'))
        _, _, journal = self.run_detector()
        self.assertIn('Wired EUD detected on end0 after 5 s (eud)', journal)

    def test_silent_link_keeps_the_old_20_second_rule(self):
        _, calls, journal = self.run_detector()
        self.assertIn('Wired EUD detected on end0 after 20 s (unknown)', journal)
        self.assertEqual(calls.count('sleep 1'), 20)

    def test_lone_device_not_asking_for_dhcp_waits_full_time(self):
        self.frames(LAPTOP_MDNS)
        _, _, journal = self.run_detector()
        self.assertIn('Wired EUD detected on end0 after 20 s (unknown)', journal)

    def test_network_decision_is_not_reprobed_on_the_same_link(self):
        self.frames(frame(ROUTER, '01:80:c2:00:00:00', '802.3, length 39: LLC, dsap STP (0x42): STP 802.1d'))
        self.run_detector()
        (self.root / 'calls').unlink()
        _, calls, journal = self.run_detector()
        self.assertIn('Network without DHCP already detected', journal)
        self.assertNotIn('ip link set end0 nomaster', calls)
        # A real link change probes again.
        self.link(carrier=1, changes=5)
        _, _, journal = self.run_detector()
        self.assertIn('detecting role', journal.split('Network without DHCP already detected')[-1])

    def test_lease_still_means_uplink_without_waiting(self):
        (self.root / 'addr').write_text('192.168.69.51')
        self.frames(DHCP_REQUEST)
        _, calls, journal = self.run_detector()
        self.assertIn('IP acquired on end0: 192.168.69.51', journal)
        self.assertEqual(calls.count('sleep 1'), 0)

    def test_capture_failure_falls_back_to_old_rule(self):
        # No evidence (tcpdump missing or failing) must never block a real EUD.
        self.stub('tcpdump', '#!/bin/bash\nexit 1\n')
        _, _, journal = self.run_detector()
        self.assertIn('after 20 s (unknown)', journal)
        self.assertIn('Wired EUD configuration complete', journal)


class GatewayShortcutTests(AutodetectHarness):
    def healthy_gateway(self):
        (self.root / 'var/run/mesh-gateway.state').touch()
        (self.root / 'addr').write_text('192.168.69.51')
        (self.root / 'route').touch()

    def test_gateway_decided_on_this_link_is_kept(self):
        self.healthy_gateway()
        (self.root / 'var/run/eth-carrier-generation').write_text('end0 gateway 3\n')
        _, calls, journal = self.run_detector()
        self.assertIn('Existing gateway state is healthy', journal)
        self.assertNotIn('ip link set end0 nomaster', calls)

    def test_fast_swap_from_eud_to_router_is_not_mistaken_for_healthy(self):
        # Stale lease/route from before, but the recorded decision belongs to a
        # different link generation: detection must run.
        self.healthy_gateway()
        (self.root / 'var/run/eth-carrier-generation').write_text('end0 gateway 3\n')
        self.link(carrier=1, changes=5)
        _, calls, journal = self.run_detector()
        self.assertNotIn('Existing gateway state is healthy', journal)
        self.assertIn('detecting role', journal)

    def test_dispatcher_promoted_gateway_without_record_keeps_old_behavior(self):
        self.healthy_gateway()
        _, _, journal = self.run_detector()
        self.assertIn('Existing gateway state is healthy', journal)


class CaptureBoundTests(AutodetectHarness):
    def test_cancelled_detector_stops_capture_and_removes_its_file(self):
        self.stub('tcpdump', '#!/bin/bash\necho $$ > "$REVIEW_ROOT/capture-pid"\n'
                  'exec /bin/sleep 30\n')
        source = (TOOLS / 'ethernet-autodetect.sh').read_text()
        capture = source[source.index('LINK_CAPTURE=""'):source.index('\ndetect_hotplug_mode()')]
        capture = capture.replace('/var/run/', str(self.root / 'var/run') + '/')
        script = 'exec 200>"$REVIEW_ROOT/lock"\nETH_IFACE=end0\n' + capture
        script += '\nstart_link_capture\necho READY\nwhile :; do /bin/sleep .1; done\n'
        env = dict(os.environ, REVIEW_ROOT=str(self.root),
                   PATH=str(self.root / 'bin') + os.pathsep + os.environ['PATH'])
        process = subprocess.Popen(['bash', '-c', script], env=env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'READY')
            deadline = time.monotonic() + 3
            pid_file = self.root / 'capture-pid'
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            child = int(pid_file.read_text())
            process.terminate()
            process.communicate(timeout=5)
            self.assertEqual(process.returncode, 143)
            self.assertEqual(list((self.root / 'var/run').glob('eth-detect-capture.*')), [])
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)

    def test_capture_limit_refuses_bridge_and_removes_capture(self):
        (self.root / 'frames').write_text(DHCP_REQUEST + 'X' * (1536 * 1024))
        result, _, journal = self.run_detector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('network is attached (capture-limit)', journal)
        self.assertFalse((self.root / 'sys/class/net/end0/master').is_symlink())
        self.assertEqual(list((self.root / 'var/run').glob('eth-detect-capture.*')), [])

    def test_capture_child_does_not_hold_detector_lock(self):
        self.stub('tcpdump', '#!/bin/bash\n'
                  'if [ -e /proc/$$/fd/200 ]; then echo inherited > "$REVIEW_ROOT/child-lock"; '
                  'else echo closed > "$REVIEW_ROOT/child-lock"; fi\nexit 0\n')
        result, _, _ = self.run_detector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'child-lock').read_text().strip(), 'closed')
        self.assertEqual(list((self.root / 'var/run').glob('eth-detect-capture.*')), [])



class SpeedGateTests(AutodetectHarness):
    def leased_with_internet(self):
        (self.root / 'addr').write_text('192.168.69.51')
        (self.root / 'route').touch()
        self.stub('ping', '#!/bin/bash\necho "ping $*" >> "$REVIEW_ROOT/calls"\nexit 0\n')

    def test_internet_and_speed_test_make_a_gateway(self):
        self.leased_with_internet()
        _, calls, journal = self.run_detector()
        self.assertIn('uplink-speed measure end0', calls)
        self.assertIn('Configuring as gateway/uplink', journal)
        self.assertIn('uplink-speed announce end0', calls)

    def test_failed_speed_test_is_not_a_gateway(self):
        self.leased_with_internet()
        (self.root / 'speed-rc' / 'end0').write_text('1')
        _, calls, journal = self.run_detector()
        self.assertIn('uplink-speed measure end0', calls)
        self.assertIn('internet test failed; leaving as mesh client', journal)
        self.assertNotIn('Configuring as gateway/uplink', journal)
        self.assertNotIn('uplink-speed announce end0', calls)


if __name__ == '__main__':
    unittest.main()


class NoInternetCacheTests(AutodetectHarness):
    """The no-internet cache runs on the boot clock, not the wall clock."""

    def detect_with_cache(self, stamp, uptime=5000):
        (self.root / 'addr').write_text('192.168.69.5')
        (self.root / 'var/run/eth-no-internet.state').write_text(f'end0 192.168.69.5 {stamp}\n')
        clock = self.root / 'uptime'
        clock.write_text(f'{uptime}.25 0.00\n')
        old = os.environ.get('MESH_UPTIME_FILE')
        os.environ['MESH_UPTIME_FILE'] = str(clock)
        try:
            return self.run_detector()
        finally:
            if old is None:
                os.environ.pop('MESH_UPTIME_FILE')
            else:
                os.environ['MESH_UPTIME_FILE'] = old

    def test_recent_no_internet_verdict_skips_redetection(self):
        _, _, journal = self.detect_with_cache(4990)
        self.assertIn('No-internet state is current', journal)

    def test_expired_verdict_redetects(self):
        _, _, journal = self.detect_with_cache(4000)
        self.assertNotIn('No-internet state is current', journal)

    def test_wall_time_from_an_older_version_counts_as_expired(self):
        _, _, journal = self.detect_with_cache(1791288000)
        self.assertNotIn('No-internet state is current', journal)
