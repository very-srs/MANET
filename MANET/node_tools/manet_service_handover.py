"""Internal, short-lived service handover I/O. No daemon or operator command."""

import base64
import ctypes
import fcntl
import ipaddress
import os
from pathlib import Path
import select
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid

from manet_ip_runtime import module
from manet_registry_builder import RECORD


def handover_ranking(service, registry):
    """Read one fresh election snapshot, including the winner's identities."""
    ranker = module('mesh-service-election')
    text = Path(registry).read_text()
    now = float(Path(os.environ.get('MESH_UPTIME_FILE', '/proc/uptime')).read_text().split()[0])
    winner, score, incumbent = ranker.elect(service, text, now)
    for node in ranker.parse(text).values():
        entry, _ = ranker.eligible(node, now)
        if entry and entry[0] == winner:
            aliases = {mac.strip().lower() for mac in node.get('MAC_ADDRESSES', '').split(',')}
            aliases.add(winner)
            aliases = {mac for mac in aliases if ranker.MAC.fullmatch(mac)}
            return winner, score, incumbent, node.get(ranker.SERVER_FIELD[service]) == 'true', float(node['OBSERVED_AT_UPTIME']), aliases
    return winner, score, incumbent, False, 0, set()


def vip_claim_filter(vip4, vip6):
    """Admit service ARP claims or direct IPv6 NAs for the service target."""
    # Ethernet is decoded on br0 after the mesh encapsulation is removed.
    # Only the direct ICMPv6 form sent by send_vip_na is needed here.
    program = [(0x28, 0, 0, 12), (0x15, 'arp', 0, 0x0806),
               (0x15, 'na', 'drop', 0x86dd)]
    labels = {'arp': len(program)}
    program += [
        (0x20, 0, 0, 14), (0x15, 0, 'drop', 0x00010800),
        (0x28, 0, 0, 18), (0x15, 0, 'drop', 0x0604),
        (0x28, 0, 0, 20), (0x15, 1, 0, 1), (0x15, 0, 'drop', 2),
        (0x20, 0, 0, 28),
        (0x15, 0, 'drop', int(ipaddress.IPv4Address(vip4))),
        (0x06, 0, 0, 42),
    ]
    labels['na'] = len(program)
    program += [
        (0x30, 0, 0, 14), (0x54, 0, 0, 0xf0),
        (0x15, 0, 'drop', 0x60),  # IPv6 version
        (0x28, 0, 0, 20), (0x15, 0, 'drop', 0x3aff),  # ICMPv6, hop 255
        (0x28, 0, 0, 54), (0x15, 0, 'drop', 0x8800),  # NA type/code
    ]
    for offset, word in enumerate(struct.unpack(
            '!4I', socket.inet_pton(socket.AF_INET6, vip6))):
        program += [(0x20, 0, 0, 62 + offset * 4),
                    (0x15, 0, 'drop', word)]
    # Preserve the complete bounded NA for checksum and option validation.
    program.append((0x06, 0, 0, 2048))
    labels['drop'] = len(program)
    program.append((0x06, 0, 0, 0))
    return tuple((code, labels[jt] - i - 1 if isinstance(jt, str) else jt,
                  labels[jf] - i - 1 if isinstance(jf, str) else jf, value)
                 for i, (code, jt, jf, value) in enumerate(program))


def open_vip_claim_socket(iface, vip4, vip6, factory=socket.socket):
    """Attach and lock classic BPF before enabling ingress on the bridge."""
    class ClaimInstruction(ctypes.Structure):
        """Native Linux sock_filter layout."""
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                    ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint32)]

    class ClaimProgram(ctypes.Structure):
        """Native Linux sock_fprog layout, including pointer alignment."""
        _fields_ = [('length', ctypes.c_ushort),
                    ('instructions', ctypes.POINTER(ClaimInstruction))]

    rows = vip_claim_filter(vip4, vip6)
    instructions = (ClaimInstruction * len(rows))(
        *(ClaimInstruction(*row) for row in rows))
    descriptor = ClaimProgram(len(rows), instructions)
    sock = factory(socket.AF_PACKET, socket.SOCK_RAW, 0)
    try:
        sock.setsockopt(socket.SOL_SOCKET, 26, bytes(descriptor))
        sock.setsockopt(socket.SOL_SOCKET, 44, 1)
        sock.setsockopt(263, 23, 1)  # PACKET_IGNORE_OUTGOING
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
        sock.bind((iface, 3))  # ETH_P_ALL, restricted by BPF before reception
        return sock
    except BaseException:
        sock.close()
        raise


