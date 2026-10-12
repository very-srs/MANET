# Update a radio

## Marine

![Crayon drawing of a software toolbox downloading into a powered radio connected to a router, followed by the radio beside a clock with both cables still connected.](../assets/updates-crayon.png)

### New tools. Keep the cables in.

1. **Power and internet.** Leave both connected.
2. **Update this radio.** Over SSH, run `sudo node-update.sh`.
3. **Wait.** Let it finish, then check that your radios can still talk.

The updater waits for the radio's clock to synchronize. Automatic updates happen only when enabled.

This updates the radio's tools. Publishing a documentation page does not update your radios.

## User

### The normal update follows stable releases

MANET's tools updater uses the GitHub release marked **Latest**, which is the stable release. It checks the downloaded package before installing it. Changes pushed to the source repository or this guide do not by themselves announce a radio update.

The tools package updates MANET software and services. A kernel or complete system upgrade has a separate procedure; follow the instructions for that release instead of treating a tools update as a complete reflash.

### Update one radio manually

1. Give the radio working internet access and leave it powered. Wait for its first verified clock synchronization; **Mesh recovery** in the dashboard shows clock information.
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

Enable **automatic updates** through [Node config](settings.md). An opted-in radio queues a stable check when its selected uplink becomes routable and when its clock first synchronizes. It rechecks that an uplink route is available before downloading.

These are event-triggered checks. There is no periodic download timer. Routine checks normally skip another check for 24 hours after a successful check or installation. An explicit manual run does not use that routine throttle.

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

Local recovery works without internet or clock synchronization. If the recovery data is missing or corrupt, retain it and collect the error for diagnosis. Successful recovery finishes the interrupted installation; it does not undo a defective release. See [troubleshooting](troubleshooting.md) for what to collect.

## Developer

### Release identity and package boundary

`node-update.sh` delegates to `node-update.py`. `manet_release.py` selects stable metadata from Latest, or explicitly selects the newest published MANET release for `--development`. Fixed tag URLs bind `manet-release.json`, the board's tools archive, and its checksum to one release. Both embedded version files must agree with the selected release.

Tools archives cannot carry kernels, firmware, networkd interface definitions, or `/etc/mesh.conf`. The updater validates paths, ownership, file types, archive limits, required files, available space, the manifest digest, and the checksum sidecar before live replacement. Downloading requires `/run/initial_time_synced`; local recovery does not.

### Installation and recovery

A nonblocking `/run/manet-update.lock` serializes attempts. Before arming an installation, the updater resolves dependencies and saves a verified archive plus a recovery updater and release helper under `/var/lib/manet-update/recovery`. `/var/lib/manet-update/in-progress` takes precedence over release selection, version equality, and the routine throttle.

Files are staged and atomically replaced individually. The two version markers are committed only after manager selection, systemd reload, required service restarts, and active-state checks. That is not a whole-filesystem transaction or proof of working RF links.

`manet-update-recover.service` restores the pending files early in boot; `manet-update-finish.service` performs activation once normal startup is available. Recovery revalidates the retained package and retries the same release without downloading or running APT. Failed or corrupt recovery remains pending for diagnosis.

### Automatic triggering and kernel ownership

The dispatcher queues `manet-auto-update.service` after uplink reconciliation. `manet-auto-update.path` and the clock-triggered service retry after initial verified time sync. Each path checks opt-in and the selected uplink; no timer polls for new releases.

`manet-kernel-protect.py` applies MANET's boot-package policy before updater dependency work. Distribution userland updates must not replace the MANET kernel and radio drivers. Package publication and kernel replacement remain governed by their separate maintenance procedures.

- [Updater behavior and recovery commands](../../../MANET/node_tools/README.md#utilities)
- [Installation and failure handling](../../../docs/node-tools-internals.md#tools-update-failure-handling)
- [Automatic update triggers](../../../MANET/networkd-dispatcher/README.md#when-the-interface-becomes-routable)
- [Release publication](../../../MANET/releases/README.md)
- [Kernel ownership](../../../docs/kernel-6.18-morse-port.md#81-apt-owns-userland-manet-releases-own-the-kernel)
