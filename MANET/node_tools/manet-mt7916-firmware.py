#!/usr/bin/env python3
"""Prepare the opt-in MT7916 firmware override; never load or reload a driver."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zlib

NAMES = ('mt7916_wm.bin', 'mt7916_wa.bin', 'mt7916_rom_patch.bin')
OWNER = '.manet-mt7916'
WM_SHA256 = '18a2e1d03f17913ef0450a905536d5defcdf78ee1d98a1232649c31a70a63e1e'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def positioning(path):
    # Keep the positioning service's literal key/value parsing, including quotes.
    result = {}
    try:
        text = path.read_text()
    except FileNotFoundError:
        return 'n'
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            result[key.strip()] = value.strip().strip('\"\'')
    return result.get('positioning', 'n').lower()


def enabled(path):
    return positioning(path) == 'y'


def timing_driver(root):
    # Inspect the installed module selected for this kernel, not a module left
    # loaded from an older installation. modinfo never loads anything.
    try:
        result = subprocess.run(['modinfo', '-b', str(root), '-k', os.uname().release,
                                 '-F', 'manet_timing', 'mt7915e'],
                                capture_output=True, text=True, timeout=10)
        return result.returncode == 0 and result.stdout.strip() == '1'
    except (OSError, subprocess.SubprocessError):
        return False


def patch_wm(stock, spec):
    if len(stock) != spec['size'] or digest(stock) != spec['input_sha256']:
        raise ValueError('stock WM hash mismatch')
    output = bytearray(stock)
    trailer = len(stock) - 36
    count = stock[trailer + 2]
    regions, offset = [], 0
    for i in range(count):
        desc = trailer - (count - i) * 40
        length, = struct.unpack_from('<I', stock, desc + 20)
        if stock[desc + 24] not in (0, 0x20) or length < 4 or length % 4:
            raise ValueError('unsupported WM region')
        regions.append((offset, length))
        offset += length
    if offset != spec['data_end'] or offset > trailer - count * 40:
        raise ValueError('invalid WM layout')
    for edit in spec['replacements']:
        start, data = edit['offset'], bytes.fromhex(edit['hex'])
        if not any(o <= start and start + len(data) <= o + n - 4 for o, n in regions):
            raise ValueError('patch outside WM payload')
        output[start:start + len(data)] = data
    for start, length in regions:
        state = 0
        # One Fibonacci step per LE32 word. The last word cancels the residue.
        for word, in struct.iter_unpack('<I', memoryview(output)[start:start + length]):
            state = (state >> 1) ^ (((state & 0x10921111).bit_count() & 1) << 31) ^ word
        tail = start + length - 4
        struct.pack_into('<I', output, tail, struct.unpack_from('<I', output, tail)[0] ^ state)
    struct.pack_into('<I', output, len(output) - 4, zlib.crc32(output[:-4]))
    if digest(output) != spec['output_sha256']:
        raise ValueError('sealed WM hash mismatch')
    return bytes(output)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked(directory):
    # Serialize preparers only. A directory lock needs no lock-file writes and
    # has no involvement in probing/loading the radio.
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def file_hash(path):
    if path.is_symlink():
        return None
    try:
        return digest(path.read_bytes())
    except FileNotFoundError:
        return None


def identity(path):
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_mtime_ns]


def staged_file(path, data):
    # The temporary directory is on the destination filesystem. Publish only
    # after a complete write and fsync, preserving this inode's identity.
    with path.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o644)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class Firmware:
    def __init__(self, root=Path('/')):
        self.root = Path(root)
        self.assets = self.root / 'usr/local/share/manet'
        self.updates = self.root / 'lib/firmware/updates/mediatek'
        self.owner = self.updates / OWNER
        self.state = self.root / 'var/lib/manet'
        self.pending = self.state / 'mt7916-probe-pending'
        self.disabled = self.state / 'mt7916-auto-disabled'

    def read_state(self, path):
        try:
            record = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        if (record.get('schema') != 1 or
                any(not isinstance(record.get(k), str)
                    for k in ('boot_id', 'wm_sha256', 'positioning'))):
            raise ValueError(f'invalid recovery state: {path.name}')
        return record

    def write_state(self, path, record):
        # Make newly created parent directories durable too, including the
        # first install where /var/lib/manet does not exist yet.
        missing, parent = [], self.state
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir(exist_ok=True)
            sync_directory(directory.parent)
        with tempfile.TemporaryDirectory(prefix='.mt7916-', dir=self.state) as temp:
            staged = Path(temp) / path.name
            staged_file(staged, json.dumps(record, sort_keys=True).encode() + b'\n')
            os.replace(staged, path)
            sync_directory(self.state)

    def remove_state(self, path):
        if path.exists():
            path.unlink()
            sync_directory(self.state)

    def boot_id(self):
        value = (self.root / 'proc/sys/kernel/random/boot_id').read_text().strip()
        if not value:
            raise ValueError('missing boot identity')
        return value

    def present(self):
        # Presence is available before coldplug; no bound driver is required.
        # The auxiliary 0x790a function alone cannot load this firmware.
        return any((device / 'vendor').read_text().strip() == '0x14c3' and
                   (device / 'device').read_text().strip() == '0x7906'
                   for device in (self.root / 'sys/bus/pci/devices').glob('*'))

    def survived(self):
        # Also check the target here so a premature manual invocation cannot
        # erase the marker. The unit orders this once, without polling.
        result = subprocess.run(['systemctl', 'is-active', '--quiet', 'multi-user.target'],
                                capture_output=True, timeout=10)
        if result.returncode:
            return False
        # CM4 enumerates PCI before userspace coldplug. Require every MT7916
        # primary function to be bound, or a successfully read empty inventory.
        primary, secondary = False, False
        for device in (self.root / 'sys/bus/pci/devices').iterdir():
            if (device / 'vendor').read_text().strip() != '0x14c3':
                continue
            pci_id = (device / 'device').read_text().strip()
            if pci_id == '0x790a':
                secondary = True
            if pci_id == '0x7906':
                primary = True
                if not (device / 'driver').is_symlink():
                    return False
                if (device / 'driver').resolve(strict=True).name != 'mt7915e':
                    return False
                # The driver symlink is installed before probe() runs. wiphy
                # registration follows synchronous MCU/firmware initialization
                # in mt7915_register_device(), so also require that evidence.
                if not any(p.is_dir() for p in (device / 'ieee80211').glob('phy*')):
                    return False
        # 0x7906 is MT7916's primary function; 0x790a is its auxiliary HIF.
        # Binding only the auxiliary function does not prove firmware probed.
        return primary or not secondary

    def select(self, mode):
        value = positioning(self.root / 'etc/mesh.conf')
        pending = self.read_state(self.pending)
        disabled = self.read_state(self.disabled)
        current = self.boot_id() if pending or (mode == 'boot' and value == 'y') else None
        stale = pending and pending['boot_id'] != current
        if stale:
            # Record the failure before removing either the firmware or the
            # evidence. A crash during recovery must never silently rearm it.
            disabled = dict(pending, positioning=value, reason='boot did not reach survival check')
            self.write_state(self.disabled, disabled)
            self.deactivate()
            self.remove_state(self.pending)
            pending = None
        if mode == 'survived':
            if pending and self.survived():
                self.remove_state(self.pending)
                return 'boot survived; pending marker cleared'
            if not disabled:
                return 'pending marker retained; survival unconfirmed' if pending else 'no pending boot'
        # An observed value change rearms the choice; unrelated config edits
        # and repeated writes of the same value do not. --rearm is explicit.
        if disabled and mode != 'survived':
            if mode == 'rearm' or (not stale and value != disabled['positioning']):
                self.remove_state(self.disabled)
                disabled = None
        if disabled:
            return ('AUTO-DISABLED after failed boot; WM ' + disabled['wm_sha256'] +
                    '; change positioning or run --rearm; ' + self.prepare(False))
        if value != 'y':
            return self.prepare(False)
        if not self.present():
            return 'no MT7916 primary PCI function (14c3:7906); ' + self.prepare(False)
        if not timing_driver(self.root):
            return 'installed mt7915e lacks manet_timing=1 for running kernel; ' + self.prepare(False)
        if mode == 'boot' and not pending:
            self.write_state(self.pending, {'schema': 1, 'boot_id': current,
                                           'wm_sha256': WM_SHA256, 'positioning': value})
        return self.prepare(True)

    def owners(self):
        try:
            record = json.loads(self.owner.read_text())
        except FileNotFoundError:
            return {}
        if record.get('schema') != 1 or not isinstance(record.get('files'), dict):
            raise ValueError('invalid MT7916 owner manifest')
        return record['files']

    def owned(self, name, owners):
        path = self.updates / name
        value = file_hash(path)
        if name == NAMES[0]:
            return value == WM_SHA256
        record = owners.get(name, {})
        return (value is not None and value == record.get('sha256')
                and identity(path) == record.get('identity'))

    def deactivate(self):
        # The unique patched WM hash remains recognizable without either JSON
        # file. Remove it first; preserve the owner record if cleanup fails so
        # the next boot can retry WA/ROM removal.
        wm = self.updates / NAMES[0]
        if self.owned(NAMES[0], {}):
            wm.unlink()
            sync_directory(self.updates)
        owners = self.owners()
        for name in NAMES[1:]:
            if self.owned(name, owners):
                (self.updates / name).unlink()
                sync_directory(self.updates)
        if self.owner.exists():
            self.owner.unlink()
            sync_directory(self.updates)

    def prepare(self, enable):
        if not enable:
            self.deactivate()
            if any(os.path.lexists(self.updates / n) for n in NAMES):
                return 'disabled; MANET overrides removed, unowned overrides preserved'
            return 'disabled; stock firmware selected for next boot'
        manifest = json.loads((self.assets / 'mt7916-firmware.json').read_text())
        spec = manifest['wm_patch']
        expected = {n: manifest['linux_firmware']['files'][n]['sha256'] for n in NAMES}
        expected[NAMES[0]] = spec['output_sha256']
        if expected[NAMES[0]] != WM_SHA256:
            raise ValueError('unexpected WM output hash')
        changed = [n for n in NAMES if file_hash(self.updates / n) != expected[n]]
        if not changed:
            # Ready boots read only the intent and installed set, not the stock
            # cache. Do not reseal, adopt foreign companions or rewrite metadata.
            return 'enabled; firmware already prepared'
        owners = self.owners()
        for name in changed:
            if os.path.lexists(self.updates / name) and not self.owned(name, owners):
                raise ValueError(f'unowned firmware override: {name}')
        stock = {}
        for name in NAMES:
            data = (self.assets / 'firmware/mt7916' / name).read_bytes()
            if digest(data) != manifest['linux_firmware']['files'][name]['sha256']:
                raise ValueError(f'stock {name} hash mismatch')
            stock[name] = data
        if NAMES[0] in changed:
            stock[NAMES[0]] = patch_wm(stock[NAMES[0]], spec)
        with tempfile.TemporaryDirectory(prefix='.manet-mt7916-', dir=self.updates) as temp:
            work = Path(temp)
            for name in changed:
                staged_file(work / name, stock[name])
                if name != NAMES[0]:
                    owners[name] = {'sha256': expected[name], 'identity': identity(work / name)}
            if any(n != NAMES[0] for n in changed):
                # Record staged inode identities first. A crash between renames
                # is recoverable without adopting an identical foreign file.
                staged_file(work / OWNER, json.dumps({'schema': 1, 'files': owners},
                                                     sort_keys=True).encode() + b'\n')
                os.replace(work / OWNER, self.owner)
                sync_directory(self.updates)
            # WM is published last, after both companions are complete.
            for name in (*NAMES[1:], NAMES[0]):
                if name in changed:
                    os.replace(work / name, self.updates / name)
                    sync_directory(self.updates)
        return 'enabled; firmware prepared for next boot'

    def run(self, mode='apply'):
        try:
            if not self.updates.exists():
                if (not self.state.exists() and not enabled(self.root / 'etc/mesh.conf')
                        and mode != 'survived'):
                    return True, 'disabled; stock firmware selected for next boot'
                self.updates.mkdir(parents=True, exist_ok=True)
            with locked(self.updates):
                try:
                    return True, self.select(mode)
                except Exception as error:
                    try:
                        self.deactivate()
                        recovery = 'owned overrides removed; other firmware left untouched'
                    except Exception as cleanup:
                        recovery = f'override cleanup failed, retry at next boot: {cleanup}'
                    return False, f'{error}; {recovery}'
        except Exception as error:
            return False, f'{error}; preparation unavailable, retry at next boot'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/'), help='offline root for testing')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--boot', dest='mode', action='store_const', const='boot',
                       help='arm boot recovery before coldplug')
    modes.add_argument('--survived', dest='mode', action='store_const', const='survived',
                       help='clear this boot only after multi-user and PCI binding')
    modes.add_argument('--rearm', dest='mode', action='store_const', const='rearm',
                       help='explicitly retry an auto-disabled choice for next boot')
    args = parser.parse_args()
    ok, message = Firmware(args.root.resolve()).run(args.mode or 'apply')
    print('manet-mt7916-firmware: ' + message, file=sys.stderr)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
