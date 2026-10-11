"""Run USB event handlers against an isolated node with recorded commands."""

from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parent

STUB = r'''#!/bin/bash
T="$REVIEW_ROOT"
name="$(basename "$0")"
printf '%s\n' "$name $*" >> "$T/calls"
case "$name" in
  systemctl)
    case "$1" in
      restart) touch "$T/active/$2" ;;
      stop) rm -f "$T/active/$2" ;;
      enable) touch "$T/enabled/$2" ;;
      disable) rm -f "$T/enabled/$2" ;;
      is-active)
        if [ -f "$T/active/$2" ]; then echo active; else echo inactive; exit 3; fi
        ;;
    esac ;;
  systemd-cat) cat >> "$T/journal" ;;
  iw)
    if [ -f "$T/active/usb-wifi-client@$2.service" ]; then
      echo "Connected to 02:00:00:00:00:01 (on $2)"
    fi ;;
  wpa_passphrase)
    printf 'network={\n    ssid="%s"\n    psk="test-psk"\n}\n' "$1" ;;
  manet-uplink-dispatch.sh)
    case "$1" in
      add) echo "$2" > "$T/var/run/gateway" ;;
      remove) rm -f "$T/var/run/gateway" ;;
    esac ;;
esac
exit 0
'''


class UsbHarness(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='manet-usb-')
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        for directory in (
                'bin', 'active', 'enabled', 'etc/manet', 'etc/wpa_supplicant',
                'etc/systemd/network', 'etc/systemd/system', 'var/run',
                'var/log', 'var/lib', 'sys/class/net', 'usr/local/bin'):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.env = dict(
            os.environ, REVIEW_ROOT=str(self.root),
            MANET_TOOLS_DIR=str(self.root),
            PATH=str(self.root / 'bin') + os.pathsep + os.environ['PATH'])
        for key in ('ACTION', 'INTERFACE', 'ID_BUS', 'ID_NET_DRIVER', 'DEVTYPE'):
            self.env.pop(key, None)
        for command in ('systemctl', 'systemd-cat', 'networkctl', 'ip', 'iw',
                        'sleep', 'wpa_passphrase', 'manet-uplink-dispatch.sh'):
            directory = ('usr/local/bin' if command.endswith('.sh') else 'bin')
            path = self.root / directory / command
            path.write_text(STUB)
            path.chmod(0o755)
        for library in ('manet-common.sh', 'manet-runtime-client.sh'):
            (self.root / library).write_text(
                self.isolated((TOOLS / library).read_text()))

    def isolated(self, source):
        # Redirect paths only; execute the real script and shared helpers.
        return re.sub(r'/(?:etc|var|sys|usr/local/bin)/',
                      lambda match: str(self.root) + match[0], source)

    def iface(self, name, bus='usb', driver='mt76x2u', wireless=True):
        path = self.root / 'sys/class/net' / name
        (path / 'device').mkdir(parents=True)
        (path / 'device/subsystem').symlink_to('/sys/bus/' + bus)
        (path / 'device/driver').symlink_to('/sys/bus/' + bus + '/drivers/' + driver)
        if wireless:
            (path / 'wireless').mkdir()
        (path / 'operstate').write_text('down\n')
        return path

    def run_script(self, name, *args, shell='bash', **environment):
        script = self.root / name
        script.write_text(self.isolated((TOOLS / name).read_text()))
        return subprocess.run(
            [shell, str(script), *args], env=dict(self.env, **environment),
            capture_output=True, text=True, timeout=10)

    def history(self):
        path = self.root / 'calls'
        return path.read_text().splitlines() if path.exists() else []


