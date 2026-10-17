"""Local DFS capability, passive channels and bounded background-CAC requests."""
import json
import os
from pathlib import Path
import re
import socket
import struct

from manet_acs_agreement import DFS_CHANNELS
from manet_rx_policy import cm4_enabled


def dfs_frequencies(text):
    """Use live PHY flags, including NOP, instead of a regulatory whitelist."""
    found = []
    # iw phy info uses decimal MHz; iw phy channels uses integers. Split
    # every channel so a neighbor's disabled/NOP state cannot leak into it.
    for block in re.split(r'(?=\* \d+(?:\.\d+)? MHz)', text):
        match = re.match(r'\* (\d+)(?:\.0+)? MHz', block)
        if (match and int(match[1]) in DFS_CHANNELS and
                'radar detection' in block.lower() and
                'disabled' not in block.lower() and
                'unavailable' not in block.lower()):
            found.append(int(match[1]))
    return sorted(set(found))


def capable(path, phy_text):
    """Only the paired release ABI and a DFS-marked live PHY admit plans."""
    if not cm4_enabled():
        return False
    try:
        raw = json.loads(path.read_text())
        return raw.get('dfs_mesh_v1') is True and bool(dfs_frequencies(phy_text)) and (
            Path('/sys/module/mt7915e/parameters/dfs_mesh_v1').read_text().strip() == 'Y')
    except (OSError, ValueError, TypeError):
        return False


def attr(kind, data):
    raw = struct.pack('HH', len(data) + 4, kind) + data
    return raw + b'\0' * (-len(raw) % 4)


def attributes(data):
    while data:
        if len(data) < 4:
            raise ValueError('short netlink attribute')
        size, kind = struct.unpack_from('HH', data)
        if size < 4 or size > len(data):
            raise ValueError('invalid netlink attribute')
        yield kind & 0x3fff, data[4:size]
        data = data[(size + 3) & ~3:]


def request(iface, command, payload):
    """Send one bounded nl80211 operation and require its kernel ACK."""
    index = socket.if_nametoindex(iface)
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 16) as sock:
        sock.settimeout(2)
        sock.bind((0, 0))
        sequence = 0
        def request(family, cmd, attrs):
            nonlocal sequence
            sequence += 1
            payload = struct.pack('BBH', cmd, 1, 0) + b''.join(attrs)
            sock.sendto(struct.pack('IHHII', 16 + len(payload), family, 5,
                                    sequence, 0) + payload, (0, 0))
            messages = []
            while True:
                data = sock.recv(65536)
                while len(data) >= 16:
                    size, kind, flags, seq, pid = struct.unpack_from('IHHII', data)
                    if size < 16 or size > len(data):
                        raise ValueError('invalid netlink message')
                    payload = data[16:size]
                    data = data[(size + 3) & ~3:]
                    if seq != sequence:
                        continue
                    if kind == 2:
                        error, = struct.unpack_from('i', payload)
                        if error:
                            raise OSError(-error, os.strerror(-error))
                        return messages
                    messages.append(payload)
        family = request(16, 3, [attr(2, b'nl80211\0')])
        ids = [struct.unpack('H', value)[0] for msg in family
               for kind, value in attributes(msg[4:]) if kind == 1]
        if len(ids) != 1:
            raise ValueError('cannot resolve nl80211')
        request(ids[0], command, [attr(3, struct.pack('I', index))] + payload)


def channel_attrs(frequency):
    if type(frequency) is not int or frequency not in DFS_CHANNELS:
        raise ValueError('invalid DFS primary')
    return [attr(k, struct.pack('I', v)) for k, v in
            ((38, frequency), (159, 1), (160, frequency))]


def start_cac(iface, frequency):
    """ACK starts kernel-owned background CAC, never proves clearance."""
    request(iface, 94, channel_attrs(frequency) + [attr(308, b'')])


def switch_dfs(iface, frequency):
    """CSA includes HANDLE_DFS, required for a mesh entering radar channels."""
    request(iface, 102, channel_attrs(frequency) +
            [attr(183, struct.pack('I', 5)), attr(191, b'')])
