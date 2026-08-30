#!/usr/bin/env python3
"""EUD/AP settings stay on this node; mesh keys still go over Alfred."""

import unittest

import os
import tempfile

from mesh_config import (
    LOCAL_KEYS,
    apply_local_to_conf,
    local_changes,
    mesh_changes,
    split_config,
    strip_local_keys,
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
    def test_partition(self):
        local, mesh = split_config({
            'eud': 'wired',
            'mesh_ssid': 'MANET',
            'lan_ap_ssid': 'ap',
        })
        self.assertEqual(local, {'eud': 'wired', 'lan_ap_ssid': 'ap'})
        self.assertEqual(mesh, {'mesh_ssid': 'MANET'})

    def test_local_keys_constant(self):
        self.assertEqual(
            LOCAL_KEYS,
            frozenset({'eud', 'lan_ap_ssid', 'lan_ap_key', 'max_euds_per_node'}),
        )


class LocalChangesTests(unittest.TestCase):
    def test_only_values_that_differ_from_mesh_conf(self):
        current = {'lan_ap_ssid': 'mesh-ap-6b28', 'eud': 'wired'}
        cfg = {
            'lan_ap_ssid': 'mesh-ap-new',
            'eud': 'wired',
            'lan_ap_key': '',
            'mesh_ssid': 'MANET',
        }
        self.assertEqual(
            local_changes(cfg, current),
            {'lan_ap_ssid': 'mesh-ap-new'},
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
                {'eud': 'wireless', 'lan_ap_ssid': 'mesh-ap-6b28'},
                path,
            )
            self.assertEqual(set(applied), {'eud', 'lan_ap_ssid'})
            with open(path) as fh:
                text = fh.read()
            self.assertIn('eud=wireless\n', text)
            self.assertIn('lan_ap_ssid=mesh-ap-6b28\n', text)
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
