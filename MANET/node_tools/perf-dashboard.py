#!/usr/bin/env python3
"""
MANET Performance Dashboard
-----------------------------
Port 8081. Runs on all nodes. Accessible via wlan3 AP or LAN.

Endpoints:
  GET  /                        - Dashboard HTML
  GET  /api/topology            - Mesh topology (nodes, interfaces)
  POST /api/interface/toggle    - Toggle wlan interface on node(s)
  POST /api/halow/channel       - Set HaLow channel/BW on all nodes
  POST /api/wifi/channel        - Set Wi-Fi channel on all nodes
  POST /api/txpower             - Set TX power on node/interface
  POST /api/measure/start       - Start iperf3/ping session
  GET  /api/measure/status      - Current measurement status
  GET  /api/sessions            - List saved sessions
  GET  /api/sessions/<id>       - Get session JSON
  GET  /api/sessions/<id>/csv   - Get session CSV
  DELETE /api/sessions/<id>     - Delete saved session
  POST /api/upload/github       - Git push measurements/
  POST /api/upload/ventum       - curl -u upload to Ventum
"""

import http.server
import socketserver
import json
import subprocess
import re
import os
import time
import threading
import csv
import io
import shutil
import hashlib
import urllib.request
import ipaddress
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote

PORT            = 8081
MESH_CONF_FILE  = '/etc/mesh.conf'
MESH_STATE_FILE = '/etc/mesh_ipv4_state'
REGISTRY_FILE   = '/var/run/mesh_node_registry'
SESSIONS_DIR    = '/var/log/manet-measurements'
CONTROL_PORT    = 80  # mesh-status.py port on each node
ALFRED_RADIO_TYPE = 71
ALFRED_RADIO_ACK_TYPE = 72

# EU S1G channels (centre frequencies in MHz)
HALOW_EU_CHANNELS = [863500, 864500, 865500, 866500, 867500, 868500]
HALOW_BW_OPTIONS  = ['1MHz', '2MHz', '4MHz']

# Active measurement state
_measure_lock   = threading.Lock()
_measure_status = {
    'running': False, 'label': '', 'progress': '', 'error': '',
    'done': 0, 'total': 0, 'started_at': None, 'current_started_at': None,
    'current': None, 'last_result': None,
}

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def load_kv_file(path):
    conf = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    k, v = line.split('=', 1)
                    conf[k.strip()] = v.strip().strip('"\'')
    except Exception:
        pass
    return conf

def norm_mac(mac):
    return mac.lower().replace('-', ':').strip()

def get_my_hostname():
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return 'unknown'

def get_my_ip():
    state = load_kv_file(MESH_STATE_FILE)
    return state.get('CURRENT_IPV4', '')

def has_internet():
    try:
        urllib.request.urlopen('https://github.com', timeout=3)
        return True
    except Exception:
        return False

def parse_registry():
    nodes = {}
    try:
        with open(REGISTRY_FILE) as f:
            content = f.read()
        pattern = re.compile(r"NODE_([A-Fa-f0-9]+)_([A-Z0-9_]+)='([^']*)'")
        for m in pattern.finditer(content):
            nid, field, value = m.groups()
            if nid not in nodes:
                nodes[nid] = {'id': nid}
            nodes[nid][field] = value
    except Exception:
        pass
    return nodes

def get_bat0_active_ifaces():
    try:
        r = subprocess.run(['batctl', 'if'], capture_output=True, text=True, timeout=5)
        return [
            m.group(1)
            for l in r.stdout.splitlines()
            for m in [re.match(r'^\s*([^:\s]+):\s+active\b', l)]
            if m
        ]
    except Exception:
        return []

def _first_flat_value(data, keys):
    """Find the first matching key in a nested dict/list structure."""
    if isinstance(data, dict):
        for key in keys:
            if key in data and data[key] not in (None, ''):
                return data[key]
        for value in data.values():
            found = _first_flat_value(value, keys)
            if found not in (None, ''):
                return found
    elif isinstance(data, list):
        for item in data:
            found = _first_flat_value(item, keys)
            if found not in (None, ''):
                return found
    return None

def _json_from_text(text):
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char not in '[{':
            continue
        try:
            data, _ = decoder.raw_decode(text[idx:])
            return data
        except Exception:
            pass
    return None

def _format_halow_bw(value):
    if value in (None, ''):
        return ''
    text = str(value).strip()
    low = text.lower()
    if low.endswith('mhz'):
        return text.replace('mhz', 'MHz').replace('MHZ', 'MHz')
    try:
        num = float(text)
        if num >= 1000000:
            num /= 1000000
        elif num >= 1000:
            num /= 1000
        if num in (1, 2, 4):
            return f'{int(num)}MHz'
    except Exception:
        pass
    return text

def _channel_from_frequency(freq_value):
    try:
        m = re.search(r'[0-9.]+', str(freq_value))
        freq = float(m.group(0)) if m else None
    except Exception:
        return '', ''
    if freq is None:
        return '', ''
    if freq > 1000000:
        freq_khz = freq / 1000.0
        freq_mhz = freq / 1000000.0
    elif freq > 1000:
        freq_khz = freq
        freq_mhz = freq / 1000.0
    else:
        freq_khz = freq * 1000.0
        freq_mhz = freq
    channel = ''
    for idx, center_khz in enumerate(HALOW_EU_CHANNELS, start=1):
        if abs(freq_khz - center_khz) <= 500:
            channel = str(idx)
            break
    return channel, f'{freq_mhz:.3f}'.rstrip('0').rstrip('.')

def _parse_morse_channel_output(text):
    info = {}
    data = _json_from_text(text)
    if data is not None:
        freq = _first_flat_value(data, [
            'channel_frequency', 'frequency', 'freq', 'freq_khz', 'freq_hz',
            'operating_frequency', 'op_chan_freq'
        ])
        bw = _first_flat_value(data, [
            'channel_op_bw', 'op_bw', 'operating_bw', 'channel_bw',
            'bandwidth', 'bw', 'op_chan_bw'
        ])
        idx = _first_flat_value(data, [
            'channel_index', 'channel', 'primary_channel', 's1g_channel'
        ])
    else:
        freq = None
        bw = None
        idx = None
        for key in ('channel_frequency', 'frequency', 'freq_khz', 'freq_hz', 'op_chan_freq'):
            m = re.search(rf'{key}\s*[:=]\s*"?([0-9.]+)"?', text, re.I)
            if m:
                freq = m.group(1)
                break
        for key in ('channel_op_bw', 'op_bw', 'operating_bw', 'channel_bw', 'bandwidth', 'op_chan_bw'):
            m = re.search(rf'{key}\s*[:=]\s*"?([0-9.]+\s*(?:[kKmM][hH][zZ])?)"?', text, re.I)
            if m:
                bw = m.group(1)
                break
        m = re.search(r'channel(?:_index)?\s*[:=]\s*"?(\d+)"?', text, re.I)
        if m:
            idx = m.group(1)

    if freq not in (None, ''):
        channel, freq_mhz = _channel_from_frequency(freq)
        if channel:
            info['channel'] = channel
        if freq_mhz:
            info['freq_mhz'] = freq_mhz
    if bw not in (None, ''):
        info['halow_bw'] = _format_halow_bw(bw)
    if idx not in (None, '') and 'channel' not in info:
        info['channel'] = str(idx)
    if info:
        info['halow_source'] = 'morse'
    return info

def get_halow_driver_info(iface='wlan2'):
    """Read HaLow runtime channel data from Morse tooling; config is only fallback."""
    binaries = ['/usr/local/bin/morse_cli', 'morse_cli']
    variants = [
        lambda b: [b, '-i', iface, 'channel', '-j'],
        lambda b: [b, '-i', iface, 'channel', '--json'],
        lambda b: [b, 'channel', '-i', iface, '-j'],
        lambda b: [b, '-i', iface, 'channel'],
        lambda b: [b, 'channel', '-i', iface],
    ]
    seen = set()
    for binary in binaries:
        if binary.startswith('/') and not os.path.exists(binary):
            continue
        for build in variants:
            cmd = build(binary)
            key = tuple(cmd)
            if key in seen:
                continue
            seen.add(key)
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            except Exception:
                continue
            text = (r.stdout or '') + '\n' + (r.stderr or '')
            if r.returncode != 0 and not text.strip():
                continue
            parsed = _parse_morse_channel_output(text)
            if parsed:
                return parsed

    info = {}
    for conf_path in (
        '/etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf',
        '/etc/wpa_supplicant/wpa_supplicant_s1g-wlan2.conf',
    ):
        try:
            with open(conf_path) as f:
                txt = f.read()
        except Exception:
            continue
        m = re.search(r'channel\s*=\s*(\d+)', txt)
        if m:
            info['channel'] = m.group(1)
        m = re.search(r'op_class\s*=\s*(\d+)', txt)
        if m:
            info['op_class'] = m.group(1)
        m = re.search(r's1g_prim_chwidth\s*=\s*(\d+)', txt)
        if m:
            info['halow_bw'] = {'0': '1MHz', '1': '2MHz', '2': '4MHz'}.get(m.group(1), m.group(1))
        if info:
            info['halow_source'] = 'config'
            return info
    return info

def call_node_api(node_ip, path, method='GET', data=None, timeout=8):
    """Call mesh-status.py control API on a remote node."""
    try:
        url = f'http://{node_ip}:{CONTROL_PORT}{path}'
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={'Content-Type': 'application/json', 'User-Agent': 'perf-dashboard/1'}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {'ok': False, 'error': str(e)}

def _add_alfred_candidate(items, value, kind):
    if isinstance(value, bytes):
        value = value.decode(errors='ignore')
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return
        try:
            value = json.loads(value)
        except Exception:
            return
    if isinstance(value, dict) and value.get('kind') == kind:
        items.append(value)

def _extract_alfred_objects(raw, kind):
    items = []
    _add_alfred_candidate(items, raw, kind)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for value in data.values():
                _add_alfred_candidate(items, value, kind)
        elif isinstance(data, list):
            for value in data:
                _add_alfred_candidate(items, value, kind)
    except Exception:
        pass
    for line in raw.splitlines():
        _add_alfred_candidate(items, line, kind)
    for match in re.finditer(r'"((?:\\.|[^"\\])*)"\s*(?:[,}])', raw):
        try:
            text = bytes(match.group(1), 'utf-8').decode('unicode_escape')
        except Exception:
            continue
        _add_alfred_candidate(items, text, kind)
    return items

