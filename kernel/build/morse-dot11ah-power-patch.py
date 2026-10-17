#!/usr/bin/env python3
"""Patch max_eirp in ALL Morse dot11ah.ko regulatory rules.

Scans for ieee80211_reg_rule structs by detecting HaLow start_freq values
(700–960 MHz = 700000–960000 kHz), then caps max_eirp in every rule to the
requested value.  No hardcoded offsets - works for any kernel/driver build.

Struct layout (linux/cfg80211.h):
  ieee80211_freq_range  (12 bytes): start_freq_khz, end_freq_khz, max_bandwidth_khz
  ieee80211_power_rule  ( 8 bytes): max_antenna_gain_mBi, max_eirp_mBm   ← offset 0x10
  flags                 ( 4 bytes)
  ... padding to 120 bytes total

Usage:
  python3 morse-dot11ah-power-patch.py dot11ah.ko dot11ah.ko.out --power-dbm 30
  python3 morse-dot11ah-power-patch.py dot11ah.ko dot11ah.ko.out --power-dbm 30 --min-only

  --min-only  only raise rules whose current max_eirp is below the target;
              leave rules that are already above the target unchanged.
              Without this flag all rules are set to exactly the target.
"""

from __future__ import annotations

import argparse
import shutil
import struct
from pathlib import Path

START_FREQ_MIN_KHZ = 700_000
START_FREQ_MAX_KHZ = 960_000
END_FREQ_MAX_KHZ   = 980_000

MAX_EIRP_OFFSET = 0x10   # bytes from start of reg_rule to max_eirp field


def find_reg_rules(data: bytes) -> list[int]:
    """Return file offsets of every plausible ieee80211_reg_rule."""
    rules = []
    for i in range(0, len(data) - 24, 4):
        start_freq = struct.unpack_from("<I", data, i)[0]
        if not (START_FREQ_MIN_KHZ <= start_freq <= START_FREQ_MAX_KHZ):
            continue
        end_freq = struct.unpack_from("<I", data, i + 4)[0]
        if not (start_freq < end_freq <= END_FREQ_MAX_KHZ):
            continue
        rules.append(i)
    return rules


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Patch max_eirp in all dot11ah.ko regulatory rules."
    )
    p.add_argument("input",  type=Path, help="Input dot11ah.ko")
    p.add_argument("output", type=Path, help="Output dot11ah.ko")
    p.add_argument("--power-dbm", type=float, required=True,
                   help="Target max EIRP in dBm (e.g. 30)")
    p.add_argument("--min-only", action="store_true",
                   help="Only raise rules below target; leave higher ones unchanged")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    target_mbm = round(args.power_dbm * 100)

    shutil.copyfile(args.input, args.output)
    data = bytearray(args.output.read_bytes())

    rules = find_reg_rules(data)
    if not rules:
        raise SystemExit("No HaLow reg_rules found - wrong binary?")

    changed = 0
    for rule_off in rules:
        eirp_off = rule_off + MAX_EIRP_OFFSET
        old_mbm = struct.unpack_from("<I", data, eirp_off)[0]
        if args.min_only and old_mbm >= target_mbm:
            continue
        if old_mbm == target_mbm:
            continue
        struct.pack_into("<I", data, eirp_off, target_mbm)
        start = struct.unpack_from("<I", data, rule_off)[0]
        end   = struct.unpack_from("<I", data, rule_off + 4)[0]
        print(f"  0x{rule_off:06x}  {start/1000:.1f}–{end/1000:.1f} MHz: "
              f"{old_mbm/100:.1f} dBm → {target_mbm/100:.1f} dBm")
        changed += 1

    args.output.write_bytes(data)
    print(f"Patched {changed}/{len(rules)} rules → {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
