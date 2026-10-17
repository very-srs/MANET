#!/usr/bin/env python3
"""Wrap a new-updater CM4 archive in paths accepted by stable v0.569."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'node_tools'))
from manet_kernel_update import validate_payload, sha256


def stage(archive_path, destination):
    """Replace top-level kernel inputs with a verified embedded archive."""
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = {m.name.removeprefix('./').rstrip('/'): m for m in archive}
        manifest = validate_payload(archive, members, 'cm4')
        if not manifest:
            raise ValueError('CM4 bootstrap requires a matched kernel payload')
        kernel_bytes = archive.extractfile(
            members['manet-kernel/manifest.json']
        ).read()
        markers = [
            archive.extractfile(members[name]).read().decode('ascii')
            for name in ('etc/manet_version.txt', 'usr/local/bin/version.txt')
        ]
        if markers[0] != markers[1]:
            raise ValueError('inner version markers disagree')
        if any(
            name.startswith('usr/local/share/manet/bootstrap/')
            for name in members
        ):
            raise ValueError('recursive bootstrap is forbidden')
    target = destination / 'usr/local/share/manet/bootstrap'
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(archive_path, target / 'cm4-tools.tar.gz')
    digest = sha256(target / 'cm4-tools.tar.gz')
    (target / 'cm4-tools.tar.gz.sha256').write_text(
        digest + '  cm4-tools.tar.gz\n'
    )
    (target / 'manifest.json').write_text(
        json.dumps(
            dict(
                schema=1,
                board='cm4',
                version=markers[0].splitlines()[0],
                kernel_release=manifest['release'],
                archive_sha256=digest,
                kernel_manifest_sha256=hashlib.sha256(
                    kernel_bytes
                ).hexdigest(),
            ),
            sort_keys=True,
            indent=2,
        )
        + '\n'
    )
    # It is already safely inside the inner archive. The old updater must
    # never see a root-level manet-kernel directory or raw boot/module files.
    shutil.rmtree(destination / 'manet-kernel')
    link = (
        destination
        / 'etc/systemd/system/timers.target.wants/manet-release-bootstrap.timer'
    )
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to('../manet-release-bootstrap.timer')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--stage', type=Path, required=True)
    args = parser.parse_args()
    stage(args.archive, args.stage)
