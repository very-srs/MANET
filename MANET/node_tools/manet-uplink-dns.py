#!/usr/bin/env python3
"""Select qualified DHCP DNS and resolve candidate probes on their own link."""
import argparse
import fcntl
import ipaddress
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import subprocess
import sys
import tempfile
import time

# networkd retains lease DNS for probes but never publishes it to resolved.
# Explicit option requests also retain the DHCP search list with UseDomains=no.
POLICY = '''# BEGIN MANET qualified DNS
[DHCPv4]
UseDNS=no
UseDomains=no
RoutesToDNS=no
RequestOptions=6 15 119

[DHCPv6]
UseDNS=no
UseDomains=no

[IPv6AcceptRA]
UseDNS=no
UseDomains=no
# END MANET qualified DNS
'''
PROBE_HOSTS = ('connectivitycheck.gstatic.com', 'cp.cloudflare.com')


def read_dns_text(path):
    """Read bounded local state, treating a missing producer as unavailable."""
    try:
        with path.open() as stream:
            value = stream.read(131073)
        return value if len(value) <= 131072 else ''
    except FileNotFoundError:
        return ''


def write_dns_text(path, text):
    """Durably replace owned configuration without exposing partial contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text.encode())
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)


def valid_iface(iface):
    """Accept a plain candidate name, never a mesh or loopback device."""
    return (re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', iface) is not None
            and not iface.startswith(('lo', 'br', 'bat')))


def dns_ipv4(value):
    """Accept unicast resolver/answer addresses, including private LANs."""
    try:
        address = ipaddress.IPv4Address(value)
        if (address.is_unspecified or address.is_multicast
                or address.is_loopback or int(address) == 0xffffffff):
            return ''
        return str(address)
    except ipaddress.AddressValueError:
        return ''


def domain(value):
    """Accept a DNS name without configuration or search-routing syntax."""
    value = value.rstrip('.').lower()
    if len(value) > 253:
        return ''
    labels = value.split('.')
    return value if all(re.fullmatch(
        r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
        for label in labels) else ''


def lease(root, iface):
    """Read the candidate's networkd IPv4 lease without executing its text."""
    if not valid_iface(iface):
        return {}, []
    net = root / 'sys/class/net' / iface
    try:
        index = read_dns_text(net / 'ifindex').strip()
        carrier = read_dns_text(net / 'carrier').strip()
        if (not index.isdecimal() or carrier != '1'
                or (net / 'master').exists()):
            return {}, []
        contents = read_dns_text(root / 'run/systemd/netif/leases' / index)
    except (OSError, UnicodeError):
        # A down/removed device can return EINVAL or ENODEV from sysfs.
        return {}, []
    fields = {}
    for line in contents.splitlines():
        key, sep, value = line.partition('=')
        if sep and not key.startswith('#'):
            fields[key] = value.strip().strip('"')
    if not dns_ipv4(fields.get('ADDRESS', '')):
        return {}, []
    servers = list(dict.fromkeys(filter(None, (
        dns_ipv4(word) for word in fields.get('DNS', '').split()))))[:3]
    return fields, servers


def resolver_config(root):
    """Publish only the promoted, live uplink; otherwise use public fallback."""
    iface = read_dns_text(root / 'run/upstream_iface').strip()
    fields, servers = ({}, [])
    if (root / 'run/mesh-gateway.state').exists():
        fields, servers = lease(root, iface)
    domains = []
    if servers:
        for word in (fields.get('DOMAIN_SEARCH_LIST', '') + ' ' +
                     fields.get('DOMAINNAME', '')).split():
            name = domain(word)
            if name and name not in domains:
                domains.append(name)
    text = '# Managed by manet-uplink-dns.py\n[Resolve]\nDNS=\nDomains=\n'
    if servers:
        # Debian 13 resolved 257.13 ignored scoped global DNS on the bench.
        # Plain servers use the on-link route or promoted default route.
        text += 'DNS=' + ' '.join(servers) + '\n'
        if domains:
            text += 'Domains=' + ' '.join(domains[:6]) + '\n'
    return text, iface if servers else ''


