#!/usr/bin/env python3
"""Local built-in Wi-Fi client ownership, discovery and shared AP following."""

import argparse
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from manet_ap_mesh import Transition, locked, text
from manet_config_io import atomic_write, load_kv_file, rewrite_keys, supplicant_string
from manet_wifi_events import UplinkEvents

AP_VIF = 'manetap'
DFS_FOLLOW_CAP = Path('/sys/module/cfg80211/parameters/dfs_follow_ap_v1')
DFS_FOLLOW_MARKER = '2.10-manet-dfs-follow-v1'
DFS_FAILURE_REASON = ('EUD hotspot off: DFS follower startup failed. The Wi-Fi '
                      'uplink can stay connected. Reconnect the uplink to retry.')
BANDS = {'wlan0': '2.4', 'wlan1': '5'}
DFS_AP_REASON = ('EUD hotspot off: the upstream uses a DFS channel. '
                 'The Wi-Fi uplink can stay connected. Use Ethernet or '
                 'another node for access, or choose a non-DFS upstream.')
SAFETY_INTERVAL = 120
RECOVERY_INTERVAL = 3


OWN_HOTSPOT_REASON = "This network is one of this mesh's own hotspots and cannot be used as an uplink"


def is_mesh_hotspot(ssid, settings=None):
    """Match the configured EUD stem literally and its lowercase MAC suffix."""
    if settings is None:
        settings = load_kv_file(os.environ.get('MANET_MESH_CONF', '/etc/mesh.conf'))
    prefix = settings.get('lan_ap_ssid')
    return bool(isinstance(ssid, str) and prefix and
                re.fullmatch(re.escape(prefix) + r'-[0-9a-f]{4}', ssid))


def reject_mesh_hotspot(ssid, settings=None):
    """Reject both typed credentials and replayed scan selections."""
    if is_mesh_hotspot(ssid, settings):
        raise ValueError(OWN_HOTSPOT_REASON)


def uplink_profile_allowed(iface):
    """Gate gateway qualification even before the client watcher cleans up."""
    if iface in BANDS:
        path = Path(os.environ.get('MANET_WIFI_UPLINK_CONFIG', '/etc/manet/wifi-uplink.json'))
        profile = json.loads(text(path, '{}'))
        if profile.get('iface', profile.get('blocked_iface')) != iface:
            return True
    else:
        path = Path(os.environ.get('MANET_USB_WIFI_UPLINK_CONFIG',
                                   '/etc/manet/usb-wifi-uplink.conf'))
        try:
            # USB credentials are literal, unlike shell-style mesh.conf.
            # Match cfg_get's first assignment without stripping SSID bytes.
            ssid = next((line.partition('=')[2] for line in path.read_text().splitlines()
                         if line.startswith('ssid=')), '')
        except FileNotFoundError:
            ssid = ''
        profile = {'ssid': ssid}
    return not is_mesh_hotspot(profile.get('ssid', profile.get('blocked_ssid')))


def uplink_owner(roles=None):
    """Return the reserved physical radio, even before its client associates."""
    roles = Path(roles or os.environ.get('MANET_IFACE_STATE_DIR', '/var/lib'))
    value = text(roles / 'wifi_uplink_if')
    return value if value in BANDS else ''


def uplink_owned(iface, roles=None):
    """Exclude the physical client and its AP VIF from mesh operations."""
    owner = uplink_owner(roles)
    return bool(owner and iface in (owner, AP_VIF))


def frequency_channel(freq):
    """Convert supported 2.4/5 GHz primary MHz to an IEEE channel."""
    if freq == 2484:
        return 14
    if 2412 <= freq <= 2472 and (freq - 2407) % 5 == 0:
        return (freq - 2407) // 5
    if 5180 <= freq <= 5885 and freq % 5 == 0:
        return (freq - 5000) // 5
    raise ValueError('Unsupported upstream frequency')


def scan_records(output, settings=None):
    """Parse iw BSS records, keeping SSIDs literal and unsupported auth visible."""
    if settings is None:
        settings = load_kv_file(os.environ.get('MANET_MESH_CONF', '/etc/mesh.conf'))
    records = []
    for block in re.split(r'^BSS ', output, flags=re.M)[1:]:
        mac = re.match(r'([0-9a-fA-F:]{17})\b', block)
        freq = re.search(r'^\s*freq: (\d+)', block, re.M)
        signal = re.search(r'^\s*signal: (-?[\d.]+)', block, re.M)
        ssid = re.search(r'^[ \t]*SSID: ?(.*)$', block, re.M)
        if not mac or not freq or not signal or not ssid:
            continue
        mhz = int(freq[1])
        try:
            channel = frequency_channel(mhz)
        except ValueError:
            continue
        # iw escapes backslashes, edge spaces and non-printable bytes as \xNN.
        # Decode once, keeping invalid UTF-8/control bytes visible but disabled.
        raw = b''.join(bytes.fromhex(part[2:]) if re.fullmatch(r'\\x[0-9a-fA-F]{2}', part)
                       else part.encode() for part in re.split(r'(\\x[0-9a-fA-F]{2})', ssid[1]))
        reason = ''
        try:
            name = raw.decode('utf-8')
            if any(ord(c) < 32 or ord(c) == 127 for c in name):
                name, reason = ssid[1], 'Network name contains unsupported control characters'
        except UnicodeDecodeError:
            name, reason = ssid[1], 'Network name uses unsupported text encoding'
        if is_mesh_hotspot(name, settings):
            continue
        if not raw or not raw.strip(b'\0'):
            name, reason = '', 'Hidden network: its name is not broadcast'
        elif len(raw) > 32:
            reason = 'Network name is too long'
        auth = re.findall(r'Authentication suites: (.*)', block)
        suites = ' '.join(auth)
        if 'SAE' in suites:
            security = 'sae'
        elif 'PSK' in suites and re.search(r'^\s*RSN:', block, re.M):
            security = 'wpa2'
        elif 'Privacy' not in block and not auth:
            security = 'open'
        else:
            security = 'unsupported'
        if security == 'unsupported' and not reason:
            reason = 'This sign-in method is not supported'
        records.append({'ssid': name, 'bssid': mac[1].lower(), 'freq': mhz,
                        'channel': channel, 'signal': float(signal[1]),
                        'security': security, 'selectable': not reason,
                        'reason': reason, 'ssid_hex': raw.hex(),
                        'wpa2_available': bool('PSK' in suites and re.search(r'^\s*RSN:', block, re.M))})
    return sorted(records, key=lambda row: row['signal'], reverse=True)


