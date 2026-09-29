# Install packages

Two kinds of archive are published here, one pair per board.

Keep completed local builds and their checksum sidecars in this directory.
Temporary directories can be used for building and verification; copy verified
results here, preserving older packages before replacement and updating any
matching entries in `SHA256SUMS`. Local placement does not publish a release.

`*-tools.tar.gz` updates a node that is already running. `node-update.sh`
fetches one and extracts it. It carries the node scripts, systemd units and
supporting files, and no kernel.

`*-install.tar.gz` sets up a new node. It carries everything the tools archive
does, plus the kernel, device trees, modules and radio firmware for that board.
First-boot provisioning downloads it by exact filename, so these are not
renamed.

Every builder also produces `<archive-name>.sha256` in `sha256sum` format.
Upload both files together, keeping the exact filenames. For example:

```text
cm4-tools.tar.gz
cm4-tools.tar.gz.sha256
```

The updater requires the checksum and verifies the archive against its release
manifest before staging it. First-boot provisioning also checks the install
archive's SHA-256 and embedded version before extraction.

Increment both source version files for every newly published package set.
Build all three boards' tools/install archives and sidecars, commit and push the
source, then publish the complete set as a GitHub prerelease. Source pushes no
longer announce an update. Normal installs and updates follow the stable release
marked Latest; testing uses an explicit `--development` option.

The publisher creates a draft, uploads and verifies all assets, then publishes
it. It keeps the newest three prereleases and preserves stable releases. See
[Publishing MANET releases](../releases/README.md) for publishing, promotion and
cleanup commands. Tarballs remain outside Git history.

If copying an identical tools archive to another board's filename, regenerate
its checksum file with that board's basename; the updater checks the name too.
