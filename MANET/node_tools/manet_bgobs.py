#!/usr/bin/env python3
"""Collect bounded MT7916 third-chain candidate occupancy for ACS."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time

# ACS currently elects these non-DFS 20 MHz primaries. The kernel additionally
# checks the current regulatory flags, including disabled/radar channels.
CANDIDATES = frozenset((5200, 5220, 5240, 5745, 5765, 5785, 5805, 5825))
DWELL_MS = 250


def integer(record, key, minimum=0):
    """Require a non-Boolean integer field within its lower bound."""
    value = record[key]
    if type(value) is not int or value < minimum:
        raise ValueError('invalid observer ' + key)
    return value


def occupancy(record, frequency):
    """Correct foreign energy for measured local TX receiver blanking."""
    if (record.get('schema') != 1 or record.get('valid') is not True
            or record.get('rx_reserved') is not True
            or record.get('active') is not False
            or record.get('reserved') is not False
            or record.get('error') != 0 or record.get('restore_error') != 0
            or record.get('freq_mhz') != frequency
            or record.get('center_mhz') != frequency
            or record.get('width_mhz') != 20
            or record.get('threshold_bucket') != 2
            or record.get('counter_policy') != 'modulo23_no_clear'):
        raise ValueError('unusable observer window')
    elapsed = integer(record, 'elapsed_us', DWELL_MS * 1000)
    total = integer(record, 'sample_count', 1024)
    bins = record['delta']
    if (not isinstance(bins, list) or len(bins) != 12
            or any(type(v) is not int or not 0 <= v < 2**23 for v in bins)
            or sum(bins[:11]) != total or elapsed > 6000000
            or not elapsed * .8 <= total * 8 <= elapsed * 1.2):
        raise ValueError('invalid observer population')
    if integer(record, 'tx_read_max_us') > min(10000, elapsed / 20):
        raise ValueError('survey TX window not aligned')
    tx = integer(record, 'tx_last_us') - integer(record, 'tx_first_us')
    own = tx / elapsed
    top = bins[10] / total
    # Too little unblanked time amplifies small alignment errors. Never turn
    # a saturated local transmitter or a reset into a clean candidate.
    if not 0 <= own < .9 or own > top + .05:
        raise ValueError('survey TX inconsistent or receiver mostly blanked')
    foreign = sum(bins[2:10]) / total + max(0, top - own)
    return min(100, max(0, round(100 * foreign / (1 - own))))


def observer_path(iface):
    """Resolve the interface's actual wiphy to its optional debugfs ABI."""
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', iface):
        raise ValueError('invalid interface')
    net = Path(os.environ.get('MANET_SYS_NET', '/sys/class/net'))
    debug = Path(os.environ.get('MANET_DEBUGFS', '/sys/kernel/debug'))
    try:
        phy = (net / iface / 'phy80211/name').read_text().strip()
    except FileNotFoundError:
        return None
    if not re.fullmatch(r'phy[0-9]+', phy):
        raise ValueError('invalid wiphy')
    path = debug / 'ieee80211' / phy / 'mt76/background_scan'
    return path if path.is_file() else None


def read_status(path):
    """Read one complete, bounded status snapshot from debugfs."""
    with path.open() as stream:
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise ValueError('oversized observer status')
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get('schema') != 1:
        raise ValueError('unsupported observer ABI')
    integer(result, 'sequence')
    return result


def write_command(path, command):
    """Submit a command using one write, checking for short writes."""
    with path.open('w') as stream:
        if stream.write(command + '\n') != len(command) + 1:
            raise OSError('short observer write')


def measure(path, frequency):
    """Acquire one sequence and release only that sequence on failure."""
    before = read_status(path)
    if before.get('active') or before.get('reserved'):
        raise ValueError('observer already owned')
    sequence = integer(before, 'sequence') + 1
    accepted = False
    try:
        write_command(path, f'start {frequency} 20 {frequency} {DWELL_MS} 2')
        accepted = True
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = read_status(path)
            if status['sequence'] != sequence:
                raise ValueError('observer sequence changed')
            if status.get('active') is False:
                return occupancy(status, frequency)
            time.sleep(.025)
        raise TimeoutError('observer deadline exceeded')
    finally:
        if accepted:
            status = read_status(path)
            if (status['sequence'] == sequence
                    and (status.get('active') or status.get('reserved'))):
                write_command(path, 'stop')


def collect_candidates(path, frequencies, iface):
    """Serialize cooperating readers and omit invalid candidate samples."""
    results = []
    run = Path(os.environ.get('MANET_ACS_RUN_DIR', '/run'))
    lock = run / ('manet-bgobs-' + path.parents[1].name + '.lock')
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for frequency in frequencies:
            if frequency not in CANDIDATES:
                continue
            try:
                busy = measure(path, frequency)
            except (OSError, ValueError, KeyError, TypeError) as error:
                print(f'manet_bgobs: {frequency}: {error}', file=sys.stderr)
                continue
            # The histogram does not supply calibrated channel noise or BSS
            # counts. Publish only measured occupancy; no invented noise floor.
            results.append(dict(channel=frequency, busy_pct=busy, bss_count=0,
                                _observer=True, _survey_iface=iface,
                                _survey_boot=int(time.clock_gettime(
                                    time.CLOCK_BOOTTIME))))
    return results


def observer_main():
    """Print candidate JSON, returning 3 only when the observer is absent."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('iface')
    parser.add_argument('frequencies', nargs='*', type=int)
    args = parser.parse_args()
    try:
        path = observer_path(args.iface)
        if path is None:
            return 3
        results = collect_candidates(path, args.frequencies, args.iface)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('manet_bgobs: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps({'results': results}, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    sys.exit(observer_main())