def usb_scan_result(output, iface):
    """Describe USB choices using its existing open/WPA2 client capabilities."""
    rows = scan_records(output)
    for row in rows:
        row['dfs'] = 5260 <= row['freq'] <= 5720
        if row['security'] == 'sae':
            if row['wpa2_available']:
                row['security'] = 'wpa2'
            else:
                row.update(selectable=False, reason='WPA3 is not supported by this USB connection')
    return {'iface': iface, 'at': time.clock_gettime(time.CLOCK_BOOTTIME), 'networks': rows}


def validate_usb_selection(value, scans, iface):
    """Bind a menu selection to a recent scan of the currently selected dongle."""
    reject_mesh_hotspot(value.get('ssid'))
    age = time.clock_gettime(time.CLOCK_BOOTTIME) - scans.get('at', -1000)
    matches = [row for row in scans.get('networks', []) if row.get('selectable') and all(
        value.get(key) == row.get(key) for key in ('ssid', 'bssid', 'freq', 'security'))]
    if (not iface or value.get('scan_iface') != iface or scans.get('iface') != iface
            or not 0 <= age <= 300 or not matches):
        raise ValueError('Scan again and choose a network from the fresh list')


def validate_profile(value):
    """Accept only local, single-radio personal/open network credentials."""
    if not isinstance(value, dict) or type(value.get('enabled', True)) is not bool:
        raise ValueError('Invalid Wi-Fi uplink request')
    if not value.get('enabled', True):
        return {'enabled': False}
    iface, ssid = value.get('iface'), value.get('ssid')
    password, security = value.get('password', ''), value.get('security')
    freq, bssid = value.get('freq'), value.get('bssid', '')
    if iface not in BANDS or type(freq) is not int:
        raise ValueError('Choose a built-in radio and a scanned network')
    frequency_channel(freq)
    if (freq < 3000) != (iface == 'wlan0'):
        raise ValueError('Upstream band does not match the selected radio')
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32:
        raise ValueError('SSID must be 1 to 32 UTF-8 bytes')
    if not isinstance(password, str) or any(c in ssid + password for c in '\r\n\0'):
        raise ValueError('Credentials must occupy one line')
    if not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', bssid):
        raise ValueError('Choose a scanned access point')
    if security not in ('open', 'wpa2', 'sae'):
        raise ValueError('Only open, WPA2 Personal and WPA3 Personal are supported')
    if security != 'open' and not 8 <= len(password.encode()) <= 63:
        raise ValueError('Password must be 8 to 63 UTF-8 bytes')
    return dict(enabled=True, iface=iface, ssid=ssid, password=password,
                security=security, freq=freq, bssid=bssid.lower())


def client_config(profile, frequencies, country):
    """Render literal credentials, locking band/BSSID but allowing AP channel moves."""
    if not re.fullmatch(r'[A-Z]{2}', country):
        raise ValueError('Set a two-letter Wi-Fi regulatory domain first')
    lines = ['ctrl_interface=/run/wpa_supplicant', 'country=' + country,
             'network={', '    ssid=' + profile['ssid'].encode().hex(),
             '    bssid=' + profile['bssid'],
             '    freq_list=' + ' '.join(str(f) for f in frequencies)]
    security = profile['security']
    if security == 'open':
        lines.append('    key_mgmt=NONE')
    elif security == 'wpa2':
        psk = hashlib.pbkdf2_hmac('sha1', profile['password'].encode(),
                                  profile['ssid'].encode(), 4096, 32).hex()
        lines += ['    key_mgmt=WPA-PSK', '    proto=RSN', '    psk=' + psk]
    else:
        # SAE passwords use wpa_supplicant's literal quoted string syntax.
        # Unlike SSID/PSK, sae_password does not accept a hexadecimal string.
        if '"' in profile['password']:
            raise ValueError('WPA3 passwords containing double quotes are unsupported')
        lines += ['    key_mgmt=SAE', '    ieee80211w=2',
                  '    sae_password=' + supplicant_string(profile['password'])]
    return '\n'.join(lines + ['}', ''])


