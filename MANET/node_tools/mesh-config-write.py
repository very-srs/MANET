#!/usr/bin/env python3
"""Write a literal configuration value without treating it as a sed program."""

from pathlib import Path
import sys
from manet_config_io import atomic_write, rewrite_keys


def write_key(path, key, value, quoted=False):
    path = Path(path)
    text = rewrite_keys(path.read_text(), {key: value}, {key} if quoted else ())
    atomic_write(path, text)


if __name__ == '__main__':
    try:
        if len(sys.argv) not in (4, 5) or (len(sys.argv) == 5 and sys.argv[4] != '--quoted'):
            raise ValueError('usage: mesh-config-write.py PATH KEY VALUE [--quoted]')
        write_key(*sys.argv[1:4], quoted=len(sys.argv) == 5)
    except (OSError, ValueError) as error:
        print(f'mesh-config-write.py: {error}', file=sys.stderr)
        sys.exit(1)
