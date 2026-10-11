#!/usr/bin/env python3
"""ACS occupancy regressions and replay of the CM4 election journal."""

from datetime import datetime
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import unittest

from test_acs import AcsHarness, TOOLS


class AcsPublishTests(AcsHarness):
    """Only successful sends record the last telemetry publication time."""

    def test_failed_publish_keeps_timestamp_until_next_success(self):
        self.command('encoder', "print('telemetry')")
        self.command('alfred', '''
sys.stdin.read()
counter = Path(os.environ['TEST_ROOT']) / 'sends'
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
sys.exit(1 if count == 1 else 0)
''')
        (self.root / 'my_ipv4_chunk').write_text('0\n')
        (self.root / 'loadavg').write_text('0.10 0.10 0.10\n')
        source = (TOOLS / 'node-manager-acs.sh').read_text()
        for branch, start, end in (
                ('lobby', '# === PUBLISH STATUS (so other nodes can see us) ===',
                 '# Check discovery after publishing both records'),
                ('data', '# === STAGE 2: PUBLISH (every 3 min at :15) ===',
                 '# === STAGE 3: REGISTRY REFRESH AND IP MANAGEMENT')):
            with self.subTest(branch=branch):
                (self.root / 'sends').unlink(missing_ok=True)
                block = source.split(start, 1)[1].split(end, 1)[0]
                for prefix in ('/var/run/', '/proc/'):
                    block = block.replace(prefix, str(self.root) + '/')
                # Lobby uses its interval; data uses real ACS publish windows.
                ticks = '1005 1020 1185' if branch == 'data' else '1000 1015 1030'
                expected = ['100', '100', '1185'] if branch == 'data' else [
                    '100', '1015', '1015']
                setup = self.definitions('node-manager-acs.sh') + '''
log() { :; }
detect_and_update_gateway_state() { :; }
is_ntp_time_source() { return 1; }
is_hosting_service() { return 1; }
is_hosting_mumble_service() { return 1; }
collect_radio_mcs() { :; }
collect_interfaces_json() { printf '[]'; }
collect_ap_ssid() { :; }
date() { printf '%s\n' "$MONO"; }
ENCODER_PATH="$TEST_ROOT/bin/encoder"
THROUGHPUT_MEAN=true
GPS_STATUS_FILE="$TEST_ROOT/missing-gps"
BOOTSTRAPPING=false
LAST_PUBLISH_TIME=100
''' + 'for MONO in ' + ticks + '; do\nNOW=$MONO\n'
                result = self.shell(setup + block + '''
printf '%s\n' "$LAST_PUBLISH_TIME"
done
''')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), expected)
                self.assertEqual((self.root / 'sends').read_text(), '2')


