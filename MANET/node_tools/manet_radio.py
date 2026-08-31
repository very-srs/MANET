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
import stat
import subprocess
import time

# S1G channel plans, transcribed from the Morse driver's own tables in
# dot11ah/s1g_channels_rules.c ({eu,us}_s1g_channels).  Keys are the S1G
# channel number the supplicant config wants; values the centre frequency in
# kHz.  A bandwidth appears for a region only where the driver defines a
# channel of that width — which is why EU stops at 2 MHz: the whole 863-868
# allocation is 5 MHz wide, so there is nowhere to put a 4 or 8 MHz channel.
#
# Only EU and US are listed because radio-setup.sh generates exactly two
# supplicant templates: US, and everything else on the EU plan
# (radio-setup.sh:1132).  Offering a third region's channels here would write
# a config no template backs.
HALOW_CHANNEL_PLANS = {
    'EU': {
        '1MHz': {1: 863500, 3: 864500, 5: 865500, 7: 866500, 9: 867500},
        '2MHz': {2: 864000, 6: 866000},
    },
    'US': {
        '1MHz': {1: 902500, 3: 903500, 5: 904500, 7: 905500, 9: 906500,
                 11: 907500, 13: 908500, 15: 909500, 17: 910500, 19: 911500,
                 21: 912500, 23: 913500, 25: 914500, 27: 915500, 29: 916500,
                 31: 917500, 33: 918500, 35: 919500, 37: 920500, 39: 921500,
                 41: 922500, 43: 923500, 45: 924500, 47: 925500, 49: 926500,
                 51: 927500},
        '2MHz': {2: 903000, 6: 905000, 10: 907000, 14: 909000, 18: 911000,
                 22: 913000, 26: 915000, 30: 917000, 34: 919000, 38: 921000,
                 42: 923000, 46: 925000, 50: 927000},
        '4MHz': {8: 906000, 16: 910000, 24: 914000, 32: 918000, 40: 922000,
                 48: 926000},
        '8MHz': {12: 908000, 28: 916000, 44: 924000},
    },
}

# S1G global operating class per region and bandwidth.  The supplicant
# rejects a class that disagrees with the country or the channel, and a
# rejected config is a silent crashloop, so treat these as load-bearing.
#
# VERIFIED - these two are the values radio-setup.sh's own working templates
# ship: EU 1 MHz = 66 (non-US template), US 8 MHz = 71 (US template).
# INFERRED - the rest follow the standard's contiguous S1G block (EU 2 MHz =
# 67 is documented; US 1/2/4 MHz then fill 68/69/70 below the verified 71).
# They have not been confirmed against the standard text or on hardware, so
# apply_halow_channel() restarts the supplicant and rolls the config back if
# it will not come up on a class it has not used before.
HALOW_OP_CLASS = {
    ('EU', '1MHz'): 66, ('EU', '2MHz'): 67,
    ('US', '1MHz'): 68, ('US', '2MHz'): 69,
    ('US', '4MHz'): 70, ('US', '8MHz'): 71,
}

# The Morse driver/BCF fixes TX power per bandwidth; these are the caps.
# Measured on mesh-f86f (2026-04-22).  8 MHz has never been measured, so it is
# deliberately absent: callers fall back to reading the interface's own cap
# rather than acting on a number nobody checked.
HALOW_BW_TXPOWER_CAP_DBM = {'1MHz': '24', '2MHz': '24', '4MHz': '22'}

HALOW_WPA_CONF = '/etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf'
HALOW_OVERRIDE_FILE = '/var/run/halow-channel-override'
USB_WIFI_UPLINK_SCRIPT = '/usr/local/bin/usb-wifi-uplink.sh'


def halow_region():
    """'US' or 'EU' — mirrors radio-setup.sh:1132, which is the only place a
    HaLow supplicant config is generated: US gets its own template, every
    other region gets the EU one."""
    domain = ''
    try:
        with open('/etc/mesh.conf') as f:
            for line in f:
                if line.startswith('halow_regulatory_domain='):
                    domain = line.split('=', 1)[1].strip().strip('"').upper()
                    break
    except OSError:
        pass
    return 'US' if domain == 'US' else 'EU'


def halow_plan(region=None):
    """{bandwidth: {s1g_channel: centre_khz}} for this node's region."""
    return HALOW_CHANNEL_PLANS.get(region or halow_region(),
                                   HALOW_CHANNEL_PLANS['EU'])


