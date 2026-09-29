#!/usr/bin/env python3
"""Bounded boot enumeration before radio-setup assigns persistent roles."""

from pathlib import Path
import re
import subprocess
import sys
import time


NET = Path('/sys/class/net')


def snapshot(remaining):
    """Include identity and driver binding, not just a netdev count."""
    started = time.monotonic()
    output = subprocess.run(['iw', 'dev'], check=True, capture_output=True,
                            text=True, timeout=min(2, remaining)).stdout
    phys, interfaces, rows = set(), set(), []
    phy = None
    ready = True
    for line in output.splitlines():
        line = line.strip()
        if re.fullmatch(r'phy#\d+', line):
            phy = line
            phys.add(phy)
        elif line.startswith('Interface '):
            iface = line.split()[1]
            interfaces.add(phy)
            try:
                device = NET / iface
                driver = (device / 'device/driver').resolve(strict=True).name
                mac = (device / 'address').read_text().strip()
                index = int((device / 'ifindex').read_text())
                if not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', mac):
                    raise ValueError('incomplete MAC address')
            except (OSError, ValueError):
                driver, mac, index = '', '', 0
                ready = False
            rows.append((phy or '', iface, driver, mac, index))
    # A PHY with no netdev yet is still probing. A failed iw/udev query must
    # never be mistaken for a wired-only node.
    ready = ready and phys == interfaces and None not in interfaces and bool(phys or not output.strip())
    remaining -= time.monotonic() - started
    if remaining <= 0:
        raise TimeoutError('radio query exceeded enumeration deadline')
    settled = subprocess.run(['udevadm', 'settle', '--timeout=1'],
                             capture_output=True, timeout=min(2, remaining))
    return (tuple(sorted(phys)), tuple(sorted(rows))), ready and settled.returncode == 0


def wait_for_radios(timeout=60, minimum=10, quiet=5):
    start = time.monotonic()
    deadline = start + timeout
    previous = None
    stable_since = None
    seen_radio = False
    last_empty = False
    detail = 'wireless interfaces did not settle'
    while time.monotonic() < deadline:
        try:
            state, ready = snapshot(max(0.001, deadline - time.monotonic()))
            now = time.monotonic()
            seen_radio = seen_radio or bool(state[0] or state[1])
            last_empty = ready and not state[0] and not state[1]
            if ready and state[1]:
                if state != previous or stable_since is None:
                    stable_since = now
                if now < deadline and now - start >= minimum and now - stable_since >= quiet:
                    return tuple(row[1] for row in state[1])
            else:
                stable_since = None
            previous = state
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            stable_since = None
            last_empty = False
            detail = f'wireless enumeration failed: {exc}'
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1, remaining))
    if last_empty and not seen_radio:
        return ()
    raise TimeoutError(detail)


def main():
    print('Waiting up to 60 seconds for wireless enumeration (10-second minimum, 5-second quiet period)...', flush=True)
    try:
        interfaces = wait_for_radios()
    except TimeoutError as exc:
        print(f'ERROR: {exc}; retaining the previous radio roles', file=sys.stderr)
        return 1
    if interfaces:
        print('Wireless interfaces settled: ' + ', '.join(interfaces))
    else:
        print('No wireless interfaces found; continuing with wired-only setup')
    return 0


if __name__ == '__main__':
    sys.exit(main())
