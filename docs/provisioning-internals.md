# Provisioning internals

How the flashers build an image: the template tokens, where the substitution
lists live, and how operator setup scripts are embedded. Operator-facing
instructions are in
[`MANET/provisioning/README.md`](../MANET/provisioning/README.md).

This file is for someone developing on the project. It is not user
documentation.

---

### Template tokens

`linux-flasher.sh` and `windows.ps1` bake mesh settings into the image by substituting
`__TOKEN__` placeholders in those templates at flash time. Edit the templates
using the tokens, not concrete values; a leftover `__MESH_SSID__` in a flashed
image means the substitution list in the flasher was not updated.

Tokens (same set on Linux and Windows):

`__HARDWARE_MODEL__` `__EUD_CONNECTION__` `__LAN_AP_SSID__` `__LAN_AP_KEY__`
`__MAX_EUDS_PER_NODE__` `__INSTALL_MEDIAMTX__` `__INSTALL_MUMBLE__`
`__VOICE_ENABLED__` `__MESH_SSID__` `__MESH_SAE_KEY__` `__LAN_CIDR_BLOCK__`
`__AUTO_CHANNEL__` `__RADIO_USER__` `__RADIO_PW__` `__REGULATORY_DOMAIN__`
`__HALOW_REGULATORY_DOMAIN__` `__ADMIN_PW__` `__AUTO_UPDATE__`
`__RELEASE_VERSION__` `__INSTALL_URL__` `__INSTALL_SHA256__`

Adding a new flash-time setting means the token in both templates **and** the
`sed` / `-replace` list in both flashers. The lists live in `linux-flasher.sh`
(`flash_rpi` and the Rock 3A path) and in `windows.ps1`, in
`Expand-ProvisioningTokens`, which is one list covering the Raspberry Pi path,
the Rock 3A path, and the window.

The window adds no third list. It writes the same `.mesh-configs/*.conf` file
that `windows.ps1` and `flash-a-radio.sh` already share, then calls
`Build-ProvisioningScript`, so a setting added in the two places above reaches
it with no further work.

`RADIO_USER` is the SSH login name in saved flasher configurations. Loading a
configuration without that key resets it to `radio`, even after a custom
account was previously loaded. Both host engines validate usernames before
rendering `__RADIO_USER__`. Raspberry Pi first boot creates the account with
`useradd`, while the Rock 3A host path writes the account into the mounted
image and refuses name or UID/GID collisions.

Both templates persist the selected name as `ssh_user` in `/etc/mesh.conf`.
Runtime setup, SSH recovery, Syncthing, and Mumble resolve that local account
and its home directory. It is a flash-time account choice, not an account
rename operation on a running radio and not a mesh-wide setting. The SSH
password is installed into the account database, not added to `mesh.conf`.

Scripts from `additional-scripts/` are inserted **after** substitution and are
never token-substituted, so a script containing a literal `__ADMIN_PW__`
retains it.

They are inserted at an anchor rather than appended. Both templates carry the
line:

```
# >>> MANET_ADDITIONAL_SCRIPTS <<<
```

The flashers replace that line with the generated heredocs, and remove it when
there is nothing to embed. An anchor is required because neither template
executes to its final line: `firstrun.sh.template` ends with completion
messages and `rock3a-provision.sh.template` ends with `reboot`, so an appended
block would never run. A template with the anchor removed causes the flasher to
abort rather than produce an image whose scripts have no effect.

---

## What each launcher fetches

Both launchers select a published release before downloading setup code. Stable
reads `/releases/latest/download/manet-release.json`; development lists published
releases and selects by `published_at` and release ID, excluding drafts and
unrelated releases without a MANET manifest. Both then use the selected tag's
fixed download URL for `manet-flasher.zip` and verify its size and SHA-256.

