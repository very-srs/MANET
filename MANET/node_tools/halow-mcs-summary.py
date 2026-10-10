#!/usr/bin/env python3
"""Summarize one peer from an iw station dump for radio telemetry."""
import argparse
import json
import re
import shlex
import subprocess
import sys


def parse_mcs(text):
    """Extract the MCS index from an iw bitrate field."""
    # Match a decimal MCS index with an optional VHT prefix.
    m = re.search(r'\b(?:VHT-)?MCS\s+(\d+)\b', text or '')
    return f"MCS{m.group(1)}" if m else ''


def parse_nss(text):
    """Extract the spatial stream count from an iw bitrate field."""
    # Accept stream counts labelled with VHT, HE or EHT prefixes.
    m = re.search(r'\b(?:VHT-|HE-|EHT-)?NSS\s+(\d+)\b', text or '')
    return f"N{m.group(1)}" if m else ''


def parse_gi(text):
    """Extract a short or numeric guard interval from iw output."""
    text = text or ''
    if re.search(r'\bshort GI\b', text):
        return 'SGI'
    # Numeric GI fields may carry an HE or EHT prefix.
    m = re.search(r'\b(?:HE-|EHT-)?GI\s+([0-9.]+)\b', text)
    return f"GI{m.group(1)}" if m else ''


def parse_bw(text):
    """Extract bandwidth in MHz from an iw bitrate field."""
    # The integer before MHz is the reported channel width.
    m = re.search(r'\b(\d+)\s*MHz\b', text or '')
    return f"{m.group(1)}M" if m else ''


def parse_rate_summary(text):
    """Combine MCS with any reported streams, guard interval and width."""
    mcs = parse_mcs(text)
    if not mcs:
        return {'mcs': '', 'summary': ''}
    parts = [mcs]
    for extra in (parse_nss(text), parse_gi(text), parse_bw(text)):
        if extra:
            parts.append(extra)
    return {'mcs': mcs, 'summary': ' '.join(parts)}


def station_blocks(text):
    """Split an iw station dump at each Station header."""
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
    """Read peer identity, activity, signal and rates from one station."""
    # Sentinel values deprioritize peers with missing activity or signal.
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
    # Station headers start with a MAC followed by the interface in ().
    m = re.search(r'^Station\s+([0-9a-f:]{17})\s+\(', lines[0], re.I)
    if m:
        data['peer_mac'] = m.group(1).lower()
    # Activity is milliseconds and signal is signed dBm.
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


def collect(iface):
    """Select the most recently active peer with a reported MCS."""
    try:
        # Bound the driver query to five seconds. Exceptions leave no peers.
        r = subprocess.run(
            ['/usr/sbin/iw', 'dev', iface, 'station', 'dump'],
            capture_output=True, text=True, timeout=5, check=False
        )
    except Exception:
        r = None

    peers = []
    if r and r.stdout:
        peers = [parse_block(block) for block in station_blocks(r.stdout)]

    peers = [p for p in peers if p.get('tx_mcs') or p.get('rx_mcs')]
    # Lowest inactivity wins, then strongest signal for equal ages.
    peers.sort(key=lambda p: (p.get('inactive_ms', 10**9), -p.get('signal', -999)))
    best = peers[0] if peers else {}

    result = {
        'iface': iface,
        'peer_mac': best.get('peer_mac', ''),
        'tx_mcs': best.get('tx_summary') or best.get('tx_mcs', ''),
        'rx_mcs': best.get('rx_summary') or best.get('rx_mcs', ''),
        'signal_dbm': best.get('signal', ''),
        'inactive_ms': best.get('inactive_ms', ''),
        'peer_count': len(peers),
    }

    return result


def shell_output(result):
    """Format interface-prefixed, quoted assignments for the managers."""
    # Replace interface punctuation before constructing shell variable names.
    prefix = re.sub(r'[^A-Za-z0-9]+', '_', result['iface']).upper()
    fields = {'TX_MCS': 'tx_mcs', 'RX_MCS': 'rx_mcs', 'MCS_PEER': 'peer_mac',
              'MCS_SIGNAL_DBM': 'signal_dbm', 'MCS_PEER_COUNT': 'peer_count'}
    return '\n'.join(f"{prefix}_{key}={shlex.quote(str(result[field]))}" for key, field in fields.items())


def main():
    """Print one interface summary as JSON or shell assignments."""
    ap = argparse.ArgumentParser()
    ap.add_argument('--iface', default='wlan2')
    ap.add_argument('--shell', action='store_true')
    args = ap.parse_args()
    result = collect(args.iface)
    if args.shell:
        print(shell_output(result))
    else:
        json.dump(result, sys.stdout)


if __name__ == '__main__':
    main()
