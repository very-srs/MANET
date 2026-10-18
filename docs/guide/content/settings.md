# Change settings

## Marine

![Crayon sequence showing a setting chosen on a phone, three radios acknowledging it with check marks, and a clock beside the radios changing together.](../assets/settings-crayon.png)

### Pick. Wait. Apply.

1. **Open [Manage](http://manet.local/manage/).** Ask an adult to enter the admin password.
2. **Pick your change in Node config.** Check whether it affects one radio or everyone.
3. **Stage the shared change.** Wait for every expected radio to say yes.
4. **Apply.** Wait for the countdown and for the radios to reconnect.

Leave **Skip the safety net** unchecked. If a radio is missing, bring it back to the group before changing the group's settings.

Your radio keeps the phone Wi-Fi name chosen when it was flashed. You can
change its Wi-Fi password. That change stays on your radio and takes effect
when you press **Stage Changes**.

Give your radio a name on the [status page](http://manet.local/). Type it in
**Your operator name** and press **Save**. You do not need a password.
Clear the name and save when you hand the radio to someone else.

## User

### Choose the scope before changing anything

Connect to your radio, open **[Manage](http://manet.local/manage/)**, and select **Node config**. All participating radios need the same admin password for shared changes. Bring the radios you intend to change online first.

| Setting | What changes |
|---|---|
| EUD connection mode, AP password | This radio's client connection only. An AP change may disconnect your phone. |
| AP SSID, maximum EUDs per node | Set during flashing. The full phone Wi-Fi name is shown here and cannot be changed after flashing. |
| Mesh SSID, mesh key, IPv4 network | Shared mesh configuration; connections can drop while it is applied. |
| Admin password, MediaMTX, Mumble, automatic updates | Shared settings applied through the mesh transaction. |
| Automatic channel selection (ACS) | Changes the channel manager and restarts it when applied. |
| Regulatory domain | Writes the selected region; radio changes require a reboot. |

The AP SSID is the Wi-Fi name phones use to join this radio, such as `EUD-5cc2`.
Choose the same base name for every radio when flashing. Keeping that name
fixed helps radios recognize the mesh's phone hotspots and exclude them as
internet connections. The AP password remains editable.

An AP name is for phones joining this radio. The mesh SSID and mesh key are for radios joining each other. They are separate settings. See [connection modes](connect.md) before changing EUD mode.

Local EUD and AP edits take effect when you press **Stage Changes**. They do not wait for a mesh ACK or the activation countdown. Make those edits separately from shared mesh changes so you can reconnect your phone before continuing.

To label just your radio, open the [public status page](http://manet.local/),
enter a name or callsign in **Your operator name**, and press **Save**. No admin
password is needed. It appears alongside the radio's hostname on status pages.
Other radios learn it with the next status announcement in either static or
automatic channel mode. Clear the field and save again to remove it. Anyone
connected to this radio can replace the label when the radio changes hands.
The label does not rename an ATAK phone or choose the phone that controls
location.

### Apply a shared configuration

1. Edit the intended fields and select **Stage Changes**.
2. Read the acknowledgement table. An **ACK** means that a node received and staged this particular change. Whether it was applied and verified is reported separately, after activation. Check the node list as well as the percentage. An offline radio cannot confirm receipt.
3. When the table reaches 100%, select **Apply Now**. Activation is scheduled about 60 seconds later.
4. Keep the radios powered while they apply the change. Reconnect your phone if its connection changed, then check the [dashboard](dashboard.md) for the expected peers.

If the form is waiting for time synchronization, let a verified GPS or network time source become available. Timed mesh changes wait for that first synchronization. The public status page and local-only configuration remain available.

**Force Apply** bypasses the acknowledgement gate. A radio that missed a mesh key, SSID, or network change may be unable to rejoin. Restore contact with missing radios before using the ordinary apply flow.

### What the safety net does

For a changed mesh SSID, mesh key, or IP network, each radio saves its previous configuration. If it had peers before the change but sees none after the default five-minute trial, it restores that configuration. Leave **Skip the safety net** unchecked for this protection.

This check needs only one reachable peer. It does not prove that every expected radio returned. A radio that was already alone keeps the new settings.

If an apply is interrupted, for example by a service restart, the radio finishes it on its own from a saved record. You do not need to send the change again. A reboot during the trial period ends the trial and restores the previous settings, so leave the radios powered until the trial is over. A rollback is final for that change. To try again, stage it as a new change.

Changing the admin password ends existing management sessions. Use the new password to sign in again. After changing the regulatory domain, reboot the affected radios and check their channels.

### Radio controls are a separate tab

Use **Radio config** for interface on/off, HaLow channels, and supported Wi-Fi channel or power controls. Check the selected node or mesh scope before applying. Manual Wi-Fi channel selection is unavailable while ACS owns the channels.

Wi-Fi transmit-power selections do not survive radio restarts or reboots. The displayed value is the driver's report, which can differ from the request. HaLow power is shown but cannot be changed from this UI.

## Developer

### Keep local and shared settings separate

`operator_name` is an optional node-local setting limited to 32 UTF-8 bytes.
The public status form sends only that key to `POST /api/operator-name`.
No session is required, but the client must be in this radio's DHCP pool.
The endpoint requires bounded same-origin JSON, rejects all other keys, and
uses the management write lock for its atomic local write. It neither runs
AP configuration nor stages a mesh change. An empty submitted value clears
the label. It is excluded from mesh configuration
packages, but the telemetry encoder reads it on each publication and uses
the existing `atak_user` field. Static operation and both ACS publication
paths use that same encoder. Missing or invalid local labels publish as empty.

`mesh_config.py` owns the split. `eud` and `lan_ap_key` apply locally.
`lan_ap_ssid` and `max_euds_per_node` are fixed at flash time and shown read-only.
AP fields must not enter Alfred packages. AP-name changes fail before local
writes or shared staging, including direct config apply and AP activation.
The password remains editable, with local file restoration if activation fails.

`manet_eud_ap.py` records the full broadcast name in `/var/lib/manet-ap-ssid`
during radio setup. Setup reruns verify it before touching radio configuration.
Existing nodes without that record preserve the SSID already in hostapd.
A completed node without either record refuses a new name. Wired flashes
with no AP name retain that empty choice. The fixed base name preserves the
uplink filter's local-prefix assumption. See the
[configuration internals](../../../docs/node-tools-internals.md#mesh-configuration-push).

On phones, Node config stacks the form above deployment status, places labels
above full-width inputs, and uses the current theme for inputs and node rows.

The form validates changed fields before local writes or shared publication. A mixed local/shared request also checks clock readiness before any local side effects. Local fields apply during staging. Only the shared fields enter the ACK and activation flow. The receiver validates the same allowed keys and value constraints. `mesh-config-write.py` writes literal configuration data atomically, including quoted values, without interpreting them as shell or sed input.

### Transaction authority and lifecycle

Configuration staging uses authenticated, encrypted Alfred type 70. Configuration ACKs use type 73. Radio changes use types 71 and 72. `manet_admin.py` authenticates control with the shared admin password. SAE membership and public telemetry do not authorize a command or satisfy the ACK gate. HTTP is the operator interface, not peer-to-peer control transport.

Each stage has a fresh transaction identity. Normal control waits for `/run/initial_time_synced`, validates message age, and retains replay history in `/var/lib/manet-admin`. Transport consumption prevents replay, and it is separate from applying. The local journal in `/var/lib/manet-config` owns the job, so an interrupted apply resumes from local state without a fresh request or the original message. The updater migrates pending work into the journal. Clock-independent recovery messages cannot authorize configuration edits.

The configuration workflow separates receipt from activation because a
change can remove the very link that carries its instructions:

1. **Stage and validate.** The sender creates a transaction with no active
   activation time. Each receiver authenticates it, validates the allowed
   settings, stores the pending values, and sends an authenticated ACK for
   that transaction. An ACK means the proposal was accepted for staging,
   not that the new settings already work.
2. **Authorize activation.** Normal Apply waits for the UI's full ACK table.
   Force Apply explicitly bypasses that gate. Activation is rebroadcast for
   60 seconds in the future, giving receivers time to learn the deadline
   before changing their links. This administrative gate differs from ACS's
   automatic strict-majority channel vote.
3. **Prepare and apply locally.** At the deadline, a node whose changed
   settings threaten connectivity prepares its rollback state. If that
   fails, it defers the protected change. Once ready, it saves the job and
   its rollback snapshot in the journal and records the command as consumed
   before any live write, so a restart resumes this job but cannot apply an
   old activation again. The result is checked against the real files and
   services before it counts as verified, and ACKs report applied status
   only for verified or committed work.
4. **Judge the trial.** Each node checks its own connectivity at its rollback
   deadline. Nodes can reach different outcomes. This is not a distributed
   transaction that either changes every radio or changes none.

The encrypted envelope binds the message to its Alfred control type using
AES-256-GCM authenticated data. It carries a random message identity and send
time. Password-derived keys and fresh nonces protect message contents and
integrity. Ordinary envelopes expire after 900 seconds and tolerate at most
60 seconds of future clock skew. Persistent replay state adds protection
across service restarts, beyond the timestamp check. The
[control transport reference](../../../docs/node-tools-internals.md#mesh-configuration-push)
explains the key derivation, receive limits, and replay ordering.

### Rollback is local and conditional

`mesh-config-rollback.sh` counts distinct BATMAN originators, snapshots configuration and supplicant files, and records a persistent trial deadline. Dangerous apply requires successful preparation unless the request explicitly disables it. An unreadable peer baseline is an error, not proof of a solo node.

At the deadline, a previously connected node keeps the change only if its files and services still match the request and at least one peer is visible. No peers or a failed query cause restoration. A verified empty baseline permits a solo node to keep the change. A reboot during the trial expires it at once and restores: there is no reboot grace extension. Failed restoration retains the snapshot and retries, including across reboot. A completed rollback is terminal for that transaction. An already armed trial cannot have its original snapshot or deadline replaced.

For example, a wrong mesh key can isolate a node immediately after it applies
the change. Waiting for a remote "undo" would not work because that command
would need the lost connection. The local snapshot and default five-minute
trial let it restore without receiving anything else. Conversely, observing
one peer at the deadline only proves some mesh contact survived. It does
not prove all original peers returned or that voice and internet work.
That limited success criterion is why the operator should inspect the
result even after a protected change is retained.

UI radio-power requests have a different persistence contract: they are applied and read back, but are not saved for restart. Avoid describing all radio controls as permanent configuration.

- [Configuration flow and rollback reference](../../../MANET/node_tools/README.md#mesh-configuration-push)
- [Authentication, writing, and rollback internals](../../../docs/node-tools-internals.md#mesh-configuration-push)
- [Local/shared setting definitions](../../../MANET/node_tools/mesh_config.py)
- [Radio control behavior](../../../MANET/node_tools/README.md#web-interface)