The publisher builds the ZIP from committed provisioning sources, with a copy
of `manet_release.py`. `MANET/releases/publish.py:PROVISIONING` lists its inputs.
The Linux launcher runs `linux-flasher.sh`; Windows runs the GUI or console
engine. `MANET_RELEASE_FILE` passes the selected manifest, and
`MANET_FLASHER_WORK` keeps saved settings, downloaded images, tool paths, and
operator setup scripts outside the temporary extracted bundle. No source files
are overwritten. `--local-scripts` explicitly runs a checkout's scripts against
the selected published packages.

The Linux launcher passes its embedded Python program with `-c`, preserving
stdin for the interactive engine. The Windows launcher carries its PowerShell
bootstrap below the batch entry point and passes channel options through UAC.
Network or verification failures stop startup; there is no fallback to a
cached build from a different channel.

At flash time `prepare-release.py` (Linux) and `Expand-ProvisioningTokens`
(Windows) insert the install URL, digest, and version. First boot checks all three
before extraction. The selected package cannot drift to another release between
imaging and first boot. Deleted prereleases require reflashing old test images.

---

## Offline setup

An offline card carries everything a CM4 first boot otherwise downloads, so
the radio provisions with no network at all. It holds two release files in the
root of its boot partition: the board's install package and the board's
offline bundle. Only `cm4` has a bundle, which also serves the Raspberry Pi 4B.
Without the option the flashers run the original flow, and the templates find
nothing on the card.

### The offline bundle

`MANET/packaging/build-offline-bundle.py` builds `cm4-offline.tar`, an
uncompressed tar with `debs/*.deb` and an `apt-ftparchive` `debs/Packages`
index, `yq_linux_arm64`, the MediaMTX archive, a `VERSION` copy of the version
files, and `os-image.json` recording the Raspberry Pi OS image it was built for.

The packages are resolved against the exact image both flashers write. The
builder reads `PI_OS_IMAGE_URL` from `linux-flasher.sh` and refuses to run if
`windows.ps1` names a different one. It downloads that image, checks it against
the `.sha256` Raspberry Pi publishes beside it, and loop-mounts its root
partition. apt's lists and downloads go to bind-mounted folders outside the
image, so the image needs no resizing. In a qemu-user chroot it installs the
`00-manet-kernel` preferences and runs `manet-kernel-protect.py --board rpi4`,
as first boot does before its first apt command, so the resolver cannot pull a
kernel. Then it runs `apt-get install --download-only` for each provisioning
install in order. `install_steps()` reads those from `firstrun.sh.template`
(the main list without recommends, then `mumble-server` with recommends) and
from the two `install_packages` calls in `radio-setup.sh`. A fifth install site
stops the build until the builder knows about it. yq and MediaMTX are
downloaded from their releases and checked against the template pins.

The bundle for the current image holds 163 packages, about 175 MiB with yq and
MediaMTX. The install removes `systemd-timesyncd` (chrony replaces it) and
upgrades 32 base packages, as an online first boot does.

### Release records

`publish.py` requires `cm4-offline.tar` beside the package archives.
`check_offline_bundle` checks its sidecar, that `VERSION` matches the committed
version files, that `os-image.json` names the image in the committed
`linux-flasher.sh`, that yq and MediaMTX match the committed template pins, and
that a package repository is present. The manifest gains the asset and an
`os_image` record (`url`, `sha256`, `size`). `validate_manifest` requires that
record whenever an offline bundle is listed and checks that the URL is a
Raspberry Pi download. Older releases have neither and still validate.
`promote` verifies the bundle when the manifest lists one.

### Kits

`--make-kit DIR` (both launchers) selects the stable or development manifest and
fills `DIR` with `manet-flasher.zip`, both launchers, `cm4-install.tar.gz`,
`cm4-offline.tar`, the image named by `os_image`, and the manifest. Every file
is checked against its size and digest, a download goes to `.partial` first,
and a verified file already present is kept, so a second run resumes. The
marker `MANET-OFFLINE-KIT.txt` is removed at the start and written last, so a
half-filled folder is not taken for a kit. The Windows launcher makes kits
before its UAC relaunch, since a folder needs no administrator access. Both
write the same kit, so either computer can use it.