def halow_channel_options(region=None):
    """The channel/bandwidth menu the UI renders, for this node's region."""
    region = region or halow_region()
    plan = halow_plan(region)
    order = sorted(plan, key=lambda bw: int(bw[:-3]))
    return {
        'region': region,
        'bandwidths': order,
        'channels': {
            bw: [{'channel': ch, 'mhz': round(khz / 1000.0, 1)}
                 for ch, khz in sorted(plan[bw].items())]
            for bw in order
        },
    }


def halow_bandwidth_for_channel(channel, region=None):
    """The bandwidth an S1G channel number belongs to, or ''.

    Channel numbers do not repeat across bandwidths within a region, so the
    number alone says how wide the channel is.
    """
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        return ''
    for bw, channels in halow_plan(region).items():
        if channel in channels:
            return bw
    return ''


def halow_channel_for_frequency(freq_khz, region=None):
    """(s1g_channel, bandwidth) for a centre frequency, or ('', '').

    Centre frequencies are unique across bandwidths within a region, so the
    frequency alone identifies the channel.
    """
    plan = halow_plan(region)
    for bw, channels in plan.items():
        for ch, khz in channels.items():
            if abs(freq_khz - khz) < 100:
                return str(ch), bw
    return '', ''


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
        if num in (1, 2, 4, 8):
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
    channel, _bw = halow_channel_for_frequency(freq_khz)
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
        # Not from s1g_prim_chwidth: that is the *primary* channel width, which
        # is 2 MHz for every operating width above 1 MHz, so reading it as the
        # operating width reports a 4 or 8 MHz channel as 2 MHz. The channel
        # number identifies the width on its own.
        bw = halow_bandwidth_for_channel(info.get('channel'))
        if bw:
            info['halow_bw'] = bw
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


S1G_SERVICE = 'wpa_supplicant-s1g-wlan2.service'


def _s1g_supplicant_healthy(settle=6):
    """True if the s1g supplicant is up and not crashlooping on its config.

    A rejected field is fatal to this build and shows up as a restart loop,
    not as a running daemon with a complaint, so watch the restart counter
    rather than a single is-active.
    """
    def counter():
        r = subprocess.run(['systemctl', 'show', '-p', 'NRestarts', '--value',
                            S1G_SERVICE], capture_output=True, text=True, timeout=10)
        try:
            return int(r.stdout.strip())
        except ValueError:
            return 0

    before = counter()
    time.sleep(settle)
    active = subprocess.run(['systemctl', 'is-active', '--quiet', S1G_SERVICE],
                            timeout=10).returncode == 0
    return active and counter() == before


