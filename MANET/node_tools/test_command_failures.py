"""Failed control commands must not be reported as successful actions."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import manet_radio

TOOLS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    'battery_reader', TOOLS / 'battery-reader.py')
battery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(battery)


class CommandFailureTests(unittest.TestCase):
    def test_poweroff_failure_is_retried_until_accepted(self):
        data = dict(percentage=1, voltage_v=12, current_ma=-100, power_w=0,
                    status='discharging', charging=False, cell_mv=[3100] * 4)
        replies = [subprocess.CompletedProcess([], code) for code in (1, 0)]
        with patch.object(battery, 'open_bus'), \
                patch.object(battery, 'read_battery', return_value=data), \
                patch.object(battery, 'write_atomic'), \
                patch.object(battery.subprocess, 'run',
                             side_effect=replies) as poweroff, \
                patch.object(battery.time, 'sleep',
                             side_effect=[None, None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                battery.main()
        self.assertEqual(poweroff.call_count, 2)
        self.assertEqual(poweroff.call_args.args[0], ['systemctl', 'poweroff'])

    def test_failed_halow_restart_cannot_pass_using_the_old_process(self):
        for restore_status in (0, 1):
            with self.subTest(restore_status=restore_status), \
                    tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                conf = root / 'wpa.conf'
                old = ('channel=1\nop_class=68\ns1g_prim_chwidth=0\n'
                       's1g_prim_1mhz_chan_index=0\n')
                conf.write_text(old)
                replies = [subprocess.CompletedProcess([], code)
                           for code in (1, 1, restore_status)]
                with patch.object(manet_radio, 'halow_region',
                                  return_value='US'), \
                        patch.object(manet_radio, 'HALOW_OVERRIDE_FILE',
                                     str(root / 'override')), \
                        patch.object(manet_radio, 'HALOW_WPA_CONF',
                                     str(conf)), \
                        patch.object(manet_radio.subprocess, 'run',
                                     side_effect=replies), \
                        patch.object(manet_radio, '_s1g_supplicant_healthy',
                                     return_value=True) as healthy:
                    result = manet_radio.apply_halow_channel(10, '2MHz')
                self.assertFalse(result['ok'])
                self.assertEqual(conf.read_text(), old)
                healthy.assert_not_called()
                if restore_status:
                    self.assertIn('restart also failed', result['error'])


if __name__ == '__main__':
    unittest.main()
