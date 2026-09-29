#!/bin/bash
# Download and run the selected MANET flasher. Requires Python 3 and curl.
set -e
command -v python3 >/dev/null || { echo "Install python3, then run this again." >&2; exit 1; }
command -v curl >/dev/null || { echo "Install curl, then run this again." >&2; exit 1; }
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
parser = argparse.ArgumentParser(description="Image a MANET node using the latest stable release")
parser.add_argument("--development", action="store_true", help="test the newest published build, including prereleases")
parser.add_argument("--local-scripts", action="store_true", help="use setup scripts from this checkout with the selected release packages")
launcher = Path(sys.argv[1]).resolve()
args = parser.parse_args(sys.argv[2:])


def fetch(url, target, limit=4 * 1024 * 1024):
    subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location",
                    "--proto", "=https", "--proto-redir", "=https",
                    "--connect-timeout", "10", "--max-time", "120", "--retry", "2",
                    "--header", "User-Agent: manet-flasher", "--max-filesize", str(limit),
                    "--output", str(target), url], check=True, timeout=380)
    if target.stat().st_size > limit:
        raise ValueError("Download exceeds size limit")


def start():
    work = launcher.parent
    if not (work / "linux-flasher.sh").exists() and work.name != "manet-flasher":
        work = work / "manet-flasher"
        work.mkdir(exist_ok=True)
        shutil.copy2(launcher, work / "flash-a-radio.sh")
    with tempfile.TemporaryDirectory(prefix=".release-", dir=work) as scratch:
        stage = Path(scratch)
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
        manifest = json.loads(manifest_file.read_text())
        if (manifest.get("schema") != 1
                or not re.fullmatch(r"v[0-9]+(?:\.[0-9]+)+", manifest.get("tag", ""))
                or manifest["tag"] != "v" + manifest.get("version", "")
                or (tag and tag != manifest["tag"])):
            raise ValueError("Invalid release manifest")
        print(f"Using {'development' if args.development else 'stable'} release {manifest['version']}", flush=True)
        if args.local_scripts:
            scripts = launcher.parent
            if not (scripts / "linux-flasher.sh").is_file():
                raise ValueError("--local-scripts requires a source checkout")
        else:
            asset = manifest["assets"]["manet-flasher.zip"]
            bundle = stage / "manet-flasher.zip"
            fetch(BASE + f"/download/{manifest['tag']}/manet-flasher.zip", bundle, 16 * 1024 * 1024)
            if (bundle.stat().st_size != asset["size"]
                    or hashlib.sha256(bundle.read_bytes()).hexdigest() != asset["sha256"]):
                raise ValueError("Flasher download does not match its checksum")
            scripts = stage / "scripts"
            with zipfile.ZipFile(bundle) as archive:
                if sum(item.file_size for item in archive.infolist()) > 32 * 1024 * 1024:
                    raise ValueError("Flasher archive is too large")
                for item in archive.infolist():
                    path = PurePosixPath(item.filename)
                    if path.is_absolute() or ".." in path.parts or "\\" in item.filename:
                        raise ValueError("Unsafe flasher archive path")
                archive.extractall(scripts)
        env = dict(os.environ, MANET_RELEASE_FILE=str(manifest_file), MANET_FLASHER_WORK=str(work))
        return subprocess.call(["bash", str(scripts / "linux-flasher.sh")], env=env)


try:
    sys.exit(start())
except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
    print(f"Unable to start MANET flasher: {error}", file=sys.stderr)
    sys.exit(1)
MANET_LAUNCHER_PY
exec python3 -c "$MANET_LAUNCHER_CODE" "$0" "$@"
