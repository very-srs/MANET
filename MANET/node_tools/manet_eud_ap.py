"""Node AP naming and reversible local configuration activation."""

from pathlib import Path
import re
import socket
import subprocess

from manet_config_io import atomic_write, rewrite_keys
from mesh_config import apply_local_to_conf, validate_config


def ap_suffix():
    # end0 is the provisioned Ethernet identity, independent of current uplink.
    try:
        mac = Path('/sys/class/net/end0/address').read_text().strip()
        if re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac):
            return mac.replace(':', '')[-4:]
    except FileNotFoundError:
        pass
    match = re.fullmatch(r'mesh-([0-9a-f]{4})', socket.gethostname())
    if match:
        return match[1]
    raise ValueError('Cannot determine the node AP suffix')


def broadcast_ssid(base):
    result = base + '-' + ap_suffix()
    if not 1 <= len(result.encode('utf-8')) <= 32:
        raise ValueError('Broadcast SSID exceeds 32 UTF-8 bytes')
    return result


def configured_ssid(path='/etc/hostapd/hostapd.conf'):
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith('ssid='):
                return line.partition('=')[2]
    except OSError:
        pass
    return ''


def restart_hostapd():
    from manet_ap_mesh import Transition, locked
    with locked(Transition().role_lock):
        # Saving AP credentials while its radio is serving mesh must not reclaim
        # that radio. try-restart also preserves an intervening Ethernet stop.
        running = subprocess.run(['systemctl', 'is-active', '--quiet', 'hostapd.service'],
                                 capture_output=True, timeout=5).returncode == 0
        if not running:
            return
        subprocess.run(['systemctl', 'try-restart', 'hostapd.service'], check=True,
                       capture_output=True, timeout=90)
        subprocess.run(['systemctl', 'is-active', '--quiet', 'hostapd.service'],
                       check=True, capture_output=True, timeout=5)


def apply_local(changes, mesh_conf, hostapd='/etc/hostapd/hostapd.conf',
                ap_role='/var/lib/ap_interface'):
    ok, why = validate_config(changes, local=True)
    if not ok:
        raise ValueError(why)
    conf_path, ap_path = Path(mesh_conf), Path(hostapd)
    before = conf_path.read_bytes()
    radio_change = bool({'lan_ap_ssid', 'lan_ap_key'} & changes.keys())
    enabled = (Path(ap_role).exists() and bool(Path(ap_role).read_text().strip())
               and ap_path.exists())
    ap_before = ap_path.read_bytes() if radio_change and enabled else None
    updated = None
    if ap_before is not None:
        values = {}
        if 'lan_ap_ssid' in changes:
            values['ssid'] = broadcast_ssid(changes['lan_ap_ssid'])
        if 'lan_ap_key' in changes:
            values['wpa_passphrase'] = changes['lan_ap_key']
        updated = rewrite_keys(ap_before.decode('utf-8'), values)
    changed = []
    try:
        changed.append((conf_path, before))
        apply_local_to_conf(changes, mesh_conf)
        if updated is not None:
            changed.append((ap_path, ap_before))
            atomic_write(ap_path, updated)
            restart_hostapd()
    except Exception as error:
        failures = []
        for path, content in reversed(changed):
            try:
                atomic_write(path, content)
            except OSError:
                failures.append(path.name)
        if any(path == ap_path for path, _ in changed):
            try:
                restart_hostapd()
            except (OSError, subprocess.SubprocessError):
                failures.append('hostapd restart')
        detail = '; recovery failed: ' + ', '.join(failures) if failures else '; previous settings restored'
        # CalledProcessError can contain argv; do not include configuration data.
        raise RuntimeError('Cannot activate local AP settings' + detail) from error


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 2 and sys.argv[1] == 'suffix':
        print(ap_suffix())
    elif len(sys.argv) == 3 and sys.argv[1] == 'ssid':
        print(broadcast_ssid(sys.argv[2]))
    elif len(sys.argv) == 2 and sys.argv[1] == 'current':
        print(configured_ssid())
    else:
        sys.exit('usage: manet_eud_ap.py {suffix|ssid BASE|current}')
