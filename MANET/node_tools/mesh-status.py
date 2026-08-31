#!/usr/bin/env python3
"""
MANET Node Status Web Server
-----------------------------
Serves mesh network status and topology information on port 8080.

Access (everything here is restricted to localhost + the EUD/mesh subnet;
nothing is reachable from the uplink/LAN side):
  /          - Status page, no password
  /api/data  - JSON data endpoint, no password
  /manage/*  - Management UI, requires the provisioned admin password.
               Routes come from manet_manage.py, mixed into this handler.
               There is no second listening port.
  /api/admin/* - Mesh config staging/apply, requires the same password.
  /admin     - Legacy path, redirects into /manage/

Reads:
  /etc/mesh.conf                - Node configuration
  /etc/mesh_ipv4_state          - Current IP state
  /var/run/mesh_node_registry   - Peer registry (built by mesh-registry-builder.sh)

Calls:
  batctl o   - Originator throughput table
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
import urllib.request
import hashlib
import hmac
import html
from urllib.parse import urlparse, parse_qs, quote

# Radio control lives in one place, shared with mesh-radio-state.py so an
# Alfred-staged change and a local one do exactly the same thing.
from manet_manage import ManageRoutes
from manet_peer_radios import interfaces_for_telemetry, peer_status_panel
from mesh_config import apply_local_to_conf, local_changes, mesh_changes, strip_local_keys
from manet_radio import (
    HALOW_BW_TXPOWER_CAP_DBM, halow_channel_options,
    _format_halow_bw, get_halow_driver_info, wifi_channel_to_freq, _fmt_dbm,
    parse_phy_txpower_options, txpower_choices_from_cap, txpower_options_for_iface,
    txpower_request_allowed, unsupported_txpower_response, get_halow_bw_txpower_cap,
    get_iface_txpower_cap, read_iface_txpower_dbm, set_iface_txpower_verified,
    apply_txpower, apply_halow_channel, apply_wifi_channel, apply_uplink_wifi,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
REGISTRY_FILE   = "/var/run/mesh_node_registry"
MESH_CONF_FILE  = "/etc/mesh.conf"
# Seconds before /run/gps_status.json counts as stale. gps-reader polls every 5 s;
# node-manager uses the same budget so the UI and the mesh agree on the position.
GPS_FIX_MAX_AGE = 60
MESH_STATE_FILE = "/etc/mesh_ipv4_state"
PORT            = 8080
REFRESH_MS      = 15000   # Status page polling interval (ms)
PERF_AUTH_COOKIE = 'manet_perf_auth'
PERF_AUTH_COOKIE_MAX_AGE = 15552000
# The management UI lives behind this prefix on port 80, served in-process by
# the ManageRoutes mixin. Nothing reaches it without the password cookie.
MANAGE_PREFIX = '/manage'
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
CONTROL_POST_PATHS = {
    '/api/control/interface',
    '/api/control/txpower',
    '/api/control/halow_channel',
    '/api/control/wifi_channel',
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

def _machine_token_salt():
    for path in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
        try:
            with open(path) as f:
                value = f.read().strip()
            if value:
                return value
        except Exception:
            pass
    return socket.gethostname()

def get_provisioned_manage_password(conf=None):
    conf = conf or load_kv_file(MESH_CONF_FILE)
    for key in ('admin_password', 'radio_password', 'lan_ap_key'):
        value = conf.get(key, '').strip()
        if value:
            return value
    return ''

def get_perf_auth_token():
    conf = load_kv_file(MESH_CONF_FILE)
    manage_password = get_provisioned_manage_password(conf)
    if not manage_password:
        return ''
    raw = f'{manage_password}|perf-local|v1|{_machine_token_salt()}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def is_valid_perf_auth_token(token):
    expected = get_perf_auth_token()
    return bool(expected and token) and hmac.compare_digest(str(token), expected)

def parse_cookie_header(header):
    cookies = {}
    if not header:
        return cookies
    for part in header.split(';'):
        if '=' not in part:
            continue
        key, value = part.split('=', 1)
        cookies[key.strip()] = value.strip()
    return cookies

def normalize_local_redirect(target):
    target = str(target or '/').strip()
    if not target.startswith('/'):
        return '/'
    if target.startswith('//'):
        return '/'
    return target




















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
      mbps_map: {mac -> best throughput}  (indexes both orig + nexthop MACs)
      orig_map: {orig_mac -> {mbps, nexthop, iface}}  (best path per originator)

    The mesh runs BATMAN_V, whose metric is throughput in Mbit/s — batctl
    prints it as `%u.%u` (originators.c), so 43.2 means 43.2 Mbit/s. It is
    carried through as-is; there is no 0-255 link quality here to scale to.
    """
    mbps_map = {}
    orig_map = {}  # orig_mac -> {'mbps': float, 'nexthop': str, 'iface': str}

    def _set_mbps(mac, mbps):
        if mac and (mac not in mbps_map or mbps > mbps_map[mac]):
            mbps_map[mac] = mbps

    try:
        r = subprocess.run(['batctl', 'o', '-n'],
                           capture_output=True, text=True, timeout=5)
        orig_best = {}  # orig_mac -> (best_mbps, nexthop_mac, outgoing_iface)
        for line in r.stdout.splitlines():
            m = re.match(
                r'[\s*]+([0-9a-f:]{17})\s+[\d.]+(?:ms|s)\s+\(\s*([\d.]+)\)\s+([0-9a-f:]{17})(?:\s+\[\s*(\S+)\s*\])?',
                line)
            if m:
                orig    = norm_mac(m.group(1))
                mbps    = float(m.group(2))
                nexthop = norm_mac(m.group(3))
                iface   = (m.group(4) or '').strip()
                prev = orig_best.get(orig)
                if prev is None or mbps > prev[0]:
                    orig_best[orig] = (mbps, nexthop, iface)

        for orig, (mbps, nexthop, iface) in orig_best.items():
            _set_mbps(orig, mbps)
            if nexthop != orig:
                _set_mbps(nexthop, mbps)
            orig_map[orig] = {'mbps': mbps, 'nexthop': nexthop, 'iface': iface}
    except Exception:
        pass
    return mbps_map, orig_map

