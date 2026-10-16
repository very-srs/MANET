# MT7916 third-chain observation

The two patches reserve the third band-1 RX path and add a production energy
observer. They target the same exact kernel HEADs as `mt76-ftm`: CM4
`95b85bebbedcaedfa7ca79116ed38b7376fba412` and Rock 3A
`7d0a66e4bb9081d75c82ec4957c50034cb0ea449`. Apply txpower first, then:

```sh
python3 kernel/patches/mt76-bgobs/apply.py /path/to/kernel
python3 -m unittest discover -s kernel/patches/mt76-bgobs -v
```

The helper applies or verifies the timing prerequisite and this series in one
transaction. It accepts a clean tree, the complete timing prefix, or the
complete combined series. Unknown HEADs, partial series and conflicts fail
without changing source files. Unrelated edits remain intact. Both board
builders call this helper in place of the standalone timing helper. Timing's
standalone reverse check cannot recognize its overlapping hunks after this
series is applied. Use this combined helper for subsequent builds.

Patch 0001 adds the read-only `background_radar_2t2r` module parameter, default
off. It validates MT7916 DBDC with 2T2R band 0 and three RX paths on band 1,
including selectable 5/6 GHz EEPROM byte `0xdb` when running at 5 GHz. It
modifies only the RAM EEPROM shadow, checks firmware acceptance before wiphy
registration, and advertises the reduced antenna masks. It reserves the third
RX chain for observation or background CAC; the main 5 GHz radio becomes
2T2R. Antenna reconfiguration cannot reclaim that chain. Band 0 does not gain
background CAC capability from the band-1 reservation.

Patch 0002 uses the cold-start configuration validated on Rock 3A with WM
build `20240823172741`: frontend routing, RDD2 detector and paired parameters,
with RDD2 interrupts masked and its pulse writer off. It never programs a
firmware buffer pointer, enables that writer, sends RDD_START, or clears the
band-wide histogram. Diagnostic modes and register sweeps are absent.
Masked writes have readback checks. START and STOP are fenced by MCU queries;
failed restoration retains ownership and blocks further observation/CAC.
Radio stop, channel changes, reset and removal cancel work. A reset releases
ownership only after successful firmware reinitialization. Band-0/1 radar
reports retain their existing routing; observer RDD2 reports have no CAC owner.
RDD2 events carry no epoch tag. A delayed event arriving after a later CAC
claims the receiver remains a hardware lifecycle check for future DFS work.

## Runtime interface

With the reservation active and debugfs enabled/mounted, the 5 GHz PHY exposes
`/sys/kernel/debug/ieee80211/phyN/mt76/background_scan`, mode 0600. Resolve the
PHY from the interface's `phy80211/name`; PHY numbering is not fixed.
Node-manager runs as root and can use this file without changing permissions.
Debugfs keeps this local, hardware-specific ABI out of nl80211; MANET maintains
its explicit `schema: 1` contract. An absent file selects ordinary ACS scans.

Write `start FREQ WIDTH CENTER DWELL_MS THRESHOLD` or `stop`. The production
ACS reader uses 20 MHz, center equal to frequency, 250 ms and threshold bin 2.
Dwell accepts 100..5000 ms. The kernel validates the channel definition and
rejects disabled, no-IR and radar channels and 5600..5650 MHz overlap. No diagnostic
commands are accepted. A concurrent receiver owner returns EBUSY.

Reads return JSON. Fields include `schema`, monotonic `sequence`, `active`,
`reserved` (an acquisition or failed cleanup still owns the receiver),
`rx_reserved` (boot-time chain reservation), `valid`, `error`, `restore_error`,
channel/width, dwell, `started_boottime_us`, `elapsed_us`, `threshold_bucket`,
`sample_count`, `first`, `last`, `delta`, and `wrap_mask`. The twelve-element
arrays retain eleven measured bins; element 11 is unused. `energy_permille`
excludes bin 10; `energy_upper_permille` includes it. These are raw bounds.
`busy_permille` remains null. `tx_first_us` and `tx_last_us` are the main
band's accumulated survey TX counters sampled at both histogram endpoints;
`tx_read_max_us` bounds the read latency. The firmware MIB request uses the
same accumulated accounting as `survey time_tx`, under the driver mutex.
MCU failure invalidates the observation. No separate destructive MIB read is
introduced. Module tags are `manet_observer=third-chain-v1` and
`manet_observer_probe=0`.

Deltas are modulo 2^23, with no clear. Each bin's low-bit wrap interval is
67.108864 seconds at the observed 8 us cadence. Actual windows longer than
six seconds, insufficient population, or population outside 80..120% of that
cadence are invalid. A plausible untracked hardware reset cannot always be
distinguished from a wrap; never run other histogram-control tools concurrently.

Own-TX correction is in `manet_bgobs.py`. With fractions L for bins 2..9,
B for bin 10, and T for main-band survey TX, it publishes
`100 * (L + max(0, B - T)) / (1 - T)`, clamped to 0..100. Bin-10 energy above
own TX remains foreign. At least 10% unblanked time is required. Excessive
query latency, counter decreases or inconsistent bin-10/TX evidence omit the
sample. The independent TX accounting and correction still require bench
validation under mixed local and foreign load, especially on CM4.

All cooperating users must hold `/run/manet-bgobs-phyN.lock` through acquisition
and release. The helper verifies each sequence and never stops a different
owner. The kernel also rejects overlapping acquisition, active CAC, scanning,
reset and stopped radios. It finishes the dwell even if userspace exits.

## Verification boundary

Tests apply actual patches to disposable snapshots of both local repositories
and compile extracted C with AddressSanitizer/UndefinedBehaviorSanitizer.
They exercise EEPROM layouts, antenna masks, command fencing, failed writes,
restoration, cancellation, resets, radar routing, TX-query failure and counter
wraps. Module builds use isolated prepared build directories. These checks do
not validate RF response, module loading or hardware cleanup. CM4 cold boot,
chain masks, concurrent traffic and restoration remain bench acceptance work.

The observer is not background CAC and does not make DFS channels available.
Any future DFS ACS move must coordinate per-node CAC and activation, preserving
radar ownership and handling expiration of unused channel availability. See the
channel-scoring design note in `docs/node-tools-internals.md`.
