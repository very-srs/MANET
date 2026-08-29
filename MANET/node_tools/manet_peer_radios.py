"""Peer radio chips for the manage topology view.

Alfred telemetry already carries MCS and 2.4/5 GHz data-channel MHz, but
node-manager historically published INTERFACES_JSON as []. The topology UI
then had no wlan0/1/2 keys, so every peer rendered as 2.4G/5G/HaLow OFF.

This module fills those keys from the published interface list when present,
and otherwise from the registry MCS / DATA_CHANNEL_* fields so older nodes
still show radio values.
"""

import json

WLAN_IFACES = ('wlan0', 'wlan1', 'wlan2')


def parse_json_field(text):
    """Decode a JSON list carried through the registry; [] on anything odd."""
    try:
        value = json.loads(text or '[]')
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []


def freq_mhz_to_channel(mhz):
    """IEEE channel number from a centre frequency in MHz, or ''."""
    if mhz is None or mhz == '':
        return ''
    try:
        mhz_i = int(float(mhz))
    except (TypeError, ValueError):
        return ''
    if mhz_i <= 0:
        return ''
    if 2400 <= mhz_i <= 2500:
        if mhz_i == 2484:
            return '14'
        channel = (mhz_i - 2407) // 5
        return str(channel) if channel > 0 else ''
    if 4900 <= mhz_i <= 5900:
        return str((mhz_i - 5000) // 5)
    return ''


def _mcs_map(nd):
    return {
        'wlan0': {
            'tx_mcs': nd.get('WIFI_24_TX_MCS', '') or '',
            'rx_mcs': nd.get('WIFI_24_RX_MCS', '') or '',
        },
        'wlan1': {
            'tx_mcs': nd.get('WIFI_5_TX_MCS', '') or '',
            'rx_mcs': nd.get('WIFI_5_RX_MCS', '') or '',
        },
        'wlan2': {
            'tx_mcs': nd.get('HALOW_TX_MCS', '') or '',
            'rx_mcs': nd.get('HALOW_RX_MCS', '') or '',
        },
    }


def _blank_radio(mcs):
    return {
        'active': False,
        'channel': '',
        'freq_mhz': '',
        'txpower_dbm': '',
        'txpower_cap_dbm': '',
        'txpower_options_dbm': [],
        'tx_mcs': mcs.get('tx_mcs', ''),
        'rx_mcs': mcs.get('rx_mcs', ''),
        'halow_bw': '',
        'halow_source': '',
    }


def peer_radio_interfaces(nd):
    """wlan0/1/2 dicts for a registry row that is not this node."""
    mcs_map = _mcs_map(nd)
    published = {}
    for entry in parse_json_field(nd.get('INTERFACES_JSON', '')):
        if isinstance(entry, dict) and entry.get('name') in WLAN_IFACES:
            published[entry['name']] = entry

    band_freq = {
        'wlan0': str(nd.get('DATA_CHANNEL_2_4', '') or ''),
        'wlan1': str(nd.get('DATA_CHANNEL_5_0', '') or ''),
        'wlan2': '',
    }

    result = {}
    for name in WLAN_IFACES:
        mcs = mcs_map[name]
        pub = published.get(name)
        info = _blank_radio(mcs)
        if pub:
            info['active'] = pub.get('state') == 'UP' and pub.get('role') == 'mesh'
            info['channel'] = str(pub.get('channel') or '') or freq_mhz_to_channel(band_freq[name])
            info['freq_mhz'] = str(pub.get('freq_mhz') or pub.get('freq') or '') or band_freq[name]
            info['txpower_dbm'] = str(pub.get('txpower_dbm') or '')
            info['halow_bw'] = str(pub.get('halow_bw') or '')
            info['tx_mcs'] = str(pub.get('tx_mcs') or '') or mcs['tx_mcs']
            info['rx_mcs'] = str(pub.get('rx_mcs') or '') or mcs['rx_mcs']
        else:
            info['channel'] = freq_mhz_to_channel(band_freq[name])
            info['freq_mhz'] = band_freq[name]
            if name == 'wlan0':
                info['active'] = bool(band_freq['wlan0'] or mcs['tx_mcs'] or mcs['rx_mcs'])
            elif name == 'wlan2':
                info['active'] = bool(mcs['tx_mcs'] or mcs['rx_mcs'])
        result[name] = info
    return result


def interfaces_for_telemetry(ifaces):
    """Shrink get_interfaces() output to what the Alfred encoder accepts."""
    out = []
    for iface in ifaces:
        if not isinstance(iface, dict):
            continue
        name = iface.get('name')
        if name not in WLAN_IFACES:
            continue
        out.append({
            'name': name,
            'role': iface.get('role', '') or '',
            'state': iface.get('state', '') or '',
            'ipv4': list(iface.get('ipv4') or iface.get('addrs') or []),
            'tx_mcs': iface.get('tx_mcs', '') or '',
            'rx_mcs': iface.get('rx_mcs', '') or '',
            'channel': str(iface.get('channel', '') or ''),
            'freq_mhz': str(iface.get('freq_mhz', '') or ''),
            'txpower_dbm': str(iface.get('txpower_dbm', '') or ''),
            'halow_bw': str(iface.get('halow_bw', '') or ''),
        })
    return out


def _truthy(nd, key):
    return str(nd.get(key, '')).lower() == 'true'


def _peer_iface_health(role, state):
    if state == 'DOWN' and role in ('mesh', 'bat', 'bridge', 'gateway', 'ap'):
        return 'fault', [f'{role} is down']
    if state == 'UNKNOWN':
        return 'warn', []
    if role in ('bat', 'bridge'):
        return 'info', []
    return 'ok', []


def _peer_iface_detail(iface, nd):
    role = iface.get('role', '')
    name = iface.get('name', '')
    channel = str(iface.get('channel') or '')
    if role == 'bat':
        return 'BATMAN-ADV mesh bridge'
    if role == 'bridge':
        return 'L2 bridge (mesh + EUD)'
    if role == 'gateway':
        return 'Internet gateway'
    if role == 'ap':
        ssid = nd.get('AP_SSID', '') or ''
        return f'EUD AP — {ssid}' if ssid else 'EUD AP'
    if role == 'mesh':
        if name == 'wlan2' or iface.get('halow_bw'):
            return f'HaLow — ch{channel}' if channel else 'HaLow'
        if name == 'wlan0':
            return f'2.4 GHz — ch{channel}' if channel else '2.4 GHz'
        if name == 'wlan1':
            return f'5 GHz — ch{channel}' if channel else '5 GHz'
        return f'Mesh — ch{channel}' if channel else 'Mesh radio'
    return ''


def _mcs_for_name(nd, name):
    return {
        'wlan0': (nd.get('WIFI_24_TX_MCS', '') or '', nd.get('WIFI_24_RX_MCS', '') or ''),
        'wlan1': (nd.get('WIFI_5_TX_MCS', '') or '', nd.get('WIFI_5_RX_MCS', '') or ''),
        'wlan2': (nd.get('HALOW_TX_MCS', '') or '', nd.get('HALOW_RX_MCS', '') or ''),
    }.get(name, ('', ''))


def _decorate_published_iface(iface, nd):
    name = iface['name']
    role = iface.get('role', '') or 'other'
    state = iface.get('state', '') or 'UNKNOWN'
    health, faults = _peer_iface_health(role, state)
    tx_mcs, rx_mcs = _mcs_for_name(nd, name)
    return {
        'name': name,
        'role': role,
        'state': state,
        'addrs': list(iface.get('ipv4') or iface.get('addrs') or []),
        'health': health,
        'faults': faults,
        'tx_mcs': str(iface.get('tx_mcs') or '') or tx_mcs,
        'rx_mcs': str(iface.get('rx_mcs') or '') or rx_mcs,
        'channel': str(iface.get('channel') or ''),
        'freq_mhz': str(iface.get('freq_mhz') or ''),
        'txpower_dbm': str(iface.get('txpower_dbm') or ''),
        'halow_bw': str(iface.get('halow_bw') or ''),
        'detail': _peer_iface_detail(iface, nd),
    }


def _synthetic_iface(name, role, state, nd, addrs=None):
    fake = {'name': name, 'role': role, 'state': state, 'channel': ''}
    row = _decorate_published_iface(fake, nd)
    if addrs is not None:
        row['addrs'] = addrs
    return row


def peer_status_panel(nd):
    """Status-page peer drawer from a registry row (no live probe)."""
    names = set()
    interfaces = []
    for iface in parse_json_field(nd.get('INTERFACES_JSON', '')):
        if not isinstance(iface, dict) or not iface.get('name'):
            continue
        row = _decorate_published_iface(iface, nd)
        names.add(row['name'])
        interfaces.append(row)

    mesh_up = any(i['role'] == 'mesh' and i['state'] == 'UP' for i in interfaces)
    if 'bat0' not in names and mesh_up:
        interfaces.append(_synthetic_iface('bat0', 'bat', 'UP', nd))
    gw_name = (nd.get('GATEWAY_IFACE') or '').strip()
    if _truthy(nd, 'IS_GATEWAY') and gw_name and gw_name not in names:
        interfaces.append(_synthetic_iface(gw_name, 'gateway', 'UP', nd))
    mesh_ip = (nd.get('IPV4_ADDRESS') or '').strip()
    if 'br0' not in names and mesh_ip:
        interfaces.append(_synthetic_iface(
            'br0', 'bridge', 'UP', nd, addrs=[mesh_ip]))

    order = {'bat': 0, 'mesh': 1, 'ap': 2, 'gateway': 3, 'eud-bridge': 4, 'bridge': 5}
    interfaces.sort(key=lambda i: (order.get(i['role'], 9), i['name']))

    try:
        eud_count = int(nd.get('EUD_COUNT') or 0)
    except (TypeError, ValueError):
        eud_count = 0

    return {
        'interfaces': interfaces,
        'services': {
            'mumble': _truthy(nd, 'IS_MUMBLE_SERVER'),
            'mediamtx': _truthy(nd, 'IS_MEDIAMTX_SERVER'),
            'ntp': _truthy(nd, 'IS_NTP_SERVER'),
            'syncthing': False,
            'tak': _truthy(nd, 'IS_TAK_SERVER'),
        },
        'euds': [],
        'eud_count': eud_count,
    }
