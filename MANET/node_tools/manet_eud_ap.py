"""Node AP naming and reversible local configuration activation."""

from pathlib import Path
import re
import socket
import subprocess

from manet_config_io import atomic_write, rewrite_keys, load_kv_file
from mesh_config import AP_SSID_FIXED, apply_local_to_conf, validate_config, valid_value

AP_IDENTITY = '/var/lib/manet-ap-ssid'


def ap_suffix():
    """Derive four hex digits from end0, falling back to the mesh hostname."""
    # end0 is the provisioned Ethernet identity, independent of current uplink.
    try:
        mac = Path('/sys/class/net/end0/address').read_text().strip()
        if re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac):
            return mac.replace(':', '')[-4:]
    except FileNotFoundError:
        pass
    # The provisioned hostname preserves the suffix if end0 is absent.
    match = re.fullmatch(r'mesh-([0-9a-f]{4})', socket.gethostname())
    if match:
        return match[1]
    raise ValueError('Cannot determine the node AP suffix')


def broadcast_ssid(base):
    """Append the node suffix and enforce the 32-byte SSID limit."""
    result = base + '-' + ap_suffix()
    if not 1 <= len(result.encode('utf-8')) <= 32:
        raise ValueError('Broadcast SSID exceeds 32 UTF-8 bytes')
    return result


def configured_ssid(path='/etc/hostapd/hostapd.conf'):
    """Read the literal hostapd SSID, returning empty when unavailable."""
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith('ssid='):
                return line.partition('=')[2]
    except OSError:
        pass
    return ''


def setup_ssid(mesh_conf='/etc/mesh.conf', hostapd='/etc/hostapd/hostapd.conf',
               identity=AP_IDENTITY, provisioned='/var/lib/radio-setup.done'):
    """Freeze the flash-time name, or verify it before any setup rerun writes.

    Existing nodes use their hostapd name as the fixed identity. A completed
    node without either record fails closed. New wired-only nodes also save
    the identity, so enabling an AP later cannot choose a different name.
    The setup process lock serializes this with all other setup invocations.
    """
    conf = load_kv_file(mesh_conf)
    base = conf.get('lan_ap_ssid', '')
    record = Path(identity)
    fixed = record.read_text().rstrip('\n') if record.exists() else configured_ssid(hostapd)
    if fixed:
        if not re.fullmatch(re.escape(base) + r'-[0-9a-f]{4}', fixed):
            raise ValueError(AP_SSID_FIXED + '; restore lan_ap_ssid in mesh.conf')
    elif not base and conf.get('eud') == 'wired':
        # Wired flashers deliberately leave the AP name empty. Freeze that
        # choice too, without requiring a wireless interface to be present.
        fixed = ''
    else:
        if record.exists() or Path(provisioned).exists():
            raise ValueError(AP_SSID_FIXED + '; the original AP name is unavailable')
        ok, why = valid_value('lan_ap_ssid', base)
        if not ok:
            raise ValueError('Invalid flash-time AP SSID: ' + why)
        fixed = broadcast_ssid(base)
    if not record.exists():
        atomic_write(record, fixed + '\n')
    return fixed


def display_ssid(base):
    """Show the full configured name, including when the AP is inactive."""
    current = configured_ssid()
    if current:
        return current
    try:
        return Path(AP_IDENTITY).read_text().strip()
    except FileNotFoundError:
        try:
            return broadcast_ssid(base) if base else ''
        except ValueError:
            return ''


def restart_hostapd():
    """Restart only a running AP while serializing radio ownership."""
    from manet_ap_mesh import Transition, locked
    with locked(Transition().role_lock):
        # Saving AP credentials while its radio is serving mesh must not reclaim
        # that radio. try-restart also preserves an intervening Ethernet stop.
        running = subprocess.run(['systemctl', 'is-active', '--quiet', 'hostapd.service'],
                                 capture_output=True, timeout=5).returncode == 0
        if not running:
            return
        # Allow 90 seconds for restart, then verify activity within five.
        subprocess.run(['systemctl', 'try-restart', 'hostapd.service'], check=True,
                       capture_output=True, timeout=90)
        subprocess.run(['systemctl', 'is-active', '--quiet', 'hostapd.service'],
                       check=True, capture_output=True, timeout=5)


def apply_local(changes, mesh_conf, hostapd='/etc/hostapd/hostapd.conf',
                ap_role='/var/lib/ap_interface'):
    """Validate and activate local settings, restoring files on failure."""
    ok, why = validate_config(changes, local=True)
    if not ok:
        raise ValueError(why)
    conf_path, ap_path = Path(mesh_conf), Path(hostapd)
    from manet_wifi_uplink import uplink_owner
    if ('eud' in changes and changes['eud'] != load_kv_file(conf_path).get('eud')
            and uplink_owner(Path(ap_role).parent)):
        raise ValueError('Disconnect the built-in Wi-Fi uplink before changing EUD mode')
    before = conf_path.read_bytes()
    radio_change = 'lan_ap_key' in changes
    enabled = (Path(ap_role).exists() and bool(Path(ap_role).read_text().strip())
               and ap_path.exists())
    ap_before = ap_path.read_bytes() if radio_change and enabled else None
    updated = None
    if ap_before is not None:
        values = {}
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
        # Restore the last attempted write first, including failed writes.
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
        # CalledProcessError may contain argv. Exclude configuration data.
        raise RuntimeError('Cannot activate local AP settings' + detail) from error


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 2 and sys.argv[1] == 'suffix':
        print(ap_suffix())
    elif len(sys.argv) == 3 and sys.argv[1] == 'ssid':
        print(broadcast_ssid(sys.argv[2]))
    elif len(sys.argv) == 2 and sys.argv[1] == 'current':
        print(configured_ssid())
    elif len(sys.argv) == 2 and sys.argv[1] == 'setup-ssid':
        try:
            print(setup_ssid())
        except (OSError, ValueError) as error:
            sys.exit(str(error))
    else:
        sys.exit('usage: manet_eud_ap.py {suffix|ssid BASE|current|setup-ssid}')
