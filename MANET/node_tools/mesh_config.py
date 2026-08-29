"""Split per-node EUD/AP settings from mesh-wide config packages.

Alfred type 70 is for settings every radio must share (mesh SSID, SAE key,
CIDR, services). The EUD AP on a node is that node's own Wi-Fi for clients;
pushing it over Alfred renamed every AP on the mesh.
"""

LOCAL_KEYS = frozenset({
    'eud',
    'lan_ap_ssid',
    'lan_ap_key',
    'max_euds_per_node',
})


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
    for key in LOCAL_KEYS:
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
    try:
        with open(mesh_conf) as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    applied = []
    for key, val in changes.items():
        if key not in LOCAL_KEYS:
            continue
        prefix = f'{key}='
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith(prefix):
                lines[i] = f'{prefix}{val}\n'
                replaced = True
                break
        if not replaced:
            if lines and not lines[-1].endswith('\n'):
                lines[-1] += '\n'
            lines.append(f'{prefix}{val}\n')
        applied.append(key)

    with open(mesh_conf, 'w') as f:
        f.writelines(lines)
    return applied

