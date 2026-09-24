#!/usr/bin/env python3
"""Verify and stage tools updates before modifying a running node.

Individual files are replaced atomically. This is not a filesystem-wide
transaction: interrupted installation leaves a persistent retry marker.
"""

import argparse
from collections import defaultdict
import fcntl
import gzip
import hashlib
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


VERSION_URL = "https://raw.githubusercontent.com/very-srs/MANET/refs/heads/main/MANET/node_tools/version.txt"
PACKAGE_URLS = {
    "cm4": "https://www.colorado-governor.com/manet/cm4-tools.tar.gz",
    "r3a": "https://www.colorado-governor.com/manet/r3a-tools.tar.gz",
    "rpi5": "https://www.colorado-governor.com/manet/rpi5/rpi5-tools.tar.gz",
}
MIB = 1024 * 1024
MAX_DOWNLOAD = 64 * MIB
MAX_EXPANDED = 512 * MIB
RESERVE = 16 * MIB
MARKERS = {"etc/manet_version.txt", "usr/local/bin/version.txt"}
REQUIRED = MARKERS | {
    "usr/local/bin/node-update.sh", "usr/local/bin/node-update.py",
    "usr/local/bin/node-manager-static.sh", "usr/local/bin/node-manager-acs.sh",
    "usr/local/bin/manet-admin-setup.sh", "usr/local/bin/mesh-status.py",
    "usr/local/bin/manet-provision-status.sh", "usr/local/bin/manet-power-status.sh",
    "etc/systemd/system/manet-admin-setup.service",
    "usr/local/bin/mesh-channel-agreement.py", "usr/local/bin/manet_acs_agreement.py",
    "usr/local/bin/mesh-acs-common.sh", "usr/local/bin/manet_admin.py",
    "usr/local/bin/manet_rendezvous.py",
    "etc/systemd/system/mesh-channel-agreement.service",
    "etc/systemd/system/node-manager.service.d/acs-agreement.conf",
    "usr/local/bin/mesh-time-sync.py", "usr/local/bin/one-shot-time-sync.sh",
    "etc/systemd/system/one-shot-time-sync.service",
    "etc/systemd/system/node-manager.service.d/time-sync.conf",
}
VERSION_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)+")


class UpdateError(Exception):
    pass


def version(text):
    first = text.splitlines()[0] if text else ""
    if not VERSION_PATTERN.fullmatch(first):
        raise UpdateError("Invalid release version")
    return first


def stop_process(process):
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


def run_command(args, timeout=120):
    with subprocess.Popen([str(arg) for arg in args], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=True) as process:
        try:
            output, _ = process.communicate(timeout=timeout)
        except BaseException:
            stop_process(process)
            raise
        if process.returncode:
            detail = output.decode("utf-8", errors="replace")[-2000:].strip()
            raise UpdateError(f"{Path(args[0]).name} failed ({process.returncode}): {detail}")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_file(destination, source=None, data=None, mode=0o644):
    fd, scratch = tempfile.mkstemp(prefix=".manet-update-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            if source is not None:
                with source.open("rb") as incoming:
                    shutil.copyfileobj(incoming, output)
            else:
                output.write(data)
            os.fchmod(output.fileno(), mode)
            output.flush()
            os.fsync(output.fileno())
        os.replace(scratch, destination)
        sync_directory(destination.parent)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)


def archive_path(name):
    # GNU tar builders prefix members with ./; no other normalization is safe.
    while name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/")
    parts = name.split("/")
    if (not name or any(part in ("", ".", "..") for part in parts)
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or parts[0] not in ("etc", "usr", "root")):
        raise UpdateError(f"Unsafe archive path: {name!r}")
    for forbidden in ("etc/mesh.conf", "etc/systemd/network", "usr/lib/modules",
                      "usr/lib/firmware", "usr/local/bin/node-manager.sh"):
        if name == forbidden or name.startswith(forbidden + "/"):
            raise UpdateError(f"Tools archive contains excluded path: {name}")
    return name


