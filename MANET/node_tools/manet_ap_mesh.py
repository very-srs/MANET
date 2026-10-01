#!/usr/bin/env python3
"""Move the provisioned EUD radio between AP and mesh ownership.

Never invoke the Ethernet detector or uplink dispatcher here: those callers
hold policy locks while invoking this helper. Hostapd starts only outside the
channel lock, because its ExecStartPre takes that same lock.
"""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from manet_admin import clock_ready
from manet_config_io import atomic_write, supplicant_string
import manet_acs_agreement as agreement
import manet_rendezvous as rendezvous
import manet_rejoin_channel as rejoin
import manet_static_channels as static

ROLE_FILES = {'2.4': 'mesh_24_if', '5': 'mesh_5_if'}
LOCK_TIMEOUT = 10
READY_TIMEOUT = 10


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Radio transition busy; retry shortly') from error
                time.sleep(.05)
        yield


def text(path, default=''):
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return default


def read_json(path):
    raw = text(path)
    result = json.loads(raw) if raw else {}
    if not isinstance(result, dict):
        raise ValueError(f'Invalid state in {path.name}')
    return result


class Transition:
    def __init__(self):
        self.roles = Path(os.environ.get('MANET_IFACE_STATE_DIR', '/var/lib'))
        self.wpa = Path(os.environ.get('MANET_WPA_DIR', '/etc/wpa_supplicant'))
        self.run = Path(os.environ.get('MANET_ACS_RUN_DIR', '/run'))
        self.state = Path(os.environ.get('MANET_ACS_STATE_DIR', '/var/lib/manet-acs'))
        self.conf = Path(os.environ.get('MANET_MESH_CONF', '/etc/mesh.conf'))
        self.sysnet = Path(os.environ.get('MANET_SYS_NET', '/sys/class/net'))
        self.channel_lock = Path(os.environ.get('MANET_ACS_LOCK_FILE', '/run/channel-election.lock'))
        self.role_lock = Path(os.environ.get('MANET_AP_ROLE_LOCK', str(self.run / 'manet-ap-role.lock')))
        self.radio_state = Path(os.environ.get('MANET_RADIO_STATE_FILE', str(self.roles / 'mesh_radio_state.json')))
        self.boot_id = Path(os.environ.get('MANET_BOOT_ID_FILE', '/proc/sys/kernel/random/boot_id'))
        self.static_plan = Path(os.environ.get('MANET_STATIC_CHANNELS', str(static.PATH)))
        self.registry = Path(os.environ.get('REGISTRY_FILE', '/var/run/mesh_node_registry'))
        self.selection = {}

    def command(self, args, timeout=10):
        return subprocess.run(args, check=True, capture_output=True, text=True,
                              timeout=timeout).stdout

    def active(self, service):
        try:
            self.command(['systemctl', 'is-active', '--quiet', service], timeout=3)
            return True
        except subprocess.CalledProcessError:
            return False

    def radio_info(self, iface):
        return self.command(['iw', 'dev', iface, 'info'], timeout=3)

    def settings(self):
        return {key.strip(): value.strip().strip('\"\'')
                for key, value in (line.split('=', 1) for line in self.conf.read_text().splitlines()
                                   if '=' in line and not line.lstrip().startswith('#'))}

    def identity(self):
        iface = self.ap_identity()
        band = text(self.roles / 'ap_mesh_band', default='missing')
        if band not in ('', '2.4', '5'):
            raise ValueError('Missing AP mesh capability; rerun radio-setup')
        if band and iface in (text(self.roles / 'no_mesh_if').split() + text(self.roles / 'halow_if').split()):
            raise ValueError('AP-only/HaLow hardware cannot become a conventional mesh radio')
        return iface, band

    def ap_identity(self):
        iface = text(self.roles / 'ap_interface')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', iface):
            raise ValueError('Invalid or missing AP interface')
        driver = (self.sysnet / iface / 'device/driver').resolve().name
        if iface in text(self.roles / 'halow_if').split() or driver.startswith('morse'):
            raise ValueError('HaLow hardware cannot serve as the EUD AP')
        return iface

    def phy_power(self, iface, value):
        phy = re.search(r'\bwiphy (\d+)', self.radio_info(iface))
        if not phy:
            raise ValueError('Cannot identify radio PHY')
        name = 'phy' + phy[1]
        # PHY power affects every virtual interface on that radio. A second
        # active interface may have a different AP/mesh/uplink policy.
        for other in self.sysnet.iterdir():
            if (other.name != iface and text(other / 'phy80211/name') == name
                    and int(text(other / 'flags', '0x1'), 0) & 1):
                raise ValueError(f'Cannot change {iface} power: {name} also serves {other.name}')
        self.command(['iw', 'phy', name, 'set', 'txpower', *value])
        if value[0] == 'fixed':
            ceiling = int(value[1]) / 100
            # A regulatory/driver ceiling below the request is valid. A
            # successful iw exit alone does not prove the requested cap stuck.
            for attempt in range(5):
                actual = re.search(r'^\s*txpower (-?\d+(?:\.\d+)?) dBm', self.radio_info(iface), re.M)
                if actual and float(actual[1]) <= ceiling + .01:
                    return
                if attempt < 4:
                    time.sleep(.1)
            raise RuntimeError(f'TX power on {iface} did not settle at or below {ceiling:g} dBm')

    def ap_power(self):
        # Called by hostapd ExecStartPost and by policy reconciliation. A
        # queued old AP job must not cap a radio which has since joined mesh.
        with locked(self.channel_lock):
            iface = self.ap_identity()
            info = self.radio_info(iface)
            if (iface in text(self.roles / 'mesh_if').split()
                    or not re.search(r'^\s*type AP\s*$', info, re.M)):
                return {'mode': 'ap-power', 'changed': False}
            actual = re.search(r'^\s*txpower (-?\d+(?:\.\d+)?) dBm', info, re.M)
            if actual and float(actual[1]) <= 5.01:
                # Keep healing driver resets without rewriting a healthy cap
                # or raising a deliberately lower operator setting.
                return {'mode': 'ap-power', 'changed': False}
            self.phy_power(iface, ['fixed', '500'])
            return {'mode': 'ap-power', 'changed': True}

    def disabled(self, iface):
        desired = read_json(self.radio_state).get('desired', {})
        if not isinstance(desired, dict):
            raise ValueError('Invalid desired radio state')
        return desired.get(iface) == 'down'

    def attached(self, iface):
        return bool(re.search(r'^\s*' + re.escape(iface) + r':',
                              self.command(['batctl', 'if']), re.M))

    def link_down(self, iface):
        flags = text(self.sysnet / iface / 'flags')
        return bool(flags) and not (int(flags, 0) & 1)

    def healthy(self, iface, freq):
        try:
            pong = self.command(['wpa_cli', '-p', '/var/run/wpa_supplicant', '-i', iface, 'ping'], timeout=3)
            info = self.radio_info(iface)
            return ('PONG' in pong.splitlines() and self.active(f'wpa_supplicant@{iface}.service')
                    and bool(re.search(r'^\s*type mesh point\s*$', info, re.M))
                    and bool(re.search(r'channel.*\(' + str(freq) + r' MHz\)', info)))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False

    def set_roles(self, iface, band=''):
        for candidate, filename in ROLE_FILES.items():
            old = text(self.roles / filename)
            new = iface if band == candidate else ('' if old == iface else old)
            if band == candidate and old not in ('', iface):
                raise ValueError(f'{candidate} GHz mesh role already belongs to another radio')
            if new != old:
                atomic_write(self.roles / filename, new + '\n')
        names = [name for name in text(self.roles / 'mesh_if').split() if name != iface]
        if band:
            names.append(iface)
        updated = ''.join(name + '\n' for name in dict.fromkeys(names))
        if updated.strip() != text(self.roles / 'mesh_if'):
            atomic_write(self.roles / 'mesh_if', updated)

    def snapshot(self, paths):
        return {path: (path.read_bytes(), path.stat()) if path.exists() else None for path in paths}

    def restore(self, before):
        failures = []
        for path, saved in before.items():
            try:
                if saved is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, saved[0], metadata=saved[1])
            except OSError:
                failures.append(path.name)
        if failures:
            raise RuntimeError('Cannot restore ' + ', '.join(failures))

    def role_paths(self):
        return [self.roles / name for name in ('mesh_if', *ROLE_FILES.values())]

    def render_config(self, config, freq):
        ssid, key = config.get('mesh_ssid', ''), config.get('mesh_key', '')
        if (not 1 <= len(ssid.encode()) <= 32 or not 8 <= len(key.encode()) <= 63
                or any(ord(c) < 32 or ord(c) == 127 for c in ssid + key)):
            raise ValueError('Invalid mesh SSID or SAE key')
        country = config.get('regulatory_domain', 'US')
        if not re.fullmatch(r'[A-Z]{2}|00', country):
            raise ValueError('Invalid regulatory domain')
        return (f'ctrl_interface=/var/run/wpa_supplicant\ncountry={country}\n'
                'update_config=1\nsae_pwe=1\nap_scan=2\nnetwork={\n'
                f'    ssid={supplicant_string(ssid)}\n    mode=5\n    frequency={freq}\n'
                f'    key_mgmt=SAE\n    sae_password={supplicant_string(key)}\n'
                '    ieee80211w=2\n    mesh_fwding=0\n    group_rekey=0\n}\n')

    def select_frequency(self, iface, band, acs):
        info = self.radio_info(iface)
        phy = re.search(r'\bwiphy (\d+)', info)
        if not phy:
            raise ValueError('Cannot identify the AP radio PHY')
        capabilities = self.command(['iw', 'phy', 'phy' + phy[1], 'info'], timeout=3)
        if not re.search(r'^\s*\* mesh point\s*$', capabilities, re.M):
            raise ValueError('The AP radio does not support mesh mode')
        permitted = rendezvous.permitted_frequencies(capabilities)
        own_macs = [text(path) for path in self.sysnet.glob('*/address')]
        nodes = rejoin.load_registry(self.registry)
        if not acs:
            freq = static.load(self.static_plan)[band]
            reason = 'static-plan'
            searching = False
        else:
            now = int(time.time())
            busy = text(self.run / 'manet-acs-busy')
            if busy and 0 <= int(busy) - now <= 125:
                raise RuntimeError('Channel agreement is in progress; retry the role transition')
            state = read_json(self.state / 'agreement.json')
            current, configured = {}, {}
            stable = True
            for other, filename in ROLE_FILES.items():
                name = text(self.roles / filename)
                if not name or name == iface or self.disabled(name):
                    continue
                actual = re.search(r'channel.*\((\d+) MHz\)', self.radio_info(name))
                saved = re.search(r'^\s*frequency=(\d+)\s*$',
                                  text(self.wpa / f'wpa_supplicant-{name}.conf'), re.M)
                if actual and saved:
                    current[other] = int(actual[1]); configured[other] = int(saved[1])
                else:
                    stable = False
            searching = not current
            freq = rendezvous.ANCHORS[band]
            status = {'boot': text(self.boot_id).replace('-', ''), 'acs': True, 'ready': False,
                      'current': current, 'allowed': {b: sorted(agreement.CHANNELS[b]) for b in current},
                      'stable': stable and current == configured,
                      'discovery': read_json(self.run / 'manet-rendezvous.json').get('mode') == 'search'}
            if clock_ready() and state.get('clock_boot') == status['boot']:
                destination = agreement.live_destination(state.get('destination'), status, now)
                if (destination and band in destination['plan']['channels']
                        and destination['plan']['channels'][band] in permitted):
                    freq = destination['plan']['channels'][band]
            # Telemetry can corroborate a committed ACS destination; it cannot
            # authorize an unagreed channel move. Recovery handles that case.
            reason = 'anchor' if freq == rendezvous.ANCHORS[band] else 'acs-agreement'
        if freq not in permitted:
            raise ValueError(f'The AP radio cannot initiate mesh operation at {freq} MHz')
        evidence, peers, other = rejoin.check_frequency(band, freq, nodes, own_macs)
        self.selection = {'frequency': freq, 'source': reason, 'registry': evidence,
                          'peers': peers, 'peer_frequency': other}
        return freq, searching

    def ready(self, iface, freq):
        deadline = time.monotonic() + READY_TIMEOUT
        while True:
            if self.healthy(iface, freq):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('Mesh supplicant did not reach the requested channel')
            time.sleep(.25)

    def start_mesh(self, iface, freq):
        self.command(['ip', 'link', 'set', iface, 'down'])
        self.command(['ip', 'link', 'set', iface, 'nomaster'])
        self.command(['iw', 'dev', iface, 'set', 'type', 'mp'])
        self.command(['ip', 'link', 'set', iface, 'mtu', '1532'])
        self.command(['ip', 'link', 'set', iface, 'up'])
        # AP operation sets a PHY-wide 5 dBm cap. Stopping its oneshot does
        # not undo that cap; let cfg80211 choose mesh power for this channel.
        self.phy_power(iface, ['auto'])
        self.command(['systemctl', 'restart', f'wpa_supplicant@{iface}.service'], timeout=30)
        self.ready(iface, freq)
        if not self.attached(iface):
            self.command(['batctl', 'if', 'add', iface])
        if not self.attached(iface):
            raise RuntimeError('Radio did not attach to bat0')

    def to_mesh(self):
        with locked(self.role_lock):
            iface, band = self.identity()
            service = f'wpa_supplicant@{iface}.service'
            was_ap = self.active('hostapd.service')
            # Fail a busy/invalid request before interrupting a serving AP.
            if was_ap and band:
                with locked(self.channel_lock):
                    if text(self.roles / ROLE_FILES[band]) not in ('', iface):
                        raise RuntimeError('Requested mesh band belongs to another radio')
                    config = self.settings()
                    self.select_frequency(iface, band, config.get('acs', '').lower() in ('y', 'yes', '1', 'true'))
            # Do not wait on hostapd's ExecStartPre while holding its channel lock.
            try:
                self.command(['systemctl', 'stop', 'hostapd.service'], timeout=30)
                with locked(self.channel_lock):
                    before = self.snapshot(self.role_paths())
                    had_mesh = iface in text(self.roles / 'mesh_if').split()
                    live = self.wpa / f'wpa_supplicant-{iface}.conf'
                    lobby = self.wpa / f'wpa_supplicant-{iface}-lobby.conf'
                    before.update(self.snapshot([live, lobby, self.run / 'manet-rendezvous.json']))
                    try:
                        config = self.settings()
                        acs = config.get('acs', '').lower() in ('y', 'yes', '1', 'true')
                        down = self.disabled(iface)
                        if band:
                            owner = text(self.roles / ROLE_FILES[band])
                            if owner not in ('', iface):
                                raise ValueError('Requested mesh band belongs to another radio')
                            # The agreement daemon owns a radio already in mesh.
                            # Reconcile must not reset it to a fallback anchor.
                            if had_mesh and owner == iface and not was_ap:
                                if down and not self.active(service) and not self.attached(iface) and self.link_down(iface):
                                    return {'mode': 'mesh-disabled', 'changed': False}
                                saved = re.search(r'^\s*frequency=(\d+)\s*$', text(live), re.M)
                                expected = int(saved[1]) if saved else None
                                if not acs:
                                    expected = static.load(self.static_plan)[band]
                                if (not down and expected and self.attached(iface)
                                        and self.healthy(iface, expected)):
                                    return {'mode': 'mesh', 'changed': False}
                            freq, search = self.select_frequency(iface, band, acs)
                            self.wpa.mkdir(parents=True, exist_ok=True)
                            atomic_write(live, self.render_config(config, freq))
                            atomic_write(lobby, self.render_config(config, rendezvous.ANCHORS[band] if acs else freq))
                        elif (not was_ap and not had_mesh and not self.active(service)
                              and not self.attached(iface) and self.link_down(iface)):
                            return {'mode': 'off', 'changed': False}
                        self.command(['systemctl', 'stop', 'ap-txpower.service'])
                        self.command(['systemctl', 'stop', service], timeout=20)
                        if self.attached(iface):
                            self.command(['batctl', 'if', 'del', iface])
                        self.command(['ip', 'link', 'set', iface, 'down'])
                        self.command(['ip', 'link', 'set', iface, 'nomaster'])
                        self.set_roles(iface, band)
                        if band and not down:
                            if acs and search:
                                rendezvous.Discovery(self.run).set_mode('search')
                            self.start_mesh(iface, freq)
                        self.command(['systemctl', 'disable', 'hostapd.service'])
                        return {'mode': ('mesh-disabled' if down else 'mesh') if band else 'off',
                                'iface': iface, 'band': band, 'changed': True, **self.selection}
                    except Exception as error:
                        failures = []
                        try:
                            self.command(['systemctl', 'stop', service], timeout=20)
                            if self.attached(iface):
                                self.command(['batctl', 'if', 'del', iface])
                        except (OSError, subprocess.SubprocessError):
                            failures.append('radio teardown')
                        try:
                            self.restore(before)
                        except (OSError, RuntimeError):
                            failures.append('configuration restore')
                        try:
                            if had_mesh and not was_ap and not self.disabled(iface):
                                old = re.search(r'^\s*frequency=(\d+)\s*$', text(live), re.M)
                                if old:
                                    self.start_mesh(iface, int(old[1]))
                        except (OSError, RuntimeError, subprocess.SubprocessError):
                            failures.append('mesh recovery')
                        if failures:
                            raise RuntimeError('Recovery failed: ' + ', '.join(failures)) from error
                        raise
            except Exception as error:
                if was_ap:
                    try:
                        # ExecStartPre acquires channel lock; we have released it.
                        self.command(['systemctl', 'start', 'hostapd.service'], timeout=60)
                        self.command(['systemctl', 'start', 'ap-txpower.service'])
                    except Exception as recovery:
                        raise RuntimeError('Mesh transition failed; AP recovery also failed') from recovery
                raise RuntimeError(f'Cannot return AP radio to mesh: {error}') from error

    def prepare_ap(self):
        with locked(self.channel_lock):
            iface = self.ap_identity()
            before = self.snapshot(self.role_paths())
            had_mesh = iface in text(self.roles / 'mesh_if').split()
            try:
                # Withdraw roles before hardware changes. Other radio writers
                # take the same lock and therefore cannot act on a stale list.
                # Do not defer AP service for a prepared ACS vote: withdrawing
                # this band makes the next protocol status reflect its absence.
                self.set_roles(iface)
                if self.active('hostapd.service'):
                    return {'mode': 'ap', 'changed': False}
                self.command(['/usr/local/bin/unblock-wifi-rfkill.sh'], timeout=5)
                self.command(['systemctl', 'stop', f'wpa_supplicant@{iface}.service'], timeout=20)
                if self.attached(iface):
                    self.command(['batctl', 'if', 'del', iface])
                self.command(['ip', 'link', 'set', iface, 'down'])
                self.command(['ip', 'link', 'set', iface, 'nomaster'])
                self.command(['iw', 'dev', iface, 'set', 'type', 'managed'])
                self.command(['ip', 'link', 'set', iface, 'up'])
                return {'mode': 'ap', 'changed': True}
            except Exception:
                self.restore(before)
                if had_mesh and not self.disabled(iface):
                    old = re.search(r'^\s*frequency=(\d+)\s*$', text(self.wpa / f'wpa_supplicant-{iface}.conf'), re.M)
                    if old:
                        self.start_mesh(iface, int(old[1]))
                raise


def main():
    transition = Transition()
    if sys.argv[1:] == ['mesh']:
        result = transition.to_mesh()
    elif sys.argv[1:] == ['prepare-ap']:
        result = transition.prepare_ap()
    elif sys.argv[1:] == ['ap-power']:
        result = transition.ap_power()
    elif sys.argv[1:] == ['ap-power-post-start']:
        try:
            result = transition.ap_power()
        except Exception as error:
            # The EUD range cap is a preference, not a prerequisite for AP
            # availability. Keep hostapd up; the standalone policy action
            # still fails visibly and reconciliation retries it.
            print(f'WARNING: AP TX power cap failed: {error}', file=sys.stderr)
            result = {'mode': 'ap-power', 'changed': False, 'warning': str(error)}
    else:
        raise ValueError('usage: manet_ap_mesh.py {mesh|prepare-ap|ap-power|ap-power-post-start}')
    if sys.argv[1:] != ['ap-power'] or result.get('changed'):
        print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
