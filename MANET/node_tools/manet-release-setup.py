#!/usr/bin/env python3
"""Install and verify release-owned userland payloads before version commit."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import sys

from manet_kernel_update import sha256

PACKAGE_VERSION = '2:2.10-24+manet1'
PIN = (
    'Package: hostapd\nPin: version 2:2.10-24+manet*\n' 'Pin-Priority: 1001\n'
)


def atomic_copy(source, target):
    """Replace one regulatory file and flush its containing directory."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.manet-release-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(source.read_bytes())
            os.fchmod(stream.fileno(), 0o644)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
        fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


def install_release_payload(root=Path('/'), run=subprocess.run):
    """Only the supplied hash-verified Debian package can satisfy this release."""
    base = root / 'usr/local/share/manet/release'
    manifest = base / 'manifest.json'
    if not manifest.exists():
        return
    data = json.loads(manifest.read_text())
    board = data.get('board')
    expected = {'hostapd.deb'} | (
        {'regulatory.db'} if board == 'cm4' else set()
    )
    if (
        data.get('schema') != 1
        or set(data.get('files', {})) != expected
        or board not in ('cm4', 'r3a', 'rpi5')
        or data.get('hostapd_version') != PACKAGE_VERSION
    ):
        raise ValueError('invalid release payload manifest')
    for name, value in data['files'].items():
        path = base / name
        if (
            path.is_symlink()
            or not path.is_file()
            or not re.fullmatch('[a-f0-9]{64}', str(value))
            or sha256(path) != value
        ):
            raise ValueError('release payload digest mismatch: ' + name)
    pin = root / 'etc/apt/preferences.d/01-manet-hostapd'
    if pin.read_text() != PIN:
        raise ValueError('hostapd APT pin does not match the release policy')
    package = base / 'hostapd.deb'
    metadata = run(
        ['dpkg-deb', '-f', str(package), 'Package', 'Version', 'Architecture'],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    if metadata.splitlines() != [
        'Package: hostapd',
        'Version: ' + PACKAGE_VERSION,
        'Architecture: arm64',
    ]:
        raise ValueError('wrong hostapd Debian package')
    # Dependencies match the already installed Debian hostapd. Do not fetch
    # arbitrary package replacements during an offline transaction retry.
    # Debian's conffiles include /etc/default/hostapd. Preserve local edits
    # without an interactive dpkg prompt during unattended installation.
    run(
        ['dpkg', '--force-confold', '--install', str(package)],
        check=True,
        timeout=120,
        env=dict(os.environ, DEBIAN_FRONTEND='noninteractive'),
    )
    installed = run(
        [
            'dpkg-query',
            '-W',
            '-f=${Status} ${Version} ${Architecture}',
            'hostapd',
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    if (
        installed.strip()
        != 'install ok installed ' + PACKAGE_VERSION + ' arm64'
    ):
        raise ValueError('hostapd package installation did not verify')
    version = run(
        [str(root / 'usr/sbin/hostapd'), '-v'],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if (
        version.returncode not in (0, 1)
        or version.stdout
        or not version.stderr.splitlines()
        or version.stderr.splitlines()[0]
        != 'hostapd v2.10-manet-dfs-follow-v1'
        or re.search(r'not found|no version information', version.stderr, re.I)
    ):
        raise ValueError('installed hostapd lacks DFS follower support')
    policy = run(
        ['apt-cache', 'policy', 'hostapd'],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    if not re.search(
        r'^\s*Candidate: ' + re.escape(PACKAGE_VERSION) + r'\s*$', policy, re.M
    ):
        raise ValueError('hostapd APT candidate is not the pinned package')
    model = (root / 'proc/device-tree/model').read_text()
    if board == 'cm4' and 'Raspberry Pi Compute Module 4' in model:
        for target in ('root/regulatory.db', 'usr/lib/firmware/regulatory.db'):
            atomic_copy(base / 'regulatory.db', root / target)
            if sha256(root / target) != data['files']['regulatory.db']:
                raise ValueError(
                    'installed regulatory database did not verify'
                )
    # The new regulatory flags take effect on reboot. No live global regdom
    # change is made while HaLow or an existing Wi-Fi role owns a channel.


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        install_release_payload()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('manet-release-setup: ' + str(error), file=sys.stderr)
        sys.exit(1)