def dns_command(args):
    """Require systemd operations to finish successfully within 30 seconds."""
    subprocess.run(args, check=True, timeout=30, capture_output=True, text=True)


def reconcile_dns(root, command=dns_command):
    """Reload resolved only on selection changes, retrying failed activation."""
    state = root / 'run/manet-uplink-dns'
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        wanted, iface = resolver_config(root)
        config = root / 'run/systemd/resolved.conf.d/80-manet-uplink.conf'
        applied = state / 'applied'
        changed = read_dns_text(config) != wanted
        if changed:
            write_dns_text(config, wanted)
        if changed or read_dns_text(applied) != wanted:
            # systemd 257 resolved uses Type=notify-reload. No networkd reload.
            command(['systemctl', 'reload-or-restart',
                     'systemd-resolved.service'])
            write_dns_text(applied, wanted)
            print('manet-uplink-dns: ' + (f'using DHCP DNS on {iface}' if iface
                                        else 'using mesh fallback DNS'),
                  file=sys.stderr)


def policy_text(text):
    """Append the managed policy while preserving identity and other keys."""
    text = re.sub(r'(?ms)^# BEGIN MANET qualified DNS\n.*?'
                  r'^# END MANET qualified DNS\n?', '', text)
    return text.rstrip() + '\n\n' + POLICY


def migrate(root, command):
    """Migrate MANET DHCP files once, retaining an interrupted reload duty."""
    pending = root / 'var/lib/manet-uplink-dns/network-reload-needed'
    directory = root / 'etc/systemd/network'
    for path in sorted(directory.glob('*.network*')):
        # These are generated Ethernet defaults, backups and uplink configs.
        if (path.is_symlink() or not path.is_file() or not re.fullmatch(
                r'(?:10-end[0-9]+|20-[A-Za-z0-9_.-]+)\.network'
                r'(?:\.dhcp-default|\.off)?', path.name)):
            continue
        text = read_dns_text(path)
        if not re.search(r'(?m)^\s*DHCP=(?:yes|true|ipv4)\s*$', text):
            continue
        wanted = policy_text(text)
        if wanted != text:
            # Persist before replacement so a crash cannot lose activation.
            write_dns_text(pending, 'reload\n')
            write_dns_text(path, wanted)
    if pending.exists():
        command(['networkctl', 'reload'])
        pending.unlink()
    reconcile_dns(root, command)


def install(root, command=dns_command):
    """Serialize first-boot setup and recoverable updater migrations."""
    state = root / 'run/manet-uplink-dns'
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        migrate(root, command)


def dns_name(packet, offset):
    """Decode a bounded DNS name, rejecting invalid/cyclic compression."""
    labels, seen, end = [], set(), None
    while True:
        if offset in seen or offset >= len(packet) or len(seen) >= 128:
            raise ValueError('invalid DNS name')
        seen.add(offset)
        size = packet[offset]
        if size & 0xc0 == 0xc0:
            if offset + 1 >= len(packet):
                raise ValueError('short DNS pointer')
            if end is None:
                end = offset + 2
            offset = ((size & 63) << 8) | packet[offset + 1]
            continue
        if size > 63 or offset + 1 + size > len(packet):
            raise ValueError('invalid DNS label')
        offset += 1
        if not size:
            name = b'.'.join(labels).decode('ascii').lower()
            if len(name) > 253:
                raise ValueError('oversized DNS name')
            return name, end if end is not None else offset
        labels.append(packet[offset:offset + size])
        offset += size


