# Measure a connection

## Marine

![Crayon drawing of a phone inspecting connected radios through a magnifying glass.](../assets/dashboard-crayon.png)

### Try sending some stuff

1. Open **Manage → Measure**.
2. Pick another radio and a test.
3. Keep the radios still while it runs.
4. Save the result from **Sessions**.

A speed test uses the radio too. Let important talking finish first.

## User

### Choose the question you want to answer

The dashboard estimates useful paths. A measurement sends real test traffic
to see what a connection did during that run. It measures between radios,
not an internet website, and does not include your phone's own link in that
test path.

| Test or result | Plain-language meaning |
|---|---|
| TCP throughput | How quickly a reliable data transfer delivered information. One stream and four streams can behave differently. |
| UDP throughput | What arrived when the sender offered traffic at the selected rate. Sending faster than a path can carry can cause loss. |
| Jitter | How unevenly packets arrived, relevant to real-time audio/video. |
| Packet loss | How much test traffic failed to arrive. |
| Ping | Whether the peer answered, and the round-trip delay. This is not a capacity test. |
| Reverse direction | Traffic travels back from the peer toward the radio running the test. Results can differ by direction. |

### Run and label a comparison

Connect to the radio that will run the test and sign in to **Manage**.
Open **Measure**, select the peer, tests, direction and duration, and give
the session a useful name. Throughput durations range from five to three
hundred seconds. Only one batch can run on this radio at once.

Keep positions, antennas and settings fixed during the test. Record what
you changed between runs, such as location, HaLow width or Wi-Fi band.
Traffic from other users affects the result; the test also competes with
their traffic. A faster result after several changes cannot identify which
change helped.

Use **Sessions** to inspect and download JSON or CSV results. Export anything
you need to keep before deleting sessions. The radio does not automatically
erase old measurements to make room; when its measurement budget is full,
new work reports an error until space is available. See [storage](storage.md).

### Interpret a result in context

A multihop transfer can cross different radios or change path during a
test. The stored topology helps explain conditions, but it is not a recording
of every packet's route. The displayed BATMAN Mbit/s figure, the wireless
connection rate and measured application throughput answer different questions.

Repeat a comparison under similar conditions before drawing a conclusion.
A failed test can mean a missing route or unreachable test service, not
necessarily a defective radio. Check the [dashboard](dashboard.md) and
[troubleshooting guide](troubleshooting.md) alongside the result.

## Developer

### Local worker, remote test endpoint

The authenticated management handler admits one local measurement worker.
It runs `iperf3` against the selected peer's listening endpoint or sends
ICMP ping. There is no Alfred configuration transaction for a test, and
selecting a pair does not arrange an arbitrary remote-to-remote worker.
Reverse iperf changes the traffic direction while retaining the local
control client. Port 5201 is limited to the configured mesh subnet.

TCP single/four-stream tests use the respective parallelism. UDP throughput,
jitter and loss views use a configured offered bitrate; reverse uses iperf's
reverse flag. A TCP result prefers received totals. UDP summaries expose
receiver loss and jitter, so offered rate must accompany a throughput claim.
Ping parses round-trip statistics and loss separately.

### Admission and execution bounds

A batch allows up to 64 pairs and seven test types. Throughput duration must
be 5–300 seconds. Admission budgets the combinations, subprocess overhead,
route collection and pauses against a one-hour monotonic execution limit.
An iperf command has a duration-plus-25-second timeout; the default hundred
ping probes at 0.2-second intervals have a thirty-second command budget.
The worker checks remaining time between operations rather than starting
work known not to fit.

The web server's management-write serialization is distinct from the
long-running measurement lock. A submitted job continues without requiring
a browser poll to remain connected. Status reports progress and errors;
another start is rejected while it runs. Authentication and request-size
limits still apply before job admission.

### Evidence and storage consistency

A single topology snapshot annotates the batch, while individual results
record endpoints, type, time and command output/metrics. That snapshot
cannot establish a per-packet path or freeze routing. Use packet capture
or lower-layer evidence when a claim depends on which radio carried traffic.

Capacity is checked at admission, before tests and before saves. Results
are durably replaced atomically, with temporary-file allocation included in
the storage budget. Exhaustion is an explicit failure and existing sessions
remain intact. Session paths are validated as direct real children of the
measurement directory; arbitrary paths and symlinks are not export/delete
targets. The [storage guide](storage.md#developer-measurement-admission-is-a-budget-not-pruning)
explains configurable limits.

The implementation entry points are
[measurement management](../../../MANET/node_tools/manet_manage.py) and
[storage accounting](../../../MANET/node_tools/manet_measurement_storage.py).
Use their result fields rather than scraping display text for external
analysis. See also [web limits and observations](dashboard.md#developer-freshness-load-and-sessions).
