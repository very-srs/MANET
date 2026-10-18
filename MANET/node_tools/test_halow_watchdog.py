"""The enslave watchdog restarts an active S1G supplicant whose mesh is gone."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent


class DeadHalowRestartTests(unittest.TestCase):
    """Run the real watchdog functions against stub systemctl and iw."""

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.stub('systemctl', '#!/bin/bash\n'
                  'case "$1" in\n'
                  '  is-active) [ "$TEST_S1G_ACTIVE" = 1 ] ;;\n'
                  '  restart) echo "$2" >> "$TEST_ROOT/restarts" ;;\n'
                  'esac\n')
        self.stub('iw', '#!/bin/bash\n[ "$3" = get ] && exit "$TEST_GET_RC"\nexit 0\n')
        self.net = self.root / 'sys/class/net/wlan2'
        self.net.mkdir(parents=True)
        self.flags('0x1003')
        self.uptime = self.root / 'uptime'
        source = (TOOLS / 'batman-enslave-watch.sh').read_text()
        body = source.split('while true; do', 1)[0].replace(
            '/sys/class/net/', f'{self.root}/sys/class/net/')
        self.script = (body + f'HALOW_STRIKES_FILE={self.root}/strikes\n'
                       f'HALOW_RESTART_FILE={self.root}/restart\n'
                       'log() { echo "$*" >> "$TEST_ROOT/journal"; }\n'
                       'HALOW_IFS=wlan2\nnote_halow_health\nrestart_dead_halow_if_needed\n')
        self.env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}',
                        TEST_ROOT=str(self.root), TEST_S1G_ACTIVE='1', TEST_GET_RC='0',
                        MESH_UPTIME_FILE=str(self.uptime),
                        MANET_RADIO_STATE_FILE=str(self.root / 'radio-state.json'),
                        MANET_IFACE_STATE_DIR=str(self.root),
                        MANET_TOOLS_DIR=str(TOOLS))

    def stub(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def flags(self, value):
        (self.net / 'flags').write_text(value + '\n')

    def restarts(self):
        path = self.root / 'restarts'
        return path.read_text().split() if path.exists() else []

    def passes(self, count, start=1000, step=8):
        """Run watchdog passes 8 s apart and return the boot time after them."""
        now = start
        for _ in range(count):
            self.uptime.write_text(f'{now}.00 0.00\n')
            result = subprocess.run(['bash', '-c', self.script], env=self.env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, '')
            now += step
        return now

    def test_joined_halow_is_never_restarted(self):
        self.passes(10)
        self.assertEqual(self.restarts(), [])

    def test_downed_interface_restarts_after_four_passes(self):
        # The DFS fence left wlan2 administratively down under an active unit.
        self.flags('0x1002')
        self.passes(3)
        self.assertEqual(self.restarts(), [])
        self.passes(1, start=1024)
        self.assertEqual(self.restarts(), ['wpa_supplicant-s1g-wlan2.service'])
        self.assertIn('no joined mesh', (self.root / 'journal').read_text())

    def test_up_interface_with_no_mesh_restarts(self):
        # The driver disabled the BSS: mesh point, no channel, mesh_param fails.
        self.env['TEST_GET_RC'] = '1'
        self.passes(4)
        self.assertEqual(self.restarts(), ['wpa_supplicant-s1g-wlan2.service'])

    def test_a_join_between_failures_resets_the_count(self):
        self.env['TEST_GET_RC'] = '1'
        now = self.passes(3)
        self.env['TEST_GET_RC'] = '0'
        now = self.passes(1, start=now)
        self.env['TEST_GET_RC'] = '1'
        self.passes(3, start=now)
        self.assertEqual(self.restarts(), [])

    def test_inactive_or_disabled_halow_is_left_alone(self):
        self.flags('0x1002')
        self.env['TEST_S1G_ACTIVE'] = '0'
        self.passes(6)
        self.env['TEST_S1G_ACTIVE'] = '1'
        (self.root / 'radio-state.json').write_text('{"desired": {"wlan2": "down"}}')
        self.passes(6, start=2000)
        self.assertEqual(self.restarts(), [])

    def test_repeat_restarts_back_off(self):
        self.flags('0x1002')
        # Restarts at 1024, then 60 s, 120 s and 240 s later while still dead.
        self.passes(70)
        self.assertEqual(len(self.restarts()), 4)
        self.assertEqual((self.root / 'restart').read_text().split()[1], '480')

    def test_rejoining_resets_the_back_off(self):
        self.flags('0x1002')
        now = self.passes(20)
        self.flags('0x1003')
        now = self.passes(1, start=now)
        self.assertFalse((self.root / 'restart').exists())
        self.flags('0x1002')
        self.passes(4, start=now)
        self.assertEqual(len(self.restarts()), 3)


if __name__ == '__main__':
    unittest.main()
