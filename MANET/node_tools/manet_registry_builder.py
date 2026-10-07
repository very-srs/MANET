#!/usr/bin/env python3
"""Build the Alfred registry in one process; decode only changed payloads.

Keep the shell entry point for callers. The IP discovery helper imports build
directly, so one allocation pass pays for one interpreter and at most one
protobuf import, irrespective of mesh size. Freshness is NEVER cached.
"""

import base64
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

IDENTITY_FIELDS = 'HOSTNAME MAC_ADDRESSES IPV4_ADDRESS IPV4_CHUNK IPV4_CHUNK_SIZE SYNCTHING_ID'.split()
FIELDS = '''HOSTNAME MAC_ADDRESS MAC_ADDRESSES IPV4_ADDRESS IPV4_CHUNK
IPV4_CHUNK_SIZE SYNCTHING_ID MEAN_THROUGHPUT_MBPS GATEWAY_IFACE IS_NTP_SERVER
IS_MUMBLE_SERVER IS_TAK_SERVER IS_MEDIAMTX_SERVER UPTIME_SECONDS BATTERY_PERCENTAGE
CPU_LOAD_AVERAGE GPS_LATITUDE GPS_LONGITUDE GPS_ALTITUDE ATAK_USER
DATA_CHANNEL_2_4 DATA_CHANNEL_5_0 CHANNEL_REPORT_JSON LAST_SEEN_TIMESTAMP
IS_IN_LIMP_MODE LAST_TOURGUIDE_TIMESTAMP LAST_TOURGUIDE_RADIO CONFIG_ACK_VERSION
HALOW_TX_MCS HALOW_RX_MCS HALOW_MCS_PEER WIFI_24_TX_MCS WIFI_24_RX_MCS
WIFI_5_TX_MCS WIFI_5_RX_MCS INTERFACES_JSON EUD_MODE AP_SSID EUD_COUNT'''.split()
RECORD = re.compile(r'^\s*\{\s*"([0-9a-fA-F:]{17})"\s*,\s*"([^"]*)"', re.M)


def atomic_text(path, text, mode=0o644):
    """Unchanged files retain their inode/mtime for downstream change checks."""
    path = Path(path)
    try:
        if path.read_text() == text:
            return
    except FileNotFoundError:
        pass
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '-')
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def previous_fields(path):
    nodes = {}
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return nodes
    for line in lines:
        match = re.fullmatch(r'NODE_([0-9a-f]{12})_([A-Z0-9_]+)=(.*)', line)
        if match:
            node, key, value = match.groups()
            try:
                parts = shlex.split(value)
            except ValueError:
                continue
            if len(parts) == 1:
                nodes.setdefault(node, {})[key] = parts[0]
    return nodes


def decode(kind, payload, mac):
    # Lazy: steady, identical reads do not even import protobuf.
    import decoder
    fields = {}
    def emit(key, value):
        fields[key] = str(value)
    raw = base64.b64decode(payload)
    if kind == 'identity':
        decoder.decode_identity(raw, mac, emit, emit)
    else:
        decoder.decode_telemetry(raw, emit, emit)
    return fields


def build():
    registry = Path(os.environ.get('MESH_REGISTRY_FILE', '/var/run/mesh_node_registry'))
    claims = Path(os.environ.get('MESH_CLAIMED_CHUNKS_FILE', '/tmp/claimed_chunks.txt'))
    observed = Path(os.environ.get('MESH_REGISTRY_OBSERVED_FILE', '/run/manet-registry/observed.tsv'))
    observed.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(observed) + '.lock').open('a') as lock:
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Registry lock busy')
                time.sleep(.05)
        return _build(registry, claims, observed)