def read_alfred_objects(type_id, kind):
    try:
        r = subprocess.run(['alfred', '-r', str(type_id)],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
        return _extract_alfred_objects(r.stdout, kind)
    except Exception:
        return []

def send_alfred_object(type_id, obj):
    payload = json.dumps(obj, separators=(',', ':'))
    try:
        r = subprocess.run(['alfred', '-s', str(type_id)],
                           input=payload, capture_output=True, text=True, timeout=5)
        return r.returncode == 0, (r.stderr or r.stdout or '').strip()
    except Exception as e:
        return False, str(e)

def radio_expected_hosts():
    try:
        subprocess.run(['/usr/local/bin/mesh-registry-builder.sh'],
                       capture_output=True, text=True, timeout=8)
    except Exception:
        pass
    nodes = parse_registry()
    now = int(time.time())
    hosts = []
    for nd in nodes.values():
        host = nd.get('HOSTNAME', '')
        if not host:
            continue
        if nd.get('NODE_STATE', 'ACTIVE') == 'SHUTTING_DOWN':
            continue
        try:
            last_seen = int(nd.get('LAST_SEEN_TIMESTAMP', '0') or 0)
        except Exception:
            last_seen = 0
        if last_seen and now - last_seen > 600:
            continue
        hosts.append(host)
    my_host = get_my_hostname()
    if my_host and my_host not in hosts:
        hosts.append(my_host)
    return sorted(set(hosts))

def radio_target_for_node(node_ip):
    if node_ip == 'all':
        return 'all'
    for nd in parse_registry().values():
        if nd.get('IPV4_ADDRESS', '') == node_ip:
            host = nd.get('HOSTNAME', '')
            if host:
                return [host]
    raise ValueError(f'Unknown node IP {node_ip}')

def make_radio_version(pkg):
    basis = json.dumps(pkg, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(basis.encode()).hexdigest()[:10]

def radio_ack_snapshot(version):
    latest = {}
    for ack in read_alfred_objects(ALFRED_RADIO_ACK_TYPE, 'radio_ack'):
        if ack.get('version') != version:
            continue
        host = ack.get('hostname', '')
        if host:
            latest[host] = ack
    return latest

def wait_radio_acks(version, expected_hosts, timeout=75):
    deadline = time.time() + timeout
    expected = set(expected_hosts)
    last = {}
    while time.time() < deadline:
        last = radio_ack_snapshot(version)
        ok_hosts = {h for h, a in last.items() if a.get('ok') is True}
        bad = {h: a for h, a in last.items() if a.get('ok') is False}
        missing = sorted(expected - ok_hosts - set(bad.keys()))
        if bad or not missing:
            return {'ok': not bad and not missing, 'acks': last,
                    'missing': missing, 'bad': bad}
        time.sleep(2)
    ok_hosts = {h for h, a in last.items() if a.get('ok') is True}
    bad = {h: a for h, a in last.items() if a.get('ok') is False}
    missing = sorted(expected - ok_hosts - set(bad.keys()))
    return {'ok': False, 'acks': last, 'missing': missing, 'bad': bad}

def cancel_radio_version(version):
    cancel = {
        'kind': 'radio_cancel',
        'version': version,
        'issued_by': get_my_hostname(),
        'issued_at': int(time.time()),
    }
    send_alfred_object(ALFRED_RADIO_TYPE, cancel)

def coordinate_radio_toggle(node_ip, iface, state):
    if iface not in ('wlan0', 'wlan1', 'wlan2') or state not in ('up', 'down'):
        return {'ok': False, 'error': 'Invalid iface or state'}

    targets = radio_target_for_node(node_ip)
    expected = radio_expected_hosts()
    if not expected:
        return {'ok': False, 'error': 'No reachable nodes in registry'}
    if node_ip == 'all' and len(expected) < 2:
        return {
            'ok': False,
            'error': 'Refusing global radio change: registry sees fewer than 2 reachable nodes. Wait for Alfred registry refresh and retry.',
            'expected': expected,
        }

    pkg = {
        'kind': 'radio_state',
        'issued_by': get_my_hostname(),
        'issued_at': int(time.time()),
        'activate_at': 0,
        'targets': targets,
        'desired': {iface: state},
    }
    pkg['version'] = make_radio_version(pkg)

    ok, error = send_alfred_object(ALFRED_RADIO_TYPE, pkg)
    if not ok:
        return {'ok': False, 'error': f'Alfred stage failed: {error}'}

    ack_state = wait_radio_acks(pkg['version'], expected, timeout=75)
    if not ack_state['ok']:
        cancel_radio_version(pkg['version'])
        bad = [
            f"{host}: {ack.get('error') or 'rejected'}"
            for host, ack in sorted(ack_state['bad'].items())
        ]
        parts = []
        if ack_state['missing']:
            parts.append('missing ACK: ' + ', '.join(ack_state['missing']))
        if bad:
            parts.append('rejected: ' + '; '.join(bad))
        return {
            'ok': False,
            'error': '; '.join(parts) or 'ACK timeout',
            'version': pkg['version'],
            'missing': ack_state['missing'],
            'bad': bad,
        }

    activate_at = int(time.time()) + 20
    pkg['activate_at'] = activate_at
    pkg['issued_at'] = int(time.time())
    ok, error = send_alfred_object(ALFRED_RADIO_TYPE, pkg)
    if not ok:
        cancel_radio_version(pkg['version'])
        return {'ok': False, 'error': f'Alfred activate failed: {error}'}

    return {
        'ok': True,
        'version': pkg['version'],
        'activate_at': activate_at,
        'acked': sorted(ack_state['acks'].keys()),
        'expected': expected,
        'targets': targets,
    }

def get_iw_info(iface):
    """Get current channel/freq/txpower for an interface."""
    info = {}
    try:
        r = subprocess.run(['iw', 'dev', iface, 'info'],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r'channel (\d+).*?(\d+) MHz', r.stdout)
        if m:
            info['channel'] = m.group(1)
            info['freq_mhz'] = m.group(2)
        m = re.search(r'txpower ([\d.]+) dBm', r.stdout)
        if m:
            info['txpower_dbm'] = m.group(1)
    except Exception:
        pass

    # HaLow (morse_usb): iw can report a regular Wi-Fi channel; Morse driver is the runtime source.
    if iface == 'wlan2':
        info.update(get_halow_driver_info(iface))

    return info

# ─────────────────────────────────────────────────────────────────────────────
# Topology
# ─────────────────────────────────────────────────────────────────────────────
def build_topology():
    nodes_raw = parse_registry()
    my_host   = get_my_hostname()
    my_ip     = get_my_ip()
    conf      = load_kv_file(MESH_CONF_FILE)

    # Get local interface state
    active_ifaces = get_bat0_active_ifaces()
    iw_wlan0 = get_iw_info('wlan0')
    iw_wlan1 = get_iw_info('wlan1')
    iw_wlan2 = get_iw_info('wlan2')

    nodes = []
    for nid, nd in nodes_raw.items():
        hostname = nd.get('HOSTNAME', 'unknown')
        ip       = nd.get('IPV4_ADDRESS', '')
        is_me    = (hostname == my_host)

        node_info = {
            'id':       nid,
            'hostname': hostname,
            'ip':       ip,
            'is_me':    is_me,
            'is_gateway': nd.get('IS_GATEWAY', 'false').lower() == 'true',
            'battery':  nd.get('BATTERY_PERCENTAGE', ''),
            'uptime':   nd.get('UPTIME_SECONDS', ''),
        }

        if is_me:
            node_info['interfaces'] = {
                'wlan0': {'active': 'wlan0' in active_ifaces, **iw_wlan0},
                'wlan1': {'active': 'wlan1' in active_ifaces, **iw_wlan1},
                'wlan2': {'active': 'wlan2' in active_ifaces, **iw_wlan2},
            }
        else:
            # Fetch from remote node's /api/local via peer proxy
            try:
                local = call_node_api(ip, '/api/local')
                ifaces_raw = local.get('interfaces', [])
                node_info['interfaces'] = {
                    i['name']: {
                        'active': i.get('health') == 'ok' and i.get('role') == 'mesh',
                        'channel': i.get('channel', ''),
                        'freq_mhz': i.get('freq_mhz', ''),
                        'halow_bw': i.get('halow_bw', ''),
                        'halow_source': i.get('halow_source', ''),
                    }
                    for i in ifaces_raw if i.get('name') in ('wlan0', 'wlan1', 'wlan2')
                }
            except Exception:
                node_info['interfaces'] = {}

        nodes.append(node_info)

    # Sort: self first
    nodes.sort(key=lambda n: (not n['is_me'], n['hostname']))

    return {
        'nodes':      nodes,
        'my_hostname': my_host,
        'my_ip':      my_ip,
        'internet':   has_internet(),
        'timestamp':  int(time.time()),
    }

# ─────────────────────────────────────────────────────────────────────────────
# Measurements
# ─────────────────────────────────────────────────────────────────────────────
def ensure_sessions_dir():
    os.makedirs(SESSIONS_DIR, exist_ok=True)

def list_sessions():
    ensure_sessions_dir()
    sessions = []
    for name in sorted(os.listdir(SESSIONS_DIR), reverse=True):
        d = os.path.join(SESSIONS_DIR, name)
        if os.path.isdir(d):
            files = [f for f in os.listdir(d) if f.endswith('.json')]
            results = get_session_results(name)
            sessions.append({
                'label': name,
                'tests': len(files),
                'summary': summarize_session_results(results),
            })
    return sessions

def get_session_results(label):
    d = os.path.join(SESSIONS_DIR, label)
    results = []
    if not os.path.isdir(d):
        return results
    for fname in sorted(os.listdir(d)):
        if fname.endswith('.json'):
            try:
                with open(os.path.join(d, fname)) as f:
                    results.append(json.load(f))
            except Exception:
                pass
    return results

def delete_session(label):
    safe_label = os.path.basename(label)
    if safe_label != label or not label:
        return False, 'Invalid session label'
    d = os.path.join(SESSIONS_DIR, safe_label)
    if not os.path.isdir(d):
        return False, 'Session not found'
    shutil.rmtree(d)
    return True, ''

def session_to_csv(label):
    results = get_session_results(label)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'timestamp', 'session_label', 'test_type',
        'src_node', 'dst_node',
        'active_interfaces', 'halow_channel', 'halow_bw',
        'tcp_mbps', 'udp_mbps', 'jitter_ms', 'loss_pct',
        'rtt_avg_ms', 'rtt_min_ms', 'rtt_max_ms',
    ])
    for r in results:
        iperf = r.get('iperf3_result', {})
        ping  = r.get('ping_result', {})
        # Extract iperf3 metrics
        tcp_mbps = udp_mbps = jitter = loss = None
        try:
            end = iperf.get('end', {})
            if 'sum_received' in end:
                tcp_mbps = round(end['sum_received']['bits_per_second'] / 1e6, 2)
            if 'sum' in end:
                s = end['sum']
                udp_mbps = round(s.get('bits_per_second', 0) / 1e6, 2)
                jitter   = round(s.get('jitter_ms', 0), 3)
                loss     = round(s.get('lost_percent', 0), 2)
        except Exception:
            pass
        writer.writerow([
            r.get('timestamp', ''),
            r.get('session_label', ''),
            r.get('test_type', ''),
            r.get('source_node', ''),
            r.get('destination_node', ''),
            ','.join(r.get('active_interfaces', [])),
            r.get('halow_channel', ''),
            r.get('halow_bw', ''),
            tcp_mbps or '',
            udp_mbps or '',
            jitter or '',
            loss or (ping.get('loss_pct', '') if ping else ''),
            ping.get('rtt_avg', '') if ping else '',
            ping.get('rtt_min', '') if ping else '',
            ping.get('rtt_max', '') if ping else '',
        ])
    return output.getvalue()

def _stats(values):
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return None
    return {
        'avg': round(sum(nums) / len(nums), 2),
        'min': round(min(nums), 2),
        'max': round(max(nums), 2),
        'n': len(nums),
    }

def summarize_session_results(results):
    metrics = {
        'tcp_mbps': [], 'udp_mbps': [], 'jitter_ms': [],
        'loss_pct': [], 'rtt_avg': [],
    }
    ok = fail = 0
    for record in results:
        if record.get('ok'):
            ok += 1
        else:
            fail += 1
        summary = summarize_measurement_result(record)
        for key in metrics:
            if summary.get(key) is not None:
                metrics[key].append(summary.get(key))
    return {
        'ok': ok,
        'fail': fail,
        'tcp_mbps': _stats(metrics['tcp_mbps']),
        'udp_mbps': _stats(metrics['udp_mbps']),
        'jitter_ms': _stats(metrics['jitter_ms']),
        'loss_pct': _stats(metrics['loss_pct']),
        'rtt_avg': _stats(metrics['rtt_avg']),
    }

def snapshot_topology():
    """Lightweight topology snapshot for embedding in test results."""
    active = get_bat0_active_ifaces()
    iw2    = get_iw_info('wlan2')
    iw0    = get_iw_info('wlan0')
    return {
        'active_interfaces': active,
        'halow_channel':  iw2.get('channel', ''),
        'halow_bw':       iw2.get('halow_bw', ''),
        'ch_2g':          iw0.get('channel', ''),
    }

def summarize_measurement_result(record):
    summary = {
        'test_type': record.get('test_type', ''),
        'src': record.get('source_node', ''),
        'dst': record.get('destination_node', ''),
        'ok': record.get('ok', False),
    }
    if record.get('error'):
        summary['error'] = record.get('error')

    iperf = record.get('iperf3_result') or {}
    ping = record.get('ping_result') or {}
    try:
        end = iperf.get('end', {})
        if 'sum_received' in end:
            summary['tcp_mbps'] = round(end['sum_received'].get('bits_per_second', 0) / 1e6, 2)
        if 'sum_sent' in end and 'tcp_mbps' not in summary:
            summary['tcp_mbps'] = round(end['sum_sent'].get('bits_per_second', 0) / 1e6, 2)
        if 'sum' in end:
            s = end['sum']
            if s.get('bits_per_second') is not None:
                summary['udp_mbps'] = round(s.get('bits_per_second', 0) / 1e6, 2)
            if s.get('jitter_ms') is not None:
                summary['jitter_ms'] = round(s.get('jitter_ms', 0), 3)
            if s.get('lost_percent') is not None:
                summary['loss_pct'] = round(s.get('lost_percent', 0), 2)
    except Exception:
        pass

    for key in ('rtt_avg', 'rtt_min', 'rtt_max', 'loss_pct'):
        if ping.get(key) is not None:
            summary[key] = ping.get(key)
    return summary

