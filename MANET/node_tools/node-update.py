#!/usr/bin/env python3
"""Verify and stage tools updates before modifying a running node.

Individual files are replaced atomically. Interrupted installations resume
from a verified local archive, using a saved copy of this updater.
"""

import argparse
from collections import defaultdict
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import syslog
import tarfile
import tempfile
import time
import zlib

from manet_release import asset_url, select_release, verify_asset
from manet_kernel_update import (KernelError, KernelTrial, MANIFEST as KERNEL_MANIFEST,
                                 PREFIX as KERNEL_PREFIX, MAX_DOWNLOAD, MAX_EXPANDED,
                                 POLICY as KERNEL_POLICY, kernel_enabled, validate_payload)


MIB = 1024 * 1024
# Bound compressed and expanded bytes, retaining 128 MiB free per filesystem.
RESERVE = 128 * MIB
RECOVERY_UNITS = {
    'manet-update-recover.service': 'sysinit.target.wants',
    'manet-update-finish.service': 'multi-user.target.wants',
    'manet-kernel-trial-guard.service': 'multi-user.target.wants',
}
# Resident node_tools processes from MANET/systemd. Manager PartOf children
# restart below with node-manager. Boot/setup oneshots must not be replayed.
RUNTIME_SERVICES = (
    'gps-reader.service', 'manet-positioning.service', 'manet-atak.service',
    'mesh-voice.service', 'button-monitor.service', 'sae-watchdog.service',
    'batman-enslave-watch.service', 'led-boot.service', 'manet-wifi-uplink.service',
)
MARKERS = {"etc/manet_version.txt", "usr/local/bin/version.txt"}
REQUIRED = MARKERS | {
    "etc/apt/preferences.d/00-manet-kernel",
    "usr/local/bin/manet-kernel-protect.py",
    "usr/local/bin/manet-mesh-supplicant.py",
    "etc/systemd/system/wpa_supplicant@.service.d/20-manet-binary.conf",
    "usr/local/bin/node-update.sh", "usr/local/bin/node-update.py",
    "usr/local/bin/manet_release.py",
    "usr/local/bin/manet_kernel_update.py",
    "usr/local/bin/manet-auto-update.sh",
    "etc/systemd/system/manet-auto-update.service",
    "etc/systemd/system/manet-auto-update.path",
    "etc/systemd/system/manet-auto-update.timer",
    "etc/systemd/system/manet-auto-update-clock.service",
    "usr/local/bin/node-manager-static.sh", "usr/local/bin/node-manager-acs.sh",
    "usr/local/bin/manet-admin-setup.sh", "usr/local/bin/mesh-status.py",
    "usr/local/bin/manet_manage.py", "usr/local/bin/manet_web_sessions.py",
    "usr/local/bin/manet_web_limits.py", "usr/local/bin/manet_recovery_status.py",
    "usr/local/bin/manet-provision-status.sh", "usr/local/bin/manet-power-status.sh",
    "usr/local/bin/prepare-ap-iface.sh", "usr/local/bin/manet-wait-radios.py",
    "usr/local/bin/manet-dns-setup.sh",
    "usr/local/bin/manet-uplink-dns.py",
    "etc/systemd/system/manet-uplink-dns.service",
    "etc/systemd/system/manet-uplink-dns.path",
    "usr/local/bin/manet_node_ipv4.py",
    "usr/local/bin/manet-radio-names.py",
    "usr/local/bin/manet-os-cleanup.py",
    "etc/systemd/system/manet-radio-names.service",
    "etc/systemd/system/hostapd.service.d/10-manet-prepare.conf",
    "etc/systemd/system/dnsmasq.service.d/10-manet-resolved.conf",
    "etc/systemd/system/manet-admin-setup.service",
    "usr/local/bin/mesh-channel-agreement.py", "usr/local/bin/manet_acs_agreement.py",
    "usr/local/bin/mesh-acs-common.sh", "usr/local/bin/manet_admin.py",
    "usr/local/bin/manet_rendezvous.py",
    "etc/systemd/system/mesh-channel-agreement.service",
    "etc/systemd/system/node-manager.service.d/acs-agreement.conf",
    "usr/local/bin/mesh-time-sync.py", "usr/local/bin/one-shot-time-sync.sh",
    "etc/systemd/system/one-shot-time-sync.service",
    "etc/systemd/system/node-manager.service.d/time-sync.conf",
    "etc/systemd/system/node-manager.service.d/select.conf",
    "usr/local/bin/node-manager-select.sh",
    "usr/local/bin/manet-dhcp-isolation.py",
    "usr/local/share/manet/dhcp-isolation.nft",
    "etc/systemd/system/manet-dhcp-isolation.service",
    "etc/systemd/system/dnsmasq.service.d/20-manet-dhcp-isolation.conf",
    "usr/local/bin/manet_ap_mesh.py",
    "usr/local/bin/manet_wifi_uplink.py",
    "usr/local/bin/manet_wifi_events.py",
    "etc/systemd/system/manet-wifi-uplink.service",
    "etc/systemd/system/manet-wifi-uplink.path",
    "etc/systemd/system/manet-wifi-client@.service",
    "etc/systemd/system/hostapd.service.d/30-wifi-uplink-events.conf",
    "etc/systemd/system/ap-txpower.service",
    "etc/systemd/system/manet-mesh-power.service",
    "usr/local/bin/manet-mesh-power.sh",
    "etc/systemd/system/manet-cpu-policy.service",
    "usr/local/bin/manet-cpu-policy.sh",
    "usr/local/bin/manet-uplink-speed.sh",
    "usr/local/bin/manet-region.py",
    "usr/local/bin/mesh-service-election.py",
    "usr/local/bin/manet-common.sh",
    "usr/local/bin/mesh-peer-count.sh",
    "usr/local/bin/manet-mt7916-firmware.py",
    "usr/local/bin/manet-mt7916-setup.sh",
    "usr/local/share/manet/mt7916-firmware.json",
    "usr/local/share/manet/firmware/mt7916/mt7916_wm.bin",
    "usr/local/share/manet/firmware/mt7916/mt7916_wa.bin",
    "usr/local/share/manet/firmware/mt7916/mt7916_rom_patch.bin",
    "usr/local/share/manet/firmware/mt7916/LICENCE.mediatek",
    "etc/systemd/system/manet-mt7916-firmware.service",
    "etc/systemd/system/manet-mt7916-firmware-survived.service",
    "etc/systemd/system/manet-mt7916-firmware-apply.service",
    "etc/systemd/system/manet-mt7916-firmware.path",
    "usr/local/bin/manet_measurement_storage.py",
    "usr/local/bin/mesh-hosts-update.sh", "usr/local/bin/manet_hosts.py",
    "usr/local/bin/manet_config_io.py",
    "usr/local/bin/manet_config_journal.py",
    "usr/local/bin/mesh-config-sync.py", "usr/local/bin/mesh-config-apply.sh",
    "usr/local/bin/mesh_config.py", "usr/local/bin/manet_admin_limits.py",
    "usr/local/bin/manet_supplicant.py",
    "usr/local/share/manet/logrotate.conf",
    "etc/systemd/system/manet-logrotate.service",
    "etc/systemd/system/manet-logrotate.timer",
    *("etc/systemd/system/" + name for name in RECOVERY_UNITS),
}
# Match at least two decimal components without a tag prefix or suffix.
VERSION_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)+")


