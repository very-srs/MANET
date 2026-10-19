"""Morse BCF TX power policy, applied on the node to the stock files."""
import importlib.util
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

REPO = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bcf = load('manet_morse_bcf', Path(__file__).with_name('manet-morse-bcf.py'))
STOCK_DIR = REPO / 'kernel-work/morse-firmware/bcf'


def tlv(tag, value):
    return bytes([tag, len(value)]) + value


def board_section(payload=b'\1\2\3\4'):
    body = struct.pack('<I', len(payload)) + payload
    section = bytearray(b'\0' * 8 + body)
    struct.pack_into('<I', section, 0, 0xdeadbeef)
    # Layout: magic, crc, word, length, payload. The crc covers word onward.
    section = bytearray(struct.pack('<IIII', 0xdeadbeef, 0, 0, len(payload)) + payload)
    struct.pack_into('<I', section, 4, zlib.crc32(bytes(section[8:8 + len(payload) + 8])))
    return bytes(section)


def regdomain_section(tlvs):
    payload = b''.join(tlvs)
    return struct.pack('<III', 0, len(payload), 0) + payload


def elf(sections, wide=False):
    """Build a little-endian ELF whose sections are {name: bytes}."""
    names = b'\0'
    name_at = {}
    for name in [*sections, '.shstrtab']:
        name_at[name] = len(names)
        names += name.encode() + b'\0'
    header_size = 64 if wide else 52
    entry_size = 64 if wide else 40
    body = b''
    placed = {}
    for name, data in [*sections.items(), ('.shstrtab', names)]:
        placed[name] = (header_size + len(body), len(data))
        body += data
    table = header_size + len(body)
    count = len(sections) + 2
    ident = b'\x7fELF' + bytes([2 if wide else 1, 1, 1]) + b'\0' * 9
    if wide:
        header = ident + struct.pack('<HHIQQQIHHHHHH', 2, 243, 1, 0, 0, table, 0, 64, 0, 0,
                                     entry_size, count, count - 1)
    else:
        header = ident + struct.pack('<HHIIIIIHHHHHH', 2, 243, 1, 0, 0, table, 0, 52, 0, 0,
                                     entry_size, count, count - 1)
    rows = [b'\0' * entry_size]
    for name in [*sections, '.shstrtab']:
        offset, size = placed[name]
        if wide:
            rows.append(struct.pack('<IIQQQQIIQQ', name_at[name], 1, 0, 0, offset, size, 0, 0, 1, 0))
        else:
            rows.append(struct.pack('<IIIIIIIIII', name_at[name], 1, 0, 0, offset, size, 0, 0, 1, 0))
    return header + body + b''.join(rows)


def stock(wide=False):
    return elf({'.board_config': board_section(),
                '.regdom_US': regdomain_section([tlv(0xc9, b'\1\0\0\0'), tlv(0xcc, struct.pack('<h', 80)),
                                                 tlv(0xcc, struct.pack('<i', 64))]),
                '.regdom_EU': regdomain_section([tlv(0xc9, b'\2\0\0\0'), tlv(0xcc, struct.pack('<h', 40))]),
                '.regdom_XX': regdomain_section([tlv(0xc9, b'\3\0\0\0')])}, wide)


def powers(image):
    found = {}
    for name, (offset, size) in bcf.sections(image).items():
        if name.startswith('.regdom_'):
            section = image[offset:offset + size]
            found[name] = [struct.unpack_from('<h' if n == 2 else '<i', section, v)[0]
                           for tag, n, v in bcf.regdomain_tlvs(section) if tag == 0xcc]
    return found


