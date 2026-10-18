#!/usr/bin/env python3
"""Fail-closed DFS handoff, independent of ACS/config/admin state and imports."""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile

NON_DFS = {5180, 5200, 5220, 5240, 5745, 5765, 5785, 5805, 5825}


def safety_command(args):
    """Bound local radio/control operations and service shutdown separately."""
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=15 if args[0] == 'systemctl' else 2).stdout


def notify_ready():
    """Notify PID 1 only from the subscribed guardian process."""
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect('\0' + address[1:] if address.startswith('@') else address)
            sock.sendall(b'READY=1')


def safety_write(path, content):
    """Atomically save a private fallback or resume record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def safety_read(path):
    """Treat missing or corrupt private records as absent authority."""
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def morse_halow(root, iface):
    """Recognize Morse HaLow, which reports S1G channels as 5 GHz numbers."""
    try:
        driver = os.readlink(root / f'sys/class/net/{iface}/device/driver')
    except OSError:
        return False  # Unknown hardware stays subject to the DFS fence.
    return os.path.basename(driver).startswith('morse')


def stop_dfs(root=Path('/')):
    """Fence all operating DFS meshes before stopping or replacing protection.

    Save a trusted same-boot fallback for restart. With no fallback, leave
    the supplicant stopped. Never infer radio departure from its config file.
    This helper deliberately needs only the standard library, so an updater
    can run the staged copy before replacing any live Python modules.
    """
    root = Path(root)
    contract = safety_read(root / 'run/manet-dfs-contract.json')
    boot = (root / 'proc/sys/kernel/random/boot_id').read_text().strip().replace('-', '')
    resume_path = root / 'run/manet-dfs-resume.json'
    resume = safety_read(resume_path)
    if resume.get('boot') != boot:
        resume = dict(boot=boot, interfaces=[])
    try:
        mesh5 = (root / 'var/lib/mesh_5_if').read_text().strip()
    except OSError:
        mesh5 = ''
    output = safety_command(['iw', 'dev'])
    for block in re.split(r'(?=\bInterface )', output):
        name = re.match(r'Interface ([A-Za-z0-9_.-]{1,15})\s', block)
        if not name or not re.search(r'\btype mesh point\b', block):
            continue
        iface = name[1]
        if morse_halow(root, iface):
            continue  # A mapped HaLow channel such as 5660 MHz is not radar-bound.
        frequency = re.search(r'channel.*\((\d+) MHz\)', block)
        if frequency and (int(frequency[1]) < 5000 or int(frequency[1]) in NON_DFS):
            continue
        if not frequency and iface not in (mesh5, contract.get('iface')):
            continue  # An idle HaLow/2.4 GHz interface is not our DFS owner.
        try:
            if safety_command(['wpa_cli', '-i', iface, 'raw', 'MANET_DFS_STATUS']).strip() == 'MANET_DFS_V3 owner=0':
                continue  # This BSS retains native evacuation without a guardian.
        except (OSError, subprocess.SubprocessError):
            pass
        # Fence before changing files or stopping services.
        try:
            safety_command(['ip', 'link', 'set', 'dev', iface, 'down'])
        except (OSError, subprocess.SubprocessError):
            pass  # A stopped supplicant is the second independent fence.
        safety_command(['systemctl', 'stop', f'wpa_supplicant@{iface}.service'])
        # Stop can race an already executing supplicant command. Fence again.
        safety_command(['ip', 'link', 'set', 'dev', iface, 'down'])
        plan = contract.get('operating') or contract.get('pending') or {}
        if not isinstance(plan, dict) or not isinstance(plan.get('dfs'), dict):
            continue
        fallback = plan.get('dfs', {}).get('fallback', {}).get('5')
        if (contract.get('boot') == boot and contract.get('iface') == iface and
                type(fallback) is int and fallback in NON_DFS):
            config = root / f'etc/wpa_supplicant/wpa_supplicant-{iface}.conf'
            body, count = re.subn(r'(?m)^(\s*frequency=)\d+\s*$',
                                 lambda m: m[1] + str(fallback), config.read_text())
            if count != 1:
                raise ValueError('ambiguous DFS fallback configuration')
            safety_write(config, body)
            if iface not in resume['interfaces']:
                resume['interfaces'].append(iface)
            safety_write(resume_path, json.dumps(resume))


def resume_dfs(root=Path('/')):
    """Resume only same-boot saved non-DFS configs behind a ready guardian."""
    root = Path(root)
    safety_command(['systemctl', 'is-active', '--quiet', 'manet-dfs-watch.service'])
    path = root / 'run/manet-dfs-resume.json'
    resume = safety_read(path)
    boot = (root / 'proc/sys/kernel/random/boot_id').read_text().strip().replace('-', '')
    if resume.get('boot') == boot:
        for iface in resume.get('interfaces', []):
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', iface):
                raise ValueError('invalid DFS resume interface')
            config = root / f'etc/wpa_supplicant/wpa_supplicant-{iface}.conf'
            match = re.search(r'(?m)^\s*frequency=(\d+)\s*$', config.read_text())
            if not match or int(match[1]) not in NON_DFS:
                raise ValueError('DFS restart requires a non-DFS configuration')
            safety_command(['systemctl', 'start', f'wpa_supplicant@{iface}.service'])
    path.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('stop', 'resume'))
    parser.add_argument('--root', type=Path, default=Path('/'))
    args = parser.parse_args()
    (stop_dfs if args.action == 'stop' else resume_dfs)(args.root)
