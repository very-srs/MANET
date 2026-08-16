#!/usr/bin/env python3
"""Radio control primitives, shared by the two things that drive the radios.

mesh-status.py calls these for local, immediate changes from the management
UI. mesh-radio-state.py calls the apply_* functions when an Alfred-staged
package activates. Keeping one implementation is the point: a channel change
must mean the same thing whichever path asked for it.

Nothing here talks to another node. Anything mesh-wide is staged over Alfred
by the caller.
"""

import json
import os
import re
import subprocess
import time

# EU S1G channel plan: UI index 1-5 -> centre frequency (kHz), and the
# corresponding S1G channel number the supplicant config wants.
HALOW_EU_CHANNELS = [863500, 864500, 865500, 866500, 867500]
HALOW_EU_UI_TO_S1G_CHANNEL = {idx: 1 + ((idx - 1) * 2) for idx in range(1, 6)}
# The Morse driver/BCF fixes TX power per bandwidth; these are the caps.
HALOW_BW_TXPOWER_CAP_DBM = {'1MHz': '24', '2MHz': '24', '4MHz': '22'}

HALOW_WPA_CONF = '/etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf'
HALOW_OVERRIDE_FILE = '/var/run/halow-channel-override'
USB_WIFI_UPLINK_SCRIPT = '/usr/local/bin/usb-wifi-uplink.sh'


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

def txpower_request_allowed(iface, requested, cap_dbm, options=None):
    if iface == 'wlan2':
        opts = options if options is not None else txpower_options_for_iface(iface, cap_dbm)
        try:
            req = float(requested)
            return any(abs(req - float(opt)) < 0.05 for opt in opts)
        except Exception:
            return False
    try:
        return not cap_dbm or float(requested) <= float(cap_dbm)
    except Exception:
        return False

def unsupported_txpower_response(iface, requested, cap_dbm, options=None):
    opts = options if options is not None else txpower_options_for_iface(iface, cap_dbm)
    if iface == 'wlan2':
        return {
            'ok': False,
            'error': (
                f'Unsupported txpower {requested} dBm for {iface}; '
                f'HaLow txpower is fixed by the Morse driver/BCF for the selected bandwidth'
            ),
            'options': opts,
        }
    return {
        'ok': False,
        'error': f'Unsupported txpower {requested} dBm for {iface} (max {cap_dbm} dBm)',
        'options': opts,
    }

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

def read_iface_txpower_dbm(iface):
    try:
        r = subprocess.run(['iw', 'dev', iface, 'info'],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r'txpower ([\d.]+) dBm', r.stdout)
        if m:
            return _fmt_dbm(m.group(1))
    except Exception:
        pass
    return ''

