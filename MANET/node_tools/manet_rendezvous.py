#!/usr/bin/env python3
"""Shared clock-derived Wi-Fi rendezvous schedule and explicit discovery mode."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import time

from manet_admin import private_json_write


# Schedule v1. Never prune/reindex these lists based on one node's RF view.
# Unsupported/non-initiating/DFS slots are skipped using the actual PHY limits.
ANCHORS = {'2.4': 2412, '5': 5180}
CHANNELS = {'2.4': (2412, 2437, 2462), '5': (5180, 5220, 5745)}
WINDOW_SECONDS = 120
CYCLE_SECONDS = 2 * WINDOW_SECONDS * 3
# Two visits to even the sole usable entry, plus a complete exchange window.
RECOVERY_SECONDS = 2 * CYCLE_SECONDS + WINDOW_SECONDS
HALOW_HEALTH_SECONDS = 15


def halow_ready():
    """Local S1G mesh is usable for recovery, even without a current peer.

    Role assignment identifies HaLow: Morse can report ordinary Wi-Fi
    frequencies through iw. Probe local readiness, never RF range or peers.
    """
    roles = Path(os.environ.get('MANET_IFACE_STATE_DIR', '/var/lib'))
    sysnet = Path(os.environ.get('MANET_SYS_NET', '/sys/class/net'))
    state = Path(os.environ.get('MANET_RADIO_STATE_FILE', '/var/lib/mesh_radio_state.json'))

    def command(args):
        return subprocess.run(args, capture_output=True, text=True, check=True, timeout=2).stdout

    try:
        names = (roles / 'halow_if').read_text().split()
        desired = json.loads(state.read_text()).get('desired', {}) if state.exists() else {}
        candidates = [name for name in names if re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', name)
                      and desired.get(name) != 'down']
        if not candidates:
            return False
        attached = command([os.environ.get('BATCTL_PATH', '/usr/sbin/batctl'), 'if'])
        active = set(re.findall(r'^\s*([^:\s]+):\s+active\b', attached, re.M))
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        return False
    for iface in candidates:
        try:
            if iface not in active or not int((sysnet / iface / 'flags').read_text().strip(), 16) & 1:
                continue
            command(['systemctl', 'is-active', '--quiet', f'wpa_supplicant-s1g-{iface}.service'])
            info = command(['iw', 'dev', iface, 'info'])
            if not re.search(r'^\s*type mesh point\s*$', info, re.M):
                continue
            # This query succeeds only after joining the local mesh; it does
            # not require stations to be in range (same check as the watchdog).
            joined = command(['iw', 'dev', iface, 'get', 'mesh_param', 'mesh_plink_timeout'])
            if re.fullmatch(r'\d+', joined.strip()):
                return True
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return False


def search_channels(now, synchronized=True):
    if not synchronized:
        return dict(ANCHORS)
    cycle = int(now) // (2 * WINDOW_SECONDS)
    return {band: channels[cycle % len(channels)] for band, channels in CHANNELS.items()}


def slot(now):
    band = '2.4' if int(now) // WINDOW_SECONDS % 2 == 0 else '5'
    return band, search_channels(now)[band]


def permitted_frequencies(info):
    result = set()
    for line in info.splitlines():
        match = re.search(r'\* (\d+)(?:\.\d+)? MHz', line)
        if match and not re.search(r'disabled|no IR|radar detection|passive scan', line):
            result.add(int(match[1]))
    return result


class Discovery:
    """Mode survives daemon restarts; boot/setup reset starts from anchor configs.

    Frequencies seed the state once, before any discovery/tourguide hop. Once
    initialized, a channel used for both discovery and data cannot change mode.
    """
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get('MANET_ACS_RUN_DIR', '/run'))
        self.path = self.directory / 'manet-rendezvous.json'

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / '.rendezvous-state.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def mode(self, configured):
        with self.locked():
            if not self.path.exists():
                if not configured:
                    return 'data'  # No Wi-Fi yet; do not seed mode before role/config discovery.
                mode = 'search' if all(ANCHORS.get(b) == f for b, f in configured.items()) else 'data'
                private_json_write(self.path, {'mode': mode})
            state = json.loads(self.path.read_text())
            if state.get('mode') not in ('search', 'data'):
                raise ValueError('invalid rendezvous mode')
            return state['mode']

    def set_mode(self, mode):
        if mode not in ('search', 'data'):
            raise ValueError('invalid rendezvous mode')
        with self.locked():
            if self.path.exists() and json.loads(self.path.read_text()).get('mode') == mode:
                return
            private_json_write(self.path, {'mode': mode})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('slot', 'state', 'set-mode', 'permitted', 'halow-ready'))
    parser.add_argument('values', nargs='*')
    args = parser.parse_args()
    if args.action == 'halow-ready':
        return 0 if halow_ready() else 1
    elif args.action == 'slot':
        now = int(args.values[0]) if args.values else int(time.time())
        print('|'.join(map(str, slot(now))))
    elif args.action == 'state':
        configured = {b: int(f) for b, f in zip(('2.4', '5'), args.values) if f}
        print('true' if Discovery().mode(configured) == 'search' else 'false')
    elif args.action == 'set-mode':
        Discovery().set_mode(args.values[0])
    else:
        iface, freq = args.values
        info = subprocess.run(['iw', 'dev', iface, 'info'], capture_output=True, text=True, check=True, timeout=2).stdout
        phy = re.search(r'\bwiphy (\d+)', info)
        if not phy:
            return 1
        info = subprocess.run(['iw', 'phy', 'phy' + phy[1], 'info'], capture_output=True, text=True, check=True, timeout=2).stdout
        return 0 if int(freq) in permitted_frequencies(info) else 1
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
        parser_message = 'Rendezvous unavailable: ' + str(exc)
        import sys
        print(parser_message, file=sys.stderr)
        raise SystemExit(1)
