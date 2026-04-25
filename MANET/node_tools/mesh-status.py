#!/usr/bin/env python3
"""
MANET Node Status Web Server
-----------------------------
Serves mesh network status and topology information on port 8080.

Access:
  /          - Public status page (localhost + mesh subnet)
  /api/data  - JSON data endpoint (same access control)
  /admin     - Admin config page (no Basic auth)

Reads:
  /etc/mesh.conf                - Node configuration
  /etc/mesh_ipv4_state          - Current IP state
  /var/run/mesh_node_registry   - Peer registry (built by mesh-registry-builder.sh)

Calls:
  batctl o   - Originator/TQ table
  batctl n   - Direct neighbors
  batctl gwl - Gateway list
"""

import http.server
import socketserver
import json
import subprocess
import re
import os
import ipaddress
import socket
import threading
import time
from urllib.parse import urlparse, parse_qs

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
REGISTRY_FILE   = "/var/run/mesh_node_registry"
MESH_CONF_FILE  = "/etc/mesh.conf"
MESH_STATE_FILE = "/etc/mesh_ipv4_state"
PORT            = 8080
REFRESH_MS      = 15000   # Status page polling interval (ms)
HALOW_EU_CHANNELS = [863500, 864500, 865500, 866500, 867500, 868500]
CONTROL_POST_PATHS = {
    '/api/control/interface',
    '/api/control/txpower',
    '/api/control/halow_channel',
    '/api/control/wifi_channel',
    '/api/iperf/server/start',
    '/api/iperf/server/stop',
    '/api/iperf/client/run',
    '/api/ping/run',
}

# ─────────────────────────────────────────────────────────────────────────────
# Config / State Loaders
# ─────────────────────────────────────────────────────────────────────────────
def load_kv_file(path):
    """Parse a key=value or key='value' file into a dict."""
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
        m = re.search(r's1g_prim_chwidth\s*=\s*(\d+)', txt)
        if m:
            info['halow_bw'] = {'0': '1MHz', '1': '2MHz', '2': '4MHz'}.get(m.group(1), m.group(1))
        if info:
            info['halow_source'] = 'config'
            return info
    return info

def wifi_channel_to_freq(iface, channel):
    try:
        ch = int(channel)
    except Exception:
        return None
    if iface == 'wlan0' and 1 <= ch <= 13:
        return 2407 + ch * 5
    if iface == 'wlan1':
        # Common 5 GHz channels; enough for manual dashboard control.
        if ch == 14:
            return 2484
        if 32 <= ch <= 177:
            return 5000 + ch * 5
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Registry Parser
# ─────────────────────────────────────────────────────────────────────────────
def parse_registry():
    """Parse /var/run/mesh_node_registry into a dict of node dicts."""
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

# ─────────────────────────────────────────────────────────────────────────────
# batctl Wrappers
# ─────────────────────────────────────────────────────────────────────────────
def norm_mac(mac):
    return mac.lower().replace('-', ':').strip()

def run_batctl_originators():
    """Parse `batctl o` into two structures:
      tq_map:   {mac -> best_tq_norm}  (indexes both orig + nexthop MACs)
      orig_map: {orig_mac -> {tq, nexthop}}  (best path per originator)

    BATMAN_V reports throughput in Mbit/s (>255); BATMAN_IV uses 0-255 LQ.
    Both are normalised to 0-255.
    """
    tq_map   = {}
    orig_map = {}  # orig_mac -> {'tq': int, 'nexthop': str}

    def _set_tq(mac, tq):
        if mac and (mac not in tq_map or tq > tq_map[mac]):
            tq_map[mac] = tq

    try:
        r = subprocess.run(['batctl', 'o', '-n'],
                           capture_output=True, text=True, timeout=5)
        orig_best = {}  # orig_mac -> (best_tq_float, nexthop_mac)
        for line in r.stdout.splitlines():
            m = re.match(
                r'[\s*]+([0-9a-f:]{17})\s+[\d.]+(?:ms|s)\s+\(\s*([\d.]+)\)\s+([0-9a-f:]{17})',
                line)
            if m:
                orig    = norm_mac(m.group(1))
                tq      = float(m.group(2))
                nexthop = norm_mac(m.group(3))
                prev = orig_best.get(orig)
                if prev is None or tq > prev[0]:
                    orig_best[orig] = (tq, nexthop)

        for orig, (tq, nexthop) in orig_best.items():
            tq_norm = int(min(tq / 1000 * 255, 255)) if tq > 255 else int(tq)
            _set_tq(orig, tq_norm)
            if nexthop != orig:
                _set_tq(nexthop, tq_norm)
            orig_map[orig] = {'tq': tq_norm, 'nexthop': nexthop}
    except Exception:
        pass
    return tq_map, orig_map

