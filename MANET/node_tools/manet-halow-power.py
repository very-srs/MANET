#!/usr/bin/env python3
"""Request automatic transmit power on each HaLow mesh radio.

Replaces MANET's former fixed 24 dBm request with `auto`, so MANET itself no
longer sets a HaLow ceiling. The driver, regulatory tables and firmware still
apply their own limits; the value read back is what the driver reports after
them. It is logged as reported power, not as the module's proven physical
maximum or radiated output.

Never reloads the driver: a live reload can wedge the USB HaLow card.
"""

import os
from pathlib import Path
import re
import subprocess
import sys
import time

NAME = re.compile(r'[A-Za-z0-9_.-]{1,15}')
READBACK_ATTEMPTS = 10
READBACK_DELAY = 1.0


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10).stdout


def halow_interfaces(roles):
    try:
        names = (roles / 'halow_if').read_text().split()
    except FileNotFoundError:
        return []
    for name in names:
        if not NAME.fullmatch(name):
            raise ValueError(f'Invalid HaLow interface name {name!r}')
    return names


def is_morse(sysnet, iface):
    driver = (sysnet / iface / 'device' / 'driver').resolve().name
    return driver.startswith('morse')


def reported_power(info):
    match = re.search(r'^\s*txpower (-?\d+(?:\.\d+)?) dBm', info, re.M)
    return float(match[1]) if match else None


def apply(iface, command=run, sleep=time.sleep):
    command(['iw', 'dev', iface, 'set', 'txpower', 'auto'])
    for attempt in range(READBACK_ATTEMPTS):
        power = reported_power(command(['iw', 'dev', iface, 'info']))
        if power is not None:
            return power
        if attempt + 1 < READBACK_ATTEMPTS:
            sleep(READBACK_DELAY)
    raise RuntimeError(f'{iface}: no transmit power reported after requesting auto')


def main(command=run, sleep=time.sleep):
    roles = Path(os.environ.get('MANET_IFACE_STATE_DIR', '/var/lib'))
    sysnet = Path(os.environ.get('MANET_SYS_NET', '/sys/class/net'))
    failures = []
    for iface in halow_interfaces(roles):
        if not (sysnet / iface).exists():
            failures.append(f'{iface}: interface not present')
            continue
        if not is_morse(sysnet, iface):
            failures.append(f'{iface}: not a Morse HaLow radio; leaving it alone')
            continue
        try:
            power = apply(iface, command, sleep)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            failures.append(f'{iface}: {error}')
            continue
        print(f'{iface}: requested auto; driver reports {power:.2f} dBm', flush=True)
    if failures:
        raise RuntimeError('; '.join(failures))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(f'ERROR: HaLow power: {error}', file=sys.stderr)
        sys.exit(1)
