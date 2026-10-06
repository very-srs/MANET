"""
GNSS samples from gpsd JSON, and the anomaly hints that start spoof checks.

gps-reader.py feeds every gpsd message through GnssReader. Each receiver
(gpsd device) has its own ReceiverState, so two receivers never share a
fix, signal summary or clock baseline. Times called mono are arrival times
on CLOCK_MONOTONIC_RAW: chrony slews CLOCK_MONOTONIC toward the time it
serves, which may come from this same receiver, so only the raw clock is
independent of it. Raw time does not count suspend; these nodes do not
suspend.

gpsd may send several TPV reports for one measurement epoch, some without
every field. Reports with the same GNSS time are merged into one sample. A
report that claims a fix but lacks a position is incomplete, not a lost
fix. Only a new epoch renews a fix's freshness, so a replayed report does
not keep an old position looking current.

Cold start is normal (Mike, 2026-10-06). Until a receiver's first fix has
held for SETTLE_S, signal and clock anomalies are not raised and the clock
baseline is not set. Jamming evidence is the exception, but stock gpsd 3.25
on Debian 13 does not report jam, so that path stays quiet there.

Anomalies are hints, never a verdict. They trigger range checks and are
compared across nodes: a spoofer or jammer hits every receiver in its
footprint at once, local multipath hits one.
"""

from collections import deque
from datetime import datetime, timedelta
import math

