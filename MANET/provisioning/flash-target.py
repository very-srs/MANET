#!/usr/bin/env python3
"""Inspect and revalidate Linux flash destinations before destructive writes."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

COLUMNS = 'NAME,TYPE,SIZE,MODEL,SERIAL,WWN,RO,RM,TRAN,MOUNTPOINTS,MAJ:MIN'


def descendants(disk):
    yield disk
    for child in disk.get('children', []):
        yield from descendants(child)


def inspect_disk(disk):
    if disk.get('type') != 'disk' or disk.get('ro') or int(disk.get('size') or 0) <= 0:
        raise ValueError('Target must be a nonempty writable disk')
    mounts = []
    for part in descendants(disk):
        if part.get('type') not in ('disk', 'part'):
            raise ValueError('Target contains active mapped storage')
        for mount in part.get('mountpoints') or []:
            if not mount:
                continue
            if not (mount.startswith('/media/') or mount.startswith('/run/media/')):
                raise ValueError(f'Target is in use at {mount}')
            mounts.append(mount)
    name = disk['name']
    try:
        diskseq = (Path('/sys/class/block') / Path(name).name / 'diskseq').read_text().strip()
    except FileNotFoundError:
        diskseq = ''
    identity = {k: disk.get(k) for k in ('name', 'maj:min', 'size', 'model', 'serial', 'wwn')}
    identity['diskseq'] = diskseq
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    description = (f"{disk.get('model') or 'unknown model'}, {int(disk['size']) / 2**30:.1f} GiB, "
                   f"serial {disk.get('serial') or 'unknown'}, "
                   + ('removable' if disk.get('rm') else 'NON-REMOVABLE USB/storage')
                   + (', mounted: ' + ', '.join(mounts) if mounts else ', unmounted'))
    return {'device': name, 'fingerprint': fingerprint, 'mounts': mounts,
            'description': description.replace('\n', ' ').replace('\t', ' ')}


def inventory(device=None):
    args = ['lsblk', '--json', '--bytes', '--paths', '--output', COLUMNS]
    if device:
        args += ['--', device]
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ValueError('Cannot inspect disks (lsblk with MOUNTPOINTS support is required): '
                         + result.stderr.strip())
    return json.loads(result.stdout)['blockdevices']


def inspect_target(device):
    rows = inventory(device)
    if len(rows) != 1 or rows[0]['name'] != str(Path(device).resolve()):
        raise ValueError('Target no longer identifies one whole disk')
    return inspect_disk(rows[0])


def prepare(device, expected):
    target = inspect_target(device)
    if target['fingerprint'] != expected:
        raise ValueError('Target identity changed since confirmation; rescan and confirm again')
    for mount in sorted(set(target['mounts']), key=len, reverse=True):
        subprocess.run(['umount', '--', mount], check=True, timeout=30)
    target = inspect_target(device)
    if target['fingerprint'] != expected or target['mounts']:
        raise ValueError('Target changed or remains mounted; refusing to flash')


def main():
    if sys.argv[1:] == ['list']:
        for disk in inventory():
            if disk.get('tran') != 'usb':
                name = Path(disk['name']).name
                try:
                    kind = (Path('/sys/class/block') / name / 'device/type').read_text().strip()
                except FileNotFoundError:
                    continue
                if not name.startswith('mmcblk') or kind != 'SD':
                    continue
            try:
                info = inspect_disk(disk)
            except ValueError:
                continue
            print(f"{info['device']}\t{info['fingerprint']}\t{info['description']}")
    elif len(sys.argv) == 3 and sys.argv[1] in ('describe', 'fingerprint'):
        info = inspect_target(sys.argv[2])
        print(info['description' if sys.argv[1] == 'describe' else 'fingerprint'])
    elif len(sys.argv) == 4 and sys.argv[1] == 'prepare':
        prepare(sys.argv[2], sys.argv[3])
    else:
        raise ValueError('usage: flash-target.py {list|describe DEVICE|fingerprint DEVICE|prepare DEVICE FINGERPRINT}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f'Flash target rejected: {error}')
