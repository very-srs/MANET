#!/usr/bin/env python3
"""Stage the native Debian follower package and DFS-tagged regulatory DB."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def stage(destination, package, regdb, board):
    """Check native package identity and stage board-specific release inputs."""
    metadata = subprocess.check_output(
        ['dpkg-deb', '-f', str(package), 'Package', 'Version', 'Architecture'],
        text=True,
    ).splitlines()
    if metadata != [
        'Package: hostapd',
        'Version: 2:2.10-24+manet1',
        'Architecture: arm64',
    ]:
        raise ValueError(
            'expected the native Debian 2:2.10-24+manet1 arm64 hostapd package'
        )
    if board == 'cm4' and (regdb is None or regdb.read_bytes()[:4] != b'RGDB'):
        raise ValueError('invalid regulatory.db header')
    target = destination / 'usr/local/share/manet/release'
    target.mkdir(parents=True, exist_ok=True)
    files = {}
    inputs = [(package, 'hostapd.deb')]
    if board == 'cm4':
        inputs.append((regdb, 'regulatory.db'))
    for source, name in inputs:
        shutil.copyfile(source, target / name)
        (target / name).chmod(0o644)
        files[name] = hashlib.sha256((target / name).read_bytes()).hexdigest()
    (target / 'manifest.json').write_text(
        json.dumps(
            dict(
                schema=1,
                board=board,
                hostapd_version='2:2.10-24+manet1',
                files=files,
            ),
            sort_keys=True,
            indent=2,
        )
        + '\n'
    )
    repo = Path(__file__).resolve().parents[2]
    pin = destination / 'etc/apt/preferences.d/01-manet-hostapd'
    pin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(repo / 'MANET/etc/apt/preferences.d/01-manet-hostapd', pin)
    if board == 'cm4':
        (destination / 'root').mkdir(exist_ok=True)
        shutil.copyfile(regdb, destination / 'root/regulatory.db')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--hostapd', type=Path, required=True)
    parser.add_argument('--regdb', type=Path)
    parser.add_argument(
        '--board', choices=('cm4', 'r3a', 'rpi5'), required=True
    )
    args = parser.parse_args()
    stage(args.stage, args.hostapd, args.regdb, args.board)
