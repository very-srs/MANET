#!/usr/bin/env python3
"""Patch Morse Micro ELF BCF regdomain TX power and regenerate BCF CRCs.

This tool targets the ELF BCF files used by the Linux Morse driver, for example
bcf_boardtype_0807.bin. It updates all TX power TLVs (tag 0xcc) in one selected
.regdom_XX section, or every .regdom_* section with --country ALL, and regenerates
the firmware-validated CRC fields.
"""

from __future__ import annotations

import argparse
import shutil
import struct
import sys
import zlib
from pathlib import Path

try:
    from elftools.elf.elffile import ELFFile
except ImportError as exc:  # pragma: no cover - operator guidance
    raise SystemExit(
        "Missing dependency: pyelftools. Install with: python3 -m pip install --user pyelftools"
    ) from exc


REGDOMAIN_SALT = b"# Morse Micro regulatory domain #"

# Exit status for a file that has nothing to patch (no regulatory domain
# sections, or no TX power TLVs in any selected one). Callers may skip such a
# file. Every other failure exits non-zero otherwise and must stop a build.
NOTHING_TO_PATCH = 3


def nothing_to_patch(message: str):
    print(message, file=sys.stderr)
    raise SystemExit(NOTHING_TO_PATCH)


def u32le(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def put_u32le(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value & 0xFFFFFFFF)


def put_i16le(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<h", data, offset, value)


def section_info(path: Path, section_name: str) -> tuple[int, int]:
    with path.open("rb") as fh:
        elf = ELFFile(fh)
        section = elf.get_section_by_name(section_name)
        if section is None:
            if section_name.startswith(".regdom_"):
                nothing_to_patch(f"Section not found: {section_name}")
            raise SystemExit(f"Section not found: {section_name}")
        return int(section["sh_offset"]), int(section["sh_size"])


def bcf_crc(blob: bytes | bytearray, rel_offset: int, length: int, seed: int = 0) -> int:
    return zlib.crc32(bytes(blob[rel_offset : rel_offset + length]), seed) & 0xFFFFFFFF


def recalc_board_config(image: bytearray, board_off: int, board_size: int) -> tuple[int, int]:
    board = image[board_off : board_off + board_size]
    if u32le(board, 0) != 0xDEADBEEF:
        raise SystemExit(".board_config magic is not 0xdeadbeef")
    payload_len = u32le(board, 12)
    crc_len = payload_len + 8
    if 8 + crc_len > board_size:
        raise SystemExit(f".board_config declared length exceeds section: {payload_len}")
    old_crc = u32le(board, 4)
    new_crc = bcf_crc(board, 8, crc_len)
    put_u32le(image, board_off + 4, new_crc)
    return old_crc, new_crc


def recalc_regdomain(
    image: bytearray, reg_off: int, reg_size: int, board_crc: int
) -> tuple[int, int]:
    reg = image[reg_off : reg_off + reg_size]
    payload_len = u32le(reg, 4)
    crc_len = payload_len + 8
    if 4 + crc_len > reg_size:
        raise SystemExit(f"regdomain declared length exceeds section: {payload_len}")
    old_crc = u32le(reg, 0)
    seed = zlib.crc32(REGDOMAIN_SALT, board_crc) & 0xFFFFFFFF
    new_crc = bcf_crc(reg, 4, crc_len, seed)
    put_u32le(image, reg_off, new_crc)
    return old_crc, new_crc


def iter_tlvs(section: bytes | bytearray):
    """TLVs of a regulatory domain section, which run from offset 12 to the
    declared payload end. A truncated or overrunning TLV is an error, never a
    quiet end of list: a malformed file must not pass as "nothing to patch"."""
    if len(section) < 12:
        raise SystemExit(f"regdomain section too short: {len(section)} bytes")
    end = 12 + u32le(section, 4)
    if end > len(section):
        raise SystemExit(f"regdomain declared length exceeds section: {u32le(section, 4)}")
    pos = 12
    while pos < end:
        if pos + 2 > end:
            raise SystemExit(f"truncated TLV header at section offset 0x{pos:x}")
        tag = section[pos]
        length = section[pos + 1]
        value_off = pos + 2
        next_pos = value_off + length
        if next_pos > end:
            raise SystemExit(f"TLV at section offset 0x{pos:x} overruns the declared payload")
        yield tag, length, value_off
        pos = next_pos


def patch_regdomain_power(
    image: bytearray, reg_off: int, reg_size: int, power_qdbm: int
) -> list[tuple[int, int]]:
    reg = image[reg_off : reg_off + reg_size]
    changed: list[tuple[int, int]] = []
    for tag, length, value_off in iter_tlvs(reg):
        if tag != 0xCC:
            continue
        if length not in (2, 4):
            raise SystemExit(f"Unexpected TX power TLV length {length} at section offset 0x{value_off:x}")
        absolute = reg_off + value_off
        if length == 2:
            old_qdbm = struct.unpack_from("<h", image, absolute)[0]
            put_i16le(image, absolute, power_qdbm)
        else:
            old_qdbm = struct.unpack_from("<i", image, absolute)[0]
            struct.pack_into("<i", image, absolute, power_qdbm)
        changed.append((absolute, old_qdbm))
    return changed


def list_regdomain(path: Path, country: str) -> None:
    reg_off, reg_size = section_info(path, f".regdom_{country}")
    data = path.read_bytes()[reg_off : reg_off + reg_size]
    print(f"{path} .regdom_{country} off=0x{reg_off:x} size={reg_size}")
    current_freq = None
    current_bw = None
    for tag, length, value_off in iter_tlvs(data):
        value = data[value_off : value_off + length]
        if tag == 0xC9 and length == 4:
            current_freq = struct.unpack_from("<I", value)[0]
        elif tag == 0xCA and length in (1, 4):
            current_bw = value[0] if length == 1 else struct.unpack_from("<I", value)[0]
        elif tag == 0xCC and length in (2, 4):
            qdbm = struct.unpack_from("<h", value)[0] if length == 2 else struct.unpack_from("<i", value)[0]
            freq = f"{current_freq / 1000:.3f} MHz" if current_freq else "unknown"
            bw = f"{current_bw} MHz" if current_bw else "unknown"
            print(f"  {freq:12} bw={bw:7} power={qdbm / 4:.2f} dBm ({qdbm} qdBm)")


def list_regdomain_sections(path: Path) -> list[tuple[str, int, int]]:
    sections = []
    with path.open("rb") as fh:
        elf = ELFFile(fh)
        for section in elf.iter_sections():
            name = section.name
            if not name.startswith(".regdom_"):
                continue
            sections.append((name.removeprefix(".regdom_"), int(section["sh_offset"]), int(section["sh_size"])))
    if not sections:
        nothing_to_patch("No .regdom_* sections found")
    return sections


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Patch Morse Micro ELF BCF regdomain TX power and regenerate CRCs."
    )
    parser.add_argument("input", type=Path, help="Input ELF BCF, e.g. bcf_boardtype_0807.bin")
    parser.add_argument("output", type=Path, nargs="?", help="Output ELF BCF")
    parser.add_argument("--country", default="EU", help="Regdomain suffix to patch, or ALL; default: EU")
    parser.add_argument("--power-dbm", type=float, help="New TX power in dBm, e.g. 16 or 22")
    parser.add_argument("--list", action="store_true", help="List TX power TLVs and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    country = args.country.upper()

    if args.list:
        list_regdomain(args.input, country)
        return 0

    if args.output is None:
        raise SystemExit("Output path is required unless --list is used")
    if args.power_dbm is None:
        raise SystemExit("--power-dbm is required unless --list is used")

    power_qdbm = round(args.power_dbm * 4)
    if power_qdbm < -128 or power_qdbm > 127:
        raise SystemExit("Power is outside signed qdBm range supported by this BCF TLV")

    shutil.copyfile(args.input, args.output)
    image = bytearray(args.output.read_bytes())

    board_off, board_size = section_info(args.output, ".board_config")
    board_old, board_new = recalc_board_config(image, board_off, board_size)

    if country == "ALL":
        regdomains = list_regdomain_sections(args.output)
    else:
        reg_off, reg_size = section_info(args.output, f".regdom_{country}")
        regdomains = [(country, reg_off, reg_size)]

    results = []
    skipped = []
    for reg_country, reg_off, reg_size in regdomains:
        changed = patch_regdomain_power(image, reg_off, reg_size, power_qdbm)
        if not changed:
            # A region without TX power TLVs has nothing to raise. With ALL,
            # leave it untouched rather than abandon the regions that do.
            skipped.append(reg_country)
            continue
        reg_old, reg_new = recalc_regdomain(image, reg_off, reg_size, board_new)
        results.append((reg_country, reg_old, reg_new, changed))
    if not results:
        nothing_to_patch("No TX power TLVs (tag 0xcc) found in selected regdomain")

    args.output.write_bytes(image)

    print(f"Wrote {args.output}")
    print(f".board_config CRC: 0x{board_old:08x} -> 0x{board_new:08x}")
    for reg_country in skipped:
        print(f".regdom_{reg_country}: no TX power TLVs, left as is")
    for reg_country, reg_old, reg_new, changed in results:
        print(f".regdom_{reg_country} CRC: 0x{reg_old:08x} -> 0x{reg_new:08x}")
        for absolute, old_qdbm in changed:
            print(
                f"  power TLV @ file 0x{absolute:04x}: "
                f"{old_qdbm / 4:.2f} dBm ({old_qdbm}) -> {power_qdbm / 4:.2f} dBm ({power_qdbm})"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
