#!/usr/bin/env python3
"""Resume one embedded CM4 kernel update after a legacy tools installation."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from manet_kernel_update import KernelError, kernel_enabled


def load_updater():
    """Load the newly installed updater independently of legacy recovery."""
    spec = importlib.util.spec_from_file_location(
        'manet_bootstrap_updater', Path(__file__).with_name('node-update.py')
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def complete(updater, data):
    """Require the exact candidate, committed selector and installed tools."""
    from manet_kernel_update import sha256

    path = updater.root / 'var/lib/manet-kernel/confirmed.json'
    if updater.pending.exists() or not path.is_file():
        return False
    confirmed = json.loads(path.read_text())
    return (
        confirmed.get('release') == data['kernel_release']
        and confirmed.get('slot') == data['kernel_manifest_sha256'][:24]
        and updater.kernel().release() == data['kernel_release']
        and confirmed.get('config_sha256')
        == sha256(updater.root / 'boot/firmware/config.txt')
        and updater.marker.read_text()
        == (updater.root / 'usr/local/bin/version.txt').read_text()
        and updater.marker.read_text().splitlines()[0] == data['version']
    )


def apply_embedded_update(root=Path('/'), module=None, command=subprocess.run):
    """Resume or finish the board-bound update after the outer tools commit."""
    module = module or load_updater()
    updater = module.Updater(root)
    # A mistakenly copied bootstrap on another board must not even inspect
    # the CM4 payload or create a kernel transaction.
    if updater.board() != 'cm4':
        return False
    # A live transaction must still resolve even if the operator opts out now.
    # Retain the payload and timer so re-enabling can pick it up later.
    if not updater.pending.exists() and not kernel_enabled(root):
        return False
    base = root / 'usr/local/share/manet/bootstrap'
    descriptor = base / 'manifest.json'
    if not descriptor.exists():
        return False
    data = json.loads(descriptor.read_text())
    import re

    if (
        data.get('schema') != 1
        or data.get('board') != 'cm4'
        or not re.fullmatch(
            '[a-f0-9]{64}', data.get('kernel_manifest_sha256', '')
        )
        or not re.fullmatch('[a-f0-9]{64}', data.get('archive_sha256', ''))
        or not re.fullmatch(
            '[A-Za-z0-9][A-Za-z0-9_.+-]{0,95}', data.get('kernel_release', '')
        )
    ):
        raise ValueError('invalid embedded update descriptor')
    module.version(data['version'])
    if not complete(updater, data):
        if updater.pending.exists():
            if not (updater.recovery / 'kernel.json').is_file():
                return False  # The legacy updater still owns its transaction.
            transaction = json.loads(
                (updater.recovery / 'kernel.json').read_text()
            )
            if (
                transaction.get('manifest_sha256')
                != data['kernel_manifest_sha256']
            ):
                raise ValueError('another kernel transaction is pending')
        else:
            cancelled = updater.state / 'last-kernel-trial.json'
            if cancelled.exists():
                previous = json.loads(cancelled.read_text())
                if (
                    previous.get('phase') == 'cancelled'
                    and previous.get('manifest_sha256')
                    == data['kernel_manifest_sha256']
                ):
                    command(
                        [
                            'systemctl',
                            'disable',
                            '--now',
                            'manet-release-bootstrap.timer',
                        ],
                        check=True,
                        timeout=30,
                    )
                    return False
            # Both markers commit last in v0.569. Do not race partial install.
            if (
                updater.marker.read_text().splitlines()[0] != data['version']
                or updater.marker.read_text()
                != (root / 'usr/local/bin/version.txt').read_text()
            ):
                return False
        from manet_kernel_update import sha256

        archive = base / 'cm4-tools.tar.gz'
        if sha256(archive) != data['archive_sha256']:
            raise ValueError('embedded update hash changed')
        updater.update(local_archive=archive, local_version=data['version'])
        if not complete(updater, data):
            return False
    # The update lock excludes installation while its own cleanup runs.
    # Recheck after acquiring it because another updater may have started.
    import fcntl

    with (root / 'run/manet-update.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        if not complete(updater, data):
            return False
        command(
            ['systemctl', 'disable', '--now', 'manet-release-bootstrap.timer'],
            check=True,
            timeout=30,
        )
        # Retain a small completion record before removing the large payload.
        module.atomic_file(
            updater.state / 'bootstrap-complete.json',
            data=json.dumps(data, sort_keys=True).encode(),
            mode=0o600,
        )
        for name in (
            'cm4-tools.tar.gz',
            'cm4-tools.tar.gz.sha256',
            'manifest.json',
        ):
            (base / name).unlink(missing_ok=True)
        module.sync_directory(base)
        for name in (
            'manet-release-bootstrap.service',
            'manet-release-bootstrap.timer',
        ):
            (root / 'etc/systemd/system' / name).unlink(missing_ok=True)
        command(['systemctl', 'daemon-reload'], check=True, timeout=30)
    return True


if __name__ == '__main__':
    try:
        apply_embedded_update()
    except (
        KernelError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print('manet-release-bootstrap: ' + str(error), file=sys.stderr)
        sys.exit(1)
