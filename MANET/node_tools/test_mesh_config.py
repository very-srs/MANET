#!/usr/bin/env python3
"""EUD/AP settings stay on this node; mesh keys still go over Alfred."""

import unittest

import os
import base64
from pathlib import Path
import subprocess
import sys
import tempfile
import NodeInfo_pb2

from mesh_config import (
    LOCAL_KEYS,
    validate_config,
    apply_local_to_conf,
    local_changes,
    mesh_changes,
    strip_local_keys,
    submitted_changes,
)


class StripLocalKeysTests(unittest.TestCase):
    def test_drops_ap_and_eud_keeps_mesh(self):
        cfg = {
            'lan_ap_ssid': 'mesh-ap-6b28',
            'lan_ap_key': 'secret',
            'eud': 'wireless',
            'max_euds_per_node': '5',
            'mesh_ssid': 'MANET',
            'mtx': 'y',
        }
        self.assertEqual(
            strip_local_keys(cfg),
            {'mesh_ssid': 'MANET', 'mtx': 'y'},
        )

    def test_unknown_keys_are_left_for_the_validator(self):
        self.assertEqual(strip_local_keys({'bogus': 'x'}), {'bogus': 'x'})


class SplitConfigTests(unittest.TestCase):
    def test_local_keys_constant(self):
        self.assertEqual(
            LOCAL_KEYS,
            frozenset({'eud', 'lan_ap_ssid', 'lan_ap_key',
                       'max_euds_per_node', 'operator_name'}),
        )


class LocalChangesTests(unittest.TestCase):
    def test_operator_label_sets_clears_and_never_changes_peer_config(self):
        """Persist the optional label while leaving shared settings alone."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'mesh.conf')
            current = {'operator_name': 'Operator'}
            for name in ('Team Lead', ''):
                submitted = submitted_changes({'operator_name': name}, current)
                changed = local_changes(submitted, current)
                self.assertEqual(changed, {'operator_name': name})
                self.assertEqual(mesh_changes(submitted, current), {})
                self.assertEqual(strip_local_keys(submitted), {})
                apply_local_to_conf(changed, path)
                with open(path) as stream:
                    self.assertEqual(stream.read(), f'operator_name={name}\n')
                payload = subprocess.check_output([
                    sys.executable, '-B',
                    str(Path(__file__).with_name('encoder.py')), 'telemetry',
                    '--timestamp', '1', '--mesh-config', path], timeout=5)
                telemetry = NodeInfo_pb2.NodeTelemetry()
                telemetry.ParseFromString(base64.b64decode(payload))
                self.assertEqual(telemetry.atak_user, name)
                current = changed
            self.assertEqual(submitted_changes({'operator_name': ''}, {}), {})

    def test_operator_label_uses_utf8_byte_limit_and_local_validation(self):
        """Names remain bounded configuration data, never shared settings."""
        for name in ('', 'é' * 16, 'Team Lead <A>'):
            self.assertTrue(validate_config({'operator_name': name},
                                            local=True)[0])
            self.assertFalse(validate_config({'operator_name': name})[0])
        for name in ('é' * 17, 'x' * 33, 'Bad\nName', ' leading'):
            self.assertFalse(validate_config({'operator_name': name},
                                             local=True)[0])

    def test_only_values_that_differ_from_mesh_conf(self):
        current = {'lan_ap_key': 'oldpassword', 'eud': 'wired'}
        cfg = {
            'lan_ap_key': 'newpassword',
            'eud': 'wired',
            'mesh_ssid': 'MANET',
        }
        self.assertEqual(
            local_changes(cfg, current),
            {'lan_ap_key': 'newpassword'},
        )

    def test_empty_value_is_not_a_change(self):
        current = {'lan_ap_key': 'keep'}
        self.assertEqual(local_changes({'lan_ap_key': ''}, current), {})

    def test_max_euds_is_never_a_local_change(self):
        current = {'max_euds_per_node': '5'}
        self.assertEqual(
            local_changes({'max_euds_per_node': '12'}, current),
            {},
        )


class ValidatePackageTests(unittest.TestCase):
    def setUp(self):
        """Validation and future receiver calls must use a temporary node."""
        from test_config_sandbox import isolate_config
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        isolate_config(self, scratch.name)

    def test_mixed_package_is_accepted(self):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parent / 'mesh-config-sync.py'
        spec = importlib.util.spec_from_file_location('mesh_config_sync', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        pkg = {
            'kind': 'mesh_config',
            'version': 'aabbcc',
            'config': {
                'lan_ap_ssid': 'mesh-ap-6b28',
                'mesh_ssid': 'MANET',
            },
        }
        ok, why = mod.validate_package(pkg)
        self.assertTrue(ok, why)


class NetworkSizeTests(unittest.TestCase):
    def test_mesh_network_is_at_most_a_slash_16(self):
        self.assertTrue(validate_config({'ipv4_network': '10.30.0.0/16'})[0])
        for cidr in ('10.0.0.0/8', '10.30.0.0/15'):
            with self.subTest(cidr=cidr):
                self.assertFalse(validate_config({'ipv4_network': cidr})[0])


class MeshChangesTests(unittest.TestCase):
    def test_ignores_local_keys_and_unchanged_mesh(self):
        current = {'mesh_ssid': 'MANET', 'mtx': 'y', 'lan_ap_ssid': 'old'}
        cfg = {
            'lan_ap_ssid': 'new-ap',
            'mesh_ssid': 'MANET',
            'mtx': 'n',
        }
        self.assertEqual(mesh_changes(cfg, current), {'mtx': 'n'})


class ApplyLocalToConfTests(unittest.TestCase):
    def test_updates_existing_and_appends_missing(self):
        with tempfile.NamedTemporaryFile('w+', delete=False) as fh:
            fh.write('eud=wired\nmesh_ssid=MANET\n')
            path = fh.name
        try:
            applied = apply_local_to_conf(
                {'eud': 'wireless', 'lan_ap_key': 'newpassword'},
                path,
            )
            self.assertEqual(set(applied), {'eud', 'lan_ap_key'})
            with open(path) as fh:
                text = fh.read()
            self.assertIn('eud=wireless\n', text)
            self.assertIn('lan_ap_key=newpassword\n', text)
            self.assertIn('mesh_ssid=MANET\n', text)
        finally:
            os.unlink(path)

    def test_does_not_write_max_euds(self):
        with tempfile.NamedTemporaryFile('w+', delete=False) as fh:
            fh.write('max_euds_per_node=5\nmesh_ssid=MANET\n')
            path = fh.name
        try:
            applied = apply_local_to_conf({'max_euds_per_node': '12'}, path)
            self.assertEqual(applied, [])
            with open(path) as fh:
                self.assertEqual(fh.read(), 'max_euds_per_node=5\nmesh_ssid=MANET\n')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