class PatchTests(unittest.TestCase):
    def test_raises_every_power_entry_in_both_elf_classes(self):
        for wide in (False, True):
            patched = bcf.patch_image(stock(wide))
            self.assertEqual(powers(patched), {'.regdom_US': [120, 120], '.regdom_EU': [120],
                                               '.regdom_XX': []})

    def test_checksums_follow_the_firmware_chain(self):
        patched = bcf.patch_image(stock())
        table = bcf.sections(patched)
        offset, size = table['.board_config']
        board = patched[offset:offset + size]
        self.assertEqual(struct.unpack_from('<I', board, 4)[0],
                         zlib.crc32(board[8:8 + struct.unpack_from('<I', board, 12)[0] + 8]))
        seed = zlib.crc32(bcf.REGDOMAIN_SALT, struct.unpack_from('<I', board, 4)[0])
        offset, size = table['.regdom_US']
        section = patched[offset:offset + size]
        length = struct.unpack_from('<I', section, 4)[0]
        self.assertEqual(struct.unpack_from('<I', section, 0)[0],
                         zlib.crc32(section[4:12 + length], seed))

    def test_region_without_power_entries_is_untouched(self):
        original, patched = stock(), bcf.patch_image(stock())
        offset, size = bcf.sections(original)['.regdom_XX']
        self.assertEqual(original[offset:offset + size], patched[offset:offset + size])

    def test_is_idempotent(self):
        once = bcf.patch_image(stock())
        self.assertEqual(bcf.patch_image(once), once)

    def test_file_with_nothing_to_patch_is_left_alone(self):
        self.assertIsNone(bcf.patch_image(elf({'.text': b'code'})))
        self.assertIsNone(bcf.patch_image(elf({'.board_config': board_section(),
                                               '.regdom_XX': regdomain_section([tlv(0xc9, b'\3\0\0\0')])})))

    def test_malformed_input_is_an_error_not_a_quiet_skip(self):
        with self.assertRaises(ValueError):
            bcf.patch_image(b'not an elf at all')
        broken = elf({'.board_config': board_section(),
                      '.regdom_US': regdomain_section([b'\xcc\x09\x01\x02'])})
        with self.assertRaises(ValueError):
            bcf.patch_image(broken)
        with self.assertRaises(ValueError):
            bcf.patch_image(stock()[:40])


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.directory = self.root / 'usr/lib/firmware/morse'
        self.directory.mkdir(parents=True)

    def test_apply_patches_in_place_keeping_mode_and_ignoring_links(self):
        target = self.directory / 'bcf_a.bin'
        target.write_bytes(stock())
        target.chmod(0o644)
        (self.directory / 'bcf_link.bin').symlink_to('bcf_a.bin')
        (self.directory / 'mm6108.bin').write_bytes(b'firmware, not a bcf')
        self.assertEqual(bcf.patch_directory(self.directory), (1, 0, 0))
        self.assertEqual(oct(target.stat().st_mode & 0o777), '0o644')
        self.assertEqual(powers(target.read_bytes())['.regdom_US'], [120, 120])
        self.assertEqual((self.directory / 'mm6108.bin').read_bytes(), b'firmware, not a bcf')
        self.assertEqual(bcf.patch_directory(self.directory), (0, 1, 0))
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()),
                         ['bcf_a.bin', 'bcf_link.bin', 'mm6108.bin'])

    def test_one_bad_file_does_not_block_the_others(self):
        (self.directory / 'bcf_a.bin').write_bytes(b'garbage')
        (self.directory / 'bcf_b.bin').write_bytes(stock())
        self.assertEqual(bcf.patch_directory(self.directory), (1, 0, 1))
        self.assertEqual(bcf.main(['--directory', str(self.directory)]), 1)

    def test_check_stock_flags_only_patched_files(self):
        (self.directory / 'bcf_stock.bin').write_bytes(stock())
        (self.directory / 'bcf_done.bin').write_bytes(bcf.patch_image(stock()))
        (self.directory / 'bcf_none.bin').write_bytes(elf({'.text': b'x'}))
        self.assertEqual(bcf.check_stock(self.directory), ['bcf_done.bin'])
        self.assertEqual(bcf.main(['--directory', str(self.directory), '--check-stock']), 1)

    def test_kernel_trial_slot_directory_is_the_one_patched(self):
        self.assertEqual(bcf.firmware_directory(self.root), self.directory)
        cmdline = self.root / 'proc/cmdline'
        cmdline.parent.mkdir()
        cmdline.write_text('root=/dev/x manet.kernel_trial=abc '
                           'firmware_class.path=/var/lib/manet-kernel/abc/firmware panic=10\n')
        self.assertEqual(bcf.firmware_directory(self.root),
                         self.root / 'var/lib/manet-kernel/abc/firmware/morse')

    def test_usage_does_not_depend_on_docstrings(self):
        # Running with -OO strips docstrings; the command line must still work.
        result = subprocess.run([sys.executable, '-OO', str(Path(bcf.__file__)), '--help'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--check-stock', result.stdout)

    def test_missing_directory_does_not_fail_boot(self):
        self.assertEqual(bcf.main(['--directory', str(self.root / 'absent')]), 0)


@unittest.skipUnless(STOCK_DIR.is_dir(), 'pinned Morse firmware checkout not present')
class PublishedFileTests(unittest.TestCase):
    def test_every_published_bcf_patches_cleanly(self):
        files = sorted(STOCK_DIR.glob('*/bcf_*.bin'))
        self.assertTrue(files)
        for path in files:
            image = path.read_bytes()
            patched = bcf.patch_image(image)
            if patched is None:
                continue
            with self.subTest(path.name):
                self.assertNotEqual(patched, image)
                self.assertEqual(bcf.patch_image(patched), patched)
                self.assertTrue(all(v == 120 for values in powers(patched).values() for v in values))


class WiringTests(unittest.TestCase):
    def test_unit_runs_before_drivers_probe(self):
        unit = (REPO / 'MANET/systemd/manet-morse-bcf.service').read_text()
        self.assertIn('Before=systemd-udev-trigger.service', unit)
        self.assertIn('DefaultDependencies=no', unit)
        self.assertIn('ExecStart=/usr/bin/python3 /usr/local/bin/manet-morse-bcf.py', unit)

    def test_updater_requires_the_tool_and_unit(self):
        text = (REPO / 'MANET/node_tools/node-update.py').read_text()
        for name in ('usr/local/bin/manet-morse-bcf.py', 'etc/systemd/system/manet-morse-bcf.service'):
            self.assertIn(f'"{name}"', text)
        self.assertIn("'manet-morse-bcf.service'", text)

    def test_every_archive_builder_enables_the_unit(self):
        for name in ('build-tools-tarball.sh', 'build-cm4-tarball.sh', 'build-r3a-tarball.sh'):
            with self.subTest(name):
                self.assertIn('sysinit.target.wants/manet-morse-bcf.service',
                              (REPO / 'MANET/packaging' / name).read_text())

    def test_overlay_builds_no_longer_modify_the_bcfs(self):
        for name in ('cm4', 'r3a', 'rpi5'):
            with self.subTest(name):
                text = (REPO / f'kernel/build/build-{name}-sbc-overlay.sh').read_text()
                self.assertNotIn('morse-bcf-patch', text)

    def test_overlay_builds_ship_the_morse_license(self):
        for name in ('cm4', 'r3a', 'rpi5'):
            with self.subTest(name):
                text = (REPO / f'kernel/build/build-{name}-sbc-overlay.sh').read_text()
                self.assertIn('firmware/morse/LICENSE', text)

    def test_payload_stager_refuses_a_patched_overlay(self):
        sys.path.insert(0, str(REPO / 'MANET/node_tools'))
        stager = load('stage_kernel_payload', REPO / 'MANET/packaging/stage-kernel-payload.py')
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'bcf_a.bin').write_bytes(stock())
            with self.assertRaisesRegex(ValueError, 'license missing'):
                stager.require_stock_bcfs(Path(temp))
            (Path(temp) / 'LICENSE').write_text('Morse Micro binary distribution license\n')
            stager.require_stock_bcfs(Path(temp))
            (Path(temp) / 'bcf_b.bin').write_bytes(bcf.patch_image(stock()))
            with self.assertRaisesRegex(ValueError, 'bcf_b.bin'):
                stager.require_stock_bcfs(Path(temp))


if __name__ == '__main__':
    unittest.main()