def run_batctl_neighbors():
    """Return list of {iface, mac, tq} from `batctl n`."""
    neighbors = []
    try:
        r = subprocess.run(['batctl', 'n', '-n'],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            # wlan0   aa:bb:cc:dd:ee:ff   0.500ms   (240)
            m = re.match(r'\s*(\S+)\s+([0-9a-f:]{17})\s+[\d.]+(?:ms|s)\s+\(\s*([\d.]+)\)', line)
            if m:
                raw_tq = float(m.group(3))
                tq_norm = int(min(raw_tq / 1000 * 255, 255)) if raw_tq > 255 else int(raw_tq)
                neighbors.append({
                    'iface': m.group(1),
                    'mac':   norm_mac(m.group(2)),
                    'tq':    tq_norm
                })
    except Exception:
        pass
    return neighbors

def run_batctl_gateways():
    """Return list of {mac, tq, selected} from `batctl gwl`.

    BATMAN_V format:
      => <gw_mac>  <age>s (  <Mbit/s>)  <nexthop_mac> [<if>]
    BATMAN_IV format:
      => <gw_mac>  <age>ms (<lq>)  <nexthop_mac> [<if>]
    Header lines and the self-node are skipped.
    """
    gateways = []
    try:
        r = subprocess.run(['batctl', 'gwl', '-n'],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            # Skip header / blank lines
            if not line.strip() or line.startswith('[') or line.strip().startswith('Router'):
                continue
            selected = line.lstrip().startswith('=>')
            # Extract first MAC on the line (the gateway's originator MAC)
            mac_m = re.search(r'([0-9a-f]{2}(?::[0-9a-f]{2}){5})', line)
            # Extract throughput/LQ: handles both "( 100.0)" and "(255)"
            tq_m  = re.search(r'\(\s*([\d.]+)\s*\)', line)
            if mac_m:
                raw_tq = float(tq_m.group(1)) if tq_m else 0.0
                tq_norm = int(min(raw_tq / 1000 * 255, 255)) if raw_tq > 255 else int(raw_tq)
                gateways.append({
                    'mac':      norm_mac(mac_m.group(1)),
                    'tq':       tq_norm,
                    'selected': selected,
                })
    except Exception:
        pass
    return gateways

def get_my_mac():
    try:
        r = subprocess.run(['cat', '/sys/class/net/bat0/address'],
                           capture_output=True, text=True, timeout=3)
        return norm_mac(r.stdout.strip())
    except Exception:
        return None

def get_my_hostname():
    try:
        return socket.gethostname()
    except Exception:
        return 'unknown'

# ─────────────────────────────────────────────────────────────────────────────
# Local Node Detail Gathering
# ─────────────────────────────────────────────────────────────────────────────
def get_battery():
    """Return battery dict from battery-reader.py output, or None.

    Dict keys: percentage, voltage_v, current_ma, power_w, charging, status, timestamp.
    Falls back to /sys/class/power_supply capacity (int) for backwards compat.
    """
    try:
        with open('/run/battery_status.json') as f:
            data = json.load(f)
        if data.get('percentage') is not None:
            return data
    except Exception:
        pass
    # Fallback: kernel power_supply sysfs (only present when I2C driver registers the device)
    for root, dirs, files in os.walk('/sys/class/power_supply'):
        for d in dirs:
            cap_path  = os.path.join(root, d, 'capacity')
            type_path = os.path.join(root, d, 'type')
            try:
                with open(type_path) as f:
                    if f.read().strip().lower() != 'battery':
                        continue
                with open(cap_path) as f:
                    return {'percentage': int(f.read().strip()), 'status': 'unknown',
                            'voltage_v': None, 'current_ma': None, 'power_w': None,
                            'charging': None, 'timestamp': None}
            except Exception:
                continue
    return None

def get_interfaces():
    """
    Return list of interface dicts with role, health, and fault details.

    Health values:
      'ok'      — up and doing its job
      'warn'    — up but something is degraded (e.g. wpa_supplicant stopped)
      'fault'   — interface is DOWN or not participating when it should be
      'info'    — informational (bridge, bat0, loopback — no health expectation)
    """
    ifaces = []
    try:
        r = subprocess.run(['ip', '-j', 'addr'], capture_output=True, text=True, timeout=5)
        raw = json.loads(r.stdout)
    except Exception:
        return ifaces

    # ── iw dev info ──
    iw_info = {}
    try:
        r2 = subprocess.run(['iw', 'dev'], capture_output=True, text=True, timeout=5)
        cur_iface = None
        for line in r2.stdout.splitlines():
            m = re.match(r'\s+Interface (\S+)', line)
            if m:
                cur_iface = m.group(1)
                iw_info[cur_iface] = {}
            if cur_iface:
                tm = re.search(r'type (\S+)', line)
                if tm: iw_info[cur_iface]['type'] = tm.group(1)
                sm = re.search(r'ssid (.+)', line)
                if sm: iw_info[cur_iface]['ssid'] = sm.group(1).strip()
                fm = re.search(r'channel (\d+).*MHz', line)
                if fm: iw_info[cur_iface]['channel'] = fm.group(1)
                freqm = re.search(r'([\d.]+) GHz', line)
                if freqm: iw_info[cur_iface]['freq'] = freqm.group(1)
                wm = re.search(r'wiphy (\d+)', line)
                if wm: iw_info[cur_iface]['wiphy'] = wm.group(1)
                dm = re.search(r'wdev (0x[0-9a-fA-F]+)', line)
                if dm: iw_info[cur_iface]['wiphy'] = str(int(dm.group(1), 16) >> 32)
    except Exception:
        pass

    # Build phy→band map: Band 1 = 2.4 GHz, Band 2 = 5 GHz (IEEE 802.11 convention)
    # A phy with both bands → 2.4 GHz takes precedence (dual-band chip)
    phy_band = {}  # wiphy_num_str -> '2.4 GHz' | '5 GHz'
    try:
        r3 = subprocess.run(['iw', 'phy'], capture_output=True, text=True, timeout=5)
        cur_phy = None
        for line in r3.stdout.splitlines():
            pm = re.match(r'Wiphy phy(\d+)', line)
            if pm:
                cur_phy = pm.group(1)
                continue
            if cur_phy is None:
                continue
            bh = re.match(r'\s+Band (\d+):', line)
            if bh:
                band_num = int(bh.group(1))
                if band_num == 1:
                    phy_band[cur_phy] = '2.4 GHz'  # Band 1 always 2.4 GHz
                elif band_num == 2 and cur_phy not in phy_band:
                    phy_band[cur_phy] = '5 GHz'    # Band 2 = 5 GHz, only if no Band 1
    except Exception:
        pass

    # Read driver via ethtool and assign band_label
    for iname in list(iw_info.keys()):
        driver = ''
        try:
            et = subprocess.run(['ethtool', '-i', iname], capture_output=True, text=True, timeout=3)
            for line in et.stdout.splitlines():
                if line.startswith('driver:'):
                    driver = line.split(':', 1)[1].strip()
                    break
        except Exception:
            pass
        iw_info[iname]['driver'] = driver
        if 'morse' in driver:
            iw_info[iname]['band_label'] = 'HaLow 900MHz'
        else:
            # Try runtime freq first, fall back to phy capability
            freq_str = iw_info[iname].get('freq', '')
            try:
                freq_f = float(freq_str)
                if freq_f < 2.0:
                    iw_info[iname]['band_label'] = 'HaLow 900MHz'
                elif freq_f < 3.0:
                    iw_info[iname]['band_label'] = '2.4 GHz'
                else:
                    iw_info[iname]['band_label'] = '5 GHz'
            except ValueError:
                wiphy_num = iw_info[iname].get('wiphy', '')
                iw_info[iname]['band_label'] = phy_band.get(wiphy_num, '')

    # HaLow (morse_usb): iw can report a regular Wi-Fi channel; Morse driver is the runtime source.
    for iname in list(iw_info.keys()):
        if 'morse' in iw_info[iname].get('driver', ''):
            iw_info[iname].update(get_halow_driver_info(iname))

    # ── bat0 slaves (active interfaces per batctl) ──
    bat0_slaves_active   = set()   # confirmed active in batctl
    bat0_slaves_inactive = set()   # listed but NOT active
    try:
        bat_r = subprocess.run(['batctl', 'if'], capture_output=True, text=True, timeout=5)
        for line in bat_r.stdout.splitlines():
            m_act = re.match(r'(\S+):\s+active', line)
            m_ina = re.match(r'(\S+):\s+inactive', line)
            if m_act: bat0_slaves_active.add(m_act.group(1))
            elif m_ina: bat0_slaves_inactive.add(m_ina.group(1))
    except Exception:
        pass
    bat0_all_slaves = bat0_slaves_active | bat0_slaves_inactive

    # ── which wpa_supplicant units are running ──
    wpa_running = set()
    try:
        sp = subprocess.run(
            ['systemctl', 'list-units', '--state=active', '--no-legend',
             'wpa_supplicant*', 'wpa_supplicant-s1g*'],
            capture_output=True, text=True, timeout=5)
        for line in sp.stdout.splitlines():
            # wpa_supplicant@wlan0.service or wpa_supplicant-s1g-wlan2.service
            m = re.search(r'wpa_supplicant[^@]*[@-](wlan\d+|halow\d+)', line)
            if m: wpa_running.add(m.group(1))
    except Exception:
        pass

    conf     = load_kv_file(MESH_CONF_FILE)
    eud_mode = conf.get('eud', 'wired')

    # Non-mesh interfaces (EUD AP) — must not be checked for bat0/wpa_supplicant
    no_mesh_ifaces = set()
    try:
        with open('/var/lib/no_mesh_if') as f:
            no_mesh_ifaces = {l.strip() for l in f if l.strip()}
    except Exception:
        pass

    # Build a set of all iface names present for cross-referencing
    all_names = {d.get('ifname', '') for d in raw}

    for iface_data in raw:
        name   = iface_data.get('ifname', '')
        state  = iface_data.get('operstate', 'UNKNOWN')   # UP / DOWN / UNKNOWN
        flags  = iface_data.get('flags', [])
        link_type = iface_data.get('link_type', '')

        if name == 'lo':
            continue

        addrs = [a['local'] for a in iface_data.get('addr_info', [])
                 if a.get('family') in ('inet', 'inet6') and not a['local'].startswith('fe80')]

        is_up   = state == 'UP'
        is_down = state == 'DOWN'
        iw      = iw_info.get(name, {})

        role   = 'other'
        health = 'ok'
        detail = ''
        faults = []   # list of human-readable problem strings

        if name == 'bat0':
            role   = 'bat'
            health = 'info'
            detail = 'BATMAN-ADV mesh bridge'
            if not is_up and state != 'UNKNOWN':
                health = 'fault'
                faults.append('bat0 is DOWN')
            elif not bat0_all_slaves:
                health = 'warn'
                faults.append('No interfaces enslaved to bat0')

        elif name == 'br0':
            role   = 'bridge'
            health = 'info'
            detail = 'L2 bridge (mesh + EUD)'
            if not addrs:
                health = 'warn'
                faults.append('No IP assigned')

        elif name in bat0_all_slaves or (
            name.startswith('wlan') and
            name not in no_mesh_ifaces and
            iw.get('type') != 'AP' and
            name not in [d.get('ifname') for d in raw if d.get('master') == 'bat0']
        ):
            # Mesh radio
            role = 'mesh'
            freq        = iw.get('freq', '')
            ch          = iw.get('channel', '')
            band_label  = iw.get('band_label', '')
            if band_label:
                detail = f"{band_label} — ch{ch}" if ch else band_label
            elif freq:
                detail = f"Mesh radio — {freq}GHz ch{ch}"
            else:
                detail = 'Mesh radio'
            if iw.get('ssid'):
                detail += f" [{iw['ssid']}]"

            if is_down:
                health = 'fault'
                faults.append(f'{name} is DOWN')
            elif name in bat0_slaves_inactive:
                health = 'fault'
                faults.append(f'Inactive in bat0 (wpa_supplicant issue?)')
            elif name not in bat0_slaves_active:
                health = 'warn'
                faults.append(f'Not active in bat0')

            # wpa_supplicant check only for mesh radios (not AP/no_mesh)
            if name not in wpa_running:
                if health == 'ok': health = 'warn'
                faults.append(f'wpa_supplicant not running for {name}')

        elif iw.get('type') == 'AP' or name in no_mesh_ifaces:
            role = 'ap'
            ssid = iw.get('ssid', '')
            freq = iw.get('freq', '')
            detail = f"EUD AP — {ssid}" + (f" ({freq}GHz)" if freq else '')
            if is_down:
                health = 'fault'
                faults.append(f'{name} AP is DOWN')
            elif not ssid:
                health = 'warn'
                faults.append('AP has no SSID (hostapd issue?)')

        elif name.startswith(('end', 'eth', 'enp', 'ens')):
            has_gw = False
            try:
                rout = subprocess.run(['ip', 'route', 'show', 'dev', name],
                                      capture_output=True, text=True, timeout=3)
                has_gw = 'default' in rout.stdout
            except Exception:
                pass

            if has_gw:
                role   = 'gateway'
                detail = 'Ethernet — Internet gateway'
            elif is_up and eud_mode == 'wired':
                role   = 'eud-bridge'
                detail = 'Ethernet — EUD connection'
            else:
                role   = 'other'
                detail = 'Ethernet'
            # Ethernet DOWN is usually fine (cable unplugged) — just informational
            if is_down:
                health = 'info'
                detail += ' (no cable)' if not detail.endswith(')') else ''

        elif (name.startswith('wlan') or name.startswith(('halow', 'mlan'))) and name not in no_mesh_ifaces and name not in bat0_all_slaves:
            # wlan not in bat0 and not AP — unexpected
            freq = iw.get('freq', '')
            detail = f"Wireless {freq}GHz" if freq else 'Wireless'
            if is_down:
                health = 'fault'
                faults.append(f'{name} is DOWN — not participating in mesh')
            elif name not in bat0_all_slaves and iw.get('type') != 'AP':
                health = 'warn'
                faults.append(f'Not in bat0 and not an AP — check wpa_supplicant')

        ifaces.append({
            'name':     name,
            'role':     role,
            'health':   health,
            'detail':   detail,
            'faults':   faults,
            'addrs':    addrs,
            'state':    state,
            'channel':  iw.get('channel', ''),
            'freq_mhz': iw.get('freq_mhz', ''),
            'halow_bw': iw.get('halow_bw', ''),
            'halow_source': iw.get('halow_source', ''),
        })

    # Sort: bat0, mesh, ap, gateway, eud-bridge, bridge, other
    # Within each role, faulted interfaces sort first (most visible)
    health_order = {'fault': 0, 'warn': 1, 'ok': 2, 'info': 3}
    role_order   = {'bat': 0, 'mesh': 1, 'ap': 2, 'gateway': 3, 'eud-bridge': 4, 'bridge': 5, 'other': 6}
    ifaces.sort(key=lambda x: (role_order.get(x['role'], 9), health_order.get(x['health'], 9)))
    return ifaces

def get_connected_euds():
    """
    Return list of {mac, ip, hostname} from dnsmasq leases.
    """
    euds = []
    lease_paths = [
        '/var/lib/misc/dnsmasq.leases',
        '/tmp/dnsmasq.leases',
        '/run/dnsmasq.leases',
    ]
    for path in lease_paths:
        try:
            with open(path) as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 4:
                        # format: expiry mac ip hostname [clientid]
                        euds.append({
                            'mac':      parts[1],
                            'ip':       parts[2],
                            'hostname': parts[3] if parts[3] != '*' else '',
                        })
            break
        except Exception:
            continue
    return euds

def get_running_services():
    """
    Return dict of service_name -> bool for elected/running mesh services.
    """
    checks = {
        'mumble':   ['mumble-server', 'murmur', 'mumble'],
        'mediamtx': ['mediamtx', 'rtsp-server'],
        'ntp':      ['chrony', 'chronyd', 'ntp', 'ntpd'],
        'syncthing':['syncthing'],
        'tak':      ['tak-server', 'takserver'],
    }
    results = {}
    for svc_name, unit_names in checks.items():
        active = False
        for unit in unit_names:
            try:
                r = subprocess.run(
                    ['systemctl', 'is-active', '--quiet', unit],
                    timeout=2
                )
                if r.returncode == 0:
                    active = True
                    break
            except Exception:
                pass
        results[svc_name] = active
    return results

def get_local_uptime():
    try:
        with open('/proc/uptime') as f:
            secs = float(f.read().split()[0])
        return fmt_uptime(secs)
    except Exception:
        return ''

def assemble_local_data():
    conf     = load_kv_file(MESH_CONF_FILE)
    state    = load_kv_file(MESH_STATE_FILE)
    hostname = get_my_hostname()
    battery  = get_battery()
    ifaces   = get_interfaces()
    euds     = get_connected_euds()
    services = get_running_services()
    uptime   = get_local_uptime()

    # Pull self entry from registry for extra fields
    nodes_raw = parse_registry()
    my_mac    = get_my_mac()
    my_node   = {}
    for nid, ndata in nodes_raw.items():
        if norm_mac(ndata.get('MAC_ADDRESS', '')) == my_mac or ndata.get('HOSTNAME', '') == hostname:
            my_node = ndata
            break

    # GPS — placeholder, will be populated when registry has GPS fields
    gps = {
        'available': bool(my_node.get('GPS_LATITUDE')),
        'lat':       my_node.get('GPS_LATITUDE', ''),
        'lon':       my_node.get('GPS_LONGITUDE', ''),
        'alt':       my_node.get('GPS_ALTITUDE', ''),
    }

    return {
        'hostname':  hostname,
        'ip':        (state.get('CURRENT_IPV4') or my_node.get('IPV4_ADDRESS', '')),
        'mac':       my_mac or '',
        'uptime':    uptime,
        'battery':   battery,
        'gps':       gps,
        'interfaces': ifaces,
        'euds':      euds,
        'services':  services,
        'eud_mode':  conf.get('eud', 'wired'),
        'ap_ssid':   conf.get('lan_ap_ssid', ''),
        'mesh_ssid': conf.get('mesh_ssid', ''),
    }

def fmt_uptime(seconds):
    try:
        s = int(float(seconds))
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h > 0:
            return f"{h}h {m}m"
        return f"{m}m {sec}s"
    except Exception:
        return seconds

# ─────────────────────────────────────────────────────────────────────────────
# Access Control
# ─────────────────────────────────────────────────────────────────────────────
def is_allowed_ip(client_ip, conf):
    if client_ip in ('127.0.0.1', '::1'):
        return True
    try:
        network = ipaddress.ip_network(conf.get('ipv4_network', '10.30.2.0/24'), strict=False)
        if ipaddress.ip_address(client_ip) in network:
            return True
    except Exception:
        pass
    return False

# ─────────────────────────────────────────────────────────────────────────────
# Data Assembly
# ─────────────────────────────────────────────────────────────────────────────
def assemble_status_data():
    conf       = load_kv_file(MESH_CONF_FILE)
    state      = load_kv_file(MESH_STATE_FILE)
    nodes_raw  = parse_registry()
    orig_tq, orig_map = run_batctl_originators()
    neighbors  = run_batctl_neighbors()
    gateways   = run_batctl_gateways()
    my_mac     = get_my_mac()
    my_host    = get_my_hostname()

    neighbor_macs = {n['mac'] for n in neighbors}
    gw_mac_map    = {g['mac']: g for g in gateways}
    selected_gw   = next((g['mac'] for g in gateways if g['selected']), None)

    # If batctl gwl has no gateways, fall back to registry IS_GATEWAY flags.
    # This covers nodes that have internet but haven't configured batctl gw server.
    registry_gw_nodes = [
        nid for nid, nd in nodes_raw.items()
        if nd.get('IS_GATEWAY', 'false').lower() == 'true'
    ]
    if not gateways and registry_gw_nodes:
        # Use the registry gateway with best TQ as the "selected" gateway
        def _node_tq(nid):
            nd = nodes_raw[nid]
            for m in nd.get('MAC_ADDRESSES', '').split(','):
                t = orig_tq.get(norm_mac(m.strip()))
                if t is not None:
                    return t
            return 0
        best_gw_nid = max(registry_gw_nodes, key=_node_tq)
        best_gw_nd  = nodes_raw[best_gw_nid]
        selected_gw = norm_mac(best_gw_nd.get('MAC_ADDRESS', ''))
        gateways    = [{'mac': selected_gw, 'tq': 0, 'selected': True}]

    node_list = []
    self_found = False

    for nid, ndata in nodes_raw.items():
        raw_mac  = ndata.get('MAC_ADDRESS', '')
        node_mac = norm_mac(raw_mac)
        hostname = ndata.get('HOSTNAME', 'unknown')

        # TQ: check primary mac then all macs
        tq = orig_tq.get(node_mac)
        if tq is None:
            for alt_mac in ndata.get('MAC_ADDRESSES', '').split(','):
                alt = norm_mac(alt_mac)
                if alt in orig_tq:
                    tq = orig_tq[alt]
                    break

        is_me = (my_mac and node_mac == my_mac) or (hostname == my_host)
        if is_me:
            tq = 255
            self_found = True

        all_node_macs = [norm_mac(m) for m in ndata.get('MAC_ADDRESSES', '').split(',') if m.strip()]
        is_direct = any(m in neighbor_macs for m in all_node_macs) or node_mac in neighbor_macs
        gw_info   = gw_mac_map.get(node_mac)

        node_list.append({
            'id':           nid,
            'hostname':     hostname,
            'mac':          raw_mac,
            'ip':           ndata.get('IPV4_ADDRESS', ''),
            'tq':           tq,
            'is_me':        is_me,
            'is_direct':    is_direct or is_me,
            'is_gateway':   ndata.get('IS_GATEWAY', 'false').lower() == 'true',
            'is_selected_gw': bool(selected_gw and (
                node_mac == selected_gw or
                selected_gw in [norm_mac(m) for m in ndata.get('MAC_ADDRESSES','').split(',') if m.strip()]
            )),
            'uptime':       fmt_uptime(ndata.get('UPTIME_SECONDS', '')),
            'cpu':          ndata.get('CPU_LOAD_AVERAGE', ''),
            'battery':      {'percentage': int(ndata['BATTERY_PERCENTAGE'])} if ndata.get('BATTERY_PERCENTAGE') else None,
            'mumble':       ndata.get('IS_MUMBLE_SERVER', 'false').lower() == 'true',
            'mediamtx':     ndata.get('IS_MEDIAMTX_SERVER', 'false').lower() == 'true',
            'ntp':          ndata.get('IS_NTP_SERVER', 'false').lower() == 'true',
            'state':        ndata.get('NODE_STATE', 'ACTIVE'),
            'ch_2g':        ndata.get('DATA_CHANNEL_2_4', ''),
            'ch_5g':        ndata.get('DATA_CHANNEL_5_0', ''),
            'limp':         ndata.get('IS_IN_LIMP_MODE', 'false').lower() == 'true',
            'all_macs':     [norm_mac(m) for m in ndata.get('MAC_ADDRESSES', '').split(',') if m.strip()],
        })

    # If self not in registry, inject a placeholder
    if not self_found and my_host:
        node_list.insert(0, {
            'id': 'self',
            'hostname': my_host,
            'mac': my_mac or '',
            'ip': (state.get('CURRENT_IPV4') or ''),
            'tq': 255, 'is_me': True, 'is_direct': True,
            'is_gateway': False, 'is_selected_gw': False,
            'uptime': '', 'cpu': '', 'battery': None,
            'mumble': False, 'mediamtx': False, 'ntp': False,
            'state': 'ACTIVE', 'ch_2g': '', 'ch_5g': '', 'limp': False,
        })

    node_list.sort(key=lambda n: (not n['is_me'], -(n['tq'] if n['tq'] is not None else -1)))

    # ── Build topology edges from batctl o nexthop data ──
    # mac_to_node_id: every MAC (all interfaces) -> node_id
    mac_to_node_id = {}
    for node in node_list:
        for m in node.get('all_macs', []):
            mac_to_node_id[m] = node['id']
        mac_to_node_id[norm_mac(node['mac'])] = node['id']

    edges = []
    self_node = next((n for n in node_list if n['is_me']), None)
    if self_node:
        for node in node_list:
            if node['is_me']:
                continue
            node_all_macs = set(node.get('all_macs', []))

            # Find the best orig_map entry for this node (match any of its MACs)
            best_entry = None
            for omac, odata in orig_map.items():
                if omac in node_all_macs:
                    if best_entry is None or odata['tq'] > best_entry['tq']:
                        best_entry = odata

            if best_entry is None:
                # Node in registry but not in originator table — no path known
                edges.append({
                    'source':  self_node['id'],
                    'target':  node['id'],
                    'type':    'unknown',
                    'via':     None,
                    'tq':      node['tq'],
                })
                continue

            nexthop = best_entry['nexthop']
            # Is nexthop one of this node's own MACs? -> direct
            if nexthop in node_all_macs or node['is_direct']:
                edges.append({
                    'source': self_node['id'],
                    'target': node['id'],
                    'type':   'direct',
                    'via':    None,
                    'tq':     node['tq'],
                })
            else:
                # nexthop is a different node -> multi-hop via that node
                via_id = mac_to_node_id.get(nexthop)
                edges.append({
                    'source': self_node['id'],
                    'target': node['id'],
                    'type':   'multihop',
                    'via':    via_id,
                    'tq':     node['tq'],
                })

        # Also add edges between non-self nodes where we can infer adjacency:
        # if node A routes to node C via node B, we know B->C exists
        inferred = set()
        for edge in list(edges):
            if edge['type'] == 'multihop' and edge['via']:
                pair = tuple(sorted([edge['via'], edge['target']]))
                if pair not in inferred:
                    inferred.add(pair)
                    # TQ for inferred edge: use target node's tq (conservative)
                    target_node = next((n for n in node_list if n['id'] == edge['target']), None)
                    edges.append({
                        'source': edge['via'],
                        'target': edge['target'],
                        'type':   'inferred',
                        'via':    None,
                        'tq':     target_node['tq'] if target_node else None,
                    })

    return {
        'nodes':          node_list,
        'my_mac':         my_mac or '',
        'my_hostname':    my_host,
        'my_ip':          next((n['ip'] for n in node_list if n.get('is_me')), '') or (state.get('CURRENT_IPV4') or ''),
        'mesh_ssid':      conf.get('mesh_ssid', ''),
        'network':        conf.get('ipv4_network', ''),
        'gateway_count':  len(gateways),  # includes registry fallback
        'selected_gw':    selected_gw or '',
        'neighbors':      neighbors,
        'edges':          edges,
        'timestamp':      int(time.time()),
    }

# ─────────────────────────────────────────────────────────────────────────────
# HTML Pages
# ─────────────────────────────────────────────────────────────────────────────
CSS = """
:root {
  --bg:       #ebeae8;
  --surface:  #ffffff;
  --panel:    #f7f6f3;
  --border:   #d6d2cb;
  --border2:  #e7e2da;
  --accent:   #00003f;
  --accent2:  #ecb000;
  --fer-yellow:#ecb000;
  --fer-black:#02000d;
  --text:     #02000d;
  --muted:    #615f68;
  --good:     #22c55e;
  --ok:       #eab308;
  --warn:     #f97316;
  --bad:      #ef4444;
  --gw:       #ecb000;
  --self:     #00003f;
  --shadow:   0 18px 50px rgba(2,0,13,.10);
  --font:     Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
:root[data-theme="dark"] {
  --bg:       #02000d;
  --surface:  #121118;
  --panel:    #0b0a12;
  --border:   #34313b;
  --border2:  #24212b;
  --accent:   #ecb000;
  --accent2:  #ecb000;
  --text:     #f8f6ef;
  --muted:    #aaa5b2;
  --gw:       #ecb000;
  --self:     #ecb000;
  --shadow:   0 18px 50px rgba(0,0,0,.36);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; background: var(--bg); color: var(--text); font-family: var(--font); font-size: 13px; overflow-x: hidden; }
body {
  background:
    linear-gradient(135deg, rgba(236,176,0,.24) 0 9px, transparent 9px 72px),
    linear-gradient(160deg, transparent 0 62%, rgba(0,0,63,.08) 62% 76%, transparent 76%),
    var(--bg);
}

/* ── Layout ── */
#app { display: flex; flex-direction: column; height: 100vh; }
#header { background: rgba(255,255,255,.92); backdrop-filter: blur(18px); border-bottom: 1px solid var(--border2); padding: 0 14px 0 0; display: flex; align-items: stretch; gap: 0; flex-shrink: 0; min-height: 58px; box-shadow: 0 1px 0 rgba(2,0,13,.05); position: relative; }
#header::after { content:''; position:absolute; left:0; right:0; bottom:0; height:3px; background: linear-gradient(90deg, var(--fer-yellow) 0 24%, var(--accent) 24% 72%, transparent 72%); pointer-events:none; }
:root[data-theme="dark"] #header { background: rgba(18,17,24,.92); }
/* Health pill — left edge strip */
#hdr-health { display: flex; align-items: center; gap: 7px; padding: 0 14px 0 12px;
              border-right: 1px solid var(--border); margin-right: 12px; flex-shrink: 0;
              transition: background 0.4s; }
#hdr-health-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0;
                  box-shadow: none; transition: background 0.4s, color 0.4s; }
#hdr-health-label { font-size: 11px; font-weight: 800; letter-spacing: 0;
                    transition: color 0.4s; white-space: nowrap; }
.health-ok   { color: #136c36; background: rgba(22,163,74,.07); }
.health-warn { color: #8a4b07; background: rgba(217,119,6,.08); }
.health-fault{ color: #b42318; background: rgba(180,35,24,.08); }
.health-loading { color: var(--muted); background: transparent; }
.fer-lockup { display:flex; align-items:center; align-self:center; height:34px; min-width:76px; padding:0 12px 0 0; margin-right:10px; border-right:1px solid var(--border); }
.fer-logo-img { display:block; width:82px; max-height:22px; object-fit:contain; }
:root[data-theme="dark"] .fer-logo-img { filter: invert(1) brightness(1.18); }
.fer-logo-fallback { display:none; align-items:center; height:28px; padding:0 10px; background:var(--fer-yellow); color:var(--fer-black); border-radius:4px; font-size:15px; font-weight:900; letter-spacing:0; }
.theme-toggle { border:1px solid var(--accent2); background:rgba(236,176,0,.10); color:var(--text); border-radius:999px; padding:6px 10px; font-family:var(--font); font-size:11px; font-weight:850; cursor:pointer; min-width:74px; }
.theme-toggle:hover { background:var(--accent2); color:var(--fer-black); box-shadow:0 8px 22px rgba(236,176,0,.20); }
/* Centre identity items */
#header .meta { color: var(--muted); font-size: 12px; display: flex; align-items: center; }
#hdr-hostname { color: var(--text); font-size: 13px; font-weight: 800; padding-right: 10px;
                border-right: 1px solid var(--border); margin-right: 10px; }
#hdr-ssid { color: var(--accent); }
#header .spacer { flex: 1; }
/* Right-side items */
#hdr-right { display: flex; align-items: center; gap: 12px; }
#hdr-gw-label { font-size: 11px; }
.gw-ok   { color: var(--good); }
.gw-none { color: var(--muted); }
.pill { display: inline-block; padding: 4px 9px; border-radius: 999px; font-size: 10px; font-weight: 800; letter-spacing: 0; }
.pill-accent { background: #eff6ff; color: var(--accent); border: 1px solid #bfdbfe; }
.pill-gw     { background: #fff7ed; color: #8a4b07; border: 1px solid #f3d29a; }
.pill-self   { background: #f5f3ff; color: #4c1d95; border: 1px solid #d7cdfa; }

/* ── Main Panels ── */
#main { display: flex; flex: 1; overflow: hidden; }
#topo-panel {
  flex: 1; position: relative; min-width: 0;
  background:
    linear-gradient(90deg, rgba(2,0,13,.045) 1px, transparent 1px),
    linear-gradient(0deg, rgba(2,0,13,.045) 1px, transparent 1px),
    linear-gradient(135deg, rgba(236,176,0,.12), transparent 36%),
    var(--panel);
  background-size: 42px 42px, 42px 42px, auto, auto;
}
#topo-panel canvas { width: 100%; height: 100%; display: block; }
#side-panel { width: 300px; flex-shrink: 0; overflow-y: auto; border-left: 1px solid var(--border2); background: var(--surface); }

/* ── Node Table ── */
.section-hdr { padding: 10px 12px; font-size: 11px; font-weight: 800; letter-spacing: 0; color: var(--muted); text-transform: none; border-bottom: 1px solid var(--border2); position: sticky; top: 0; background: var(--surface); z-index: 1; }
.node-row { padding: 10px 12px; border-bottom: 1px solid var(--border2); cursor: default; transition: background .15s; }
.node-row:hover { background: rgba(236,176,0,.08); }
.node-row.is-me { border-left: 2px solid var(--self); }
.node-row.is-gw { border-left: 2px solid var(--gw); }
.node-row.is-me.is-gw { border-left: 2px solid var(--gw); }
.node-name { font-size: 13px; font-weight: bold; display: flex; align-items: center; gap: 5px; margin-bottom: 3px; }
.node-ip { color: var(--muted); font-size: 11px; margin-bottom: 3px; }
.node-meta { display: flex; gap: 6px; flex-wrap: wrap; }
.badge { padding: 3px 7px; border-radius: 999px; font-size: 10px; font-weight: 750; }
.badge-tq-great  { background: rgba(22,163,74,.07); color: var(--text); border:1px solid rgba(22,163,74,.28); }
.badge-tq-ok     { background: rgba(236,176,0,.10); color: var(--text); border:1px solid rgba(236,176,0,.28); }
.badge-tq-warn   { background: rgba(217,119,6,.08); color: var(--text); border:1px solid rgba(217,119,6,.28); }
.badge-tq-bad    { background: rgba(180,35,24,.08); color: var(--text); border:1px solid rgba(180,35,24,.28); }
.badge-tq-none   { background: #f7f8fa; color: var(--muted); }
.badge-svc       { background: #eff6ff; color: var(--accent); }
.badge-gw        { background: #fff7ed; color: #8a4b07; }
.badge-direct    { background: #ecfdf3; color: #136c36; }
.tq-bar-wrap     { margin-top: 6px; height: 5px; background: var(--border2); border-radius: 999px; overflow: hidden; }
.tq-bar          { height: 100%; border-radius: 2px; transition: width .5s; }

/* ── Tooltip ── */
#tooltip { position: fixed; background: color-mix(in srgb, var(--surface) 92%, transparent); backdrop-filter: blur(12px); border: 1px solid var(--border); padding: 9px 11px; border-radius: 8px; pointer-events: none; font-size: 11px; line-height: 1.6; display: none; z-index: 100; max-width: 220px; box-shadow: var(--shadow); }

/* ── Admin Page ── */
.admin-wrap { max-width: 600px; margin: 0 auto; padding: 16px 12px 40px; }
.admin-wrap h2 { color: var(--text); font-size: 18px; letter-spacing: 0; margin-bottom: 4px; }
.admin-wrap .notice { color: var(--warn); font-size: 11px; margin-bottom: 20px; padding: 8px; background: #f9731610; border: 1px solid #f9731630; border-radius: 4px; }
.form-section { background: var(--surface); border: 1px solid var(--border2); border-radius: 8px; margin-bottom: 16px; box-shadow: var(--shadow); }
.form-section-title { padding: 12px 14px; font-size: 12px; font-weight: 800; letter-spacing: 0; color: var(--text); text-transform: none; border-bottom: 1px solid var(--border2); }
.form-row { display: flex; align-items: center; gap: 10px; padding: 10px 14px; border-bottom: 1px solid var(--border2); }
.form-row:last-child { border-bottom: none; }
.form-row label { flex: 0 0 160px; color: var(--text); font-size: 12px; }
.form-row .hint  { font-size: 10px; color: var(--muted); display: block; margin-top: 2px; }
.form-row input[type=text], .form-row input[type=password], .form-row select {
  flex: 1; background: var(--surface); border: 1px solid var(--border); color: var(--text);
  padding: 5px 8px; border-radius: 4px; font-family: var(--font); font-size: 12px;
}
.form-row input[type=text]:focus, .form-row input[type=password]:focus, .form-row select:focus {
  outline: none; border-color: var(--accent);
}
.form-row select option { background: var(--surface); }
.form-row input[type=checkbox] { width: 16px; height: 16px; accent-color: var(--accent); }
.form-btn { display: block; width: 100%; padding: 10px; background: var(--accent2); color: var(--fer-black);
  border: 1px solid var(--accent2); border-radius: 8px; font-family: var(--font); font-size: 13px; font-weight:850; letter-spacing: 0;
  cursor: not-allowed; opacity: .6; margin-top: 8px; text-transform: uppercase; }
.form-btn.active { cursor: pointer; opacity: 1; }

/* ── Local Node Panel ── */
.local-panel { border-bottom: 1px solid var(--border2); }
.local-row { display: flex; align-items: flex-start; gap: 8px; padding: 7px 12px; border-bottom: 1px solid var(--border2); font-size: 12px; }
.local-row:last-child { border-bottom: none; }
.local-label { flex: 0 0 90px; color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: .8px; padding-top: 1px; }
.local-val { flex: 1; color: var(--text); word-break: break-all; }
.iface-row { padding: 8px 12px; border-bottom: 1px solid var(--border2); position: relative; }
.iface-row.health-fault { border-left: 2px solid var(--bad);  background: rgba(180,35,24,.08); }
.iface-row.health-warn  { border-left: 2px solid var(--warn); background: rgba(217,119,6,.08); }
.iface-row.health-ok    { border-left: 2px solid transparent; }
.iface-row.health-info  { border-left: 2px solid transparent; }
.iface-header { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; margin-bottom: 2px; }
.iface-name { font-size: 11px; font-weight: bold; min-width: 52px; }
.iface-state { font-size: 10px; font-weight: bold; padding: 1px 4px; border-radius: 2px; }
.iface-state-up      { color: var(--good); }
.iface-state-down    { color: var(--bad); background: #ef444420; }
.iface-state-unknown { color: var(--muted); }
.iface-role { font-size: 10px; padding: 2px 6px; border-radius: 999px; font-weight: 750; }
.role-mesh     { background:#eff6ff; color:var(--accent); }
.role-ap       { background:#ecfdf3; color:#136c36; }
.role-gateway  { background:#fff7ed; color:#8a4b07; }
.role-bat      { background:#f5f3ff; color:#4c1d95; }
.role-bridge   { background:#f5f3ff; color:#4c1d95; }
.role-eud-bridge{ background:#ecfdf3; color:#136c36; }
.role-other    { background:#f7f8fa; color:var(--muted); }
.iface-detail  { font-size: 10px; color: var(--muted); }
.iface-addrs   { font-size: 10px; color: #60b8d4; }
.iface-fault   { font-size: 10px; color: var(--bad); margin-top: 2px; }
.iface-fault::before { content: '⚠ '; }
.iface-warn    { font-size: 10px; color: var(--warn); margin-top: 2px; }
.iface-warn::before  { content: '⚠ '; }
.fault-count { display:inline-block; padding:1px 5px; border-radius:3px; font-size:10px;
               background:#ef444420; color:var(--bad); margin-left:6px; vertical-align:middle; }
.warn-count  { display:inline-block; padding:1px 5px; border-radius:3px; font-size:10px;
               background:#f9731620; color:var(--warn); margin-left:6px; vertical-align:middle; }
/* ── Peer Detail Drawer ── */
#peer-drawer { display: flex; flex-direction: column; border-bottom: 1px solid var(--border2);
               background: var(--surface); flex-shrink: 0; max-height: 55vh; overflow-y: auto; }
#peer-drawer-hdr { display: flex; align-items: center; gap: 8px; padding: 8px 12px;
                   border-bottom: 1px solid var(--border2); background: var(--surface);
                   position: sticky; top: 0; z-index: 1; flex-shrink: 0; }
#peer-drawer-title { flex: 1; font-size: 13px; font-weight: 800; color: var(--text); }
#peer-drawer-body { overflow-y: auto; flex: 1; }
.peer-loading { padding: 16px; color: var(--muted); font-size: 12px; text-align: center; letter-spacing: 1px; }
.node-row.peer-selected { background: rgba(236,176,0,.14); outline: 1px solid rgba(236,176,0,.5); outline-offset: -1px; }
.eud-row { padding: 5px 12px; border-bottom: 1px solid var(--border2); font-size: 11px; }
.eud-row:last-child { border-bottom: none; }
.eud-name { color: var(--text); }
.eud-ip   { color: #60b8d4; }
.eud-mac  { color: var(--muted); font-size: 10px; }
.svc-grid { display: flex; flex-wrap: wrap; gap: 5px; padding: 7px 10px; }
.svc-pill { padding: 4px 8px; border-radius: 999px; font-size: 10px; font-weight: 750; letter-spacing: 0; }
.svc-on  { background: rgba(22,163,74,.07); color: var(--text); border: 1px solid rgba(22,163,74,.28); }
.svc-off { background: #f7f8fa; color: var(--muted); border: 1px solid var(--border); }
.batt-bar-wrap { display:inline-block; width:36px; height:10px; background:var(--border2); border-radius:999px; overflow:hidden; vertical-align:middle; margin-left:4px; }
.batt-bar { height:100%; border-radius:2px; }
.gps-dot { display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:4px; vertical-align:middle; }

/* ── Loading / Error ── */
#loading { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: var(--muted); font-size: 13px; font-weight: 700; letter-spacing: 0; }

/* ── Responsive: narrow → stack ── */
@media (max-width: 768px) {
  #main { flex-direction: column; }
  #topo-panel { height: var(--topo-h, 50vh); min-height: 80px; flex: none; }
  #side-panel { width: 100%; flex: 1; overflow-y: auto; border-left: none; border-top: none; }
  #header { flex-wrap: wrap; height: auto; min-height: 58px; padding: 6px 10px 8px 0; }
  .fer-lockup { order: 1; min-width: 64px; padding-right: 8px; margin-right: 6px; }
  .fer-logo-img { width: 64px; }
  #hdr-hostname { order: 2; max-width: 42vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #hdr-ip, #hdr-ssid { order: 3; }
  #hdr-right { order: 4; flex: 0 0 100%; margin-left: 0; gap: 8px; flex-wrap: wrap; justify-content: flex-start; }
  #header .meta { font-size: 11px; }
  .theme-toggle { padding: 5px 8px; min-width: 66px; }
}
@media (min-width: 769px) {
  #drag-handle { display: none; }
}
#drag-handle {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 22px;
  background: var(--surface);
  border-top: 1px solid var(--border2);
  border-bottom: 1px solid var(--border2);
  cursor: row-resize;
  flex-shrink: 0;
  touch-action: none;
  user-select: none;
  -webkit-user-select: none;
}
#drag-handle::before {
  content: '';
  width: 36px;
  height: 4px;
  border-radius: 2px;
  background: var(--muted);
  opacity: 0.6;
}
"""

STATUS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MANET Node</title>
<style>__CSS__</style>
</head>
<body>
<div id="app">
  <div id="header">
    <!-- Health pill -->
    <div id="hdr-health" class="health-loading">
      <div id="hdr-health-dot"></div>
      <span id="hdr-health-label">—</span>
    </div>
    <div class="fer-lockup" title="FER">
      <img class="fer-logo-img" src="https://www.fer.unizg.hr/_pub/themes_static/fer_2025/default/img/FERlogo.svg" alt="FER" onerror="this.style.display='none';this.nextElementSibling.style.display='inline-flex'">
      <span class="fer-logo-fallback">FER</span>
    </div>
    <!-- Identity -->
    <div class="meta" id="hdr-hostname">—</div>
    <div class="meta" id="hdr-ip" style="padding-right:10px">—</div>
    <div class="meta" id="hdr-ssid">—</div>
    <div class="spacer"></div>
    <!-- Right: mesh stats -->
    <div id="hdr-right">
      <div class="meta" id="hdr-nodes">—</div>
      <div class="meta" id="hdr-gw-label">—</div>
      <div class="meta" id="hdr-time" style="color:var(--muted);min-width:54px;text-align:right">—</div>
      <button id="theme-toggle" class="theme-toggle" type="button" onclick="toggleTheme()">Dark</button>
    </div>
  </div>
  <div id="main">
    <div id="topo-panel">
      <canvas id="topo"></canvas>
      <div id="loading">LOADING TOPOLOGY…</div>
    </div>
    <div id="drag-handle"></div>
    <div id="side-panel">
      <div id="peer-drawer">
        <div id="peer-drawer-hdr">
          <span id="peer-drawer-title">—</span>
        </div>
        <div id="peer-drawer-body"><div class="peer-loading">Loading…</div></div>
      </div>
      <div class="section-hdr">MESH NODES <span id="node-count"></span></div>
      <div id="node-list"></div>
    </div>
  </div>
</div>
<div id="tooltip"></div>

<script>
// ── Data & State ────────────────────────────────────────────────────────────
let DATA = null;
let SIM  = { nodes: [], links: [], running: false, raf: null };
let HOVER_NODE = null;
const POLL_INTERVAL_MS = __REFRESH__;
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
  if (window.MANET_CANVAS_READY && typeof drawTopo === 'function' && !SIM.running) drawTopo();
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem(THEME_KEY, next); } catch(e) {}
  setTheme(next);
}

