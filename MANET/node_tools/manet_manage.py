#!/usr/bin/env python3
"""
MANET Management UI
-------------------
Not a server. This is the route set behind /manage on mesh-status.py's port
80, mixed into its request handler — there is no second listening port, and
nothing here is reachable without the admin password.

Peers are never queried directly: node state comes from the Alfred-built
registry, and changes that affect other nodes are staged over Alfred.
Measurement runs from this node outward toward a peer's always-listening
iperf3 daemon.

Routes (relative to /manage):
  GET  /                        - Management UI HTML
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
  GET  /api/uplink/wifi         - USB Wi-Fi uplink status
  POST /api/uplink/wifi         - Set USB Wi-Fi uplink credentials (all nodes)
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

MESH_CONF_FILE  = '/etc/mesh.conf'
MESH_STATE_FILE = '/etc/mesh_ipv4_state'
REGISTRY_FILE   = '/var/run/mesh_node_registry'
SESSIONS_DIR    = '/var/log/manet-measurements'
CONTROL_PORT    = 80  # mesh-status.py port on each node
ALFRED_RADIO_TYPE = 71
ALFRED_RADIO_ACK_TYPE = 72
# The hexagon badge is line art with no fill, so it needs two inks: near-black
# strokes on light themes, white strokes on dark. Same artwork, same geometry.
FER_LOGO_DARK_INK_FILE  = '/usr/local/share/manet/fer-logo-black.png'
FER_LOGO_LIGHT_INK_FILE = '/usr/local/share/manet/fer-logo-white.png'
# Served under /assets/<name>, extension optional, so swapping a logo between
# svg and png only means changing the paths above.
FER_LOGO_ASSETS = {
    'fer-logo':       FER_LOGO_DARK_INK_FILE,
    'fer-logo-black': FER_LOGO_DARK_INK_FILE,
    'fer-logo-white': FER_LOGO_LIGHT_INK_FILE,
}


def logo_asset_token():
    """Cache-busting token for logo URLs.

    Logo assets are served with a one-hour cache. Without a token in the URL,
    replacing a logo leaves browsers showing the previous artwork from cache
    until it expires, because the URL never changes.
    """
    parts = []
    for path in (FER_LOGO_DARK_INK_FILE, FER_LOGO_LIGHT_INK_FILE):
        try:
            st = os.stat(path)
            parts.append(f'{st.st_mtime_ns}:{st.st_size}')
        except OSError:
            parts.append('missing')
    return hashlib.sha1('|'.join(parts).encode()).hexdigest()[:8]
USB_WIFI_UPLINK_SCRIPT = '/usr/local/bin/usb-wifi-uplink.sh'

# EU S1G channels (centre frequencies in MHz)
HALOW_EU_CHANNELS = [863500, 864500, 865500, 866500, 867500]
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
    # mesh-ip-manager.sh only ever writes PERSISTENT_IPV4 to this file
    return state.get('CURRENT_IPV4') or state.get('PERSISTENT_IPV4', '')

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


def get_usb_wifi_uplink_status():
    if not os.path.exists(USB_WIFI_UPLINK_SCRIPT):
        return {
            'enabled': True,
            'iface': '',
            'ssid': 'hotspot',
            'state': 'script-missing',
            'connected': False,
            'ip': '',
            'service': '',
        }
    try:
        r = subprocess.run(
            [USB_WIFI_UPLINK_SCRIPT, 'status-json'],
            capture_output=True, text=True, timeout=8
        )
        data = json.loads((r.stdout or '{}').strip() or '{}')
        data.setdefault('ssid', 'hotspot')
        data.setdefault('iface', '')
        data.setdefault('state', 'unknown')
        data.setdefault('connected', False)
        data.setdefault('ip', '')
        data.setdefault('service', '')
        data.setdefault('enabled', True)
        return data
    except Exception as e:
        return {
            'enabled': True,
            'iface': '',
            'ssid': 'hotspot',
            'state': 'error',
            'connected': False,
            'ip': '',
            'service': '',
            'error': str(e),
        }


def apply_usb_wifi_uplink(ssid, password, enabled=True):
    ssid = (ssid or 'hotspot').strip()
    password = password if password is not None else 'raspberry'
    if not ssid:
        raise ValueError('SSID is required')
    if not password:
        raise ValueError('Password is required')
    if not os.path.exists(USB_WIFI_UPLINK_SCRIPT):
        raise RuntimeError(f'Missing {USB_WIFI_UPLINK_SCRIPT}')
    r = subprocess.run(
        [USB_WIFI_UPLINK_SCRIPT, 'set', ssid, password, '1' if enabled else '0'],
        capture_output=True, text=True, timeout=45
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or '').strip()
        raise RuntimeError(err or f'usb-wifi-uplink exited {r.returncode}')
    return get_usb_wifi_uplink_status()


def apply_usb_wifi_uplink_all(ssid, password, enabled=True):
    """Push uplink credentials to every node via Alfred.

    This used to POST to each peer's port 8081 — the one place in the system
    that talked to another node over HTTP. It is staged like any other
    mesh-wide radio change now, so every node applies the same credentials at
    the same activate_at.
    """
    result = coordinate_radio_change({'uplink_wifi': {
        'ssid': ssid, 'password': password, 'enabled': bool(enabled),
    }})
    status = dict(get_usb_wifi_uplink_status())
    status.update({
        'ok': result.get('ok', False),
        'applied': result.get('acked', []),
        'error': result.get('error', ''),
    })
    return status


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


def get_session_hop_count(src_ip, dst_ip):
    """Hops from this node to dst, straight out of batman's own table.

    Measurements always originate here, so the local hop count is the right
    one — and asking a peer for it over HTTP is exactly what we are removing.
    """
    if not dst_ip:
        return None, 'missing'
    try:
        r = subprocess.run(['batctl', 'o'], capture_output=True, text=True, timeout=5)
    except Exception:
        return None, 'error'
    nodes = parse_registry()
    dst_macs = set()
    for nd in nodes.values():
        if nd.get('IPV4_ADDRESS') == dst_ip:
            dst_macs = {m.strip().lower()
                        for m in (nd.get('MAC_ADDRESSES', '') or '').split(',') if m.strip()}
            break
    if not dst_macs:
        return None, 'unknown'
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        if parts[0].lower().rstrip('*') in dst_macs:
            # A direct originator entry is one hop; anything reached via a
            # different nexthop is at least two.
            nexthop = parts[3].strip('[]').lower()
            return (1 if nexthop in dst_macs else 2), 'batctl'
    return None, 'unknown'


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


def txpower_options_for_iface(iface, cap_dbm, current_dbm=''):
    if iface == 'wlan2':
        fixed = _fmt_dbm(cap_dbm or current_dbm)
        return [fixed] if fixed else []
    return txpower_choices_from_cap(cap_dbm)


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
    return coordinate_radio_change({'desired': {iface: state}}, node_ip)


def coordinate_radio_change(actions, node_ip='all'):
    """Stage a radio change over Alfred and apply it once everyone has ACKed.

    This is the only way a change reaches another node. Targeting one node
    still goes through Alfred — the difference is the `targets` list, not the
    transport, so there is no second code path that talks to peers directly.
    """
    targets = 'all'
    if node_ip != 'all':
        try:
            targets = radio_target_for_node(node_ip)
        except ValueError as e:
            return {'ok': False, 'error': str(e)}

    expected = radio_expected_hosts() if targets == 'all' else list(targets)
    if not expected:
        return {'ok': False, 'error': 'No reachable nodes in registry'}
    if targets == 'all' and len(expected) < 2:
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
    }
    pkg.update(actions)
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
    cap = get_iface_txpower_cap(iface)
    if cap:
        info['txpower_cap_dbm'] = cap
        info['txpower_options_dbm'] = txpower_options_for_iface(iface, cap, info.get('txpower_dbm', ''))
    else:
        info['txpower_options_dbm'] = []

    # HaLow (morse_usb): iw can report a regular Wi-Fi channel; Morse driver is the runtime source.
    if iface == 'wlan2':
        info.update(get_halow_driver_info(iface))

    return info

# ─────────────────────────────────────────────────────────────────────────────
# Topology
# ─────────────────────────────────────────────────────────────────────────────
def parse_json_field(text):
    """Decode a JSON list carried through the registry; [] on anything odd."""
    try:
        value = json.loads(text or '[]')
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []


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
            # Straight from the registry. Peers publish their interface list
            # over Alfred, so this needs no round trip and works for a node
            # that is momentarily unreachable but still replicating.
            node_info['interfaces'] = {
                i['name']: {
                    'active': i.get('state') == 'UP' and i.get('role') == 'mesh',
                    'channel': '',
                    'freq_mhz': '',
                    'txpower_dbm': '',
                    'txpower_cap_dbm': '',
                    'txpower_options_dbm': [],
                    'tx_mcs': mcs_map.get(i['name'], {}).get('tx_mcs', ''),
                    'rx_mcs': mcs_map.get(i['name'], {}).get('rx_mcs', ''),
                    'halow_bw': '',
                    'halow_source': '',
                }
                for i in parse_json_field(nd.get('INTERFACES_JSON', ''))
                if i.get('name') in ('wlan0', 'wlan1', 'wlan2')
            }

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
    ])
    for r in results:
        iperf = r.get('iperf3_result', {})
        ping  = r.get('ping_result', {})
        metrics = extract_iperf3_metrics(iperf)
        ping_loss = ping.get('loss_pct', '') if ping else ''
        loss_value = metrics['loss_pct'] if metrics['loss_pct'] is not None else ping_loss
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

def run_local_ping(target, count=100, interval=0.2):
    """Ping a peer from this node. Returns the parsed summary, or None."""
    if not target:
        return None
    try:
        r = subprocess.run(['ping', '-c', str(count), '-i', str(interval), target],
                           capture_output=True, text=True,
                           timeout=count * interval + 10)
    except Exception:
        return None
    rtt = re.search(r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', r.stdout)
    loss = re.search(r'(\d+)% packet loss', r.stdout)
    return {
        'output':   r.stdout,
        'rtt_min':  float(rtt.group(1)) if rtt else None,
        'rtt_avg':  float(rtt.group(2)) if rtt else None,
        'rtt_max':  float(rtt.group(3)) if rtt else None,
        'rtt_mdev': float(rtt.group(4)) if rtt else None,
        'loss_pct': int(loss.group(1)) if loss else None,
    }


def run_local_iperf3(server_ip, test_type, duration, bitrate,
                     parallel=1, reverse=False):
    """Run iperf3 from this node against a peer's always-on daemon.

    Returns (ok, parsed_json, error).
    """
    cmd = ['iperf3', '-c', server_ip, '-t', str(duration), '-J']
    if test_type in ('udp_throughput', 'udp_jitter', 'packet_loss'):
        cmd += ['-u', '-b', bitrate]
    if parallel > 1:
        cmd += ['-P', str(parallel)]
    if reverse:
        cmd += ['-R']
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 25)
    try:
        payload = json.loads(r.stdout)
    except Exception:
        payload = {'raw': r.stdout, 'stderr': r.stderr}
    if payload.get('error'):
        return False, payload, payload['error']
    return r.returncode == 0, payload, r.stderr.strip()


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
                hop_count, hop_source = get_session_hop_count(src_ip, dst_ip)
                result_record['hop_count'] = hop_count
                result_record['hop_count_source'] = hop_source

                if test_type == 'icmp_ping':
                    result_record['ping_result'] = run_local_ping(dst_ip)
                    result_record['ok'] = result_record['ping_result'] is not None
                    if not result_record['ok']:
                        result_record['error'] = 'ping failed'
                else:
                    # iperf3 runs as a daemon on every node, so there is no
                    # server to start or stop remotely — just point a local
                    # client at the peer that is already listening.
                    try:
                        ok, payload, err = run_local_iperf3(
                            dst_ip, test_type, duration, udp_bitrate,
                            parallel=4 if test_type == 'tcp_4stream' else 1,
                            reverse=test_type == 'reverse')
                        result_record['iperf3_result'] = payload
                        result_record['ok'] = ok
                        if not ok:
                            result_record['error'] = err or 'iperf client failed'
                    except Exception as e:
                        result_record['ok'] = False
                        result_record['error'] = str(e)

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
.fer-lockup{display:flex;align-items:center;justify-content:flex-start;height:58px;padding-right:12px;border-right:1px solid var(--border);color:var(--fer-black);overflow:hidden;flex:0 0 auto}
/* the badge is near-square, so height drives the size and width follows the
   aspect ratio — a fixed width would letterbox it and leave dead space */
.fer-logo-img{display:block;width:auto;height:44px;max-width:100%;object-fit:contain;object-position:left center;filter:none;transition:height .18s ease}
:root[data-theme="dark"] .fer-lockup{color:#ffffff}
/* no brightness(0) invert(1) here — that flattens the badge to a solid silhouette */
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
  body.chrome-compact .fer-logo-img{height:34px}
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
input[type=text],input[type=password],input[type=number],select{
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

.footer-actions{display:flex;justify-content:flex-end;padding:8px 0 6px}
.logout-link-btn{border:1px solid rgba(180,35,24,.26);background:rgba(180,35,24,.05);color:#b42318;border-radius:999px;padding:8px 14px;font-family:var(--font);font-size:11px;font-weight:850;cursor:pointer;transition:background .18s ease,color .18s ease,box-shadow .18s ease,transform .18s ease}
.logout-link-btn:hover{background:#b42318;color:#ffffff;box-shadow:0 8px 20px rgba(180,35,24,.16);transform:translateY(-1px)}
:root[data-theme="dark"] .logout-link-btn{border-color:rgba(239,68,68,.34);background:rgba(239,68,68,.08);color:#ffb4b4}
:root[data-theme="dark"] .logout-link-btn:hover{background:#ef4444;color:#ffffff;box-shadow:0 8px 20px rgba(239,68,68,.22)}

@media (max-width: 620px) {
  html,body{font-size:12px}
  #hdr{position:relative;padding:10px 12px;gap:6px;align-items:flex-start;flex-wrap:wrap}
  .fer-lockup{order:1;min-width:clamp(92px,24vw,132px);width:clamp(92px,24vw,132px);height:46px;padding-right:4px}
  .fer-logo-img{height:36px}
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
  input[type=text],input[type=password],input[type=number],select{width:100%!important;min-width:0}
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
// This page is served under /manage, and its own routes live under that
// prefix. Worked out from the URL rather than baked in, so the page still
// functions if the prefix ever moves.
const MANAGE_BASE = (() => {
  const m = window.location.pathname.match(/^(\/[^/]*manage)(\/|$)/);
  return m ? m[1] : '';
})();
// U() is for routes this page owns. /api/admin/* is served at the site root by
// mesh-status.py and must NOT be prefixed.
//
// Any new fetch to a route in this file has to go through U(). Without it the
// request lands on mesh-status.py's root router, which answers 404 with the
// text "Not found" — and the caller dies in JSON.parse rather than saying so.
function U(path) { return MANAGE_BASE + path; }

const VALID_TABS = ['topology','radio','measure','sessions','uplink', 'config'];
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
    const r = await fetch(U('/api/topology'));
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
  return [normalizeDbm(num)];
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
  if (name === 'uplink') loadUsbWifiUplink();
  if (name === 'config') startConfigPolling(); else stopConfigPolling();
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
    const r = await fetch(U('/api/interface/toggle'), {
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
        setTimeout(() => { hideOverlay(); fetchTopo(); }, 3000);
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
  const r = await fetch(U('/api/txpower'), {
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
    const r = await fetch(U('/api/interface/toggle'), {
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
    const r = await fetch(U('/api/halow/channel'), {
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
    const r = await fetch(U('/api/wifi/channel'), {
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
    const r = await fetch(U('/api/wifi/channel'), {
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
async function loadUsbWifiUplink() {
  try {
    const r = await fetch(U('/api/uplink/wifi'));
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    const ssid = document.getElementById('uplink-wifi-ssid');
    const pass = document.getElementById('uplink-wifi-password');
    const status = document.getElementById('uplink-wifi-status');
    if (ssid && !ssid.value) ssid.value = d.ssid || 'hotspot';
    if (pass && !pass.value) pass.value = 'raspberry';
    if (status) {
      const parts = [];
      parts.push(d.iface ? `iface ${d.iface}` : 'no USB Wi-Fi adapter detected');
      parts.push(d.state || 'unknown');
      if (d.ip) parts.push(d.ip);
      if (d.service) parts.push(`service ${d.service}`);
      if (d.error) parts.push(d.error);
      status.textContent = parts.join(' / ');
      status.style.color = d.connected ? 'var(--green)' : 'var(--muted)';
    }
  } catch (e) {
    const status = document.getElementById('uplink-wifi-status');
    if (status) {
      status.textContent = 'Error: ' + e.message;
      status.style.color = 'var(--red)';
    }
  }
}

async function applyUsbWifiUplink() {
  const ssid = document.getElementById('uplink-wifi-ssid').value.trim() || 'hotspot';
  const password = document.getElementById('uplink-wifi-password').value || 'raspberry';
  const btn = document.getElementById('btn-uplink-wifi-apply');
  if (btn) { btn.disabled = true; btn.textContent = 'APPLYING...'; }
  showMsg(`Configuring USB Wi-Fi uplink for SSID ${ssid}...`, 'info');
  try {
    const r = await fetch(U('/api/uplink/wifi'), {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ssid, password, enabled: true})
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
    showMsg(`USB Wi-Fi uplink saved for ${ssid}`, 'ok');
    await loadUsbWifiUplink();
    await fetchTopo();
  } catch (e) {
    showMsg('USB Wi-Fi uplink failed: ' + e.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'APPLY'; }
  }
}

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
    const r = await fetch(U('/api/measure/start'), {
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
    const r = await fetch(U('/api/measure/status'));
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
  const r = await fetch(U('/api/sessions'));
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
        <a href="${U(`/api/sessions/${encodeURIComponent(s.label)}/csv`)}" download="${s.label}.csv"
           class="btn" style="text-decoration:none;font-size:10px">CSV</a>
        <a href="${U(`/api/sessions/${encodeURIComponent(s.label)}`)}" target="_blank"
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
    const r = await fetch(U(`/api/sessions/${encodedLabel}`), {method: 'DELETE'});
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
    showMsg(`Deleted session ${label}`, 'ok');
    await loadSessions();
  } catch (e) {
    showMsg('Delete failed: ' + e.message, 'err');
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
};

window.addEventListener('hashchange', () => showTab(getInitialTab(), false));
window.addEventListener('scroll', updateChromeCompact, {passive: true});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) autoRefreshUi();
});
"""

