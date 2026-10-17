#!/usr/bin/env python3
"""Fetch pinned kernel inputs and apply the MANET Morse driver patch."""

import argparse
import json
import os
from pathlib import Path
import subprocess


REPO = Path(__file__).resolve().parents[2]


def git(path, *args, capture=False):
    """Run Git in an input checkout and propagate failures."""
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
    )


def prepare(work, entry, check_only):
    """Obtain one pinned revision without resetting an existing checkout."""
    path = work / entry["path"]
    revision = entry["commit"]
    if not (path / ".git").exists():
        if check_only:
            raise ValueError(f"Missing {path}. Run prepare-sources.py first.")
        if path.exists() and any(path.iterdir()):
            raise ValueError(f"Refusing nonempty source directory: {path}")
        path.mkdir(parents=True, exist_ok=True)
        git(path, "init", "--quiet")
        git(path, "remote", "add", "origin", entry["url"])

    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
        text=True, capture_output=True,
    )
    if head.returncode:
        if check_only:
            raise ValueError(f"No pinned revision checked out in {path}")
        # A previous interrupted fetch can resume in the empty repository.
        if git(path, "status", "--porcelain", capture=True).stdout:
            raise ValueError(f"Refusing files in an uninitialized tree: {path}")
        git(path, "fetch", "--depth=1", entry["url"], revision)
        git(path, "checkout", "--detach", revision)
    elif head.stdout.strip() != revision:
        raise ValueError(
            f"Unexpected revision in {path}. Expected {revision}. "
            "Use a separate MANET_KERNEL_WORK directory for this version."
        )

    if "patch" in entry:
        patch = REPO / entry["patch"]
        applied = subprocess.run(
            ["git", "-C", str(path), "apply", "--reverse", "--check",
             str(patch)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
        if not applied:
            if check_only:
                raise ValueError(f"Morse patch missing or changed in {path}")
            git(path, "apply", "--check", str(patch))
            git(path, "apply", str(patch))
    print(f"Ready: {entry['path']} at {revision}")


def main():
    """Prepare selected board inputs, or check them without network access."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", choices=("cm4", "r3a", "rpi5", "all"),
                        default="cm4")
    parser.add_argument("--check", action="store_true",
                        help="verify revisions and Morse patch without writes")
    args = parser.parse_args()
    work = Path(os.environ.get("MANET_KERNEL_WORK", REPO / "kernel-work"))
    sources = json.loads(Path(__file__).with_name("sources.json").read_text())
    try:
        for entry in sources.values():
            if args.board == "all" or args.board in entry["boards"]:
                prepare(work.resolve(), entry, args.check)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"prepare-sources: {error}\n")


if __name__ == "__main__":
    main()
