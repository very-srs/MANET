#!/usr/bin/env python3
"""Inspect and revalidate Linux flash destinations before destructive writes."""

import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

COLUMNS = 'NAME,TYPE,SIZE,MODEL,SERIAL,WWN,RO,RM,TRAN,MOUNTPOINTS,MAJ:MIN'
# While the flasher runs, the desktop must not automount the destination: an
# automount racing the unmount leaves it busy (GNOME reads every new mount),
# and a mount finishing after the unmount (ext4 journal recovery takes
# seconds) is live while the image is written. UDISKS_AUTO=0 stops udisks and
# GNOME automounting it. /run keeps a leftover rule from outliving a reboot.
NOAUTO_RULES = Path(os.environ.get('MANET_UDEV_RULES_DIR', '/run/udev/rules.d')) / '99-manet-flash-noauto.rules'
# The eMMC rpiboot exposes: "Raspberry Pi Compute Module" mass storage.
CM4_RULE = 'SUBSYSTEM=="block", ATTRS{idVendor}=="0a5c", ATTRS{idProduct}=="0001", ENV{UDISKS_AUTO}="0"'
UNMOUNT_WAIT = 30


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


def hold(rule):
    """Add a no-automount rule; it applies to events from now on."""
    NOAUTO_RULES.parent.mkdir(parents=True, exist_ok=True)
    lines = NOAUTO_RULES.read_text().splitlines() if NOAUTO_RULES.exists() else []
    if rule not in lines:
        NOAUTO_RULES.write_text('\n'.join(lines + [rule]) + '\n')
    subprocess.run(['udevadm', 'control', '--reload'], check=True, timeout=30)


def release():
    if NOAUTO_RULES.exists():
        NOAUTO_RULES.unlink()
        subprocess.run(['udevadm', 'control', '--reload'], check=True, timeout=30)


def hold_device(device):
    """No automount for this disk and its partitions, from now on."""
    name = Path(device).name
    hold(f'SUBSYSTEM=="block", KERNEL=="{name}|{name}[0-9]*|{name}p[0-9]*", ENV{{UDISKS_AUTO}}="0"')
    subprocess.run(['udevadm', 'trigger', '--action=change', '--subsystem-match=block',
                    f'--sysname-match={name}*'], check=True, timeout=30)
    subprocess.run(['udevadm', 'settle', '--timeout=10'], timeout=30)


def claimed(device):
    """True while anything holds the disk: a mount, or one still in progress,
    which lsblk does not show yet but which already claims the device."""
    try:
        os.close(os.open(device, os.O_RDONLY | os.O_EXCL))
    except OSError as error:
        if error.errno == errno.EBUSY:
            return True
        raise
    return False


def holders(mount):
    """Processes using a mount, for the error message."""
    names = set()
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            links = [proc / 'cwd', proc / 'root'] + list((proc / 'fd').iterdir())
            paths = [os.readlink(link) for link in links]
            if any(p == mount or p.startswith(mount + '/') for p in paths):
                names.add(f"{(proc / 'comm').read_text().strip()} (pid {proc.name})")
        except OSError:
            continue
    return sorted(names)


def prepare(device, expected):
    """Unmount the confirmed destination and keep it unmounted. Desktop
    automounters can hold a fresh mount busy for a few seconds, or finish a
    mount after an unmount, so wait until the disk stays free."""
    target = inspect_target(device)
    if target['fingerprint'] != expected:
        raise ValueError('Target identity changed since confirmation; rescan and confirm again')
    hold_device(device)
    deadline = time.monotonic() + UNMOUNT_WAIT
    free = 0
    while True:
        target = inspect_target(device)
        if target['fingerprint'] != expected:
            raise ValueError('Target changed while unmounting; refusing to flash')
        busy = {}
        for mount in sorted(set(target['mounts']), key=lambda m: (-len(m), m)):
            result = subprocess.run(['umount', '--', mount], capture_output=True, text=True,
                                    timeout=30)
            if result.returncode:
                busy[mount] = result.stderr.strip()
        free = 0 if target['mounts'] or claimed(device) else free + 1
        if free >= 2:
            return
        if time.monotonic() >= deadline:
            detail = '; '.join(f"{mount} is in use by {', '.join(holders(mount)) or 'unknown'}"
                               for mount in busy) or 'the disk is still claimed'
            raise ValueError(f'Target stays mounted ({detail}). Close any window showing it '
                             'and run the flasher again')
        time.sleep(1)


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
    elif sys.argv[1:] == ['hold-cm4']:
        hold(CM4_RULE)
    elif sys.argv[1:] == ['release']:
        release()
    else:
        raise ValueError('usage: flash-target.py {list|describe DEVICE|fingerprint DEVICE|'
                         'prepare DEVICE FINGERPRINT|hold-cm4|release}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f'Flash target rejected: {error}')