class OccupancyTests(AcsHarness):
    """Exercise real survey collection and scoring with isolated radios."""

    scorer = TOOLS / 'channel-election.sh'

    def score(self, reports, current=2462, band='2_4'):
        """Score a shared snapshot and return its winner and journal."""
        self.configure(current if band == '2_4' else None,
                       current if band == '5_0' else None)
        (self.root / 'uptime').write_text('10000.0 1.0\n')
        self.reports(reports)
        result = self.run_command(['bash', str(self.scorer), '--score'])
        self.assertEqual(result.returncode, 0, result.stderr)
        output = (self.root / 'election').read_text()
        winner = re.search(rf'WINNER_{band}=(\d+)', output)[1]
        return int(winner), result.stderr, output

    def scans(self, samples, cache='{}', fault=''):
        """Collect scans through the manager's real command substitution."""
        self.configure(2462, None)
        (self.root / 'samples').write_text(json.dumps(samples))
        (self.root / 'survey-count').write_text('0')
        self.command('iw', '''
import json
args = sys.argv[1:]
root = Path(os.environ['TEST_ROOT'])
if args[-2:] == ['survey', 'dump']:
    counter = root / 'survey-count'
    count = int(counter.read_text())
    counter.write_text(str(count + 1))
    sample = json.loads((root / 'samples').read_text())[count // 2]
    for freq, busy in zip((2437, 2462), sample):
        active = 10 if busy is None else 1000
        print('frequency:', freq, 'MHz')
        print('noise: -81 dBm')
        print('channel active time:', active if count % 2 else 0, 'ms')
        print('channel busy time:',
              int((busy or 0) * active / 100) if count % 2 else 0, 'ms')
        print('channel transmit time: 0 ms')
''')
        body = (self.definitions('node-manager-acs.sh') +
                '\nCACHED_SCAN_REPORT_JSON=' + shlex.quote(cache) +
                '\nfault=' + shlex.quote(fault) + '''
phy_usable_freqs() { printf '%s\n' "$2"; }
sleep() { :; }
jq() {
    if [ "$round" -eq 0 ] && [[ "$fault" == *jq* ]]; then
        printf '%s\n' 'forced jq failure' >&2
        return 1
    fi
    command jq "$@"
}
''' + ('' if 'uptime' not in fault else '''
uptime_now() {
    [ "$round" -ne 0 ] || return 1
    printf '%s\n' "$((10000 + round * 180))"
}
''') + '''
for ((round=0; round<''' + str(len(samples)) + '''; round++)); do
    printf '%s 1.0\n' "$((10000 + round * 180))" > "$MESH_UPTIME_FILE"
    CACHED_SCAN_REPORT_JSON=$(perform_scan) || exit
    printf '%s\n' "$CACHED_SCAN_REPORT_JSON" | command jq -c .
done
''')
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_empty_or_invalid_cache_recovers_and_next_scan_publishes(self):
        for cache in ('', '{bad', 'null', '[]'):
            with self.subTest(cache=cache):
                reports = self.scans([(10, 20)] * 3, cache=cache)
                self.assertEqual(len(reports), 3)
                for report in reports:
                    self.assertEqual([r['channel'] for r in report['results']],
                                     [2437, 2462])
                self.assertTrue(all('busy_pct' not in r
                                    for r in reports[0]['results']))
                self.assertEqual([r['busy_pct'] for r in reports[2]['results']],
                                 [10, 20])

    def test_jq_failure_keeps_unknown_report_and_next_scan_publishes(self):
        for fault in ('jq', 'uptime', 'uptime+jq'):
            with self.subTest(fault=fault):
                reports = self.scans([(10, 20)] * 4, fault=fault)
                self.assertEqual(len(reports), 4)
                self.assertEqual(reports[0]['results'], [
                    {'channel': channel, 'noise_floor': -81, 'bss_count': 0,
                     '_survey_iface': 'wlan0'} for channel in (2437, 2462)])
                self.assertEqual(len(reports[1]['results']), 2)
                self.assertEqual([r['busy_pct'] for r in reports[3]['results']],
                                 [10, 20])

    def smooth(self, observations):
        """Run the actual cache filter at supplied boot-clock timestamps."""
        (self.root / 'observations').write_text(''.join(
            json.dumps({'time': now, 'report': report}) + '\n'
            for now, report in observations))
        body = self.definitions('node-manager-acs.sh') + '''
set -e
while IFS= read -r observation; do
    now=$(printf '%s' "$observation" | jq '.time')
    printf '%s 1.0\n' "$now" > "$MESH_UPTIME_FILE"
    CACHED_SCAN_REPORT_JSON=$(printf '%s' "$observation" |
        jq -c '.report' | smooth_scan_report)
    printf '%s\n' "$CACHED_SCAN_REPORT_JSON"
done < "$TEST_ROOT/observations"
'''
        result = subprocess.run(
            ['bash', '-c', body], env=self.env, text=True, capture_output=True,
            timeout=max(20, len(observations) * 2))
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_unmeasured_candidate_does_not_look_idle(self):
        """Missing occupancy cannot beat a measured home by pretending zero."""
        for band, home, other in [('2_4', 2462, 2437),
                                  ('5_0', 5220, 5200)]:
            with self.subTest(band=band):
                reports = [[{'channel': home, 'noise_floor': -81,
                             'busy_pct': 47, 'bss_count': 2},
                            {'channel': other, 'noise_floor': -81,
                             'bss_count': 2}]]
                winner, journal, _ = self.score(reports, home, band)
                self.assertEqual(winner, home)
                self.assertIn('55 1 -81 - 2 0/1', journal)

    def test_quiet_scans_ignore_an_isolated_occupancy_swing(self):
        """A short busy burst does not migrate an otherwise quiet radio."""
        scans = self.scans([(10, 10)] * 3 + [(1, 47), (10, 10)])
        winners = [self.score([scan['results']])[0] for scan in scans]
        self.assertEqual(winners, [2462] * len(scans))
        self.assertEqual(scans[3]['results'][1]['busy_pct'], 10)

    def test_sustained_congestion_moves_within_two_measured_rounds(self):
        """The median rejects a burst but responds to sustained congestion."""
        scans = self.scans([(10, 10)] * 3 + [(10, 95)] * 3)
        winners = [self.score([scan['results']])[0] for scan in scans]
        self.assertEqual(winners, [2462] * 4 + [2437] * 2)

    def test_warmup_and_short_dwell_remain_unknown(self):
        """Require three valid samples and keep an unmeasured scan absent."""
        scans = self.scans([(0, 10)] * 3 + [(None, 10), (0, 10)])
        for index in (0, 1, 3):
            self.assertNotIn('busy_pct', scans[index]['results'][0])
        for index in (2, 4):
            self.assertEqual(scans[index]['results'][0]['busy_pct'], 0)

    def test_measured_zero_still_beats_a_congested_home(self):
        """A measured idle channel remains distinct from an unknown one."""
        reports = [[{'channel': 2462, 'noise_floor': -81, 'busy_pct': 70},
                    {'channel': 2437, 'noise_floor': -81, 'busy_pct': 0}]]
        self.assertEqual(self.score(reports)[0], 2437)

    def test_unknown_fallback_is_independent_of_registry_order(self):
        """All scorers use the shared incumbent's median, not local state."""
        reports = [[{'channel': 2462, 'noise_floor': -81, 'busy_pct': 47},
                    {'channel': 2437, 'noise_floor': -81}],
                   [{'channel': 2462, 'noise_floor': -81, 'busy_pct': 53},
                    {'channel': 2437, 'noise_floor': -81}]]
        first = self.score(reports)
        second = self.score(reports[::-1])
        self.assertEqual(first, second)
        self.assertEqual(first[0], 2462)

    def test_stale_or_different_radio_history_needs_new_measurements(self):
        """Reject expired history, backwards clocks and interface changes."""
        report = {'results': [{'channel': 2462, 'noise_floor': -81,
                               '_survey_iface': 'wlan0', 'busy_pct': 10}]}
        ready = [(1000 + i * 180, report) for i in range(3)]
        for stamp, iface in [(1961, 'wlan0'), (900, 'wlan0'), (1540, 'wlan1')]:
            with self.subTest(stamp=stamp, iface=iface):
                changed = {'results': [dict(report['results'][0],
                                            _survey_iface=iface, busy_pct=90)]}
                scans = self.smooth(ready + [(stamp, changed)])
                self.assertEqual(scans[2]['results'][0]['busy_pct'], 10)
                self.assertNotIn('busy_pct', scans[3]['results'][0])

    def test_partial_missing_reports_do_not_dilute_measured_median(self):
        """One missing reporter does not discount another reporter's data."""
        reports = [[{'channel': 2462, 'noise_floor': -81, 'busy_pct': 60},
                    {'channel': 2437, 'noise_floor': -81}],
                   [{'channel': 2462, 'noise_floor': -81},
                    {'channel': 2437, 'noise_floor': -81, 'busy_pct': 10}]]
        winner, journal, _ = self.score(reports)
        self.assertEqual(winner, 2437)
        self.assertIn('17 2 -81 10 0 0/1', journal)

    def test_unmeasured_incumbent_uses_highest_candidate_median(self):
        """Unknown busy does not become a free score at bootstrap either."""
        reports = [[{'channel': 2437, 'noise_floor': -81},
                    {'channel': 2462, 'noise_floor': -95, 'busy_pct': 50}]]
        winner, journal, _ = self.score(reports, current=2412)
        self.assertEqual(winner, 2462)
        self.assertIn('57 1 -81 - 0 0/1', journal)

    def test_noise_only_reports_can_still_escape_disqualification(self):
        """Noise voting remains usable when all occupancy data is unknown."""
        reports = [[{'channel': 2462, 'noise_floor': -65},
                    {'channel': 2437, 'noise_floor': -90}]]
        self.assertEqual(self.score(reports)[0], 2437)

    def test_unknown_occupancy_cannot_hide_limp_mode(self):
        """An unmeasured fallback inherits the bad home's occupancy score."""
        reports = [[{'channel': 2462, 'noise_floor': -81, 'busy_pct': 95},
                    {'channel': 2437, 'noise_floor': -81}]]
        winner, _, output = self.score(reports)
        self.assertEqual(winner, 2437)
        self.assertIn('LIMP_MODE=true', output)