class Updater:
    def __init__(self, root=Path("/"), routine=False):
        self.root = Path(root)
        self.routine = routine
        self.marker = self.root / "etc/manet_version.txt"
        self.state = self.root / "var/lib/manet-update"
        self.pending = self.state / "in-progress"

    def log(self, message, error=False):
        syslog.syslog(syslog.LOG_ERR if error else syslog.LOG_INFO, message)
        if not self.routine:
            print(message, file=sys.stderr if error else sys.stdout, flush=True)

    def destination(self, name):
        target = self.root / name
        for parent in target.parents:
            if parent == self.root:
                break
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise UpdateError(f"Unsafe installed parent: {parent}")
        return target

    def make_directory(self, target, mode=0o755):
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
            device = path.stat().st_dev
            totals[device] += size
            representatives[device] = path
        for device, size in totals.items():
            path = representatives[device]
            if shutil.disk_usage(path).free < size + RESERVE:
                raise UpdateError(f"Insufficient disk space on {path}: need {size + RESERVE} bytes free")

    def download(self, url, target, limit):
        self.space([(target.parent, limit)])
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
        model = (self.root / "proc/device-tree/model").read_text().rstrip("\0")
        if "ROCK3" in model:
            return "r3a"
        if "Raspberry Pi 5" in model:
            return "rpi5"
        if "Raspberry Pi 4" in model or "Raspberry Pi Compute Module 4" in model:
            return "cm4"
        raise UpdateError(f"Unknown board: {model}")

    def validate(self, package, checksum, filename, expected):
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
                if name in members or len(members) >= 20000:
                    raise UpdateError("Duplicate archive path or too many members")
                if not (member.isdir() or member.isfile() or member.issym()):
                    raise UpdateError(f"Unsupported archive entry: {name}")
                if (member.uid != 0 or member.gid != 0 or member.mode & 0o7000
                        or (member.isdir() and member.mode & 0o022)):
                    raise UpdateError(f"Unsafe archive ownership or permissions: {name}")
                if member.size < 0 or member.size > MAX_EXPANDED or member.issparse():
                    raise UpdateError(f"Unsupported archive size or sparse file: {name}")
                members[name] = member
            for name, member in members.items():
                for parent in PurePosixPath(name).parents:
                    entry = members.get(str(parent))
                    if entry and not entry.isdir():
                        raise UpdateError(f"Archive entry below a non-directory: {name}")
                if member.issym():
                    link = member.linkname
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(name), link))
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
        return members

    def stage(self, package, members, directory):
        allocations = {name: ((member.size + 4095) // 4096 + 1) * 4096
                       for name, member in members.items()}
        requests = [(directory, sum(allocations.values()))]
        requests.extend((self.destination(name).parent, size)
                        for name, size in allocations.items())
        self.space(requests)
        directory.mkdir()
        with tarfile.open(package, "r:gz") as archive:
            # Extract regular files only; links are installed later. Never apply
            # tar's ownership restoration or follow an archive symlink.
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

    def install(self, directory, members):
        # Read local choices before any payload is installed.
        config = (self.root / "etc/mesh.conf").read_text()
        acs = re.search(r"^acs=(y|yes|1|true)[ \t]*$", config, re.I | re.M)
        selected = "node-manager-acs.sh" if acs else "node-manager-static.sh"
        motd = self.destination("etc/update-motd.d/50-manet-provision").parent
        self.destination("usr/local/bin/node-manager.sh")
        # Resolve dependencies before replacing the running software.
        run_command([directory / "usr/local/bin/manet-admin-setup.sh"], timeout=600)
        # Dependency installation may have consumed space since staging.
        self.space([(self.destination(name).parent, ((member.size + 4095) // 4096 + 1) * 4096)
                    for name, member in members.items()])
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        sync_directory(self.state.parent)
        atomic_file(self.pending, data=b"Installation incomplete; retry required\n", mode=0o600)
        for name, member in sorted(members.items(), key=lambda pair: (pair[0].count("/"), pair[0])):
            if name in MARKERS:
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
        atomic_file(self.destination("usr/local/bin/node-manager.sh"),
                    source=directory / "usr/local/bin" / selected, mode=0o755)
        self.make_directory(motd)
        for label, script in (("50-manet-provision", "manet-provision-status.sh"),
                              ("55-manet-power", "manet-power-status.sh")):
            self.install_link(motd / label, f"/usr/local/bin/{script}")
        run_command(["systemctl", "daemon-reload"])
        for service in ("mesh-status.service", "node-manager.service"):
            run_command(["systemctl", "restart", service])
        for service in ("mesh-status.service", "node-manager.service", "mesh-channel-agreement.service", "one-shot-time-sync.service"):
            run_command(["systemctl", "is-active", "--quiet", service])
        # Restart on every attempt, including retries after a previous copy
        # succeeded but its service restart failed. Commit version markers last.
        for name in ("usr/local/bin/version.txt", "etc/manet_version.txt"):
            atomic_file(self.destination(name), source=directory / name)
        self.pending.unlink()
        sync_directory(self.state)

    @staticmethod
    def install_link(target, link):
        with tempfile.TemporaryDirectory(prefix=".manet-link-", dir=target.parent) as scratch:
            temporary = Path(scratch) / "link"
            temporary.symlink_to(link)
            os.replace(temporary, target)
            sync_directory(target.parent)

    def update(self):
        run = self.root / "run"
        run.mkdir(exist_ok=True)
        with (run / "manet-update.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.log("Another tools update is already running")
                return
            pending = self.pending.exists()
            try:
                local = version(self.marker.read_text())
                other = self.root / "usr/local/bin/version.txt"
                if other.read_text() != self.marker.read_text():
                    local = "unknown"
            except (FileNotFoundError, ValueError, UpdateError):
                local = "unknown"
            if (self.routine and not pending and local != "unknown"
                    and 0 <= time.time() - self.marker.stat().st_mtime < 86400):
                return
            board = self.board()
            self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix="download-", dir=self.state) as scratch:
                work = Path(scratch)
                remote = work / "release.txt"
                self.download(VERSION_URL, remote, 1024)
                expected = version(remote.read_text(encoding="ascii"))
                if local == expected and not pending:
                    self.marker.touch()
                    self.log(f"Node is already running release {expected}")
                    return
                url = PACKAGE_URLS[board]
                filename = url.rsplit("/", 1)[1]
                package, checksum = work / filename, work / (filename + ".sha256")
                self.download(url + ".sha256", checksum, 1024)
                self.download(url, package, MAX_DOWNLOAD)
                members = self.validate(package, checksum, filename, expected)
                staged = work / "stage"
                self.stage(package, members, staged)
                self.install(staged, members)
                self.log(f"Node tools updated to version {expected}")


def interrupted(signum, frame):
    raise UpdateError(f"Update interrupted by signal {signum}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routine", action="store_true", help="quiet daily check; errors go to the journal")
    args = parser.parse_args()
    syslog.openlog("manet-update", syslog.LOG_PID, syslog.LOG_DAEMON)
    updater = Updater(routine=args.routine)
    try:
        if os.geteuid() != 0:
            raise UpdateError("Run node-update.sh as root")
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        updater.update()
    except (UpdateError, OSError, ValueError, EOFError, tarfile.TarError, zlib.error,
            subprocess.SubprocessError) as error:
        updater.log(f"ERROR: Tools update failed: {error}", error=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
