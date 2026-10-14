#!/usr/bin/env python3
"""Write a literal configuration value without treating it as a sed program."""

from pathlib import Path
import sys
from manet_config_io import atomic_write, rewrite_keys


def write_key(path, key, value, quoted=False):
    """Replace one key atomically, with optional supplicant quoting."""
    path = Path(path)
    original = path.read_text()
    text = rewrite_keys(original, {key: value}, {key} if quoted else ())
    if text == original:
        return False
    atomic_write(path, text)
    return True


if __name__ == '__main__':
    try:
        flags = sys.argv[4:]
        if len(sys.argv) < 4 or any(flag not in ('--quoted', '--report-change') for flag in flags):
            raise ValueError('usage: mesh-config-write.py PATH KEY VALUE [--quoted] [--report-change]')
        changed = write_key(*sys.argv[1:4], quoted='--quoted' in flags)
        if changed and '--report-change' in flags:
            print('changed')
    except (OSError, ValueError) as error:
        print(f'mesh-config-write.py: {error}', file=sys.stderr)
        sys.exit(1)
