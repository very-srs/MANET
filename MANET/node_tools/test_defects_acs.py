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

    def test_data_and_bootstrap_publications_expire_each_band_separately(self):
        self.command('encoder', '''
import json
Path(os.environ['TEST_ROOT'], 'encoder-args').write_text(json.dumps(sys.argv[1:]))
print('telemetry')
''')
        self.command('alfred', 'sys.stdin.read()')
        (self.root / 'my_ipv4_chunk').write_text('0\n')
        (self.root / 'loadavg').write_text('0.1 0.1 0.1\n')
        (self.root / 'uptime').write_text('1200.0 1.0\n')
        source = (TOOLS / 'node-manager-acs.sh').read_text()
        for branch, start, end in (
                ('lobby', '# === PUBLISH STATUS (so other nodes can see us) ===',
                 '# Check discovery after publishing both records'),
                ('data', '# === STAGE 2: PUBLISH (every 3 min at :15) ===',
                 '# === STAGE 3: REGISTRY REFRESH AND IP MANAGEMENT')):
            for fresh in (True, False):
                with self.subTest(branch=branch, fresh=fresh):
                    report = {'results': [
                        {'channel': 2437, 'noise_floor': -81, '_survey_boot': 1000},
                        {'channel': 5200, 'noise_floor': -81,
                         '_survey_boot': 1100 if fresh else 1000}]}
                    block = source.split(start, 1)[1].split(end, 1)[0]
                    for prefix in ('/var/run/', '/proc/'):
                        block = block.replace(prefix, str(self.root) + '/')
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
BOOTSTRAPPING=true
NOW=1200
MONO=1200
''' + 'CACHED_SCAN_REPORT_JSON=' + shlex.quote(json.dumps(report)) + '\n'
                    result = self.shell(setup + block)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    args = json.loads((self.root / 'encoder-args').read_text())
                    sent = json.loads(args[args.index('--channel-report-json') + 1])
                    self.assertEqual([r['channel'] for r in sent['results']],
                                     [5200] if fresh else [])


class OccupancyTests(AcsHarness):
    """Exercise real survey collection and scoring with isolated radios."""

    scorer = TOOLS / 'channel-election.sh'

    def score(self, reports, current=2462, band='2_4'):
        """Score a shared snapshot and return its winner and journal."""
        self.configure(current if band == '2_4' else None,
                       current if band == '5_0' else None)
        (self.root / 'uptime').write_text('10000.0 1.0\n')
        reports = [[dict(r, foreign_pct=r['busy_pct'])
                    if r.get('channel') == current and 'busy_pct' in r
                    else r for r in report] for report in reports]
        self.reports(reports)
        result = self.run_command(['bash', str(self.scorer), '--score'])
        self.assertEqual(result.returncode, 0, result.stderr)
        output = (self.root / 'election').read_text()
        winner = re.search(rf'WINNER_{band}=(\d+)', output)[1]
        return int(winner), result.stderr, output

    def scans(self, samples, cache='{}', fault=''):
        """Collect the unchanged discovery scans and their warmup."""
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
iface_driver() { printf 'test-driver'; }
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
    perform_scan discovery
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
                     '_survey_iface': 'wlan0', '_survey_boot': 10000}
                    for channel in (2437, 2462)])
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
        for index in (0, 1, 4):
            self.assertNotIn('busy_pct', scans[index]['results'][0])
        self.assertEqual(scans[2]['results'][0]['busy_pct'], 0)
        self.assertEqual([r['channel'] for r in scans[3]['results']], [2462])
        self.assertEqual(len(scans[4]['results'][0]['_busy_samples']), 1)

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

    def test_history_keeps_acquisition_time_instead_of_smoothing_time(self):
        report = {'results': [{'channel': 2462, 'noise_floor': -81,
                              '_survey_iface': 'wlan0', '_survey_boot': 995,
                              'busy_pct': 10}]}
        scans = self.smooth([(1000, report)])
        self.assertEqual(scans[0]['results'][0]['_busy_samples'], [[995, 10]])

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


class ScanAcquisitionTests(AcsHarness):
    """Failing acquisitions cannot renew an earlier interface's evidence."""

    @staticmethod
    def survey(freq=2437, active=100, busy=20, tx=0, noise=-81, home=False):
        lines = [f'frequency: {freq} MHz' + (' [in use]' if home else '')]
        if noise is not None:
            lines.append(f'noise: {noise} dBm')
        for name, value in [('active', active), ('busy', busy), ('transmit', tx)]:
            if value is not None:
                lines.append(f'channel {name} time: {value} ms')
        return '\n'.join(lines) + '\n'

    def sample(self, **changes):
        row = {'pre': self.survey(active=0, busy=0),
               'post': self.survey(), 'output': '', 'status': 0}
        row.update(changes)
        return row

    def acquire(self, rounds, *, driver='test-driver', two_bands=False,
                after='', cache=None, real_filter=False):
        self.configure(2462, 5220 if two_bands else None)
        for marker in self.root.glob('survey-*'):
            marker.unlink()
        (self.root / 'scan-rounds').write_text(json.dumps(rounds))
        self.command('iw', '''
