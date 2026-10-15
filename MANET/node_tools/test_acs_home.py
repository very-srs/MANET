"""Home airtime, RF hysteresis and candidate visits with command stubs."""

import base64
import json
import shlex
import subprocess
import sys

import decoder
from test_acs import AcsHarness, OWN, PEER, TOOLS


class HomeMonitorTests(AcsHarness):
    """Run real collection against cumulative surveys and station airtime."""

    def setUp(self):
        super().setUp()
        sleep = self.bin / 'sleep'
        sleep.write_text(sleep.read_text() + '''
import json
with Path(os.environ['TEST_ROOT'], 'rf-commands').open('a') as stream:
    stream.write(json.dumps(['sleep', *sys.argv[1:]]) + '\\n')
''')

    def collect(self, rows, *, peer=False, members=None, status_age=0,
                report_age=0, driver='test-driver', mode='data', busy=False,
                boot='a' * 32, report_mac=PEER, peer_channel=2437,
                freqs24='2437 2462'):
        """Replay complete cycles, logging every wireless command."""
        self.configure(2462, 5220)
        for pattern in ('roc-*', 'scan-*'):
            for marker in self.root.glob(pattern):
                marker.unlink()
        (self.root / 'home-rows').write_text(json.dumps(rows))
        (self.root / 'rf-commands').write_text('')
        self.reports([[{'channel': peer_channel, 'foreign_pct': 70,
                        'rf_trouble': peer}]], [report_mac])
        self.registry.write_text(self.registry.read_text().replace(
            "'10000'", repr(str(10000 - report_age))))
        self.command('iw', '''
import json
root = Path(os.environ['TEST_ROOT'])
args = sys.argv[1:]
with (root / 'rf-commands').open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
row = json.loads((root / 'home-rows').read_text())[int(os.environ['ROUND'])]
iface = args[1]
row = row.get(iface, row)
home = row.get('home', 5220 if iface == 'wlan1' else 2462)
if args[2:] == ['station', 'dump']:
    if row.get('station_fail'):
        sys.exit(1)
    for i, (rx, tx) in enumerate(row.get('peers', [(0, 0)])):
        print('Station 02:00:00:00:00:%02x (on %s)' % (i, iface))
        if rx is not None:
            print('  rx duration:', rx, 'us')
        if tx is not None:
            print('  tx duration:', tx, 'us')
elif args[2:4] == ['scan', 'freq']:
    (root / ('scan-' + iface)).write_text(os.environ['ROUND'])
    for i, freq in enumerate(row.get('bss_freqs', [2437])):
        print('BSS 02:00:00:00:00:%02x\\n  freq: %s.0' % (i, freq))
    if row.get('scan_aborted'):
        print('scan aborted!')
    sys.exit(row.get('scan_status', 0))
elif args[2] == 'offchannel':
    assert args[-1] == '250'
    (root / ('roc-' + iface + '-' + args[3])).write_text(os.environ['ROUND'])
    sys.exit(row.get('roc_status', 0))
elif args[2:] == ['survey', 'dump']:
    if row.get('survey_fail'):
        sys.exit(1)
    print('frequency:', home, 'MHz [in use]')
    print('noise:', row.get('noise', -90), 'dBm')
    for key, default in [('active', 1000), ('busy', 100),
                         ('transmit', 0), ('receive', 0)]:
        print('channel', key, 'time:', row.get(key, default), 'ms')
    others = row.get('candidates', [5200] if iface == 'wlan1' else [2437])
    for other in others:
        print('frequency:', other, 'MHz')
        print('noise: -95 dBm')
        marker = root / ('roc-' + iface + '-' + str(other))
        visited = marker.exists() and marker.read_text() == os.environ['ROUND']
        pre = row.get('candidate_pre', [100, 10, 0])
        post = row.get('candidate_post', [350, 85, 25])
        # Bootstrap and 5 GHz retain their existing scan acquisition path.
        marker = root / ('scan-' + iface)
        scanned = marker.exists() and marker.read_text() == os.environ['ROUND']
        values = post if visited or scanned else pre
        for key, value in zip(('active', 'busy', 'transmit'), values):
            if value is not None:
                print('channel', key, 'time:', value, 'ms')
else:
    sys.exit(99)
''')
        body = self.definitions('node-manager-acs.sh') + '''
phy_usable_freqs() { printf '%s\n' "$2"; }
iface_driver() { printf '%s\n' ''' + shlex.quote(driver) + '''; }
acs_agreement_busy() { ''' + ('true' if busy else 'false') + '''; }
SCAN_FREQS_5_0='5200 5220'
''' + 'SCAN_FREQS_2_4=' + shlex.quote(freqs24) + '\n'
        body += 'MY_MAC=' + shlex.quote(OWN) + '\n'
        for i, row in enumerate(rows):
            now = row.get('time', 10000 + i * 180)
            snapshot = {'boot': boot, 'observed_boot': now - status_age,
                        'members': members if members is not None else [
                            'NODE_' + m.replace(':', '') for m in (OWN, PEER)]}
            body += f'export ROUND={i}\n'
            body += 'printf \'%s\\n\' ' + shlex.quote(
                f'{now}.0 1.0') + ' > "$MESH_UPTIME_FILE"\n'
            body += 'printf \'%s\\n\' ' + shlex.quote(json.dumps(snapshot))
            body += ' > "$MANET_ACS_RUN_DIR/manet-acs-status.json"\n'
            if row.get('change_iface'):
                body += 'printf wlan9 > "$MANET_IFACE_STATE_DIR/mesh_24_if"\n'
            body += f'perform_scan {mode}\n'
            body += 'printf \'%s\\n\' "$CACHED_SCAN_REPORT_JSON"\n'
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('error', result.stderr.lower())
        reports = [json.loads(line) for line in result.stdout.splitlines()]
        self.command_trace = [json.loads(line) for line in
                              (self.root / 'rf-commands').read_text().splitlines()]
        commands = [c for c in self.command_trace if c[0] == 'dev']
        return reports, commands

    @staticmethod
    def row(i, foreign=30, noise=-90, **extra):
        """Make one cumulative 180-second window with 20% own airtime."""
        row = dict(active=1000 + i * 180000,
                   busy=100 + i * (foreign + 20) * 1800,
                   transmit=i * 18000, receive=i * 18000,
                   peers=[(i * 9000000, i * 4000000),
                          (i * 9000000, i * 5000000)], noise=noise)
        row.update(extra)
        return row

    def test_home_total_busy_and_foreign_subtract_peer_rx(self):
        """A long home window measures both bands without candidate commands."""
        reports, commands = self.collect([self.row(0), self.row(1)])
        self.assertTrue(all('busy_pct' not in r
                            for r in reports[0]['results']))
        for sample in reports[1]['results']:
            self.assertEqual(sample['busy_pct'], 50)
            self.assertEqual(sample['foreign_pct'], 30)
            self.assertEqual(sample['window_s'], 180)
            self.assertFalse(sample['rf_trouble'])
            self.assertEqual(sample['bss_count'], 0)
        self.assertEqual(len(reports[1]['results']), 2)
        self.assertTrue(all(c[2:] in (['survey', 'dump'], ['station', 'dump'])
                            for c in commands), commands)

    def test_reset_channel_interface_and_short_window(self):
        """Bad baselines cannot become a fresh home measurement."""
        cases = [dict(active=5), dict(busy=5), dict(transmit=0),
                 dict(receive=0), dict(peers=[(0, 0)]),
                 dict(home=2437), dict(change_iface=True), dict(time=10200)]
        for change in cases:
            with self.subTest(change=change):
                reports, _ = self.collect([
                    self.row(0), self.row(1), self.row(2, **change)])
                self.assertTrue(all('foreign_pct' not in r
                                    for r in reports[2]['results']
                                    if r['channel'] < 5000))

    def test_real_elapsed_window_and_restart_baseline(self):
        """Time metadata is elapsed boot time, never a fixed cycle constant."""
        reports, _ = self.collect([self.row(0), self.row(1, time=10123)])
        self.assertEqual(reports[1]['results'][0]['window_s'], 123)
        reports, _ = self.collect([self.row(2)])
        self.assertTrue(all('busy_pct' not in r
                            for r in reports[0]['results']))

    def test_peer_only_gate_visits_one_24_candidate_and_no_5_candidates(self):
        """A peer's band flag opens only that band before local trouble."""
        reports, commands = self.collect([self.row(0)], peer=True)
        self.assertEqual([c for c in commands if c[2] == 'offchannel'],
                         [['dev', 'wlan0', 'offchannel', '2437', '250']])
        scans = [c for c in commands if c[2] == 'scan']
        self.assertEqual(scans, [])
        sample = next(r for r in reports[0]['results']
                      if r['channel'] == 2437)
        self.assertEqual(sample['channel'], 2437)
        self.assertEqual(sample['busy_pct'], 20)
        self.assertEqual(sample['bss_count'], 0)
        self.assertEqual(reports[0]['results'][0]['bss_count'], 0)
        self.assertNotIn('foreign_pct', sample)
        self.assertNotIn('_busy_samples', sample)

    def test_24_bss_presence_cannot_bias_collected_election_scores(self):
        """Equal RF conditions have equal raw scores even among many BSS."""
        for count in (0, 6, 7):
            with self.subTest(candidate_bss=count):
                reports, commands = self.collect([
                    self.row(i, 20, -95, bss_freqs=[2437] * count,
                             scan_status=124) for i in range(2)], peer=True)
                self.assertFalse(any(c[2] == 'scan' for c in commands))
                samples = [r for r in reports[-1]['results']
                           if r['channel'] < 2500]
                self.assertEqual([r['bss_count'] for r in samples], [0, 0])
                self.assertEqual(samples[0]['foreign_pct'], 20)
                self.assertEqual(samples[1]['busy_pct'], 20)
                self.reports([samples])
                result = self.election()
                self.assertEqual(result.returncode, 0, result.stderr)
                for channel in (2462, 2437):
                    self.assertIn(f'{channel}: ok score/nodes/noise/busy/'
                                  'bss/veto = 20 1 -95 20 0 0/1', result.stderr)

    def test_5_home_and_candidates_pay_bss_from_the_same_scan(self):
        """The successful 5 GHz scan counts home and candidate BSS equally."""
        for home_count, candidate_count in ((0, 6), (6, 6), (6, 8)):
            with self.subTest(home=home_count, candidate=candidate_count):
                reports, commands = self.collect([
                    self.row(i, 20, -95, time=10000 + i * 60,
                             bss_freqs=[5220] * home_count
                                       + [5200] * candidate_count)
                    for i in range(3)], peer=True, peer_channel=5200)
                scans = [c for c in commands if c[2] == 'scan']
                self.assertEqual(scans, [
                    ['dev', 'wlan1', 'scan', 'freq', '5200', '5220', 'flush']
                ] * 3)
                self.assertFalse(any(c[2] == 'offchannel' for c in commands))
                samples = [r for r in reports[-1]['results']
                           if r['channel'] > 5000]
                self.assertEqual([r['bss_count'] for r in samples],
                                 [home_count, candidate_count])
                self.assertEqual(samples[0]['foreign_pct'], 20)
                self.assertEqual(samples[1]['busy_pct'], 20)
                self.reports([samples])
                result = self.election()
                self.assertEqual(result.returncode, 0, result.stderr)
                for channel, count in ((5220, home_count),
                                       (5200, candidate_count)):
                    score = 20 + count * 0.5
                    self.assertIn(f'{channel}: ok score/nodes/noise/busy/'
                                  f'bss/veto = {score:g} 1 -95 20 {count} 0/1',
                                  result.stderr)

    def test_each_roc_waits_at_least_its_dwell_before_post_survey(self):
        """iw returning only acknowledges ROC, so every visit must wait."""
        reports, commands = self.collect([
            self.row(0, candidates=[2422, 2437])], peer=True,
            freqs24='2422 2437 2462')
        visits = [c for c in commands if c[2] == 'offchannel']
        self.assertEqual([c[3] for c in visits], ['2422', '2437'])
        for visit in visits:
            index = self.command_trace.index(visit)
            wait, post = self.command_trace[index + 1:index + 3]
            self.assertEqual(wait[0], 'sleep')
            self.assertGreaterEqual(float(wait[1]) * 1000, int(visit[4]))
            self.assertEqual(post, ['dev', visit[1], 'survey', 'dump'])
        self.assertEqual([r['channel'] for r in reports[0]['results']
                          if r.get('_roc')], [2422, 2437])

    def test_disconnected_stale_wrong_boot_or_future_peer_cannot_scan(self):
        """Only the fresh connected view and fresh peer flags open the gate."""
        cases = [dict(members=['NODE_' + OWN.replace(':', '')]),
                 dict(status_age=30), dict(status_age=-1),
                 dict(report_age=240), dict(report_age=-1),
                 dict(boot='b' * 32)]
        for changes in cases:
            with self.subTest(changes=changes):
                _, commands = self.collect([self.row(0)], peer=True, **changes)
                self.assertFalse(any(c[2] in ('scan', 'offchannel')
                                     for c in commands), commands)

    def test_local_trouble_opens_after_two_windows(self):
        """Two bad long windows open both bands with no peer flag."""
        reports, commands = self.collect([self.row(i, 50) for i in range(3)])
        self.assertFalse(reports[1]['results'][0]['rf_trouble'])
        home = [r for r in reports[2]['results'] if 'foreign_pct' in r]
        self.assertTrue(all(r['rf_trouble'] for r in home))
        self.assertEqual([c[1] for c in commands if c[2] == 'scan'], ['wlan1'])
        self.assertEqual(len([c for c in commands if c[2] == 'offchannel']), 1)

    def test_old_self_report_does_not_reopen_a_healthy_band(self):
        """Local hysteresis supersedes the previous local registry report."""
        _, commands = self.collect([self.row(0), self.row(1)],
                                   peer=True, report_mac=OWN)
        self.assertFalse(any(c[2] in ('scan', 'offchannel') for c in commands))

    def test_gate_clears_and_drops_candidates_after_three_good_windows(self):
        """Two bad windows followed by three good windows close each band."""
        rows = [self.row(i, 50) for i in range(3)]
        for i in range(3, 6):
            row = self.row(i, 30)
            row['busy'] += 2 * 20 * 1800
            rows.append(row)
        reports, commands = self.collect(rows)
        self.assertTrue(any(r.get('_roc') for r in reports[4]['results']))
        self.assertEqual(len(reports[5]['results']), 2)
        self.assertTrue(all(r['rf_trouble'] is False
                            for r in reports[5]['results']))
        self.assertEqual(len([c for c in commands if c[2] == 'offchannel']), 3)

    def test_missing_station_duration_never_invents_foreign_occupancy(self):
        """A peer lacking duration counters makes that home window unknown."""
        reports, _ = self.collect([self.row(0),
                                  self.row(1, peers=[(None, 10)])])
        self.assertTrue(all('foreign_pct' not in r
                            for r in reports[1]['results']))

    def test_counter_reset_preserves_trouble_until_three_good_windows(self):
        """Reset measurements never masquerade as a cleared trouble flag."""
        rows = [self.row(i, 50) for i in range(3)]
        rows += [self.row(i, 30, time=10540 + i * 180) for i in range(4)]
        reports, _ = self.collect(rows)
        reset_homes = [r for r in reports[3]['results'] if 'rf_trouble' in r]
        self.assertEqual(len(reset_homes), 2)
        self.assertTrue(all(r['rf_trouble'] for r in reset_homes))
        self.assertTrue(all('foreign_pct' not in r for r in reset_homes))
        for i in (4, 5):
            self.assertTrue(all(r['rf_trouble'] for r in reports[i]['results']
                                if 'rf_trouble' in r))
        self.assertTrue(all(r['rf_trouble'] is False
                            for r in reports[6]['results']))

    def test_empty_station_list_and_negative_foreign_clamp(self):
        """No peers is a valid zero sum, and overlapping counters clamp."""
        reports, _ = self.collect([self.row(0, peers=[]),
                                   self.row(1, peers=[])])
        self.assertEqual(reports[1]['results'][0]['foreign_pct'], 40)
        reports, _ = self.collect([self.row(0),
                                  self.row(1, peers=[(900000000, 0)])])
        self.assertEqual(reports[1]['results'][0]['foreign_pct'], 0)

    def test_roc_reset_short_missing_and_failed_visits_are_omitted(self):
        """Invalid or failed visits cannot publish candidate RF evidence."""
        cases = [dict(candidate_post=[349, 85, 25]),
                 dict(candidate_post=[50, 20, 0]),
                 dict(candidate_post=[350, 5, 0]),
                 dict(candidate_post=[350, 85, None]),
                 dict(roc_status=1)]
        for changes in cases:
            with self.subTest(changes=changes):
                reports, _ = self.collect([self.row(0, **changes)], peer=True)
                self.assertTrue(all('busy_pct' not in r
                                    for r in reports[0]['results']))

    def test_mt7915_roc_uses_visit_counters_and_250ms_minimum(self):
        """Known driver resets are valid only after a successful long visit."""
        reports, _ = self.collect([self.row(0,
            candidate_pre=[273, 81, 0], candidate_post=[273, 81, 0])],
            peer=True, driver='mt7915e')
        candidate = next(r for r in reports[0]['results']
                         if r['channel'] == 2437)
        self.assertEqual(candidate['busy_pct'], 29)
        reports, _ = self.collect([self.row(0,
            candidate_pre=[273, 81, 0], candidate_post=[249, 81, 0])],
            peer=True, driver='mt7915e')
        self.assertFalse(any(r['channel'] == 2437
                             for r in reports[0]['results']))

    def test_agreement_pause_retains_home_monitor(self):
        """Radio changes pause candidates while home monitoring continues."""
        reports, commands = self.collect([self.row(0), self.row(1)],
                                         peer=True, busy=True)
        self.assertEqual(reports[1]['results'][0]['foreign_pct'], 30)
        self.assertFalse(any(c[2] in ('scan', 'offchannel') for c in commands))

    def test_home_survives_failed_candidate_scan(self):
        """5 GHz failures clear old BSS/candidates and preserve home data."""
        for failure in (dict(scan_status=1), dict(scan_status=124),
                        dict(scan_aborted=True)):
            with self.subTest(failure=failure):
                reports, _ = self.collect([
                    self.row(0, bss_freqs=[5220] * 6),
                    self.row(1, bss_freqs=[5220] * 6, **failure)],
                    peer=True, peer_channel=5200)
                self.assertEqual(reports[0]['results'][1]['bss_count'], 6)
                self.assertEqual(len(reports[1]['results']), 2)
                home = reports[1]['results'][1]
                self.assertEqual(home['foreign_pct'], 30)
                self.assertEqual(home['bss_count'], 0)

    def test_discovery_scans_are_not_subject_to_trouble_gate(self):
        """Lobby bootstrap keeps its software scan path with no trouble."""
        reports, commands = self.collect([self.row(0)], mode='discovery')
        self.assertEqual(len([c for c in commands if c[2] == 'scan']), 2)
        self.assertFalse(any(c[2] == 'offchannel' for c in commands))
        candidate = next(r for r in reports[0]['results']
                         if r['channel'] == 2437)
        self.assertEqual(candidate['bss_count'], 1)

    def test_hysteresis_boundaries_and_interrupted_streaks(self):
        """Either limit activates trouble, both must clear to turn it off."""
        body = self.definitions('node-manager-acs.sh') + '''
for sample in '50 -90' '49 -90' '50 -90' '50 -90' \
              '39 -86' '40 -86' '39 -86' '39 -85' \
              '39 -86' '39 -86' '39 -86' '0 -80' '0 -80'; do
    update_home_trouble 2_4 $sample
    printf '%s\n' "${HOME_TROUBLE[2_4]:-false}"
done
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
            ['false'] * 3 + ['true'] * 7 + ['false', 'false', 'true'])


class HomeScoringTests(AcsHarness):
    """Use explicit home fields with the real stateless shell scorer."""

    def score(self, foreign, candidates=(), *, busy=99, noise=-95):
        """Score reports while leaving total home busy high."""
        self.configure(2462, None)
        self.reports([[dict(channel=2462, foreign_pct=foreign, busy_pct=busy,
                            noise_floor=noise, window_s=180, rf_trouble=False),
                       *candidates]])
        result = self.election()
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split('=') for line in
                    (self.root / 'election').read_text().splitlines())

    def test_total_busy_does_not_disqualify_healthy_incumbent(self):
        """Foreign occupancy, not total mesh traffic, sets score and urgency."""
        result = self.score(10, [dict(channel=2437, busy_pct=15,
                                       noise_floor=-95)])
        self.assertEqual(result['WINNER_2_4'], '2462')
        self.assertEqual(result['URGENT_2_4'], 'false')
        self.assertEqual(result['LIMP_MODE'], 'false')

    def test_no_candidates_holds_without_urgency_then_bad_home_is_urgent(self):
        """No candidate data cannot cause a move even above disqualification."""
        for foreign in (10, 90):
            result = self.score(foreign)
            self.assertEqual(result['WINNER_2_4'], '2462')
            self.assertEqual(result['URGENT_2_4'], 'false')
            self.assertEqual(result['LIMP_MODE'], 'false')
        result = self.score(90, [dict(channel=2437, busy_pct=20,
                                       noise_floor=-95)])
        self.assertEqual(result['WINNER_2_4'], '2437')
        self.assertEqual(result['URGENT_2_4'], 'true')

    def test_other_nodes_home_measurement_is_not_a_candidate_visit(self):
        """A group on another home channel must still measure candidates."""
        result = self.score(10, [dict(channel=2437, busy_pct=0, foreign_pct=0,
                                     noise_floor=-95)])
        self.assertEqual(result['WINNER_2_4'], '2462')
        self.assertEqual(result['URGENT_2_4'], 'false')

    def test_empty_registry_writes_a_hold_instead_of_reusing_previous_output(
            self):
        """A vanished report cannot leave a previous urgent winner behind."""
        self.score(90, [dict(channel=2437, busy_pct=20, noise_floor=-95)])
        self.registry.write_text('')
        result = self.election()
        self.assertEqual(result.returncode, 0, result.stderr)
        output = (self.root / 'election').read_text()
        self.assertIn('WINNER_2_4=2462', output)
        self.assertIn('URGENT_2_4=false', output)

    def test_trouble_only_home_is_not_a_noise_measurement(self):
        """Absent counters cannot turn protobuf's zero noise into a veto."""
        self.configure(2462, None)
        self.reports([[dict(channel=2462, rf_trouble=True, noise_floor=0)]])
        result = self.election()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('2462: no scan data', result.stderr)
        output = (self.root / 'election').read_text()
        self.assertIn('WINNER_2_4=2462', output)
        self.assertIn('URGENT_2_4=false', output)
        self.assertIn('LIMP_MODE=false', output)

    def test_home_foreign_uses_reporter_median_and_quorum(self):
        """One bad home does not veto or dominate three connected reports."""
        self.configure(2462, None)
        self.reports([[dict(channel=2462, busy_pct=99, foreign_pct=foreign,
                            noise_floor=-95),
                       dict(channel=2437, busy_pct=25, noise_floor=-95)]
                      for foreign in (10, 90, 20)])
        result = self.election()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('2462: ok score/nodes/noise/busy/bss/veto = '
                      '20 3 -95 20 0 1/2', result.stderr)
        self.assertIn('WINNER_2_4=2462', (self.root / 'election').read_text())

    def test_limp_uses_foreign_home_and_candidate_dwell(self):
        """Raw foreign occupancy can assert limp even with incumbent bias."""
        result = self.score(65, [dict(channel=2437, busy_pct=90,
                                       noise_floor=-95)])
        self.assertEqual(result['WINNER_2_4'], '2462')
        self.assertEqual(result['LIMP_MODE'], 'true')
        self.assertEqual(result['URGENT_2_4'], 'true')

    def test_wire_presence_and_zero_values_survive_encoder_decoder(self):
        """Home metadata is optional and candidate wire values stay intact."""
        results = [dict(channel=2462, busy_pct=80, foreign_pct=0,
                        window_s=180, rf_trouble=False, noise_floor=-90,
                        bss_count=0),
                   dict(channel=2437, busy_pct=0, noise_floor=-95, bss_count=2)]
        encoded = subprocess.check_output([
            sys.executable, str(TOOLS / 'encoder.py'), 'telemetry',
            '--timestamp', '1000', '--mesh-config', '/dev/null',
            '--channel-report-json', json.dumps({'results': results})],
            text=True)
        decoded = {}
        decoder.decode_telemetry(base64.b64decode(encoded),
            emit=lambda key, value: decoded.update({key: value}),
            emit_raw=lambda key, value: None)
        self.assertEqual(json.loads(decoded['CHANNEL_REPORT_JSON']),
                         {'results': results})