function isDarkTheme() {
  return document.documentElement.dataset.theme === 'dark';
}

setTheme(preferredTheme());

// ── Utilities ────────────────────────────────────────────────────────────────
function tqClass(tq) {
  if (tq == null) return 'badge-tq-none';
  if (tq >= 200)  return 'badge-tq-great';
  if (tq >= 130)  return 'badge-tq-ok';
  if (tq >= 60)   return 'badge-tq-warn';
  return 'badge-tq-bad';
}
function tqColor(tq) {
  if (tq == null) return '#9aa4b2';
  if (tq >= 200)  return '#22c55e';
  if (tq >= 130)  return '#eab308';
  if (tq >= 60)   return '#f97316';
  return '#ef4444';
}
function tqLabel(tq) {
  if (tq == null) return '?';
  return `TQ ${tq}`;
}
function tqPct(tq) {
  if (tq == null) return 0;
  return Math.round((tq / 255) * 100);
}
function themeColor(light, dark) {
  return isDarkTheme() ? dark : light;
}
function ts(epoch) {
  const d = new Date(epoch * 1000);
  return d.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
}

// ── Node List ────────────────────────────────────────────────────────────────
function renderNodeList(nodes) {
  const el = document.getElementById('node-list');
  document.getElementById('node-count').textContent = `(${nodes.length})`;
  el.innerHTML = nodes.map(n => {
    const cls = [
      'node-row',
      n.is_me  ? 'is-me' : '',
      n.is_gateway ? 'is-gw' : ''
    ].filter(Boolean).join(' ');

    const badges = [];
    if (n.is_gateway)  badges.push(`<span class="badge badge-gw">${n.is_selected_gw ? '★ GW' : 'GW'}</span>`);
    if (n.is_direct && !n.is_me) badges.push(`<span class="badge badge-direct">DIRECT</span>`);
    if (n.mumble)      badges.push(`<span class="badge badge-svc">MUMBLE</span>`);
    if (n.mediamtx)    badges.push(`<span class="badge badge-svc">MTX</span>`);
    if (n.ntp)         badges.push(`<span class="badge badge-svc">NTP</span>`);
    if (n.limp)        badges.push(`<span class="badge badge-tq-bad">LIMP</span>`);

    const thisNodeLabel = n.is_me
      ? `<span style="background:var(--self);color:#fff;font-size:10px;font-weight:bold;padding:1px 7px;border-radius:3px;letter-spacing:.8px;margin-left:6px">THIS NODE</span>`
      : '';
    const tqBadge = `<span class="badge ${tqClass(n.tq)}">${tqLabel(n.tq)}</span>`;
    const bar = `<div class="tq-bar-wrap"><div class="tq-bar" style="width:${tqPct(n.tq)}%;background:${tqColor(n.tq)}"></div></div>`;
    const meta = n.uptime  ? `<span style="color:var(--muted)">up ${n.uptime}</span>` : '';
    const cpu  = n.cpu     ? `<span style="color:var(--muted)">CPU ${n.cpu}</span>` : '';
    let battMeta = '';
    if (n.battery != null) {
      const pct = n.battery.percentage;
      const col = battColor(pct);
      const icon = (n.battery.charging === true) ? '⚡' : (pct <= 15 ? '⚠' : '');
      battMeta = `<span style="color:${col};font-size:10px">${icon}${pct}%</span>`;
    }

    return `<div class="${cls}" data-id="${n.id}">
      <div class="node-name">${n.hostname}${thisNodeLabel}${n.state==='SHUTTING_DOWN'?'<span style="color:var(--bad);font-size:10px;margin-left:4px">OFFLINE</span>':''}</div>
      <div class="node-ip">${n.ip||'—'} &nbsp; <span style="color:var(--muted)">${n.mac}</span></div>
      <div class="node-meta">${tqBadge}${badges.join('')}${meta}${cpu}${battMeta}</div>
      ${bar}
    </div>`;
  }).join('');
}

