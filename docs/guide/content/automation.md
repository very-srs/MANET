# What runs automatically

## Marine

![Crayon drawing of green radios connected together and to a phone.](../assets/overview-crayon.png)

### The radio has little jobs to do

It looks for friends. It picks addresses. It checks its connections. It
shares what it knows.

You do not have to run those jobs yourself because your radio will help you out.
Use your phone to look at the **[http://manet.local/](http://manet.local/)**
dashboard and see the network your radios made.

## User

### Use a desired result to find the program responsible

The radio runs many small programs, called node tools. These are designed to
do a single specific task and they work in the background.
To learn more about which program is responsible for an action, start with the 
result you want to understand:

| What happens automatically | Where to see or understand the result |
|---|---|
| Hardware starts and setup records a verdict | [First boot](first-boot.md), [hardware](hardware.md), [indicators](power.md) |
| Radios discover peers, choose paths/channels, and recover | [Mesh behavior](mesh.md), [radio controls](radios.md) |
| Nodes and attached devices get addresses | [Addresses and names](addresses.md), [connections](connect.md) |
| An available internet connection is shared | [Internet gateways](internet.md) |
| Nodes exchange identity and operating information | [Dashboard](dashboard.md) |
| Clocks find and verify a source | [Time](time.md) |
| Shared services choose their host | [Services](services.md) |
| Audio and phone location reach their consumers | [Voice](voice.md), [location and ATAK](location.md) |
| Authorized changes are staged and applied | [Settings](settings.md), [updates](updates.md) |
| Logs/results and optional setup work are retained | [Storage](storage.md), [customization](customization.md) |

Automatic does not mean instantaneous. Some work waits for hardware, an
address, a clock, or a peer's acknowledgement. Another radio's saved information
can lag behind its actual state. Each chapter explains the relevant wait
and how to recognize a result.

If something fails, begin with [the symptom](troubleshooting.md). Running
unfamiliar tools by hand can bypass the ordering their normal controllers
provide.

## Developer

### Read the system by responsibility

The [node-tool map](../../node-tools-map.md) indexes every production file
from `MANET/node_tools`, grouped by responsibility. Each group links
to the guide chapter explaining its outcome; each file links to its source.
Use it to find task ownership after reading the mechanism responsible, rather than starting
with an alphabetical script list and guessing.

### Services, orchestrators, helpers, and libraries have different lifetimes

Systemd starts persistent services such as node status, time, voice, and 
the ATAK helper.  Systemd oneshot setup/recovery units configure known states
for those services to run from. The node manager
performs recurring reconciliation: publish local records, rebuild registry
observations, reconcile IPv4, run eligible elections, and handle channel/
discovery policy. It delegates instead of owning every daemon's internal
state. [Core orchestration](../../../docs/node-tools-internals.md#core-orchestration)
describes the scheduling and service relationships.

Before each manager start, the selector creates `node-manager.sh` as a
symlink to the ACS (automatic channel selection) 
 or static orchestrator according to `acs` in mesh.conf.
The symlink is generated and should not be shipped as a third file.
Both versions of node-manager preserve address, status, and service work.
Disabling ACS changes the Wi-Fi channel policy, not whether the radio is a functioning
mesh node.

Some helpers are executable wrappers around reusable Python modules. The
resident control process also handles supported runtime requests through
a local FIFO worker, avoiding repeated Python imports for common work.
A one-shot fallback covers unavailable resident execution. A request that
may already have been accepted must not be blindly replayed and fallback is
not permission to apply a side effect twice after an ambiguous failure.

### Reconciliation compares desired state with live evidence

Stable inputs do not always mean no work is needed. The IP preflight confirms
kernel addresses/policy, EUD readiness, and dnsmasq before skipping a settled
allocation. The MediaMTX election checks both VIPs and live service state
against the current winner. Missing outputs, failed services or changed
configuration return to the normal repair path.

Conversely, a stable valid state should not cause needless writes/restarts.
Content-based registry/hosts/config handling and retained process caches
reduce storage writes and subprocess work. Time-based freshness must still
advance independently of cached parsing. A cached peer can become stale
without a file changing. The [registry reference](../../../docs/node-tools-internals.md#data-management)
explains observation age and publication.

### Events and periodic checks complement one another

Udev and networkd-dispatcher hooks react to USB and network carrier events. Periodic
managers and watchdogs repair missed transitions or incomplete state.
Locks serialize local operations such as channel/config changes and
service ownership; they do not establish a global distributed lock.
Alfred exchanges observations and authenticated control, while BATMAN
provides the data path and current reachability.

