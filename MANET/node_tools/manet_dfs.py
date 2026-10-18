"""Local DFS capability, passive channels and bounded background-CAC requests."""
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import selectors
import socket
import struct
import subprocess
import time

from manet_acs_agreement import DFS_CHANNELS
from manet_rx_policy import cm4_enabled


def netlink_messages(data):
    """Decode an entire datagram, including aligned multipart notifications."""
    while data:
        if len(data) < 16:
            raise ValueError('short netlink message')
        size, kind, flags, seq, pid = struct.unpack_from('IHHII', data)
        if size < 16 or size > len(data):
            raise ValueError('invalid netlink message')
        yield kind, seq, data[16:size]
        data = data[(size + 3) & ~3:]


class RadarEvents:
    """Sleep on nl80211 and file changes. No timed wakeups or debugfs polling."""

    def __init__(self, files):
        self.selector = selectors.DefaultSelector()
        self.net = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 16)
        self.fd = -1
        self.deferred = []
        try:
            self.net.bind((0, 0))
            self.net.settimeout(2)
            body = struct.pack('BBH', 3, 1, 0) + attr(2, b'nl80211\0')
            self.net.sendto(struct.pack('IHHII', 16 + len(body), 16, 1, 1, 0) + body, (0, 0))
            data, address = self.net.recvfrom(65536)
            if address[0] != 0:
                raise ValueError('non-kernel netlink family reply')
            families = [dict(attributes(body[4:])) for kind, seq, body in netlink_messages(data)
                        if kind == 16 and seq == 1 and len(body) >= 4]
            if len(families) != 1:
                raise ValueError('cannot resolve radar event family')
            family = families[0]
            self.family, = struct.unpack('H', family[1])
            groups = {}
            for _, group in attributes(family[7]):
                fields = dict(attributes(group))
                groups[fields[1].rstrip(b'\0')] = struct.unpack('I', fields[2])[0]
            for name in (b'mlme', b'config'):
                self.net.setsockopt(270, 1, groups[name])  # SOL_NETLINK, ADD_MEMBERSHIP
            self.net.setblocking(False)
            self.selector.register(self.net, selectors.EVENT_READ, 'net')
            self.libc = ctypes.CDLL(None, use_errno=True)
            self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if self.fd < 0:
                raise OSError(ctypes.get_errno(), 'inotify_init1')
            self.files = {Path(p).resolve() for p in files}
            self.watches = {}
            for directory in {p.parent for p in self.files}:
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                wd = self.libc.inotify_add_watch(self.fd, os.fsencode(directory),
                                                0x8 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800)
                if wd < 0:
                    raise OSError(ctypes.get_errno(), 'inotify_add_watch')
                self.watches[wd] = directory
            self.selector.register(self.fd, selectors.EVENT_READ, 'files')
        except BaseException:
            self.close()
            raise

    def close(self):
        self.selector.close()
        self.net.close()
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def wireless(self, data):
        result = []
        for kind, seq, body in netlink_messages(data):
            if kind != self.family or len(body) < 4:
                continue
            command = body[0]
            if command == 94:  # RADAR_DETECT, including CAC lifecycle
                fields = dict(attributes(body[4:]))
                if all(len(fields.get(k, b'')) == 4 for k in (1, 38, 168)):
                    result.append(dict(kind='radar', phy=struct.unpack('I', fields[1])[0],
                        frequency=struct.unpack('I', fields[38])[0],
                        event=struct.unpack('I', fields[168])[0], background=308 in fields))
            elif command == 110:  # CH_SWITCH_STARTED_NOTIFY
                fields = dict(attributes(body[4:]))
                if all(len(fields.get(k, b'')) == 4 for k in (3, 38, 159, 160)):
                    change = dict(kind='switch', ifindex=struct.unpack('I', fields[3])[0],
                        frequency=struct.unpack('I', fields[38])[0],
                        width=struct.unpack('I', fields[159])[0],
                        center=struct.unpack('I', fields[160])[0], block_tx=184 in fields,
                        started_boottime=time.clock_gettime(time.CLOCK_BOOTTIME))
                    handler = getattr(self, 'on_switch', None)
                    if handler:
                        handler(change)
                    # Recording a CSA must not request a radar-latch replay.
                    # In particular, DFS entry can share a frequency with an
                    # expired radar token from an earlier operating contract.
            elif command in (1, 2, 3, 4, 7, 8):  # wiphy/interface lifecycle
                result.append(dict(kind='change'))
        return result

    def file_events(self, data):
        result = []
        while len(data) >= 16:
            wd, mask, cookie, size = struct.unpack_from('iIII', data)
            if len(data) < 16 + size:
                raise ValueError('short inotify message')
            name = os.fsdecode(data[16:16 + size].rstrip(b'\0'))
            data = data[16 + size:]
            if mask & (0x400 | 0x800 | 0x8000):
                # systemd restarts and resubscribes before reading the latch.
                raise OSError('DFS event directory replaced')
            if mask & 0x4000:
                result.append(dict(kind='change'))
            parent = self.watches.get(wd)
            if parent is not None and parent / name in self.files:
                result.append(dict(kind='file', name=name))
        return result

    def drain_wireless(self):
        """Collect a switch racing a rejected command without losing radar.

        The kernel emits switch-start after blocking its queues and before
        releasing the wiphy lock used by a competing local CSA request.
        """
        while True:
            try:
                data, address = self.net.recvfrom(65536)
                if address[0] == 0:
                    self.deferred.extend(self.wireless(data))
            except BlockingIOError:
                return

    def wait(self):
        """Drain both inputs before any slow radio or publication operation."""
        while True:
            if getattr(self, 'deferred', None):
                result, self.deferred = self.deferred, []
                return result
            result = []
            for key, _ in self.selector.select():
                try:
                    if key.data == 'net':
                        data, address = self.net.recvfrom(65536)
                        if address[0] == 0:  # Notifications must come from kernel.
                            result.extend(self.wireless(data))
                    else:
                        result.extend(self.file_events(os.read(self.fd, 65536)))
                except BlockingIOError:
                    continue
                except OSError as exc:
                    if exc.errno != errno.ENOBUFS:
                        raise
                    result.append(dict(kind='change'))  # Re-read the latched event.
            if result:
                return result


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