function updateHeader(d) {
  document.getElementById('hdr-hostname').textContent = d.my_hostname || '—';
  document.getElementById('hdr-ip').textContent       = d.my_ip      || '—';
  document.getElementById('hdr-ssid').textContent     = d.mesh_ssid  ? `▶ ${d.mesh_ssid}` : '';
  document.getElementById('hdr-nodes').textContent    = `${d.nodes.length} node${d.nodes.length!==1?'s':''}`;
  document.getElementById('hdr-time').textContent     = ts(d.timestamp);

  // Gateway label: show selected gateway hostname if known, else count, else "No GW"
  const gwEl = document.getElementById('hdr-gw-label');
  if (d.gateway_count === 0) {
    gwEl.textContent  = 'No GW';
    gwEl.className    = 'meta gw-none';
  } else {
    // Find selected gateway node by MAC
    const selMac = (d.selected_gw || '').toLowerCase();
    // Match by any MAC, then fall back to any node flagged is_gateway
    const gwNode = selMac
      ? d.nodes.find(n => {
          if (n.mac && n.mac.toLowerCase() === selMac) return true;
          return (n.all_macs || []).some(m => m.toLowerCase() === selMac);
        }) || d.nodes.find(n => n.is_gateway)
      : d.nodes.find(n => n.is_gateway);
    const gwName = gwNode ? gwNode.hostname : `${d.gateway_count} GW`;
    gwEl.textContent = `via ${gwName}`;
    gwEl.className   = 'meta gw-ok';
  }
}

function updateHealthPill(localData) {
  const hdr    = document.getElementById('hdr-health');
  const dot    = document.getElementById('hdr-health-dot');
  const label  = document.getElementById('hdr-health-label');
  if (!localData || !localData.interfaces) return;

  const faults = localData.interfaces.filter(i => i.health === 'fault');
  const warns  = localData.interfaces.filter(i => i.health === 'warn');

  let cls, dotColor, text;
  if (faults.length > 0) {
    cls = 'health-fault';
    dotColor = 'var(--bad)';
    text = faults.length === 1
      ? `⚠ ${faults[0].name} FAULT`
      : `⚠ ${faults.length} FAULTS`;
  } else if (warns.length > 0) {
    cls = 'health-warn';
    dotColor = 'var(--warn)';
    text = warns.length === 1
      ? `⚠ ${warns[0].name} WARN`
      : `⚠ ${warns.length} WARNS`;
  } else {
    cls = 'health-ok';
    dotColor = 'var(--good)';
    text = '● ALL OK';
  }

  // Apply to pill container
  hdr.className      = cls;
  dot.style.background = dotColor;
  dot.style.color      = dotColor;
  label.textContent  = text;
  label.style.color  = dotColor;
}

// ── Topology Simulation ───────────────────────────────────────────────────────
const canvas = document.getElementById('topo');
const ctx    = canvas.getContext('2d');
window.MANET_CANVAS_READY = true;

function initSim(nodes, edges) {
  const W = canvas.offsetWidth, H = canvas.offsetHeight;
  const cx = W / 2, cy = H / 2;

  // Build node id -> sim node index for quick lookup
  const idIndex = {};
  SIM.nodes = nodes.map((n, i) => {
    let x, y;
    if (n.is_me) {
      x = cx; y = cy;
    } else {
      const angle = (i / (nodes.length - 1 || 1)) * Math.PI * 2;
      const r = Math.min(W, H) * 0.28;
      x = cx + Math.cos(angle) * r + (Math.random() - .5) * 40;
      y = cy + Math.sin(angle) * r + (Math.random() - .5) * 40;
    }
    idIndex[n.id] = i;
    return { ...n, x, y, vx: 0, vy: 0, r: n.is_me ? 14 : 10 };
  });

  // Build simulation links from server-computed edges
  SIM.links = [];
  (edges || []).forEach(edge => {
    const srcNode = SIM.nodes[idIndex[edge.source]];
    const dstNode = SIM.nodes[idIndex[edge.target]];
    if (!srcNode || !dstNode) return;
    SIM.links.push({
      source:   srcNode,
      target:   dstNode,
      type:     edge.type,    // 'direct' | 'multihop' | 'inferred' | 'unknown'
      via:      edge.via,     // node id of hop, or null
      tq:       edge.tq,
    });
  });
}