# ─────────────────────────────────────────────────────────────────────────────
# NODE CONFIG tab
# ─────────────────────────────────────────────────────────────────────────────
# Was the standalone /admin page on port 80 with no authentication at all,
# handing out the mesh SAE key in a text field. Same staging/ACK/apply flow,
# now a tab in here behind the admin password.
#
# Held as plain strings and substituted into the dashboard template, rather
# than written inline: the template is an f-string, and CSS/JS braces would
# have to be doubled throughout if this lived inside it.

CONFIG_TAB_CSS = r"""

body { overflow-y: auto; }
/* ── Admin layout ── */
.admin-body { display: flex; gap: 0; height: calc(100vh - 34px); overflow: hidden; }
.admin-form-col { flex: 1; overflow-y: auto; padding: 20px 24px; border-right: 1px solid var(--border); }
.admin-status-col { width: 320px; flex-shrink: 0; overflow-y: auto; padding: 16px; background: #ffffff; }
.admin-col-hdr { font-size: 11px; color: var(--muted); font-weight: 800; letter-spacing: 0; text-transform: none;
                 padding-bottom: 10px; border-bottom: 1px solid var(--border); margin-bottom: 14px; }
/* Section styling */
.cfg-section { margin-bottom: 22px; }
.cfg-section-title { font-size: 12px; color: var(--text); font-weight: 800; letter-spacing: 0; text-transform: none;
                      padding: 0 0 8px 0; border-bottom: 1px solid var(--border); margin-bottom: 12px; }
.cfg-row { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 10px; }
.cfg-row label { flex: 0 0 180px; font-size: 11px; color: var(--muted); padding-top: 6px; }
.cfg-row .hint { display: block; font-size: 9px; color: var(--muted); margin-top: 2px; }
.cfg-row input[type=text], .cfg-row input[type=password], .cfg-row select {
  flex: 1; background: #ffffff; border: 1px solid var(--border); border-radius: 8px;
  color: var(--text); font-family: var(--font); font-size: 12px;
  padding: 5px 8px; outline: none; }
.cfg-row input:focus, .cfg-row select:focus { border-color: var(--accent); }
.cfg-row input[type=checkbox] { width: 16px; height: 16px; margin-top: 6px; accent-color: var(--accent); }
/* Danger badge on dangerous fields */
.danger-badge { font-size: 9px; font-weight: bold; color: var(--bad); background: #ef444415;
                border: 1px solid #ef444430; border-radius: 2px; padding: 1px 5px;
                margin-left: 6px; vertical-align: middle; letter-spacing: .5px; }
/* Action buttons */
.admin-actions { display: flex; gap: 10px; margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--border); }
.cfg-btn { padding: 9px 16px; border-radius: 8px; font-size: 12px; font-weight: 850; letter-spacing: 0;
       font-family: var(--font); cursor: pointer; border: 1px solid var(--accent); text-transform: none; background:var(--surface); color:var(--accent); transition:all .15s; }
.cfg-btn-stage  { background: var(--surface); color: var(--accent); border-color: var(--accent); }
.cfg-btn-stage:hover:not(:disabled)  { background: var(--accent); color: #ffffff; }
.cfg-btn-apply  { background: var(--accent2); color: var(--fer-black); border-color: var(--accent2); }
.cfg-btn-apply:hover:not(:disabled)  { background: var(--fer-black); color: #ffffff; border-color: var(--fer-black); }
.cfg-btn-force  { background: rgba(236,176,0,.10); color: var(--text); border-color: rgba(236,176,0,.48); }
.cfg-btn-force:hover:not(:disabled)  { background: var(--accent2); color: var(--fer-black); border-color: var(--accent2); }
.cfg-btn-cancel { background: transparent; color: #b42318; border-color: #e9b2ad; }
.cfg-btn-cancel:hover:not(:disabled) { background: #b42318; color: #ffffff; border-color: #b42318; }
:root[data-theme="dark"] .cfg-btn-stage { background: transparent; color: var(--accent2); border-color: var(--accent2); }
:root[data-theme="dark"] .cfg-btn-stage:hover:not(:disabled), :root[data-theme="dark"] .cfg-btn-apply:hover:not(:disabled), :root[data-theme="dark"] .cfg-btn-force:hover:not(:disabled) { background:#f8f6ef; color:var(--fer-black); border-color:#f8f6ef; }
:root[data-theme="dark"] .cfg-btn-apply { background: var(--accent2); color: var(--fer-black); border-color: var(--accent2); }
:root[data-theme="dark"] .cfg-btn-force { background: rgba(236,176,0,.10); color: var(--accent2); border-color: rgba(236,176,0,.48); }
:root[data-theme="dark"] .cfg-btn-cancel { color:#fca5a5; border-color:#7f1d1d; }
:root[data-theme="dark"] .cfg-btn-cancel:hover:not(:disabled) { background:#b42318; color:#ffffff; border-color:#b42318; }
.cfg-btn:disabled { opacity: 0.35; cursor: not-allowed; }
/* Status column — node ACK table */
.ack-table { width: 100%; border-collapse: collapse; }
.ack-table th { font-size: 9px; color: var(--muted); text-align: left; padding: 4px 6px;
                letter-spacing: .8px; text-transform: uppercase; border-bottom: 1px solid var(--border); }
.ack-table td { font-size: 11px; padding: 6px; border-bottom: 1px solid #edf0f4; }
.ack-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 5px; }
.ack-dot-yes  { background: var(--good); }
.ack-dot-no   { background: var(--muted); }
.ack-dot-self { background: var(--accent); }
/* Pending config info box */
.pending-box { background: #f8fafc; border: 1px solid var(--border); border-radius: 8px;
               padding: 10px 12px; margin-bottom: 14px; }
.pending-box.pending-active { border-color: #f59e0b40; background: #f59e0b08; }
.pending-label { font-size: 9px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
.pending-version { font-size: 12px; font-family: var(--font); font-weight: 800; color: var(--accent); }
.pending-stat { font-size: 10px; color: var(--muted); margin-top: 4px; }
.pending-stat span { color: var(--text); }
/* Progress bar */
.ack-progress { height: 4px; background: var(--border); border-radius: 2px; margin-top: 8px; }
.ack-progress-bar { height: 100%; border-radius: 2px; background: var(--good); transition: width .5s; }
/* Warning modal */
#cfg-force-modal { display:none; position:fixed; top:0;left:0;right:0;bottom:0;
               background:#000a; z-index:100; align-items:center; justify-content:center; }
#cfg-force-modal.show { display:flex; }
.modal-box { background: var(--surface); border: 1px solid #ef444440; border-radius: 4px;
             padding: 24px; max-width: 380px; }
.modal-title { color: var(--bad); font-size: 14px; font-weight: bold; margin-bottom: 10px; }
.modal-body { color: var(--muted); font-size: 12px; line-height: 1.6; margin-bottom: 16px; }
.modal-actions { display:flex; gap:10px; justify-content:flex-end; }
/* Toast */
#cfg-toast { position:fixed; bottom:20px; right:20px; background: var(--surface); border:1px solid var(--border);
         border-radius:4px; padding:10px 16px; font-size:12px; opacity:0; transition:opacity .3s;
         pointer-events:none; z-index:200; }
#cfg-toast.show { opacity:1; }
#cfg-toast.toast-ok   { border-color:#22c55e60; color:var(--good); }
#cfg-toast.toast-err  { border-color:#ef444460; color:var(--bad); }
#cfg-toast.toast-warn { border-color:#f9731660; color:var(--warn); }
"""

