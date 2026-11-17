#!/usr/bin/env python3
"""Build cm4-offline.tar, everything a CM4 first boot downloads besides the
install package: the Debian packages provisioning installs on the pinned
Raspberry Pi OS image, yq, MediaMTX, and a version marker.

apt resolves the packages inside a chroot of that exact image, after the same
kernel protection first boot applies, so the bundle installs on a card written
from it. Linux only. Needs sudo, losetup, xz, curl, apt-ftparchive (apt-utils)
and qemu-user-static registered for aarch64 with the F flag (binfmt-support).
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PROVISIONING = ROOT / "MANET/provisioning"
TOOLS = ROOT / "MANET/node_tools"
PACKAGES = ROOT / "MANET/install_packages"
# The template's hardware token for a CM4, which first boot passes to the
# kernel protection helper.
PROTECT_BOARD = "rpi4"


def image_url():
    """The Raspberry Pi OS image both flashers write. They must agree."""
    linux = re.search(r'^PI_OS_IMAGE_URL="(https://[^"]+)"$',
                      (PROVISIONING / "linux-flasher.sh").read_text(), re.M)
    windows = re.search(r'^\$OS_IMAGE_URL\s*=\s*"(https://[^"]+)"',
                        (PROVISIONING / "windows.ps1").read_text(), re.M)
    if not linux or not windows or linux[1] != windows[1]:
        raise ValueError("linux-flasher.sh and windows.ps1 must name the same image URL")
    return linux[1]


def pins():
    """The yq and MediaMTX versions and digests first boot verifies."""
    text = (PROVISIONING / "firstrun.sh.template").read_text()
    values = {}
    for key in ("YQ_VERSION", "YQ_SHA256", "MEDIAMTX_VERSION", "MEDIAMTX_SHA256"):
        found = re.findall(rf"^\s*{key}='([^']+)'$", text, re.M)
        if len(set(found)) != 1:
            raise ValueError(f"Expected one {key} in firstrun.sh.template")
        values[key] = found[0]
    return values


def shell_lines(text):
    """Logical shell lines, with backslash continuations joined."""
    return re.sub(r"\\\n", " ", text).splitlines()


def apt_names(words):
    """Package names from apt-get install arguments, and whether recommends
    are installed."""
    names, skip = [], False
    for word in words:
        if skip:
            skip = False
        elif word == "-o":
            skip = True
        elif not word.startswith("-") and not word.startswith("$"):
            names.append(word)
    return "--no-install-recommends" not in words, names


def install_steps():
    """Every apt install that provisioning runs on a Raspberry Pi, in order:
    (with recommends, package names)."""
    steps = []
    command = re.compile(r"^(?:DEBIAN_FRONTEND=noninteractive )?apt(?:-get)? install\s")
    for line in shell_lines((PROVISIONING / "firstrun.sh.template").read_text()):
        if command.match(line.strip()):
            words = shlex.split(line.strip())
            words = words[words.index("install") + 1:]
            steps.append(apt_names(words))
    for line in shell_lines((TOOLS / "radio-setup.sh").read_text()):
        if "install_packages " in line and "()" not in line:
            words = shlex.split(line.strip())
            steps.append(apt_names(words[words.index("install_packages") + 1:]))
    if len(steps) != 4 or not all(names for _, names in steps):
        raise ValueError(f"Expected four provisioning apt installs, found {len(steps)}")
    return steps


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def run(*args, **kwargs):
    print("+", " ".join(shlex.quote(str(a)) for a in args), flush=True)
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def fetch(url, target):
    run("curl", "--fail", "--silent", "--show-error", "--location",
        "--proto", "=https", "--proto-redir", "=https", "--retry", "2",
        "--connect-timeout", "15", "--max-time", "3600", "--output", target, url)


def fetch_verified(url, target, expected):
    target = Path(target)
    if target.exists() and sha256(target) == expected:
        return target
    partial = target.with_name(target.name + ".partial")
    fetch(url, partial)
    if sha256(partial) != expected:
        partial.unlink()
        raise ValueError(f"{url} does not match its expected SHA-256")
    partial.replace(target)
    return target


def os_image(cache):
    """The pinned image, downloaded once and checked against the digest that
    Raspberry Pi publishes beside it."""
    url = image_url()
    cache.mkdir(parents=True, exist_ok=True)
    listing = cache / (url.rsplit("/", 1)[1] + ".sha256")
    fetch(url + ".sha256", listing)
    expected = listing.read_text().split()[0]
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Unexpected image checksum file")
    image = fetch_verified(url, cache / url.rsplit("/", 1)[1], expected)
    return {"url": url, "sha256": expected, "size": image.stat().st_size}, image


def check_host():
    for tool in ("sudo", "losetup", "xz", "curl", "apt-ftparchive", "tar", "findmnt"):
        if not shutil.which(tool):
            raise ValueError(f"{tool} is required")
    binfmt = Path("/proc/sys/fs/binfmt_misc/qemu-aarch64")
    flags = re.search(r"^flags: *(\S*)", binfmt.read_text(), re.M) if binfmt.exists() else None
    if not flags or "F" not in flags[1]:
        raise ValueError("qemu-user-static must be registered for aarch64 with the F flag "
                         "(install qemu-user-static and binfmt-support)")


class Chroot:
    """The image's root partition mounted with apt's lists and downloads kept
    outside it, so the image needs no resizing."""

    def __init__(self, image, work):
        self.work = work
        self.root = work / "root"
        self.lists = work / "apt-lists"
        self.cache = work / "apt-cache"
        self.mounts = []
        self.loop = None
        self.image = image

    def __enter__(self):
        for path in (self.root, self.lists, self.cache / "archives/partial"):
            path.mkdir(parents=True, exist_ok=True)
        try:
            self.loop = subprocess.run(["sudo", "losetup", "-fP", "--show", str(self.image)],
                                       check=True, capture_output=True, text=True).stdout.strip()
            self.mount(f"{self.loop}p2", self.root)
            self.mount(f"{self.loop}p1", self.root / "boot/firmware")
            for name in ("dev", "dev/pts", "proc", "sys"):
                self.mount(f"/{name}", self.root / name, "--bind")
            self.mount(self.lists, self.root / "var/lib/apt/lists", "--bind")
            self.mount(self.cache, self.root / "var/cache/apt", "--bind")
            run("sudo", "cp", "-L", "--remove-destination", "/etc/resolv.conf", self.root / "etc/resolv.conf")
        except BaseException:
            # A with block calls __exit__ only after __enter__ returns.
            self.__exit__(None, None, None)
            raise
        return self

    def mount(self, source, target, *options):
        run("sudo", "mount", *options, source, target)
        self.mounts.append(target)

    def __call__(self, *args):
        run("sudo", "chroot", self.root, "env", "DEBIAN_FRONTEND=noninteractive", "LC_ALL=C", *args)

    def __exit__(self, *exc):
        for target in reversed(self.mounts):
            subprocess.run(["sudo", "umount", str(target)])
        if self.loop:
            subprocess.run(["sudo", "losetup", "-d", self.loop])


def remove_work(work):
    """Delete the work folder, but never while anything is mounted in it: a
    leftover bind mount of /dev would be deleted along with it."""
    targets = subprocess.run(["findmnt", "-rn", "-o", "TARGET"], capture_output=True,
                             text=True, check=True).stdout.split()
    if any(t == str(work) or t.startswith(str(work) + "/") for t in targets):
        print(f"Leaving {work}: something is still mounted inside it", file=sys.stderr)
        return
    run("sudo", "rm", "-rf", "--one-file-system", work)


def build(output, cache):
    check_host()
    version = (TOOLS / "version.txt").read_bytes()
    if version != (ROOT / "MANET/etc/manet_version.txt").read_bytes():
        raise ValueError("Both source version files must match")
    steps = install_steps()
    pin = pins()
    record, image_xz = os_image(cache)
    work = Path(tempfile.mkdtemp(prefix="offline-work-", dir=PACKAGES))
    try:
        image = work / "image.img"
        with image.open("wb") as raw:
            run("xz", "-dc", image_xz, stdout=raw)
        stage = work / "stage"
        (stage / "debs").mkdir(parents=True)
        with Chroot(image, work) as chroot:
            run("sudo", "install", "-D", "-m", "0644",
                ROOT / "MANET/etc/apt/preferences.d/00-manet-kernel",
                chroot.root / "etc/apt/preferences.d/00-manet-kernel")
            run("sudo", "install", "-D", "-m", "0755", TOOLS / "manet-kernel-protect.py",
                chroot.root / "usr/local/bin/manet-kernel-protect.py")
            chroot("python3", "/usr/local/bin/manet-kernel-protect.py", "--board", PROTECT_BOARD)
            chroot("apt-get", "update")
            for recommends, names in steps:
                chroot("apt-get", "install", "--download-only", "-y",
                       *([] if recommends else ["--no-install-recommends"]), *names)
            debs = sorted((chroot.cache / "archives").glob("*.deb"))
            for deb in debs:
                shutil.copyfile(deb, stage / "debs" / deb.name)
        index = subprocess.run(["apt-ftparchive", "packages", "."], cwd=stage / "debs",
                               check=True, capture_output=True).stdout
        (stage / "debs/Packages").write_bytes(index)
        fetch_verified(f"https://github.com/mikefarah/yq/releases/download/{pin['YQ_VERSION']}/yq_linux_arm64",
                       stage / "yq_linux_arm64", pin["YQ_SHA256"])
        mediamtx = f"mediamtx_{pin['MEDIAMTX_VERSION']}_linux_arm64.tar.gz"
        fetch_verified(f"https://github.com/bluenviron/mediamtx/releases/download/{pin['MEDIAMTX_VERSION']}/{mediamtx}",
                       stage / mediamtx, pin["MEDIAMTX_SHA256"])
        (stage / "VERSION").write_bytes(version)
        (stage / "os-image.json").write_text(json.dumps(record, indent=2) + "\n")
        partial = output.with_name(output.name + ".partial")
        run("tar", "--sort=name", "--owner=0", "--group=0", "--numeric-owner",
            "-cf", partial, "-C", stage, ".")
        partial.replace(output)
        output.with_name(output.name + ".sha256").write_text(f"{sha256(output)}  {output.name}\n")
        print(f"{output}: {len(debs)} packages, {output.stat().st_size / 2**20:.0f} MiB")
    finally:
        remove_work(work)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=PACKAGES / "cm4-offline.tar")
    parser.add_argument("--cache", type=Path, default=PACKAGES / "offline-cache",
                        help="where the downloaded Raspberry Pi OS image is kept")
    args = parser.parse_args()
    build(args.output.resolve(), args.cache.resolve())


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f"Offline bundle build failed: {error}")
