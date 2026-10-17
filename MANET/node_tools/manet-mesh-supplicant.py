#!/usr/bin/env python3
"""Prepare mesh width/inactivity and start the MANET Wi-Fi supplicant."""

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
    """Enforce 20 MHz Wi-Fi mesh blocks and default global inactivity."""
    content = path.read_text()
    mode = r'^[ \t]*mode[ \t]*=[ \t]*5[ \t]*(?:#[^\n]*)?$'
    if not re.search(mode, content, re.M):
        return False
    updated = content
    if not path.name.endswith('-s1g.conf'):
        def mesh_width(match):
            opening, body, closing = match.groups()
            if not re.search(mode, body, re.M):
                return match[0]
            # ht40=0 alone still permits HT40 on the benched supplicant.
            # Keep HE/VHT enabled, limiting only the operating channel width.
            body = re.sub(r'^[ \t]*ht40[ \t]*=[^\n]*\n', '', body,
                          flags=re.M)
            for key, value in (('max_oper_chwidth', 0), ('disable_ht40', 1)):
                body, count = re.subn(
                    rf'^[ \t]*{key}[ \t]*=[^\n]*$',
                    f'    {key}={value}', body, flags=re.M)
                if not count:
                    body += f'    {key}={value}\n'
            return opening + body + closing
        # Closing braces must occupy their own line, so braces in SSIDs or
        # passwords cannot terminate a block. Client blocks remain untouched.
        updated = re.sub(
            r'(^[ \t]*network[ \t]*=[ \t]*\{[ \t]*(?:#[^\n]*)?\n)'
            r'(.*?)(^[ \t]*\}[ \t]*(?:#[^\n]*)?$)',
            mesh_width, updated, flags=re.M | re.S)
    # mesh_max_inactivity is global, not a network-block field.
    global_part = re.split(r'^\s*network\s*=\s*\{', updated, maxsplit=1,
                           flags=re.M)[0]
    if not re.search(r'^\s*mesh_max_inactivity\s*=', global_part, re.M):
        seconds = (HALOW_INACTIVITY if path.name.endswith('-s1g.conf')
                   else WIFI_INACTIVITY)
        updated = f'mesh_max_inactivity={seconds}\n' + updated
    if updated == content:
        return False
    atomic_write(path, updated)
    return True


def prepare_all(directory=WPA_DIR):
    """Update live and lobby configs without touching client uplinks."""
    for path in sorted(directory.glob('wpa_supplicant-*.conf')):
        if path.name.endswith('-uplink.conf'):
            continue
        if prepare_config(path):
            print(f'manet-mesh-supplicant: prepared mesh settings in {path}',
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
