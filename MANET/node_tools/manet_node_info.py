"""Local node facts and their web display formats."""

import socket


def get_my_hostname():
    """Read the local hostname, with a display fallback on failure."""
    try:
        return socket.gethostname()
    except Exception:
        return 'unknown'


def _compact_uptime(seconds):
    """Format management durations in the compact day/hour notation."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ''
    if s < 60:
        return f'{s}s'
    m = s // 60
    if m < 60:
        return f'{m}m'
    h = m // 60
    rm = m % 60
    if h < 24:
        return f'{h}h{rm:02d}m'
    d = h // 24
    rh = h % 24
    return f'{d}d{rh:02d}h'


def fmt_uptime(seconds, *, compact=False):
    """Format seconds as hours/minutes or minutes/seconds when numeric."""
    if compact:
        return _compact_uptime(seconds)
    try:
        s = int(float(seconds))
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h > 0:
            return f"{h}h {m}m"
        return f"{m}m {sec}s"
    except Exception:
        return seconds


def get_local_uptime(*, compact=False):
    """Format elapsed boot time from procfs for the local detail panel."""
    try:
        with open('/proc/uptime') as f:
            # procfs gives elapsed seconds first, then aggregate idle time.
            secs = float(f.read().split()[0])
        return fmt_uptime(secs, compact=compact)
    except Exception:
        return ''
