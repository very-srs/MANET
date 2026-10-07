#!/usr/bin/env python3
"""Passive bat0 header census. No probes, rules, promiscuous mode or saved payloads."""

import argparse
from collections import Counter, defaultdict
import ipaddress
import json
import math
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time

FIELDS = ('direction', 'source_port', 'source_mac', 'protocol', 'sport', 'dport',
          'destination_kind', 'destination', 'vlan')
SOL_PACKET, PACKET_STATISTICS = 263, 6


def decode(frame):
    """Decode headers only; unknown/truncated/fragmented traffic stays visible."""
    result = dict(source_mac='?', source_ip=None, protocol='truncated', sport=None,
                  dport=None, destination_kind='unknown', destination='?', vlan='')
    if len(frame) < 14:
        return result
    result['source_mac'] = frame[6:12].hex(':')
    result['destination'] = frame[:6].hex(':')
    result['destination_kind'] = ('broadcast' if frame[:6] == b'\xff' * 6 else
                                  'multicast' if frame[0] & 1 else 'unicast')
    ethertype = int.from_bytes(frame[12:14], 'big')
    offset, vlans = 14, []
    while ethertype in (0x8100, 0x88a8):
        if len(frame) < offset + 4:
            return result
        tag, ethertype = struct.unpack_from('!HH', frame, offset)
        vlans.append(str(tag & 4095))
        offset += 4
    result['vlan'] = ','.join(vlans)
    result['protocol'] = {0x0806: 'ARP', 0x4305: 'BATMAN'}.get(ethertype, f'ether:0x{ethertype:04x}')
    if ethertype == 0x0800:
        if len(frame) < offset + 20 or frame[offset] >> 4 != 4:
            result['protocol'] = 'IPv4/truncated'
            return result
        ihl = (frame[offset] & 15) * 4
        total = int.from_bytes(frame[offset + 2:offset + 4], 'big')
        if ihl < 20 or total < ihl or len(frame) < offset + ihl:
            result['protocol'] = 'IPv4/malformed'
            return result
        frame = frame[:offset + total]  # Ethernet padding is not a transport header.
        proto = frame[offset + 9]
        result['source_ip'] = str(ipaddress.ip_address(frame[offset + 12:offset + 16]))
        destination = ipaddress.ip_address(frame[offset + 16:offset + 20])
        fragment = int.from_bytes(frame[offset + 6:offset + 8], 'big') & 0x1fff
        offset += ihl
        family = 'IPv4'
    elif ethertype == 0x86dd:
        if len(frame) < offset + 40 or frame[offset] >> 4 != 6:
            result['protocol'] = 'IPv6/truncated'
            return result
        payload_length = int.from_bytes(frame[offset + 4:offset + 6], 'big')
        proto = frame[offset + 6]
        result['source_ip'] = str(ipaddress.ip_address(frame[offset + 8:offset + 24]))
        destination = ipaddress.ip_address(frame[offset + 24:offset + 40])
        frame = frame[:offset + 40 + payload_length]
        offset += 40
        fragment, family = False, 'IPv6'
        # Hop-by-hop, routing, destination options, fragment, authentication.
        for _ in range(16):
            if proto not in (0, 43, 60, 44, 51):
                break
            if len(frame) < offset + 8:
                result['protocol'] = 'IPv6/truncated-extension'
                return result
            next_proto = frame[offset]
            if proto == 44:
                fragment = bool(int.from_bytes(frame[offset + 2:offset + 4], 'big') & 0xfff8)
                length = 8
            else:
                length = (frame[offset + 1] + (2 if proto == 51 else 1)) * (4 if proto == 51 else 8)
            offset += length
            proto = next_proto
            if fragment:
                break
    else:
        return result
    result['destination'] = str(destination)
    if destination.is_multicast:
        result['destination_kind'] = 'multicast'
    result['protocol'] = family + '/' + {1: 'ICMP', 2: 'IGMP', 6: 'TCP', 17: 'UDP', 58: 'ICMPv6'}.get(proto, str(proto))
    if fragment:
        result['protocol'] += '/fragment'
    elif proto in (6, 17):
        if len(frame) >= offset + (20 if proto == 6 else 8):
            result['sport'], result['dport'] = struct.unpack_from('!HH', frame, offset)
        else:
            result['protocol'] += '/truncated'
    return result


def read_json(*command):
    return json.loads(subprocess.run(command, check=True, capture_output=True,
                                     text=True, timeout=1).stdout)