def defer_busy(error):
    """Contention is not evidence of lost hardware support or clearance."""
    if error.errno in (errno.EBUSY, errno.EAGAIN):
        raise BlockingIOError(error.errno, error.strerror) from error


def hold_snapshot(path):
    """One bounded snapshot, shared by capability and live-token validation."""
    try:
        with Path(path).open() as stream:
            data = stream.read(16385)
        if len(data) > 16384:
            raise ValueError('oversized DFS hold')
        raw = json.loads(data)
        return raw if isinstance(raw, dict) else {}
    except OSError as exc:
        defer_busy(exc)
        return {}
    except (ValueError, TypeError):
        return {}


def guardian_ready():
    """Type=notify becomes active only after the radar subscription is ready."""
    try:
        subprocess.run(['systemctl', 'is-active', '--quiet', 'manet-dfs-watch.service'],
                       check=True, capture_output=True, timeout=2)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def supplicant_control(iface, message):
    """Send a bounded request to this interface's running control endpoint."""
    if not isinstance(iface, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', iface):
        raise ValueError('invalid DFS mesh interface')
    return subprocess.run(['wpa_cli', '-i', iface, 'raw', message], check=True,
                          capture_output=True, text=True, timeout=2).stdout.strip()


def running_supplicant(iface):
    """Query the live BSS, never a newly copied executable on disk."""
    try:
        return supplicant_control(iface, 'MANET_DFS_STATUS') in (
            'MANET_DFS_V3 owner=0', 'MANET_DFS_V3 owner=1')
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def external_owner(iface, enabled):
    """Change volatile per-BSS ownership only after a contract is armed."""
    if supplicant_control(iface, 'MANET_DFS_OWNER ' + str(int(enabled))) != 'OK':
        raise ValueError('running supplicant rejected DFS ownership')


def capable(path, phy_text, raw=None, iface=None):
    """Require the paired kernel, live supplicant and subscribed guardian."""
    if not cm4_enabled():
        return False
    try:
        raw = json.loads(path.read_text()) if raw is None else raw
        policy = Path('/usr/local/share/manet/dfs-enabled')
        return (raw.get('dfs_mesh_v1') is True and bool(dfs_frequencies(phy_text)) and
            Path('/sys/module/mt7915e/parameters/dfs_mesh_v1').read_text().strip() == 'Y' and
            (not policy.exists() or policy.read_text().strip() == '1') and
            running_supplicant(iface) and guardian_ready())
    except OSError as exc:
        defer_busy(exc)
        return False
    except (ValueError, TypeError):
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