def run_measurement_session(label, pairs, tests, duration, udp_bitrate):
    """Run all test combinations. Blocking — call in thread."""
    global _measure_status
    done = 0
    try:
        ensure_sessions_dir()
        session_dir = os.path.join(SESSIONS_DIR, label)
        os.makedirs(session_dir, exist_ok=True)
        topo = snapshot_topology()

        total = len(pairs) * len(tests)

        for pair in pairs:
            src_ip   = pair['src_ip']
            dst_ip   = pair['dst_ip']
            src_name = pair['src_name']
            dst_name = pair['dst_name']

            for test_type in tests:
                now = int(time.time())
                with _measure_lock:
                    _measure_status.update({
                        'progress': f'{src_name}→{dst_name} {test_type} ({done+1}/{total})',
                        'done': done,
                        'total': total,
                        'current_started_at': now,
                        'current': {
                            'src': src_name,
                            'dst': dst_name,
                            'test_type': test_type,
                            'index': done + 1,
                            'total': total,
                        },
                    })

                ts    = datetime.now().strftime('%Y%m%dT%H%M%S')
                fname = f'{ts}_{src_name}_{dst_name}_{test_type}.json'
                result_record = {
                    'session_label':    label,
                    'timestamp':        datetime.now().isoformat(),
                    'test_type':        test_type,
                    'source_node':      src_name,
                    'destination_node': dst_name,
                    'active_interfaces': topo['active_interfaces'],
                    'halow_channel':    topo['halow_channel'],
                    'halow_bw':         topo['halow_bw'],
                    'ch_2g':            topo['ch_2g'],
                    'gps_source':       None,
                    'gps_destination':  None,
                }

                if test_type == 'icmp_ping':
                    # Run ping locally toward dst
                    resp = call_node_api(src_ip, '/api/ping/run', 'POST', {
                        'target': dst_ip, 'count': 100, 'interval': 0.2
                    }, timeout=40)
                    result_record['ping_result'] = resp.get('result')
                    result_record['ok'] = resp.get('ok', False)
                    if not result_record['ok']:
                        result_record['error'] = resp.get('error', 'ping failed')
                else:
                    # Start iperf3 server on dst, run client on src
                    server_resp = call_node_api(dst_ip, '/api/iperf/server/start', 'POST', {})
                    if not server_resp.get('ok'):
                        raise RuntimeError(f'{dst_name} iperf server failed: {server_resp.get("error")}')
                    time.sleep(1)

                    reverse = test_type == 'reverse'
                    parallel = 4 if test_type == 'tcp_4stream' else 1
                    try:
                        resp = call_node_api(src_ip, '/api/iperf/client/run', 'POST', {
                            'server_ip':  dst_ip,
                            'test_type':  test_type,
                            'duration':   duration,
                            'bitrate':    udp_bitrate,
                            'parallel':   parallel,
                            'reverse':    reverse,
                        }, timeout=duration + 25)
                        result_record['iperf3_result'] = resp.get('result')
                        result_record['ok'] = resp.get('ok', False)
                        if not result_record['ok']:
                            result_record['error'] = resp.get('error', 'iperf client failed')
                    finally:
                        call_node_api(dst_ip, '/api/iperf/server/stop', 'POST', {}, timeout=8)

                # Save result
                with open(os.path.join(session_dir, fname), 'w') as f:
                    json.dump(result_record, f, indent=2)

                done += 1
                with _measure_lock:
                    _measure_status.update({
                        'done': done,
                        'total': total,
                        'last_result': summarize_measurement_result(result_record),
                    })
                time.sleep(2)  # brief pause between tests

        with _measure_lock:
            _measure_status['running']  = False
            _measure_status['progress'] = f'Done — {done} tests saved'
            _measure_status['error']    = ''
            _measure_status['done']     = done
            _measure_status['total']    = total
            _measure_status['current']  = None
            _measure_status['current_started_at'] = None
    except Exception as e:
        with _measure_lock:
            _measure_status['running']  = False
            _measure_status['progress'] = f'Failed after {done} test(s)'
            _measure_status['error']    = str(e)
            _measure_status['done']     = done

