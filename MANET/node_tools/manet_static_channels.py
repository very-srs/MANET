#!/usr/bin/env python3
"""Stored Wi-Fi mesh channel plan for static (non-ACS) mode.

Static mode keeps each Wi-Fi mesh band on one fixed channel. The plan lives
here rather than as constants in the node manager so an operator's channel
change survives the manager's periodic enforcement and a reboot. It is not in
mesh.conf: config apply and rollback have nothing to do with it.

Missing file: the historic defaults (the lobby channels). Malformed file: an
error, never a silent fallback, since falling back could strand this radio on
a different channel from the rest of the mesh.

No imports beyond the standard library, so the radio code can import this.
"""

import json
import os
from pathlib import Path
import sys
import tempfile


PATH = Path('/etc/manet/static-channels.json')
BANDS = ('2.4', '5')
DEFAULTS = {'2.4': 2412, '5': 5180}

# 20 MHz channel centres. 2.4 GHz channels 1-13; 5 GHz UNII-1 to UNII-4.
# Regulatory permission is still the kernel's decision when the radio starts.
_FREQUENCIES = {
    '2.4': frozenset(2407 + 5 * ch for ch in range(1, 14)),
    '5': frozenset(5000 + 5 * ch for ch in
                   [*range(36, 65, 4), *range(100, 145, 4), *range(149, 178, 4)]),
}


class StaticChannelError(ValueError):
    pass


def valid(band, freq):
    return (band in _FREQUENCIES and type(freq) is int
            and freq in _FREQUENCIES[band])


def band_for_frequency(freq):
    for band in BANDS:
        if valid(band, freq):
            return band
    return None


def load(path=PATH):
    path = Path(path)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return dict(DEFAULTS)
    except OSError as error:
        raise StaticChannelError(f'Cannot read static channel plan {path}: {error}') from None
    try:
        plan = json.loads(text)
    except ValueError:
        raise StaticChannelError(f'Static channel plan {path} is not valid JSON') from None
    if not isinstance(plan, dict) or set(plan) != set(BANDS):
        raise StaticChannelError(f'Static channel plan {path} must list exactly the 2.4 and 5 bands')
    for band in BANDS:
        if not valid(band, plan[band]):
            raise StaticChannelError(f'Static channel plan {path}: invalid {band} GHz '
                                     f'frequency {plan[band]!r}')
    return {band: plan[band] for band in BANDS}


def save(band, freq, path=PATH):
    """Store one band's frequency atomically; return the complete new plan."""
    if not valid(band, freq):
        raise StaticChannelError(f'Invalid {band} GHz static frequency {freq!r}')
    path = Path(path)
    plan = load(path)
    plan[band] = freq
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as target:
            os.fchmod(target.fileno(), 0o644)
            json.dump(plan, target, sort_keys=True)
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return plan


def main(argv):
    path = Path(os.environ.get('MANET_STATIC_CHANNELS', PATH))
    try:
        if len(argv) == 2 and argv[0] == 'get' and argv[1] in BANDS:
            print(load(path)[argv[1]])
        elif len(argv) == 1 and argv[0] == 'show':
            print(json.dumps(load(path), sort_keys=True))
        elif len(argv) == 3 and argv[0] == 'set' and argv[1] in BANDS and argv[2].isdigit():
            print(json.dumps(save(argv[1], int(argv[2]), path), sort_keys=True))
        else:
            print('usage: manet_static_channels.py {get 2.4|5 | set 2.4|5 FREQ_MHZ | show}',
                  file=sys.stderr)
            return 2
    except StaticChannelError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