function simStep() {
  const W = canvas.offsetWidth, H = canvas.offsetHeight;
  const cx = W / 2, cy = H / 2;
  const nodes = SIM.nodes;
  const n_count = nodes.length || 1;

  // Max link length: scale with canvas but cap so nodes never reach edge.
  // Use a fraction of the smaller dimension so the graph stays readable.
  const maxLink = Math.min(W, H) * 0.30;

  // Repulsion: scale down with more nodes to avoid explosion on large meshes,
  // but also don't let 2-node setups push each other to opposite corners.
  const repulseK = Math.min(W, H) * 0.18 / Math.sqrt(n_count);

  // Clear forces
  nodes.forEach(n => { n.fx = 0; n.fy = 0; });

  // Repulsion
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      const dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.max(Math.sqrt(dx*dx + dy*dy), 0.1);
      const repel = (repulseK * repulseK) / dist;
      const fx = (dx / dist) * repel;
      const fy = (dy / dist) * repel;
      a.fx -= fx; a.fy -= fy;
      b.fx += fx; b.fy += fy;
    }
  }

  // Spring attraction along links
  SIM.links.forEach(link => {
    const a = link.source, b = link.target;
    const dx = b.x - a.x, dy = b.y - a.y;
    const dist = Math.max(Math.sqrt(dx*dx + dy*dy), 0.1);
    const tq = link.tq != null ? link.tq : 128;
    // Rest length: great link = shorter, poor link = longer, capped at maxLink
    const restLen = Math.min(60 + ((255 - tq) / 255) * maxLink * 0.85, maxLink);
    const spring  = (dist - restLen) * 0.05;
    const fx = (dx / dist) * spring;
    const fy = (dy / dist) * spring;
    a.fx += fx; a.fy += fy;
    b.fx -= fx; b.fy -= fy;
  });

  // Center gravity — stronger pull keeps everything near center
  nodes.forEach(n => {
    n.fx += (cx - n.x) * 0.03;
    n.fy += (cy - n.y) * 0.03;
  });

  // Self node pinned firmly to center
  nodes.forEach(n => {
    if (n.is_me) {
      n.fx += (cx - n.x) * 0.6;
      n.fy += (cy - n.y) * 0.6;
    }
  });

  // Integrate with damping
  const damp = 0.78;
  // Keep nodes inside an inset margin so labels are always visible
  const margin = 28;
  nodes.forEach(n => {
    n.vx = (n.vx + n.fx) * damp;
    n.vy = (n.vy + n.fy) * damp;
    n.x  = Math.max(margin, Math.min(W - margin, n.x + n.vx));
    n.y  = Math.max(margin, Math.min(H - margin, n.y + n.vy));
  });
}

const view = { x: 0, y: 0, scale: 1 };

function drawTopo() {
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.offsetWidth, H = canvas.offsetHeight;
  canvas.width  = W * dpr;
  canvas.height = H * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.translate(view.x, view.y);
  ctx.scale(view.scale, view.scale);

  ctx.clearRect(-view.x / view.scale, -view.y / view.scale, W / view.scale, H / view.scale);

  if (SIM.nodes.length === 0) return;

  // Grid dots
  ctx.fillStyle = themeColor('#d6d2cb77', '#34313b88');
  for (let gx = 20; gx < W; gx += 30)
    for (let gy = 20; gy < H; gy += 30)
      ctx.fillRect(gx, gy, 1, 1);

  // Links
  // Draw inferred/multihop edges first (behind direct edges)
  const drawOrder = ['unknown', 'multihop', 'inferred', 'direct'];
  const sorted_links = [...SIM.links].sort(
    (a, b) => drawOrder.indexOf(a.type) - drawOrder.indexOf(b.type)
  );

  sorted_links.forEach(link => {
    const a = link.source, b = link.target;
    const tq  = link.tq;
    const col = tqColor(tq);
    const isDirect   = link.type === 'direct';
    const isInferred = link.type === 'inferred';
    const isUnknown  = link.type === 'unknown';

    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);

    if (isDirect) {
      ctx.strokeStyle = col + 'dd';
      ctx.lineWidth   = 2;
      ctx.setLineDash([]);
    } else if (isInferred) {
      // Inferred peer-to-peer edges (not involving self)
      ctx.strokeStyle = col + '55';
      ctx.lineWidth   = 1;
      ctx.setLineDash([3, 5]);
    } else if (isUnknown) {
      ctx.strokeStyle = col + '33';
      ctx.lineWidth   = 0.8;
      ctx.setLineDash([2, 6]);
    } else {
      // multihop: self -> distant node, routed via intermediate
      ctx.strokeStyle = col + '66';
      ctx.lineWidth   = 1.2;
      ctx.setLineDash([5, 4]);
    }
    ctx.stroke();
    ctx.setLineDash([]);

    // TQ label at midpoint for direct and multihop lines involving self
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    if (tq != null && (isDirect || link.type === 'multihop')) {
      ctx.fillStyle = col + 'cc';
      ctx.font = '9px Inter, system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      // Small white backing for readability
      ctx.fillStyle = themeColor('#ffffffe8', '#121118e8');
      ctx.fillRect(mx - 12, my - 7, 24, 13);
      ctx.fillStyle = col + 'dd';
      ctx.fillText(`${tq}`, mx, my);
      ctx.textBaseline = 'alphabetic';
    }

    // Hop arrow for multihop: small arrow at midpoint pointing toward target
    if (link.type === 'multihop') {
      const angle = Math.atan2(b.y - a.y, b.x - a.x);
      const arrowX = a.x + (b.x - a.x) * 0.65;
      const arrowY = a.y + (b.y - a.y) * 0.65;
      const arrowLen = 7, arrowW = 3;
      ctx.beginPath();
      ctx.moveTo(arrowX, arrowY);
      ctx.lineTo(
        arrowX - arrowLen * Math.cos(angle - 0.4),
        arrowY - arrowLen * Math.sin(angle - 0.4)
      );
      ctx.moveTo(arrowX, arrowY);
      ctx.lineTo(
        arrowX - arrowLen * Math.cos(angle + 0.4),
        arrowY - arrowLen * Math.sin(angle + 0.4)
      );
      ctx.strokeStyle = col + '99';
      ctx.lineWidth = 1.2;
      ctx.setLineDash([]);
      ctx.stroke();
    }
  });

  // Nodes
  SIM.nodes.forEach(n => {
    const isHover = HOVER_NODE && HOVER_NODE.id === n.id;
    const isSelected = (SELECTED_PEER_ID === null && n.is_me) || (SELECTED_PEER_ID && SELECTED_PEER_ID === n.id);
    const col = n.is_me ? themeColor('#00003f', '#ecb000') : (n.is_gateway ? '#ecb000' : tqColor(n.tq));
    const r = n.r + (isHover ? 3 : (isSelected ? 2 : 0));

    // Soft focus halo for selected node
    const glowR = isSelected ? r * 4.5 : r * 3;
    const grd = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, glowR);
    grd.addColorStop(0, isSelected ? col + '70' : col + '30');
    grd.addColorStop(0.4, isSelected ? col + '40' : col + '10');
    grd.addColorStop(1, col + '00');
    ctx.beginPath();
    ctx.arc(n.x, n.y, glowR, 0, Math.PI * 2);
    ctx.fillStyle = grd;
    ctx.fill();

    // Circle
    ctx.beginPath();
    ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
    ctx.fillStyle = isSelected ? col + '18' : themeColor('#ffffff', '#121118');
    ctx.fill();
    ctx.strokeStyle = isSelected ? col : col;
    ctx.lineWidth = isSelected ? 2.5 : (n.is_me ? 2.5 : (isHover ? 2 : 1.5));
    ctx.stroke();

    // Icon inside node: gateway star takes priority, THIS NODE gets dot overlay
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    if (n.is_gateway) {
      ctx.fillStyle = n.is_selected_gw ? '#ecb000' : '#ecb00080';
      ctx.font = `${Math.round(r * 0.9)}px serif`;
      ctx.fillText('★', n.x, n.y);
    } else if (n.is_me) {
      ctx.fillStyle = themeColor('#00003f', '#ecb000');
      ctx.font = `${Math.round(r)}px Inter, system-ui, sans-serif`;
      ctx.fillText('◉', n.x, n.y);
    }
    // THIS NODE: small filled dot in top-right corner of circle
    if (n.is_me) {
      ctx.beginPath();
      ctx.arc(n.x + r * 0.65, n.y - r * 0.65, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = themeColor('#00003f', '#ecb000');
      ctx.fill();
      ctx.strokeStyle = themeColor('#ffffff', '#02000d');
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    ctx.textBaseline = 'alphabetic';

    // Label
    ctx.fillStyle = isHover || isSelected ? themeColor('#02000d', '#f8f6ef') : themeColor('#4a4752', '#cfc9d8');
    ctx.font = `${isHover || isSelected ? '700 ' : '600 '}11px Inter, system-ui, sans-serif`;
    ctx.textAlign = 'center';
    ctx.fillText(n.hostname, n.x, n.y + r + 12);
  });
}

function animate() {
  if (!SIM.running) return;
  simStep();
  drawTopo();
  SIM.raf = requestAnimationFrame(animate);
}

function startSim(data) {
  if (SIM.raf) cancelAnimationFrame(SIM.raf);
  SIM.running = true;
  initSim(data.nodes, data.edges || []);
  animate();
  // Slow down after 4s
  setTimeout(() => { SIM.running = false; }, 4000);
}

// Convert screen coords to canvas/simulation coords
function screenToSim(sx, sy) {
  return { x: (sx - view.x) / view.scale, y: (sy - view.y) / view.scale };
}

// ── Canvas click → open peer drawer ──────────────────────────────────────────
canvas.addEventListener('click', e => {
  const rect = canvas.getBoundingClientRect();
  const { x: mx, y: my } = screenToSim(e.clientX - rect.left, e.clientY - rect.top);
  const clicked = SIM.nodes.find(n => {
    const dx = n.x - mx, dy = n.y - my;
    return Math.sqrt(dx*dx + dy*dy) < n.r + 12;
  });
  if (!clicked) return;
  if (clicked.is_me) { showLocalInDrawer(); return; }
  const node = DATA && DATA.nodes.find(n => n.id === clicked.id);
  if (node) openPeerDrawer(node);
});

// ── Tooltip ───────────────────────────────────────────────────────────────────
canvas.addEventListener('mousemove', e => {
  const rect = canvas.getBoundingClientRect();
  const { x: mx, y: my } = screenToSim(e.clientX - rect.left, e.clientY - rect.top);
  HOVER_NODE = SIM.nodes.find(n => {
    const dx = n.x - mx, dy = n.y - my;
    return Math.sqrt(dx*dx + dy*dy) < n.r + 8;
  }) || null;

  const tip = document.getElementById('tooltip');
  canvas.style.cursor = HOVER_NODE ? 'pointer' : 'default';
  if (HOVER_NODE) {
    const n = HOVER_NODE;
    const svcs = [n.is_gateway?'Gateway':'', n.mumble?'Mumble':'', n.mediamtx?'MediaMTX':'', n.ntp?'NTP':''].filter(Boolean);
    tip.innerHTML = `<b style="color:var(--accent)">${n.hostname}</b><br>
      IP: ${n.ip||'—'}<br>
      MAC: ${n.mac}<br>
      TQ: <b style="color:${tqColor(n.tq)}">${tqLabel(n.tq)}</b> / 255<br>
      ${n.uptime ? `Up: ${n.uptime}<br>` : ''}
      ${n.cpu    ? `CPU: ${n.cpu}<br>`    : ''}
      ${svcs.length ? `Svcs: ${svcs.join(', ')}` : ''}`;
    tip.style.display = 'block';
    tip.style.left    = `${e.clientX + 12}px`;
    tip.style.top     = `${e.clientY - 10}px`;
  } else {
    tip.style.display = 'none';
  }
  if (!SIM.running) drawTopo();
});

canvas.addEventListener('mouseleave', () => {
  HOVER_NODE = null;
  document.getElementById('tooltip').style.display = 'none';
  if (!SIM.running) drawTopo();
});

canvas.addEventListener('click', e => {
  const rect = canvas.getBoundingClientRect();
  const { x: mx, y: my } = screenToSim(e.clientX - rect.left, e.clientY - rect.top);
  const hit = SIM.nodes.find(n => {
    const dx = n.x - mx, dy = n.y - my;
    return Math.sqrt(dx*dx + dy*dy) < n.r + 10;
  });
  if (!hit) return;
  if (hit.is_me) { showLocalInDrawer(); return; }
  const listRow = document.querySelector(`#node-list .node-row[data-id="${hit.id}"]`);
  if (listRow) listRow.scrollIntoView({ block: 'nearest' });
  openPeerDrawer(hit);
});

// ── Touch: pinch-to-zoom + pan + node drag ────────────────────────────────────
let dragNode = null, dragOX = 0, dragOY = 0;
let lastPanX = 0, lastPanY = 0, isPanning = false;
let pinchDist0 = 0, pinchScale0 = 1, pinchCX = 0, pinchCY = 0;

function getTouchDist(touches) {
  const dx = touches[0].clientX - touches[1].clientX;
  const dy = touches[0].clientY - touches[1].clientY;
  return Math.sqrt(dx*dx + dy*dy);
}
function getTouchCenter(touches, rect) {
  return {
    x: (touches[0].clientX + touches[1].clientX) / 2 - rect.left,
    y: (touches[0].clientY + touches[1].clientY) / 2 - rect.top,
  };
}

canvas.addEventListener('touchstart', e => {
  const rect = canvas.getBoundingClientRect();
  if (e.touches.length === 1) {
    const t = e.touches[0];
    touchStartX = t.clientX; touchStartY = t.clientY; touchStartT = Date.now();
    const sx = t.clientX - rect.left, sy = t.clientY - rect.top;
    const { x: mx, y: my } = screenToSim(sx, sy);
    dragNode = SIM.nodes.find(n => {
      const dx = n.x - mx, dy = n.y - my;
      return Math.sqrt(dx*dx + dy*dy) < n.r + 12;
    }) || null;
    if (dragNode) {
      dragOX = mx - dragNode.x; dragOY = my - dragNode.y;
      e.preventDefault(); // only prevent default when dragging a node
    } else {
      isPanning = true; lastPanX = sx; lastPanY = sy;
      e.preventDefault(); // prevent scroll while panning canvas
    }
  } else if (e.touches.length === 2) {
    dragNode = null; isPanning = false;
    pinchDist0  = getTouchDist(e.touches);
    pinchScale0 = view.scale;
    const c = getTouchCenter(e.touches, rect);
    pinchCX = c.x; pinchCY = c.y;
    e.preventDefault();
  }
}, { passive: false });

