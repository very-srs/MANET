# Publishing MANET releases

GitHub Releases holds the install and tools packages. Archives stay outside Git
history. The local copies remain in `MANET/install_packages/`.

Normal imaging, setup and `node-update.sh` use the full release marked **Latest**.
We use that designation for the recommended stable build. Development builds are
prereleases; they do not move Latest. `--development` selects the most recently
published MANET build, including prereleases, by publication date.

## Publish a build

Increment both `MANET/node_tools/version.txt` and `MANET/etc/manet_version.txt`.
Build all six archives and their checksum sidecars, run the checks, and commit
and push the source. A source push alone does not announce an update.

Write release notes to a text file, then run:

```bash
python3 MANET/releases/publish.py publish \
  --output /tmp/manet-release \
  --notes-file /tmp/manet-release-notes.md
```

The default is a prerelease. The publisher checks every archive's checksum and
both embedded version files. It packages the committed provisioning scripts in
`manet-flasher.zip`, adds standalone Linux and Windows launchers, and creates a
manifest with the source commit, version, file sizes and SHA-256 digests. It
uploads everything to a draft, checks GitHub's stored sizes and digests, and
publishes only when the complete set matches. An interrupted upload leaves a
draft; rerunning with the same commit and packages resumes it. To correct an
unpublished draft after changing the source or packages, use `--replace-draft`.
This discards only that draft's uploaded files. Published assets
are never overwritten by this script.

Authentication uses `GH_TOKEN`, `GITHUB_TOKEN`, or the existing Git HTTPS
credential helper, in that order. The credential needs repository Contents write
permission. It is used only by the publisher; nodes download without credentials.

`prepare` accepts the same `--output` and `--packages` options and performs the
local checks without contacting GitHub. `--packages` defaults to
`MANET/install_packages/`. Source changes must be committed first.

## Mark a tested build stable

Promote the exact prerelease that was tested:

```bash
python3 MANET/releases/publish.py promote v0.551
```

This clears the prerelease flag and marks the release Latest without rebuilding
its packages. The equivalent GitHub controls are clearing **This is a pre-release**
and selecting **Set as latest release**. Only do this for a build accepted as
stable. `publish --stable` is available when a new build should start as stable;
the initial release uses it so normal installs have a download target.

## Cleanup

After publishing, the script retains the three most recently published MANET
prereleases and deletes older prereleases and their downloads. Full releases,
drafts, unrelated releases and Git tags are retained. There is no age grace
period. Tags are small source references and do not retain deleted release assets.

Preview cleanup or run it separately:

```bash
python3 MANET/releases/publish.py cleanup
python3 MANET/releases/publish.py cleanup --apply
```

## Test the newest upload

```bash
./MANET/provisioning/flash-a-radio.sh --development
sudo node-update.sh --development
```

On Windows, run `"Flash a Radio.cmd" --development` from Command Prompt. This
selects the uploaded setup scripts as well as the install packages. Add
`--local-scripts` on either platform to test scripts from a source checkout
against the selected release packages. Neither option overwrites source files.

These choices apply to that invocation. Automatic updates continue to select
stable. They leave a newer installed development version alone; returning to an
older stable version requires `sudo node-update.sh --allow-downgrade`.

The flashers pin the selected release into the generated first-boot script.
Later uploads cannot change its download target. If an old prerelease was
deleted before that image first boots, reflash with a retained release.

The standalone Windows download is named `Flash-a-Radio.cmd` because GitHub
renames filenames containing spaces. It creates a working copy named
`Flash a Radio.cmd`, matching the launcher inside the flasher ZIP and checkout.

## One-time CM4 upgrade from 0.541

[`upgrade-0.541-to-0.559.py`](upgrade-0.541-to-0.559.py) is a standalone script
for a CM4 on the project's Debian/Raspberry Pi OS 13 image. Copy this one file
to the radio. It targets the existing stable **0.559** release; it does not use
`main`, select a prerelease, or publish a new release.

With Ethernet internet access and steady power, check the node first:

```bash
sudo python3 upgrade-0.541-to-0.559.py --check
```

This downloads the pinned tools archive, checks its SHA-256, checks installed
paths and space using the matching updater, and checks the DHCP rules against
the running kernel. It writes only a private download directory under
`/var/lib/manet-upgrade-0.559`; it does not install packages or activate services.

Run the upgrade under systemd so an SSH disconnection cannot stop it:

```bash
sudo systemd-run --unit=manet-upgrade-0559 --collect \
  /usr/bin/python3 "$(realpath upgrade-0.541-to-0.559.py)"
sudo journalctl -fu manet-upgrade-0559
```

Wait for **Upgrade complete: 0.559**, then stop following the journal with
Ctrl-C and run `sudo reboot`. The reboot starts the other newly enabled units.
After reconnecting, check:

```bash
head -n 1 /etc/manet_version.txt
systemctl is-active node-manager mesh-status mesh-channel-agreement one-shot-time-sync
```

The script obtains the three matching updater files from the verified tools
archive. The 0.559 updater installs the encryption dependency, retires obsolete
MANET units, selects the manager from `acs=`, and records success only after its
service checks pass. Subsequent normal updates use `sudo node-update.sh`.

The script saves a root-only configuration backup at
`/var/lib/manet-upgrade-0.559/configuration-before-upgrade.tar.gz`. This contains
passwords and keys; keep it private. It is a configuration backup, not a complete
rollback image. The first backup is retained on retries. The upgrade preserves
mesh/AP settings, existing HaLow channels, network definitions and operator data.
It does not run `radio-setup.sh`, change boot files, or upgrade kernel/modules.
Preserved HaLow settings must match the intended peers; this update does not
retune an older radio to the defaults used by a freshly flashed radio.

On failure, retain power and internet access, read the reported error, and rerun
the same script after correcting it. Do not delete the pending update marker.
The 0.559 updater requires a manual retry after interruption; it does not contain
the newer automatic offline recovery work. A different OS version or an update
already managed by that newer recovery system is refused before installation.

Validation uses the actual published 0.559 archive in an isolated filesystem
with a 0.541 version, copied manager, old units and preserved configuration. It
checks installation, backup retention, activation failure and retry. Service and
APT calls are simulated in that test. The same tests pass on the bench CM4, and
a check-only run using a temporary cache passed against its real filesystem and
kernel, with its newer installed version left unchanged. This is not a physical
0.541 upgrade/reboot test. The on-node `--check` handles several compatibility checks on the recipient's
actual kernel and filesystem, but cannot prove radio connectivity after reboot.
