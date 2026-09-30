"""Split per-node EUD/AP settings from mesh-wide config packages.

Alfred type 70 is for settings every radio must share (mesh SSID, SAE key,
CIDR, services). The EUD AP on a node is that node's own Wi-Fi for clients;
pushing it over Alfred renamed every AP on the mesh.
"""

import ipaddress
from pathlib import Path
import re

from manet_config_io import atomic_write, rewrite_keys

SAFE_KEYS = ('admin_password', 'mtx', 'mumble', 'auto_update')
DANGEROUS_KEYS = ('mesh_ssid', 'mesh_key', 'ipv4_network')
MESH_KEYS = frozenset(SAFE_KEYS + DANGEROUS_KEYS + ('regulatory_domain', 'acs'))

LOCAL_KEYS = frozenset({
    'eud',
    'lan_ap_ssid',
    'lan_ap_key',
    'max_euds_per_node',
})

# max_euds_per_node is stripped from Alfred with the other per-node keys, but
# it is set at flash and must not be rewritten from the management UI.
LOCAL_APPLY_KEYS = LOCAL_KEYS - {'max_euds_per_node'}


def valid_value(key, value):
    if key not in MESH_KEYS | LOCAL_KEYS:
        return False, 'unknown setting'
    if not isinstance(value, str):
        return False, 'not a string'
    if re.search(r'["\'\x00-\x1f\x7f]', value):
        return False, 'contains a quote or control character'
    try:
        length = len(value.encode('utf-8'))
    except UnicodeError:
        return False, 'invalid UTF-8'
    if length > 128 or value != value.strip():
        return False, 'too long or contains leading/trailing whitespace'
    if key in ('mesh_ssid', 'lan_ap_ssid'):
        limit = 27 if key == 'lan_ap_ssid' else 32  # -xxxx node suffix
        if not 1 <= length <= limit:
            return False, f'SSID must be 1-{limit} UTF-8 bytes before the node suffix'
    elif key in ('mesh_key', 'lan_ap_key'):
        if not 8 <= length <= 63:
            return False, 'wireless key must be 8-63 bytes'
        if key == 'lan_ap_key' and not value.isascii():
            return False, 'AP key must be printable ASCII'
    elif key == 'admin_password':
        if not value:
            return False, 'admin password must not be empty'
    elif key == 'ipv4_network':
        try:
            network = ipaddress.IPv4Network(value, strict=True)
            if not 8 <= network.prefixlen <= 28:
                return False, 'network must have room for reserved services and a node allocation'
        except ValueError:
            return False, 'not a canonical IPv4 CIDR block'
    elif key == 'eud' and value not in ('wired', 'wireless', 'auto'):
        return False, 'must be wired, wireless or auto'
    elif key in ('mtx', 'mumble', 'auto_update', 'acs') and value not in ('y', 'n'):
        return False, 'must be y or n'
    elif key == 'regulatory_domain' and not re.fullmatch(r'[A-Z]{2}', value):
        return False, 'must be a 2-letter country code'
    elif key == 'max_euds_per_node' and (not value.isascii() or not value.isdigit() or int(value) > 253):
        return False, 'must be a number 0-253'
    return True, ''


def validate_config(config, current=None, local=False):
    if not isinstance(config, dict) or not config:
        return False, 'No configuration provided'
    allowed = MESH_KEYS | LOCAL_KEYS if local else MESH_KEYS
    for key, value in config.items():
        if key not in allowed:
            return False, f'unknown setting {key!r}'
        ok, why = valid_value(key, value)
        if not ok:
            return False, f'{key}: {why}'
        if key == 'max_euds_per_node' and current is not None and value != current.get(key):
            return False, 'max_euds_per_node is fixed at provisioning'
    if 'ipv4_network' in config:
        network = ipaddress.IPv4Network(config['ipv4_network'])
        try:
            max_euds = int((current or {}).get('max_euds_per_node', '1'))
        except (ValueError, TypeError):
            return False, 'Invalid provisioned allocation size'
        if network.num_addresses - 2 - 5 < max(max_euds, 1) + 2:
            return False, 'IPv4 network is too small for this node allocation and reserved services'
    return True, ''


def submitted_changes(config, current):
    """Keep changed form fields and unknown keys (which validation must reject).

    Existing provisioned values need not pass today's rules to edit an unrelated
    setting. Empty editable fields mean leave the value alone.
    """
    return {key: value for key, value in config.items()
            if key not in MESH_KEYS | LOCAL_KEYS
            or (not (value == '' and key in MESH_KEYS | LOCAL_APPLY_KEYS)
                and value != current.get(key))}


def strip_local_keys(config):
    """Return a copy with per-node keys removed, for Alfred / apply."""
    if not isinstance(config, dict):
        return {}
    return {k: v for k, v in config.items() if k not in LOCAL_KEYS}


def split_config(config):
    """Partition a form payload into (local, mesh) dicts."""
    if not isinstance(config, dict):
        return {}, {}
    local = {k: v for k, v in config.items() if k in LOCAL_KEYS}
    mesh = {k: v for k, v in config.items() if k not in LOCAL_KEYS}
    return local, mesh


def local_changes(config, current):
    """Local keys in `config` whose non-empty value differs from mesh.conf."""
    if not isinstance(config, dict):
        return {}
    current = current or {}
    changed = {}
    for key in LOCAL_APPLY_KEYS:
        val = config.get(key)
        if val is None or val == '':
            continue
        if str(val) != str(current.get(key, '')):
            changed[key] = val
    return changed


def mesh_changes(config, current):
    """Mesh-wide keys in `config` whose non-empty value differs from mesh.conf."""
    if not isinstance(config, dict):
        return {}
    current = current or {}
    changed = {}
    for key, val in strip_local_keys(config).items():
        if val is None or val == '':
            continue
        if str(val) != str(current.get(key, '')):
            changed[key] = val
    return changed


def apply_local_to_conf(changes, mesh_conf):
    """Write per-node keys into mesh.conf. Returns the keys written."""
    if not changes:
        return []
    changes = {k: v for k, v in changes.items() if k in LOCAL_APPLY_KEYS}
    if not changes:
        return []
    ok, why = validate_config(changes, local=True)
    if not ok:
        raise ValueError(why)
    path = Path(mesh_conf)
    text = path.read_text() if path.exists() else ''
    atomic_write(path, rewrite_keys(text, changes))
    return list(changes)
