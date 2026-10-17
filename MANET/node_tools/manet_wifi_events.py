"""Linux event inputs for the optional built-in Wi-Fi uplink follower.

No background commands or threads. Control sockets carry wireless events,
rtnetlink carries link/address changes, and inotify carries local intent and
service lifecycle notifications. All inputs are hints to re-read live state.
"""

import ctypes
import errno
import os
from pathlib import Path
import selectors
import shutil
import socket
import struct
import tempfile
import time


CONTROL_EVENTS = (
    'CTRL-EVENT-CONNECTED', 'CTRL-EVENT-DISCONNECTED',
    'CTRL-EVENT-CHANNEL-SWITCH', 'CTRL-EVENT-STARTED-CHANNEL-SWITCH',
    'CTRL-EVENT-REGDOM-CHANGE',
    'CTRL-EVENT-TERMINATING', 'INTERFACE-ENABLED', 'INTERFACE-DISABLED',
    'AP-ENABLED', 'AP-DISABLED', 'AP-CSA-FINISHED', 'DFS-CAC-START',
    'DFS-CAC-COMPLETED', 'DFS-RADAR-DETECTED',
)


class UplinkEvents:
    """Coalesce relevant events and survive replaced control sockets/files."""

    def __init__(self, uplink):
        self.uplink = uplink
        self.selector = selectors.DefaultSelector()
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p,
                                               ctypes.c_uint32]
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), 'inotify_init1')
        self.selector.register(self.fd, selectors.EVENT_READ, 'files')
        self.net = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW,
                                 socket.NETLINK_ROUTE)
        self.net.bind((0, 1 | 0x10))  # RTMGRP_LINK | RTMGRP_IPV4_IFADDR
        self.net.setblocking(False)
        self.selector.register(self.net, selectors.EVENT_READ, 'net')
        uplink.events_dir.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix='control-',
                                              dir=uplink.events_dir))
        self.watches = {}
        self.files = {}
        self.controls = {}
        self.retry_after = {}
        self.serial = 0
        self.iface = ''
        self.links = {}
        self.pending = set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        for path in list(self.controls):
            self.drop_control(path)
        self.net.close()
        os.close(self.fd)
        self.selector.close()
        shutil.rmtree(self.directory)

    @staticmethod
    def canonical(path):
        # /var/run and /run share an inotify watch descriptor. Keep event
        # names and input keys in the same namespace, including absent files.
        return path.parent.resolve() / path.name

    @staticmethod
    def fingerprint(path):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def watch_directories(self, paths):
        # Create missing socket/input directories before subscribing. Watching
        # an ancestor such as /run wakes us for unrelated service churn even
        # when file_events later discards every notification.
        directories = {path.parent.resolve() for path in paths}
        for wd, directory in list(self.watches.items()):
            if directory not in directories:
                self.libc.inotify_rm_watch(self.fd, wd)
                del self.watches[wd]
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            if directory in self.watches.values():
                continue
            mask = 0x4 | 0x8 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800
            wd = self.libc.inotify_add_watch(self.fd, os.fsencode(directory), mask)
            if wd < 0:
                raise OSError(ctypes.get_errno(), 'inotify_add_watch')
            self.watches[wd] = directory

    def sync(self, iface, *, probe=False):
        """Subscribe before reconciliation, and resample after ATTACH ACK."""
        u = self.uplink
        self.iface = iface
        if probe:
            self.retry_after.clear()
        inputs = {u.config, u.radio.conf, u.repair_path, u.radio.gateway,
                  u.events_dir / 'client-event',
                  u.events_dir / 'ap-event'}
        inputs.update(u.roles / name for name in
                      ('mesh_if', 'mesh_24_if', 'mesh_5_if', 'ap_interface'))
        inputs = {self.canonical(path) for path in inputs}
        for path in inputs:
            if path not in self.files:
                self.files[path] = self.fingerprint(path)
        targets = ({u.run / 'wpa_supplicant' / iface,
                    u.run / 'hostapd' / 'manetap'} if iface else set())
        targets = {self.canonical(path) for path in targets}
        self.watch_directories(inputs | targets)
        for path in list(self.controls):
            endpoint = self.controls[path]
            try:
                stat = path.stat()
                same = (stat.st_dev, stat.st_ino, stat.st_ctime_ns) == endpoint['identity']
            except FileNotFoundError:
                same = False
            if path not in targets or not same:
                self.drop_control(path)
                self.pending.add('change')
            elif probe and endpoint['deadline'] is None:
                try:
                    endpoint['socket'].send(b'PING')
                    endpoint['deadline'] = time.monotonic() + 3
                except OSError:
                    self.drop_control(path)
                    self.pending.add('change')
        for path in targets - self.controls.keys():
            self.attach(path)

    def attach(self, path):
        if time.monotonic() < self.retry_after.get(path, 0):
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.serial += 1
        local = self.directory / str(self.serial)
        try:
            sock.bind(str(local))
            sock.setblocking(False)
            sock.connect(str(path))
            stat = path.stat()
            sock.send(b'ATTACH')
        except OSError:
            sock.close()
            local.unlink(missing_ok=True)
            # Service start/stop and socket creation trigger another attach.
            # The slow safety pass covers a stale socket after an unclean exit.
            return
        self.controls[path] = {'socket': sock, 'local': local,
                               'identity': (stat.st_dev, stat.st_ino, stat.st_ctime_ns),
                               'deadline': time.monotonic() + 3}
        self.selector.register(sock, selectors.EVENT_READ, path)

    def drop_control(self, path):
        endpoint = self.controls.pop(path)
        sock = endpoint['socket']
        try:
            sock.send(b'DETACH')
        except OSError:
            pass
        self.selector.unregister(sock)
        sock.close()
        endpoint['local'].unlink(missing_ok=True)

    def file_events(self, data):
        changed = set()
        offset = 0
        while offset + 16 <= len(data):
            wd, mask, _, size = struct.unpack_from('iIII', data, offset)
            name = os.fsdecode(data[offset + 16:offset + 16 + size].rstrip(b'\0'))
            offset += 16 + size
            if mask & 0x4000:  # IN_Q_OVERFLOW
                changed.add('change')
            parent = self.watches.get(wd)
            if mask & (0x400 | 0x800 | 0x8000):
                self.watches.pop(wd, None)
                if not mask & 0x8000:
                    self.libc.inotify_rm_watch(self.fd, wd)
                changed.add('change')
            if parent is None:
                continue
            path = parent / name
            if path in self.files:
                value = self.fingerprint(path)
                # Lifecycle markers use touch, so equal content still signals.
                if value != self.files[path] or path.name in (
                        'client-event', 'ap-event'):
                    self.files[path] = value
                    changed.add('change')
            if path.name in ('hostapd', 'wpa_supplicant', self.iface, 'manetap'):
                self.retry_after.pop(path, None)
                if path in self.controls and mask & (0x80 | 0x100 | 0x200):
                    self.drop_control(path)
                changed.add('change')
        return changed

    def control_events(self, path, data):
        message = data.decode(errors='replace').strip()
        if message in ('OK', 'PONG'):
            self.controls[path]['deadline'] = None
            return {'change'} if message == 'OK' else set()
        event = message.partition('>')[2].split(' ', 1)[0]
        if event not in CONTROL_EVENTS:
            return set()
        if event == 'CTRL-EVENT-TERMINATING':
            self.drop_control(path)
        if event == 'CTRL-EVENT-DISCONNECTED' and path.name == self.iface:
            return {'change', 'disconnect'}
        if event == 'CTRL-EVENT-STARTED-CHANNEL-SWITCH' and path.name == self.iface:
            return {'change', 'csa-start'}
        if event == 'CTRL-EVENT-CHANNEL-SWITCH' and path.name == self.iface:
            return {'change', 'csa-complete'}
        return {'change'}

    def net_events(self, data):
        changed = set()
        offset = 0
        while offset + 16 <= len(data):
            size, kind = struct.unpack_from('IH', data, offset)
            if size < 16 or offset + size > len(data):
                break
            body = data[offset + 16:offset + size]
            offset += (size + 3) & ~3
            if kind in (16, 17) and len(body) >= 16:  # NEWLINK/DELLINK
                _, _, _, index, flags, _ = struct.unpack_from('BBHiII', body)
                attrs, pos = {}, 16
                while pos + 4 <= len(body):
                    length, key = struct.unpack_from('HH', body, pos)
                    if length < 4 or pos + length > len(body):
                        break
                    attrs[key] = body[pos + 4:pos + length]
                    pos += (length + 3) & ~3
                name = attrs.get(3, b'').rstrip(b'\0').decode(errors='replace')
                if name not in (self.iface, 'manetap', 'end0'):
                    continue
                state = kind, flags, attrs.get(10), attrs.get(16)  # master/operstate
                if self.links.get(index) != state:
                    self.links[index] = state
                    changed.add('change')
            elif kind in (20, 21) and len(body) >= 8:  # NEWADDR/DELADDR
                index = struct.unpack_from('I', body, 4)[0]
                try:
                    name = socket.if_indextoname(index)
                except OSError:
                    continue
                if name == self.iface:
                    changed.add('address')
        return changed

    def wait(self, timeout):
        """Block until a relevant event or the caller's recovery deadline."""
        until = time.monotonic() + timeout
        while True:
            now = time.monotonic()
            deadlines = [e['deadline'] for e in self.controls.values()
                         if e['deadline'] is not None]
            delay = max(0, min([until, *deadlines]) - now)
            ready = self.selector.select(0 if self.pending else delay)
            result, self.pending = self.pending, set()
            for key, _ in ready:
                try:
                    if key.data == 'files':
                        result.update(self.file_events(os.read(self.fd, 65536)))
                    elif key.data == 'net':
                        result.update(self.net_events(self.net.recv(65536)))
                    elif key.data in self.controls:
                        result.update(self.control_events(key.data, key.fileobj.recv(65536)))
                except BlockingIOError:
                    continue
                except OSError as error:
                    if error.errno == errno.ENOBUFS:
                        result.add('change')
                    elif key.data in self.controls:
                        self.drop_control(key.data)
                        result.add('change')
                    else:
                        raise
            now = time.monotonic()
            for path, endpoint in list(self.controls.items()):
                if endpoint['deadline'] is not None and endpoint['deadline'] <= now:
                    self.drop_control(path)
                    self.retry_after[path] = now + 120
                    result.add('change')
            if result or now >= until:
                return result