def vip_arp_sender(frame, vip):
    """Validate an ARP claim again in userspace and return its sender MAC."""
    if (len(frame) < 42 or frame[12:20] != bytes.fromhex('0806000108000604')
            or frame[20:22] not in (b'\0\1', b'\0\2')
            or frame[28:32] != socket.inet_aton(vip)
            or frame[6:12] != frame[22:28]):
        return None
    return frame[22:28].hex(':')


def vip_icmp_checksum(source, destination, packet):
    """Compute the ICMPv6 one's-complement checksum, including its header."""
    pseudo = source + destination + struct.pack(
        '!I3xB', len(packet), socket.IPPROTO_ICMPV6)
    data = pseudo + packet
    data += b'\0' * (len(data) % 2)
    total = sum(word[0] for word in struct.iter_unpack('!H', data))
    while total >> 16:
        total = (total & 0xffff) + (total >> 16)
    return (~total) & 0xffff


def vip_probe_identity(iface):
    """Read a usable bridge link-local address and MAC without spawning ip."""
    mac = bytes.fromhex(Path(
        f'/sys/class/net/{iface}/address').read_text().replace(':', ''))
    if len(mac) != 6 or mac[0] & 1:
        raise ValueError('Invalid bridge MAC for VIP probe')
    for line in Path('/proc/net/if_inet6').read_text().splitlines():
        fields = line.split()
        if len(fields) != 6 or fields[5] != iface:
            continue
        address = ipaddress.IPv6Address(int(fields[0], 16))
        # IFA_F_TENTATIVE and IFA_F_DADFAILED cannot source a valid probe.
        if address.is_link_local and not int(fields[4], 16) & 0x48:
            return address.packed, mac
    raise OSError('No usable bridge link-local address for VIP probe')


def send_vip_ns(packets, iface, vip6, aliases):
    """Send one unicast NS per winner alias, bypassing the local VIP route."""
    source, mac = vip_probe_identity(iface)
    target = ipaddress.IPv6Address(vip6).packed
    packet = struct.pack('!BBHI', 135, 0, 0, 0) + target + b'\x01\x01' + mac
    checksum = vip_icmp_checksum(source, target, packet)
    packet = packet[:2] + struct.pack('!H', checksum) + packet[4:]
    header = struct.pack('!IHBB', 6 << 28, len(packet), 58, 255)
    sent = False
    error = OSError('No remote winner alias for VIP probe')
    for alias in sorted(aliases):
        peer = bytes.fromhex(alias.replace(':', ''))
        if len(peer) != 6 or peer == mac or peer[0] & 1:
            continue
        frame = peer + mac + b'\x86\xdd' + header + source + target + packet
        try:
            packets.sendto(frame, socket.MSG_DONTWAIT, (iface, 0x86dd))
            sent = True
        except OSError as failure:
            error = failure
    if not sent:
        raise error
    return source, mac


def vip_na_sender(frame, vip6, probe=None):
    """Validate unsolicited claims or solicited replies to our VIP probe."""
    # AF_PACKET bypasses IPv6/ICMP validation. Check the direct, unfragmented
    # announcement form, including the RFC 4861 section 7.1.2 validity rules.
    target = ipaddress.IPv6Address(vip6)
    if (len(frame) < 78 or frame[12:14] != b'\x86\xdd'
            or frame[14] >> 4 != 6 or frame[20:22] != b'\x3a\xff'
            or frame[54:56] != b'\x88\x00'
            or target.is_multicast or frame[62:78] != target.packed
            or frame[22:38] != target.packed):
        return None
    if frame[58] & 0x40:
        if (probe is None or frame[38:54] != probe[0]
                or frame[:6] != probe[1] or frame[6:12] == probe[1]):
            return None
    elif (frame[38:54] != socket.inet_pton(socket.AF_INET6, 'ff02::1')
          or frame[:6] != bytes.fromhex('333300000001')):
        return None
    size = int.from_bytes(frame[18:20], 'big')
    if size < 24 or len(frame) < 54 + size:
        return None
    packet = frame[54:54 + size]
    if vip_icmp_checksum(frame[22:38], frame[38:54], packet):
        return None
    offset = 24
    while offset < size:
        if offset + 2 > size:
            return None
        length = packet[offset + 1] * 8
        if not length or offset + length > size:
            return None
        if packet[offset] == 2 and (length != 8 or
                packet[offset + 2:offset + 8] != frame[6:12]):
            return None
        offset += length
    return frame[6:12].hex(':')