# ─────────────────────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────────────────────
CSS = """
:root {
  --bg:      #ebeae8;
  --surface: #ffffff;
  --card:    #ffffff;
  --panel:   #f7f6f3;
  --border:  #d6d2cb;
  --border2: #e7e2da;
  --accent:  #00003f;
  --accent2: #ecb000;
  --fer-yellow:#ecb000;
  --fer-black:#02000d;
  --green:   #16a34a;
  --orange:  #d97706;
  --red:     #dc2626;
  --purple:  #00003f;
  --text:    #02000d;
  --muted:   #615f68;
  --shadow:  0 18px 50px rgba(2,0,13,.10);
  --font:    Roobert, Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
:root[data-theme="dark"] {
  --bg:      #02000d;
  --surface: #121118;
  --card:    #17151d;
  --panel:   #0b0a12;
  --border:  #34313b;
  --border2: #24212b;
  --accent:  #ecb000;
  --accent2: #ecb000;
  --text:    #f8f6ef;
  --muted:   #aaa5b2;
  --shadow:  0 18px 50px rgba(0,0,0,.36);
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:13px;min-height:100vh;max-width:100%;overflow-x:hidden}

body{font-feature-settings:"cv02","cv03","cv04","cv11"}
body::before{display:none}
body{
  background:
    linear-gradient(135deg, rgba(236,176,0,.22) 0 9px, transparent 9px 72px),
    linear-gradient(160deg, transparent 0 60%, rgba(0,0,63,.08) 60% 74%, transparent 74%),
    var(--bg);
}

/* header */
#hdr{background:rgba(255,255,255,.92);backdrop-filter:blur(18px);border-bottom:1px solid var(--border2);padding:0 22px;min-height:58px;display:flex;align-items:center;gap:18px;position:sticky;top:0;z-index:100;box-shadow:0 1px 0 rgba(2,0,13,.05);transition:min-height .18s ease,padding .18s ease,gap .18s ease}
#hdr::after{content:'';position:absolute;left:0;right:0;bottom:0;height:3px;background:linear-gradient(90deg,var(--fer-yellow) 0 33%,transparent 33%);pointer-events:none}
:root[data-theme="dark"] #hdr{background:rgba(18,17,24,.92)}
.fer-lockup{display:flex;align-items:center;height:34px;min-width:82px;padding-right:14px;border-right:1px solid var(--border)}
.fer-logo-img{display:block;width:82px;max-height:22px;object-fit:contain;transition:width .18s ease,max-height .18s ease}
:root[data-theme="dark"] .fer-logo-img{filter:invert(1) brightness(1.18)}
.fer-logo-fallback{display:none;align-items:center;height:28px;padding:0 10px;background:var(--fer-yellow);color:var(--fer-black);border-radius:4px;font-size:15px;font-weight:900}
#hdr-logo{color:var(--text);font-size:17px;letter-spacing:0;font-weight:900}
#hdr-logo span{color:var(--accent2)}
#hdr-node{font-size:12px;color:var(--muted);border-left:1px solid var(--border);padding-left:16px;transition:opacity .18s ease,max-width .18s ease,padding .18s ease,border .18s ease}
#hdr-node strong{color:var(--text)}
#hdr-right{margin-left:auto;display:flex;align-items:center;gap:16px;font-size:11px}
#hdr-inet{padding:5px 10px;border-radius:999px;letter-spacing:0;font-size:11px;font-weight:700}
#hdr-inet.ok{color:var(--text);border:1px solid rgba(22,163,74,.34);background:rgba(22,163,74,.07)}
#hdr-inet.no{color:var(--text);border:1px solid rgba(217,119,6,.34);background:rgba(217,119,6,.07)}
#hdr-clock{color:var(--muted);font-size:11px}
.theme-toggle{border:1px solid var(--accent2);background:rgba(236,176,0,.10);color:var(--text);border-radius:999px;padding:6px 10px;font-family:var(--font);font-size:11px;font-weight:850;cursor:pointer;min-width:74px}
.theme-toggle:hover{background:var(--accent2);color:var(--fer-black);box-shadow:0 8px 22px rgba(236,176,0,.20)}

/* sidebar nav */
#page{display:flex;min-height:calc(100vh - 58px)}
#nav{background:var(--surface);border-right:1px solid var(--border2);width:160px;flex:0 0 160px;display:flex;flex-direction:column;padding:12px 0;position:sticky;top:58px;height:calc(100vh - 58px);overflow-y:auto;z-index:90;transition:top .18s ease}
.tab{padding:11px 20px;cursor:pointer;font-size:12px;font-weight:700;letter-spacing:0;color:var(--muted);border-left:3px solid transparent;text-transform:none;transition:all .15s;white-space:nowrap}
.tab:hover{color:var(--text);background:var(--panel)}
.tab.active{color:var(--accent);border-left-color:var(--accent);background:var(--panel)}
.tab.active::after{display:none}

/* layout */
#content{padding:22px;max-width:1120px;width:100%;flex:1;min-width:0;position:relative}
#content::before{content:'';display:block;height:44px;margin:-22px -22px 18px;background:
  linear-gradient(104deg,transparent 0 36%,rgba(236,176,0,.42) 36% 41%,transparent 41%);
  border-bottom:1px solid var(--border2);transition:height .18s ease,margin .18s ease,opacity .18s ease}
:root[data-theme="dark"] #content::before{background:
  linear-gradient(104deg,transparent 0 36%,rgba(236,176,0,.46) 36% 41%,transparent 41%)}
body.chrome-compact #hdr{min-height:46px;gap:14px}
body.chrome-compact .fer-logo-img{width:70px;max-height:18px}
body.chrome-compact #hdr-node{opacity:0;max-width:0;padding-left:0;border-left:0;overflow:hidden;white-space:nowrap}
body.chrome-compact #nav{top:46px;height:calc(100vh - 46px)}
body.chrome-compact #content::before{height:8px;margin:-22px -22px 12px;opacity:.72}

/* card */
.card{background:var(--card);border:1px solid var(--border2);border-radius:8px;margin-bottom:16px;position:relative;overflow:hidden;box-shadow:var(--shadow)}
.card::before{display:none}
.card-title{padding:14px 16px;font-size:12px;font-weight:800;letter-spacing:0;color:var(--text);text-transform:none;border-bottom:1px solid var(--border2);display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}
.card-title::before{display:none}

/* rows */
.row{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--border2);flex-wrap:wrap}
.row:last-child{border-bottom:none}
.row-label{flex:0 0 150px;font-size:11px;color:var(--muted);letter-spacing:.5px;text-transform:uppercase}

/* inputs */
input[type=text],input[type=number],select{
  background:var(--surface);border:1px solid var(--border);color:var(--text);
  padding:6px 10px;font-family:var(--font);font-size:12px;
  outline:none;transition:border .15s,box-shadow .15s;min-width:150px;max-width:100%;border-radius:8px;
}
input:focus,select:focus{border-color:var(--accent2);box-shadow:0 0 0 3px rgba(236,176,0,.18)}
select option{background:var(--surface)}
input[type=checkbox]{width:15px;height:15px;accent-color:var(--accent)}

/* buttons */
.btn{padding:8px 15px;background:var(--surface);color:var(--accent);border:1px solid var(--accent);border-radius:8px;font-family:var(--font);font-size:12px;font-weight:850;cursor:pointer;letter-spacing:0;text-transform:none;transition:all .15s;position:relative;overflow:hidden}
.btn::before{display:none}
.btn:hover{background:var(--accent);color:#ffffff;border-color:var(--accent);box-shadow:0 8px 22px rgba(2,0,13,.14);transform:translateY(-1px)}
.btn:disabled{opacity:.45;cursor:not-allowed;box-shadow:none;transform:none}
.btn-green{background:var(--accent2);color:var(--fer-black);border-color:var(--accent2)}
.btn-green:hover{background:var(--fer-black);color:#ffffff;border-color:var(--fer-black);box-shadow:0 10px 26px rgba(236,176,0,.18)}
.btn-red{color:#b42318;border-color:#e9b2ad;background:transparent}
.btn-red:hover{background:#b42318;color:#ffffff;border-color:#b42318;box-shadow:0 8px 22px rgba(180,35,24,.14)}
.btn-run{padding:13px 30px;font-size:13px;color:#ffffff;border-color:var(--accent);background:var(--accent);letter-spacing:0}
.btn-run:hover{background:var(--accent2);color:var(--fer-black);border-color:var(--accent2);box-shadow:0 12px 28px rgba(236,176,0,.24)}
:root[data-theme="dark"] .btn{background:transparent;color:var(--accent2);border-color:var(--accent2)}
:root[data-theme="dark"] .btn:hover{background:var(--accent2);color:var(--fer-black);border-color:var(--accent2)}
:root[data-theme="dark"] .btn-green,:root[data-theme="dark"] .btn-run{background:var(--accent2);color:var(--fer-black);border-color:var(--accent2)}
:root[data-theme="dark"] .btn-green:hover,:root[data-theme="dark"] .btn-run:hover{background:#f8f6ef;color:var(--fer-black);border-color:#f8f6ef}
:root[data-theme="dark"] .btn-red{background:transparent;color:#fca5a5;border-color:#7f1d1d}
:root[data-theme="dark"] .btn-red:hover{background:#b42318;color:#ffffff;border-color:#b42318}

/* badges */
.badge{padding:3px 8px;font-size:10px;font-weight:750;letter-spacing:0;border:1px solid;border-radius:999px}
.b-on {color:var(--text); border-color:rgba(22,163,74,.34);background:rgba(22,163,74,.07)}
.b-off{color:var(--muted); border-color:var(--border);background:#f7f8fa}
.b-gw {color:#8a4b07;border-color:#f3d29a;background:#fff7ed}
.b-me {color:#3b2e7e;border-color:#d7cdfa;background:#f5f3ff}

/* table */
table{width:100%;border-collapse:collapse}
thead tr{border-bottom:1px solid var(--border)}
th{padding:8px 12px;font-size:9px;letter-spacing:2px;color:var(--muted);text-transform:uppercase;text-align:left}
td{padding:10px 12px;border-bottom:1px solid var(--border2);font-size:12px;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(236,176,0,.08)}

/* node grid */
.node-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(220px,100%),1fr));gap:12px;padding:16px}
.node-card{background:var(--card);border:1px solid var(--border2);border-radius:8px;padding:14px;position:relative;transition:border .15s,box-shadow .15s}
.node-card:hover{border-color:var(--border);box-shadow:0 10px 26px rgba(18,24,38,.07)}
.node-card.is-me{border-color:#d7cdfa}
.node-card.is-gw{border-color:#f3d29a}
.node-name{font-size:14px;font-weight:700;margin-bottom:4px;color:var(--text)}
.node-ip{font-size:10px;color:var(--muted);margin-bottom:8px}
.node-ifaces{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px}
.iface-chip{font-size:9px;font-weight:750;padding:3px 7px;border:1px solid;border-radius:999px}
.iface-on {color:var(--text);border-color:rgba(22,163,74,.34);background:rgba(22,163,74,.07)}
.iface-off{color:var(--muted);border-color:var(--border);background:#f7f8fa}
.node-battery{font-size:10px;color:var(--muted)}
.node-tags{position:absolute;top:10px;right:10px;display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end;max-width:45%}

/* interface toggle cards */
.iface-block{padding:12px 16px;border-bottom:1px solid var(--border2)}
.iface-block:last-child{border-bottom:none}
.iface-header{display:flex;align-items:center;gap:12px;margin-bottom:6px;flex-wrap:wrap}
.iface-name{font-size:13px;font-weight:700;min-width:60px}
.iface-band{font-size:10px;color:var(--muted);letter-spacing:.5px}
.iface-controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.txpwr-row{display:flex;align-items:center;gap:8px;margin-top:4px;padding-left:72px;font-size:11px;color:var(--muted);flex-wrap:wrap}

/* global actions bar */
.global-bar{display:flex;gap:8px;flex-wrap:wrap;padding:12px 16px}

/* pairs */
.pairs-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(260px,100%),1fr));gap:6px;padding:12px 16px}
.pair-item,.pair-row{display:flex;align-items:center;gap:8px;padding:8px 10px;background:var(--card);border:1px solid var(--border2);border-radius:8px;cursor:pointer;transition:border .15s,box-shadow .15s;min-width:0}
.pair-item:hover,.pair-row:hover{border-color:var(--border)}
.pair-item input,.pair-row input{flex-shrink:0}
.pair-label{font-size:12px;flex:1}
.pair-arrow{color:var(--accent);margin:0 4px}

/* tests */
.tests-wrap{display:flex;flex-wrap:wrap;gap:8px;padding:12px 16px}
.test-chip{display:flex;align-items:center;gap:6px;padding:8px 12px;background:var(--card);border:1px solid var(--border2);border-radius:8px;cursor:pointer;font-size:11px;font-weight:700;transition:all .15s;letter-spacing:0}
.test-chip:has(input:checked){border-color:var(--accent2);color:var(--accent);background:rgba(236,176,0,.12)}
.test-chip:hover{border-color:var(--border)}

/* progress */
.progress-wrap{padding:12px 16px}
.progress-label{font-size:11px;color:var(--muted);margin-bottom:6px;letter-spacing:.5px}
.progress-bar-bg{height:6px;background:var(--border2);position:relative;border-radius:999px;overflow:hidden}
.progress-bar-fill{height:6px;background:var(--accent2);transition:width .3s;width:0;border-radius:999px}
.progress-bar-fill.running{animation:progressPulse 1.2s ease-in-out infinite}
@keyframes progressPulse{0%{width:15%;opacity:.55}50%{width:80%;opacity:1}100%{width:15%;opacity:.55}}
.progress-text{font-size:12px;color:var(--accent);margin-top:6px;min-height:18px}
.progress-text.done{color:var(--green)}
.progress-text.err{color:var(--red)}
.progress-stats{margin-top:10px;display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:6px}
.stat-chip{border:1px solid var(--border2);background:var(--card);border-radius:8px;padding:8px 10px;font-size:10px;color:var(--muted);min-height:38px}
.stat-chip strong{display:block;color:var(--text);font-size:12px;margin-top:2px;overflow-wrap:anywhere}
.stat-chip.good strong{color:var(--green)}
.stat-chip.warn strong{color:var(--orange)}
.stat-chip.bad strong{color:var(--red)}

/* msg */
#msg{padding:10px 16px;font-size:12px;display:none;margin-bottom:16px;border-left:3px solid;letter-spacing:.3px}
#msg.ok  {border-color:var(--green);color:#136c36;background:#ecfdf3;display:block}
#msg.err {border-color:var(--red);  color:#b42318;background:#fff5f5;display:block}
#msg.info{border-color:var(--accent);color:var(--accent);background:#eff6ff;display:block}

/* foreground overlay */
#overlay{position:fixed;left:50%;top:18px;transform:translate(-50%,-130%);z-index:10000;width:min(520px,calc(100vw - 24px));background:var(--surface);border:1px solid var(--accent);border-radius:8px;box-shadow:0 18px 60px rgba(2,0,13,.22);opacity:0;transition:transform .18s ease,opacity .18s ease;pointer-events:none}
#overlay.show{transform:translate(-50%,0);opacity:1;pointer-events:auto}
#overlay.ok{border-color:#b8e6c8}
#overlay.err{border-color:#f3b6b1}
#overlay.info{border-color:var(--accent)}
.overlay-body{display:flex;align-items:flex-start;gap:10px;padding:12px 14px}
#overlay-text{flex:1;font-size:12px;line-height:1.35;overflow-wrap:anywhere}
#overlay-close{background:transparent;border:0;color:var(--muted);font-family:var(--font);font-size:18px;line-height:1;cursor:pointer;padding:0 2px}
#overlay.ok #overlay-text{color:var(--green)}
#overlay.err #overlay-text{color:var(--red)}
#overlay.info #overlay-text{color:var(--accent)}

/* session list */
.session-row{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--border2);flex-wrap:wrap}
.session-row:last-child{border-bottom:none}
.session-label{flex:1;font-size:13px}
.session-count{font-size:11px;color:var(--muted)}
.session-actions{display:flex;gap:6px}
.session-summary{width:100%;display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:6px;margin-top:4px}
.metric-mini{background:var(--card);border:1px solid var(--border2);border-radius:8px;padding:6px 8px;font-size:10px;color:var(--muted)}
.metric-mini strong{display:block;color:var(--text);font-size:11px;margin-top:2px}

/* upload */
.upload-card{padding:16px;display:flex;align-items:center;gap:16px;border-bottom:1px solid var(--border2);flex-wrap:wrap}
.upload-card:last-child{border-bottom:none}
.upload-info{flex:1}
.upload-title{font-size:13px;margin-bottom:2px}
.upload-sub{font-size:10px;color:var(--muted);letter-spacing:.3px}

@media (max-width: 620px) {
  html,body{font-size:12px}
  #hdr{position:relative;padding:10px 12px;gap:8px;align-items:flex-start;flex-wrap:wrap}
  .fer-lockup{order:1;min-width:66px;height:28px;padding-right:8px}
  .fer-logo-img{width:66px;max-height:18px}
  #hdr-logo{order:2;font-size:16px;letter-spacing:0;flex:1;min-width:140px}
  #hdr-node{order:3;flex:0 0 100%;border-left:0;padding-left:0;font-size:10px;max-width:100%;overflow-wrap:anywhere}
  #hdr-right{order:4;flex:0 0 100%;margin-left:0;width:100%;justify-content:flex-start;gap:8px;flex-wrap:wrap}
  #hdr-clock{font-size:10px}
  #hdr-inet{font-size:9px;padding:3px 8px}
  .theme-toggle{padding:5px 8px;min-width:66px;font-size:10px}
  #page{flex-direction:column}
  #nav{width:100%;flex:0 0 auto;display:grid;grid-template-columns:repeat(3,1fr);position:static;height:auto;border-right:none;border-bottom:1px solid var(--border2);padding:0;overflow:visible;top:auto}
  .tab{padding:9px 4px;font-size:10px;white-space:nowrap;text-align:center;border-left:none;border-bottom:3px solid transparent;box-sizing:border-box;overflow:hidden;text-overflow:ellipsis}
  .tab.active{border-left-color:transparent;border-bottom-color:var(--accent)}
  #content{padding:10px}
  .card{margin-bottom:10px}
  .card-title{padding:9px 10px;letter-spacing:1px}
  .row{padding:10px;gap:8px;align-items:stretch}
  .row-label{flex:0 0 100%;font-size:10px}
  input[type=text],input[type=number],select{width:100%!important;min-width:0}
  .btn{width:100%;justify-content:center;text-align:center;padding:9px 10px}
  .btn-run{padding:12px 10px;font-size:12px;letter-spacing:1px}
  .global-bar{padding:10px;display:grid;grid-template-columns:1fr 1fr;gap:8px}
  .global-bar .btn{width:100%;font-size:10px;letter-spacing:.5px}
  .node-grid{padding:10px;grid-template-columns:1fr}
  .node-card{padding:12px}
  .node-tags{position:static;max-width:none;margin-bottom:6px;justify-content:flex-start}
  .iface-block{padding:10px}
  .iface-header{gap:8px}
  .iface-name{min-width:52px}
  .iface-band{flex:1;min-width:90px}
  .iface-controls{width:100%;display:grid;grid-template-columns:1fr}
  .txpwr-row{padding-left:0;display:grid;grid-template-columns:auto 72px auto 1fr;align-items:center}
  .txpwr-row input{width:72px!important}
  .txpwr-row .btn{width:auto}
  .pairs-grid{padding:10px;grid-template-columns:1fr}
  .pair-row label{min-width:0;overflow-wrap:anywhere}
  .tests-wrap{padding:10px;display:grid;grid-template-columns:1fr;gap:6px}
  .test-chip{width:100%;min-width:0}
  .session-row{padding:10px;align-items:flex-start}
  .session-label,.session-count,.session-actions{width:100%}
  .progress-stats,.session-summary{grid-template-columns:1fr}
  .session-actions{display:grid;grid-template-columns:1fr 1fr}
  .upload-card{padding:12px;align-items:flex-start}
  .upload-info,.upload-card .btn{width:100%}
  .upload-sub{overflow-wrap:anywhere}
  table{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch}
  body.chrome-compact #hdr{gap:6px;padding:6px 12px}
  body.chrome-compact #hdr-node{display:none}
  body.chrome-compact #nav{top:0}
  body.chrome-compact #content::before{height:6px;margin:-10px -10px 10px}
  #overlay{top:10px;width:calc(100vw - 16px)}
}
"""

