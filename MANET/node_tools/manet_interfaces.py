#!/usr/bin/env python3
"""Live Alfred radio telemetry, without importing the status web server.

Only collect fields sent over Alfred. UI health checks, power-cap choices,
service inventory and wired route probes belong to the status page.
"""
import json
from pathlib import Path
import re
import subprocess

from manet_peer_radios import WLAN_IFACES, interfaces_for_telemetry


def command(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5,
                              check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def collect(sysnet=Path('/sys/class/net'), no_mesh=Path('/var/lib/no_mesh_if')):
    try:
        raw = json.loads(command('ip', '-j', 'addr'))
    except ValueError:
        return []
    wireless, current = {}, None
    for line in command('iw', 'dev').splitlines():
        match = re.match(r'\s+Interface (\S+)', line)
        if match:
            current = wireless.setdefault(match[1], {})
        if current is None:
            continue
        for field, pattern in [('type', r'type (\S+)'),
                               ('channel', r'channel (\d+) \('),
                               ('freq_mhz', r'channel \d+ \((\d+) MHz\)'),
                               ('txpower_dbm', r'txpower ([\d.]+) dBm')]:
            match = re.search(pattern, line)
            if match:
                current[field] = match[1]
    bat = set(re.findall(r'^(\S+):\s+(?:active|inactive)', command('batctl', 'if'), re.M))
    try:
        excluded = set(no_mesh.read_text().split())
    except OSError:
        excluded = set()
    mastered = {row.get('ifname') for row in raw if row.get('master') == 'bat0'}
    result = []
    for row in raw:
        name = row.get('ifname')
        if name not in WLAN_IFACES:
            continue
        iw = wireless.get(name, {}).copy()
        driver_path = sysnet / name / 'device/driver'
        driver = driver_path.resolve().name if driver_path.exists() else ''
        if not driver and name in wireless:
            match = re.search(r'^driver:\s*(.+)', command('ethtool', '-i', name), re.M)
            driver = match[1] if match else ''
        if 'morse' in driver:
            from manet_radio import get_halow_driver_info
            iw.update(get_halow_driver_info(name))
        role = 'other'
        if name in bat or (name not in excluded and iw.get('type') != 'AP' and name not in mastered):
            role = 'mesh'
        elif iw.get('type') == 'AP' or name in excluded:
            role = 'ap'
        result.append(dict(iw, name=name, role=role, state=row.get('operstate', 'UNKNOWN'),
                           addrs=[a['local'] for a in row.get('addr_info', [])
                                  if a.get('family') in ('inet', 'inet6')
                                  and not a['local'].startswith('fe80')]))
    return interfaces_for_telemetry(result)


if __name__ == '__main__':
    print(json.dumps(collect(), separators=(',', ':')))
