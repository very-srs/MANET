#!/usr/bin/env python3
"""Select and restart configured mesh supplicants, including HaLow."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def service_name(iface, halow=False):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', iface):
        raise ValueError('Invalid mesh interface')
    return f'wpa_supplicant-s1g-{iface}.service' if halow else f'wpa_supplicant@{iface}.service'


def configured(roles):
    result = {}
    for filename, halow in [('mesh_if', False), ('halow_if', True)]:
        try:
            names = (roles / filename).read_text().split()
        except FileNotFoundError:
            continue
        for iface in names:
            service_name(iface, halow)
            driver = (Path('/sys/class/net') / iface / 'device/driver').resolve().name
            result[iface] = result.get(iface, False) or halow or driver.startswith('morse')
    return result


def restart_configured(roles=None, only=None):
    roles = Path(roles or os.environ.get('MANET_IFACE_STATE_DIR', '/var/lib'))
    try:
        state = json.loads((roles / 'mesh_radio_state.json').read_text())
        desired = state.get('desired', {})
        if not isinstance(desired, dict):
            raise ValueError('Invalid desired radio state')
    except FileNotFoundError:
        desired = {}
    interfaces = configured(roles)
    if only is not None and set(only) - interfaces.keys():
        raise ValueError('Requested interface has no configured mesh supplicant')
    errors = []
    for iface, halow in interfaces.items():
        if only is not None and iface not in only:
            continue
        if desired.get(iface) == 'down':
            continue
        service = service_name(iface, halow)
        try:
            subprocess.run(['systemctl', 'restart', service], check=True,
                           capture_output=True, timeout=30)
            cli = 'wpa_cli_s1g' if halow else 'wpa_cli'
            control = '/var/run/wpa_supplicant_s1g' if halow else '/var/run/wpa_supplicant'
            deadline = time.monotonic() + 10
            while True:
                active = subprocess.run(['systemctl', 'is-active', '--quiet', service],
                                        capture_output=True, timeout=3)
                response = subprocess.run([cli, '-p', control, '-i', iface, 'ping'],
                                          capture_output=True, text=True, timeout=3)
                if active.returncode == 0 and response.returncode == 0 and 'PONG' in response.stdout.splitlines():
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'{service} did not become ready')
                time.sleep(0.25)
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            errors.append(f'{service}: {type(error).__name__}')
    if errors:
        raise RuntimeError('Cannot restart mesh services: ' + '; '.join(errors))


if __name__ == '__main__':
    try:
        if sys.argv[1:] != ['restart']:
            raise ValueError('usage: manet_supplicant.py restart')
        restart_configured()
    except (OSError, ValueError, RuntimeError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