class UpdateError(KernelError):
    """Report a rejected update or a failed installation command."""
    pass


def version(text):
    """Validate and return the dotted version on the first line."""
    first = text.splitlines()[0] if text else ""
    if not VERSION_PATTERN.fullmatch(first):
        raise UpdateError("Invalid release version")
    return first


def stop_process(process):
    """Terminate a command group, escalating to SIGKILL after five seconds."""
    # Stop the complete command, including apt/dpkg children, before giving up.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()


def run_command(args, timeout=120, env=None):
    """Run a bounded command and stop its process group on interruption."""
    # A new session gives timeout cleanup its own process group to terminate.
    with subprocess.Popen([str(arg) for arg in args], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=True,
                          env=env) as process:
        try:
            output, _ = process.communicate(timeout=timeout)
        except BaseException:
            stop_process(process)
            raise
        if process.returncode:
            detail = output.decode("utf-8", errors="replace")[-2000:].strip()
            raise UpdateError(f"{Path(args[0]).name} failed ({process.returncode}): {detail}")
        return output


def sync_directory(path):
    """Persist directory entry changes with fsync."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_file(destination, source=None, data=None, mode=0o644):
    """Flush and atomically replace a file with explicit permissions."""
    fd, scratch = tempfile.mkstemp(prefix=".manet-update-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            if source is not None:
                with source.open("rb") as incoming:
                    shutil.copyfileobj(incoming, output)
            else:
                output.write(data)
            if mode is not None:  # FAT permissions come from the mount options.
                os.fchmod(output.fileno(), mode)
            output.flush()
            os.fsync(output.fileno())
        # A sibling temporary keeps replacement atomic on this filesystem.
        os.replace(scratch, destination)
        sync_directory(destination.parent)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)


def archive_path(name):
    """Validate a root-relative update member without limiting payload scope."""
    # GNU tar builders prefix members with ./. No other normalization is safe.
    while name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/")
    parts = name.split("/")
    if (not name or any(part in ("", ".", "..") for part in parts)
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise UpdateError(f"Unsafe archive path: {name!r}")
    return name


class Updater:
    """Stage verified tools and retain offline recovery until activation."""
    def __init__(self, root=Path("/"), routine=False, development=False, allow_downgrade=False):
        """Set the target root, release policy and transaction paths."""
        self.root = Path(root)
        self.routine = routine
        self.development = development
        self.allow_downgrade = allow_downgrade
        self.marker = self.root / "etc/manet_version.txt"
        self.state = self.root / "var/lib/manet-update"
        self.pending = self.state / "in-progress"
        self.recovery = self.state / "recovery"

    def log(self, message, error=False):
        """Send messages to syslog and also print outside routine mode."""
        syslog.syslog(syslog.LOG_ERR if error else syslog.LOG_INFO, message)
        if not self.routine:
            print(message, file=sys.stderr if error else sys.stdout, flush=True)

    def destination(self, name):
        """Resolve a payload destination without traversing symlink parents."""
        target = self.root / name
        for parent in target.parents:
            if parent == self.root:
                break
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise UpdateError(f"Unsafe installed parent: {parent}")
        return target

    def make_directory(self, target, mode=0o755):
        """Create missing parents with explicit modes and durable entries."""
        # Explicit modes also cover implicit archive parents under a permissive
        # inherited umask. Existing directories keep their original modes.
        missing = []
        current = target
        while current != self.root and not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=mode if directory == target else 0o755)
            directory.chmod(mode if directory == target else 0o755)
            sync_directory(directory.parent)

    def space(self, requests):
        """Account for staging and installation together on each filesystem."""
        totals = defaultdict(int)
        representatives = {}
        for path, size in requests:
            while not path.exists():
                path = path.parent
            # Staging and destination paths can share free space on one device.
            device = path.stat().st_dev
            totals[device] += size
            representatives[device] = path
        for device, size in totals.items():
            path = representatives[device]
            if shutil.disk_usage(path).free < size + RESERVE:
                raise UpdateError(f"Insufficient disk space on {path}: need {size + RESERVE} bytes free")

    def download(self, url, target, limit):
        """Reserve space and fetch a bounded HTTPS asset with retries."""
        run = Path(os.environ.get('MANET_TIME_RUN_DIR', self.root / 'run'))
        if not (run / 'initial_time_synced').is_file():
            raise UpdateError('Clock not synced; wait for mesh time sync before downloading updates')
        self.space([(target.parent, limit)])
        # Bound each attempt to 120 s and the entire curl process to 260 s.
        # Reject non-HTTPS redirects and recheck size after curl returns.
        run_command([
            "curl", "--fail", "--silent", "--show-error", "--location",
            "--proto", "=https", "--proto-redir", "=https",
            "--connect-timeout", "10", "--max-time", "120",
            "--retry", "2", "--retry-max-time", "240",
            "--max-filesize", str(limit),
            "--header", "Cache-Control: no-cache, no-store",
            "--header", "Pragma: no-cache", "--output", target, url,
        ], timeout=260)
        if target.stat().st_size > limit:
            raise UpdateError("Download exceeds size limit")

    def board(self):
        """Map the device-tree model to a supported package board name."""
        model = (self.root / "proc/device-tree/model").read_text().rstrip("\0")
        # "ROCK3 Model A" on older kernels, "Radxa ROCK 3A" on 6.18.
        if "ROCK3" in model.replace(" ", ""):
            return "r3a"
        if "Raspberry Pi 5" in model:
            return "rpi5"
        if "Raspberry Pi 4" in model or "Raspberry Pi Compute Module 4" in model:
            return "cm4"
        raise UpdateError(f"Unknown board: {model}")

    def validate(self, package, checksum, filename, expected):
        """Verify archive contents, checksum and release version."""
        if package.stat().st_size > MAX_DOWNLOAD:
            raise UpdateError("Compressed archive exceeds size limit")
        # Accept one sha256sum line with its text/binary marker and exact name.
        match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *]([^\r\n]+)\n?",
                             checksum.read_text(encoding="ascii"))
        if not match or match[2] != filename:
            raise UpdateError("Missing or malformed package checksum")
        digest = hashlib.sha256()
        with package.open("rb") as stream:
            for chunk in iter(lambda: stream.read(MIB), b""):
                digest.update(chunk)
        if digest.hexdigest() != match[1].lower():
            raise UpdateError("Package SHA-256 mismatch")
        # Read the gzip footer too: tar readers may stop at end-of-tar blocks.
        expanded = 0
        with gzip.open(package, "rb") as stream:
            for chunk in iter(lambda: stream.read(MIB), b""):
                expanded += len(chunk)
                if expanded > MAX_EXPANDED:
                    raise UpdateError("Expanded archive exceeds size limit")
        members = {}
        with tarfile.open(package, "r:gz") as archive:
            for member in archive:
                name = archive_path(member.name)
                if name.startswith(('boot/', 'usr/lib/modules/', 'lib/modules/',
                                    'usr/lib/firmware/morse/', 'var/lib/manet-kernel/')):
                    raise UpdateError('Kernel files require the matched kernel payload namespace')
                if name in ('etc/manet-kernel-trial.json', 'usr/local/sbin/manet-kernel-health-check', KERNEL_POLICY):
                    raise UpdateError('Kernel qualification and policy must be installed locally')
                if name in ('boot', 'usr/lib/modules', 'lib/modules', 'usr/lib/firmware/morse',
                            'var/lib/manet-kernel') and not member.isdir():
                    raise UpdateError('Boot and module paths must be directories')
                # Bound metadata growth as well as decompressed archive bytes.
                if name in members or len(members) >= 20000:
                    raise UpdateError("Duplicate archive path or too many members")
                if not (member.isdir() or member.isfile() or member.issym()):
                    raise UpdateError(f"Unsupported archive entry: {name}")
                # Refuse special mode bits and group/world-writable directories.
                if (member.uid != 0 or member.gid != 0 or member.mode & 0o7000
                        or (member.isdir() and member.mode & 0o022)):
                    raise UpdateError(f"Unsafe archive ownership or permissions: {name}")
                if member.size < 0 or member.size > MAX_EXPANDED or member.issparse():
                    raise UpdateError(f"Unsupported archive size or sparse file: {name}")
                members[name] = member
            # Check the complete member map before resolving parents and links.
            for name, member in members.items():
                for parent in PurePosixPath(name).parents:
                    entry = members.get(str(parent))
                    if entry and not entry.isdir():
                        raise UpdateError(f"Archive entry below a non-directory: {name}")
                if member.issym():
                    link = member.linkname
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(name), link))
                    # A relative link must resolve to a regular payload file.
                    if (link.startswith("/") or target not in members
                            or not members[target].isfile()):
                        raise UpdateError(f"Invalid archive symlink: {name}")
                live = self.destination(name)
                if member.isdir():
                    if live.is_symlink() or (live.exists() and not live.is_dir()):
                        raise UpdateError(f"Installed directory conflicts with archive: {name}")
                elif live.exists() and not (live.is_file() or live.is_symlink()):
                    raise UpdateError(f"Installed file conflicts with archive: {name}")
            for name in REQUIRED:
                member = members.get(name)
                if member is None or not member.isfile():
                    raise UpdateError(f"Missing required file: {name}")
                if name.endswith((".sh", ".py")) and not member.mode & 0o100:
                    raise UpdateError(f"Required script is not executable: {name}")
            markers = []
            for name in sorted(MARKERS):
                if members[name].size > 1024:
                    raise UpdateError("Oversized version file")
                text = archive.extractfile(members[name]).read().decode("ascii")
                if version(text) != expected:
                    raise UpdateError("Archive version does not match GitHub release")
                markers.append(text)
            if markers[0] != markers[1]:
                raise UpdateError("Archive version files disagree")
            validate_payload(archive, members, self.board())
        return members

    def stage(self, package, members, directory):
        """Reserve space and unpack regular files into private staging."""
        # Round up to 4 KiB blocks and reserve one extra block per member.
        # Account for both staged files and their live replacements.
        allocations = {name: ((member.size + 4095) // 4096 + 1) * 4096
                       for name, member in members.items()}
        requests = [(directory, sum(allocations.values()))]
        requests.extend((self.destination(name).parent, size)
                        for name, size in allocations.items())
        self.space(requests)
        directory.mkdir()
        with tarfile.open(package, "r:gz") as archive:
            # Extract regular files only. Install links later, without restoring
            # tar ownership or following archive symlinks.
            for name, member in members.items():
                target = directory / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    target.mkdir(exist_ok=True)
                elif member.isfile():
                    with archive.extractfile(member) as incoming, target.open("xb") as output:
                        shutil.copyfileobj(incoming, output)
                    if target.stat().st_size != member.size:
                        raise UpdateError(f"Incomplete staged file: {name}")
                    target.chmod(member.mode & 0o777)

    def clean_scratch(self):
        """Remove abandoned download trees and completed recovery state."""
        # The update lock excludes live downloads. Never follow a leftover
        # symlink or clean anything outside this updater's private namespace.
        for path in self.state.glob('download-*'):
            # tempfile adds an eight-character lowercase/digit/underscore tail.
            if re.fullmatch(r'download-[a-z0-9_]{8}', path.name):
                if path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
        if not self.pending.exists() and self.recovery.exists():
            if self.recovery.is_symlink():
                raise UpdateError('Unsafe recovery directory')
            shutil.rmtree(self.recovery)
            sync_directory(self.state)

    def clean_payload_scratch(self, members):
        """Remove updater temporaries from validated payload parents."""
        # A hard stop can also leave a temporary sibling of a live file.
        # Match exact updater temporary names only within validated parents.
        parents = {self.destination(name).parent for name in members}
        for parent in parents:
            if not parent.is_dir():
                continue
            for path in parent.iterdir():
                if re.fullmatch(r'\.manet-update-[a-z0-9_]{8}', path.name):
                    if path.is_symlink() or path.is_file():
                        path.unlink()
                elif re.fullmatch(r'\.manet-link-[a-z0-9_]{8}', path.name):
                    if path.is_symlink():
                        path.unlink()
                    elif path.is_dir():
                        shutil.rmtree(path)

    def arm_recovery(self, package, checksum, filename, expected, staged, kernel_selected=True):
        """Publish a durable offline recovery source before live replacements."""
        self.space([(self.state, package.stat().st_size + MIB)])
        self.recovery.mkdir(mode=0o700)
        for name, source in (
                ('package.tar.gz', package), ('package.sha256', checksum),
                ('node-update.py', Path(__file__)),
                ('manet_release.py',
                 Path(__file__).with_name('manet_release.py')),
                ('manet_kernel_update.py',
                 Path(__file__).with_name('manet_kernel_update.py'))):
            atomic_file(self.recovery / name, source=source, mode=0o600)
        info = {'version': expected, 'filename': filename, 'kernel_selected': kernel_selected}
        atomic_file(self.recovery / 'release.json',
                    data=json.dumps(info).encode(), mode=0o600)
        sync_directory(self.state)
        # Arm boot recovery even on the first update using this mechanism.
        # No daemon reload or service start is needed to persist enable links.
        for name, target in RECOVERY_UNITS.items():
            relative = 'etc/systemd/system/' + name
            destination = self.destination(relative)
            self.make_directory(destination.parent)
            atomic_file(destination, source=staged / relative)
            link = self.destination('etc/systemd/system/' + target + '/' + name)
            self.make_directory(link.parent)
            self.install_link(link, '../' + name)
        # The marker must become durable after all recovery inputs and units.
        atomic_file(self.pending, data=b'Offline recovery required\n',
                    mode=0o600)

    def recover(self, files_only=False):
        """Revalidate and reinstall a saved transaction offline."""
        if self.recovery.is_symlink() or not self.recovery.is_dir():
            raise UpdateError('Recovery archive is missing; keep in-progress '
                              'and repair the installation locally')
        descriptor = self.recovery / 'release.json'
        if descriptor.stat().st_size > 1024:
            raise UpdateError('Invalid recovery descriptor')
        info = json.loads(descriptor.read_text())
        filename = self.board() + '-tools.tar.gz'
        if (not isinstance(info, dict) or info.get('filename') != filename
                or not isinstance(info.get('version'), str)):
            raise UpdateError('Recovery archive does not match this board')
        expected = version(info['version'])
        package = self.recovery / 'package.tar.gz'
        checksum = self.recovery / 'package.sha256'
        if (package.stat().st_size > MAX_DOWNLOAD
                or checksum.stat().st_size > 1024):
            raise UpdateError('Recovery archive exceeds size limit')
        members = self.validate(package, checksum, filename, expected)
        kernel_selected = info.get('kernel_selected', True)
        if type(kernel_selected) is not bool:
            raise UpdateError('Invalid recovery kernel selection')
        # Kernel trials precede runtime replacement. Early recovery must not
        # depend on the FAT mount or install a release before trial confirmation.
        if KERNEL_MANIFEST in members and kernel_selected and files_only:
            info = self.kernel().read()
            if not info or info['phase'] != 'committed':
                return
        self.clean_payload_scratch(members)
        self.log(f'Resuming release {expected} from local recovery archive')
        with tempfile.TemporaryDirectory(prefix='download-',
                                         dir=self.state) as scratch:
            staged = Path(scratch) / 'stage'
            self.stage(package, members, staged)
            # Early boot restores files without activation or version commits.
            if files_only:
                self.install_payload(staged, members)
                # Restore pending authority before the manager can start at
                # boot and accept a replacement from Alfred. No activation
                # or systemctl calls belong in this early recovery phase.
                self.restore_config()
            else:
                if KERNEL_MANIFEST in members and kernel_selected and not self.kernel().prepare(staged):
                    return
                self.install(staged, members)
        if not files_only:
            self.clean_scratch()
            self.log(f'Node tools recovered to version {expected}')

    def install_payload(self, directory, members):
        """Replace payload and generated links while withholding versions."""
        motd = self.destination("etc/update-motd.d/50-manet-provision").parent
        self.destination("usr/local/bin/node-manager.sh")
        # Space is checked again on every retry, including early boot recovery.
        self.space([(self.destination(name).parent, ((member.size + 4095) // 4096 + 1) * 4096)
                    for name, member in members.items()])
        # Install shallow paths first, with lexical order at each depth.
        for name, member in sorted(members.items(), key=lambda pair: (pair[0].count("/"), pair[0])):
            if name in MARKERS or name == KERNEL_PREFIX.rstrip("/") or name.startswith(KERNEL_PREFIX):
                continue
            target = self.destination(name)
            self.make_directory(target.parent)
            if member.isdir():
                # Never change permissions on an existing live directory.
                self.make_directory(target, mode=member.mode & 0o777)
            elif member.isfile():
                atomic_file(target, source=directory / name, mode=member.mode & 0o777)
            else:
                self.install_link(target, member.linkname)
        # Use the installed config, including any deliberate payload change.
        config = (self.root / "etc/mesh.conf").read_text()
        # Match an unquoted true acs assignment at the start of a config line.
        values = re.findall(r'^acs=(.*)$', config, re.M)
        acs = values and values[-1].rstrip().lower() in ('y', 'yes', '1', 'true')
        selected = "node-manager-acs.sh" if acs else "node-manager-static.sh"
        self.install_link(self.destination("usr/local/bin/node-manager.sh"), selected)
        self.make_directory(motd)
        for label, script in (("50-manet-provision", "manet-provision-status.sh"),
                              ("55-manet-power", "manet-power-status.sh")):
            self.install_link(motd / label, f"/usr/local/bin/{script}")

    def quiesce_config(self):
        """Stop config producers and wait for an in-flight applier to exit."""
        manager = 'node-manager.service'
        worker = 'mesh-config-apply.service'
        run_command(['systemctl', 'stop', manager])
        state = run_command(['systemctl', 'show', '--property=ActiveState',
                             '--value', worker]).strip()
        if state not in {b'inactive', b'failed'}:
            run_command(['systemctl', 'stop', worker])
            # An ACS worker can restart the manager while stopping. Stop it
            # again after the worker has exited, before touching its inputs.
            run_command(['systemctl', 'stop', manager])
        for service in (manager, worker):
            state = run_command(['systemctl', 'show', '--property=ActiveState',
                                 '--value', service]).strip()
            if state not in {b'inactive', b'failed'}:
                raise UpdateError('Config service did not stop: ' + service)

    def preserve_config(self):
        """Save volatile legacy inputs once, before replacing any payload."""
        snapshot = self.recovery / 'config-staging.json'
        if snapshot.exists():
            return
        saved = {}
        for name in ('mesh_pending_config.json', 'mesh_config_ack_version'):
            path = self.root / 'run' / name
            if not path.exists():
                continue
            # Do not turn an untrusted staging file into a trusted private
            # copy. The receiver applies the same check on legacy import.
            if name == 'mesh_pending_config.json':
                st = path.stat()
                if st.st_uid != os.geteuid() or st.st_mode & 0o077:
                    raise UpdateError('Legacy pending config is not private')
            saved[name] = path.read_text()
        atomic_file(snapshot, data=json.dumps(saved).encode(), mode=0o600)

    def restore_config(self):
        """Restore saved volatile authority before manager startup or import."""
        snapshot = self.recovery / 'config-staging.json'
        if not snapshot.exists():
            return
        journal = self.root / 'var/lib/manet-config/journal.json'
        pending = self.root / 'run/mesh_pending_config.json'
        if not journal.exists() and not pending.exists():
            saved = json.loads(snapshot.read_text())
            # Publish ACK first so death during restoration cannot leave a
            # pending package that conceals its still-missing consumed ACK.
            for name in ('mesh_config_ack_version', 'mesh_pending_config.json'):
                if name in saved:
                    atomic_file(self.root / 'run' / name,
                                data=saved[name].encode(), mode=0o600)

    def migrate_config(self):
        """Restore lost volatile inputs and durably import before activation."""
        self.restore_config()
        # Rooted paths also let recovery tests execute the real migration.
        layout = {
            'MANET_CONFIG_STATE_DIR': 'var/lib/manet-config',
            'MANET_ADMIN_STATE_DIR': 'var/lib/manet-admin',
            'MANET_ROLLBACK_DIR': 'var/lib/manet-config-rollback',
            'MANET_RUN_DIR': 'run', 'MANET_TIME_RUN_DIR': 'run',
            'MANET_MESH_CONF': 'etc/mesh.conf',
            'MANET_WPA_DIR': 'etc/wpa_supplicant',
        }
        env = dict(os.environ)
        env.update({key: str(self.root / value)
                    for key, value in layout.items()})
        helper = self.destination('usr/local/bin/mesh-config-sync.py')
        run_command([str(helper), 'migrate'], env=env)

    def install(self, directory, members):
        """Install files, activate services and commit versions after checks."""
        active_runtime = []
        for service in RUNTIME_SERVICES:
            state = run_command(['systemctl', 'show', '--property=ActiveState',
                                 '--value', service]).strip()
            if state == b'active':
                active_runtime.append(service)
        # A failed restart must not turn an active daemon into an optional
        # stopped daemon on the next attempt. Preserve that obligation for
        # this boot, while respecting normal configured startup after reboot.
        boot = (self.root / 'proc/sys/kernel/random/boot_id').read_text().strip()
        snapshot = self.recovery / 'runtime-active.json'
        if snapshot.exists():
            previous = json.loads(snapshot.read_text())
            if previous.get('boot') == boot:
                active_runtime = sorted(set(active_runtime) | set(previous['services']))
                if not set(active_runtime) <= set(RUNTIME_SERVICES):
                    raise UpdateError('Invalid runtime activation snapshot')
        atomic_file(snapshot, data=json.dumps({'boot': boot, 'services': active_runtime}).encode(),
                    mode=0o600)
        self.quiesce_config()
        self.preserve_config()
        self.install_payload(directory, members)
        self.retire_bootstrap(directory, members)
        self.migrate_config()
        self.protect_kernel(directory)
        mesh_setup = self.destination('usr/local/bin/manet-mesh-supplicant.py')
        # Migrate live/lobby width and inactivity under the ACS/AP lock.
        output = run_command([
            'flock', '--exclusive', '--timeout', '10',
            str(self.root / 'run/channel-election.lock'),
            str(mesh_setup), '--update-configs'])
        for line in output.decode('utf-8', errors='replace').splitlines():
            self.log(line)
        self.retire_route_fix()
        self.retire_battery_reader()
        self.retire_network_boot_units()
        self.retire_cpu_powersave()
        run_command(["systemctl", "daemon-reload"])
        # Preserve MAC-matched DHCP files while disabling unqualified DNS.
        # Fail before version commit; recovery repeats the pending migration.
        run_command([str(self.destination('usr/local/bin/manet-dns-setup.sh'))])
        dns_setup = self.destination('usr/local/bin/manet-uplink-dns.py')
        run_command([str(dns_setup), '--root', str(self.root), 'install'],
                    timeout=120)
        run_command(['systemctl', 'enable', '--now', 'manet-uplink-dns.path',
                     'manet-uplink-dns.service'])
        firmware_setup = self.destination('usr/local/bin/manet-mt7916-setup.sh')
        # Bound firmware setup and service integration to ten minutes.
        output = run_command([str(firmware_setup)], timeout=600)
        for line in output.decode('utf-8', errors='replace').splitlines():
            if line.startswith('manet-mt7916-firmware:'):
                self.log(line)
        try:
            run_command(["systemctl", "enable", "--now", "manet-dhcp-isolation.service"])
            run_command([str(self.destination('usr/local/bin/manet-dhcp-isolation.py')), 'ensure'])
        except (UpdateError, OSError, subprocess.SubprocessError):
            # An already running dnsmasq does not execute the new start guard.
            # Leave the update pending and stop serving if isolation failed.
            run_command(["systemctl", "stop", "dnsmasq.service"])
            raise
        run_command(["systemctl", "enable", "--now", "manet-mesh-power.service"])
        run_command(["systemctl", "enable", "--now", "manet-cpu-policy.service"])
        run_command(["systemctl", "enable", "--now", "manet-logrotate.timer"])
        run_command(["systemctl", "enable", "--now", "manet-auto-update.path"])
        run_command(["systemctl", "enable", "--now", "manet-auto-update.timer"])
        run_command(["systemctl", "enable", "manet-wifi-uplink.service"])
        run_command(["systemctl", "start", "manet-wifi-uplink.service"])
        run_command(["systemctl", "try-restart", "manet-wifi-client@*.service"])
        # Running Wi-Fi mesh instances adopt the binary and 20 MHz width now.
        # HaLow candidates require separate bench approval and activation.
        run_command(["systemctl", "try-restart", "wpa_supplicant@*.service"])
        for service in ("mesh-status.service", "node-manager.service"):
            run_command(["systemctl", "restart", service])
        for service in ("mesh-status.service", "node-manager.service", "mesh-channel-agreement.service", "one-shot-time-sync.service"):
            run_command(["systemctl", "is-active", "--quiet", service])
        for service in RUNTIME_SERVICES:
            run_command(['systemctl', 'try-restart', service])
        for service in active_runtime:
            run_command(['systemctl', 'is-active', '--quiet', service])
        # Restart on every attempt, including retries after a previous copy
        # succeeded but its service restart failed. Commit version markers last.
        for name in ("usr/local/bin/version.txt", "etc/manet_version.txt"):
            atomic_file(self.destination(name), source=directory / name)
        self.pending.unlink()
        # The obligation ends with this install. A later same-boot update must
        # not require a service the operator has since stopped.
        snapshot.unlink(missing_ok=True)
        sync_directory(self.state)

    def kernel(self):
        """Use the same durable-file and command primitives as tools recovery."""
        return KernelTrial(self, run_command, atomic_file, sync_directory)

    def retire_bootstrap(self, directory, members):
        """A direct payload supersedes the legacy wrapper under the update lock.

        Pending recovery already owns a durable copy of the full payload.
        Keep a newly installed wrapper, but do not let an old timer or archive
        survive a direct update or get re-enabled by firmware setup.
        """
        prefix = 'usr/local/share/manet/bootstrap/'
        if prefix + 'manifest.json' in members:
            return
        descriptor = self.destination(prefix + 'manifest.json')
        try:
            data = json.loads(descriptor.read_text())
        except (OSError, ValueError):
            data = None
        info = self.kernel().read() if KERNEL_MANIFEST in members else None
        if (isinstance(data, dict) and info and info['phase'] == 'committed'
                and data.get('version') == version((directory / 'etc/manet_version.txt').read_text())
                and data.get('kernel_manifest_sha256') == info['manifest_sha256']):
            # This is the current wrapper's inner install. Its helper removes
            # the wrapper only after the tools markers have also committed.
            return
        names = [prefix + name for name in ('manifest.json', 'cm4-tools.tar.gz',
                                            'cm4-tools.tar.gz.sha256')]
        names += ['etc/systemd/system/' + name for name in (
            'manet-release-bootstrap.service', 'manet-release-bootstrap.timer',
            'timers.target.wants/manet-release-bootstrap.timer')]
        paths = [self.destination(name) for name in names]
        if not any(p.exists() or p.is_symlink() for p in paths):
            return
        # Do not stop the service: it may be the process doing this install.
        timer = self.destination('etc/systemd/system/manet-release-bootstrap.timer')
        if timer.exists():
            run_command(['systemctl', 'disable', '--now', timer.name])
        for path in paths:
            path.unlink(missing_ok=True)
        for directory in {p.parent for p in paths}:
            if directory.is_dir():
                sync_directory(directory)
        self.log('Retired the legacy kernel bootstrap; this update owns recovery')

    def protect_kernel(self, directory):
        """Protect boot files before package setup and on installation retries."""
        run_command([str(directory / 'usr/local/bin/manet-kernel-protect.py'),
                     '--root', str(self.root), '--board', self.board(),
                     '--policy', str(directory / 'etc/apt/preferences.d/00-manet-kernel')],
                    timeout=600)

    @staticmethod
    def install_link(target, link):
        """Replace a symlink atomically and persist its directory entry."""
        with tempfile.TemporaryDirectory(prefix=".manet-link-", dir=target.parent) as scratch:
            temporary = Path(scratch) / "link"
            temporary.symlink_to(link)
            os.replace(temporary, target)
            sync_directory(target.parent)

    def retire_route_fix(self):
        """Disable and remove the route-fix unit and script."""
        unit = self.destination('etc/systemd/system/mesh-default-route-fix.service')
        if unit.exists():
            run_command(['systemctl', 'disable', '--now', 'mesh-default-route-fix.service'])
        for name in ('etc/systemd/system/multi-user.target.wants/mesh-default-route-fix.service',
                     'etc/systemd/system/mesh-default-route-fix.service',
                     'usr/local/bin/mesh-default-route-fix.sh'):
            path = self.destination(name)
            path.unlink(missing_ok=True)

    def retire_battery_reader(self):
        """Stop the retired battery service and remove its files and state."""
        unit = self.destination('etc/systemd/system/battery-reader.service')
        if unit.exists():
            run_command(['systemctl', 'disable', '--now', unit.name])
        for name in (
                'etc/systemd/system/multi-user.target.wants/' + unit.name,
                'etc/systemd/system/battery-reader.service',
                'usr/local/bin/battery-reader.py',
                'run/battery_status.json'):
            self.destination(name).unlink(missing_ok=True)

    def retire_cpu_powersave(self):
        """Stop the old CPU policy before applying the board-aware policy."""
        unit = self.destination('etc/systemd/system/cpu-powersave.service')
        if unit.exists():
            run_command(['systemctl', 'disable', '--now', unit.name])
        self.destination('etc/systemd/system/multi-user.target.wants/' + unit.name).unlink(missing_ok=True)
        unit.unlink(missing_ok=True)

    def retire_network_boot_units(self):
        """Disable listed boot units and retain the AP power unit file."""
        services = ['ebtables-restore.service', 'ap-interface-setup.service',
                    'ap-txpower.service', 'manet-txpower.service',
                    'manet-halow-power.service']
        services.extend(path.name for path in self.destination('etc/systemd/system').glob(
            'halow-txpower-wlan*.service'))
        for service in services:
            unit = self.destination('etc/systemd/system/' + service)
            if unit.exists():
                run_command(['systemctl', 'disable', '--now', service])
            self.destination('etc/systemd/system/multi-user.target.wants/' + service).unlink(missing_ok=True)
            # ap-txpower remains an explicit, role-aware policy action. It is
            # also invoked by hostapd itself, never independently at boot.
            if service != 'ap-txpower.service':
                unit.unlink(missing_ok=True)
        self.destination('usr/local/bin/manet-halow-power.py').unlink(missing_ok=True)
        self.destination('etc/ebtables.rules').unlink(missing_ok=True)

    def update(self, recover_only=False, files_only=False, cancel_kernel=False,
               local_archive=None, local_version=None):
        """Serialize recovery or release selection, verification and install."""
        run = self.root / "run"
        run.mkdir(exist_ok=True)
        with (run / "manet-update.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.log("Another tools update is already running")
                return
            # Pending recovery takes precedence over selection and throttling.
            pending = self.pending.exists()
            self.destination('var/lib/manet-update/placeholder')
            self.make_directory(self.state, mode=0o700)
            self.clean_scratch()
            if cancel_kernel:
                if not pending:
                    raise UpdateError('No pending kernel transaction')
                self.kernel().cancel()
                self.clean_scratch()
                return
            if pending:
                self.recover(files_only=files_only)
                return
            if recover_only or files_only:
                return
            try:
                local = version(self.marker.read_text())
                other = self.root / "usr/local/bin/version.txt"
                if other.read_text() != self.marker.read_text():
                    local = "unknown"
            except (FileNotFoundError, ValueError, UpdateError):
                local = "unknown"
            # Routine checks use marker mtime as a one-day wall-clock throttle.
            # A backward clock step does not count as a recent successful check.
            if (self.routine and local_archive is None and not pending and local != "unknown"
                    and 0 <= time.time() - self.marker.stat().st_mtime < 86400):
                return
            board = self.board()
            self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix="download-", dir=self.state) as scratch:
                work = Path(scratch)
                if local_archive is not None:
                    expected = version(local_version)
                    release = None
                else:
                    release = select_release(self.development, self.download, work)
                    expected = release["version"]
                # Compare decimal components so 0.10 sorts after 0.9.
                if (local != "unknown" and not pending and not self.allow_downgrade
                        and tuple(map(int, local.split("."))) > tuple(map(int, expected.split(".")))):
                    self.marker.touch()
                    self.log(f"Installed {local} is newer than selected {expected}; use --allow-downgrade to replace it")
                    return
                if local == expected and not pending and local_archive is None:
                    self.marker.touch()
                    self.log(f"Node is already running release {expected}")
                    return
                filename = f"{board}-tools.tar.gz"
                package, checksum = work / filename, work / (filename + ".sha256")
                if local_archive is not None:
                    source = Path(local_archive)
                    sidecar = source.with_name(source.name + '.sha256')
                    if (source.name != filename or source.stat().st_size > MAX_DOWNLOAD
                            or sidecar.stat().st_size > 1024):
                        raise UpdateError('Invalid local board archive name or size')
                    self.space([(work, source.stat().st_size + 1024)])
                    shutil.copyfile(source, package)
                    shutil.copyfile(sidecar, checksum)
                    self.log('Using explicit local archive and checksum; no GitHub release metadata')
                else:
                    url = asset_url(release, filename)
                    self.download(url + ".sha256", checksum, 1024)
                    self.download(url, package, MAX_DOWNLOAD)
                    verify_asset(package, release["assets"][filename])
                members = self.validate(package, checksum, filename, expected)
                self.clean_payload_scratch(members)
                staged = work / "stage"
                self.stage(package, members, staged)
                kernel_selected = (KERNEL_MANIFEST not in members or board != 'cm4'
                                   or kernel_enabled(self.root))
                if KERNEL_MANIFEST in members and kernel_selected:
                    self.kernel().preflight(staged)
                elif KERNEL_MANIFEST in members:
                    self.log('CM4 kernel update disabled locally; installing runtime payload only')
                # APT is allowed only before arming the transaction. Boot
                # recovery never depends on network access or package installs.
                self.protect_kernel(staged)
                run_command([staged / 'usr/local/bin/manet-admin-setup.sh'],
                            timeout=600)
                self.arm_recovery(package, checksum, filename, expected, staged, kernel_selected)
                if KERNEL_MANIFEST in members and kernel_selected and not self.kernel().prepare(staged):
                    return
                self.install(staged, members)
                self.log(f"Node tools updated to version {expected}")
            self.clean_scratch()


def interrupted(signum, frame):
    """Turn a termination signal into an update failure."""
    raise UpdateError(f"Update interrupted by signal {signum}")


def main():
    """Parse update mode, require root and report installation failures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routine", action="store_true", help="quiet daily check; errors go to the journal")
    parser.add_argument("--development", action="store_true", help="use the newest published build, including prereleases")
    parser.add_argument("--allow-downgrade", action="store_true", help="allow replacement by an older selected release")
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument('--recover-only', action='store_true',
                          help='finish a pending update without networking')
    recovery.add_argument('--local-archive', type=Path,
                          help='install an explicit local board archive with its adjacent SHA-256 sidecar')
    parser.add_argument('--local-version', help='expected embedded version for --local-archive')
    recovery.add_argument('--cancel-kernel-trial', action='store_true',
                          help='cancel an uncommitted kernel trial on its original kernel')
    recovery.add_argument('--kernel-guard', action='store_true',
                          help=argparse.SUPPRESS)
    recovery.add_argument('--recover-files', action='store_true',
                          help='restore pending payload before service startup')
    args = parser.parse_args()
    if (args.local_archive is None) != (args.local_version is None):
        parser.error('--local-archive and --local-version must be supplied together')
    syslog.openlog("manet-update", syslog.LOG_PID, syslog.LOG_DAEMON)
    updater = Updater(routine=args.routine, development=args.development,
                      allow_downgrade=args.allow_downgrade)
    try:
        if os.geteuid() != 0:
            raise UpdateError("Run node-update.sh as root")
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        if args.kernel_guard:
            updater.kernel().guard()
            return 0
        updater.update(recover_only=args.recover_only,
                       files_only=args.recover_files,
                       cancel_kernel=args.cancel_kernel_trial,
                       local_archive=args.local_archive, local_version=args.local_version)
    except (KernelError, OSError, ValueError, EOFError, tarfile.TarError, zlib.error,
            subprocess.SubprocessError) as error:
        updater.log(f"node-update.sh: Tools update failed: {error}",
                    error=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