CONFIG_TAB_HTML = r"""
<div class="admin-body">
    <!-- ── Left: Config Form ── -->
    <div class="admin-form-col">

      <div class="cfg-section">
        <div class="cfg-section-title">EUD / Client Connection</div>

        <div class="cfg-row">
          <label>Connection Mode<span class="hint">How clients connect to this node</span></label>
          <select id="f-eud">
            <option value="wired">Wired (USB/Ethernet)</option>
            <option value="wireless">Wireless (5GHz AP)</option>
            <option value="auto">Auto (Wireless unless wired)</option>
          </select>
        </div>
        <div class="cfg-row">
          <label>AP SSID<span class="hint">WiFi network name for clients</span></label>
          <input type="text" id="f-lan-ap-ssid">
        </div>
        <div class="cfg-row">
          <label>AP Password<span class="hint">Client WiFi password</span></label>
          <input type="password" id="f-lan-ap-key" autocomplete="new-password">
        </div>
        <div class="cfg-row">
          <label>Max EUDs per Node<span class="hint">Max concurrent client devices</span></label>
          <input type="text" id="f-max-euds">
        </div>
      </div>

      <div class="cfg-section">
        <div class="cfg-section-title">Mesh Network</div>

        <div class="cfg-row">
          <label>Mesh SSID<span class="hint">BATMAN mesh network name
            <span class="danger-badge">⚠ DANGEROUS</span></span></label>
          <input type="text" id="f-mesh-ssid">
        </div>
        <div class="cfg-row">
          <label>Mesh SAE Key<span class="hint">WPA3-SAE passphrase
            <span class="danger-badge">⚠ DANGEROUS</span></span></label>
          <input type="password" id="f-mesh-key" autocomplete="new-password">
        </div>
        <div class="cfg-row">
          <label>IP Range (CIDR)<span class="hint">Mesh network address space
            <span class="danger-badge">⚠ DANGEROUS</span></span></label>
          <input type="text" id="f-ipv4-network">
        </div>
        <div class="cfg-row">
          <label>Regulatory Domain<span class="hint">2-letter country code for RF</span></label>
          <input type="text" id="f-regulatory-domain" maxlength="2" style="width:48px;flex:none;">
        </div>
        <div class="cfg-row">
          <label>Auto Channel<span class="hint">Scan and select best channel</span></label>
          <input type="checkbox" id="f-acs">
        </div>
      </div>

      <div class="cfg-section">
        <div class="cfg-section-title">Services</div>

        <div class="cfg-row">
          <label>MediaMTX<span class="hint">RTSP/WebRTC streaming</span></label>
          <input type="checkbox" id="f-mtx">
        </div>
        <div class="cfg-row">
          <label>Mumble<span class="hint">Voice communications</span></label>
          <input type="checkbox" id="f-mumble">
        </div>
        <div class="cfg-row">
          <label>Auto Update<span class="hint">Automatic MANET tool updates</span></label>
          <input type="checkbox" id="f-auto-update">
        </div>
      </div>

      <div class="cfg-section">
        <div class="cfg-section-title">Security</div>

        <div class="cfg-row">
          <label>Admin Password<span class="hint">This admin interface</span></label>
          <input type="password" id="f-admin-password" autocomplete="new-password">
        </div>
      </div>

      <div class="admin-actions">
        <button class="cfg-btn cfg-btn-stage"  id="btn-stage"  onclick="stageChanges()">Stage Changes</button>
        <button class="cfg-btn cfg-btn-apply"  id="btn-apply"  onclick="applyChanges(false)" disabled>Apply Now</button>
        <button class="cfg-btn cfg-btn-force"  id="btn-force"  onclick="showForceModal()" style="display:none">Force Apply</button>
        <button class="cfg-btn cfg-btn-cancel" id="btn-cancel" onclick="cancelPending()" style="display:none">Cancel</button>
      </div>
      <div id="action-msg" style="font-size:10px;color:var(--muted);margin-top:8px;min-height:14px;"></div>
    </div>

    <!-- ── Right: Deployment Status ── -->
    <div class="admin-status-col">
      <div class="admin-col-hdr">Deployment Status</div>

      <!-- Pending config box -->
      <div class="pending-box" id="pending-box">
        <div class="pending-label">Pending Config</div>
        <div id="pending-version" class="pending-version">None</div>
        <div id="pending-stat"   class="pending-stat" style="display:none"></div>
        <div id="ack-progress-wrap" style="display:none">
          <div class="ack-progress"><div class="ack-progress-bar" id="ack-bar" style="width:0%"></div></div>
        </div>
      </div>

      <!-- Node ACK table -->
      <div class="admin-col-hdr" style="margin-top:14px">Nodes (<span id="node-count">—</span>)</div>
      <table class="ack-table" id="ack-table">
        <thead><tr><th>Node</th><th>IP</th><th>ACK</th></tr></thead>
        <tbody id="ack-tbody"></tbody>
      </table>

      <!-- Dangerous change warning -->
      <div id="danger-warn" style="display:none;margin-top:14px;padding:10px;
           background:#ef444408;border:1px solid #ef444430;border-radius:3px;
           font-size:10px;color:var(--bad);line-height:1.6;">
        ⚠ Staged changes include <strong>DANGEROUS</strong> settings (mesh SSID, key, or IP range).
        All nodes will briefly disconnect while applying. Ensure 100% ACK before applying.
        <label style="display:flex;align-items:center;gap:6px;margin-top:8px;color:var(--muted);cursor:pointer">
          <input type="checkbox" id="f-no-rollback" style="width:14px;height:14px;margin:0">
          Skip the safety net — keep the change even if the mesh does not come back
        </label>
      </div>

      <!-- Rollback armed on this node -->
      <div id="rollback-box" style="display:none;margin-top:14px;padding:10px;
           background:#f59e0b0d;border:1px solid #f59e0b40;border-radius:3px;
           font-size:10px;color:var(--text);line-height:1.6;">
        <strong>Rollback armed</strong>
        <div id="rollback-detail" style="color:var(--muted);margin-top:4px"></div>
      </div>
    </div>
  </div>

  <!-- Force apply modal -->
  <div id="cfg-force-modal">
    <div class="modal-box">
      <div class="modal-title">⚠ Force Apply</div>
      <div class="modal-body" id="force-modal-body">
        Not all nodes have acknowledged the pending config.
        Forcing apply will push changes to this node only — unreachable nodes
        will remain on the old config and may need manual intervention.
      </div>
      <div class="modal-actions">
        <button class="cfg-btn cfg-btn-cancel" onclick="closeForceModal()">Cancel</button>
        <button class="cfg-btn cfg-btn-force"  onclick="applyChanges(true)">Force Apply</button>
      </div>
    </div>
  </div>

  <div id="cfg-toast"></div>
"""

