#!/usr/bin/env python3
"""Exercise cached Ethernet estimates, partial transfers and unknown scores."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent
SCRIPT = TOOLS / 'manet-uplink-speed.sh'

# curl: each URL's reply comes from $T/curl/<cloudflare|ovh> ("code size start
# total"); a missing file is a failed transfer.
STUB = r'''#!/bin/bash
T="$TEST_ROOT"
echo "$(basename "$0") $*" >> "$T/calls"
case "$(basename "$0")" in
  systemd-cat) exit 1 ;;  # Exercise the stderr fallback without a host journal.
  curl)
    case "$*" in *cloudflare*) f="$T/curl/cloudflare" ;; *) f="$T/curl/ovh" ;; esac
    [ -f "$f" ] || exit 28
    printf '%s' "$(cat "$f")"
    [ -f "$f.rc" ] && exit "$(cat "$f.rc")" ;;
  ip)
    case "$*" in
      "-4 -o addr show dev "*) echo "2: ${@: -1} inet $(cat "$T/addr")/24 brd x scope global" ;;
      "-4 route show default dev "*) echo "default via $(cat "$T/router") dev ${@: -1}" ;;
    esac ;;
esac
exit 0
'''


class UplinkSpeedTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        for name in ('bin', 'run', 'net', 'curl', 'drivers'):
            (self.root / name).mkdir()
        for tool in ('curl', 'ip', 'batctl', 'systemd-cat'):
            (self.root / 'bin' / tool).write_text(STUB)
            (self.root / 'bin' / tool).chmod(0o755)
        (self.root / 'addr').write_text('192.168.1.20')
        (self.root / 'router').write_text('192.168.1.1')
        self.uptime = self.root / 'uptime'
        self.uptime.write_text('1000.00 0.00\n')
        self.nic('end0', 'bcmgenet')
        self.env = dict(os.environ, TEST_ROOT=str(self.root),
                        PATH=f'{self.root / "bin"}:{os.environ["PATH"]}',
                        MANET_RUN_DIR=str(self.root / 'run'), MANET_SYS_NET=str(self.root / 'net'),
                        MANET_UPTIME_FILE=str(self.uptime))

    def nic(self, name, driver, wireless=False):
        path = self.root / 'net' / name
        (path / 'device').mkdir(parents=True)
        (path / 'type').write_text('1\n')
        (self.root / 'drivers' / driver).mkdir(exist_ok=True)
        (path / 'device' / 'driver').symlink_to(self.root / 'drivers' / driver)
        if wireless:
            (path / 'wireless').mkdir()

    def reply(self, endpoint, text, rc=0):
        """Supply curl write-out values and its independent exit status."""
        (self.root / 'curl' / endpoint).write_text(text)
        (self.root / 'curl' / (endpoint + ".rc")).write_text(str(rc))

    def run_tool(self, *args):
        (self.root / 'calls').unlink(missing_ok=True)
        result = subprocess.run(['bash', str(SCRIPT), *args], env=self.env,
                                capture_output=True, text=True, timeout=30)
        calls = (self.root / 'calls').read_text().splitlines() if (self.root / 'calls').exists() else []
        return result, calls

    def test_measures_once_and_reuses_the_result_for_the_same_uplink(self):
        # 5 MB in 0.4 s after the first byte: 100 Mbit/s, setup time excluded.
        self.reply('cloudflare', '200 5000000 0.2 0.6')
        result, calls = self.run_tool('measure', 'end0')
        self.assertEqual((result.returncode, result.stdout), (0, '100.0\n'), result.stderr)
        self.assertEqual(len([c for c in calls if c.startswith('curl')]), 1)
        self.assertIn('bytes=5000000', next(c for c in calls
                                          if c.startswith('curl ')))
        result, calls = self.run_tool('measure', 'end0')
        self.assertEqual((result.returncode, result.stdout), (0, '100.0\n'))
        self.assertFalse([c for c in calls if c.startswith('curl')])
        # A new address or router is a new uplink: measured again.
        (self.root / 'router').write_text('10.0.0.1')
        _, calls = self.run_tool('measure', 'end0')
        self.assertEqual(len([c for c in calls if c.startswith('curl')]), 1)

    def test_fallback_endpoint_is_capped_at_5_mb(self):
        self.reply('ovh', '206 5000000 0.5 1.5')
        result, calls = self.run_tool('measure', 'end0')
        self.assertEqual((result.returncode, result.stdout), (0, '40.0\n'), result.stderr)
        self.assertIn('-r 0-4999999', [c for c in calls if 'ovh' in c][0])

    def test_unknown_speed_is_not_retried_immediately(self):
        # Both HTTPS providers fail, independently of connectivity.
        result, calls = self.run_tool('measure', 'end0')
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertEqual(len([c for c in calls if c.startswith('curl')]), 2)
        result, calls = self.run_tool('measure', 'end0')
        self.assertEqual(result.returncode, 3)
        self.assertFalse([c for c in calls if c.startswith('curl')])
        # forget (each pass without an uplink) must not clear the backoff.
        self.run_tool('forget')
        _, calls = self.run_tool('measure', 'end0')
        self.assertFalse([c for c in calls if c.startswith('curl')])
        self.uptime.write_text('1061.00 0.00\n')
        self.reply('cloudflare', '200 5000000 0.2 0.6')
        result, _ = self.run_tool('measure', 'end0')
        self.assertEqual(result.returncode, 0)

    def test_short_or_wrong_replies_are_failures(self):
        for reply in ('200 249999 0.2 0.6', '302 5000000 0.2 0.6',
                      '200 5000000'):
            with self.subTest(reply=reply):
                for f in (self.root / 'run').iterdir():
                    f.unlink()
                self.reply('cloudflare', reply)
                result, _ = self.run_tool('measure', 'end0')
                self.assertEqual(result.returncode, 1)

    def test_deadline_partial_transfer_gives_a_low_cached_score(self):
        """Use transferred bytes at the deadline, even with curl exit 28."""
        self.reply('cloudflare', '200 1000000 1.0 15.0', rc=28)
        result, calls = self.run_tool('measure', 'end0')
        self.assertEqual((result.returncode, result.stdout), (0, '0.6\n'))
        self.assertEqual(sum(c.startswith('curl') for c in calls), 1)
        self.assertIn('--interface end0', next(c for c in calls
                                              if c.startswith('curl ')))
        self.assertEqual(self.run_announce('end0'),
                         ['batctl gw_mode server 600kbit/120kbit'])
        _, calls = self.run_tool('measure', 'end0')
        self.assertFalse(any(c.startswith('curl') for c in calls))

    def test_tls_failure_is_unknown_then_remeasured_after_cooldown(self):
        """A bad clock yields unknown, never zero speed or a cached success."""
        self.reply('cloudflare', '000 0 0 0.2', rc=60)
        self.reply('ovh', '000 0 0 0.2', rc=60)
        result, _ = self.run_tool('measure', 'end0')
        self.assertEqual(result.returncode, 1)
        self.assertIn('bandwidth unknown', result.stderr)
        self.assertEqual(self.run_announce('end0'),
                         ['batctl gw_mode server 1000kbit/200kbit'])
        self.uptime.write_text('1061.00 0.00\n')
        result, _ = self.run_tool('measure', 'end0')
        self.assertEqual(result.stderr, '')
        self.uptime.write_text('1122.00 0.00\n')
        self.reply('cloudflare', '200 5000000 0.2 1.2')
        result, _ = self.run_tool('measure', 'end0')
        self.assertEqual((result.returncode, result.stdout), (0, '40.0\n'))
        self.assertIn('downloads at 40.0', result.stderr)

    def test_estimate_transitions_reach_journal_once(self):
        """Periodic callers may discard stderr, so log through systemd-cat."""
        (self.root / 'bin/systemd-cat').write_text(
            '#!/bin/bash\ncat >> "$TEST_ROOT/journal"\n')
        self.run_tool('measure', 'end0')
        self.run_tool('measure', 'end0')
        self.uptime.write_text('1061.00 0.00\n')
        self.run_tool('measure', 'end0')
        self.uptime.write_text('1122.00 0.00\n')
        self.reply('cloudflare', '200 5000000 0.2 1.2')
        self.run_tool('measure', 'end0')
        self.run_tool('measure', 'end0')
        lines = (self.root / 'journal').read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn('bandwidth unknown', lines[0])
        self.assertIn('downloads at 40.0', lines[1])

    def test_partial_ovh_and_non_timeout_errors(self):
        """Accept a ranged deadline sample, but not another transfer error."""
        self.reply('ovh', '206 250000 1 15', rc=28)
        result, _ = self.run_tool('measure', 'end0')
        self.assertEqual((result.returncode, result.stdout), (0, '0.1\n'))
        self.run_tool('forget')
        self.reply('ovh', '206 250000 1 15', rc=56)
        result, _ = self.run_tool('measure', 'end0')
        self.assertEqual(result.returncode, 1)

    def test_metered_and_wireless_uplinks_are_never_tested(self):
        self.nic('usb0', 'rndis_host')
        self.nic('enx001122334455', 'cdc_ncm')
        self.nic('wlan3', 'mt7921u', wireless=True)
        self.nic('enx66778899aabb', 'r8152')
        for iface, expected in (('usb0', 2), ('enx001122334455', 2), ('wlan3', 2),
                                ('enx66778899aabb', 1)):
            with self.subTest(iface=iface):
                result, calls = self.run_tool('measure', iface)
                self.assertEqual(result.returncode, expected)
                self.assertEqual(bool([c for c in calls if c.startswith('curl')]), expected == 1)

    def run_announce(self, iface):
        _, calls = self.run_tool('announce', iface)
        return [c for c in calls if c.startswith('batctl')]

    def test_announce_uses_the_measurement_or_the_default(self):
        self.reply('cloudflare', '200 5000000 0.2 0.6')
        self.run_tool('measure', 'end0')
        self.assertEqual(self.run_announce('end0'), ['batctl gw_mode server 100000kbit/20000kbit'])
        # Never a bare "server": that would keep the last measured value.
        for iface in ('usb0', ''):
            self.assertEqual(self.run_announce(iface), ['batctl gw_mode server 10000kbit/2000kbit'])
        self.run_tool('forget')
        self.assertEqual(self.run_announce('end0'),
                         ['batctl gw_mode server 1000kbit/200kbit'])


if __name__ == '__main__':
    unittest.main()