def journal_rounds(path):
    """Recover single-node scoring snapshots and changes from the journal."""
    rounds = []
    results = []
    home = 2462  # The first winner receives incumbent bias in this capture.
    for line in path.read_text().splitlines():
        match = re.search(
            r'\[2.4GHz\] (\d+): (ok|dq) '
            r'score/nodes/noise/busy/bss/veto = (.*)', line)
        if match:
            score, nodes, noise, busy, bss, veto = match[3].split()
            if nodes != '1':
                raise ValueError('Replay requires single-node score snapshots')
            entry = {'channel': int(match[1]), 'noise_floor': int(noise),
                     'bss_count': float(bss), '_survey_iface': 'wlan0'}
            if busy != '-':
                entry['busy_pct'] = float(busy)
            results.append(entry)
        match = re.search(r'\[2.4GHz\] Winner is (\d+)', line)
        if match:
            stamp = line.split()[0]
            timestamp = int(datetime.fromisoformat(stamp).timestamp())
            rounds.append({'stamp': stamp, 'home': home,
                           'time': timestamp,
                           'winner': int(match[1]),
                           'report': {'results': results}})
            results = []
        match = re.search(r'ACS applied channels (.*)', line)
        if match:
            home = json.loads(match[1])['2.4']
            if home != rounds[-1]['winner']:
                raise ValueError('Applied plan does not match scored winner')
    return rounds


