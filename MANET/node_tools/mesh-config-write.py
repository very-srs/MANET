#!/usr/bin/env python3
"""Write a literal configuration value without treating it as a sed program."""

import os
from pathlib import Path
import re
import stat
import sys
import tempfile


def write_key(path, key, value, quoted=False):
    if not re.fullmatch('[a-z][a-z0-9_]*', key):
        raise ValueError('Invalid configuration key')
    if any(character in value for character in '\n\r\0'):
        raise ValueError('Configuration values must occupy one line')
    path = Path(path)
    original = path.stat()
    if quoted:
        value = '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    replacement = f'{key}={value}\n'
    lines, found = [], False
    for line in path.read_text().splitlines(keepends=True):
        if line.partition('=')[0].strip() == key:
            indent = line[:len(line) - len(line.lstrip())]
            lines.append(indent + replacement)
            found = True
        else:
            lines.append(line)
    if not found:
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        lines.append(replacement)

    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as target:
            os.fchmod(target.fileno(), stat.S_IMODE(original.st_mode))
            if os.geteuid() == 0:
                os.fchown(target.fileno(), original.st_uid, original.st_gid)
            target.writelines(lines)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    try:
        if len(sys.argv) not in (4, 5) or (len(sys.argv) == 5 and sys.argv[4] != '--quoted'):
            raise ValueError('usage: mesh-config-write.py PATH KEY VALUE [--quoted]')
        write_key(*sys.argv[1:4], quoted=len(sys.argv) == 5)
    except (OSError, ValueError) as error:
        print(f'Config write failed: {error}', file=sys.stderr)
        sys.exit(1)
