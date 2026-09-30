#!/usr/bin/env python3
"""Remove the unused Raspberry Pi OS build/desktop stack from a MANET node."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import platform
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
'''.split())
KEEP = set('''
openssh-server sudo systemd systemd-sysv systemd-resolved networkd-dispatcher
iproute2 iputils-ping iw rfkill wireless-regdb wpasupplicant hostapd dnsmasq
nftables iptables ebtables bridge-utils radvd chrony curl ca-certificates
avahi-daemon libnss-mdns libnss-resolve libnss-myhostname syncthing
gpsd gpsd-tools python3 python3-cryptography python3-protobuf python3-zeroconf
python3-gi gir1.2-gstreamer-1.0 python3-smbus python3-smbus2 python3-spidev
python3-libgpiod python3-rpi-lgpio python3-gpiozero i2c-tools gpiod
pulseaudio pulseaudio-utils alsa-utils alsa-ucm-conf alsa-topology-conf rtkit dbus-user-session
gstreamer1.0-alsa gstreamer1.0-plugins-base gstreamer1.0-plugins-good
libnl-3-200 libnl-genl-3-200 libnl-route-3-200 libcap2 libssl3t64
libavahi-client3 libglib2.0-data libatomic1 libstdc++6
iperf3 tcpdump nmap lshw ethtool pciutils usbutils usb.ids usb-modeswitch netcat-openbsd
screen arping bc jq sqlite3 traceroute net-tools wireless-tools mpg123
initramfs-tools busybox zstd raspi-firmware raspberrypi-sys-mods rpi-eeprom
'''.split())
STATE = Path('/var/lib/manet-os-cleanup')
PROFILE = '1'


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
    print(json.dumps({'remove': removed, 'package_mib': round(size / 1024, 1),
                      'swap_mib': round(Path('/var/swap').stat().st_blocks / 2048)
                      if Path('/var/swap').exists() else 0}, indent=2), flush=True)
    if not apply:
        return
    STATE.mkdir(parents=True, exist_ok=True)
    stamp = str(int(time.time()))
    (STATE / f'{stamp}-packages.json').write_text(json.dumps(installed, indent=2))
    (STATE / f'{stamp}-manual.txt').write_text(command(['apt-mark', 'showmanual']) + '\n')
    (STATE / f'{stamp}-plan.txt').write_text(plan)
    command(['apt-mark', 'manual'] + protected)
    for unit in ('rpi-zram-writeback.timer', 'rpi-zram-writeback.service',
                 'rpi-resize-swap-file.service'):
        if command(['systemctl', 'show', unit, '-p', 'LoadState', '--value']) != 'not-found':
            subprocess.run(['systemctl', 'stop', unit], check=True)
    if targets:
        # Validate again against real APT state before executing the transaction.
        validate_plan(command(apt[:1] + ['--simulate'] + apt[1:]), protected)
        subprocess.run(apt[:1] + ['--yes'] + apt[1:], check=True,
                       env=dict(os.environ, LC_ALL='C', DEBIAN_FRONTEND='noninteractive'))
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    cleanup_swap()
    subprocess.run(['apt-get', 'clean'], check=True)
    if command(['dpkg', '--audit']):
        raise RuntimeError('dpkg audit failed after cleanup')
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
