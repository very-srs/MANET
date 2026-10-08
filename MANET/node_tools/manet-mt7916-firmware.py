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
import sys
import tempfile
import zlib

NAMES = ('mt7916_wm.bin', 'mt7916_wa.bin', 'mt7916_rom_patch.bin')
OWNER = '.manet-mt7916'
WM_SHA256 = '18a2e1d03f17913ef0450a905536d5defcdf78ee1d98a1232649c31a70a63e1e'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def enabled(path):
    # Keep the positioning service's literal key/value parsing, including quotes.
    result = {}
    try:
        text = path.read_text()
    except FileNotFoundError:
        return False
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            result[key.strip()] = value.strip().strip('\"\'')
    return result.get('positioning', 'n').lower() == 'y'


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

    def run(self):
        try:
            if not self.updates.exists():
                if not enabled(self.root / 'etc/mesh.conf'):
                    return True, 'disabled; stock firmware selected for next boot'
                self.updates.mkdir(parents=True, exist_ok=True)
            with locked(self.updates):
                try:
                    return True, self.prepare(enabled(self.root / 'etc/mesh.conf'))
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
    args = parser.parse_args()
    ok, message = Firmware(args.root.resolve()).run()
    print('manet-mt7916-firmware: ' + message, file=sys.stderr)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