CONFIG_TAB_JS = r"""

const POLL_MS = 5000;
let STATUS = null;
let pollTimer = null;

// ── Init ────────────────────────────────────────────────────────────────────
// Polls only while the NODE CONFIG tab is on screen — this is a 5 s poll and
// there is no reason to run it while the operator is looking at the topology.
function startConfigPolling() {
  if (pollTimer) return;
  refreshStatus();
  pollTimer = setInterval(refreshStatus, POLL_MS);
}
function stopConfigPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

// ── Fetch status ─────────────────────────────────────────────────────────────
async function refreshStatus() {
  try {
    const r = await fetch('/api/admin/status');
    if (!r.ok) return;
    STATUS = await r.json();
    renderStatus(STATUS);
  } catch(e) {}
}

// ── Populate form from current config ────────────────────────────────────────
function populateForm(cfg) {
  setVal('f-eud',              cfg.eud              || 'wired');
  setVal('f-lan-ap-ssid',      cfg.lan_ap_ssid      || '');
  setVal('f-lan-ap-key',       cfg.lan_ap_key        || '');
  setVal('f-max-euds',         cfg.max_euds_per_node || '');
  setVal('f-mesh-ssid',        cfg.mesh_ssid         || '');
  setVal('f-mesh-key',         cfg.mesh_key          || '');
  setVal('f-ipv4-network',     cfg.ipv4_network      || '');
  setVal('f-regulatory-domain',cfg.regulatory_domain || 'US');
  setChk('f-acs',              cfg.acs        === 'y');
  setChk('f-mtx',              cfg.mtx        === 'y');
  setChk('f-mumble',           cfg.mumble     === 'y');
  setChk('f-auto-update',      cfg.auto_update=== 'y');
  setVal('f-admin-password',   cfg.admin_password    || '');
}

function setVal(id, v) { const el = document.getElementById(id); if (el) el.value = v; }
function setChk(id, v) { const el = document.getElementById(id); if (el) el.checked = v; }
function getVal(id)    { const el = document.getElementById(id); return el ? el.value.trim() : ''; }
function getChk(id)    { const el = document.getElementById(id); return el ? el.checked : false; }

// ── Render status panel ───────────────────────────────────────────────────────
let formPopulated = false;
function renderStatus(s) {
  // Populate form once from live config
  if (!formPopulated && s.current_config) {
    populateForm(s.current_config);
    formPopulated = true;
  }

  document.getElementById('node-count').textContent = s.total_nodes || '0';

  const pending  = s.pending;
  const pBox     = document.getElementById('pending-box');
  const pVersion = document.getElementById('pending-version');
  const pStat    = document.getElementById('pending-stat');
  const pWrap    = document.getElementById('ack-progress-wrap');
  const ackBar   = document.getElementById('ack-bar');
  const rb = s.rollback || {};
  const rbBox = document.getElementById('rollback-box');
  if (rbBox) {
    rbBox.style.display = rb.armed ? '' : 'none';
    if (rb.armed) {
      const left = rb.seconds_left > 0
        ? `${Math.ceil(rb.seconds_left / 60)} min remaining`
        : 'deadline passed, deciding on the next cycle';
      document.getElementById('rollback-detail').textContent =
        `Version ${(rb.version || '').slice(0, 8)} is on trial. This node had ` +
        `${rb.peers_before} peer(s) before the change; if none are back by the ` +
        `deadline it restores the previous config by itself. ${left}.`;
    }
  }

  const dangerWarn = document.getElementById('danger-warn');
  const btnApply = document.getElementById('btn-apply');
  const btnForce = document.getElementById('btn-force');
  const btnCancel= document.getElementById('btn-cancel');

  if (pending && pending.version) {
    pBox.className = 'pending-box pending-active';
    pVersion.textContent = 'v' + pending.version;

    const nodes = s.nodes || [];
    const acked = nodes.filter(n => n.ack === pending.version).length;
    const total = nodes.length;
    const pct   = total > 0 ? Math.round(acked / total * 100) : 0;

    pStat.style.display = '';
    pStat.innerHTML = `<span>${acked}/${total}</span> nodes ACKed &nbsp; <span>${pct}%</span>`;
    pWrap.style.display = '';
    ackBar.style.width = pct + '%';
    ackBar.style.background = acked === total ? 'var(--good)' : 'var(--warn)';

    const isDangerous = pending.dangerous === true;
    dangerWarn.style.display = isDangerous ? '' : 'none';

    const allAcked = acked === total && total > 0;
    btnApply.disabled = !allAcked;
    btnApply.style.display = '';
    btnForce.style.display = !allAcked ? '' : 'none';
    btnCancel.style.display = '';

    const activateAt = pending.activate_at || 0;
    if (activateAt > 0) {
      const secs = Math.max(0, activateAt - Math.floor(Date.now() / 1000));
      document.getElementById('action-msg').textContent =
        secs > 0 ? `Applying in ${secs}s...` : 'Applying now...';
    }
  } else {
    pBox.className = 'pending-box';
    pVersion.textContent = 'None';
    pStat.style.display = 'none';
    pWrap.style.display = 'none';
    dangerWarn.style.display = 'none';
    btnApply.disabled = true;
    btnApply.style.display = 'none';
    btnForce.style.display = 'none';
    btnCancel.style.display = 'none';
    document.getElementById('action-msg').textContent = '';
  }

  // Node ACK table
  const tbody = document.getElementById('ack-tbody');
  tbody.innerHTML = (s.nodes || []).map(n => {
    const pendingVer = pending && pending.version;
    const isAcked    = pendingVer && n.ack === pendingVer;
    const isSelf     = n.hostname === (STATUS && STATUS.my_hostname);
    let dotCls, ackLabel;
    if (!pendingVer) {
      dotCls   = 'ack-dot-self';
      ackLabel = '—';
    } else if (isSelf && isAcked) {
      dotCls   = 'ack-dot-self';
      ackLabel = 'Self ✓';
    } else if (isAcked) {
      dotCls   = 'ack-dot-yes';
      ackLabel = '✓';
    } else {
      dotCls   = 'ack-dot-no';
      ackLabel = 'Waiting';
    }
    const staleMs = (DATA.timestamp - parseInt(n.last_seen || 0));
    const stale   = staleMs > 300;
    const nameStyle = stale ? 'color:var(--muted)' : '';
    return `<tr>
      <td style="${nameStyle}">${n.hostname}</td>
      <td style="color:var(--muted);font-size:10px">${n.ip}</td>
      <td><span class="ack-dot ${dotCls}"></span>${ackLabel}</td>
    </tr>`;
  }).join('');
}

// ── Read form into config object ──────────────────────────────────────────────
function readForm() {
  return {
    eud:               getVal('f-eud'),
    lan_ap_ssid:       getVal('f-lan-ap-ssid'),
    lan_ap_key:        getVal('f-lan-ap-key'),
    max_euds_per_node: getVal('f-max-euds'),
    mesh_ssid:         getVal('f-mesh-ssid'),
    mesh_key:          getVal('f-mesh-key'),
    ipv4_network:      getVal('f-ipv4-network'),
    regulatory_domain: getVal('f-regulatory-domain'),
    acs:               getChk('f-acs')          ? 'y' : 'n',
    mtx:               getChk('f-mtx')          ? 'y' : 'n',
    mumble:            getChk('f-mumble')        ? 'y' : 'n',
    auto_update:       getChk('f-auto-update')   ? 'y' : 'n',
    admin_password:    getVal('f-admin-password'),
  };
}

// ── Stage changes ─────────────────────────────────────────────────────────────
async function stageChanges() {
  const cfg = readForm();
  cfgShowMsg('Staging...', 'muted');
  try {
    const r   = await fetch('/api/admin/stage', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({config: cfg})
    });
    const res = await r.json();
    if (r.ok && res.ok) {
      toast('Changes staged — waiting for nodes to ACK', 'ok');
      cfgShowMsg('Staged. Waiting for ' + (STATUS && STATUS.total_nodes || '?') + ' nodes to ACK.', 'ok');
      await refreshStatus();
    } else {
      toast('Stage failed: ' + (res.error || r.status), 'err');
      cfgShowMsg('Stage failed.', 'err');
    }
  } catch(e) {
    toast('Stage error: ' + e, 'err');
  }
}

// ── Apply changes ─────────────────────────────────────────────────────────────
async function applyChanges(force) {
  closeForceModal();
  cfgShowMsg('Sending activate signal...', 'ok');
  try {
    const r   = await fetch('/api/admin/activate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({force: force, no_rollback: skipRollback()})
    });
    const res = await r.json();
    if (r.ok && res.ok) {
      toast('Activate signal sent — applying in 60s', 'ok');
      cfgShowMsg(res.no_rollback
        ? 'All nodes will apply in ~60s. Safety net skipped — this change is permanent.'
        : 'All nodes will apply in ~60s. Dangerous changes roll back automatically if the mesh does not re-form.',
        'ok');
      await refreshStatus();
    } else {
      toast('Activate failed: ' + (res.error || r.status), 'err');
    }
  } catch(e) {
    toast('Activate error: ' + e, 'err');
  }
}

// ── Cancel pending ────────────────────────────────────────────────────────────
async function cancelPending() {
  try {
    const r   = await fetch('/api/admin/cancel', {method: 'POST'});
    const res = await r.json();
    if (r.ok && res.ok) {
      toast('Pending config cancelled', 'warn');
      cfgShowMsg('', '');
      await refreshStatus();
    }
  } catch(e) {}
}

function skipRollback() {
  const el = document.getElementById('f-no-rollback');
  return !!(el && el.checked);
}

// ── Force modal ───────────────────────────────────────────────────────────────
function showForceModal() {
  const s     = STATUS;
  const nodes = s && s.nodes || [];
  const pv    = s && s.pending && s.pending.version;
  const acked = pv ? nodes.filter(n => n.ack === pv).length : 0;
  const total = nodes.length;
  document.getElementById('force-modal-body').textContent =
    `${acked} of ${total} nodes have ACKed. Forcing will apply on all reachable nodes. ` +
    `Unreachable nodes (${total - acked}) will remain on the old config until they reconnect. ` +
    (skipRollback()
      ? 'The safety net is switched off: nodes will keep the change even if the mesh does not come back.'
      : 'The safety net still applies: a node that loses the mesh restores its previous config.');
  document.getElementById('cfg-force-modal').classList.add('show');
}
function closeForceModal() {
  document.getElementById('cfg-force-modal').classList.remove('show');
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function cfgShowMsg(msg, cls) {
  const el = document.getElementById('action-msg');
  el.textContent = msg;
  el.style.color = cls === 'ok' ? 'var(--good)' : cls === 'err' ? 'var(--bad)' : 'var(--muted)';
}

let toastTimer = null;
function toast(msg, cls) {
  const el = document.getElementById('cfg-toast');
  el.textContent = msg;
  el.className = 'show toast-' + cls;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.className = '', 3000);
}
"""