canvas.addEventListener('touchmove', e => {
  const rect = canvas.getBoundingClientRect();
  if (e.touches.length === 2) {
    // Pinch zoom
    const dist   = getTouchDist(e.touches);
    const newScale = Math.min(Math.max(pinchScale0 * (dist / pinchDist0), 0.3), 5);
    // Zoom around pinch center
    view.x = pinchCX - (pinchCX - view.x) * (newScale / view.scale);
    view.y = pinchCY - (pinchCY - view.y) * (newScale / view.scale);
    view.scale = newScale;
    if (!SIM.running) drawTopo();
    e.preventDefault();
  } else if (e.touches.length === 1) {
    const t = e.touches[0];
    const sx = t.clientX - rect.left, sy = t.clientY - rect.top;
    if (dragNode) {
      const { x: mx, y: my } = screenToSim(sx, sy);
      dragNode.x = mx - dragOX; dragNode.y = my - dragOY;
      dragNode.vx = 0; dragNode.vy = 0;
    } else if (isPanning) {
      view.x += sx - lastPanX; view.y += sy - lastPanY;
      lastPanX = sx; lastPanY = sy;
    }
    if (!SIM.running) drawTopo();
    e.preventDefault();
  }
}, { passive: false });

let touchStartX = 0, touchStartY = 0, touchStartT = 0;

canvas.addEventListener('touchend', e => {
  if (e.touches.length < 2) { pinchDist0 = 0; }
  if (e.touches.length === 0) {
    const wasDraggingNode = dragNode;
    const wasPanning = isPanning;
    dragNode = null; isPanning = false;

    // Tap detection: short time, small movement
    const dt = Date.now() - touchStartT;
    const ct = e.changedTouches[0];
    const dx = ct.clientX - touchStartX, dy = ct.clientY - touchStartY;
    const dist = Math.sqrt(dx*dx + dy*dy);
    if (dt < 300 && dist < 12) {
      const rect = canvas.getBoundingClientRect();
      const sx = ct.clientX - rect.left, sy = ct.clientY - rect.top;
      const { x: mx, y: my } = screenToSim(sx, sy);
      const hit = SIM.nodes.find(n => {
        const ndx = n.x - mx, ndy = n.y - my;
        return Math.sqrt(ndx*ndx + ndy*ndy) < n.r + 14;
      });
      if (hit) {
        if (hit.is_me) {
          showLocalInDrawer();
        } else {
          const listRow = document.querySelector(`#node-list .node-row[data-id="${hit.id}"]`);
          if (listRow) listRow.scrollIntoView({ block: 'nearest' });
          openPeerDrawer(hit);
        }
      }
    }
  }
});

// ── Panel drag-to-resize (mobile) ────────────────────────────────────────────
(function () {
  const handle = document.getElementById('drag-handle');
  const main   = document.getElementById('main');
  let dragging = false, startY = 0, startH = 0;

  function onStart(y) {
    dragging = true;
    startY   = y;
    startH   = document.getElementById('topo-panel').getBoundingClientRect().height;
    document.body.style.userSelect = 'none';
  }
  function onMove(y) {
    if (!dragging) return;
    const mainH  = main.getBoundingClientRect().height;
    const newH   = Math.min(Math.max(startH + (y - startY), 80), mainH - 60);
    document.documentElement.style.setProperty('--topo-h', newH + 'px');
    if (!SIM.running) drawTopo();
  }
  function onEnd() {
    dragging = false;
    document.body.style.userSelect = '';
  }

  handle.addEventListener('touchstart', e => { onStart(e.touches[0].clientY); e.preventDefault(); }, { passive: false });
  handle.addEventListener('touchmove',  e => { onMove(e.touches[0].clientY);  e.preventDefault(); }, { passive: false });
  handle.addEventListener('touchend',   onEnd);
  handle.addEventListener('mousedown',  e => onStart(e.clientY));
  window.addEventListener('mousemove',  e => onMove(e.clientY));
  window.addEventListener('mouseup',    onEnd);
})();

// ── Resize ────────────────────────────────────────────────────────────────────
window.addEventListener('resize', () => {
  if (!SIM.running) drawTopo();
});

function battColor(pct) {
  if (pct >= 60) return '#22c55e';
  if (pct >= 30) return '#eab308';
  return '#ef4444';
}

function renderLocalPanel(d) {
  const el = document.getElementById('peer-drawer-body');

  // ── Identity rows ──
  let html = '';

  // Battery
  let battHtml = '—';
  if (d.battery != null) {
    const b   = d.battery;
    const pct = b.percentage;
    const col = battColor(pct);
    let extra = '';
    if (b.voltage_v != null) {
      const v   = b.voltage_v.toFixed(3);
      const mA  = b.current_ma != null ? b.current_ma : null;
      const mW  = b.power_w  != null ? b.power_w  : null;
      const st  = b.status || '';
      const stIcon = st === 'charging' ? ' ⚡' : st === 'full' ? ' ✓' : '';
      const mAstr  = mA != null ? ` &nbsp;${mA > 0 ? '+' : ''}${mA}mA` : '';
      const mWstr  = mW != null ? ` &nbsp;${mW}W` : '';
      extra = `<span style="font-size:9px;color:var(--muted)"> ${v}V${mAstr}${mWstr}${stIcon}</span>`;
    }
    battHtml = `${pct}%<span class="batt-bar-wrap"><span class="batt-bar" style="width:${pct}%;background:${col}"></span></span>${extra}`;
  }

  // GPS
  let gpsHtml;
  if (d.gps && d.gps.available) {
    gpsHtml = `<span class="gps-dot" style="background:var(--good)"></span>${d.gps.lat}, ${d.gps.lon}` +
              (d.gps.alt ? ` &nbsp;${d.gps.alt}m` : '');
  } else {
    gpsHtml = `<span class="gps-dot" style="background:var(--muted)"></span><span style="color:var(--muted)">No fix</span>`;
  }

  const rows = [
    ['Hostname', d.hostname || '—'],
    ['Mesh IP',  d.ip       || '—'],
    ['Uptime',   d.uptime   || '—'],
    ['Battery',  battHtml],
    ['GPS',      gpsHtml],
    ['EUD Mode', d.eud_mode || '—'],
  ];
  if (d.eud_mode !== 'wired' && d.ap_ssid) {
    rows.push(['AP SSID', d.ap_ssid]);
  }

  html += rows.map(([label, val]) =>
    `<div class="local-row"><span class="local-label">${label}</span><span class="local-val">${val}</span></div>`
  ).join('');

  // ── Interfaces ──
  if (d.interfaces && d.interfaces.length) {
    const faultCount = d.interfaces.filter(i => i.health === 'fault').length;
    const warnCount  = d.interfaces.filter(i => i.health === 'warn').length;
    let ifaceHdr = `<div class="section-hdr" style="font-size:9px;position:static">INTERFACES`;
    if (faultCount) ifaceHdr += `<span class="fault-count">⚠ ${faultCount} FAULT${faultCount>1?'S':''}</span>`;
    else if (warnCount) ifaceHdr += `<span class="warn-count">⚠ ${warnCount} WARN${warnCount>1?'S':''}</span>`;
    ifaceHdr += `</div>`;
    html += ifaceHdr;

    const roleLabel = {
      bat: 'BATMAN', mesh: 'MESH', ap: 'EUD AP', gateway: 'GATEWAY',
      'eud-bridge': 'EUD', bridge: 'BRIDGE', other: ''
    };
    html += d.interfaces.map(iface => {
      const label = roleLabel[iface.role] || iface.role.toUpperCase();
      const stateCls = iface.state === 'UP' ? 'up' : iface.state === 'DOWN' ? 'down' : 'unknown';
      const stateLabel = iface.state || '?';
      const addrs = iface.addrs && iface.addrs.length
        ? `<div class="iface-addrs">${iface.addrs.join(' &nbsp; ')}</div>` : '';
      const faultLines = (iface.faults || []).map(f =>
        `<div class="${iface.health === 'fault' ? 'iface-fault' : 'iface-warn'}">${f}</div>`
      ).join('');
      return `<div class="iface-row health-${iface.health}">
        <div class="iface-header">
          <span class="iface-name">${iface.name}</span>
          <span class="iface-state iface-state-${stateCls}">${stateLabel}</span>
          ${label ? `<span class="iface-role role-${iface.role}">${label}</span>` : ''}
        </div>
        ${iface.detail ? `<div class="iface-detail">${iface.detail}</div>` : ''}
        ${addrs}
        ${faultLines}
      </div>`;
    }).join('');
  }

  // ── Connected EUDs ──
  html += `<div class="section-hdr" style="font-size:9px;position:static">CONNECTED EUDS (${d.euds ? d.euds.length : 0})</div>`;
  if (d.euds && d.euds.length) {
    html += d.euds.map(e =>
      `<div class="eud-row">
        <span class="eud-name">${e.hostname || '<i style="color:var(--muted)">unknown</i>'}</span>
        &nbsp;<span class="eud-ip">${e.ip}</span>
        <div class="eud-mac">${e.mac}</div>
      </div>`
    ).join('');
  } else {
    html += `<div style="padding:5px 10px;font-size:11px;color:var(--muted)">None</div>`;
  }

  // ── Services ──
  html += `<div class="section-hdr" style="font-size:9px;position:static">SERVICES</div>`;
  const svcLabels = {
    mumble: 'Mumble', mediamtx: 'MediaMTX', ntp: 'NTP', syncthing: 'Syncthing', tak: 'TAK'
  };
  html += `<div class="svc-grid">`;
  for (const [key, label] of Object.entries(svcLabels)) {
    const on = d.services && d.services[key];
    html += `<span class="svc-pill ${on ? 'svc-on' : 'svc-off'}">${label}</span>`;
  }
  html += `</div>`;

  el.innerHTML = html;
}

async function fetchLocal() {
  try {
    const r = await fetch('/api/local');
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    updateHealthPill(d);
    if (SELECTED_PEER_ID === null) {
      document.getElementById('peer-drawer-title').textContent =
        (d.hostname || '—') + (d.ip ? '  ' + d.ip : '') + '  ★ THIS NODE';
      renderLocalPanel(d);
    }
  } catch (err) {
    if (SELECTED_PEER_ID === null)
      document.getElementById('peer-drawer-body').textContent = `Error: ${err.message}`;
  }
}

// ── Data Fetching ─────────────────────────────────────────────────────────────
async function fetchData() {
  try {
    const r = await fetch('/api/data');
    if (!r.ok) throw new Error(r.status);
    DATA = await r.json();
    document.getElementById('loading').style.display = 'none';
    updateHeader(DATA);
    renderNodeList(DATA.nodes);
    startSim(DATA);
  } catch (err) {
    document.getElementById('loading').textContent = `ERROR: ${err.message}`;
  }
}

fetchData();
fetchLocal();
setInterval(fetchData, POLL_INTERVAL_MS);
setInterval(fetchLocal, POLL_INTERVAL_MS);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    fetchData();
    fetchLocal();
  }
});

// ── Peer Detail Drawer ────────────────────────────────────────────────────────
let SELECTED_PEER_ID = null;

document.getElementById('node-list').addEventListener('click', e => {
  const row = e.target.closest('.node-row');
  if (!row) return;
  const id = row.dataset.id;
  if (!id || !DATA) return;
  const node = DATA.nodes.find(n => n.id === id);
  if (!node) return;
  if (node.is_me) { showLocalInDrawer(); return; }
  openPeerDrawer(node);
});

function openPeerDrawer(node) {
  SELECTED_PEER_ID = node.id;
  document.querySelectorAll('.node-row').forEach(r => r.classList.toggle('peer-selected', r.dataset.id === node.id));
  document.getElementById('peer-drawer-title').textContent = node.hostname + (node.ip ? '  ' + node.ip : '');
  document.getElementById('peer-drawer-body').innerHTML = '<div class="peer-loading">FETCHING…</div>';
  document.getElementById('side-panel').scrollTop = 0;
  if (!SIM.running) drawTopo();
  if (!node.ip) {
    document.getElementById('peer-drawer-body').innerHTML = '<div class="peer-loading" style="color:var(--muted)">No IP known for this node</div>';
    return;
  }
  fetchPeer(node.ip, node.hostname);
}

function showLocalInDrawer() {
  SELECTED_PEER_ID = null;
  document.querySelectorAll('.node-row').forEach(r => r.classList.remove('peer-selected'));
  document.getElementById('side-panel').scrollTop = 0;
  if (!SIM.running) drawTopo();
  fetchLocal();
}

async function fetchPeer(ip, hostname) {
  try {
    const r = await fetch('/api/peer/' + ip);
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || r.status);
    renderPeerDrawer(d, hostname);
  } catch (err) {
    document.getElementById('peer-drawer-body').innerHTML =
      '<div class="peer-loading" style="color:var(--bad)">Error: ' + err.message + '</div>';
  }
}

