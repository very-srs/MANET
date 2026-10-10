#!/usr/bin/env python3
"""Store the fixed Wi-Fi mesh channel plan for non-ACS mode.

The separate plan persists through manager enforcement and reboot. It is
outside mesh.conf, so config apply/rollback does not change it. A missing
file uses lobby defaults. A malformed file raises an error to avoid
stranding the radio on a fallback channel. Uses only the standard library.
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
    """Reject an unreadable or invalid fixed channel plan."""
    pass


def valid(band, freq):
    """Check that a band supports the supplied integer center frequency."""
    return (band in _FREQUENCIES and type(freq) is int
            and freq in _FREQUENCIES[band])


def load(path=PATH):
    """Load both bands, using defaults only when the file is absent."""
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
    """Store one band's frequency atomically and return the new plan."""
    if not valid(band, freq):
        raise StaticChannelError(f'Invalid {band} GHz static frequency {freq!r}')
    path = Path(path)
    plan = load(path)
    plan[band] = freq
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    # A same-directory temporary makes the final replacement atomic.
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as target:
            os.fchmod(target.fileno(), 0o644)
            json.dump(plan, target, sort_keys=True)
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        # Sync the renamed directory entry as well as the file contents.
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
    """Read or update the persistent plan using the requested CLI operation."""
    path = Path(os.environ.get('MANET_STATIC_CHANNELS', PATH))
    try:
        if len(argv) == 2 and argv[0] == 'get' and argv[1] in BANDS:
            print(load(path)[argv[1]])
        elif len(argv) == 1 and argv[0] == 'show':
            print(json.dumps(load(path), sort_keys=True))
        elif argv == ['frequencies']:
            plan = load(path)
            print(plan['2.4'], plan['5'])
        elif len(argv) == 3 and argv[0] == 'set' and argv[1] in BANDS and argv[2].isdigit():
            print(json.dumps(save(argv[1], int(argv[2]), path), sort_keys=True))
        else:
            print('usage: manet_static_channels.py {get 2.4|5 | set 2.4|5 FREQ_MHZ | show}',
                  file=sys.stderr)
            return 2
    except (StaticChannelError, OSError) as error:
        print(f'manet_static_channels.py: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