def run_batctl_neighbors():
    """Return list of {iface, mac, mbps} from `batctl n`."""
    neighbors = []
    try:
        r = subprocess.run(['batctl', 'n', '-n'],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            # wlan0   aa:bb:cc:dd:ee:ff   0.500ms   (240)
            m = re.match(r'\s*(\S+)\s+([0-9a-f:]{17})\s+[\d.]+(?:ms|s)\s+\(\s*([\d.]+)\)', line)
            if m:
                neighbors.append({
                    'iface': m.group(1),
                    'mac':   norm_mac(m.group(2)),
                    'mbps':  float(m.group(3)),
                })
    except Exception:
        pass
    return neighbors

def run_batctl_gateways():
    """Return list of {mac, mbps, selected} from `batctl gwl`.

    Format: => <gw_mac>  <age>s (  <Mbit/s>)  <nexthop_mac> [<if>]
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
            mbps_m = re.search(r'\(\s*([\d.]+)\s*\)', line)
            if mac_m:
                gateways.append({
                    'mac':      norm_mac(mac_m.group(1)),
                    'mbps':     float(mbps_m.group(1)) if mbps_m else 0.0,
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

def best_orig_entry_for_node(node, orig_map):
    if not isinstance(node, dict):
        return None
    node_all_macs = set(node.get('all_macs', []))
    raw_mac = norm_mac(node.get('mac', ''))
    if raw_mac:
        node_all_macs.add(raw_mac)
    best_entry = None
    for omac, odata in orig_map.items():
        if omac in node_all_macs:
            if best_entry is None or odata.get('mbps', 0) > best_entry.get('mbps', 0):
                best_entry = odata
    return best_entry


def resolve_hop_count(node_id, node_by_id, orig_map, mac_to_node_id, visited=None):
    if visited is None:
        visited = set()
    if node_id in visited:
        return None
    visited.add(node_id)

    node = node_by_id.get(node_id)
    if not node or node.get('is_me'):
        return None
    if node.get('is_direct'):
        return 1

    best_entry = best_orig_entry_for_node(node, orig_map)
    if not best_entry:
        return None

    node_all_macs = set(node.get('all_macs', []))
    raw_mac = norm_mac(node.get('mac', ''))
    if raw_mac:
        node_all_macs.add(raw_mac)

    nexthop = norm_mac(best_entry.get('nexthop', ''))
    if not nexthop:
        return None
    if nexthop in node_all_macs:
        return 1

    via_id = mac_to_node_id.get(nexthop)
    if not via_id or via_id == node_id:
        return None

    via_hops = resolve_hop_count(via_id, node_by_id, orig_map, mac_to_node_id, visited)
    if via_hops is None:
        return None
    return via_hops + 1

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
                pm = re.search(r'txpower ([\d.]+) dBm', line)
                if pm: iw_info[cur_iface]['txpower_dbm'] = pm.group(1)
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
    phy_txpower_options = {}
    try:
        r3 = subprocess.run(['iw', 'phy'], capture_output=True, text=True, timeout=5)
        phy_txpower_options = parse_phy_txpower_options(r3.stdout)
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
            iw_info[iname]['band_label'] = 'HaLow'
        else:
            # Try runtime freq first, fall back to phy capability
            freq_str = iw_info[iname].get('freq', '')
            try:
                freq_f = float(freq_str)
                if freq_f < 2.0:
                    iw_info[iname]['band_label'] = 'HaLow'
                elif freq_f < 3.0:
                    iw_info[iname]['band_label'] = '2.4 GHz'
                else:
                    iw_info[iname]['band_label'] = '5 GHz'
            except ValueError:
                wiphy_num = iw_info[iname].get('wiphy', '')
                iw_info[iname]['band_label'] = phy_band.get(wiphy_num, '')
        cap = get_iface_txpower_cap(iname)
        if cap:
            iw_info[iname]['txpower_cap_dbm'] = cap
            iw_info[iname]['txpower_options_dbm'] = txpower_options_for_iface(
                iname, cap, iw_info[iname].get('txpower_dbm', '')
            )

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

        # bat0/br0 report operstate=UNKNOWN (virtual iface); derive from UP flag instead
        display_state = state
        if state == 'UNKNOWN' and name in ('bat0', 'br0'):
            display_state = 'UP' if 'UP' in flags else 'DOWN'

        ifaces.append({
            'name':     name,
            'role':     role,
            'health':   health,
            'detail':   detail,
            'faults':   faults,
            'addrs':    addrs,
            'state':    display_state,
            'channel':  iw.get('channel', ''),
            'freq_mhz': iw.get('freq_mhz', ''),
            'txpower_dbm': iw.get('txpower_dbm', ''),
            'txpower_cap_dbm': iw.get('txpower_cap_dbm', ''),
            'txpower_options_dbm': iw.get('txpower_options_dbm', []),
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
    Return list of active {mac, ip, hostname} from dnsmasq leases.
    """
    euds = []
    now = int(time.time())
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
                        try:
                            expiry = int(parts[0])
                        except Exception:
                            expiry = 0
                        if expiry and expiry < now:
                            continue
                        euds.append({
                            'mac':      parts[1],
                            'ip':       parts[2],
                            'hostname': parts[3] if parts[3] != '*' else '',
                            'expires_in': max(0, expiry - now) if expiry else None,
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


def enrich_interfaces_with_registry_mcs(ifaces, node_data):
    if not ifaces or not isinstance(node_data, dict):
        return ifaces

    mcs_by_iface = {
        'wlan0': {
            'tx_mcs': node_data.get('WIFI_24_TX_MCS', ''),
            'rx_mcs': node_data.get('WIFI_24_RX_MCS', ''),
        },
        'wlan1': {
            'tx_mcs': node_data.get('WIFI_5_TX_MCS', ''),
            'rx_mcs': node_data.get('WIFI_5_RX_MCS', ''),
        },
        'wlan2': {
            'tx_mcs': node_data.get('HALOW_TX_MCS', ''),
            'rx_mcs': node_data.get('HALOW_RX_MCS', ''),
        },
    }

    for iface in ifaces:
        if not isinstance(iface, dict):
            continue
        extra = mcs_by_iface.get(iface.get('name', ''), {})
        iface['tx_mcs'] = extra.get('tx_mcs', '')
        iface['rx_mcs'] = extra.get('rx_mcs', '')
    return ifaces

_POWER_CACHE = {'at': 0.0, 'data': {'available': False}}

def get_power_status():
    """Throttling / under-voltage state, from manet-power-status.sh --json.

    The decoding lives in the shell script because the motd banner needs it
    too, and one reading of the bitmask is enough for the whole node. Cached
    briefly: every open dashboard polls this, and the answer changes slowly.
    """
    now = time.time()
    if now - _POWER_CACHE['at'] < 10:
        return _POWER_CACHE['data']
    data = {'available': False}
    try:
        out = subprocess.run(
            ['/usr/local/bin/manet-power-status.sh', '--json'],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if out:
            data = json.loads(out)
    except Exception:
        pass
    _POWER_CACHE['at'] = now
    _POWER_CACHE['data'] = data
    return data

def assemble_local_data():
    conf     = load_kv_file(MESH_CONF_FILE)
    state    = load_kv_file(MESH_STATE_FILE)
    hostname = get_my_hostname()
    battery  = get_battery()
    ifaces   = get_interfaces()
    euds     = get_connected_euds()
    services = get_running_services()
    uptime   = get_local_uptime()
    power    = get_power_status()

    # Pull self entry from registry for extra fields
    nodes_raw = parse_registry()
    my_mac    = get_my_mac()
    my_node   = {}
    for nid, ndata in nodes_raw.items():
        if norm_mac(ndata.get('MAC_ADDRESS', '')) == my_mac or ndata.get('HOSTNAME', '') == hostname:
            my_node = ndata
            break

    ifaces = enrich_interfaces_with_registry_mcs(ifaces, my_node)

    # GPS — read directly from gps-reader output for freshness; fall back to registry.
    # gps-reader stamps every write, so a frozen timestamp means it died or hung
    # while still holding a fix. Presenting that position as current is worse than
    # showing none, so a stale file is treated the same as no fix.
    gps = {'available': False, 'lat': '', 'lon': '', 'alt': ''}
    try:
        with open('/run/gps_status.json') as _gf:
            _gd = json.load(_gf)
        if _gd.get('has_fix') and time.time() - _gd.get('timestamp', 0) <= GPS_FIX_MAX_AGE:
            gps = {
                'available': True,
                'lat': str(_gd['latitude']),
                'lon': str(_gd['longitude']),
                'alt': str(_gd.get('altitude', '')),
            }
    except Exception:
        pass
    if not gps['available']:
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
        'power':     power,
        'eud_mode':  conf.get('eud', 'wired'),
        'ap_ssid':   conf.get('lan_ap_ssid', ''),
        'mesh_ssid': conf.get('mesh_ssid', ''),
    }

def assemble_peer_data(peer_ip):
    """Peer detail for the status drawer, entirely from the registry.

    This used to proxy the peer's own /api/local over HTTP. Everything it
    showed is replicated over Alfred now, so a peer that is briefly
    unreachable still renders — and one unreachable peer no longer stalls the
    page waiting on a timeout.
    """
    for ndata in parse_registry().values():
        if ndata.get('IPV4_ADDRESS') != peer_ip:
            continue

        battery = None
        if ndata.get('BATTERY_PERCENTAGE'):
            try:
                battery = {'percentage': int(ndata['BATTERY_PERCENTAGE']),
                           'status': 'unknown', 'voltage_v': None, 'current_ma': None,
                           'power_w': None, 'charging': None, 'timestamp': None}
            except ValueError:
                battery = None

        try:
            eud_count = int(ndata.get('EUD_COUNT') or 0)
        except (TypeError, ValueError):
            eud_count = 0
        panel = peer_status_panel(ndata)

        return {
            'hostname':   ndata.get('HOSTNAME', ''),
            'ip':         peer_ip,
            'mac':        ndata.get('MAC_ADDRESS', ''),
            'uptime':     fmt_uptime(ndata.get('UPTIME_SECONDS', '')),
            'battery':    battery,
            'gps': {
                'available': bool(ndata.get('GPS_LATITUDE')),
                'lat':       ndata.get('GPS_LATITUDE', ''),
                'lon':       ndata.get('GPS_LONGITUDE', ''),
                'alt':       ndata.get('GPS_ALTITUDE', ''),
            },
            'interfaces': panel['interfaces'],
            'euds':       panel['euds'],
            'eud_count':  panel['eud_count'] if panel['eud_count'] else eud_count,
            'services':   panel['services'],
            'eud_mode':   ndata.get('EUD_MODE', ''),
            'ap_ssid':    ndata.get('AP_SSID', ''),
            'mesh_ssid':  '',
            'stale':      ndata.get('NODE_STATE', 'ACTIVE') != 'ACTIVE',
        }
    return None


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
    local_battery = get_battery()
    orig_mbps, orig_map = run_batctl_originators()
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
        # Use the registry gateway with the best throughput as "selected"
        def _node_mbps(nid):
            nd = nodes_raw[nid]
            for m in nd.get('MAC_ADDRESSES', '').split(','):
                t = orig_mbps.get(norm_mac(m.strip()))
                if t is not None:
                    return t
            return 0
        best_gw_nid = max(registry_gw_nodes, key=_node_mbps)
        best_gw_nd  = nodes_raw[best_gw_nid]
        selected_gw = norm_mac(best_gw_nd.get('MAC_ADDRESS', ''))
        gateways    = [{'mac': selected_gw, 'mbps': 0, 'selected': True}]

    node_list = []
    self_found = False

    for nid, ndata in nodes_raw.items():
        raw_mac  = ndata.get('MAC_ADDRESS', '')
        node_mac = norm_mac(raw_mac)
        hostname = ndata.get('HOSTNAME', 'unknown')

        # Throughput: check primary mac then all macs
        mbps = orig_mbps.get(node_mac)
        if mbps is None:
            for alt_mac in ndata.get('MAC_ADDRESSES', '').split(','):
                alt = norm_mac(alt_mac)
                if alt in orig_mbps:
                    mbps = orig_mbps[alt]
                    break

        is_me = (my_mac and node_mac == my_mac) or (hostname == my_host)
        if is_me:
            mbps = None
            self_found = True

        battery = {'percentage': int(ndata['BATTERY_PERCENTAGE'])} if ndata.get('BATTERY_PERCENTAGE') else None
        if is_me and local_battery and local_battery.get('percentage') is not None:
            battery = local_battery

        all_node_macs = [norm_mac(m) for m in ndata.get('MAC_ADDRESSES', '').split(',') if m.strip()]
        is_direct = any(m in neighbor_macs for m in all_node_macs) or node_mac in neighbor_macs
        gw_info   = gw_mac_map.get(node_mac)
        best_link = {}
        if not is_me:
            for omac, odata in orig_map.items():
                if omac == node_mac or omac in all_node_macs:
                    if not best_link or odata.get('mbps', 0) > best_link.get('mbps', 0):
                        best_link = {
                            'iface':   odata.get('iface', ''),
                            'nexthop': odata.get('nexthop', ''),
                            'mbps':    odata.get('mbps'),
                        }

        node_list.append({
            'id':           nid,
            'hostname':     hostname,
            'mac':          raw_mac,
            'ip':           ndata.get('IPV4_ADDRESS', ''),
            'mbps':         mbps,
            'is_me':        is_me,
            'is_direct':    is_direct or is_me,
            'is_gateway':   ndata.get('IS_GATEWAY', 'false').lower() == 'true',
            'is_selected_gw': bool(selected_gw and (
                node_mac == selected_gw or
                selected_gw in [norm_mac(m) for m in ndata.get('MAC_ADDRESSES','').split(',') if m.strip()]
            )),
            'uptime':       fmt_uptime(ndata.get('UPTIME_SECONDS', '')),
            'cpu':          (f"{float(ndata['CPU_LOAD_AVERAGE']):.2f}" if ndata.get('CPU_LOAD_AVERAGE') else ''),
            'battery':      battery,
            'mumble':       ndata.get('IS_MUMBLE_SERVER', 'false').lower() == 'true',
            'mediamtx':     ndata.get('IS_MEDIAMTX_SERVER', 'false').lower() == 'true',
            'ntp':          ndata.get('IS_NTP_SERVER', 'false').lower() == 'true',
            'state':        ndata.get('NODE_STATE', 'ACTIVE'),
            'ch_2g':        ndata.get('DATA_CHANNEL_2_4', ''),
            'ch_5g':        ndata.get('DATA_CHANNEL_5_0', ''),
            'limp':         ndata.get('IS_IN_LIMP_MODE', 'false').lower() == 'true',
            'all_macs':     [norm_mac(m) for m in ndata.get('MAC_ADDRESSES', '').split(',') if m.strip()],
            'best_link':    best_link,
            'hop_count':    None,
            'last_seen':    ndata.get('LAST_REGISTRY_UPDATE', ndata.get('LAST_SEEN_TIMESTAMP', '0')),
        })

    # If self not in registry, inject a placeholder
    if not self_found and my_host:
        node_list.insert(0, {
            'id': 'self',
            'hostname': my_host,
            'mac': my_mac or '',
            'ip': (state.get('CURRENT_IPV4') or ''),
            'mbps': None, 'is_me': True, 'is_direct': True,
            'is_gateway': False, 'is_selected_gw': False,
            'uptime': '', 'cpu': '', 'battery': None,
            'mumble': False, 'mediamtx': False, 'ntp': False,
            'state': 'ACTIVE', 'ch_2g': '', 'ch_5g': '', 'limp': False,
            'hop_count': None, 'last_seen': str(int(time.time())),
        })

    node_list.sort(key=lambda n: (not n['is_me'], -(n['mbps'] if n['mbps'] is not None else -1)))

    # ── Build topology edges from batctl o nexthop data ──
    # mac_to_node_id: every MAC (all interfaces) -> node_id
    mac_to_node_id = {}
    for node in node_list:
        for m in node.get('all_macs', []):
            mac_to_node_id[m] = node['id']
        mac_to_node_id[norm_mac(node['mac'])] = node['id']
    node_by_id = {node['id']: node for node in node_list}

    for node in node_list:
        if node.get('is_me'):
            continue
        node['hop_count'] = resolve_hop_count(node['id'], node_by_id, orig_map, mac_to_node_id)

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
                    if best_entry is None or odata['mbps'] > best_entry['mbps']:
                        best_entry = odata

            if best_entry is None:
                # Node in registry but not in originator table — no path known
                edges.append({
                    'source':  self_node['id'],
                    'target':  node['id'],
                    'type':    'unknown',
                    'via':     None,
                    'mbps':    node['mbps'],
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
                    'mbps':   node['mbps'],
                })
            else:
                # nexthop is a different node -> multi-hop via that node
                via_id = mac_to_node_id.get(nexthop)
                edges.append({
                    'source': self_node['id'],
                    'target': node['id'],
                    'type':   'multihop',
                    'via':    via_id,
                    'mbps':   node['mbps'],
                })

        # Also add edges between non-self nodes where we can infer adjacency:
        # if node A routes to node C via node B, we know B->C exists
        inferred = set()
        for edge in list(edges):
            if edge['type'] == 'multihop' and edge['via']:
                pair = tuple(sorted([edge['via'], edge['target']]))
                if pair not in inferred:
                    inferred.add(pair)
                    # Inferred edge: use the target node's throughput (conservative)
                    target_node = next((n for n in node_list if n['id'] == edge['target']), None)
                    edges.append({
                        'source': edge['via'],
                        'target': edge['target'],
                        'type':   'inferred',
                        'via':    None,
                        'mbps':   target_node['mbps'] if target_node else None,
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
  --accent:   #02000d;
  --accent2:  #ecb000;
  --info:     #02000d;
  --fer-yellow:#ecb000;
  --fer-black:#02000d;
  --text:     #02000d;
  --muted:    #615f68;
  --good:     #22c55e;
  --ok:       #ecb000;
  --warn:     #ecb000;
  --bad:      #ef4444;
  --gw:       #ecb000;
  --self:     #ecb000;
  --shadow:   0 18px 50px rgba(2,0,13,.10);
  --font:     Roobert, Arial, sans-serif;
}
:root[data-theme="dark"] {
  --bg:       #02000d;
  --surface:  #121118;
  --panel:    #0b0a12;
  --border:   #34313b;
  --border2:  #24212b;
  --accent:   #f8f6ef;
  --accent2:  #ecb000;
  --info:     #9fa8ff;
  --text:     #f8f6ef;
  --muted:    #aaa5b2;
  --gw:       #ecb000;
  --self:     #ecb000;
  --shadow:   0 18px 50px rgba(0,0,0,.36);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html { min-height: 100%; scroll-behavior: smooth; }
body { min-height: 100%; background: var(--bg); color: var(--text); font-family: var(--font); font-size: 13px; overflow-x: hidden; overflow-y: auto; -webkit-overflow-scrolling: touch; overscroll-behavior-y: auto; text-rendering: optimizeLegibility; }
body {
  background:
    radial-gradient(circle at top left, rgba(236,176,0,.18), transparent 28%),
    radial-gradient(circle at top right, rgba(2,0,13,.08), transparent 24%),
    var(--bg);
}

/* ── Layout ── */
#app { display: flex; flex-direction: column; min-height: 100vh; }
#header { background: rgba(255,255,255,.94); backdrop-filter: blur(18px); border-bottom: 1px solid var(--border2); padding: 10px 14px 10px 0; display: grid; grid-template-columns: auto 1fr; grid-template-areas: "brand brand" "meta meta" "actions actions"; row-gap: 8px; column-gap: 12px; flex-shrink: 0; min-height: 58px; box-shadow: 0 1px 0 rgba(2,0,13,.05); position: relative; }
#header::after { content:''; position:absolute; left:0; right:0; bottom:0; height:2px; background: linear-gradient(90deg, rgba(236,176,0,.92) 0 36%, rgba(236,176,0,.28) 36% 68%, transparent 68%); pointer-events:none; }
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
.health-warn { color: #8a6a00; background: rgba(236,176,0,.12); }
.health-fault{ color: #b42318; background: rgba(180,35,24,.08); }
.health-loading { color: var(--muted); background: transparent; }
.hdr-brand { grid-area: brand; display:flex; align-items:center; gap:8px; min-width:0; padding-left:12px; }
.fer-lockup { display:flex; align-items:center; justify-content:flex-start; align-self:center; height:58px; padding:0 12px 0 0; border-right:1px solid var(--border); color:var(--fer-black); overflow:hidden; flex:0 0 auto; }
/* the badge is near-square, so height drives the size and width follows the
   aspect ratio — a fixed width would letterbox it and leave dead space */
.fer-logo-img { display:block; width:auto; height:44px; max-width:100%; object-fit:contain; object-position:left center; filter:none; transition:height .18s ease; }
:root[data-theme="dark"] .fer-lockup { color:#ffffff; }
/* no brightness(0) invert(1) here — that flattens the badge to a solid silhouette */
.hdr-logo { color: var(--text); font-size: 17px; letter-spacing: 0; font-weight: 900; display:flex; align-items:center; min-height:46px; line-height:1; }
.hdr-logo span { color: var(--accent2); }
.theme-toggle { border:1px solid var(--accent2); background:rgba(236,176,0,.10); color:var(--text); border-radius:999px; padding:6px 10px; font-family:var(--font); font-size:11px; font-weight:850; cursor:pointer; min-width:74px; }
.theme-toggle:hover { background:var(--accent2); color:var(--fer-black); box-shadow:0 8px 22px rgba(236,176,0,.20); }
.perf-link-btn { border:1px solid var(--accent2); background:var(--accent2); color:var(--fer-black); border-radius:999px; padding:6px 12px; font-family:var(--font); font-size:11px; font-weight:850; cursor:pointer; min-width:92px; transition:background .18s ease,color .18s ease,box-shadow .18s ease,transform .18s ease; box-shadow:0 8px 20px rgba(236,176,0,.16); }
.perf-link-btn:hover { background:#f6c62f; color:var(--fer-black); box-shadow:0 10px 24px rgba(236,176,0,.26); transform:translateY(-1px); }
:root[data-theme="dark"] .perf-link-btn { background:var(--accent2); color:var(--fer-black); border-color:var(--accent2); }
:root[data-theme="dark"] .perf-link-btn:hover { background:#f6c62f; color:var(--fer-black); box-shadow:0 10px 24px rgba(236,176,0,.28); }
/* Centre identity items */
#header .meta { color: var(--muted); font-size: 12px; display: flex; align-items: center; }
#hdr-meta { grid-area: meta; display:flex; align-items:center; gap:0; flex-wrap:wrap; min-width:0; padding-left:12px; }
#hdr-hostname { color: var(--text); font-size: 13px; font-weight: 800; padding-right: 10px;
                border-right: 1px solid var(--border); margin-right: 10px; }
#hdr-ssid { color: var(--text); }
#header .spacer { display:none; }
/* Right-side items */
#hdr-right { grid-area: actions; display: flex; align-items: center; gap: 12px; flex-wrap:wrap; padding-left:12px; }
#hdr-gw-label { font-size: 11px; }
.gw-ok   { color: var(--good); }
.gw-none { color: var(--muted); }
.pill { display: inline-block; padding: 4px 9px; border-radius: 999px; font-size: 10px; font-weight: 800; letter-spacing: 0; }
.pill-accent { background: rgba(236,176,0,.10); color: var(--text); border: 1px solid rgba(236,176,0,.30); }
.pill-gw     { background: rgba(236,176,0,.14); color: var(--fer-black); border: 1px solid rgba(236,176,0,.38); }
.pill-self   { background: rgba(236,176,0,.16); color: var(--fer-black); border: 1px solid rgba(236,176,0,.34); }

/* ── Main Panels ── */
#main { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 16px; flex: 1; padding: 16px; align-items: start; }
#topo-panel {
  position: relative; min-width: 0; min-height: 520px;
  /* Flat panel on purpose: decorative blobs behind the canvas read as
     mesh activity at a glance and pulled the eye off the real nodes. */
  background: var(--panel);
  border: 1px solid var(--border2);
  border-radius: 18px;
  overflow: hidden;
  box-shadow:
    inset 0 0 0 1px rgba(2,0,13,.04),
    0 22px 52px rgba(2,0,13,.08);
}
#topo-panel canvas { width: 100%; height: 100%; display: block; touch-action: pan-y pinch-zoom; }
#side-panel { width: 100%; overflow: visible; border: 1px solid var(--border2); border-radius: 18px; background: var(--surface); box-shadow: 0 18px 44px rgba(2,0,13,.06); }

/* ── Node Table ── */
.section-hdr { padding: 12px 14px; font-size: 11px; font-weight: 800; letter-spacing: .3px; color: var(--muted); text-transform: none; border-bottom: 1px solid var(--border2); position: sticky; top: 0; background: color-mix(in srgb, var(--surface) 92%, transparent); backdrop-filter: blur(12px); z-index: 1; }
.node-row { padding: 10px 12px; border-bottom: 1px solid var(--border2); cursor: pointer; transition: background .18s ease, transform .18s ease; }
.node-row:hover { background: rgba(236,176,0,.08); transform: translateY(-1px); }
.node-row.is-me { border-left: 2px solid var(--self); }
.node-row.is-gw { border-left: 2px solid var(--gw); }
.node-row.is-me.is-gw { border-left: 2px solid var(--gw); }
.node-row.peer-selected { background: rgba(236,176,0,.12); outline: 1px solid rgba(236,176,0,.36); outline-offset: -1px; }
.node-summary { min-width: 0; }
.node-name { font-size: 12px; font-weight: bold; display: flex; align-items: center; gap: 5px; margin-bottom: 2px; }
.node-ip { color: var(--muted); font-size: 10px; margin-bottom: 2px; }
.node-meta { display: flex; gap: 5px; flex-wrap: wrap; }
.node-inline-detail { margin-top: 12px; border: 1px solid var(--border2); border-radius: 14px; overflow: hidden; background: color-mix(in srgb, var(--surface) 96%, rgba(236,176,0,.06)); box-shadow: 0 16px 34px rgba(2,0,13,.05); }
.inline-detail-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 10px 12px; border-bottom: 1px solid var(--border2); background: transparent; }
.inline-detail-title { font-size: 12px; font-weight: 800; color: var(--text); }
.node-inline-detail .section-hdr { position: static; top: auto; }
.badge { padding: 3px 7px; border-radius: 999px; font-size: 10px; font-weight: 750; }
.badge-link-great  { background: rgba(22,163,74,.07); color: var(--text); border:1px solid rgba(22,163,74,.28); }
.badge-link-ok     { background: rgba(236,176,0,.10); color: var(--text); border:1px solid rgba(236,176,0,.28); }
.badge-link-warn   { background: rgba(217,119,6,.08); color: var(--text); border:1px solid rgba(217,119,6,.28); }
.badge-link-bad    { background: rgba(180,35,24,.08); color: var(--text); border:1px solid rgba(180,35,24,.28); }
.badge-link-none   { background: #f7f8fa; color: var(--muted); }
.badge-svc       { background: rgba(236,176,0,.10); color: var(--text); border:1px solid rgba(236,176,0,.26); }
.badge-gw        { background: rgba(236,176,0,.14); color: var(--fer-black); }
.badge-direct    { background: #ecfdf3; color: #136c36; }
.badge-bestlink  { background: rgba(2,0,13,.08); color: var(--text); border: 1px solid rgba(2,0,13,.18); }
:root[data-theme="dark"] .badge-bestlink { background: rgba(248,246,239,.08); border-color: rgba(248,246,239,.18); }
.self-node-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 850;
  letter-spacing: .2px;
  background: linear-gradient(135deg, rgba(236,176,0,.96), rgba(236,176,0,.82));
  color: var(--fer-black);
  border: 1px solid rgba(236,176,0,.40);
  box-shadow: 0 0 0 1px rgba(236,176,0,.12), 0 0 18px rgba(236,176,0,.18), 0 0 30px rgba(236,176,0,.16);
}
:root[data-theme="dark"] .self-node-badge {
  background: linear-gradient(135deg, rgba(236,176,0,.96), rgba(236,176,0,.82));
  color: var(--fer-black);
  border-color: rgba(236,176,0,.48);
  box-shadow: 0 0 0 1px rgba(236,176,0,.14), 0 0 18px rgba(236,176,0,.20), 0 0 34px rgba(236,176,0,.18);
}
.link-bar-wrap     { margin-top: 6px; height: 5px; background: var(--border2); border-radius: 999px; overflow: hidden; }
.link-bar          { height: 100%; border-radius: 2px; transition: width .5s; }

/* ── Tooltip ── */
/* ── Admin Page ── */
.admin-wrap { max-width: 600px; margin: 0 auto; padding: 16px 12px 40px; }
.admin-wrap h2 { color: var(--text); font-size: 18px; letter-spacing: 0; margin-bottom: 4px; }
.admin-wrap .notice { color: #8a6a00; font-size: 11px; margin-bottom: 20px; padding: 8px; background: rgba(236,176,0,.12); border: 1px solid rgba(236,176,0,.28); border-radius: 4px; }
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
.role-mesh     { background:rgba(236,176,0,.10); color:var(--text); }
.role-ap       { background:#ecfdf3; color:#136c36; }
.role-gateway  { background:rgba(236,176,0,.14); color:var(--fer-black); }
.role-bat      { background:rgba(236,176,0,.10); color:var(--text); }
.role-bridge   { background:rgba(236,176,0,.10); color:var(--text); }
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
.peer-loading { padding: 16px; color: var(--muted); font-size: 12px; text-align: center; letter-spacing: 1px; }
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
#loading { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: var(--muted); font-size: 13px; font-weight: 700; letter-spacing: 0; backdrop-filter: blur(2px); }

/* ── Responsive: narrow → stack ── */
@media (max-width: 768px) {
  #main { grid-template-columns: 1fr; gap: 10px; padding: 10px; }
  #topo-panel { order: 1; height: var(--topo-h, 46vh); min-height: 260px; }
  #drag-handle { order: 2; }
  #side-panel { order: 3; width: 100%; overflow: visible; }
  #header { grid-template-columns:1fr; grid-template-areas:"brand" "meta" "actions"; height: auto; min-height: 58px; padding: 8px 10px 10px; row-gap:6px; }
  .hdr-brand { padding-left:0; gap:6px; }
  .fer-lockup { min-width:clamp(92px,24vw,132px); width:clamp(92px,24vw,132px); height:46px; padding-right:4px; }
  .fer-logo-img { height: 36px; }
  .hdr-logo { font-size:15px; min-height:38px; }
  #hdr-meta { padding-left:0; }
  #hdr-hostname { max-width: 42vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #hdr-right { margin-left: 0; gap: 8px; justify-content: flex-start; padding-left:0; }
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
  border: 1px solid var(--border2);
  border-radius: 999px;
  cursor: row-resize;
  flex-shrink: 0;
  touch-action: none;
  user-select: none;
  -webkit-user-select: none;
  margin: -2px auto 0;
  width: calc(100% - 24px);
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
<style>__CSS__
#power-banner { padding:7px 14px; font-size:12px; font-weight:600; letter-spacing:.4px;
  text-align:center; border-bottom:1px solid rgba(0,0,0,.25); }
#power-banner.crit { background:var(--bad);  color:#fff; }
#power-banner.warn { background:var(--warn); color:#000; }
</style>
</head>
<body>
<div id="app">
  <div id="header">
    <div class="hdr-brand">
      <div class="fer-lockup" title="FER" aria-label="FER">
        <img class="fer-logo-img" src="/assets/fer-logo-black?v=__LOGO_V__" data-light="/assets/fer-logo-black?v=__LOGO_V__" data-dark="/assets/fer-logo-white?v=__LOGO_V__" alt="FER">
      </div>
      <div class="hdr-logo">MANET//<span>STAT</span></div>
    </div>
    <div id="hdr-meta">
      <div id="hdr-health" class="health-loading">
        <div id="hdr-health-dot"></div>
        <span id="hdr-health-label">—</span>
      </div>
      <div class="meta" id="hdr-hostname">—</div>
      <div class="meta" id="hdr-ip" style="padding-right:10px">—</div>
      <div class="meta" id="hdr-ssid">—</div>
      <div class="spacer"></div>
    </div>
    <div id="hdr-right">
      <button id="perf-link" class="perf-link-btn" type="button" onclick="goPerfDashboard()">MANAGE</button>
      <div class="meta" id="hdr-nodes">—</div>
      <div class="meta" id="hdr-gw-label">—</div>
      <div class="meta" id="hdr-time" style="color:var(--muted);min-width:54px;text-align:right">—</div>
      <button id="theme-toggle" class="theme-toggle" type="button" onclick="toggleTheme()">Dark</button>
    </div>
  </div>
  <div id="power-banner" style="display:none"></div>
  <div id="main">
    <div id="topo-panel">
      <canvas id="topo"></canvas>
      <div id="loading">LOADING TOPOLOGY…</div>
    </div>
    <div id="drag-handle"></div>
    <div id="side-panel">
      <div class="section-hdr">MESH NODES <span id="node-count"></span></div>
      <div id="node-list"></div>
    </div>
  </div>
</div>
<script>
// ── Data & State ────────────────────────────────────────────────────────────
let DATA = null;
let SIM  = { nodes: [], links: [], running: false, raf: null };
let HOVER_NODE = null;
let drawQueued = false;
let EXPANDED_NODE_IDS = new Set();
let LOCAL_DETAIL_HTML = '<div class="peer-loading">Loading…</div>';
let PEER_DETAIL_CACHE = {};
let PEER_LOADING_IDS = new Set();
const POLL_INTERVAL_MS = __REFRESH__;
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
  document.querySelectorAll('.fer-logo-img[data-light][data-dark], .logo[data-light][data-dark]').forEach(img => {
    img.src = theme === 'dark' ? img.dataset.dark : img.dataset.light;
  });
  if (window.MANET_CANVAS_READY && typeof scheduleDraw === 'function' && !SIM.running) scheduleDraw();
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem(THEME_KEY, next); } catch(e) {}
  setTheme(next);
}

function isDarkTheme() {
  return document.documentElement.dataset.theme === 'dark';
}

function goPerfDashboard() {
  // Same origin, so this works from any EUD without depending on perf.local
  // resolving. The password prompt lives on the other side.
  window.location.href = '/manage/?theme=' + encodeURIComponent(document.documentElement.dataset.theme || 'light');
}

setTheme(preferredTheme());

// ── Utilities ────────────────────────────────────────────────────────────────
// BATMAN_V's metric is throughput in Mbit/s, not a 0-255 link quality.
// HaLow tops out near 43 Mbit/s at 8 MHz, so "great" is set where a healthy
// HaLow link lands rather than where a 5 GHz link does.
const LINK_GREAT_MBPS = 30, LINK_OK_MBPS = 15, LINK_WARN_MBPS = 5;
// Full scale for the bar and the graph's spring lengths.
const LINK_FULL_SCALE_MBPS = 50;

function linkClass(mbps) {
  if (mbps == null) return 'badge-link-none';
  if (mbps >= LINK_GREAT_MBPS) return 'badge-link-great';
  if (mbps >= LINK_OK_MBPS)    return 'badge-link-ok';
  if (mbps >= LINK_WARN_MBPS)  return 'badge-link-warn';
  return 'badge-link-bad';
}
function linkColor(mbps) {
  if (mbps == null) return '#9aa4b2';
  if (mbps >= LINK_GREAT_MBPS) return '#22c55e';
  if (mbps >= LINK_OK_MBPS)    return '#eab308';
  if (mbps >= LINK_WARN_MBPS)  return '#f97316';
  return '#ef4444';
}
function fmtMbps(mbps) {
  if (mbps == null) return '?';
  return mbps >= 10 ? Math.round(mbps).toString() : mbps.toFixed(1);
}
function linkLabel(mbps) {
  if (mbps == null) return '?';
  return `${fmtMbps(mbps)} Mbps`;
}
function linkPct(mbps) {
  if (mbps == null) return 0;
  return Math.round(Math.min(mbps / LINK_FULL_SCALE_MBPS, 1) * 100);
}
function fmtAge(ts) {
  const secs = (DATA ? DATA.timestamp : Math.floor(Date.now() / 1000)) - parseInt(ts || 0);
  if (secs < 60)   return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs/60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs/3600)}h ago`;
  return `${Math.floor(secs/86400)}d ago`;
}
function themeColor(light, dark) {
  return isDarkTheme() ? dark : light;
}
function topoFont(weight, size) {
  return `${weight} ${size}px Roobert, Arial, sans-serif`;
}
function ts(epoch) {
  const d = new Date(epoch * 1000);
  return d.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
}

function getLocalNodeId() {
  return DATA && DATA.nodes ? ((DATA.nodes.find(n => n.is_me) || {}).id || '') : '';
}

// ── Node List ────────────────────────────────────────────────────────────────
function renderNodeList(nodes) {
  const el = document.getElementById('node-list');
  document.getElementById('node-count').textContent = `(${nodes.length})`;
  el.innerHTML = nodes.map(n => {
    const cls = [
      'node-row',
      EXPANDED_NODE_IDS.has(n.id) ? 'peer-selected' : '',
      n.is_me  ? 'is-me' : '',
      n.is_gateway ? 'is-gw' : ''
    ].filter(Boolean).join(' ');

    const badges = [];
    if (n.is_gateway)  badges.push(`<span class="badge badge-gw">${n.is_selected_gw ? '★ GW' : 'GW'}</span>`);
    if (n.is_direct && !n.is_me) badges.push(`<span class="badge badge-direct">DIRECT</span>`);
    if (n.best_link && n.best_link.iface && !n.is_me) {
      const linkRate = linkLabel(n.best_link.mbps);
      badges.push(`<span class="badge badge-bestlink" title="BATMAN selected route via ${n.best_link.nexthop || 'unknown'}">BEST ${n.best_link.iface} ${linkRate}</span>`);
    }
    if (n.mumble)      badges.push(`<span class="badge badge-svc">MUMBLE</span>`);
    if (n.mediamtx)    badges.push(`<span class="badge badge-svc">MTX</span>`);
    if (n.ntp)         badges.push(`<span class="badge badge-svc">NTP</span>`);
    if (n.limp)        badges.push(`<span class="badge badge-link-bad">LIMP</span>`);

    const thisNodeLabel = n.is_me
      ? `<span class="self-node-badge">THIS NODE</span>`
      : '';
    const nodeStale = !n.is_me && (DATA.timestamp - parseInt(n.last_seen || 0)) > 300;
    const rateBadge = (n.is_me || nodeStale) ? '' : `<span class="badge ${linkClass(n.mbps)}">${linkLabel(n.mbps)}</span>`;
    const bar = nodeStale ? '' : `<div class="link-bar-wrap"><div class="link-bar" style="width:${linkPct(n.mbps)}%;background:${linkColor(n.mbps)}"></div></div>`;
    const meta = (!nodeStale && n.uptime) ? `<span style="color:var(--muted)">up ${n.uptime}</span>` : '';
    const cpu  = (!nodeStale && n.cpu)    ? `<span style="color:var(--muted)">CPU ${n.cpu}</span>` : '';
    let battMeta = '';
    if (!nodeStale && n.battery != null) {
      const pct = n.battery.percentage;
      const col = battColor(pct);
      const icon = (n.battery.charging === true) ? '⚡' : (pct <= 15 ? '⚠' : '');
      battMeta = `<span style="color:${col};font-size:10px">${icon}${pct}%</span>`;
    }
    const offlineBadge = nodeStale ? `<span class="badge badge-link-bad" style="opacity:.7">OFFLINE</span><span style="color:var(--muted);font-size:10px">last seen ${fmtAge(n.last_seen)}</span>` : '';

    const expanded = EXPANDED_NODE_IDS.has(n.id);
      const detailBody = n.is_me
        ? LOCAL_DETAIL_HTML
        : (PEER_LOADING_IDS.has(n.id)
            ? '<div class="peer-loading">FETCHING…</div>'
          : (PEER_DETAIL_CACHE[n.id] || '<div class="peer-loading" style="color:var(--muted)">No details loaded</div>'));
    const detail = expanded ? `<div class="node-inline-detail">
      <div class="inline-detail-head">
        <span class="inline-detail-title">${n.hostname}${n.ip ? '  ' + n.ip : ''}</span>
        ${n.is_me ? '<span class="self-node-badge">THIS NODE</span>' : ''}
      </div>
      <div class="inline-detail-body">${detailBody}</div>
    </div>` : '';

    return `<div class="${cls}" data-id="${n.id}">
      <div class="node-summary">
        <div class="node-name" style="${nodeStale ? 'color:var(--muted)' : ''}">${n.hostname}${thisNodeLabel}${n.state==='SHUTTING_DOWN'?'<span style="color:var(--bad);font-size:10px;margin-left:4px">OFFLINE</span>':''}</div>
        <div class="node-ip">${n.ip||'—'} &nbsp; <span style="color:var(--muted)">${n.mac}</span></div>
        <div class="node-meta">${nodeStale ? offlineBadge : rateBadge+badges.join('')+meta+cpu+battMeta}</div>
        ${bar}
      </div>
      ${detail}
    </div>`;
  }).join('');
}

function tickLocalTime() {
  const el = document.getElementById('hdr-time');
  if (el) el.textContent = new Date().toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
}
setInterval(tickLocalTime, 1000);
tickLocalTime();

function updateHeader(d) {
  document.getElementById('hdr-hostname').textContent = d.my_hostname || '—';
  document.getElementById('hdr-ip').textContent       = d.my_ip      || '—';
  document.getElementById('hdr-ssid').textContent     = d.mesh_ssid  ? `▶ ${d.mesh_ssid}` : '';
  document.getElementById('hdr-nodes').textContent    = `${d.nodes.length} node${d.nodes.length!==1?'s':''}`;
  // hdr-time is kept live by tickLocalTime(), no need to override here

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

// A sagging supply is the most misleading fault on these boards: it surfaces
// as radios that will not associate or a card that stops answering, and people
// go driver hunting. The SoC knows, so say it across the top of the page
// rather than in a row nobody scrolls to.
function updatePowerBanner(localData) {
  const el = document.getElementById('power-banner');
  if (!el) return;
  const p = localData && localData.power;
  if (!p || !p.available || p.state === 'ok' || p.state === 'notice') {
    el.style.display = 'none';
    return;
  }
  const crit = p.state === 'critical';
  el.className = crit ? 'crit' : 'warn';
  el.textContent = crit
    ? `\u26a0 UNDER-VOLTAGE NOW \u2014 this board is not getting enough power (${p.raw}). Radio faults are expected; check the PSU and cable.`
    : `\u26a0 Power problem recorded since boot (${p.raw}) \u2014 ${p.undervoltage_ever ? 'under-voltage' : 'throttling'} has occurred. Suspect the PSU before the radios.`;
  el.style.display = 'block';
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
    text = 'ALL OK';
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
      mbps:     edge.mbps,
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
    const mbps = link.mbps != null ? link.mbps : LINK_OK_MBPS;
    // Rest length: great link = shorter, poor link = longer, capped at maxLink
    const slack = 1 - Math.min(mbps / LINK_FULL_SCALE_MBPS, 1);
    const restLen = Math.min(60 + slack * maxLink * 0.85, maxLink);
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
  drawQueued = false;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.offsetWidth, H = canvas.offsetHeight;
  canvas.width  = W * dpr;
  canvas.height = H * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.translate(view.x, view.y);
  ctx.scale(view.scale, view.scale);

  ctx.clearRect(-view.x / view.scale, -view.y / view.scale, W / view.scale, H / view.scale);

  if (SIM.nodes.length === 0) return;

  // Subtle guide dots
  ctx.fillStyle = themeColor('#d6d2cb55', '#34313b66');
  for (let gx = 28; gx < W; gx += 38)
    for (let gy = 28; gy < H; gy += 38)
      ctx.fillRect(gx, gy, 1, 1);

  // Links
  // Draw inferred/multihop edges first (behind direct edges)
  const drawOrder = ['unknown', 'multihop', 'inferred', 'direct'];
  const sorted_links = [...SIM.links].sort(
    (a, b) => drawOrder.indexOf(a.type) - drawOrder.indexOf(b.type)
  );

  sorted_links.forEach(link => {
    const a = link.source, b = link.target;
    const mbps = link.mbps;
    const col = linkColor(mbps);
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

    // Throughput label at midpoint for direct and multihop lines involving self
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    if (mbps != null && (isDirect || link.type === 'multihop')) {
      ctx.fillStyle = col + 'cc';
      ctx.font = topoFont('700', 9);
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      // Small white backing for readability
      ctx.fillStyle = themeColor('#ffffffe8', '#121118e8');
      ctx.fillRect(mx - 12, my - 7, 24, 13);
      ctx.fillStyle = col + 'dd';
      ctx.fillText(fmtMbps(mbps), mx, my);
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
    const isSelected = EXPANDED_NODE_IDS.has(n.id);
    const nodeStaleCanvas = !n.is_me && (DATA.timestamp - parseInt(n.last_seen || 0)) > 300;
    const col = n.is_me ? '#ecb000' : (nodeStaleCanvas ? '#6b7280' : (n.is_gateway ? '#ecb000' : linkColor(n.mbps)));
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
      ctx.font = topoFont('700', Math.round(r * 0.9));
      ctx.fillText('★', n.x, n.y);
    } else if (n.is_me) {
      ctx.fillStyle = '#ecb000';
      ctx.font = topoFont('700', Math.round(r));
      ctx.fillText('◉', n.x, n.y);
    }
    // THIS NODE: small filled dot in top-right corner of circle
    if (n.is_me) {
      ctx.beginPath();
      ctx.arc(n.x + r * 0.65, n.y - r * 0.65, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = '#ecb000';
      ctx.fill();
      ctx.strokeStyle = themeColor('#ffffff', '#02000d');
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    ctx.textBaseline = 'alphabetic';

    // Label
    ctx.fillStyle = isHover || isSelected ? themeColor('#02000d', '#f8f6ef') : themeColor('#4a4752', '#cfc9d8');
    ctx.font = topoFont(isHover || isSelected ? '700' : '600', 11);
    ctx.textAlign = 'center';
    ctx.fillText(n.hostname, n.x, n.y + r + 12);
  });
}

function scheduleDraw() {
  if (drawQueued) return;
  drawQueued = true;
  requestAnimationFrame(() => {
    if (!window.MANET_CANVAS_READY) {
      drawQueued = false;
      return;
    }
    drawTopo();
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

canvas.addEventListener('mousemove', e => {
  const rect = canvas.getBoundingClientRect();
  const { x: mx, y: my } = screenToSim(e.clientX - rect.left, e.clientY - rect.top);
  HOVER_NODE = SIM.nodes.find(n => {
    const dx = n.x - mx, dy = n.y - my;
    return Math.sqrt(dx*dx + dy*dy) < n.r + 8;
  }) || null;
  canvas.style.cursor = HOVER_NODE ? 'pointer' : 'default';
  if (!SIM.running) scheduleDraw();
});

canvas.addEventListener('mouseleave', () => {
  HOVER_NODE = null;
  if (!SIM.running) scheduleDraw();
});

canvas.addEventListener('click', e => {
  const rect = canvas.getBoundingClientRect();
  const { x: mx, y: my } = screenToSim(e.clientX - rect.left, e.clientY - rect.top);
  const hit = SIM.nodes.find(n => {
    const dx = n.x - mx, dy = n.y - my;
    return Math.sqrt(dx*dx + dy*dy) < n.r + 10;
  });
  if (!hit) return;
  const node = DATA && DATA.nodes.find(n => n.id === hit.id);
  if (!node) return;
  if (EXPANDED_NODE_IDS.has(node.id)) { collapseNodeDetail(node.id); return; }
  if (node.is_me) { showLocalInDrawer(); return; }
  openPeerDrawer(node);
});

// ── Touch: two-finger pinch only (single finger always scrolls page) ────────
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
  if (e.touches.length === 2) {
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
    if (!SIM.running) scheduleDraw();
    e.preventDefault();
  }
}, { passive: false });

canvas.addEventListener('touchend', e => {
  if (e.touches.length < 2) pinchDist0 = 0;
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
    if (!SIM.running) scheduleDraw();
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
  if (!SIM.running) scheduleDraw();
});

function battColor(pct) {
  if (pct >= 60) return '#22c55e';
  if (pct >= 30) return '#eab308';
  return '#ef4444';
}

function renderLocalPanel(d) {
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
  if (d.power && d.power.available) {
    const ps = d.power.state;
    const col = ps === 'critical' ? 'var(--bad)' : ps === 'ok' ? 'var(--good)' : 'var(--warn)';
    const txt = ps === 'critical' ? 'UNDER-VOLTAGE NOW'
              : ps === 'warning'  ? (d.power.undervoltage_ever ? 'under-voltage since boot' : 'throttled since boot')
              : ps === 'notice'   ? 'throttling since boot'
              : 'OK';
    rows.push(['Power', `<span class="gps-dot" style="background:${col}"></span>${txt}` +
      (ps === 'ok' ? '' : ` <span style="font-size:9px;color:var(--muted)">${d.power.raw}</span>`)]);
  }
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
      const mcs = (iface.tx_mcs || iface.rx_mcs)
        ? `<div class="iface-addrs">TX ${iface.tx_mcs || '—'} &nbsp; RX ${iface.rx_mcs || '—'}</div>` : '';
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
        ${mcs}
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

  return html;
}

async function fetchLocal() {
  try {
    const r = await fetch('/api/local');
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    updateHealthPill(d);
    updatePowerBanner(d);
    LOCAL_DETAIL_HTML = renderLocalPanel(d);
    if (DATA) renderNodeList(DATA.nodes);
  } catch (err) {
    LOCAL_DETAIL_HTML = `<div class="peer-loading" style="color:var(--bad)">Error: ${err.message}</div>`;
    if (DATA) renderNodeList(DATA.nodes);
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

document.getElementById('node-list').addEventListener('click', e => {
  const row = e.target.closest('.node-row');
  if (!row) return;
  const id = row.dataset.id;
  if (!id || !DATA) return;
  const node = DATA.nodes.find(n => n.id === id);
  if (!node) return;
  if (EXPANDED_NODE_IDS.has(id)) {
    collapseNodeDetail(id);
    return;
  }
  if (node.is_me) { showLocalInDrawer(); return; }
  openPeerDrawer(node);
});

function collapseNodeDetail(id) {
  EXPANDED_NODE_IDS.delete(id);
  PEER_LOADING_IDS.delete(id);
  if (DATA) renderNodeList(DATA.nodes);
  if (!SIM.running) drawTopo();
}

function openPeerDrawer(node) {
  EXPANDED_NODE_IDS.add(node.id);
  PEER_LOADING_IDS.add(node.id);
  if (DATA) renderNodeList(DATA.nodes);
  const listRow = document.querySelector(`#node-list .node-row[data-id="${node.id}"]`);
  if (listRow) listRow.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  if (!SIM.running) drawTopo();
  if (!node.ip) {
    PEER_DETAIL_CACHE[node.id] = '<div class="peer-loading" style="color:var(--muted)">No IP known for this node</div>';
    PEER_LOADING_IDS.delete(node.id);
    if (DATA) renderNodeList(DATA.nodes);
    return;
  }
  fetchPeer(node.ip, node.hostname);
}

function showLocalInDrawer() {
  const localId = getLocalNodeId();
  if (!localId) return;
  EXPANDED_NODE_IDS.add(localId);
  PEER_LOADING_IDS.delete(localId);
  if (DATA) renderNodeList(DATA.nodes);
  const listRow = document.querySelector(`#node-list .node-row[data-id="${localId}"]`);
  if (listRow) listRow.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  if (!SIM.running) drawTopo();
  fetchLocal();
}

async function fetchPeer(ip, hostname) {
  const node = DATA && DATA.nodes
    ? (DATA.nodes.find(n => n.ip === ip && n.hostname === hostname) || DATA.nodes.find(n => n.ip === ip))
    : null;
  const nodeId = node ? node.id : null;
  try {
    const r = await fetch('/api/peer/' + ip);
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || r.status);
    if (nodeId) PEER_DETAIL_CACHE[nodeId] = renderPeerDrawer(d, hostname);
  } catch (err) {
    if (nodeId) PEER_DETAIL_CACHE[nodeId] =
      '<div class="peer-loading" style="color:var(--bad)">Error: ' + err.message + '</div>';
  } finally {
    PEER_LOADING_IDS.delete(nodeId);
    if (DATA) renderNodeList(DATA.nodes);
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
      const mcs = (iface.tx_mcs || iface.rx_mcs)
        ? `<div class="iface-addrs">TX ${iface.tx_mcs || '—'} &nbsp; RX ${iface.rx_mcs || '—'}</div>` : '';
      const faultLines = (iface.faults || []).map(f => `<div class="${iface.health==='fault'?'iface-fault':'iface-warn'}">${f}</div>`).join('');
      return `<div class="iface-row health-${iface.health}">
        <div class="iface-header">
          <span class="iface-name">${iface.name}</span>
          <span class="iface-state iface-state-${sc}">${iface.state||'?'}</span>
          ${label ? `<span class="iface-role role-${iface.role}">${label}</span>` : ''}
        </div>
        ${iface.detail ? `<div class="iface-detail">${iface.detail}</div>` : ''}
        ${addrs}${mcs}${faultLines}
      </div>`;
    }).join('');
  }

  // Connected EUDs
  const eudCount = (d.euds && d.euds.length) ? d.euds.length : (d.eud_count || 0);
  html += `<div class="section-hdr" style="font-size:9px;position:static">CONNECTED EUDS (${eudCount})</div>`;
  if (d.euds && d.euds.length) {
    html += d.euds.map(e => `<div class="eud-row">
      <span class="eud-name">${e.hostname || '<i style="color:var(--muted)">unknown</i>'}</span>
      &nbsp;<span class="eud-ip">${e.ip}</span>
      <div class="eud-mac">${e.mac}</div></div>`).join('');
  } else if (eudCount) {
    html += `<div style="padding:5px 10px;font-size:11px;color:var(--muted)">${eudCount} connected (names not published)</div>`;
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

  return html;
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
ROLLBACK_STATE_FILE = '/var/lib/manet-config-rollback/state'

def broadcast_config_package(pkg):
    """Write config package to Alfred type 70."""
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


def restart_eud_ap(changes):
    """Restart hostapd on this node when AP SSID or key changed."""
    if not ({'lan_ap_ssid', 'lan_ap_key'} & set(changes)):
        return
    ap_iface = ''
    try:
        with open('/var/lib/ap_interface') as f:
            ap_iface = f.read().strip()
    except Exception:
        return
    if not ap_iface or not os.path.isfile('/etc/hostapd/hostapd.conf'):
        return
    ssid = changes.get('lan_ap_ssid', '')
    key = changes.get('lan_ap_key', '')
    try:
        with open('/etc/hostapd/hostapd.conf') as f:
            text = f.read()
        if ssid:
            text = re.sub(r'^ssid=.*', f'ssid={ssid}', text, flags=re.M)
        if key:
            text = re.sub(r'^wpa_passphrase=.*', f'wpa_passphrase={key}', text, flags=re.M)
        with open('/etc/hostapd/hostapd.conf', 'w') as f:
            f.write(text)
        subprocess.run(['systemctl', 'restart', 'hostapd'], capture_output=True, timeout=30)
    except Exception:
        pass

def read_rollback_state():
    """Whether this node has a config rollback armed, and until when.

    Written by mesh-config-rollback.sh as shell assignments; read rather than
    sourced, because it is only ever consumed here for display.
    """
    state = load_kv_file(ROLLBACK_STATE_FILE)
    if not state:
        return {'armed': False}
    try:
        deadline = int(state.get('DEADLINE', '0') or 0)
    except ValueError:
        deadline = 0
    return {
        'armed':        True,
        'version':      state.get('VERSION', ''),
        'deadline':     deadline,
        'seconds_left': max(0, deadline - int(time.time())),
        'peers_before': state.get('PEERS_BEFORE', '0'),
    }


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
        'rollback':     read_rollback_state(),
        'nodes':        node_status,
        'total_nodes':  len(node_status),
        'active_nodes': sum(1 for n in node_status if n['node_state'] == 'ACTIVE'),
    }

def render_status_page():
    html = STATUS_HTML
    html = html.replace('__CSS__',     CSS)
    html = html.replace('__REFRESH__', str(REFRESH_MS))
    html = html.replace('__LOGO_V__',  logo_asset_token())
    return html

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

def send_file_response(handler, path, content_type):
    try:
        with open(path, 'rb') as f:
            body = f.read()
    except FileNotFoundError:
        handler.send_response(404)
        handler.end_headers()
        handler.wfile.write(b'Not found')
        return
    handler.send_response(200)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'public, max-age=3600')
    handler.end_headers()
    handler.wfile.write(body)

def render_perf_auth_page(next_path='/', error=''):
    safe_next = html.escape(next_path, quote=True)
    logo_v = logo_asset_token()
    # A node that came through provisioning always has admin_password. If one
    # somehow doesn't, say so instead of rejecting every password silently.
    if not get_provisioned_manage_password():
        intro = ('No management password is set on this node. Add '
                 '<code>admin_password=&lt;password&gt;</code> to /etc/mesh.conf '
                 'and run <code>systemctl restart mesh-status</code>.')
        error = error or 'No admin_password in /etc/mesh.conf'
    else:
        intro = 'Enter the provisioned management password to continue.'
    safe_error = html.escape(error)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MANET management login</title>
<script>
(() => {{
  const key = 'manetUiTheme';
  const params = new URLSearchParams(window.location.search);
  const forced = params.get('theme');
  let theme = (forced === 'dark' || forced === 'light') ? forced : null;
  if (!theme) {{
    try {{
      const saved = localStorage.getItem(key);
      if (saved === 'dark' || saved === 'light') theme = saved;
    }} catch (e) {{}}
  }}
  if (!theme && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) theme = 'dark';
  if (!theme) theme = 'light';
  document.documentElement.dataset.theme = theme;
  try {{ localStorage.setItem(key, theme); }} catch (e) {{}}
}})();
</script>
<style>
@keyframes autofill-detect {{ from {{ opacity: 1; }} to {{ opacity: 1; }} }}
body {{
  margin: 0;
  min-height: 100vh;
  min-height: 100svh;
  display: grid;
  place-items: center;
  padding: 18px;
  box-sizing: border-box;
  background:
    radial-gradient(circle at top left, rgba(236,176,0,.18), transparent 28%),
    radial-gradient(circle at top right, rgba(2,0,13,.08), transparent 26%),
    #ebeae8;
  color: #02000d;
  font-family: Roobert, Arial, sans-serif;
}}
.wrap {{
  width: min(460px, calc(100vw - 28px));
  position: fixed;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  max-height: calc(100svh - 36px);
  background: rgba(255,255,255,.94);
  border: 1px solid #e7e2da;
  border-radius: 18px;
  box-shadow: 0 24px 60px rgba(2,0,13,.12);
  overflow: auto;
}}
.top {{
  padding: 18px 20px 14px;
  border-bottom: 1px solid #e7e2da;
  background: transparent;
  text-align: center;
}}
/* near-square badge: drive off height so it doesn't tower over the form the
   way a width-driven size would (the old wordmark was ~1.6:1, this is ~0.86:1) */
.logo {{
  width: auto;
  height: min(150px, 38vw);
  max-width: 100%;
  object-fit: contain;
  display: block;
  margin: 0 auto;
  filter: none;
}}
h1 {{ margin: 14px 0 4px; font-size: 18px; }}
p  {{ margin: 0; font-size: 13px; color: #615f68; }}
form {{ padding: 18px 20px 20px; display: grid; gap: 12px; }}
label {{ font-size: 12px; font-weight: 700; color: #02000d; }}
.sr-only {{
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}}
input {{
  width: 100%;
  box-sizing: border-box;
  border: 1px solid #d6d2cb;
  border-radius: 10px;
  padding: 10px 12px;
  font: inherit;
  color: #02000d;
  background: #ffffff;
  animation: autofill-detect 0s both;
}}
input:focus {{
  outline: none;
  border-color: #ecb000;
  box-shadow: 0 0 0 3px rgba(236,176,0,.18);
}}
button {{
  width: 100%;
  box-sizing: border-box;
  border: 1px solid #ecb000;
  background: #ecb000;
  color: #02000d;
  border-radius: 999px;
  padding: 10px 14px;
  font: inherit;
  font-size: 12px;
  font-weight: 800;
  cursor: pointer;
}}
.err {{
  min-height: 18px;
  color: #b42318;
  font-size: 12px;
}}
:root[data-theme="dark"] body {{
  background:
    radial-gradient(circle at top left, rgba(236,176,0,.18), transparent 28%),
    radial-gradient(circle at top right, rgba(255,255,255,.08), transparent 26%),
    #02000d;
  color: #f8f6ef;
}}
:root[data-theme="dark"] .wrap {{
  background: rgba(18,17,24,.94);
  border-color: #24212b;
  box-shadow: 0 24px 60px rgba(0,0,0,.34);
}}
:root[data-theme="dark"] .top {{
  border-bottom-color: #24212b;
  background: transparent;
}}
/* dark mode swaps in the dedicated white asset (see data-dark) rather than
   filtering — brightness(0) invert(1) flattens artwork to a solid silhouette */
:root[data-theme="dark"] p {{
  color: #aaa5b2;
}}
:root[data-theme="dark"] label {{
  color: #f8f6ef;
}}
:root[data-theme="dark"] input {{
  border-color: #34313b;
  color: #f8f6ef;
  background: #121118;
}}
:root[data-theme="dark"] button {{
  border-color: #ecb000;
  background: #ecb000;
  color: #02000d;
}}
</style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <img class="logo" src="/assets/fer-logo-black?v={logo_v}" data-light="/assets/fer-logo-black?v={logo_v}" data-dark="/assets/fer-logo-white?v={logo_v}" alt="FER">
      <h1>MANET&#8203;//MANAGE</h1>
      <p>{intro}</p>
    </div>
    <form id="perf-login-form" method="post" action="{MANAGE_PREFIX}/login" autocomplete="on">
      <input type="hidden" name="next" value="{safe_next}">
      <label class="sr-only" for="username">Username</label>
      <input class="sr-only" id="username" name="username" type="text" value="admin" autocomplete="username" tabindex="-1" aria-hidden="true">
      <label for="password">Management password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" autofocus>
      <div class="err">{safe_error}</div>
      <button type="submit">Login</button>
    </form>
  </div>
  <script>
    (() => {{
      const form = document.getElementById('perf-login-form');
      const password = document.getElementById('password');
      if (!form || !password) return;
      let submitted = false;
      password.addEventListener('animationstart', () => {{
        if (submitted || !password.value) return;
        submitted = true;
        requestAnimationFrame(() => form.requestSubmit());
      }});
    }})();
  </script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────────────────────
# HTTP Handler
# ─────────────────────────────────────────────────────────────────────────────
class MeshHandler(ManageRoutes, http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Suppress default access logs (use stderr only for errors)
        pass

    def send_403(self):
        self.send_response(403)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Forbidden')

    def send_401_json(self, error='Management password required'):
        """Unauthenticated XHR from the management UI — the page turns this
        into a bounce back to the login form rather than a silent failure."""
        body = json.dumps({'ok': False, 'auth_required': True, 'error': error}).encode('utf-8')
        self.send_response(401)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

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

    def read_json_body(self):
        length = int(self.headers.get('Content-Length', '0') or '0')
        raw = self.rfile.read(length) if length else b'{}'
        return json.loads(raw.decode('utf-8') or '{}')

    def read_form_body(self):
        length = int(self.headers.get('Content-Length', '0') or '0')
        raw = self.rfile.read(length) if length else b''
        return parse_qs(raw.decode('utf-8'), keep_blank_values=True)

    def _perf_cookie_valid(self):
        cookies = parse_cookie_header(self.headers.get('Cookie', ''))
        return is_valid_perf_auth_token(cookies.get(PERF_AUTH_COOKIE, ''))

    def _send_perf_cookie_redirect(self, target_path, token):
        target_path = normalize_local_redirect(target_path)
        self.send_response(303)
        self.send_header('Location', target_path or MANAGE_PREFIX + '/')
        self.send_header(
            'Set-Cookie',
            f'{PERF_AUTH_COOKIE}={token}; Path=/; Max-Age={PERF_AUTH_COOKIE_MAX_AGE}; '
            'HttpOnly; SameSite=Lax'
        )
        self.end_headers()

    def _send_perf_auth_required(self, next_path='/', error=''):
        next_path = normalize_local_redirect(next_path)
        body = render_perf_auth_page(next_path=next_path, error=error).encode('utf-8')
        self.send_response(401)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _send_perf_logout_redirect(self):
        self.send_response(303)
        self.send_header('Location', MANAGE_PREFIX + '/login')
        self.send_header('Set-Cookie', f'{PERF_AUTH_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax')
        self.end_headers()

    def _send_redirect(self, location):
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _is_perf_host(self):
        host = self.headers.get('Host', '').split(':')[0].lower()
        return host == 'perf.local' or host == 'perf'

    def _perf_host_target(self, parsed):
        """Fold a perf.local URL into the /manage space on this host."""
        target = MANAGE_PREFIX + (parsed.path or '/')
        if parsed.query:
            target += '?' + parsed.query
        return target

    def _is_manage_path(self, path):
        return path == MANAGE_PREFIX or path.startswith(MANAGE_PREFIX + '/')

    def _handle_manage(self, parsed):
        """Login, logout, or a management route once the password has been given."""
        path = parsed.path
        if path == MANAGE_PREFIX:
            self._send_redirect(MANAGE_PREFIX + '/')
            return
        if path in (MANAGE_PREFIX + '/logout', '/auth/perf-logout'):
            self._send_perf_logout_redirect()
            return
        if path in (MANAGE_PREFIX + '/login', '/auth/perf-login'):
            if self.command == 'POST':
                form = self.read_form_body()
                password = (form.get('password', [''])[0] or '').strip()
                next_path = normalize_local_redirect((form.get('next', [''])[0] or '').strip())
                if not self._is_manage_path(next_path):
                    next_path = MANAGE_PREFIX + '/'
                if password and password == get_provisioned_manage_password():
                    self._send_perf_cookie_redirect(next_path, get_perf_auth_token())
                else:
                    self._send_perf_auth_required(next_path=next_path,
                                                  error='Wrong management password')
                return
            next_path = parse_qs(parsed.query).get('next', [MANAGE_PREFIX + '/'])[0]
            if not self._is_manage_path(normalize_local_redirect(next_path)):
                next_path = MANAGE_PREFIX + '/'
            self._send_perf_auth_required(next_path=next_path)
            return

        # Everything else under /manage is the UI itself. This is the gate:
        # without a valid cookie the request never reaches a management route.
        if not self._perf_cookie_valid():
            next_path = path
            if parsed.query:
                next_path += '?' + parsed.query
            self._send_perf_auth_required(next_path=next_path)
            return

        self._dispatch_manage()

    def _dispatch_manage(self):
        """Hand a /manage request to the mixin with the prefix stripped.

        The routes were written as if they owned the site root, so the prefix
        is removed here rather than threaded through every one of them.
        """
        original = self.path
        stripped = original[len(MANAGE_PREFIX):] or '/'
        self.path = stripped if stripped.startswith('/') else '/' + stripped
        try:
            handler = {
                'GET': self.manage_do_GET,
                'POST': self.manage_do_POST,
                'DELETE': self.manage_do_DELETE,
            }.get(self.command)
            if handler is None:
                self.send_response(405)
                self.end_headers()
                return
            handler()
        finally:
            self.path = original

    def do_GET(self):
        parsed = urlparse(self.path)
        logo_file = logo_asset_file(parsed.path)
        if logo_file:
            send_file_response(self, logo_file, asset_content_type(logo_file))
            return

        conf       = load_kv_file(MESH_CONF_FILE)
        client_ip  = self.client_address[0]

        # Nothing this server offers is meant for the uplink/LAN side — only
        # localhost and clients holding a DHCP lease from the mesh.
        if not is_allowed_ip(client_ip, conf):
            self.send_403()
            return

        # dnsmasq still points EUDs at perf.local; that name now lands in /manage.
        if self._is_perf_host():
            self._send_redirect(self._perf_host_target(parsed))
            return

        if self._is_manage_path(parsed.path) or parsed.path.startswith('/auth/perf-'):
            self._handle_manage(parsed)
            return

        path = parsed.path.rstrip('/') or '/'

        # The config form moved into the management UI as its NODE CONFIG tab.
        if path == '/admin':
            self._send_redirect(MANAGE_PREFIX + '/#config')
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
            try:
                ipaddress.ip_address(peer_ip)
            except ValueError:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Bad IP')
                return
            data = assemble_peer_data(peer_ip)
            if data is None:
                self.send_response(404)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': f'No registry entry for {peer_ip}'}).encode())
                return
            self.send_json(data)

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
            # Hands out the mesh SAE key and admin password — same gate as /manage.
            if not self._perf_cookie_valid():
                self.send_401_json()
                return
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
        parsed = urlparse(self.path)
        conf   = load_kv_file(MESH_CONF_FILE)
        if not is_allowed_ip(self.client_address[0], conf):
            self.send_403()
            return
        if self._is_perf_host():
            self._send_redirect(self._perf_host_target(parsed))
            return
        if self._is_manage_path(parsed.path):
            self._handle_manage(parsed)
            return

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'Not found')

    def do_POST(self):
        conf      = load_kv_file(MESH_CONF_FILE)
        client_ip = self.client_address[0]
        parsed    = urlparse(self.path)

        if not is_allowed_ip(client_ip, conf):
            self.send_403()
            return

        if self._is_perf_host():
            self._send_redirect(self._perf_host_target(parsed))
            return

        if self._is_manage_path(parsed.path) or parsed.path.startswith('/auth/perf-'):
            self._handle_manage(parsed)
            return

        path      = parsed.path.rstrip('/') or '/'
        length    = int(self.headers.get('Content-Length', 0))
        body      = self.rfile.read(length) if length else b'{}'

        # Runtime control endpoints are called server-to-server by the
        # management UI on peer nodes, so they authenticate by source IP rather
        # than by cookie. Admin POSTs below are operator actions and need the
        # password.
        if path == '/api/perf-auth':
            try:
                req = json.loads(body)
            except Exception:
                req = {}
            password = str(req.get('password', '')).strip()
            if password and password == get_provisioned_manage_password(conf):
                self.send_json({'ok': True, 'token': get_perf_auth_token()})
            else:
                self.send_response(401)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'ok': False, 'error': 'Wrong management password'}).encode('utf-8'))
            return

        # Reprovisioning the mesh (SAE key, SSID, IP range) is the most
        # destructive thing this UI can do — never without the password.
        if path.startswith('/api/admin/') and not self._perf_cookie_valid():
            self.send_401_json()
            return

        if path == '/api/admin/stage':
            try:
                req    = json.loads(body)
                config = req.get('config', {})
                if not config:
                    self.send_json({'ok': False, 'error': 'No config provided'})
                    return

                applied_local = local_changes(config, conf)
                if applied_local:
                    apply_local_to_conf(applied_local, MESH_CONF_FILE)
                    restart_eud_ap(applied_local)

                mesh_cfg = strip_local_keys(config)
                if not mesh_changes(config, conf):
                    self.send_json({
                        'ok': True,
                        'local_only': True,
                        'applied': sorted(applied_local.keys()),
                    })
                    return

                # Detect dangerous fields
                cur_ssid = conf.get('mesh_ssid', '')
                cur_key  = conf.get('mesh_key', '')
                cur_cidr = conf.get('ipv4_network', '')
                dangerous = (
                    (mesh_cfg.get('mesh_ssid',    cur_ssid) != cur_ssid) or
                    (mesh_cfg.get('mesh_key',     cur_key)  != cur_key)  or
                    (mesh_cfg.get('ipv4_network', cur_cidr) != cur_cidr)
                )

                version = make_config_version(mesh_cfg)
                pkg = {
                    'kind':       'mesh_config',
                    'version':    version,
                    'issued_by':  get_my_hostname(),
                    'issued_at':  int(__import__('time').time()),
                    'activate_at': 0,   # 0 = stage only
                    'dangerous':  dangerous,
                    'config':     mesh_cfg,
                }
                save_pending_config(pkg)
                broadcast_config_package(pkg)

                # This node ACKs immediately (it's the one staging)
                try:
                    with open('/var/run/mesh_config_ack_version', 'w') as f:
                        f.write(version)
                except Exception:
                    pass

                self.send_json({
                    'ok': True,
                    'version': version,
                    'dangerous': dangerous,
                    'applied': sorted(applied_local.keys()),
                })
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/admin/activate':
            try:
                req   = json.loads(body)
                force = req.get('force', False)
                # Carried in the package rather than decided per node, so the
                # whole mesh applies under the same terms.
                no_rollback = bool(req.get('no_rollback', False))
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
                activate_at = int(time.time()) + 60
                pkg['activate_at'] = activate_at
                pkg['no_rollback'] = no_rollback
                save_pending_config(pkg)
                broadcast_config_package(pkg)
                self.send_json({'ok': True, 'activate_at': activate_at,
                                'no_rollback': no_rollback})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/admin/cancel':
            try:
                # Clearing our own files is not enough: the package is still
                # resident in Alfred, so every node (including this one) would
                # re-stage it on the next sync. Broadcast a cancel the way the
                # radio path does.
                pending = get_pending_config() or {}
                broadcast_config_package({
                    'kind':      'mesh_config_cancel',
                    'version':   pending.get('version', ''),
                    'issued_by': get_my_hostname(),
                    'issued_at': int(time.time()),
                })
                clear_pending_config()
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
                                time.sleep(3)
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
                req = json.loads(body)
                self.send_json(apply_txpower(req.get('iface', ''), req.get('dbm')))
            except subprocess.CalledProcessError as e:
                err = (e.stderr or e.stdout or str(e)).strip()
                self.send_json({'ok': False, 'error': err or str(e)})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/control/halow_channel':
            try:
                req = json.loads(body)
                self.send_json(apply_halow_channel(
                    req.get('channel'), req.get('bw', '1MHz'), req.get('dbm')))
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/control/wifi_channel':
            try:
                req = json.loads(body)
                self.send_json(apply_wifi_channel(
                    req.get('iface', ''), req.get('channel'), req.get('dbm')))
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
    if len(sys.argv) > 1 and sys.argv[1] == '--dump-interfaces':
        print(json.dumps(interfaces_for_telemetry(get_interfaces()),
                         separators=(',', ':')))
        raise SystemExit(0)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    server = ThreadedServer(('0.0.0.0', port), MeshHandler)
    print(f'MANET Status Server listening on port {port}')
    print(f'  Status:  http://localhost:{port}/          (no password)')
    print(f'  Manage:  http://localhost:{port}{MANAGE_PREFIX}/    (admin_password from /etc/mesh.conf)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutdown.')
        server.shutdown()