function renderPeerDrawer(d, hostname) {
  let html = '';

  // Battery
  let battHtml = '—';
  if (d.battery != null) {
    const b = d.battery, pct = b.percentage, col = battColor(pct);
    const icon = b.charging === true ? ' ⚡' : (pct <= 15 ? ' ⚠' : '');
    let extra = '';
    if (b.voltage_v != null) {
      const mAstr = b.current_ma != null ? ` ${b.current_ma > 0 ? '+' : ''}${b.current_ma}mA` : '';
      const mWstr = b.power_w   != null ? ` ${b.power_w}W` : '';
      const stIcon = b.status === 'charging' ? ' ⚡' : '';
      extra = `<span style="font-size:9px;color:var(--muted)"> ${b.voltage_v.toFixed(3)}V${mAstr}${mWstr}${stIcon}</span>`;
    }
    battHtml = `${pct}%<span class="batt-bar-wrap"><span class="batt-bar" style="width:${pct}%;background:${col}"></span></span>${icon}${extra}`;
  }

  const rows = [
    ['Hostname', d.hostname || '—'],
    ['Mesh IP',  d.ip       || '—'],
    ['Uptime',   d.uptime   || '—'],
    ['Battery',  battHtml],
    ['EUD Mode', d.eud_mode || '—'],
  ];
  if (d.ap_ssid) rows.push(['AP SSID', d.ap_ssid]);

  html += rows.map(([label, val]) =>
    `<div class="local-row"><span class="local-label">${label}</span><span class="local-val">${val}</span></div>`
  ).join('');

  // Interfaces
  if (d.interfaces && d.interfaces.length) {
    const faults = d.interfaces.filter(i => i.health === 'fault').length;
    const warns  = d.interfaces.filter(i => i.health === 'warn').length;
    let hdr = `<div class="section-hdr" style="font-size:9px;position:static">INTERFACES`;
    if (faults) hdr += `<span class="fault-count">⚠ ${faults} FAULT${faults>1?'S':''}</span>`;
    else if (warns) hdr += `<span class="warn-count">⚠ ${warns} WARN${warns>1?'S':''}</span>`;
    hdr += `</div>`;
    html += hdr;
    const roleLabel = { bat:'BATMAN', mesh:'MESH', ap:'EUD AP', gateway:'GATEWAY', 'eud-bridge':'EUD', bridge:'BRIDGE', other:'' };
    html += d.interfaces.map(iface => {
      const label = roleLabel[iface.role] || iface.role.toUpperCase();
      const sc = iface.state === 'UP' ? 'up' : iface.state === 'DOWN' ? 'down' : 'unknown';
      const addrs = iface.addrs && iface.addrs.length ? `<div class="iface-addrs">${iface.addrs.join(' &nbsp; ')}</div>` : '';
      const faultLines = (iface.faults || []).map(f => `<div class="${iface.health==='fault'?'iface-fault':'iface-warn'}">${f}</div>`).join('');
      return `<div class="iface-row health-${iface.health}">
        <div class="iface-header">
          <span class="iface-name">${iface.name}</span>
          <span class="iface-state iface-state-${sc}">${iface.state||'?'}</span>
          ${label ? `<span class="iface-role role-${iface.role}">${label}</span>` : ''}
        </div>
        ${iface.detail ? `<div class="iface-detail">${iface.detail}</div>` : ''}
        ${addrs}${faultLines}
      </div>`;
    }).join('');
  }

  // Connected EUDs
  html += `<div class="section-hdr" style="font-size:9px;position:static">CONNECTED EUDS (${d.euds ? d.euds.length : 0})</div>`;
  if (d.euds && d.euds.length) {
    html += d.euds.map(e => `<div class="eud-row">
      <span class="eud-name">${e.hostname || '<i style="color:var(--muted)">unknown</i>'}</span>
      &nbsp;<span class="eud-ip">${e.ip}</span>
      <div class="eud-mac">${e.mac}</div></div>`).join('');
  } else {
    html += `<div style="padding:5px 10px;font-size:11px;color:var(--muted)">None</div>`;
  }

  // Services
  html += `<div class="section-hdr" style="font-size:9px;position:static">SERVICES</div>`;
  const svcLabels = { mumble:'Mumble', mediamtx:'MediaMTX', ntp:'NTP', syncthing:'Syncthing', tak:'TAK' };
  html += `<div class="svc-grid">`;
  for (const [key, label] of Object.entries(svcLabels)) {
    const on = d.services && d.services[key];
    html += `<span class="svc-pill ${on?'svc-on':'svc-off'}">${label}</span>`;
  }
  html += `</div>`;

  document.getElementById('peer-drawer-body').innerHTML = html;
}
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────────────────────
# Admin API helpers
# ─────────────────────────────────────────────────────────────────────────────
import hashlib

ALFRED_CONFIG_TYPE = 70
PENDING_CONFIG_FILE = '/var/run/mesh_pending_config.json'