A launcher that finds the marker in its own folder, or the folder above (its
`manet-flasher` or `MANET Flasher` working copy), runs from the kit. It reads
the kit's manifest, checks the flasher ZIP and image against it, and starts the
engine with `MANET_OFFLINE=1`, `MANET_KIT_DIR`, and `MANET_OS_IMAGE`. Nothing
in that path calls curl or `Invoke-WebRequest`. `--development` and
`--local-scripts` are refused, since a kit is fixed to one release. A missing
host tool stops the run with a list of what to install. The Linux preflight
does not offer its package-manager install, and the Windows prerequisites page
does not download installers. The window refuses Rock 3A, which would need the
Armbian image and a network.

### Flasher side

`--offline` reaches the engines as `MANET_OFFLINE=1`. The Windows launcher sets
it in batch and passes `--offline` again through the UAC relaunch, because an
elevated process does not inherit the caller's environment. The GUI seeds its
confirmation-page checkbox from it, keeps it ticked and locked in a kit, and
clears and disables it for boards without a bundle. Both engines refuse other
boards straight after board selection.

`prepare-release.py` and `Get-ReleaseInstallAsset` return the install asset and,
when the release has one, the offline asset. `fetch_release_file` and
`Get-ReleaseFile` take a file from the kit, or keep one verified copy per board
in `packages/` under `MANET_FLASHER_WORK`, downloading to `.partial` and
renaming only after the digest matches. The offline digest fills the templates'
`__OFFLINE_SHA256__`, which is empty on every other flash. With
`MANET_OS_IMAGE` set, the engines give rpi-imager the kit's image file in place
of the URL, after checking that its name matches the one they would download.

The copy happens after rpi-imager exits:

1. The imager's eject would take the card away first, so the engines pass
   `--disable-eject` when the imager accepts it. Linux asks the binary the
   same way `imager_state_of` does, with no device, and treats an
   `Unknown option` reply as unsupported. Windows decides from the file
   version without running the program. It passes the flag only to a build
   whose file version is 2 or later, or to `rpi-imager-cli.cmd`, which only
   2.x ships, and refuses offline setup with any other imager. Passing the
   flag to 1.x would fail the whole run.
2. Linux rereads the partition table, takes the card's first partition, and
   requires `blkid` to report `vfat`. Windows takes the lowest-numbered
   partition, adds a drive letter if it has none, and requires a FAT volume.
3. Both require free space for the files plus 32 MiB, which covers the kernel
   and overlay files (about 12 MB) that first boot extracts onto the same
   partition while the files are still there. The stock image leaves about
   437 MB free.
4. Each file is written as `<name>.partial`, renamed, and hashed on the card
   against the digest from the manifest. That catches a cached file replaced
   since it was checked, for example by a second flasher sharing `packages/`.
   If any file fails, every file and partial is removed again. The volume is
   then flushed and unmounted (Linux) or the added drive letter is removed
   (Windows).

A card that does not get both files is a failed flash, because it cannot set
itself up offline. Linux stops the run. Windows reports the card as failed.
Linux keeps the automount hold from `flash-target.py` in place during the copy,
so the desktop does not mount the partition underneath it.

### Node side

The readiness block runs `bootstrap_local_package` and `bootstrap_offline_bundle`
before it waits. Each looks for its file under `/boot/firmware/` and then
`/boot/`, and accepts it only if its SHA-256 matches the digest pinned in the
script. The offline bundle's name is the install asset's with `-install.tar.gz`
replaced by `-offline.tar`. With an empty `__OFFLINE_SHA256__` the bundle is
never considered. A matching install package alone drops the GitHub release
from the resources the gate waits for. With both, the gate does not run, and no
probe or clock request is sent.

The package step hard-links the card copy to `/root/morse-pi-install.tar.gz`,
or copies it when the two are on different filesystems, as they are on a
Raspberry Pi. The usual digest and version checks follow.

