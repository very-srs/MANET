"""Select published MANET releases and verify their download manifest."""

import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote

REPOSITORY = "very-srs/MANET"
API = f"https://api.github.com/repos/{REPOSITORY}"
DOWNLOADS = f"https://github.com/{REPOSITORY}/releases/download"
STABLE_MANIFEST = f"https://github.com/{REPOSITORY}/releases/latest/download/manet-release.json"
TAG_PATTERN = re.compile(r"v([0-9]+(?:\.[0-9]+)+)")
BOARDS = ("cm4", "r3a", "rpi5")
PACKAGES = tuple(f"{board}-{kind}.tar.gz" for board in BOARDS for kind in ("tools", "install"))


class ReleaseError(ValueError):
    pass


def download(url, destination, limit):
    result = subprocess.run([
        "curl", "--fail", "--silent", "--show-error", "--location",
        "--proto", "=https", "--proto-redir", "=https",
        "--connect-timeout", "10", "--max-time", "120", "--retry", "2",
        "--max-filesize", str(limit), "--header", "User-Agent: manet-release",
        "--output", str(destination), url,
    ], capture_output=True, timeout=380)
    if result.returncode:
        raise ReleaseError(f"Could not download {url}: {result.stderr.decode(errors='replace')[-500:]}")
    if Path(destination).stat().st_size > limit:
        raise ReleaseError("Download exceeds size limit")


def release_list(fetch_json):
    releases = []
    for page in range(1, 101):
        batch = fetch_json(f"{API}/releases?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ReleaseError("Invalid GitHub release list")
        releases.extend(batch)
        if len(batch) < 100:
            return releases
    raise ReleaseError("Too many releases to select safely")


def newest_release(releases):
    eligible = [r for r in releases if not r.get("draft")
                and TAG_PATTERN.fullmatch(r.get("tag_name", ""))
                and r.get("published_at")
                and any(a.get("name") == "manet-release.json" for a in r.get("assets", []))]
    if not eligible:
        raise ReleaseError("No published MANET release is available")
    return max(eligible, key=lambda r: (r["published_at"], r["id"]))


def validate_manifest(value, tag=None):
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ReleaseError("Unsupported MANET release manifest")
    match = TAG_PATTERN.fullmatch(value.get("tag", ""))
    if not match or match[1] != value.get("version") or (tag and tag != value["tag"]):
        raise ReleaseError("Release tag and version disagree")
    if not re.fullmatch(r"[0-9a-f]{40}", value.get("commit", "")):
        raise ReleaseError("Invalid source commit")
    assets = value.get("assets", {})
    if not isinstance(assets, dict) or not all(name in assets for name in PACKAGES):
        raise ReleaseError("Release does not contain packages for all boards")
    for name, asset in assets.items():
        if (not re.fullmatch(r"[A-Za-z0-9_. -]+", name)
                or not isinstance(asset, dict)
                or not re.fullmatch(r"[a-f0-9]{64}", asset.get("sha256", ""))
                or type(asset.get("size")) is not int or asset["size"] <= 0):
            raise ReleaseError("Invalid release asset")
    return value


def select_release(development=False, fetch=download, directory=None):
    with tempfile.TemporaryDirectory(prefix="manet-release-", dir=directory) as scratch:
        target = Path(scratch) / "metadata.json"

        def fetch_json(url):
            fetch(url, target, 4 * 1024 * 1024)
            return json.loads(target.read_text(encoding="utf-8"))

        tag = None
        url = STABLE_MANIFEST
        if development:
            tag = newest_release(release_list(fetch_json))["tag_name"]
            url = f"{DOWNLOADS}/{tag}/manet-release.json"
        return validate_manifest(fetch_json(url), tag)


def asset_url(manifest, name):
    if name not in manifest["assets"]:
        raise ReleaseError(f"Release is missing {name}")
    return f"{DOWNLOADS}/{manifest['tag']}/{quote(name)}"


def verify_asset(path, asset):
    path = Path(path)
    if path.stat().st_size != asset["size"]:
        raise ReleaseError(f"Wrong size for {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != asset["sha256"]:
        raise ReleaseError(f"SHA-256 mismatch for {path.name}")
