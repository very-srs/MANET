# Update a radio

## Marine

![Crayon drawing of a software toolbox downloading into a powered radio connected to a router, followed by the radio beside a clock with both cables still connected.](../assets/updates-crayon.png)

### Update. Keep the cables in.

With automatic updates on, your radio does a little peek at the internet to
check if there is an update for its software.

Without automatic updates, you can ask it to do that yourself:

1. **Power and internet.** Leave both connected.
2. **Update this radio.** Over SSH, run `sudo node-update.sh`.
3. **Wait.** Let every update step and restart finish, then check that your radios can still talk.

## User

### The normal update follows stable releases

MANET's tools updater uses the GitHub release marked **Latest**, which is the stable release. It checks the downloaded package before installing it. Changes pushed to the source repository or this guide do not by themselves announce a radio update.

The update archive can carry any component a release needs, including kernels,
drivers, firmware, and configuration changes. The name **tools** does not limit
its contents. Radios may be sealed in enclosures, so updates keep them working
in place without reflashing.

### New kernels on CM4 radios

A CM4 release can carry a new kernel along with the tools. The update installs
the tools first, then restarts the radio once to try the new kernel. Before
keeping it, the radio checks its own startup, drivers, attached radios, and
mesh interface. It does not need Ethernet, internet, another radio, or a set
clock for this check. The tools version shown can change before the kernel
check finishes. A radio that already runs the release's kernel updates its
tools without the extra restart.

If the check fails, the radio returns to its previous kernel by itself and never
tries that kernel again. If the new kernel cannot start at all, the firmware
falls back to the old one on the next restart. A sealed radio may need one power
cycle for that, so keep access to its power supply.

Radios flashed before mid-October still have a Raspberry Pi setting,
`auto_initramfs=1`, in their boot files. The update tries the new kernel
without changing that file, and only updates the setting once the new kernel has
passed its check. You do not need to edit boot files or reflash. A radio whose
startup really needs an initramfs, such as an encrypted or network root,
keeps its old kernel and the updater says why.

Rock 3A and RPi5 radios update their tools only.

To turn off kernel updates on a CM4, run `sudoedit /etc/manet-kernel-update.conf`
and save `enabled=n`. Tools updates still work. Save `enabled=y` to allow kernel
updates again. A kernel check already in progress finishes or falls back first.
This setting stays on this radio and is separate from automatic update checks.

### Update one radio manually

1. Give the radio working internet access and leave it powered. Wait for its first verified clock synchronization. **Mesh recovery** in the dashboard shows clock information.
2. Connect over SSH with the account and hostname chosen during flashing.
3. Run:

```bash
sudo node-update.sh
```

4. Wait for the result. Services restart during installation, so the dashboard and radio links may briefly disappear. Sign in to management again if needed.
5. Confirm the installed version and check the dashboard, expected peers, and any services you use, such as voice.

To read the local version and recent updater messages:

```bash
cat /etc/manet_version.txt
sudo journalctl -t manet-update -n 60 --no-pager
```

Run the update on each radio you intend to update. Updating one node does not install software on its peers.

### Automatic updates

Enable **automatic updates** through [Node config](settings.md). An opted-in
radio queues a stable check when its own internet connection becomes ready,
when it first gains an internet route through a mesh gateway (another radio
sharing internet), and when its clock first synchronizes. It needs a route
and a synchronized clock before checking. Changing from one mesh gateway to
another does not trigger an extra check.

A daily timer also queues a check, with a random delay of up to two hours,
and catches a missed scheduled check after the radio powers back on. Checks
through a mesh gateway wait another random 0–15 minutes to spread downloads
among radios sharing that connection. A radio with its own internet uplink
does not add that mesh delay. Turning automatic updates off prevents these
checks even though the timer remains enabled.

Routine checks normally skip another check for 24 hours after a successful
check or installation. The daily schedule can therefore skip a day if its
check falls inside that window. An explicit manual run does not use that
routine throttle.

