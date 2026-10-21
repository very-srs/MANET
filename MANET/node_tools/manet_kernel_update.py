#!/usr/bin/env python3
"""Matched kernel payload validation and default-on CM4 one-shot boot trials."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

PREFIX = 'manet-kernel/'
MANIFEST = PREFIX + 'manifest.json'
PROFILE = 'etc/manet-kernel-trial.json'
HEALTH = 'usr/local/sbin/manet-kernel-health-check'
POLICY = 'etc/manet-kernel-update.conf'
# Install archives record the matched set they flash. Tools archives never ship it.
INSTALLED = 'usr/local/share/manet/installed-kernel.json'
FIELD_HEALTH_WAIT = 60
MAX_DOWNLOAD = 256 * 1024 * 1024
MAX_EXPANDED = 1536 * 1024 * 1024
MANIFEST_LIMIT = 4 * 1024 * 1024


def kernel_enabled(root):
    """A local operator setting, never distributed through mesh configuration."""
    path = root / POLICY
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return True
    enabled = True
    for line in lines:
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        key, separator, value = line.partition('=')
        value = value.strip().lower()
        if key.strip() != 'enabled' or not separator or value not in (
                'y', 'yes', 'true', '1', 'n', 'no', 'false', '0'):
            raise KernelError('Invalid kernel update policy: use enabled=y or enabled=n')
        enabled = value in ('y', 'yes', 'true', '1')
    return enabled


class KernelError(Exception):
    """Reject an unsafe payload or an unconfirmed kernel transaction."""


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(name):
    return (isinstance(name, str) and not name.startswith('/')
            and all(re.fullmatch(r'[A-Za-z0-9_.+-]+', part)
                    and part not in ('.', '..') for part in name.split('/')))


def validate_payload(archive, members, board):
    """Check every kernel input before installation or dependency changes."""
    payload = {name: member for name, member in members.items()
               if name.startswith(PREFIX)}
    if not payload:
        return None
    entry = payload.get(MANIFEST)
    if entry is None or not entry.isfile() or entry.size > MANIFEST_LIMIT:
        raise KernelError('Missing or oversized kernel manifest')
    manifest = json.loads(archive.extractfile(entry).read())
    if (not isinstance(manifest, dict) or manifest.get('schema') != 1
            or manifest.get('board') != board or board not in ('cm4', 'r3a')
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,95}',
                                str(manifest.get('release', '')))):
        raise KernelError('Invalid kernel manifest or board mismatch')
    release = manifest['release']
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        raise KernelError('Empty kernel file inventory')
    actual = {name[len(PREFIX):] for name, member in payload.items()
              if member.isfile() and name != MANIFEST}
    if actual != set(files):
        raise KernelError('Kernel file inventory does not match archive')
    if any(not (member.isfile() or member.isdir()) for member in payload.values()):
        raise KernelError('Kernel payload links are not allowed')
    for name, expected in files.items():
        if (not safe_path(name) or not isinstance(expected, dict)
                or type(expected.get('size')) is not int
                or not re.fullmatch('[a-f0-9]{64}', str(expected.get('sha256', '')))):
            raise KernelError('Invalid kernel file inventory entry')
        if not name.startswith(('boot/', f'modules/{release}/', 'firmware/morse/')):
            raise KernelError('Unexpected kernel payload path: ' + name)
        # The payload cannot change firmware selectors or bootloader code.
        if name.startswith('boot/') and not (
                name in ('boot/kernel8.img', 'boot/Image', 'boot/initrd',
                         'boot/uInitrd', 'boot/overlays/README')
                or name.endswith(('.dtb', '.dtbo'))):
            raise KernelError('Unexpected boot input: ' + name)
    # Hash in physical tar order. Alphabetical manifest order can make a gzip
    # reader rewind and decompress the whole archive for every out-of-order
    # module, exhausting the trial confirmation deadline on real overlays.
    for name in sorted(files, key=lambda name: payload[PREFIX + name].offset_data):
        expected = files[name]
        member = payload[PREFIX + name]
        digest = hashlib.sha256()
        with archive.extractfile(member) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        if member.size != expected['size'] or digest.hexdigest() != expected['sha256']:
            raise KernelError('Kernel payload hash mismatch: ' + name)
    required = ({'boot/kernel8.img', 'boot/bcm2711-rpi-cm4.dtb', 'boot/overlays/README'}
                if board == 'cm4' else {'boot/Image', 'boot/dtb/rockchip/rk3568-rock-3a.dtb'})
    required |= {f'modules/{release}/modules.order', f'modules/{release}/modules.builtin'}
    if (not required <= set(files)
            or not any(name.startswith('firmware/morse/') for name in files)
            or not any(name.startswith(f'modules/{release}/') and
                       re.search(r'\.ko(?:\.(?:xz|zst|gz))?$', name) for name in files)):
        raise KernelError('Incomplete matched kernel set')
    return manifest


class KernelTrial:
    """Journal candidate staging, a single trial, and selector commit.

    Call under the updater lock. The guard is a read-only observer except for
    requesting a normal reboot. No hardware capability is inferred from a model.
    """

    def __init__(self, updater, run, atomic, sync):
        self.u = updater
        self.root = updater.root
        self.run = run
        self.atomic = atomic
        self.sync = sync
        self.state = updater.recovery / 'kernel.json'
        self.boot = updater.destination('boot/firmware')

    def read(self):
        try:
            return json.loads(self.state.read_text())
        except FileNotFoundError:
            # The guard can race successful recovery-directory removal.
            return None

    def save(self, info):
        self.atomic(self.state, data=(json.dumps(info, sort_keys=True) + '\n').encode(), mode=0o600)

    def boot_id(self):
        return (self.root / 'proc/sys/kernel/random/boot_id').read_text().strip()

    def release(self):
        return (self.root / 'proc/sys/kernel/osrelease').read_text().strip()

    def profile(self):
        info = self.read()
        if info and info.get('profile', {}).get('health') == 'field-v1':
            return info['profile']
        path = self.u.destination(PROFILE)
        if not path.exists() and not path.is_symlink():
            if info:
                raise KernelError('Missing bench profile for pending kernel trial')
            base, args = self.base()
            mount = self.run(['findmnt', '--noheadings', '--raw', '--output',
                              'FSTYPE,UUID', '--mountpoint', self.boot], timeout=10).decode().split()
            if len(mount) != 2 or mount[0] != 'vfat':
                raise KernelError('Expected CM4 FAT boot filesystem is not mounted')
            return dict(backend='cm4-tryboot-v1', health='field-v1', boot_uuid=mount[1],
                        config_sha256=hashlib.sha256(base.encode()).hexdigest(),
                        cmdline_sha256=hashlib.sha256(args.encode()).hexdigest(),
                        hardware=self.hardware())
        if not path.is_file() or path.is_symlink():
            raise KernelError('Invalid local kernel trial profile')
        st = path.stat()
        if st.st_uid != 0 or st.st_mode & 0o022 or st.st_size > 8192:
            raise KernelError('Kernel trial profile must be root-owned and not writable by others')
        profile = json.loads(path.read_text())
        if (not isinstance(profile, dict) or profile.get('backend') != 'cm4-tryboot-v1'
                or profile.get('watchdog_validated') is not True
                or not profile.get('validation_id')):
            raise KernelError('Kernel trial watchdog/bootloader qualification is missing')
        for key in ('config_sha256', 'cmdline_sha256', 'health_sha256'):
            if not re.fullmatch('[a-f0-9]{64}', str(profile.get(key, ''))):
                raise KernelError('Invalid kernel trial profile hash: ' + key)
        health = self.u.destination(HEALTH)
        if not health.is_file() or health.is_symlink():
            raise KernelError('Missing local kernel health check')
        st = health.stat()
        if (st.st_uid != 0 or st.st_mode & 0o022 or not st.st_mode & stat.S_IXUSR
                or sha256(health) != profile['health_sha256']):
            raise KernelError('Kernel health check ownership or hash changed')
        return profile

    def mounted(self, profile):
        output = self.run(['findmnt', '--noheadings', '--raw', '--output', 'FSTYPE,UUID',
                           '--mountpoint', self.boot]).decode().strip().split()
        if output != ['vfat', profile.get('boot_uuid')]:
            raise KernelError('Expected CM4 FAT boot filesystem is not mounted')
        for name in ('config.txt', 'cmdline.txt', 'tryboot.txt', 'autoboot.txt'):
            if (self.boot / name).is_symlink():
                raise KernelError('Boot selector must not be a symlink: ' + name)

    def base(self, profile=None):
        """Accept this node's config or the last committed selector."""
        config = self.boot / 'config.txt'
        cmdline = self.boot / 'cmdline.txt'
        previous = self.u.destination('var/lib/manet-kernel/confirmed.json')
        if previous.exists():
            saved = json.loads(previous.read_text())
            if sha256(config) != saved['config_sha256']:
                raise KernelError('Boot config changed after kernel confirmation')
            base = saved['base_config']
            args = saved['base_cmdline']
        else:
            base, args = config.read_text(), cmdline.read_text()
        if profile and (hashlib.sha256(base.encode()).hexdigest() != profile['config_sha256']
                        or hashlib.sha256(args.encode()).hexdigest() != profile['cmdline_sha256']):
            raise KernelError('Boot config or command line differs from validated profile')
        return base, args

    def config(self, base, slot, files):
        """Restrict the supported config dialect; preserve hardware settings."""
        retained = []
        for line in base.splitlines():
            original = line
            line = line.split('#', 1)[0].strip()
            if not line or line.startswith('['):
                retained.append(original)
                continue
            key, _, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if line.split()[0] == 'initramfs' or key in ('initramfs', 'ramfsfile', 'ramfsaddr'):
                raise KernelError('Cannot trial without an initramfs: config.txt names a custom initramfs ('
                                  + line + ')')
            if (line.split()[0] == 'include'
                    or key in ('os_prefix', 'boot_ramdisk',
                               'start_file', 'fixup_file', 'armstub')
                    or (key == 'auto_initramfs' and value not in ('0', '1'))
                    or (key == 'kernel' and value != 'kernel8.img')
                    or (key == 'cmdline' and value != 'cmdline.txt')
                    or (key == 'overlay_prefix' and value != 'overlays/')
                    or (key == 'device_tree' and value != 'bcm2711-rpi-cm4.dtb')):
                raise KernelError('Unsupported boot config directive: ' + line)
            if key == 'dtoverlay':
                overlay = value.split(',', 1)[0]
                if overlay:
                    overlay = overlay.removesuffix('.dtbo')
                    if f'boot/overlays/{overlay}.dtbo' not in files:
                        raise KernelError('Kernel payload lacks configured overlay: ' + overlay)
            # Normalize only the candidate. The original selector, including
            # every conditional auto-initramfs setting, remains the fallback.
            if key != 'auto_initramfs':
                retained.append(original)
        return (f'[all]\nos_prefix=manet/{slot}/\n' + '\n'.join(retained).rstrip() +
                '\n[all]\nkernel=kernel8.img\ndevice_tree=bcm2711-rpi-cm4.dtb\n'
                'cmdline=cmdline.txt\nauto_initramfs=0\nkernel_watchdog_timeout=180\n')

    def initramfs_preflight(self, base, args):
        """Reject evidence of early userspace needed to mount this node's root.

        Loading a stock initramfs does not itself establish a dependency on
        it. Keep the original file and selector for the firmware fallback.
        """
        previous = []
        section = '[all]'
        for line in base.splitlines():
            line = line.split('#', 1)[0].strip()
            if line.startswith('['):
                section = line
            key, _, value = line.partition('=')
            if key.strip() == 'auto_initramfs':
                previous.append(dict(section=section, value=value.strip()))

        def refuse(reason):
            raise KernelError('Cannot trial without an initramfs: ' + reason)

        roots = []
        for label, cmdline in (('saved command line', args),
                               ('running command line', (self.root / 'proc/cmdline').read_text())):
            tokens = cmdline.split()
            for token in tokens:
                if token.startswith(('cryptdevice=', 'cryptopts=', 'rd.luks.', 'luks.')):
                    refuse(label + ' requests encrypted root setup')
                if token.startswith(('rd.lvm.', 'lvm.')):
                    refuse(label + ' requests LVM root setup')
                if token.startswith(('nfsroot=', 'nfsaddrs=', 'netroot=')):
                    refuse(label + ' requests a network root')
                if token.startswith(('rd.md.', 'md=')):
                    refuse(label + ' requests RAID root setup')
                if token.startswith(('dm-mod.create=', 'dm=')):
                    refuse(label + ' requests encrypted or mapped root storage')
                if token.startswith('rootfstype=') and any(
                        fs in ('nfs', 'nfs4', 'cifs', 'overlay', 'tmpfs')
                        for fs in token.split('=', 1)[1].split(',')):
                    refuse(label + ' requests a root filesystem that needs early setup')
            values = [t.split('=', 1)[1] for t in tokens if t.startswith('root=')]
            root = values[-1] if values else ''
            if root.startswith(('/dev/mapper/', '/dev/dm-')):
                refuse(label + ' uses encrypted or mapped root storage (' + root + ')')
            if root.startswith('/dev/md'):
                refuse(label + ' uses RAID root storage (' + root + ')')
            # Linux early_lookup_bdev also accepts partition labels and
            # numeric block devices without any userspace name lookup.
            numeric = re.fullmatch(r'(?:[0-9]+:[0-9]+|[0-9a-fA-F]+)', root)
            if root in ('nfs', 'cifs', '/dev/nfs', '/dev/cifs') or (':' in root and not numeric):
                refuse(label + ' uses a network root (' + root + ')')
            if not (numeric or re.fullmatch(r'PARTLABEL=[^/\s]+', root)
                    or re.fullmatch(r'PARTUUID=[A-Za-z0-9-]+(?:/PARTNROFF=-?\d+)?', root)
                    or (re.fullmatch(r'/dev/[A-Za-z][A-Za-z0-9._-]*', root)
                        and not root.startswith(('/dev/ram', '/dev/loop', '/dev/root')))):
                refuse(label + ' has a root selector the kernel cannot mount directly ('
                       + (root or 'missing root=') + ')')
            roots.append(root)

        mount = self.run(['findmnt', '--noheadings', '--raw', '--output',
                          'SOURCE,FSTYPE,MAJ:MIN', '--mountpoint', self.root], timeout=10).decode().split()
        if len(mount) != 3 or not re.fullmatch(r'\d+:\d+', mount[2]):
            raise KernelError('Cannot inspect the mounted root storage for the kernel trial')
        source, filesystem, device_number = mount
        if source.startswith(('/dev/mapper/', '/dev/dm-')):
            refuse('the mounted root uses encrypted or mapped storage (' + source + ')')
        if source.startswith('/dev/md'):
            refuse('the mounted root uses RAID storage (' + source + ')')
        if filesystem in ('nfs', 'nfs4', 'cifs', 'overlay', 'tmpfs'):
            refuse('the mounted root filesystem is ' + filesystem)
        modules = self.root / 'proc/modules'
        loaded = ({line.split()[0] for line in modules.read_text().splitlines() if line.split()}
                  if modules.exists() else set())
        if filesystem.replace('-', '_') in loaded:
            refuse('the root filesystem ' + filesystem + ' is a loadable module')
        device = (self.root / 'sys/dev/block' / device_number).resolve()
        sysfs = self.root / 'sys'
        if device.is_relative_to(sysfs):
            for parent in (device, *device.parents):
                if parent == sysfs:
                    break
                if (parent / 'dm').exists() or (parent / 'md').exists():
                    refuse('the mounted root uses mapped or RAID storage')
                module = parent / 'driver/module'
                if module.is_symlink():
                    refuse('the root storage driver ' + module.resolve().name + ' is a loadable module')
        return dict(previous_auto_initramfs=previous, saved_root=roots[0], running_root=roots[1],
                    source=source, filesystem=filesystem, device=device_number)

    def running_set(self, directory):
        """Whether the payload is the matched set this node already runs.

        A committed trial names its slot after the manifest digest. A flashed
        node keeps the install archive's record. Either needs the running
        release, so a different set under the same uname -r still fails preflight.
        """
        manifest = json.loads((directory / MANIFEST).read_text())
        if manifest['release'] != self.release():
            return False

        def record(name):
            try:
                return json.loads(self.u.destination(name).read_text())
            except (FileNotFoundError, ValueError):
                return None
        confirmed = record('var/lib/manet-kernel/confirmed.json')
        if isinstance(confirmed, dict) and confirmed.get('release') == manifest['release']:
            return confirmed.get('slot') == sha256(directory / MANIFEST)[:24]
        return record(INSTALLED) == manifest

    def preflight(self, directory):
        manifest = json.loads((directory / MANIFEST).read_text())
        if self.u.board() != 'cm4':
            raise KernelError('Kernel activation is unavailable: Rock 3A bootcount/reset path is unverified')
        profile = self.profile()
        self.mounted(profile)
        info = self.read()
        if info:
            if info['manifest_sha256'] != sha256(directory / MANIFEST):
                raise KernelError('Kernel transaction identity changed')
            return manifest, profile
        if manifest['release'] == self.release():
            raise KernelError('Running kernel ' + manifest['release'] + ' is not this matched set; '
                              'a kernel update must have a new unique uname -r')
        modules = self.u.destination('usr/lib/modules/' + manifest['release'])
        if modules.exists() or modules.is_symlink():
            raise KernelError('Candidate module directory already exists')
        if (self.boot / 'autoboot.txt').exists() or (self.boot / 'tryboot.txt').exists():
            raise KernelError('Existing autoboot/tryboot selector requires explicit commissioning')
        base, args = self.base(profile)
        if len(args.splitlines()) != 1 or any(
                arg.startswith(('manet.kernel_trial=', 'firmware_class.path=')) for arg in args.split()):
            raise KernelError('Unsupported kernel command line')
        self.config(base, 'candidate', manifest['files'])
        self.initramfs_preflight(base, args)
        self.watchdogs()
        sizes = manifest['files']
        self.u.space([(self.boot, sum(v['size'] + 4096 for k, v in sizes.items() if k.startswith('boot/'))),
                      (self.root / 'usr/lib', sum(v['size'] + 4096 for k, v in sizes.items()
                                                 if not k.startswith('boot/')))])
        return manifest, profile

    def watchdogs(self):
        # Option B uses start4.elf's kernel watchdog and PID 1. An EEPROM
        # setting alone cannot prove the reset path.
        # Older CM4 EEPROMs lack a boot watchdog; failure before kernel handoff
        # then needs a power cycle to return to the normal one-shot selector.
        runtime = self.run(['systemctl', 'show', '--property=RuntimeWatchdogUSec', '--value']).strip()
        if not re.fullmatch(rb'(?:[0-9]+(?:\.[0-9]+)?(?:us|ms|s|min|h|d|w)?\s*)+', runtime) or not any(
                float(value) > 0 for value in re.findall(rb'[0-9]+(?:\.[0-9]+)?', runtime)):
            raise KernelError('PID 1 runtime watchdog is not enabled')
        evidence = dict(kernel_timeout_sec=180, runtime=runtime.decode(),
                        eeprom_boot_watchdog='unknown')
        try:
            config = self.run(['vcgencmd', 'bootloader_config'], timeout=10).decode()
        except (OSError, KernelError, subprocess.TimeoutExpired, UnicodeError) as error:
            evidence['eeprom_query_error'] = str(error)
        else:
            value = re.search(r'^BOOT_WATCHDOG_TIMEOUT=(\d+)\s*$', config, re.M)
            evidence['eeprom_boot_watchdog'] = 'enabled' if value and int(value[1]) else 'disabled-or-absent'
            evidence['eeprom_timeout_sec'] = int(value[1]) if value else None
        return evidence

    def target(self, name, info):
        if name.startswith('boot/'):
            relative = 'boot/firmware/manet/' + info['slot'] + '/' + name[5:]
        elif name.startswith('modules/'):
            relative = 'usr/lib/' + name
        else:
            relative = 'var/lib/manet-kernel/' + info['slot'] + '/' + name
        return self.u.destination(relative)

    def hardware(self):
        """Bus enumeration survives a missing driver, netdev or PHY.

        SPI has no discoverable vendor ID. Only an already bound Morse SPI
        device proves a fitted hat; the common overlay also probes empty slots.
        Save that proof before reboot so a failed candidate bind is detected.
        """
        result = []
        for bus, keys in (('pci', ('vendor', 'device')), ('usb', ('idVendor', 'idProduct'))):
            for device in sorted((self.root / 'sys/bus' / bus / 'devices').glob('*')):
                if not (device / keys[0]).exists():
                    continue
                identity = ':'.join((device / key).read_text().strip().lower().removeprefix('0x')
                                    for key in keys)
                if bus == 'pci' and identity in ('14c3:7915', '14c3:7916'):
                    driver, module = 'mt7915e', 'mt7915e'
                    phys = 2 if identity == '14c3:7916' else 1
                elif bus == 'usb' and identity == '325b:8100':
                    driver, module, phys = 'morse_usb', 'morse', 1
                else:
                    continue
                result.append(dict(bus=bus, identity=identity, path=str(device.relative_to(self.root)),
                                   driver=driver, module=module, phys=phys))
        for device in sorted((self.root / 'sys/bus/spi/devices').glob('*')):
            if (device / 'driver').is_symlink() and (device / 'driver').resolve().name == 'morse_spi':
                result.append(dict(bus='spi', identity='morse_spi', path=str(device.relative_to(self.root)),
                                   driver='morse_spi', module='morse', phys=1))
        return result

    def field_health(self, info, profile, manifest):
        """Bound local readiness by elapsed time and the boot commit deadline."""
        uptime = float((self.root / 'proc/uptime').read_text().split()[0])
        if uptime >= 240:
            raise KernelError('Kernel confirmation deadline expired')
        deadline = time.monotonic() + min(FIELD_HEALTH_WAIT, 240 - uptime)

        def command(args):
            return self.run(args, timeout=max(0.001, min(3, deadline - time.monotonic())))

        # Verify the exact payload tree once, including out-of-tree modules.
        # depmod's regenerated indexes are checked by staging, not rehashed.
        for name, item in manifest['files'].items():
            if not name.startswith('modules/') or not re.search(r'\.ko(?:\.(?:xz|zst|gz))?$', name):
                continue
            if sha256(self.target(name, info)) != item['sha256']:
                raise KernelError('Installed candidate module hash mismatch: ' + name)
            if FIELD_HEALTH_WAIT and time.monotonic() >= deadline:
                raise KernelError('Module verification exceeded health deadline')
        while True:
            try:
                self.field_ready(profile, command)
                return
            except (KernelError, OSError, ValueError, subprocess.SubprocessError) as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise KernelError(str(error)) from error
                time.sleep(min(2, remaining))

    def field_ready(self, profile, command):
        active = ('multi-user.target', 'local-fs.target', 'systemd-udevd.service',
                  'systemd-networkd.service', 'node-manager.service', 'mesh-status.service', 'ssh.service')
        for service in active:
            command(['systemctl', 'is-active', '--quiet', service])
        critical = set(active) | {'radio-setup-run-once.service', 'batman-enslave.service',
                                  'alfred.service', 'mesh-clone-identity.service',
                                  'ssh-recovery.service', 'mesh-boot-lobby.service',
                                  'manet-mt7916-firmware.service', 'manet-radio-names.service'}
        failed = command(['systemctl', '--failed', '--no-legend', '--plain', '--no-pager']).decode()
        units = [line.split()[0] for line in failed.splitlines() if line.split()]
        if any(unit in critical or unit.startswith(('wpa_supplicant@', 'wpa_supplicant-s1g-'))
               for unit in units):
            raise KernelError('Critical boot unit failed')
        root_mount = command(['findmnt', '--noheadings', '--raw', '--output', 'FSTYPE,OPTIONS',
                              '--mountpoint', self.root]).decode().split()
        if len(root_mount) != 2 or 'rw' not in root_mount[1].split(','):
            raise KernelError('Root filesystem is not mounted read-write')
        boot_mount = command(['findmnt', '--noheadings', '--raw', '--output', 'FSTYPE,UUID',
                              '--mountpoint', self.boot]).decode().split()
        if boot_mount != ['vfat', profile['boot_uuid']]:
            raise KernelError('Expected CM4 FAT boot filesystem is not mounted')
        runtime = command(['systemctl', 'show', '--property=RuntimeWatchdogUSec', '--value']).strip()
        if not re.fullmatch(rb'(?:[0-9]+(?:\.[0-9]+)?(?:us|ms|s|min|h|d|w)?\s*)+', runtime) or not any(
                float(value) > 0 for value in re.findall(rb'[0-9]+(?:\.[0-9]+)?', runtime)):
            raise KernelError('PID 1 runtime watchdog is not enabled')
        held = {os.readlink(fd) for fd in (self.root / 'proc/1/fd').iterdir() if fd.is_symlink()}
        armed = False
        for device in (self.root / 'sys/class/watchdog').glob('watchdog*'):
            if ('/dev/' + device.name in held or (device.name == 'watchdog0' and '/dev/watchdog' in held)):
                armed |= ((device / 'state').read_text().strip() == 'active'
                          and int((device / 'timeout').read_text()) > 0)
        if not armed:
            raise KernelError('PID 1 does not hold an armed hardware watchdog')
        present = self.hardware()
        identity = lambda item: (item['bus'], item['identity'])
        if Counter(map(identity, profile['hardware'])) - Counter(map(identity, present)):
            raise KernelError('Previously detected radio hardware disappeared')
        phys = [phy.resolve() for phy in (self.root / 'sys/class/ieee80211').glob('*/device')]
        checked = set()
        for radio in present:
            device = self.root / radio['path']
            bindings = [device] if radio['bus'] != 'usb' else list(device.glob('*:*'))
            if not any((path / 'driver').is_symlink() and
                       (path / 'driver').resolve().name == radio['driver'] for path in bindings):
                raise KernelError('Radio driver is not bound: ' + radio['identity'])
            if not (self.root / 'sys/module' / radio['module']).is_dir():
                raise KernelError('Radio module is not loaded: ' + radio['module'])
            device = device.resolve()
            if sum(phy == device or phy.is_relative_to(device) for phy in phys) < radio['phys']:
                raise KernelError('Radio PHY missing: ' + radio['identity'])
            if radio['module'] not in checked:
                version = command(['modinfo', '-k', self.release(), '-F', 'vermagic', radio['module']]).decode().split()
                if not version or version[0] != self.release():
                    raise KernelError('Radio module release does not match running kernel')
                if radio['module'] == 'mt7915e' and command(
                        ['modinfo', '-k', self.release(), '-F', 'manet_timing', 'mt7915e']).strip() != b'1':
                    raise KernelError('mt7915e is missing manet_timing=1')
                checked.add(radio['module'])
        if not int((self.root / 'sys/class/net/bat0/flags').read_text().strip(), 16) & 1:
            raise KernelError('bat0 is not up')

    def make_parent(self, target):
        if not target.is_relative_to(self.boot):
            self.u.make_directory(target.parent)
            return
        missing = []
        directory = target.parent
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        for directory in reversed(missing):
            directory.mkdir()
            self.sync(directory.parent)

    def write(self, target, **kwargs):
        # VFAT has mount-defined permissions and can reject POSIX chmod.
        if target.is_relative_to(self.boot):
            kwargs['mode'] = None
        self.atomic(target, **kwargs)

    def persist_modules(self, release):
        directory = self.u.destination('usr/lib/modules/' + release)
        if not (directory / 'modules.dep').is_file():
            raise KernelError('depmod did not produce modules.dep')
        for path in directory.rglob('*'):
            if path.is_file():
                with path.open('rb') as stream:
                    os.fsync(stream.fileno())
        for path in sorted(directory.rglob('*'), reverse=True):
            if path.is_dir():
                self.sync(path)
        self.sync(directory)

    def prepare(self, directory):
        """Stage completely, then request one trial. Return False until confirmed."""
        manifest, profile = self.preflight(directory)
        info = self.read()
        if info and info['phase'] != 'staging':
            return self.resolve(info, profile, manifest)
        if not info:
            base, args = self.base(profile)
            slot = sha256(directory / MANIFEST)[:24]
            slot_path = self.u.destination('boot/firmware/manet/' + slot)
            firmware = self.u.destination('var/lib/manet-kernel/' + slot)
            if slot_path.exists() or firmware.exists():
                raise KernelError('Candidate slot already exists outside this transaction')
            info = dict(phase='staging', slot=slot, release=manifest['release'],
                        manifest_sha256=sha256(directory / MANIFEST),
                        old_config=(self.boot / 'config.txt').read_text(),
                        base_config=base, base_cmdline=args, origin_boot=self.boot_id(),
                        old_release=self.release(), profile=profile)
            info['trial_config'] = self.config(base, slot, manifest['files'])
            info['initramfs'] = self.initramfs_preflight(info['old_config'], args)
            self.save(info)
        # Only this journal owns these paths; retries never touch running modules.
        if (self.boot / 'config.txt').read_text() != info['old_config']:
            raise KernelError('Boot config changed while staging the candidate')
        if info['release'] == self.release():
            raise KernelError('Refusing to restage modules for the running kernel')
        self.u.space([(self.target(name, info).parent, item['size'] + 4096)
                      for name, item in manifest['files'].items()])
        parents = {self.target(name, info).parent for name in manifest['files']} | {self.boot}
        for parent in parents:
            if parent.is_dir():
                for path in parent.glob('.manet-update-*'):
                    if re.fullmatch(r'\.manet-update-[a-z0-9_]{8}', path.name):
                        if path.is_file() or path.is_symlink():
                            path.unlink()
        for name in sorted(manifest['files']):
            target = self.target(name, info)
            self.make_parent(target)
            self.write(target, source=directory / PREFIX / name)
        self.run(['depmod', '-b', self.root, manifest['release']], timeout=600)
        self.persist_modules(manifest['release'])
        for name, item in manifest['files'].items():
            # depmod regenerates these indexes from the matched module tree.
            if name.startswith('modules/') and Path(name).name.startswith('modules.'):
                continue
            if sha256(self.target(name, info)) != item['sha256']:
                raise KernelError('Installed candidate hash mismatch: ' + name)
        # The kernel pets the firmware-armed watchdog until userspace opens
        # it, forever by default. Bound that, so a trial that never reaches
        # systemd (no root device under rootwait, for one) still resets.
        args = info['base_cmdline'].strip()
        args += (f" manet.kernel_trial={info['slot']} firmware_class.path="
                 f"/var/lib/manet-kernel/{info['slot']}/firmware panic=10"
                 f" watchdog.open_timeout=120\n")
        target = self.u.destination('boot/firmware/manet/' + info['slot'] + '/cmdline.txt')
        self.write(target, data=args.encode())
        # Recheck immediately before arming, including a resumed staging run.
        self.initramfs_preflight(info['old_config'], info['base_cmdline'])
        info['watchdogs'] = self.watchdogs()
        self.write(self.boot / 'tryboot.txt', data=info['trial_config'].encode())
        info['phase'] = 'attempted'
        info['origin_boot'] = self.boot_id()
        self.save(info)
        self.run(['systemctl', 'daemon-reload'])
        self.run(['reboot', '0 tryboot'])
        self.u.log('Kernel trial requested; runtime installation waits for confirmation')
        return False

    def is_trial(self, info):
        args = (self.root / 'proc/cmdline').read_text().split()
        flag = self.root / 'proc/device-tree/chosen/bootloader/tryboot'
        return (self.release() == info['release']
                and 'manet.kernel_trial=' + info['slot'] in args
                and flag.is_file() and flag.read_bytes() == b'\x00\x00\x00\x01')

    def fail(self, info, message, reboot=False):
        info['phase'] = 'failed'
        info['failure'] = message
        self.save(info)
        if reboot:
            self.run(['systemctl', 'reboot'])
        raise KernelError(message + '; kernel transaction retained, no automatic retrial')

    def resolve(self, info, profile, manifest):
        phase = info['phase']
        if phase == 'committed':
            if (self.release() != info['release']
                    or (self.boot / 'config.txt').read_text() != info['trial_config']):
                raise KernelError('Committed kernel or selector is not active')
            return True
        if phase == 'failed':
            raise KernelError(info['failure'] + '; cancel or repair the failed kernel transaction')
        if phase == 'attempted' and self.boot_id() == info['origin_boot']:
            raise KernelError('Kernel trial already requested in this boot; not requesting it twice')
        config = self.boot / 'config.txt'
        if config.read_text() not in (info['old_config'], info['trial_config']):
            self.fail(info, 'Boot selector changed outside the kernel transaction')
        if phase == 'committing':
            # Intent was written only after health checks. The selector may have
            # landed before the last journal write, even if this is a new boot.
            if config.read_text() == info['trial_config']:
                if self.release() != info['release']:
                    raise KernelError('Committed selector requires its matching kernel boot')
                self.finish_commit(info)
                return True
            if not self.is_trial(info):
                self.fail(info, 'Reset before selector commit; old kernel retained')
        elif not self.is_trial(info):
            flag = self.root / 'proc/device-tree/chosen/bootloader/tryboot'
            self.fail(info, 'Trial did not confirm; fallback kernel retained',
                      reboot=(self.release() == info['release']
                              or (flag.is_file() and flag.read_bytes() == b'\x00\x00\x00\x01')))
        try:
            if profile.get('health') == 'field-v1':
                self.field_health(info, profile, manifest)
            else:
                for service in ('node-manager.service', 'mesh-status.service', 'ssh.service'):
                    self.run(['systemctl', 'is-active', '--quiet', service])
                self.run([self.u.destination(HEALTH)], timeout=60)
        except Exception as error:
            self.fail(info, 'Kernel health confirmation failed: ' + str(error), reboot=True)
        # Guard gives late activation 5 minutes. Never commit after its deadline.
        uptime = float((self.root / 'proc/uptime').read_text().split()[0])
        if uptime >= 240:
            self.fail(info, 'Kernel confirmation deadline expired', reboot=True)
        info['phase'] = 'committing'
        self.save(info)
        self.write(config, data=info['trial_config'].encode())
        self.finish_commit(info)
        return True

    def finish_commit(self, info):
        directory = self.u.destination('var/lib/manet-kernel')
        self.u.make_directory(directory)
        confirmed = dict(config_sha256=hashlib.sha256(info['trial_config'].encode()).hexdigest(),
                         base_config=info['base_config'], base_cmdline=info['base_cmdline'],
                         release=info['release'], slot=info['slot'],
                         initramfs=info.get('initramfs'),
                         watchdogs=info.get('watchdogs'))
        self.atomic(directory / 'confirmed.json', data=json.dumps(confirmed).encode(), mode=0o600)
        (self.boot / 'tryboot.txt').unlink(missing_ok=True)
        self.sync(self.boot)
        info['phase'] = 'committed'
        self.save(info)

    def cancel(self):
        """Leave boot/runtime state intact and retain a failed-trial record."""
        info = self.read()
        if not info or info['phase'] in ('committing', 'committed'):
            raise KernelError('No uncommitted kernel trial to cancel')
        if (self.release() != info['old_release'] or self.is_trial(info)
                or (info['phase'] == 'attempted' and self.boot_id() == info['origin_boot'])):
            raise KernelError('Cancel kernel trial only after returning to the original kernel')
        profile = self.profile()
        self.mounted(profile)
        if (self.boot / 'config.txt').read_text() != info['old_config']:
            raise KernelError('Original boot selector is not intact')
        trial = self.boot / 'tryboot.txt'
        if trial.exists():
            if trial.read_text() != info['trial_config']:
                raise KernelError('Unrecognized tryboot selector')
            trial.unlink()
            self.sync(self.boot)
        info['phase'] = 'cancelled'
        self.atomic(self.u.state / 'last-kernel-trial.json',
                    data=json.dumps(info).encode(), mode=0o600)
        self.u.pending.unlink()
        self.sync(self.u.state)
        self.u.log('Kernel trial cancelled; original runtime and retained slots unchanged')

    def guard(self):
        """Bound confirmation even when multi-user startup or finish hangs."""
        flag = self.root / 'proc/device-tree/chosen/bootloader/tryboot'
        if not flag.is_file() or flag.read_bytes() != b'\x00\x00\x00\x01':
            return
        info = self.read()
        if not info or info['phase'] == 'committed':
            return
        while float((self.root / 'proc/uptime').read_text().split()[0]) < 300:
            info = self.read()
            if not info or info['phase'] == 'committed':
                return
            time.sleep(2)
        # Do not acquire the updater lock: the finishing process may be hung.
        self.run(['systemctl', 'reboot'])
        raise KernelError('Unconfirmed kernel exceeded boot deadline')
