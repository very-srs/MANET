"""Freshness of locally observed registry records, independent of wall time."""

import math
import re
import time


def observed_age(node):
    try:
        age = time.clock_gettime(time.CLOCK_BOOTTIME) - float(node['OBSERVED_AT_UPTIME'])
    except (KeyError, TypeError, ValueError):
        return None
    return age if math.isfinite(age) and age >= 0 else None


def node_state(node):
    if node.get('NODE_STATE') == 'SHUTTING_DOWN':
        return 'SHUTTING_DOWN'
    age = observed_age(node)
    return 'ACTIVE' if age is not None and age <= 300 else 'STALE'


def parse_registry(path):
    """Parse /var/run/mesh_node_registry into a dict of node dicts."""
    nodes = {}
    try:
        with open(path) as f:
            content = f.read()
        # Match hex node IDs, uppercase field names, and quoted values.
        # Registry shell assignments remain data, never sourced code.
        pattern = re.compile(r"NODE_([A-Fa-f0-9]+)_([A-Z0-9_]+)='([^']*)'")
        for m in pattern.finditer(content):
            nid, field, value = m.groups()
            if nid not in nodes:
                nodes[nid] = {'id': nid}
            nodes[nid][field] = value
    except Exception:
        pass
    return nodes
