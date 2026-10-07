#!/usr/bin/env python3
"""Remove the unused Raspberry Pi OS build/desktop stack from a MANET node."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import subprocess
import tempfile
import time


# Explicit appliance policy: retain networking, field diagnostics, GPS, audio,
# video and firmware. No kernel images, user files or logs are cleanup targets.
REMOVE = set('''
build-essential gcc g++ cpp make dpkg-dev manpages-dev pkg-config pkgconf gdb git
libgps-dev libcap-dev libssl-dev libnl-3-dev libnl-genl-3-dev libnl-route-3-dev
libdbus-1-dev gpsd-clients rpi-swap systemd-zram-generator rpi-loop-utils
rpicam-apps-lite rpicam-apps-core mkvtoolnix cloud-init rpi-update
rpi-keyboard-config rpi-keyboard-fw-update mesa-vulkan-drivers
modemmanager bluez udisks2
pulseaudio pulseaudio-utils rtkit
'''.split())
KEEP = set('''
openssh-server sudo systemd systemd-sysv systemd-resolved networkd-dispatcher
iproute2 iputils-ping iw rfkill wireless-regdb wpasupplicant hostapd dnsmasq
nftables iptables ebtables bridge-utils radvd chrony curl ca-certificates
avahi-daemon libnss-mdns libnss-resolve libnss-myhostname syncthing
gpsd gpsd-tools python3 python3-cryptography python3-protobuf python3-zeroconf
python3-gi gir1.2-gstreamer-1.0 python3-smbus python3-smbus2 python3-spidev
python3-libgpiod python3-rpi-lgpio python3-gpiozero i2c-tools gpiod
alsa-utils alsa-ucm-conf alsa-topology-conf
# dbus-user-session stays installed: purging it removes gstreamer1.0-plugins-good
# (via libsoup3, glib-networking, dconf). Its user units are masked instead.
dbus dbus-user-session dbus-daemon dbus-bin libpam-systemd libnss-systemd polkitd
openssh-client gnupg gpg gpgv gpg-agent dirmngr keyboxd
cron man-db e2fsprogs logrotate console-setup console-setup-linux keyboard-configuration
gstreamer1.0-alsa gstreamer1.0-plugins-base gstreamer1.0-plugins-good
libnl-3-200 libnl-genl-3-200 libnl-route-3-200 libcap2 libssl3t64
libavahi-client3 libglib2.0-data libatomic1 libstdc++6
iperf3 tcpdump nmap lshw ethtool pciutils usbutils usb.ids usb-modeswitch netcat-openbsd
screen arping bc jq sqlite3 traceroute net-tools wireless-tools mpg123
initramfs-tools busybox zstd raspi-firmware raspberrypi-sys-mods rpi-eeprom
'''.split())
STATE = Path('/var/lib/manet-os-cleanup')
PROFILE = '2'

# Exact units only. Never mask wpa_supplicant@.service, user@.service,
# system D-Bus, login/SSH, or socket-activated credential agents.
SYSTEM_UNITS = {
    'rtkit-daemon.service': 'no PulseAudio consumer; voice uses ALSA directly',
    'man-db.timer': 'no scheduled manual-page indexing needed; man/mandb retained',
    'dpkg-db-backup.timer': 'optional periodic duplicate of dpkg metadata',
    'wpa_supplicant.service': 'unused global D-Bus instance; per-radio instances retained',
    'keyboard-setup.service': 'headless appliance; console tools retained for recovery',
    'console-setup.service': 'headless appliance; console tools retained for recovery',
}
USER_UNITS = {
    'pulseaudio.socket': 'voice uses alsasrc/alsasink; no PulseAudio prompts',
    'pulseaudio.service': 'voice uses alsasrc/alsasink; no PulseAudio prompts',
    'dbus.socket': 'no MANET user-bus consumers; system D-Bus and user managers retained',
    'dbus.service': 'no MANET user-bus consumers; system D-Bus and user managers retained',
}


def cron_consumers(root):
    """Do not disable an operator's cron work when upgrading profile 1.

    Stock run-parts scheduling is redundant only when every executable job
    explicitly exits under systemd. Unknown jobs/tables conservatively keep cron.
    """
    consumers = []
    spool = root / 'var/spool/cron/crontabs'
    consumers.extend(str(p) for p in spool.glob('*') if p.is_file() and p.read_text().strip())
    table = root / 'etc/crontab'
    if table.exists():
        for line in table.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or re.match(r'\w+=', line):
                continue
            # The distribution's four run-parts entries, with optional anacron.
            if not re.fullmatch(r'[\d*/\s,\-]+\s+root\s+(?:test -x /usr/sbin/anacron \|\| )?'
                                r'\(?\s*cd / && run-parts --report /etc/cron\.'
                                r'(?:hourly|daily|weekly|monthly)\s*\)?', line):
                consumers.append(str(table))
                break
    for directory in ('cron.d', 'cron.hourly', 'cron.daily', 'cron.weekly', 'cron.monthly'):
        for path in (root / 'etc' / directory).glob('*'):
            # run-parts/cron ignore dot files, backup extensions and subdirs.
            if not re.fullmatch(r'[A-Za-z0-9_-]+', path.name) or not path.is_file():
                continue
            if directory != 'cron.d' and not os.access(path, os.X_OK):
                continue
            body = '\n'.join(line for line in path.read_text().splitlines()
                             if line.strip() and not line.lstrip().startswith('#'))
            if not body:
                continue
            # Recognize only distribution jobs and their systemd guards. A new
            # package or locally scheduled command keeps cron for manual review.
            known = path.name in {'apt-compat', 'dpkg', 'logrotate', 'man-db', 'e2scrub_all'}
            # Nothing except shell options may run before the early exit.
            guarded = re.match(r'(?:set -[a-z]+\s+)*if \[ -d /run/systemd/system \]; then\s*exit 0\s*fi', body)
            if directory == 'cron.d':
                guarded = all(re.match(r'\w+=', line.strip()) or
                              re.match(r'[\d*/\s,\-]+\s+root\s+(?:'
                                       r'\[ ! -d /run/systemd/system \] &&|'
                                       r'test -e /run/systemd/system \|\|) ', line)
                              for line in body.splitlines())
            if not known or not guarded:
                consumers.append(str(path))
    return sorted(set(consumers))


def runtime_policy(root=Path('/')):
    system, keep = dict(SYSTEM_UNITS), {}
    jobs = cron_consumers(root)
    if jobs:
        keep['cron.service'] = 'cron consumers require review: ' + ', '.join(jobs)
    else:
        system['cron.service'] = 'no cron consumers beyond jobs that exit under systemd'
    # lvm2 also covers inactive/offline operator volumes, not just mounted ones.
    lvm = (root / 'usr/sbin/lvm').exists() or any(
        p.read_text().startswith('LVM-') for p in (root / 'sys/class/block').glob('*/dm/uuid'))
    for unit in ('e2scrub_all.timer', 'e2scrub_reap.service'):
        if lvm:
            keep[unit] = 'LVM tools/volumes present; retain filesystem maintenance'
        else:
            system[unit] = 'no LVM volumes or tools; raw ext4 uses normal fsck'
    return {'mask_system': system, 'mask_user_global': dict(USER_UNITS), 'keep': keep}


def live_user_managers(root=Path('/run/user')):
    return [pwd.getpwuid(int(p.name)).pw_name for p in root.glob('[0-9]*')
            if p.name.isdecimal() and (p / 'systemd/private').exists()]


def unit_inventory(prefix, names):
    return command(prefix + ['show', *names, '-p', 'Id', '-p', 'LoadState',
                             '-p', 'ActiveState', '-p', 'UnitFileState'])


def user_systemctl(user):
    # Talk directly to systemd's private socket as its owner. This still works
    # after removing the user's D-Bus service; never terminate login sessions.
    uid = pwd.getpwnam(user).pw_uid
    return ['runuser', '-u', user, '--', 'env', f'XDG_RUNTIME_DIR=/run/user/{uid}',
            'systemctl', '--user']


def apply_runtime(policy, users, record):
    def execute(args):
        # Append before execution so interrupted/failed runs remain reviewable.
        with record.open('a') as log:
            log.write(json.dumps(args) + '\n')
        command(args)

    for unit in policy['mask_system']:
        state = command(['systemctl', 'show', unit, '-p', 'LoadState', '--value'])
        if state != 'not-found':
            execute(['systemctl', 'disable', '--now', unit])
        execute(['systemctl', 'mask', unit])
    names = list(policy['mask_user_global'])
    # --global does not stop/reload already running per-user managers.
    available = {line.split()[0] for line in command(
        ['systemctl', '--global', 'list-unit-files', '--no-legend', '--no-pager']).splitlines() if line.strip()}
    existing = [name for name in names if name in available]
    if existing:
        execute(['systemctl', '--global', 'disable', *existing])
    execute(['systemctl', '--global', 'mask', *names])
    for user in users:
        prefix = user_systemctl(user)
        execute(prefix + ['daemon-reload'])
        # Stop PulseAudio before stopping its session bus. Individual absent
        # units are skipped, so this is safe after an interrupted purge too.
        for unit in names:
            if command(prefix + ['show', unit, '-p', 'LoadState', '--value']) != 'not-found':
                execute(prefix + ['stop', unit])
    # Package removal follows; system dbus.service/socket are never targeted.


def command(args, **kwargs):
    # APT plan tags must have a stable language for validation.
    kwargs.setdefault('env', dict(os.environ, LC_ALL='C'))
    return subprocess.check_output(args, text=True, **kwargs).strip()


def package_inventory():
    fmt = '${binary:Package}\t${db:Status-Status}\t${Installed-Size}\t${Essential}\t${Version}\n'
    result = {}
    for line in command(['dpkg-query', '-W', '-f', fmt]).splitlines():
        name, status, size, essential, version = line.split('\t')
        if status == 'installed':
            result[name] = {'size_kib': int(size or 0), 'essential': essential == 'yes',
                            'version': version}
    return result


def choose_packages(installed):
    targets, protected = set(), set()
    for name, info in installed.items():
        base = name.split(':')[0]
        if base in REMOVE or base.startswith(('linux-headers-', 'linux-kbuild-')):
            targets.add(name)
        if (base in KEEP or info['essential'] or
                base.startswith(('firmware-', 'bluez-firmware', 'linux-image-', 'linux-base'))):
            protected.add(name)
    if targets & protected:
        raise RuntimeError('Cleanup policy overlaps runtime packages')
    if any(n.split(':')[0] == 'gpsd-clients' for n in targets):
        if not any(n.split(':')[0] == 'gpsd-tools' for n in protected):
            raise RuntimeError('Install gpsd-tools before removing gpsd-clients')
    return sorted(targets), sorted(protected)


def validate_plan(output, protected):
    removed = []
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[0] in ('Inst', 'Conf'):
            raise RuntimeError('Cleanup would install or upgrade packages: ' + line)
        if fields and fields[0] in ('Remv', 'Purg'):
            removed.append(fields[1])
    blocked = {n.split(':')[0] for n in protected} & {n.split(':')[0] for n in removed}
    if blocked:
        raise RuntimeError('Cleanup would remove protected packages: ' + ', '.join(sorted(blocked)))
    return removed


def swap_is_unused(path, swaps, loops):
    # Do not unlink a swap file or a backing file still referenced by the kernel.
    return (path.is_file() and not path.is_symlink() and
            not swaps.splitlines()[1:] and not loops.strip())


def cleanup_swap():
    path = Path('/var/swap')
    if not path.exists():
        return
    loops = command(['losetup', '-j', str(path)])
    if not swap_is_unused(path, Path('/proc/swaps').read_text(), loops):
        raise RuntimeError('Swap is still active or attached; leaving /var/swap intact')
    if command(['blkid', '-p', '-s', 'TYPE', '-o', 'value', str(path)]) != 'swap':
        raise RuntimeError('/var/swap is not a recognized swap file; leaving it intact')
    path.unlink()


def run(apply=False, force=False):
    model = Path('/proc/device-tree/model').read_text().rstrip('\0')
    os_release = Path('/etc/os-release').read_text()
    if 'Raspberry Pi' not in model or 'VERSION_CODENAME=trixie' not in os_release:
        print('OS cleanup applies only to Raspberry Pi OS Trixie; skipped')
        return
    if not platform.release().endswith('-manet'):
        raise RuntimeError('Boot the MANET kernel before cleaning up the OS')
    marker = STATE / 'complete'
    if apply and not force and marker.exists() and marker.read_text().strip() == PROFILE:
        print('OS cleanup already completed; operator additions are preserved')
        return
    if Path('/proc/swaps').read_text().splitlines()[1:]:
        raise RuntimeError('Active swap must be disabled before OS cleanup')
    if command(['dpkg', '--audit']):
        raise RuntimeError('Resolve the dpkg audit before OS cleanup')
    installed = package_inventory()
    targets, protected = choose_packages(installed)
    protected += command(['apt-mark', 'showhold']).splitlines()
    policy = runtime_policy()
    users = live_user_managers()
    units_before = {
        'enabled': command(['systemctl', 'list-unit-files', '--state=enabled', '--no-legend', '--no-pager']),
        'system': unit_inventory(['systemctl'], list(policy['mask_system'])),
        'user_global': command(['systemctl', '--global', 'list-unit-files', '--no-legend', '--no-pager']),
        'users': {user: unit_inventory(user_systemctl(user), list(USER_UNITS)) for user in users},
    }
    apt = ['apt-get', '-o', 'DPkg::Lock::Timeout=120', '-o',
           'APT::AutoRemove::RecommendsImportant=false', '--autoremove', 'purge'] + targets
    if not targets:
        # An interrupted run may have removed packages but not its swap/cache files.
        plan, removed = '', []
    else:
        # Preview with runtime roots pinned in a private copy of APT's state.
        # Even --dry-run must not change the live auto/manual package selection.
        with tempfile.TemporaryDirectory(prefix='manet-cleanup-') as scratch:
            state = Path(scratch) / 'extended_states'
            source = Path('/var/lib/apt/extended_states')
            if source.exists():
                shutil.copyfile(source, state)
            else:
                state.touch()
            option = ['-o', f'Dir::State::extended_states={state}']
            command(['apt-mark'] + option + ['manual'] + protected)
            plan = command(apt[:1] + option + ['--simulate'] + apt[1:])
            removed = validate_plan(plan, protected)
    size = sum(installed[n]['size_kib'] for n in installed
               if n.split(':')[0] in {p.split(':')[0] for p in removed})
    print(json.dumps({'profile': PROFILE, 'runtime': policy, 'units_before': units_before,
                      'remove': removed, 'package_mib': round(size / 1024, 1),
                      'swap_mib': round(Path('/var/swap').stat().st_blocks / 2048)
                      if Path('/var/swap').exists() else 0}, indent=2), flush=True)
    if not apply:
        return
    STATE.mkdir(parents=True, exist_ok=True)
    stamp = str(time.time_ns())
    (STATE / f'{stamp}-packages.json').write_text(json.dumps(installed, indent=2))
    (STATE / f'{stamp}-manual.txt').write_text(command(['apt-mark', 'showmanual']) + '\n')
    (STATE / f'{stamp}-plan.txt').write_text(plan)
    (STATE / f'{stamp}-runtime.json').write_text(json.dumps(
        {'profile': PROFILE, 'policy': policy, 'before': units_before}, indent=2))
    command(['apt-mark', 'manual'] + protected)
    for unit in ('rpi-zram-writeback.timer', 'rpi-zram-writeback.service',
                 'rpi-resize-swap-file.service'):
        if command(['systemctl', 'show', unit, '-p', 'LoadState', '--value']) != 'not-found':
            subprocess.run(['systemctl', 'stop', unit], check=True)
    if targets:
        # Validate again against real APT state before executing the transaction.
        validate_plan(command(apt[:1] + ['--simulate'] + apt[1:]), protected)
    apply_runtime(policy, users, STATE / f'{stamp}-actions.jsonl')
    if targets:
        subprocess.run(apt[:1] + ['--yes'] + apt[1:], check=True,
                       env=dict(os.environ, LC_ALL='C', DEBIAN_FRONTEND='noninteractive'))
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    cleanup_swap()
    subprocess.run(['apt-get', 'clean'], check=True)
    if command(['dpkg', '--audit']):
        raise RuntimeError('dpkg audit failed after cleanup')
    (STATE / f'{stamp}-units-after.txt').write_text(unit_inventory(
        ['systemctl'], list(policy['mask_system'])))
    marker.write_text(PROFILE + '\n')
    print('OS cleanup complete', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='apply the plan; default is dry-run')
    parser.add_argument('--force', action='store_true', help='reapply after a previous successful cleanup')
    args = parser.parse_args()
    with open('/run/lock/manet-os-cleanup.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        run(args.apply, args.force)
