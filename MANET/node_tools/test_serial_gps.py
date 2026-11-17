#!/usr/bin/env python3
"""Serial GPS detection in radio setup and CM4 first-boot UART settings."""
import os
import pty
import re
import subprocess
import unittest
from pathlib import Path

MANET = Path(__file__).resolve().parents[1]
RADIO_SETUP = MANET / 'node_tools/radio-setup.sh'
FIRSTRUN = MANET / 'provisioning/firstrun.sh.template'


class SerialGpsProbeTests(unittest.TestCase):
    def probe(self, data, wait='3'):
        source = RADIO_SETUP.read_text()
        function = re.search(r'^serial_gps_device\(\) \{\n.*?^\}', source,
                             re.M | re.S).group()
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        function = function.replace('/dev/ttyAMA0', os.ttyname(slave))
        function = function.replace('timeout 3 ', 'timeout ' + wait + ' ')
        if data:
            os.write(master, data)
        result = subprocess.run(['bash', '-c', function + '\nserial_gps_device\n'],
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=20)
        return result.returncode, result.stdout.strip(), os.ttyname(slave)

    def test_receiver_sending_nmea_is_named(self):
        status, output, port = self.probe(
            b'$GNGGA,001127.712,,,,,0,00,99.99,,M,,M,,*49\r\n'
            b'$GNRMC,001127.712,V,,,,,,,060180,,,N,V*27\r\n')
        self.assertEqual((status, output), (0, port))

    def test_idle_port_is_not_named(self):
        status, output, _ = self.probe(b'', wait='0.3')
        self.assertEqual((status, output), (1, ''))

    def test_console_text_is_not_a_receiver(self):
        status, output, _ = self.probe(b'mesh-5cc2 login: \r\n[ 1.0] $GPS boot\r\n',
                                       wait='0.3')
        self.assertEqual((status, output), (1, ''))

    def test_missing_port_is_not_named(self):
        source = RADIO_SETUP.read_text()
        function = re.search(r'^serial_gps_device\(\) \{\n.*?^\}', source,
                             re.M | re.S).group()
        function = function.replace('/dev/ttyAMA0', '/nonexistent/ttyAMA0')
        result = subprocess.run(['bash', '-c', function + '\nserial_gps_device\n'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout), (1, ''))

    def test_gpsd_config_uses_the_probe_result(self):
        source = RADIO_SETUP.read_text()
        self.assertIn('GPSD_DEVICES=$(serial_gps_device) || GPSD_DEVICES=""',
                      source)
        self.assertIn('DEVICES="$GPSD_DEVICES"\n', source)
        self.assertIn('USBAUTO="true"\n', source)


class FirstBootUartTests(unittest.TestCase):
    def test_cm4_boot_config_frees_the_uart_once(self):
        source = FIRSTRUN.read_text()
        line = next(line for line in source.splitlines()
                    if "'^# MANET CM4 serial GPS$'" in line)
        start = source.index('if [ "__HARDWARE_MODEL__" = "rpi4" ]; then\n'
                             '        # MANET CM4 has MMC')
        self.assertLess(start, source.index(line))
        self.assertTrue(line.strip().startswith('grep -q '))
        self.assertIn(r'[cm4]\nenable_uart=1\ndtoverlay=disable-bt\n'
                      r'dtoverlay=pps-gpio,gpiopin=18,pull=down\n[all]\n', line)

    def test_serial_console_is_removed_from_the_command_line(self):
        source = FIRSTRUN.read_text()
        command = re.search(r"sed -i -E '(s/[^']*console=[^']*)' \"\$_cmdline\"",
                            source).group(1)
        cases = {
            'console=serial0,115200 console=tty1 root=PARTUUID=1 rootwait\n':
                'console=tty1 root=PARTUUID=1 rootwait\n',
            'console=tty1 console=ttyAMA0,115200 rootwait\n':
                'console=tty1 rootwait\n',
            'console=tty1 root=PARTUUID=1 rootwait\n':
                'console=tty1 root=PARTUUID=1 rootwait\n',
        }
        for before, after in cases.items():
            with self.subTest(before=before):
                result = subprocess.run(['sed', '-E', command], input=before,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.stdout, after)


if __name__ == '__main__':
    unittest.main()