JS = """
let _topo = null;
let _tab  = 'topology';
let _msgTimer = null;
let _pollTimer = null;
let _overlayTimer = null;
let _autoRefreshTimer = null;
let _autoRefreshBusy = false;
const VALID_TABS = ['topology','interfaces','radio','measure','sessions','upload'];
const AUTO_REFRESH_MS = 15000;
const THEME_KEY = 'manetUiTheme';

function preferredTheme() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === 'dark' || saved === 'light') return saved;
  } catch(e) {}
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = theme === 'dark' ? 'Light' : 'Dark';
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem(THEME_KEY, next); } catch(e) {}
  setTheme(next);
}

setTheme(preferredTheme());

async function fetchTopo() {
  try {
    const r = await fetch('/api/topology');
    _topo = await r.json();
    renderTopology();
    updatePairs();
  } catch(e) { showMsg('Topology fetch failed: ' + e, 'err'); }
}

function userIsEditing() {
  const el = document.activeElement;
  if (!el) return false;
  return ['INPUT','SELECT','TEXTAREA'].includes(el.tagName);
}

async function autoRefreshUi() {
  if (_autoRefreshBusy || document.hidden || userIsEditing()) return;
  _autoRefreshBusy = true;
  try {
    await fetchTopo();
    if (_tab === 'interfaces') buildIfaceControl();
    if (_tab === 'sessions') await loadSessions();
  } finally {
    _autoRefreshBusy = false;
  }
}

function startAutoRefresh() {
  clearInterval(_autoRefreshTimer);
  _autoRefreshTimer = setInterval(autoRefreshUi, AUTO_REFRESH_MS);
}

function updateChromeCompact() {
  if (window.innerWidth <= 620) { document.body.classList.remove('chrome-compact'); return; }
  document.body.classList.toggle('chrome-compact', window.scrollY > 24);
}

function showMsg(txt, cls) {
  const el = document.getElementById('msg');
  if (!el) return;
  clearTimeout(_msgTimer);
  el.textContent = txt;
  el.className = cls;
  el.style.display = 'block';
  if (cls !== 'info') {
    _msgTimer = setTimeout(() => { el.style.display = 'none'; }, 8000);
  }
  if (cls === 'err' || cls === 'ok') showOverlay(txt, cls);
}

function showOverlay(txt, cls) {
  const box = document.getElementById('overlay');
  const text = document.getElementById('overlay-text');
  if (!box || !text) return;
  clearTimeout(_overlayTimer);
  text.textContent = txt;
  box.className = (cls || 'info') + ' show';
  _overlayTimer = setTimeout(hideOverlay, cls === 'err' ? 12000 : 5500);
}

function hideOverlay() {
  const box = document.getElementById('overlay');
  if (box) box.className = box.className.replace(' show', '');
}

function setRunButton(running, label) {
  const btn = document.getElementById('btn-run');
  if (!btn) return;
  btn.disabled = !!running;
  btn.textContent = label || (running ? 'RUNNING...' : '▶ RUN MEASUREMENTS');
}

function setButtonBusy(id, busy, label, idleLabel) {
  const btn = document.getElementById(id);
  if (!btn) return;
  btn.disabled = !!busy;
  btn.textContent = busy ? label : idleLabel;
}

function radioTargetNodes(nodeIp) {
  if (!_topo || !_topo.nodes) return [];
  if (nodeIp === 'all') return _topo.nodes;
  return _topo.nodes.filter(n => n.ip === nodeIp);
}

function radioStateOk(nodes, iface, state) {
  const expectedActive = state === 'up';
  const bad = [];
  for (const node of nodes) {
    const info = node.interfaces && node.interfaces[iface];
    const active = !!(info && info.active === true);
    if (active !== expectedActive) bad.push(node.hostname || node.ip || 'unknown');
  }
  return {ok: bad.length === 0, bad};
}

function confirmRadioDown(nodeIp, iface) {
  if (nodeIp !== 'all') {
    const node = radioTargetNodes(nodeIp)[0];
    const label = node ? `${node.hostname} (${node.ip})` : nodeIp;
    return confirm(
      `Disable ${iface} on ${label}?\\n\\n` +
      `This will stop the wpa_supplicant service for that radio.`
    );
  }
  const nodes = radioTargetNodes('all');
  const nodeList = nodes.map(n => n.hostname || n.ip).join(', ') || 'all nodes';
  return confirm(
    `Disable ${iface} on ALL nodes?\\n\\n` +
    `Targets: ${nodeList}\\n\\n` +
    `The change will be staged through Alfred, all nodes must ACK, then the wpa_supplicant service for ${iface} will be stopped.`
  );
}

async function verifyRadioExecution(nodeIp, iface, state, activateAt) {
  const delayMs = Math.max(0, ((activateAt || 0) - Math.floor(Date.now() / 1000) + 3) * 1000);
  await new Promise(resolve => setTimeout(resolve, delayMs));

  showOverlay(`Verifying ${iface} ${state} after coordinated apply...`, 'info');
  showMsg(`Verifying ${iface} ${state} after coordinated apply...`, 'info');

  let lastBad = [];
  for (let attempt = 0; attempt < 15; attempt++) {
    await fetchTopo();
    const nodes = radioTargetNodes(nodeIp);
    const result = radioStateOk(nodes, iface, state);
    if (nodes.length && result.ok) {
      buildIfaceControl();
      const scope = nodeIp === 'all' ? 'all nodes' : (nodes[0].hostname || nodeIp);
      showOverlay(`${iface} ${state} executed on ${scope}`, 'ok');
      showMsg(`${iface} ${state} executed on ${scope}`, 'ok');
      return true;
    }
    lastBad = result.bad;
    await new Promise(resolve => setTimeout(resolve, 3000));
  }

  buildIfaceControl();
  const msg = `${iface} ${state} was scheduled, but execution is not confirmed` +
              (lastBad.length ? ` on: ${lastBad.join(', ')}` : '');
  showOverlay(msg, 'err');
  showMsg(msg, 'err');
  return false;
}

function setProgress(text, state) {
  const card = document.getElementById('progress-card');
  const txt  = document.getElementById('progress-text');
  const fill = document.getElementById('progress-fill');
  if (!card || !txt || !fill) return;
  card.style.display = '';
  txt.textContent = text || '';
  txt.className = 'progress-text' + (state === 'err' ? ' err' : state === 'done' ? ' done' : '');
  fill.className = 'progress-bar-fill' + (state === 'running' ? ' running' : '');
  fill.style.background = state === 'err' ? 'var(--red)' : state === 'done' ? 'var(--green)' : 'var(--accent)';
  fill.style.width = state === 'running' ? '60%' : state ? '100%' : '0';
}

function fmtSecs(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m}m ${s}s` : `${s}s`;
}

function metricText(stats, unit) {
  if (!stats || stats.avg == null) return '';
  return `avg ${stats.avg}${unit} / min ${stats.min}${unit} / max ${stats.max}${unit}`;
}

function lastResultText(r) {
  if (!r) return 'waiting for first completed test';
  const parts = [];
  if (r.tcp_mbps != null) parts.push(`TCP ${r.tcp_mbps} Mbps`);
  if (r.udp_mbps != null) parts.push(`UDP ${r.udp_mbps} Mbps`);
  if (r.rtt_avg != null) parts.push(`RTT ${r.rtt_avg} ms`);
  if (r.jitter_ms != null) parts.push(`jitter ${r.jitter_ms} ms`);
  if (r.loss_pct != null) parts.push(`loss ${r.loss_pct}%`);
  if (r.error) parts.push(r.error);
  const metrics = parts.length ? parts.join(' · ') : (r.ok ? 'ok' : 'failed');
  return `${r.src} -> ${r.dst} ${r.test_type}: ${metrics}`;
}

function renderMeasureStats(d) {
  const el = document.getElementById('progress-stats');
  if (!el) return;
  const now = Math.floor(Date.now() / 1000);
  const done = d.done || 0;
  const total = d.total || 0;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const elapsed = d.started_at ? fmtSecs(now - d.started_at) : '0s';
  const curElapsed = d.current_started_at ? fmtSecs(now - d.current_started_at) : '-';
  const cur = d.current ? `${d.current.src} -> ${d.current.dst} ${d.current.test_type}` : '-';
  el.innerHTML = `
    <div class="stat-chip"><span>completed</span><strong>${done}/${total} (${pct}%)</strong></div>
    <div class="stat-chip"><span>elapsed</span><strong>${elapsed}</strong></div>
    <div class="stat-chip"><span>current</span><strong>${cur}</strong></div>
    <div class="stat-chip"><span>current time</span><strong>${curElapsed}</strong></div>
    <div class="stat-chip ${d.last_result && d.last_result.ok ? 'good' : ''}" style="grid-column:1/-1"><span>last result</span><strong>${lastResultText(d.last_result)}</strong></div>`;
}

function getInitialTab() {
  const hashTab = window.location.hash ? window.location.hash.substring(1) : '';
  if (VALID_TABS.includes(hashTab)) return hashTab;
  try {
    const saved = localStorage.getItem('perfDashboardTab');
    if (VALID_TABS.includes(saved)) return saved;
  } catch(e) {}
  return 'topology';
}

function showTab(name, updateUrl = true) {
  if (!VALID_TABS.includes(name)) name = 'topology';
  _tab = name;
  try { localStorage.setItem('perfDashboardTab', name); } catch(e) {}
  if (updateUrl && window.location.hash !== '#' + name) {
    history.replaceState(null, '', '#' + name);
  }
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-pane').forEach(p => p.style.display = p.id === 'tab-' + name ? '' : 'none');
  if (name === 'sessions') loadSessions();
}

// ── Clock ──
function tickClock() {
  const el = document.getElementById('hdr-clock');
  if (el) el.textContent = new Date().toLocaleString('hr-HR', {hour12:false}).replace(',','');
}
setInterval(tickClock, 1000); tickClock();

// ── Topology tab ──
function renderTopology() {
  if (!_topo) return;
  const grid = document.getElementById('node-grid');
  grid.innerHTML = '';
  for (const node of _topo.nodes) {
    const ifaces = node.interfaces || {};
    const chips = ['wlan0','wlan1','wlan2'].map(i => {
      const info = ifaces[i] || {};
      const on = info.active === true;
      const band = i === 'wlan0' ? '2.4G' : i === 'wlan1' ? '5G' : 'HaLow';
      const ch = info.channel ? ` ch${info.channel}` : (info.freq_mhz ? ` ${Math.round(info.freq_mhz)}MHz` : '');
      return `<span class="iface-chip ${on ? 'iface-on' : 'iface-off'}">${band}${on ? ch : ' OFF'}</span>`;
    }).join('');
    const bat = node.battery ? `<div class="node-battery">BAT ${node.battery}%</div>` : '';
    const tags = [
      node.is_me ? '<span class="badge b-me" style="font-size:9px">ME</span>' : '',
      node.is_gateway ? '<span class="badge b-gw" style="font-size:9px">GW</span>' : '',
    ].filter(Boolean).join('');
    const cls = [node.is_me ? 'is-me' : '', node.is_gateway ? 'is-gw' : ''].join(' ');
    grid.innerHTML += `<div class="node-card ${cls}">
      <div class="node-tags">${tags}</div>
      <div class="node-name">${node.hostname}</div>
      <div class="node-ip">${node.ip}</div>
      <div class="node-ifaces">${chips}</div>
      ${bat}
    </div>`;
  }
  // Internet + upload buttons
  const inet = document.getElementById('hdr-inet');
  if (_topo.internet) { inet.textContent = '● INET OK'; inet.className = 'ok'; }
  else { inet.textContent = '○ NO INET'; inet.className = 'no'; }
  document.getElementById('upload-github').disabled = !_topo.internet;
  document.getElementById('upload-ventum').disabled = !_topo.internet;
}

// ── Interface control tab ──
function buildIfaceControl() {
  if (!_topo) return;
  const wrap = document.getElementById('iface-cards');
  wrap.innerHTML = '';
  for (const node of _topo.nodes) {
    const ifaces = node.interfaces || {};
    const card = document.createElement('div');
    card.className = 'card';
    const BANDS = {wlan0: '2.4 GHz', wlan1: '5 GHz', wlan2: 'HaLow 900 MHz'};
    let html = `<div class="card-title">${node.hostname} &nbsp;<span style="color:var(--muted);font-size:10px">${node.ip}${node.is_me ? ' &bull; THIS NODE' : ''}</span></div>`;
    for (const iface of ['wlan0','wlan1','wlan2']) {
      const info = ifaces[iface] || {};
      const on = info.active === true;
      html += `<div class="iface-block">
        <div class="iface-header">
          <span class="iface-name">${iface}</span>
          <span class="iface-band">${BANDS[iface]}</span>
          <span class="badge ${on ? 'b-on' : 'b-off'}" id="ibadge-${node.id}-${iface}">${on ? 'ACTIVE' : 'DOWN'}</span>
          <div class="iface-controls">
            ${on
              ? `<button class="btn btn-red" onclick="toggleIface('${node.ip}','${node.id}','${iface}','down')">DISABLE</button>`
              : `<button class="btn btn-green" onclick="toggleIface('${node.ip}','${node.id}','${iface}','up')">ENABLE</button>`
            }
          </div>
        </div>
        <div class="txpwr-row">
          TX POWER
          <input type="number" id="txpwr-${node.id}-${iface}" value="${info.txpower_dbm || 20}" min="0" max="30" style="width:65px">
          dBm
          <button class="btn" style="padding:4px 10px;font-size:10px" onclick="setTxPower('${node.ip}','${node.id}','${iface}')">SET</button>
        </div>
      </div>`;
    }
    card.innerHTML = html;
    wrap.appendChild(card);
  }
}

async function toggleIface(nodeIp, nodeId, iface, state) {
  if (state === 'down' && !confirmRadioDown(nodeIp, iface)) return;
  showOverlay(`Coordinating ${iface} ${state} on ${nodeIp} through Alfred...`, 'info');
  showMsg(`Staging ${iface} ${state}; waiting for mesh ACKs...`, 'info');
  try {
    const r = await fetch('/api/interface/toggle', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({node_ip: nodeIp, iface, state})
    });
    const d = await r.json();
    if (d.ok) {
      const wait = d.activate_at ? Math.max(0, d.activate_at - Math.floor(Date.now() / 1000)) : 0;
      showOverlay(`${iface} ${state} ACKed by ${d.acked?.length || 0}/${d.expected?.length || 0} nodes. Applying in ${wait}s...`, 'ok');
      showMsg(`${iface} ${state} scheduled through Alfred`, 'ok');
      verifyRadioExecution(nodeIp, iface, state, d.activate_at);
    } else {
      showOverlay(`Radio change failed: ${d.error}`, 'err');
      showMsg('Error: ' + d.error, 'err');
    }
  } catch(e) {
    showOverlay(`Radio change failed: ${e.message}`, 'err');
    showMsg('Error: ' + e.message, 'err');
  }
}

async function setTxPower(nodeIp, nodeId, iface) {
  const dbm = document.getElementById(`txpwr-${nodeId}-${iface}`).value;
  const r = await fetch('/api/txpower', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({node_ip: nodeIp, iface, dbm: parseFloat(dbm)})
  });
  const d = await r.json();
  if (d.ok) showMsg(`TX power set to ${dbm}dBm on ${iface}@${nodeIp}`, 'ok');
  else showMsg('Error: ' + d.error, 'err');
}

async function toggleAll(iface, state) {
  if (state === 'down' && !confirmRadioDown('all', iface)) return;
  showOverlay(`Coordinating ${iface} ${state} on all nodes through Alfred...`, 'info');
  showMsg(`Staging ${iface} ${state}; waiting for all mesh ACKs...`, 'info');
  try {
    const r = await fetch('/api/interface/toggle', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({node_ip: 'all', iface, state})
    });
    const d = await r.json();
    if (d.ok) {
      const wait = d.activate_at ? Math.max(0, d.activate_at - Math.floor(Date.now() / 1000)) : 0;
      showOverlay(`${iface} ${state} ACKed by ${d.acked?.length || 0}/${d.expected?.length || 0} nodes. Applying in ${wait}s...`, 'ok');
      showMsg(`${iface} ${state} scheduled on all nodes through Alfred`, 'ok');
      verifyRadioExecution('all', iface, state, d.activate_at);
    } else {
      showOverlay(`Radio change failed: ${d.error}`, 'err');
      showMsg('Error: ' + d.error, 'err');
    }
  } catch(e) {
    showOverlay(`Radio change failed: ${e.message}`, 'err');
    showMsg('Error: ' + e.message, 'err');
  }
}

// ── HaLow config tab (HTML is static in template) ──
function buildHalowConfig() {}

async function applyHalow() {
  const ch = document.getElementById('halow-ch').value;
  const bw = document.getElementById('halow-bw').value;
  setButtonBusy('btn-apply-halow', true, 'APPLYING...', 'APPLY TO ALL NODES');
  showOverlay(`Applying HaLow ch${ch} / ${bw} — verifying all nodes...`, 'info');
  try {
    const r = await fetch('/api/halow/channel', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({channel: parseInt(ch), bw})
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    if (!d.ok) {
      const msg = d.rolled_back
        ? `ROLLED BACK — ${d.error}` + (d.unreachable?.length ? ` · not in mesh: ${d.unreachable.join(', ')}` : '')
        : d.error;
      showMsg(msg, 'err');
      return;
    }
    let msg = `HaLow ch${ch} / ${bw} applied to: ${d.applied?.join(', ')}`;
    if (d.unreachable?.length) msg += ` · WARNING: not in mesh: ${d.unreachable.join(', ')}`;
    showMsg(msg, d.unreachable?.length ? 'info' : 'ok');
    await fetchTopo();
  } catch (e) {
    showMsg('HaLow apply failed: ' + e.message, 'err');
  } finally {
    setButtonBusy('btn-apply-halow', false, '', 'APPLY TO ALL NODES');
  }
}

async function apply2G() {
  const ch = document.getElementById('ch-2g').value;
  setButtonBusy('btn-apply-2g', true, 'APPLYING...', 'APPLY TO ALL NODES');
  showOverlay(`Applying 2.4G channel ${ch} to all nodes...`, 'info');
  showMsg(`Applying 2.4G channel ${ch} to all nodes...`, 'info');
  try {
    const r = await fetch('/api/wifi/channel', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({interface: 'wlan0', channel: parseInt(ch)})
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
    showMsg('2.4G channel applied to all nodes', 'ok');
    await fetchTopo();
  } catch (e) {
    showMsg('2.4G channel failed: ' + e.message, 'err');
  } finally {
    setButtonBusy('btn-apply-2g', false, '', 'APPLY TO ALL NODES');
  }
}

async function apply5G() {
  const ch = document.getElementById('ch-5g').value;
  setButtonBusy('btn-apply-5g', true, 'APPLYING...', 'APPLY TO ALL NODES');
  showOverlay(`Applying 5G channel ${ch} to all nodes...`, 'info');
  try {
    const r = await fetch('/api/wifi/channel', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({interface: 'wlan1', channel: parseInt(ch)})
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
    showMsg('5G channel applied to all nodes', 'ok');
    await fetchTopo();
  } catch (e) {
    showMsg('5G channel failed: ' + e.message, 'err');
  } finally {
    setButtonBusy('btn-apply-5g', false, '', 'APPLY TO ALL NODES');
  }
}

// ── Measurements tab ──
function updatePairs() {
  if (!_topo) return;
  const wrap = document.getElementById('pairs-grid');
  wrap.innerHTML = '';
  const nodes = _topo.nodes;
  for (let i = 0; i < nodes.length; i++) {
    for (let j = 0; j < nodes.length; j++) {
      if (i === j) continue;
      const src = nodes[i], dst = nodes[j];
      const key = `${src.id}-${dst.id}`;
      wrap.innerHTML += `
        <div class="pair-row">
          <input type="checkbox" id="pair-${key}" value="${key}" data-src="${src.ip}" data-dst="${dst.ip}" data-src-name="${src.hostname}" data-dst-name="${dst.hostname}">
          <label for="pair-${key}" style="flex:none;font-size:12px">${src.hostname} → ${dst.hostname}</label>
        </div>`;
    }
  }
}

async function startMeasurement() {
  const label = document.getElementById('session-label').value.trim();
  if (!label) {
    showMsg('Enter a session label', 'err');
    setProgress('Missing session label.', 'err');
    return;
  }

  const pairs = [];
  document.querySelectorAll('#pairs-grid input:checked').forEach(el => {
    pairs.push({
      src_ip: el.dataset.src, dst_ip: el.dataset.dst,
      src_name: el.dataset.srcName || el.dataset.src,
      dst_name: el.dataset.dstName || el.dataset.dst,
    });
  });
  if (!pairs.length) {
    showMsg('Select at least one test pair', 'err');
    setProgress('No source → destination pair selected.', 'err');
    return;
  }

  const tests = [];
  document.querySelectorAll('#tests-grid input:checked').forEach(el => tests.push(el.value));
  if (!tests.length) {
    showMsg('Select at least one test type', 'err');
    setProgress('No test type selected.', 'err');
    return;
  }

  const duration   = parseInt(document.getElementById('duration').value) || 30;
  const udpBitrate = document.getElementById('udp-bitrate').value || '4M';

  clearTimeout(_pollTimer);
  setRunButton(true, 'STARTING...');
  setProgress('Starting measurement session...', 'running');
  showMsg('Starting measurement session...', 'info');

  try {
    const r = await fetch('/api/measure/start', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({label, pairs, tests, duration, udp_bitrate: udpBitrate})
    });
    const d = await r.json();
    if (!r.ok || !d.ok) {
      throw new Error(d.error || `HTTP ${r.status}`);
    }
    setRunButton(true, 'RUNNING...');
    setProgress('Running... waiting for first status update.', 'running');
    showMsg('Measurement is running...', 'info');
    pollStatus();
  } catch (e) {
    setRunButton(false);
    setProgress('Start failed: ' + e.message, 'err');
    showMsg('Start failed: ' + e.message, 'err');
  }
}

async function pollStatus() {
  try {
    const r = await fetch('/api/measure/status');
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);

    if (d.running) {
      setRunButton(true, 'RUNNING...');
      setProgress(d.progress || 'Running...', 'running');
      renderMeasureStats(d);
      _pollTimer = setTimeout(pollStatus, 2000);
    } else if (d.error) {
      setRunButton(false);
      setProgress('Error: ' + d.error, 'err');
      renderMeasureStats(d);
      showMsg('Error: ' + d.error, 'err');
    } else {
      setRunButton(false);
      setProgress(d.progress || 'Measurement complete — results saved.', 'done');
      renderMeasureStats(d);
      showMsg('Measurement complete — results saved.', 'ok');
      loadSessions();
    }
  } catch (e) {
    setRunButton(false);
    setProgress('Status failed: ' + e.message, 'err');
    showMsg('Status failed: ' + e.message, 'err');
  }
}

// ── Sessions tab ──
async function loadSessions() {
  const r = await fetch('/api/sessions');
  const d = await r.json();
  const list = document.getElementById('sessions-list');
  if (!d.length) {
    list.innerHTML = '<div style="padding:16px;color:var(--muted);font-size:11px">No sessions recorded yet.</div>';
    return;
  }
  list.innerHTML = d.map(s => `
    <div class="session-row">
      <div class="session-label">${s.label}</div>
      <div class="session-count">${s.tests} test${s.tests !== 1 ? 's' : ''} · ${s.summary?.ok || 0} ok / ${s.summary?.fail || 0} fail</div>
      <div class="session-actions">
        <a href="/api/sessions/${encodeURIComponent(s.label)}/csv" download="${s.label}.csv"
           class="btn" style="text-decoration:none;font-size:10px">CSV</a>
        <a href="/api/sessions/${encodeURIComponent(s.label)}" target="_blank"
           class="btn" style="text-decoration:none;font-size:10px">JSON</a>
        <button class="btn btn-red" style="font-size:10px" onclick="deleteSession('${encodeURIComponent(s.label)}')">DELETE</button>
      </div>
      <div class="session-summary">
        ${sessionMetric('TCP', s.summary?.tcp_mbps, ' Mbps')}
        ${sessionMetric('UDP', s.summary?.udp_mbps, ' Mbps')}
        ${sessionMetric('RTT', s.summary?.rtt_avg, ' ms')}
        ${sessionMetric('JITTER', s.summary?.jitter_ms, ' ms')}
        ${sessionMetric('LOSS', s.summary?.loss_pct, '%')}
      </div>
    </div>`).join('');
}

function sessionMetric(label, stats, unit) {
  const txt = metricText(stats, unit);
  return `<div class="metric-mini">${label}<strong>${txt || '-'}</strong></div>`;
}

async function deleteSession(encodedLabel) {
  const label = decodeURIComponent(encodedLabel);
  if (!confirm(`Delete measurement session "${label}"? This cannot be undone.`)) return;
  try {
    const r = await fetch(`/api/sessions/${encodedLabel}`, {method: 'DELETE'});
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
    showMsg(`Deleted session ${label}`, 'ok');
    await loadSessions();
  } catch (e) {
    showMsg('Delete failed: ' + e.message, 'err');
  }
}

async function uploadGithub() {
  document.getElementById('upload-github').disabled = true;
  const r = await fetch('/api/upload/github', {method:'POST'});
  const d = await r.json();
  document.getElementById('upload-github').disabled = !_topo?.internet;
  if (d.ok) showMsg('Uploaded to GitHub', 'ok');
  else showMsg('GitHub upload failed: ' + d.error, 'err');
}

async function uploadVentum() {
  document.getElementById('upload-ventum').disabled = true;
  const r = await fetch('/api/upload/ventum', {method:'POST'});
  const d = await r.json();
  document.getElementById('upload-ventum').disabled = !_topo?.internet;
  if (d.ok) showMsg('Uploaded to Ventum', 'ok');
  else showMsg('Ventum upload failed: ' + d.error, 'err');
}

window.onload = async () => {
  updateChromeCompact();
  showTab(getInitialTab());
  await fetchTopo();
  buildIfaceControl();
  buildHalowConfig();
  startAutoRefresh();
};

window.addEventListener('hashchange', () => showTab(getInitialTab(), false));
window.addEventListener('scroll', updateChromeCompact, {passive: true});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) autoRefreshUi();
});
"""

