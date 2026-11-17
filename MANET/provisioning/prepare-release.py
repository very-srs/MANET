#!/usr/bin/env python3
"""Print the pinned install version, URL and digest for a flasher, then the
offline bundle's URL and digest (empty lines when the release has none)."""
import json
from pathlib import Path
import sys

# The release bundle carries this module alongside the provisioning scripts.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "node_tools"))
from manet_release import BOARDS, asset_url, validate_manifest


def main():
    manifest = validate_manifest(json.loads(Path(sys.argv[1]).read_text()))
    board = sys.argv[2]
    if board not in BOARDS:
        raise ValueError("Unknown board")
    filename = f"{board}-install.tar.gz"
    print(manifest["version"])
    print(asset_url(manifest, filename))
    print(manifest["assets"][filename]["sha256"])
    offline = f"{board}-offline.tar"
    if offline in manifest["assets"]:
        print(asset_url(manifest, offline))
        print(manifest["assets"][offline]["sha256"])
    else:
        print()
        print()


if __name__ == "__main__":
    main()