def wait_for_successor(service, registry, winner, vip4, vip6, iface, since,
                       wake_path, ignored_path, timeout=12,
                       factory=open_vip_claim_socket):
    """Block only for claims or the next election, keeping service on timeout."""
    def status():
        """Re-rank before any withdrawal, rejecting old server observations."""
        current, _, _, serving, observed, aliases = handover_ranking(service, registry)
        if current != winner:
            return 'recheck', aliases
        ready = serving and observed > since
        return ('ready:registry' if ready else 'waiting'), aliases

    decision, aliases = status()
    if decision != 'waiting':
        return decision
    deadline = time.monotonic() + timeout
    # The caller holds its election flock until both sockets have closed.
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as wake:
        Path(wake_path).unlink(missing_ok=True)
        wake.bind(wake_path)
        os.chmod(wake_path, 0o600)
        try:
            with factory(iface, vip4, vip6) as packets:
                probe = None
                try:
                    probe = send_vip_ns(packets, iface, vip6, aliases)
                except (OSError, ValueError) as error:
                    # Passive claims and the next registry pass still work.
                    marker = Path(ignored_path + '.probe-error')
                    if not marker.exists():
                        marker.touch()
                        print(f'VIP NS probe unavailable: {error}',
                              file=sys.stderr)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return 'waiting'
                    readable, _, _ = select.select([wake, packets], [], [], remaining)
                    if not readable:
                        return 'waiting'
                    if wake in readable:
                        return 'recheck'
                    frame = packets.recv(2048)
                    sender = vip_arp_sender(frame, vip4)
                    signal = 'ARP'
                    if sender is None:
                        sender = vip_na_sender(frame, vip6, probe)
                        signal = ('NS probe' if sender is not None
                                  and frame[58] & 0x40 else 'NA')
                    if sender is None:
                        continue
                    decision, aliases = status()
                    if decision != 'waiting':
                        return decision
                    if sender in aliases:
                        return f'ready:{signal}'
                    if not Path(ignored_path).exists():
                        Path(ignored_path).touch()
                        print(f'Ignoring VIP {signal} from non-winner '
                              f'{sender}', file=sys.stderr)
        finally:
            Path(wake_path).unlink(missing_ok=True)