def render_dashboard():
    hostname = get_my_hostname()
    ip       = get_my_ip()
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MANET // PERF</title>
<style>{CSS}</style>
</head><body>

<div id="overlay" class="info">
  <div class="overlay-body">
    <div id="overlay-text"></div>
    <button id="overlay-close" onclick="hideOverlay()" aria-label="Close">&times;</button>
  </div>
</div>

<div id="hdr">
  <div class="fer-lockup" title="FER">
    <img class="fer-logo-img" src="https://www.fer.unizg.hr/_pub/themes_static/fer_2025/default/img/FERlogo.svg" alt="FER" onerror="this.style.display='none';this.nextElementSibling.style.display='inline-flex'">
    <span class="fer-logo-fallback">FER</span>
  </div>
  <div id="hdr-logo">MANET//<span>PERF</span></div>
  <div id="hdr-node"><strong>{hostname}</strong> &nbsp;{ip}</div>
  <div id="hdr-right">
    <span id="hdr-inet" class="no">○ NO INET</span>
    <span id="hdr-clock"></span>
    <button id="theme-toggle" class="theme-toggle" type="button" onclick="toggleTheme()">Dark</button>
  </div>
</div>

<div id="page">
<div id="nav">
  <div class="tab active" data-tab="topology"   onclick="showTab('topology')">TOPOLOGY</div>
  <div class="tab"        data-tab="interfaces" onclick="showTab('interfaces')">INTERFACES</div>
  <div class="tab"        data-tab="radio"      onclick="showTab('radio')">RADIO CONFIG</div>
  <div class="tab"        data-tab="measure"    onclick="showTab('measure')">MEASURE</div>
  <div class="tab"        data-tab="sessions"   onclick="showTab('sessions')">SESSIONS</div>
  <div class="tab"        data-tab="upload"     onclick="showTab('upload')">UPLOAD</div>
