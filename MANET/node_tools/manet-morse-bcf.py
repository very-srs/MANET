#!/usr/bin/env python3
import argparse
import os
from pathlib import Path
import struct
import sys
import tempfile
import zlib

POWER_DBM = 30
REGDOMAIN_SALT = b'# Morse Micro regulatory domain #'
TX_POWER_TAG = 0xcc
DEFAULT_DIR = '/usr/lib/firmware/morse'


def u32(data, offset):
    """Read a little-endian unsigned 32-bit word."""
    return struct.unpack_from('<I', data, offset)[0]


def sections(image):
    """Map ELF section names to (file offset, size) for a little-endian ELF."""
    if len(image) < 6 or image[:4] != b'\x7fELF' or image[5] != 1 or image[4] not in (1, 2):
        raise ValueError('not a little-endian ELF file')
    if len(image) < (52 if image[4] == 1 else 64):
        raise ValueError('ELF header is truncated')
    if image[4] == 1:
        shoff, = struct.unpack_from('<I', image, 32)
        shentsize, shnum, shstrndx = struct.unpack_from('<HHH', image, 46)
        header, name_at, offset_at = '<IIIIIIIIII', 0, 16
    else:
        shoff, = struct.unpack_from('<Q', image, 40)
        shentsize, shnum, shstrndx = struct.unpack_from('<HHH', image, 58)
        header, name_at, offset_at = '<IIQQQQIIQQ', 0, 24
    if not shoff or shnum == 0 or shstrndx >= shnum or shentsize < struct.calcsize(header):
        raise ValueError('ELF section table is missing or malformed')
    if shoff + shnum * shentsize > len(image):
        raise ValueError('ELF section table overruns the file')

    def entry(index):
        return shoff + index * shentsize

    if image[4] == 1:
        names_offset, names_size = struct.unpack_from('<II', image, entry(shstrndx) + 16)
    else:
        names_offset, names_size = struct.unpack_from('<QQ', image, entry(shstrndx) + 24)
    if names_offset + names_size > len(image):
        raise ValueError('ELF section names overrun the file')
    names = image[names_offset:names_offset + names_size]
    found = {}
    for index in range(shnum):
        base = entry(index)
        name_index = u32(image, base + name_at)
        end = names.find(b'\0', name_index)
        if name_index >= len(names) or end < 0:
            raise ValueError('ELF section name is unterminated')
        if image[4] == 1:
            offset, size = struct.unpack_from('<II', image, base + offset_at)
        else:
            offset, size = struct.unpack_from('<QQ', image, base + offset_at)
        if offset + size > len(image) and struct.unpack_from('<I', image, base + 4)[0] != 8:
            raise ValueError('ELF section overruns the file')
        found[names[name_index:end].decode('ascii', 'replace')] = (offset, size)
    return found


def crc(blob, start, length, seed=0):
    """CRC-32 over a slice, as the BCF headers define it."""
    return zlib.crc32(bytes(blob[start:start + length]), seed) & 0xffffffff


def regdomain_tlvs(section):
    """Yield (tag, length, value offset) for a regdomain section's TLVs."""
    if len(section) < 12:
        raise ValueError('regdomain section too short')
    end = 12 + u32(section, 4)
    if end > len(section):
        raise ValueError('regdomain declared length exceeds section')
    pos = 12
    while pos < end:
        if pos + 2 > end:
            raise ValueError('truncated TLV header')
        tag, length = section[pos], section[pos + 1]
        if pos + 2 + length > end:
            raise ValueError('TLV overruns the declared payload')
        yield tag, length, pos + 2
        pos += 2 + length


