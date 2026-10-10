"""Gate MT7916 debugfs/UDP/trace controls on capture association evidence.

Normal sessions fail before writes: capture cannot prove the absolute
accumulator origin or exclude lost/misassociated background responder rows.
Jitter alone cannot bound clock offset or certify completeness. Majority
STEP repair misses a lost prefix's uniform +k*STEP range bias. Low spread
does not prove a valid association.
Verified time association, counter origin and capture invalidation could
suffice without hardware identity or a WM change. The interface does not
prove these together. diagnostic_only=True permits controls, paced marked
probes and read_raw_reports().
Normalized readers still raise AssociationUnavailable.
Run in a privileged helper separate from the network protocol process. It
must validate callers, own the device exclusively, poll regularly, forward
events to Ranging, close on disconnect/exit and abort on netlink role/channel
changes. The driver has no crash-safe lease. No daemon, IPC or privilege
installation is provided. I/O is injected and imports perform none.
"""

from collections import deque
from dataclasses import dataclass
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import time

import manet_ranging as ranging

# Bound textual trace reads to 1 MiB and decoded captures to 4096 rows.
MAX_TRACE_BYTES = 1024 * 1024
MAX_RAW_REPORTS = 4096
# Fixed band-1 register selects normal WM report routing.
NORMAL_ROUTE = 0x820F701C
# tmr_ctrl profiles follow the enable flag in the driver debugfs command.
CTRL_INIT = "0 2 0 3 3 3 3"
CTRL_RESP = "1 2 0 0x22 2 3 3 8 10"
ASSOCIATION_GAP = ("capture cannot establish initial time association/WM report origin "
                   "or verify complete background-report accounting")


class RadioError(RuntimeError):
    """Signal a radio setup, operation or cleanup failure."""
    pass


class AssociationUnavailable(RadioError):
    """Signal that capture evidence cannot support normalized samples."""
    pass


class CaptureInvalid(RadioError):
    """Signal an incomplete, malformed or ambiguous timestamp capture."""
    pass


class FileIO:
    """Only existing pseudo-files are written. No shell, mounts or chmods."""
    def read(self, path, limit=8192):
        """Read bounded ASCII content without following the final symlink."""
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise CaptureInvalid(f"oversized file: {path.name}")
        return data.decode("ascii", errors="strict")

    def write(self, path, value):
        """Write ASCII to an existing path and reject a partial write."""
        data = value.encode("ascii")
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            if os.write(fd, data) != len(data):
                raise RadioError(f"short write: {path.name}")
        finally:
            os.close(fd)

    def mkdir_instance(self, path):
        """Create a trace instance, refusing an existing owner."""
        path.mkdir()  # Refuse an existing instance.

    def remove_instance(self, path):
        """Remove the owned tracefs instance and its virtual children."""
        path.rmdir()  # tracefs removes virtual children.

    def stats_paths(self, instance):
        """Return per-CPU trace loss-counter paths in stable order."""
        return sorted((instance / "per_cpu").glob("cpu[0-9]*/stats"))


