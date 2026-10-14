"""Freshness of locally observed registry records, independent of wall time."""

import math
import re
import time


def observed_age(node):
    """Return seconds since local observation, or None for invalid state."""
    try:
        age = time.clock_gettime(time.CLOCK_BOOTTIME) - float(node['OBSERVED_AT_UPTIME'])
    except (KeyError, TypeError, ValueError):
        return None
    return age if math.isfinite(age) and age >= 0 else None


def node_state(node):
    """Preserve shutdown notices and age other records with local boottime."""
    if node.get('NODE_STATE') == 'SHUTTING_DOWN':
        return 'SHUTTING_DOWN'
    age = observed_age(node)
    # Records remain active through 300 seconds. Missing age is stale.
    return 'ACTIVE' if age is not None and age <= 300 else 'STALE'


def parse_registry(path):
    """Parse /var/run/mesh_node_registry into a dict of node dicts."""
    nodes = {}
    try:
        with open(path) as f:
            content = f.read()
        # Match hex node IDs, uppercase fields and shell-quoted values,
        # including the apostrophe quote breaks emitted by registry writers.
        # Registry shell assignments remain data, never sourced code.
        pattern = re.compile(
            r"^NODE_([A-Fa-f0-9]+)_([A-Z0-9_]+)='((?:[^']|'\\'')*)'$",
            re.MULTILINE)
        for m in pattern.finditer(content):
            nid, field, value = m.groups()
            if nid not in nodes:
                nodes[nid] = {'id': nid}
            value = value.replace("'\\''", "'")
            # A display name can contain spaces, apostrophes and punctuation.
            # Bound its size and reject control characters, never treat it as HTML.
            if field == 'HOSTNAME' and (not value.strip() or len(value) > 253
                                        or not value.isprintable()):
                value = ''
            nodes[nid][field] = value
    except Exception:
        pass
    return nodes