# Clock: GNSS time minus raw arrival time ("offset") should hold steady. A
# step is a jump of more than CLOCK_STEP_S between consecutive samples that
# then holds for STEP_HOLD_S. A delivery stall or backlog shifts arrivals
# and then recovers, so it cancels instead.
CLOCK_STEP_S = 1.0
STEP_HOLD_S = 10.0
# Drift compares the median offset over DRIFT_WINDOW_S, so a few late
# samples cannot move it.
DRIFT_WINDOW_S = 30.0
# Slow drift of the offset since the settled baseline, beyond what was
# reported as steps. A CM4 crystal is within about 50 ppm; 200 leaves room.
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
# gpsd's jam is 0 (none) to 255 (severe); -1 or absent means unknown.
# Uncalibrated: needs receiver evidence.
JAM_HIGH = 128
HISTORY_S = 600.0
# After a clock step, drift or discontinuity, this receiver's time is not
# fit to serve for this long, whatever its position does.
TIME_DOUBT_S = 600.0
TIME_EVENTS = ("clock_step", "clock_drift", "time_discontinuity")
# Consecutive position reports with already-seen epochs before they are
# taken as a receiver clock going back rather than a replay.
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
    """Great-circle distance; plenty for jump detection."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def sky_summary(msg, mono):
    """Used-signal count and strength statistics from a SKY message."""
    sats = [s for s in (msg.get("satellites") or []) if isinstance(s, dict)]
    used = [s for s in sats if s.get("used")]
    ss = [v for v in (_num(s.get("ss")) for s in used) if v is not None and v > 0]
    mean = sum(ss) / len(ss) if ss else None
    sd = math.sqrt(sum((v - mean) ** 2 for v in ss) / len(ss)) if len(ss) >= 2 else None
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
        self.device = device
        self.events = events          # shared with the reader, tagged by device
        self.sky = None
        self.last = None              # latest sample, fix or not
        self.last_fix = None          # latest sample with a position
        self.fix_live = False
        self.fix_since = None         # raw time the current fix began
        self.ever_settled = False     # a fix has held SETTLE_S this session
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
        self.events.append({"device": self.device, "kind": kind, "mono": round(mono, 3),
                            "value": value, "detail": detail})

    def on_sky(self, msg, mono):
        summary = sky_summary(msg, mono)
        prev, self.sky = self.sky, summary
        if not self.settled(mono) or prev is None or mono - prev["mono"] > SKY_STALE_S:
            return
        if prev["ss_mean"] is not None and summary["ss_mean"] is not None \
                and min(prev["ss_count"], summary["ss_count"]) >= 4 \
                and abs(summary["ss_mean"] - prev["ss_mean"]) >= SS_STEP_DB:
            step = round(summary["ss_mean"] - prev["ss_mean"], 1)
            self._event("ss_step", mono, f"{prev['ss_mean']}->{summary['ss_mean']} dB-Hz", step)
        uniform = summary["ss_sd"] is not None and summary["ss_count"] >= SS_MIN_SATS \
            and summary["ss_sd"] < SS_UNIFORM_SD_DB
        was_uniform = prev["ss_sd"] is not None and prev["ss_count"] >= SS_MIN_SATS \
            and prev["ss_sd"] < SS_UNIFORM_SD_DB
        if uniform and not was_uniform:
            self._event("ss_uniform", mono, f"sd {summary['ss_sd']} dB over {summary['ss_count']}",
                        summary["ss_sd"])
        if prev["sats_used"] and summary["sats_used"] is not None \
                and prev["sats_used"] >= 4 and summary["sats_used"] <= prev["sats_used"] // 2:
            self._event("sat_drop", mono, f"{prev['sats_used']}->{summary['sats_used']}",
                        summary["sats_used"])

    def on_tpv(self, msg, mono):
        """Merge one TPV report. Returns the sample it produced or updated, or None."""
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
            # An epoch already seen: a replay, or the receiver's clock went
            # back. After a clock correction the old-looking times keep
            # advancing; a replay of one or two epochs repeats or alternates.
            # REPLAY_RUN advancing reports are accepted as a discontinuity.
            # A replay of an advancing recording cannot be told apart from a
            # correction this way (codex-007), so the discontinuity makes the
            # receiver's time unfit to serve (TIME_DOUBT_S); the position
            # monitor, not this check, guards the position.
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
        if self.fix_live and self.last_fix is not None \
                and mono - self.last_fix["mono"] > FIX_STALE_S:
            self.lose(mono, "silence")

    def _fields(self, msg, mode):
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
        self.seq += 1
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
        sample.update(self._fields(msg, mode) if lat is not None else
                      dict.fromkeys(("alt_hae", "alt_msl", "alt_legacy", "eph", "epv",
                                     "speed", "track")))
        return sample

    def _check_jam(self, jam, mono):
        high = jam is not None and jam >= JAM_HIGH
        if high and not self.jam_high:
            self._event("jam", mono, str(jam), jam)
        self.jam_high = high

    def _check_clock(self, cur):
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

        window = sorted(o for _, o in self.offsets)
        ref = window[len(window) // 2]
        if self.pending_step is not None:
            since, before, after = self.pending_step
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
        allowed = CLOCK_STEP_S + CLOCK_DRIFT_PPM * 1e-6 * (mono - base_mono)
        drift = ref - base_offset - self.clock_steps
        drifting = abs(drift) > allowed
        if drifting and not self.clock_drifting:
            self._event("clock_drift", mono, f"{drift:+.3f} s since baseline", round(drift, 3))
        self.clock_drifting = drifting

    def _check_jump(self, cur):
        last = self.last_fix
        if last is None or not self.fix_live:
            return
        if cur["gnss_time"] is not None and last["gnss_time"] is not None:
            dt = cur["gnss_time"] - last["gnss_time"]
        else:
            dt = cur["mono"] - last["mono"]
        d = distance_m(last["lat"], last["lon"], cur["lat"], cur["lon"])
        if d >= JUMP_MIN_M and d > MAX_SPEED_MS * max(dt, 1.0):
            self._event("jump", cur["mono"], f"{d:.0f} m in {dt:.1f} s", round(d, 1))


class GnssReader:
    """All receivers on one gpsd, and the choice of which one is primary.

    The primary is the receiver with a live fix, preferring the current
    primary; with no live fix, the most recent receiver to have had one.
    """

    def __init__(self, first_fix_mono=None, events_len=50):
        self.receivers = {}
        self.devices = set()        # devices gpsd reports as present
        self.devices_known = False  # a DEVICES message has been seen
        self.events = deque(maxlen=events_len)
        self.boot_first_fix = first_fix_mono
        self.primary = None

    def receiver(self, device):
        if device not in self.receivers:
            self.receivers[device] = ReceiverState(device, self.events)
        return self.receivers[device]

    def feed(self, msg, mono):
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
            # DEVICE names its receiver in path (omitted with one receiver);
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
        for rx in self.receivers.values():
            rx.expire(mono)
        live = [rx for rx in self.receivers.values() if rx.fix_live]
        current = self.receivers.get(self.primary)
        if current is not None and current.fix_live:
            return current
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
        for rx in self.receivers.values():
            rx.lose(mono, reason)