def replay(path, original=None):
    """Replay available snapshots through real smoothing and shell scoring."""
    rounds = journal_rounds(path)
    harness = OccupancyTests()
    harness.setUp()
    try:
        # Shell logging avoids starting Python for each line during the replay.
        (harness.bin / 'systemd-cat').write_text('#!/bin/sh\ncat >&2\n')
        observed = sum(r['winner'] != r['home'] for r in rounds)
        if original:
            harness.scorer = original
            for row in rounds:
                actual = harness.score([row['report']['results']], row['home'])
                harness.assertEqual(actual[0], row['winner'], row['stamp'])
            print(f'Original scorer matches {len(rounds)} recorded winners.')
        harness.scorer = TOOLS / 'channel-election.sh'
        origin = rounds[0]['time']
        filtered = harness.smooth([
            (row['time'] - origin + 10000, row['report']) for row in rounds])
        current = rounds[0]['home']
        changes = []
        for row, scan in zip(rounds, filtered):
            winner = harness.score([scan['results']], current)[0]
            if winner != current:
                changes.append(f"{row['stamp']}: {current} -> {winner}")
                current = winner
        print(f'Recorded changes: {observed}; replay changes: {len(changes)}')
        for change in changes:
            print(change)
        print('Replay preserves frequencies and time gaps; omitted scans and')
        print('home/off-channel measurement changes cannot be reconstructed.')
    finally:
        harness.doCleanups()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--replay':
        original = Path(sys.argv[3]) if len(sys.argv) > 3 else None
        replay(Path(sys.argv[2]), original)
    else:
        unittest.main()
