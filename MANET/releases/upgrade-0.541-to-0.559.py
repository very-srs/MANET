#!/usr/bin/env python3
"""One-time CM4 upgrade from MANET 0.541 to stable 0.559.

Copy this file to the radio and run: sudo python3 upgrade-0.541-to-0.559.py
Use --check to download and validate without installing. Use wired access and
keep power connected. Reboot after success to start all newly enabled services.
"""

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile


VERSION = '0.559'
COMMIT = '099f1430f00bb814b17a10f05dfcd0578d2121ee'
BASE = 'https://github.com/very-srs/MANET/releases/download/v0.559/'
PACKAGE = 'cm4-tools.tar.gz'
SIZE = 6259527
SHA256 = 'bc848d7c9814bbdbdc31fb9a1064c13bf240e42a83cfcff7c6f432f150f4d8b3'
STATE = Path('/var/lib/manet-upgrade-0.559')
BOOTSTRAP = ('node-update.sh', 'node-update.py', 'manet_release.py')
BACKUP = ('etc/mesh.conf', 'etc/mesh_ipv4_state', 'etc/manet',
          'etc/manet_version.txt', 'etc/systemd/system', 'etc/systemd/network',
          'etc/wpa_supplicant', 'etc/hostapd', 'etc/dnsmasq.d',
          'etc/avahi', 'etc/chrony', 'etc/modprobe.d',
          'var/lib/mesh_if', 'var/lib/mesh_24_if', 'var/lib/mesh_5_if',
          'var/lib/halow_if', 'var/lib/no_mesh_if', 'var/lib/ap_interface',
          'var/lib/iface_map', 'usr/local/bin/node-manager.sh')


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def preflight(root=Path('/')):
    model = (root / 'proc/device-tree/model').read_text().rstrip('\0')
    if 'Raspberry Pi Compute Module 4' not in model:
        raise RuntimeError('This script is for Compute Module 4 only')
    current = (root / 'etc/manet_version.txt').read_text().splitlines()[0]
    if current not in ('0.541', VERSION):
        raise RuntimeError(f'Expected 0.541 or a 0.559 retry; found {current}')
    release = dict(line.split('=', 1) for line in
                   (root / 'etc/os-release').read_text().splitlines()
                   if '=' in line and not line.startswith('#'))
    if release.get('VERSION_ID', '').strip('"') != '13':
        raise RuntimeError('This upgrade requires the Debian/Raspberry Pi OS 13 image; '
                           'it does not upgrade the operating system')
    (root / 'etc/mesh.conf').read_text()
    if (root / 'var/lib/manet-update/recovery').exists():
        raise RuntimeError('A newer updater recovery exists; finish that update first')
    return current


def fetch(name, target, limit):
    temporary = target.with_name(target.name + '.part')
    subprocess.run([
        'curl', '--fail', '--silent', '--show-error', '--location',
        '--proto', '=https', '--proto-redir', '=https',
        '--connect-timeout', '10', '--max-time', '120', '--retry', '2',
        '--max-filesize', str(limit), '--output', str(temporary), BASE + name,
    ], check=True, timeout=400)
    if temporary.stat().st_size > limit:
        raise RuntimeError('Download exceeds size limit')
    temporary.replace(target)


def prepare(work):
    """Authenticate the exact published payload before importing its updater."""
    package = work / PACKAGE
    if (not package.exists() or package.stat().st_size != SIZE
            or digest(package) != SHA256):
        fetch(PACKAGE, package, SIZE)
    if package.stat().st_size != SIZE or digest(package) != SHA256:
        raise RuntimeError('Published 0.559 tools archive failed SHA-256 verification')
    manifest_path = work / 'manet-release.json'
    fetch(manifest_path.name, manifest_path, 1024 * 1024)
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get('version') != VERSION or manifest.get('commit') != COMMIT
            or manifest.get('tag') != 'v' + VERSION
            or manifest.get('assets', {}).get(PACKAGE) != {'size': SIZE, 'sha256': SHA256}):
        raise RuntimeError('Release manifest does not match the pinned 0.559 payload')
    checksum = work / (PACKAGE + '.sha256')
    fetch(checksum.name, checksum, 1024)
    with tarfile.open(package, 'r:gz') as archive:
        for name in BOOTSTRAP:
            matches = [entry for entry in archive.getmembers()
                       if entry.name.removeprefix('./') == 'usr/local/bin/' + name]
            if len(matches) != 1 or not matches[0].isfile():
                raise RuntimeError('Missing bootstrap file: ' + name)
            (work / name).write_bytes(archive.extractfile(matches[0]).read())
    sys.path.insert(0, str(work))
    spec = importlib.util.spec_from_file_location('manet_0559_updater', work / 'node-update.py')
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    updater.select_release = lambda *args: manifest
    return updater, package, checksum