import json, signal, time
root = Path(os.environ['TEST_ROOT'])
args = sys.argv[1:]
with (root / 'scan-commands').open('a') as log:
    log.write(json.dumps(args) + '\\n')
if args[0] == 'phy':
    print('* 2412.0 MHz [1] (20.0 dBm)')
    sys.exit(0)
if args[2:] == ['info']:
    print('wiphy 0')
    sys.exit(0)
row = json.loads((root / 'scan-rounds').read_text())[int(os.environ['ROUND'])][args[1]]
if args[2:] == ['survey', 'dump']:
    marker = root / ('survey-' + args[1] + '-' + os.environ['ROUND'])
    phase = 'post' if marker.exists() else 'pre'
    marker.touch()
    print(row[phase])
    sys.exit(row.get(phase + '_status', 0))
if args[2:4] == ['scan', 'dump']:
    print('BSS 02:00:00:00:00:aa\\n  freq: 2437.0')
    sys.exit(0)
if args[2:4] != ['scan', 'freq'] or args[-1] != 'flush':
    sys.exit(99)
print(row['output'], flush=True)
if row.get('hang'):
    (root / 'scan-child').write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
sys.exit(row['status'])
''')
        body = self.definitions('node-manager-acs.sh') + ('' if real_filter else '''
phy_usable_freqs() { printf '%s\n' "$2"; }
''') + '''
iface_driver() { printf '%s\n' ''' + shlex.quote(driver) + '''; }
''' + ('' if cache is None else
       'CACHED_SCAN_REPORT_JSON=' + shlex.quote(json.dumps(cache)) + '\n') + '''
for ((round=0; round<''' + str(len(rounds)) + '''; round++)); do
    export ROUND=$round
    printf '%s.50 1.0\n' "$((10000 + round * 180))" > "$MESH_UPTIME_FILE"
    perform_scan discovery
    printf '%s\n' "$CACHED_SCAN_REPORT_JSON"
