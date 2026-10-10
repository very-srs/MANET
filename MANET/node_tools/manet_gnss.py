"""Read per-device gpsd samples and produce anomaly hints for range checks.

gps-reader.py feeds each message to GnssReader. Each ReceiverState owns its
fix, signal summary and clock baseline. Arrival mono times use
CLOCK_MONOTONIC_RAW. CLOCK_MONOTONIC follows chrony's potentially GNSS-derived
slews, so it cannot independently check receiver time. Raw time excludes
suspend, which these nodes do not use.
TPV reports with the same GNSS epoch merge. Missing positions are incomplete
reports. Only a new epoch refreshes a fix, so replays cannot keep it live.
The first fix must hold SETTLE_S before signal/clock checks or a baseline.
Jamming bypasses settling, but Debian 13 gpsd 3.25 does not report jam.
Anomalies trigger checks, not verdicts. Shared anomalies suggest a common
spoofer/jammer footprint, while local multipath affects one receiver.
"""

from collections import deque
from datetime import datetime, timedelta
import math

# Offset is GNSS time minus raw arrival time. A step exceeds CLOCK_STEP_S
# and holds for STEP_HOLD_S. Arrival shifts from stalls/backlogs cancel
# when the offset recovers.
CLOCK_STEP_S = 1.0
STEP_HOLD_S = 10.0
# Drift compares the median offset over DRIFT_WINDOW_S, so a few late
# samples cannot move it.
DRIFT_WINDOW_S = 30.0
# Slow drift of the offset since the settled baseline, beyond what was
# reported as steps. A CM4 crystal is within about 50 ppm. The limit is 200 ppm.
CLOCK_DRIFT_PPM = 200.0
SETTLE_S = 30.0            # a first fix must hold this long before anomalies
FIX_STALE_S = 5.0          # no new epoch for this long ends the fix
SKY_STALE_S = 5.0          # an older signal summary is not attached
# A position change faster than any vehicle carrying a radio.
MAX_SPEED_MS = 70.0
JUMP_MIN_M = 50.0
# Mean signal strength of used signals changing this much in one report.
SS_STEP_DB = 6.0
# Counterfeit signals from one transmitter tend to arrive at similar
# strengths. A heuristic until baseline recordings exist.
SS_UNIFORM_SD_DB = 1.0
SS_MIN_SATS = 6
# gpsd's jam is 0 (none) to 255 (severe). -1 or absent means unknown.
# Uncalibrated: needs receiver evidence.
JAM_HIGH = 128
HISTORY_S = 600.0
# Seconds to withhold receiver time after a step, drift or discontinuity,
# independent of position.
TIME_DOUBT_S = 600.0
TIME_EVENTS = ("clock_step", "clock_drift", "time_discontinuity")
# Advancing reports with already-seen epochs indicating a discontinuity.
REPLAY_RUN = 3

EARTH_RADIUS_M = 6371008.8


def parse_gnss_time(value):
    """Return gpsd's ISO 8601 UTC time as epoch seconds, or None.

    A leap second (:60) is read as the following second.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    leap = len(text) > 18 and text[17:19] == "60"
    if leap:
        text = text[:17] + "59" + text[19:]
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (t + timedelta(seconds=1) if leap else t).timestamp()


def _num(value):
    """A finite float, or None for absent and non-numeric values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def distance_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres for jump detection."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def sky_summary(msg, mono):
    """Used-signal count and strength statistics from a SKY message."""
    sats = [s for s in (msg.get("satellites") or []) if isinstance(s, dict)]
    used = [s for s in sats if s.get("used")]
    # Exclude unused and absent strengths from the dB-Hz distribution.
    ss = [v for v in (_num(s.get("ss")) for s in used) if v is not None and v > 0]
    mean = sum(ss) / len(ss) if ss else None
    sd = math.sqrt(sum((v - mean) ** 2 for v in ss) / len(ss)) if len(ss) >= 2 else None
    # Prefer gpsd's used-satellite count over the detailed signal list.
    n_used = _num(msg.get("uSat"))
    return {
        "mono": round(mono, 3),
        "sats_used": int(n_used) if n_used is not None else (len(used) if sats else None),
        "sats_seen": len(sats) if sats else None,
        "ss_mean": round(mean, 1) if mean is not None else None,
        "ss_sd": round(sd, 2) if sd is not None else None,
        "ss_count": len(ss),
        "hdop": _num(msg.get("hdop")),
    }