class UsbEthernetTests(UsbHarness):
    def test_removed_usb_device_cleans_up_without_sysfs(self):
        upstream = self.root / 'var/run/upstream_iface'
        for action in ('remove', 'offline'):
            with self.subTest(action=action):
                upstream.write_text('usb0\n')
                result = self.run_script(
                    'usb-ethernet-watch.sh', ACTION=action,
                    INTERFACE='usb0', ID_BUS='usb')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('manet-uplink-dispatch.sh remove usb0',
                              self.history())
                self.assertFalse(upstream.exists())

    def test_removing_standby_usb_preserves_other_upstream(self):
        upstream = self.root / 'var/run/upstream_iface'
        upstream.write_text('end0\n')
        result = self.run_script(
            'usb-ethernet-watch.sh', 'remove', 'usb0', ID_BUS='usb')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('manet-uplink-dispatch.sh remove usb0', self.history())
        self.assertEqual(upstream.read_text(), 'end0\n')

    def test_remove_ignores_non_usb_and_mesh_devices(self):
        for iface, bus in (('end0', 'pci'), ('usb0', ''),
                           ('wlan2', 'usb'), ('br0', 'usb'), ('bat0', 'usb')):
            with self.subTest(iface=iface, bus=bus):
                result = self.run_script(
                    'usb-ethernet-watch.sh', 'remove', iface, ID_BUS=bus)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c.startswith('manet-uplink-dispatch.sh')
                             for c in self.history()))

    def test_live_usb_detection_and_offline_still_work_without_udev(self):
        self.iface('usb0', wireless=False)
        for action, dispatch in (('add', 'add'), ('offline', 'remove')):
            result = self.run_script('usb-ethernet-watch.sh', action, 'usb0')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f'manet-uplink-dispatch.sh {dispatch} usb0',
                          self.history())
        config = self.root / 'etc/systemd/network/20-usb0.network'
        self.assertIn('Name=usb0\n', config.read_text())


