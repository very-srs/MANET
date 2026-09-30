#!/usr/bin/env python3
"""Finish MAC-pinned wlan renames before networking starts.

udev cannot exchange two occupied kernel names. Move every displaced pinned
radio aside first, then assign the final names and retry its udev add event.
"""
import configparser
import json
from pathlib import Path
import re
import subprocess
import sys


def read_pins(directory=Path('/etc/systemd/network')):
    pins = {}
    for path in sorted(directory.glob('10-wlan*.link')):
        config = configparser.ConfigParser(interpolation=None)
        config.read(path)
        name = config.get('Link', 'Name')
        mac = config.get('Match', 'MACAddress').lower()
        if (not re.fullmatch(r'wlan[0-9]+', name)
                or path.name != f'10-{name}.link'
                or not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac)
                or config.get('Match', 'Type') != 'wlan'):
            raise ValueError(f'Invalid radio pin: {path}')
        if name in pins.values() or mac in pins:
            raise ValueError(f'Duplicate radio pin: {path}')
        pins[mac] = name
    return pins


def rename_plan(pins, links):
    by_name = {link['ifname']: link for link in links}
    by_mac = {}
    for link in links:
        mac = link.get('address', '').lower()
        if mac in pins:
            if mac in by_mac:
                raise ValueError(f'Multiple interfaces match radio MAC {mac}')
            by_mac[mac] = link
    moves = []
    for mac, target in pins.items():
        link = by_mac.get(mac)
        if link is None:
            print(f'Pinned radio {target} ({mac}) is absent', flush=True)
            continue
        source = link['ifname']
        if source != target:
            if 'UP' in link.get('flags', []) or link.get('master'):
                raise ValueError(f'Refusing to rename active radio {source}')
            temporary = f'mnr{link["ifindex"]}'
            if temporary in by_name or len(temporary) > 15:
                raise ValueError(f'Temporary radio name unavailable: {temporary}')
            moves.append((source, temporary, target))
    moving = {source for source, _, _ in moves}
    for _, _, target in moves:
        if target in by_name and target not in moving:
            raise ValueError(f'Radio name {target} is occupied by an unpinned interface')
    return moves


def reconcile(pins, links, run=subprocess.run):
    moves = rename_plan(pins, links)  # Validate the entire exchange before changing anything.
    for source, temporary, _ in moves:
        run(['ip', 'link', 'set', 'dev', source, 'name', temporary], check=True, timeout=10)
    for source, temporary, target in moves:
        run(['ip', 'link', 'set', 'dev', temporary, 'name', target], check=True, timeout=10)
        print(f'Radio name restored: {source} -> {target}', flush=True)
    if moves:
        # Failed udev renames leave .device units unavailable to supplicants.
        # Names are now correct, so a fresh add event can finish successfully.
        run(['udevadm', 'trigger', '--action=add'] +
            [f'/sys/class/net/{target}' for _, _, target in moves], check=True, timeout=10)
        run(['udevadm', 'settle', '--timeout=20'], check=True, timeout=25)


def main():
    pins = read_pins()
    if pins:
        links = json.loads(subprocess.check_output(['ip', '-j', 'link', 'show'], timeout=10))
        reconcile(pins, links)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, configparser.Error, OSError, subprocess.SubprocessError) as exc:
        print(f'Cannot restore radio names: {exc}', file=sys.stderr)
        sys.exit(1)