def apply_halow_channel(channel, bw='1MHz', dbm=None):
    if not channel:
        raise ValueError('Missing channel')
    region = halow_region()
    bw = _format_halow_bw(bw) or '1MHz'
    plan = halow_plan(region)
    if bw not in plan:
        raise ValueError(
            f'{bw} is not available on the {region} S1G plan '
            f'(offered: {", ".join(sorted(plan, key=lambda b: int(b[:-3])))})')
    s1g_channel = int(channel)
    freq_khz = plan[bw].get(s1g_channel)
    if not freq_khz:
        raise ValueError(f'Channel {s1g_channel} is not a {bw} channel on the '
                         f'{region} S1G plan')
    op_class = HALOW_OP_CLASS.get((region, bw))
    if not op_class:
        raise ValueError(f'No S1G operating class known for {region} {bw}')

    bw_mhz = int(bw[:-3])
    requested = actual = ''
    if dbm is not None:
        cap = get_halow_bw_txpower_cap(bw) or get_iface_txpower_cap('wlan2')
        requested = _fmt_dbm(dbm)
        options = txpower_options_for_iface('wlan2', cap, read_iface_txpower_dbm('wlan2'))
        if cap and not txpower_request_allowed('wlan2', requested, cap, options):
            return unsupported_txpower_response('wlan2', requested, cap, options)

    # s1g_prim_chwidth: 0 = 1 MHz primary, 1 = 2 MHz primary. Everything wider
    # than 1 MHz keeps a 2 MHz primary, which is what both radio-setup
    # templates do.
    chwidth = 0 if bw_mhz == 1 else 1

    # Tell channel-election.sh to leave this alone.
    with open(HALOW_OVERRIDE_FILE, 'w') as f:
        f.write(f'{s1g_channel},{bw}')

    # Persist across reboots, then apply live.
    with open(HALOW_WPA_CONF) as f:
        previous = f.read()
    m = re.search(r'op_class\s*=\s*(\d+)', previous)
    op_class_changed = not m or int(m.group(1)) != op_class

    # s1g_prim_1mhz_chan_index addresses a 1 MHz slice of the operating
    # channel, so it must be 0..bw-1. Narrowing the channel without clamping
    # it leaves an out-of-range index behind and the supplicant refuses the
    # whole config.
    def _clamp_prim_index(text):
        idx = re.search(r's1g_prim_1mhz_chan_index\s*=\s*(\d+)', text)
        if not idx:
            return text
        wanted = min(int(idx.group(1)), bw_mhz - 1)
        return re.sub(r'(s1g_prim_1mhz_chan_index\s*=\s*)\d+',
                      rf'\g<1>{wanted}', text)

    content = re.sub(r'(channel\s*=\s*)\d+', rf'\g<1>{s1g_channel}', previous)
    content = re.sub(r'(op_class\s*=\s*)\d+', rf'\g<1>{op_class}', content)
    content = re.sub(r'(s1g_prim_chwidth\s*=\s*)\d+', rf'\g<1>{chwidth}', content)
    content = _clamp_prim_index(content)
    with open(HALOW_WPA_CONF, 'w') as f:
        f.write(content)

    result = subprocess.run(
        ['morse_cli', '-i', 'wlan2', 'channel',
         '-c', str(freq_khz), '-o', str(bw_mhz), '-p', str(bw_mhz)],
        capture_output=True, text=True, timeout=10)

    # Restart when the live change failed (the config above then carries it),
    # and also whenever the operating class changed: only a restart proves the
    # supplicant accepts the new class, and a class it rejects is a silent
    # crashloop that would otherwise surface at the next reboot.
    if result.returncode != 0 or op_class_changed:
        subprocess.run(['systemctl', 'restart', S1G_SERVICE], timeout=15)
        if not _s1g_supplicant_healthy():
            with open(HALOW_WPA_CONF, 'w') as f:
                f.write(previous)
            subprocess.run(['systemctl', 'restart', S1G_SERVICE], timeout=15)
            return {'ok': False, 'error': (
                f'{region} {bw} (channel {s1g_channel}, op_class {op_class}) '
                f'was refused by wpa_supplicant_s1g - config restored')}

    if dbm is not None:
        requested, actual = set_iface_txpower_verified('wlan2', dbm)
    return {'ok': True, 'channel': s1g_channel, 'freq_khz': freq_khz, 'bw': bw,
            'region': region, 'op_class': op_class,
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


# --- voice codec -------------------------------------------------------------

VOICE_CODECS = ('lyra', 'opus')
MESH_CONF_FILE = '/etc/mesh.conf'


def set_mesh_conf_key(key, value, conf_file=MESH_CONF_FILE):
    """Replace one key in mesh.conf, leaving every other line byte for byte.

    mesh.conf holds mesh_key and admin_password, so this is deliberately
    conservative: only the named key is touched, duplicates are collapsed
    rather than stacked, the original mode and ownership are carried over, and
    the replacement lands by atomic rename. A crash mid-write cannot leave a
    node with a truncated config and no way back in.
    """
    with open(conf_file) as f:
        lines = f.readlines()

    out, replaced = [], False
    for line in lines:
        if line.split('=', 1)[0].strip() == key:
            if not replaced:
                out.append('%s=%s\n' % (key, value))
                replaced = True
            continue
        out.append(line)
    if not replaced:
        if out and not out[-1].endswith('\n'):
            out[-1] += '\n'
        out.append('%s=%s\n' % (key, value))

    tmp = conf_file + '.tmp'
    try:
        st = os.stat(conf_file)
        with open(tmp, 'w') as f:
            f.writelines(out)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass          # not root: mode still carried, ownership stays ours
        os.replace(tmp, conf_file)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply_voice_codec(codec):
    """Switch this node's voice codec.

    Restart, not reload: mesh-voice's SIGHUP path retunes the talk group only.
    A codec change swaps the encoder, payloader, RTP clock rate and the raw
    caps either side of it, which means rebuilding both pipelines.

    This is staged mesh-wide through Alfred rather than applied node by node
    because the two codecs cannot hear each other. Sender and receiver both
    derive their RTP payload type and clock rate from this one setting, so a
    node left on the other codec is mutually inaudible -- it is not degraded
    audio, it is silence. See the codec note in node_tools/README.md.
    """
    codec = (codec or '').strip().lower()
    if codec not in VOICE_CODECS:
        raise ValueError('codec must be one of: %s' % ', '.join(VOICE_CODECS))

    set_mesh_conf_key('voice_codec', codec)

    restarted = False
    try:
        r = subprocess.run(['systemctl', 'restart', 'mesh-voice'],
                           timeout=30, stderr=subprocess.DEVNULL)
        restarted = r.returncode == 0
    except Exception:
        pass
    return {'ok': True, 'codec': codec, 'restarted': restarted}