def broadcast_config_package(pkg):
    """Write config package to Alfred type 70."""
    import subprocess, json
    payload = json.dumps(pkg, separators=(',', ':'))
    try:
        r = subprocess.run(
            ['alfred', '-s', str(ALFRED_CONFIG_TYPE)],
            input=payload, capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False

def get_pending_config():
    """Read pending config package from disk, or None."""
    try:
        with open(PENDING_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def save_pending_config(pkg):
    try:
        with open(PENDING_CONFIG_FILE, 'w') as f:
            json.dump(pkg, f)
        return True
    except Exception:
        return False

def clear_pending_config():
    try:
        os.remove(PENDING_CONFIG_FILE)
    except Exception:
        pass

def make_config_version(config_dict):
    """8-char SHA-256 prefix of the JSON config (deterministic)."""
    s = json.dumps(config_dict, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(s.encode()).hexdigest()[:8]

def assemble_admin_status():
    """Return current config, pending config, and per-node ACK status."""
    conf       = load_kv_file(MESH_CONF_FILE)
    nodes_raw  = parse_registry()
    pending    = get_pending_config()

    node_status = []
    for nid, nd in nodes_raw.items():
        node_status.append({
            'hostname':   nd.get('HOSTNAME', 'unknown'),
            'ip':         nd.get('IPV4_ADDRESS', ''),
            'ack':        nd.get('CONFIG_ACK_VERSION', ''),
            'last_seen':  nd.get('LAST_SEEN_TIMESTAMP', '0'),
            'node_state': nd.get('NODE_STATE', 'ACTIVE'),
        })
    node_status.sort(key=lambda n: n['hostname'])

    return {
        'current_config': {
            'eud':              conf.get('eud', 'wired'),
            'lan_ap_ssid':      conf.get('lan_ap_ssid', ''),
            'lan_ap_key':       conf.get('lan_ap_key', ''),
            'max_euds_per_node':conf.get('max_euds_per_node', '0'),
            'mesh_ssid':        conf.get('mesh_ssid', ''),
            'mesh_key':         conf.get('mesh_key', ''),
            'ipv4_network':     conf.get('ipv4_network', ''),
            'regulatory_domain':conf.get('regulatory_domain', 'US'),
            'acs':              conf.get('acs', 'n'),
            'mtx':              conf.get('mtx', 'n'),
            'mumble':           conf.get('mumble', 'n'),
            'auto_update':      conf.get('auto_update', 'n'),
            'admin_password':   conf.get('admin_password', ''),
        },
        'pending':      pending,
        'nodes':        node_status,
        'total_nodes':  len(node_status),
        'active_nodes': sum(1 for n in node_status if n['node_state'] == 'ACTIVE'),
    }

# ─────────────────────────────────────────────────────────────────────────────
# Admin HTML
# ─────────────────────────────────────────────────────────────────────────────
ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MANET Admin</title>
<style>__CSS__
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
.btn { padding: 9px 16px; border-radius: 8px; font-size: 12px; font-weight: 850; letter-spacing: 0;
       font-family: var(--font); cursor: pointer; border: 1px solid var(--accent); text-transform: none; background:var(--surface); color:var(--accent); transition:all .15s; }
.btn-stage  { background: var(--surface); color: var(--accent); border-color: var(--accent); }
.btn-stage:hover:not(:disabled)  { background: var(--accent); color: #ffffff; }
.btn-apply  { background: var(--accent2); color: var(--fer-black); border-color: var(--accent2); }
.btn-apply:hover:not(:disabled)  { background: var(--fer-black); color: #ffffff; border-color: var(--fer-black); }
.btn-force  { background: rgba(236,176,0,.10); color: var(--text); border-color: rgba(236,176,0,.48); }
.btn-force:hover:not(:disabled)  { background: var(--accent2); color: var(--fer-black); border-color: var(--accent2); }
.btn-cancel { background: transparent; color: #b42318; border-color: #e9b2ad; }
.btn-cancel:hover:not(:disabled) { background: #b42318; color: #ffffff; border-color: #b42318; }
:root[data-theme="dark"] .btn-stage { background: transparent; color: var(--accent2); border-color: var(--accent2); }
:root[data-theme="dark"] .btn-stage:hover:not(:disabled), :root[data-theme="dark"] .btn-apply:hover:not(:disabled), :root[data-theme="dark"] .btn-force:hover:not(:disabled) { background:#f8f6ef; color:var(--fer-black); border-color:#f8f6ef; }
:root[data-theme="dark"] .btn-apply { background: var(--accent2); color: var(--fer-black); border-color: var(--accent2); }
:root[data-theme="dark"] .btn-force { background: rgba(236,176,0,.10); color: var(--accent2); border-color: rgba(236,176,0,.48); }
:root[data-theme="dark"] .btn-cancel { color:#fca5a5; border-color:#7f1d1d; }
:root[data-theme="dark"] .btn-cancel:hover:not(:disabled) { background:#b42318; color:#ffffff; border-color:#b42318; }
.btn:disabled { opacity: 0.35; cursor: not-allowed; }
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
#force-modal { display:none; position:fixed; top:0;left:0;right:0;bottom:0;
               background:#000a; z-index:100; align-items:center; justify-content:center; }
#force-modal.show { display:flex; }
.modal-box { background: var(--surface); border: 1px solid #ef444440; border-radius: 4px;
             padding: 24px; max-width: 380px; }
.modal-title { color: var(--bad); font-size: 14px; font-weight: bold; margin-bottom: 10px; }
.modal-body { color: var(--muted); font-size: 12px; line-height: 1.6; margin-bottom: 16px; }
.modal-actions { display:flex; gap:10px; justify-content:flex-end; }
/* Toast */
#toast { position:fixed; bottom:20px; right:20px; background: var(--surface); border:1px solid var(--border);
         border-radius:4px; padding:10px 16px; font-size:12px; opacity:0; transition:opacity .3s;
         pointer-events:none; z-index:200; }
#toast.show { opacity:1; }
#toast.toast-ok   { border-color:#22c55e60; color:var(--good); }
#toast.toast-err  { border-color:#ef444460; color:var(--bad); }
#toast.toast-warn { border-color:#f9731660; color:var(--warn); }
</style>
</head>
<body>
<div id="app" style="display:block;">
  <div id="header">
    <div id="hdr-health" class="health-loading">
      <div id="hdr-health-dot"></div>
      <span id="hdr-health-label">ADMIN</span>
    </div>
    <div class="meta" id="hdr-hostname" style="color:var(--text);font-size:12px;font-weight:bold;padding-right:10px;border-right:1px solid var(--border);margin-right:10px;">—</div>
    <div class="spacer"></div>
    <a href="/" style="color:var(--muted);text-decoration:none;font-size:11px;padding-right:4px">&#8592; Status</a>
  </div>

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
        <button class="btn btn-stage"  id="btn-stage"  onclick="stageChanges()">Stage Changes</button>
        <button class="btn btn-apply"  id="btn-apply"  onclick="applyChanges(false)" disabled>Apply Now</button>
        <button class="btn btn-force"  id="btn-force"  onclick="showForceModal()" style="display:none">Force Apply</button>
        <button class="btn btn-cancel" id="btn-cancel" onclick="cancelPending()" style="display:none">Cancel</button>
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
      </div>
    </div>
  </div>

  <!-- Force apply modal -->
  <div id="force-modal">
    <div class="modal-box">
      <div class="modal-title">⚠ Force Apply</div>
      <div class="modal-body" id="force-modal-body">
        Not all nodes have acknowledged the pending config.
        Forcing apply will push changes to this node only — unreachable nodes
        will remain on the old config and may need manual intervention.
      </div>
      <div class="modal-actions">
        <button class="btn btn-cancel" onclick="closeForceModal()">Cancel</button>
        <button class="btn btn-force"  onclick="applyChanges(true)">Force Apply</button>
      </div>
    </div>
  </div>

  <div id="toast"></div>
</div>

<script>
const POLL_MS = 5000;
let STATUS = null;
let pollTimer = null;

// ── Init ────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('hdr-hostname').textContent = location.hostname;
  await refreshStatus();
  pollTimer = setInterval(refreshStatus, POLL_MS);
});

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
    const staleMs = (Date.now() / 1000) - parseInt(n.last_seen || 0);
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
  showMsg('Staging...', 'muted');
  try {
    const r   = await fetch('/api/admin/stage', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({config: cfg})
    });
    const res = await r.json();
    if (r.ok && res.ok) {
      toast('Changes staged — waiting for nodes to ACK', 'ok');
      showMsg('Staged. Waiting for ' + (STATUS && STATUS.total_nodes || '?') + ' nodes to ACK.', 'ok');
      await refreshStatus();
    } else {
      toast('Stage failed: ' + (res.error || r.status), 'err');
      showMsg('Stage failed.', 'err');
    }
  } catch(e) {
    toast('Stage error: ' + e, 'err');
  }
}

// ── Apply changes ─────────────────────────────────────────────────────────────
async function applyChanges(force) {
  closeForceModal();
  showMsg('Sending activate signal...', 'ok');
  try {
    const r   = await fetch('/api/admin/activate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({force: force})
    });
    const res = await r.json();
    if (r.ok && res.ok) {
      toast('Activate signal sent — applying in 60s', 'ok');
      showMsg('All nodes will apply in ~60s.', 'ok');
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
      showMsg('', '');
      await refreshStatus();
    }
  } catch(e) {}
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
    `Unreachable nodes (${total - acked}) will remain on the old config until they reconnect.`;
  document.getElementById('force-modal').classList.add('show');
}
function closeForceModal() {
  document.getElementById('force-modal').classList.remove('show');
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function showMsg(msg, cls) {
  const el = document.getElementById('action-msg');
  el.textContent = msg;
  el.style.color = cls === 'ok' ? 'var(--good)' : cls === 'err' ? 'var(--bad)' : 'var(--muted)';
}

let toastTimer = null;
function toast(msg, cls) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show toast-' + cls;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.className = '', 3000);
}
</script>
</body>
</html>"""

def render_admin_page():
    html = ADMIN_HTML
    html = html.replace('__CSS__', CSS)
    return html

def render_status_page():
    html = STATUS_HTML
    html = html.replace('__CSS__',     CSS)
    html = html.replace('__REFRESH__', str(REFRESH_MS))
    return html

# ─────────────────────────────────────────────────────────────────────────────
# HTTP Handler
# ─────────────────────────────────────────────────────────────────────────────
class MeshHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Suppress default access logs (use stderr only for errors)
        pass

    def send_403(self):
        self.send_response(403)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Forbidden')

    def send_html(self, body):
        encoded = body.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, obj):
        body = json.dumps(obj, default=str).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)

    def _proxy_to_perf(self):
        import urllib.request as _ur
        target = 'http://127.0.0.1:8081' + self.path
        req = _ur.Request(target, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in ('host', 'content-length'):
                req.add_header(k, v)
        length = int(self.headers.get('Content-Length', 0))
        data = self.rfile.read(length) if length else None
        if data:
            req.data = data
            req.add_header('Content-Length', str(len(data)))
        try:
            with _ur.urlopen(req, timeout=120) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ('transfer-encoding',):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def _is_perf_host(self):
        host = self.headers.get('Host', '').split(':')[0].lower()
        return host == 'perf.local' or host == 'perf'

    def do_GET(self):
        if self._is_perf_host():
            self._proxy_to_perf()
            return

        conf       = load_kv_file(MESH_CONF_FILE)
        client_ip  = self.client_address[0]
        parsed     = urlparse(self.path)
        path       = parsed.path.rstrip('/') or '/'

        # Admin page — no Basic auth
        if path == '/admin':
            self.send_html(render_admin_page())
            return

        # All other routes — IP restricted
        if not is_allowed_ip(client_ip, conf):
            self.send_403()
            return

        if path in ('/', '/index.html'):
            self.send_html(render_status_page())

        elif path == '/api/data':
            try:
                data = assemble_status_data()
                self.send_json(data)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())


        elif path == '/api/local':
            try:
                data = assemble_local_data()
                self.send_json(data)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())

        elif path.startswith('/api/peer/'):
            peer_ip = path[len('/api/peer/'):]
            # Validate: must look like an IP address
            try:
                ipaddress.ip_address(peer_ip)
            except ValueError:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Bad IP')
                return
            try:
                import urllib.request
                url = f'http://{peer_ip}:80/api/local'
                req = urllib.request.Request(url, headers={'User-Agent': 'manet-proxy/1'})
                with urllib.request.urlopen(req, timeout=4) as resp:
                    data = json.loads(resp.read().decode())
                self.send_json(data)
            except Exception as e:
                self.send_response(502)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())

        elif path == '/api/debug':
            try:
                _, orig_map = run_batctl_originators()
                neighbors  = run_batctl_neighbors()
                gateways   = run_batctl_gateways()
                nodes_raw  = parse_registry()
                debug = {
                    'gateways':  gateways,
                    'neighbors': neighbors,
                    'orig_map_sample': {k: v for k, v in list(orig_map.items())[:5]},
                    'node_macs': {
                        nid: {
                            'hostname': nd.get('HOSTNAME'),
                            'primary':  nd.get('MAC_ADDRESS'),
                            'all':      nd.get('MAC_ADDRESSES'),
                        }
                        for nid, nd in nodes_raw.items()
                    }
                }
                self.send_json(debug)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
        elif path == '/api/admin/status':
            try:
                data = assemble_admin_status()
                data['my_hostname'] = get_my_hostname()
                self.send_json(data)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found')

    def do_DELETE(self):
        if self._is_perf_host():
            self._proxy_to_perf()
            return

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'Not found')

    def do_POST(self):
        if self._is_perf_host():
            self._proxy_to_perf()
            return

        conf      = load_kv_file(MESH_CONF_FILE)
        client_ip = self.client_address[0]
        parsed    = urlparse(self.path)
        path      = parsed.path.rstrip('/') or '/'
        length    = int(self.headers.get('Content-Length', 0))
        body      = self.rfile.read(length) if length else b'{}'

        # Runtime control endpoints are called server-to-server by perf-dashboard,
        # so restrict them to localhost/mesh IPs. Admin POSTs are intentionally
        # not protected by Basic auth for the local field UI.
        if path in CONTROL_POST_PATHS:
            if not is_allowed_ip(client_ip, conf):
                self.send_403()
                return

        if path == '/api/admin/stage':
            try:
                req    = json.loads(body)
                config = req.get('config', {})
                if not config:
                    self.send_json({'ok': False, 'error': 'No config provided'})
                    return

                # Detect dangerous fields
                cur_ssid = conf.get('mesh_ssid', '')
                cur_key  = conf.get('mesh_key', '')
                cur_cidr = conf.get('ipv4_network', '')
                dangerous = (
                    (config.get('mesh_ssid',    cur_ssid) != cur_ssid) or
                    (config.get('mesh_key',     cur_key)  != cur_key)  or
                    (config.get('ipv4_network', cur_cidr) != cur_cidr)
                )

                version = make_config_version(config)
                pkg = {
                    'version':    version,
                    'issued_by':  get_my_hostname(),
                    'issued_at':  int(__import__('time').time()),
                    'activate_at': 0,   # 0 = stage only
                    'dangerous':  dangerous,
                    'config':     config,
                }
                save_pending_config(pkg)
                broadcast_config_package(pkg)

                # This node ACKs immediately (it's the one staging)
                try:
                    with open('/var/run/mesh_config_ack_version', 'w') as f:
                        f.write(version)
                except Exception:
                    pass

                self.send_json({'ok': True, 'version': version, 'dangerous': dangerous})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/admin/activate':
            try:
                req   = json.loads(body)
                force = req.get('force', False)
                pkg   = get_pending_config()
                if not pkg:
                    self.send_json({'ok': False, 'error': 'No pending config'})
                    return

                # Check all nodes have ACKed (unless force)
                if not force:
                    nodes_raw = parse_registry()
                    version   = pkg['version']
                    not_acked = [
                        nd.get('HOSTNAME', nid)
                        for nid, nd in nodes_raw.items()
                        if nd.get('CONFIG_ACK_VERSION', '') != version
                    ]
                    if not_acked:
                        self.send_json({
                            'ok': False,
                            'error': f'{len(not_acked)} nodes have not ACKed: {", ".join(not_acked)}'
                        })
                        return

                # Set activate_at to now + 60 seconds
                import time
                activate_at = int(time.time()) + 60
                pkg['activate_at'] = activate_at
                save_pending_config(pkg)
                broadcast_config_package(pkg)
                self.send_json({'ok': True, 'activate_at': activate_at})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/admin/cancel':
            try:
                clear_pending_config()
                # Clear local ACK state
                try:
                    os.remove('/var/run/mesh_config_ack_version')
                except Exception:
                    pass
                self.send_json({'ok': True})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/control/interface':
            try:
                req   = json.loads(body)
                iface = req.get('iface', '')
                state = req.get('state', '')  # 'up' or 'down'
                if iface not in ('wlan0', 'wlan1', 'wlan2') or state not in ('up', 'down'):
                    self.send_json({'ok': False, 'error': 'Invalid iface or state'})
                    return
                # Detect HaLow (morse_usb) — requires wpa_supplicant-s1g lifecycle
                et = subprocess.run(['ethtool', '-i', iface], capture_output=True, text=True, timeout=3)
                is_halow = any('morse' in l for l in et.stdout.splitlines() if l.startswith('driver:'))
                if is_halow:
                    svc = f'wpa_supplicant-s1g-{iface}.service'
                    if state == 'down':
                        subprocess.run(['systemctl', 'stop', svc], timeout=15)
                        subprocess.run(['ip', 'link', 'set', iface, 'down'], timeout=5)
                    else:
                        # cfg80211 regulatory must be set before wpa_supplicant_s1g starts.
                        # Restarting wpa_supplicant for a standard iface re-asserts country=EU.
                        for std_svc in ('wpa_supplicant@wlan0.service', 'wpa_supplicant@wlan1.service'):
                            r = subprocess.run(['systemctl', 'is-active', std_svc],
                                               capture_output=True, text=True, timeout=3)
                            if r.stdout.strip() == 'active':
                                subprocess.run(['systemctl', 'restart', std_svc], timeout=15)
                                import time; time.sleep(3)
                                break
                        subprocess.run(['ip', 'link', 'set', iface, 'up'], timeout=5)
                        subprocess.run(['systemctl', 'start', svc], timeout=15)
                        bat_r = subprocess.run(['batctl', 'if'], capture_output=True, text=True, timeout=5)
                        if not any(l.startswith(iface + ':') for l in bat_r.stdout.splitlines()):
                            subprocess.run(['batctl', 'if', 'add', iface], timeout=10)
                else:
                    svc = f'wpa_supplicant@{iface}.service'
                    if state == 'down':
                        subprocess.run(['batctl', 'if', 'del', iface], timeout=10)
                        subprocess.run(['systemctl', 'stop', svc], timeout=15)
                        subprocess.run(['ip', 'link', 'set', iface, 'down'], timeout=5)
                    else:
                        subprocess.run(['ip', 'link', 'set', iface, 'up'], timeout=5)
                        subprocess.run(['systemctl', 'start', svc], timeout=15)
                        bat_r = subprocess.run(['batctl', 'if'], capture_output=True, text=True, timeout=5)
                        if not any(l.startswith(iface + ':') for l in bat_r.stdout.splitlines()):
                            subprocess.run(['batctl', 'if', 'add', iface], timeout=10)
                self.send_json({'ok': True, 'iface': iface, 'state': state})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/control/txpower':
            try:
                req   = json.loads(body)
                iface = req.get('iface', '')
                dbm   = req.get('dbm')   # integer dBm
                if not iface or dbm is None:
                    self.send_json({'ok': False, 'error': 'Missing iface or dbm'})
                    return
                mbm = int(float(dbm) * 100)
                subprocess.run(['iw', 'dev', iface, 'set', 'txpower', 'fixed', str(mbm)], timeout=5)
                self.send_json({'ok': True, 'iface': iface, 'dbm': dbm})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/control/halow_channel':
            try:
                req     = json.loads(body)
                channel = req.get('channel')
                bw      = req.get('bw', '1MHz')
                if not channel:
                    self.send_json({'ok': False, 'error': 'Missing channel'})
                    return
                channel = int(channel)
                # EU S1G channel index → centre frequency in kHz
                eu_s1g_freq_khz = {1: 863500, 2: 864500, 3: 865500,
                                   4: 866500, 5: 867500, 6: 868500}
                freq_khz = eu_s1g_freq_khz.get(channel)
                if not freq_khz:
                    self.send_json({'ok': False, 'error': f'Invalid EU S1G channel {channel}'})
                    return
                bw_mhz = int(str(bw).replace('MHz', ''))
                # s1g_prim_chwidth: 0=1MHz primary, 1=2MHz primary
                # For 4MHz operation, primary channel is 2MHz → chwidth=1
                chwidth = {1: 0, 2: 1, 4: 1}.get(bw_mhz, 0)
                # Write override flag so channel-election.sh doesn't overwrite
                with open('/var/run/halow-channel-override', 'w') as f:
                    f.write(f'{channel},{bw}')
                # Update wpa_supplicant conf for persistence across reboots
                wpa_conf = '/etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf'
                with open(wpa_conf) as f:
                    content = f.read()
                content = re.sub(r'(channel\s*=\s*)\d+', rf'\g<1>{channel}', content)
                content = re.sub(r'(s1g_prim_chwidth\s*=\s*)\d+', rf'\g<1>{chwidth}', content)
                with open(wpa_conf, 'w') as f:
                    f.write(content)
                # Apply immediately via morse_cli (needs root; mesh-status runs as root)
                morse_result = subprocess.run(
                    ['morse_cli', '-i', 'wlan2', 'channel',
                     '-c', str(freq_khz), '-o', str(bw_mhz), '-p', str(bw_mhz)],
                    capture_output=True, text=True, timeout=10
                )
                if morse_result.returncode != 0:
                    # Fall back to wpa_supplicant restart if morse_cli fails
                    subprocess.run(['systemctl', 'restart', 'wpa_supplicant-s1g-wlan2.service'],
                                   timeout=15)
                self.send_json({'ok': True, 'channel': channel, 'freq_khz': freq_khz, 'bw': bw})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/control/wifi_channel':
            try:
                req     = json.loads(body)
                iface   = req.get('interface', req.get('iface', ''))
                channel = req.get('channel')
                if iface not in ('wlan0', 'wlan1'):
                    self.send_json({'ok': False, 'error': 'Invalid Wi-Fi interface'})
                    return
                freq = wifi_channel_to_freq(iface, channel)
                if not freq:
                    self.send_json({'ok': False, 'error': f'Invalid channel {channel} for {iface}'})
                    return

                conf_path = f'/etc/wpa_supplicant/wpa_supplicant-{iface}.conf'
                if not os.path.exists(conf_path):
                    self.send_json({'ok': False, 'error': f'Missing {conf_path}'})
                    return
                with open(conf_path) as f:
                    content = f.read()
                if re.search(r'frequency=\d+', content):
                    content = re.sub(r'frequency=\d+', f'frequency={freq}', content)
                else:
                    content = re.sub(r'(network=\{\n)', rf'\1    frequency={freq}\n', content, count=1)
                with open(conf_path, 'w') as f:
                    f.write(content)

                subprocess.run(['systemctl', 'restart', f'wpa_supplicant@{iface}.service'],
                               check=True, timeout=15)
                self.send_json({'ok': True, 'iface': iface, 'channel': channel, 'frequency': freq})
            except subprocess.CalledProcessError as e:
                self.send_json({'ok': False, 'error': str(e)})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/iperf/server/start':
            try:
                subprocess.run(['pkill', '-f', 'iperf3 -s'], capture_output=True)
                subprocess.Popen(['iperf3', '-s', '--one-off', '-J',
                                  '--logfile', '/tmp/iperf3-server.log'])
                self.send_json({'ok': True})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/iperf/server/stop':
            try:
                subprocess.run(['pkill', '-f', 'iperf3 -s'], capture_output=True)
                self.send_json({'ok': True})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/iperf/client/run':
            try:
                req        = json.loads(body)
                server_ip  = req.get('server_ip', '')
                test_type  = req.get('test_type', 'tcp_1stream')
                duration   = int(req.get('duration', 30))
                bitrate    = req.get('bitrate', '4M')
                parallel   = int(req.get('parallel', 1))
                reverse    = bool(req.get('reverse', False))

                cmd = ['iperf3', '-c', server_ip, '-t', str(duration), '-J']
                if test_type in ('udp_throughput', 'udp_jitter', 'packet_loss'):
                    cmd += ['-u', '-b', bitrate]
                if parallel > 1:
                    cmd += ['-P', str(parallel)]
                if reverse:
                    cmd += ['-R']

                r = subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 15)
                try:
                    result = json.loads(r.stdout)
                except Exception:
                    result = {'raw': r.stdout, 'stderr': r.stderr}
                if result.get('error'):
                    self.send_json({'ok': False, 'error': result.get('error'), 'result': result})
                else:
                    self.send_json({'ok': r.returncode == 0, 'error': r.stderr.strip(), 'result': result})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/ping/run':
            try:
                req    = json.loads(body)
                target = req.get('target', '')
                count  = int(req.get('count', 100))
                interval = float(req.get('interval', 0.2))
                if not target:
                    self.send_json({'ok': False, 'error': 'Missing target'})
                    return
                r = subprocess.run(
                    ['ping', '-c', str(count), '-i', str(interval), target],
                    capture_output=True, text=True, timeout=count * interval + 10
                )
                # Parse ping summary line
                rtt_match  = re.search(r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', r.stdout)
                loss_match = re.search(r'(\d+)% packet loss', r.stdout)
                result = {
                    'output':   r.stdout,
                    'rtt_min':  float(rtt_match.group(1)) if rtt_match else None,
                    'rtt_avg':  float(rtt_match.group(2)) if rtt_match else None,
                    'rtt_max':  float(rtt_match.group(3)) if rtt_match else None,
                    'rtt_mdev': float(rtt_match.group(4)) if rtt_match else None,
                    'loss_pct': int(loss_match.group(1))  if loss_match else None,
                }
                self.send_json({'ok': True, 'result': result})
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
    server = ThreadedServer(('0.0.0.0', port), MeshHandler)
    print(f'MANET Status Server listening on port {port}')
    print(f'  Status:  http://localhost:{port}/')
    print(f'  Admin:   http://localhost:{port}/admin  (no auth)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutdown.')
        server.shutdown()