class UsbWifiUplinkTests(UsbHarness):
    def setUp(self):
        super().setUp()
        self.iface('wlan5')
        self.service = 'usb-wifi-client@wlan5.service'
        self.wpa = self.root / 'etc/wpa_supplicant/wpa_supplicant-wlan5-uplink.conf'
        self.network = self.root / 'etc/systemd/network/20-wlan5-usb-wifi-uplink.network'

    def start_uplink(self):
        result = self.run_script(
            'usb-wifi-uplink.sh', 'set', 'test-hotspot', 'test-password', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'active' / self.service).exists())
        self.assertTrue((self.root / 'enabled' / self.service).exists())
        self.assertTrue(self.wpa.exists())
        self.assertTrue(self.network.exists())
        self.assertEqual((self.root / 'var/run/gateway').read_text(), 'wlan5\n')

    def assert_uplink_stopped(self):
        self.assertIn('systemctl stop ' + self.service, self.history())
        self.assertIn('systemctl disable ' + self.service, self.history())
        self.assertFalse((self.root / 'active' / self.service).exists())
        self.assertFalse((self.root / 'enabled' / self.service).exists())
        self.assertFalse(self.wpa.exists())
        self.assertFalse(self.network.exists())
        self.assertIn('manet-uplink-dispatch.sh remove wlan5', self.history())
        self.assertFalse((self.root / 'var/run/gateway').exists())

    def test_disabling_active_uplink_stops_it_and_reenable_connects(self):
        self.start_uplink()
        result = self.run_script(
            'usb-wifi-uplink.sh', 'set', 'test-hotspot', 'test-password', '0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_uplink_stopped()
        status = json.loads(result.stdout)
        self.assertFalse(status['enabled'])
        self.assertFalse(status['connected'])
        self.start_uplink()

    def test_reconcile_disabled_config_stops_active_uplink(self):
        self.start_uplink()
        (self.root / 'etc/manet/usb-wifi-uplink.conf').write_text(
            'enabled=0\nssid=test-hotspot\npassword=test-password\n')
        result = self.run_script('usb-wifi-uplink.sh', 'reconcile')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assert_uplink_stopped()


class UsbWifiRecoveryTests(UsbHarness):
    def setUp(self):
        super().setUp()
        self.halow = self.iface('wlan2', driver='morse_usb')
        self.restart = 'systemctl restart wpa_supplicant-s1g-wlan2.service'

    def recover(self, action, iface, **environment):
        result = self.run_script(
            'usb-wifi-halow-recovery.sh', action, iface, shell='sh',
            **environment)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ethernet_add_and_remove_do_not_restart_halow(self):
        adapter = self.iface('enx001122334455', driver='cdc_ether', wireless=False)
        self.recover('add', adapter.name, ID_BUS='usb', ID_NET_DRIVER='cdc_ether')
        shutil.rmtree(adapter)
        self.recover('remove', adapter.name, ID_BUS='usb', ID_NET_DRIVER='cdc_ether')
        self.assertNotIn(self.restart, self.history())
        self.assertNotIn('sleep 8', self.history())

    def test_wireless_name_alone_does_not_schedule_recovery(self):
        self.iface('wlan5', driver='cdc_ether', wireless=False)
        self.recover('add', 'wlan5', ID_BUS='usb', ID_NET_DRIVER='cdc_ether')
        self.assertNotIn(self.restart, self.history())

    def test_usb_wifi_add_and_removed_sysfs_still_restart_halow(self):
        adapter = self.iface('wlx001122334455')
        self.recover('add', adapter.name, ID_BUS='usb', DEVTYPE='wlan',
                     ID_NET_DRIVER='mt76x2u')
        self.assertEqual(self.history().count(self.restart), 1)
        shutil.rmtree(adapter)
        self.recover('remove', adapter.name, ID_BUS='usb', DEVTYPE='wlan',
                     ID_NET_DRIVER='mt76x2u')
        self.assertEqual(self.history().count(self.restart), 2)

    def test_live_usb_wifi_can_be_identified_without_udev(self):
        self.iface('wlan5')
        self.recover('add', 'wlan5')
        self.assertIn(self.restart, self.history())

    def test_pci_wifi_does_not_restart_halow(self):
        self.iface('wlan5', bus='pci', driver='ath9k')
        self.recover('add', 'wlan5', DEVTYPE='wlan', ID_BUS='pci',
                     ID_NET_DRIVER='ath9k')
        self.assertNotIn(self.restart, self.history())

    def test_morse_adapter_is_excluded_even_before_rename(self):
        self.iface('wlan5', driver='morse_usb')
        for action, iface in (('add', 'wlan2'), ('add', 'wlan5'),
                              ('remove', 'wlan6')):
            with self.subTest(action=action, iface=iface):
                self.recover(action, iface, ID_BUS='usb', DEVTYPE='wlan',
                             ID_NET_DRIVER='morse_usb')
                self.assertNotIn(self.restart, self.history())

    def test_live_morse_adapter_is_excluded_without_udev(self):
        self.iface('wlan5', driver='morse_usb')
        self.recover('add', 'wlan5')
        self.assertNotIn(self.restart, self.history())

    def test_add_skips_recovered_halow_and_missing_halow(self):
        self.iface('wlan5')
        (self.halow / 'operstate').write_text('up\n')
        self.recover('add', 'wlan5', ID_BUS='usb', DEVTYPE='wlan')
        self.assertNotIn(self.restart, self.history())
        shutil.rmtree(self.halow)
        self.recover('remove', 'wlan5', ID_BUS='usb', DEVTYPE='wlan')
        self.assertNotIn(self.restart, self.history())

    def test_udev_rules_match_only_non_morse_usb_wireless_events(self):
        rules = (TOOLS.parent / 'udev/rules.d/81-usb-wifi-halow-recovery.rules')
        for rule in rules.read_text().splitlines():
            if not rule.startswith('ACTION=='):
                continue
            conditions = re.findall(
                r'(ACTION|SUBSYSTEM|ENV\{\w+\})(==|!=)"([^"]*)"', rule)
            for action in ('add', 'remove'):
                for bus, driver, devtype, eligible in (
                        ('usb', 'mt76x2u', 'wlan', True),
                        ('usb', 'cdc_ether', '', False),
                        ('usb', 'morse_usb', 'wlan', False),
                        ('pci', 'ath9k', 'wlan', False)):
                    with self.subTest(action=action, bus=bus, driver=driver):
                        event = {
                            'ACTION': action, 'SUBSYSTEM': 'net',
                            'ENV{ID_BUS}': bus, 'ENV{ID_NET_DRIVER}': driver,
                            'ENV{DEVTYPE}': devtype,
                        }
                        matches = all(
                            fnmatchcase(event[key], value) == (op == '==')
                            for key, op, value in conditions)
                        expected = eligible and f'ACTION=="{action}"' in rule
                        self.assertEqual(matches, expected, rule)


if __name__ == '__main__':
    unittest.main()