done
''' + after
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()], result.stderr

    def test_failure_invalidates_warm_cache_and_logs_only_transitions(self):
        good = self.sample()
        bad = self.sample(status=16, output='BSS 02:00:00:00:00:aa\n freq: 2437.0')
        rounds = [{'wlan0': row} for row in [good] * 3 + [bad, bad, good, good]]
        reports, log = self.acquire(rounds, real_filter=True)
        self.assertIn('busy_pct', reports[2]['results'][0])
        self.assertEqual(reports[3]['results'], [])
        self.assertEqual(reports[4]['results'], [])
        self.assertNotIn('busy_pct', reports[5]['results'][0])
        self.assertEqual(len(reports[5]['results'][0]['_busy_samples']), 1)
        self.assertEqual(log.count('Scan failed on wlan0 (exit 16)'), 1)
        self.assertEqual(log.count('Scan recovered on wlan0'), 1)
        self.assertEqual(len(log.splitlines()), 2)
        commands = (self.root / 'scan-commands').read_text()
        self.assertNotIn('"scan", "dump"', commands)

    def test_timeout_discards_partial_output_and_reaps_child(self):
        reports, log = self.acquire([{'wlan0': self.sample(hang=True,
            output='BSS 02:00:00:00:00:aa\n freq: 2437.0')}],
            after='jobs -pr > "$TEST_ROOT/children"\n')
        self.assertEqual(reports[0]['results'], [])
        self.assertIn('Scan failed on wlan0 (timeout)', log)
        self.assertEqual((self.root / 'children').read_text(), '')
        pid = int((self.root / 'scan-child').read_text())
        self.assertFalse(Path(f'/proc/{pid}').exists())

    def test_zero_exit_scan_abort_is_failure(self):
        reports, log = self.acquire([{'wlan0': self.sample(output='scan aborted!')}])
        self.assertEqual(reports[0]['results'], [])
        self.assertIn('(aborted)', log)

    def test_one_failed_band_leaves_other_band_and_history_healthy(self):
        good24 = self.sample()
        good5 = self.sample(pre=self.survey(5200, active=0, busy=0),
                            post=self.survey(5200))
        rounds = [{'wlan0': good24, 'wlan1': good5}] * 2 + [
            {'wlan0': self.sample(status=1), 'wlan1': good5}]
        reports, _ = self.acquire(rounds, two_bands=True)
        self.assertEqual([r['channel'] for r in reports[-1]['results']], [5200])
        self.assertEqual(reports[-1]['results'][0]['busy_pct'], 20)
        (self.root / 'uptime').write_text('10000.0 1.0\n')
        self.reports([reports[-1]['results']])
        result = self.election()
        self.assertEqual(result.returncode, 0, result.stderr)
        output = (self.root / 'election').read_text()
        self.assertIn('WINNER_2_4=2462', output)
        self.assertIn('WINNER_5_0=5200', output)
        self.assertIn('LIMP_MODE=false', output)

    def test_unchanged_short_reset_missing_counters_or_noise_omit_channel(self):
        baseline = self.survey(active=100, busy=20)
        cases = [self.survey(active=100), self.survey(active=124),
                 self.survey(active=50), self.survey(active=None),
                 self.survey(active=200, noise=None)]
        reports, _ = self.acquire([{'wlan0': self.sample(pre=baseline, post=post)}
                                   for post in cases])
        self.assertTrue(all(r['results'] == [] for r in reports))

    def test_failed_survey_does_not_invent_zero_baseline(self):
        reports, log = self.acquire([
            {'wlan0': self.sample(pre_status=1)},
            {'wlan0': self.sample(post_status=1)},
            {'wlan0': self.sample(pre='')},
        ])
        self.assertTrue(all(r['results'] == [] for r in reports))
        self.assertEqual(log.count('(survey unavailable)'), 1)

    def test_fresh_noise_without_busy_is_distinct_from_failed_scan(self):
        row = self.sample(post=self.survey(busy=None))
        reports, log = self.acquire([{'wlan0': row}] * 3)
        self.assertEqual(log, '')
        for report in reports:
            self.assertEqual(report['results'][0]['noise_floor'], -81)
            self.assertNotIn('busy_pct', report['results'][0])
        reports, _ = self.acquire([{'wlan0': self.sample(
            pre=self.survey(active=100, busy=50),
            post=self.survey(active=200, busy=10))}])
        self.assertNotIn('busy_pct', reports[0]['results'][0])

    def test_mt7915_repeated_offchannel_visits_and_home_delta(self):
        row = self.sample(
            pre=self.survey(active=60, busy=12) +
                self.survey(2462, active=1000, busy=100, home=True),
            post=self.survey(active=60, busy=12) +
                 self.survey(2462, active=1100, busy=140, home=True))
        reports, _ = self.acquire([{'wlan0': row}] * 3, driver='mt7915e')
        self.assertEqual([r['busy_pct'] for r in reports[-1]['results']], [20, 40])
        bad = self.sample(pre=row['post'], post=row['post'], status=1)
        reports, _ = self.acquire([{'wlan0': bad}], driver='mt7915e')
        self.assertEqual(reports[0]['results'], [])

    def test_scan_output_counts_fresh_integer_and_decimal_bss_frequencies(self):
        output = '''BSS 02:00:00:00:00:01(on wlan0)
    freq: 2437
BSS 02:00:00:00:00:02(on wlan0)
    freq: 2437.0
BSS 02:00:00:00:00:03(on wlan0) -- associated
    last seen: 9999.0s [boottime]
    freq: 2437
BSS 02:00:00:00:00:04(on wlan0) -- associated
    freq: 2437
BSS 02:00:00:00:00:05(on wlan0)
    freq: 24370
BSS 02:00:00:00:00:06(on wlan0) -- associated
    last seen: 10000.700s [boottime]
    freq: 2437
'''
        reports, _ = self.acquire([{'wlan0': self.sample(output=output)}])
        self.assertEqual(reports[0]['results'][0]['bss_count'], 3)

    def test_cache_expires_without_scan_and_publication_does_not_refresh_it(self):
        reports, _ = self.acquire([{'wlan0': self.sample()}], after='''
fresh_scan_report
printf '10180.0 1.0\n' > "$MESH_UPTIME_FILE"
fresh_scan_report
printf '10181.0 1.0\n' > "$MESH_UPTIME_FILE"
fresh_scan_report
printf '9999.0 1.0\n' > "$MESH_UPTIME_FILE"
fresh_scan_report
''')
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[1], reports[2])
        self.assertEqual(reports[3]['results'], [])
        self.assertEqual(reports[4]['results'], [])

    def test_empty_observation_holds_both_data_and_bootstrap_channels(self):
        for freq in (2412, 2462):
            with self.subTest(freq=freq):
                self.configure(freq, None)
                self.reports([[]])
                result = self.election()
                self.assertEqual(result.returncode, 0, result.stderr)
                output = (self.root / 'election').read_text()
                self.assertIn(f'WINNER_2_4={freq}', output)
                self.assertIn('LIMP_MODE=false', output)


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
