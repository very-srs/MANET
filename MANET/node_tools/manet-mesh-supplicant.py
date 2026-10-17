#!/usr/bin/env python3
"""Prepare mesh inactivity settings and start the MANET Wi-Fi supplicant."""

import argparse
import os
from pathlib import Path
import re
import sys

from manet_config_io import atomic_write


WIFI_INACTIVITY = 10
HALOW_INACTIVITY = 300
WPA_DIR = Path('/etc/wpa_supplicant')
MANET_BINARY = Path('/usr/sbin/wpa_supplicant_manet')
DEBIAN_BINARY = Path('/usr/sbin/wpa_supplicant')


def prepare_config(path):
    """Add a global default to a mesh file, preserving explicit tuning."""
    content = path.read_text()
    if not re.search(r'^\s*mode=5\s*(?:#.*)?$', content, re.M):
        return False
    # mesh_max_inactivity is global, not a network-block field.
    global_part = re.split(r'^\s*network\s*=\s*\{', content, maxsplit=1,
                           flags=re.M)[0]
    if re.search(r'^\s*mesh_max_inactivity\s*=', global_part, re.M):
        return False
    seconds = (HALOW_INACTIVITY if path.name.endswith('-s1g.conf')
               else WIFI_INACTIVITY)
    atomic_write(path, f'mesh_max_inactivity={seconds}\n' + content)
    return True


def prepare_all(directory=WPA_DIR):
    """Update live and lobby configs without touching client uplinks."""
    for path in sorted(directory.glob('wpa_supplicant-*.conf')):
        if path.name.endswith('-uplink.conf'):
            continue
        if prepare_config(path):
            print(f'manet-mesh-supplicant: set inactivity default in {path}',
                  flush=True)


def launch(iface):
    """Prepare this interface and exec the mesh binary or logged fallback."""
    if not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,15}', iface):
        raise ValueError('Invalid interface name')
    from manet_wifi_uplink import uplink_owned
    if uplink_owned(iface):
        raise ValueError('Radio belongs to the local Wi-Fi uplink')
    config = WPA_DIR / f'wpa_supplicant-{iface}.conf'
    prepare_config(config)
    lobby = WPA_DIR / f'wpa_supplicant-{iface}-lobby.conf'
    if lobby.exists():
        prepare_config(lobby)
    binary = MANET_BINARY
    if not os.access(binary, os.X_OK):
        binary = DEBIAN_BINARY
        print('manet-mesh-supplicant: WARNING: MANET binary missing; '
              'using /usr/sbin/wpa_supplicant; HE and fast recovery '
              'are unavailable', file=sys.stderr, flush=True)
    args = [str(binary), '-i', iface, '-c', str(config), '-D', 'nl80211']
    os.execv(str(binary), args)


def mesh_supplicant_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--update-configs', action='store_true')
    parser.add_argument('interface', nargs='?')
    args = parser.parse_args()
    if args.update_configs and args.interface is None:
        prepare_all()
    elif args.interface and not args.update_configs:
        launch(args.interface)
    else:
        parser.error('Specify an interface or --update-configs')


if __name__ == '__main__':
    mesh_supplicant_main()