def backup(updater, work, root=Path('/')):
    target = work / 'configuration-before-upgrade.tar.gz'
    if target.exists():
        return
    scratch = work / 'configuration-before-upgrade.partial'
    with tarfile.open(scratch, 'w:gz', dereference=False) as archive:
        for name in BACKUP:
            path = root / name
            if path.exists() or path.is_symlink():
                archive.add(path, arcname=name)
    updater.atomic_file(target, source=scratch, mode=0o600)
    scratch.unlink()


def install_cached(module, work, package, checksum, root=Path('/')):
    updater = module.Updater(root=root)

    def cached_download(url, target, limit):
        sources = {BASE + PACKAGE: package, BASE + PACKAGE + '.sha256': checksum}
        source = sources.get(url)
        if source is None or source.stat().st_size > limit:
            raise RuntimeError('Unexpected updater download: ' + url)
        shutil.copyfile(source, target)

    updater.download = cached_download
    updater.update()
    for name in module.MARKERS:
        if (root / name).read_text().splitlines()[0] != VERSION:
            raise RuntimeError('Update did not complete; another update may be running')
    if updater.pending.exists():
        raise RuntimeError('Update remains incomplete; rerun this same script')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='check compatibility and downloads without installing')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Run this script with sudo')
    if os.uname().machine != 'aarch64':
        raise RuntimeError('This upgrade requires the 64-bit CM4 image')
    os.umask(0o077)
    current = preflight()
    for name in ('curl', 'systemctl', 'nft', 'apt-get'):
        if not shutil.which(name):
            raise RuntimeError('Required command is missing: ' + name)
    subprocess.run([sys.executable, '-c', 'import google.protobuf'], check=True)
    if STATE.is_symlink():
        raise RuntimeError('Unsafe upgrade directory')
    STATE.mkdir(mode=0o700, exist_ok=True)
    if STATE.stat().st_uid != 0:
        raise RuntimeError('Upgrade directory must be owned by root')
    STATE.chmod(0o700)
    with (STATE / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(f'Checking CM4 {current} -> {VERSION}; downloads are pinned to v{VERSION}', flush=True)
        module, package, checksum = prepare(STATE)
        updater = module.Updater()
        # Retain an additional margin above the stable updater's normal reserve.
        module.RESERVE = 128 * 1024 * 1024
        members = updater.validate(package, checksum, PACKAGE, VERSION)
        with tempfile.TemporaryDirectory(prefix='check-', dir=STATE) as scratch:
            stage = Path(scratch) / 'stage'
            updater.stage(package, members, stage)
            subprocess.run(['nft', '--check', '--file',
                            str(stage / 'usr/local/share/manet/dhcp-isolation.nft')], check=True)
        if args.check:
            print('Preflight passed. No tools, configuration, packages or services were changed.')
            return
        backup(module, STATE)
        print('Configuration backup: ' + str(STATE / 'configuration-before-upgrade.tar.gz'), flush=True)
        install_cached(module, STATE, package, checksum)
        print('Upgrade complete: 0.559. Reboot with sudo reboot to start all enabled services.')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(f'Upgrade stopped: {error}\nKeep power/internet connected. Correct the error and rerun '
              'this same script; do not run radio-setup.sh or remove the updater retry marker.', file=sys.stderr)
        sys.exit(1)
