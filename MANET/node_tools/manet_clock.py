"""Detect wall-clock jumps against the boot clock for timed mesh protocols.

Admin commands and ACS rounds carry wall-clock times that compare only within
one timeline. A jump of more than JUMP_SECONDS starts a new clock epoch: a
merge step, a first sync or an operator's date command. Pending commands are
then voided and agreement rounds restart. Slewing moves the clock by less than
0.1 s per second, so callers checking every few seconds never see it as a jump.
"""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

JUMP_SECONDS = 10


def observe():
    """Record any jump since the last call and return this boot's epoch.

    The state holds the boot ID, the epoch count, the wall clock's offset from
    the boot clock and jump_ns, the new wall time when the latest jump was seen.
    """
    path = Path(os.environ.get('MANET_TIME_RUN_DIR', '/run')) / 'manet-clock-epoch.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    boot = Path(os.environ.get('MANET_BOOT_ID_FILE', '/proc/sys/kernel/random/boot_id')).read_text().strip()
    with open(path.with_name(path.name + '.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        offset = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
        try:
            state = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            state = None
        if (not isinstance(state, dict) or state.get('boot') != boot or type(state.get('epoch')) is not int
                or type(state.get('offset')) not in (int, float)):
            state = {'boot': boot, 'epoch': 0, 'offset': offset, 'jump_ns': 0}
        elif abs(offset - state['offset']) > JUMP_SECONDS:
            state.update(epoch=state['epoch'] + 1, jump_ns=time.time_ns())
        elif abs(offset - state['offset']) <= 0.5 and path.exists():
            return state  # Writes only follow slewing in half-second steps.
        state['offset'] = offset
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(state, out)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return state


def epoch_id(state=None):
    """Return the comparable [boot ID, epoch] pair for persisted records."""
    state = state or observe()
    return [state['boot'], state['epoch']]