def source_snapshot():
    """Read bridge learning and local addresses; never modify the network."""
    ports = {p.name for p in Path('/sys/class/net/br0/brif').iterdir()}
    local_macs, local_ips, fdb = set(), set(), defaultdict(set)
    for interface in read_json('ip', '-j', 'address', 'show'):
        if interface.get('address'):
            local_macs.add(interface['address'].lower())
        local_ips.update(a['local'] for a in interface.get('addr_info', []) if 'local' in a)
    for entry in read_json('bridge', '-j', 'fdb', 'show', 'br', 'br0'):
        if entry.get('dev') in ports and entry.get('mac') not in local_macs:
            fdb[entry['mac'].lower()].add(entry['dev'])
    return local_macs, local_ips, fdb


def source_port(packet, direction, snapshot):
    if direction == 'in':
        return 'bat0'
    macs, ips, fdb = snapshot
    if packet['source_mac'] in macs:
        if packet['source_ip'] is None or packet['source_ip'] in ips:
            return 'local(mac/ip)'
        return 'routed-or-unknown'
    ports = fdb.get(packet['source_mac'], set())
    if len(ports) == 1 and 'bat0' not in ports and not packet['vlan']:
        return next(iter(ports)) + '(fdb)'
    return 'unknown'


class Census:
    def __init__(self, max_rows=20000):
        self.rows = {}
        self.max_rows = max_rows
        self.totals = Counter(packets=0, bytes=0, overflow_packets=0, overflow_bytes=0)

    def add(self, frame, size, direction, snapshot):
        packet = decode(frame)
        packet.update(direction=direction, source_port=source_port(packet, direction, snapshot))
        key = tuple(packet[field] for field in FIELDS)
        self.totals['packets'] += 1
        self.totals['bytes'] += size
        if key not in self.rows and len(self.rows) >= self.max_rows:
            self.totals['overflow_packets'] += 1
            self.totals['overflow_bytes'] += size
            return
        counts = self.rows.setdefault(key, [0, 0])
        counts[0] += 1
        counts[1] += size

    def report(self):
        return [dict(zip(FIELDS, key), packets=value[0], bytes=value[1])
                for key, value in sorted(self.rows.items(), key=lambda item: -item[1][1])]


def capture(minutes, max_rows):
    census = Census(max_rows)
    snapshot, refresh = (set(), set(), {}), 0
    start = time.monotonic()
    deadline = start + minutes * 60
    interrupted, snapshot_errors = False, 0
    # Binding AF_PACKET observes only bat0. No promiscuity, membership joins,
    # pcap payload file, rule changes or active network probes.
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)) as sock:
        sock.bind(('bat0', 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        buffer = bytearray(2048)
        try:
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= refresh:
                    try:
                        snapshot = source_snapshot()
                    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                        snapshot, snapshot_errors = (set(), set(), {}), snapshot_errors + 1
                    refresh = time.monotonic() + 2
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(min(.5, remaining))
                try:
                    size, _, _, address = sock.recvmsg_into([buffer], 0, socket.MSG_TRUNC)
                except socket.timeout:
                    continue
                # PACKET_OUTGOING=4. Ingress is visible before bridge filtering.
                # Ignore PACKET_LOOPBACK=5 if supplied by a future kernel.
                if address[2] == 5:
                    continue
                census.add(bytes(buffer[:min(size, len(buffer))]), size,
                           'out' if address[2] == 4 else 'in', snapshot)
        except KeyboardInterrupt:
            interrupted = True
        received, dropped = struct.unpack('II', sock.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8))
    return dict(interface='bat0', seconds=round(time.monotonic() - start, 3),
                interrupted=interrupted, packet_socket_received=received, kernel_drops=dropped,
                snapshot_errors=snapshot_errors, totals=dict(census.totals), rows=census.report(),
                notes=['in is pre-filter arrival; out has passed bridge filtering but is not a remote delivery receipt',
                       'source_port is bridge ingress attribution, sport is TCP/UDP source port',
                       'local(mac/ip) and end0(fdb)/AP(fdb) are inferences from snapshots every 2 seconds',
                       'routed, VLAN, ambiguous or unlearned sources can be unknown; spoofed MAC/IP cannot be distinguished',
                       'bytes are Ethernet lengths without FCS; headers only, no fragment reassembly; overflow is counted'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--minutes', type=float, default=5, help='capture duration (0 < N <= 1440)')
    parser.add_argument('--max-rows', type=int, default=20000, help='bounded detail rows; excess still counted')
    args = parser.parse_args()
    if not math.isfinite(args.minutes) or not 0 < args.minutes <= 1440:
        parser.error('--minutes must be finite and between 0 (exclusive) and 1440')
    if not 1 <= args.max_rows <= 100000:
        parser.error('--max-rows must be between 1 and 100000')
    try:
        print(json.dumps(capture(args.minutes, args.max_rows), indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, f'census: {error} (requires Linux bat0 and CAP_NET_RAW/root)\n')


if __name__ == '__main__':
    main()
