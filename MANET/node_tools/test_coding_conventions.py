"""Check node shell interpreters, service sandbox baselines and manuals."""
import configparser
from pathlib import Path
import shutil
import subprocess
import unittest

MANET = Path(__file__).resolve().parent.parent
MAN_COMMANDS = (
    'radio-setup.sh', 'node-update.sh', 'ethernet-autodetect.sh',
    'usb-wifi-uplink.sh', 'manet-user-scripts.sh', 'manet-provision-status.sh',
    'manet-power-status.sh', 'verify-bridge.sh', 'manet-region.py',
    'manet-os-cleanup.py', 'manet-mesh-census.py', 'manet-cpu-sample.py',
    'mesh-neighbor-count.py', 'halow-mcs-summary.py', 'mac-to-ip.sh',
)
HARDENED = (
    'mesh-status.service.d/20-hardening.conf',
    'mesh-voice.service', 'mesh-channel-agreement.service',
    'gps-reader.service', 'battery-reader.service', 'button-monitor.service',
    'led-boot.service', 'manet-led-status.service', 'sae-watchdog.service',
    'batman-enslave-watch.service', 'ap-txpower.service',
    'manet-mesh-power.service', 'manet-dhcp-isolation.service',
)


class CodingConventionTests(unittest.TestCase):
    def test_operator_commands_have_manual_pages(self):
        for name in MAN_COMMANDS:
            with self.subTest(command=name):
                self.assertTrue((MANET / 'node_tools' / name).is_file())
                page = MANET / 'man' / 'man8' / (name + '.8')
                self.assertTrue(page.is_file(), str(page))
                source = page.read_text()
                self.assertIn(f'.Dt {name.upper()} 8\n', source)
                self.assertIn(f'.Nm {name}\n', source)
                for section in ('NAME', 'SYNOPSIS', 'DESCRIPTION',
                                'EXIT STATUS'):
                    self.assertIn(f'.Sh {section}\n', source)

    def test_manual_pages_parse(self):
        if shutil.which('mandoc'):
            command = ['mandoc', '-T', 'lint']
        elif shutil.which('groff'):
            command = ['groff', '-mandoc', '-ww', '-z']
        else:
            self.skipTest('manual-page lint requires mandoc or groff')
        pages = sorted((MANET / 'man').rglob('*.8'))
        self.assertTrue(pages, 'no section 8 manual pages found')
        for page in pages:
            with self.subTest(page=page.relative_to(MANET)):
                result = subprocess.run(
                    command + [str(page)], capture_output=True,
                    text=True, timeout=10)
                self.assertEqual(result.returncode, 0,
                                 result.stdout + result.stderr)
                self.assertEqual(result.stderr, '')

    def test_node_shell_interpreters(self):
        for directory in ('node_tools', 'networkd-dispatcher', 'udev'):
            for path in sorted((MANET / directory).rglob('*')):
                if not path.is_file():
                    continue
                with path.open('rb') as stream:
                    first = stream.readline().rstrip(b'\r\n')
                shell = (path.suffix == '.sh' or
                         first in (b'#!/bin/sh', b'#!/bin/bash') or
                         first.startswith(b'#!/usr/bin/env bash') or
                         first.startswith(b'#!/usr/bin/env sh'))
                if not shell:
                    continue
                with self.subTest(path=path.relative_to(MANET)):
                    self.assertIn(first, (b'#!/bin/bash', b'#!/bin/sh'))
                    if first == b'#!/bin/sh':
                        result = subprocess.run(
                            ['dash', '-n', str(path)], capture_output=True,
                            text=True, timeout=5)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        if shutil.which('checkbashisms'):
                            result = subprocess.run(
                                ['checkbashisms', str(path)],
                                capture_output=True, text=True, timeout=5)
                            self.assertEqual(result.returncode, 0,
                                             result.stderr)

    def test_hardened_services_keep_the_baseline(self):
        for name in HARDENED:
            with self.subTest(unit=name):
                config = configparser.ConfigParser(
                    strict=False, interpolation=None)
                with (MANET / 'systemd' / name).open() as stream:
                    config.read_file(stream)
                self.assertEqual(config['Service']['NoNewPrivileges'], 'yes')
                self.assertEqual(config['Service']['ProtectSystem'], 'strict')


if __name__ == '__main__':
    unittest.main()
