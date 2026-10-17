"""Reserve the MT7916 third receive path only for local RF trouble or a hold."""

import errno
import json
import os
from pathlib import Path
import time

from manet_bgobs import write_command


def cm4_enabled():
    """Enable this release's receive-path policy on CM4 only."""
    path = Path(os.environ.get('MANET_MODEL_FILE', '/proc/device-tree/model'))
    try:
        return ('Raspberry Pi Compute Module 4' in path.read_text() or
                os.environ.get('MANET_DFS_BENCH') == '1')
    except OSError:
        return False


def reconcile_receive_paths(observer, trouble):
    """Called with the observer lock held. Kernel errors never force ownership.

    The benched transition accepts 2/3, has a 60 second reversal limit, and
    rejects a live observer/CAC owner. Its status survives process restarts.
    A missing hold file means no hold support. An unreadable existing file
    does not justify releasing a potentially owned receiver.
    """
    if not cm4_enabled():
        return 'unsupported-board'
    path = observer.with_name('rx_transition_probe')
    try:
        status = json.loads(path.read_text())
    except FileNotFoundError:
        return 'unsupported-kernel'
    if (
        type(status.get('schema')) is not int
        or status['schema'] != 1
        or type(status.get('reserved')) is not bool
        or status.get('failed') is not False
        or status.get('chainmask') != (12 if status['reserved'] else 28)
        or type(status.get('transitions')) is not int
        or type(status.get('last_boottime_us')) is not int
    ):
        raise ValueError('invalid RX transition status')
    hold_path = observer.parent.parent / 'background_hold'
    try:
        hold = json.loads(hold_path.read_text())
        if (
            hold.get('schema') != 2
            or type(hold.get('owner')) is not bool
            or type(hold.get('enabled')) is not bool
        ):
            raise ValueError('invalid hold ownership')
        owned = hold['owner'] or hold.get('main_active') is True
    except FileNotFoundError:
        owned = False
    reserve = trouble or owned
    if status['reserved'] == reserve:
        return 'reserved' if reserve else '3x3'
    elapsed = int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1000000)
    if (
        status['transitions']
        and elapsed - status['last_boottime_us'] < 60000000
    ):
        return 'deferred'
    try:
        write_command(path, '2' if reserve else '3')
    except OSError as error:
        if error.errno in (errno.EBUSY, errno.EAGAIN):
            return 'deferred'
        raise
    after = json.loads(path.read_text())
    if (
        after.get('reserved') is not reserve
        or after.get('failed') is not False
        or after.get('chainmask') != (12 if reserve else 28)
    ):
        raise ValueError('RX transition did not reach requested state')
    return 'reserved' if reserve else '3x3'