class ReceiverState:
    """Latest sample, history and anomaly events for one gpsd device."""

    def __init__(self, device, events, first_fix_mono=None):
        """Initialize one receiver with the shared anomaly queue."""
        self.device = device
        self.events = events          # shared with the reader, tagged by device
        self.sky = None
        self.last = None              # latest sample, fix or not
        self.last_fix = None          # latest sample with a position
        self.fix_live = False
        self.fix_since = None         # raw time the current fix began
        self.ever_settled = False     # a fix has held SETTLE_S this session
        # Replay detection retains the latest 64 accepted GNSS time tags.
        self.recent_epochs = deque(maxlen=64)
        self.replay_run = 0
        self.replay_last = None
        self.first_fix_mono = first_fix_mono
        self.offsets = deque()        # (mono, offset) over DRIFT_WINDOW_S
        self.pending_step = None      # (mono, offset before, offset after)
        self.clock_base = None        # (offset, mono) once settled
        self.clock_steps = 0.0
        self.clock_drifting = False
        self.jam_high = False
        self.seq = 0
        self.history = deque()

    def time_ok(self, mono):
        """Settled, and no clock step, drift or discontinuity for TIME_DOUBT_S."""
        return self.settled(mono) and not any(
            e["device"] == self.device and e["kind"] in TIME_EVENTS
            and mono - e["mono"] <= TIME_DOUBT_S for e in self.events)

    def settled(self, mono):
        """A fix has held without a break for SETTLE_S at some point."""
        if not self.ever_settled and self.fix_live and self.fix_since is not None \
                and mono - self.fix_since >= SETTLE_S:
            self.ever_settled = True
        return self.ever_settled

    def _event(self, kind, mono, detail="", value=None):
        """Append a device-tagged anomaly using raw monotonic seconds."""
        self.events.append({"device": self.device, "kind": kind, "mono": round(mono, 3),
                            "value": value, "detail": detail})

    def on_sky(self, msg, mono):
        """Update signal statistics and flag changes after settling."""
        summary = sky_summary(msg, mono)
        prev, self.sky = self.sky, summary
        if not self.settled(mono) or prev is None or mono - prev["mono"] > SKY_STALE_S:
            return
        # Compare strength steps with at least four signals per report.
        if prev["ss_mean"] is not None and summary["ss_mean"] is not None \
                and min(prev["ss_count"], summary["ss_count"]) >= 4 \
                and abs(summary["ss_mean"] - prev["ss_mean"]) >= SS_STEP_DB:
            step = round(summary["ss_mean"] - prev["ss_mean"], 1)
            self._event("ss_step", mono, f"{prev['ss_mean']}->{summary['ss_mean']} dB-Hz", step)
        uniform = summary["ss_sd"] is not None and summary["ss_count"] >= SS_MIN_SATS \
            and summary["ss_sd"] < SS_UNIFORM_SD_DB
        was_uniform = prev["ss_sd"] is not None and prev["ss_count"] >= SS_MIN_SATS \
            and prev["ss_sd"] < SS_UNIFORM_SD_DB
        # Emit one event on entering uniform strength, not on every report.
        if uniform and not was_uniform:
            self._event("ss_uniform", mono, f"sd {summary['ss_sd']} dB over {summary['ss_count']}",
                        summary["ss_sd"])
        # Losing at least half of four or more used satellites is an event.
        if prev["sats_used"] and summary["sats_used"] is not None \
                and prev["sats_used"] >= 4 and summary["sats_used"] <= prev["sats_used"] // 2:
            self._event("sat_drop", mono, f"{prev['sats_used']}->{summary['sats_used']}",
                        summary["sats_used"])

    def on_tpv(self, msg, mono):
        """Merge a TPV report and return its sample, or None."""
        mode = msg.get("mode") if isinstance(msg.get("mode"), int) else 0
        gtime = parse_gnss_time(msg.get("time"))
        lat, lon = _num(msg.get("lat")), _num(msg.get("lon"))
        has_pos = lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180
        jam = _num(msg.get("jam"))
        jam = int(jam) if jam is not None and jam >= 0 else None
        self._check_jam(jam, mono)
        self.expire(mono)  # a gap before this report ends the previous fix first

        if mode < 2:
            # Loss is a fact about the receiver, whatever its time tag says.
            if self.fix_live:
                self.lose(mono, "receiver")
            sample = self._sample(mono, gtime, 1, None, None, msg, jam)
            self.last = sample
            return sample

        same_epoch = self.fix_live and self.last_fix is not None and gtime is not None \
            and self.last_fix["gnss_time"] == gtime
        # Revise fields in place without renewing arrival time or sequence.
        if same_epoch and mode >= 2:
            merged = self.last_fix
            for key, val in self._fields(msg, mode).items():
                if val is not None:
                    merged[key] = val
            if has_pos:
                merged["lat"], merged["lon"] = lat, lon
            merged["mode"] = max(merged["mode"], mode)
            return merged
        if not has_pos:
            return None  # incomplete report, and no epoch to merge it into
        if gtime is not None and gtime in self.recent_epochs:
            # REPLAY_RUN advancing old epochs indicate a clock discontinuity.
            # A recording replay can look the same, so quarantine time for
            # TIME_DOUBT_S and leave position checks to the position monitor.
            advancing = self.replay_last is None or gtime > self.replay_last
            self.replay_run = self.replay_run + 1 if advancing else 0
            self.replay_last = gtime
            if self.replay_run < REPLAY_RUN:
                return None
            self._event("time_discontinuity", mono, f"{REPLAY_RUN} reports with past epochs")
            self.recent_epochs.clear()
        self.replay_run = 0
        self.replay_last = None

        sample = self._sample(mono, gtime, mode, lat, lon, msg, jam)
        if gtime is not None:
            self.recent_epochs.append(gtime)
        if not self.fix_live:
            self.fix_since = mono
            if self.first_fix_mono is None:
                self.first_fix_mono = mono
            else:
                self._event("fix_regained", mono)
            self.fix_live = True
        self._check_clock(sample)
        self._check_jump(sample)
        self.last = self.last_fix = sample
        self.history.append(sample)
        while self.history and mono - self.history[0]["mono"] > HISTORY_S:
            self.history.popleft()
        return sample

    def new_session(self):
        """Settling, clock state and epochs belong to one gpsd session."""
        self.lose(self.last_fix["mono"] if self.last_fix else 0.0, "gpsd session ended") \
            if self.fix_live else None
        self.fix_since = None
        self.ever_settled = False
        self.recent_epochs.clear()
        self.replay_run = 0
        self.replay_last = None
        self.offsets.clear()
        self.pending_step = None
        self.clock_base = None
        self.clock_steps = 0.0
        self.clock_drifting = False

    def lose(self, mono, reason):
        """End the current fix: the receiver said so, or it went quiet."""
        if self.fix_live:
            self.fix_live = False
            self.fix_since = None
            self._event("fix_lost", mono, reason)

    def expire(self, mono):
        """End a fix whose latest new epoch exceeds the stale limit."""
        if self.fix_live and self.last_fix is not None \
                and mono - self.last_fix["mono"] > FIX_STALE_S:
            self.lose(mono, "silence")

    def _fields(self, msg, mode):
        """Extract numeric TPV fields, allowing heights only in 3D."""
        three_d = mode >= 3
        return {
            "alt_hae": _num(msg.get("altHAE")) if three_d else None,
            "alt_msl": _num(msg.get("altMSL")) if three_d else None,
            "alt_legacy": _num(msg.get("alt")) if three_d else None,
            "eph": _num(msg.get("eph")),
            "epv": _num(msg.get("epv")),
            "speed": _num(msg.get("speed")),
            "track": _num(msg.get("track")),
        }

    def _sample(self, mono, gtime, mode, lat, lon, msg, jam):
        """Build a sequenced TPV sample with available fresh SKY fields."""
        self.seq += 1
        # SKY data older than five seconds cannot describe this sample.
        sky = self.sky if self.sky and mono - self.sky["mono"] <= SKY_STALE_S else {}
        sample = {
            "device": self.device,
            "seq": self.seq,
            "mono": round(mono, 3),
            "gnss_time": gtime,
            "mode": mode,
            "lat": lat,
            "lon": lon,
            "jam": jam,
            "sats_used": sky.get("sats_used"),
            "ss_mean": sky.get("ss_mean"),
            "ss_sd": sky.get("ss_sd"),
            "hdop": sky.get("hdop") if sky.get("hdop") is not None else _num(msg.get("hdop")),
            "sky_age": round(mono - sky["mono"], 3) if sky else None,
        }
        # No-position reports clear positional fields even if TPV carries them.
        sample.update(self._fields(msg, mode) if lat is not None else
                      dict.fromkeys(("alt_hae", "alt_msl", "alt_legacy", "eph", "epv",
                                     "speed", "track")))
        return sample

    def _check_jam(self, jam, mono):
        """Emit a jamming hint when the receiver first reports high."""
        high = jam is not None and jam >= JAM_HIGH
        if high and not self.jam_high:
            self._event("jam", mono, str(jam), jam)
        self.jam_high = high

    def _check_clock(self, cur):
        """Detect sustained offset steps and baseline drift."""
        t, mono = cur["gnss_time"], cur["mono"]
        if t is None:
            return
        offset = t - mono
        self.offsets.append((mono, offset))
        while mono - self.offsets[0][0] > DRIFT_WINDOW_S:
            self.offsets.popleft()
        if not self.settled(mono):
            self.offsets.clear()
            self.offsets.append((mono, offset))
            return
        if self.clock_base is None:
            self.clock_base = (offset, mono)
            return

        # The upper median rejects a minority of delayed arrivals.
        window = sorted(o for _, o in self.offsets)
        ref = window[len(window) // 2]
        if self.pending_step is not None:
            since, before, after = self.pending_step
            # A candidate step must stay within 0.5 s for the hold interval.
            if abs(offset - after) > CLOCK_STEP_S / 2:
                self.pending_step = None  # it came back or moved on: not a step
            elif mono - since >= STEP_HOLD_S:
                step = after - before
                self._event("clock_step", mono, f"{step:+.3f} s", round(step, 3))
                self.clock_steps += step
                self.pending_step = None
                self.offsets.clear()
                self.offsets.append((mono, offset))
                return  # the window starts again at the new offset
        if self.pending_step is None and abs(offset - ref) > CLOCK_STEP_S:
            self.pending_step = (mono, ref, offset)
        if self.pending_step is not None:
            return

        base_offset, base_mono = self.clock_base
        # Drift allowance grows in seconds from ppm elapsed since baseline.
        # Subtract confirmed steps so they do not also count as drift.
        allowed = CLOCK_STEP_S + CLOCK_DRIFT_PPM * 1e-6 * (mono - base_mono)
        drift = ref - base_offset - self.clock_steps
        drifting = abs(drift) > allowed
        if drifting and not self.clock_drifting:
            self._event("clock_drift", mono, f"{drift:+.3f} s since baseline", round(drift, 3))
        self.clock_drifting = drifting

    def _check_jump(self, cur):
        """Flag displacement beyond the distance and speed limits."""
        last = self.last_fix
        if last is None or not self.fix_live:
            return
        # Receiver time avoids false speed from bunched report arrivals.
        if cur["gnss_time"] is not None and last["gnss_time"] is not None:
            dt = cur["gnss_time"] - last["gnss_time"]
        else:
            dt = cur["mono"] - last["mono"]
        d = distance_m(last["lat"], last["lon"], cur["lat"], cur["lon"])
        # A one-second floor bounds the speed test for tiny or reversed deltas.
        if d >= JUMP_MIN_M and d > MAX_SPEED_MS * max(dt, 1.0):
            self._event("jump", cur["mono"], f"{d:.0f} m in {dt:.1f} s", round(d, 1))


class GnssReader:
    """Track gpsd receivers and choose a primary.

    Prefer the current primary with a live fix, then another live fix. With no
    live fix, choose the receiver whose fix is most recent.
    """

    def __init__(self, first_fix_mono=None, events_len=50):
        """Initialize device tracking and the bounded anomaly queue."""
        self.receivers = {}
        self.devices = set()        # devices gpsd reports as present
        self.devices_known = False  # a DEVICES message has been seen
        self.events = deque(maxlen=events_len)
        self.boot_first_fix = first_fix_mono
        self.primary = None

    def receiver(self, device):
        """Return or create the state for a receiver identity."""
        if device not in self.receivers:
            self.receivers[device] = ReceiverState(device, self.events)
        return self.receivers[device]

    def feed(self, msg, mono):
        """Route gpsd messages and return samples from TPV reports."""
        cls = msg.get("class")
        device = self._resolve(msg.get("device") if isinstance(msg.get("device"), str) else "")
        if cls == "DEVICES":
            self.devices_known = True
            self.devices = {d.get("path") for d in msg.get("devices") or []
                            if isinstance(d, dict) and isinstance(d.get("path"), str)}
            if len(self.devices) == 1:
                self._adopt_anonymous(next(iter(self.devices)))
            for path, rx in self.receivers.items():
                if path and path not in self.devices:
                    rx.lose(mono, "device removed")
        elif cls == "DEVICE" and self._resolve(msg.get("path") if isinstance(msg.get("path"), str) else ""):
            # DEVICE names its receiver in path (omitted with one receiver).
            # TPV and SKY use device.
            path = self._resolve(msg.get("path") if isinstance(msg.get("path"), str) else "")
            self.devices_known = True
            if len(self.devices | {path}) == 1:
                self._adopt_anonymous(path)
            if msg.get("activated") == 0:
                self.devices.discard(path)
                if path in self.receivers:
                    self.receivers[path].lose(mono, "device removed")
            else:
                self.devices.add(path)
        elif cls == "SKY":
            self.receiver(device).on_sky(msg, mono)
        elif cls == "TPV":
            rx = self.receiver(device)
            sample = rx.on_tpv(msg, mono)
            if sample is not None and sample["lat"] is not None and self.boot_first_fix is None:
                self.boot_first_fix = mono
            return sample
        return None

    def tick(self, mono):
        """Expire silent fixes and select the primary receiver."""
        for rx in self.receivers.values():
            rx.expire(mono)
        live = [rx for rx in self.receivers.values() if rx.fix_live]
        current = self.receivers.get(self.primary)
        # Keep a live primary to avoid switching between healthy receivers.
        if current is not None and current.fix_live:
            return current
        # Choose the freshest available fix after losing the current primary.
        if live:
            choice = max(live, key=lambda rx: rx.last_fix["mono"])
        else:
            had = [rx for rx in self.receivers.values() if rx.last_fix is not None]
            choice = max(had, key=lambda rx: rx.last_fix["mono"]) if had else current
        self.primary = choice.device if choice else None
        return choice

    def new_session(self):
        """Device presence and receiver session state end with the session."""
        self.devices = set()
        self.devices_known = False
        for rx in self.receivers.values():
            rx.new_session()

    def _adopt_anonymous(self, path):
        """Give an anonymous receiver's state to the only known device.

        A TPV without device can arrive before gpsd names its device. Once
        exactly one device is known, that state is the device's, so removing
        the device ends its fix.
        """
        anon = self.receivers.get("")
        if anon is None or path in self.receivers:
            return
        anon.device = path
        self.receivers[path] = self.receivers.pop("")
        if self.primary == "":
            self.primary = path

    def _resolve(self, name):
        """An omitted identity is the only known receiver, if there is one."""
        if name:
            return name
        known = self.devices or set(self.receivers) - {""}
        return next(iter(known)) if len(known) == 1 else ""

    def lose_all(self, mono, reason):
        """End every live fix with a shared loss reason."""
        for rx in self.receivers.values():
            rx.lose(mono, reason)
