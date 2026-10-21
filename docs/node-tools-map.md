# Node-tool map

Start with [what runs automatically](guide/content/automation.md) for the
system's lifecycle and ownership. This map answers which file implements a
responsibility. The linked outcome chapters explain the mechanism and its
observable results; the [runtime reference](../MANET/node_tools/README.md)
and [internals](node-tools-internals.md) carry detailed contracts.

This is an index of production files, not a list of commands to run by hand.
Python libraries, generated schemas, and supplied binaries are included.
Systemd units, network hooks, and the controller that invokes a helper still
determine its lifecycle. Tests and fixtures belong to their source directories.

## Orchestration and shared execution

Read [automatic work](guide/content/automation.md) and
[core orchestration](node-tools-internals.md#core-orchestration).

| File | Responsibility |
|---|---|
| [node-manager-select.sh](../MANET/node_tools/node-manager-select.sh) | Atomically select the generated manager symlink from the ACS setting. |
| [node-manager-acs.sh](../MANET/node_tools/node-manager-acs.sh) | Schedule discovery, channel work, publication, addressing, and services in ACS mode. |
| [node-manager-static.sh](../MANET/node_tools/node-manager-static.sh) | Reconcile ordinary mesh work with fixed conventional Wi-Fi channels. |
| [manet-manager-common.sh](../MANET/node_tools/manet-manager-common.sh) | Shared manager scheduling and state-dependent operations. |
| [manet-common.sh](../MANET/node_tools/manet-common.sh) | Shared shell queries, logging, and runtime helper access. |
| [manet-runtime-client.sh](../MANET/node_tools/manet-runtime-client.sh) | Submit bounded local helper work; distinguish safe fallback from an already submitted request. |
| [manet_runtime.py](../MANET/node_tools/manet_runtime.py) | Execute supported on-demand helpers in the resident control process. |

## Identity, telemetry, and addresses

Read [addresses](guide/content/addresses.md), [dashboard observations](guide/content/dashboard.md),
and [registry internals](node-tools-internals.md#data-management).

| File | Responsibility |
|---|---|
| [NodeInfo.proto](../MANET/node_tools/NodeInfo.proto) | Source schema for identity, telemetry, and related wire fields. |
| [NodeInfo_pb2.py](../MANET/node_tools/NodeInfo_pb2.py) | Committed generated Python binding for that schema. |
| [encoder.py](../MANET/node_tools/encoder.py) | Encode local identity/telemetry for Alfred publication. |
| [decoder.py](../MANET/node_tools/decoder.py) | Decode published records into the registry's field representation. |
| [manet_ids.py](../MANET/node_tools/manet_ids.py) | Convert wire identifiers to and from display forms. |
| [mesh-registry-builder.sh](../MANET/node_tools/mesh-registry-builder.sh) | Retained executable entry point for registry construction. |
| [manet_registry_builder.py](../MANET/node_tools/manet_registry_builder.py) | Join Alfred identity/telemetry and rebuild changed registry observations. |
| [manet_registry.py](../MANET/node_tools/manet_registry.py) | Age locally observed records independently of wall-clock corrections. |
| [mesh-ip-startup.py](../MANET/node_tools/mesh-ip-startup.py) | Wait for usable Alfred discovery before claiming IPv4 space. |
| [mesh-ip-manager.sh](../MANET/node_tools/mesh-ip-manager.sh) | Allocate/reconcile the node block, local DHCP pool, and management names. |
| [manet_ip_runtime.py](../MANET/node_tools/manet_ip_runtime.py) | Check live IP/policy outputs and skip only an already settled reconciliation. |
| [manet_node_ipv4.py](../MANET/node_tools/manet_node_ipv4.py) | Identify the primary node address without mistaking a service VIP or client alias for it. |
| [manet-ipcalc.sh](../MANET/node_tools/manet-ipcalc.sh) | Calculate IPv4 network boundaries for allocation helpers. |
| [mac-to-ip.sh](../MANET/node_tools/mac-to-ip.sh) | Resolve a node MAC through the registry. |
| [mesh-hosts-update.sh](../MANET/node_tools/mesh-hosts-update.sh) | Timer-facing entry point for managed hostname updates. |
| [manet_hosts.py](../MANET/node_tools/manet_hosts.py) | Replace the managed hosts block only when mappings change. |
| [manet-syncthing-id.py](../MANET/node_tools/manet-syncthing-id.py) | Read the certificate-derived local Syncthing identity for publication. |
| [manet_interfaces.py](../MANET/node_tools/manet_interfaces.py) | Collect local radio telemetry without importing the web server. |
| [manet_node_info.py](../MANET/node_tools/manet_node_info.py) | Gather local node facts and common display representations. |
| [halow-mcs-summary.py](../MANET/node_tools/halow-mcs-summary.py) | Summarize HaLow station modulation/rate information for telemetry. |

## Mesh channels and partition recovery

Read [mesh mechanisms](guide/content/mesh.md) and
[channel agreement](node-tools-internals.md#channel-agreement-and-recovery).

| File | Responsibility |
|---|---|
| [mesh-channel-agreement.py](../MANET/node_tools/mesh-channel-agreement.py) | Run authenticated ACS agreement and recovery across the connected component. |
| [manet_acs_agreement.py](../MANET/node_tools/manet_acs_agreement.py) | Pure bounded majority-agreement state machine. |
| [mesh-acs-common.sh](../MANET/node_tools/mesh-acs-common.sh) | Shared clock, role, and channel configuration helpers. |
| [manet_bgobs.py](../MANET/node_tools/manet_bgobs.py) | Measure 5 GHz candidates on the reserved third RX chain and correct own-TX blanking. |
| [manet_dfs.py](../MANET/node_tools/manet_dfs.py) | Check paired DFS capability and live channel flags, subscribe to radar/file events without polling, and send bounded CAC and mesh CSA requests. |
| [manet_dfs_safety.py](../MANET/node_tools/manet_dfs_safety.py) | Fence DFS interfaces during guardian failure or code replacement and resume stored non-DFS fallbacks only after guardian readiness. |
| [manet_rx_policy.py](../MANET/node_tools/manet_rx_policy.py) | Reconcile CM4 receive paths from local RF trouble and kernel hold ownership. |
| [channel-election.sh](../MANET/node_tools/channel-election.sh) | Score measured channels and request an agreed change; signal limp conditions. |
| [quorum-checker.sh](../MANET/node_tools/quorum-checker.sh) | Decide whether isolation requires returning to discovery, distinguishing unavailable checks. |
| [limp-mode-manager.sh](../MANET/node_tools/limp-mode-manager.sh) | Reconcile limp-mode entry/exit from mesh observations. |
| [tourguide-manager.sh](../MANET/node_tools/tourguide-manager.sh) | Schedule elected partition-discovery excursions and merge handling. |
| [mesh-tourguide-election.py](../MANET/node_tools/mesh-tourguide-election.py) | Choose a tourguide using reachable membership mapped to node identities. |
| [manet_rendezvous.py](../MANET/node_tools/manet_rendezvous.py) | Derive the shared clock-based rendezvous schedule and discovery mode. |
| [manet_rejoin_channel.py](../MANET/node_tools/manet_rejoin_channel.py) | Validate a rejoining radio's channel against peer observations. |
| [mesh-peer-count.sh](../MANET/node_tools/mesh-peer-count.sh) | Count distinct reachable BATMAN originators for whole-component decisions. |

## Radio preparation and control

Read [radio controls](guide/content/radios.md) and [hardware](guide/content/hardware.md).

| File | Responsibility |
|---|---|
| [mesh-radio-state.py](../MANET/node_tools/mesh-radio-state.py) | Receive, stage, acknowledge, and activate authenticated radio changes. |
| [manet_radio.py](../MANET/node_tools/manet_radio.py) | Query radios and apply local actions authorized by staged control. |
| [manet_static_channels.py](../MANET/node_tools/manet_static_channels.py) | Validate and persist band-based static channel plans. |
| [manet_supplicant.py](../MANET/node_tools/manet_supplicant.py) | Select and restart configured conventional/HaLow supplicants. |
| [manet-mesh-supplicant.py](../MANET/node_tools/manet-mesh-supplicant.py) | Enforce 20 MHz Wi-Fi mesh width, prepare inactivity policy, and launch the MANET supplicant. |
| [prepare-standard-mesh-iface.sh](../MANET/node_tools/prepare-standard-mesh-iface.sh) | Prepare conventional mesh interface mode before supplicant start. |
| [batman-if-setup.sh](../MANET/node_tools/batman-if-setup.sh) | Create/configure BATMAN and attach valid mesh interfaces. |
| [batman-enslave-watch.sh](../MANET/node_tools/batman-enslave-watch.sh) | Repair interfaces that fall out of BATMAN membership, and restart a HaLow supplicant whose mesh is gone. |
| [sae-watchdog.sh](../MANET/node_tools/sae-watchdog.sh) | Guarded recovery for SAE-authentication-blocked missing mesh interfaces. |
| [unblock-wifi-rfkill.sh](../MANET/node_tools/unblock-wifi-rfkill.sh) | Clear software Wi-Fi blocks while respecting hardware blocks. |
| [manet-mesh-power.sh](../MANET/node_tools/manet-mesh-power.sh) | Request mesh startup transmit power without claiming measured RF output. |
| [manet-region.py](../MANET/node_tools/manet-region.py) | Propagate the configured regulatory domain to radio configuration. |
| [morse_cli](../MANET/node_tools/morse_cli) | Supplied Morse driver/firmware diagnostic executable. |

## Client access and internet uplinks

Read [client packet paths](guide/content/connect.md), [internet sharing](guide/content/internet.md),
and [dispatcher ownership](../MANET/networkd-dispatcher/README.md).

| File | Responsibility |
|---|---|
| [ethernet-autodetect.sh](../MANET/node_tools/ethernet-autodetect.sh) | Classify Ethernet and reconcile its uplink/client role. |
| [manet-uplink-dispatch.sh](../MANET/node_tools/manet-uplink-dispatch.sh) | Reconcile direct uplinks, gateway publication, and dependent EUD radio roles. |
| [manet-uplink-speed.sh](../MANET/node_tools/manet-uplink-speed.sh) | Estimate Ethernet bandwidth and advertise measured or fallback capacity. |
| [manet-uplink-dns.py](../MANET/node_tools/manet-uplink-dns.py) | Select qualified DHCP DNS, migrate networkd policy, and resolve candidate probes. |
| [gateway-route-manager.sh](../MANET/node_tools/gateway-route-manager.sh) | Select a reachable gateway and maintain the Linux mesh default route with hysteresis. |
| [usb-ethernet-watch.sh](../MANET/node_tools/usb-ethernet-watch.sh) | Pass USB Ethernet/tether hotplug into role detection. |
| [manet_wifi_uplink.py](../MANET/node_tools/manet_wifi_uplink.py) | Reserve a local built-in Wi-Fi client, scan nearby APs, and follow its channel with the shared EUD AP. |
| [manet-retire-uplink.py](../MANET/node_tools/manet-retire-uplink.py) | Remove retired external Wi-Fi uplink artifacts during updates. |
| [manet_wifi_saved.py](../MANET/node_tools/manet_wifi_saved.py) | Keep successful uplink credentials private on this node, shared by built-in radios. |
| [manet_wifi_events.py](../MANET/node_tools/manet_wifi_events.py) | Deliver supplicant, hostapd, link/address, and local-file events to the optional Wi-Fi follower without periodic helper commands. |
| [manet_ap_mesh.py](../MANET/node_tools/manet_ap_mesh.py) | Transfer the candidate physical interface between AP and mesh ownership. |
| [manet_eud_ap.py](../MANET/node_tools/manet_eud_ap.py) | Keep the flash-time AP name fixed and activate local password changes with recovery. |
| [prepare-ap-iface.sh](../MANET/node_tools/prepare-ap-iface.sh) | Withdraw mesh ownership before each hostapd preparation. |
| [manet-ap-guard.sh](../MANET/node_tools/manet-ap-guard.sh) | Prevent a mesh supplicant starting on a candidate owned by the AP. |
| [manet-dhcp-isolation.py](../MANET/node_tools/manet-dhcp-isolation.py) | Install local DHCP/discovery confinement at the bridge boundary. |
| [manet-dns-setup.sh](../MANET/node_tools/manet-dns-setup.sh) | Prepare consistent DNS forwarding and resolver configuration. |
| [manet-ui-firewall.sh](../MANET/node_tools/manet-ui-firewall.sh) | Restrict web access to this node's client pool and iperf to the mesh subnet. |
| [verify-bridge.sh](../MANET/node_tools/verify-bridge.sh) | Manually inspect the bridged client architecture; not an automatic repair daemon. |

## Administrative configuration

Read [settings transactions](guide/content/settings.md) and
[configuration internals](node-tools-internals.md#mesh-configuration-push).

| File | Responsibility |
|---|---|
| [manet_admin.py](../MANET/node_tools/manet_admin.py) | Password-authenticated encrypted Alfred control and replay protection. |
| [manet_admin_limits.py](../MANET/node_tools/manet_admin_limits.py) | Share boot-local limits on unauthenticated password-derivation work. |
| [manet_clock.py](../MANET/node_tools/manet_clock.py) | Count wall-clock jumps against the boot clock so timed protocols can void old commands and rounds. |
| [manet-admin-setup.sh](../MANET/node_tools/manet-admin-setup.sh) | Install the control transport's cryptography dependency during setup/update. |
| [mesh_config.py](../MANET/node_tools/mesh_config.py) | Separate local EUD/AP keys from mesh-wide packages. |
| [mesh-config-sync.py](../MANET/node_tools/mesh-config-sync.py) | Receive/acknowledge shared configuration and invoke scheduled activation. |
| [mesh-config-apply.sh](../MANET/node_tools/mesh-config-apply.sh) | Apply staged configuration and reconcile affected services. |
| [mesh-config-rollback.sh](../MANET/node_tools/mesh-config-rollback.sh) | Snapshot and restore a protected connectivity-changing trial. |
| [mesh-config-write.py](../MANET/node_tools/mesh-config-write.py) | Write a literal configuration value without treating it as replacement syntax. |
| [manet_config_io.py](../MANET/node_tools/manet_config_io.py) | Shared literal rendering and durable permission-preserving file replacement. |
| [manet_config_journal.py](../MANET/node_tools/manet_config_journal.py) | Persist, resume, verify, and roll back local configuration jobs. |

## Shared service hosting

Read [shared services](guide/content/services.md).

| File | Responsibility |
|---|---|
| [mesh-throughput-mean.sh](../MANET/node_tools/mesh-throughput-mean.sh) | Aggregate best BATMAN throughput paths into the election metric. |
| [mesh-service-election.py](../MANET/node_tools/mesh-service-election.py) | Rank fresh eligible hosts with incumbent bias and deterministic ties. |
| [manet_election_runtime.py](../MANET/node_tools/manet_election_runtime.py) | Skip settled MediaMTX and Mumble passes after checking winner, unit, listeners, and VIPs. |
| [manet-service-common.sh](../MANET/node_tools/manet-service-common.sh) | Shared election flow: prepare the service, claim and announce VIPs, wait for a successor. |
| [manet_service_handover.py](../MANET/node_tools/manet_service_handover.py) | Announce VIP claims, wait for the winner's claim, and republish the serving flag. |
| [mediamtx-election.sh](../MANET/node_tools/mediamtx-election.sh) | Acquire/release MediaMTX service and virtual addresses. |
| [mumble-election.sh](../MANET/node_tools/mumble-election.sh) | Acquire/release Mumble and synchronize its database around ownership changes. |
| [mtx-ip.sh](../MANET/node_tools/mtx-ip.sh) | Derive the stable MediaMTX IPv6 service address. |
| [mumble-ip.sh](../MANET/node_tools/mumble-ip.sh) | Derive the stable Mumble IPv6 service address. |
| [syncthing-peer-manager.sh](../MANET/node_tools/syncthing-peer-manager.sh) | Discover Syncthing peers and reconcile the configured shared folder. |

## Time and voice

Read [time qualification](guide/content/time.md) and [PTT transport](guide/content/voice.md).

| File | Responsibility |
|---|---|
| [gps-reader.py](../MANET/node_tools/gps-reader.py) | Read gpsd and publish local GPS observations. |
| [mesh-time-sync.py](../MANET/node_tools/mesh-time-sync.py) | Own chrony source roles, bounded peer attempts, refresh, and holdover. |
| [one-shot-time-sync.sh](../MANET/node_tools/one-shot-time-sync.sh) | Retained service entry point for the time controller. |
| [chronyc](../MANET/node_tools/chronyc) | Supplied chrony control/query executable. |
| [mesh-voice.py](../MANET/node_tools/mesh-voice.py) | Manage physical PTT, multicast RTP, and per-speaker audio pipelines. |
| [manet-voice-setup.sh](../MANET/node_tools/manet-voice-setup.sh) | Install voice dependencies required after a tools update. |

## Location and experimental positioning

Read [ATAK admission and positioning limits](guide/content/location.md).
The positioning/ranging implementation is experimental and default-off;
the presence of these files is not evidence of a working field capability.

| File | Responsibility |
|---|---|
| [manet-atak.py](../MANET/node_tools/manet-atak.py) | Admit local wired/AP ATAK phones from mesh SA or unicast and own their CoT I/O and retained state. |
| [manet_phone.py](../MANET/node_tools/manet_phone.py) | Select location/feedback for the admitted phone from supplied observations. |
| [manet_cot.py](../MANET/node_tools/manet_cot.py) | Build CoT XML and parse untrusted marker candidates. |
| [manet_tak.py](../MANET/node_tools/manet_tak.py) | Decode bounded mesh v1/XML self SA into the shared phone observation type. |
| [manet_gnss.py](../MANET/node_tools/manet_gnss.py) | Track per-receiver fix freshness and anomaly hints independently of system-clock slew. |
| [manet-positioning.py](../MANET/node_tools/manet-positioning.py) | Orchestrate experimental ranging/monitoring/solving and socket-free simulation. |
| [manet_ranging.py](../MANET/node_tools/manet_ranging.py) | Event-driven data/ACK ranging protocol and timing state; not 802.11mc FTM. |
| [manet_ranging_radio.py](../MANET/node_tools/manet_ranging_radio.py) | Gate experimental radio controls on capture/association evidence. |
| [manet_locate.py](../MANET/node_tools/manet_locate.py) | Solve positions/layouts from supplied ranges, anchors, and uncertainty models. |
| [manet_spoof.py](../MANET/node_tools/manet_spoof.py) | Compare ranges and GNSS geometry, retaining disagreement/quarantine without claiming authenticity. |
| [ranging.proto](../MANET/node_tools/ranging.proto) | Source schema for ranging messages, separate from transport authorization. |
| [ranging_pb2.py](../MANET/node_tools/ranging_pb2.py) | Committed generated Python ranging binding. |

## Web status, measurements, and diagnostics

Read [dashboard evidence](guide/content/dashboard.md),
[measurements](guide/content/measurements.md), and [storage](guide/content/storage.md).

| File | Responsibility |
|---|---|
| [mesh-status.py](../MANET/node_tools/mesh-status.py) | Serve status APIs and the embedded authenticated management interface. |
| [manet_manage.py](../MANET/node_tools/manet_manage.py) | Management UI/actions, local measurement jobs, saved-session exports and deletion. |
| [manet_peer_radios.py](../MANET/node_tools/manet_peer_radios.py) | Render peer interface details from replicated telemetry. |
| [manet_recovery_status.py](../MANET/node_tools/manet_recovery_status.py) | Combine local channel/time/recovery observations without contacting peers. |
| [manet_web_limits.py](../MANET/node_tools/manet_web_limits.py) | Bound web connections, collection/cache work, and management concurrency. |
| [manet_web_sessions.py](../MANET/node_tools/manet_web_sessions.py) | Keep independent local login sessions with monotonic absolute expiry. |
| [manet_measurement_storage.py](../MANET/node_tools/manet_measurement_storage.py) | Enforce measurement storage admission/save limits without pruning results. |
| [manet-mesh-census.py](../MANET/node_tools/manet-mesh-census.py) | Observe bat0 traffic headers without probes, promiscuity, or saved payloads. |
| [manet-cpu-sample.py](../MANET/node_tools/manet-cpu-sample.py) | Measure service-tree CPU use and aggregate system load over a timed interval. |

## Provisioning, packages, and base system

Read [first boot](guide/content/first-boot.md), [updates](guide/content/updates.md),
[customization](guide/content/customization.md), and [development](guide/content/development.md).

| File | Responsibility |
|---|---|
| [radio-setup.sh](../MANET/node_tools/radio-setup.sh) | Configure discovered radios, network roles, and services from stored setup. |
| [manet-wait-radios.py](../MANET/node_tools/manet-wait-radios.py) | Bound boot enumeration before assigning persistent interface roles. |
| [manet-radio-names.py](../MANET/node_tools/manet-radio-names.py) | Finish MAC-pinned interface naming before networking. |
| [manet-mt7916-firmware.py](../MANET/node_tools/manet-mt7916-firmware.py) | Prepare default patched WM or pinned-stock fallback, boot RX reservation, and recovery state. |
| [manet-morse-bcf.py](../MANET/node_tools/manet-morse-bcf.py) | Apply the 30 dBm TX power policy and CRCs to the stock Morse BCFs at boot. |
| [manet-mt7916-setup.sh](../MANET/node_tools/manet-mt7916-setup.sh) | Enable firmware-selection/survival integration during install/update. |
| [manet-kernel-protect.py](../MANET/node_tools/manet-kernel-protect.py) | Protect MANET boot files from replacement by distribution package transactions. |
| [manet-cpu-policy.sh](../MANET/node_tools/manet-cpu-policy.sh) | Keep CPUs available with demand scaling and the CM4 thermal policy. |
| [manet-os-cleanup.py](../MANET/node_tools/manet-os-cleanup.py) | Plan/apply guarded base-image cleanup while protecting runtime dependencies. |
| [manet-user-scripts.sh](../MANET/node_tools/manet-user-scripts.sh) | Run operator hooks and record completed attempts independently of provisioning success. |
| [manet_release.py](../MANET/node_tools/manet_release.py) | Select a published stable/development release and validate its manifest. |
| [node-update.sh](../MANET/node_tools/node-update.sh) | Enter the Python updater or resume using the retained recovery copy. |
| [node-update.py](../MANET/node_tools/node-update.py) | Verify, stage, replace, and activate an update with durable recovery state. |
| [manet-release-bootstrap.py](../MANET/node_tools/manet-release-bootstrap.py) | Apply an embedded CM4 archive after the legacy updater finishes, retaining recovery until kernel confirmation. |
| [manet-release-setup.py](../MANET/node_tools/manet-release-setup.py) | Verify and install the native follower hostapd package, APT pin, and regulatory database. |
| [manet_kernel_update.py](../MANET/node_tools/manet_kernel_update.py) | Validate matched kernel inputs, derive local CM4 field profiles, honor operator opt-out, and manage trial health, confirmation, and fallback. |
| [manet-auto-update.sh](../MANET/node_tools/manet-auto-update.sh) | Gate routine updates on opt-in, clock sync, and a direct or mesh-gateway route; spread mesh checks with a bounded delay. |
| [version.txt](../MANET/node_tools/version.txt) | Runtime package version; must match the paired source version when publishing packages. |

## Power, indicators, and recovery

Read [power and shutdown](guide/content/power.md) and
[troubleshooting](guide/content/troubleshooting.md).

| File | Responsibility |
|---|---|
| [manet-provision-status.sh](../MANET/node_tools/manet-provision-status.sh) | Report the durable provisioning verdict and optional script results. |
| [manet-led-status.sh](../MANET/node_tools/manet-led-status.sh) | Present that provisioning verdict on supported onboard LEDs. |
| [manet-led-common.sh](../MANET/node_tools/manet-led-common.sh) | Own optional external GPIO configuration and shared display locking. |
| [led-boot.sh](../MANET/node_tools/led-boot.sh) | Display external boot/connectivity progress without blocking boot completion. |
| [led-info.sh](../MANET/node_tools/led-info.sh) | Display a bounded one-shot direct-neighbor indication. |
| [button-monitor.sh](../MANET/node_tools/button-monitor.sh) | Wait for GPIO edges and request the external neighbor display. |
| [mesh-neighbor-count.py](../MANET/node_tools/mesh-neighbor-count.py) | Count distinct direct neighbors, with separate unknown and connected-but-uncounted outcomes. |
| [manet-power-status.sh](../MANET/node_tools/manet-power-status.sh) | Report supported boards' live and sticky power/throttling events. |
| [mesh-shutdown.sh](../MANET/node_tools/mesh-shutdown.sh) | Announce a shutdown tombstone before systemd completes shutdown. |
| [mesh-clone-identity.sh](../MANET/node_tools/mesh-clone-identity.sh) | Reset cloned machine/SSH identity and allocation state after hardware changes. |
| [ssh-recovery.sh](../MANET/node_tools/ssh-recovery.sh) | Restore the configured SSH account and SSH service for headless access. |

## Maintaining this map

Each production file has one row, linked with its exact relative path. The
guide builder compares these rows with top-level production files in
`MANET/node_tools`; it excludes the README and `test_*` files, and does not
inventory test-fixture directories. A missing, duplicate, or removed file
fails the build. Generated `node-manager.sh` must not enter the source tree.

When adding a tool, document its role here and its result/mechanism in the
appropriate three-tier chapter. A passing inventory check confirms presence,
not technical accuracy. Follow the [maintenance guide](guide/README.md)
and [coverage map](guide/coverage.md) for the other repository areas.