</div>

<div id="content">
  <div id="msg"></div>

  <!-- ── TOPOLOGY ── -->
  <div id="tab-topology" class="tab-pane">
    <div class="card">
      <div class="card-title">Mesh Nodes</div>
      <div class="node-grid" id="node-grid">
        <div style="padding:20px;color:var(--muted);font-size:11px">Loading topology...</div>
      </div>
    </div>
  </div>

  <!-- ── INTERFACES ── -->
  <div id="tab-interfaces" class="tab-pane" style="display:none">
    <div class="card">
      <div class="card-title">Global Actions</div>
      <div class="global-bar" id="global-bar">
        <button class="btn btn-green" onclick="toggleAll('wlan0','up')">↑ 2.4G ALL</button>
        <button class="btn btn-red"   onclick="toggleAll('wlan0','down')">↓ 2.4G ALL</button>
        <button class="btn btn-green" onclick="toggleAll('wlan1','up')">↑ 5G ALL</button>
        <button class="btn btn-red"   onclick="toggleAll('wlan1','down')">↓ 5G ALL</button>
        <button class="btn btn-green" onclick="toggleAll('wlan2','up')">↑ HALOW ALL</button>
        <button class="btn btn-red"   onclick="toggleAll('wlan2','down')">↓ HALOW ALL</button>
      </div>
    </div>
    <div id="iface-cards"></div>
  </div>

  <!-- ── RADIO CONFIG ── -->
  <div id="tab-radio" class="tab-pane" style="display:none">
    <div class="card">
      <div class="card-title">HaLow 900 MHz</div>
      <div class="row">
        <span class="row-label">Channel</span>
        <select id="halow-ch">
          <option value="1">ch 1 (863.5 MHz)</option>
          <option value="2">ch 2 (864.5 MHz)</option>
          <option value="3">ch 3 (865.5 MHz)</option>
          <option value="4">ch 4 (866.5 MHz)</option>
          <option value="5" selected>ch 5 (867.5 MHz)</option>
          <option value="6">ch 6 (868.5 MHz)</option>
        </select>
      </div>
      <div class="row">
        <span class="row-label">Bandwidth</span>
        <select id="halow-bw">
          <option value="1MHz" selected>1 MHz</option>
          <option value="2MHz">2 MHz</option>
          <option value="4MHz">4 MHz</option>
        </select>
      </div>
      <div class="row">
        <span class="row-label"></span>
        <button class="btn btn-green" id="btn-apply-halow" onclick="applyHalow()">APPLY TO ALL NODES</button>
      </div>
    </div>
    <div class="card">
      <div class="card-title">2.4 GHz</div>
      <div class="row">
        <span class="row-label">Channel</span>
        <select id="ch-2g">
          ${''.join(f'<option value="{c}"{" selected" if c==6 else ""}>ch {c} ({2407+c*5} MHz)</option>' for c in range(1,14))}
        </select>
      </div>
      <div class="row">
        <span class="row-label"></span>
        <button class="btn btn-green" id="btn-apply-2g" onclick="apply2G()">APPLY TO ALL NODES</button>
      </div>
    </div>
    <div class="card">
      <div class="card-title">5 GHz</div>
      <div class="row">
        <span class="row-label">Channel</span>
        <select id="ch-5g">
          <option value="36">ch 36 (5180 MHz)</option>
          <option value="40">ch 40 (5200 MHz)</option>
          <option value="44">ch 44 (5220 MHz)</option>
          <option value="48">ch 48 (5240 MHz)</option>
          <option value="52">ch 52 (5260 MHz)</option>
          <option value="56">ch 56 (5280 MHz)</option>
          <option value="60">ch 60 (5300 MHz)</option>
          <option value="64">ch 64 (5320 MHz)</option>
          <option value="100">ch 100 (5500 MHz)</option>
          <option value="104">ch 104 (5520 MHz)</option>
          <option value="108">ch 108 (5540 MHz)</option>
          <option value="112">ch 112 (5560 MHz)</option>
          <option value="116">ch 116 (5580 MHz)</option>
          <option value="120">ch 120 (5600 MHz)</option>
          <option value="124">ch 124 (5620 MHz)</option>
          <option value="128">ch 128 (5640 MHz)</option>
          <option value="132">ch 132 (5660 MHz)</option>
          <option value="136">ch 136 (5680 MHz)</option>
          <option value="140">ch 140 (5700 MHz)</option>
          <option value="149">ch 149 (5745 MHz)</option>
          <option value="153">ch 153 (5765 MHz)</option>
          <option value="157">ch 157 (5785 MHz)</option>
          <option value="161">ch 161 (5805 MHz)</option>
          <option value="165">ch 165 (5825 MHz)</option>
        </select>
      </div>
      <div class="row">
        <span class="row-label"></span>
        <button class="btn btn-green" id="btn-apply-5g" onclick="apply5G()">APPLY TO ALL NODES</button>
      </div>
    </div>
  </div>

  <!-- ── MEASURE ── -->
  <div id="tab-measure" class="tab-pane" style="display:none">
    <div class="card">
      <div class="card-title">Session Label</div>
      <div class="row">
        <span class="row-label">Label</span>
        <input type="text" id="session-label" placeholder="e.g. outdoor-50m  /  line-of-sight-100m" style="width:320px">
      </div>
    </div>
    <div class="card">
      <div class="card-title">Test Pairs &nbsp;<span style="color:var(--muted);font-size:10px">Select source → destination</span></div>
      <div class="pairs-grid" id="pairs-grid">
        <div style="padding:12px;color:var(--muted);font-size:11px">Loading nodes...</div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Test Types</div>
      <div class="tests-wrap" id="tests-grid">
        <label class="test-chip"><input type="checkbox" value="tcp_1stream"    checked> TCP 1-STREAM</label>
        <label class="test-chip"><input type="checkbox" value="tcp_4stream"          > TCP 4-STREAM</label>
        <label class="test-chip"><input type="checkbox" value="udp_throughput" checked> UDP THROUGHPUT</label>
        <label class="test-chip"><input type="checkbox" value="udp_jitter"           > UDP JITTER</label>
        <label class="test-chip"><input type="checkbox" value="packet_loss"          > PACKET LOSS</label>
        <label class="test-chip"><input type="checkbox" value="reverse"              > REVERSE</label>
        <label class="test-chip"><input type="checkbox" value="icmp_ping"      checked> ICMP PING</label>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Parameters</div>
      <div class="row">
        <span class="row-label">Duration</span>
        <input type="number" id="duration" value="30" min="5" max="300" style="width:80px">
        <span style="font-size:11px;color:var(--muted)">seconds</span>
      </div>
      <div class="row">
        <span class="row-label">UDP Bitrate</span>
        <select id="udp-bitrate">
          <option value="1M">1 Mbps</option>
          <option value="2M">2 Mbps</option>
          <option value="4M" selected>4 Mbps &nbsp;[HaLow typical max]</option>
          <option value="10M">10 Mbps</option>
          <option value="50M">50 Mbps</option>
          <option value="100M">100 Mbps</option>
        </select>
      </div>
    </div>
    <div style="padding:16px 0">
      <button class="btn btn-run" id="btn-run" onclick="startMeasurement()">&#9654; RUN MEASUREMENTS</button>
    </div>
    <div class="card" id="progress-card" style="display:none">
      <div class="card-title">Progress</div>
      <div class="progress-wrap">
        <div class="progress-bar-bg"><div class="progress-bar-fill" id="progress-fill"></div></div>
        <div class="progress-text" id="progress-text"></div>
        <div class="progress-stats" id="progress-stats"></div>
      </div>
    </div>
  </div>

  <!-- ── SESSIONS ── -->
  <div id="tab-sessions" class="tab-pane" style="display:none">
    <div class="card">
      <div class="card-title">Saved Sessions</div>
      <div id="sessions-list">
        <div style="padding:16px;color:var(--muted);font-size:11px">Loading...</div>
      </div>
    </div>
  </div>

  <!-- ── UPLOAD ── -->
  <div id="tab-upload" class="tab-pane" style="display:none">
    <div class="card">
      <div class="card-title">Upload Results</div>
      <div class="upload-card">
        <div class="upload-info">
          <div class="upload-title">GitHub</div>
          <div class="upload-sub">mrleongalaxyum/manet-dev &rarr; measurements/ &nbsp;&bull;&nbsp; git commit + push</div>
        </div>
        <button class="btn btn-green" id="upload-github" onclick="uploadGithub()" disabled>PUSH</button>
      </div>
      <div class="upload-card">
        <div class="upload-info">
          <div class="upload-title">Ventum</div>
          <div class="upload-sub">curl -u upload to manet.ventum.hr/upload/rpi5/measurements</div>
        </div>
        <button class="btn btn-green" id="upload-ventum" onclick="uploadVentum()" disabled>UPLOAD</button>
      </div>
      <div style="padding:12px 16px;font-size:10px;color:var(--muted);letter-spacing:.5px">
        UPLOAD BUTTONS ENABLED ONLY WHEN INTERNET IS AVAILABLE
      </div>
    </div>
  </div>