def wake_handover(path):
    """Interrupt an overlapping wait so its locked owner re-runs the election."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.2)
            sock.sendto(b'election', path)
    except OSError:
        pass  # The owner may instead be starting or stopping its service.


def send_vip_na(iface, vip, factory=socket.socket):
    """Send an unsolicited, overriding NA to all nodes with hop limit 255."""
    index = socket.if_nametoindex(iface)
    mac = bytes.fromhex(Path(f'/sys/class/net/{iface}/address').read_text().strip().replace(':', ''))
    # RFC 4861: Solicited=0, Override=1, target link-layer address option.
    packet = struct.pack('!BBHI', 136, 0, 0, 0x20000000) + socket.inet_pton(socket.AF_INET6, vip)
    packet += b'\x02\x01' + mac
    with factory(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6) as sock:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, index)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
        sock.bind((vip, 0, 0, index))
        # Linux computes the ICMPv6 checksum on raw ICMPv6 sockets.
        sock.sendto(packet, ('ff02::1', 0, 0, index))


def announce_service_vips(iface, vip4, vip6):
    """Announce IPv6 and three IPv4 claims using either Debian arping flavor."""
    send_vip_na(iface, vip6)
    version = subprocess.run(['arping', '-V'], capture_output=True, text=True, timeout=2)
    if 'iputils' in version.stdout + version.stderr:
        options = ['-I', iface, '-s', vip4, '-i', '1']
    else:
        options = ['-i', iface, '-S', vip4, '-W', '1']
    result = subprocess.run(['arping', '-U', '-c', '3', '-w', '4', *options, vip4],
                            capture_output=True, text=True, timeout=6)
    # Habets arping exits 1 when unsolicited requests receive no replies.
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise OSError(f'ARP announcement failed: {result.stderr.strip()}')


def publish_service_flag(service, own, serving, lock_path='/run/manet-service-telemetry.lock'):
    """Republish this node's current telemetry with its changed service flag."""
    import NodeInfo_pb2
    from google.protobuf.message import DecodeError

    # Serialize the two service jobs so one cannot overwrite the other's flag.
    # The manager continues its ordinary full telemetry publication schedule.
    with open(lock_path, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raw = subprocess.run(['alfred', '-r', '68'], check=True,
                             capture_output=True, text=True, timeout=3).stdout
        records = {mac.lower(): payload for mac, payload in RECORD.findall(raw)}
        if own not in records:
            raise ValueError('No local telemetry to update')
        message = NodeInfo_pb2.NodeTelemetry()
        try:
            message.ParseFromString(base64.b64decode(records[own], validate=True))
        except DecodeError as error:
            raise ValueError('Invalid local telemetry') from error
        if message.node_state == NodeInfo_pb2.NodeTelemetry.SHUTTING_DOWN:
            raise ValueError('Cannot republish shutdown telemetry as serving')
        setattr(message, f'is_{service}_server', serving == 'true')
        # Make the receiver observe a new record even if a previous claim's
        # flag was already true. Other service/radio/GPS fields are preserved.
        message.uptime_seconds = int(float(Path(os.environ.get('MESH_UPTIME_FILE', '/proc/uptime')).read_text().split()[0]))
        message.last_seen_timestamp = int(time.time())
        payload = base64.b64encode(message.SerializeToString()).decode('ascii')
        subprocess.run(['alfred', '-s', '68'], input=payload, check=True,
                       capture_output=True, text=True, timeout=3)


def prune_mumble_retired(current, now=None):
    """Keep this final export and two recent owner snapshots, up to 30 days."""
    current = Path(current)
    if not stat.S_ISREG(current.lstat().st_mode):
        raise ValueError('Retired export is not a regular file')
    cutoff = (time.time() if now is None else now) - 30 * 86400
    snapshots = []
    for path in current.parent.iterdir():
        if path == current or path.suffix != '.sqlite':
            continue
        try:
            if str(uuid.UUID(path.stem)) != path.stem:
                continue
        except ValueError:
            continue  # Leave Syncthing conflicts and unrelated files alone.
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue  # A synchronized deletion may have already arrived.
        if stat.S_ISREG(info.st_mode):
            snapshots.append((info.st_mtime_ns, path))
    snapshots.sort(key=lambda row: (row[0], row[1].name), reverse=True)
    for index, (modified, path) in enumerate(snapshots):
        if index >= 2 or modified < cutoff * 1_000_000_000:
            path.unlink(missing_ok=True)


def handover_main(argv):
    """Dispatch internal shell requests, retaining VIPs on input/I/O errors."""
    try:
        action, *args = argv
        if action == 'rank':
            winner, score, incumbent, _, _, _ = handover_ranking(*args)
            print(f'{winner or "-"} {score if score is not None else "-"} {incumbent or "-"}')
        elif action == 'wait':
            (service, registry, winner, vip4, vip6, iface, since,
             wake, ignored) = args
            print(wait_for_successor(service, registry, winner, vip4, vip6,
                                     iface, float(since), wake, ignored))
        elif action == 'wake':
            wake_handover(*args)
        elif action == 'announce':
            announce_service_vips(*args)
        elif action == 'publish':
            publish_service_flag(*args)
        elif action == 'db-signature':
            from manet_election_runtime import mumble_database_signature
            print(mumble_database_signature(*args))
        elif action == 'prune-retired':
            prune_mumble_retired(*args)
        elif action == 'normalize':
            print(ipaddress.IPv6Interface(args[0]))
        elif action == 'hosting':
            from manet_election_runtime import hosting
            print('true' if hosting(args[0]) else 'false')
        else:
            raise ValueError('unknown internal action')
        return 0
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
        if argv and argv[0] == 'wait' and len(argv) == 10:
            marker = Path(argv[-1] + '.error')
            if marker.exists():
                return 1
            marker.touch()
        print(f'Service handover: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(handover_main(sys.argv[1:]))