def _build(registry, claims, observed):
    snapshots = []
    for type_id in (67, 68):
        # Read BOTH successfully before touching any published state.
        raw = subprocess.run(['alfred', '-r', str(type_id)], check=True,
                             capture_output=True, text=True, timeout=5).stdout
        snapshots.append({mac.lower(): payload for mac, payload in RECORD.findall(raw) if payload})
    identities, telemetry = snapshots
    now = int(time.time())
    uptime = int(float(Path(os.environ.get('MESH_UPTIME_FILE', '/proc/uptime')).read_text().split()[0]))
    stale = int(os.environ.get('MESH_REGISTRY_STALE_AFTER', '300'))
    tombstone = int(os.environ.get('MESH_REGISTRY_TOMBSTONE_SECONDS', '900'))
    observations = {}
    try:
        for line in observed.read_text().splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
                observations[parts[0]] = (parts[1], int(parts[2]), int(parts[3]))
    except FileNotFoundError:
        pass
    previous = previous_fields(registry)
    cache_path = Path(str(observed) + '.decoded.json')
    cache = read_json(cache_path)
    # A tools/schema update must never reuse older decoding semantics.
    tools = Path(__file__).resolve().parent
    generation = [(p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in
                  (tools / 'decoder.py', tools / 'NodeInfo_pb2.py', tools / 'manet_ids.py')]
    generation = json.dumps(generation)
    old = cache.get('records', {}) if cache.get('generation') == generation else {}
    if not isinstance(old, dict):
        old = {}
    decoded = {}

    def fields(kind, mac, payload):
        key = kind + '/' + mac
        entry = old.get(key, {})
        if (not isinstance(entry, dict) or entry.get('payload') != payload
                or not isinstance(entry.get('fields'), dict)
                or any(not isinstance(k, str) or not isinstance(v, str)
                       for k, v in entry.get('fields', {}).items())):
            entry = {'payload': payload, 'fields': decode(kind, payload, mac)}
        decoded[key] = entry
        return dict(entry['fields'])

    output = ['# Mesh Node Registry', '# Generated locally from Alfred; quoted assignments.', '']
    claimed = []
    for mac, payload in sorted(telemetry.items()):
        try:
            data = fields('telemetry', mac, payload)
        except Exception as error:
            print(f'REGISTRY: telemetry decode failed for {mac}: {type(error).__name__}', file=sys.stderr)
            continue
        if mac in identities:
            try:
                data.update(fields('identity', mac, identities[mac]))
            except Exception as error:
                print(f'REGISTRY: identity decode failed for {mac}: {type(error).__name__}', file=sys.stderr)
        if not data.get('HOSTNAME'):
            saved = previous.get(mac.replace(':', ''), {})
            data.update({key: saved.get(key, '') for key in IDENTITY_FIELDS})
        data['MAC_ADDRESS'] = mac
        data['MAC_ADDRESSES'] = data.get('MAC_ADDRESSES') or mac
        digest = hashlib.sha256(payload.encode()).hexdigest()
        old_hash, seen, _ = observations.get(mac, ('', uptime, uptime))
        if old_hash != digest:
            seen = uptime
        observations[mac] = (digest, seen, uptime)
        age = max(0, uptime - seen)
        data['NODE_STATE'] = 'STALE' if age > stale else data.get('NODE_STATE', 'ACTIVE')
        data.update(IS_GATEWAY=data.get('IS_INTERNET_GATEWAY', ''),
                    OBSERVED_AGE_SECONDS=str(age), OBSERVED_AT_UPTIME=str(seen),
                    LAST_REGISTRY_UPDATE=str(now))
        prefix = 'NODE_' + mac.replace(':', '')
        for key in [*FIELDS, 'IS_GATEWAY', 'NODE_STATE', 'OBSERVED_AGE_SECONDS',
                    'OBSERVED_AT_UPTIME', 'LAST_REGISTRY_UPDATE']:
            value = data.get(key, '').replace("'", "'\\''")
            output.append(f"{prefix}_{key}='{value}'")
        output.append('')
        chunk, address = data.get('IPV4_CHUNK', ''), data.get('IPV4_ADDRESS', '')
        if data['NODE_STATE'] == 'ACTIVE' and re.fullmatch(r'[0-9]+', chunk) and address:
            try:
                start = str(int(ipaddress.IPv4Address(address)))
            except ValueError:
                start = ''
            size = data.get('IPV4_CHUNK_SIZE', '')
            if not re.fullmatch(r'[0-9]{1,6}', size):
                size = '0'
            claimed.append(f'{chunk},{mac},{start},{size}')
    retained = [f'{mac} {digest} {seen} {present}\n'
                for mac, (digest, seen, present) in sorted(observations.items())
                if uptime - present <= tombstone]
    atomic_text(claims, ''.join(line + '\n' for line in sorted(set(claimed))))
    atomic_text(registry, '\n'.join(output) + '\n')
    atomic_text(observed, ''.join(retained), 0o600)
    atomic_text(cache_path, json.dumps({'generation': generation, 'records': decoded}), 0o600)


if __name__ == '__main__':
    try:
        build()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'REGISTRY: refresh failed; retaining last complete snapshot: {error}', file=sys.stderr)
        sys.exit(1)
