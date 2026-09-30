#!/usr/bin/env python3
"""Publish verified MANET packages, promote a stable release, or prune prereleases."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "MANET/node_tools"))
from manet_release import API, DOWNLOADS, PACKAGES, TAG_PATTERN, download, validate_manifest

PROVISIONING = (
    "flash-a-radio.sh", "linux-flasher.sh", "Flash a Radio.cmd", "windows.ps1",
    "manet-flasher.ps1", "prepare-release.py", "flash-target.py", "firstrun.sh.template",
    "rock3a-provision.sh.template", "additional-scripts/README.md",
)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def check_package(path, expected):
    expected_sum = f"{digest(path)}  {path.name}\n"
    if path.with_name(path.name + ".sha256").read_text() != expected_sum:
        raise ValueError(f"Checksum sidecar does not match {path.name}")
    with tarfile.open(path, "r:gz") as archive:
        for marker in ("etc/manet_version.txt", "usr/local/bin/version.txt"):
            matches = [m for m in archive.getmembers() if m.name.removeprefix("./") == marker]
            if len(matches) != 1 or not matches[0].isfile():
                raise ValueError(f"Missing or duplicate version marker in {path.name}")
            if archive.extractfile(matches[0]).read() != expected:
                raise ValueError(f"Wrong version in {path.name}")


def build_assets(packages, output):
    if git("status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("Commit the source changes before publishing")
    commit = git("rev-parse", "HEAD").decode().strip()
    expected = git("show", "HEAD:MANET/node_tools/version.txt")
    if expected != git("show", "HEAD:MANET/etc/manet_version.txt"):
        raise ValueError("Both source version files must match")
    version = expected.decode("ascii").splitlines()[0]
    tag = "v" + version
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError("Invalid release version")
    output.mkdir(parents=True, exist_ok=True)
    paths = [packages / name for name in PACKAGES]
    for path in paths:
        check_package(path, expected)
    bundle = output / "manet-flasher.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in PROVISIONING:
            entry = zipfile.ZipInfo(name)
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, git("show", f"HEAD:MANET/provisioning/{name}"))
        entry = zipfile.ZipInfo("manet_release.py")
        entry.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(entry, git("show", "HEAD:MANET/node_tools/manet_release.py"))
    paths.append(bundle)
    # GitHub replaces spaces in uploaded filenames. Keep public asset names
    # portable while retaining the familiar launcher name inside the ZIP.
    for source, name in (("flash-a-radio.sh", "flash-a-radio.sh"),
                         ("Flash a Radio.cmd", "Flash-a-Radio.cmd")):
        path = output / name
        path.write_bytes(git("show", f"HEAD:MANET/provisioning/{source}"))
        paths.append(path)
    assets = {}
    uploads = {}
    for path in paths:
        assets[path.name] = {"size": path.stat().st_size, "sha256": digest(path)}
        uploads[path.name] = path
        checksum = output / (path.name + ".sha256")
        checksum.write_text(f"{assets[path.name]['sha256']}  {path.name}\n")
        uploads[checksum.name] = checksum
    manifest = validate_manifest({"schema": 1, "version": version, "tag": tag,
                                  "commit": commit, "assets": assets})
    manifest_path = output / "manet-release.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    uploads[manifest_path.name] = manifest_path
    return manifest, uploads


class GitHub:
    def __init__(self):
        self.token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            result = subprocess.run(["git", "credential", "fill"],
                                    input=b"protocol=https\nhost=github.com\n\n",
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), timeout=30)
            fields = dict(line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line)
            self.token = fields.get("password")
        if not self.token:
            raise ValueError("GitHub credentials are required: set GH_TOKEN or configure Git HTTPS authentication")

    def request(self, path, method="GET", data=None, file=None):
        url = path if path.startswith("https://") else API + path
        if urlsplit(url).hostname not in ("api.github.com", "uploads.github.com"):
            raise ValueError("Refusing to send credentials to an unexpected host")
        headers = {"Authorization": "Bearer " + self.token,
                   "Accept": "application/vnd.github+json", "User-Agent": "manet-publisher",
                   "X-GitHub-Api-Version": "2022-11-28"}
        body = None
        if file is not None:
            body = file.read_bytes()
            headers["Content-Type"] = "application/octet-stream"
        elif data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        with urlopen(Request(url, data=body, headers=headers, method=method), timeout=600) as response:
            result = response.read()
        return json.loads(result) if result else None

    def releases(self):
        values = []
        for page in range(1, 101):
            batch = self.request(f"/releases?per_page=100&page={page}")
            values.extend(batch)
            if len(batch) < 100:
                return values
        raise ValueError("Too many releases to clean up safely")


def cleanup_candidates(releases, keep=3):
    candidates = [r for r in releases if r.get("prerelease") is True and not r.get("draft")
                  and TAG_PATTERN.fullmatch(r.get("tag_name", ""))
                  and r.get("published_at")
                  and any(a.get("name") == "manet-release.json" for a in r.get("assets", []))]
    return sorted(candidates, key=lambda r: (r["published_at"], r["id"]), reverse=True)[keep:]


def cleanup(client, apply=False):
    candidates = cleanup_candidates(client.releases())
    for release in candidates:
        print(f"{'Deleting' if apply else 'Would delete'} prerelease {release['tag_name']}", flush=True)
        if apply:
            # Re-read just before deletion so a release promoted since listing
            # is retained. Tags and source history are deliberately preserved.
            current = client.request(f"/releases/{release['id']}")
            if current.get("prerelease") is True and not current.get("draft"):
                client.request(f"/releases/{release['id']}", "DELETE")
    if not candidates:
        print("No old prereleases to remove")


def verify_upload(asset, path):
    if (asset.get("name") != path.name or asset.get("size") != path.stat().st_size
            or asset.get("digest") != "sha256:" + digest(path)):
        raise ValueError(f"GitHub upload verification failed for {path.name}")


def publish(client, manifest, uploads, notes, stable=False, replace_draft=False):
    tag = manifest["tag"]
    existing = next((r for r in client.releases() if r["tag_name"] == tag), None)
    if existing and not existing["draft"]:
        raise ValueError(f"{tag} is already published; increment the version for new tarballs, or use promote")
    title = f"MANET {manifest['version']}" + (" (stable)" if stable else " (prerelease)")
    if existing:
        release = existing
        if replace_draft:
            current = client.request(f"/releases/{release['id']}")
            if not current["draft"]:
                raise ValueError("The release has already been published")
            for asset in current["assets"]:
                client.request(f"/releases/assets/{asset['id']}", "DELETE")
            release = client.request(f"/releases/{release['id']}", "PATCH", {
                "tag_name": tag, "target_commitish": manifest["commit"], "name": title, "body": notes,
            })
        elif release["target_commitish"] != manifest["commit"]:
            raise ValueError("Existing draft targets a different source commit")
    else:
        release = client.request("/releases", "POST", {
            "tag_name": tag, "target_commitish": manifest["commit"], "name": title,
            "body": notes, "draft": True, "prerelease": not stable, "make_latest": "false",
        })
    upload_url = release["upload_url"].split("{", 1)[0]
    existing_assets = {a["name"]: a for a in release["assets"]}
    for name, path in uploads.items():
        asset = existing_assets.get(name)
        if asset:
            verify_upload(asset, path)
        else:
            print(f"Uploading {name}", flush=True)
            asset = client.request(upload_url + "?name=" + quote(name), "POST", file=path)
            verify_upload(asset, path)
    refreshed = client.request(f"/releases/{release['id']}")
    remote_assets = {a["name"]: a for a in refreshed["assets"]}
    if set(remote_assets) != set(uploads):
        raise ValueError("Draft has unexpected or missing assets; review before publishing")
    for name, path in uploads.items():
        verify_upload(remote_assets[name], path)
    published = client.request(f"/releases/{release['id']}", "PATCH", {
        "tag_name": tag,
        "draft": False, "prerelease": not stable, "make_latest": "true" if stable else "false",
        "name": title, "body": notes,
    })
    if (published.get("tag_name") != tag or published.get("draft") is not False
            or published.get("prerelease") is not (not stable)):
        raise ValueError("GitHub published unexpected release metadata; check the tag and channel")
    print(f"Published {published['html_url']}")
    cleanup(client, apply=True)


def promote(client, tag, fetch=download):
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError("Expected a MANET tag such as v0.551")
    release = client.request("/releases/tags/" + tag)
    if release["draft"]:
        raise ValueError("Publish the complete draft before promoting it")
    names = {a["name"] for a in release["assets"]}
    required = set(PACKAGES) | {name + ".sha256" for name in PACKAGES}
    required |= {"manet-release.json", "manet-flasher.zip", "flash-a-radio.sh", "Flash-a-Radio.cmd"}
    if not required <= names:
        raise ValueError("Release is missing required downloads")
    with tempfile.TemporaryDirectory(prefix="manet-promote-") as scratch:
        manifest_file = Path(scratch) / "manet-release.json"
        fetch(f"{DOWNLOADS}/{tag}/manet-release.json", manifest_file, 4 * 1024 * 1024)
        remote = {a["name"]: a for a in release["assets"]}
        verify_upload(remote["manet-release.json"], manifest_file)
        manifest = validate_manifest(json.loads(manifest_file.read_text()), tag)
        for name in set(PACKAGES) | {"manet-flasher.zip", "flash-a-radio.sh", "Flash-a-Radio.cmd"}:
            asset = manifest["assets"].get(name)
            if (not asset or remote[name].get("size") != asset["size"]
                    or remote[name].get("digest") != "sha256:" + asset["sha256"]):
                raise ValueError(f"Release asset does not match the manifest: {name}")
    client.request(f"/releases/{release['id']}", "PATCH", {
        "tag_name": tag, "prerelease": False, "make_latest": "true", "name": f"MANET {tag[1:]} (stable)",
    })
    print(f"Marked {tag} stable and Latest; packages are unchanged")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "publish"):
        sub = commands.add_parser(command)
        sub.add_argument("--packages", type=Path, default=ROOT / "MANET/install_packages")
        sub.add_argument("--output", type=Path, required=True, help="directory for manifest, launchers and checksums")
        if command == "publish":
            sub.add_argument("--notes-file", type=Path, required=True)
            sub.add_argument("--stable", action="store_true", help="explicitly publish a stable release and mark it Latest")
            sub.add_argument("--replace-draft", action="store_true", help="replace an unpublished draft's uploads after correcting a build")
    sub = commands.add_parser("promote")
    sub.add_argument("tag")
    sub = commands.add_parser("cleanup")
    sub.add_argument("--apply", action="store_true", help="delete older prereleases, retaining the newest three")
    args = parser.parse_args()
    if args.command in ("prepare", "publish"):
        manifest, uploads = build_assets(args.packages, args.output)
        print(f"Verified {manifest['tag']} from {manifest['commit']}: {len(uploads)} assets")
        if args.command == "publish":
            client = GitHub()
            try:
                tagged = client.request("/git/ref/tags/" + manifest["tag"])["object"]
            except HTTPError as error:
                if error.code != 404:
                    raise
            else:
                for _ in range(10):
                    if tagged["type"] != "tag":
                        break
                    tagged = client.request("/git/tags/" + tagged["sha"])["object"]
                if tagged["type"] != "commit" or tagged["sha"] != manifest["commit"]:
                    raise ValueError("The release tag already points to different source; use a new version")
            publish(client, manifest, uploads, args.notes_file.read_text(), args.stable, args.replace_draft)
    elif args.command == "promote":
        promote(GitHub(), args.tag)
    else:
        cleanup(GitHub(), args.apply)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
        print(f"Release operation failed: {error}", file=sys.stderr)
        sys.exit(1)