class DeviceLock:
    """The driver stores mark/peer/SPE/rate per device, shared by both PHYs."""
    def __init__(self, path):
        """Store the device lock path without acquiring it."""
        self.path, self.fd = Path(path), None

    def acquire(self):
        """Acquire the device lock without waiting for another owner."""
        if self.fd is not None:
            raise RadioError("device already owned by this adapter")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(fd)
            raise
        self.fd = fd

    def release(self):
        """Close the lock descriptor to release device ownership."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


@dataclass(frozen=True)
class Config:
    """Hold radio identifiers, paths and diagnostic pacing limits."""
    interface: str
    phy: str
    device: str  # PCI BDF, as printed by mt7915_rx_tmr's dev= field.
    port: int   # Deployment-selected port, not installed here.
    mark: int = 0x77
    debug_root: Path = Path("/sys/kernel/debug/ieee80211")
    trace_root: Path = Path("/sys/kernel/tracing")
    lock_root: Path = Path("/run/lock")
    # Lease defaults to its 10 s maximum. Prep phases default to 0.5 s each.
    lease_s: float = 10.0
    prepare_s: float = 0.5
    # Default pacing tolerates 1 ms lateness, configurable up to 2 ms.
    max_send_lateness_s: float = 0.001

    def __post_init__(self):
        """Validate device identifiers, socket values and bounded timings."""
        # Restrict path components to interface, phyN and PCI BDF syntax.
        if (not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", self.interface) or
                self.interface in (".", "..") or not re.fullmatch(r"phy[0-9]+", self.phy) or
                not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", self.device) or
                not 1 <= self.port <= 65535 or not 1 <= self.mark <= 0xFFFFFFFF or
                not math.isfinite(self.lease_s) or not 0 < self.lease_s <= 10 or
                not math.isfinite(self.prepare_s) or not 0 < self.prepare_s <= 1 or
                not math.isfinite(self.max_send_lateness_s) or
                not 0 < self.max_send_lateness_s <= 0.002):
            raise ValueError("invalid radio configuration")


@dataclass(frozen=True)
class Link:
    """Carry verified link-local endpoints, peer MAC and channel width."""
    ifindex: int
    local_address: str
    peer_address: str
    peer_mac: bytes
    width_mhz: int


def _link_local(address):
    """Validate an unscoped unicast IPv6 link-local address."""
    if "%" in address:
        raise RadioError("scope is supplied separately, not inside the address")
    parsed = ipaddress.IPv6Address(address)
    if not parsed.is_link_local or parsed.is_multicast:
        raise RadioError("a unicast IPv6 link-local address is required")
    return str(parsed)


def _command(args):
    """Run a read-only discovery command with a two-second limit."""
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=2).stdout


class LinuxLinkProbe:
    """Resolve known neighbours without guessing EUI-64 or using routed IPs.

    endpoint(peer) is the helper's trusted neighbour mapping, not a free-form
    command from a mesh packet. No neighbour discovery or interface changes are
    performed here. Later role/channel changes must be delivered via abort().
    """
    def __init__(self, config, endpoint, *, sys_root=Path("/sys/class/net"), run=_command):
        """Bind link discovery to trusted endpoint and command providers."""
        self.config, self.endpoint = config, endpoint
        self.sys_root, self.run = Path(sys_root), run

    def __call__(self, peer):
        """Verify mesh role, direct station and link-local address mapping."""
        cfg = self.config
        iface = self.sys_root / cfg.interface
        phy = (iface / "phy80211").resolve(strict=True)
        if (phy.name != cfg.phy or (iface / "master").resolve(strict=True).name != "bat0" or
                (phy / "device").resolve(strict=True).name != cfg.device):
            raise RadioError("interface is not this device's bat0 mesh PHY")
        info = self.run(["iw", "dev", cfg.interface, "info"])
        # iw reports primary channel frequency and width in MHz.
        channel = re.search(r"channel\s+\d+\s+\((\d+) MHz\), width:\s*(\d+) MHz", info)
        if (not re.search(r"^\s*type mesh point\s*$", info, re.M) or not channel or
                not 5000 <= int(channel[1]) < 5900):
            raise RadioError("ranging requires the 5 GHz mesh interface")
        ifindex = int((iface / "ifindex").read_text().strip())
        mac = peer.mac.hex(":")
        station = self.run(["iw", "dev", cfg.interface, "station", "get", mac])
        # Match the requested MAC at the start of an iw Station record.
        if not re.search(rf"^Station {re.escape(mac)}\b", station, re.M | re.I):
            raise RadioError("peer is not a direct 5 GHz station")
        remote = _link_local(self.endpoint(peer))
        neighbours = json.loads(self.run(["ip", "-j", "-6", "neigh", "show", "to", remote,
                                          "dev", cfg.interface]))
        # ip JSON must map the trusted endpoint to this MAC in a usable state.
        if not any(row.get("dst") == remote and row.get("lladdr", "").lower() == mac and
                   set(row.get("state", [])) & {"REACHABLE", "STALE", "DELAY", "PROBE", "PERMANENT"}
                   for row in neighbours):
            raise RadioError("link-local neighbour does not resolve to the authorized peer MAC")
        addresses = json.loads(self.run(["ip", "-j", "-6", "addr", "show", "dev", cfg.interface,
                                         "scope", "link"]))
        # ip can expose DAD failures as boolean fields or flag names.
        local = [a["local"] for dev in addresses for a in dev.get("addr_info", [])
                 if a.get("scope") == "link" and a.get("family") == "inet6" and
                 not a.get("tentative") and not a.get("dadfailed") and
                 not set(a.get("flags", [])) & {"tentative", "dadfailed"}]
        if len(local) != 1:
            raise RadioError("exactly one usable local IPv6 link-local address required")
        return Link(ifindex, _link_local(local[0]), remote, peer.mac, int(channel[2]))


@dataclass(frozen=True)
class RawReport:
    """Diagnostic timestamps only. record_number is not a WM report_index.

    DW3 high half is a likely 802.11 sequence-control field on RESPONDER
    records, not the UDP sequence. Processed DW4 overwrote four peer MAC bytes.
    No complete peer identity, session cookie or producer ordinal is exported.
    """
    record_number: int
    trace_time: float
    queue: int
    departure: int
    arrival: int
    words: tuple


@dataclass(frozen=True)
class ClockOffsetBound:
    """Independent responder-arrival minus initiator-departure bound in counts.

    reference_departure is on the initiator clock. A future hardware join must
    supply this bound from independent evidence, never from its chosen seed.
    No such evidence provider is installed by the current diagnostic adapter.
    """
    reference_departure: int
    offset: int
    uncertainty: int

    def __post_init__(self):
        """Validate the reference timestamp, signed offset and uncertainty."""
        half = ranging.CLOCK_MODULUS // 2
        if (type(self.reference_departure) is not int or
                not 0 <= self.reference_departure < ranging.CLOCK_MODULUS or
                type(self.offset) is not int or not -half <= self.offset < half or
                type(self.uncertainty) is not int or not 0 < self.uncertainty < half):
            raise ValueError("invalid independent clock-offset bound")

    def permits(self, departure, offset, ppm):
        """Check an offset against uncertainty plus elapsed clock drift."""
        elapsed = abs(ranging.clock_delta(departure, self.reference_departure))
        return abs(ranging.clock_delta(offset, self.offset)) <= self.uncertainty + elapsed * ppm / 1e6


def time_pair_reports(initiator, responder, *, initial_offset, min_pairs=10,
                      tolerance_counts=4000, max_drift_ppm=40.0):
    """Diagnostic time join returning indices into the complete input streams.

    Requires an independent offset bound and rejects competing supported joins.
    Compatible suffix walks are not competitors. This establishes neither UDP
    sequence identity nor WM ordinals. Normal Radio readers remain refused.
    Background rows may be skipped in pairing, but never renumbered beforehand.
    """
    if not isinstance(initial_offset, ClockOffsetBound):
        raise CaptureInvalid("independent initial clock-offset bound required")
    if (type(min_pairs) is not int or not 2 <= min_pairs <= ranging.MAX_FRAMES or
            not math.isfinite(tolerance_counts) or tolerance_counts <= 0 or
            not math.isfinite(max_drift_ppm) or max_drift_ppm < 0):
        raise ValueError("invalid time pairing limits")
    a, b = tuple(initiator), tuple(responder)
    if not all(min_pairs <= len(rows) <= MAX_RAW_REPORTS for rows in (a, b)):
        raise CaptureInvalid("invalid time pairing capture size")
    for rows, field in ((a, "departure"), (b, "arrival")):
        values = [getattr(r, field) for r in rows]
        if (any(type(v) is not int or not 0 <= v < ranging.CLOCK_MODULUS for v in values) or
                any(ranging.clock_delta(y, x) <= 0 for x, y in zip(values, values[1:]))):
            raise CaptureInvalid("unordered or invalid hardware timestamps")
    total_elapsed = ranging.clock_delta(a[-1].departure, a[0].departure)
    # Nonoverlapping initiator time gates prevent two probes sharing a row.
    largest_gate = tolerance_counts + total_elapsed * max_drift_ppm / 1e6
    if any(ranging.clock_delta(y.departure, x.departure) <= 2 * largest_gate
           for x, y in zip(a, a[1:])):
        raise CaptureInvalid("initiator reports too close for unambiguous time pairing")
    candidates, seeds = [], 0
    for i0 in range(min(10, len(a))):
        # Scan all responder rows: ordinary data may precede the first probe.
        for j0 in range(len(b)):
            offset = ranging.clock_delta(b[j0].arrival, a[i0].departure)
            if not initial_offset.permits(a[i0].departure, offset, max_drift_ppm):
                continue
            seeds += 1
            # Bound candidate-search work even with a broad offset bound.
            if seeds > 100:
                raise CaptureInvalid("too many candidate time seeds")
            previous, j, pairs = a[i0].departure, j0 + 1, [(i0, j0)]
            for i in range(i0 + 1, len(a)):
                elapsed = ranging.clock_delta(a[i].departure, previous)
                allowance = tolerance_counts + elapsed * max_drift_ppm / 1e6
                matches = []
                # Skip responder rows before this probe's tracked time gate.
                while j < len(b):
                    current = ranging.clock_delta(b[j].arrival, a[i].departure)
                    residual = ranging.clock_delta(current, offset)
                    if residual < -allowance:
                        j += 1
                        continue
                    break
                k = j
                while k < len(b):
                    current = ranging.clock_delta(b[k].arrival, a[i].departure)
                    if ranging.clock_delta(current, offset) >= allowance:
                        break
                    if initial_offset.permits(a[i].departure, current, max_drift_ppm):
                        matches.append((k, current))
                    if len(matches) > 1:
                        raise CaptureInvalid("multiple responder reports inside time gate")
                    k += 1
                if matches:
                    j, offset = matches[0]
                    pairs.append((i, j))
                    previous, j = a[i].departure, j + 1
            if len(pairs) >= min_pairs:
                candidates.append(tuple(pairs))
    if not candidates:
        raise CaptureInvalid("too few pairs inside independent offset bound")
    # Keep the longest join only if other supported joins agree on matches.
    best = max(candidates, key=len)
    forward, reverse = dict(best), {j: i for i, j in best}
    if any((i in forward and forward[i] != j) or (j in reverse and reverse[j] != i)
           for walk in candidates for i, j in walk):
        raise CaptureInvalid("competing time alignments")
    return best


def decode_wm_accumulator(payload):
    """Decode the measured 0x57 reply for the fixed 64-byte block at e00f20c0.

    Pure offline decoder, not an MCU command sender or a capture fence. The
    caller must correlate the reply. A valid payload alone does not prove epoch.
    """
    if (not isinstance(payload, bytes) or len(payload) != 104 or
            struct.unpack_from("<I", payload)[0] != 0x38000068 or
            struct.unpack_from("<II", payload, 32) != (0x57, 0xE00F20C0)):
        raise CaptureInvalid("unsupported WM accumulator dump")
    # The accumulator is at offset 0x1c within the block after a 40-byte header.
    return struct.unpack_from("<I", payload, 40 + 0x1C)[0]


def accumulator_report_count(before, after, width_mhz):
    """Count correction calls across independently fenced, same-width snapshots.

    Preconditions: correction enabled, no reset, no mixed-band/width updates,
    <= MAX_RAW_REPORTS calls. A modulo-32 subtraction handles the word wrapping.
    Both enable and disable through tmr_ctrl reset S. Read before disarming.
    S changes before report export. This count alone cannot certify the fence,
    locate a missing row, detect duplicates, or establish the absolute baseline.
    """
    if (any(type(v) is not int or not 0 <= v < 1 << 32 for v in (before, after)) or
            width_mhz not in (20, 40, 80)):
        raise ValueError("invalid accumulator snapshot/profile")
    # Each width has its own measured decrement per correction call.
    step = {20: 2454, 40: 1254, 80: 750}[width_mhz]
    count, remainder = divmod((before - after) & 0xFFFFFFFF, step)
    if remainder or count > MAX_RAW_REPORTS:
        raise CaptureInvalid("accumulator delta is not a bounded report count")
    return count


# Match trace time, PCI device, queue, lengths, copy status and hex payload.
_TRACE = re.compile(r"\s([0-9]+\.[0-9]+):\s+mt7915_rx_tmr:\s+"
                    r"dev=(\S+) queue=(-?\d+) len=(\d+) captured=(\d+) copy_error=(-?\d+) data=(.*)$")


def parse_trace(text, device):
    """Decode complete type-4 TMR records for the selected PCI device."""
    reports = []
    for line in text.splitlines():
        if "mt7915_rx_tmr:" not in line:
            continue
        match = _TRACE.search(line)
        if not match:
            raise CaptureInvalid("malformed TMR trace record")
        stamp, observed_device, queue, length, captured, error, data = match.groups()
        if observed_device != device:
            continue
        try:
            raw = bytes.fromhex(data)
        except ValueError as exc:
            raise CaptureInvalid("invalid trace hex") from exc
        if (int(error) or int(length) != 40 or int(captured) != 40 or len(raw) != 40 or
                int(queue) < 0):
            raise CaptureInvalid("incomplete/unsupported TMR report")
        # Ten little-endian words carry the measured 40-byte type-4 format.
        words = struct.unpack("<10I", raw)
        if (words[0] & 0xFFFF) != 40 or (words[0] >> 27) != 4:
            raise CaptureInvalid("not the measured MT7916 type-4 report format")
        # DW6 bits 0..15 extend DW4 and bits 16..31 extend DW5 to 48 bits.
        reports.append(RawReport(len(reports) + 1, float(stamp), int(queue),
                                 ((words[6] & 0xFFFF) << 32) | words[4],
                                 ((words[6] >> 16) << 32) | words[5], words))
        if len(reports) > MAX_RAW_REPORTS:
            raise CaptureInvalid("too many TMR records")
    return tuple(reports)


class TraceCapture:
    """Use a private tracefs instance, preserving global tracing settings."""
    def __init__(self, config, io):
        """Initialize an unowned trace instance and empty snapshot cache."""
        self.config, self.io = config, io
        self.instance = None
        self.cached = None

    def start(self, sid):
        """Create and enable a session-specific device-filtered trace."""
        if self.instance is not None:
            raise RadioError("trace instance already owned")
        instance = self.config.trace_root / "instances" / ("manet-ranging-" + sid.hex())
        self.io.mkdir_instance(instance)
        self.instance, self.cached = instance, None
        event = instance / "events/mt7915/mt7915_rx_tmr"
        self.io.write(instance / "tracing_on", "0\n")
        self.io.write(instance / "current_tracer", "nop\n")
        # tracefs brackets the selected clock in its space-separated list.
        clocks = self.io.read(instance / "trace_clock").replace("[", "").replace("]", "").split()
        if "mono" not in clocks:
            raise RadioError("tracefs mono clock unavailable")
        self.io.write(instance / "trace_clock", "mono\n")
        self.io.write(instance / "buffer_size_kb", "64\n")
        self.io.write(event / "enable", "0\n")
        # The trace field is 'device', even though TP_printk prints 'dev='.
        self.io.write(event / "filter", f'device == "{self.config.device}"\n')
        # Clear the device-filtered buffer before enabling capture.
        self.io.write(instance / "trace", "")
        self.io.write(event / "enable", "1\n")
        self.io.write(instance / "tracing_on", "1\n")

    def read(self):
        """Freeze capture and cache parsed reports after checking loss."""
        if self.cached is not None:
            return self.cached
        if self.instance is None:
            raise CaptureInvalid("no trace capture")
        # Stop recording before reading loss counters and the frozen buffer.
        self.io.write(self.instance / "tracing_on", "0\n")
        stats = self.io.stats_paths(self.instance)
        if not stats:
            raise CaptureInvalid("trace loss counters unavailable")
        for path in stats:
            # Every CPU must expose all three counters, each equal to zero.
            fields = dict(re.findall(r"^(overrun|commit overrun|dropped events):\s*(\d+)\s*$",
                                     self.io.read(path), re.M))
            if len(fields) != 3 or any(int(v) for v in fields.values()):
                raise CaptureInvalid("trace records were lost or loss counters are missing")
        self.cached = parse_trace(self.io.read(self.instance / "trace", MAX_TRACE_BYTES), self.config.device)
        return self.cached

    def close(self):
        """Attempt to disable tracing and remove the private instance."""
        if self.instance is None:
            return
        errors = []
        for path in (self.instance / "tracing_on", self.instance / "events/mt7915/mt7915_rx_tmr/enable"):
            try:
                self.io.write(path, "0\n")
            except Exception as exc:
                errors.append(str(exc))
        try:
            self.io.remove_instance(self.instance)
            self.instance = None
        except Exception as exc:
            errors.append(str(exc))
        if errors:
            raise RadioError("trace cleanup: " + "; ".join(errors))


@dataclass(frozen=True)
class Event:
    """Carry a deferred completion or failure event to the owning loop."""
    kind: str  # Forward burst_finished/radio_error after poll() returns.
    session_id: bytes
    detail: str = ""


class RadioAdapter:
    """Implements Radio's interface, failing closed on missing driver metadata.

    diagnostic_only=True is an explicit bench mode, never a production enable
    switch: neither normalized timestamp reader can return guessed associations.
    Sends require poll(). There are no sleeping loops, worker callbacks or shell
    commands in the scheduler. First TX waits prepare_s after each off/on phase
    (bench uses 0.5 s each). Diagnostic pacing is independent of the R2 lease.
    It does not prove that asynchronous MCU control satisfies Ready.
    """
    can_range = False

    def __init__(self, config, link_probe, *, diagnostic_only=False, io=None,
                 clock=time.monotonic, socket_factory=socket.socket, lock=None):
        """Bind injected I/O and initialize unowned diagnostic state."""
        self.config, self.link_probe = config, link_probe
        self.diagnostic_only, self.io, self.clock = diagnostic_only, io or FileIO(), clock
        self.socket_factory = socket_factory
        self.lock = lock or DeviceLock(config.lock_root / f"manet-ranging-{config.device}.lock")
        self.root = config.debug_root / config.phy / "mt76"
        self.capture = TraceCapture(config, self.io)
        self.session_id, self.role, self.link = None, None, None
        self.saved = {}
        self.owned = False
        self.cleanup_failed = False
        self._disarmed = True
        self.socket = None
        self.pending = deque()
        self.events = deque(maxlen=16)
        self.phase = None
        self.next_send = self.deadline = 0.0
        self.schedule_us, self.sent_count, self.burst_start = (), 0, 0.0
        self._last_now = -math.inf

    def _read(self, name):
        """Read a stripped value from the configured mt76 debugfs path."""
        return self.io.read(self.root / name).strip()

    def _write(self, name, value):
        """Write a newline-terminated value to an mt76 control."""
        self.io.write(self.root / name, str(value) + "\n")

    def _time(self):
        """Require finite monotonic time for lease and pacing deadlines."""
        now = self.clock()
        if not math.isfinite(now) or now < self._last_now:
            raise RadioError("monotonic clock failed")
        self._last_now = now
        return now

    def _ctrl(self, enable):
        """Apply the fixed TMR control profile for the current role."""
        self._write("tmr_ctrl", f"{int(enable)} {CTRL_RESP if self.role == 'responder' else CTRL_INIT}")

    def _begin(self, peer, sid, width, role):
        """Validate and snapshot diagnostic state before enabling capture."""
        # Association must be proven before production can touch controls.
        if not self.diagnostic_only:
            raise AssociationUnavailable(ASSOCIATION_GAP)
        if self.owned or self.role is not None:
            raise RadioError("radio session or incomplete cleanup already active")
        if not isinstance(sid, bytes) or len(sid) != 16 or not any(sid):
            raise ValueError("invalid session id")
        if width != 20:
            raise RadioError("only the measured 20 MHz probe profile is implemented")
        link = self.link_probe(peer)
        if link.ifindex <= 0 or link.width_mhz != width or link.peer_mac != peer.mac:
            raise RadioError("link does not match the requested peer/width")
        _link_local(link.local_address)
        _link_local(link.peer_address)
        now = self._time()
        self.lock.acquire()
        self.owned = True
        try:
            registers = self._read("tmr_registers")
            if not registers.startswith("chip=0x7916 band=1\n"):
                raise RadioError("MT7916 band 1 required")
            if not self._read("tmr_peer").startswith("off "):
                raise RadioError("another TMR user has enabled peer marking")
            ack = self._read("tmr_ack_spe")
            # Readback needs the default marker and four response SPE values.
            if not re.fullmatch(r"default(?: \d+){4}", ack):
                raise RadioError("another user owns the saved ACK settings")
            spe = int(self._read("tmr_spe"), 0)
            mark, rate = int(self._read("tmr_mark"), 0), int(self._read("tmr_rate"), 0)
            # Match the driver's SPE range and permitted tmr_rate bit mask.
            if not -1 <= spe <= 31 or not 0 <= mark <= 0xFFFFFFFF or rate & ~0xFFFFFC0F:
                raise RadioError("unexpected driver settings")
            # No cleanup writes until this snapshot proves baseline ownership.
            self.saved = {"tmr_spe": "off" if spe == -1 else str(spe),
                          "tmr_mark": str(mark), "tmr_rate": str(rate), "ack": ack}
            self.session_id, self.role, self.link = sid, role, link
            self._disarmed = False
            self.deadline = now + self.config.lease_s
            self._write("tmr_peer", "off")
            self._ctrl(False)
            self._write("tmr_mark", hex(self.config.mark))
            # SPE 0 and rate 0 select the driver's fixed 20 MHz probe profile.
            self._write("tmr_spe", "0")
            self._write("tmr_rate", "0")
            if role == "responder":
                # Fixed register only: normal WM report routing.
                self.saved["regidx"] = self._read("regidx")
                self._write("regidx", hex(NORMAL_ROUTE))
                self.saved["route"] = self._read("regval")
                self._write("regval", "0xc003")
                self._write("regidx", self.saved["regidx"])
                self._write("tmr_ack_spe", "0")
            # Capture starts before responder enable or initiator preparation.
            self.capture.start(sid)
            if role == "responder":
                self._ctrl(True)
            else:
                self.phase = "enable"
                self.next_send = now + self.config.prepare_s
        except Exception:
            self._cleanup_after_error()
            raise

    def arm_responder(self, peer, session_id, width_mhz):
        # tmr_peer is TX-only. CTRL_RESP sets category=0, filters 0x22/2:
        # QoS Data/Data, not a peer or a probe. TIMING_MEASURE is a local TXD
        # bit, not an over-air marker this responder can filter on.
        """Start responder diagnostics with the fixed capture profile."""
        self._begin(peer, session_id, width_mhz, "responder")

    def send_burst(self, peer, frames, width_mhz, interval_ms):
        """Validate and queue marked IPv6 probes within the helper lease."""
        frames = tuple(frames)
        if not 1 <= len(frames) <= ranging.MAX_FRAMES or not 20 <= interval_ms <= 100:
            raise ValueError("invalid burst bounds")
        messages = [ranging.decode(frame) for frame in frames]
        sid, challenge = messages[0].session_id, messages[0].challenge
        if len(challenge) != 16 or not any(challenge):
            raise ValueError("burst needs responder challenge")
        for seq, message in enumerate(messages):
            if (message.WhichOneof("body") != "burst" or message.session_id != sid or
                    message.challenge != challenge or message.burst.sequence != seq or
                    message.burst.done != (seq == len(frames) - 1)):
                raise ValueError("inconsistent burst")
        schedule = ranging.burst_schedule_us(sid, challenge, len(frames), interval_ms)
        # Both off/on settling periods and the last-send margin fit the lease.
        if (2 * self.config.prepare_s + schedule[-1] / 1e6 +
                self.config.max_send_lateness_s >= self.config.lease_s):
            raise ValueError("burst exceeds helper lease")
        self._begin(peer, sid, width_mhz, "initiator")
        try:
            self.socket = self.socket_factory(socket.AF_INET6, socket.SOCK_DGRAM, 0)
            self.socket.setblocking(False)
            # Interface binding, link scope and hop limit constrain egress.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                                   self.config.interface.encode("ascii") + b"\0")
            # skb mark and peer MAC select TX timing in the patched driver.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, self.config.mark)
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_UNICAST_HOPS, 1)
            self.socket.bind((self.link.local_address, 0, 0, self.link.ifindex))
            self._write("tmr_peer", peer.mac.hex(":"))
            self.pending.extend(frames)
            self.schedule_us, self.sent_count = schedule, 0
        except Exception:
            self._cleanup_after_error()
            raise

    def poll(self):
        """Drive diagnostics. Keep polling even if the client vanishes."""
        if self.role is not None:
            try:
                now = self._time()
                if now >= self.deadline:
                    raise RadioError("helper radio lease expired")
                if self.phase and now >= self.next_send:
                    if self.phase == "enable":
                        self._ctrl(True)
                        self.phase = "send"
                        self.burst_start = self.next_send = now + self.config.prepare_s
                    elif self.pending:
                        if now - self.next_send > self.config.max_send_lateness_s:
                            raise RadioError("probe scheduler missed its pacing deadline")
                        frame = self.pending[0]
                        destination = (self.link.peer_address, self.config.port, 0, self.link.ifindex)
                        if self.socket.sendto(frame, destination) != len(frame):
                            raise RadioError("short UDP send")
                        self.pending.popleft()
                        self.sent_count += 1
                        # Absolute offsets prevent accumulated polling delay
                        # from extending the R2 lease or flattening the jitter.
                        if self.pending:
                            self.next_send = self.burst_start + self.schedule_us[self.sent_count] / 1e6
                        if not self.pending:
                            self.phase = None
                            self.events.append(Event("burst_finished", self.session_id))
            except Exception as exc:
                self.abort(str(exc))
        events = tuple(self.events)
        self.events.clear()
        return events

    def read_raw_reports(self, session_id):
        """Freeze trace capture and return raw diagnostics, never R2 samples."""
        if self.role is None or session_id != self.session_id or self.pending:
            raise CaptureInvalid("wrong/incomplete session")
        return self.capture.read()

    def read_initiator_timestamps(self, session_id):
        """Freeze initiator diagnostics and refuse unproven associations."""
        if self.role != "initiator":
            raise CaptureInvalid("not an initiator session")
        self.read_raw_reports(session_id)
        raise AssociationUnavailable(ASSOCIATION_GAP)

    def read_responder_report(self, session_id):
        """Freeze responder diagnostics and refuse unproven associations."""
        if self.role != "responder":
            raise CaptureInvalid("not a responder session")
        self.read_raw_reports(session_id)
        raise AssociationUnavailable(ASSOCIATION_GAP)

    def disarm(self):
        """Cancel probes, restore saved controls and close trace capture."""
        self.pending.clear()
        self.schedule_us, self.sent_count = (), 0
        self.phase = None
        errors = []
        if self.socket is not None:
            try:
                self.socket.close()
                self.socket = None
            except Exception as exc:
                errors.append(str(exc))
        if self.saved:
            # Clear peer marking and disable TMR before restoring TX settings.
            actions = [("tmr_peer", "off"), ("tmr_ctrl", f"0 {CTRL_RESP if self.role == 'responder' else CTRL_INIT}")]
            actions.extend((key, self.saved[key]) for key in ("tmr_mark", "tmr_spe", "tmr_rate"))
            for name, value in actions:
                try:
                    self._write(name, value)
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
            if "route" in self.saved:
                try:
                    self._write("regidx", hex(NORMAL_ROUTE))
                    # Dependent writes: never write regval if selecting its
                    # target failed, or a different register could be damaged.
                    self._write("regval", self.saved["route"])
                except Exception as exc:
                    errors.append(f"route: {exc}")
            if "regidx" in self.saved:
                try:
                    self._write("regidx", self.saved["regidx"])
                except Exception as exc:
                    errors.append(f"regidx: {exc}")
        try:
            self.capture.close()
        except Exception as exc:
            errors.append(str(exc))
        self.cleanup_failed = bool(errors)
        self._disarmed = not errors
        if errors:
            raise RadioError("disarm: " + "; ".join(errors))

    def restore_ack(self):
        """Verify restored ACK state before releasing a clean device."""
        if not self.owned:
            return
        if self.saved:
            self._write("tmr_ack_spe", "restore")
            if self._read("tmr_ack_spe") != self.saved["ack"]:
                self.cleanup_failed = True
                raise RadioError("ACK restoration readback mismatch")
        # Retain ownership after any failure so another session cannot start.
        if self._disarmed and not self.cleanup_failed:
            self.saved.clear()
            self.role = None
            self.owned = False
            self.lock.release()

    def _cleanup_after_error(self):
        """Try both cleanup stages and collect their error messages."""
        failures = []
        for operation in (self.disarm, self.restore_ack):
            try:
                operation()
            except Exception as exc:
                failures.append(str(exc))
        return failures

    def abort(self, detail):
        """Attempt cleanup and queue a failure for the current session."""
        sid = self.session_id
        failures = self._cleanup_after_error()
        if sid is not None:
            self.events.append(Event("radio_error", sid, "; ".join([detail] + failures)))

    def close(self):
        """Attempt both cleanup stages and report any remaining failures."""
        failures = self._cleanup_after_error()
        if failures:
            raise RadioError("; ".join(failures))
