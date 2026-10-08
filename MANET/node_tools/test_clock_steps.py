#!/usr/bin/env python3
"""Local timers run on the boot clock: a wall-clock step neither fires nor stalls them.

A time sync can step the wall clock by any amount, in either direction. Each
test here holds the boot clock (MESH_UPTIME_FILE) fixed or advancing and
moves the wall clock underneath, through a fake date command.
"""
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent
WALL = 1791288000


class ClockHarness(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='manet-clock-test-')
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.uptime = self.root / 'uptime'
        self.wall = WALL
        self.stub('date', '#!/bin/bash\n[ "$1" = +%s ] && { cat "$TEST_WALL_FILE"; exit 0; }\necho test-clock\n')
        self.stub('systemd-cat', '#!/bin/bash\ncat >> "$TEST_ROOT/journal"\n')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def env(self, uptime, **extra):
        self.uptime.write_text(f'{uptime}.73 100.00\n')
        (self.root / 'wall').write_text(f'{self.wall}\n')
        return dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                    MESH_UPTIME_FILE=str(self.uptime), TEST_WALL_FILE=str(self.root / 'wall'),
                    TEST_ROOT=str(self.root), **extra)


class UptimeNowTests(ClockHarness):
    def read(self, text):
        self.uptime.write_text(text)
        env = dict(os.environ, MESH_UPTIME_FILE=str(self.uptime))
        out = subprocess.run(['bash', '-c', f'. "{TOOLS}/manet-common.sh"; uptime_now'],
                             env=env, capture_output=True, text=True, timeout=10)
        return out.returncode, out.stdout.strip()

    def test_whole_seconds_from_the_boot_clock(self):
        self.assertEqual(self.read('4321.73 100.00\n'), (0, '4321'))
        self.assertEqual(self.read('00008.50 0\n'), (0, '8'))
        self.assertEqual(self.read('0.00 0\n'), (0, '0'))

    def test_malformed_clock_fails_instead_of_reading_as_zero(self):
        # a bad read must not become an arithmetic zero.
        for text in ('\n', '', 'not-a-clock 0\n', '1.2.3 0\n', '-5 0\n'):
            self.assertEqual(self.read(text), (1, ''), repr(text))