</div><!-- #content -->
</div><!-- #page -->

<script>{JS}</script>
</body></html>"""

# ─────────────────────────────────────────────────────────────────────────────
# Request Handler
# ─────────────────────────────────────────────────────────────────────────────
class PerfHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress access logs

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/') or '/'

        if path in ('/', '/index.html'):
            self.send_html(render_dashboard())

        elif path == '/api/topology':
            try:
                self.send_json(build_topology())
            except Exception as e:
                self.send_json({'error': str(e)}, 500)

        elif path == '/api/measure/status':
            with _measure_lock:
                self.send_json(dict(_measure_status))

        elif path == '/api/sessions':
            self.send_json(list_sessions())

        elif path.startswith('/api/sessions/'):
            parts = path[len('/api/sessions/'):].split('/')
            label = parts[0]
            if len(parts) > 1 and parts[1] == 'csv':
                csv_data = session_to_csv(label).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/csv')
                self.send_header('Content-Disposition', f'attachment; filename="{label}.csv"')
                self.send_header('Content-Length', str(len(csv_data)))
                self.end_headers()
                self.wfile.write(csv_data)
            else:
                self.send_json(get_session_results(label))

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found')

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/') or '/'

        if path.startswith('/api/sessions/'):
            label = unquote(path[len('/api/sessions/'):].split('/')[0])
            ok, error = delete_session(label)
            self.send_json({'ok': ok, 'error': error})
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found')

    def do_POST(self):
        global _measure_status
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/') or '/'
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length) if length else b'{}'

        if path == '/api/interface/toggle':
            try:
                req      = json.loads(body)
                node_ip  = req.get('node_ip', '')
                iface    = req.get('iface', '')
                state    = req.get('state', '')
                self.send_json(coordinate_radio_toggle(node_ip, iface, state))
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/txpower':
            try:
                req     = json.loads(body)
                node_ip = req.get('node_ip', '')
                iface   = req.get('iface', '')
                dbm     = req.get('dbm')
                r = call_node_api(node_ip, '/api/control/txpower', 'POST',
                                  {'iface': iface, 'dbm': dbm})
                self.send_json(r)
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/halow/channel':
            try:
                req       = json.loads(body)
                new_ch    = int(req.get('channel', 0))
                new_bw    = req.get('bw', '1MHz')
                if not new_ch:
                    self.send_json({'ok': False, 'error': 'Missing channel'})
                    return

                nodes_raw = parse_registry()
                # Step 1: probe reachable nodes via mesh IP and snapshot current channel
                # Nodes not reachable through mesh will timeout — no ethernet fallback
                reachable = []   # {hostname, ip, old_ch, old_bw}
                unreachable = [] # hostnames
                for nd in nodes_raw.values():
                    ip       = nd.get('IPV4_ADDRESS', '')
                    hostname = nd.get('HOSTNAME', ip)
                    if not ip:
                        continue
                    local = call_node_api(ip, '/api/local', timeout=4)
                    if 'error' in local and local.get('ok') is False:
                        unreachable.append(hostname)
                        continue
                    ifaces = {i['name']: i for i in local.get('interfaces', []) if 'name' in i}
                    w2 = ifaces.get('wlan2', {})
                    reachable.append({
                        'hostname': hostname,
                        'ip':       ip,
                        'old_ch':   w2.get('channel', ''),
                        'old_bw':   w2.get('halow_bw', ''),
                    })

                if not reachable:
                    self.send_json({'ok': False, 'error': 'No reachable nodes'})
                    return

                # Step 2: apply new channel to all reachable nodes
                failed = []
                applied = []
                for node in reachable:
                    r = call_node_api(node['ip'], '/api/control/halow_channel', 'POST', req)
                    if r.get('ok'):
                        applied.append(node)
                    else:
                        failed.append({'hostname': node['hostname'], 'error': r.get('error', 'failed')})

                # Step 3: verify via morse_cli (re-read; retry up to 3x with 2s spacing)
                verify_failed = []
                for node in applied:
                    confirmed = False
                    for _ in range(3):
                        time.sleep(2)
                        local = call_node_api(node['ip'], '/api/local', timeout=5)
                        ifaces = {i['name']: i for i in local.get('interfaces', []) if 'name' in i}
                        actual_ch = ifaces.get('wlan2', {}).get('channel', '')
                        if str(actual_ch) == str(new_ch):
                            confirmed = True
                            break
                    if not confirmed:
                        verify_failed.append(node['hostname'])

                # Step 4: rollback if any verification failed
                if verify_failed or failed:
                    for node in applied:
                        if node['old_ch']:
                            call_node_api(node['ip'], '/api/control/halow_channel', 'POST', {
                                'channel': node['old_ch'], 'bw': node['old_bw'] or '1MHz'
                            })
                    err_parts = []
                    if failed:
                        err_parts.append('apply failed: ' + ', '.join(f['hostname'] for f in failed))
                    if verify_failed:
                        err_parts.append('verify failed: ' + ', '.join(verify_failed))
                    self.send_json({'ok': False, 'error': '; '.join(err_parts),
                                    'rolled_back': True,
                                    'unreachable': unreachable})
                    return

                result = {'ok': True, 'applied': [n['hostname'] for n in applied]}
                if unreachable:
                    result['warning'] = 'Unreachable (not in mesh): ' + ', '.join(unreachable)
                    result['unreachable'] = unreachable
                self.send_json(result)
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/wifi/channel':
            try:
                req = json.loads(body)
                iface = req.get('interface', req.get('iface', ''))
                channel = req.get('channel')
                if iface not in ('wlan0', 'wlan1'):
                    self.send_json({'ok': False, 'error': 'Invalid Wi-Fi interface'})
                    return
                nodes_raw = parse_registry()
                errors = []
                for nd in nodes_raw.values():
                    ip = nd.get('IPV4_ADDRESS', '')
                    if not ip:
                        continue
                    r = call_node_api(ip, '/api/control/wifi_channel', 'POST',
                                      {'interface': iface, 'channel': channel})
                    if not r.get('ok'):
                        errors.append(f"{ip}: {r.get('error')}")
                if errors:
                    self.send_json({'ok': False, 'error': '; '.join(errors)})
                else:
                    self.send_json({'ok': True})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/measure/start':
            with _measure_lock:
                if _measure_status['running']:
                    self.send_json({'ok': False, 'error': 'Measurement already running'})
                    return
                try:
                    req    = json.loads(body)
                    label  = req.get('label', '').strip()
                    pairs  = req.get('pairs', [])
                    tests  = req.get('tests', [])
                    dur    = int(req.get('duration', 30))
                    bitrate = req.get('udp_bitrate', '4M')
                    if not label or not pairs or not tests:
                        self.send_json({'ok': False, 'error': 'Missing label, pairs, or tests'})
                        return
                    _measure_status['running']  = True
                    _measure_status['label']    = label
                    _measure_status['progress'] = 'Starting...'
                    _measure_status['error']    = ''
                    _measure_status['done']     = 0
                    _measure_status['total']    = len(pairs) * len(tests)
                    _measure_status['started_at'] = int(time.time())
                    _measure_status['current_started_at'] = None
                    _measure_status['current'] = None
                    _measure_status['last_result'] = None
                    t = threading.Thread(
                        target=run_measurement_session,
                        args=(label, pairs, tests, dur, bitrate),
                        daemon=True
                    )
                    t.start()
                    self.send_json({'ok': True})
                except Exception as e:
                    _measure_status['running'] = False
                    self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/upload/github':
            try:
                repo_dir = '/home/radio/manet-dev'
                meas_src = SESSIONS_DIR
                meas_dst = os.path.join(repo_dir, 'measurements')
                subprocess.run(['rsync', '-a', meas_src + '/', meas_dst + '/'],
                               check=True, timeout=30)
                subprocess.run(['git', '-C', repo_dir, 'add', 'measurements/'],
                               check=True, timeout=10)
                ts = datetime.now().strftime('%Y-%m-%d %H:%M')
                subprocess.run(['git', '-C', repo_dir, 'commit', '-m',
                                f'measurements: add results {ts}'],
                               check=True, timeout=10)
                subprocess.run(['git', '-C', repo_dir, 'push'],
                               check=True, timeout=30)
                self.send_json({'ok': True})
            except subprocess.CalledProcessError as e:
                self.send_json({'ok': False, 'error': str(e)})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/upload/ventum':
            try:
                conf = load_kv_file('/etc/mesh.conf')
                ventum_url = conf.get('ventum_upload_url', 'https://manet.ventum.hr/upload/rpi5/measurements')
                ventum_auth = conf.get('ventum_auth', '')
                if not ventum_auth:
                    user = conf.get('ventum_user', 'clanker')
                    password = conf.get('ventum_password', 'really-strong-password-321')
                    ventum_auth = f'{user}:{password}'

                ts = datetime.now().strftime('%Y%m%dT%H%M%S')
                host = get_my_hostname()
                archive = f'/tmp/manet-measurements-{host}-{ts}.tar.gz'
                remote_name = os.path.basename(archive)
                upload_url = ventum_url.rstrip('/') + '/' + remote_name

                subprocess.run(
                    ['tar', '-C', os.path.dirname(SESSIONS_DIR),
                     '-czf', archive, os.path.basename(SESSIONS_DIR)],
                    check=True, timeout=60
                )
                try:
                    subprocess.run(
                        ['curl', '-fS', '-u', ventum_auth, '-T', archive, upload_url],
                        check=True, timeout=120
                    )
                finally:
                    try:
                        os.remove(archive)
                    except Exception:
                        pass
                self.send_json({'ok': True, 'file': remote_name, 'url': upload_url})
            except subprocess.CalledProcessError as e:
                self.send_json({'ok': False, 'error': str(e)})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found')

# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────
class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

AVAHI_PERF_SERVICE = '/etc/avahi/services/perf-http.service'
AVAHI_PERF_CONTENT = """<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>MANET Perf Dashboard</name>
  <host-name>perf.local</host-name>
  <service>
    <type>_http._tcp</type>
    <port>8081</port>
  </service>
</service-group>
"""

def _is_gateway():
    return os.path.exists('/var/run/mesh-gateway.state')

def _manage_avahi_perf():
    """Run in background thread: install/remove avahi perf.local based on gateway status."""
    last = None
    while True:
        gw = _is_gateway()
        if gw != last:
            try:
                if gw:
                    with open(AVAHI_PERF_SERVICE, 'w') as f:
                        f.write(AVAHI_PERF_CONTENT)
                    subprocess.run(['systemctl', 'reload', 'avahi-daemon'], timeout=5, capture_output=True)
                else:
                    if os.path.exists(AVAHI_PERF_SERVICE):
                        os.remove(AVAHI_PERF_SERVICE)
                        subprocess.run(['systemctl', 'reload', 'avahi-daemon'], timeout=5, capture_output=True)
            except Exception:
                pass
            last = gw
        time.sleep(30)

if __name__ == '__main__':
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    os.makedirs(SESSIONS_DIR, exist_ok=True)

    t = threading.Thread(target=_manage_avahi_perf, daemon=True)
    t.start()

    server = ThreadedServer(('0.0.0.0', port), PerfHandler)
    print(f'MANET Perf Dashboard listening on port {port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
