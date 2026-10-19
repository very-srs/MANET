#!/usr/bin/env python3
"""Assemble an explicit board kernel input, or enforce updater archive limits."""
import argparse
import gzip
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'MANET/node_tools'))
from manet_kernel_update import (MAX_DOWNLOAD, MAX_EXPANDED, MANIFEST_LIMIT,
                                 validate_payload, sha256, KernelError)


def require_stock_bcfs(directory):
    """Morse Micro licenses its BCFs for distribution unmodified, with its license."""
    if not (directory / 'LICENSE').is_file():
        raise ValueError('Morse Micro license missing from the firmware directory. '
                         'Rebuild the SBC overlay.')
    spec = importlib.util.spec_from_file_location(
        'manet_morse_bcf', REPO / 'MANET/node_tools/manet-morse-bcf.py')
    bcf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bcf)
    modified = bcf.check_stock(directory)
    if modified:
        raise ValueError('Morse BCFs must ship as published; the node applies the TX power '
                         'policy. Rebuild the SBC overlay. Modified: ' + ', '.join(modified))


def check_archive(path, legacy=False):
    download = 64 * 1024 * 1024 if legacy else MAX_DOWNLOAD
    maximum = 512 * 1024 * 1024 if legacy else MAX_EXPANDED
    if path.stat().st_size > download:
        raise ValueError(f'Compressed archive exceeds updater limit of {download} bytes')
    expanded = 0
    with gzip.open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            expanded += len(chunk)
            if expanded > maximum:
                raise ValueError(f'Expanded archive exceeds updater limit of {maximum} bytes')
    with tarfile.open(path, 'r:gz') as archive:
        members = list(archive)
        if len(members) > 20000:
            raise ValueError('Archive exceeds updater member limit')
        if legacy:
            for member in members:
                name = member.name.removeprefix('./').rstrip('/')
                forbidden = ('etc/mesh.conf', 'etc/systemd/network', 'usr/lib/modules',
                             'usr/lib/firmware', 'usr/local/bin/node-manager.sh')
                if (name.split('/')[0] not in ('etc', 'usr', 'root') or
                        any(name == p or name.startswith(p + '/') for p in forbidden)):
                    raise ValueError('v0.569 excludes archive path: ' + name)
    print(f'Archive bounds: {path.stat().st_size} compressed, {expanded} expanded bytes')


def copy_regular(source, target):
    if source.is_symlink() or not source.is_file():
        raise ValueError('Kernel payload requires regular files: ' + str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o644)


def assemble(overlay, board, payload):
    modules = overlay / 'usr/lib/modules'
    releases = list(modules.iterdir())
    if len(releases) != 1 or not releases[0].is_dir() or releases[0].is_symlink():
        raise ValueError('SBC overlay must contain exactly one versioned module directory')
    release = releases[0].name
    boot = overlay / ('boot/firmware' if board == 'cm4' else 'boot')
    require_stock_bcfs(overlay / 'usr/lib/firmware/morse')
    roots = [(boot, 'boot'), (releases[0], 'modules/' + release),
             (overlay / 'usr/lib/firmware/morse', 'firmware/morse')]
    for source, relative in roots:
        if source.is_symlink() or not source.is_dir():
            raise ValueError('Missing regular kernel input directory: ' + str(source))
        for path in sorted(source.rglob('*')):
            name = path.relative_to(source)
            if relative.startswith('modules/') and name.parts[0] in ('build', 'source'):
                continue
            if path.is_symlink():
                raise ValueError('Unexpected kernel input symlink: ' + str(path))
            if path.is_dir():
                continue
            if relative == 'boot' and not (str(name) in ('kernel8.img', 'Image', 'initrd', 'uInitrd',
                                                        'overlays/README')
                                            or path.suffix in ('.dtb', '.dtbo')):
                continue
            copy_regular(path, payload / relative / name)
    if board == 'cm4':
        # This sentinel makes Pi firmware use the private overlay directory.
        readme = payload / 'boot/overlays/README'
        readme.parent.mkdir(parents=True, exist_ok=True)
        if not readme.exists():
            readme.write_text('Matched MANET kernel overlays.\n')
    files = {str(path.relative_to(payload)): dict(size=path.stat().st_size, sha256=sha256(path))
             for path in sorted(payload.rglob('*')) if path.is_file()}
    manifest = dict(schema=1, board=board, release=release, files=files)
    serialized = json.dumps(manifest, sort_keys=True, indent=2) + '\n'
    if len(serialized.encode()) > MANIFEST_LIMIT:
        raise ValueError('Kernel manifest exceeds updater limit')
    (payload / 'manifest.json').write_text(serialized)
    # Use the runtime validator, including inventory/layout checks, before tar.
    with tempfile.TemporaryFile() as stream:
        with tarfile.open(fileobj=stream, mode='w') as archive:
            archive.add(payload, arcname='manet-kernel')
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode='r:') as archive:
            validate_payload(archive, {entry.name: entry for entry in archive}, board)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-archive', type=Path)
    parser.add_argument('--legacy', action='store_true')
    parser.add_argument('--board', choices=('cm4', 'r3a'))
    parser.add_argument('--overlay', type=Path)
    parser.add_argument('--stage', type=Path)
    parser.add_argument('--install', action='store_true', help='add a matched-set inventory to an install archive')
    args = parser.parse_args()
    try:
        if args.check_archive:
            check_archive(args.check_archive, args.legacy)
            return
        if not all((args.board, args.overlay, args.stage)):
            parser.error('--board, --overlay and --stage are required')
        with tempfile.TemporaryDirectory(prefix='manet-kernel-package-') as scratch:
            payload = Path(scratch) / 'manet-kernel'
            manifest = assemble(args.overlay, args.board, payload)
            if args.install:
                if args.board == 'cm4':
                    copy_regular(payload / 'boot/overlays/README',
                                 args.stage / 'boot/firmware/overlays/README')
                # Install archives already contain the SBC overlay at root.
                # Record its identity without duplicating a second kernel set.
                target = args.stage / 'usr/local/share/manet/installed-kernel.json'
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
            else:
                target = args.stage / 'manet-kernel'
                if target.exists():
                    raise ValueError('Kernel payload destination already exists')
                shutil.copytree(payload, target)
            print(f"Kernel input: {args.board} {manifest['release']} ({len(manifest['files'])} files)")
    except (OSError, ValueError, KernelError) as error:
        parser.exit(1, f'Kernel packaging failed: {error}\n')


if __name__ == '__main__':
    main()
