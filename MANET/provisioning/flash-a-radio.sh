#!/bin/bash
# Download and run the selected MANET flasher. Requires Python 3, and curl
# unless it runs from an offline kit.
set -e
command -v python3 >/dev/null || { echo "Install python3, then run this again." >&2; exit 1; }
read -r -d '' MANET_LAUNCHER_CODE <<'MANET_LAUNCHER_PY' || true
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

REPO = "very-srs/MANET"
BASE = f"https://github.com/{REPO}/releases"
# A folder holding this file is an offline kit, see make_kit.
KIT_MARKER = "MANET-OFFLINE-KIT.txt"
# Offline setup covers the CM4, the board with an offline bundle.
KIT_BOARD = "cm4"
parser = argparse.ArgumentParser(description="Image a MANET node using the latest stable release")
parser.add_argument("--development", action="store_true", help="test the newest published build, including prereleases")
parser.add_argument("--local-scripts", action="store_true", help="use setup scripts from this checkout with the selected release packages")
parser.add_argument("--offline", action="store_true", help="put everything a CM4 needs on each card, so it sets itself up without internet")
parser.add_argument("--make-kit", metavar="DIR", type=Path, help="fill DIR, such as a USB stick, with everything a computer without internet needs to flash CM4 radios")
launcher = Path(sys.argv[1]).resolve()
args = parser.parse_args(sys.argv[2:])


def fetch(url, target, limit=4 * 1024 * 1024, max_time=120):
    if not shutil.which("curl"):
        raise ValueError("Install curl, then run this again")
    subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location",
                    "--proto", "=https", "--proto-redir", "=https",
                    "--connect-timeout", "10", "--max-time", str(max_time), "--retry", "2",
                    "--header", "User-Agent: manet-flasher", "--max-filesize", str(limit),
                    "--output", str(target), url], check=True, timeout=3 * max_time + 20)
    if target.stat().st_size > limit:
        raise ValueError("Download exceeds size limit")


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def matches(path, size, digest):
    return path.is_file() and path.stat().st_size == size and sha256(path) == digest


def check_manifest(manifest, tag=None):
    if (manifest.get("schema") != 1
            or not re.fullmatch(r"v[0-9]+(?:\.[0-9]+)+", manifest.get("tag", ""))
            or manifest["tag"] != "v" + manifest.get("version", "")
            or (tag and tag != manifest["tag"])):
        raise ValueError("Invalid release manifest")
    return manifest


def select_manifest(stage):
    """Download the stable or development release manifest into stage."""
    metadata = stage / "metadata.json"
    tag = None
    url = BASE + "/latest/download/manet-release.json"
    if args.development:
        releases = []
        for page in range(1, 101):
            fetch(f"https://api.github.com/repos/{REPO}/releases?per_page=100&page={page}", metadata)
            batch = json.loads(metadata.read_text())
            if not isinstance(batch, list):
                raise ValueError("Invalid release list")
            releases.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise ValueError("Too many releases")
        eligible = [r for r in releases if not r.get("draft") and r.get("published_at")
                    and re.fullmatch(r"v[0-9]+(?:\.[0-9]+)+", r.get("tag_name", ""))
                    and any(a.get("name") == "manet-release.json" for a in r.get("assets", []))]
        if not eligible:
            raise ValueError("No published MANET build is available")
        tag = max(eligible, key=lambda r: (r["published_at"], r["id"]))["tag_name"]
        url = BASE + f"/download/{tag}/manet-release.json"
    manifest_file = stage / "manet-release.json"
    fetch(url, manifest_file)
    manifest = check_manifest(json.loads(manifest_file.read_text()), tag)
    print(f"Using {'development' if args.development else 'stable'} release {manifest['version']}", flush=True)
    return manifest, manifest_file


def unpack(bundle, asset, scripts):
    """Extract a flasher ZIP that matches its manifest entry."""
    if bundle.stat().st_size != asset["size"] or sha256(bundle) != asset["sha256"]:
        raise ValueError("Flasher download does not match its checksum")
    with zipfile.ZipFile(bundle) as archive:
        if sum(item.file_size for item in archive.infolist()) > 32 * 1024 * 1024:
            raise ValueError("Flasher archive is too large")
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in item.filename:
                raise ValueError("Unsafe flasher archive path")
        archive.extractall(scripts)
    return scripts


def offline_assets(manifest):
    """The offline bundle and Raspberry Pi OS image a kit carries."""
    offline = f"{KIT_BOARD}-offline.tar"
    image = manifest.get("os_image")
    if offline not in manifest.get("assets", {}) or not isinstance(image, dict):
        raise ValueError(f"Release {manifest['version']} has no offline bundle. Use a newer release")
    name = image.get("url", "").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.img\.xz", name):
        raise ValueError("Invalid Raspberry Pi OS image in the release manifest")
    return offline, image, name