def render_dashboard():
    hostname = get_my_hostname()
    ip       = get_my_ip()
    logo_v   = logo_asset_token()
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MANET // MANAGE</title>
<style>{CSS}{CONFIG_TAB_CSS}
</style>
</head><body>

<div id="overlay" class="info">
  <div class="overlay-body">
    <div id="overlay-text"></div>
    <button id="overlay-close" onclick="hideOverlay()" aria-label="Close">&times;</button>
  </div>
</div>

<div id="hdr">
  <div class="fer-lockup" title="FER" aria-label="FER">
    <img class="fer-logo-img" src="/assets/fer-logo-black?v={logo_v}" data-light="/assets/fer-logo-black?v={logo_v}" data-dark="/assets/fer-logo-white?v={logo_v}" alt="FER">
  </div>
  <div id="hdr-logo">MANET//<span>MANAGE</span></div>
  <div id="hdr-node"><strong>{hostname}</strong> &nbsp;{ip}</div>
  <div id="hdr-right">
    <span id="hdr-inet" class="no">○ NO INET</span>
    <span id="hdr-clock"></span>
    <button id="overview-link" class="overview-link-btn" type="button" onclick="window.location.href='/?theme=' + encodeURIComponent(document.documentElement.dataset.theme || 'light')">OVERVIEW</button>
    <button id="theme-toggle" class="theme-toggle" type="button" onclick="toggleTheme()">Dark</button>
  </div>
