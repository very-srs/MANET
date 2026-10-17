#!/usr/bin/env python3
"""Restore only DFS flags using the historical database with matching bands."""
import argparse
import difflib
import hashlib
import io
import json
from pathlib import Path
import re
import struct
import subprocess
import sys

ENCODER = '389b9b702cf9018e9a9078ccc4fa8eaae2e054e3'
BASELINE = '8c784a10448e36dc387fe02ae6bf69f4ae0208d2'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decode(path):
    """Decode every rule, including CAC and WMM, independently of dbparse."""
    data = path.read_bytes()
    if struct.unpack_from('>II', data) != (0x52474442, 20):
        raise ValueError('Expected v20 regulatory database')
    countries, offset = {}, 8
    while True:
        name, pointer = struct.unpack_from('>2sH', data, offset)
        if not pointer:
            break
        offset += 4
        pos = pointer * 4
        length, count, region = struct.unpack_from('>BBB', data, pos)
        rules = []
        for index in range(count):
            start = struct.unpack_from('>H', data, pos + (length + 1) // 2 * 2 + index * 2)[0] * 4
            size, flags, eirp, low, high, width = struct.unpack_from('>BBHIII', data, start)
            cac = struct.unpack_from('>H', data, start + 16)[0] if size >= 18 else 0
            wmm = None
            if size >= 20:
                wmm_at = struct.unpack_from('>H', data, start + 18)[0] * 4
                wmm = data[wmm_at:wmm_at + 32].hex()
            rules.append(dict(flags=flags, eirp=eirp, low=low, high=high,
                              width=width, cac=cac, wmm=wmm))
        countries[name.decode()] = dict(region=region, rules=rules)
    return countries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', required=True, type=Path)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--shipped', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    upstream = args.upstream.resolve()
    for name in ('dbparse.py', 'db2fw.py'):
        expected = subprocess.check_output(['git', '-C', str(upstream), 'show', ENCODER + ':' + name])
        if (upstream / name).read_bytes() != expected:
            raise ValueError('Encoder differs from pinned source: ' + name)
    sys.path.insert(0, str(upstream))
    from dbparse import DBParser
    baseline_text = subprocess.check_output(['git', '-C', str(upstream), 'show', BASELINE + ':db.txt'], text=True)
    historical = DBParser().parse(io.StringIO(baseline_text))
    original = args.source.read_text()
    current = DBParser().parse(io.StringIO(original))
    key = lambda r: (r.freqband.start, r.freqband.end, r.freqband.maxbw)
    dfs = {}
    for country, entry in current.items():
        for rule in entry.permissions:
            if country == b'00':
                # MANET widened these two world rules before this work. Keep
                # their exact bounds and restore the corresponding DFS tag.
                wanted = key(rule) in ((5250., 5350., 80.), (5470., 5730., 160.))
            else:
                matches = [r for r in historical[country].permissions if key(r) == key(rule)]
                if len(matches) != 1:
                    raise ValueError('Historical rule is ambiguous or missing')
                wanted = bool(matches[0].flags & 16)
            dfs[country, key(rule)] = wanted
    lines, countries, changes = [], [], 0
    for line in original.splitlines(True):
        if line.startswith('country '):
            countries = [c.strip().encode() for c in line[8:].split(':')[0].split(',')]
        match = re.match(r'\s*\(([\d.]+) - ([\d.]+) @ ([\d.]+)\)', line)
        if match:
            band = tuple(map(float, match.groups()))
            flags = {dfs[c, band] for c in countries}
            if len(flags) != 1:
                raise ValueError('Grouped countries need different DFS flags')
            if flags == {True} and not re.search(r'\bDFS\b', line):
                if ', wmmrule=' in line:
                    line = line.replace(', wmmrule=', ', DFS, wmmrule=')
                else:
                    line = line.rstrip('\n') + ', DFS\n'
                changes += 1
        lines.append(line)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'db.txt').write_text(''.join(lines))
    (args.output / 'db.txt.diff').write_text(''.join(difflib.unified_diff(
        original.splitlines(True), lines, fromfile='MANET/root/db.txt', tofile='release-r1/regdb/db.txt')))
    for source, destination in ((args.source, 'current-roundtrip.db'), (args.output / 'db.txt', 'regulatory.db')):
        subprocess.run([sys.executable, str(upstream / 'db2fw.py'), str(args.output / destination), str(source)], check=True)
    if (args.output / 'current-roundtrip.db').read_bytes() != args.shipped.read_bytes():
        raise ValueError('Current db.txt does not reproduce the shipped binary exactly')
    before, after = decode(args.shipped), decode(args.output / 'regulatory.db')
    if before.keys() != after.keys():
        raise ValueError('Country inventory changed')
    restored = 0
    for country, entry in before.items():
        new = after[country]
        if entry['region'] != new['region'] or len(entry['rules']) != len(new['rules']):
            raise ValueError('Country region or rule inventory changed')
        for a, b in zip(entry['rules'], new['rules']):
            if (a['flags'] ^ b['flags']) & ~4 or a['flags'] & ~b['flags']:
                raise ValueError('A non-DFS flag changed, or a flag was cleared')
            restored += bool((a['flags'] ^ b['flags']) & 4)
            if {k: v for k, v in a.items() if k != 'flags'} != {k: v for k, v in b.items() if k != 'flags'}:
                raise ValueError('Frequency, power, width, CAC or WMM changed')
    result = dict(encoder_commit=ENCODER, historical_rule_commit=BASELINE,
                  source_sha256=sha(args.source), shipped_sha256=sha(args.shipped),
                  regulatory_sha256=sha(args.output / 'regulatory.db'),
                  db_txt_sha256=sha(args.output / 'db.txt'), countries=len(after),
                  source_rule_lines_changed=changes, country_rules_restored=restored,
                  only_dfs_flags_changed=True, trust='unsigned, same as shipped; CONFIG_CFG80211_REQUIRE_SIGNED_REGDB=n',
                  world_mapping='Existing 5250-5350 and 5470-5730 bounds retained; DFS restored from historical world DFS bands')
    (args.output / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
