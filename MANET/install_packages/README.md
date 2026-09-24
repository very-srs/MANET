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

The tools updater requires the checksum, checks the archive's version against
GitHub, and stages the files before installation. Install archive checksums are
available for manual verification with `sha256sum -c <archive-name>.sha256`;
first-boot provisioning does not yet enforce them.

Upload and verify the archive/checksum pairs before pushing the matching version
bump to GitHub. No separate version file is uploaded to the download server.
If copying an identical tools archive to another board's filename, regenerate
its checksum file with that board's basename; the updater checks the name too.
