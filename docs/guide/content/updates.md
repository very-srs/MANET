# Update a radio

## Marine

![Crayon drawing of a software toolbox downloading into a powered radio connected to a router, followed by the radio beside a clock with both cables still connected.](../assets/updates-crayon.png)

### Update. Keep the cables in.

With automatic updates on, your radio checks when internet becomes available,
including through another radio. It also tries each day. Sharing another
radio's internet can add up to 15 minutes of waiting before a check.

To update manually:

1. **Power and internet.** Leave both connected.
2. **Update this radio.** Over SSH, run `sudo node-update.sh`.
3. **Wait.** Let it finish, then check that your radios can still talk.

The updater waits for the radio's clock to synchronize. Automatic updates happen only when enabled.

## User

### The normal update follows stable releases

MANET's tools updater uses the GitHub release marked **Latest**, which is the stable release. It checks the downloaded package before installing it. Changes pushed to the source repository or this guide do not by themselves announce a radio update.

The update archive can carry any component a release needs, including kernels,
drivers, firmware, and configuration changes. The name **tools** does not limit
its contents. Radios may be sealed in enclosures, so updates are intended to
maintain them in place rather than make reflashing a routine requirement.

Current packages update runtime software and some radio firmware. Full kernel
updating is still under development. Follow the selected release's instructions
for any required restart or reboot.

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

Local recovery works without internet or clock synchronization. If the recovery data is missing or corrupt, retain it and collect the error for diagnosis. Successful recovery finishes the interrupted installation; it does not undo a defective release. See [troubleshooting](troubleshooting.md) for what to collect.

## Developer

### Update scope and release identity

Treat the archive as the general field-update mechanism. Any required component
may be included in a release; match its installation, activation, and recovery
procedure to that payload. Preserve operator configuration and identity unless
the release includes a deliberate migration. See the
[field update policy](../../../docs/node-tools-internals.md#field-update-policy).

`node-update.sh` delegates to `node-update.py`. `manet_release.py` selects stable metadata from Latest, or explicitly selects the newest published MANET release for `--development`. Fixed tag URLs bind `manet-release.json`, the board's tools archive, and its checksum to one release. Both embedded version files must agree with the selected release.

The updater validates paths, ownership, file types, archive limits, required
files, available space, the manifest digest, and the checksum sidecar before
live replacement. There is no kernel, firmware, or network-configuration
category ban. Downloading requires `/run/initial_time_synced`; local recovery
does not.

The current builder includes the managed MT7916 firmware payload but not a
board-specific kernel overlay. Accepting boot files in an archive does not
implement a complete kernel upgrade: boot-filesystem handling, a matched
kernel/modules/device-tree set, module dependency generation, reboot sequencing,
size limits, and boot-failure recovery need validation before that release ships.

### Installation and recovery

A nonblocking `/run/manet-update.lock` serializes attempts. Before arming an installation, the updater resolves dependencies and saves a verified archive plus a recovery updater and release helper under `/var/lib/manet-update/recovery`. `/var/lib/manet-update/in-progress` takes precedence over release selection, version equality, and the routine throttle.

Files are staged and atomically replaced individually. The two version markers are committed only after manager selection, systemd reload, required service restarts, and active-state checks. That is not a whole-filesystem transaction or proof of working RF links.

The durable boundary is the pending marker. The archive, its checksum,
release descriptor, recovery program, and boot-recovery units must be saved
before that marker is made durable. Once it exists, an attempt finishes that
release before considering a newer one. This prevents a failed installation
from being abandoned midway simply because GitHub now offers another version.
Dependencies are prepared before arming the transaction so the retained
recovery path can run without internet or a functioning package repository.

For each ordinary payload file, installation writes a temporary sibling,
flushes it, then renames it over the destination and flushes the directory.
A reader should therefore see the old or new complete file rather than a
partially written file. Across many files, however, an interruption can leave
a mixture of versions. Reinstalling the entire retained payload repairs that
mixture; the updater does not keep an old filesystem image to roll back to.

| Interruption point | Meaning on the next attempt |
|---|---|
| Before the transaction is armed | No pending payload transaction exists; selection and preparation can run again. Dependency setup may already have changed the system. |
| During file replacement | The pending marker directs recovery to the retained release, including files already copied on the previous attempt. |
| During service activation | Files can be current while services failed; activation is retried rather than treating file presence as success. |
| During final version-marker writes | Pending recovery still wins, even if one or both markers already show the new version. |
| After the pending marker is durably removed | The installation has passed its completion checks; an application defect needs diagnosis or a later release, not interrupted-install recovery. |

`manet-update-recover.service` restores the pending files early in boot; `manet-update-finish.service` performs activation once normal startup is available. Recovery revalidates the retained package and retries the same release without downloading or running APT. Failed or corrupt recovery remains pending for diagnosis.

Separating file restoration from activation lets early boot repair the
runtime before depending on its normal services. The later phase can then
restart and inspect those services. Neither phase proves that a new kernel
can boot: a kernel payload will require its own boot-set installation and
recovery design under the general update policy. The
[failure-handling reference](../../../docs/node-tools-internals.md#tools-update-failure-handling)
describes retained state, validation limits, and recovery entry points.

### Automatic triggering and kernel ownership

The dispatcher queues `manet-auto-update.service` after uplink reconciliation.
The gateway route manager queues the same service only after successfully
installing a mesh default where none existed; route refreshes and gateway
switches do not queue it. `manet-auto-update.path` and the latched clock relay
retry after initial verified time sync. `manet-auto-update.timer` adds a daily
calendar event with `RandomizedDelaySec=2h` and `Persistent=true`, so a missed
event is caught after downtime.

Every trigger checks opt-in and the clock marker. The wrapper accepts either
the selected direct uplink's IPv4 default or `default via <IPv4> dev br0`,
which only the gateway route manager installs. Mesh clients need no saved
upstream interface. Only mesh checks sleep a random 0–900 seconds;
`MANET_AUTO_UPDATE_DELAY` provides a bounded test override. The
45-minute service timeout covers sleep and update together, leaving
30 minutes at maximum jitter. Route and opt-in are rechecked after the sleep.
Systemd coalesces starts of this oneshot while it is activating, and the
updater's flock excludes overlapping manual updates. The version-marker mtime
still enforces the actual 24-hour routine throttle. The timer and path are
shipped enabled and activated by setup and updates; disabled opt-in exits
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
