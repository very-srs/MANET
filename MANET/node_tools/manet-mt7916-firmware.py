#!/usr/bin/env python3
"""Prepare the pinned MT7916 firmware without loading or reloading drivers."""
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
STOCK_WM_SHA256 = ('64d86cfa4f00f9452b4706389d648d4ce0e'
                  '563ddd512cc9845ba4e4442e99cb0')
WM_SHA256 = '18a2e1d03f17913ef0450a905536d5defcdf78ee1d98a1232649c31a70a63e1e'


def digest(data):
    """Return the SHA-256 hex digest of firmware bytes."""
    return hashlib.sha256(data).hexdigest()


def config_choice(path, requested_key, default="n"):
    """Read a literal config choice, without evaluating shell syntax."""
    # Keep the positioning service's literal key/value parsing, including quotes.
    result = {}
    try:
        text = path.read_text()
    except FileNotFoundError:
        return default
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            result[key.strip()] = value.strip().strip('\"\'')
    return result.get(requested_key, default).lower()


def timing_driver(root):
    """Check the installed mt7915e timing tag without loading the driver."""
    # Inspect the installed module selected for this kernel with modinfo.
    # A loaded module may predate installation. modinfo loads no modules.
    try:
        # A missing or stalled lookup cannot qualify the module for patching.
        # Give modinfo at most 10 s to report the single timing flag.
        result = subprocess.run(['modinfo', '-b', str(root), '-k', os.uname().release,
                                 '-F', 'manet_timing', 'mt7915e'],
                                capture_output=True, text=True, timeout=10)
        return result.returncode == 0 and result.stdout.strip() == '1'
    except (OSError, subprocess.SubprocessError):
        return False


def patch_wm(stock, spec):
    """Apply bounded WM replacements and reseal the expected output image."""
    if len(stock) != spec['size'] or digest(stock) != spec['input_sha256']:
        raise ValueError('stock WM hash mismatch')
    output = bytearray(stock)
    # The final 36 bytes hold the trailer. Byte 2 counts 40-byte descriptors
    # immediately before it, each describing a consecutive data region.
    trailer = len(stock) - 36
    count = stock[trailer + 2]
    regions, offset = [], 0
    for i in range(count):
        desc = trailer - (count - i) * 40
        # Region lengths are little-endian bytes and include a checksum word.
        length, = struct.unpack_from('<I', stock, desc + 20)
        if stock[desc + 24] not in (0, 0x20) or length < 4 or length % 4:
            raise ValueError('unsupported WM region')
        regions.append((offset, length))
        offset += length
    if offset != spec['data_end'] or offset > trailer - count * 40:
        raise ValueError('invalid WM layout')
    for edit in spec['replacements']:
        start, data = edit['offset'], bytes.fromhex(edit['hex'])
        # Keep replacements inside one region and clear of its checksum tail.
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
    # Seal the whole image after updating the individual region checksums.
    struct.pack_into('<I', output, len(output) - 4, zlib.crc32(output[:-4]))
    if digest(output) != spec['output_sha256']:
        raise ValueError('sealed WM hash mismatch')
    return bytes(output)