After kernel protection, the Raspberry Pi template's offline block unpacks the
bundle into `/var/lib/manet-offline`, checks that `VERSION` matches the selected
release, and writes two files. `/etc/apt/manet-offline.list` names the bundle as
a `[trusted=yes]` file repository. `/etc/apt/apt.conf.d/99manet-offline` points
`Dir::Etc::SourceList` at that list, `Dir::Etc::SourceParts` at an empty
folder, and turns translations off. That one drop-in moves every apt command to
the bundle, including radio-setup's `install_packages` and the admin and voice
setup units, without changing them. The bundle carries no repository signature.
Its integrity comes from the pinned digest, and its packages were fetched with
apt's signature checks when the bundle was built. The yq and MediaMTX steps copy
from the unpacked bundle when it is there and keep their pinned digest checks.

radio-setup's `have_package_network` returns at once while the drop-in exists.
After radio-setup records `complete`, it removes the drop-in, the list, and
`/var/lib/manet-offline`, so apt returns to the image's own sources. When a
retry finds the card files missing or damaged and falls back to downloading,
the offline block removes any drop-in and bundle an earlier offline attempt
left, so apt uses the image's sources and `have_package_network` checks the
network again. The card copies are removed at the end of the earlier
provisioning stage, after `mesh-provision` is disabled and a `sync` has written
that to disk. A power cut before that point leaves the stage enabled with the
files in place, and a cut after it leaves only the space they take. A run that
fails earlier keeps everything in place for the retry. The readiness,
package, yq, MediaMTX, and cleanup blocks are identical in both templates. The
offline apt block exists only in `firstrun.sh.template`.

### Tests

`test_first_boot_downloads.py` runs the readiness gate with both files on the
card in each location, with the bundle alone, unpinned, or mismatched. It also
runs the offline apt block against a fixture bundle and reads the drop-in back
with `apt-config`, and covers yq and MediaMTX from the bundle and radio-setup's
offline check. `test_offline_bundle.py` covers the builder's install steps,
`check_offline_bundle`, and the manifest record. `test_release_provisioning.py`
makes and uses kits with both launchers and fails any network use from a kit,
runs the flasher functions against stand-in block-device tools, and checks that
the imager arguments are unchanged without the option.

---

## First boot without a trusted clock

### Bounded resource readiness