</div>

<div id="page">
<div id="nav">
  <div class="tab active" data-tab="topology" onclick="showTab('topology')">TOPOLOGY</div>
  <div class="tab"        data-tab="radio"    onclick="showTab('radio')">RADIO CONFIG</div>
  <div class="tab"        data-tab="measure"    onclick="showTab('measure')">MEASURE</div>
  <div class="tab"        data-tab="sessions"   onclick="showTab('sessions')">SESSIONS</div>
  <div class="tab"        data-tab="uplink"     onclick="showTab('uplink')">UPLINK</div>
  <div class="tab"        data-tab="config"     onclick="showTab('config')">NODE CONFIG</div>
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
          <option value="3" selected>ch 3 (865.5 MHz)</option>
          <option value="4">ch 4 (866.5 MHz)</option>
          <option value="5">ch 5 (867.5 MHz)</option>
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
  <div id="tab-uplink" class="tab-pane" style="display:none">
    <div class="card">
      <div class="card-title">USB Wi-Fi Internet Uplink</div>
      <div class="row">
        <span class="row-label">SSID</span>
        <input type="text" id="uplink-wifi-ssid" value="hotspot" style="width:260px">
      </div>
      <div class="row">
        <span class="row-label">Password</span>
        <input type="password" id="uplink-wifi-password" value="raspberry" autocomplete="new-password" style="width:260px">
      </div>
      <div class="row">
        <span class="row-label">Status</span>
        <span id="uplink-wifi-status" style="font-size:11px;color:var(--muted)">Loading...</span>
      </div>
      <div class="row">
        <span class="row-label"></span>
        <button class="btn btn-green" id="btn-uplink-wifi-apply" onclick="applyUsbWifiUplink()">APPLY</button>
      </div>
    </div>
  </div>
  <!-- ── NODE CONFIG ── -->
  <!-- The old /admin page. Same staging/ACK/apply flow, now behind the same
       password as everything else here rather than open on port 80. -->
  <div id="tab-config" class="tab-pane" style="display:none">
{CONFIG_TAB_HTML}
  </div>

  <div class="footer-actions">
    <button class="logout-link-btn" type="button" onclick="window.location.href='/manage/logout'">LOGOUT</button>
  </div>