def sync_directory(path):
    """Persist directory entry changes through a directory descriptor."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked(directory):
    """Hold an exclusive directory lock until the caller leaves the context."""
    # Serialize preparers with a directory lock, without lock-file writes or
    # radio probing.
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def file_hash(path):
    """Hash a present file, leaving missing files and symlinks unowned."""
    if path.is_symlink():
        return None
    try:
        return digest(path.read_bytes())
    except FileNotFoundError:
        return None


def identity(path):
    """Return device, inode and nanosecond mtime for ownership checks."""
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_mtime_ns]


def staged_file(path, data):
    """Create and fsync a new 0644 file before its atomic publication."""
    # The temporary directory is on the destination filesystem. Publish only
    # after a complete write and fsync, preserving this inode's identity.
    with path.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o644)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class Firmware:
    """Select owned overrides and retain evidence of unconfirmed boots."""

    def __init__(self, root=Path('/')):
        """Locate assets, overrides and recovery state beneath the root."""
        self.root = Path(root)
        self.assets = self.root / 'usr/local/share/manet'
        self.updates = self.root / 'lib/firmware/updates/mediatek'
        self.owner = self.updates / OWNER
        self.state = self.root / 'var/lib/manet'
        self.pending = self.state / 'mt7916-probe-pending'
        self.disabled = self.state / 'mt7916-auto-disabled'

    def read_state(self, path):
        """Load a recovery record and validate its schema and identities."""
        try:
            record = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        if (record.get('schema') != 1 or
                any(not isinstance(record.get(k), str)
                    for k in ('boot_id', 'wm_sha256'))):
            raise ValueError(f'invalid recovery state: {path.name}')
        return record

    def write_state(self, path, record):
        """Atomically persist a recovery record and its directory entries."""
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
        """Remove a present recovery record and persist the deletion."""
        if path.exists():
            path.unlink()
            sync_directory(self.state)

    def boot_id(self):
        """Read a nonempty boot ID for pending-probe comparisons."""
        value = (self.root / 'proc/sys/kernel/random/boot_id').read_text().strip()
        if not value:
            raise ValueError('missing boot identity')
        return value

    def present(self):
        """Detect a primary MT7916 PCI function from its vendor/device IDs."""
        # Detect presence before coldplug without a bound driver.
        # The auxiliary 0x790a function alone cannot load this firmware.
        return any((device / 'vendor').read_text().strip() == '0x14c3' and
                   (device / 'device').read_text().strip() == '0x7906'
                   for device in (self.root / 'sys/bus/pci/devices').glob('*'))

    def survived(self):
        """Check multi-user readiness and completed primary-radio binding."""
        # Also check the target here so a premature manual invocation cannot
        # erase the marker. The unit orders this once, without polling.
        # Query target readiness once, with a 10 s command deadline.
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
        # 0x7906 is MT7916's primary function, 0x790a its auxiliary HIF.
        # Auxiliary binding alone does not prove firmware probed.
        return primary or not secondary

    def select(self, mode):
        """Prefer patched WM on timing drivers, with a pinned-stock fallback."""
        pending = self.read_state(self.pending)
        disabled = self.read_state(self.disabled)
        current = self.boot_id() if pending or mode == 'boot' else None
        stale = pending and pending['boot_id'] != current
        if stale:
            # Record the failure before removing either the firmware or the
            # evidence. A crash during recovery must never silently rearm it.
            disabled = dict(pending, reason='boot did not reach survival check')
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
        # Retry a new pinned patch or an explicit --rearm. Config changes,
        # including positioning, cannot erase evidence of a failed boot.
        if disabled and mode != 'survived':
            if mode == 'rearm' or WM_SHA256 != disabled['wm_sha256']:
                self.remove_state(self.disabled)
                disabled = None
        if mode != 'survived':
            self.configure_reservation(allow=not disabled)
        if not self.present():
            return 'no MT7916 primary PCI function (14c3:7906); ' + self.prepare(False)
        if disabled:
            return ('AUTO-DISABLED after failed boot; WM ' + disabled['wm_sha256'] +
                    '; run --rearm or install an updated patched WM; ' +
                    self.prepare(True, patched=False))
        patched = timing_driver(self.root)
        reason = ''
        if not patched:
            reason = 'installed mt7915e lacks manet_timing=1; using pinned stock; '
        if mode == 'boot' and patched and not pending:
            # Persist probe intent before leaving patched WM available.
            # Pinned stock is the recovery set and does not arm another trial.
            self.write_state(self.pending, {'schema': 1, 'boot_id': current,
                                           'wm_sha256': WM_SHA256})
        return reason + self.prepare(True, patched=patched)

    def configure_reservation(self, *, allow=True):
        """Reconcile the local boot-only option through the config watcher."""
        path = self.root / "etc/modprobe.d/manet-mt7916.conf"
        content = ("# Managed by manet-mt7916-firmware.py\n"
                   "options mt7915e background_radar_2t2r=1\n")
        requested = (config_choice(self.root / "etc/mesh.conf",
                                   "mt7916_background_rx") if allow else "n")
        if requested not in ("y", "n"):
            raise ValueError("mt7916_background_rx must be y or n")
        if path.is_symlink() or (path.exists() and path.read_text() != content):
            raise ValueError("unowned MT7916 modprobe config")
        if requested == "n":
            if path.exists():
                path.unlink()
                sync_directory(path.parent)
            return
        try:
            result = subprocess.run(
                ["modinfo", "-b", str(self.root), "-k", os.uname().release,
                 "-F", "parm", "mt7915e"], capture_output=True, text=True,
                timeout=10)
            supported = result.returncode == 0 and any(
                line.startswith("background_radar_2t2r:")
                for line in result.stdout.splitlines())
        except (OSError, subprocess.SubprocessError):
            supported = False
        if not supported:
            # An unknown module option prevents probing on older kernels.
            if path.exists():
                path.unlink()
                sync_directory(path.parent)
            return
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".mt7916-",
                                         dir=path.parent) as temp:
            staged = Path(temp) / path.name
            staged_file(staged, content.encode())
            os.replace(staged, path)
            sync_directory(path.parent)

    def owners(self):
        """Load firmware ownership records, or an empty mapping if absent."""
        try:
            record = json.loads(self.owner.read_text())
        except FileNotFoundError:
            return {}
        if record.get('schema') != 1 or not isinstance(record.get('files'), dict):
            raise ValueError('invalid MT7916 owner manifest')
        return record['files']

    def owned(self, name, owners):
        """Match current patched WM by hash; other files need owned identity."""
        path = self.updates / name
        value = file_hash(path)
        if name == NAMES[0] and value == WM_SHA256:
            return True
        record = owners.get(name, {})
        return any(value is not None and value == candidate.get('sha256')
                   and identity(path) == candidate.get('identity')
                   for candidate in (record, record.get('previous', {})))

    def deactivate(self):
        """Remove recognized overrides and their ownership manifest."""
        # Remove the unique patched WM hash first, recognizable without JSON.
        # On failure, keep the owner record to retry WA/ROM removal next boot.
        wm = self.updates / NAMES[0]
        if self.owned(NAMES[0], {}):
            wm.unlink()
            sync_directory(self.updates)
        owners = self.owners()
        for name in NAMES:
            if self.owned(name, owners):
                (self.updates / name).unlink()
                sync_directory(self.updates)
        if self.owner.exists():
            self.owner.unlink()
            sync_directory(self.updates)

    def prepare(self, enable, *, patched=True):
        """Remove owned overrides or stage the verified firmware set."""
        if not enable:
            self.deactivate()
            if any(os.path.lexists(self.updates / n) for n in NAMES):
                return 'disabled; MANET overrides removed, unowned overrides preserved'
            return 'disabled; MANET overrides removed'
        manifest = json.loads((self.assets / 'mt7916-firmware.json').read_text())
        spec = manifest['wm_patch']
        expected = {n: manifest['linux_firmware']['files'][n]['sha256'] for n in NAMES}
        if patched:
            expected[NAMES[0]] = spec['output_sha256']
        if expected[NAMES[0]] != (WM_SHA256 if patched else STOCK_WM_SHA256):
            raise ValueError('unexpected WM output hash')
        selection = 'patched WM' if patched else 'pinned stock'
        changed = [n for n in NAMES if file_hash(self.updates / n) != expected[n]]
        if not changed:
            # Ready boots read only the intent and installed set, not the stock
            # cache. Do not reseal, adopt foreign companions or rewrite metadata.
            return selection + '; firmware already prepared'
        owners = self.owners()
        for name in changed:
            # lexists also sees dangling symlinks, which must not be replaced.
            if os.path.lexists(self.updates / name) and not self.owned(name, owners):
                raise ValueError(f'unowned firmware override: {name}')
        stock = {}
        for name in NAMES:
            data = (self.assets / 'firmware/mt7916' / name).read_bytes()
            if digest(data) != manifest['linux_firmware']['files'][name]['sha256']:
                raise ValueError(f'stock {name} hash mismatch')
            stock[name] = data
        if patched and NAMES[0] in changed:
            stock[NAMES[0]] = patch_wm(stock[NAMES[0]], spec)
        with tempfile.TemporaryDirectory(prefix='.manet-mt7916-', dir=self.updates) as temp:
            work = Path(temp)
            for name in changed:
                staged_file(work / name, stock[name])
                record = {'sha256': expected[name], 'identity': identity(work / name)}
                path = self.updates / name
                previous_hash = file_hash(path)
                if previous_hash is not None:
                    # Keep the currently owned inode recognizable if power is
                    # lost after the manifest rename but before file publication.
                    record['previous'] = {'sha256': previous_hash,
                                          'identity': identity(path)}
                owners[name] = record
            # Record every staged identity first, including patched WM so a
            # later update can replace its old hash without adopting files.
            staged_file(work / OWNER, json.dumps({'schema': 1, 'files': owners},
                                                 sort_keys=True).encode() + b'\n')
            os.replace(work / OWNER, self.owner)
            sync_directory(self.updates)
            # WM is published last, after both companions are complete.
            for name in (*NAMES[1:], NAMES[0]):
                if name in changed:
                    os.replace(work / name, self.updates / name)
                    sync_directory(self.updates)
        return selection + '; firmware prepared for next boot'

    def run(self, mode='apply'):
        """Serialize selection and return success plus a diagnostic message."""
        try:
            if not self.updates.exists():
                self.updates.mkdir(parents=True, exist_ok=True)
            with locked(self.updates):
                try:
                    return True, self.select(mode)
                except Exception as error:
                    # A failed preparation must remove unsafe owned overrides.
                    # Keep both errors visible if owned-file cleanup also fails.
                    failures = []
                    for cleanup in (self.deactivate, lambda:
                                    self.configure_reservation(allow=False)):
                        try:
                            cleanup()
                        except Exception as failure:
                            failures.append(str(failure))
                    recovery = ('owned overrides removed; other firmware '
                                'left untouched')
                    if failures:
                        recovery = ('override cleanup failed, retry at next '
                                    'boot: ' + '; '.join(failures))
                    return False, f'{error}; {recovery}'
        except Exception as error:
            return False, f'{error}; preparation unavailable, retry at next boot'


def main():
    """Apply the requested firmware mode and return its process exit status."""
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
