#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import sys


def parse_mcs(text):
    m = re.search(r'\b(?:VHT-)?MCS\s+(\d+)\b', text or '')
    return f"MCS{m.group(1)}" if m else ''


def parse_nss(text):
    m = re.search(r'\b(?:VHT-|HE-|EHT-)?NSS\s+(\d+)\b', text or '')
    return f"N{m.group(1)}" if m else ''


def parse_gi(text):
    text = text or ''
    if re.search(r'\bshort GI\b', text):
        return 'SGI'
    m = re.search(r'\b(?:HE-|EHT-)?GI\s+([0-9.]+)\b', text)
    return f"GI{m.group(1)}" if m else ''


def parse_bw(text):
    m = re.search(r'\b(\d+)\s*MHz\b', text or '')
    return f"{m.group(1)}M" if m else ''


def parse_rate_summary(text):
    mcs = parse_mcs(text)
    if not mcs:
        return {'mcs': '', 'summary': ''}
    parts = [mcs]
    for extra in (parse_nss(text), parse_gi(text), parse_bw(text)):
        if extra:
            parts.append(extra)
    return {'mcs': mcs, 'summary': ' '.join(parts)}


def station_blocks(text):
    blocks = []
    current = []
    for line in (text or '').splitlines():
        if line.startswith('Station '):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def parse_block(lines):
    data = {
        'peer_mac': '',
        'inactive_ms': 10**9,
        'signal': -999,
        'tx_mcs': '',
        'rx_mcs': '',
        'tx_summary': '',
        'rx_summary': '',
    }
    if not lines:
        return data
    m = re.search(r'^Station\s+([0-9a-f:]{17})\s+\(', lines[0], re.I)
    if m:
        data['peer_mac'] = m.group(1).lower()
    for line in lines[1:]:
        m = re.search(r'inactive time:\s*(\d+)\s*ms', line)
        if m:
            data['inactive_ms'] = int(m.group(1))
        m = re.search(r'signal:\s*(-?\d+)\s*dBm', line)
        if m:
            data['signal'] = int(m.group(1))
        if 'tx bitrate:' in line:
            parsed = parse_rate_summary(line)
            data['tx_mcs'] = parsed['mcs']
            data['tx_summary'] = parsed['summary']
        if 'rx bitrate:' in line:
            parsed = parse_rate_summary(line)
            data['rx_mcs'] = parsed['mcs']
            data['rx_summary'] = parsed['summary']
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--iface', default='wlan2')
    ap.add_argument('--shell', action='store_true')
    args = ap.parse_args()

    try:
        r = subprocess.run(
            ['/usr/sbin/iw', 'dev', args.iface, 'station', 'dump'],
            capture_output=True, text=True, timeout=5, check=False
        )
    except Exception:
        r = None

    peers = []
    if r and r.stdout:
        peers = [parse_block(block) for block in station_blocks(r.stdout)]

    peers = [p for p in peers if p.get('tx_mcs') or p.get('rx_mcs')]
    peers.sort(key=lambda p: (p.get('inactive_ms', 10**9), -p.get('signal', -999)))
    best = peers[0] if peers else {}

    result = {
        'iface': args.iface,
        'peer_mac': best.get('peer_mac', ''),
        'tx_mcs': best.get('tx_summary') or best.get('tx_mcs', ''),
        'rx_mcs': best.get('rx_summary') or best.get('rx_mcs', ''),
        'signal_dbm': best.get('signal', ''),
        'inactive_ms': best.get('inactive_ms', ''),
        'peer_count': len(peers),
    }

    if args.shell:
        prefix = re.sub(r'[^A-Za-z0-9]+', '_', args.iface).upper()
        print(f"{prefix}_TX_MCS='{result['tx_mcs']}'")
        print(f"{prefix}_RX_MCS='{result['rx_mcs']}'")
        print(f"{prefix}_MCS_PEER='{result['peer_mac']}'")
        print(f"{prefix}_MCS_SIGNAL_DBM='{result['signal_dbm']}'")
        print(f"{prefix}_MCS_PEER_COUNT='{result['peer_count']}'")
    else:
        json.dump(result, sys.stdout)


if __name__ == '__main__':
    main()
