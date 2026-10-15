# Node tools internals

Engineering record for the node runtime: reasoning,
measurements, and approaches that were tried and rejected. Operator-facing
behaviour and the config keys live in
[`MANET/node_tools/README.md`](../MANET/node_tools/README.md) and
[`MANET/networkd-dispatcher/README.md`](../MANET/networkd-dispatcher/README.md).

This file is for someone developing on the project. It is not user
documentation.

---

## Coding conventions

Node shell scripts use `#!/bin/bash`. Use `#!/bin/sh` only for POSIX shell
code that passes `dash -n`, and `checkbashisms` when available. Laptop
provisioning scripts keep their own interpreter choices. Keep existing shell
error and tracing options; changing them requires a separate behavior review.

Use `printf` with a literal format for variable messages and data. Diagnostics
include the program name and go to stderr; reserve stdout for command output.
Let the journal add timestamps, and retain timestamps in standalone log files.
Share identical log helpers where the existing common helper is already loaded.
Shell commands reject bad arguments with `usage: name ...` on stderr and exit
1. Existing `-h` help prints to stdout and exits 0. Python CLIs use argparse
for argument errors and report expected operating errors as `prog: message`
without a traceback. Check subprocess results before recording success;
optional probes may fail only when the result still represents unavailable data.

Keep new and rewritten lines within 80 columns. Preserve indentation and leave
unrelated long lines alone. Do not apply C formatting rules to shell or Python.

Operator command manuals use mdoc in `MANET/man/man8/<command>.8`, including
the command's literal `.sh` or `.py` suffix. Keep options, files and exit status
in step with the CLI. Lint with `mandoc -T lint`, or `groff -mandoc -ww -z`
when mandoc is unavailable. `test_coding_conventions.py` checks the required
command set and parses every section 8 page with an available formatter.

Service restrictions follow actual files, devices, socket families and child
commands. Prefer a dedicated service user where possible. Use
`NoNewPrivileges=yes` and `ProtectSystem=strict` with explicit writable paths,
then narrow capabilities, home access, devices and process features as the
service permits. Account for atomic file replacement, hardware hot-plug and
helpers that control other units. Document necessary exceptions beside the
settings. Static tests guard the baseline; service restarts and functional
checks on the target hardware are required before deployment.

`wpa_cli` and `wpa_cli_s1g` bind a client socket in the host's `/tmp`, and the
supplicant sends its reply to that path. A service that runs them needs `/tmp`
in `ReadWritePaths` and must not use `PrivateTmp`, which hides the socket from
the supplicant. `mesh-channel-agreement.service` runs them for ACS
reconfiguration, drift repair, supplicant readiness and AP-to-mesh
transitions. Without `/tmp`, each call failed with a read-only file system, so
channel moves fell back to supplicant restarts and the AP radio could not
return to the mesh after a gateway demotion.

The GPS reader uses a dynamic `manet-gps` user and `/run/manet-gps`. Its
privileged startup command publishes the existing `/run/gps_*.json` names as
symlinks using the shipped tmpfiles configuration. The runtime directory is
preserved through service stops so the first fix survives a restart; reboot
clears it. Other services retain root where they need device access, protected
configuration or systemd control. A restricted service that asks systemd to
start another service does not transfer its restrictions to that service.

### Comments

- Give every function, method and class a docstring: what it does and, when
  it fits in a clause, why.
- Put a brief comment over any mechanism that is not obvious on first
  reading: a regex, an ordering dependency, a timeout, a fallback, a parse
  of tool output, a workaround. Do not restate the code.
- Use short sentences or fragments in the present tense. No first person,
  narrative, rhetoric or capitalized emphasis.
- Describe current constraints. Leave history such as "old pipeline",
  "an earlier revision", "used to" and "was the bug" in the Git log.
- Keep facts that prevent regressions: measured values, units, limits,
  hardware quirks, ordering constraints and references to functions or files.
  Compact measurement tables are fine.
- Limit rejected alternatives to one line, only where a reader might retry
  them: "Tried: X, Y. No effect."
- Use one line to name a section, then state its constraints.
- For variables and constants, explain meaning, units, range and default.
- Start docstrings with one imperative or descriptive summary line. Add
  arguments, return values and side effects only when they are not obvious.
- Keep most comments to one to three lines. Blocks longer than about eight
  lines need data, such as a table or measured response, to justify their size.
- Keep lines within 80 columns. Do not use em dashes.

---

## Packaging and install
Everything here is installed to `/usr/local/bin/` on the node. The current
version is in `version.txt`; `MANET/etc/manet_version.txt` must always match it,
because `node-update.sh` compares the two to decide whether a node is current.

Install tarballs are root-relative (`boot/`, `etc/`, `usr/`, `root/`) and packed
with numeric owner/group `0/0`. Keep the shipped binaries marked as binary in
`.gitattributes` (`morse_cli`, `chronyc`, `alfred`, `batctl`, `wpa_cli_s1g`,
`wpa_supplicant_s1g`, `wpa_supplicant_manet`, `wpa_cli_manet`), or line-ending
normalization corrupts them.

All four archive builders source `MANET/packaging/lib-man.sh` and call
`stage_man_pages` to install `MANET/man/man8/*.8` as mode 0644 under
`/usr/local/share/man/man8`. The tools updater accepts these paths through its
existing archive validation. On a node, use `man 8 radio-setup.sh`, for example.

### Field update policy

End users may seal radios in enclosures. Routine maintenance must not assume
access to the storage device or require opening the enclosure to reflash it.
The `*-tools.tar.gz` name identifies the update archive; it does not limit
the kinds of files a release may deliver. Updates may carry any required
component, including kernels, device trees, modules, firmware, binaries,
services, interface definitions, and configuration migrations.

The builder chooses the payload for each release. The updater validates
archive structure, integrity, ownership, paths, and available space rather
than excluding components by category. Operator settings and identity must
be preserved unless a release deliberately migrates them. A generic copy of
`mesh.conf` is not a substitute for such a migration. `node-manager.sh`
remains a generated selector symlink and is regenerated from the installed
configuration after payload installation.

The current tools builder includes a managed MT7916 firmware set under
`usr/local/share/manet/firmware/mt7916`; firmware delivery is already part of
the update path. It does not yet include board-specific kernel overlays.
Before shipping a kernel update, the release needs a matched board/kernel,
modules, device trees and firmware set, boot-filesystem handling, module
dependency generation, activation/reboot sequencing, and tested recovery
from interruption or a failed boot. Existing per-file replacement and
service checks do not establish those guarantees. The current archive size
limits and early recovery mount ordering also need review for that payload.
These are implementation requirements for field updates, not reasons to
make reflashing the normal maintenance procedure.

### Tools update failure handling

`node-update.sh` executes `node-update.py`. Loading the Python program before
installation allows it to replace its own installed sources safely. A nonblocking
`flock` on `/run/manet-update.lock` covers the entire update. Each attempt has a
private `download-*` directory under `/var/lib/manet-update`, removed on ordinary
success or failure. The next invocation removes abandoned staging directories
under the lock, without following symlinks or touching unrelated state files.
After archive validation, it also removes exact updater temporary-file/link
names in the payload's destination directories, bounding leftovers from hard
stops during live replacement.