class LimpModeTests(ClockHarness):
    """Minimum residence in limp mode counts boot-clock seconds."""

    def setUp(self):
        super().setUp()
        self.registry = self.root / 'registry'
        self.state = self.root / 'limp.state'
        self.stub('iw', '#!/bin/bash\necho "iw $*" >> "$TEST_ROOT/iw"\n')

    def consensus(self, limp, uptime):
        rows = []
        for i in range(4):
            mac = f'0200000000{i:02x}'
            rows.append(f"NODE_{mac}_OBSERVED_AT_UPTIME='{uptime - 5}'")
            rows.append(f"NODE_{mac}_IS_IN_LIMP_MODE='{'true' if limp else 'false'}'")
        self.registry.write_text('\n'.join(rows) + '\n')

    def run_limp(self, uptime):
        env = self.env(uptime, MESH_REGISTRY_FILE=str(self.registry),
                       MANET_LIMP_STATE_FILE=str(self.state),
                       MANET_ACS_LOCK_FILE=str(self.root / 'lock'))
        result = subprocess.run(['bash', str(TOOLS / 'limp-mode-manager.sh')], env=env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wall_steps_do_not_change_minimum_residence(self):
        self.consensus(True, 1000)
        self.run_limp(1000)
        self.assertEqual(self.state.read_text().strip(), '1000')
        self.consensus(False, 1100)
        for wall in (WALL + 3600, WALL - 3600, 0):
            self.wall = wall
            self.run_limp(1100)
            self.assertTrue(self.state.exists(), wall)
        self.run_limp(1300)
        self.assertFalse(self.state.exists())

    def test_wall_time_from_an_older_version_restarts_residence_once(self):
        self.state.write_text(f'{WALL}\n')
        self.consensus(False, 2000)
        self.run_limp(2000)
        self.assertEqual(self.state.read_text().strip(), '2000')
        self.run_limp(2299)
        self.assertTrue(self.state.exists())
        self.run_limp(2300)
        self.assertFalse(self.state.exists())


class BrowserClockTests(unittest.TestCase):
    """Pages count down to mesh times with the mesh clock, not the phone's."""

    def test_countdowns_to_mesh_times_never_use_the_browser_clock(self):
        page = (TOOLS / 'manet_manage.py').read_text()
        for line in page.splitlines():
            if 'activate_at' in line.lower() and 'Date.now()' in line:
                self.fail('countdown uses the browser clock: ' + line.strip())
        self.assertIn("response.headers.get('X-Mesh-Time')", page)
        mesh_now = page[page.index('function meshNow()'):page.index('let manageRetryAt')]
        self.assertIn('performance.now()', mesh_now)
        self.assertNotIn('meshSkew', page)

    def test_measurement_durations_come_from_the_node(self):
        page = (TOOLS / 'manet_manage.py').read_text()
        stats = page[page.index('function renderMeasureStats('):]
        stats = stats[:stats.index('el.innerHTML')]
        self.assertNotIn('started_at', stats)
        self.assertIn('d.elapsed', stats)

    def test_every_json_reply_carries_the_mesh_clock(self):
        server = (TOOLS / 'mesh-status.py').read_text()
        start = server.index('    def send_json(')
        self.assertIn("'X-Mesh-Time'", server[start:server.index('    def read_json_body(', start)])

    def test_retry_holdoffs_use_the_page_monotonic_clock(self):
        for name in ('manet_manage.py', 'mesh-status.py'):
            text = (TOOLS / name).read_text()
            self.assertNotIn('RetryAt = Date.now()', text, name)


class EnslaveWatchCooldownTests(ClockHarness):
    """The HaLow-primary repair's 60 s cooldown, run from the real function."""

    def setUp(self):
        super().setUp()
        self.stub('ip', '#!/bin/bash\nexit 0\n')
        self.stub('batctl', '#!/bin/bash\ncase "$*" in\n'
                  '  "bat0 if") echo "wlan1: active" ;;\n'
                  '  "bat0 o") echo "[B.A.T.M.A.N. adv, MainIF/MAC: wlan0/02:00:00:00:00:01 (bat0)]" ;;\n'
                  'esac\n')
        self.stub('batman-if-setup.sh', '#!/bin/bash\necho "$1" >> "$TEST_ROOT/setup"\n')
        source = (TOOLS / 'batman-enslave-watch.sh').read_text()
        start = source.index('batman_mainif() {')
        body = source[start:source.index('# hostapd can be left')]
        body = body.replace('/usr/local/bin/batman-if-setup.sh', str(self.bin / 'batman-if-setup.sh'))
        self.cooldown = self.root / 'cooldown'
        body = re.sub(r'/run/batman-enslave-watch-halow-primary-reset', str(self.cooldown), body)
        self.script = (f'. "{TOOLS}/manet-common.sh"\nlog() {{ echo "$*" >> "$TEST_ROOT/journal"; }}\n'
                       'sleep() { :; }\nHALOW_IFS=wlan1\n' + body + 'restore_halow_primary_if_needed\n')

    def repairs(self, uptime, stamp=None):
        if stamp is None:
            self.cooldown.unlink(missing_ok=True)
        else:
            self.cooldown.write_text(f'{stamp}\n')
        before = (self.root / 'setup').read_text() if (self.root / 'setup').exists() else ''
        subprocess.run(['bash', '-c', self.script], env=self.env(uptime), timeout=10, check=True)
        after = (self.root / 'setup').read_text() if (self.root / 'setup').exists() else ''
        return after != before

    def test_cooldown_counts_boot_clock_seconds(self):
        self.assertTrue(self.repairs(5000))
        self.assertEqual(self.cooldown.read_text().strip(), '5000')
        self.assertFalse(self.repairs(5059, stamp=5000))
        self.assertTrue(self.repairs(5060, stamp=5000))

    def test_missing_stamp_repairs_even_in_the_first_minute_of_boot(self):
        self.assertTrue(self.repairs(12))

    def test_wall_time_from_an_older_version_does_not_block_repair(self):
        # a same-boot tools update leaves an epoch stamp behind.
        self.assertTrue(self.repairs(5000, stamp=1791288000))


class RollbackStatusTests(unittest.TestCase):
    """The config tab's countdown follows the controller's boot-clock rules."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location('clock_step_status', TOOLS / 'mesh-status.py')
        self.status = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.status)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.state = Path(scratch.name) / 'state'
        self.boot = Path(scratch.name) / 'boot_id'
        self.boot.write_text('boot-a\n')
        for name, value in (('ROLLBACK_STATE_FILE', str(self.state)), ('BOOT_ID_FILE', str(self.boot))):
            p = patch.object(self.status, name, value)
            p.start()
            self.addCleanup(p.stop)

    def left(self, state, boot_now=5000.0, wall=WALL):
        self.state.write_text(state)
        with patch.object(self.status.time, 'clock_gettime', return_value=boot_now), \
                patch.object(self.status.time, 'time', return_value=wall):
            return self.status.read_rollback_state()['seconds_left']

    def test_same_boot_counts_down_on_the_boot_clock(self):
        state = "VERSION='aabbcc'\nPEERS_BEFORE=1\nBOOT_ID='boot-a'\nDEADLINE=5300\nREARMED=0\n"
        for wall in (WALL, WALL + 3600, WALL - 3600):
            self.assertEqual(self.left(state, wall=wall), 300)
        self.assertEqual(self.left(state, boot_now=5301.0), 0)

    def test_first_reboot_shows_the_full_grace_and_second_decides_now(self):
        self.assertEqual(self.left("BOOT_ID='boot-old'\nDEADLINE=5300\nREARMED=0\n"), 300)
        self.assertEqual(self.left("BOOT_ID='boot-old'\nDEADLINE=40\nREARMED=1\n"), 0)


class MeasurementDurationTests(unittest.TestCase):
    def test_durations_follow_the_monotonic_clock_not_wall_steps(self):
        import manet_manage
        with patch.dict(manet_manage._measure_status, started_at=WALL, current_started_at=WALL + 5), \
                patch.dict(manet_manage._measure_mono, started=100.0, current=105.0):
            for wall in (WALL + 3600, WALL - 3600):
                with patch.object(manet_manage.time, 'monotonic', return_value=110.0), \
                        patch.object(manet_manage.time, 'time', return_value=wall):
                    snap = manet_manage.measure_status_snapshot()
                self.assertEqual((snap['elapsed'], snap['current_elapsed']), (10, 5))


if __name__ == '__main__':
    unittest.main()