</div><!-- #content -->
</div><!-- #page -->

<script>{JS}
{CONFIG_TAB_JS}</script>
</body></html>"""

# ─────────────────────────────────────────────────────────────────────────────
# Request Handler
# ─────────────────────────────────────────────────────────────────────────────
ASSET_CONTENT_TYPES = {
    '.svg':  'image/svg+xml',
    '.png':  'image/png',
    '.jpg':  'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
}

def asset_content_type(path):
    ext = os.path.splitext(path)[1].lower()
    return ASSET_CONTENT_TYPES.get(ext, 'application/octet-stream')

def logo_asset_file(url_path):
    """Map /assets/fer-logo-white[.png|.svg] to the file we actually ship."""
    if not url_path.startswith('/assets/'):
        return None
    name = os.path.splitext(url_path[len('/assets/'):])[0]
    return FER_LOGO_ASSETS.get(name)

class ManageRoutes:
    """Mixed into mesh-status.py's handler; relies on its send_json/send_html.

    Route methods are prefixed so they never shadow the real do_* dispatch —
    mesh-status calls them only after the password cookie checks out.
    """
    def log_message(self, fmt, *args):
        pass  # suppress access logs

    def manage_do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/') or '/'

        logo_file = logo_asset_file(parsed.path)
        if logo_file:
            try:
                with open(logo_file, 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', asset_content_type(logo_file))
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

        elif path == '/api/uplink/wifi':
            self.send_json(get_usb_wifi_uplink_status())

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

    def manage_do_DELETE(self):
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

    def manage_do_POST(self):
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
                req = json.loads(body)
                self.send_json(coordinate_radio_change(
                    {'txpower': {req.get('iface', ''): req.get('dbm')}},
                    req.get('node_ip', 'all')))
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/halow/channel':
            # Every node has to land on the new channel together: when HaLow is
            # the only active mesh interface, the mesh is down between the first
            # node moving and the last. Alfred stages the change and applies it
            # at a common activate_at, which is why this can be done at all.
            try:
                req = json.loads(body)
                if not int(req.get('channel', 0)):
                    self.send_json({'ok': False, 'error': 'Missing channel'})
                    return
                self.send_json(coordinate_radio_change({'halow_channel': {
                    'channel': int(req['channel']),
                    'bw': req.get('bw', '1MHz'),
                    'dbm': req.get('dbm'),
                }}))
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/wifi/channel':
            try:
                req = json.loads(body)
                iface = req.get('interface', req.get('iface', ''))
                if iface not in ('wlan0', 'wlan1'):
                    self.send_json({'ok': False, 'error': 'Invalid Wi-Fi interface'})
                    return
                self.send_json(coordinate_radio_change({'wifi_channel': {
                    'iface': iface,
                    'channel': req.get('channel'),
                    'dbm': req.get('dbm'),
                }}))
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

        elif path == '/api/control/uplink_wifi':
            try:
                req = json.loads(body)
                status = apply_usb_wifi_uplink(
                    req.get('ssid', 'hotspot'),
                    req.get('password', 'raspberry'),
                    bool(req.get('enabled', True)),
                )
                status['ok'] = True
                self.send_json(status)
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/uplink/wifi':
            try:
                req = json.loads(body)
                result = apply_usb_wifi_uplink_all(
                    req.get('ssid', 'hotspot'),
                    req.get('password', 'raspberry'),
                    bool(req.get('enabled', True)),
                )
                self.send_json(result)
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found')
