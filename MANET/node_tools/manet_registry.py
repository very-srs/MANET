"""Freshness of locally observed registry records, independent of wall time."""

import math
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