def fetch_into(kit, name, url, size, digest):
    """Download one kit file unless a verified copy is already there."""
    target = kit / name
    if matches(target, size, digest):
        print(f"  {name}: already in the kit", flush=True)
        return
    print(f"  {name}: downloading {size / 2**20:.0f} MiB", flush=True)
    partial = kit / (name + ".partial")
    fetch(url, partial, size, 3600)
    if not matches(partial, size, digest):
        partial.unlink()
        raise ValueError(f"{name} does not match its checksum")
    partial.replace(target)


def make_kit(kit):
    """Fill kit with the launchers, setup scripts, image and packages for one
    release, so a computer without internet can flash CM4 radios from it."""
    if args.offline or args.local_scripts:
        raise ValueError("--make-kit cannot be combined with --offline or --local-scripts")
    with tempfile.TemporaryDirectory(prefix="manet-kit-") as scratch:
        manifest, manifest_file = select_manifest(Path(scratch))
        offline, image, image_name = offline_assets(manifest)
        kit.mkdir(parents=True, exist_ok=True)
        # Until the marker is written again, a half-filled kit is not a kit.
        (kit / KIT_MARKER).unlink(missing_ok=True)
        print(f"Filling {kit}", flush=True)
        for name in ("manet-flasher.zip", "flash-a-radio.sh", "Flash-a-Radio.cmd",
                     f"{KIT_BOARD}-install.tar.gz", offline):
            asset = manifest["assets"][name]
            fetch_into(kit, name, BASE + f"/download/{manifest['tag']}/{name}", asset["size"], asset["sha256"])
        fetch_into(kit, image_name, image["url"], image["size"], image["sha256"])
        shutil.copyfile(manifest_file, kit / "manet-release.json")
    try:
        (kit / "flash-a-radio.sh").chmod(0o755)
    except OSError:
        pass
    (kit / KIT_MARKER).write_text(
        f"MANET offline kit, release {manifest['version']}\n\n"
        "This folder holds everything needed to flash CM4 radios that set\n"
        "themselves up without internet, on a computer with no network.\n\n"
        "Linux:   bash flash-a-radio.sh\n"
        "Windows: double-click Flash-a-Radio.cmd\n\n"
        "The computer needs Raspberry Pi Imager and rpiboot installed. Windows\n"
        "needs Raspberry Pi Imager 2.0 or later. Saved settings are kept in\n"
        "the manet-flasher folder here.\n")
    print(f"Offline kit for release {manifest['version']} is ready in {kit}")
    return 0


def kit_folder():
    for folder in (launcher.parent, launcher.parent.parent):
        if (folder / KIT_MARKER).is_file():
            return folder
    return None


def start():
    if args.make_kit:
        return make_kit(args.make_kit.resolve())
    kit = kit_folder()
    work = launcher.parent
    if not (work / "linux-flasher.sh").exists() and work.name != "manet-flasher":
        work = work / "manet-flasher"
        work.mkdir(exist_ok=True)
        shutil.copy2(launcher, work / "flash-a-radio.sh")
    with tempfile.TemporaryDirectory(prefix=".release-", dir=work) as scratch:
        stage = Path(scratch)
        env = dict(os.environ, MANET_FLASHER_WORK=str(work), MANET_KIT_DIR="", MANET_OS_IMAGE="",
                   MANET_OFFLINE="1" if args.offline or kit else "0")
        if kit:
            # Everything comes from the kit. Nothing here uses the network.
            if args.development or args.local_scripts:
                raise ValueError("An offline kit is fixed to one release; make a new kit to change it")
            manifest_file = kit / "manet-release.json"
            manifest = check_manifest(json.loads(manifest_file.read_text()))
            offline, image, image_name = offline_assets(manifest)
            print(f"Using the offline kit for release {manifest['version']} in {kit}", flush=True)
            scripts = unpack(kit / "manet-flasher.zip", manifest["assets"]["manet-flasher.zip"], stage / "scripts")
            print("Checking the Raspberry Pi OS image in the kit...", flush=True)
            if not matches(kit / image_name, image["size"], image["sha256"]):
                raise ValueError(f"{image_name} in the kit is missing or damaged. Make the kit again")
            env.update(MANET_KIT_DIR=str(kit), MANET_OS_IMAGE=str(kit / image_name))
        else:
            manifest, manifest_file = select_manifest(stage)
            if args.local_scripts:
                scripts = launcher.parent
                if not (scripts / "linux-flasher.sh").is_file():
                    raise ValueError("--local-scripts requires a source checkout")
            else:
                bundle = stage / "manet-flasher.zip"
                fetch(BASE + f"/download/{manifest['tag']}/manet-flasher.zip", bundle, 16 * 1024 * 1024)
                scripts = unpack(bundle, manifest["assets"]["manet-flasher.zip"], stage / "scripts")
        env["MANET_RELEASE_FILE"] = str(manifest_file)
        return subprocess.call(["bash", str(scripts / "linux-flasher.sh")], env=env)


try:
    sys.exit(start())
except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
    print(f"Unable to start MANET flasher: {error}", file=sys.stderr)
    sys.exit(1)
MANET_LAUNCHER_PY
exec python3 -c "$MANET_LAUNCHER_CODE" "$0" "$@"