### Trying a development build

For a deliberate test of the newest published build, including prereleases:

```bash
sudo node-update.sh --development
```

That choice affects this invocation only. Automatic checks still select stable, and they leave a newer installed development version alone. To deliberately replace it with an older stable tools release:

```bash
sudo node-update.sh --allow-downgrade
```

Check the intended release before downgrading. This does not restore an earlier operating system or kernel.

### If an update is interrupted

Once the updater has saved its recovery package, a later attempt or reboot resumes that same package. To retry only a pending installation, without selecting or downloading a new release:

```bash
sudo node-update.sh --recover-only
```

Local recovery works without internet or clock synchronization. If the recovery data is missing or corrupt, retain it and collect the error for diagnosis. Successful recovery finishes the interrupted installation.

If the same release fails to install three times, the radio goes back to the last release that installed cleanly on it and skips the failed release until a newer one is published. A newer release also replaces a stuck one at the next daily check. See [troubleshooting](troubleshooting.md) for what to collect.

## Developer

### Update scope and release identity

Treat the archive as the general field-update mechanism. Any required component
may be included in a release. Match its installation, activation, and recovery
procedure to that payload. Preserve operator configuration and identity unless
the release includes a deliberate migration. See the
[field update policy](../../../docs/node-tools-internals.md#field-update-policy).

`node-update.sh` delegates to `node-update.py`. `manet_release.py` selects stable metadata from Latest, or explicitly selects the newest published MANET release for `--development`. Fixed tag URLs bind `manet-release.json`, the board's tools archive, and its checksum to one release. Both embedded version files must agree with the selected release.

The updater validates paths, ownership, file types, archive limits, required
files, available space, the manifest digest, and the checksum sidecar before
live replacement. When a validated archive is newer than the installed release
and carries a different updater, the radio installs that updater first and
runs the check again with it. Updater fixes therefore apply to the release
that carries them. Reinstalls and downgrades keep the running updater. Boot files, modules, and Morse firmware use a separate
matched kernel payload with trial activation. Downloading requires
`/run/initial_time_synced`. Local recovery does not.

The default builder includes managed MT7916 firmware. Optional board kernel
inputs carry a manifest, boot files, versioned modules, and isolated Morse
firmware. CM4 defaults to a one-shot trial unless its local
`/etc/manet-kernel-update.conf` contains `enabled=n`. The profile is derived
from the node's boot config, command line, FAT UUID, and hardware and saved in
the transaction before reboot. An existing local trial profile overrides it.
Trials use the firmware kernel watchdog, `watchdog.open_timeout=120`, PID 1's
runtime watchdog, and a 300-second guard. The EEPROM boot watchdog is optional.
Without it, failure before kernel handoff can require a power cycle. CM4
provisioning disables the stock kernel's unmatched initramfs. Older CM4 images
with `auto_initramfs=1` retain their original `config.txt` and initramfs during
the trial. The candidate `tryboot.txt` sets `auto_initramfs=0` under `[all]`
alongside its slot prefix. Only health confirmation atomically commits both
settings to `config.txt`. The journal preserves the old assignments and their
sections. Preflight checks the saved and running root selectors, mounted root
and its storage drivers. Custom initramfs directives, userspace root aliases,
encrypted/LVM/RAID/network roots, and modular root filesystem or storage drivers
are refused with a specific reason. RPi5 retains its
matched image configuration. The trial runs before runtime software replacement, and a failed trial
is not automatically repeated. Early file recovery avoids the boot filesystem.
Late recovery stages or confirms the trial after normal mounts. Rock 3A kernel
activation is rejected because its bootloader fallback and reset path are not
supported. Details and limitations are in the
[kernel activation protocol](../../../docs/node-tools-internals.md#kernel-payload-and-activation-protocol).

Field confirmation requires multi-user startup with healthy critical units,
PID 1 holding an active hardware watchdog, root and boot mounts, the matching
module tree, radio driver bindings and PHYs, and an up `bat0`. PCI/USB IDs
identify radios even when no interfaces appeared. A bound SPI hat is recorded
before reboot, while an unbound overlay is not treated as hardware. The
saved inventory catches a radio disappearing during trial. Health has a
60-second budget and must finish before 240 seconds of boot uptime. No network
path, peers, or clock sync is required. Opt-out skips the kernel and persists
that selection for interrupted runtime recovery. It cannot abandon an armed
trial or erase a failed candidate. A payload that matches the running kernel
is skipped and recorded the same way. Its release must be the running
release, and the manifest must match the committed slot in `confirmed.json`
or, on a flashed radio, `installed-kernel.json`. A different set under the
running release is refused before anything is installed.

For an offline update, `--local-archive` and `--local-version` validate an
explicitly supplied board tools archive and adjacent checksum without release
discovery. This trusts the locally supplied files and allows an equal version
to be reinstalled. It uses the same staging, recovery, and trial transaction.

### Compatibility with stable v0.569

The old updater accepts only root-relative `etc`, `usr`, and `root` paths,
rejects raw boot/modules/firmware, and caps downloads at 64 MiB with 512 MiB
expanded. `MANET_LEGACY_BOOTSTRAP=1` wraps a complete new-updater CM4 archive
under `/usr/local/share/manet/bootstrap/`. The builder enforces those older
limits. A timer starts through the firmware setup helper that v0.569 already
calls. It waits for both version markers and the old transaction to finish,
then runs the new updater on the pinned local archive.

The inner transaction verifies its own checksum and matched kernel manifest,
uses tryboot, and keeps the original selector until health confirmation. Its
failure is retained without automatically retrying the kernel. Explicit kernel
cancellation also stops the wrapper timer for that candidate. The wrapper
removes its archive and units only after the exact candidate and tools are
confirmed. The wrapper defaults to the derived field profile and honors the
same opt-out. While opted out, its archive and timer remain available so
re-enabling can resume the kernel stage.

The same wrapper delivers the fix to v0.574 radios whose follow-up stopped on
`auto_initramfs=1` before creating a pending transaction. Its tools step replaces
the updater and inner archive, then the new updater starts the trial. Repeated
timer calls cannot duplicate an armed trial. When a direct payload takes over,
the updater retires the old bootstrap archive and timer under its update lock.
Its retained recovery archive completes an interrupted installation.

Optional release payloads carry the native Debian follower hostapd package
and the DFS-tagged regulatory database. Setup verifies their hashes, package
identity, installed version, follower marker, and APT candidate before version
commit. The hostapd preference pins `2:2.10-24+manet*` at priority 1001.
The new regulatory DB is CM4-only and takes effect after reboot. Rock 3A and
RPi5 retain their existing databases.

### Installation and recovery

A nonblocking `/run/manet-update.lock` serializes attempts. Before arming an installation, the updater resolves dependencies and saves a verified archive plus a recovery updater and release helper under `/var/lib/manet-update/recovery`. `/var/lib/manet-update/in-progress` takes precedence over release selection, version equality, and the routine throttle.

Files are staged and atomically replaced individually. The two version markers are committed only after manager selection, systemd reload, required service restarts, and active-state checks. That is not a whole-filesystem transaction or proof of working RF links.

The durable boundary is the pending marker. The archive, its checksum,
release descriptor, recovery program, and boot-recovery units must be saved
before that marker is made durable. Once it exists, a manual or boot attempt
finishes that release without network access. Only the routine daily check
looks for a newer published release, and it replaces the pending one only
outside a kernel trial. An explicit `--local-archive` also replaces it.
`/var/lib/manet-update/recovery/attempts` counts install attempts. After three,
the updater reinstalls the runtime payload of the last clean release from
`/var/lib/manet-update/installed`, leaves the kernel to its own trial and
fallback, and records the failed version in `failed.json` so selection skips
it. Without a stored release it restarts the management services and stays
pending. A successful installation moves its recovery archive to `installed`.
Dependencies are prepared before arming the transaction so the retained
recovery path can run without internet or a functioning package repository.

For each ordinary payload file, installation writes a temporary sibling,
flushes it, then renames it over the destination and flushes the directory.
A reader should therefore see the old or new complete file rather than a
partially written file. Across many files, however, an interruption can leave
a mixture of versions. Reinstalling the entire retained payload repairs that
mixture. The updater does not keep an old filesystem image to roll back to.

| Interruption point | Meaning on the next attempt |
|---|---|
| Before the transaction is armed | No pending payload transaction exists; selection and preparation can run again. Dependency setup may already have changed the system. |
| During file replacement | The pending marker directs recovery to the retained release, including files already copied on the previous attempt. |
| During service activation | Files can be current while services failed; activation is retried rather than treating file presence as success. |
| During final version-marker writes | Pending recovery still wins, even if one or both markers already show the new version. |
| After the pending marker is durably removed | The installation has passed its completion checks; an application defect needs diagnosis or a later release, not interrupted-install recovery. |

`manet-update-recover.service` restores the pending files early in boot. `manet-update-finish.service` performs activation once normal startup is available. Recovery revalidates the retained package and retries the same release without downloading or running APT. Failed or corrupt recovery remains pending for diagnosis.

Separating file restoration from activation lets early boot repair the
runtime before depending on its normal services. The later phase can then
restart and inspect those services. Neither phase proves that a new kernel
can boot: a kernel payload will require its own boot-set installation and
recovery design under the general update policy. The
[failure-handling reference](../../../docs/node-tools-internals.md#tools-update-failure-handling)
describes retained state and recovery entry points.

### Automatic triggering and kernel ownership

The dispatcher queues `manet-auto-update.service` after uplink reconciliation.
The gateway route manager queues the same service only after successfully
installing a mesh default where none existed. Route refreshes and gateway
switches do not queue it. `manet-auto-update.path` and the latched clock relay
retry after initial verified time sync. `manet-auto-update.timer` adds a daily
calendar event with `RandomizedDelaySec=2h` and `Persistent=true`, so a missed
event is caught after downtime.

Every trigger checks opt-in and the clock marker. The wrapper accepts either
the selected direct uplink's IPv4 default or `default via <IPv4> dev br0`,
which only the gateway route manager installs. Mesh clients need no saved
upstream interface. Only mesh checks sleep a random 0–900 seconds.
`MANET_AUTO_UPDATE_DELAY` provides a bounded test override. The
45-minute service timeout covers sleep and update together, leaving
30 minutes at maximum jitter. Route and opt-in are rechecked after the sleep.
Systemd coalesces starts of this oneshot while it is activating, and the
updater's flock excludes overlapping manual updates. The version-marker mtime
still enforces the actual 24-hour routine throttle. The timer and path are
shipped enabled and activated by setup and updates. Disabled opt-in exits
before any network check.

`manet-kernel-protect.py` applies MANET's boot-package policy before updater
dependency work. It prevents distribution packages from replacing the MANET
kernel and radio drivers. It does not prohibit a deliberate MANET kernel
update delivered through the release's update archive.

- [Updater behavior and recovery commands](../../../MANET/node_tools/README.md#utilities)
- [Installation and failure handling](../../../docs/node-tools-internals.md#tools-update-failure-handling)
- [Automatic update triggers](../../../MANET/networkd-dispatcher/README.md#when-the-interface-becomes-routable)
- [Release publication](../../../MANET/releases/README.md)
- [Kernel ownership](../../../docs/kernel-6.18-morse-port.md#81-apt-owns-userland-manet-releases-own-the-kernel)
