"""Boot-time radio name exchanges must tolerate driver enumeration order."""
import importlib.util
from itertools import permutations
from pathlib import Path
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('radio_names', TOOLS / 'manet-radio-names.py')
names = importlib.util.module_from_spec(spec)
spec.loader.exec_module(names)


class RadioNameTests(unittest.TestCase):
    def links(self, order):
        return [{'ifindex': n + 3, 'ifname': f'wlan{n}',
                 'address': f'00:0a:52:0d:d1:{mac:02x}', 'flags': ['BROADCAST']}
                for n, mac in enumerate(order)]

    def pins(self):
        return {f'00:0a:52:0d:d1:{n:02x}': f'wlan{n}' for n in range(3)}

    def test_every_driver_enumeration_order_reaches_the_pinned_layout(self):
        for order in permutations(range(3)):
            with self.subTest(order=order):
                links = self.links(order)
                current = {link['ifname']: link['address'] for link in links}
                commands = []

                def run(argv, **kwargs):
                    commands.append(argv)
                    if argv[0] == 'ip':
                        source, target = argv[4], argv[6]
                        self.assertNotIn(target, current, 'kernel would reject an occupied name')
                        current[target] = current.pop(source)
                    else:
                        self.assertEqual(current, {v: k for k, v in self.pins().items()})

                names.reconcile(self.pins(), links, run)
                self.assertEqual(current, {v: k for k, v in self.pins().items()})
                self.assertEqual(any(cmd[:2] == ['udevadm', 'trigger'] for cmd in commands),
                                 order != (0, 1, 2))

    def test_missing_radio_does_not_prevent_other_radios_from_being_named(self):
        links = self.links((1, 0))
        self.assertEqual(len(names.rename_plan(self.pins(), links)), 2)

    def test_unpinned_occupant_is_not_displaced(self):
        links = self.links((1, 9))
        with self.assertRaisesRegex(ValueError, 'unpinned'):
            names.rename_plan(self.pins(), links)

    def test_active_or_enslaved_radio_is_not_changed(self):
        for extra in ({'flags': ['UP']}, {'master': 'bat0'}):
            links = self.links((1, 0, 2))
            links[1].update(extra)
            commands = []
            with self.assertRaisesRegex(ValueError, 'active'):
                names.reconcile(self.pins(), links, lambda argv, **kw: commands.append(argv))
            self.assertFalse(commands)

    def test_occupied_temporary_name_is_not_reused(self):
        links = self.links((1, 0, 2)) + [{'ifname': 'mnr3', 'address': '00:01:02:03:04:05'}]
        with self.assertRaisesRegex(ValueError, 'Temporary'):
            names.rename_plan(self.pins(), links)

    def test_failed_move_does_not_continue_or_trigger_udev(self):
        commands = []

        def fail(argv, **kwargs):
            commands.append(argv)
            raise subprocess.CalledProcessError(2, argv)

        with self.assertRaises(subprocess.CalledProcessError):
            names.reconcile(self.pins(), self.links((1, 0, 2)), fail)
        self.assertEqual(len(commands), 1)

    def test_only_valid_mac_pins_are_loaded_and_first_boot_is_a_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(names.read_pins(root), {})
            pin = root / '10-wlan0.link'
            pin.write_text('[Match]\nMACAddress=00:0A:52:0D:D1:00\nType=wlan\n'
                           '[Link]\nName=wlan0\n')
            self.assertEqual(names.read_pins(root), {'00:0a:52:0d:d1:00': 'wlan0'})
            for bad in ('eth0', 'wlan1'):
                pin.write_text('[Match]\nMACAddress=00:0a:52:0d:d1:00\nType=wlan\n'
                               f'[Link]\nName={bad}\n')
                with self.assertRaises(ValueError):
                    names.read_pins(root)


if __name__ == '__main__':
    unittest.main()
