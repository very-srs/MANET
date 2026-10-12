# What runs automatically

## Marine

![Crayon drawing of green radios connected together and to a phone.](../assets/overview-crayon.png)

### The radio has little jobs to do

It looks for friends. It picks addresses. It checks its connections. It
shares what it knows.

You do not have to run all those jobs yourself. Look at the dashboard to
see what happened.

## User

### Judge the result, not the program names

The radio runs many small programs, called node tools. Most work in the
background. A long list of their filenames would not tell you whether your
radio is doing what you need. Start with the result you want to understand:

| What happens automatically | Where to see or understand the result |
|---|---|
| Hardware starts and setup records a verdict | [First boot](first-boot.md), [hardware](hardware.md), [indicators](power.md) |
| Radios discover peers, choose paths/channels and recover | [Mesh behavior](mesh.md), [radio controls](radios.md) |
| Nodes and attached devices get addresses | [Addresses and names](addresses.md), [connections](connect.md) |
| An available internet connection is shared | [Internet gateways](internet.md) |
| Nodes exchange identity and operating information | [Dashboard](dashboard.md) |
| Clocks find and verify a source | [Time](time.md) |
| Shared services choose their host | [Services](services.md) |
| Audio and phone location reach their consumers | [Voice](voice.md), [location and ATAK](location.md) |
| Authorized changes are staged and applied | [Settings](settings.md), [updates](updates.md) |
| Logs/results and optional setup work are retained | [Storage](storage.md), [customization](customization.md) |

Automatic does not mean instantaneous. Some work waits for hardware, an
address, a clock or an acknowledgement. Another node's displayed information
can lag behind its actual state. Each chapter explains the relevant wait
and how to recognize a result.

If something fails, begin with [the symptom](troubleshooting.md). Running
unfamiliar tools by hand can bypass the ordering their normal controllers
provide. The Developer tier explains those relationships for maintainers.

## Developer

### Read the system by responsibility

The [node-tool map](../../node-tools-map.md) indexes every production file
at the top of `MANET/node_tools`, grouped by responsibility. Each group links
to the guide chapter explaining its outcome; each file links to its source.
Use it to find ownership after reading the mechanism, rather than starting
with an alphabetical script list. The builder checks that new or removed
production files are reconciled with the map.

### Services, orchestrators, helpers and libraries have different lifetimes

Systemd starts persistent owners such as status, time, voice and ATAK, and
oneshot setup/recovery units establish prerequisites. The node manager
performs recurring reconciliation: publish local records, rebuild registry
observations, reconcile IPv4, run eligible elections and handle channel/
discovery policy. It delegates instead of owning every daemon's internal
state. [Core orchestration](../../../docs/node-tools-internals.md#core-orchestration)
describes the scheduling and service relationships.

Before each manager start, the selector creates `node-manager.sh` as a
symlink to the ACS or static orchestrator according to `acs` in mesh.conf.
The symlink is generated state and must not be shipped as a third manager.
Both real managers preserve address, status and service work; disabling
ACS changes Wi-Fi channel policy, not whether the radio is a functioning
mesh node.

Some helpers are executable wrappers around reusable Python modules. The
resident control process also handles supported runtime requests through
a local FIFO worker, avoiding repeated Python imports for common work.
A one-shot fallback covers unavailable resident execution. A request that
may already have been accepted must not be blindly replayed: fallback is
not permission to apply a side effect twice after an ambiguous failure.

### Reconciliation compares desired state with live evidence

Stable inputs do not always mean no work is needed. The IP preflight checks
kernel addresses/policy, EUD readiness and dnsmasq before skipping a settled
allocation. The MediaMTX fast path checks both VIPs and live service state
against the current winner. Missing outputs, failed services or changed
configuration return to the normal repair path.

Conversely, stable valid state should not cause needless writes/restarts.
Content-based registry/hosts/config handling and retained process caches
reduce storage writes and subprocess work. Time-based freshness must still
advance independently of cached parsing: a cached peer can become stale
without a file changing. The [registry reference](../../../docs/node-tools-internals.md#data-management)
explains observation age and publication.

### Events and periodic checks complement one another

Udev and networkd-dispatcher hooks react to USB/carrier events. Periodic
managers and watchdogs repair missed transitions or incomplete state.
Locks serialize local operations such as channel/config changes and
service ownership; they do not establish a global distributed lock.
Alfred exchanges observations and authenticated control, while BATMAN
provides the data path and current reachability.

When extending a tool, identify its owner, inputs, side effects, lock,
retry trigger, persistence and observable result. Update the outcome chapter
and the [runtime reference](../../../MANET/node_tools/README.md), then add
or amend its map entry. The [development guide](development.md) covers
verification and release boundaries.