class WifiUplink:
    """Serialize client transitions with AP policy and mesh channel changes."""
    def __init__(self):
        self.radio = Transition()
        self.roles, self.run = self.radio.roles, self.radio.run
        self.events_dir = self.run / 'manet-wifi-uplink'
        self.config = Path(os.environ.get('MANET_WIFI_UPLINK_CONFIG', '/etc/manet/wifi-uplink.json'))
        self.saved = self.roles / 'wifi-uplink-restore.json'
        self.status_path = self.run / 'manet-wifi-uplink.json'
        self.scan_path = self.run / 'manet-wifi-scan.json'
        self.repair_path = self.events_dir / 'scan-repair.json'
        self.network = Path(os.environ.get('MANET_NETWORKD_DIR', '/etc/systemd/network'))
        self.hostapd = Path(os.environ.get('MANET_HOSTAPD_CONF', '/etc/hostapd/hostapd.conf'))
        self.lock = self.run / 'manet-wifi-uplink.lock'
        self.defer_ap_until = 0

    def invoke(self, args, timeout=15, optional=False):
        """Bound subprocesses and never expose their stderr or credential argv."""
        try:
            return self.radio.command(args, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as error:
            if optional:
                return ''
            if args[:2] == ['iw', 'dev'] and 'scan' in args:
                if isinstance(error, subprocess.TimeoutExpired):
                    raise RuntimeError('Wi-Fi scan timed out') from error
                # iw scan has no credential input; its driver reason is safe
                # to display as text. Other commands may carry private data.
                detail = getattr(error, 'stderr', '') or ''
                raise RuntimeError('Wi-Fi scan failed: ' +
                                   (detail.strip()[:300] or
                                    'radio rejected the scan')) from error
            raise RuntimeError('Wi-Fi uplink command failed: ' + args[0]) from error

    def profile(self):
        """Read the private desired profile, treating absence as disabled."""
        value = json.loads(text(self.config, '{"enabled":false}'))
        profile = validate_profile(value)
        if not profile['enabled'] and value.get('blocked_ssid'):
            profile['blocked_ssid'] = value['blocked_ssid']
        return profile

    def store(self, path, value):
        """Durably save a private JSON record, including intent before mutation."""
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(value, sort_keys=True) + '\n', mode=0o600)

    def capabilities(self, iface):
        """Require the physical MT7916 band and parse live channel restrictions."""
        if iface not in BANDS:
            raise ValueError('Choose wlan0 or wlan1')
        driver = (self.radio.sysnet / iface / 'device/driver').resolve().name
        if driver != 'mt7915e':
            raise ValueError('Selected radio is not an MT7916 interface')
        phy = text(self.radio.sysnet / iface / 'phy80211/name')
        if not re.fullmatch(r'phy\d+', phy):
            raise ValueError('Radio PHY is unavailable')
        output = self.invoke(['iw', 'phy', phy, 'info'])
        channels, current = {}, None
        for line in output.splitlines():
            match = re.search(r'\* (\d+)(?:\.0)? MHz \[(\d+)\](.*)', line)
            if match:
                freq = int(match[1])
                current = None
                if (freq < 3000) != (iface == 'wlan0') or 'disabled' in match[3]:
                    continue
                try:
                    frequency_channel(freq)
                except ValueError:
                    continue
                current = channels.setdefault(freq, {'dfs': 'radar detection' in match[3],
                    'no_ir': 'no IR' in match[3], 'unavailable': False})
            elif current is not None and 'DFS state:' in line:
                current['unavailable'] = 'unavailable' in line
        return phy, channels

    def choices(self):
        """Offer present built-in bands and describe their EUD consequences."""
        settings = self.radio.settings()
        eud = settings.get('eud', 'wireless')
        ap = text(self.roles / 'ap_interface')
        result = []
        for iface in BANDS:
            try:
                self.capabilities(iface)
                # Shared operation needs the MT7916 5 GHz EUD configuration.
                if iface == 'wlan1' and eud != 'wired' and ap not in ('wlan1', AP_VIF):
                    continue
                result.append({'iface': iface, 'band': BANDS[iface],
                               'shared_ap': iface == 'wlan1' and eud != 'wired'})
            except (ValueError, RuntimeError):
                continue
        return result

    def ap_wanted(self):
        """Follow current wireless/auto EUD policy without acquiring dispatcher locks."""
        mode = self.radio.settings().get('eud')
        if mode == 'wireless':
            return True
        if mode != 'auto':
            return False
        wired = (self.radio.sysnet / 'end0/master').resolve().name == 'br0'
        acs = self.radio.settings().get('acs', '').lower() in ('y', 'yes', '1', 'true')
        return not wired and (not acs or self.radio.gateway.exists())

    def ipv4_address(self, iface):
        """Read the client address separately from wireless association."""
        try:
            rows = json.loads(self.invoke(
                ['ip', '-4', '-j', 'addr', 'show', 'dev', iface],
                optional=True))
            return next((a['local'] for row in rows
                         for a in row.get('addr_info', [])
                         if a.get('family') == 'inet'), '')
        except (ValueError, KeyError, TypeError):
            return ''

    def read_status(self):
        """Return public local state without credentials or recovery data."""
        profile = self.profile()
        status = json.loads(text(self.status_path, '{}'))
        address = (self.ipv4_address(profile['iface'])
                   if profile['enabled'] else '')
        if status.get('state') in ('connected', 'waiting-address'):
            status['state'] = 'connected' if address else 'waiting-address'
        status.update(enabled=profile['enabled'], iface=profile.get('iface', ''),
                      ssid=profile.get('ssid', ''), bssid=profile.get('bssid', ''),
                      security=profile.get('security', ''), choices=self.choices(), ip=address,
                      gateway=bool(profile['enabled'] and self.radio.gateway.exists()
                                   and text(self.run / 'upstream_iface') == profile['iface']))
        if profile.get('blocked_ssid') or is_mesh_hotspot(profile.get('ssid'), self.radio.settings()):
            status.update(enabled=False, connected=False, gateway=False, ip='',
                          state='blocked', error=OWN_HOTSPOT_REASON,
                          ssid=profile.get('blocked_ssid', profile.get('ssid', '')))
        status['scan'] = self.saved_scan()
        return status

    def publish(self, state, **values):
        """Record watcher progress for the admin UI."""
        self.store(self.status_path, dict(state=state, **values))

    def shared(self):
        """Identify the virtual EUD AP associated with the active client."""
        return uplink_owner(self.roles) == 'wlan1' and text(self.roles / 'ap_interface') == AP_VIF

    def ap_status(self):
        """Read hostapd state, including DFS CAC, without relying on carrier."""
        output = self.invoke(['hostapd_cli', '-s', str(self.run), '-i', AP_VIF, 'status'], optional=True)
        return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)

    def dfs_follow_supported(self):
        """Require the loaded kernel opt-in and the paired hostapd protocol."""
        if text(DFS_FOLLOW_CAP) != 'Y':
            return False
        try:
            output = self.radio.command(['/usr/sbin/hostapd', '-v'], timeout=3)
        except subprocess.CalledProcessError as error:
            # hostapd 2.10 deliberately exits 1 after printing its version.
            if error.returncode != 1:
                return False
            output = (error.stdout or '') + (error.stderr or '')
        except (OSError, subprocess.SubprocessError):
            return False
        return DFS_FOLLOW_MARKER in output

    def prepare_shared_ap(self):
        """Validate shared AP start without downing or retyping the client PHY."""
        if not self.shared() or not (self.radio.sysnet / AP_VIF).exists():
            raise ValueError('Shared AP is not ready')
        # Starts from policy/watchdogs must recheck the station, since its
        # channel can change between watcher passes.
        link = self.invoke(['iw', 'dev', 'wlan1', 'link'], optional=True)
        match = re.search(r'^\s*freq: (\d+)', link, re.M)
        if not match:
            raise ValueError('Waiting for upstream association before '
                             'starting the EUD AP')
        freq = int(match[1])
        _, channels = self.capabilities('wlan1')
        channel = channels.get(freq)
        if not channel or channel['unavailable']:
            raise ValueError('Upstream channel is unavailable for the EUD AP')
        if channel['dfs']:
            if not self.dfs_follow_supported():
                raise ValueError(DFS_AP_REASON)
            prior = json.loads(text(self.status_path, '{}'))
            if prior.get('dfs_failed_freq') == freq:
                raise ValueError(DFS_FAILURE_REASON)
        if channel['no_ir']:
            raise ValueError('Upstream channel is unavailable for the EUD AP')
        self.write_ap_channel(freq, dfs_follow=channel['dfs'])

    def write_ap_channel(self, freq, *, dfs_follow=False):
        """Set HT20, enabling the paired follower mode only for the shared DFS AP."""
        values = {'interface': AP_VIF, 'channel': str(frequency_channel(freq)),
                  'hw_mode': 'a', 'ieee80211d': '1', 'ieee80211h': '1',
                  'ctrl_interface': '/run/hostapd', 'ht_capab': '',
                  'vht_oper_chwidth': '0', 'vht_oper_centr_freq_seg0_idx': '0'}
        # Stock hostapd must never see the private follower option, even =0.
        source = re.sub(r'^\s*dfs_follow_station=.*\n?', '',
                        self.hostapd.read_text(), flags=re.M)
        if dfs_follow:
            values['dfs_follow_station'] = '1'
        atomic_write(self.hostapd, rewrite_keys(source, values))

    def withdraw(self, iface):
        """Remove only this PHY from mesh role files under the channel lock."""
        self.radio.set_roles(iface)
        atomic_write(self.roles / 'wifi_uplink_if', iface + '\n', mode=0o644)

    def activate(self, profile):
        """Reserve the PHY, configure DHCP and start its dedicated client."""
        reject_mesh_hotspot(profile['ssid'], self.radio.settings())
        iface = profile['iface']
        phy, channels = self.capabilities(iface)
        if not any(row['iface'] == iface for row in self.choices()):
            raise ValueError('Radio is unavailable in this EUD mode')
        if profile['freq'] not in channels:
            raise ValueError('Selected channel is disabled on this radio')
        settings = self.radio.settings()
        shared = iface == 'wlan1' and settings.get('eud') != 'wired'
        members = self.invoke(['batctl', 'if'])
        survivors = set(re.findall(r'^(\S+):\s+active', members, re.M)) - {iface}
        if iface == 'wlan0' and settings.get('eud') != 'wired':
            survivors &= set(text(self.roles / 'halow_if').split())
        if not survivors:
            raise ValueError('A working mesh radio must remain after enabling the uplink')
        rendered = client_config(profile, sorted(channels), settings.get('regulatory_domain', ''))
        if not self.saved.exists():
            self.store(self.saved, {'iface': iface,
                'ap_interface': text(self.roles / 'ap_interface'),
                'ap_mesh_band': text(self.roles / 'ap_mesh_band'),
                'no_mesh_if': text(self.roles / 'no_mesh_if'),
                'hostapd': text(self.hostapd), 'shared': shared})
        if shared:
            self.invoke(['systemctl', 'stop', 'hostapd.service'], timeout=30)
        with locked(self.radio.channel_lock):
            self.withdraw(iface)
            self.invoke(['systemctl', 'stop', f'wpa_supplicant@{iface}.service'], timeout=30)
            if self.radio.attached(iface):
                self.invoke(['batctl', 'if', 'del', iface])
            self.invoke(['ip', 'link', 'set', iface, 'down'])
            self.invoke(['ip', 'link', 'set', iface, 'nomaster'])
            self.invoke(['iw', 'dev', iface, 'set', 'type', 'managed'])
            self.invoke(['iw', 'phy', phy, 'set', 'txpower', 'auto'])
            self.invoke(['ip', 'link', 'set', iface, 'mtu', '1500'])
            self.invoke(['ip', 'link', 'set', iface, 'up'])
            self.radio.wpa.mkdir(parents=True, exist_ok=True)
            target = self.radio.wpa / f'wpa_supplicant-{iface}-uplink.conf'
            atomic_write(target, rendered, mode=0o600)
            if shared:
                self.create_ap(phy)
                atomic_write(self.roles / 'ap_interface', AP_VIF + '\n')
                atomic_write(self.roles / 'ap_mesh_band', '\n')
                nonmesh = set(text(self.roles / 'no_mesh_if').split()) - {iface}
                atomic_write(self.roles / 'no_mesh_if', '\n'.join(sorted(nonmesh | {AP_VIF})) + '\n')
            self.network.mkdir(parents=True, exist_ok=True)
            atomic_write(self.network / f'05-{iface}-wifi-uplink.network',
                f'[Match]\nName={iface}\n[Link]\nRequiredForOnline=no\nMTUBytes=1500\n'
                '[Network]\nDHCP=ipv4\nIPv6AcceptRA=yes\nBridge=\n'
                '[DHCPv4]\nUseDNS=no\nUseDomains=no\nRoutesToDNS=no\nRequestOptions=6 15 119\n'
                'UseRoutes=yes\nRouteMetric=1024\n[IPv6AcceptRA]\n'
                'UseDNS=no\nUseDomains=no\n', mode=0o644)
            self.invoke(['networkctl', 'reload'])
            self.invoke(['systemctl', 'restart', f'manet-wifi-client@{iface}.service'], timeout=30)
            self.invoke(['networkctl', 'reconfigure', iface])
        self.publish('connecting', shared_ap=shared)

    def ap_address(self, phy):
        """Derive a stable unicast AP MAC, excluding all local addresses."""
        source = next((self.radio.sysnet / iface / 'phy80211/macaddress'
                       for iface in BANDS
                       if text(self.radio.sysnet / iface / 'phy80211/name')
                       == phy), None)
        address = text(source) if source else ''
        if not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', address):
            raise ValueError('Radio PHY address is unavailable')
        octets = bytes.fromhex(address.replace(':', ''))
        first = (octets[0] | 2) & 0xfe
        suffix = int.from_bytes(octets[1:], 'big')
        used = {text(path).lower()
                for path in self.radio.sysnet.glob('*/address')
                if path.parent.name != AP_VIF}
        # Advance only the low 40 bits so local/unicast bits stay fixed.
        # At most len(used) candidates can collide, including br0 and bat0.
        for offset in range(len(used) + 1):
            tail = ((suffix + offset) % (1 << 40)).to_bytes(5, 'big')
            candidate = bytes([first]) + tail
            rendered = ':'.join(f'{byte:02x}' for byte in candidate)
            if rendered not in used:
                return rendered
        raise RuntimeError('No unused local AP address')

    def create_ap(self, phy):
        """Create a locally administered AP VIF without renaming physical radios."""
        if not (self.radio.sysnet / AP_VIF).exists():
            self.invoke(['iw', 'phy', phy, 'interface', 'add', AP_VIF,
                         'type', '__ap', 'addr', self.ap_address(phy)])
        elif text(self.radio.sysnet / AP_VIF / 'phy80211/name') != phy:
            raise ValueError('EUD virtual interface belongs to another PHY')
        else:
            address = self.ap_address(phy)
            if text(self.radio.sysnet / AP_VIF / 'address').lower() != address:
                self.invoke(['ip', 'link', 'set', AP_VIF, 'down'])
                self.invoke(['ip', 'link', 'set', AP_VIF, 'address', address])
        self.network.mkdir(parents=True, exist_ok=True)
        atomic_write(self.network / '05-manetap.network',
                     '[Match]\nName=manetap\n[Link]\nUnmanaged=yes\n'
                     'ActivationPolicy=manual\n', mode=0o644)

    def release(self):
        """Stop client ownership and restore current mesh/AP policy on this PHY."""
        saved = json.loads(text(self.saved, '{}'))
        iface = saved.get('iface') or uplink_owner(self.roles)
        if not iface:
            return
        shared = saved.get('shared', False)
        if shared:
            self.invoke(['systemctl', 'stop', 'hostapd.service'], timeout=30)
        with locked(self.radio.channel_lock):
            self.invoke(['systemctl', 'stop', f'manet-wifi-client@{iface}.service'], timeout=30)
            self.invoke(['ip', 'link', 'set', iface, 'down'])
            self.invoke(['ip', 'addr', 'flush', 'dev', iface])
            (self.network / f'05-{iface}-wifi-uplink.network').unlink(missing_ok=True)
            (self.radio.wpa / f'wpa_supplicant-{iface}-uplink.conf').unlink(missing_ok=True)
            if shared:
                if (self.radio.sysnet / AP_VIF).exists():
                    self.invoke(['iw', 'dev', AP_VIF, 'del'])
                (self.network / '05-manetap.network').unlink(missing_ok=True)
                for name in ('ap_interface', 'ap_mesh_band', 'no_mesh_if'):
                    atomic_write(self.roles / name, saved.get(name, '') + '\n')
                # Preserve any local EUD SSID/password changes during sharing.
                current = load_kv_file(self.hostapd)
                original = saved.get('hostapd', '')
                values = {key: current[key] for key in ('ssid', 'wpa_passphrase') if key in current}
                atomic_write(self.hostapd, rewrite_keys(original, values))
            (self.roles / 'wifi_uplink_if').unlink(missing_ok=True)
            if iface != text(self.roles / 'ap_interface'):
                self.radio.set_roles(iface, BANDS[iface])
                if not self.radio.disabled(iface):
                    acs = self.radio.settings().get('acs', '').lower() in ('y', 'yes', 'true', '1')
                    # Disconnect must work during agreement. Under the channel
                    # lock, read the existing commitment (or recovery anchor),
                    # without starting a round or changing agreement state.
                    freq, _ = self.radio.select_frequency(
                        iface, BANDS[iface], acs, allow_busy=True)
                    conf = self.radio.wpa / f'wpa_supplicant-{iface}.conf'
                    atomic_write(conf, self.radio.render_config(self.radio.settings(), freq))
                    self.radio.start_mesh(iface, freq)
            self.invoke(['networkctl', 'reload'])
            self.invoke(['networkctl', 'reconfigure', iface])
        self.saved.unlink(missing_ok=True)
        # AP policy runs after all locks are released by the caller.
        self.publish('disabled')

    def set_profile(self, value):
        """Apply a local admin request, restoring the prior profile on failure."""
        desired = validate_profile(value)
        if desired['enabled']:
            reject_mesh_hotspot(desired['ssid'], self.radio.settings())
        with locked(self.lock), locked(self.radio.role_lock):
            old = self.profile()
            if is_mesh_hotspot(old.get('ssid'), self.radio.settings()):
                old = {'enabled': False, 'blocked_ssid': old['ssid'], 'blocked_iface': old['iface']}
            if desired['enabled']:
                scans = json.loads(text(self.scan_path, '{}'))
                age = time.clock_gettime(time.CLOCK_BOOTTIME) - scans.get('at', -1000)
                matches = [r for r in scans.get('networks', []) if r.get('selectable') and all(
                    desired[k] == r.get(k) for k in ('ssid', 'bssid', 'freq', 'security'))]
                if scans.get('iface') != desired['iface'] or not 0 <= age <= 300 or not matches:
                    raise ValueError('Scan again and choose a network from the fresh list')
            try:
                self.release()
                self.store(self.config, desired)
                if desired['enabled']:
                    self.activate(desired)
            except Exception as error:
                try:
                    self.release()
                    self.store(self.config, old)
                    if old['enabled']:
                        self.activate(old)
                except Exception as recovery:
                    self.publish('recovery-failed')
                    raise RuntimeError('Wi-Fi activation and recovery failed; retry or disconnect') from recovery
                detail = ': ' + str(error) if isinstance(error, ValueError) else ''
                raise RuntimeError('Wi-Fi activation failed' + detail + '; previous profile restored') from error
        self.refresh_policy()
        return self.read_status()

    def refresh_policy(self):
        """Request gateway/EUD policy only after releasing every radio lock."""
        self.invoke(['/usr/local/bin/manet-uplink-dispatch.sh', 'reconcile'], timeout=60, optional=True)

    def saved_scan(self):
        """Expose saved scans with boot-clock age, without renewing them."""
        scan = json.loads(text(self.scan_path, '{}'))
        if not scan:
            return None
        settings = self.radio.settings()
        fields = ('ssid', 'ssid_hex', 'bssid', 'freq', 'channel', 'signal',
                  'security', 'selectable', 'reason', 'dfs')
        scan['networks'] = [{k: row[k] for k in fields if k in row}
                            for row in scan.get('networks', [])
                            if not is_mesh_hotspot(row.get('ssid'), settings)]
        age = time.clock_gettime(time.CLOCK_BOOTTIME) - scan['at']
        return dict(scan, age_seconds=max(0, age), fresh=0 <= age <= 300)

    def scan_plan(self, iface, channels=None):
        """Describe interruption using live station and AP state."""
        if channels is None:
            _, channels = self.capabilities(iface)
        ap_iface = text(self.roles / 'ap_interface')
        was_ap = (iface == ap_iface or
                  (iface == 'wlan1' and ap_iface == AP_VIF))
        was_ap = was_ap and self.radio.active('hostapd.service')
        keep_ap = False
        if was_ap and self.shared() and iface == 'wlan1':
            link = self.invoke(['iw', 'dev', iface, 'link'], optional=True)
            match = re.search(r'^\s*freq: (\d+)', link, re.M)
            ap = self.ap_status()
            channel = channels.get(int(match[1]), {}) if match else {}
            keep_ap = bool(match and channel and
                           not channel.get('unavailable') and
                           ap.get('state') == 'ENABLED' and
                           ap.get('freq') == match[1] and
                           (not channel['dfs'] or
                            (ap.get('dfs_follow_station') == '1' and
                             self.dfs_follow_supported())))
        return {'iface': iface, 'interrupts_ap': bool(was_ap and not keep_ap)}

    def restore_scan_owner(self):
        """Retry interrupted ownership separately from successful observations.

        The caller holds the client and role locks. The mesh config was set
        to live MHz before scanning; a later agreement may have updated it.
        """
        repair = json.loads(text(self.repair_path, '{}'))
        if not repair:
            return
        iface = repair['iface']
        if repair['kind'] == 'mesh':
            with locked(self.radio.channel_lock):
                if (iface in text(self.roles / 'mesh_if').split() and
                        not uplink_owned(iface, self.roles) and
                        not self.radio.disabled(iface)):
                    config = self.radio.wpa / f'wpa_supplicant-{iface}.conf'
                    match = re.search(r'^\s*frequency=(\d+)',
                                      text(config), re.M)
                    freq = int(match[1]) if match else repair['freq']
                    if not self.radio.healthy(iface, freq):
                        self.radio.start_mesh(iface, freq)
                    elif not self.radio.attached(iface):
                        self.invoke(['batctl', 'if', 'add', iface])
        elif self.ap_wanted():
            if self.shared():
                self.follow_ap(self.profile())
            else:
                self.invoke(['systemctl', 'start', 'hostapd.service'],
                            timeout=45)
        self.repair_path.unlink(missing_ok=True)
        scan = json.loads(text(self.scan_path, '{}'))
        if scan.get('at') == repair.get('at') and 'warning' in scan:
            scan.pop('warning')
            self.store(self.scan_path, scan)

    def scan_warning(self, error):
        """Keep public restore diagnostics free of subprocess arguments."""
        detail = (str(error) if isinstance(error, (ValueError, RuntimeError))
                  else 'Radio restore command failed')
        repair = json.loads(text(self.repair_path, '{}'))
        owner = 'Mesh' if repair.get('kind') == 'mesh' else 'Phone hotspot'
        return f'{owner} restore pending: {detail}. Retrying automatically.'

    def discover(self, iface, *, allow_ap_stop=True):
        """Keep a supported shared AP serving and retain scans across repair."""
        with locked(self.lock), locked(self.radio.role_lock):
            # A pending repair may start an AP. Observe interruption only
            # after it finishes, while still holding the ownership locks.
            self.restore_scan_owner()
            _, channels = self.capabilities(iface)
            plan = self.scan_plan(iface, channels)
            if plan['interrupts_ap'] and not allow_ap_stop:
                raise ValueError('Scan would disconnect phones on this '
                                 'hotspot for about 15 seconds. '
                                 'Confirm and scan again.')
            owner = uplink_owner(self.roles)
            was_mesh = iface in text(self.roles / 'mesh_if').split()
            result = None
            try:
                if plan['interrupts_ap']:
                    self.store(self.repair_path,
                               {'iface': iface, 'kind': 'ap'})
                    self.invoke(['systemctl', 'stop', 'hostapd.service'],
                                timeout=30)
                with locked(self.radio.channel_lock):
                    if was_mesh:
                        config = (self.radio.wpa /
                                  f'wpa_supplicant-{iface}.conf')
                        live = re.search(r'channel.*\((\d+) MHz\)',
                                         self.radio.radio_info(iface))
                        if live:
                            freq = int(live[1])
                        else:
                            acs = self.radio.settings().get('acs', '').lower()
                            freq, _ = self.radio.select_frequency(
                                iface, BANDS[iface],
                                acs in ('y', 'yes', '1', 'true'),
                                allow_busy=True)
                        # start_mesh reads this file. A stale frequency here
                        # otherwise defeats a correct readiness target.
                        source = text(config)
                        if re.search(r'^\s*frequency=\d+', source, re.M):
                            source = re.sub(r'^(\s*frequency=)\d+',
                                            lambda m: m[1] + str(freq),
                                            source, flags=re.M)
                        else:
                            source = self.radio.render_config(
                                self.radio.settings(), freq)
                        atomic_write(config, source + '\n')
                        self.store(self.repair_path, {'iface': iface,
                                   'kind': 'mesh', 'freq': freq})
                        self.invoke(['systemctl', 'stop',
                                     f'wpa_supplicant@{iface}.service'],
                                    timeout=30)
                        if self.radio.attached(iface):
                            self.invoke(['batctl', 'if', 'del', iface])
                    if owner != iface:
                        self.invoke(['ip', 'link', 'set', iface, 'down'])
                        self.invoke(['ip', 'link', 'set', iface, 'nomaster'])
                        self.invoke(['iw', 'dev', iface, 'set', 'type',
                                     'managed'])
                        self.invoke(['ip', 'link', 'set', iface, 'up'])
                    output = self.invoke(['iw', 'dev', iface, 'scan', 'freq',
                                          *map(str, sorted(channels)),
                                          'passive'], timeout=35)
                    rows = [dict(row, dfs=channels[row['freq']]['dfs'])
                            for row in scan_records(output, self.radio.settings())
                            if row['freq'] in channels]
                    for row in rows:
                        if channels[row['freq']]['unavailable']:
                            row.update(selectable=False, reason=
                                       'Channel unavailable after radar '
                                       'detection')
                    result = {'iface': iface, 'networks': rows,
                              'at': time.clock_gettime(time.CLOCK_BOOTTIME)}
                    # Save before any restart can fail or lose the browser.
                    self.store(self.scan_path, result)
                    if self.repair_path.exists():
                        repair = json.loads(self.repair_path.read_text())
                        self.store(self.repair_path,
                                   dict(repair, at=result['at']))
            finally:
                try:
                    self.restore_scan_owner()
                    profile = self.profile()
                    if profile['enabled']:
                        self.follow_ap(profile)
                except (ValueError, RuntimeError, OSError,
                        subprocess.SubprocessError) as error:
                    if result is not None:
                        if not self.repair_path.exists() and self.shared():
                            self.store(self.repair_path, {'iface': 'wlan1',
                                       'kind': 'ap', 'at': result['at']})
                        result['warning'] = self.scan_warning(error)
                        self.store(self.scan_path, result)
                    # An unsuccessful scan keeps its original exception.
                    # The repair record and watcher own restoration retries.
        return self.saved_scan()

    def station_control(self, iface, action):
        """Require supplicant acknowledgement before relying on quiet/reconnect state."""
        result = self.invoke(['wpa_cli', '-s', str(self.run), '-i', iface, action])
        if 'OK' not in result.splitlines():
            raise RuntimeError('Wi-Fi client did not acknowledge ' + action)

    def follow_ap(self, profile):
        """Follow live station MHz with bounded starts and no local DFS CAC."""
        iface = profile['iface']
        link = self.invoke(['iw', 'dev', iface, 'link'], optional=True)
        match = re.search(r'^\s*freq: (\d+)', link, re.M)
        prior = json.loads(text(self.status_path, '{}'))
        associated_state = ('connected' if match and self.ipv4_address(iface)
                            else 'waiting-address')
        if not self.shared():
            self.publish(associated_state if match else 'connecting',
                         connected=bool(match),
                         freq=int(match[1]) if match else None)
            return
        ap = self.ap_status()
        if not match:
            # AP and STA cannot use different channel contexts. On loss,
            # release even an old CAC/reconnect wait before asking for a scan.
            if ap.get('state') or self.radio.active('hostapd.service'):
                self.invoke(['systemctl', 'stop', 'hostapd.service'],
                            timeout=30)
            self.station_control(iface, 'reconnect')
            self.publish('connecting', shared_ap=True, connected=False)
            return
        freq = int(match[1])
        _, channels = self.capabilities(iface)
        allowed = channels.get(freq)
        follower = bool(allowed and allowed['dfs'] and
                        self.dfs_follow_supported())
        ap_channel = channels.get(int(ap.get('freq') or 0), {})
        configured_follow = ap.get('dfs_follow_station') == '1'
        # Release old CAC/radar contexts, including post-CAC state. A new
        # follower unexpectedly entering CAC is a failure, never a retry loop.
        unexpected_cac = ap.get('state') == 'DFS' and configured_follow
        if (ap.get('state') == 'DFS' or
                (ap_channel.get('dfs') and not (follower and configured_follow))):
            self.invoke(['systemctl', 'stop', 'hostapd.service'], timeout=30)
            ap = {}
        if not allowed or allowed['unavailable'] or (allowed['no_ir'] and not allowed['dfs']):
            self.invoke(['systemctl', 'stop', 'hostapd.service'], timeout=30)
            self.publish('ap-channel-unavailable', freq=freq, connected=True, shared_ap=True)
            return
        if allowed['dfs'] and not follower:
            # Missing/disabled kernel support or stock hostapd: retain 012c's
            # bounded fallback, without disconnecting the upstream.
            if ap.get('state') or self.radio.active('hostapd.service'):
                self.invoke(['systemctl', 'stop', 'hostapd.service'],
                            timeout=30)
            self.publish(associated_state, freq=freq, connected=True,
                         shared_ap=True, ap_blocked='dfs-upstream',
                         ap_reason=DFS_AP_REASON)
            return
        if follower and (unexpected_cac or prior.get('dfs_failed_freq') == freq):
            self.invoke(['systemctl', 'stop', 'hostapd.service'], timeout=30)
            self.publish(associated_state, freq=freq, connected=True,
                         shared_ap=True, ap_blocked='dfs-start-failed',
                         ap_reason=DFS_FAILURE_REASON, dfs_failed_freq=freq)
            return
        if not self.ap_wanted():
            if ap.get('state') or self.radio.active('hostapd.service'):
                self.invoke(['systemctl', 'stop', 'hostapd.service'], timeout=30)
            self.publish(associated_state, freq=freq, connected=True,
                         shared_ap=True)
            return
        if ap.get('state') == 'ENABLED' and ap.get('freq') == str(freq):
            self.publish(associated_state, freq=freq, connected=True,
                         shared_ap=True, dfs_follow=follower)
            return
        if time.monotonic() < self.defer_ap_until:
            # The kernel revokes a DFS follower during the upstream CSA
            # countdown. Wait for completion before spending a start attempt.
            # A missing completion gets a bounded live check after three seconds.
            self.publish('ap-starting', freq=freq, connected=True, shared_ap=True,
                         dfs_attempts=prior.get('dfs_attempts', 0))
            return
        attempts = (prior.get('dfs_attempts', 0)
                    if prior.get('freq') == freq else 0)
        if follower and attempts >= 3:
            self.invoke(['systemctl', 'stop', 'hostapd.service'], timeout=30)
            self.publish(associated_state, freq=freq, connected=True,
                         shared_ap=True, ap_blocked='dfs-start-failed',
                         ap_reason=DFS_FAILURE_REASON, dfs_failed_freq=freq)
            return
        self.publish('ap-starting', freq=freq, connected=True, shared_ap=True,
                     dfs_attempts=attempts + 1 if follower else 0)
        with locked(self.radio.channel_lock):
            self.write_ap_channel(freq, dfs_follow=follower)
        # Non-DFS CSA is preferred. An OK is only acceptance: verify next pass.
        if ap.get('state') == 'ENABLED' and not follower and not ap_channel.get('dfs'):
            result = self.invoke(['hostapd_cli', '-s', str(self.run), '-i', AP_VIF, 'chan_switch', '5', str(freq),
                                  'bandwidth=20', f'center_freq1={freq}', 'ht'], optional=True)
            if 'OK' in result.splitlines() and prior.get('state') != 'ap-starting':
                return
        self.invoke(['systemctl', 'stop', 'hostapd.service'], timeout=30)
        self.invoke(['systemctl', 'start', 'hostapd.service'], timeout=45)

    def reserve_profile(self):
        """Withdraw saved client roles before setup starts the remaining mesh radios."""
        with locked(self.lock), locked(self.radio.role_lock), locked(self.radio.channel_lock):
            profile = self.profile()
            if profile['enabled'] and not is_mesh_hotspot(profile['ssid'], self.radio.settings()):
                self.withdraw(profile['iface'])

    def reconcile(self, *, disconnected=False):
        """Recover saved ownership and follow events using live radio state."""
        blocked = False
        with locked(self.lock), locked(self.radio.role_lock):
            profile = self.profile()
            if profile['enabled'] and is_mesh_hotspot(profile['ssid'], self.radio.settings()):
                # Persist disabled intent before cleanup, so a partial restore
                # can retry without ever reconnecting the rejected network.
                self.store(self.config, {'enabled': False, 'blocked_ssid': profile['ssid'],
                                         'blocked_iface': profile['iface']})
                blocked = True
            if not self.profile()['enabled']:
                blocked = blocked or bool(uplink_owner(self.roles))
                self.release()
            try:
                self.restore_scan_owner()
            except (ValueError, RuntimeError, OSError,
                    subprocess.SubprocessError) as error:
                scan = json.loads(text(self.scan_path, '{}'))
                if scan:
                    scan['warning'] = self.scan_warning(error)
                    self.store(self.scan_path, scan)
            profile = self.profile()
            if profile['enabled']:
                if disconnected:
                    # A loss/reconnect can be coalesced before this lock is
                    # available. It still resets a failed follower's latch.
                    prior = json.loads(text(self.status_path, '{}'))
                    prior.pop('dfs_failed_freq', None)
                    prior.pop('dfs_attempts', None)
                    self.store(self.status_path, prior)
                iface = profile['iface']
                if (uplink_owner(self.roles) != iface or not self.radio.active(f'manet-wifi-client@{iface}.service')
                        or (self.saved.exists() and json.loads(self.saved.read_text()).get('shared')
                            and not (self.radio.sysnet / AP_VIF).exists())):
                    self.activate(profile)
                with locked(self.radio.channel_lock):
                    self.withdraw(iface)
                self.follow_ap(profile)
        if blocked:
            self.refresh_policy()

    def watch_once(self, *, disconnected=False):
        """Leave scan/role lock contention as progress, never a client error."""
        setup_lock = Path(os.environ.get('MANET_RADIO_SETUP_LOCK',
                                        '/run/lock/manet-radio-setup.lock'))
        setup_lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Setup keeps its exclusive lock through enumeration and restarts.
            with setup_lock.open('a') as setup:
                fcntl.flock(setup, fcntl.LOCK_SH | fcntl.LOCK_NB)
                self.reconcile(disconnected=disconnected)
            return True
        except BlockingIOError:
            return False
        except Exception as error:
            if str(error) == 'Radio transition busy; retry shortly':
                return False
            # Raw subprocess exceptions may include private config.
            previous = json.loads(text(self.status_path, '{}'))
            previous['error'] = ('Wi-Fi reconciliation failed; '
                                 'retry or disconnect')
            self.store(self.status_path, previous)
            return False

    def watch_pending(self):
        """Exit once disabled cleanup is complete, including interrupted scans."""
        return (self.profile()['enabled'] or self.saved.exists() or
                bool(uplink_owner(self.roles)) or self.repair_path.exists())

    def watch(self):
        """Sleep on events while enabled; leave disabled wakeups to systemd.path."""
        if not self.watch_pending():
            return
        last_policy = None
        disconnected = False
        safety_at = time.monotonic() + SAFETY_INTERVAL
        with UplinkEvents(self) as events:
            reasons = {'change'}
            while True:
                profile = self.profile()
                events.sync(profile.get('iface', ''))
                if 'csa-start' in reasons:
                    self.defer_ap_until = time.monotonic() + RECOVERY_INTERVAL
                if reasons & {'csa-complete', 'disconnect'}:
                    self.defer_ap_until = 0
                disconnected |= 'disconnect' in reasons
                success = self.watch_once(disconnected=disconnected)
                if success:
                    disconnected = False
                    profile = self.profile()
                    state = json.loads(text(self.status_path, '{}'))
                    # Do not let our own file/unit notifications cause a
                    # gateway reconcile feedback loop. DHCP changes count
                    # even if wireless association itself is unchanged.
                    policy = (profile.get('iface'), profile['enabled'],
                              state.get('state'), state.get('freq'),
                              self.ipv4_address(profile['iface'])
                              if profile['enabled'] else '',
                              self.ap_wanted())
                    # Node-manager already requalifies internet reachability
                    # every 60 seconds. Safety only dispatches changed policy.
                    if policy != last_policy:
                        self.refresh_policy()
                        last_policy = policy
                if not self.watch_pending():
                    return
                profile = self.profile()
                events.sync(profile.get('iface', ''), probe='safety' in reasons)
                state = json.loads(text(self.status_path, '{}'))
                retry = (not success or self.repair_path.exists() or
                         state.get('state') == 'ap-starting')
                now = time.monotonic()
                if now >= safety_at:
                    safety_at = now + SAFETY_INTERVAL
                delay = min(RECOVERY_INTERVAL, safety_at - now) if retry else safety_at - now
                reasons = events.wait(delay)
                if time.monotonic() >= safety_at:
                    reasons.add('safety')