Downloads use HTTPS, bounded curl retries/timeouts, and limits of 4 MiB for
release metadata, 1 KiB for checksum files and 64 MiB for tools archives. Builders emit a single-line
SHA-256 sidecar naming the exact archive basename. The updater requires a hash
match, reads gzip through its footer (with a 512 MiB expanded limit), and rejects
unsafe paths, duplicate names, hardlinks/devices/sparse files, non-root ownership,
special mode bits, writable directories, and symlink traversal. Relative symlinks
must target regular files included in the archive. Existing directory modes are
preserved, and installation cannot traverse existing directory symlinks.
Members may target any root-relative system path that passes these structural
checks, including boot files, modules, firmware, and interface definitions.
Payload selection and activation follow the [field update policy](#field-update-policy).

Both embedded version files must match each other and the advertised release.
Required updater, manager, status, dependency, agreement, time-service and MOTD
files must be present, including the node-manager service drop-ins.
The updater estimates staging plus installation space per filesystem, leaving
128 MiB headroom. It stages regular files and resolves the admin dependency.
Before replacing payload files, it durably stores the verified compressed
archive, checksum, release identity, and the running updater and release helper
in a private `recovery` directory. Boot recovery units and their enable links
are persisted before the `in-progress` marker is created. Installation space is
checked again after retaining the recovery copy. Files are copied to temporary
siblings, fsynced and
renamed; the version files are withheld until the end. Required commands have
checked exit status and timeouts; timeout/interruption terminates their process
groups so child installers cannot continue after the updater exits.

The selected static/ACS manager is regenerated, MOTD links refreshed, and systemd
reloaded. Both mesh-status and node-manager are restarted on every installation
attempt, including retries. They and the agreement/time services are checked
active; the latter follow node-manager through their drop-ins and `PartOf=`.
Other node_tools processes use try-restart: gps-reader, manet-positioning,
manet-atak, mesh-voice, button-monitor, sae-watchdog, batman-enslave-watch and
led-boot. Setup oneshots are excluded. Only services active before installation
have an additional active-state check. Their names are saved with the boot ID
in recovery/runtime-active.json, so a failed restart still blocks completion on
a retry in the same boot. A failed stopped service may need an explicit start
before try-restart can recover it. Only then are the version files replaced and the retry marker removed. This fixes false success after extraction,
copy, dependency or service failure. It does not provide whole-update rollback
or guarantee that radio functionality is healthy just because services are active.

After interruption, recovery precedes release selection, version equality and
the routine throttle. It revalidates the local archive, restages it and retries
the same release without downloads or APT. The shell entry point uses the saved
updater when a marker exists. Boot recovery invokes that saved updater directly:
the early unit only replaces payload files and generated manager/MOTD links,
then reloads systemd; the later unit runs the normal activation and version
commit after `multi-user.target`, avoiding a restart job waiting on the same
boot target as its caller. Early ordering follows MT7916 firmware preparation's
mount dependencies and runs before coldplug, avoiding a
`local-fs.target`/boot-partition cycle.
Missing/corrupt recovery data remains available for diagnosis and fails closed.
The recovery directory is deleted only after durable removal of the pending
marker. This mechanism repairs interrupted payload installation, not partial
APT operations before arming or a release with defective application behavior.
Error details go to the journal even in routine mode. Ethernet carrier still
triggers discovery of new releases; boot recovery never discovers a new release.

### Bounded logs and measurements

The dedicated logrotate timer uses its own state file and an explicit list of
MANET logs, avoiding changes to application log policies or the global rotation
schedule. A 5 MiB threshold, three archives and delayed compression retain useful
history. Checks run every fifteen minutes; the threshold is not a hard quota.
`copytruncate` preserves long-lived append descriptors but can lose lines during
the copy. The persistent journal configuration is unchanged.

`manet_measurement_storage.py` checks allocated/logical bytes, regular-file
count, session count, individual result size and filesystem free space. One
measurement worker is admitted by the web handler's existing lock. Capacity is
checked before admitting a batch, before each test and again at each save;
result JSON is published using the existing durable atomic-write helper.
Admission reserves room for one maximum-size result. Existing sessions are
never pruned; exhaustion is reported through the normal measurement error UI.
Operator overrides live in service environment drop-ins so tools updates do not
overwrite their choices. Limits apply only to MANET measurements, not other
services or synchronized files; they are not filesystem-wide quotas.

Validation (2026-10-08): local and CM4 temporary-root tests cover abrupt process
death during replacement, offline recovery with the original updater, corrupt
recovery data, activation retries, retained user files, storage exhaustion and
real log rotation with an open append descriptor. The full local suite's socket
and PowerShell cases passed when rerun outside the restricted sandbox. CM4
tools/install archives were rebuilt and their recovery/rotation entries checked.
No live updater installation or physical power-cut boot test was performed;
those remain necessary before deploying this recovery path to field radios.

---

## Core orchestration

`mesh-acs-common.sh` supplies the role/config handling shared by the ACS
orchestrator, channel election and tourguide. Explicit `mesh_24_if` and
`mesh_5_if` files are authoritative: an empty file is an absent band, not a
request to guess from `mesh_if` array positions. Readiness and lobby detection
consider enabled roles only. One band is sufficient; no conventional Wi-Fi
roles means status/IP management continues without Wi-Fi ACS activity.
Helper adoption and partition merge require at least one shared enabled band,
write only the channels supplied for those bands, and preserve others.

`radio-setup.sh` assigns roles by capabilities even with one standard Wi-Fi
radio, so a lone 5 GHz interface is not mislabeled as 2.4 GHz. A dual-band
interface assigned to the 5 GHz role starts on 5180 rather than the first
frequency its phy supports. Reserving an interface for AP use clears either
mesh role that referred to it, but preserves the original band in
`/var/lib/ap_mesh_band`. An empty saved band denotes AP-only hardware.

Scan frequencies are filtered against the interface's phy before the request
goes out (`phy_usable_freqs`). `iw scan freq` refuses the **whole** request if
any single frequency is not permitted on that phy, so one bad entry takes out
the scan for every channel on the radio and the only symptom is an empty
survey. The filter fails open: if the phy cannot be read, or nothing parses, the
requested list is used unchanged, because filtering to an empty set would take a
band off the air on every node at once.

**A solo discovery node never elects itself onto data channels.** With a qualified
clock it follows the rotating rendezvous schedule; without one it parks on the
fixed anchors. Any BATMAN peer contact stops rotation while nodes recover or
bootstrap together. Authenticated confirmation of a live destination ends
discovery, including when the visited channel already is the data channel.
The explicit mode survives daemon restarts; a frequency match alone cannot
identify whether a node is searching or operating a data plan.

**`node-manager.sh` is a selected symlink.** Packages carry the two real
variants and `node-manager-select.sh`, not a copied `node-manager.sh` source.
The selector atomically points the symlink at the ACS or static variant from
`acs=` in `/etc/mesh.conf`, accepting `y`/`yes`/`1`/`true` case-insensitively.
The service's `ExecStartPre` runs it before every start. Provisioning, setup,
and configuration application also invoke the selector when needed. The
updater installs the matching symlink directly using the same configuration
choice.

The `--service-start` path records the running target in
`/run/node-manager.running`. Configuration application uses that record to
compare the running and selected variants before restarting the manager.
This keeps the entry point tied to configuration across both updates and
reboots. See [Mesh Configuration Push](#mesh-configuration-push).

---

## Web interface

There is no unauthenticated route that changes anything. A set of
`/api/control/*` handlers at the site root used to apply interface, TX power and
channel changes locally, behind nothing but the subnet check, and were removed
once every caller had moved to the Alfred-staged path; a local change and a
mesh-wide one now take the same route through `manet_manage.py`.

**manet_web_sessions.py**

Management logins use a fresh 256-bit random token per session. The threaded
web process stores only the token's SHA-256 digest and deadline, under a lock.
The 48-hour absolute limit has no idle timeout, allowing a login to cover a
typical deployment. Deadlines use monotonic elapsed time, so GPS/NTP corrections
cannot extend or prematurely end a session. No session state is written to disk
or announced to peers; a service restart or reboot requires a new login.

Login and validation read the current admin password under the same lock. A
changed or missing password clears all sessions when observed. An unrelated
config edit preserves them. Logout revokes the presented token; successful
re-login replaces that browser's token. Each node keeps at most 64 sessions,
discarding expired entries first and then the oldest if a new login needs room.

The cookie is HttpOnly with SameSite=Lax. Its Max-Age matches the server's
lifetime, but the server enforces expiry and revocation independently. Login,
logout and dynamic HTML/JSON/CSV responses use `Cache-Control: no-store`.
Management APIs return JSON 401 for invalid sessions; the dashboard returns to
login while retaining its tab and query. Both form-login aliases and the JSON
login endpoint use the same store. `test_web_sessions.py` checks token replay,
expiry, concurrent requests, password changes and HTTP route authorization.

Login failures share a monotonic sixty-second window across all login aliases:
five failures per client address, thirty per node. The bounded list is updated
under the session lock. HTTP 429 includes `Retry-After`; throttling does not
revoke established sessions or grow memory with attacker-selected addresses.

Both pages treat registry values as display text. The shared esc() helper
encodes HTML text and quoted attributes. Dynamic control arguments use DOM
data attributes instead of interpolated JavaScript strings. The voice peer
table uses textContent. Registry parsing bounds display names without
restricting them to DNS labels. DNS publication has its own stricter policy.
Measurements admit only a local source, record the actual executing hostname
and IP, and require a received packet for ping success.

**Browser content policy**

`MeshHandler.send_html()` creates a fresh 256-bit random nonce and passes it
to the status, dashboard or login renderer. Only trusted template script tags
receive it. Rendered user data is never scanned to authorize additional
scripts. `end_headers()` applies the same policy to every handler response,
including standard-library HTTP error pages, and clears the response nonce
before a persistent connection handles another request:

```text
default-src 'self'; script-src 'nonce-<response nonce>';
script-src-attr 'none'; style-src 'self' 'unsafe-inline';
connect-src 'self'; img-src 'self' data:; object-src 'none';
base-uri 'none'; frame-ancestors 'none'; form-action 'self'
```

There is no script `unsafe-inline` or `unsafe-eval` permission. Delegated click
listeners dispatch fixed action names and pass escaped data attributes as
literal arguments. They cover radio and session controls rebuilt by polling,
and ATAK controls inserted into node details. Talk-group buttons attach their
listeners as they are created. Login theme selection and password autofill
submission are also nonced scripts. HTML remains `no-store`.

Inline styling remains allowed: the two page modules contain 90 `style=`
attribute sites (42 status/login, 48 management), and shared recovery markup
adds four, for 94 source sites. These include JavaScript-generated rows, whose
counts vary with peers and measurements. Three template `<style>` blocks
provide the status, login and dashboard styles, one per rendered document.
The UI also updates element styles for progress, visibility and canvas layout.

No external browser resource needs an exception: logos use local `/assets/`
URLs, typography uses installed Roobert or system fallback fonts, topology is
drawn on canvas, and all fetches and forms use this origin. The management
module's GitHub reachability probe runs on the node, not in the browser.
`X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin` accompany
handler responses. CSP adds no changes to the authenticated admin status JSON,
including its existing credential fields.

`test_web_csp.py` checks HTML aliases, login failures, HTTP errors, nonce
freshness across requests and persistent connections, static templates and
generated markup, and literal event dispatch. Hostile-metadata rendering tests
continue to verify escaping. Browser click-through remains the check for
actual CSP enforcement and interactive layout behavior.

**Web resource limits and recovery status**

`manet_web_limits.py` bounds the HTTP worker count at eight and concurrent status
collectors at two. Fixed cache keys share status work across clients; a busy
collector returns 503 instead of starting duplicate subprocesses. Management
POSTs are serialized and invalidate cached observations when they finish.
Authentication is checked before serving any cached management data. Body
validation rejects oversized, ambiguous and incomplete requests before routing
them to actions. Reads have a ten-second socket timeout and bodies also have a
ten-second elapsed-time deadline.

`mesh-channel-agreement.py` writes a local observation to
`/run/manet-acs-status.json` each tick. Successful automatic channel changes
record their reason in `/run/manet-last-channel-change.json`. Both are volatile;
failure to write display state cannot stop channel recovery. No extra Alfred
fields or messages are added. `manet_recovery_status.py` combines these with the
existing time-client state and registry, and rejects observations older than
45 seconds as current evidence. A HaLow route is identified by its assigned
interface, while HaLow readiness only explains why Wi-Fi tours are suppressed.

`test_mesh_recovery_sequence.py` runs three independent ACS instances with real
authenticated messages, simulated radios and delayed/lost Alfred delivery. It
covers missing votes, restart before activation, straggler recovery, complete
link loss and reconnection, and the existing advertisement interval. This does
not measure physical switching time or RF airtime.

**manet_radio.py**

Radio primitives shared by the UI that offers a change and the code that
applies one: `mesh-status.py` and `manet_manage.py` read state and build the
menus, `mesh-radio-state.py` applies an Alfred-staged package. One
implementation, so what the UI offers and what the node does cannot diverge.

**The HaLow channel plan is derived from the node's region.** The channel,
bandwidth and S1G operating-class tables are transcribed from the Morse driver's
`dot11ah` tables, and a bandwidth appears for a region only where the driver
defines a channel of that width and the Morse supplicant will join it. **US
reaches 8 MHz.** **EU is 1 MHz only**: the 863–868 MHz allocation has no room
for 4 or 8 MHz, and `wpa_supplicant_s1g` 1.16.4 rejects every EU 2 MHz mesh
config ("Invalid S1G configuration of operating class, country code and
channel", channels 2 and 6, op_class 67 and 7).
`halow_channel_options()` builds the menu the Radio config tab renders, so an
EU node is never offered a width it cannot use. Channel numbers and center
frequencies are both unique within a region, so either resolves the other:
`halow_bandwidth_for_channel()` recovers the width from the channel number,
which is how the status readout avoids `s1g_prim_chwidth`; that reports the
*primary* channel width, 2 MHz for every operating width above 1 MHz, and
reading it as the operating width reports a 4 or 8 MHz channel as 2 MHz.

Wi-Fi TX power options come from the PHY's own advertised channel range
(`parse_phy_txpower_options`). A request above that range is refused
(`txpower_request_allowed`, `unsupported_txpower_response`).
`set_iface_txpower_verified` resolves `/sys/class/net/<if>/phy80211/name`
(under `MANET_SYS_NET` when set) and uses `iw phy <phy> set txpower fixed`.
mt76 ignores per-interface power requests. A missing or invalid PHY, or another
UP interface on the same PHY, is refused before the write. The helper reads
power back through `iw dev <if> info` and returns the reported value, which may
be lower than requested when the card limits itself. No reported power, or a
value still above the request, is an error. Static channel apply and rollback
use the same helper. HaLow live power changes remain refused.

UI power requests are applied from the staged radio-state transaction, but
`record_current_state` only saves interface up/down state and metadata in
`/var/lib/mesh_radio_state.json`. There is no saved power reapply after a
radio restart or reboot. The boot mesh power service requests 30 dBm; an
AP-to-mesh transition requests the same default.

`manet-region.py apply` is the runtime counterpart of radio-setup's region
writes. `mesh-config-apply.sh` runs it when a `regulatory_domain` change is
applied, so a web UI change reaches `/etc/modprobe.d/{cfg80211,morse}.conf`,
`/etc/default/crda`, hostapd's `country_code` and every supplicant's country
before the reboot that loads them. A US/EU plan change also rewrites the
HaLow supplicant to that region's template channel (`HALOW_DEFAULT_CHANNEL`,
checked against `radio-setup.sh` by `test_region.py`, as is the EU country
list). Within one plan the operator's HaLow channel is kept. Verified on
two CM4 nodes: DE to US through the apply step, reboot, both nodes up on
907 MHz / 2 MHz.

An unknown region falls back to the EU plan, the narrower of the two, so a
misconfigured node cannot be offered channels its region may not permit.

**manet_peer_radios.py**

Builds the per-peer radio chips and the expandable status panel the topology
views show for another node: role, up/down state, channel, MCS, service pills,
and the inferred `bat0` / `br0` / gateway rows.

Everything comes from what Alfred already replicates, so no node is queried.
Where a peer publishes `INTERFACES_JSON`, that wins. Where it publishes an empty
list, as `node-manager` did historically (leaving the UI with no
`wlan0`/`wlan1`/`wlan2` keys and every peer rendered as 2.4G/5G/HaLow OFF), the
values are reconstructed from the registry's MCS and `DATA_CHANNEL_*` fields, so
an older node still shows its radios.

**manet-ui-firewall.sh**

Installs the nftables rules described above: port 80 restricted to localhost and
this node's DHCP pool, port 5201 (iperf3) to the mesh subnet. Uses source
addresses rather than interfaces, because `br0` bridges `bat0`; a packet from a
remote node arrives on `br0` exactly like one from a local EUD. Re-run by
`mesh-ip-manager.sh` whenever the DHCP pool moves. The pool-based policy is
intentional. Replacement runs as one nftables transaction under a local lock;
if any rule fails, the previous table and success marker remain intact. The
marker is replaced only after nft reports success, so a later invocation can
retry. Missing pool data continues to leave existing rules alone.

---

## Voice

GStreamer handles encoding, decoding, buffering and mixing. Python buffer
probes handle source admission, activity counters and decoder revival:

```
TX  alsasrc -> level -> valve -> <enc> -> <pay> -> multiudpsink
RX  udpsrc  -> rtpbin -+-> <depay> -> <dec> -\
                       +-> <depay> -> <dec> --+-> audiomixer -> alsasink
                                (one branch per talker)
```

All nodes must use the same codec. The codec setting determines RTP parameters
and receive decoders; mismatched nodes cannot exchange voice. Codec changes
are staged across the mesh over Alfred (see below). Missing Lyra plugins cause
a logged fallback to Opus, so check for that message after installation.

The daemon also reads the PTT button, keeps the unicast peer list current,
and publishes `/run/mesh-voice.json` for the UI.

**Unicast redundancy defaults off.** With
`multicast_forceflood` disabled, as this node configures it, and listeners at or below
`multicast_fanout` (default 16), `batadv_mcast_forw_mode_by_count()` returns
`BATADV_FORW_UCASTS` and emits **one unicast frame per listener**. Measured on
the bench: 200 multicast packets produced exactly 200 unicast frames addressed
to the peer's MAC on `wlan2`, with no broadcast frames above baseline, and the
peer received all 200. Those frames already get 802.11 ACKs and retries, so a
userspace unicast copy per peer would double airtime for no extra reliability.

`voice_unicast=y` remains available for the two cases where it stops being
redundant: more than `multicast_fanout` listeners, where batman-adv falls back
to `BATADV_FORW_BCAST`; and any future configuration that enables
`multicast_forceflood`. When enabled, `multiudpsink`'s `clients` property is
rewritten live from the registry, including nodes that have never transmitted.

**Receivers must join the group or nothing transmits.** The same optimization
means `batadv_mcast_forw_mode()` returns `BATADV_FORW_NONE`, dropping the
packet at the *sender*, when no node has announced interest in the group. This
was observed directly: multicast sent with no listener never reached the radio
at all. `udpsrc` performs the IGMP join (`auto-multicast=true`), so this works
in normal operation, but expect a brief window after start-up before joins
propagate, and note that a sender with no listeners is silently idle rather
than wasting air.

**Loopback suppression uses both the socket and the RTP source ID.**
`multiudpsink`'s own `loop` property is silently ignored with
`auto-multicast=false`, because GStreamer only applies it on the code path that
also joins the group, so loopback is cleared on the socket instead, reached
through `used-socket`. That call is now retried for up to 5 s: the sink has no
socket until it has started, a GStreamer state change is asynchronous, and the
original code asked once, got `None`, logged a warning and never asked again.
A pipeline that lost that race ran its whole life with loopback on, and what
the operator hears then is their own voice back in the headset one jitter
buffer late.

Any packet arriving with this node's own
SSRC is dropped at the `udpsrc` probe, before `rtpbin` sees it. Each run chooses
a random 32-bit SSRC. A collision remains possible, with probability about 1 in
2^32 for a particular pair of runs. It also
keeps `rx_active` honest, which matters with `voice_half_duplex=y`: without it
a node reads its own transmission as a remote talker and refuses to key.
`rx_loopback` in `/run/mesh-voice.json` counts these; anything other than 0
means the socket-level suppression did not take.

**Note for bench testing:** `IP_MULTICAST_LOOP` has no effect on `lo`. A node
configured with `voice_iface=lo` will always receive its own multicast, and
that is the loopback device, not a bug in the daemon. Verified both ways on a
real interface, where clearing it works correctly.

`voice_half_duplex=y` restores the refuse-to-key behavior if you want it.

A single `rtpjitterbuffer` mixes the two senders' sequence numbers and corrupts
the output. Each talker therefore needs a separate receive branch.
Verified by feeding two senders (440 Hz and 880 Hz, distinct SSRCs) into the
receive pipeline: with one talker only 440 Hz is present; with both, 440 Hz and
880 Hz appear together at comparable amplitude.

A permanent silent input feeds the mixer, because `audiomixer` only produces
output while it has one; without it the sink is starved whenever nobody is
talking and every transmission starts with the DAC spinning up. Branches are
built on `pad-added` and torn down on `pad-removed`, so a decoder is not leaked
per talker; measured, two idle talkers were reaped and the count returned to 0.

**Keeping decode branches avoids clipping the next transmission.**
The receiver retains a talker's branch across normal conversation gaps, then
expires inactive sources using `rtpbin autoremove=true` and a longer native
session timeout. Keeping an established source avoids the first-contact cost.
Measured on a CM4 with lyra, 3.00 s bursts:

| receiver state when the talker keys up | audio arrived | lost |
|---|---|---|
| branch rebuilt on demand | 2.82 s | 180 ms |
| branch pre-built and attached | 2.92 s | 80 ms |
| talker already established | 3.04 s | none |

An established `rtpbin` source avoids this delay. Blocking the pad during
construction, lowering RTP source probation, and raising the mixer's
`min-upstream-latency` were each tried and none of them helped; the residual
is `rtpbin` establishing a new source rather than the pipeline linking, so the
fix is to ensure the source is not new.

**The sender establishes its RTP source.** A receive slot in
`rtpbin` is keyed by **SSRC, not by IP**, and it exists only once a packet
carrying that SSRC arrives. Two pieces close that gap.

First, all **32 SSRC bits identify the run**, chosen randomly on pipeline
construction. The former eight-bit generation could collide with retained
restart state too easily. Talker names now use the UDP sender address matched
to the registry, independent of the SSRC. This does not require a back channel.

A node that reuses its SSRC after restarting has a fresh sequence-number base
that no longer matches the receiver's stored source. In testing, an entire
three-second transmission was lost; neither a beacon nor
`max-misorder-time`/`max-dropout-time` tuning recovered it. A new SSRC
lets the receiver establish a new source:

| after a sender restart | speech arrived of 3.00 s |
|---|---|
| same SSRC reused | **never arrived** |
| new SSRC | 2.96 s |
| new SSRC + beacon | **3.00 s** |

Second, each node sends a **presence beacon**: a ~140 ms muted transmission
(`volume` to 0, valve open, valve shut, volume back) through the normal RTP
payloader, so receivers establish the source before speech begins.

Beacons run at start-up and whenever a new node appears in the registry.
`voice_beacon_sec` (default 600) also refreshes retained sources and covers
missed peer arrivals. Zero disables beacons. The default interval is shorter
than the 15-minute idle threshold, so a connected, quiet peer keeps its
established source. That is about **21 packets an hour, ~5 bps averaged**,
against 420/hour at the former 30-second interval.

**Locally injected RTP cannot establish a peer's source.** Injecting a packet
with a peer's SSRC creates a slot with sequence numbers and timestamps that do
not match the sender. The jitter buffer then resyncs and discards incoming
audio. In testing, a three-second transmission produced silence (peak
amplitude zero), with `rtpjitterbuffer` logging one `resync`. Use the sender's
beacon to establish the source.

**Table size follows the node registry.** Every known node is a potential
talker, and an evicted one pays the first-contact penalty again, so
`voice_max_talkers` (default 8) is a floor rather than a fixed size: on each
registry poll the table is raised to known nodes + 2 headroom. It never
shrinks below the configured value and never exceeds the hard cap of 64.

**Evicted talkers retain a receive pad until expiry.** Removing a decoder does
not remove its native RTP source. An unlinked pad would produce `not-linked`
bus errors, and an existing source does not emit another `pad-added` on speech.

Eviction now links the pad to a `fakesink` and puts a buffer probe on it. The
probe is what `pad-added` would have been: it fires when that talker next
speaks, and the branch is rebuilt. The cost is the same measured ~180 ms of
head loss that eviction was always documented to cost, rather than permanent
silence. `talkers_parked` in `/run/mesh-voice.json` says how many are in this
state.

**Source admission and parking have separate budgets.** Before `rtpbin` sees
a packet, the UDP probe checks its RTP header and sender metadata. It accepts
only the configured payload type and rejects CSRC lists, which could create
additional native source records. There is no RTCP receive input. There are
at most 128 admitted remote sources and 64 parked sinks, independent of the
decoder cap. Reservations prevent concurrent pad callbacks from exceeding
the parking budget. At parking capacity, the evicted stream is retired and
cleared on the existing two-second state timer. Its admission slot remains
charged until native expiry, and further packets for it are dropped.

Each sender IP can retain four identities. Admission uses token buckets with
a per-address burst of two and refill of one every 30 seconds, plus a global
burst of 64 and refill of one per second. Rejected identities create no source
record, sink, decoder, or GLib callback. Existing accepted sources consume no
new tokens. One ordinary restart fits the per-address burst and a new talker
has its own allowance. A full table or repeated rapid restarts can still reject
a new source. These are resource bounds, not authentication of mesh peers.
An admitted SSRC stays bound to its original IP and UDP port, preventing port
rotation from creating native collision-address records for that identity.

**Native expiry releases the complete source.** The internal RTPSession uses
`rtcp-min-interval=180s`, fixed `bandwidth=1000000` bytes/s, and
`timeout-inactive-sources=true`. The fixed bandwidth prevents participant
count from stretching the interval. GStreamer 1.26 removes inactive sources
after five deterministic intervals, at its next maintenance pass. The nominal
15-minute threshold preserves conversation gaps and exceeds the default
10-minute beacon interval. The timeout signal precedes native table removal,
so the state timer releases admission only after `get-source-by-ssrc` returns
no source. See [GStreamer 1.26 RTPSession](https://raw.githubusercontent.com/GStreamer/gstreamer/1.26.0/subprojects/gst-plugins-good/gst/rtpmanager/rtpsession.c).

`rtpbin autoremove=true` removes the demux stream on native timeout.
`max-streams=128` also bounds the demux, but does not bound RTPSession's source
table. Likewise, `clear-ssrc` removes stream objects without forgetting the
session record. This is why retired slots remain charged. Pad removal releases
the decoder, mixer request pad, parked sink, probes and pending revival, and
drops the Python jitterbuffer reference. Sources still in probation are
reconciled even if they never produced a receive pad. See
[GStreamer 1.26 rtpbin](https://raw.githubusercontent.com/GStreamer/gstreamer/1.26.0/subprojects/gst-plugins-good/gst/rtpmanager/gstrtpbin.c).

The native RTCP maintenance thread starts from received RTP even without an
RTCP output pad. No additional polling loop or RTCP network traffic is needed.
See [GStreamer 1.26 rtpsession element](https://raw.githubusercontent.com/GStreamer/gstreamer/1.26.0/subprojects/gst-plugins-good/gst/rtpmanager/gstrtpsession.c).
`udpsrc retrieve-sender-address=true` supplies the sender metadata used for
admission and naming. See [GStreamer 1.26 udpsrc](https://raw.githubusercontent.com/GStreamer/gstreamer/1.26.0/subprojects/gst-plugins-good/gst/udp/gstudpsrc.c).

**The ceiling is RAM.** A warm branch costs about **5.3 MB with lyra** and
**0.6 MB with opus**, measured on a CM4. So the hard cap of 64 is roughly
340 MB of lyra decoders against the 3.4 GB a node has free. That is comfortable, but
it is the reason a cap exists at all rather than tracking the registry without
limit. A mesh larger than 62 nodes on one talk group will log that it is
capped, and the least recently heard talkers will be evicted and pay the
first-contact delay when they next speak. Raising the decoder cap also requires
reviewing the separate admission and parking budgets and measuring CPU and RAM.

`ignore-inactive-pads` on the mixer is required rather than optional once
branches are kept: they are silent between transmissions and the mixer would
otherwise wait on them.

**Buffering: there are no queues.** Neither pipeline contains a `queue`
element. Each is a single push thread from source to sink, and every buffer in
the audio path is one of four things:

| Where | Size | Set by |
|---|---|---|
| ALSA capture and playback rings | driver defaults, not configured | `alsasrc` / `alsasink` |
| Kernel UDP receive socket | 1 MB (`buffer-size=1048576`) | `udpsrc` |
| Per-talker RTP jitter buffer | `voice_jitter_ms`, default 100 ms | `rtpbin latency=` |
| Mixer blend window | internal | `audiomixer` |

The socket buffer is burst tolerance, not a working set: steady-state traffic
is around 20 kbps, and 1 MB is there so a scheduling stall cannot cost
packets before `udpsrc` runs again.

Both sinks run `sync=false`, for different reasons. On transmit `alsasrc` is
the clock for a live capture, so there is nothing for the sink to synchronize
to. On receive the jitter buffer already does the timing, so making `alsasink`
wait on running time as well would only add latency.

**The jitter buffer is per talker and sized once.** `rtpbin` creates one
`rtpjitterbuffer` per SSRC, and its depth is
`max(voice_jitter_ms, two packets)`, the two-packet floor being the least
that can absorb a single late packet. At the default 40 ms packing that
resolves to 100 ms.

That figure is computed when the pipelines are built, from the packing in
force at the time, and nothing resizes it while the pipeline runs. So if
adaptive packing climbs to the top of its range (3 frames, 60 ms packets),
a 100 ms buffer is 1.67 packets deep rather than the 2 the formula intends,
until the next rebuild. (A SIGHUP retune rebuilds both pipelines, so it also
re-sizes the buffer for the packing then in force.) This is a margin
question rather than a fault, and the coupling runs in the safe direction: the
controller shrinks packets under loss, which *deepens* the buffer in packet
terms, and only grows them after 30 s below 1 % loss. Note also that a
receiver's buffer has to suit what the *senders* are transmitting, which this
node cannot know and which the formula has never tracked. If the floor is ever
wanted at its stated value, raise `voice_jitter_ms` to 120 rather than
resizing at runtime; a jitter buffer that resyncs mid-stream discards the
transmission outright, which is the same failure the random per-run SSRC
exists to prevent.

`do-lost=true` makes `rtpbin` emit a gap event for every lost packet so the
decoder conceals rather than glitches; `opusdec` additionally runs `plc=true`
and in-band FEC, and Lyra conceals internally.

**Transmit does not buffer at all.** The PTT valve *drops* upstream buffers
while the button is released rather than holding them, so keying the
microphone plays live audio rather than flushing a backlog of whatever the
capture device collected beforehand. The only delay on the transmit side is
packetization: 20 ms per Lyra frame times `frames-per-packet` must accumulate
before a packet leaves.

**Pipeline rebuilds are checked and can roll back.** A
successful `parse_launch()` is not a working pipeline: a port already bound or
an ALSA device held by something else fails during the *state change*, so both
pipelines are taken to `PLAYING` and then confirmed with `get_state()`. Build
failures are caught as `Exception` rather than `GLib.Error`, because `build()`
reaches for elements by name and hangs pad probes off them, and PyGObject
prints an exception raised inside a signal handler and then swallows it, which
`SIGHUP` arrives through. That combination used to leave the daemon alive with
`self.tx` on the new pipeline, `self.rx` on the old one it had already set to
`NULL`, and `self.valve` on elements of neither: no audio, no error in the
journal, and a state file still saying `running`. If the new group does not
come up the daemon reverts to the previous one; if the revert fails too it
exits non-zero and lets systemd rebuild the whole stack, because by then there
is nothing left in process to fall back to.

**Retuning releases what it replaces.** `gst_bus_add_signal_watch()` attaches a
GSource that holds a reference to the bus, so a bus whose watch is never
removed is never finalised, and every `GstBus` carries a `GstPoll` control
pipe. A retune builds two new pipelines, so a talk group change that only set
the old ones to `NULL` leaked **four file descriptors and two live GSources
every time**. Measured on the bench: 4 fds per retune, and dead flat once the
watch is removed. This is exactly the case a rotary channel selector produces,
and at the default 1024-descriptor limit an operator could reach it inside a
single operation, after which the daemon fails to open the very sockets and
ALSA devices voice needs.

Retuning in place rather than restarting the unit is what makes a hardware
channel selector practical; clicking through groups on a rotary switch would
otherwise mean a systemd restart per detent, several seconds each, plus a
TFLite model reload under lyra. Anything that can write `mesh.conf` and send
`SIGHUP` drives this, so the web UI and a future panel switch share one path.

**Adaptive packing changes packet size.** With
`voice_codec=lyra` the daemon adapts `frames-per-packet` to measured receive
loss while keeping bitrate fixed. On the
HaLow link a batman-adv frame carrying one 20 ms Lyra frame is 101 bytes, of
which 86 is header, so at 6 kbps the packet overhead dominates completely:

| frames/packet | interval | on-air | a lost packet costs | listening test at 10 % loss |
|---|---|---|---|---|
| 1 | 20 ms | 40.4 kbps | 20 ms | clean |
| 2 | 40 ms | 23.2 kbps | 40 ms | barely audible |
| 3 | 60 ms | 17.5 kbps | 60 ms | audible glitches |
| 4 | 80 ms | 14.6 kbps | 80 ms | unpleasant |

Going 1 → 2 frames/packet takes **43 %** off the wire. Dropping the codec from
6000 to 3200 bps takes **12 %** off (23.2 → 20.4 kbps at 40 ms) and is plainly
audible. So bitrate stays fixed and packing moves.

Under loss, packets get smaller. A lost packet takes `frames-per-packet` frames
with it, and the
audibility knee sits exactly in this range, so the loss response spends airtime
to keep each loss short enough for Lyra's concealment to hide. That is only
safe while loss means fades rather than congestion; on a saturated link,
offering more packets makes it worse, so the controller will not go below 2
frames/packet when batman-adv's throughput estimate for the worst neighbor is
under 2 Mbit/s.

Loss above 5 % steps down immediately; recovery needs 30 s below 1 % per step,
and anything in between holds position. Windows with fewer than 25 packets are
ignored rather than treated as clean. The signal is this node's *own* receive loss
(plain multicast RTP has no back channel), which half duplex makes a fair proxy,
since it measures the same link in the other direction moments before keying
up. An asymmetric link will fool it.

No signaling is needed for any of this: Lyra frames are a fixed size per
bitrate (8/15/23 bytes), so `rtplyradepay` recovers the packet geometry from
the payload length alone and follows a mid-stream change with no renegotiation.
Verified on hardware: switching 2 → 1 → 3 → 2 while playing produced 27/42/57
byte payloads and 1001 frames decoded against 1000 sent.

**QoS uses CS6 (48).** The `voice_dscp` value accounts for batman-adv's priority
mapping.

Linux 6.12+ added an RFC 8325 mapping to `cfg80211_classify8021d()`
(`net/wireless/util.c`) that sends DSCP 46/EF to 802.1d UP 6, i.e. WMM AC_VO.
On 6.6 and older the naive `dscp >> 5` rule sent it to UP 5 / AC_VI. We ship
6.18 everywhere, so on a plain wireless interface EF would be correct.

**batman-adv never lets that code run.** `batadv_skb_set_priority()`
(`net/batman-adv/main.c`) is called from `batadv_interface_tx()` for every
packet entering `bat0`, locally originated included, and from both forwarding
paths in `routing.c`. It stamps:

```c
if (skb->priority >= 256 && skb->priority <= 263) return;  /* already set */
prio = (ipv4_get_dsfield(ip_hdr) & 0xfc) >> 5;             /* the OLD rule */
skb->priority = prio + 256;
```

Values 256–263 are the 802.1d passthrough range, which `cfg80211_classify8021d()`
checks **first** and returns directly as the UP, so the RFC 8325 DSCP code is
never reached. The result is kernel-version independent:

| DSCP | TOS | batman-adv priority | 802.11 UP | Access category |
|---|---|---|---|---|
| 46 (EF) | 0xB8 | 261 | 5 | AC_VI (video) |
| **48 (CS6)** | **0xC0** | **262** | **6** | **AC_VO (voice)** |

Hence the default of 48. The `return` guard also means an explicitly-set
`SO_PRIORITY` in 256–263 survives batman-adv, which is how OpenMANET pins the
access category: they set `IP_TOS` and `SO_PRIORITY` together, in that order,
because the kernel rewrites `sk_priority` as a side effect of `IP_TOS`.
GStreamer's `multiudpsink` exposes only `qos-dscp`, so batman-adv's derivation
is relied on instead, which is why the DSCP value has to be chosen for what
batman-adv will make of it.

Multicast TTL is set explicitly to 32: the default of 1 silently black-holes
voice one hop out.

`multicast_forceflood` stays off to allow batman-adv's multicast-to-unicast
fanout.

**Packet overhead.** Every packet carries 12 B RTP +
8 UDP + 20 IP + 14 Ethernet = 54 B of overhead. At 20 ms framing that is 50
packets/sec, so ~21.6 kbps is spent on headers no matter which codec is used.
Measured on-wire cost:

| Opus bitrate | 20 ms frames | 60 ms frames |
|---|---|---|
| 24 kbps | 45.6 kbps | 31.8 kbps |
| 16 kbps | 37.6 kbps | **23.7 kbps** |
| 12 kbps | 33.6 kbps | 19.6 kbps |
| 8 kbps | 29.6 kbps | 15.5 kbps |

Frame size is therefore the larger lever: 16 kbps at 60 ms costs less on air
than 6 kbps at 20 ms (27.7 kbps) and sounds far better. The cost is latency
(a 60 ms frame adds 60 ms) and coarser loss, since one dropped packet now takes
60 ms of audio with it. The jitter buffer floor is raised to two frames at
start-up; see Buffering above for what that does and does not cover. For a
PTT system where the multiplier is unicast redundancy rather than continuous
full-duplex, 40–60 ms is usually the right trade.

**High-pass (on by default, 80 Hz).** Measured, identical at 16 and 48 kHz:

| 20 Hz | 40 Hz | 50 Hz | 60 Hz | 80 Hz | 120 Hz | 300 Hz | 3 kHz |
|---|---|---|---|---|---|---|---|
| -53.2 dB | -27.2 dB | -17.9 dB | -9.6 dB | 0.0 dB | 0.0 dB | +0.2 dB | 0.0 dB |

80 Hz keeps a male fundamental (~85 Hz up) and takes out everything below. Note
60 Hz mains only sees -9.6 dB at that corner; raise the corner toward 120 if
hum rather than rumble is the actual complaint.

**Band-pass (off by default).** Setting `voice_lowpass_hz` as well switches the
stage to `audiochebband`. Narrowing to something like 100-4000 Hz is the
classic walkie-talkie sound: clearer and more cutting on some headsets, thin on
others. It is a taste call that needs a field comparison, which is why it is
off rather than defaulted. Measured at 100-4000 Hz:

| 40 Hz | 60 Hz | 100 Hz | 300 Hz | 1 kHz | 4 kHz | 5 kHz | 6 kHz |
|---|---|---|---|---|---|---|---|
| -36.9 dB | -20.6 dB | -0.2 dB | 0.0 dB | -0.1 dB | -0.2 dB | -7.8 dB | -17.2 dB |

**Filter pole counts follow the measurements.** `audiocheblimit` uses 4:
at 8 poles and 48 kHz, an 80 Hz corner is a
normalised frequency of 0.0017 and the coefficients lose their precision, which
produces **a flat +4.9 dB of gain at every frequency
from 20 Hz to 3 kHz**. `audiochebband` uses 8, because it splits its poles
between the two edges (4 gives a limp -3.5 dB at 60 Hz) and, unlike
`audiocheblimit`, is still stable there at 48 kHz. Both use Chebyshev type 1:
type 2 puts its ripple in the stopband and its cutoff means the stopband edge,
so at the same setting it measures -0.3 dB at 60 Hz against type 1's -9.6 dB.

**Three-band EQ (off by default).** `voice_eq=y` adds `equalizer-3bands` with
fixed centres at 100 Hz, 1100 Hz and 11 kHz, for a formant or presence lift.
`voice_eq_mid=3.0` measures +2.6 dB at 1-1.1 kHz, tapering to +1.5 dB at 500 Hz
and +1.9 dB at 2 kHz, so it is a wide gentle lift rather than a peak.

EQ constraints:

- **The 11 kHz band is forced to 0 under lyra.** Lyra's raw rate is 16 kHz, so
  11 kHz is above Nyquist. At
  16 kHz, `band2=+6` lifted a 1 kHz tone by 2.6 dB and clipped 4608 samples,
  and `band2=-12` cut the same tone by 6 dB. The daemon zeroes it and logs why.
- **Attenuation before the EQ prevents clipping.** The block converts back to
  S16LE at its end, so a boost clips there and no attenuation further down the
  pipeline can undo it. A `volume` element is inserted at the *head* of the
  block and trimmed by the largest positive gain, which makes a boost a change
  of tone rather than a change of level. Measured at the encoder input with a
  0.8 full-scale tone and `voice_eq_mid=6.0`: **37,645 clipped samples with the
  trim defeated, zero with it applied.** The cost is level, not headroom, and
  playback has 20 dB spare.

**Tuning is live.** Every property of these elements is `controllable`, so
`systemctl reload mesh-voice` applies new cutoffs and gains to the running
pipeline with no gap in the audio and no TFLite model reload. That matters
because picking a voice band and a formant lift is a listening test across
headsets, and a listening test is useless if every change costs a restart.
Adding or removing a stage does change the pipeline's shape, and that still
rebuilds, through the same checked path a talk group change uses.

Cost is negligible: the high-pass measured 0.03% of realtime at 16 kHz on a dev
box, so under 1% of one CM4 core even allowing 20x, and a flat equalizer is
about a tenth of that.

**`rnnoise` is not packaged.** There is no stock GStreamer element for it, and
`webrtcdsp` (which has noise suppression, AGC and its own high-pass) lives in
`gstreamer1.0-plugins-bad`, which nodes do not install. Either is a real
addition rather than a config change, and neither has been measured on a CM4.
Worth noting before reaching for one: lyra is itself a neural speech codec, so
some of what a denoiser would do is already happening inside it.

**The stall watchdog counts continuous audio buffers.** In a
push-to-talk system `tx_packets` and `rx_packets` stay flat while idle: the valve is
shut until somebody keys up, and nothing is received until somebody else does.
A watchdog driven off them would either restart a healthy quiet node or need a
timeout so long it never fires. Two other flows do not stop while the pipelines
are healthy, and those are what is counted, both at about one buffer per 20 ms:

- **Capture buffers arriving at the valve.** The probe sits on the valve's
  *sink* pad, so buffers are counted before the valve drops them, and the count
  therefore runs with the PTT released.
- **Playback buffers reaching the sink**, fed continuously by the permanent
  silent mixer input the receive pipeline already carries for its own reasons.

Multicast membership is checked separately, by reading `/proc/net/igmp`,
because there is no dataflow to miss: a receiver nobody is talking to looks
exactly like one that has fallen out of the group. A membership that cannot be
read at all counts as unknown, never as a fault. Losing the join stops both
transmission and reception, since batman-adv drops multicast to a group with
no listeners.

The ladder, in order of cost:

| Symptom | Response |
|---------|----------|
| A flow stopped for `voice_watchdog_sec` | Restart that pipeline in place |
| Three such restarts inside 10 minutes | Exit non-zero; `Restart=on-failure` rebuilds the whole stack |
| Talk group change fails to come up | Revert to the group that was working |
| The revert fails too | Exit non-zero, same whole-stack rebuild |
| The GLib main loop stops turning | `WatchdogSec=60` in the unit; systemd aborts and restarts |

The daemon exits to let systemd restart it. `systemctl restart mesh-voice` from inside
mesh-voice blocks on the very unit issuing it, and the `--no-block` form races
the process it is killing; `Restart=on-failure` with `RestartSec=10` already
does it properly. A fresh process is the point: new ALSA handles, new sockets,
a new multicast join, `mesh.conf` read again and, with lyra, freshly loaded
TFLite models. `/etc/mesh.conf` already holds the operator's talk group by the
time a failed change gets this far, so the restarted daemon comes up on the
group they asked for.

Two things it deliberately does **not** do. It never touches a pipeline the
bus-error backoff already owns, so a node provisioned with `voice=y` before its
OpenVLM board was fitted keeps its quiet five-minute retry instead of being
restarted every fifteen seconds. And it never escalates a pipeline that has
never produced a buffer at all: that node was never working, so there is
nothing to recover.

`WatchdogSec=60` in the unit means the unit and `node_tools` must be updated
together. A node given the unit with a `mesh-voice.py` that predates the
keep-alive would be killed every 60 s for ever. Both ship in the same tarballs,
so the only way to reach that state is to install one of them by hand.

The VOICE tab shows all of this on an **Audio path** row, and
`/run/mesh-voice.json` carries `capture_idle`, `playback_idle` (both `null`
when the flow has never run, which is a missing audio device rather than a
stall), `igmp_joined`, `stalls` and `watchdog_sec`.

---

## Elections, channels and healing

### BATMAN peer counts

`mesh-peer-count.py` queries `batctl meshif bat0 originators_json` with a
five-second timeout. It validates the complete list, normalizes MAC case, and
counts distinct `orig_address` values across all interfaces. Only a successful
empty list means zero peers; command errors, timeouts and malformed rows return
nonzero without a count. The ACS bootstrap, quorum checker and tourguide
partition sizing share this reader.

The old `awk 'NR>1 {print $1}' | sort -u | wc -l` counted the table's second
header and treated every selected route as the same `*` entry. It could let a
solo lobby node bootstrap, hide isolation, and distort partition comparisons.
Multiple routes to one originator could also inflate the count.

On a query failure, lobby bootstrap clears its start window and waits for a
fresh scan/publish round after recovery. `quorum-checker.sh` returns 2 for an
unavailable check; its caller returns to the lobby only for exit 1, so a failed
query cannot force a channel change. The quorum thresholds are unchanged.
Tourguide sizing adds self only to a valid peer count and aborts a hop if sizing
fails before beacon encoding. The pre-hop size is retained for partition
comparison rather than querying the temporary lobby topology. `--list` on the
same peer helper supplies originator MACs for tourguide election.

### Service elections

MediaMTX and Mumble share an election algorithm: the best-connected node wins,
measured by `MEAN_THROUGHPUT_MBPS` in the registry, the mean of BATMAN_V's
metric across that node's originators, in Mbit/s. Eligibility requires a valid
MAC, finite nonnegative metric, a state other than SHUTTING_DOWN/STALE, and a
valid local boot-clock observation no more than 300 seconds old. Future
observation times are rejected. Of eligible advertised incumbents, the highest
metric wins incumbent status, with lowest MAC breaking ties. That incumbent
receives a 10 Mbit/s bonus; highest final score wins, again with lowest MAC on
ties. Registry read failure is distinct from an empty eligible set. Time
serving uses its own [source qualification](#time-synchronization).

This field used to be called `TQ_AVERAGE`, which was wrong: BATMAN_V's metric is
throughput, not a 0-255 link quality. The behavior never changed (highest
wins either way), but the name misled, so it now says what it holds.

Managers wait for `/run/my_ipv4_chunk` before running service elections and
limit them to one pass per fifteen seconds of boot uptime, including discovery
loops. MediaMTX and Mumble serialize their own local runs. These locks cannot
prevent one winner in each disconnected partition; the existing deterministic
election releases a losing node's service and VIP after the partitions merge
and the successor confirms that it is serving.

Before loading transition inputs, both shell elections ask the resident
runtime to check their fixed point. A settled owner is the current winner,
with an active unit, all required wildcard listeners and both local VIPs.
Its completed `leader` state and absence of pending publications confirm
there is no remaining announcement, claim publication or final DB export.
A settled non-owner has neither VIP, an inactive unit and no wait or pending
publication. Waiting
incumbents always take the bounded handover path. A shared, nonblocking
observation lock prevents skipping an in-progress local transition. Mumble
also stats its working DB and `-wal`, comparing mtime, size, ctime and inode
with the last successful export stamp. A missing stamp or changed file sends
the pass to the shell. This check performs no SQLite queries or process starts.
Integrity checks run on transitions and changed-data exports, never on an
unchanged settled pass.

The resident worker reads fresh `/proc/net/tcp`, `tcp6`, `udp` and `udp6`
tables instead of starting `ss`. It caches each normalized IPv6 VIP by service,
radvd config bytes and derivation-helper metadata. Transition requests reuse
that cache too. Serving telemetry uses the same live readiness observations,
avoiding socket-tool starts on every publication, including the roughly
one-second startup publications and normal three-minute publications. The
shell retains its one-shot fallback when the worker is unavailable. Concurrent
service observations can wait up to one second for the FIFO client lock so
simultaneously launched elections do not routinely fall back to Python.
Other runtime callers keep their nonblocking behavior. No new timer or daemon
is involved.

The shell elections share `manet-service-common.sh`. A winner prepares its
configuration and starts the unit before claiming either VIP. MediaMTX uses
wildcard TCP listeners on 8554 and 8889, and Mumble uses wildcard TCP and UDP
listeners on 64738. Unit activity alone is insufficient. The manager applies
the same listener checks when publishing serving flags. Each handover also
immediately republishes the local Alfred telemetry with the changed service
flag, preserving its other fields, so the fallback does not wait for the
ordinary three-minute telemetry period. Failed publications retry at the
normal election interval. Mumble also checks its
working SQLite database before claiming. Its existing checked copies, backups,
local fallback and final publication after stopping remain in place. A
preparation/start failure or exhausted readiness check stops the unit,
resets its failed state and clears the preparation marker. This cancels
systemd's automatic restart loop between election passes. The next election
retries preparation, while repeated failures retain a single transition log.
This cleanup does not publish a failed candidate's Mumble DB. A ready service
whose VIP installation fails is still reused without another DB copy/start.

The serving Mumble winner exports changed working data at election cadence
using a WAL checkpoint, SQLite backup, integrity check and atomic publication.
It rechecks its local election winner immediately before publishing the
canonical shared database. Live exports never fall back to copying changing
DB/WAL files separately. The successful-export stamp records the source stat
signature before the backup, so a concurrent write remains dirty for the next
pass. A failed publication does not advance the stamp. Unchanged data needs
no export, including when the shell fallback is used.
Cadence diagnostics are buffered for the duration of the export. Success logs
only `Exported Mumble DB (<size> bytes)`. Failure replays the full diagnostics.
Transition-time logging remains detailed. Numeric WAL checkpoint result rows
are discarded on every path, while SQLite error diagnostics are retained.

After stopping, the retiring owner preserves its final checked DB under
`Sync/mumble/retired/<owner-MAC-without-colons>/<random-UUID>.sqlite`.
It never writes that final snapshot over the winner's canonical shared DB.
The owner namespace and unique filename make final exports independent of
the successor's cadence exports. A release-pending marker survives export
failure and requires a retry even after the unit and VIPs are gone. Final
snapshots are separate from the ten ordinary backups. After each successful
final export, its owner's directory is pruned to three snapshots, including
the one just written. Other snapshots older than 30 days are removed even
when fewer than three remain. Age and ordering use file mtimes and the local
wall clock, but the new snapshot is always protected, including after a clock
step. Pruning only unlinks direct regular UUID-named `.sqlite` files in that
owner directory. It leaves symlinks, conflict copies, incomplete uploads and
other owners alone. Syncthing propagates the deletions. Cleanup errors are
logged without retrying an already successful final copy, and pruning is
attempted again at the next final export. There is no separate cleanup timer.

Automatic late re-import is not implemented. The canonical shared file and
Syncthing conflict copies carry no ordered ownership generation identifying
the previous owner's state for this exact handover. Filesystem time alone
cannot establish which snapshot supersedes the import. Retired UUIDs identify
distinct files but do not establish that ordering either. A late replacement
could restore an older branch, even if the new working DB has not changed.
The remaining active-state loss window is changes newer than the last cadence
export delivered before import, including the election/export delay and
Syncthing latency. Final snapshots retain those changes for manual recovery.
Conflict copies are not selected or merged automatically. Fresh-install,
delayed-delivery and independent-partition conflicts can still occur because
the local winner check is not distributed fencing.

IPv6 VIP installation uses `nodad` because the incumbent deliberately still
owns the address during transfer. The internal `manet_service_handover.py`
helper sends an unsolicited overriding Neighbor Advertisement to `ff02::1`
with hop limit 255, using Python's raw ICMPv6 socket support. It also sends
three `arping -U` announcements one second apart. Both Debian arping variants
are supported. No new package, service unit or operator command is required.

On losing rank, an incumbent retains both VIPs and its service indefinitely
until the successor claims. It sends no unsolicited announcements while
waiting. A short-lived AF_PACKET listener on br0 has a locked classic-BPF
filter for ARP requests/replies whose sender IP equals the IPv4 VIP, or a
direct ICMPv6 Neighbor Advertisement for the IPv6 VIP. Both use one socket.
The NA path works when batman-adv DAT absorbs the gratuitous ARP. Userspace
validates Ethernet/ARP sender agreement. For NA it checks IPv6, hop limit
255, type 136/code 0, source and target equal to the service VIP6, destination
ff02::1 and its Ethernet multicast address, Solicited=0, payload bounds,
checksum and option lengths. A target link-layer option must agree with the
Ethernet source. The filter accepts the direct ICMPv6 form emitted by the
announcer, without extension headers or fragmentation. Both paths require
the Ethernet source to belong to the freshly ranked winner's registry
identities. Other senders are ignored, with one diagnostic per wait
transition. The single release line reports ARP, NA, NS probe or registry as the
signal. The listener blocks on packet input and a private election wake
socket, without periodic polling.
It closes on a claim, an election wake, an error or a twelve-second deadline.
An overlapping election wakes the locked owner to re-rank, so a ranking flip
can retain ownership without a stop/start gap. Timeouts keep serving and the
normal fifteen-second election schedule starts the next bounded wait.

The winner sends its NA before the three-ARP burst, then republishes its
serving flag. If the registry fallback is not already satisfied, each wait
opens its listener and sends one unicast NS per winner MAC alias. Its source
is a usable br0 link-local address read from `/proc/net/if_inet6`. AF_PACKET
sends directly to the remote MAC, bypassing the local route to the VIP that
the incumbent still owns. The local MAC is excluded. The NS targets VIP6,
uses hop limit 255 and includes the incumbent's source link-layer option.
Sends are nonblocking and there is no timed retry within the wait.

A solicited NA must have Solicited=1, destination equal to the probe's
link-local IP and local Ethernet MAC, and an Ethernet source among the freshly
ranked winner's aliases. The existing target, source, hop-limit, checksum and
option checks still apply. It reports `via NS probe`. This handles a winner
that claimed before the listener opened, without ARP probes or DAT changes.
If link-local/probe setup fails, passive claims and registry fallback remain
available, with one probe-error diagnostic per wait transition. Packet loss
can still require the next wait or fresh post-claim registry observation.

The registry fallback requires a fresh serving observation made after the
local node's latest claim, preventing an earlier host's flag from being reused
during flapping. This still accepts a successor that claims between two local
election cycles, before the incumbent enters its wait. A missing winner also
retains existing ownership. Transition state
under the election lock suppresses repeated journal messages. Packet loss can
delay withdrawal until the next election with a new serving observation.
Brief overlap is part of this handover, and independent partitions still have
independent owners. This is not a distributed lease or a guarantee of lossless
Mumble writes during Syncthing lag.

Manual Wi-Fi changes use the band (`2.4` or `5`) as their Alfred identity,
then resolve each receiver's `mesh_24_if` or `mesh_5_if`. Static plans live in
`/etc/manet/static-channels.json`; an absent band is saved without restarting
a radio. The API, receiver and apply path reject manual changes while ACS is
enabled. Plan, live config, lobby config and restart share the channel lock;
an apply waits at most ten seconds for it. Failure restores the previous plan
and configs. Static enforcement reads the plan only while holding that lock,
skips a busy pass, and leaves radios alone if the plan is malformed.

### Channel agreement and recovery

Independent scoring used to diverge when scan reports or incumbents differed.
For channels 2437/2462, A reporting busy 10/80 and B reporting 80/10 gives a
45/45 median tie. With both reports the incumbent wins; B seeing only itself
moves to 2462. Equal reports arriving later preserve that split through bias.

`channel-election.sh` now requests readiness for the current 180-second round.
Only `--score` executes its scoring logic, without radio writes. The
`mesh-channel-agreement.service` starts through the node-manager drop-in and
restarts with it. Static nodes participate passively as compatibility voters.
The generated/static manager publication path is unchanged.

`manet_acs_agreement.py` contains the protocol; `mesh-channel-agreement.py`
handles discovery, authentication, persistence and application:

- The :25 manager request follows scan/publication. Proposal creation is allowed
  at :45–:60 to allow readiness to replicate. The lowest ready ACS identity
  coordinates; it freezes one result, participant list and activation time.
- Discovery maps BATMAN originators to canonical Alfred identities using
  registry identities and authenticated status aliases. Missing identity or
  query failure defers discovery. Missing status keeps that member in the
  denominator. The protocol supports up to 64 participants.
- The view is the entire component reachable over **any** BATMAN radio. HaLow
  linking two Wi-Fi neighborhoods makes them one decision domain. The coordinator
  scores their shared registry observations, filtered to reachable identities;
  no per-Wi-Fi-island channel decision or separate discovery registry is created.
- A node ACKs only a compatible plan whose membership matches its view. Votes
  bind the full plan hash and boot session. Durable state in
  `/var/lib/manet-acs/agreement.json` precedes publishing or applying; a process
  restart cannot cast a different vote in the same round.
- The ACK deadline is exactly proposal time + 60 seconds. The coordinator
  attests a strict majority of the frozen membership after that deadline.
  Two nodes need both; three need two. No majority means expiry. Topology churn
  cannot extend the deadline or remove a nonresponder mid-attempt.
  Expansion of reachable membership cancels an uncommitted island proposal;
  commitments already issued remain binding, then reconcile in a shared round.
- Activation is deadline + 30 seconds. A receiver accepts a commit only before
  activation minus five seconds and applies within a five-second grace. Slow
  discovery refreshes the clock before deciding, so a timed-out query cannot
  authorize a late switch. A fresh round rediscovers departed peers.
- Candidates come from advertised radio capabilities; peers validate their
  own shared bands rather than re-score local reports. Missing bands remain
  unchanged. Static nodes refuse changes to their configured bands. HaLow-only
  nodes can ACK without becoming the Wi-Fi coordinator.

Types 74 (`acs_state`) and 75 (`acs_helper`) reuse `manet_admin.py` encryption
and the shared admin password; payload identities are bound to Alfred record
keys. Records older than 45 seconds or over five seconds in the future are
ignored. A commit is the authenticated coordinator's attestation, not a set
of independent cryptographic signatures. Members sharing the admin password
are trusted. This does not provide global consensus across different partition
views or atomic final-message delivery; clock alignment remains required.

The daemon polls separately from the manager. An expiring `/run/manet-acs-busy`
marker suppresses ACS mutations during preparation while ordinary status/IP
work continues. Activation and tourguide hops use the shared channel lock.
After switching, a 30-second settling interval prevents premature quorum loss.
Application reconfigures all changed radios, checks actual frequencies, and
uses a bounded restart fallback with a second frequency check. Failed landing
is reported, with bounded repair retries; systemd active state alone is not
radio success.

The same daemon also repairs sustained local frequency drift outside a recent
plan's bounded repair window. It already owns the channel lock and observes
agreement, discovery and actual MHz on each pass. For an enabled conventional
mesh radio, 60 seconds of idle mismatch against the same configured frequency
permits one `wpa_cli reconfigure`, with a two-second command timeout. It never
rewrites the target config or selects a channel. This works for both ACS and
static nodes, including before clock synchronization, because it only restores
the local configured setting.

Each attempt logs the interface, actual and configured MHz, and persists a
five-minute per-interface backoff in `agreement.json` before issuing the
command. Failed, rejected and timed-out commands use the same backoff; there is
no restart fallback or polling loop in this repair path. Normal status reads
check landing. A 30-second busy interval gives links time to reform. Backoff
uses the boot clock and survives same-boot daemon restarts; a new boot discards
it. The mismatch grace starts fresh after daemon restart, a matching read,
target/role change, failed observation or blocked channel lock. Discovery,
prepared/committed agreement, the busy marker and ongoing plan repair/settling
also clear that grace. The longer recollection hold does not prevent restoring
the configured data channel.

After a moving plan, participants suppress further election votes for 1560
seconds (26 minutes), allowing two twelve-minute rendezvous cycles and an
exchange window even when a band has only one usable entry.
An unchanged plan can update limp mode without starting that hold. A node
still reachable on another band can follow a freshly advertised committed
destination from a reachable peer already operating there, even days after
the original switch. Otherwise lobby recollection handles the straggler. No radio that
is physically out of range is guaranteed to rejoin until contact returns.

### Recovery and reconciliation across all radios

Type 74 now carries the currently operating plan for as long as it remains
applicable, rather than stopping at the end of the election hold.
The 45-second envelope age limit remains: an old plan is usable only through a
fresh authenticated statement from a currently reachable node whose actual,
stable radio settings match it. Old cached Alfred records, future activation,
incomplete majority certificates, changed radio settings and failed discovery
cannot authorize recovery. Only enabled conventional Wi-Fi radios are touched;
there is no S1G hop, new transport or new periodic announcement stream.

To limit airtime, holders piggyback the plan hash on their existing type-74
status. The lowest reachable holder of each plan includes its full certificate;
stale or unreachable publishers no longer suppress another holder. The old
certificate is omitted when publishing the next round's commit so two full
64-member certificates cannot exceed the authenticated envelope limit. It
returns after activation. A newly recovered node can retain the certificate
without hopping again when its radios already match. After a reboot only a
validated, actually operating destination survives; old rounds/votes/holds do not.

A newer committed plan that includes the entire currently reachable membership
supersedes a straggler's older plan. If incompatible operating plans came from
independent islands, neither a newer timestamp nor a certificate hash chooses
the winner: clear the recollection hold and use the next normal shared
scan/publication/majority round. Conflicting plans in the same latest round also
require reconciliation. Per-band capability checks and the frozen majority
deadline remain. Static participants can ACK an unchanged plan without running
the ACS radio-application path.

The distinction is between **different radio settings** and **lost connectivity**.
No Wi-Fi peers while on the agreed frequencies is not an invitation to hop if
HaLow still links the node. Different Wi-Fi RF observations across a HaLow-linked
area remain inputs to one channel decision. Only loss of all paths separates
decision domains; any re-established path brings their registry data and ACS
state together again. Asymmetric or incomplete discovery can delay convergence;
this remains bounded agreement, not an atomic global consensus guarantee.

Cold nodes use the challenge exchange below over any surviving link. A holder
of the live plan answers without leaving its data channel, with one lowest-MAC
source per request/common band and duplicate suppression. Under inconsistent
discovery more than one source can temporarily reply; nonce validation still
applies. A matching channel plan needs no radio change. A clockless requester
in discovery still receives confirmation to finish searching; an established
data node with matching channels does not provoke a reply.
Failed radio applications retry at most once per 30 seconds. Working HaLow now
suppresses local Wi-Fi tourguide duty. Nodes without it retain scheduled visits
for newcomers and genuinely disconnected groups.

### Rotating rendezvous fallback

`manet_rendezvous.py` supplies one absolute schedule to the agreement daemon
and tourguide. Schedule v1 uses 2412/2437/2462 MHz on 2.4 GHz and
5180/5220/5745 MHz on 5 GHz. The existing 2412/5180 anchors are the first entries.
Globally aligned 120-second windows alternate bands; `floor(epoch / 240) modulo 3`
selects each band's entry. Each frequency recurs every twelve minutes. A node
skips unsupported, disabled, non-initiating or DFS entries using its actual PHY
report; it never substitutes frequencies or prunes/reindexes its schedule.
All nodes need the same provisioned schedule. Existing mesh width/mode settings
are retained; compatibility and geographic guide coverage remain bench checks.

A disconnected synchronized searcher tunes each enabled band to its entry at
four-minute boundaries. A clockless searcher stays at the fixed anchors until
authenticated recovery or clock qualification; it does not perform an unsynchronized
scan rotation. On cold daemon startup a disconnected searcher left at a rotating
frequency returns to its anchors. Any surviving BATMAN path, including HaLow,
holds discovery still for recovery or joint bootstrap. A failed peer query does
not authorize movement. A solo searcher cannot self-elect a data plan.

Healthy data nodes with working HaLow keep their Wi-Fi channels throughout.
Other nodes can perform elected guide duty: visit one band/frequency per window,
enter between seconds 30 and 49,
and dwell until second 75. The entry deadline is rechecked after slow preparation;
existing restoration traps and the monotonic duration limit remain. The other
band and HaLow stay in place. A guide whose data frequency equals that slot
advertises and listens there without outbound or return reconfiguration.
Nodes without working HaLow still serve unknown newcomers even when all known
nodes are reachable. No extra periodic announcement stream or S1G hopping was added.

`manet_rendezvous.halow_ready()` identifies S1G radios from `/var/lib/halow_if`,
which provisioning populates for either USB or SPI devices. It requires an
enabled interface with the administrative UP flag, an active entry in `batctl if`,
an active `wpa_supplicant-s1g-<iface>.service`, `iw` mesh-point mode and a successful
`mesh_plink_timeout` query (the same joined-mesh query used by the watchdog).
The readiness probe and watchdog accept both a bare integer and an integer
followed by `seconds`, including the `0 seconds` output from iw 6.9. Malformed
or failed queries do not establish readiness or trigger a watchdog parameter
write. A nonzero timeout still proves joining; the watchdog enforces zero to
avoid Morse key-teardown failures on inactive plinks.
Each readiness command has a two-second timeout. The probe does not infer S1G
from an `iw` frequency or a fixed interface name: Morse can expose an ordinary
Wi-Fi frequency there.
No current peer is required. The policy assumes a working HaLow radio reaches at
least as far as Wi-Fi; this is a local readiness check, not an RF range test.

The guide checks readiness before election and immediately before departure, so
HaLow becoming available during preparation cancels that visit. Missing/down,
disabled, inactive, unjoined or unresponsive HaLow permits the existing fallback
without a manager restart. The agreement daemon caches readiness for fifteen
monotonic seconds and includes `halow_ready` in existing encrypted type-74 status.
The guide reads fresh authenticated peer flags through `tourguide-exclusions`;
the election removes those canonical identities from its band-capable candidates.
This prevents a mixed group from repeatedly electing a node that will skip duty.
The live local probe overrides a cached report about this node. Stale or missing
peer flags do not suppress fallback; inconsistent views can defer a visit until
status converges. There is no new registry schema or additional broadcast stream.

This gate applies to elected guide excursions from data channels. Fully
disconnected searchers retain their rotating/fixed-anchor discovery behavior;
any BATMAN connection already holds them still for authenticated recovery or
joint bootstrap. The twelve-minute fallback cycle and 26-minute recovery hold
remain, allowing nodes without usable HaLow to recover too.

`/run/manet-rendezvous.json` stores `search` or `data`. The first usable config
seeds it from the fixed anchors; it survives process restarts and is reset by
radio setup/boot anchor restoration. Ordinary frequency changes never redefine
mode. This lets a rotating rendezvous also be a valid data channel. A fresh
authenticated operating certificate, helper beacon or consumed cold nonce reply
can end discovery on the current channel, without restarting that radio. The
next slot cannot pull the recovered node away. A searcher visiting the frequency
of an old saved certificate cannot advertise it as its operating plan.

Discovery uses the shared channel lock and respects prepared/committed work and
the busy marker. Failed discovery applications retry at most every 30 seconds,
with a boot-bound monotonic marker surviving daemon restarts. The recovery hold
is now 26 minutes: two full cycles plus an exchange window. Reconnected conflicting
plans still lift that hold for one shared decision. Bounded collection requires
at least one common usable frequency, overlapping coverage and successful
delivery. A clockless node cannot escape jammed fixed anchors through rotation;
GPS or a surviving HaLow/time path is needed to enable the rotating fallback.

CM4 bench to-do remains deferred for hardware setup: measure request-to-actual
frequency, first authenticated peer packet, BATMAN forwarding recovery, Alfred
request/reply latency, and PTT/traffic disruption on both the hopped and remaining
MT7916 band. Also test HaLow-only continuity, one jammed rendezvous frequency,
single-band and clockless newcomers, and completely separated groups reconnecting
through HaLow. The rotation offers frequency diversity, not immunity to a jammer
covering/following the full channel set.

**"Every candidate was measured and rejected" and "nothing reported a
measurement at all" are different verdicts.** Both leave the qualified list
empty. The first is a real RF result, and the least bad measured channel is
elected with limp mode asserted. The second is an outage: the band's radio is
absent so its scan report carries no entries, or the scan request was refused
wholesale, or Alfred was down and no reports replicated, and it now **holds the
current channel and does not assert limp mode**, logging `No scan data for any
candidate channel`. Treating it as jamming throttled the whole mesh to legacy
bitrates on the strength of missing data.

### Clock readiness and cold-node recovery

Timed ACS and admin transport wait for `/run/initial_time_synced`, created only
after the time service qualifies GPS or NTP. This includes scan/election
requests, static/HaLow compatibility votes, timed channel-plan application and scheduled
tourguide duty. Until then the agreement daemon neither advances protocol state
nor publishes timestamped envelopes. The common admin transport gates types
70–75 on send and receive, preventing a bad startup clock from writing future
sender/replay history. The configuration UI reports the wait before staging a
mesh change; local-only changes remain available. Public status, identity,
allocation and ordinary forwarding continue without this gate.
Parking a disconnected searcher at fixed anchors is also allowed before sync;
rotation itself waits for clock qualification.

The managers reset publication/action timers on the first observed sync, so a
backward startup step does not suppress discovery. The agreement state records
the local boot identity after synchronization. A different boot clears old ACS
rounds and holds; a validated destination can survive if the actual radios still
operate it. A process restart in the same boot preserves
votes. This is a new voting session, already bound into the protocol. Persistent
admin replay history is never cleared. An existing admin history timestamp far
ahead of correct UTC still needs investigation; this change prevents creating
one at unsynchronized startup rather than bypassing its replay protection.

Simply waiting for time before accepting any helper creates a loop: a cold
node may need recovery onto data channels to reach NTP. It cannot safely compare
a type-75 beacon's timestamp yet. A separate authenticated challenge exchange
provides freshness without UTC:

- A clockless ACS node with BATMAN peers present, on lobby or old data channels, publishes an
  encrypted type-76 `acs_probe` at most once per minute. A random 128-bit nonce
  is bound to its canonical MAC and boot. Its monotonic 60-second lifetime is
  stored in `/run/manet-acs-probe.json`; neither process restarts nor clock steps
  extend it. Sixty seconds allows Alfred request/reply replication and a normal
  15-second manager wakeup. There is no probe when no peer is present.
- A synchronized current-plan holder checks probes in the agreement daemon and
  answers over any surviving mesh path, without waiting for a physical visit.
  A synchronized tourguide also checks probes through the existing `helper-encode`
  calls around its lobby visit. It replies only to currently reachable radio
  aliases. Type 77 carries its original compatible data channels, partition
  size and up to 64 recipient boot/nonce pairs. Replies are bounded to one batch
  per five seconds, with a bounded ten-minute cache suppressing repeated probes.
  The extra exchange is demand-driven; ordinary type-75 beacons are unchanged.
- `helper-select` accepts only a reply authenticated under the admin password,
  bound to its Alfred publisher, containing this node's current boot and exact
  outstanding nonce. The largest compatible partition wins, with the usual MAC
  tie-break. Deadline checks run again after I/O. The challenge is atomically
  consumed before exposing the destination to the manager. Wrong, expired,
  replayed, foreign-recipient and previous-boot replies cannot authorize a hop.
- The exception is limited to following a consumed nonce-bound reply. It never sets the clock,
  advertises an NTP server, votes, runs a scheduled partition merge or authorizes
  an admin action. The node still needs GPS/NTP after joining. Unsynchronized
  nodes cannot answer probes or act as tourguides.

`seal_challenge`/`open_challenge` accept only types 76–77 and their corresponding
kinds. They reuse AES-GCM with channel binding but separate salt state and a
fixed zero timestamp; they never alter the timed sender counter. Normal
`seal`/`open` cannot use these types, and admin/control types cannot enter the
challenge path. A probe itself grants no authority; its matching, timely reply
is the sole freshness proof for cold recovery. Shared admin-key holders remain
trusted as in the rest of ACS.

If no usable source or synchronized tourguide ever appears, nodes retain their
current/lobby mesh operation and defer timed changes. No unsynchronized election
fallback or fake time server is introduced. Arbitrary external clock steps after
initial synchronization and unbounded source outages remain outside the timing
guarantees. Actual Alfred loss/latency and cold single-band recovery need the
pending CM4 bench validation.

### Channel scoring

The score, lowest wins, is

    occupancy% + max(0, median_noise - (-95 dBm)) * 0.5 + mean_bss_count * 0.5

The incumbent's occupancy is the home monitor's `foreign_pct`. A candidate's
occupancy is its off-channel dwell measurement. Total home `busy_pct` stays
available in telemetry but never becomes the incumbent's congestion score or
veto. Noise is uncalibrated across radio chips, so it contributes a weighted
penalty. BSS count is a mean across reporters, not a sum that grows with mesh
size. In data mode, 2.4 GHz home and candidate entries both carry
`bss_count: 0`, so neither pays that penalty. BSS evidence remains on 5 GHz
and the bootstrap scan paths. The incumbent receives a ten-point score
preference after the raw score is calculated.

#### Home measurement and RF trouble

Every three-minute ACS cycle, `node-manager-acs.sh` reads the in-use channel's
cumulative survey counters and summed station receive and transmit duration.
It compares these with the previous cycle on the same interface and frequency.
The manager retains baselines in memory, so a restart starts a new window.
Trouble state also starts clear on restart.
Interface or channel changes and counter decreases reset the baseline and
consecutive-window streaks. An active trouble flag remains set until three
valid good windows clear it. Missing measurements also break streaks. Windows
under 60 seconds and windows without advancing active time are rejected.

The report includes total `busy_pct`, `foreign_pct`, `noise_floor`, the real
elapsed boot-clock `window_s`, and the sender's per-band `rf_trouble` flag.
When the home frequency is known but a window is invalid, its entry carries
the trouble flag without airtime or noise measurements. The scorer excludes
that entry from RF measurements, even if a 5 GHz scan supplies its BSS count.
For differences between the two snapshots:

    busy_pct = 100 * delta_busy_ms / delta_active_ms
    foreign_pct = 100 * (delta_busy_ms - delta_transmit_ms
                        - delta_peer_rx_us / 1000) / delta_active_ms

Both percentages are clamped to 0 through 100. Station transmit duration is
retained for reset detection. Survey transmit time supplies the subtraction.
Receive duration is summed across all peers on that interface. Overheard
unicast between two other mesh nodes still counts as foreign airtime. This
limitation is accepted, so the result is not a perfect classifier of external
interference.

`HOME_FOREIGN_LIMIT=60` percent comes from a controlled interferer on a
two-node 2.4 GHz mesh. Measured foreign airtime rose by 0.82 of the
interferer's airtime at every step. Throughput lost about 20% by 60% foreign,
about 60% by 75%, and 70 to 75% above 80%. The limit is set at the point where
throughput starts to fall, not from any site's background, so a site whose
background already exceeds the clear edge keeps scanning. The 250 ms
candidate visits make that harmless. Off-channel and on-channel readings of the
same channel agreed within one point, so the mesh's own traffic is not counted
as foreign. `HOME_NOISE_LIMIT_DBM=-80` dBm remains provisional: the noise floor
did not move under Wi-Fi interference. Either limit reached
in two consecutive valid windows sets trouble. Three consecutive windows with
foreign airtime below 50% and noise below -85 dBm clear it. Values between
these edges leave the flag unchanged and break both streaks. Each band has
independent state. Invalid observations do not supply a good window.

These longer windows address measurement variance seen on MT7916 with
mt7915e: consecutive 60-second home windows showed 29 to 31% busy with noise
at -86 or -87 dBm. Scan-bracket measurements of that same channel ranged from
35 to 62%. The roughly 180-second operating window measures the channel while
it carries traffic instead of using the short scan bracket as its evidence.

#### Connected trouble gate and candidate visits

A data-state node scans a band only when it has trouble locally or a member
of the connected ACS view reports trouble on that band. The agreement service
includes its validated member identities in its existing local status snapshot.
These are the same canonical registry identities supplied as `ACS_MEMBERS` to
the scorer, including connectivity through HaLow. The manager requires the
same boot and a status snapshot younger than 30 seconds, and peer reports
observed locally less than 240 seconds ago. Missing, stale or failed membership
closes the peer-triggered path. A local flag still opens its own band. There
is no separate membership discovery process for the monitor.

With the gate closed, the manager performs no `iw scan` or `iw offchannel`
on that band. It continues home measurements, including while an agreement
settling interval pauses candidate work. Each cycle replaces the report, so
closing the gate also drops earlier candidate measurements. Lobby bootstrap,
tourguide visits and discovery retain their existing behavior.

On 2.4 GHz, an open gate permits one
`iw dev <iface> offchannel <MHz> 250` remain-on-channel visit per non-home
candidate, with no flush scan in data mode. Occupancy and noise capture a
co-channel network's actual airtime. Counting its BSS only on the candidates
would add a penalty the unscanned incumbent never pays. Both sides report
zero BSS counts. The removed scan also timed out after five seconds without
a results event on about 10 to 13% of attempts on both bench MT7916 cards.
Previously that failure skipped every ROC visit for the cycle.

Separate survey snapshots bracket each visit. The offchannel command returned
after about 6 ms on the bench, before the visit ended. The manager waits
`SCAN_DWELL_MS + 150` ms, currently 400 ms, after the command returns and before
the post-survey. This is a fixed wait, not an observed completion event.
Candidate occupancy is `100 * (delta_busy - delta_transmit) / delta_active`.
Only a successful request with at least `SCAN_DWELL_MS=250` ms of measured
active time, valid occupancy counters and measured noise is published.
A shorter post-survey sample is dropped even after the wait. Each valid
long-dwell sample is published directly, without the short-scan median warmup.

The measured software scan spent about 75 ms on the other channel, stalling
traffic for 330 to 620 ms. The driver lacks `SET_SCAN_DWELL`, so `iw scan`
cannot lengthen that measurement. Remain-on-channel requests of 100, 250 and
400 ms produced 103, 273 and 300 ms of active time, with stalls of 323, 486
and 783 ms. The 400 ms request was capped. No packet loss was observed in
those measurements. The 250 ms request provides a longer sample within that
cap. Those hardware measurements motivate the implementation, but deployment
still requires CM4 validation.

mt7915e clears non-home survey counters on each channel visit. For that known
driver, a successful acquisition that was off-channel in both snapshots uses
the post-visit counters directly. Equal counters across repeated visits are
therefore valid. Other drivers use counter differences and reject decreases.
The ROC path also rejects a frequency that becomes home during collection.

5 GHz remains on the main radio's flush-scan method with the same trouble
gate. The scan includes the home frequency. The same completed scan supplies
`scan_bss_count` for both home and candidates, while the incumbent's airtime
and noise still come from its long home window. With equal 20% occupancy and
-95 dBm noise, six BSS add three points on either side, giving raw scores of
23 before the incumbent preference. On 2.4 GHz in data mode those raw scores
remain 20 regardless of the number of co-channel BSS.

5 GHz candidate measurements require at least 25 ms of dwell and retain the
median of three valid samples within ten minutes. Lobby bootstrap and discovery
retain that short-scan path and its BSS counts on both bands. The first two
short samples leave occupancy absent. A failed, timed-out or aborted flush
scan discards its candidates and leaves the home BSS count at zero. A closed
gate also leaves the home BSS count at zero and publishes no candidates.
Neither condition discards a valid home window. Long-dwell visit failures omit
that candidate. Producer expiry removes observations older than 180 seconds at
publication.

#### Scoring, hold-down and missing data

The scorer takes median noise and occupancy across reporters for each channel.
Home entries for another operating frequency are not off-channel candidate
evidence. Missing occupancy is omitted from the median. An unknown occupancy
score uses the incumbent's measured foreign median, or the highest measured
candidate median if the incumbent is unmeasured. With neither, only noise and
BSS contribute. This comparison fallback cannot cast a congestion veto.

A node calls a channel bad above 85% occupancy or above -70 dBm noise. The
incumbent uses foreign occupancy for that comparison. Disqualification needs
`ceil(0.34 * reporters)` votes, with a minimum of one. This is one of one or
two reporters, two of three through five, and three of six. A lone bad radio
cannot disqualify a channel once three report.

Without any measured alternative, the scorer holds the current channel,
sets `URGENT` false and does not assert limp mode. This includes a bad home
channel waiting for its first candidate data. Once candidates arrive, an
incumbent that is disqualified, unmeasured, outside the candidate list, absent,
or above the raw limp score threshold of 60 is urgent. Urgent results bypass
the preference hold-down. If every measured channel is disqualified, the
scorer chooses the least bad measured channel and asserts limp mode. A
qualified winner with a raw score above 60 also asserts limp mode. The
incumbent bias cannot conceal that raw threshold. Lobby frequencies are never
elected as data channels.

The coordinator requires three consecutive three-minute rounds preferring
another channel before proposing a nonurgent move. `Runtime.score()` stores
per-band `challenge` state in `agreement.json`: incumbent, last round and
count. A round is `now // 180`. Different challengers can win successive
rounds against the same incumbent. Repeated scoring in a round counts once.
A skipped round or changed incumbent restarts the count. An incumbent win
clears it. Before the third win, the coordinator substitutes the incumbent
and logs the held preference once per band and round.

The streak survives service restarts within one boot. A new coordinator or
boot starts fresh. Observing another coordinator during the proposal window
clears local streaks. Peers ACK the frozen plan without re-scoring.
`wpa_cli reconfigure` applies changes, with bounded frequency verification
and a supplicant restart fallback. Unchanged channels do not trigger radio
reconfiguration. The agreement and recovery sections describe activation.

`ChannelScanResult` uses optional `busy_pct`, `foreign_pct`, `window_s` and
`rf_trouble` fields. Explicit presence distinguishes zero or false from an
unmeasured or unavailable value. Home-only fields pass through the encoder
and decoder into `CHANNEL_REPORT_JSON`. Local acquisition timestamps and
short-scan history remain in the manager and are not transmitted. Each
registry payload is parsed separately, so malformed JSON discards only that
report. The scorer remains stateless given the reports, candidate list and
shared incumbent.

### Tourguide election and partition comparison

`mesh-tourguide-election.py` maps reachable originator MACs to Alfred identities
using the registry's `MAC_ADDRESSES`, then elects across the reachable partition.
Direct-neighbor interface addresses were previously compared to the local
bridge MAC, often producing a winner that no node recognized as itself; the
text-table wlan pattern could also discard all neighbors. JSON discovery
includes HaLow and multi-hop reachability. Missing or ambiguous identities
defer the election. Candidates must advertise an UP mesh interface on the
scheduled band; service hosts are avoided when another candidate is eligible.
Oldest helper timestamp wins, with a MAC tie-break and the same ranking when
every eligible node hosts a service. A confirmed solo node elects itself.

The helper beacon and foreign-partition comparison use the channels and size
captured before hopping. Reading configs after the hop compares a data channel
against the temporarily written lobby frequency; recounting peers then can
include visitors or lose the original partition. Missing bands encode as absent
protobuf fields for public diagnostics. Actual adoption uses encrypted type 75
and checks the local PHY's supported shared bands. Helpers are repeated every
five seconds until second 75 of the common 120-second window, with a monotonic
dwell cap. A late start outside seconds 30–49 is skipped. A trap restores the
data channel on exit/signals; both outward and return hops check the radio.

Receivers reject unsigned or stale (>45-second) helpers. The largest compatible
partition wins consideration, with a MAC tie-break. The lobby no longer takes
the first cached type-69 record. Partition comparisons exclude canonical peers
captured before hopping instead of trusting cached ACTIVE registry entries.

Tourguide holds `channel-election.lock` for its whole run. Agreement activation
uses the same lock; the orchestrator waits while it is held and launches
tourguide only after its other data-state work. This prevents local ACS work
from interpreting a single-radio hop as a genuine channel or topology change.
The daemon's defensive drift repair uses that lock too. A blocked pass clears
its mismatch grace, so neither a lobby visit nor its temporary config can
trigger a repair. Discovery mode separately suppresses repairs throughout
search rotations, including when live MHz lags the newly written slot config.

**Which radio hops is derived from the clock, never from this node's own
history**: `(epoch / 120) % 2`, the same window index `should_perform_tourguide`
uses, so it inherits the wall-clock alignment the rest of the ACS pipeline
already needs and adds no new time-sync requirement. Two split partitions can
only find each other if their tourguides hop to the same band in the same
window. Reading this node's last-used radio out of the registry went permanently
out of phase the moment either side missed a window (an elected tourguide
excluded for hosting a service, a restart, a failed hop), after which the two
partitions alternated to opposite bands forever and partition healing was
silently dead. For a 2-node mesh that is the only recovery path there is:
`quorum-checker.sh` cannot rescue an isolated node below 3 remembered peers.

An absent or disabled scheduled band skips the window rather than substituting
the other band. A single-band node therefore has one usable window every four
minutes, aligned with dual-band nodes using that band.

**The smaller partition migrates; equal sizes break the tie on MAC**, lowest
stays put. Both tourguides run the comparison in the same window and each sees
the other's MAC, so exactly one moves. The tie-break is deliberately not the
config string: deciding a split by channel number biases every equal-size merge
toward the numerically lower pair, and can pull a node straight back onto the
channel it fled. Identity is neutral, and the next election re-optimizes the
channel once both sides are talking again.

---

### Gateways in auto EUD mode

With `acs=y` and `eud=auto`, the AP depends on `/var/run/mesh-gateway.state`.
Reconciliation decides through `auto_eud_ap_wanted` in `manet-common.sh`.
`prepare_ap`, hostapd's `ExecStartPre`, refuses every other start, so an AP
left enabled by a former gateway cannot come up at boot. It checks after
taking the channel lock, so a demotion during its lock wait still counts.

Reconciliation runs with status publication, normally every 180 seconds, so
unplug handling cannot wait for it. When end0 goes away, the off hook returns
the radio to the mesh at once, unless the recorded upstream is another
interface with carrier, such as USB. That uplink keeps the node a gateway, so
the hook then keeps gateway state, NAT and BATMAN gateway mode, cleans up only
end0, and serves the AP. That also covers a wired EUD leaving end0 while USB
stays up. One exception remains: if end0 loses carrier in the middle of
detection, the detector has already recorded end0 as the upstream, so it
returns the radio to the mesh and the next reconciliation restores a USB
gateway's AP. Without ACS, auto mode keeps its older rule, AP on unless
a wired EUD is attached.

While the AP holds the radio, its band role file is empty, so the agreement
daemon's `interfaces()` omits the band and never retunes it. `parked()` finds
that radio from `ap_interface` and `ap_mesh_band`, and `sync_parked` rewrites
only the frequency line of its supplicant config. It runs after `apply()` and,
once the clock is ready, after every tick that holds a destination, because
recovery can adopt one without retuning a radio. Clockless ticks skip the
second sync: a nonce-bound recovery reply is applied directly and is newer
than the saved timed destination. Discovery rotations never write it, because lobby
anchors are not the data channel.

The parked file is a record, not authority. On return, `select_frequency`
uses only the authenticated destination or the anchor and discovery. Registry
telemetry is unsigned, so a saved frequency that peers appear to confirm
still cannot choose the channel.

Gateways never tourguide. Election drops candidates whose registry record has
`IS_GATEWAY='true'`. `tourguide-manager.sh` exits when local gateway state
exists, both before election and immediately before departure, which covers a
registry that has not caught up and a promotion during preparation. A gateway
promoted during the lobby dwell still returns at the window's end. A
component made only of gateways has no tourguide. Agreement over connected
paths, including HaLow, still reconciles channels.

## Network management

`manet-dns-setup.sh` makes `/etc/resolv.conf` point to
`/run/systemd/resolve/resolv.conf`. A managed resolved drop-in clears static
global DNS and supplies `1.1.1.1`/`8.8.8.8` as `FallbackDNS`, used only when no
other servers are known, not when a DHCP server returns an unwanted answer.
dnsmasq reads that same upstream file with `clear-on-reload`, so a resolver
change invalidates stale cached answers without restarting DHCP. No localhost
forwarding chain is involved. This follows qualified local uplink DNS. It
does not distribute a gateway's private DNS servers over the mesh.

Both first-boot templates invoke the helper, and a dnsmasq `ExecStartPre`
invokes it on service startup. Unchanged configuration does not restart
resolved. The IP manager recognizes this resolver configuration as current
and checks service aliases only when their VIPs are available, avoiding a
repeated DHCP restart when no optional service VIP is configured.

`manet-uplink-dns.py` owns qualified DHCPv4 resolver selection. All generated
uplink DHCP files, including provisioning's MAC-matched `10-end*.network`,
set `UseDNS=no`, `UseDomains=no` and `RoutesToDNS=no`. DHCPv6 and RA DNS and
domains are also disabled on these candidates. This prevents an unqualified
lease from suppressing fallback DNS or installing a resolver /32 route that
beats the mesh default. The policy remains fixed through role changes and
renewals. Runtime per-link `resolvectl` settings are not used as ownership
state, because networkd can republish its own settings on reconfiguration.

The helper reads networkd's DHCPv4 lease for the carrier-bearing, unbridged
`upstream_iface` only while `mesh-gateway.state` exists. It publishes up to
three plain IPv4 resolvers and sanitized search domains in
`/run/systemd/resolved.conf.d/80-manet-uplink.conf`. Interface suffixes in
global `DNS=` were silently ignored by Debian 13's systemd 257.13 on the
bench, despite the upstream manual's advertised syntax. Resolver traffic
uses the main routing table: on-link LAN DNS uses its connected route, and
public DNS follows the promoted metric-100 default. No selected resolver
means empty global DNS/domains and the existing public fallback. The parser
uses systemd 257's internal lease format, so a future systemd upgrade must
check that format. Missing or invalid leases withdraw the selected DNS.

`manet-uplink-dns.path` watches gateway state, the selected-interface file,
and networkd's lease directory. It does not watch networkd's link directory:
routine link-state rewrites otherwise launch Python for unchanged DNS.
Carrier loss is covered by gateway demotion and lease removal. Its oneshot
service compares the desired configuration with the last successfully applied
bytes before reloading resolved. Systemd 257 supports configuration reload
without a restart.
Networkd renewal cannot overwrite this global drop-in. Unchanged events do
not reload resolved or emit helper logs. Failed activation remains retryable.
Promotion and demotion also invoke the service before dependent EUD services.

Candidate HTTP checks first try normal resolution. If that fails or the
endpoint does not qualify, the helper queries the candidate's DHCP resolvers
over IPv4 with `SO_BINDTODEVICE`, validating the reply ID, question and answer
chain. Truncated replies retry over TCP on the same device. Each lookup has
a four-second total budget. Curl receives the first answer with `--resolve`
and still binds its HTTP request to the candidate. Only a completed 204
qualifies Ethernet. This supports a lone node on a LAN that blocks public
DNS, without c-ares, a fixed endpoint IP, or globally publishing candidate DNS.

During `radio-setup.sh`, a failed repository DNS check triggers one
`manet-uplink-dispatch.sh provision` attempt per setup run. This uses the same
candidate selection and bound qualification, waits up to 30 seconds for each
dispatcher/detector lock, and has an overall 180-second timeout plus a
five-second kill grace. Setup checks repository DNS again before apt. A skipped
dispatch, failed activation or timeout cannot stand in for working DNS.
Working mesh or fallback DNS avoids the extra qualification pass.

The provisioning event installs the metric-100 route, NAT/filter/MSS rules,
qualified gateway markers and DHCP DNS. It skips bandwidth measurement and
announcement, radvd changes, AP/EUD transitions, mesh IP allocation and route
manager startup. In particular, restarting the route manager would pull in
node-manager before setup has finished, and AP names may still await a rename
reboot. `UPLINK_MODE=provisioning` records that a normal reconcile must finish
those deferred actions, even when the address and router have not changed.
An already fully promoted uplink keeps its `gateway` mode and refreshes DNS
without touching radios. Qualification failure withdraws gateway/DNS state
without starting radio services. These are qualified runtime settings, not a
temporary unqualified resolver. Reboot clears the `/run` state and the next
setup pass can qualify again. The permanent no-DNS networkd policy stays intact.

Both first-boot templates finish their built-in archive, apt, Mumble and
MediaMTX downloads before stopping the image's network manager and enabling
networkd. CM4's outer firstrun writes a separate `provision-mesh.sh`. Its
textually later outer commands run before that service's network takeover.
Neither template runs git clone or pip installs. The additional-script hook
only stages files. Operator scripts execute after radio setup succeeds.

The tools archive carries the helper, path/service units and updated config
writers. The updater requires them, runs the installed `install` command,
and enables the units before restarting the node manager or committing
version markers. Migration appends the managed policy to existing MANET DHCP
files, their `.dhcp-default` backups and `.off` templates, preserving MAC
matches and other settings. A persistent marker records the required
`networkctl reload` before each file replacement, and recovery retries it
after interruption. This one-time reconfiguration can briefly drop a DHCP lease.
Later installs with identical files do not reload networkd. Both provisioning
templates apply the same policy and enable the same units.

The generated bridge configuration uses `bind-dynamic`: br0 and its IPv4
addresses can appear after dnsmasq starts. Static `bind-interfaces` could leave
DNS listening only on loopback and IPv6 for the rest of the boot. An existing
static binding is replaced once; unchanged dynamic configuration is retained.

`manet_node_ipv4.py` selects the persisted allocation's primary address only
while it is present on br0 and belongs to the configured subnet. The allocator,
both node managers and dashboard share this selection. Address-list order is
not identity: MediaMTX's service VIP or the EUD gateway can appear first.

The hostapd drop-in runs `prepare-ap-iface.sh` before each real start/restart.
The older independently enabled `ap-interface-setup.service` is retired:
it could prepare an AP at boot even while that radio belonged to the mesh.
Hostapd's post-start action applies the AP PHY power cap under the channel
lock after checking active roles and actual AP type. The packaged
`ap-txpower.service` offers the same guarded action for reconciliation and
has no boot enable link. A cap already at or below 5 dBm is left alone; a
later power reset above that ceiling is corrected. Hostapd also queues an
asynchronous dnsmasq start after its AP setup, including boot and recovery;
dnsmasq's own port and isolation guards still apply. The fixed 23/24 dBm lab-power unit, the generated
HaLow fixed-ceiling units and the auto-power HaLow unit are retired.
`manet-mesh-power.service` asks every radio named in `mesh_if` or `halow_if`
for 30 dBm (`iw phy <phy> set txpower fixed 3000`) after bat0 enslavement,
skips any interface currently in AP mode, and logs the reported value. The
AP-to-mesh transition requests the same 30 dBm when it clears the AP cap. The
kernel side removes the matching driver and firmware ceilings (see
[the kernel port record](kernel-6.18-morse-port.md) §4.2 and §7). The AP cap is read back after setting it, accepting a lower
regulatory ceiling; a PHY shared with another active interface is rejected.
`manet_ap_mesh.py` owns both directions. It withdraws active mesh roles under
`channel-election.lock` before preparing AP mode. Returning to mesh restores
the provisioned band, rebuilds live/lobby supplicant files from current mesh
credentials and the current band plan, clears the AP PHY power cap, verifies
PONG and the actual mesh channel, then attaches the radio to bat0. Disabled
mesh radios keep their roles/configuration while remaining down; AP-only
hardware never becomes mesh. Failed transitions restore prior files/roles
and restart the previous AP outside the channel lock. Busy transitions leave
the AP serving and retry at the next uplink reconcile. An unplug during a
tourguide visit can similarly delay AP startup until the channel lock is
released; the next reconcile retries. Hostapd start/stop jobs are bounded by
the unit itself, so a timed-out client cannot leave a stale start queued.

Static mode uses the saved authenticated band plan, including changes received
while that band was reserved for AP use. Fresh registry radio observations
validate the chosen frequency and report confirmed/conflict/unknown; they never
rewrite the plan. A node powered off during a static change still needs that
change re-applied through the authenticated management path. ACS uses a valid
local committed destination supported by the returning PHY and consistent with
remaining active radios, or the band's anchor and authenticated recovery. With
no other active Wi-Fi band it enters discovery. Telemetry cannot authorize an
ACS move. Registry evidence excludes stale, down, AP and recent tourguide
radios; its publication interval means validation can lag a channel change.

BATMAN setup/watch and ACS activation use the same channel lock and reload
active roles after acquiring it. They cannot restart or enslave a radio using
an interface list cached before an AP transition. Ethernet detection, unplug
cleanup and uplink reconciliation also serialize their policy decisions; the
radio helper never calls those policy scripts. Hostapd starts happen outside
the channel lock because its preparation helper takes that lock.

### IPv4 allocation and collision recovery

`mesh-ip-manager.sh` allocates a contiguous block within `ipv4_network` for
each node. There is no central address allocator. The node observes peer
claims, avoids their occupied ranges, and resolves overlaps discovered later.
Discovery and random choice reduce collisions; they cannot prevent every
simultaneous claim or a collision between previously separated partitions.

#### Block layout and capacity

Let `H` be the first usable host address as an integer, `U` the number of
usable host addresses, and `E` the node's effective `max_euds_per_node`.
An active EUD mode enforces at least one EUD address. The block width is
`W = E + 2`; its zero-based chunk index `k` maps to:

```text
capacity = floor((U - 5) / W)
start(k) = H + 5 + k * W       0 <= k < capacity
primary  = start(k)           assigned to br0
gateway  = start(k) + 1       also assigned to br0
DHCP     = [start(k) + 2, start(k) + W - 1]
```

The five reserved addresses are the first five **usable hosts**, not the
network address. A chunk is an allocation within the shared subnet, not its
own routed subnet; both bridge addresses use `ipv4_network`'s prefix.
The division discards a trailing fragment too small for a whole local block.
With no EUD capacity, only the two bridge addresses remain and no client
pool is available.
The width is fixed for each node at provisioning; `mesh_config.py` rejects
changes through management. There is no supported manual chunk override:
bypassing claim observation and conflict handling would create an allocation
that the distributed mechanism cannot repair.

For `10.30.0.0/24` and one EUD, `W=3` and capacity is
`floor((254 - 5) / 3) = 83`. Chunk zero owns `.6` through `.8`:
`.6` is the mesh primary, `.7` the client gateway, and `.8` the DHCP pool.
Chunk one owns `.9` through `.11`. Service VIPs use the reserved space;
MediaMTX uses `.2` and Mumble `.3` in this example.

**Which chunks are taken comes from Alfred, one step removed.** Every node
publishes its chunk, primary address and provisioned width in its identity
record (`ipv4_chunk`, `ipv4_address`, `ipv4_chunk_size`, Alfred type 67),
and `mesh-registry-builder.sh` decodes those into
`/run/manet-registry/claimed-chunks.txt` as
`<chunk>,<mac>,<first-address-integer>,<size>` lines. Allocation and conflict
detection compare absolute ranges, since chunk numbers depend on local width.
This script reads that file and never queries Alfred itself. A successful claim
writes `/var/run/my_ipv4_chunk` and `/var/run/my_ipv4_chunk_size`, which the
node manager hands back to the encoder. The IP manager calls
`mesh-ip-startup.py` before any allocation or restoration. That helper refreshes
the registry on every pass, including after startup; ACS no longer waits up to
180 seconds to rebuild the allocation snapshot. Failed Alfred reads leave the
previous registry and claims intact and defer IP management.

Chunk numbers start at **zero**, after the five reserved service addresses:
in `10.30.0.0/24`, chunk zero starts at `10.30.0.6`. The registry includes it
in the claimed-chunk index. Because protobuf also returns zero for an unset
chunk, a claim requires an advertised IPv4 address as well as a chunk number.
The node managers omit that address until `/var/run/my_ipv4_chunk` exists;
on an already allocated node, the static manager runs IP management before
reading the chunk, so a changed allocation is advertised in the same pass.

**Boot discovery is deliberately nonblocking.** The node manager must keep
publishing types 67 and 68 while IPv4 is unassigned, or a whole mesh booting
together would wait forever for records nobody has published. Both manager
variants publish each loop until allocated; allocation changes bypass the
270-second identity keepalive, and failed identity sends are retried.
Cold nodes publish both records before the first discovery check, allowing the
observation window to start on that pass. After the first allocation, both
managers immediately start another pass to advertise the new claim, without
the usual 15-second sleep. A successful Syncthing ID lookup is cached for the
manager process; a missing ID is retried and each lookup has a three-second
timeout.

The startup helper requires a usable (non-tentative, non-failed) link-local
IPv6 address on `br0`, an active Alfred primary on `br0`, a successful BATMAN
originator query, and our own joined identity/telemetry in the registry. The
generated Alfred unit now waits on `br0`, the interface Alfred actually uses.
It explicitly sets Alfred's existing default synchronization period to 10 s.

With those prerequisites met, start **one fixed observation window** using
monotonic time. Allow allocation after **10 seconds** if every visible peer has
joined identity/telemetry (including the solo case). Missing peer records can
extend discovery only until **20 seconds from the original start**, when the
node logs the missing peers and proceeds with the claims received so far.
Peer arrivals, departures, and repeated membership changes never reset the
start time or extend the deadline. Records arriving between 10 and 20 seconds
can complete discovery immediately on the next successful check.

These limits allow one Alfred synchronization period normally and a second
period for incomplete peer data. All nodes are primary on the same BATMAN L2
domain, so the delay is not multiplied by RF hop count. `alfred -r` reads the
primary's local cache; an empty reply does not establish that no peers exist.
The helper uses
`batctl meshif bat0 originators_json` and matches originator MACs against the
identity's interface MAC list. Peers **do not need IPv4** to satisfy that check.

Proceeding at the deadline deliberately accepts an incomplete view. Delayed
radios, lost data, simultaneous choices, and partition merges can cause
collisions; prompt claim publication and MAC conflict resolution remain
necessary. Failed local commands or missing local readiness still defer
allocation even after the deadline, but preserve the original start time.
On recovery, the node uses that elapsed time rather than starting over.

Both managers sleep 1 second between loops while unallocated, then return to
15 seconds. The deadline is acted on at the next successful check; work within
a loop can delay it, so 20 seconds is a limit on the deliberate peer-data wait,
not a guarantee of IPv4 within 20 seconds of boot. Progress is recorded in
`/var/run/mesh-ip-startup.json` only for this boot. No registry is required from
a prior boot.

#### Choosing and defending a claim

Once discovery permits allocation, `load_claims()` reads the refreshed claim
snapshot, excludes our own interface MACs, and converts each peer's primary
and width into an inclusive address range. A missing or implausible width
defers a new allocation; the known primary still participates in conflict
checks for an already configured node. Guessing the peer's width from our
own setting could allocate over its client pool.

`UNCONFIGURED` first tries the saved chunk from `/etc/mesh_ipv4_state` if the
network still matches, the saved primary is in the network, and no peer range
overlaps it. This is a preference, not a reservation. Otherwise
`get_random_chunk()` marks every local candidate touched by a peer range,
collects the remaining indices, and chooses `RANDOM % available_count`.
No remaining candidate is an allocation error; the manager can try again on
a later pass. There is no ARP probe or distributed reservation/ACK round in
this allocation algorithm.

Widths can differ between nodes, so comparing chunk numbers is insufficient.
With the `/24` above, a width-five peer's chunk zero occupies `.6` through
`.10`. It blocks both width-three candidates zero (`.6`–`.8`) and one
(`.9`–`.11`); candidate two (`.12`–`.14`) is available if no other peer
claims it. Overlap is the inclusive test `peer_start <= local_end` and
`peer_end >= local_start`, including client addresses, not just primaries.

The allocator rechecks the selected candidate against the loaded snapshot,
durably saves its allocation intent, adds both bridge addresses, configures
dnsmasq, and
writes `/run/my_ipv4_chunk` and `/run/my_ipv4_chunk_size`. That second check
does not reserve the range globally or obtain a newer peer snapshot. The
manager immediately starts another pass after first allocation to publish
the claim through type 67; it does not wait for the normal identity keepalive.

`CONFIGURED` compares the live primary plus local width against peer ranges
on subsequent passes. On an overlap, the lexicographically higher MAC wins
(fixed-width MAC strings preserve numeric ordering, assuming distinct node
identities). The lower-MAC node
withdraws its management-name advertisement and allocated bridge addresses,
clears its persistent preference and runtime claim markers, and chooses a
free block on a later pass. With several overlaps this repeats as each
conflict is observed. Existing client leases/connections may need renewal;
this is convergence after a collision, not uninterrupted address ownership.

Freshness is based on changes to a peer's telemetry observed with our boot
clock, not on its wall-clock timestamp or repeated reads of Alfred's cache.
The registry marks telemetry older than 300 seconds `STALE` and omits its
claim. A returning node may therefore discover that another node now holds
its previous block. Failed Alfred reads retain the last complete snapshot
and defer reconciliation instead of making every address appear free.
Replicated claim/observation state is under `/run`; reboot rebuilds it through
discovery. See [data management](#data-management) for identity caching and
the 900-second observation tombstones.

#### Retiring a changed IPv4 network

Before reading the current allocation, `mesh-ip-manager.sh` compares the
persisted CIDR with `ipv4_network`. A change retires the old primary from
`/etc/mesh_ipv4_state`, its immediately following EUD gateway, and the old
network's two reserved service VIPs if present on `br0`. Each deletion matches
the exact address and old prefix. The allocator no longer sweeps arbitrary
aliases merely because they fall inside the current network. It never deletes
addresses on `end0` or other uplinks.

Retirement takes both service-election locks without waiting. A running
handover defers cleanup to a later allocation pass. Before stopping DHCP it
validates the saved addresses, checks that a new block is available, verifies
`CAP_NET_ADMIN`, reads live address flags and secondary-promotion policy, and
checks file access by staging snapshots and temporary replacements. The
resident runtime's unit has `ProtectKernelTunables=yes`, so retirement never
writes sysctls. Promotion is effective when either `all` or `br0` has
`promote_secondaries=1`. Otherwise it deletes owned secondary addresses before
the kernel primary. If removing an owned primary would also remove an unowned
old-prefix alias, retirement defers with the old DHCP service intact. An
operator must resolve that alias or arrange promotion outside this sandbox
before that transition can proceed. No boot-time sysctl change is required
for a prefix containing only MANET addresses.

Under the locks, retirement checkpoints files and owned addresses under
`/run/manet-ip-retirement`, stops dnsmasq, refreshes the lease snapshot, deletes
owned addresses, removes the old pool and leases, withdraws `manet.local` and
clears runtime chunk markers. A failed operation restores deleted addresses
and changed files, then restarts previously active dnsmasq with its old pool.
If address restoration or service startup also fails, the checkpoint remains
for retry. A caught termination uses the same rollback. SIGKILL cannot run
rollback immediately, but the next attempt restores an armed checkpoint
before proceeding, even if the requested CIDR has been reverted. The last
old-prefix address removes its kernel connected route. An unowned alias keeps
its connected route.

The old persistent allocation remains until retirement succeeds. State
replacement uses a staged file, filesystem synchronization and atomic rename.
The next allocation is persisted before adding either address, so a crash
during installation resumes that same pair and rebuilds DHCP and names.

Both managers poll every second when their chunk marker is missing, and the
resident runtime deliberately never caches failed reconciliation. Config apply
clears that marker, so an unbounded failure used to repeat every one or two
seconds. Retirement now records a monotonic 15-second retry deadline in
`/run/manet-ip-retirement.retry`, under the existing allocator lock. The shell
returns failure quietly before that deadline, including to one-shot callers.
The old/new CIDRs and saved address identify the attempt. A changed request
bypasses the old delay, and a successful transition clears it. Stable failure
messages are printed once per changed failure, avoiding repeated DHCP stops
and journal flooding during discovery.

The election scripts reread `mesh.conf` on each invocation, but only know that
invocation's calculated VIPs. Their shared locked loop now rejects a changed
CIDR before acting, including after a successor wait. This prevents an old
invocation from recreating a retired VIP. Normal elections claim the new
IPv4 VIPs without changing the independent IPv6 VIPs or wildcard listeners.
The ULA prefix comes from `radvd-mesh.conf`, provisioned as
`fd01:ed20:ecb4:0::/64`, not from `ipv4_network`. The ULA, link-local addresses
and IPv6 service VIPs are therefore unchanged by an IPv4 CIDR change.

`manet_ip_runtime.py` checks discovery, live DHCP isolation, addresses,
dnsmasq and the UI firewall before permitting its content-based no-op path.
Only an unchanged, previously successful, uncontested reconciliation can
skip the shell allocator. Changed claims, incomplete widths, deleted output
files or failed live checks force another pass. A local lock serializes
reconciliation on one node; it cannot serialize claims across the mesh.

The implementation is in [the allocator](../MANET/node_tools/mesh-ip-manager.sh),
[startup discovery](../MANET/node_tools/mesh-ip-startup.py),
[registry construction](../MANET/node_tools/manet_registry_builder.py), and
[runtime reconciliation](../MANET/node_tools/manet_ip_runtime.py).
[Registry/allocation tests](../MANET/node_tools/test_mesh_registry.py) exercise
claim exchange, range comparisons, saved allocations, and publication.
[Startup tests](../MANET/node_tools/test_mesh_ip_startup.py) cover readiness
and the observation deadline. Their simulated commands do not establish a
maximum convergence time under RF loss.

The gateway route manager similarly sleeps one second between pending-route
checks during its first 90 seconds, using `/proc/uptime` so an NTP clock step
cannot extend that period. A ready route, a local uplink, or expiry of that
startup period returns it to ten-second sleeps. Missing registry data, a
missing primary address, failed pings and failed route installation all retry.
`manet_node_ipv4.py` chooses the route's source address from the current
allocation, excluding service VIPs and the EUD alias; a changed source triggers
route replacement. Only a metric-100 default on the saved `upstream_iface`
preserves direct routing before the uplink dispatcher writes its gateway
marker. A DHCP default at metric 1024 does not prevent installing a mesh
default at metric 500. The manager inspects mesh defaults separately from
direct defaults, including when removing a mesh route in local gateway mode.

### Local EUD discovery

`manet-dhcp-isolation.py` keeps each node's DHCP pool and EUD discovery local using the native nftables
bridge table `manet_dhcp` (the established table/service names are retained).
The original four DHCP rules are unchanged: forwarding in both directions,
remote requests to the node's server, and local server replies onto the mesh.
For IPv4 and IPv6, the discovery rules drop UDP source **or** destination ports
137/138 (NetBIOS name/datagram), 1900 (SSDP/UPnP), 3702 (WS-Discovery),
5353 (mDNS), and 5355 (LLMNR). TCP 137/5355 also covers the name-service
fallbacks. They apply to forwarding in both directions, input from `bat0`,
and output to `bat0`. Port matches include multicast, broadcast and unicast
queries/replies, including replies to ephemeral client ports. They do not
match other ports or blanket-drop multicast. Noninitial IP fragments have no
transport ports to match; this is a chatter boundary, not fragment reassembly.
Traffic between `br0` and local `end0`/AP clients, and between those local
clients, remains allowed by this table. Mesh names still come from
`mesh-hosts-update.sh` and the registry.

The service installs rules before dnsmasq; dnsmasq's start guard and the IP
manager verify and repair them. Failure prevents DHCP service. `check` is
read-only; `ensure` preserves healthy rules and counters. Readback accepts
nft's omitted implied protocol dependencies but requires every effective
interface, family, transport, port, drop verdict and hook. The fixture's
original DHCP entries came from CM4 nft 1.1.3; added discovery entries model
that format and are not a new hardware capture. Only ordinary `iifname` and
`oifname` metadata is used; node kernels lack `ibrname`/`obrname` support.
The private table is replaced atomically, leaving NAT and UI policy intact.

After successful live verification, `apply`/`ensure` reconcile Avahi's
`[server] allow-interfaces` to the AP from `/var/lib/no_mesh_if`, plus `br0`
when present. Without a verified boundary, the failure path removes `br0`;
without either interface name the allowlist is `lo`, never empty. The
responder remains IPv4-only with its reflector disabled. `radio-setup.sh`
calls the same helper after copying the base config. The tools builder carries
the scripts, units, drop-ins and support files; the updater reloads systemd
and calls `ensure`, applying the live configuration. The IP manager reconciles
it again when `br0` appears later.

**The management name uses the node's internal EUD address.** The CM4
boundary bench found `manet.local` resolving to `10.30.2.2`, MediaMTX's
elected VIP, when Avahi automatically published addresses on `br0`. That
bridge also holds `.146` (the primary mesh identity) and `.147` (the internal
EUD gateway/DNS address); Mumble's `.3` VIP can appear too. The correct address
is `.147`, next to the `.148`–`.150` DHCP pool: it is intended for this node's
own EUDs and stays on the node through service elections. The UI firewall
accepts only localhost and this node's DHCP pool, so a stale VIP answer after
an election fails. These are example allocations, not constants in the code.

`mesh-ip-manager.sh` writes `address=/manet.local/<br0_secondary>` for dnsmasq
and `<br0_secondary> manet.local` in `/etc/avahi/hosts` from the same chunk
allocation. Both files are replaced atomically. The internal address must be
a valid IPv4 address immediately before the DHCP pool; a missing or invalid
address removes the static entry. Pending discovery and releasing a chunk
also withdraw it. Other static host entries and comments are preserved.
The normal allocation pass repairs a missing entry even if DNS is unchanged.
Avahi reloads only after a hosts change, with a pending marker to retry a
failed reload. An inactive Avahi reads the file when started. There is no
additional publisher process or polling loop.

The isolation helper fixes the host name to `manet` and keeps
`publish-addresses=no`, so the primary mesh address and elected VIPs are not
automatically announced. It owns the guarded interface allowlist; the IP
manager owns the static address. A single static name needs no alias/PTR
workaround. The existing HTTP service targets `manet.local` on port 80;
management and measurements live under `/manage/`. Requests use the same
routes for every Host header, including a cached old hostname. Once an old
DNS answer expires, a retired-hostname bookmark must be changed to
`http://manet.local/manage/`; an HTTP redirect cannot repair a DNS lookup that
never reaches the server. `mtx.local` and `mumble.local` remain unicast DNS
service aliases. Client caches must process Avahi's withdrawals/new answers
or expire when the chunk changes.

The base `nftables.service` flushes the ruleset on restart. The IP manager
repairs isolation on its next allocation pass; this is not continuous
protection against an external flush. If repair fails, DHCP stops and Avahi
is narrowed, then DHCP resumes with existing leases after recovery.
DHCP runs only while a local EUD port on `br0` has carrier and is forwarding:
the active AP or a bridged Ethernet client port. A routed uplink and `bat0`
alone do not qualify. Every service start checks this condition, and the IP
manager stops DHCP if the last EUD port disappears. Every hostapd start queues
a DHCP start after the AP is ready. Other EUD port changes are reconciled on
the next IP-manager pass (normally within about 15 seconds).

#### Discovery boundary: mesh traffic inventory

Audit of the runtime scripts and the services they configure, including ATAK
EUD defaults. All cross-mesh ports below are disjoint from the UDP
`{137,138,1900,3702,5353,5355}` and TCP `{137,5355}` sets. The packet-policy
tests exercise these destinations/ports and both request/reply directions.
Custom ATAK/video/plugin ports are not fixed by this repository; they remain
open unless explicitly configured onto one of the blocked discovery ports.

| Sender / dependency | Multicast group or broadcast destination and transport | Effect of discovery rules |
| --- | --- | --- |
| `mesh-voice.py` | `239.192.41.1`, UDP RTP `38801 + 2*(channel-1)` for channels 1–32; paired RTCP is RTP+1 (reserved by the code's convention) | Entire 38801–38864 range passes, including optional unicast redundancy |
| Alfred (`radio-setup.sh`: `alfred -m -i br0 -f -p 10`) | IPv6 `ff02::1`, UDP 16962, plus unicast synchronization | Passes; identity, telemetry, ACS, recovery, config push, elections and shutdown announcements all use Alfred rather than separate IP broadcast ports |
| Syncthing local discovery | IPv4 subnet broadcast / `255.255.255.255`:21027 and IPv6 `ff12::8384`:21027 UDP | Passes; direct file sync TCP/UDP 22000 also passes |
| MediaMTX optional multicast RTSP transport | Allocated group in `224.1.0.0/16`, UDP RTP/RTCP 8002/8003; encrypted variants 8006/8007 if enabled | Passes; no fixed single group |
| ATAK EUD SA, chat, optional sensor input | UDP `239.2.3.1:6969`, `224.10.10.1:17012`, `239.5.5.55:7171` (sensor input disabled by default upstream) | All pass; PRC-152 input UDP 10011 and CoT TCP/UDP 4242 also pass |
| IPv4 neighbor and multicast control | ARP Ethernet broadcast `ff:ff:ff:ff:ff:ff`; IGMP `224.0.0.1`, `224.0.0.2`, `224.0.0.22` and group-specific reports | Non-TCP/UDP; passes |
| IPv6 neighbor/router and multicast control, including radvd | ICMPv6 `ff02::1`, `ff02::2`, `ff02::16`, solicited-node `ff02::1:ff00:0/104`, group-specific reports | Non-TCP/UDP; passes |
| BATMAN V underlay | EtherType `0x4305`, including Ethernet broadcast on mesh radios | Not filtered; underlay runs below `bat0`, not as an IP discovery service |
| DHCP / dnsmasq | Local IPv4 broadcast UDP 67/68 | Existing DHCP isolation unchanged; local EUD operation retained |
| Avahi | `224.0.0.251:5353` (IPv6 mDNS group is `ff02::fb`, if responder IPv6 is later enabled) | Intentionally local only; wired/AP `manet.local` remains local |

No further node-tools IP broadcast/multicast destinations were found. NTP /
chrony uses unicast UDP 123, Mumble TCP/UDP 64738, the UI TCP 80, and the ATAK
node service uses unicast UDP 4242/4349 for phones and UDP `239.2.3.1:6969`
for unattended-radio SA and admitted phone position rewrites. MediaMTX unicast defaults TCP
8554/8322/1935/1936/8888/8889 and UDP 8000/8001/8004/8005/8189/8890 are also
outside the sets. No election, registry, service configuration or other
firewall policy is changed. Syncthing's optional UPnP router search **does**
use SSDP 1900: the requested block prevents discovering a UPnP gateway across
`bat0`, while preserving local discovery and mesh file transfers. Setup
disables global discovery/relays, but does not explicitly disable NAT mapping.

Protocol references: [Alfred sockets](https://raw.githubusercontent.com/open-mesh-mirror/alfred/main/netsock.c)
and [Alfred protocol analysis](https://downloads.open-mesh.org/batman/papers/Positionsdaten_in_Wireless_Mesh_Networks.pdf),
[Syncthing discovery](https://docs.syncthing.net/specs/localdisco-v4.html) and
[configuration](https://docs.syncthing.net/users/config.html),
[MediaMTX 1.15.3 defaults](https://raw.githubusercontent.com/bluenviron/mediamtx/v1.15.3/mediamtx.yml),
[ATAK CoT defaults](https://raw.githubusercontent.com/deptofdefense/AndroidTacticalAssaultKit-CIV/main/atak/ATAK/app/src/main/java/com/atakmap/comms/CotService.java)
and [chat defaults](https://raw.githubusercontent.com/deptofdefense/AndroidTacticalAssaultKit-CIV/main/atak/ATAK/app/src/main/java/com/atakmap/android/chat/ChatManagerMapComponent.java).

#### Mesh traffic census

Run `sudo /usr/local/bin/manet-mesh-census.py --minutes 5 > /tmp/mesh-census.json`
on the node with a real phone/laptop attached. It passively captures Ethernet
headers on `bat0`, with no probes, firewall rules, multicast joins, promiscuous
mode or retained payloads. JSON rows, sorted by bytes, group traffic by
direction, inferred source bridge port, source MAC, protocol, TCP/UDP source
and destination ports, destination address/kind (including multicast group
and broadcast), and visible VLAN IDs. Counts include packets and Ethernet
bytes; `kernel_drops`, snapshot failures and bounded-row overflow are explicit.
Ctrl-C prints the partial census. Non-IP, unknown protocols and fragments are
counted too; transport ports on noninitial fragments cannot be decoded.

`source_port` is `end0(fdb)` / an AP port from bridge learning, `local(mac/ip)`
from local MAC/IP ownership, `bat0` for ingress, or `unknown` /
`routed-or-unknown`. This is inference from snapshots every two seconds, not
physical ingress tracing: MAC spoofing, VLANs, routing, learning lag and port
moves limit attribution. `sport` is the separate transport source port.
`in` sees arrival before input/forward filtering, so incoming dropped packets
still appear. `out` has passed bridge filtering but does not prove reception
by another mesh node. Nonzero losses/overflow mean detail is incomplete.
Use the observed output to decide further drops later; no additional discovery
protocols are filtered speculatively.

#### Verify the management name

Install the updated IP manager, isolation helper and web server, remove the
retired publisher service and drop-in, and run `manet-dhcp-isolation.py ensure`.
The complete commands follow below. After the next allocation pass, inspect
the generated DNS and static mDNS entries:

```bash
ssh cm4 'set -e
sudo /usr/local/bin/manet-dhcp-isolation.py check
grep -E "^(dhcp-range=|dhcp-option=3,|address=/manet\.local/)" /etc/dnsmasq.d/mesh-eud.conf
cat /etc/avahi/hosts
grep -E "^(host-name=|host-name-from-machine-id=|publish-addresses=|allow-interfaces=)" /etc/avahi/avahi-daemon.conf
systemctl is-active avahi-daemon
ip -4 address show dev br0'
```

The reported allocation expects DNS and the static mDNS A record to be `.147`,
with `publish-addresses=no`, regardless of whether `.2`/`.3` VIPs are present.
On the wired laptop, set `EUD_IF` to its Ethernet interface:

```bash
sudo resolvectl mdns "$EUD_IF" yes
sudo resolvectl flush-caches
resolvectl query -i "$EUD_IF" --protocol=mdns --type=A manet.local
avahi-resolve -4 -n manet.local
dig @10.30.2.147 manet.local A +short
```

For uncached wire evidence, capture on CM4 `end0` while querying from the
laptop: `sudo timeout 30 tcpdump -Q out -nn -vv -i end0 'udp src port 5353'`.
Read the A records themselves; the reply's source IP is not the advertised
address. Expect only `.147` in answers for `manet.local`, never `.146`, `.2`
or `.3`. Check `/etc/avahi/hosts` and `journalctl -u avahi-daemon -n 30 --no-pager` if absent.
An Avahi client has its own cache, separate from systemd-resolved; restart
that laptop's Avahi daemon or wait for expiry if its old VIP answer persists.
Repeat during an otherwise scheduled service election; do not alter VIPs just
for this name test. When the allocator changes the EUD gateway, compare the
new generated DNS and hosts entries with fresh mDNS answers after the
allocation pass reloads Avahi. Offline tests exercise this change without
moving a live node's allocation.

#### Apply and verify the discovery boundary

From the checkout on the dev laptop (these are instructions, not an automated
node operation):

```bash
ssh cm4 'mkdir -p /tmp/manet-discovery'
scp MANET/share/manet/dhcp-isolation.nft \
    MANET/node_tools/manet-dhcp-isolation.py MANET/node_tools/manet-mesh-census.py \
    MANET/node_tools/radio-setup.sh MANET/node_tools/mesh-ip-manager.sh \
    MANET/node_tools/mesh-status.py cm4:/tmp/manet-discovery/
ssh cm4 'set -e
sudo systemctl stop manet-avahi-publish.service 2>/dev/null || true
sudo rm -f /usr/local/bin/manet-avahi-publish.py /etc/systemd/system/manet-avahi-publish.service /etc/systemd/system/avahi-daemon.service.d/20-manet-publish.conf
sudo install -m 0644 /tmp/manet-discovery/dhcp-isolation.nft /usr/local/share/manet/
sudo install -m 0755 /tmp/manet-discovery/manet-dhcp-isolation.py /tmp/manet-discovery/manet-mesh-census.py /tmp/manet-discovery/radio-setup.sh /tmp/manet-discovery/mesh-ip-manager.sh /tmp/manet-discovery/mesh-status.py /usr/local/bin/
sudo systemctl daemon-reload
sudo /usr/local/bin/manet-dhcp-isolation.py apply
sudo systemctl restart node-manager.service mesh-status.service
sleep 20
sudo /usr/local/bin/manet-dhcp-isolation.py check
sudo nft list table bridge manet_dhcp
cat /etc/avahi/hosts
grep -E "^(dhcp-range=|dhcp-option=3,|address=/)" /etc/dnsmasq.d/mesh-eud.conf
grep -E "^(allow-interfaces=|publish-addresses=)" /etc/avahi/avahi-daemon.conf
test ! -e /usr/local/bin/manet-avahi-publish.py
test ! -e /etc/systemd/system/manet-avahi-publish.service
test ! -e /etc/systemd/system/avahi-daemon.service.d/20-manet-publish.conf
if systemctl is-active --quiet manet-avahi-publish.service; then exit 1; fi
systemctl is-active avahi-daemon dnsmasq mesh-status node-manager'
```

Expect `allow-interfaces=<AP>,br0` (or just `br0` without an AP), passing
verification and active services with a connected EUD. In a CM4 terminal,
start this capture, then run the laptop sender below within 40 seconds:

```bash
sudo timeout 40 tcpdump -Q out -nn -i bat0 \
  'udp and (port 137 or port 138 or port 1900 or port 3702 or port 5353 or port 5355)'
```

Expect **zero egress packets** and rising nft `forward`/`oifname bat0`
discovery counters. Capture on `end0` with the same filter to verify the
stimulus arrived and local replies work; zero egress without arriving probes
or increasing counters is inconclusive. Incoming `bat0` captures are before
the drop and cannot be expected to be zero. To exercise node-originated
output as well, run the same Python sender on CM4 with `br0` and its primary
IPv4 address; `output` counters should increase, still with zero egress.

On the wired laptop, substitute its Ethernet interface and assigned IPv4:

```bash
EUD_IF=enxYOUR_ETHERNET
EUD_IP=10.30.2.149
python3 - "$EUD_IF" "$EUD_IP" <<'PY'
import socket, struct, sys, time
iface, ip = sys.argv[1:]
index = socket.if_nametoindex(iface)
def question(name):
    labels = b''.join(bytes([len(s)]) + s.encode() for s in name.split('.')) + b'\0'
    return struct.pack('!6H', 0, 0, 1, 0, 0, 0) + labels + struct.pack('!HH', 1, 1)
for family, groups in [(socket.AF_INET, ('224.0.0.251', '224.0.0.252', '239.255.255.250')),
                       (socket.AF_INET6, ('ff02::fb', 'ff02::1:3', 'ff02::c'))]:
    with socket.socket(family, socket.SOCK_DGRAM) as s:
        if family == socket.AF_INET:
            s.bind((ip, 0))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        else:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, index)
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
        for group, port, payload in [(groups[0], 5353, question('manet.local')),
                                     (groups[1], 5355, question('manet')),
                                     (groups[2], 1900, ('M-SEARCH * HTTP/1.1\r\nHOST: ' +
                                      (f'[{groups[2]}]' if family == socket.AF_INET6 else groups[2]) +
                                      ':1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\nST: ssdp:all\r\n\r\n').encode())]:
            for _ in range(3):
                s.sendto(payload, (group, port) if family == socket.AF_INET else (group, port, 0, index))
                time.sleep(.1)
            print('sent', group, port)
PY
sudo resolvectl mdns "$EUD_IF" yes
sudo resolvectl flush-caches
resolvectl query -i "$EUD_IF" --protocol=mdns --type=A manet.local
```

Expect only the internal EUD gateway IPv4 address **via mDNS** for `manet.local`;
unicast `dig` alone does not prove this. The laptop needs IPv6 enabled on the
link for the IPv6 probes. Repeat from the AP when available.

For an SA positive control, attach an ATAK client to another mesh node and
enable its SA multicast input (so BATMAN learns a remote listener). On CM4:

```bash
sudo timeout 40 tcpdump -Q out -nn -i bat0 'udp dst port 6969 and dst host 239.2.3.1'
```

During that capture, run on the wired laptop (same `EUD_IF`/`EUD_IP`):

```bash
python3 - "$EUD_IP" <<'PY'
import datetime as dt, socket, sys, time
now = dt.datetime.now(dt.timezone.utc)
def stamp(t): return t.strftime('%Y-%m-%dT%H:%M:%SZ')
cot = (f'<event version="2.0" uid="manet-s12-probe" type="a-f-G-U-C" how="h-e" '
       f'time="{stamp(now)}" start="{stamp(now)}" stale="{stamp(now+dt.timedelta(seconds=60))}">'
       '<point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/>'
       '<detail><contact callsign="S12 TEST"/></detail></event>').encode()
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    s.bind((sys.argv[1], 0))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(sys.argv[1]))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 64)
    for _ in range(5):
        s.sendto(cot, ('239.2.3.1', 6969))
        time.sleep(.2)
PY
```

Expect five CoT datagrams leaving `bat0`. Confirm reception on the other
node with `tcpdump -Q in -nn -i bat0 'udp dst port 6969 and dst host 239.2.3.1'`;
the test marker is named `S12 TEST`, at 0/0, and expires after 60 seconds.
Then collect the five-minute census with the real EUD workload.

### Gateway choice and uplink speed

batman-adv picks a gateway itself (the `*` in `batctl gwl`), but that pick only
steers its DHCP handling. Linux needs a default route, which is why this
script exists. It used to route to batman's pick, which switches as
soon as another gateway scores 5 Mbit/s more (`gw_sel_class` 50, BATMAN_V's
absolute threshold), on a single reading, with no time component. On a 6
Mbit/s HaLow path that margin is huge; on a 200 Mbit/s Wi-Fi path it is noise.
And every gateway announced batman's default 10/2 Mbit/s, so the score could
not see uplink speed, and paths faster than 10 Mbit/s all tied.

The script now reads every gateway from `batctl gwl -H -n` and decides itself.
The score matches batman's idea, the bottleneck: the lower of the path
throughput and the announced download bandwidth. A switch breaks every open
internet connection, because the new gateway NATs from a different public
address, so a voluntary switch needs a noticeable gain: at least 1.5 times
the current score and 2 Mbit/s more (relative and absolute, so it means the
same at HaLow and Wi-Fi rates), sustained for 60 seconds, and not within 300
seconds of the last switch. The first choice counts as a switch, so a node that
picked early from a partial list waits out the hold before improving. A gateway
missing from the list, or failing two consecutive one-second pings (about 20
seconds at the steady poll), is replaced at once, best remaining first. After
a restart the gateway the existing `br0` route points at is current, so
restarting the service does not move the node.

Ethernet qualification requires HTTP 204 from connectivitycheck.gstatic.com
or cp.cloudflare.com over plain HTTP. The shared `internet_probe` takes an
`http-only` mode so the dispatcher and Ethernet detector cannot qualify a
portal just because ICMP works. A 200 or redirect fails. Missing curl fails
this stricter mode too. The probe is clock-independent, so a stored clock
months behind cannot block promotion and the ensuing NTS bootstrap.
An endpoint whitelist or forged 204 can still pass this limited check. It
does not prove that every internet application is reachable.

`manet-uplink-speed.sh` separately estimates download bandwidth over HTTPS
from Cloudflare's speed endpoint, falling back to a byte range from OVH.
Each request asks for at most 5 MB with a 15-second deadline. HTTP 200 from
Cloudflare or 206 from OVH, with at least 250,000 bytes, supplies an estimate
even if curl exits 28 at the deadline. Other errors remain unknown. The rate
is bytes divided by transfer time after the first byte, excluding DNS, TCP
and TLS setup. Small samples under-read fast links during TCP startup, but
also let a 1 Mbit/s link announce a measured low score without completing 5 MB.

No usable sample means unknown bandwidth, not failed connectivity. Unknown
Ethernet advertises 1/0.2 Mbit/s, below the metered default of 10/2. This is a
ranking fallback, not a measurement or lower bound. The 250 kB minimum filters
tiny error responses and unstable short samples. A successful estimate is
reused for the same interface/address/router tuple until demotion. Failed
estimates retain the 60-second boot-clock cooldown and retry on subsequent
reconciliation, normally the node manager's three-minute status publish.
This lets HTTPS measurement recover after NTS fixes a cold clock. Repeated
unknown results and cached successes stay quiet. Estimate changes and actual
gateway promotions/demotions are logged once per transition.

Phone tethers and cellular modems (drivers `rndis_host`, `ipheth`, `cdc_ether`,
`cdc_ncm`, `qmi_wwan` and similar) and Wi-Fi uplinks avoid the download test.
They retain the ICMP/HTTP probe with TCP fallback and announce 10/2 Mbit/s.
Every `batctl gw_mode server` supplies explicit whole-kbit `down/up` values
because a bare `server` retains previous bandwidth. Upload is an assumed
fifth of download, with a 100 kbit/s minimum in either direction. Selection
uses only download.

Direct promotion installs metric 100, the mesh manager installs metric 500,
and networkd's candidate DHCP default remains at 1024. Demotion removes the
promoted route and restarts mesh selection. A failed candidate can still be
probed over its own DHCP default while ordinary traffic uses the mesh.
Both curl requests use IPv4, disable proxy use and bind the candidate device.
Linux route lookup rejects nexthops on other devices when the socket uses
`SO_BINDTODEVICE`, so the lower-metric mesh default cannot carry that request.
The existing ICMP path uses `ping -I`. Name resolution is separate and may
use resolved over a mesh path or its cache. This does not make a dead direct
HTTP path pass, but it does mean qualification is not a test of that uplink's
DNS service. If normal HTTP attempts fail, a device-bound lookup through the
candidate's DHCP DNS supplies curl's `--resolve` address. A lone node can
therefore qualify before publishing DHCP DNS globally. Qualification still
fails if neither resolution path provides an endpoint that returns a bound
HTTP 204. The qualified DNS policy is described under Network management.

Mean of BATMAN_V's metric across this node's originators, in Mbit/s, published
as `MEAN_THROUGHPUT_MBPS` and used by the service elections.

Reads the parenthesized value, never a positional field: `batctl o` shifts
columns on the selected route (prefixed `*`), so `$3` is the last-seen timestamp
there and `(` on the others. Averaging `$3`, as this did until 2026-08-16,
produced 0.14 on a mesh running at 43 Mbit/s. Keeps the best path per
originator, so one peer reachable over two radios is still one peer.

**manet-ipcalc.sh**

Pure-bash replacement for Debian's `ipcalc`, printing the same `HostMin:` /
`HostMax:` lines the mesh scripts parse. The Perl original cost ~1.5 s of CPU
per call on a CM4 and was invoked ~9 times per 15-second cycle, nearly a full
core. This runs in ~10 ms.

**batman-if-setup.sh**

Manages BATMAN-ADV interface lifecycle:
- Creates `bat0` interface.
- Enslaves active mesh roles (including a returned AP candidate).
- Sets BATMAN_V algorithm.
- Handles start/stop operations.
- HaLow is added first so it becomes batman's primary (longest-range link).
- Skips writing a `.link` file when the MAC is already pinned by an existing
  one; two link files for one MAC caused a rename ping-pong reboot loop.

---

## Time synchronization

`mesh-time-sync.py`, started by `one-shot-time-sync.service` through the retained
shell entry point, is the sole runtime owner of chrony's configuration and
start/stop lifecycle. The manager, Ethernet detector and off hook no longer
replace its config or stop it independently. This prevents a completing client
attempt or departing Ethernet link from stopping a newly available GPS source.
The active uplink dispatcher already records `mesh-gateway.state` and
`upstream_iface`; the controller observes those files as well as carrier, so
both the active dispatcher and the legacy detector can trigger internet sync.

Ethernet promotion now depends on plain HTTP connectivity, so HTTPS bandwidth
measurement can fail with a cold clock without preventing this role. During
initial acquisition, `chrony_config` emits `nocerttimecheck 1` and
`makestep 0.1 3`. Certificate activation/expiration dates are waived until the
first clock update, with signature, hostname and trust-chain checks retained.
There is no months-behind limit in this profile. Verified completion removes
both startup directives and disarms further live clock steps. An artificial
clock rollback after `/run/initial_time_synced` exists does not re-enable the
bootstrap exception and is not equivalent to a cold boot.

Local roles are checked every 15 seconds. A recent `gps_status.json` fix enables
the GPS SHM refclock. A direct uplink enables configured public NTS servers, with
`bindacqdevice` restricting NTP acquisition to that interface. Source profiles
allow the configured IPv4 mesh CIDR and the existing IPv6 mesh prefix. Client
and idle profiles deny serving; no profile has a `local stratum` fallback or
unconditional internet sources on an ordinary mesh client. Both provisioning
templates emit the same service and a quiet initial chrony config.

The controller reads `chronyc -n tracking` and `sources` with checked exit status
and bounded command timeouts. Qualification requires a real reference, stratum
1–15, a synchronized leap status, at most 0.1 seconds of remaining system-clock
correction, and a recent selected source with nonzero reachability. This is a
completion criterion, **not a bound on absolute UTC error**. GPS and internet
markers require a selected GPS refclock or an uplink NTP source respectively.
The internet source must also have negotiated NTS authentication, checked
through `chronyc authdata`; an NTS entry still awaiting keys does not qualify.
Invalid queries or loss of qualification withdraw those markers. The existing
manager publish path ORs `/run/mesh-ntp-gps.state` and `/run/mesh-ntp.state` into
type-68 `is_ntp_server`; cadence is unchanged and no separate announcement is
sent. Cached advertisements can therefore outlive source availability until
the next telemetry update; actual NTP validation still has to succeed.

Ordinary clients parse registry assignments as data, never source them as shell.
`MAC_ADDRESSES` maps interface originators to canonical identities, and IPv4
addresses must be unambiguous host addresses within the mesh CIDR. Only selected
`originators_json` routes (`best: true`) with positive BATMAN_V throughput and
last-seen age at most 30 seconds qualify. Highest throughput wins, with a MAC
tie-break. Self, shutting-down and unadvertised nodes are excluded. Registry
wall-clock timestamps and the derived STALE state are deliberately not liveness
gates: the local clock being repaired cannot reliably compare them. Kernel
route age and an actual recent NTP measurement provide bootstrap liveness.

No usable peer or failed discovery causes a local retry after 30 seconds. A
selected peer gets a 90-second attempt, followed by a five-minute exclusion on
failure so another source can be tried. Deadlines and exclusions use monotonic
time and persist in `/run/mesh-time-client.json` across service restarts, without
extending the attempt. Successful client sync requires the selected source to
match the chosen address and the sample to be from the current attempt. It
creates `/run/initial_time_synced`, writes the quiet profile and stops chrony.
The marker survives process restarts, not reboot. A nonblocking file lock
prevents duplicate owners.

The operational expectation is one or two days of use, sometimes longer, with
most nodes directly using GPS. ACS needs alignment on a seconds scale, not
millisecond accuracy. Ordinary mesh clients therefore refresh after **six
hours plus 0–600 seconds of per-node staggering**. The jitter is generated once
per boot and persisted with the monotonic refresh deadline. Service restarts
and wall-clock corrections cannot postpone it. Local source qualification
renews the deadline, so losing GPS/internet starts a holdover period based on
the last verified source. Between refreshes there are only local role checks;
no BATMAN query, NTP polling or extra Alfred announcement is required.

For scale, an assumed residual error of 50 ppm accumulates 1.08 seconds in six
hours, versus 8.64 seconds in two days. Oppositely drifting nodes can differ by
twice that amount. Those are arithmetic examples, **not measured CM4 limits**.
ACS has a five-second future-message allowance and late-apply grace; those
checks are not a blanket guarantee that every operation tolerates five seconds
of skew. Hardware measurements should determine whether the refresh interval
needs adjustment. No freshness guarantee is made if sources remain unreachable.

Each due refresh uses the same 90-second attempt and peer backoff as initial
sync. An unsuccessful refresh retains the boot-sync marker and previous success
time, keeps serving flags off, and retries discovery without claiming accuracy
or blocking ordinary mesh operation. If the refresh state is missing, a boot
marker alone cannot grant another six-hour delay. Startup may step the clock;
after the first verified sync the controller disables remaining live startup
steps with `chronyc makestep 0.1 0` and removes the directive from the config.
All later source changes, daemon restarts and client refreshes use slewing, so
routine correction does not jump backwards through saved ACS/admin timestamps.
`leapsecmode slew` also avoids a kernel leap-second step. Peer profiles use
`corrtimeratio 1`, preferring an average correction over one poll interval rather
than three to fit the bounded attempt, still subject to chrony's slew-rate cap.
The current source daemon is not restarted just to disarm stepping.

The old positional `batctl o` reader treated last-seen timestamps or `(` as the
metric and did not map radio MACs. Its unchecked command failures, executable
registry input and unbounded registry wait are removed. The old
`waitsync 60 0 0 1` also skipped the remaining-correction check: zero disables
that bound. See the primary [chronyc command reference](https://chrony-project.org/doc/4.6/chronyc.html)
for `tracking`, `sources` and `waitsync`, and the [chrony configuration reference](https://chrony-project.org/doc/4.6/chrony.conf.html)
for acquisition binding and source settings.

Remaining limits: ACS/admin activation and tourguide rendezvous still depend
on wall-clock alignment. Initial synchronization now gates timed control, with
nonce-bound cold lobby recovery as described above. Arbitrary external clock
steps after synchronization are not repaired by ACS. An unusually large
later correction may not settle inside a bounded refresh; it is not forced by
stepping. CM4 checks of real chrony/GPS timing, 24–48-hour drift, source loss,
missed telemetry and on-air traffic remain pending hardware setup.

---

## Data management

Node state is exchanged over Alfred as two message types, split by how often
the contents change. Alfred replicates every record to every node on a timer,
so anything that repeats is paid for continuously.

| Type | Message | Published | Contents |
|------|---------|-----------|----------|
| 67 | `NodeIdentity` | startup, allocation changes, 270 s keepalive | hostname, MACs, Syncthing ID, chunk, IP |
| 68 | `NodeTelemetry` | every startup loop, then 180 s | everything volatile |
| 69 | `NodeTelemetry` | tourguide window | public helper diagnostics; not migration authority |
| 74 | encrypted `acs_state` | 5 s and protocol changes | readiness, proposal, votes, commit, operating plan ID; one full-certificate publisher per plan |
| 75 | encrypted `acs_helper` | 5 s during lobby dwell | authenticated recovery channels and partition size |
| 76 | encrypted `acs_probe` | at most once per 60 s, only clockless nodes with peers | requester boot, random challenge, current Wi-Fi channels and radio aliases |
| 77 | encrypted `acs_probe_reply` | on demand over surviving paths or during tourguide visits, at most once per 5 s | channels/size and recipient boot/challenge pairs |

Alfred stamps every record with the publishing node's MAC; it runs `-i br0`, so
that key *is* the node's primary MAC. It is the join column between the two
types, and the reason neither message repeats it.

Identity is republished at 270 s because Alfred purges any record it has not
seen for `ALFRED_DATA_TIMEOUT` (600 s, confirmed on hardware: a test record was
purged at 618 s). At 270 s a publish can fail once and the record still
survives.

**mesh-registry-builder.sh**

Central registry builder.
The shell entry point now execs `manet_registry_builder.py`; IP discovery
imports the same builder directly. It decodes all records in one interpreter
and caches decoded payloads beside `observed.tsv`, invalidating that cache when
the decoder/schema changes. Every pass still reads both Alfred types and ages
observations, including tombstones and stale-claim removal. Identical claim
files keep their inode/mtime; registry display ages remain current.

The claimed-address index shares `/run/manet-registry` with observation and
decode state. The builder creates its directories on demand, including when
called by IP discovery instead of the shell entry point. All allocator readers
use this path; the old `/tmp/claimed_chunks.txt` is never a fallback.
Services with a private or read-only `/tmp` share the same file. No single
service owns a `RuntimeDirectory` that could remove shared observation state on
restart. Reboot clears the cache and normal registry discovery rebuilds it.
`MESH_CLAIMED_CHUNKS_FILE` overrides the path consistently in the producer,
allocator and runtime reconciliation, including isolated tests.

`manet_hosts.py` accepts only single-label DNS names and usable addresses
inside ipv4_network. It reserves localhost and unmanaged /etc/hosts aliases,
comparing names without case. The node's own row stays, so its name maps to
its br0 address. A peer claiming the same name makes a conflict. Conflicting addresses for a
name are omitted. Identical claims collapse to one row. Invalid network config
leaves the hosts file intact and reports failure.

`mesh-hosts-update.sh` delegates to `manet_hosts.py`. The timer still runs
every two minutes, but sorted, deduplicated host/address mappings determine
whether `/etc/hosts` changes. Peer telemetry timestamps, record order and other
unrelated fields cannot cause a rewrite. Identical content does not open a
temporary file, chmod the destination, or log an update. This avoids up to 720
unnecessary hosts rewrites per day on a node with unchanged peer mappings.
The helper parses quoted registry assignments without executing shell syntax,
validates names and IPv4 addresses, and preserves all bytes outside the marked
block. A `/run` lock serializes refreshes, and the shared configuration writer
preserves ownership/mode while fsyncing a same-directory replacement and its
parent directory. Missing/empty snapshots are skipped; malformed markers,
malformed host assignments and symlink destinations fail without replacing hosts.

`mesh-ip-manager.sh` requests `manet_ip_runtime.py` work from the existing
channel-agreement process through `manet-runtime-client.sh`. A worker thread
sleeps in `select` until requested; the main ACS loop sleeps 5 seconds
between passes, or 1 second through the proposal window and from 5 seconds
before a live plan's vote deadline to the end of its apply window. The
round's readiness request signals the loop (SIGUSR1) so readiness publishes
at once. Agreement assumes a node's request plus Alfred replication reaches
every peer before :45, which the tests check for requests by :28 and up to
15 seconds of replication. Root-only FIFOs and a client lock serialize
requests, accept only allowlisted operations/arguments, and return status
plus data. An unavailable or busy worker permits a standalone one-shot
fallback. Service observations allow a bounded one-second client-lock wait
before falling back, while other callers never queue. A submitted request's
failure or timeout never replays a potentially applied operation.
Operation framing accepts digits (`ipv4`); malformed requests with valid reply
tokens return an error instead of silently timing out. Data-returning operations
reject empty success replies, except Syncthing's missing first-boot certificate.
Allocated identities retry failed/empty address observations without publishing
an empty IPv4 or advancing the keepalive timer. Discovery, DHCP isolation
and primary-address selection share the resident interpreter. Before
skipping the shell allocator it verifies live nft isolation, EUD forwarding,
addresses/prefixes, dnsmasq state and UI-table existence. It compares config,
claims, allocation markers, generated DNS/mDNS files, local MACs and helper
generations with a previously successful, unchanged reconciliation. Contested
or incomplete claims, missing addresses, pending reloads, failed checks and
changed files take the normal allocator path. The cache is volatile in `/run`;
no polling interval, service-election gate or publication deadline changes.

- Reads both Alfred types and joins them on the record key.
- Decodes each message.
- Writes `/var/run/mesh_node_registry` with all node state.
- Writes `/run/manet-registry/claimed-chunks.txt`, the claimed-chunk index
  [`mesh-ip-manager.sh`](#network-management) allocates from.
- Caches identity across cycles: a node whose identity record has not been
  refreshed yet keeps the values from the previous registry rather than
  appearing nameless.

Freshness uses this node's boot clock and changes to the telemetry payload,
not the sender's timestamp. `/run/manet-registry/observed.tsv` stores the payload
hash, the uptime when it last changed, and the uptime when last present. Reading
an identical record does not refresh it. Missing records retain tombstones for
900 seconds, beyond Alfred's record expiry, so a cached record reappearing does
not regain freshness. Failed Alfred reads preserve the last complete snapshot.

`OBSERVED_AGE_SECONDS` is the local observation age at registry construction;
`OBSERVED_AT_UPTIME` is the corresponding `/proc/uptime` time and lets readers
age a saved registry without wall clocks. After 300 seconds the registry marks
a record `STALE` and excludes its claim. `LAST_SEEN_TIMESTAMP` is sender time
for display only; `LAST_REGISTRY_UPDATE` is local wall time for display. A peer
first observed after our boot initially counts as fresh, conservatively keeping
its claim while discovery proceeds.

**encoder.py**

Encodes this node's Alfred payloads to protobuf and Base64. Two subcommands:

- `encoder.py identity`: hostname, secondary MACs, Syncthing ID, chunk, block size, IP.
- `encoder.py telemetry`: mean throughput, service flags, uptime, battery, CPU load,
  GPS (when `/run/gps_status.json` reports `has_fix=true`), channels and scan
  reports, MCS rates, interface list, EUD mode/SSID/count, tourguide tracking,
  node state.

**decoder.py**

Decodes a payload to shell variables: `decoder.py identity <b64> --node-mac <mac>`
or `decoder.py telemetry <b64>`. Identity needs the record key passed back in so
it can reassemble `MAC_ADDRESS` / `MAC_ADDRESSES`.

**manet_ids.py**

Wire/display conversions shared by the encoder and decoder. MACs travel as 6 raw
bytes rather than 17 characters, Syncthing device IDs as their underlying 32
bytes rather than the 63-character dashed form (Luhn check characters and all),
and IPv4 as `fixed32`. Formatting back into human shapes happens on decode.

**NodeInfo_pb2.py**

Generated from `NodeInfo.proto`. Checked in because nodes have no protoc. Do
not hand-edit; regenerate.

**NodeInfo.proto**

Protocol buffer schema for both messages.
- Compile with: `protoc --python_out=. NodeInfo.proto`, from **the dev venv**:

  ```bash
  source ~/.venvs/manet/bin/activate    # bash MANET/packaging/setup-dev-env.sh
  cd MANET/node_tools && protoc --python_out=. NodeInfo.proto
  ```

- **Use protoc 3.21.x.** Nodes run the protobuf 4.21.12 Python runtime, which
  rejects generated code from protoc older than 3.19, and 5.x emits a
  `runtime_version` gate that runtime does not have.
- **Not `/usr/bin/protoc`.** Ubuntu 22.04 ships 3.12.4, which emits the old
  `_reflection`-based form: 786 lines different, and unimportable on every
  node. This is easy to do by accident and nothing downstream catches it: a dev
  box with the matching-vintage protobuf runtime imports the bad file happily.
  `setup-dev-env.sh` puts the right protoc inside the venv precisely so
  activating it cannot give you one without the other.
- Changing a field type is a flag day: there is no compatibility path, so all
  nodes must be reflashed together.

---

## Mesh configuration push

**manet_admin.py: shared admin authority over Alfred**

One administrator normally knows the password; other users read status. Every
node has the same `admin_password`. SAE membership, a publisher MAC, and
unencrypted telemetry cannot authorize a change. Receivers authenticate before
inspecting actions, choosing the newest message, staging, ACKing, cancelling,
or applying. Web authentication uses only `admin_password`, with no radio/AP
password fallback.

| Alfred type | Authenticated payloads |
|-------------|------------------------|
| 70 | `mesh_config`, `mesh_config_cancel` |
| 71 | `radio_state`, `radio_cancel` |
| 72 | `radio_ack` |
| 73 | `config_ack` |
| 74 | `acs_state` |
| 75 | `acs_helper` |
| 76 | `acs_probe` (clock-independent discovery only) |
| 77 | `acs_probe_reply` (nonce-bound cold recovery only) |

The wire format is a `manet_admin_v1` envelope carrying a 16-byte publisher
salt, 12-byte random nonce, and AES-256-GCM ciphertext with its authentication
tag. The encrypted body carries the action, a random message ID, and a send
timestamp in nanoseconds. Additional authenticated data binds the protocol
version and Alfred type, preventing a message from being used on another
control channel. A new nonce is generated on every send. Password-derived
keys use scrypt (`n=32768`, `r=8`, `p=1`, 32 MiB) and a domain-separated random
salt per publisher. Keys are cached only in process memory. The implementation
uses the cryptography library's
[AEAD](https://cryptography.io/en/43.0.0/hazmat/primitives/aead/) and
[scrypt](https://cryptography.io/en/43.0.0/hazmat/primitives/key-derivation-functions/#scrypt)
APIs. Password rotation encrypts the new password under the current password;
the apply log never prints either value.

`manet_admin_limits.py` bounds receive-side scrypt work across processes using
root-private tmpfs state in `/run/manet-admin-receive`. Unknown salts share a
burst of eight derivations, refilling at one per second; cache-miss derivations
serialize their memory use. A bounded allowlist of authenticated salt identifiers
keeps healthy senders off that budget across receiver restarts. Derived keys
remain in process memory. Exact invalid envelopes are cached for sixty seconds;
salt alone is never a rejection key. Credential-file generation scopes both
caches. Separate bounded-wait locks keep cache reads independent of slow KDFs.

Replay history lives in `/var/lib/manet-admin`, with directory mode 0700 and
atomic, fsynced 0600 files. Each control channel remembers its newest accepted
message and whether it was consumed. Repeated delivery of a pending stage is
allowed. Older messages and consumed activations/cancellations are rejected,
including after loss of `/run` or a rollback. The config receiver first saves
a durable local job and then records transport consumption. An ACS change can
kill its own node manager, so the independent transient unit owns application.
Transport consumption prevents replay, while the journal permits local retries
of unfinished work. Radio-state control retains its separate existing consume
semantics. Do not erase admin history during rollback. Corrupt history fails
closed.

Normal authenticated envelopes expire after 900 seconds and may be at most 60 seconds
ahead of the receiver's clock. Newly provisioned nodes lack replay history,
so expiry bounds their acceptance of previously recorded, otherwise valid
messages. These checks require synchronized clocks, as scheduled activation
already does. Each staged edit also gets a fresh random transaction version;
repeating identical settings does not reuse an earlier ACK. Config activation
uses encrypted type-73 ACKs, never `CONFIG_ACK_VERSION` from public telemetry.
Both send and receive wait for the initial-sync marker. Types 76–77 use only
the separate challenge API and the monotonic nonce lifetime described above;
their fixed zero timestamp is never considered a time source or an admin order.

The dependency is Debian's `python3-cryptography`; imports are lazy so its
absence disables control without disabling the public status page. Setup and
the updater install it, and an enabled `manet-admin-setup.service` handles
older tools updaters at the next boot. The service runs independently of
node-manager startup. Upgrade every node and restart the web publisher before
using control; there is no plaintext compatibility fallback. No protobuf
schema changes are needed.

Earlier publishers included `admin_password` in plaintext config packages.
An exposed password must be replaced through a trusted path on all nodes;
encrypting a replacement under a password already known to an observer cannot
revoke that observer. The encryption change does not retroactively protect
old broadcasts or old apply logs containing passwords.

This protects the Alfred control path. Browser management intentionally uses
HTTP over the local AP or Ethernet for trusted-team deployments. The shared
admin password primarily prevents accidental settings changes by teammates;
HTTPS is optional rather than a deployment prerequisite. HTTP does not
protect credentials against a participant who intercepts the connection.
The shared password also exists on each node: root access to a node conveys
network administration.

**mesh-config-write.py**

Config and supplicant values are written as literal data using atomic file
replacement, preserving ownership and permissions. The old interpolated sed
replacement interpreted delimiters, ampersands, and executable sed commands
inside accepted values. Regression tests cover those strings, backslashes,
quoted SSIDs, and rejected newlines.

Dangerous apply reconciles every explicitly named SSID/key in every mesh
supplicant file, even if mesh.conf already matches. It restarts the supplicants
before committing the dangerous mesh.conf values. The journal retains the
activation obligation across a crash or failed restart. Disabled MediaMTX and
Mumble services are reconciled even when their mesh.conf values already match.
Enabled services remain governed by the normal elections.

Ordinary supplicant quotes delimit literal bytes, so the writer does not double
backslashes. Embedded quotes use hexadecimal string encoding. Mesh changes and
rollback exclude USB `*-uplink.conf` files. A shared helper selects the standard
or HaLow service from configured roles and checks both service activity and a
control-socket PONG. A failed restart prevents a successful apply marker and
retains a rollback snapshot for retry.

**mesh_config.py**

The one place that decides which settings are this node's own and which belong
to the whole mesh. `LOCAL_KEYS` is `eud`, `lan_ap_ssid`, `lan_ap_key` and
`max_euds_per_node`; `split_config()` partitions a submitted form into a local
half and a mesh half, and `strip_local_keys()` is what keeps the local half off
Alfred. The EUD AP is a node's own Wi-Fi for its own clients, and broadcasting
it renamed every AP on the mesh.

`max_euds_per_node` is stripped with the others but excluded from
`LOCAL_APPLY_KEYS`: it sizes the IPv4 chunk allocated at flash time, so the
management UI shows it and never writes it. Shared by `mesh-config-sync.py`,
`mesh-status.py` and `manet_manage.py`, so the receiving end and the submitting
end cannot disagree about which half a key is in.

The form validates all changed fields before local writes or mesh publication;
unchanged provisioned values do not block unrelated edits. Receivers use the
same allowed keys, byte lengths, enums, and IPv4 allocation-capacity checks.
`manet_eud_ap.py` preserves the `-xxxx` node suffix, so an AP base SSID may use
at most 27 UTF-8 bytes. It writes mesh.conf and hostapd atomically per file,
restarts hostapd, and restores both files if activation fails. Telemetry reads
the actual configured broadcast SSID. Measurement session paths use a bounded
label alphabet, reject dot/parent and symlink paths, and generate random result
filenames; peer display names remain JSON data.

**manet_config_journal.py: local apply and rollback**

`/var/lib/manet-config/journal.json` has schema version 1. The directory is
0700 and journal files are 0600. One nonblocking flock excludes receivers,
migration and workers. Each transition uses atomic replacement with file and
directory fsync. The directory's parent is fsynced on creation too.

A job records authenticated message order and payload digest, literal desired
settings, phase, consumption state, original backup and deadline, and the
last error class. Errors omit subprocess output because it may contain secrets.
Terminal transaction versions retain tombstones. A duplicate or reissued
terminal version cannot create another job. New work is blocked while an
activation or restoration owns the journal. An unactivated stage may be
replaced or cancelled by a newer authenticated order.

The receiver saves staging before publishing an ACK. When the verified clock
and scheduled activation allow it, it saves `prepared` before consuming the
transport order. A crash between these operations is harmless: local recovery
finishes consumption from saved message metadata, without decrypting again.
`mesh-config-apply.sh` delegates to the locked journal worker. The existing
`mesh-config-apply` transient systemd unit remains outside node-manager's
restart scope and retains its 180-second timeout.

Before a protected change, the worker obtains a bounded BATMAN peer count and
captures mesh.conf and mesh supplicant contents plus ownership/mode. It saves
that snapshot and the original boot-clock deadline with `applying`, before
any live write. `*-uplink.conf` files are excluded. A failed peer query or
snapshot cannot be treated as a solo-node baseline. Explicit boolean
`no_rollback: true` bypasses the trial, not durable application tracking.

An interrupted `applying` phase reconciles files and repeats required service
activation. Successful shell reconciliation checks service restarts and PONG,
then the controller independently compares requested assignments and probes
service health. It persists `verified` before publishing the applied marker.
Deferred region outputs are fsynced too. Their saved fingerprints track region
assignments without treating later ACS channel changes as drift. A CIDR change
also waits for `/etc/mesh_ipv4_state` to name the requested network and for its
assigned address/prefix to appear on br0. The IP manager retains ownership of
address reconciliation, including during rollback. Its durable allocation file
is never removed by config apply.
Safe changes commit immediately. Protected changes keep their original trial
and backup. A verified trial with drift returns to reconciliation without
extending the deadline.

At the deadline, a verified job commits only if its files/services still match
and a peer returned, or its original baseline was successfully empty. Failed
peer queries cause restoration. A changed boot ID expires the trial immediately:
there is no reboot grace extension. An unfinished apply at expiry also restores.
The worker saves `rolling_back` before restoring any file. Every restore uses
atomic fsynced writes and retained metadata. Failed service restarts keep that
phase and backup for another cycle, even if peers return meanwhile. Only a
completed restore becomes `rolled_back`, which is terminal.

Config sync processes local work before reading Alfred. It needs neither the
original ciphertext nor its expiry window nor the old password after rotation.
Only first activation needs the initial clock marker. Volatile pending/ACK and
applied markers are projections of journal state. Type-73 payloads distinguish
`application_status` from their staged transaction version, and set
`applied_version` only for verified or committed work. The existing pending
admin API exposes the application phase without adding a UI flow.

`sync_alfred_command()` calls type-70 config sync when a local journal, pending
package or trial exists, even if the Alfred read is empty or fails. The status
API reads the credential-free `config_application_status()` projection for
application and rollback status. Legacy rollback state remains a fallback
when no journal job exists.

**Upgrade migration and the legacy rollback helper**

`mesh-config-rollback.sh` remains available for older trials. Migration copies
its snapshot and deadline into the journal before renaming its `state` file to
`journal-owned`. The next legacy `check` is then a no-op. A crash in that gap
is repaired by completing the retirement before the new worker starts. The
old backup remains available on disk.

`mesh-config-sync.py migrate` imports a private pending `/run` package and
any armed trial, including a trial whose old applier already cleared pending.
A pending activation with matching ACK and consumed replay history becomes
local prepared work. An unactivated stage retains its scheduling/ACK gates.
The import refuses an active old applier. The updater stops the manager and
worker before replacing payload files, saves private legacy pending/ACK
inputs in `recovery/config-staging.json`, installs the receiver, applier and
journal together, and runs migration before any activation. The saved inputs
restore an interrupted import after `/run` disappears. Early boot payload
recovery restores those inputs before the manager starts. It runs no services.
A persisted journal takes precedence over that copy. Migration failure keeps
the update pending, withholds both version markers and leaves the manager
stopped. Offline recovery repeats migration before service restarts. Ordinary sync also
imports, but updates do not depend on its first pass. Preserve both admin
replay history and the apply journal in future updates.

---

## Updates

`manet-kernel-protect.py` installs the shipped APT preference policy and
neutralizes distribution boot writers before the updater installs any APT
dependencies. The updater requires both payload files and reapplies protection
in its install step, including offline recovery. File-only early boot recovery
restores the payload; the later finish step applies holds and hook guards before
service activation and version commits. Protection failure leaves the update
failed or pending for retry, with the previous version marker retained.

`manet-admin-setup.sh` also invokes the helper from its own directory before
checking cryptography. This makes the first update through an older updater
apply the newly staged policy even when no dependency installation is needed.
The helper resolves its policy relative to the archive staging root, or `/`
when installed. It takes APT's POSIX frontend lock, writes complete policy and
configuration files, registers local dpkg diversions and holds only installed
boot packages not already held. It never installs or removes packages.
Headers are pinned against installation/upgrade but not held, allowing the
existing OS cleanup to remove them. No broad `linux-*` pin is used.

Userland-triggered `update-initramfs` calls return success without touching
boot files; package upgrades update its `.manet-distrib` copy. Pi firmware
configuration disables kernel/initramfs copying, and the firmware package is
pinned because its own postinst also copies boot firmware. Rock 3A's `99-uboot`
hook is diverted, while `flash-kernel` is disabled by its machine setting.
See [kernel ownership](kernel-6.18-morse-port.md#81-apt-owns-userland-manet-releases-own-the-kernel)
for the package scope and recovery procedure.

Automatic updates require `auto_update=` set to a true value, a direct uplink
or mesh-gateway default route, and initial qualified clock sync. Checks are
queued on route readiness, initial clock sync, and daily. See
[networkd-dispatcher/README.md](../MANET/networkd-dispatcher/README.md).

GitHub Releases now supplies metadata and packages together. Stable selection
reads `manet-release.json` through the Latest release URL; development explicitly
selects the newest published MANET release by publication timestamp and ID.
Fixed tag URLs bind the manifest, archive and checksum to one release. A source
push does not trigger updates. Publication checks all six archive versions,
uploads to a draft, verifies GitHub asset digests, then publishes. Routine builds
are prereleases; explicit promotion sets stable and Latest. Cleanup retains the
newest three prereleases and every stable release.

The updater checks both the manifest digest/size and the checksum sidecar before
its existing archive validation. A newer installed development version is left
alone by stable checks unless `--allow-downgrade` is supplied. Channel selection
is per invocation; Ethernet-triggered automatic runs always select stable.
See [release tooling](../MANET/releases/README.md).

---

## Provisioning

**CPU frequency policy**

`manet-cpu-policy.service` runs before node-manager and mesh voice. It brings
every CPU with a hotplug control online, then applies schedutil to every
cpufreq policy, falling back to ondemand or retaining the kernel governor.
Only a device-tree model containing `Compute Module 4` receives a ceiling:
`CM4_MAX_FREQ_KHZ` in `manet-cpu-policy.sh` is 800000 kHz. If the frequency
table omits that value, the highest supported value below it is used, never
below `cpuinfo_min_freq`. Rock 3A, Raspberry Pi 5 and other boards use their
reported `cpuinfo_max_freq`. No mesh.conf setting controls this policy.

The previous powersave governor held CM4 at 600 MHz and Rock 3A at 408 MHz
on two cores. Its 1008000 kHz ceiling could not help because powersave stays
at the minimum; Rock 3A also lacks that operating point and rounded the
ceiling down to 816 MHz. A CM4 to Rock 3A Wi-Fi iperf3 link reached about
100 Mbit/s under that policy and 400 to 480 Mbit/s after it was stopped.
With schedutil and normal mesh traffic, CM4 spent 87% of its time at 600 MHz,
supporting demand scaling for idle heat control. The 800 MHz CM4 ceiling
limits enclosure heat while allowing bursts above idle. Rock 3A is used as
an open-air bench board. These measurements do not establish throughput
or enclosure temperature under the new ceiling.

Setup and updates stop and remove the old unit before applying the new
policy. Stopping the new oneshot does not undo the policy. The service keeps
`ProtectKernelTunables=yes` and opens only `/sys/devices/system/cpu` with
`ReadWritePaths`, so CPU attributes remain writable inside its otherwise
protected sysfs tree. It needs root ownership access but no capabilities.
The script logs missing hotplug or cpufreq support and succeeds on those
kernels; failed writes return an error. Tests redirect `MANET_SYS_CPU` and
`MANET_CPU_MODEL_FILE` to temporary files without root.

**Mesh supplicant selection and recovery**

`wpa_supplicant@.service.d/20-manet-binary.conf` resets ExecStart to
`manet-mesh-supplicant.py`. It executes `/usr/sbin/wpa_supplicant_manet` or
logs a WARNING and falls back to Debian's `/usr/sbin/wpa_supplicant` when the
MANET binary is absent. The AP guard and interface preparation still apply.
All standard mesh start paths share this template: radio setup,
`manet_supplicant.py`, channel changes from `manet_radio.py`, BATMAN setup,
ACS, lobby/tourguide changes, node-manager and Ethernet AP transitions.
USB client uplinks use their own unit and keep Debian's binary.

The Wi-Fi build enables HE and patches mesh peer lifecycle handling. An
established peer is freed at ten seconds of kernel inactivity, without
random fuzz or AP polling delays. A beacon refreshes kernel activity;
a driver query failure alone does not establish silence. ENOENT proves a
missing station and also permits immediate replacement on a peer candidate.
Failed SAE attempts have a three or four second cleanup deadline armed
before local setup. Cleanup frees the station and its timers instead of
setting PLINK_BLOCKED. A new beacon can then initiate SAE. INFO logs expose
exchange steps and status without secrets.

`mesh_max_inactivity` is a global supplicant setting. Fresh Wi-Fi configs
and AP-to-mesh rendering write ten seconds. The start wrapper adds missing
settings to live and lobby configs; frequency and credential edits preserve
existing settings. The updater invokes `--update-configs` after installing
its payload and before service restarts. A ten-second `flock` acquisition
on `/run/channel-election.lock` serializes this rewrite with ACS and AP
transitions. Contention leaves the update pending for retry. It adds missing
settings to all mesh configs, including lobby copies, preserving tuning.
The updater uses `try-restart wpa_supplicant@*.service` after daemon-reload,
so running Wi-Fi mesh instances adopt a delivered binary immediately.
Disabled instances remain stopped. Updates still commit version markers
only after their service checks succeed.

All archive builders stage `wpa_supplicant_manet` and `wpa_cli_manet` into
`/usr/sbin` when available. They warn if either is missing; the runtime
wrapper provides the missing-supplicant fallback. The existing SAE watchdog
continues to support Debian fallback installs that can emit AUTH-BLOCKED.

HaLow retains an explicit 300 second inactivity setting and the kernel
`mesh_plink_timeout=0` guard. Its candidate build includes SAE recovery and
stale-station patches but no accelerated established-peer expiry. Candidate
names are excluded from packaging and service selection pending bench
verification. Key teardown can wedge Morse firmware; failed SAE cleanup
therefore requires separate testing even at the conservative idle timeout.
Build inputs and hardware validation notes are in
[binaries_arm64/README.md](../MANET/binaries_arm64/README.md).


**manet-ap-guard.sh**

Decides, for one interface, whether a mesh supplicant may start on it right
now. Installed as an `ExecCondition` on `wpa_supplicant@.service` through
`/etc/systemd/system/wpa_supplicant@.service.d/10-manet-ap-guard.conf`, so it
applies to every caller that starts or restarts a mesh supplicant.

The AP candidate may serve mesh when a wired EUD takes priority in auto mode,
or, with ACS, whenever the node is not a gateway.
The transition helper publishes its active mesh role before starting the
supplicant. This guard requires that membership and an inactive hostapd; a
missing mesh role refuses startup even while hostapd is still preparing.
Starting a mesh supplicant against an AP otherwise fails with -95 and can
leave hostapd active over a dead BSS.

Exits 0 to allow non-AP radios or a released candidate with an active mesh role,
and 1 to skip a radio reserved for AP.

`have_package_network` is checked before each apt phase, so "no network" is
recorded once before attempting package downloads.

This exists because a node reached the field with none of its packages
installed: its Ethernet was unplugged part-way through provisioning, every apt
call failed silently behind `|| true`, and the script still touched
`/var/lib/radio-setup.done`. Nothing on the node said anything was wrong.

The log is opened with `tee -a`, not `tee`. It used to truncate per run, which
destroyed the history of the run that went wrong, and two overlapping runs
interleaved into an unreadable file.

Interface renames reuse `radio-setup-run-once.service`, which stays enabled
until setup succeeds. A separate rename service previously started alongside
it after reboot, racing package installs and sharing the failure file. Setup
now takes a nonblocking process lock before opening its log or clearing state.
The lock belongs to the `flock` supervisor and is closed in its child, so
background descendants cannot keep it held after setup exits. Duplicate calls
return without modifying the active run. A requested rename reboot exits setup
immediately rather than falling through to the completion checks.

`manet-radio-names.service` runs after coldplug udev processing, before
`sysinit.target` and `network-pre.target`. MAC-keyed `.link` rules can fail
with `File exists` when two radios must exchange occupied `wlan` names.
The helper reads only the generated `10-wlan*.link` pins, validates the whole
exchange, moves displaced radios to temporary names, then assigns their final
names. It retries the udev add events so device units become available to the
supplicants. It refuses to move an active or enslaved radio or displace an
unpinned interface. Missing hardware is reported without preventing other
radios from being named. No pin files on first boot means no work; setup
enables the service for subsequent boots. The packaged enable symlink also
covers tools updates, taking effect at the next boot.

`manet-os-cleanup.py` runs once during setup on Raspberry Pi OS Trixie after the
MANET kernel is running. The Pi first-boot package list installs runtime libraries
instead of development headers, and setup installs `gpsd-tools` instead of the
GUI `gpsd-clients` dependency tree. Cleanup explicitly removes unused appliance
packages and their orphaned dependencies, treating recommendations as optional
for this transaction. Required recommendations such as ALSA profiles,
rfkill, regulatory data and NSS modules are protected alongside core runtime
packages and libraries used by the prebuilt radio tools. Kernel images and radio
firmware are protected too.

A dry run pins those runtime roots in a temporary copy of APT's extended state;
it does not alter live auto/manual selections. Apply saves the package inventory,
manual selections and plan, pins the real runtime roots, validates the plan again,
then purges. Plans that remove protected packages or install/upgrade anything are
rejected. The stock swap file is deleted only after checking its signature, that
no swap is active, and that no loop device references it. A success marker prevents
later setup runs from undoing operator additions. Cleanup failure is recorded as a
provisioning failure, not silently marked complete.

On the two CM4 Trixie bench nodes, cleanup reduced root filesystem usage from
5.3/5.4 GiB to 1.8/1.9 GiB. Most savings came from the unused 2 GiB swap file,
about 1.1 GiB of packages, and the APT cache. Both rebooted with healthy mesh,
AP, DNS, dashboard and internet access; the second rejoined with Ethernet
unplugged. Native tool dependencies and a Lyra encode/RTP/decode pipeline were
checked. This verifies existing installations; the revised first-boot package
list still needs a fresh-image hardware test.

Profile 2 adds runtime cleanup through the same setup/manual entry point. A
profile 1 marker does not skip it; a successful profile 2 marker preserves later
operator additions unless `--force` is explicit. Preview reads the enabled system
and global user unit lists and active user managers without changing them. Apply
saves inventories, per-unit reasons and each attempted command before execution;
failures leave the previous marker. Global user masks are followed by reload/stop
in current user managers through their private systemd sockets, so stopping the
user bus does not require terminating logins. System D-Bus is never masked.

| Unit/package | Profile 2 verdict and consumer audit |
| --- | --- |
| Global user `pulseaudio.service` / `.socket`, `rtkit-daemon.service`; `pulseaudio`, `pulseaudio-utils`, `rtkit` | Stop/mask and purge packages. `mesh-voice.py` explicitly uses `alsasrc`/`alsasink` on the USB ALSA device. `button-monitor.sh` invokes `led-info.sh`, which only drives LEDs. No runtime `pactl`, `paplay` or Pulse sink/source consumer exists. `mpg123` appears only in package policy/templates; retain this useful ALSA-capable field command. |
| `dbus-user-session`, **user** `dbus.service` / `.socket` | Keep the package; stop/mask global user activation. The CM4 removal preview showed purging it also removes `gstreamer1.0-plugins-good` through libsoup/glib-networking/dconf, so it is protected by KEEP. No MANET service uses a session bus: Syncthing runs as `syncthing@radio.service` under the system manager. Keep system `dbus`/`dbus-daemon`, PAM/logind and user managers. Operators needing session-bus applications can unmask these two user units explicitly. |
| `cron.service` | Disable/stop/mask only if no user crontabs, custom table entries, or executable jobs without a recognized systemd early-exit guard exist. No repo/provisioning cron consumer exists. Retain `cron` tools; log custom consumers for review. |
| `man-db.timer`, `dpkg-db-backup.timer` | Disable/stop/mask optional indexing/backup schedules; retain `man`, `mandb` and dpkg. Their commands remain usable manually. |
| `e2scrub_all.timer`, `e2scrub_reap.service` | Disable/stop/mask on the raw-partition appliance. Preserve both if LVM tools or device-mapper LVM volumes exist. Keep `e2fsprogs` and boot fsck. [Debian's e2scrub manual](https://manpages.debian.org/trixie/e2fsprogs/e2scrub_all.8.en.html) describes the LVM requirement. |
| Global `wpa_supplicant.service` | Disable/stop/mask its unused D-Bus instance. Keep `wpasupplicant`, `wpa_supplicant@wlanX`, the s1g services and USB-uplink instance management used by radio setup/uplink tools. No wildcard masks. |
| `polkit.service` / `polkitd` | Keep. Our UI, `networkctl`/`resolvectl`/`hostnamectl` callers run as root, but the OS networkd daemon does not. DHCP uplinks retain default hostname handling; networkd requests transient hostnames and may request the product UUID from hostnamed. The upstream [DHCP path](https://raw.githubusercontent.com/systemd/systemd/v257/src/network/networkd-dhcp4.c) and [hostnamed authorization](https://raw.githubusercontent.com/systemd/systemd/v257/src/hostname/hostnamed.c) make blanket removal inappropriate. No MANET network configuration is changed to avoid this dependency. |
| User `gpg-agent*`, `dirmngr`, `keyboxd`, `ssh-agent` sockets | Keep unmasked: no recurring polling job, and masking would break normal user signing/decryption, key retrieval or SSH credentials. Protect installed GnuPG/OpenSSH client packages from autoremove. |
| `keyboard-setup.service`, `console-setup.service` | Disable/stop/mask boot console setup on this headless appliance; retain configuration/tools for manual recovery. No runtime/provisioning consumer requires the boot services. |
| `iperf3.service` / `iperf3` | Keep enabled/package protected. `manet_manage.py:run_local_iperf3` connects to peer servers on TCP 5201; both templates enable the daemon and `manet-ui-firewall.sh` restricts it to the mesh. |

Other explicitly enabled provisioning services support mesh, voice, identity,
GPS, LEDs, web UI, Syncthing, DNS, routing and recovery; they remain.
APT/security refresh, logrotate, fstrim, tmpfiles, journald and SSH are also
outside the removal policy. Unknown enabled units are inventoried for review,
never removed by an inverse allowlist. Profile 2 was applied on CM4 after the
`dbus-user-session` correction; netplan/libnm/python3-yaml left through autoremove
(provisioning already disables netplan). Fresh-flash verification remains pending.

### ATAK runtime and package defaults

`manet-atak.service` is core and enabled on every node. An absent or empty
`atak` setting enables it; only `n`, `no`, `0` or `false`, ignoring case and
surrounding whitespace, disables it. The daemon and its pre-start firewall
command share that decision. A disabled start removes stale private tables and
exits successfully; stop/reload-to-disabled also cleans up through `ExecStopPost`.
Use `atak=n` in `/etc/mesh.conf` followed by a service restart to opt out.

Both provisioning templates enable the unit, and `radio-setup.sh` enables and
starts it after the GPS setup. All generated mesh.conf variants document
`atak=y`. The tools, CM4, Rock 3A and RPi5 builders copy both ATAK and positioning
units, with only ATAK added to `multi-user.target.wants`. The tools updater
extracts that enable symlink and reloads systemd; newly enabled units start at
the next boot, or immediately with `systemctl start manet-atak.service`.
`manet-positioning.service` remains disabled and its configuration default off.

ATAK admission independently verifies two possible local EUD ports. `end0`
retains its WIRED_EUD/br0 role, wired carrier-generation token, carrier and
forwarding checks. The AP name comes from `/var/lib/ap_interface`, must match
`interface=` with `bridge=br0` in `/etc/hostapd/hostapd.conf`, and requires a live
hostapd process plus carrier and forwarding br0 membership. Hostapd liveness is
read from `/run/hostapd.pid` and `/proc/<pid>/{stat,cgroup}`: name, non-zombie
state, process start time and the `hostapd.service` cgroup identify this lifetime.
This adds no subprocess. Either path works alone, including an AP with `end0`
routing a LAN uplink. A resolved br0 neighbor must map to a MAC dynamically
learned on exactly one admitted port; any simultaneous bat0/other-port learning
rejects it. Each peer records Wired or Wi-Fi.

Default Mesh SA is received on one `AF_PACKET/SOCK_RAW` socket per ready EUD
port, never on br0 or bat0. The socket starts with protocol zero, attaches and
locks a classic BPF filter, then binds `ETH_P_ALL` on that port so it observes
ingress before the bridge RX handler consumes the frame. BPF admits
only unfragmented IPv4 UDP to `239.2.3.1:6969`. TCP traffic, including TAK-server
ACKs and data, never enters the userspace capture queue. Outgoing/loopback traffic is excluded
both by packet type and `PACKET_IGNORE_OUTGOING`. No IP or packet multicast
membership, promiscuity or all-multicast mode is requested. The existing bat0
`MulticastRouter=permanent` setting is unchanged. Native SA forwarding continues
except for the short-lived rewrite memberships below. This service adds no
multicast listener advertisement.

Packet socket lifecycle follows the existing network/role/hostapd invalidations.
Cheap port facts include role, forwarding/carrier, hostapd lifetime and ifindex;
unchanged facts preserve sockets, removed ports close them, and a new lifetime
opens a new filtered socket. These facts permit observation only. Before SA
decoding, strict firewall readback and a resolved br0 neighbor must agree with
the Ethernet source MAC learned exclusively on the actual ingress port. Packet
sockets bypass the ordinary input firewall, so this explicit gate is required.
The contact endpoint must contain the proven source IPv4; it cannot redirect
replies. Kernel filter/bind failure closes the socket and fails the service.

`manet_tak.py` accepts mesh protocol 0 XML and the v1 `bf 01 bf` envelope. Its
small protobuf wire reader retains only the SA fields from the public TAK
definitions and applies the same `PhoneSA` classifier as XML. It preserves
`how`, `geopointsrc`, `altsrc` and parent-link echo evidence. Whole XML detail
elements take precedence over typed equivalents; unknown detail is ignored,
while unsupported detail extensions fail closed. Contact phone numbers and
raw payloads never enter retained state or logs. Limits are 8 KiB per SA,
4 KiB for v1 `xmlDetail`, 256 protobuf fields across fixed message levels and
512 bytes per retained v1 text field. Malformed lengths, varints, versions,
duplicate known singular fields and XML DTD/depth violations are rejected.

Frame reads are capped at 8274 bytes, with one frame per ready EUD socket per
existing event cycle. IPv4/UDP lengths and checksums are checked, and fragments
are not reassembled. The existing shared byte/event/admission buckets also
cover SA, including forced readback for a new ingress/IP/MAC tuple. Remote
frames arriving through bat0 or forwarded out a local port never reach the SA
decoder. The unit already had `CAP_NET_RAW` for br0 binding; it now permits
`AF_PACKET` as well. No broader capability is added.

The owned firewall uses an explicit allow-list of `end0` and the configured AP
name, computed by `--print-firewall` and the pre-start installer. A deny-list of
known mesh ports could expose a new/unknown interface. Bridge input/output
filtering and inet input confinement cover 4242/4349; first fragments to either
port are dropped before bridge defragmentation. Strict readback checks every
owned chain/rule and permits only implicit protocol matches, counters/handles
and unordered anonymous-set membership to differ. Changing the AP interface
name requires `systemctl restart manet-atak.service` to install a new list;
until then the daemon fails closed on the policy mismatch. Ordinary AP stop/start
with the same name needs no firewall replacement.

Connect by Ethernet or this radio's Wi-Fi. For radios with GPS, the documented
setup is Android location OFF, ATAK external GPS input ON at UDP 4349, and Default
Mesh SA ON. This supplies one position inside ATAK for mesh, unicast and TLS
outputs. Radio hardware without GPS instead needs Android location ON, with the
controlling phone's GPS supplying the team position. Stock CoT input stays enabled.
Unicast XML SA to UDP 4242 remains supported. The multicast rewrite is a safety
net for forgotten settings, not a substitute for this setup.

SA admits a UID into the bounded phone table and refreshes its presence lease
(the earlier of packet staleness and the 75-second receipt-age budget). Each
phone retains IP, MAC, attachment type, callsign, clock/replay state and deadline.
`max_euds_per_node` follows mesh-config validation: ASCII integer 0–253; flashers
normally offer 1–20 for Wi-Fi and use zero for wired-only provisioning. Zero
therefore admits one wired-primary phone. Missing/invalid values log and use a
bounded default of eight. A full live table rejects a new UID; absent entries
can be evicted. Replay history per phone is limited to
`min(256, max(8, 2048 // capacity))` SA timestamps, bounding aggregate memory.
A live UID cannot silently move to another MAC. A replacement after absence
can be admitted without deleting state. The radio advertises the contact
`MANET <hostname>` (`atak_callsign` can override it), with its TCP 4242 endpoint
for Send. Select that contact when sending a manual point. The contact is
hidden from the map; the separate external-position feed carries location.
Every present phone gets a contact, radio-selected external position and GPS
warnings using its own SA-derived clock. Phone GPS fallback never enters the
external feed. Per-phone sends retain the 30-second contact refresh
and at-most-1-Hz position feed, subject to the existing GPS trust and echo gates.

Controller order is: saved manual choice if present; the only wired phone;
present current controller; otherwise a random present phone. Random selection
persists across unchanged ticks and same-boot restarts. A manual preference
persists across reboot/absence, regaining control on return. No present group
is left without a controller. Only that controller's markers, drags, USER
selections and read receipts can change location or acknowledge GPS alerts.
The held manual point and distrust belong to the radio. Cross-controller manual
ordering uses radio monotonic observation time, and retirement jobs retain the
original phone owner. A guest point receives one explanation per retained guest
and controller change. Membership growth or controller change queues one group
notice per present phone; unchanged membership/controller does not repeat it.
Per-recipient notice slots coalesce superseded changes, use stable message IDs,
and park after five failures. Evicting an absent guest also forgets its notice
history; this keeps the bound tied to provisioned capacity.

The unauthenticated `/` overview adds the ATAK phone list and controller buttons
only while ATAK is enabled. Its `/api/local` ATAK object exposes only `enabled`,
`running`, each phone's `uid`, `callsign`, `port`, `controller`, `tak_server` and
`gps_disagreement_m`, and selected lat/lon/HAE with a source label. The web reader
independently expires GPS position, server markers and disagreement notes using
monotonic deadlines and boot identity even if the daemon stops writing.
The full runtime snapshot, including session identity and input state,
stays server-side for the controller POST check. `manet-ui-firewall.sh` installs
port-80 input rules at priority -10: loopback and this radio's DHCP source-address range are allowed,
then all remaining port-80 traffic is dropped. The web application's ordinary
subnet check is broader. The controller POST additionally checks the local DHCP
pool itself, requires a bounded JSON body, and rejects foreign Origin/Referer;
it needs no management cookie. These source-address checks assume the trusted
team network and are not cryptographic or ingress-port proof for HTTP.
The web process atomically writes `/run/manet-atak/controller.json`; the existing
application cycle notices file metadata changes. It rechecks current phone/path
presence and a boot/session-bound, five-second BOOTTIME envelope, then checkpoints
the choice. HTTP 202 means queued, with the resulting controller shown by the
next overview refresh. There is no extra daemon or polling loop.

The local panel also exposes an opaque held-point identity and controller UID
for **Use phone GPS again**. `/api/atak/release` uses the same JSON, origin and
DHCP-pool checks without an admin password, then requires the source IP to match
the current controller. It stages `/run/manet-atak/release.json`. The daemon
rechecks current controller UID/IP/MAC/path, held-point identity, boot/session
and a five-second BOOTTIME envelope before releasing a point selected without
trusted own GPS. Stale requests cannot release a replacement point. Guests
cannot release. Source-IP checks retain the existing trusted-team HTTP boundary.

Persistence schema 2 owns the selector, phone table, manual controller preference,
notice state and delivery ownership under `/var/lib/manet-atak`. Schema 1's old
single-pin checkpoint is explicitly discarded, including its held manual point,
on first start of this code; unknown schemas or damaged checksums still refuse
startup. Reboot retains choice and radio manual-location age lower bounds but
requires new SA before any phone becomes present. The service never sets the
OS clock.

The controller's admitted SA with `geopointsrc=GPS`, no parent echo link and
usable geometry supplies an ephemeral fallback when the selector has neither
trusted radio GPS nor a held manual point. Per-phone clock admission, replay
rejection, SA stale time and receipt-age limits remain unchanged. `phone_gps`
holds only the fix, deadline and callsign, bounded by the admitted table and
discarded on restart. Status labels it `phone GPS (<callsign>)` and marks it as
outside radio position trust. It never enters `RadioGPS`, time policy, 4349
output or mesh rewriting. Controller changes select that phone's fresh cached
GPS without erasing a manual point. If proof of attachment disappears before the
report expires, the beacon can use its remaining freshness, at most 75 seconds
from receipt. Its stale field is capped at that deadline and its source is
`MANET:phone-gps`. There is no stale hold extension. When departure is noticed
only through SA expiry, no fresh phone position remains to advertise.

While no trusted own GPS fix is selected, a held manual point is anchored to the
controller's fresh own-GPS observation at acceptance. Without one, the first
fresh controller observation supplies the anchor. Until then the point holds.
`MANUAL_MOVE_M=30` permits a stationary correction within 30 m indefinitely.
Distinct fresh observations continuously outside that radius for
`MANUAL_MOVE_S=15` seconds release the point. A single jump followed only by timer
ticks cannot release it. Returning inside, stale GPS, lost controller proof or
trusted own GPS resets the interval. `MANUAL_MAX_OFFSET_M=200` releases immediately
when any fresh controller GPS is more than 200 m from the held point, including
the first anchor observation. These are operational allowances for phone GPS
noise, small corrections and obsolete-point rejection, not accuracy guarantees.
On GPS-less radios the phone track decides movement without a spoof monitor.
Trusted-own-GPS mark/override/confirmation rules remain unchanged.

Schema 2 persists the held-point identity, controller UID and optional lat/lon
anchor. Earlier schema-2 checkpoints get an unanchored held point. This preserves
the existing radio-owned manual point on upgrade. Controller changes reanchor
to that phone's first fresh GPS. Restart preserves the anchor but discards phone
GPS and movement timing, requiring new observations over the full interval.
Movement checks also run during controller GPS ingestion, clearing rewrite
authority before forwarding the report that triggers release. The status button
can release even before fresh phone GPS is available, leaving no selected fix.

Release reserves the same retirement slot as a replaced or confirming marker.
If the retirement queue is full, geometry stays held. Otherwise it checkpoints
the release and retirement owner before one journal line and one best-effort
controller GeoChat. It adds no warning, audio or retry job, and preserves all
trust state. Existing receiver warnings continue independently. Marker retirement
still uses the original owner's delivery path and existing bounded retries.
An already retired or non-marker manual gesture needs no additional marker job.
The 200 m limit uses journal reason `offset` and explains that the phone GPS is
far from the held point. Sustained movement uses `movement`, and the status-page
button uses `request`.

Acceptance records a transient notice intent keyed to the new manual gesture.
The next selection sends one best-effort controller GeoChat only if that gesture
is actually held. A point immediately released or retired by GPS confirmation
does not produce a misleading holding message. The text explains that local ATAK
deletion does not release the radio, and says to open manet.local in a browser
and tap **Use phone GPS again**, or move on with fresh phone GPS. It has no link
markup or audio. USER/drag gestures get the same explanation. Consuming the
intent before the send prevents retries. It is not restored on restart or
created by reanchoring, so a crash before handoff can lose this one-time notice.

For a controller Send, `held_marker` retains one accepted XML packet, its manual
gesture identity and its next refresh deadline in schema 2. Hex chunks fit the
existing checkpoint string limit and bound the raw packet to `MAX_XML` (65536
bytes). Restore validates the XML and checks UID, creator and geometry against
the held gesture. The exact original bytes preserve labels, icon/color, creator
time, stale time and unknown detail. USER/drag selections have no separate
marker UID or payload. Older checkpoints retain their geometry but cannot
reconstruct an original Send, so refresh starts only after a new accepted Send.

`HELD_MARKER_REFRESH_S=60` sets the first deadline one minute after acceptance.
While the point is selected and a controller is proven present, the existing
cycle attempts unicast replay on 4242 to that controller. No refresh chat is
sent. The next deadline is checkpointed before output, including failed sends,
to avoid fast retries or duplicate bursts after a same-boot restart. Reboot
starts a new five-minute interval and still requires fresh phone admission.
An overdue replay waits for current controller proof. Controller changes do
not create an acceptance notice or change marker content.

Release clears the retained XML before its durable checkpoint. Replacement
replaces that single record, and a newer accepted edit of the same UID updates
its bytes and restarts the interval. Before replay, the service checks gesture
identity, the selector's retired flag, pending selector retirements and the
delivery retirement queue. It runs after retirement handoff, so retirement wins
even at a refresh deadline or under queue backpressure. Once suppressed, the
old record cannot reappear when the retirement replay hold expires. Deletion
delivery retains its original-owner semantics. Exact replay deliberately does
not change event timestamps, so Pixel behavior after the original stale time
still needs checking along with the new refresh cadence.

Receive-driven release from ATAK marker deletion is not implemented. The public
[RemoveBroadcastReceiver](https://github.com/deptofdefense/AndroidTacticalAssaultKit-CIV/blob/main/atak/ATAK/app/src/main/java/com/atakmap/android/user/RemoveBroadcastReceiver.java)
removes the map item or invokes its custom delete action. The public
[CotDeleteImporter](https://github.com/deptofdefense/AndroidTacticalAssaultKit-CIV/blob/main/atak/ATAK/app/src/main/java/com/atakmap/android/cotdelete/CotDeleteImporter.java)
proves incoming `t-x-d-d` support, which does not prove sending to a marker's prior
recipients. `CotMapComponent` also registers a `TaskCotReceiver` removal listener,
whose implementation could not be retrieved during this check. The examined
sources therefore do not establish outbound propagation for the ordinary Send
workflow. The subsequent Pixel test described below observed none. Deleting a
local marker is not a radio release action.

On 2026-10-14, a CM4 with an AP-connected Pixel and the real gps-reader reporting
no receiver released the 4.7-hour-old held point on the first phone GPS fix
about 14.5 km away, and the release GeoChat arrived. A new point sent to the
radio contact stayed selected as manual. **Use phone GPS again** then logged
`reason=request`, selected `phone GPS (SRS)` and retired the marker from ATAK's
map. These observations cover the offset rule, button and original-owner
retirement. They do not yet cover the new acceptance notice or marker replay.

In the same setup, a 15-minute capture of phone-to-radio TCP/UDP 4242 recorded
the `a-n-G` Send and no subsequent traffic when its marker was deleted locally.
The radio kept holding it. ATAK GeoChat also displayed both http://manet.local/
and manet.local as non-tappable text, so chat instructions explicitly say to
open the address in a browser. The 30 m / 15 s walk-away release could not be
tested with the desk radio, AP range and rain. Its unit coverage remains, and
hardware verification needs a carried radio outdoors.

While the radio selector supplies trusted own GPS or a held manual point,
`rewrite_phones` contains exact EUD interface/MAC/IPv4 concatenations for admitted
live phones without a detected TAK-server connection. A bridge forward rule drops
only their unfragmented IPv4 UDP `239.2.3.1:6969` copies going to bat0, within the
packet capture's size bound. Its source-MAC exclusion against `tak_phones` is
checked by the kernel for every frame, independently of userspace progress.
It leaves local EUD copies, unadmitted sources, remote traffic, TAK-server
outputs and unicast unchanged. Admission's first SA passes natively. Every set
element has a three-second kernel timeout, refreshed at most once per second.
An atomic netlink batch flushes and repopulates the set on the existing cycle.
No refresh occurs without admitted phones and a selected radio position.
Position/admission loss flushes membership. Process death expires it without
userspace action, and ExecStopPost deletes both private tables.

The set writer uses the existing subscribed NETLINK_NETFILTER socket and no
NLM_F_ECHO flag. Own multicast notifications can still arrive. The reader checks
the kernel sender, multicast group and originating socket port before excluding
its own rewrite notifications from proof invalidation. Generation notifications
must match one of the last 32 rewrite-message sequences, which exclude membership
dump requests. On Linux 6.18, NEWGEN uses the first operation's sequence after
the batch-begin header is removed. Element notifications use sequence zero and
must identify the bridge `manet_atak` table's `rewrite_phones` set. Nonzero element
sequences must match the corresponding recorded operation. Unknown or external
notifications still invalidate proof. This bounded history is separate from ACK
tracking so notifications arriving after ACKs remain recognizable. Requested
sequence-numbered ACKs are drained by the existing reader.
Errors, lost notifications and an unacknowledged previous transaction stop the
service. Static policy readback also verifies the set's key types, timeout and
capacity. No child process or independent polling loop handles renewal.

For blocked senders, an accepted SA can wait for a demand-driven membership
read in one bounded frame slot per EUD port. That port leaves the select reader
set until completion, so sparse reports can use the fresh result without an
unbounded userspace queue or repeated admission. The original SA freshness
deadline is retained too. Expiry, changed proof or interface identity, lost
rewrite authority and TAK membership produce original forwarding.
Teardown attempts to relay retained originals before closing the sender.
Accepted SA replaces lat, lon, HAE, CE and LE plus precisionlocation
geopointsrc/altsrc with `MANET:<hostname or configured callsign>`. The protobuf
editor preserves each unrelated field's encoded bytes, including unknown fields,
and rebuilds only enclosing lengths and precision messages. XML editing preserves
other bytes, including quoting, comments and unknown detail. An output-only
AF_PACKET socket bound to bat0 sends a complete Ethernet frame with the original
source MAC/IP, destination and UDP ports, recomputing IP/UDP lengths and checksums.
It never transmits on a local EUD interface. No IP-source behavior assumption
inside ATAK is needed. Decode ambiguity, rejection or unsupported data relays the
original unchanged. Rewrite send failure retries the original. Failure of both
stops renewal by terminating the service. Application budget exhaustion or a
nonzero packet-socket drop counter relays the current original and suspends
membership for three seconds. Capture loss can lose an intercepted SA but cannot
lose TAK-server detection, which now runs entirely in the kernel. The drop
counter is read only on SA arrival. Size/fragment exclusions pass natively.
Live CM4 verification still needs the kernel TAK exemption and crash expiry.
Phone-location-on XML rewriting has been observed as described below.

TAK-server detection is a bridge prerouting rule on the explicit local EUD port
allow-list. Separate IPv4/IPv6 matches select TCP destination ports 8087 and 8089
and update the source Ethernet MAC in `tak_phones`. The set has dynamic/timeout
flags, a 600-second default timeout and capacity 1024. Every visible qualifying
packet refreshes expiration in the kernel, including established data and ACKs,
without a packet-socket wakeup or per-packet netlink notification. Its forwarding
exemption applies immediately even while the daemon is paused or dead. A server
on the EUD side also qualifies if its traffic crosses this bridge. Encrypted
tunnels, packets without an inspectable TCP destination, different ports and
traffic switched elsewhere are outside detection. Unit restart recreates the
set, while SIGSTOP alone leaves the kernel hold and its renewal intact.

The existing NETLINK_NETFILTER socket reads `tak_phones` with
`NFT_MSG_GETSETELEM | NLM_F_REQUEST | NLM_F_DUMP` when an admitted frame is about
to be rewritten or meaningful status changes. It reuses a completed snapshot
for five seconds and caps requests at one per five seconds. Idle timer ticks
issue none, including while a phone remains present. Status identity, position,
trust, controller, warnings and server/disagreement changes create demand,
while age counters and receipt timestamps do not. Deferred demand waits for
rewrite ACKs, without losing the event. Multipart replies require the matching
unicast sequence, bridge family, table and set names. Parsing caps the result at
1024 unique six-byte MACs and 128 KiB, validates remaining lifetimes, and commits
only after successful NLMSG_DONE. A partial result is never an authoritative
empty set. Error, interruption, truncation, malformed data or a pending read
reaching two seconds stops the daemon and lets rewrite leases expire. External
policy notifications still invalidate proof, including a multicast notification
whose sequence happens to match the current read.

A recent completed membership snapshot is shared by status and rewrite selection.
Its age is measured from the request, with a five-second maximum. Unknown or stale
snapshots cannot add rewrite members or authorize replacement. Existing leases
may renew against current proof, presence and radio fix because the kernel TAK
exemption remains authoritative for native forwarding. Remaining element lifetimes produce receipt-based
UI deadlines, conservatively extending an exclusion by dump delivery time.
Membership may have been learned before SA admission, since kernel insertion is
independent of the userspace phone table. Fresh proof is still required to admit
or rewrite any phone. Reads wait for previous rewrite ACKs, and rewrite set writes
wait for a pending dump, avoiding self-induced generation interruption. Static
firewall verification checks both sets, update rules and the source-MAC exemption,
including key types, flags, timeouts and capacities.

The packet tap runs before bridge forwarding, and a netlink snapshot is not atomic
with either the tap or `tak_phones` updates. A frame captured just before its MAC
enters the set may already have lost its native copy to the block rule. Once a
snapshot contains the MAC, userspace supplies the captured original. If the native
copy instead passed after insertion, this produces an identical duplicate. With
an earlier snapshot, a replacement may still be sent during the read interval,
even though the kernel already passes the native original. The next SA needing
replacement or a status change refreshes an expired five-second cache. No atomic all-output
handoff or absolute no-replacement guarantee is claimed for that transition.
After detection, the retirement cache relays originals until both the old
three-second lease window ends and the ingress queue drains. More than 1012
retiring keys stops the service. Correct phone settings select one position inside
ATAK for mesh, unicast and TLS regardless of these safety-net races.

CM4 verification on 2026-10-13 used the preceding combined rewrite and header-tap
build, an AP-only Pixel admitted through Default Mesh SA, and a trusted bench
fix. JSON showed the phone in `rewrite_phones` and DELSETELEM/NEWSETELEM ACKs
returned error 0 each second. Inspect with
`sudo nft -j list set bridge manet_atak rewrite_phones` and
`sudo nft -j list set bridge manet_atak tak_phones`. Plain-text `nft list set`
omitted populated ifname/ether_addr/ipv4_addr elements on that CM4. A bat0 capture
contained `geopointsrc=MANET:gnss` with phone location off and the external feed
already selected. That earlier capture alone did not prove replacement of phone
GPS. A subsequent run with Android location on and trusted bench GPS captured
XML SA about 14.5 km away on AP ingress and every report rewritten to the bench
fix on bat0 with `geopointsrc=MANET:mesh-5cc2`. Control-only frames passed.
Disagreement produced one journal line at 14535 m, one delivered quiet GeoChat
and a panel note, without audio. TAK v1 rewriting remains hardware-unobserved
because this phone sent XML. With the bench writer stopped and the real reader
reporting no devices, warnings stayed quiet and disagreement cleared, but a
4.7-hour-old held point still displaced live phone GPS. The release policy above
addresses that observed failure. The subsequent offset and button checks above
verified those release paths, while the walking threshold remains unverified
on hardware.

A CM4 renewal A/B on that same pre-kernel-detection build measured 0.790 and
0.803 CPU seconds per minute with one-second renewal and three-second expiry,
versus 0.762 and 0.806 with five-second renewal and ten-second expiry. The pairs
overlap and show no measurable improvement in that run. Keep the one-second
renewal and three-second timeout for faster fail-open behavior. After kernel
detection and corrected self-notification classification, a later one-phone run
used four child executions per minute and 1.20 CPU seconds per minute with 1 Hz
membership dumps. The on-demand reader targets the earlier roughly 0.80 level,
but no CM4 CPU measurement of this change is available yet. Local event-cycle
tests verify no timer reads, cache reuse, sparse-report rewriting and unchanged
self-notification handling, rather than predicting hardware CPU cost.

The fresh per-phone GPS cache also feeds an evidence-only comparison against the
selector's trusted own GNSS fix. Manual and fallback positions never qualify.
The fixed 50 m alert threshold is the sum of two assumed 10 m GPS horizontal-error
allowances and 30 m of local AP separation, not a claimed confidence interval or
maximum AP range. Above it, status records rounded separation and a deadline
capped by both fixes. One tagged journal line and one best-effort GeoChat handoff
identify the episode. There is no retry, warning/audio job or trust mutation.
Distance within 35 m for 30 continuous seconds of fresh, present, trusted comparison
rearms the episode. Staleness, lost proof or 35–50 m separation interrupts that
timer. Agreement cannot clear distrust, including a spoofer moving both fixes.
Episode memory is bounded by admitted phone slots and resets on restart. With
Android location off, no own-GPS SA means there is no comparison.

With no live local phone lease, `RadioBeacon` advertises the selector's usable
fix as protocol-0 XML on `239.2.3.1:6969`. Its stable UID is the machine-id-derived
radio UID plus `-beacon`, distinct from the hidden contact and reserved against
phone/marker admission. The callsign is the hostname or `atak_callsign`.
Type `a-f-G-U-U-S-R` is the MIL-STD-2525 radio/signal unit, SIDC
`SFGPUUSR-------`. It omits team/role details so ATAK uses type symbology, and
omits a contact endpoint. Only geometry, source and uncertainty are published,
without a held manual point's phone identity. The same selector supplies trusted
GPS or the held manual fallback, never suspect receiver geometry or a 0,0 SA.
The fresh phone GPS exception above may also supply geometry after path loss.
Announcements use the radio's UTC clock, refresh at 30 seconds and expire at
90 seconds. Movement of five metres horizontally or in known HAE, source or
name changes can advance output, with a five-second minimum between attempts.
Failed sends use that same backoff. Work runs on the existing application cycle.

Beacon transport uses a separate nonblocking UDP socket bound to br0 and its
primary/configured IPv4, with multicast interface selection, TTL 1, loopback
disabled and fragmentation prohibited. Its datagram budget is 1200 bytes.
The bridge output chain drops local UDP to `239.2.3.1:6969` on every egress
except bat0. Linux applies that hook to each actual bridge output port, so
local EUD ports cannot receive this beacon. Phone SA follows the separate
conditional rewrite path above. Other radios' announcements are unchanged.
Existing bat0 multicast-router
configuration remains in place, without a group join. Strict firewall readback
and forwarding/carrier/br0 membership for bat0 precede output. A radio with no
ready local EUD port can establish this mesh proof without admitting any phone.
br0/bat0 ifindices identify the sender's lifetime. Invalidated proof is refreshed
only for due output or admission. Packet-type filtering and proof's exclusion
of local source addresses reject a looped beacon before SA parsing, and its
radio type cannot classify as phone self SA.

On phone arrival or loss of usable geometry, the beacon stops and schedules
`t-x-d-d` with `__forcedelete` for only its beacon UID. A live phone lease keeps
the radio-GPS/manual beacon suppressed across temporary proof loss. Phone-GPS
fallback instead tests current proven phone presence and its report deadline.
Cleanup seeks three successful local
UDP handoffs at two-second spacing, bounded to five attempts, and then stops.
A restart with a live phone also requests cleanup of a possible old beacon.
After the final lease expires, new SA replaces any remaining cleanup job.
Neither announcements nor deletions have a delivery acknowledgement. Public
ATAK `CotDeleteImporter` removes a force-deleted item immediately unless that
item disables remote deletion, whereas ordinary staleness follows client purge
settings. The 90-second stale field is a fallback, not a removal guarantee.

The public [TAK protocol contract](https://raw.githubusercontent.com/TAK-Product-Center/atak-civ/main/takproto/README.txt)
requires legacy XML receive support. The [ATAK icon adapter](https://raw.githubusercontent.com/TAK-Product-Center/atak-civ/main/atak/ATAK/app/src/main/java/com/atakmap/android/icons/Icon2525cIconAdapter.java)
uses military symbology without team metadata, and the [milsymbol ground mapping](https://raw.githubusercontent.com/spatialillusions/milsymbol/master/src/lettersidc/sidc/ground.js)
identifies UUSR as radio. The [ATAK deletion importer](https://raw.githubusercontent.com/TAK-Product-Center/atak-civ/main/atak/ATAK/app/src/main/java/com/atakmap/android/cotdelete/CotDeleteImporter.java)
supports the chosen force-delete task. Tests cover selection, identity, cadence,
handoff, ingress rejection and egress policy. Stock Pixel rendering, multicast
deletion and live bat0-only output still require CM4 verification. There is no
separate feature switch, and `atak=n` suppresses all beacon traffic.

The ATAK daemon now subscribes to route link/address/neighbor (including bridge
FDB) and nftables notifications before its first readback. A cached proof avoids
all four `nft`/`ip`/`bridge` subprocesses on unchanged ticks. Role/generation file
metadata, AP role/config metadata and hostapd process identity are additional
invalidators. With no live admitted SA lease, relevant events only discard proof
and defer readback to the next admission or due beacon. A verified snapshot
supplies a notification scope: br0, bat0 and configured local EUD link identities,
br0 address changes, IPv4-to-MAC associations and MAC-to-local-port associations.
Unrelated link and neighbor/FDB churn is ignored even with a phone present.
A protected IPv4 neighbor's `RTM_NEWNEIGH` refresh on br0 also preserves proof
when its MAC matches and its state is REACHABLE, STALE, DELAY or PROBE. Failed,
incomplete, NOARP, permanent or unrecognized states, deletion, a changed MAC or
a different device still invalidate it. A healthy IPv6 refresh carrying the
same protected MAC on br0 is likewise harmless to the IPv4 proof, while failure,
deletion and another device remain significant. A dynamic AF_BRIDGE FDB refresh
is ignored only on its proven local port, with matching br0 master identity,
REACHABLE or STALE state and exactly the non-static flags recorded at readback.
Static/permanent/self entries, deletion, changed flags, or learning that MAC on
any other port, including bat0, trigger readback. VLAN, extended or unfamiliar
metadata and ambiguous snapshot rows remain conservative. Known flag bits are
decoded separately from iproute2's bridge-master field. Identity-set membership
alone cannot hide one protected IP changing to another protected MAC, and a
later healthy event in a batch cannot cancel a deletion. New local interface
names catch recreation with a new ifindex.
Missing scope or unknown/malformed events remain conservative. Each new TCP
stream, UDP peer or mesh ingress/IP/MAC tuple forces fresh admission readback.
Relevant events and transient-readback retries continue while an SA lease is
live. Every nftables event remains significant, and output checks pending
invalidations before sending. Relevant events during a snapshot discard it.
Overflow, truncation or subscription failure closes the service and lets systemd restart
with fresh subscriptions. SIGHUP also invalidates it. The existing output and
input cycle intervals are unchanged, with no extra daemon or polling subprocess.
The kernel firewall remains the packet boundary. Asynchronous userspace
notifications cannot make an arbitrary external ruleset flush atomic with I/O.

Without current proof, `SIOCGIFADDR` cheaply detects br0 address readiness (or
uses the configured bind address). TCP/UDP 4242 listeners bind IPv4 wildcard
with `SO_BINDTODEVICE=br0`; accepted TCP destinations and UDP `IP_PKTINFO`
destinations must match the full readback's global br0 address list before
application dispatch. Thus both primary and secondary gateway addresses work,
while the contact and output sender retain the primary/configured address.
This permits first arrival after boot or address changes without a timer-driven
snapshot. Idle invalidation closes streams and the unicast sender and forgets UDP/mesh
peer admissions, leaving passive listeners. `path_ready` is false until readback succeeds;
`path_reason` says verification awaits phone traffic after invalidation, or gives
the last verification failure. A valid, unchanged cached proof can remain ready
after the last lease expires.

Before the first association, the application reads receiver inputs and selects
a position for the unattended beacon. Missing GPS alone creates no phone warning
or audio job at this stage. It also checks monitor/jamming file metadata and
freshness deadlines, durably remembering producer activation/loss so a failed
producer cannot later appear unwired. Inputs are parsed only when metadata
changes or their boot/raw-clock freshness expires. Future timestamps have their
own retry deadline; neither wall-clock steps nor the cache extend a fix's TTL.

The event loop refreshes its clocks after admission readback before evaluating
producer files. Otherwise a GPS observation published during readback can look
future-dated against the earlier cycle time. Receiver sampling runs whenever
ATAK is enabled, so temporary proof loss does not turn it off. A missing
usable receiver fix delays the unavailable warning for three continuous seconds.
This delays only the warning, never permits a stale fix, and resets on recovery.
A held usable manual point needs no unavailable warning. Distrust, jamming and
producer-loss distrust remain immediate. Each warning raise/clear emits one
journal line with its code, GPS reason, state, freshness, age and distrust flag.
The diagnostic omits phone identifiers and packet contents. A fresh reader
document explicitly reporting no receiver suppresses and clears `gps_untrusted`,
its chat jobs and its audio job even after an earlier distrust incident. Missing,
stale or malformed reader files remain unknown. A hot-plugged receiver resumes
normal handling. Spoken reminder deadlines use `WARNING_READ_TIMEOUT_S` (60 s)
with `MIN_SPOKEN_REMINDER_S` (30 s) as a floor even for a shorter configured value.
Internal phone GPS no longer raises the obsolete phone-settings warning.

After the last present phone leaves, changed safety inputs, phone presence expiry and
warning and beacon deadlines still run. A held manual point or warning history retains the
30-second durable checkpoint to preserve age lower bounds across reboot. Audio
acknowledgements are checked only with pending audio; audio output is rewritten
only on changes. Unchanged idle cycles without due work skip selection, serialization and runtime
JSON writes. `status.json` includes `idle`; its timestamps describe the last
snapshot, not a service heartbeat. Use systemd for liveness and the observation
timestamps to advance a displayed manual age. Admission/path changes are handled
on the existing event cycle, and active output stays at its previous cadence.

Remaining idle work is the existing 0.2-second select wakeup, one-second
role/generation, AP/hostapd identity, producer-file and input metadata checks,
stream expiry and netlink handling, plus the cheap br0 address ioctl while proof
is unavailable. Readable events can advance these checks within the existing
five-cycle-per-second bound. Unrelated bat0 neighbor/FDB notifications are
discarded once a scope exists. Relevant idle events invalidate proof without
running `nft`, `ip` or `bridge` immediately. A due beacon verifies dirty proof
before sending, with retries bounded to five-second intervals. An unchanged
proof requires no snapshot commands even across beacon refreshes. There is no
separate idle readback timer. New receiver observations can now trigger position
selection and status writes even before the first phone, while unchanged
never-associated state needs no periodic durable checkpoint.
Packet sockets add no timer or nonmatching-packet userspace wakeup. Their BPF
still executes in the kernel on local ingress. This is not a claim of zero
kernel cost. Active phones retain their feed and safety deadlines, but unrelated
notifications now require no snapshots. Relevant phone/path/policy changes and
new admissions still perform readback.

CM4 with a Pixel 6a running ATAK-CIV 5.6.0.12 admitted an AP-only phone using
only Default Mesh SA, sent the contact and roughly 1 Hz external GPS feed, and
gave ATAK a location lock from a continuous bench fix with `time_ok=false`.
There was one filtered wlan1 packet socket, no br0/bat0 socket, no multicast
membership and no service restart. Before notification filtering, that run used
23.2 CPU seconds over 541 seconds, about 2.6 CPU seconds per minute or 4.3% of
one core. With the warning correction, unrelated-event filtering and beacon
output rule installed, a later AP-only run measured 1.374 CPU seconds per minute
(2.29% of one core) over 120 seconds. The phone remained the Wi-Fi controller
with trusted position, no banners and no warning raises over about five minutes.
Only one verification-race message appeared in that interval. However, a
60-second trace still showed 28 successful child execs, or seven full snapshots,
correlated with the phone's own REACHABLE/STALE/PROBE neighbor cycle. Tests now
preserve proof without commands through 300 protected-neighbor and same-port
FDB refresh cycles while retaining MAC-move, failed-state, delete and firewall
revocation. This additional refresh filtering still needs a CM4 CPU repeat.

`manet-cpu-sample.py` reads cgroup-v2 `cpu.stat` twice around one timed sleep,
including child-process CPU, and reports CPU seconds per elapsed minute plus
percent of one core. It also reports aggregate busy system CPU for profile
comparisons, without double-counting guest time. Restart/counter resets invalidate
the sample. Compare before/after samples under matching phone traffic and
uptime, for example `sudo python3 /usr/local/bin/manet-cpu-sample.py --label
no-phone --seconds 60` with the phone detached, then again with it attached.
Before the multiple-phone/AP change, with a wired phone attached, the ATAK
service measured about 1.0% of one core
(0.6 CPU s/min) on CM4, down from 7.2% before. The AP-only implementation before
deferred readback measured 0.581 CPU s/min with no phone: bat0 neighbor churn
triggered four-command snapshots about every 15 seconds. Offline tests now
prove zero snapshot commands over 300 idle neighbor-event cycles, including
the main loop, and fresh four-command proof on subsequent TCP/UDP arrival.
With deferred readback, the same AP-only CM4 measured 0.254 CPU s/min (0.42% of
one core) with no phone and started no snapshot commands over 60 seconds of
that churn before the unattended beacon was added. The application
also does no further JSON parsing/writing, durable saves or CoT sends over 300
unchanged never-associated cycles with no usable position. A relay with a fresh
receiver has additional selection and beacon work whose CM4 CPU cost remains
unmeasured. Tests cover input expiry, faults before first
association, reconnect, pending audio and warning deadlines.

Operator scripts run through `manet-user-scripts.sh`. Per-script completion
markers allow an interrupted run to resume without repeating scripts that finished.

Failures are advisory. Nothing here writes to `/var/lib/manet-provision.*`, so
a failing operator script cannot cause a working node to report itself
unprovisioned, and the runner always exits 0 for the same reason.
`manet-provision-status.sh` reports the tally and names any failures on the
login banner. A script that ran and failed is not retried; only an interrupted
one is.

The shebang requirement applies on the node as well as at flash time. The
flasher never embeds a file without one, so the test only affects scripts
copied onto a live node by hand, but it is required there, because executing a
file with no shebang does not fail. The kernel refuses it and the shell falls
back to interpreting it, so a configuration file whose lines happen to parse as
shell would run and report success.

Flash-time validation is performed by the flashers rather than here; see
[additional-scripts/README.md](../MANET/provisioning/additional-scripts/README.md).
The checks in this script cover the files that reach the directory without
passing through a flasher.

---

## Tests

Tests sit alongside the code they cover and run without hardware or a node:

| File | Covers |
|------|--------|
| `test_halow_plan.py` | The region HaLow plan in `manet_radio.py`: EU capping at 2 MHz and US reaching 8, channel numbers and center frequencies unique within a region and resolving each other, every region/bandwidth pair carrying an operating class, an unknown region falling back to EU, and a channel or bandwidth the region does not have being refused |
| `test_mesh_config.py` | The local/mesh key split in `mesh_config.py`: EUD and AP keys never reaching Alfred, mesh keys still going, only values that differ from `mesh.conf` counting as changes, and `max_euds_per_node` never being written |
| `test_peer_radios.py` | The peer radio chips in `manet_peer_radios.py`: frequency-to-channel conversion, published `INTERFACES_JSON` winning over the registry fallback, the fallback filling in when it is empty, and the channel fields surviving an encode/decode round trip |
| `test_mesh_registry.py` | Real encoder/decoder/registry integration, chunk zero, saved chunks, read failures, and timely publication |
| `test_mesh_ip_startup.py` | Bounded discovery, late/missing peers, failed queries, and allocation barriers |
| `test_mesh_boot_pipeline.py` | Real manager loops, encoded Alfred records and discovery helper with a simulated clock: first-pass discovery, full observation window, failed publication, Syncthing ID caching/retry, and claim publication without the steady-state sleep |
| `test_gateway_startup.py` | Real route manager with simulated networking: late addresses, missing registry, reachability/install retries, bounded fast polling, preservation of local uplinks, stale-route removal and correct primary source |
| `test_mesh_config_rollback.py` | Real rollback script with simulated BATMAN: unique peer counts, solo nodes, bounded recovery, failed queries/backups, interrupted restoration, and the receiver's apply gate |
| `test_mesh_peer_count.py` | JSON counts and real shell callers with simulated BATMAN: empty/single/multiple peers, alternate routes and MAC case, failed queries, bootstrap reset/recovery, quorum return-to-lobby gating, and partition size/beacon/migration failure handling |
| `test_led.py` | Real boot/button displays with fake GPIO holders and BATMAN: direct nodes across radio aliases, multihop exclusion, connected before registry arrival, unknown versus zero, recovery, opt-in hardware, GPIO errors, exclusive ownership, signal cleanup and onboard provisioning verdicts |
| `test_mesh_time_sync.py` | Selected BATMAN routes and canonical identities, safe registry/address parsing, late/missing peers, bounded attempts and six-hour refreshes across restarts/clock steps, correction and source freshness, GPS/uplink transitions, quiet intervals and holdover on source loss, disarming startup steps, unchanged advertisement path and provisioning unit parity |
| `test_acs_agreement.py` | Majority/timeouts, message loss, replay/restart safety, real scoring/activation, failed persistence and radio landing, authenticated straggler recovery |
| `test_acs_bootstrap.py` | Clockless recovery with real encryption, boot/recipient/nonce binding, expiry after restart and slow reads, consume-before-move failure, radio compatibility, traffic bounds, live tourguide response and no unsynchronized timed work |
| `test_acs_connected.py` | Day-old plan recovery over HaLow, actual-channel and freshness checks, one certificate publisher and failover, bounded retries, shared RF scoring after real partitions reconnect, static ACKs, and clockless replies without tourguide hops |
| `test_acs_rendezvous.py` | Absolute rotation, cold anchor waiting, HaLow precedence, unavailable slots and peer queries, restart/retry behavior, authenticated data-channel overlap, selective helper adoption and real tourguide visits without redundant retunes |
| `test_acs_halow_tourguide.py` | Live S1G readiness without peers, failure/recovery and pre-departure cancellation, bounded health probes, readiness in existing encrypted status, fresh peer exclusions and mixed-group guide selection |
| `test_acs.py` | Single/dual-band roles, lobby transitions and channel application, real channel elections, provisioning role selection, canonical tourguide identities and eligibility, shared channel locking, pre-hop comparisons, and real single-band helper encoding/return hops |
| `test_manet_admin.py` | Real encryption, wrong keys, tampering, authenticated ACKs, expiry, replay after restart/rollback/interrupted apply, password rotation, and literal config writes |
| `test_node_update.py` | Real archive staging and installation in a temporary root: checksum/version rejection, unsafe paths and links, download/disk/copy/dependency/service failures, locking, daily throttle and interrupted-install retries |

Run them from the git root, so this directory is on `sys.path`, and from the dev
venv, so the protobuf runtime matches the fleet:

```bash
source ~/.venvs/manet/bin/activate    # bash ../packaging/setup-dev-env.sh
python -m pip install cryptography==43.0.0
python -m unittest discover -s MANET/node_tools -p 'test_*.py'
```

The venv is not optional for the full run. One case,
`test_peer_radios.test_channel_fields_survive_encode_decode`, shells out to
`encoder.py`, which imports `NodeInfo_pb2.py`, which needs
`google.protobuf.internal.builder` from runtime 3.20 or later. A system Python
with an older protobuf fails that test; a system Python with a *newer* one is
worse, because 5.x and later accept generated code every node refuses to import,
so the test passes while proving nothing.

---

## Dispatcher hooks

The teardown reloads networkd and reconfigures `end0` only. A full
`systemctl restart systemd-networkd` would reconfigure `wlan0` and `wlan2` on
the way past and kick them out of `bat0`, so it is deliberately avoided.

## What actually runs on a node

**Only the contents of `/etc/networkd-dispatcher/<state>.d/` are executed.**
A file named for the state, sitting directly in `/etc/networkd-dispatcher/`, is
not run by anything. Every builder stages this set through
`stage_dispatcher_hooks` in `MANET/packaging/lib-dispatcher.sh`, for the tools
tarball as well as the three install tarballs, so a corrected hook can be
delivered over the air instead of needing a reflash.

| Repo path | Installed as | Runs |
|---|---|---|
| `carrier.d/50-ethernet-detect` | `/etc/networkd-dispatcher/carrier.d/50-ethernet-detect` | **yes**, on carrier |
| `routable.d/50-manet-uplink` | `/etc/networkd-dispatcher/routable.d/50-manet-uplink` | **yes**, on routable |
| `off` | `…/off.d/50-gateway-disable`, and the same file again in `no-carrier.d/` and `degraded.d/` | **yes**, on all three states |
| `carrier` | `/root/networkd-dispatcher/carrier` | no (reference copy) |
| `off` | `/root/networkd-dispatcher/off` | no (reference copy) |
| `no-carrier`, `degraded`, `routable` | nothing installs them | **no** |

## Two scripts called `carrier`

`carrier.d/50-ethernet-detect` is the canonical hook. The reference `carrier`
delegates to it, and setup no longer overwrites it. The hook retains
`ethernet-autodetect.sh --hotplug` for end0; other interfaces use
`manet-uplink-dispatch.sh carrier`. The packaged Ethernet boot unit and setup
use the same hotplug command. `off` is the same file in both places.

The routable hook queues `manet-auto-update.service` with `--no-block` after
uplink reconciliation. `gateway-route-manager.sh` also queues it after a
successfully acquiring a mesh default, even with an unqualified DHCP default
present, only with `auto_update` enabled. Refreshes and gateway switches do not queue
another check. The wrapper accepts the selected non-mesh uplink with its IPv4
default route, or a `default via <IPv4> dev br0` route. The latter does not
require a saved upstream interface: the route manager is the sole writer of
`br0` defaults in the node tools, and the uplink dispatcher excludes bridges,
BATMAN devices and loopback. Link-only bridge defaults do not qualify.

The wrapper retains the opt-in and `/run/initial_time_synced` gates. An absent
clock marker logs one deferred-check line and returns success. Mesh checks
then sleep a random 0–900 seconds to spread downloads through a shared gateway;
direct-uplink checks do not sleep. `MANET_AUTO_UPDATE_DELAY` overrides the delay
with an integer in that range for tests. This uses the existing oneshot, with
no resident scheduler. Its 45-minute start timeout bounds both sleep and the
update attempt, leaving 30 minutes after the longest delay. Opt-in
and route availability are checked again after sleeping.

`manet-auto-update.path` uses `PathExists` to notice initial
qualified clock sync, including a marker already present when the path starts.
It activates `manet-auto-update-clock.service`, a oneshot that queues the same
routine service and remains active for the rest of the boot. This prevents a
persistent marker from repeatedly triggering the path. Only this clock relay
uses `RemainAfterExit`; the routine service stays repeatable for later routable
events, including when the clock arrived before an uplink. The relay orders
after any in-progress routine check so a pre-sync check cannot consume its
start request. It queues without blocking to avoid waiting on that ordering.
`manet-auto-update.timer` adds `OnCalendar=daily` with up to two hours of random
delay. `Persistent=true` catches a missed calendar event after downtime; the
clock and route gates still apply. Both path and timer ship enabled in every
systemd payload and are enabled and started by the updater and radio setup.
The updater's version-marker mtime remains the actual 24-hour throttle after a
successful routine check or installation. A daily event that falls within
that window skips release lookup, so downloads are not promised every day.
All triggers use the same oneshot service: systemd coalesces concurrent starts
while it is activating. The updater also holds an exclusive nonblocking
`/run/manet-update.lock` flock across checking and installation, serializing
manual and automatic invocations. Disabled automatic updates exit successfully
before route lookups or delays.
No ICMP probe gates downloads and no download holds up a dispatcher event.
Manual stable/development downloads reject an absent clock marker with a
clear error; local archive recovery does not require a clock.
The obsolete
`mesh-default-route-fix.service` is disabled and removed by updates; the gateway
route manager owns mesh default routes and preserves service VIPs.

`no-carrier`, `degraded` and `routable` in this directory are three-line wrappers
around `manet-uplink-dispatch.sh <state>`. Nothing installs them, and their
states are covered by the `.d/` entries above.

## Adding or changing a hook

Change the file under `carrier.d/`, `routable.d/`, or `off`, and rebuild the
tarballs; every builder picks the set up from `stage_dispatcher_hooks`, so there
is one copy of each. The two generated hooks used to be heredocs duplicated
across the three install builders, which is how one wrong `grep` came to need
fixing in four places and how the rpi5 copy drifted from the other two.

`node-update.sh` reloads systemd after installing the tools payload; it does not
run `udevadm`. Dispatcher hooks need neither; networkd-dispatcher
reads the directory on each event, so a replaced hook is live immediately.

---

## LEDs

### External harness audit

`led-boot.sh` and `led-info.sh` previously counted text rows in `batctl neighbors`.
Their filter retained a column header and counted multiple radio links as
multiple neighbors, while command failures could become a false zero. Both now
use `mesh-neighbor-count.py`: a five-second-bounded `neighbors_json` query of
direct neighbors, including HaLow and wired mesh links, excluding multihop nodes.
The existing registry's `MAC_ADDRESSES` maps radio MACs to canonical node keys,
so multiple radios to one node produce one blink. Registry entries alone never
establish connectivity. See [batctl's direct-neighbor query](https://github.com/open-mesh-mirror/batctl#batctl-neighbors_json).

The helper returns 0 with an exact count, 1 for an unavailable query, or 3 when
live neighbors exist but their identities cannot yet be resolved. Exit 3 is
distinct from Python/argparse's exit 2 on launch/argument errors. Zero or one
neighbor address needs no registry. Missing, invalid or ambiguous registry
aliases for multiple addresses yield connected-without-a-count, never invented
node totals. The button shows N green blinks for N direct nodes, red for confirmed
zero, amber for an unavailable neighbor query, or three seconds solid green for
connected-without-a-count. Boot retries failed/empty queries and completes its
connected indication as soon as direct neighbors are confirmed, even before
registry identity discovery. ACS/quorum/tourguide originator counting is unchanged.

`manet-led-common.sh` also centralizes the optional harness configuration. An
existing GPIO controller is not evidence of attached LEDs: `/etc/default/manet-led`
must explicitly set `LED_ENABLED=1`. Pin defaults remain provisional. A shared
flock prevents competing boot/button holders; boot yields between half-cycles,
and the button owns its whole count sequence. Each dwell checks the gpioset
holder for failure. EXIT/INT/TERM cleanup kills and reaps it. An explicit off
request is held briefly before normal exit, but the electrical state after GPIO
release still depends on the circuit; no software test proves that it is dark.
See [libgpiod's lifetime contract](https://libgpiod.readthedocs.io/en/master/gpioset.html).

The external boot and button units now live in `MANET/systemd/`, so tools updates
carry the same definitions as installation. Both are ordinary simple services
wanted by `multi-user.target`, with no ordering after that target. The previous
boot unit remained activating indefinitely without peers because it was an
unbounded oneshot; its `DefaultDependencies=no` avoided implicit target ordering.
The button unit, which kept default dependencies, ordered itself after its own
target and conflicted with that target's implicit ordering after wanted services.
`radio-setup.sh` enables the shipped definitions instead of overwriting them.
Onboard LED behavior is independent and unchanged; regression tests cover its
complete/incomplete/running states and legacy completion-marker fallback.

### Provisioning verdict

`radio-setup.sh` used to drive the LEDs directly from three places, and the
result was unreliable in both directions.

The success signal was `echo heartbeat > /sys/class/leds/ACT/trigger`, written
unconditionally before the failure gate was reached, so a node that was about
to be marked incomplete got the success colour anyway.

The failure signal was worse. It came from `trap led_error ERR`, and the script
sets neither `set -e` nor `set -E`. An ERR trap still fires without `set -e` on
any command that returns non-zero outside a condition, and this script
continues past failures deliberately: every apt call, and around a dozen
top-level `grep`, `test` and `systemctl is-active` calls, can return non-zero
on a completely healthy run. Nothing ever reset the trigger, so a good
provision could finish heartbeating red.

Neither signal survived a reboot either, because `radio-setup-run-once.service`
disables itself, so on the next boot nothing ran and both LEDs came back on
their kernel defaults.

`manet-led-status.sh` replaces all three. It reads the recorded verdict, the
same `STATE` and `radio-setup.done` fallback that `manet-provision-status.sh`
reports on the login banner, so the two cannot disagree.
`manet-led-status.service` runs it on every boot and `radio-setup.sh` runs it
again the moment the verdict is written, which is what makes the pattern
persist and what makes it change only when the status does.

Solid and off both clear the trigger to `none` before writing `brightness`,
because whatever the kernel had driving the LED will otherwise overwrite the
value immediately. Brightness comes from the LED's own `max_brightness`, since
that is 1 on some class devices and 255 on others.

## MT7916 firmware selection

`manet-mt7916-firmware.py` uses the positioning service's literal config parser:
only `positioning=y` (case-insensitive, optional quotes) requests it; missing
config is off. The tool first requires a 14c3:7906 primary PCI function in
`/sys/bus/pci/devices`, read under `--root` for offline tests. Without it,
including a lone 14c3:790a secondary function, the tool removes owned overrides
and creates no pending marker. This check uses file reads, with no subprocess.
`modinfo` must also resolve `manet_timing=1` on the installed
mt7915e module for the running kernel. Missing tags or lookup failures remove
owned overrides and log the reason. Packages carry the unmodified WM/WA/ROM set and license from
linux-firmware `20250613`, commit `47e03ef409e07315f2b5c1d0fc08383da2e1bde9`.
Builders verify downloads and cache entries before staging. The cache is
`kernel-work/cache/linux-firmware/47e03ef409e07315f2b5c1d0fc08383da2e1bde9/`,
overridable with `MANET_FIRMWARE_CACHE` for builders and offline tests.

The JSON intent holds our replacement bytes and exact input/output hashes.
The stdlib sealer applies the PDA recurrence and outer CRC. The node persists
patched WM and matched stock WA/ROM as real files in
`/lib/firmware/updates/mediatek/`, using temporary files, fsync and rename;
WM is published last. A ready firmware check hashes only the installed set
and rewrites no firmware or ownership metadata. Correct files are retained
when repairing a missing companion.

The unique patched hash identifies our WM. `.manet-mt7916` records hashes and
inode/mtime identities for companions we wrote, before their publication.
Identical foreign copies are never adopted. Disable or preparation failure
removes only owned overrides; failed removal is reported and retried at next
boot. Distribution firmware is untouched. A directory lock serializes preparers
without a lock file or any involvement in driver probing.

The boot oneshot runs with `--boot` after `systemd-remount-fs.service` and
before `systemd-udev-trigger.service`, covering CM4's PCI coldplug. It must not
wait for `local-fs.target`: `/boot/firmware` needs a by-partuuid device that
only coldplug creates, so that ordering deadlocks into emergency mode. It is ordered
before coldplug but never required by it: a failure in this tool must not stop
every other device from loading. If the pending marker cannot be written, the
tool removes the patched set, so the probe falls through to stock firmware. The CM4 kernel
builds the PCIe host in and mt7915e as a module; its stock-kernel initramfs images
are not used for this radio. There is no probe wrapper or initramfs work.
Setup/update prepares files once. The `.path` unit is the sole config-change
trigger: `PathChanged=/etc/mesh.conf` covers atomic replacement via inode events
in [systemd's path implementation](https://github.com/systemd/systemd/blob/v257/src/core/path.c).
No tool loads or reloads the driver; a running radio changes firmware next boot.

Before leaving patched firmware available at boot, the tool durably writes
`/var/lib/manet/mt7916-probe-pending` with boot ID, WM hash and positioning
value. File and directory fsync precede firmware preparation, including a ready
set. This small marker write is required each enabled boot; no reseal occurs.
The survival oneshot runs after `multi-user.target`. It independently checks
that target is active and every MT7916 primary PCI function (14c3:7906) is
bound to mt7915e with a registered wiphy, or that no MT7916 is present, then
unlinks/fsyncs the marker. The driver symlink exists before probe; wiphy
registration follows synchronous MCU/firmware initialization in this driver.
An auxiliary 14c3:790a HIF alone cannot prove survival. Missing sysfs or an
unbound or still-initializing device retains the marker. A late successful
probe can therefore conservatively disable the next boot. There is no polling
or resident process.

A marker from a different boot durably latches
`/var/lib/manet/mt7916-auto-disabled` before cleanup, with the failed boot ID
and firmware hash. Every subsequent check reports `AUTO-DISABLED after failed
boot` and removes owned overrides. mesh.conf is untouched. An observed change
to the parsed positioning value, or `manet-mt7916-firmware.py --rearm`, clears
the latch; the driver tag is still required. Unrelated config edits, updates
and repeated `y` writes do not rearm it. A rapid `n` then `y` that the watcher
never observes requires explicit rearm. Same-boot checks preserve pending.

An orderly reboot before the survival check is conservatively treated as an
unconfirmed boot. There is no shutdown shortcut that could clear evidence
without a completed probe. The journal and persistent JSON expose the state;
the existing login provisioning banner could also display it in future.
Recovery depends on writable persistent storage and userspace coldplug ordering.
The reported boot failures were traced to the local-fs/coldplug ordering cycle
above. After its correction, patched warm and power-on boots on CM4 with
driver b623a361 and WM 18a2e1d0 completed, and survival cleared the marker.
