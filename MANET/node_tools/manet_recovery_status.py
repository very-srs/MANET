"""Read local recovery observations without contacting another node."""
import json
import math
import os
from pathlib import Path
import time


def read_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def age(value, now):
    try:
        seconds = now - float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except (TypeError, ValueError):
        return None


def duration(seconds):
    if seconds is None:
        return 'unknown age'
    if seconds < 60:
        return f'{int(seconds)} seconds ago'
    if seconds < 3600:
        return f'{int(seconds // 60)} minutes ago'
    return f'{int(seconds // 3600)} hours ago'


def channels(values):
    if not isinstance(values, dict):
        return 'Not yet known'
    return ', '.join(f'{band} GHz: {freq} MHz' for band, freq in values.items()
                     if band in ('2.4', '5') and type(freq) is int) or 'Not yet known'


def recovery_status(conf, registry, hostname):
    run = Path(os.environ.get('MANET_ACS_RUN_DIR', '/run'))
    time_run = Path(os.environ.get('MANET_TIME_RUN_DIR', '/run'))
    roles = Path(os.environ.get('MANET_IFACE_STATE_DIR', '/var/lib'))
    now, elapsed = time.time(), time.monotonic()
    boot_now = time.clock_gettime(time.CLOCK_BOOTTIME)
    acs = read_json(run / 'manet-acs-status.json')
    observation_age = age(acs.get('monotonic'), elapsed)
    fresh = observation_age is not None and observation_age <= 45
    details = []
    def row(label, value):
        details.append({'label': label, 'value': value})
    enabled = str(conf.get('acs', '')).lower() in ('y', 'yes', 'true', '1')
    row('Channel control', 'Automatic' if enabled else 'Static')
    if not fresh:
        summary = 'Recovery status unavailable' if not acs else 'Recovery status is stale'
        row('Observation', duration(observation_age))
    else:
        reachable = acs.get('reachable')
        if type(reachable) is int and reachable > 0:
            summary = f'Connected to {reachable} mesh node' + ('s' if reachable != 1 else '')
            try:
                halow = set((roles / 'halow_if').read_text().split())
            except OSError:
                halow = set()
            routes = set(acs.get('route_interfaces', []))
            if routes and routes <= halow:
                summary += ' via HaLow'
            row('Mesh', 'All reachable radios share one channel decision')
        elif reachable == 0:
            summary = 'No other mesh nodes currently reachable'
        else:
            summary = 'Mesh reachability is unknown'
        row('Observed Wi-Fi channels', channels(acs.get('current')))
        row('Agreed Wi-Fi channels', channels(acs.get('target')))
        phase = acs.get('phase')
        if acs.get('error'):
            waiting = acs['error'] + '; retrying'
        elif not enabled:
            waiting = 'Using the configured static channels'
        elif acs.get('conflicting'):
            waiting = 'Reconnected groups are reconciling their channel plans'
        elif not acs.get('clock_ready'):
            waiting = 'Waiting for GPS or NTP before timed channel decisions'
        elif phase == 'prepared':
            waiting = f"Collecting channel acknowledgements ({acs.get('votes', 0)}/{acs.get('participants', 0)})"
        elif phase == 'committed':
            left = age(now, acs.get('activate_at', now))
            waiting = 'Channel switch scheduled' + (f' in {int(left)} seconds' if left is not None else '')
        elif acs.get('searching'):
            waiting = 'Looking for the operating mesh channel plan'
        elif acs.get('hold_until', 0) > now:
            waiting = f"Holding channels for stragglers ({int(acs['hold_until'] - now)} seconds remaining)"
        elif phase == 'expired':
            waiting = 'Agreement expired; keeping current channels until another round'
        else:
            waiting = 'Using current channels; waiting for the next shared assessment'
        row('Recovery', waiting)
        if phase in ('prepared', 'committed'):
            row('Proposed Wi-Fi channels', channels(acs.get('pending_channels')))
        if enabled:
            row('Recollection', 'HaLow ready; Wi-Fi tourguide visits suppressed' if acs.get('halow_ready')
                else 'HaLow unavailable; Wi-Fi lobby recovery is available')
    clock = read_json(time_run / 'mesh-time-client.json')
    if (time_run / 'initial_time_synced').exists():
        source = str(clock.get('last_source') or 'source unavailable')
        row('Clock', f"Last verified from {source}, {duration(age(clock.get('last_sync'), elapsed))}")
    else:
        row('Clock', 'Waiting for the first verified GPS or NTP synchronization')
    counts = {'fresh': 0, 'stale': 0, 'unknown': 0}
    for node in registry.values():
        if node.get('HOSTNAME') == hostname:
            continue
        # When this node last saw the peer's record change, on the boot clock
        # (/proc/uptime), so wall-clock corrections cannot age or refresh it.
        # The peer's own timestamp is not a freshness signal.
        try:
            seconds = boot_now - float(node.get('OBSERVED_AT_UPTIME'))
        except (TypeError, ValueError):
            seconds = None
        if seconds is None or not math.isfinite(seconds) or seconds < 0:
            counts['unknown'] += 1
        else:
            counts['fresh' if seconds <= 300 else 'stale'] += 1
    row('Peer metadata', f"{counts['fresh']} fresh, {counts['stale']} stale, {counts['unknown']} age unknown")
    event = read_json(run / 'manet-last-channel-change.json')
    if event:
        row('Last Wi-Fi change', f"{event.get('reason', 'Reason unavailable')}; "
            f"{channels(event.get('channels'))}; {duration(age(event.get('monotonic'), elapsed))}")
    else:
        row('Last Wi-Fi change', 'No recorded automatic switch since boot')
    return {'summary': summary, 'details': details, 'fresh': fresh}


RECOVERY_JS = r"""
function recoveryEscape(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function recoveryPanel(status) {
  if (!status) return '';
  return '<div style="padding:12px;line-height:1.6"><strong>' + recoveryEscape(status.summary) +
    '</strong><dl style="margin:8px 0">' + status.details.map(row =>
      '<dt style="font-weight:600">' + recoveryEscape(row.label) + '</dt><dd style="margin:0 0 6px">' +
      recoveryEscape(row.value) + '</dd>').join('') + '</dl></div>';
}
"""