def patch_image(image, power_dbm=POWER_DBM):
    """Return the patched BCF bytes, or None when it has no TX power entries."""
    power = round(power_dbm * 4)
    if not -128 <= power <= 127:
        raise ValueError('power is outside the signed qdBm range of the TLV')
    table = sections(image)
    domains = sorted((name, span) for name, span in table.items()
                     if name.startswith('.regdom_'))
    if not domains or '.board_config' not in table:
        return None
    output = bytearray(image)
    board_off, board_size = table['.board_config']
    if u32(output, board_off) != 0xdeadbeef:
        raise ValueError('.board_config magic is not 0xdeadbeef')
    crc_len = u32(output, board_off + 12) + 8
    if 8 + crc_len > board_size:
        raise ValueError('.board_config declared length exceeds section')
    board_crc = crc(output, board_off + 8, crc_len)
    struct.pack_into('<I', output, board_off + 4, board_crc)
    changed = False
    for _, (offset, size) in domains:
        section = output[offset:offset + size]
        touched = False
        for tag, length, value in regdomain_tlvs(section):
            if tag != TX_POWER_TAG:
                continue
            if length not in (2, 4):
                raise ValueError(f'unexpected TX power TLV length {length}')
            struct.pack_into('<h' if length == 2 else '<i', output, offset + value, power)
            touched = True
        if not touched:
            continue
        changed = True
        crc_len = u32(output, offset + 4) + 8
        if 4 + crc_len > size:
            raise ValueError('regdomain declared length exceeds section')
        seed = zlib.crc32(REGDOMAIN_SALT, board_crc) & 0xffffffff
        struct.pack_into('<I', output, offset, crc(output, offset + 4, crc_len, seed))
    return bytes(output) if changed else None


def firmware_directory(root=Path('/')):
    """Directory the kernel loads Morse firmware from on this boot."""
    try:
        for argument in (root / 'proc/cmdline').read_text().split():
            if argument.startswith('firmware_class.path='):
                return root / argument.split('=', 1)[1].lstrip('/') / 'morse'
    except OSError:
        pass
    return root / DEFAULT_DIR.lstrip('/')


def bcf_files(directory):
    """Regular BCF files in a directory; links are never followed."""
    return sorted(path for path in directory.glob('bcf_*.bin')
                  if path.is_file() and not path.is_symlink())


def replace_file(path, data):
    """Atomically replace a file with fsync, keeping its mode."""
    mode = path.stat().st_mode & 0o777
    descriptor, name = tempfile.mkstemp(prefix='.manet-bcf-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except BaseException:
        os.unlink(name)
        raise
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def patch_directory(directory):
    """Patch every BCF in a directory. Return (patched, current, failed)."""
    patched = current = failed = 0
    for path in bcf_files(directory):
        try:
            image = path.read_bytes()
            result = patch_image(image)
            if result is None or result == image:
                current += 1
                continue
            replace_file(path, result)
            patched += 1
        except (OSError, ValueError, struct.error) as error:
            failed += 1
            print(f'manet-morse-bcf: {path.name}: {error}', file=sys.stderr)
    return patched, current, failed


def check_stock(directory):
    """List BCFs that already carry the policy, which a release must not ship."""
    modified = []
    for path in bcf_files(directory):
        image = path.read_bytes()
        if patch_image(image) == image:
            modified.append(path.name)
    return modified


def main(argv=None):
    """Patch the active firmware directory, or verify a staged one is stock."""
    parser = argparse.ArgumentParser(description='Apply the TX power policy to the installed Morse BCFs.')
    parser.add_argument('--directory', type=Path,
                        help='BCF directory (default: the one the kernel reads)')
    parser.add_argument('--check-stock', action='store_true',
                        help='fail if any BCF already carries the TX power policy')
    args = parser.parse_args(argv)
    directory = args.directory or firmware_directory()
    if not directory.is_dir():
        print(f'manet-morse-bcf: no firmware directory: {directory}', file=sys.stderr)
        return 0 if not args.check_stock else 1
    if args.check_stock:
        modified = check_stock(directory)
        for name in modified:
            print(f'manet-morse-bcf: not stock: {name}', file=sys.stderr)
        return 1 if modified else 0
    patched, current, failed = patch_directory(directory)
    print(f'manet-morse-bcf: {directory}: {patched} patched, {current} current'
          + (f', {failed} failed' if failed else ''))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
