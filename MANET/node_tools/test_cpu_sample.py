"""CPU measurement must count service children and reject invalid comparisons."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('cpu_sample', Path(__file__).with_name('manet-cpu-sample.py'))
sample = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sample)


class CpuSampleTests(unittest.TestCase):
    def test_cgroup_measurement_counts_children_and_reports_cpu_per_minute(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            cpu = root / 'sys/fs/cgroup/system.slice/manet-atak.service/cpu.stat'
            cpu.parent.mkdir(parents=True)
            cpu.write_text('usage_usec 4312500\nuser_usec 2000000\nsystem_usec 2312500\n')
            (root / 'proc').mkdir()
            (root / 'proc/stat').write_text('cpu 100 0 50 1000 5 0 0 0 40 0\n')
            props = 'ControlGroup=/system.slice/manet-atak.service\nInvocationID=one\nActiveState=active\n'
            with patch.object(sample.subprocess, 'check_output', return_value=props), \
                    patch.object(sample.time, 'monotonic', return_value=60), \
                    patch.object(sample.os, 'sysconf', return_value=100):
                after = sample.snapshot('manet-atak.service', root)
            self.assertEqual(after['cpu'], 4.3125)
            self.assertEqual(after['system_busy'], 1.5)  # guest not double-counted
            before = dict(after, cpu=0, system_busy=0, at=0)
            result = sample.result(before, after, 'before')
            self.assertEqual(result['service_cpu_s_per_min'], 4.3125)
            self.assertEqual(result['service_percent_one_core'], 7.188)

    def test_restart_reset_and_nonpositive_elapsed_are_rejected(self):
        before = dict(invocation='one', cgroup='/one', cpu=10, system_busy=100, at=50)
        for update in ({'invocation': 'two'}, {'cgroup': '/two'}, {'cpu': 0},
                       {'system_busy': 0}, {'at': 50}):
            after = dict(before, cpu=11, system_busy=101, at=110)
            after.update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                sample.result(before, after, 'after')

    def test_inactive_or_root_cgroup_cannot_report_false_service_savings(self):
        for props in ('ActiveState=inactive\n',
                      'ActiveState=active\nInvocationID=one\nControlGroup=/\n'):
            with patch.object(sample.subprocess, 'check_output', return_value=props), \
                    self.assertRaises(ValueError):
                sample.snapshot('manet-atak.service')


if __name__ == '__main__':
    unittest.main()