def set_iface_txpower_verified(iface, dbm, retries=6, delay=0.25):
    requested = _fmt_dbm(dbm)
    subprocess.run(
        ['iw', 'dev', iface, 'set', 'txpower', 'fixed', str(int(float(requested) * 100))],
        capture_output=True, text=True, check=True, timeout=5
    )
    actual = ''
    for _ in range(retries):
        time.sleep(delay)
        actual = read_iface_txpower_dbm(iface)
        if actual and abs(float(actual) - float(requested)) < 0.05:
            return requested, actual
    raise RuntimeError(
        f'TX power command accepted but {iface} is still '
        f'{actual or "unknown"} dBm, expected {requested} dBm'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Apply operations
# ─────────────────────────────────────────────────────────────────────────────
# Each returns a result dict and raises only on genuine failure. They act on
# this node alone; mesh-wide application is Alfred's job, not theirs.

def apply_txpower(iface, dbm):
    if not iface or dbm is None:
        raise ValueError('Missing iface or dbm')
    requested = _fmt_dbm(dbm)
    cap = get_iface_txpower_cap(iface)
    options = txpower_options_for_iface(iface, cap, read_iface_txpower_dbm(iface))
    if cap and not txpower_request_allowed(iface, requested, cap, options):
        return unsupported_txpower_response(iface, requested, cap, options)
    requested, actual = set_iface_txpower_verified(iface, dbm)
    return {
        'ok': True,
        'iface': iface,
        'dbm': requested,
        'actual_dbm': actual,
        'cap': cap,
        'options': txpower_options_for_iface(iface, cap, actual) if cap else [],
    }


def apply_halow_channel(channel, bw='1MHz', dbm=None):
    if not channel:
        raise ValueError('Missing channel')
    channel = int(channel)
    freq_khz = dict(enumerate(HALOW_EU_CHANNELS, start=1)).get(channel)
    s1g_channel = HALOW_EU_UI_TO_S1G_CHANNEL.get(channel)
    if not freq_khz or not s1g_channel:
        raise ValueError(f'Invalid EU S1G channel {channel}')

    bw_mhz = int(str(bw).replace('MHz', ''))
    requested = actual = ''
    if dbm is not None:
        cap = get_halow_bw_txpower_cap(bw) or get_iface_txpower_cap('wlan2')
        requested = _fmt_dbm(dbm)
        options = txpower_options_for_iface('wlan2', cap, read_iface_txpower_dbm('wlan2'))
        if cap and not txpower_request_allowed('wlan2', requested, cap, options):
            return unsupported_txpower_response('wlan2', requested, cap, options)

    # s1g_prim_chwidth: 0 = 1 MHz primary, 1 = 2 MHz primary. 4 MHz operation
    # still has a 2 MHz primary, hence chwidth 1.
    chwidth = {1: 0, 2: 1, 4: 1}.get(bw_mhz, 0)

    # Tell channel-election.sh to leave this alone.
    with open(HALOW_OVERRIDE_FILE, 'w') as f:
        f.write(f'{channel},{bw}')

    # Persist across reboots, then apply live.
    with open(HALOW_WPA_CONF) as f:
        content = f.read()
    content = re.sub(r'(channel\s*=\s*)\d+', rf'\g<1>{s1g_channel}', content)
    content = re.sub(r'(op_class\s*=\s*)\d+', r'\g<1>66', content)
    content = re.sub(r'(s1g_prim_chwidth\s*=\s*)\d+', rf'\g<1>{chwidth}', content)
    with open(HALOW_WPA_CONF, 'w') as f:
        f.write(content)

    result = subprocess.run(
        ['morse_cli', '-i', 'wlan2', 'channel',
         '-c', str(freq_khz), '-o', str(bw_mhz), '-p', str(bw_mhz)],
        capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        # The config above is already written, so a restart lands on the same
        # channel even when morse_cli will not take it live.
        subprocess.run(['systemctl', 'restart', 'wpa_supplicant-s1g-wlan2.service'],
                       timeout=15)

    if dbm is not None:
        requested, actual = set_iface_txpower_verified('wlan2', dbm)
    return {'ok': True, 'channel': channel, 'freq_khz': freq_khz, 'bw': bw,
            'dbm': requested, 'actual_dbm': actual}


def apply_wifi_channel(iface, channel, dbm=None):
    if not iface or not channel:
        raise ValueError('Missing iface or channel')
    freq = wifi_channel_to_freq(iface, channel)
    if not freq:
        raise ValueError(f'Invalid channel {channel} for {iface}')

    conf = f'/etc/wpa_supplicant/wpa_supplicant-{iface}.conf'
    with open(conf) as f:
        content = f.read()
    content = re.sub(r'(frequency\s*=\s*)\d+', rf'\g<1>{freq}', content)
    with open(conf, 'w') as f:
        f.write(content)
    subprocess.run(['systemctl', 'restart', f'wpa_supplicant@{iface}.service'],
                   capture_output=True, text=True, timeout=25)

    requested = actual = ''
    if dbm is not None:
        requested, actual = set_iface_txpower_verified(iface, dbm)
    return {'ok': True, 'iface': iface, 'channel': int(channel), 'freq': freq,
            'dbm': requested, 'actual_dbm': actual}


def apply_uplink_wifi(ssid, password, enabled=True):
    ssid = (ssid or '').strip()
    if not ssid:
        raise ValueError('SSID is required')
    if not password:
        raise ValueError('Password is required')
    r = subprocess.run(
        [USB_WIFI_UPLINK_SCRIPT, 'set', ssid, password, '1' if enabled else '0'],
        capture_output=True, text=True, timeout=45)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or '').strip()
                           or f'usb-wifi-uplink exited {r.returncode}')
    return {'ok': True, 'ssid': ssid, 'enabled': bool(enabled)}