def dns_reply(packet, query_id, host):
    """Validate a matching A response and follow its answer CNAME chain."""
    ident, flags, questions, answers, _, _ = struct.unpack('!6H', packet[:12])
    if ident != query_id or flags & 0xf80f != 0x8000 or questions != 1:
        raise ValueError('unexpected DNS response')
    name, offset = dns_name(packet, 12)
    if (name != host or packet[offset:offset + 4] != b'\x00\x01\x00\x01'):
        raise ValueError('unexpected DNS question')
    if flags & 0x0200:
        return None  # Retry a truncated UDP answer over TCP.
    offset += 4
    records, aliases = {}, {}
    if answers > 128:
        raise ValueError('too many DNS answers')
    for _ in range(answers):
        owner, offset = dns_name(packet, offset)
        kind, family, _, size = struct.unpack('!HHIH', packet[offset:offset+10])
        offset += 10
        end = offset + size
        if end > len(packet):
            raise ValueError('short DNS answer')
        if family == 1 and kind == 1 and size == 4:
            address = dns_ipv4(socket.inet_ntoa(packet[offset:end]))
            if address:
                records.setdefault(owner, []).append(address)
        elif family == 1 and kind == 5:
            aliases[owner], consumed = dns_name(packet, offset)
            if consumed != end:
                raise ValueError('invalid CNAME length')
        offset = end
    for _ in range(16):
        if host in records:
            return list(dict.fromkeys(records[host]))[:4]
        if host not in aliases:
            return []
        host = aliases[host]
    raise ValueError('CNAME loop')


def probe_timeout(sock, deadline, limit=4):
    """Give each blocking socket operation only the remaining query budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('DNS query deadline')
    sock.settimeout(min(limit, remaining))


def receive(sock, count, deadline):
    """Read one TCP DNS frame without extending the overall query deadline."""
    data = b''
    while len(data) < count:
        probe_timeout(sock, deadline)
        chunk = sock.recv(count - len(data))
        if not chunk or time.monotonic() >= deadline:
            raise TimeoutError('short DNS frame')
        data += chunk
    return data


def resolve(root, iface, host):
    """Resolve one probe name through DHCP DNS bound to the candidate device."""
    _, servers = lease(root, iface)
    # Four seconds total over at most three DNS servers, including TCP retry.
    deadline = time.monotonic() + 4
    ident = secrets.randbits(16)
    question = b''.join(bytes([len(p)]) + p.encode() for p in host.split('.'))
    query = struct.pack('!6H', ident, 0x0100, 1, 0, 0, 0)
    query += question + b'\0\0\x01\0\x01'
    for server in servers:
        if time.monotonic() >= deadline:
            break
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                                iface.encode() + b'\0')
                probe_timeout(sock, deadline, 1.2)
                sock.connect((server, 53))
                probe_timeout(sock, deadline, 1.2)
                sock.send(query)
                probe_timeout(sock, deadline, 1.2)
                answer = dns_reply(sock.recv(65535), ident, host)
            if answer is None:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                                    iface.encode() + b'\0')
                    probe_timeout(sock, deadline)
                    sock.connect((server, 53))
                    probe_timeout(sock, deadline)
                    sock.sendall(struct.pack('!H', len(query)) + query)
                    size = struct.unpack('!H', receive(sock, 2, deadline))[0]
                    answer = dns_reply(receive(sock, size, deadline),
                                       ident, host)
            if answer and time.monotonic() < deadline:
                return answer
        except (OSError, ValueError, struct.error, UnicodeError):
            continue
    return []


def main():
    """Expose DNS reconciliation, persistent migration and bound lookup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/'))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('reconcile')
    commands.add_parser('install')
    probe = commands.add_parser('resolve')
    probe.add_argument('iface')
    probe.add_argument('host', choices=PROBE_HOSTS)
    args = parser.parse_args()
    try:
        if args.command == 'resolve':
            answers = resolve(args.root, args.iface, args.host)
            if not answers:
                return 1
            print('\n'.join(answers))
        elif args.command == 'install':
            install(args.root)
        else:
            reconcile_dns(args.root)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'manet-uplink-dns: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
