"""Pin shared helper contracts at their runtime callers."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import manet_manage as manage

TOOLS = Path(__file__).resolve().parent


def load_script(name):
    """Import a flat installed script without starting its CLI."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


status = load_script('mesh-status')


class AssetHelperTests(unittest.TestCase):
    """Keep both web route implementations on the same asset contract."""

    def test_asset_paths_and_types(self):
        """Pin complete lookup results, including rejected paths."""
        urls = ['/assets/fer-logo', '/assets/fer-logo-white.svg',
                '/assets/fer-logo-black.png', '/assets/fer-logo.jpeg',
                '/assets/../fer-logo', '/manage/assets/fer-logo',
                '/assets/unknown', '/assets/fer-logo.PNG']
        black = '/usr/local/share/manet/fer-logo-black.png'
        white = '/usr/local/share/manet/fer-logo-white.png'
        for module in (manage, status):
            self.assertEqual([module.logo_asset_file(p) for p in urls],
                             [black, white, black, black, None, None,
                              None, black])
            self.assertEqual(
                [module.asset_content_type(p) for p in
                 ('x.svg', 'x.PNG', 'x.jpg', 'x.jpeg', 'x.webp', 'x', 'x.gif')],
                ['image/svg+xml', 'image/png', 'image/jpeg', 'image/jpeg',
                 'image/webp', 'application/octet-stream',
                 'application/octet-stream'])

    def test_asset_cache_token(self):
        """Pin metadata ordering and the missing-asset fallback."""
        for module in (manage, status):
            with patch('os.stat', side_effect=[
                    SimpleNamespace(st_mtime_ns=123, st_size=45), OSError()]):
                self.assertEqual(module.logo_asset_token(), '314886b9')
            with patch('os.stat', side_effect=OSError()):
                self.assertEqual(module.logo_asset_token(), '90c4715d')


class MotdHelperTests(unittest.TestCase):
    """Exercise the shared separator through actual symlink launch paths."""

    def test_motd_symlink_output(self):
        """Pin the full banner when provisioning has just begun."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state'
            state.write_text('STATE=running\nPHASE=radio-setup\n')
            link = root / '50-manet-provision'
            link.symlink_to(TOOLS / 'manet-provision-status.sh')
            result = subprocess.run(
                ['bash', str(link)], capture_output=True, text=True,
                env={'PATH': '/usr/bin:/bin',
                     'MANET_PROVISION_STATE': str(state)}, timeout=5)
            rule = '  ' + '─' * 60 + '\n'
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout,
                             '\n' + rule +
                             '  MANET PROVISIONING IN PROGRESS: '
                             'do not disconnect\n' + rule +
                             '  Stage    : radio-setup\n'
                             '  Running  : unknown\n'
                             '  Keep Ethernet and power connected. '
                             'The node reboots itself\n'
                             '  several times; this can take about '
                             'ten minutes.\n'
                             '  Watch    : sudo tail -f '
                             '/var/log/radio-setup.log\n' + rule + '\n')
            self.assertEqual(result.stderr, '')

    def test_power_symlink_output(self):
        """Keep JSON and the complete power warning behind the MOTD link."""
        import os
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe = root / 'vcgencmd'
            probe.write_text('#!/bin/sh\nprintf "throttled=0x1\\n"\n')
            probe.chmod(0o755)
            link = root / '55-manet-power'
            link.symlink_to(TOOLS / 'manet-power-status.sh')
            env = dict(os.environ, PATH=str(root) + ':/usr/bin:/bin')
            result = subprocess.run(['bash', str(link), '--json'], env=env,
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout,
                             '{"available": true, "raw": "0x1", '
                             '"state": "critical", '
                             '"undervoltage_now": true, '
                             '"undervoltage_ever": false, '
                             '"throttled_now": false, '
                             '"throttled_ever": false, '
                             '"capped_now": false, "capped_ever": false, '
                             '"soft_temp_now": false, '
                             '"soft_temp_ever": false}\n')
            result = subprocess.run(['bash', str(link)], env=env,
                                    text=True, capture_output=True, timeout=5)
            rule = '  ' + '─' * 60 + '\n'
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, '')
            self.assertEqual(result.stdout,
                             '\n' + rule +
                             '  ** UNDER-VOLTAGE RIGHT NOW: THIS BOARD '
                             'IS NOT GETTING ENOUGH POWER **\n' + rule +
                             '  Under-voltage : NOW\n'
                             '  Raw           : 0x1\n\n'
                             '  Expect radios to misbehave in ways '
                             'that look like driver faults:\n'
                             '  a HaLow card that stops answering, '
                             'a Wi-Fi interface that will\n'
                             '  not associate, or a board that resets '
                             'with nothing in the log.\n'
                             '  Check the PSU and the cable before '
                             'suspecting the software.\n' + rule + '\n')


class RegistryHelperTests(unittest.TestCase):
    """Pin web registry parsing and tolerant JSON state readers."""

    def test_registry_at_both_callers(self):
        """Keep casing, duplicate assignment and quoted-value behavior."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'registry'
            path.write_text(
                "NODE_Aa12_HOSTNAME='first'\n"
                "NODE_Aa12_HOSTNAME='last'\n"
                "NODE_Aa12_DATA='a=b'\nNODE_ff00_EMPTY=''\n"
                "NODE_badZ_NAME='ignored'\nNODE_Aa12_lower='ignored'\n"
                "NODE_1234_NAME='can'\\''t'\n")
            expected = {
                'Aa12': {'id': 'Aa12', 'HOSTNAME': 'last', 'DATA': 'a=b'},
                'ff00': {'id': 'ff00', 'EMPTY': ''},
                '1234': {'id': '1234', 'NAME': 'can'},
            }
            for module in (manage, status):
                with patch.object(module, 'REGISTRY_FILE', str(path)):
                    self.assertEqual(
                        module.parse_registry(module.REGISTRY_FILE), expected)
            path.unlink()
            for module in (manage, status):
                with patch.object(module, 'REGISTRY_FILE', str(path)):
                    self.assertEqual(
                        module.parse_registry(module.REGISTRY_FILE), {})

    def test_dict_json_readers(self):
        """Keep every JSON shape and read failure at both users."""
        import manet_recovery_status as recovery
        import manet_registry_builder as builder
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state'
            for module in (recovery, builder):
                for raw, expected in (
                        ('{"x": [1, null], "ok": true}',
                         {'x': [1, None], 'ok': True}),
                        ('[]', {}), ('null', {}), ('1', {}),
                        ('"text"', {}), ('invalid', {}), ('', {})):
                    path.write_text(raw)
                    self.assertEqual(module.read_json(path), expected)
                path.unlink()
                self.assertEqual(module.read_json(path), {})
                self.assertEqual(module.read_json(Path(directory)), {})


if __name__ == '__main__':
    unittest.main()
