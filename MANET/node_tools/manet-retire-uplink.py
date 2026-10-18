#!/usr/bin/env python3
"""Retire external Wi-Fi client artifacts without changing current radio roles."""

import argparse
import os
from pathlib import Path
import re
import subprocess

from manet_config_io import atomic_write


class Retirement:
    """Discover explicit retired ownership before removing any per-radio files."""

    def __init__(self, root=Path('/')):
        self.root = Path(root)

    def run(self, *args):
        """Fail the migration on command errors so the update can retry."""
        return subprocess.run(args, check=True, capture_output=True, text=True,
                              timeout=30).stdout

    def path(self, name):
        """Resolve root-relative paths for installation and isolated validation."""
        return self.root / name

    def protected(self, iface):
        """Keep built-ins, discovered mesh/AP roles and every Morse device intact."""
        if not re.fullmatch(r'wlan\d+', iface) or iface in ('wlan0', 'wlan1', 'wlan2', 'wlan3'):
            return True
        for role in ('mesh_if', 'halow_if', 'no_mesh_if', 'wifi_uplink_if'):
            path = self.path('var/lib/' + role)
            if path.exists() and iface in path.read_text().split():
                return True
        driver = self.path('sys/class/net/' + iface + '/device/driver').resolve().name
        return driver.startswith('morse') or driver == 'brcmfmac'

    def apply(self):
        """Stop only retired clients, remove their state, and reload changed rules."""
        systemd = self.path('etc/systemd/system')
        units = set()
        for verb in ('list-units', 'list-unit-files'):
            try:
                output = self.run('systemctl', verb, '--all', '--no-legend', '--no-pager',
                                  'usb-wifi-client@*.service', 'wpa_supplicant@*-uplink.service')
            except subprocess.CalledProcessError as error:
                # list-unit-files exits 1 when no pattern matches anything.
                if error.returncode != 1 or (error.stdout or '').strip():
                    raise
                output = ''
            units.update(line.split()[0] for line in output.splitlines() if line.split())
        unit_paths = list(systemd.glob('**/usb-wifi-client@*.service'))
        unit_paths += list(systemd.glob('**/wpa_supplicant@*-uplink.service'))
        units.update(path.name for path in unit_paths)
        clients = {}
        for unit in units:
            match = re.fullmatch(r'(?:usb-wifi-client@(wlan\d+)|wpa_supplicant@(wlan\d+)-uplink)\.service', unit)
            if match:
                iface = match[1] or match[2]
                if not self.protected(iface):
                    clients[unit] = iface
        networks = list(self.path('etc/systemd/network').glob('20-*-usb-wifi-uplink.network'))
        ifaces = set(clients.values())
        for path in networks:
            iface = path.name.removeprefix('20-').removesuffix('-usb-wifi-uplink.network')
            if not self.protected(iface):
                ifaces.add(iface)
        # An unplugged adapter can leave only its old supplicant file behind.
        for path in self.path('etc/wpa_supplicant').glob('wpa_supplicant-*-uplink.conf'):
            iface = path.name.removeprefix('wpa_supplicant-').removesuffix('-uplink.conf')
            if self.protected(iface):
                continue
            device = self.path('sys/class/net/' + iface)
            if not device.exists() or (device / 'device/subsystem').resolve().name == 'usb':
                ifaces.add(iface)
        for unit in sorted(clients):
            self.run('systemctl', 'disable', '--now', unit)
        pending = self.path('var/lib/manet-retire-uplink/reload-pending')
        # Keep reload obligations durable across command failures or power loss.
        rule = self.path('etc/udev/rules.d/81-usb-wifi-halow-recovery.rules')
        if rule.exists() or any(not self.protected(
                path.name.removeprefix('20-').removesuffix('-usb-wifi-uplink.network'))
                for path in networks):
            pending.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(pending, 'reload\n', mode=0o600)
        for iface in sorted(ifaces):
            self.path(f'etc/wpa_supplicant/wpa_supplicant-{iface}-uplink.conf').unlink(missing_ok=True)
            path = self.path(f'etc/systemd/network/20-{iface}-usb-wifi-uplink.network')
            if path.exists():
                path.unlink()
        for path in unit_paths:
            if path.name in clients or path.name == 'usb-wifi-client@.service':
                path.unlink(missing_ok=True)
        for name in (
                'etc/manet/usb-wifi-uplink.conf',
                'run/manet-usb-wifi-scan.json',
                'usr/local/bin/usb-wifi-uplink.sh',
                'usr/local/bin/usb-wifi-halow-recovery.sh',
                'usr/local/share/man/man8/usb-wifi-uplink.sh.8',
                'usr/local/share/man/man8/usb-wifi-uplink.sh.8.gz'):
            self.path(name).unlink(missing_ok=True)
        rule.unlink(missing_ok=True)
        if pending.exists():
            self.run('udevadm', 'control', '--reload-rules')
            self.run('networkctl', 'reload')
            pending.unlink()
        # This one-shot dependency also runs when the installing updater was
        # loaded from the preceding release. Remove it only after success.
        # Drop the requirement durably before the unit it names, so a power
        # cut between the two never leaves the uplink requiring a lost unit.
        dropin = self.path('etc/systemd/system/manet-wifi-uplink.service.d/retire-uplink.conf')
        dropin.unlink(missing_ok=True)
        fd = os.open(dropin.parent, os.O_RDONLY) if dropin.parent.is_dir() else None
        if fd is not None:
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        self.path('etc/systemd/system/manet-retire-uplink.service').unlink(missing_ok=True)
        self.run('systemctl', 'daemon-reload')


def main():
    """Run the updater's idempotent retirement hook."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/'))
    Retirement(parser.parse_args().root).apply()


if __name__ == '__main__':
    main()