def wifi_uplink_main():
    """Dispatch local operations; credentials are read from stdin, never argv."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'set', 'scan',
                        'scan-plan', 'reconcile', 'watch', 'setup-begin',
                        'reserve'))
    parser.add_argument('iface', nargs='?')
    parser.add_argument('--allow-ap-stop', action='store_true')
    args = parser.parse_args()
    uplink = WifiUplink()
    if args.action == 'status':
        result = uplink.read_status()
    elif args.action == 'set':
        result = uplink.set_profile(json.load(sys.stdin))
    elif args.action == 'scan':
        result = uplink.discover(args.iface,
                                 allow_ap_stop=args.allow_ap_stop)
    elif args.action == 'scan-plan':
        result = uplink.scan_plan(args.iface)
    elif args.action == 'setup-begin':
        with locked(uplink.lock), locked(uplink.radio.role_lock):
            uplink.release()
        result = {'ok': True}
    elif args.action == 'reserve':
        uplink.reserve_profile()
        result = {'ok': True}
    elif args.action == 'reconcile':
        uplink.reconcile()
        result = {'ok': True}
    else:
        uplink.watch()
        result = {'ok': True}
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        wifi_uplink_main()
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({'ok': False, 'error': str(error) if isinstance(error, (ValueError, RuntimeError))
                          else 'Wi-Fi operation failed'}))
        sys.exit(1)
