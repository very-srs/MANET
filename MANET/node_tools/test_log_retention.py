"""Exercise the shipped rotation policy against disposable real log files."""

import grp
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import unittest


class LogRetentionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('logrotate'), 'logrotate is required')
    def test_rotation_bounds_archives_and_keeps_open_append_writer_working(self):
        policy = (Path(__file__).parents[1] /
                  'share/manet/logrotate.conf').read_text()
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            logs = root / 'logs'
            logs.mkdir()
            user = pwd.getpwuid(os.getuid()).pw_name
            group = grp.getgrgid(os.getgid()).gr_name
            policy = policy.replace('/var/log/', str(logs) + '/')
            policy = policy.replace('su root root', f'su {user} {group}')
            config = root / 'rotate.conf'
            config.write_text(policy)
            log = logs / 'mesh-config-sync.log'
            unrelated = logs / 'operator-service.log'
            unrelated.write_bytes(b'keep' * 1500000)
            with log.open('ab', buffering=0) as writer:
                for number in range(5):
                    writer.write(b'x' * (5 * 1024 * 1024 + 1))
                    result = subprocess.run([
                        'logrotate', '--state', str(root / 'state'), str(config)
                    ], capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(log.stat().st_size, 0)
                    writer.write(b'writer still works\n')
                    self.assertEqual(log.read_bytes(), b'writer still works\n')
                    self.assertLessEqual(len(list(logs.glob(log.name + '.*'))),
                                         3)
            self.assertEqual(unrelated.stat().st_size, 6000000)
            self.assertTrue((logs / (log.name + '.3.gz')).exists())


if __name__ == '__main__':
    unittest.main()
