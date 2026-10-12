# Change settings

## Marine

![Crayon sequence showing a setting chosen on a phone, three radios acknowledging it with check marks, and a clock beside the radios changing together.](../assets/settings-crayon.png)

### Pick. Wait. Apply.

1. **Open [Manage](http://manet.local/manage/).** Use the admin password.
2. **Pick your change in Node config.** Check whether it affects one radio or everyone.
3. **Stage the shared change.** Wait for every expected radio to say yes.
4. **Apply.** Wait for the countdown and for the radios to reconnect.

Leave **Skip the safety net** unchecked. If a radio is missing, bring it back before changing the group's settings.

Your radio's phone Wi-Fi settings stay on your radio and apply when you press **Stage Changes**.

## User

### Choose the scope before changing anything

Connect to your radio, open **[Manage](http://manet.local/manage/)**, and select **Node config**. All participating radios need the same admin password for shared changes. Bring the radios you intend to change online first.

| Setting | What changes |
|---|---|
| EUD connection mode, AP name, AP password | This radio's client connection only. An AP change may disconnect your phone. |
| Maximum EUDs per node | Shown here, but set during flashing and not editable in this form. |
| Mesh SSID, mesh key, IPv4 network | Shared mesh configuration; connections can drop while it is applied. |
| Admin password, MediaMTX, Mumble, automatic updates | Shared settings applied through the mesh transaction. |
| Automatic channel selection (ACS) | Changes the channel manager and restarts it when applied. |
| Regulatory domain | Writes the selected region; radio changes require a reboot. |

An AP name is for phones joining this radio. The mesh SSID and mesh key are for radios joining each other. They are separate settings. See [connection modes](connect.md) before changing EUD mode.

Local EUD and AP edits take effect when you press **Stage Changes**; they do not wait for a mesh ACK or the activation countdown. Make those edits separately from shared mesh changes so you can reconnect your phone before continuing.

### Apply a shared configuration

1. Edit the intended fields and select **Stage Changes**.
2. Read the acknowledgement table. An **ACK** means that a node received and staged this particular change. Check the node list as well as the percentage; an offline radio cannot confirm receipt.
3. When the table reaches 100%, select **Apply Now**. Activation is scheduled about 60 seconds later.
4. Keep the radios powered while they apply the change. Reconnect your phone if its connection changed, then check the [dashboard](dashboard.md) for the expected peers.

If the form is waiting for time synchronization, let a verified GPS or network time source become available. Timed mesh changes wait for that first synchronization. The public status page and local-only configuration remain available.

**Force Apply** bypasses the acknowledgement gate. A radio that missed a mesh key, SSID, or network change may be unable to rejoin. Restore contact with missing radios before using the ordinary apply flow.

### What the safety net does

For a changed mesh SSID, mesh key, or IP network, each radio saves its previous configuration. If it had peers before the change but sees none after the default five-minute trial, it restores that configuration. Leave **Skip the safety net** unchecked for this protection.

This check needs only one reachable peer; it does not prove that every expected radio returned. A radio that was already alone keeps the new settings. After a failed or interrupted apply attempt, inspect the result and stage a fresh change if another attempt is needed.

Changing the admin password ends existing management sessions. Use the new password to sign in again. After changing the regulatory domain, reboot the affected radios and check their channels.

### Radio controls are a separate tab

Use **Radio config** for interface on/off, HaLow channels, and supported Wi-Fi channel or power controls. Check the selected node or mesh scope before applying. Manual Wi-Fi channel selection is unavailable while ACS owns the channels.

Wi-Fi transmit-power selections do not survive radio restarts or reboots. The displayed value is the driver's report, which can differ from the request. HaLow power is shown but cannot be changed from this UI.

## Developer

### Keep local and shared settings separate

`mesh_config.py` owns the split. `eud`, `lan_ap_ssid`, and `lan_ap_key` apply locally; `max_euds_per_node` is local but read-only in management. AP fields must not enter Alfred packages. `manet_eud_ap.py` preserves the node suffix in the broadcast AP name and restores the previous local files if activation fails.

The form validates changed fields before local writes or shared publication. A mixed local/shared request also checks clock readiness before any local side effects. Local fields apply during staging; only the shared fields enter the ACK and activation flow. The receiver validates the same allowed keys and value constraints. `mesh-config-write.py` writes literal configuration data atomically, including quoted values, without interpreting them as shell or sed input.

### Transaction authority and lifecycle

Configuration staging uses authenticated, encrypted Alfred type 70; configuration ACKs use type 73. Radio changes use types 71 and 72. `manet_admin.py` authenticates control with the shared admin password; SAE membership and public telemetry do not authorize a command or satisfy the ACK gate. HTTP is the operator interface, not peer-to-peer control transport.

Each stage has a fresh transaction identity. Normal control waits for `/run/initial_time_synced`, validates message age, and retains replay history in `/var/lib/manet-admin`. Activation is consumed before the apply attempt, so an interrupted attempt requires a fresh request. Clock-independent recovery messages cannot authorize configuration edits.

The configuration workflow separates receipt from activation because a
change can remove the very link that carries its instructions:

1. **Stage and validate.** The sender creates a transaction with no active
   activation time. Each receiver authenticates it, validates the allowed
   settings, stores the pending values, and sends an authenticated ACK for
   that transaction. An ACK means the proposal was accepted for staging,
   not that the new settings already work.
2. **Authorize activation.** Normal Apply waits for the UI's full ACK table;
   Force Apply explicitly bypasses that gate. Activation is rebroadcast for
   60 seconds in the future, giving receivers time to learn the deadline
   before changing their links. This administrative gate differs from ACS's
   automatic strict-majority channel vote.
3. **Prepare and apply locally.** At the deadline, a node whose changed
   settings threaten connectivity prepares its rollback state. If that
   fails, it defers the protected change. Once ready, it records the command
   as consumed before attempting the change, so a restart cannot apply an
   old activation again.
4. **Judge the trial.** Each node checks its own connectivity at its rollback
   deadline. Nodes can reach different outcomes; this is not a distributed
   transaction that either changes every radio or changes none.

The encrypted envelope binds the message to its Alfred control type using
AES-256-GCM authenticated data. It carries a random message identity and send
time; password-derived keys and fresh nonces protect message contents and
integrity. Ordinary envelopes expire after 900 seconds and tolerate at most
60 seconds of future clock skew. Persistent replay state adds protection
across service restarts, beyond the timestamp check. The
[control transport reference](../../../docs/node-tools-internals.md#mesh-configuration-push)
explains the key derivation, receive limits, and replay ordering.

### Rollback is local and conditional

`mesh-config-rollback.sh` counts distinct BATMAN originators, snapshots configuration and supplicant files, and records a persistent trial deadline. Dangerous apply requires successful preparation unless the request explicitly disables it. An unreadable peer baseline is an error, not proof of a solo node.

At the deadline, a previously connected node keeps the change if at least one peer is visible; no peers or a failed query cause restoration. A verified empty baseline permits a solo node to keep the change. Failed restoration retains the snapshot and retries, including across reboot. An already armed trial cannot have its original snapshot or deadline replaced.

For example, a wrong mesh key can isolate a node immediately after it applies
the change. Waiting for a remote "undo" would not work because that command
would need the lost connection. The local snapshot and default five-minute
trial let it restore without receiving anything else. Conversely, observing
one peer at the deadline only proves some mesh contact survived; it does
not prove all original peers returned or that voice and internet work.
That limited success criterion is why the operator should inspect the
result even after a protected change is retained.

UI radio-power requests have a different persistence contract: they are applied and read back, but are not saved for restart. Avoid describing all radio controls as permanent configuration.

- [Configuration flow and rollback reference](../../../MANET/node_tools/README.md#mesh-configuration-push)
- [Authentication, writing, and rollback internals](../../../docs/node-tools-internals.md#mesh-configuration-push)
- [Local/shared setting definitions](../../../MANET/node_tools/mesh_config.py)
- [Radio control behavior](../../../MANET/node_tools/README.md#web-interface)
