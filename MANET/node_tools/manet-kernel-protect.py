#!/usr/bin/env python3
"""Keep distribution package transactions from replacing MANET boot files."""
import argparse
import fcntl
import fnmatch
import os
from pathlib import Path
import subprocess
import tempfile
import time

POLICY = Path('etc/apt/preferences.d/00-manet-kernel')
STUB = ('#!/bin/sh\n'
        'echo "MANET manages boot files; package boot hook skipped." >&2\n'
        'exit 0\n')


def kernel_package_command(args):
    # The caller owns lock-frontend. Tell dpkg children to use the backend
    # lock only; otherwise apt-mark --set-selections conflicts with our lock.
    return subprocess.run(args, check=True, text=True, capture_output=True,
                          timeout=120,
                          env=dict(os.environ, DPKG_FRONTEND_LOCKED='true')).stdout


def write_kernel_policy_file(path, data, mode=0o644):
    """Replace complete files and persist them before releasing the APT lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data.encode())
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def settings(path, values):
    """Append authoritative settings, preserving other local configuration."""
    existing = path.read_text() if path.exists() else ''
    block = '# MANET releases manage boot files.\n' + values
    if not existing.endswith(block):
        write_kernel_policy_file(path, existing.rstrip('\n') + '\n' + block)


def divert(root, name):
    """Preserve the package copy and install a successful no-op entry point."""
    # A dot in the backup name keeps run-parts from executing diverted hooks.
    backup = name + '.manet-distrib'
    args = ['dpkg-divert', '--root=' + str(root)]
    owner = kernel_package_command(args + ['--listpackage', name]).strip()
    if owner:
        target = kernel_package_command(args + ['--truename', name]).strip()
        if owner != 'LOCAL' or target != backup:
            raise RuntimeError('Conflicting boot-hook diversion: ' + name)
    else:
        kernel_package_command(args + ['--local', '--add', '--rename', '--divert', backup, name])
    write_kernel_policy_file(root / name.lstrip('/'), STUB, 0o755)


def protect(root, board, policy):
    """Apply pins, board hooks and installed-package holds without APT installs."""
    text = policy.read_text()
    patterns = [pattern for line in text.splitlines()
                if line.startswith('Package: ')
                for pattern in line.removeprefix('Package: ').split()]
    if not patterns or 'Pin-Priority: -1' not in text:
        raise RuntimeError('Missing MANET kernel package policy')
    # Never use a blanket linux-* pin: libc development needs linux-libc-dev.
    write_kernel_policy_file(root / POLICY, text)
    # Unlike update_initramfs=no, this also blocks creation and deletion.
    # Package upgrades replace the diverted copy, preserving this entry point.
    divert(root, '/usr/sbin/update-initramfs')
    write_kernel_policy_file(root / 'etc/flash-kernel/machine', 'none\n')
    if board in ('cm4', 'rpi4', 'rpi5'):
        settings(root / 'etc/default/raspi-firmware',
                 'KERNEL=none\nINITRAMFS=none\n')
    elif board == 'r3a':
        divert(root, '/etc/initramfs/post-update.d/99-uboot')
    else:
        raise RuntimeError('Unsupported board: ' + board)
    inventory = kernel_package_command(['dpkg-query', '--admindir=' + str(root / 'var/lib/dpkg'),
                         '-W', '-f=${binary:Package}\t${db:Status-Status}\n'])
    installed = []
    for line in inventory.splitlines():
        package, status = line.split('\t')
        base = package.split(':')[0]
        protected = any(fnmatch.fnmatchcase(base, p) for p in patterns)
        if protected and status not in ('installed', 'not-installed', 'config-files'):
            raise RuntimeError('Unfinished boot package transaction: ' + package
                               + '; repair offline before using APT')
        # Headers contain no boot payload. Pins block additions/upgrades, but
        # leave them removable by the OS cleanup policy.
        if (status == 'installed' and not base.startswith('linux-headers-')
                and base != 'raspberrypi-kernel-headers'
                and protected):
            installed.append(package)
    apt_options = ([] if root == Path('/') else [
        '-o', 'Dir::State::status=' + str(root / 'var/lib/dpkg/status'),
        '-o', 'DPkg::Options::=--admindir=' + str(root / 'var/lib/dpkg')])
    held = set(kernel_package_command(['apt-mark', 'showhold', *apt_options]).split())
    missing = sorted(p for p in installed
                     if p not in held and p.split(':')[0] not in held)
    if missing:
        kernel_package_command(['apt-mark', 'hold', *apt_options, *missing])


def detect_board(root):
    model = (root / 'proc/device-tree/model').read_text().lower()
    if 'raspberry pi' in model:
        return 'rpi5' if 'pi 5' in model else 'cm4'
    if 'rock' in model and '3' in model:
        return 'r3a'
    raise RuntimeError('Unsupported board model: ' + model.rstrip('\0'))


def kernel_protection_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/'))
    parser.add_argument('--board', choices=('cm4', 'rpi4', 'rpi5', 'r3a'))
    # Works both from /usr/local/bin and a verified updater staging directory.
    parser.add_argument('--policy', type=Path,
                        default=Path(__file__).resolve().parents[3] / POLICY)
    args = parser.parse_args()
    root = args.root.resolve()
    board = args.board or detect_board(root)
    # APT uses POSIX record locks, not flock. Wait for any running APT action
    # to finish before changing hooks; dpkg-divert/apt-mark take backend locks.
    lock = root / 'var/lib/dpkg/lock-frontend'
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as stream:
        deadline = time.monotonic() + 120
        while True:
            try:
                fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('APT is busy; kernel protection must retry')
                time.sleep(1)
        protect(root, board, args.policy)


if __name__ == '__main__':
    try:
        kernel_protection_main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit('Kernel protection failed: ' + str(error))
