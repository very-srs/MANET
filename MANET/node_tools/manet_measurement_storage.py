"""Bound MANET measurements without deleting operator results."""

import json
import os
from pathlib import Path
import shutil
import stat

from manet_config_io import atomic_write


MIB = 1024 * 1024
# Byte budgets include retained results and unfinished regular files.
DEFAULTS = {
    'MAX_BYTES': 256 * MIB,
    'MAX_FILES': 4096,
    'MAX_SESSIONS': 128,
    'MAX_RESULT_BYTES': MIB,
    'MIN_FREE_BYTES': 128 * MIB,
}


class StorageFull(ValueError):
    """Signal a measurement budget or free-space limit that blocks saving."""
    pass


def limits():
    """Load positive integer storage budgets from environment or defaults."""
    values = {}
    for name, default in DEFAULTS.items():
        value = int(os.environ.get('MANET_MEASUREMENTS_' + name, default))
        if value <= 0:
            raise ValueError('MANET_MEASUREMENTS_' + name + ' must be positive')
        values[name] = value
    return values


def check_capacity(root, session, incoming=None):
    """Recheck capacity before writes because other apps can consume space."""
    root, session = Path(root), Path(session)
    policy = limits()
    # Preflight reserves the maximum result size before measurements begin.
    needed = policy['MAX_RESULT_BYTES'] if incoming is None else incoming
    if needed > policy['MAX_RESULT_BYTES']:
        raise StorageFull('Measurement result exceeds its size limit')
    # Round up to 4 KiB and reserve one extra 4 KiB block per new result.
    allocation = ((needed + 4095) // 4096 + 1) * 4096
    used = files = sessions = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        if Path(directory) == root:
            sessions = sum(not (root / name).is_symlink() for name in dirs)
        for name in names:
            try:
                info = (Path(directory) / name).lstat()
            except FileNotFoundError:
                continue  # A session may be deleted while checking capacity.
            if stat.S_ISREG(info.st_mode):
                # st_blocks counts 512-byte units. Charge sparse logical size
                # too, and include temporary results left by interrupted writes.
                used += max(info.st_size, info.st_blocks * 512)
                files += 1
        if (used + allocation > policy['MAX_BYTES']
                or files >= policy['MAX_FILES']):
            raise StorageFull('Measurement storage limit reached; export '
                              'and delete saved sessions before recording more')
    if used + allocation > policy['MAX_BYTES']:
        raise StorageFull('Measurement storage limit reached; export '
                          'and delete saved sessions before recording more')
    if not session.exists() and sessions >= policy['MAX_SESSIONS']:
        raise StorageFull('Measurement session limit reached; export '
                          'and delete saved sessions before recording more')
    parent = root
    # A new store has no inode yet. Check its nearest existing filesystem.
    while not parent.exists():
        parent = parent.parent
    # Account for filesystem block allocation as well as the JSON bytes.
    if shutil.disk_usage(parent).free < policy['MIN_FREE_BYTES'] + allocation:
        raise StorageFull('Not enough free storage for measurements; '
                          'free space before recording more')


def save_result(root, destination, record):
    """Check budgets and atomically publish a new JSON measurement result."""
    destination = Path(destination)
    payload = json.dumps(record, indent=2, allow_nan=False).encode('utf-8')
    check_capacity(root, destination.parent, len(payload))
    # Names are random and the measurement worker is serialized. Refuse a
    # collision to protect any existing result or symlink.
    if destination.exists() or destination.is_symlink():
        raise ValueError('Measurement result already exists')
    atomic_write(destination, payload)
