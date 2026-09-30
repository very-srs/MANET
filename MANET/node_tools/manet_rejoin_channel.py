#!/usr/bin/env python3
"""Validate a rejoining radio's channel against the mesh, using the registry.

A radio that spent time as the EUD access point (or was off) comes back into
the mesh on a channel chosen from an authenticated source: the stored static
plan, or a committed ACS destination (else the anchor). The registry then says
whether the rest of the connected mesh is actually using that channel: every
peer publishes its radios' role, state and channel in INTERFACES_JSON.

This module only reports. Telemetry is unauthenticated Alfred data and mesh
membership alone must not authorize a control change, so nothing here picks
or persists a channel. A 'conflict' tells the operator to re-apply the channel
through the authenticated path.
"""

import json
import re

import manet_registry
import manet_static_channels as static_channels


_ENTRY = re.compile(r"^NODE_([0-9A-Fa-f]+)_([A-Z0-9_]+)='((?:[^']|'\\'')*)'$")


def load_registry(path='/var/run/mesh_node_registry'):
    """Registry file -> {node_id: {FIELD: value}}; an unreadable file is empty."""
    nodes = {}
    try:
        with open(path) as source:
            for line in source:
                match = _ENTRY.match(line.rstrip('\n'))
                if match:
                    node, field, value = match.groups()
                    nodes.setdefault(node, {})[field] = value.replace("'\\''", "'")
    except OSError:
        pass
    return nodes


def _integer(value):
    """A plain non-negative decimal integer, else None (no floats, inf or signs)."""
    value = str(value or '').strip()
    return int(value) if re.fullmatch(r'[0-9]{1,6}', value) else None


def _frequency(entry):
    """MHz of one published interface, from freq_mhz or else its channel."""
    freq = _integer(entry.get('freq_mhz'))
    if freq:
        return freq
    channel = _integer(entry.get('channel'))
    if channel is None:
        return None
    if 1 <= channel <= 13:
        return 2407 + 5 * channel
    if 32 <= channel <= 177:
        return 5000 + 5 * channel
    return None


TOURGUIDE_SECONDS = 300


def _touring(node):
    """True if the peer reported a tourguide visit shortly before this record.

    Both timestamps come from the peer's own clock, so their difference is
    meaningful even when that clock is wrong. A touring radio sits on a lobby
    channel, which is not evidence of the mesh's data channel.
    """
    try:
        seen = int(node.get('LAST_SEEN_TIMESTAMP') or 0)
        toured = int(node.get('LAST_TOURGUIDE_TIMESTAMP') or 0)
    except ValueError:
        return False
    return toured > 0 and 0 <= seen - toured <= TOURGUIDE_SECONDS


def peer_frequencies(band, nodes, own_macs):
    """{freq: count of distinct fresh peers with a live mesh radio on it}."""
    own = {mac.strip().lower() for mac in own_macs}
    own_keys = {mac.replace(':', '') for mac in own}
    counts = {}
    for key, node in nodes.items():
        macs = {m.strip().lower() for m in
                (node.get('MAC_ADDRESSES', '') + ',' + node.get('MAC_ADDRESS', '')).split(',')
                if m.strip()}
        # The registry key is the publisher's primary MAC, so our own record is
        # recognized even if its alias fields are missing.
        if macs & own or str(key).lower() in own_keys or manet_registry.node_state(node) != 'ACTIVE' or _touring(node):
            continue
        try:
            interfaces = json.loads(node.get('INTERFACES_JSON') or '[]')
        except ValueError:
            continue
        if not isinstance(interfaces, list):
            continue
        seen = set()
        for entry in interfaces:
            if (not isinstance(entry, dict) or entry.get('role') != 'mesh'
                    or str(entry.get('state', '')).upper() != 'UP'):
                continue
            freq = _frequency(entry)
            if freq is not None and static_channels.valid(band, freq):
                seen.add(freq)
        for freq in seen:
            counts[freq] = counts.get(freq, 0) + 1
    return counts


def check_frequency(band, freq, nodes, own_macs):
    """Validate a planned frequency against fresh peers' live mesh radios.

    Returns (status, peers_on_freq, best_other):
      'confirmed'  at least one peer meshes on freq and none more on another
      'conflict'   more peers mesh on best_other than on freq
      'unknown'    no peer meshes on this band now (nothing to compare)
    """
    counts = peer_frequencies(band, nodes, own_macs)
    if not counts:
        return 'unknown', 0, None
    on_plan = counts.get(freq, 0)
    others = {f: n for f, n in counts.items() if f != freq}
    best_other = min(others, key=lambda f: (-others[f], f)) if others else None
    if best_other is not None and others[best_other] > on_plan:
        return 'conflict', on_plan, best_other
    return ('confirmed' if on_plan else 'unknown'), on_plan, best_other