Both templates use the same readiness block before the logging-clock fetch,
archive download, and APT transactions. It sends bounded HTTP HEAD requests to
`deb.debian.org/debian/dists/trixie/InRelease`, the exact selected MANET release
asset unless a matching copy is on the card, and the pinned MediaMTX release
asset when selected. A card with both offline files skips the gate entirely
(see [Offline setup](#offline-setup)). Redirects are
followed through GitHub's download hosting. The HTTP Date endpoint is also
probed but remains optional, matching the following best-effort clock step.
There is no ICMP requirement. Success means the required resource endpoints
returned HTTP 200, not a guarantee that later downloads cannot fail.

HEAD probes discard their responses and ignore TLS certificate validity while
the clock may be wrong. Actual artifacts still require the pinned SHA-256 and
version checks below, and APT still authenticates its repositories. The gate
uses `/proc/uptime` for a five-minute total budget, including requests and
retry sleeps. Each request gets at most ten seconds, or the remaining budget
if smaller, with a three-second connection timeout. Clock corrections cannot
extend the wait.

Before waiting, the templates create the standard provisioning state and an
early standalone status reader if the archive has not supplied the full one.
The MOTD symlink therefore works on the base image. Timeout records
`STATE=incomplete`, `PHASE=bootstrap-network`, timestamps, and the platform's
log path, plus a failure list naming resources. PWR/its board alias uses a
100 ms on/off timer while waiting. Timeout uses the existing red heartbeat
and green solid failure pattern, with missing LEDs tolerated.

Failure exits before disabling `mesh-provision.service`. On Rock, the
success-only `ExecStartPost` also leaves `.mesh-not-provisioned` in place.
Restoring access and rebooting retries the same stage, or an operator can
restart that service directly. The banner recommends this early-stage retry
instead of invoking the not-yet-installed radio setup. A successful retry
clears the old failure list and records running status until later setup
records its final verdict.

### Kernel package protection

After verifying the install archive's checksum and version, both templates
extract only `etc/apt/preferences.d/00-manet-kernel` and
`usr/local/bin/manet-kernel-protect.py`, then run the helper before the first
APT command. Missing files or a failed helper abort provisioning. The full
archive is still extracted after package setup. Rock 3A's initial
`apt upgrade -y` therefore runs with the pins, holds, and hook guards installed.
The helper waits for APT's frontend lock while it changes hooks, so it does
not replace an entry point underneath an existing package transaction.

The policy and helper are required inputs to every install/tools builder.
`stage_kernel_protection` runs after the SBC overlay is staged, so an older
overlay cannot replace the current policy. No new flasher token is needed.
The existing hardware token selects Pi or Rock 3A hook protection. See
[kernel ownership and recovery](kernel-6.18-morse-port.md#81-apt-owns-userland-manet-releases-own-the-kernel).

### Download verification

The install archive's URL, version, and SHA-256 come from the selected release
manifest through the existing `__INSTALL_*__` and `__RELEASE_VERSION__`
tokens. Both templates also carry identical `YQ_VERSION`, `YQ_SHA256`,
`MEDIAMTX_VERSION`, and `MEDIAMTX_SHA256` constants. These constants are baked
into the image with the template. `test_first_boot_downloads.py` asserts the
pins and download/verification blocks agree between templates, and exercises
the actual Linux rendering commands. The Windows rendering test checks that
all four constants survive substitution too.

No new flasher tokens or release bundle inputs are needed. To update yq or
MediaMTX, obtain the new artifact's SHA-256 from that project's published
release checksums, then change both its version and digest in both templates.
Update the expected pins in `test_first_boot_downloads.py` and run the
provisioning tests. yq uses `yq_linux_arm64`; MediaMTX uses
`mediamtx_<version>_linux_arm64.tar.gz`. The current pins are yq v4.54.1 and
MediaMTX v1.15.3.

These three artifact downloads use `curl --insecure`. Each restricts
both the original URL and redirects to HTTPS, and checks SHA-256 before use.
yq downloads to a temporary file before `install`; MediaMTX downloads and
extracts inside a private temporary directory. A failed fetch or hash check
exits immediately. There are no curl/wget configuration files or exported
settings that could weaken verification for operator scripts or later tools.
The readiness HEAD probes above also ignore certificate validity but discard
all data and never install their responses.

The test's unverified artifact allowlist is empty. Two other network fetch
categories are explicitly accounted for: APT's changing repository metadata
and packages use repository signatures and package hashes, while the HTTP
HEAD request to `http://google.com` supplies only a logging timestamp. Neither
uses a hash baked into the image. Pinning the Date response would prevent it
from providing the current time; pinning all APT responses would require a
separate frozen repository and dependency manifest. The Date request has a
timeout, and a missing or rejected response does not stop provisioning.

Each provisioning process writes `/etc/apt/apt.conf.d/99no-time-checks.conf`
with `Acquire::Check-Date "false";` before its first APT command. Raspberry Pi
has two processes: the imager's wrapper and its generated post-network
script. Each installs its own exit/signal cleanup and recreates the override
on retries. Explicit cleanup happens before disabling the provisioning
service, staging operator scripts, or rebooting, as appropriate to the phase.
An uncatchable termination or power loss can leave the file until the next
provisioning attempt; that attempt recreates and later removes it.

`Acquire::Check-Date` disables release metadata date and expiry checks. It
does not disable package signature checks or HTTPS certificate validation
for an APT source using HTTPS. The image's APT sources must remain usable
without a trusted clock; custom HTTPS repositories may require a correct
clock. See Debian 13's
[apt.conf(5), Acquire group](https://manpages.debian.org/trixie/apt/apt.conf.5.en.html#THE_ACQUIRE_GROUP).
Once provisioning completes, normal date and certificate validation applies
again, including to operator setup scripts, node updates, and manual installs.

## CM4 disk enumeration

The Linux CM4 path takes a disk inventory before `rpiboot` and polls afterward
for at most sixty seconds. One new writable disk must report the same nonzero
size in two consecutive one-second observations. Multiple new disks are an
error, even if one is still reporting zero size. `lsblk` failures stop the flow;
existing disks and loop devices cannot become the automatic target.

`flash-target.py` is included in the release flasher bundle. It checks the whole
disk and all descendant devices, rejects active mapped storage and mounts outside
`/media` or `/run/media`, and reports disk identity for confirmation. The selected
fingerprint includes name, major/minor number, capacity, model, serial, WWN, and
the kernel disk sequence when available. `prepare` rechecks it, unmounts allowed
desktop mounts, then rechecks identity and mount state. Linux calls it before
wipefs, dd, and rpi-imager.

Desktop automounting raced the unmount. GNOME reads each new mount for a few
seconds, so `umount` failed with "target is busy", and an ext4 mount doing
journal recovery could finish after the unmount, leaving the old rootfs mounted
read-write while rpi-imager wrote the new image. Two changes close this. A udev
rule in `/run/udev/rules.d/99-manet-flash-noauto.rules` sets `UDISKS_AUTO=0`,
which udisks and GNOME honor: `hold-cm4` installs it for the rpiboot mass
storage device (USB `0a5c:0001`) before `rpiboot` runs, so a CM4 eMMC is never
automounted, and `prepare` installs it for the chosen disk. The flasher removes
the file on exit. `prepare` then retries the unmount for up to 30 seconds and
requires the disk to open with `O_EXCL` on two checks a second apart. A mount
still in progress already claims the device, though `lsblk` does not show it
yet. If the disk stays busy, the error names the processes using the mount. Batch flashing requires typed confirmation for each
target and blank input never authorizes a write. These checks are tested with
simulated inventories; physical unplug/replug behavior still needs a bench test.

## Finding a usable rpi-imager

The flasher passes `--first-run-script`, which is the only way the generated
setup script reaches the card. Builds before 1.8 do not have that option and
reject it *after* the image has been written, so the card looks flashed and
boots a stock Raspberry Pi OS. Ubuntu 22.04 still ships 1.7.2.

Capability is tested, not inferred from the version, and the test has three
outcomes rather than two:

| Outcome | How it shows |
|---|---|
| usable | takes the option |
| too-old | `Unknown option 'first-run-script'` |
| broken | dies in the dynamic loader before parsing anything |

The third case is why a version comparison alone is not enough. Ubuntu 24.04's
1.8.5 package is new enough on paper, and on a 22.04 host it will not start at
all because it wants a newer `GLIBCXX`. A version check reads that as fine. The
probe passes `--first-run-script` with no device, so the option parser rejects
it long before anything could be opened.

`resolve_rpi_imager` takes the first match:

1. a copy in the working folder, when it is *strictly* newer than the system
   one, on the assumption it was put there deliberately
2. the system `rpi-imager`, when the probe says it works
3. the package manager, when its candidate is 1.8.0 or later
4. the AppImage from raspberrypi.org, unpacked once into the working folder

Nothing replaces a working system install. That rule exists because doing it
once, from a non-interactive test where an empty answer took the `[Y/n]`
default, removed a working 1.9.6 from a machine and left 1.7.2 in its place.

The AppImage is unpacked with `--appimage-extract` rather than run directly.
That costs about 110 MB in the folder and avoids FUSE entirely, so no root is
needed to set it up and no re-extraction happens per flash. Raspberry Pi
publishes it for x86_64 only, with no arm64 `.deb` or AppImage, so an arm64
host falls back to its distribution's package or to Flathub, which does build
`org.raspberrypi.rpi-imager` for aarch64.

No checksum is published next to the AppImage, so the download is judged by
whether it runs and accepts the option. Two version banner formats exist and
both print on stderr: `rpi-imager version 1.7.2` on 1.x, and
`Raspberry Pi Imager v2.0.11.1` on 2.x.
