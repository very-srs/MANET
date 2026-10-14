"""Isolate receiver/applier fixtures and reject access to live config state."""

import builtins
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


LIVE_PATHS = ('/var/lib/manet-config', '/var/lib/manet-config-rollback',
              '/var/run', '/run')


def check_path(path):
    """Reject live runtime/config paths, including paths through symlinks."""
    if isinstance(path, int):
        return
    path = os.path.abspath(os.fsdecode(path))
    for candidate in (path, os.path.realpath(path)):
        if any(candidate == root or candidate.startswith(root + '/')
               for root in LIVE_PATHS):
            raise AssertionError('Test attempted live config access: '
                                 + candidate)


def isolate_config(test, root, env=None):
    """Install temporary defaults and guards for a fixture and its children."""
    root = Path(root)
    env = {} if env is None else env
    defaults = {
        'MANET_CONFIG_STATE_DIR': root / 'journal',
        'MANET_RUN_DIR': root / 'run',
        'MANET_ROLLBACK_DIR': root / 'rollback',
        'MANET_ADMIN_STATE_DIR': root / 'admin',
        'MANET_MESH_CONF': root / 'mesh.conf',
        'MANET_WPA_DIR': root / 'wpa',
        'MANET_CONFIG_LOG': root / 'config.log',
    }
    for key, value in defaults.items():
        env.setdefault(key, str(value))
    environment = patch.dict(os.environ, env)
    environment.start()
    test.addCleanup(environment.stop)

    def guarded_open(original):
        """Check before any Python file descriptor is opened."""
        def opened(path, *args, **kwargs):
            check_path(path)
            return original(path, *args, **kwargs)
        return opened

    for module, name in ((builtins, 'open'), (io, 'open'), (os, 'open'),
                         (os, 'stat'), (os, 'mkdir'), (os, 'chmod'),
                         (Path, 'open'), (Path, 'stat')):
        guard = patch.object(module, name, guarded_open(getattr(module, name)))
        guard.start()
        test.addCleanup(guard.stop)
    original_popen = subprocess.Popen

    def child(*args, **kwargs):
        """Require child processes to inherit every isolated state path."""
        child_env = kwargs.get('env')
        child_env = os.environ if child_env is None else child_env
        for key in defaults:
            if key not in child_env:
                raise AssertionError('Missing child config sandbox: ' + key)
            check_path(child_env[key])
        return original_popen(*args, **kwargs)

    guard = patch.object(subprocess, 'Popen', child)
    guard.start()
    test.addCleanup(guard.stop)
    return env


class ConfigSandboxTests(unittest.TestCase):
    def test_live_paths_fail_before_open_and_children_require_overrides(self):
        """The guard itself must catch an accidental default-path regression."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        isolate_config(self, scratch.name)
        for root in LIVE_PATHS:
            for opener in (builtins.open, io.open, os.open):
                with self.subTest(root=root, opener=opener):
                    with self.assertRaisesRegex(AssertionError, 'live config'):
                        opener(root + '/test-must-never-open', 0)
        with self.assertRaisesRegex(AssertionError, 'Missing child'):
            subprocess.run(['true'], env={})
        link = Path(scratch.name) / 'alias'
        link.symlink_to('/var/run')
        with self.assertRaisesRegex(AssertionError, 'live config'):
            (link / 'test-must-never-open').read_text()
