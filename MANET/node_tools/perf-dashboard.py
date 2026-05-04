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
  GET  /api/upload/status       - Current upload status
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
FER_LOGO_FULL_FILE = '/usr/local/share/manet/fer-logo.svg'
FER_LOGO_BLACK_FILE = '/usr/local/share/manet/fer-logo-black.svg'
FER_LOGO_WHITE_FILE = '/usr/local/share/manet/fer-logo-white.svg'

# EU S1G channels (centre frequencies in MHz)
HALOW_EU_CHANNELS = [863500, 864500, 865500, 866500, 867500, 868500]
HALOW_BW_OPTIONS  = ['1MHz', '2MHz', '4MHz']
# Empirical HaLow TX-power ceilings verified on mesh-f86f (2026-04-22)
# by applying channel/BW changes on the live node and reading back /api/local.
HALOW_BW_TXPOWER_CAP_DBM = {'1MHz': '24', '2MHz': '24', '4MHz': '22'}

# Active measurement state
_measure_lock   = threading.Lock()
_measure_status = {
    'running': False, 'label': '', 'progress': '', 'error': '',
    'done': 0, 'total': 0, 'started_at': None, 'current_started_at': None,
    'current': None, 'last_result': None,
}

_upload_lock = threading.Lock()
_upload_status = {
    'running': False,
    'target': '',
    'phase': '',
    'progress': '',
    'bytes_sent': 0,
    'bytes_total': 0,
    'percent': 0,
    'started_at': None,
    'finished_at': None,
    'done': False,
    'error': '',
    'file': '',
    'url': '',
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

def get_local_battery_percentage():
    try:
        with open('/run/battery_status.json') as f:
            data = json.load(f)
        pct = data.get('percentage')
        if pct is not None:
            return str(pct)
    except Exception:
        pass
    return ''


def get_local_uptime():
    try:
        with open('/proc/uptime') as f:
            secs = float(f.read().split()[0])
        return fmt_uptime(secs)
    except Exception:
        return ''


def reset_upload_status():
    with _upload_lock:
        _upload_status.update({
            'running': False,
            'target': '',
            'phase': '',
            'progress': '',
            'bytes_sent': 0,
            'bytes_total': 0,
            'percent': 0,
            'started_at': None,
            'finished_at': None,
            'done': False,
            'error': '',
            'file': '',
            'url': '',
        })


def update_upload_status(**kwargs):
    with _upload_lock:
        _upload_status.update(kwargs)


def start_upload_status(target):
    now = int(time.time())
    with _upload_lock:
        if _upload_status.get('running'):
            raise RuntimeError('Upload already running')
        _upload_status.update({
            'running': True,
            'target': target,
            'phase': 'starting',
            'progress': f'Starting {target} upload...',
            'bytes_sent': 0,
            'bytes_total': 0,
            'percent': 1,
            'started_at': now,
            'finished_at': None,
            'done': False,
            'error': '',
            'file': '',
            'url': '',
        })


def finish_upload_status(ok=True, error='', **extra):
    now = int(time.time())
    with _upload_lock:
        _upload_status.update(extra)
        _upload_status['running'] = False
        _upload_status['done'] = bool(ok)
        _upload_status['error'] = error or ''
        _upload_status['finished_at'] = now
        _upload_status['percent'] = 100 if ok else _upload_status.get('percent', 0)
        if ok:
            _upload_status['phase'] = 'done'
            _upload_status['progress'] = _upload_status.get('progress') or 'Upload complete'
        else:
            _upload_status['phase'] = 'error'
            _upload_status['progress'] = error or _upload_status.get('progress') or 'Upload failed'


def get_upload_status():
    with _upload_lock:
        return dict(_upload_status)

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

def fmt_uptime(seconds):
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ''
    if s < 60:
        return f'{s}s'
    m = s // 60
    if m < 60:
        return f'{m}m'
    h = m // 60
    rm = m % 60
    if h < 24:
        return f'{h}h{rm:02d}m'
    d = h // 24
    rh = h % 24
    return f'{d}d{rh:02d}h'


def _gps_dict(node):
    g = node.get('gps') or {}
    return {
        'available': bool(g.get('available')),
        'lat': g.get('lat', ''),
        'lon': g.get('lon', ''),
        'alt': g.get('alt', ''),
    }

def get_session_info(src_ip, dst_ip):
    """Return (hop_count, hop_source, gps_src, gps_dst) from one /api/data call on src."""
    _null_gps = {'available': False, 'lat': '', 'lon': '', 'alt': ''}
    if not src_ip or not dst_ip:
        return None, 'missing', _null_gps, _null_gps
    try:
        data = call_node_api(src_ip, '/api/data', timeout=5)
    except Exception:
        return None, 'error', _null_gps, _null_gps
    if not isinstance(data, dict) or data.get('error'):
        return None, 'error', _null_gps, _null_gps

    gps_src = _null_gps
    gps_dst = _null_gps
    hop_count = None
    hop_source = 'unknown'

    for node in data.get('nodes', []):
        if node.get('is_me'):
            gps_src = _gps_dict(node)
        if node.get('ip') == dst_ip:
            gps_dst = _gps_dict(node)
            hc = node.get('hop_count')
            if isinstance(hc, int) and hc >= 1:
                hop_count = hc
                hop_source = 'batctl'

    return hop_count, hop_source, gps_src, gps_dst


def extract_iperf3_metrics(iperf):
    metrics = {
        'tcp_mbps': None,
        'udp_mbps': None,
        'jitter_ms': None,
        'loss_pct': None,
    }
    if not isinstance(iperf, dict):
        return metrics

    end = iperf.get('end', {}) or {}

    def _mbps(section):
        try:
            bps = section.get('bits_per_second')
            if bps is None:
                return None
            return round(float(bps) / 1e6, 2)
        except Exception:
            return None

    for key in ('sum_received', 'sum_sent', 'sum'):
        section = end.get(key)
        if isinstance(section, dict):
            value = _mbps(section)
            if value is not None:
                if key == 'sum':
                    metrics['udp_mbps'] = value
                elif metrics['tcp_mbps'] is None:
                    metrics['tcp_mbps'] = value

    sum_section = end.get('sum')
    if isinstance(sum_section, dict):
        try:
            if sum_section.get('jitter_ms') is not None:
                metrics['jitter_ms'] = round(float(sum_section.get('jitter_ms')), 3)
        except Exception:
            pass
        try:
            if sum_section.get('lost_percent') is not None:
                metrics['loss_pct'] = round(float(sum_section.get('lost_percent')), 2)
        except Exception:
            pass

    streams = end.get('streams')
    if metrics['tcp_mbps'] is None and isinstance(streams, list):
        received_vals = []
        sent_vals = []
        for stream in streams:
            receiver = stream.get('receiver') if isinstance(stream, dict) else None
            sender = stream.get('sender') if isinstance(stream, dict) else None
            if isinstance(receiver, dict) and receiver.get('bits_per_second') is not None:
                try:
                    received_vals.append(float(receiver.get('bits_per_second')))
                except Exception:
                    pass
            if isinstance(sender, dict) and sender.get('bits_per_second') is not None:
                try:
                    sent_vals.append(float(sender.get('bits_per_second')))
                except Exception:
                    pass
        if received_vals:
            metrics['tcp_mbps'] = round(sum(received_vals) / 1e6, 2)
        elif sent_vals:
            metrics['tcp_mbps'] = round(sum(sent_vals) / 1e6, 2)

    return metrics

def _fmt_dbm(value):
    try:
        num = float(value)
    except Exception:
        return ''
    if abs(num - round(num)) < 0.05:
        return str(int(round(num)))
    return f'{num:.1f}'.rstrip('0').rstrip('.')

def parse_phy_txpower_options(iw_phy_text):
    options = {}
    cur_phy = None
    for line in (iw_phy_text or '').splitlines():
        pm = re.match(r'Wiphy phy(\d+)', line)
        if pm:
            cur_phy = pm.group(1)
            options.setdefault(cur_phy, set())
            continue
        if cur_phy is None:
            continue
        dm = re.search(r'\(([\d.]+)\s+dBm\)', line)
        if dm:
            fmt = _fmt_dbm(dm.group(1))
            if fmt:
                options[cur_phy].add(fmt)
    return {
        phy: sorted(vals, key=lambda v: float(v))
        for phy, vals in options.items() if vals
    }

def txpower_choices_from_cap(cap_dbm):
    try:
        cap = int(float(cap_dbm))
    except Exception:
        return []
    if cap < 1:
        return []
    return [str(v) for v in range(cap, 0, -1)]


def get_halow_bw_txpower_cap(bw):
    return HALOW_BW_TXPOWER_CAP_DBM.get(_format_halow_bw(bw), '')

def get_iface_txpower_cap(iface):
    try:
        r = subprocess.run(['iw', 'dev', iface, 'info'], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return ''
        if iface == 'wlan2':
            bw_cap = get_halow_bw_txpower_cap(get_halow_driver_info(iface).get('halow_bw', ''))
            if bw_cap:
                return bw_cap
        phy = ''
        current = ''
        m = re.search(r'txpower ([\d.]+) dBm', r.stdout)
        if m:
            current = _fmt_dbm(m.group(1))
        m = re.search(r'wiphy (\d+)', r.stdout)
        if m:
            phy = m.group(1)
        else:
            m = re.search(r'wdev (0x[0-9a-fA-F]+)', r.stdout)
            if m:
                phy = str(int(m.group(1), 16) >> 32)
        if not phy:
            return current
        r = subprocess.run(['iw', 'phy'], capture_output=True, text=True, timeout=5)
        options = parse_phy_txpower_options(r.stdout).get(phy, [])
        if not options:
            return current
        cap = max(options, key=lambda v: float(v))
        if iface == 'wlan2' and current:
            return _fmt_dbm(min(float(cap), float(current)))
        return _fmt_dbm(cap)
    except Exception:
        return ''

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

    # Per-node toggle: call the target node directly, no Alfred broadcast
    if node_ip != 'all':
        r = call_node_api(node_ip, '/api/control/interface', 'POST',
                          {'iface': iface, 'state': state})
        return r

    # Global toggle: use Alfred broadcast/consensus
    expected = radio_expected_hosts()
    if not expected:
        return {'ok': False, 'error': 'No reachable nodes in registry'}
    if len(expected) < 2:
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
        'targets': 'all',
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
        'targets': 'all',
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
    cap = get_iface_txpower_cap(iface)
    if cap:
        info['txpower_cap_dbm'] = cap
        info['txpower_options_dbm'] = txpower_choices_from_cap(cap)
    else:
        info['txpower_options_dbm'] = []

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
        mcs_map = {
            'wlan0': {'tx_mcs': nd.get('WIFI_24_TX_MCS', ''), 'rx_mcs': nd.get('WIFI_24_RX_MCS', '')},
            'wlan1': {'tx_mcs': nd.get('WIFI_5_TX_MCS', ''), 'rx_mcs': nd.get('WIFI_5_RX_MCS', '')},
            'wlan2': {'tx_mcs': nd.get('HALOW_TX_MCS', ''), 'rx_mcs': nd.get('HALOW_RX_MCS', '')},
        }

        node_info = {
            'id':       nid,
            'hostname': hostname,
            'ip':       ip,
            'is_me':    is_me,
            'is_gateway': nd.get('IS_GATEWAY', 'false').lower() == 'true',
            'gateway_iface': nd.get('GATEWAY_IFACE', ''),
            'battery':  nd.get('BATTERY_PERCENTAGE', ''),
            'uptime':   fmt_uptime(nd.get('UPTIME_SECONDS', '')),
        }

        if is_me:
            live_battery = get_local_battery_percentage()
            if live_battery:
                node_info['battery'] = live_battery
            live_uptime = get_local_uptime()
            if live_uptime:
                node_info['uptime'] = live_uptime
            node_info['interfaces'] = {
                'wlan0': {'active': 'wlan0' in active_ifaces, **iw_wlan0, **mcs_map['wlan0']},
                'wlan1': {'active': 'wlan1' in active_ifaces, **iw_wlan1, **mcs_map['wlan1']},
                'wlan2': {'active': 'wlan2' in active_ifaces, **iw_wlan2, **mcs_map['wlan2']},
            }
        else:
            # Fetch from remote node's /api/local via peer proxy
            try:
                local = call_node_api(ip, '/api/local')
                live_battery = local.get('battery')
                if isinstance(live_battery, dict) and live_battery.get('percentage') is not None:
                    node_info['battery'] = str(live_battery.get('percentage'))
                ifaces_raw = local.get('interfaces', [])
                node_info['interfaces'] = {
                    i['name']: {
                        'active': i.get('health') == 'ok' and i.get('role') == 'mesh',
                        'channel': i.get('channel', ''),
                        'freq_mhz': i.get('freq_mhz', ''),
                        'txpower_dbm': i.get('txpower_dbm', ''),
                        'txpower_cap_dbm': i.get('txpower_cap_dbm', ''),
                        'txpower_options_dbm': i.get('txpower_options_dbm', []),
                        'tx_mcs': mcs_map.get(i['name'], {}).get('tx_mcs', ''),
                        'rx_mcs': mcs_map.get(i['name'], {}).get('rx_mcs', ''),
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
        'hop_count', 'hop_count_source',
        'tcp_mbps', 'udp_mbps', 'jitter_ms', 'loss_pct',
        'rtt_avg_ms', 'rtt_min_ms', 'rtt_max_ms',
        'src_lat', 'src_lon', 'src_alt',
        'dst_lat', 'dst_lon', 'dst_alt',
    ])
    for r in results:
        iperf = r.get('iperf3_result', {})
        ping  = r.get('ping_result', {})
        metrics = extract_iperf3_metrics(iperf)
        ping_loss = ping.get('loss_pct', '') if ping else ''
        loss_value = metrics['loss_pct'] if metrics['loss_pct'] is not None else ping_loss
        gps_src = r.get('gps_source') or {}
        gps_dst = r.get('gps_destination') or {}
        writer.writerow([
            r.get('timestamp', ''),
            r.get('session_label', ''),
            r.get('test_type', ''),
            r.get('source_node', ''),
            r.get('destination_node', ''),
            ','.join(r.get('active_interfaces', [])),
            r.get('halow_channel', ''),
            r.get('halow_bw', ''),
            r.get('hop_count', ''),
            r.get('hop_count_source', ''),
            '' if metrics['tcp_mbps'] is None else metrics['tcp_mbps'],
            '' if metrics['udp_mbps'] is None else metrics['udp_mbps'],
            '' if metrics['jitter_ms'] is None else metrics['jitter_ms'],
            loss_value,
            ping.get('rtt_avg', '') if ping else '',
            ping.get('rtt_min', '') if ping else '',
            ping.get('rtt_max', '') if ping else '',
            gps_src.get('lat', ''),
            gps_src.get('lon', ''),
            gps_src.get('alt', ''),
            gps_dst.get('lat', ''),
            gps_dst.get('lon', ''),
            gps_dst.get('alt', ''),
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
    metrics = extract_iperf3_metrics(iperf)
    for key, value in metrics.items():
        if value is not None:
            summary[key] = value

    for key in ('rtt_avg', 'rtt_min', 'rtt_max', 'loss_pct'):
        if ping.get(key) is not None:
            summary[key] = ping.get(key)
    if record.get('hop_count') is not None:
        summary['hop_count'] = record.get('hop_count')
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
                    'hop_count':        None,
                    'hop_count_source': '',
                }
                hop_count, hop_source, gps_src, gps_dst = get_session_info(src_ip, dst_ip)
                result_record['hop_count'] = hop_count
                result_record['hop_count_source'] = hop_source
                result_record['gps_source'] = gps_src
                result_record['gps_destination'] = gps_dst

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


def run_upload_github_job():
    repo_dir = '/home/radio/manet-dev'
    meas_src = SESSIONS_DIR
    meas_dst = os.path.join(repo_dir, 'measurements')
    try:
        update_upload_status(phase='sync', progress='Syncing measurements into repo...', percent=10)
        subprocess.run(['rsync', '-a', meas_src + '/', meas_dst + '/'],
                       check=True, timeout=60)

        update_upload_status(phase='git-add', progress='Staging measurement files...', percent=35)
        subprocess.run(['git', '-C', repo_dir, 'add', 'measurements/'],
                       check=True, timeout=15)

        update_upload_status(phase='git-commit', progress='Creating git commit...', percent=55)
        ts = datetime.now().strftime('%Y-%m-%d %H:%M')
        commit = subprocess.run(['git', '-C', repo_dir, 'commit', '-m',
                                 f'measurements: add results {ts}'],
                                capture_output=True, text=True, timeout=15)
        if commit.returncode != 0:
            commit_out = (commit.stderr or commit.stdout or '').lower()
            if 'nothing to commit' not in commit_out:
                raise subprocess.CalledProcessError(
                    commit.returncode, commit.args, output=commit.stdout, stderr=commit.stderr
                )

        update_upload_status(phase='git-push', progress='Pushing measurements to GitHub...', percent=80)
        subprocess.run(['git', '-C', repo_dir, 'push'],
                       check=True, timeout=120)
        finish_upload_status(True, progress='Uploaded measurements to GitHub')
    except subprocess.CalledProcessError as e:
        finish_upload_status(False, error=str(e))
    except Exception as e:
        finish_upload_status(False, error=str(e))


def run_upload_ventum_job():
    archive = ''
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

        update_upload_status(phase='pack', progress='Packing measurements archive...', percent=15)
        subprocess.run(
            ['tar', '-C', os.path.dirname(SESSIONS_DIR),
             '-czf', archive, os.path.basename(SESSIONS_DIR)],
            check=True, timeout=120
        )
        total = os.path.getsize(archive) if os.path.exists(archive) else 0
        update_upload_status(
            phase='upload',
            progress='Uploading archive to Ventum...',
            bytes_total=total,
            bytes_sent=0,
            percent=55,
            file=remote_name,
            url=upload_url,
        )

        subprocess.run(
            ['curl', '-fS', '-u', ventum_auth, '-T', archive, upload_url],
            check=True, timeout=300
        )
        update_upload_status(bytes_sent=total, percent=95)
        finish_upload_status(True, progress='Uploaded measurements to Ventum', file=remote_name, url=upload_url)
    except subprocess.CalledProcessError as e:
        finish_upload_status(False, error=str(e))
    except Exception as e:
        finish_upload_status(False, error=str(e))
    finally:
        if archive:
            try:
                os.remove(archive)
            except Exception:
                pass

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
  --info:    #00003f;
  --warn:    #ecb000;
  --fer-yellow:#ecb000;
  --fer-black:#02000d;
  --green:   #16a34a;
  --orange:  #8a6a00;
  --red:     #dc2626;
  --text:    #02000d;
  --muted:   #615f68;
  --shadow:  0 18px 50px rgba(2,0,13,.10);
  --font:    Roobert, Arial, sans-serif;
}
:root[data-theme="dark"] {
  --bg:      #02000d;
  --surface: #121118;
  --card:    #17151d;
  --panel:   #0b0a12;
  --border:  #34313b;
  --border2: #24212b;
  --accent:  #00003f;
  --accent2: #ecb000;
  --info:    #9fa8ff;
  --warn:    #ecb000;
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
    radial-gradient(circle at top left, rgba(236,176,0,.16), transparent 34%),
    linear-gradient(160deg, transparent 0 56%, rgba(0,0,63,.05) 56% 70%, transparent 70%),
    var(--bg);
}

/* header */
#hdr{background:rgba(255,255,255,.94);backdrop-filter:blur(18px);border-bottom:1px solid var(--border2);padding:0 22px;min-height:58px;display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:100;box-shadow:0 1px 0 rgba(2,0,13,.05);transition:min-height .18s ease,padding .18s ease,gap .18s ease}
#hdr::after{content:'';position:absolute;left:0;right:0;bottom:0;height:2px;background:linear-gradient(90deg,rgba(236,176,0,.92) 0 36%,rgba(236,176,0,.28) 36% 68%,transparent 68%);pointer-events:none}
:root[data-theme="dark"] #hdr{background:rgba(18,17,24,.92)}
.fer-lockup{display:flex;align-items:center;justify-content:flex-start;height:58px;min-width:clamp(104px,18vw,172px);width:clamp(104px,18vw,172px);padding-right:8px;border-right:1px solid var(--border);color:var(--fer-black);overflow:hidden;flex:0 0 auto}
.fer-logo-img{display:block;width:clamp(104px,18vw,172px);height:48px;max-width:none;object-fit:contain;object-position:left center;filter:none;transition:width .18s ease,height .18s ease,filter .18s ease}
:root[data-theme="dark"] .fer-lockup{color:#ffffff}
:root[data-theme="dark"] .fer-logo-img{filter:brightness(0) invert(1)}
#hdr-logo{color:var(--text);font-size:17px;letter-spacing:0;font-weight:900;display:flex;align-items:center;min-height:46px;line-height:1}
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
.overview-link-btn{border:1px solid var(--accent2);background:var(--accent2);color:var(--fer-black);border-radius:999px;padding:6px 12px;font-family:var(--font);font-size:11px;font-weight:850;cursor:pointer;min-width:92px;transition:background .18s ease,color .18s ease,box-shadow .18s ease,transform .18s ease;box-shadow:0 8px 20px rgba(236,176,0,.16)}
.overview-link-btn:hover{background:#f6c62f;color:var(--fer-black);box-shadow:0 10px 24px rgba(236,176,0,.26);transform:translateY(-1px)}
:root[data-theme="dark"] .overview-link-btn{background:var(--accent2);color:var(--fer-black);border-color:var(--accent2)}
:root[data-theme="dark"] .overview-link-btn:hover{background:#f6c62f;color:var(--fer-black);box-shadow:0 10px 24px rgba(236,176,0,.28)}

/* sidebar nav */
#page{display:flex;min-height:calc(100vh - 58px)}
#nav{background:var(--surface);border-right:1px solid var(--border2);width:160px;flex:0 0 160px;display:flex;flex-direction:column;padding:12px 0;position:sticky;top:58px;height:calc(100vh - 58px);overflow-y:auto;z-index:90;transition:top .18s ease}
.tab{padding:11px 20px;cursor:pointer;font-size:12px;font-weight:700;letter-spacing:0;color:var(--muted);border-left:none;border-bottom:2px solid transparent;text-transform:none;transition:color .15s ease,border-color .15s ease,background .15s ease;white-space:nowrap}
.tab:hover{color:var(--text);background:color-mix(in srgb, var(--panel) 92%, transparent)}
.tab.active{color:var(--text);border-bottom-color:var(--fer-yellow);background:transparent}
.tab.active::after{display:none}

/* layout */
#content{padding:22px;max-width:1120px;width:100%;flex:1;min-width:0;position:relative}
#content::before{content:'';display:block;height:36px;margin:-22px -22px 18px;background:
  linear-gradient(90deg, rgba(236,176,0,.24) 0 12%, transparent 12% 100%);
  border-bottom:1px solid var(--border2);transition:height .18s ease,margin .18s ease,opacity .18s ease}
:root[data-theme="dark"] #content::before{background:
  linear-gradient(90deg, rgba(236,176,0,.28) 0 12%, transparent 12% 100%)}
body.chrome-compact #hdr{min-height:50px;gap:14px}
  body.chrome-compact .fer-lockup{min-width:clamp(96px,18vw,140px);width:clamp(96px,18vw,140px);height:44px}
  body.chrome-compact .fer-logo-img{width:clamp(96px,18vw,140px);height:36px}
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
.b-gw {color:var(--fer-black);border-color:rgba(236,176,0,.48);background:rgba(236,176,0,.14)}
.b-me {color:var(--info);border-color:rgba(0,0,63,.22);background:rgba(0,0,63,.08)}

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
.node-card.is-me{border-color:rgba(0,0,63,.28);box-shadow:0 10px 26px rgba(0,0,63,.08)}
.node-card.is-gw{border-color:rgba(236,176,0,.5);box-shadow:0 10px 26px rgba(236,176,0,.12)}
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
#msg.info{border-color:var(--info);color:var(--info);background:rgba(0,0,63,.06);display:block}

/* foreground overlay */
#overlay{position:fixed;left:50%;top:18px;transform:translate(-50%,-130%);z-index:10000;width:min(520px,calc(100vw - 24px));background:var(--surface);border:1px solid var(--info);border-radius:8px;box-shadow:0 18px 60px rgba(2,0,13,.22);opacity:0;transition:transform .18s ease,opacity .18s ease;pointer-events:none}
#overlay.show{transform:translate(-50%,0);opacity:1;pointer-events:auto}
#overlay.ok{border-color:#b8e6c8}
#overlay.err{border-color:#f3b6b1}
#overlay.info{border-color:var(--info)}
.overlay-body{display:flex;align-items:flex-start;gap:10px;padding:12px 14px}
#overlay-text{flex:1;font-size:12px;line-height:1.35;overflow-wrap:anywhere}
#overlay-close{background:transparent;border:0;color:var(--muted);font-family:var(--font);font-size:18px;line-height:1;cursor:pointer;padding:0 2px}
#overlay.ok #overlay-text{color:var(--green)}
#overlay.err #overlay-text{color:var(--red)}
#overlay.info #overlay-text{color:var(--info)}

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
.upload-status{display:none;margin:12px 16px 0;padding:12px;border:1px solid var(--border2);border-radius:8px;background:var(--card)}
.upload-status-title{font-size:10px;font-weight:800;color:var(--muted);letter-spacing:.4px}
.upload-status-text{margin-top:6px;font-size:11px;font-weight:700}
.upload-status-meta{margin-top:4px;font-size:10px;color:var(--muted);line-height:1.4}
.upload-status-bar{margin-top:8px;height:8px;border-radius:999px;background:var(--border2);overflow:hidden}
.upload-status-fill{height:100%;width:0;background:var(--accent2);transition:width .18s ease}
.footer-actions{display:flex;justify-content:flex-end;padding:8px 0 6px}
.logout-link-btn{border:1px solid rgba(180,35,24,.26);background:rgba(180,35,24,.05);color:#b42318;border-radius:999px;padding:8px 14px;font-family:var(--font);font-size:11px;font-weight:850;cursor:pointer;transition:background .18s ease,color .18s ease,box-shadow .18s ease,transform .18s ease}
.logout-link-btn:hover{background:#b42318;color:#ffffff;box-shadow:0 8px 20px rgba(180,35,24,.16);transform:translateY(-1px)}
:root[data-theme="dark"] .logout-link-btn{border-color:rgba(239,68,68,.34);background:rgba(239,68,68,.08);color:#ffb4b4}
:root[data-theme="dark"] .logout-link-btn:hover{background:#ef4444;color:#ffffff;box-shadow:0 8px 20px rgba(239,68,68,.22)}

@media (max-width: 620px) {
  html,body{font-size:12px}
  #hdr{position:relative;padding:10px 12px;gap:6px;align-items:flex-start;flex-wrap:wrap}
  .fer-lockup{order:1;min-width:clamp(92px,24vw,132px);width:clamp(92px,24vw,132px);height:46px;padding-right:4px}
  .fer-logo-img{width:clamp(92px,24vw,132px);height:38px}
  #hdr-logo{order:2;font-size:16px;letter-spacing:0;flex:1;min-width:140px}
  #hdr-node{order:3;flex:0 0 100%;border-left:0;padding-left:0;font-size:10px;max-width:100%;overflow-wrap:anywhere}
  #hdr-right{order:4;flex:0 0 100%;margin-left:0;width:100%;justify-content:flex-start;gap:8px;flex-wrap:wrap}
  #hdr-clock{font-size:10px}
  #hdr-inet{font-size:9px;padding:3px 8px}
  .theme-toggle{padding:5px 8px;min-width:66px;font-size:10px}
  #page{flex-direction:column}
  #nav{width:100%;flex:0 0 auto;display:grid;grid-template-columns:repeat(3,1fr);position:static;height:auto;border-right:none;border-bottom:1px solid var(--border2);padding:0;overflow:visible;top:auto}
  .tab{padding:9px 4px;font-size:10px;white-space:nowrap;text-align:center;border-left:none;border-bottom:3px solid transparent;box-sizing:border-box;overflow:hidden;text-overflow:ellipsis}
  .tab.active{border-left-color:transparent;border-bottom-color:var(--fer-yellow);background:transparent}
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
const VALID_TABS = ['topology','radio','measure','sessions','upload'];
const AUTO_REFRESH_MS = 15000;
const THEME_KEY = 'manetUiTheme';

function preferredTheme() {
  const params = new URLSearchParams(window.location.search);
  const forced = params.get('theme');
  if (forced === 'dark' || forced === 'light') {
    try { localStorage.setItem(THEME_KEY, forced); } catch(e) {}
    return forced;
  }
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
  document.querySelectorAll('.fer-logo-img[data-light][data-dark]').forEach(img => {
    img.src = theme === 'dark' ? img.dataset.dark : img.dataset.light;
  });
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
    buildHalowConfig();
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
    if (_tab === 'radio') buildIfaceControl();
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

function normalizeDbm(value) {
  const num = parseFloat(value);
  if (!Number.isFinite(num)) return '';
  return Number.isInteger(num) ? String(num) : String(num.toFixed(1)).replace(/[.]0$/, '');
}

const HALOW_BW_TXPOWER_CAPS = { '1MHz': '24', '2MHz': '24', '4MHz': '22' };

function txPowerOptionsForCap(cap) {
  const num = parseFloat(cap);
  if (!Number.isFinite(num) || num < 1) return [];
  const out = [];
  for (let v = Math.floor(num); v >= 1; v--) out.push(String(v));
  return out;
}

function txPowerOptions(info) {
  const opts = Array.isArray(info?.txpower_options_dbm) ? info.txpower_options_dbm.map(normalizeDbm).filter(Boolean) : [];
  const cur = normalizeDbm(info?.txpower_dbm);
  if (cur && !opts.includes(cur)) opts.push(cur);
  opts.sort((a, b) => parseFloat(a) - parseFloat(b));
  return opts;
}

function renderTxPowerSelect(id, info) {
  const opts = txPowerOptions(info);
  if (!opts.length) {
    const cur = normalizeDbm(info?.txpower_dbm) || '';
    return `<input id="${id}" type="number" min="1" max="30" step="1" value="${cur}" style="width:60px" placeholder="dBm">`;
  }
  const current = normalizeDbm(info?.txpower_dbm) || opts[opts.length - 1];
  return `<select id="${id}">` +
    opts.map(v => `<option value="${v}"${v === current ? ' selected' : ''}>${v} dBm</option>`).join('') +
    `</select>`;
}

function updateHalowTxpowerOptions(preferredValue = '') {
  const bwEl = document.getElementById('halow-bw');
  let select = document.getElementById('txpwr-all-wlan2');
  if (!bwEl || !select) return;

  const bw = bwEl.value || '1MHz';
  const cap = normalizeDbm(HALOW_BW_TXPOWER_CAPS[bw]);
  const opts = txPowerOptionsForCap(cap);
  if (!opts.length) {
    select.outerHTML = `<select id="txpwr-all-wlan2" disabled><option value="">n/a</option></select>`;
    return;
  }

  const prevVal = normalizeDbm(preferredValue || select.value);
  const prevCap = normalizeDbm(select.dataset.cap);
  let nextVal = opts[0];

  if (prevVal && opts.includes(prevVal)) {
    nextVal = prevVal;
  } else if (prevVal && prevCap && prevVal === prevCap) {
    nextVal = cap;
  }

  select.outerHTML = `<select id="txpwr-all-wlan2" data-cap="${cap}">` +
    opts.map(v => `<option value="${v}"${v === nextVal ? ' selected' : ''}>${v} dBm</option>`).join('') +
    `</select>`;
}

function getNodeInfo(nodeIp, iface) {
  if (!_topo || !_topo.nodes) return {};
  const node = _topo.nodes.find(n => n.ip === nodeIp || (nodeIp === 'all' && n.is_me));
  if (!node || !node.interfaces) return {};
  return node.interfaces[iface] || {};
}

function syncSelectValue(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  const normalized = value == null ? '' : String(value);
  const hasOption = Array.from(el.options || []).some(opt => opt.value === normalized);
  if (hasOption) el.value = normalized;
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
      if (_tab === 'radio') buildIfaceControl();
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
  if (name === 'radio') buildIfaceControl();
}

// ── Clock ──
function tickLocalTime() {
  const el = document.getElementById('hdr-clock');
  if (el) el.textContent = new Date().toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
}
setInterval(tickLocalTime, 1000);
tickLocalTime();

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
      const mcs = on && (info.tx_mcs || info.rx_mcs) ? ` · ${info.tx_mcs || '-'} / ${info.rx_mcs || '-'}` : '';
      return `<span class="iface-chip ${on ? 'iface-on' : 'iface-off'}">${band}${on ? ch : ' OFF'}${mcs}</span>`;
    }).join('');
    const bat = node.battery ? `<div class="node-battery">BAT ${node.battery}%</div>` : '';
    const tags = [
      node.is_me ? '<span class="badge b-me" style="font-size:9px">ME</span>' : '',
      node.is_gateway ? `<span class="badge b-gw" style="font-size:9px">GW${node.gateway_iface ? '·' + node.gateway_iface : ''}</span>` : '',
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
  setUploadButtons(false);
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
    const BANDS = {wlan0: '2.4 GHz', wlan1: '5 GHz', wlan2: 'HaLow'};
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
          LINK RATE
          <strong>${info.tx_mcs || '-'}</strong>
          /
          <strong>${info.rx_mcs || '-'}</strong>
          <span style="color:var(--muted)">TX / RX</span>
        </div>
        <div class="txpwr-row">
          TX POWER
          ${renderTxPowerSelect(`txpwr-${node.id}-${iface}`, info)}
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
  const isAll = nodeIp === 'all';
  showOverlay(isAll
    ? `Coordinating ${iface} ${state} on all nodes through Alfred...`
    : `Setting ${iface} ${state} on ${nodeIp}...`, 'info');
  showMsg(isAll
    ? `Staging ${iface} ${state}; waiting for mesh ACKs...`
    : `Setting ${iface} ${state} on ${nodeIp}...`, 'info');
  try {
    const r = await fetch('/api/interface/toggle', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({node_ip: nodeIp, iface, state})
    });
    const d = await r.json();
    if (d.ok) {
      if (d.activate_at) {
        const wait = Math.max(0, d.activate_at - Math.floor(Date.now() / 1000));
        showOverlay(`${iface} ${state} ACKed by ${d.acked?.length || 0}/${d.expected?.length || 0} nodes. Applying in ${wait}s...`, 'ok');
        showMsg(`${iface} ${state} scheduled through Alfred`, 'ok');
        verifyRadioExecution(nodeIp, iface, state, d.activate_at);
      } else {
        showOverlay(`${iface} ${state} applied on ${nodeIp}`, 'ok');
        showMsg(`${iface} ${state} applied`, 'ok');
        setTimeout(() => { hideOverlay(); loadTopology(); }, 3000);
      }
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
  if (!dbm) {
    showMsg(`No TX power options available for ${iface}@${nodeIp}`, 'err');
    return;
  }
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
function buildHalowConfig() {
  const halowInfo = getNodeInfo('all', 'wlan2');
  syncSelectValue('halow-ch', halowInfo.channel);
  syncSelectValue('halow-bw', halowInfo.halow_bw);
  syncSelectValue('ch-2g', getNodeInfo('all', 'wlan0').channel);
  syncSelectValue('ch-5g', getNodeInfo('all', 'wlan1').channel);
  for (const iface of ['wlan0', 'wlan1', 'wlan2']) {
    const info = getNodeInfo('all', iface);
    const select = document.getElementById(`txpwr-all-${iface}`);
    if (select) {
      select.outerHTML = renderTxPowerSelect(`txpwr-all-${iface}`, info);
    }
  }
  updateHalowTxpowerOptions(halowInfo.txpower_dbm);
}

async function applyHalow() {
  const ch = document.getElementById('halow-ch').value;
  const bw = document.getElementById('halow-bw').value;
  const dbm = document.getElementById('txpwr-all-wlan2').value;
  setButtonBusy('btn-apply-halow', true, 'APPLYING...', 'APPLY TO ALL NODES');
  showOverlay(`Applying HaLow ch${ch} / ${bw} / ${dbm} dBm — verifying all nodes...`, 'info');
  try {
    const r = await fetch('/api/halow/channel', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({channel: parseInt(ch), bw, dbm: parseFloat(dbm)})
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
    let msg = `HaLow ch${ch} / ${bw} / ${dbm} dBm applied to: ${d.applied?.join(', ')}`;
    if (d.unreachable?.length) msg += ` · WARNING: not in mesh: ${d.unreachable.join(', ')}`;
    if (d.warning) msg += ` - WARNING: ${d.warning}`;
    showMsg(msg, (d.warning || d.unreachable?.length) ? 'info' : 'ok');
    await fetchTopo();
  } catch (e) {
    showMsg('HaLow apply failed: ' + e.message, 'err');
  } finally {
    setButtonBusy('btn-apply-halow', false, '', 'APPLY TO ALL NODES');
  }
}

async function apply2G() {
  const ch = document.getElementById('ch-2g').value;
  const dbm = document.getElementById('txpwr-all-wlan0').value;
  setButtonBusy('btn-apply-2g', true, 'APPLYING...', 'APPLY TO ALL NODES');
  showOverlay(`Applying 2.4G ch${ch} / ${dbm} dBm to all nodes...`, 'info');
  showMsg(`Applying 2.4G ch${ch} / ${dbm} dBm to all nodes...`, 'info');
  try {
    const r = await fetch('/api/wifi/channel', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({interface: 'wlan0', channel: parseInt(ch), dbm: parseFloat(dbm)})
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
    showMsg(`2.4G ch${ch} / ${dbm} dBm applied to all nodes`, 'ok');
    await fetchTopo();
  } catch (e) {
    showMsg('2.4G channel failed: ' + e.message, 'err');
  } finally {
    setButtonBusy('btn-apply-2g', false, '', 'APPLY TO ALL NODES');
  }
}

async function apply5G() {
  const ch = document.getElementById('ch-5g').value;
  const dbm = document.getElementById('txpwr-all-wlan1').value;
  setButtonBusy('btn-apply-5g', true, 'APPLYING...', 'APPLY TO ALL NODES');
  showOverlay(`Applying 5G ch${ch} / ${dbm} dBm to all nodes...`, 'info');
  try {
    const r = await fetch('/api/wifi/channel', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({interface: 'wlan1', channel: parseInt(ch), dbm: parseFloat(dbm)})
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
    showMsg(`5G ch${ch} / ${dbm} dBm applied to all nodes`, 'ok');
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
  try {
    const r = await fetch('/api/upload/github', {method:'POST'});
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
    setUploadButtons(true);
    setUploadStatus({running:true,target:'github',phase:'starting',progress:'Starting GitHub upload...',percent:1});
    pollUploadStatus();
  } catch (e) {
    setUploadButtons(false);
    showMsg('GitHub upload failed: ' + e.message, 'err');
  }
}

async function uploadVentum() {
  try {
    const r = await fetch('/api/upload/ventum', {method:'POST'});
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
    setUploadButtons(true);
    setUploadStatus({running:true,target:'ventum',phase:'starting',progress:'Starting Ventum upload...',percent:1});
    pollUploadStatus();
  } catch (e) {
    setUploadButtons(false);
    showMsg('Ventum upload failed: ' + e.message, 'err');
  }
}

let _uploadPollTimer = null;

function setUploadButtons(running) {
  const githubBtn = document.getElementById('upload-github');
  const ventumBtn = document.getElementById('upload-ventum');
  const disabled = running || !_topo?.internet;
  if (githubBtn) githubBtn.disabled = disabled;
  if (ventumBtn) ventumBtn.disabled = disabled;
}

function setUploadStatus(d) {
  const card = document.getElementById('upload-status');
  const title = document.getElementById('upload-status-title');
  const text = document.getElementById('upload-status-text');
  const meta = document.getElementById('upload-status-meta');
  const fill = document.getElementById('upload-status-fill');
  if (!card || !title || !text || !meta || !fill) return;

  if (!d || (!d.running && !d.progress && !d.error && !d.done)) {
    card.style.display = 'none';
    return;
  }

  card.style.display = '';
  const target = (d.target || 'upload').toUpperCase();
  const percent = Number.isFinite(Number(d.percent)) ? Math.max(0, Math.min(100, Number(d.percent))) : 0;
  title.textContent = `${target} STATUS`;
  text.textContent = d.error ? `Error: ${d.error}` : (d.progress || 'Working...');
  meta.textContent = [
    d.phase ? `phase ${d.phase}` : '',
    percent ? `${percent}%` : '',
    d.bytes_total ? `${d.bytes_sent || 0}/${d.bytes_total} bytes` : '',
    d.file ? d.file : ''
  ].filter(Boolean).join(' · ');
  fill.style.width = `${percent}%`;
  fill.style.background = d.error ? 'var(--red)' : (d.done ? 'var(--green)' : 'var(--accent2)');
}

async function pollUploadStatus() {
  if (_uploadPollTimer) {
    clearTimeout(_uploadPollTimer);
    _uploadPollTimer = null;
  }
  try {
    const r = await fetch('/api/upload/status');
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    setUploadStatus(d);
    setUploadButtons(!!d.running);
    if (d.running) {
      _uploadPollTimer = setTimeout(pollUploadStatus, 1000);
      return;
    }
    if (d.done) {
      showMsg(`${(d.target || 'upload').toUpperCase()} upload complete`, 'ok');
    } else if (d.error) {
      showMsg(`${(d.target || 'upload').toUpperCase()} upload failed: ${d.error}`, 'err');
    }
  } catch (e) {
    setUploadButtons(false);
    showMsg('Upload status failed: ' + e.message, 'err');
  }
}

window.onload = async () => {
  updateChromeCompact();
  showTab(getInitialTab());
  await fetchTopo();
  buildIfaceControl();
  buildHalowConfig();
  const halowBw = document.getElementById('halow-bw');
  if (halowBw) halowBw.addEventListener('change', () => updateHalowTxpowerOptions());
  startAutoRefresh();
  pollUploadStatus();
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
  <div class="fer-lockup" title="FER" aria-label="FER">
    <img class="fer-logo-img" src="/assets/fer-logo-black.svg" data-light="/assets/fer-logo-black.svg" data-dark="/assets/fer-logo-white.svg" alt="FER">
  </div>
  <div id="hdr-logo">MANET//<span>PERF</span></div>
  <div id="hdr-node"><strong>{hostname}</strong> &nbsp;{ip}</div>
  <div id="hdr-right">
    <span id="hdr-inet" class="no">○ NO INET</span>
    <span id="hdr-clock"></span>
    <button id="overview-link" class="overview-link-btn" type="button" onclick="window.location.href='http://manet.local/?theme=' + encodeURIComponent(document.documentElement.dataset.theme || 'light')">OVERVIEW</button>
    <button id="theme-toggle" class="theme-toggle" type="button" onclick="toggleTheme()">Dark</button>
  </div>
</div>

<div id="page">
<div id="nav">
  <div class="tab active" data-tab="topology" onclick="showTab('topology')">TOPOLOGY</div>
  <div class="tab"        data-tab="radio"    onclick="showTab('radio')">RADIO CONFIG</div>
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

  <!-- ── RADIO CONFIG ── -->
  <div id="tab-radio" class="tab-pane" style="display:none">
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
    <div class="card">
      <div class="card-title">HaLow</div>
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
        <span class="row-label">TX Power</span>
        <select id="txpwr-all-wlan2"><option value="">Loading...</option></select>
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
        <span class="row-label">TX Power</span>
        <select id="txpwr-all-wlan0"><option value="">Loading...</option></select>
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
        <span class="row-label">TX Power</span>
        <select id="txpwr-all-wlan1"><option value="">Loading...</option></select>
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
      <div id="upload-status" class="upload-status">
        <div id="upload-status-title" class="upload-status-title">UPLOAD STATUS</div>
        <div id="upload-status-text" class="upload-status-text">—</div>
        <div id="upload-status-meta" class="upload-status-meta"></div>
        <div class="upload-status-bar"><div id="upload-status-fill" class="upload-status-fill"></div></div>
      </div>
      <div style="padding:12px 16px;font-size:10px;color:var(--muted);letter-spacing:.5px">
        UPLOAD BUTTONS ENABLED ONLY WHEN INTERNET IS AVAILABLE
      </div>
    </div>
  </div>
  <div class="footer-actions">
    <button class="logout-link-btn" type="button" onclick="window.location.href='/auth/perf-logout'">LOGOUT</button>
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

        if parsed.path == '/assets/fer-logo.svg':
            try:
                with open(FER_LOGO_FULL_FILE, 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'image/svg+xml')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Not found')

        elif parsed.path == '/assets/fer-logo-black.svg':
            try:
                with open(FER_LOGO_BLACK_FILE, 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'image/svg+xml')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Not found')

        elif parsed.path == '/assets/fer-logo-white.svg':
            try:
                with open(FER_LOGO_WHITE_FILE, 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'image/svg+xml')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Not found')

        elif path in ('/', '/index.html'):
            self.send_html(render_dashboard())

        elif path == '/api/topology':
            try:
                self.send_json(build_topology())
            except Exception as e:
                self.send_json({'error': str(e)}, 500)

        elif path == '/api/measure/status':
            with _measure_lock:
                self.send_json(dict(_measure_status))

        elif path == '/api/upload/status':
            self.send_json(get_upload_status())

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
                if node_ip == 'all':
                    nodes_raw = parse_registry()
                    applied = []
                    errors = []
                    for nd in nodes_raw.values():
                        ip = nd.get('IPV4_ADDRESS', '')
                        hostname = nd.get('HOSTNAME', ip)
                        if not ip:
                            continue
                        r = call_node_api(ip, '/api/control/txpower', 'POST',
                                          {'iface': iface, 'dbm': dbm})
                        if r.get('ok'):
                            applied.append(hostname)
                        else:
                            errors.append(f"{hostname}: {r.get('error')}")
                    if errors:
                        self.send_json({'ok': False, 'applied': applied, 'error': '; '.join(errors)})
                    else:
                        self.send_json({'ok': True, 'applied': applied, 'iface': iface, 'dbm': _fmt_dbm(dbm)})
                else:
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
                # Step 1: collect all known nodes from registry (no pre-ping — mesh may be only HaLow)
                targets = []
                for nd in nodes_raw.values():
                    ip       = nd.get('IPV4_ADDRESS', '')
                    hostname = nd.get('HOSTNAME', ip)
                    if ip:
                        targets.append({'hostname': hostname, 'ip': ip})

                if not targets:
                    self.send_json({'ok': False, 'error': 'No nodes in registry'})
                    return

                # Step 2: apply new channel to all nodes simultaneously.
                # NOTE: when HaLow is the only active mesh interface, all nodes will
                # temporarily lose connectivity during this step — this is expected.
                # We do NOT verify via mesh IP afterwards (mesh is down during switch).
                # We do NOT roll back (rollback calls would also fail via unreachable mesh).
                failed = []
                applied = []
                for node in targets:
                    r = call_node_api(node['ip'], '/api/control/halow_channel', 'POST', req)
                    if r.get('ok'):
                        applied.append(node['hostname'])
                    else:
                        failed.append(f"{node['hostname']}: {r.get('error', 'failed')}")

                if failed and not applied:
                    self.send_json({'ok': False, 'error': '; '.join(failed)})
                    return

                result = {'ok': True, 'applied': applied}
                if failed:
                    result['failed'] = failed
                    result['warning'] = 'Some nodes failed: ' + '; '.join(failed)
                self.send_json(result)
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/wifi/channel':
            try:
                req = json.loads(body)
                iface = req.get('interface', req.get('iface', ''))
                channel = req.get('channel')
                dbm = req.get('dbm')
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
                                      {'interface': iface, 'channel': channel, 'dbm': dbm})
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
                start_upload_status('github')
                t = threading.Thread(target=run_upload_github_job, daemon=True)
                t.start()
                self.send_json({'ok': True, 'started': True})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/upload/ventum':
            try:
                start_upload_status('ventum')
                t = threading.Thread(target=run_upload_ventum_job, daemon=True)
                t.start()
                self.send_json({'ok': True, 'started': True})
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

if __name__ == '__main__':
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    os.makedirs(SESSIONS_DIR, exist_ok=True)

    server = ThreadedServer(('0.0.0.0', port), PerfHandler)
    print(f'MANET Perf Dashboard listening on port {port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
