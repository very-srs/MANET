"""Pure selection and feedback for a radio-fed, tethered ATAK phone.

Only end0-proven, pinned-phone user gestures supply manual locations. Sent
points use creator time; hand-set self SA uses changes while no feed is live.
MANET echoes and internal phone GPS never become independent observations.
Manual positions persist. Position distrust latches until a NEW nearby user
mark confirms fresh, unjammed receiver coordinates. This never clears the
spoof monitor or its time quarantine. No function performs I/O or reads time.
Phone dates are estimated from admitted SA on radio monotonic time. Radio
UTC is used only by outbound_time before that estimate exists.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import math

from manet_cot import (ChatReceipt, ErrorEstimate, Fix, MarkCandidate, PhoneSA, PhoneChat,
                       EXTERNAL_SOURCE_PREFIX, EXTERNAL_POSITION_STALE_S,
                       _bounded, _number, _text, _utc, _usable)
from manet_spoof import FIX_TTL_S, THRESHOLD_M


RECEIPT_MAX_AGE_S = 75.0
FUTURE_TOLERANCE_S = 5.0
MAX_PENDING_RETIREMENTS = 128
RESTORE_MARGIN_M = 10.0
WARNING_READ_TIMEOUT_S = 60.0
PHONE_CLOCK_STEP_TOLERANCE_S = 5.0
PHONE_SA_HISTORY = 256
_GPS_STATES = {"NO_FIX", "UNCHECKED", "RANGE_CONSISTENT",
               "INCONSISTENT_UNATTRIBUTED", "GNSS_SUSPECTED"}
_TRUSTED = {"UNCHECKED", "RANGE_CONSISTENT"}  # usable, not authenticated


@dataclass(frozen=True)
class PhoneClockDiagnostic:
    """Observed phone-clock discontinuity; NEVER a time-authority grant."""
    generation: int
    delta_s: float
    observed_mono: float
    reason: str


@dataclass(frozen=True)
class PhoneUpdate:
    status: str
    pinned_uid: str | None
    received_uid: str
    reappeared: bool = False
    clock_diagnostic: PhoneClockDiagnostic | None = None


@dataclass(frozen=True)
class MarkerUpdate:
    status: str                  # accepted, duplicate, or candidate
    reason: str
    candidate: MarkCandidate


@dataclass(frozen=True)
class PhoneFix:
    fix: Fix
    phone_uid: str
    source: str                  # marker or self_manual
    observation_time: datetime   # original PHONE creator/SA time; see clock_generation
    observation_mono: float
    received_mono: float
    stale: datetime | None       # None: manual locations do not expire
    expires_mono: float | None   # None
    marker_uid: str | None = None
    age_s: float = 0.0
    clock_generation: int = 0


@dataclass(frozen=True)
class RadioGPS:
    """Own receiver observation in this boot's monotonic clock domain.

    fix=None means no receiver fix (including no hardware); it NEVER enables
    phone GPS ingestion. state comes from the spoof monitor; jammed comes
    from the LC76G pin. Neither may be inferred from the selected output.
    """
    fix: Fix | None
    observed_mono: float
    state: str = "UNCHECKED"
    jammed: bool = False
    # Increase for EVERY new failed range check/jam incident, including repeated
    # failures under the same monitor state. It can only revoke position trust.
    fault_epoch: int = 0
    # Fresh ranging/GPS agreement, even while the monitor remains quarantined.
    ranges_agree: bool = False


@dataclass(frozen=True)
class OutboundEvent:
    """Pending service action. IDs are stable until acknowledged/superseded.

    The service supplies a unique GeoChat messageId when acknowledging actual
    transmission. Audio/web acknowledgements mean durable local handoff.
    Event IDs are instance-local; persist state or namespace across restarts.
    """
    event_id: int
    kind: str                   # geochat, audio, web_ui
    code: str
    text: str
    phone_uid: str | None
    active: bool = True         # False clears a web UI warning


@dataclass
class _Warning:
    text: str
    audio: str
    phone_uid: str | None
    raised_mono: float
    chat_event_id: int | None = None
    message_id: str | None = None
    sent_mono: float | None = None
    deadline: float | None = None
    delivered: bool = False
    read: bool = False
    acknowledged: bool = False  # read receipt OR an admitted user gesture


@dataclass(frozen=True)
class LocationSelection:
    location: PhoneFix | None    # selected manual provenance; None when GPS wins
    retire_marker_uids: tuple[str, ...]
    own_gnss_selected: bool
    fix: Fix | None              # the one chosen position for BOTH phone and radio
    phone_present: bool
    gps_state: str
    gps_overridden: bool
    landmark_distance_m: float | None
    position_trusted: bool      # independent of raw monitor/time trust
    outbound_events: tuple[OutboundEvent, ...]


def _distance_m(a, b):
    """Great-circle horizontal displacement, including antimeridian wrap."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlat = lat2 - lat1
    dlon = math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def _fix(point, source, phone_uid, note):
    return Fix(point.lat, point.lon, "manual", hae=point.hae,
               horizontal_error=None if point.ce is None else ErrorEstimate(point.ce, 0.90),
               vertical_error=None if point.le is None else ErrorEstimate(point.le, 0.90),
               remarks=f"from tethered phone ({source}); {note}", phone_uid=phone_uid)


class PhonePosition:
    """Select GPS or the newest manual gesture and produce service actions.

    from_ethernet is bridge-port proof, never an IP/UID guess. Association
    changes require a new instance. Call position_sent only after successful
    external-position transport; its timeout guards self-drag adoption.
    Retain/persist association, manual choice, distrust and pending actions.
    """

    def __init__(self, *, receipt_max_age_s=RECEIPT_MAX_AGE_S,
                 future_tolerance_s=FUTURE_TOLERANCE_S,
                 radio_uid=None, max_pending_retirements=MAX_PENDING_RETIREMENTS,
                 alert_threshold_m=THRESHOLD_M, gps_max_age_s=FIX_TTL_S,
                 restore_margin_m=RESTORE_MARGIN_M,
                 warning_read_timeout_s=WARNING_READ_TIMEOUT_S,
                 feed_timeout_s=EXTERNAL_POSITION_STALE_S,
                 phone_clock_step_tolerance_s=PHONE_CLOCK_STEP_TOLERANCE_S):
        for name, value in (("receipt_max_age_s", receipt_max_age_s),
                            ("alert_threshold_m", alert_threshold_m),
                            ("gps_max_age_s", gps_max_age_s),
                            ("restore_margin_m", restore_margin_m),
                            ("warning_read_timeout_s", warning_read_timeout_s),
                            ("feed_timeout_s", feed_timeout_s),
                            ("phone_clock_step_tolerance_s", phone_clock_step_tolerance_s)):
            value = _number(value, name)
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            setattr(self, name, value)
        if self.restore_margin_m > self.alert_threshold_m:
            raise ValueError("restore margin must not exceed alert threshold")
        self.future_tolerance_s = _number(future_tolerance_s, "future_tolerance_s")
        if self.future_tolerance_s < 0:
            raise ValueError("future_tolerance_s must be nonnegative")
        if type(max_pending_retirements) is not int or max_pending_retirements <= 0:
            raise ValueError("max_pending_retirements must be a positive integer")
        self.max_pending_retirements = max_pending_retirements
        self.radio_uid = None if radio_uid is None else _text(radio_uid, "radio_uid")
        self.pinned_uid = None
        self._clock = None
        self._latest_time = None
        self._phone_ref = None
        self._phone_mono_ref = None
        self._phone_generation = 0
        self._seen_sa_times = {}  # bounded replay history retained through clock steps
        self.clock_diagnostic = None  # latest only; also returned on the rebasing PhoneUpdate
        self._presence = None
        self._manual = None
        self._manual_retired = False
        self._checked_manual_epoch = None
        self._distrusted = False
        self._distrust_since = None
        self._overridden = False
        self._position_confirmed = False
        self._fault_epoch = 0
        self._fault_condition = None
        self._feed_until = None
        self._last_sent_mono = None
        self._last_sent_point = None
        self._last_manual_sa_point = None
        self._last_user_sa_point = None
        self._last_sa_geopointsrc = None
        self._next_event_id = 1
        self._events = {}        # bounded: latest action per (condition, kind)
        self._warnings = {}      # bounded: one per condition, not per packet
        self._retirements = {}

    def _now(self, mono, utc):
        mono = _number(mono, "monotonic time")
        utc = _utc(utc)
        if self._clock is not None and mono < self._clock:
            raise ValueError("monotonic time moved backwards")
        self._clock = mono
        return mono, utc

    def phone_now(self, mono):
        """Estimate the pinned phone's clock, or None until admitted SA exists.

        Unit-rate extrapolation on radio monotonic time, with a maximum phase
        estimate within the step tolerance (least one-way delay). Historical
        mono values are projected into the CURRENT phone-clock generation,
        useful for comparing a new receipt with an earlier local send.
        This observation is for the phone path only, never clock discipline.
        """
        mono = _number(mono, "monotonic time")
        if self._phone_ref is None:
            return None
        return self._phone_ref + timedelta(seconds=mono - self._phone_mono_ref)

    def outbound_time(self, mono, radio_utc):
        """The service's timestamp for ALL CoT sent to this phone.

        Pass this as now to external_position, hidden_contact, geochat_report
        and retire_marker. Before a phone clock exists, use aware radio UTC.
        This neither proves presence nor permits sending to an unpinned peer.
        """
        radio_utc = _utc(radio_utc)
        phone_time = self.phone_now(mono)
        return radio_utc if phone_time is None else phone_time

    def _present(self, mono):
        if self._presence is not None and mono >= self._presence:
            self._presence = None
        return self._presence is not None

    def _admit_sa_clock(self, sa, mono):
        """Return (rejection, phone_now, diagnostic), committing only valid SA.

        First SA deliberately accepts arbitrary offset. A strictly newer SA
        outside the configured phase tolerance rebases immediately. A lower
        timestamp can rebase only after monotonic presence expiry, never while
        the current phone is live. Remember recent accepted timestamps across
        generations so known replays cannot masquerade as either step. This
        cannot distinguish an unseen/evicted replay or a large transport delay
        from a clock step; first admission has the same offset ambiguity.
        """
        if sa.time in self._seen_sa_times:
            return "replayed_or_out_of_order", None, None
        estimate = self.phone_now(mono)
        delta = 0.0 if estimate is None else (sa.time - estimate).total_seconds()
        step = estimate is not None and abs(delta) > self.phone_clock_step_tolerance_s
        if self._latest_time is not None and sa.time <= self._latest_time:
            if self._present(mono) or not step:
                return "replayed_or_out_of_order", None, None
        phone_time = sa.time if estimate is None or step else max(estimate, sa.time)
        if sa.stale <= phone_time or (phone_time - sa.time).total_seconds() >= self.receipt_max_age_s:
            return "stale", None, None
        if max((sa.time - phone_time).total_seconds(), (sa.start - phone_time).total_seconds()) > self.future_tolerance_s:
            return "future", None, None
        diagnostic = None
        if step:
            self._phone_generation += 1
            diagnostic = PhoneClockDiagnostic(self._phone_generation, delta, mono,
                                              "forward_step" if delta > 0 else "backward_step")
            self.clock_diagnostic = diagnostic
        if estimate is None or step or sa.time > estimate:
            self._phone_ref, self._phone_mono_ref = sa.time, mono
        self._latest_time = sa.time
        self._seen_sa_times[sa.time] = None
        if len(self._seen_sa_times) > PHONE_SA_HISTORY:
            del self._seen_sa_times[next(iter(self._seen_sa_times))]
        return None, phone_time, diagnostic

    def _manual_order(self, stamp, observed_mono):
        """Compare phone dates within a generation, monotonic epochs across steps.

        Keep the original manual date and monotonic age unchanged on rebase.
        Cross-step, an old point's generation is not on the wire; current-clock
        projection is conservative for forward steps but cannot authenticate
        arbitrary unseen points. Prefer a new point made after recovery.
        """
        if self._manual is None:
            return 1
        if self._manual.clock_generation == self._phone_generation:
            a, b = stamp, self._manual.observation_time
        else:
            a, b = observed_mono, self._manual.observation_mono
        return (a > b) - (a < b)

    def _replace_manual(self, location):
        previous_uid = None if self._manual is None else self._manual.marker_uid
        retiring = (previous_uid is not None and not self._manual_retired
                    and previous_uid != location.marker_uid)
        if retiring and previous_uid not in self._retirements:
            if len(self._retirements) >= self.max_pending_retirements:
                return False
            self._retirements[previous_uid] = None
        self._manual = location
        self._manual_retired = False
        return True

    def _retire_confirming_marker(self):
        """Reserve deletion before confirmation; retry if the backlog is full.

        Retain the manual geography/UID as fallback provenance, but remember
        that its map item is already retiring so later replacement cannot
        enqueue the same deletion again after the service acknowledges it.
        """
        uid = self._manual.marker_uid
        if uid is None or self._manual_retired:
            return True
        if uid not in self._retirements:
            if len(self._retirements) >= self.max_pending_retirements:
                return False
            self._retirements[uid] = None
        self._manual_retired = True
        return True

    def feed(self, sa, receipt_mono, receipt_utc, *, from_ethernet):
        """Accept presence, diagnose phone GPS, and adopt drags/USER selections."""
        if not isinstance(sa, PhoneSA):
            raise ValueError("sa must be a parsed PhoneSA")
        if type(from_ethernet) is not bool:
            raise ValueError("from_ethernet must be bool")
        mono, _ = self._now(receipt_mono, receipt_utc)

        def result(status):
            return PhoneUpdate(status, self.pinned_uid, sa.uid)

        if not from_ethernet:
            return result("wrong_ingress")
        if sa.uid == self.radio_uid:
            return result("radio_uid")
        if self.pinned_uid is not None and sa.uid != self.pinned_uid:
            return result("uid_mismatch")
        reappeared = not self._present(mono)
        rejection, phone_time, diagnostic = self._admit_sa_clock(sa, mono)
        if rejection is not None:
            return result(rejection)
        age = max(0.0, (phone_time - sa.time).total_seconds())
        self._presence = mono + min((sa.stale - phone_time).total_seconds(), self.receipt_max_age_s - age)
        self.pinned_uid = sa.uid
        feeding = self._feeding(mono)
        if feeding and sa.geopointsrc == "GPS" and sa.source == "gps":
            self._warn("phone_gps", "Phone location services are on. Turn Android location "
                       "services off so ATAK uses the radio's position.",
                       "Turn phone location services off", initial_audio=False, web=True)
        elif sa.source == "external" and (sa.geopointsrc or "").startswith(EXTERNAL_SOURCE_PREFIX):
            self._clear_warning("phone_gps", web=True)
        status = "accepted_presence"
        user_selected = sa.geopointsrc == "USER"
        dragged = sa.how == "h-e" and not sa.geopointsrc and not sa.parent_uids
        if sa.source == "manual" and (user_selected or dragged) and (sa.lat, sa.lon) != (0, 0):
            point = (sa.lat, sa.lon)
            # Explicit USER can select even the last fed point. A transition
            # into USER is an action; its periodic unchanged SA is not.
            if user_selected:
                changed = point != self._last_user_sa_point or self._last_sa_geopointsrc != "USER"
            else:
                changed = point != self._last_manual_sa_point
            if (not feeding and changed and (user_selected or point != self._last_sent_point)
                    and (self._last_sent_mono is None or mono - age > self._last_sent_mono)
                    and self._manual_order(sa.time, mono - age) > 0):
                note = "user-selected own location" if user_selected else "hand-set own dot"
                fix = _fix(sa, "self_manual", sa.uid, note)
                location = PhoneFix(fix, sa.uid, "self_manual", sa.time, mono - age,
                                    mono, None, None, clock_generation=self._phone_generation)
                self._ack_gesture(mono - age)
                if self._replace_manual(location):
                    status = "accepted_manual"
                else:
                    return PhoneUpdate("retirement_queue_full", self.pinned_uid, sa.uid, reappeared, diagnostic)
            # Periodic unchanged hand-set SA never redates a user observation.
            self._last_manual_sa_point = point
            if user_selected:
                self._last_user_sa_point = point
        self._last_sa_geopointsrc = sa.geopointsrc
        return PhoneUpdate(status, self.pinned_uid, sa.uid, reappeared, diagnostic)

    def feed_marker(self, marker, receipt_mono, receipt_utc, *, from_ethernet):
        """Auto-accept eligible Sent points; leave others as plain candidates.

        Creator time orders persistent locations; event time/stale do not
        refresh or expire them. Distinct points tied at the current epoch
        stay candidates. Repeated identical UID/epoch/geography is duplicate.
        A newer edit of the same UID does not retire that UID. No candidate
        queue or packet transmission lives here.

        A fresh admitted Send acknowledges warnings raised before it, even
        if the creator-time ordering leaves it a duplicate/older candidate.
        """
        if not isinstance(marker, MarkCandidate):
            raise ValueError("marker must be a parsed MarkCandidate")
        if type(from_ethernet) is not bool:
            raise ValueError("from_ethernet must be bool")
        mono, _ = self._now(receipt_mono, receipt_utc)

        def candidate(reason):
            return MarkerUpdate("candidate", reason, marker)

        if not from_ethernet:
            return candidate("wrong_ingress")
        if self.pinned_uid is None:
            return candidate("phone_not_pinned")
        if marker.creator_uid != self.pinned_uid:
            return candidate("creator_mismatch")
        if not (marker.type.startswith("a-") or marker.type == "b-m-p"
                or marker.type.startswith("b-m-p-")):
            return candidate("not_point")
        external_uid = None if self.radio_uid is None else self.radio_uid + ".external-position"
        if marker.uid in (self.pinned_uid, self.radio_uid, external_uid):
            return candidate("reserved_uid")
        if marker.creator_time is None:
            return candidate("missing_creator_time")
        try:
            created = _utc(marker.creator_time)
        except ValueError:
            return candidate("invalid_creator_time")
        phone_time = self.phone_now(mono)
        if (created - phone_time).total_seconds() > self.future_tolerance_s:
            return candidate("future_creator_time")
        # A fresh authorized Send acknowledges the alert independently of
        # creator-time location ordering. An old packet replay does not.
        sent_age = (phone_time - marker.time).total_seconds()
        if (marker.stale > phone_time and sent_age < self.receipt_max_age_s
                and max(-sent_age, (marker.start - phone_time).total_seconds()) <= self.future_tolerance_s):
            self._ack_gesture(mono - max(0.0, sent_age))
        age = max(0.0, (phone_time - created).total_seconds())
        observed_mono = mono - age
        current = self._manual
        if current is not None:
            same_point = (marker.lat, marker.lon, marker.hae) == (current.fix.lat, current.fix.lon, current.fix.hae)
            if marker.uid == current.marker_uid and created == current.observation_time and same_point:
                return MarkerUpdate("duplicate", "same_location", marker)
            order = self._manual_order(created, observed_mono)
            if order < 0:
                return candidate("older_location")
            if order == 0:
                return candidate("tied_creator_time")
        if marker.uid in self._retirements:
            return candidate("retirement_pending")
        note = f"Sent location marker {marker.uid}; creator time {created.isoformat()}"
        fix = _fix(marker, "marker", self.pinned_uid, note)
        location = PhoneFix(fix, self.pinned_uid, "marker", created, observed_mono,
                            mono, None, None, marker.uid, clock_generation=self._phone_generation)
        if not self._replace_manual(location):
            return candidate("retirement_queue_full")
        return MarkerUpdate("accepted", "newest_location", marker)

    def _feeding(self, mono):
        return self._feed_until is not None and mono < self._feed_until

    def position_sent(self, fix, sent_mono, sent_utc):
        """Record a successful 4349 send of the chosen fix, never just selection.

        Once sends stop, wait feed_timeout_s (device observed ~10 s) before
        accepting a drag. Suppress a held copy of the last fed lat/lon even
        after expiry; an altitude-only change does not establish a new drag.
        """
        if not _usable(fix) or (fix.phone_uid is not None and fix.source != "manual"):
            raise ValueError("position_sent needs a usable chosen fix")
        point = (_bounded(fix.lat, "lat", -90, 90), _bounded(fix.lon, "lon", -180, 180))
        if point == (0, 0):
            raise ValueError("external position rejects 0,0")
        mono, _ = self._now(sent_mono, sent_utc)
        self._feed_until = mono + self.feed_timeout_s
        self._last_sent_mono = mono
        self._last_sent_point = point
        self._last_manual_sa_point = point

    @property
    def outbound_events(self):
        return tuple(sorted(self._events.values(), key=lambda event: event.event_id))

    def _emit(self, code, kind, text, *, active=True):
        event = OutboundEvent(self._next_event_id, kind, code, text, self.pinned_uid, active)
        self._next_event_id += 1
        self._events[code, kind] = event
        return event.event_id

    def _warn(self, code, text, audio, *, initial_audio=True, web=False, acknowledged=False):
        warning = self._warnings.get(code)
        if warning is None:
            warning = self._warnings[code] = _Warning(
                text, audio, self.pinned_uid, self._clock, acknowledged=acknowledged)
            if initial_audio:
                self._emit(code, "audio", audio)
            if web:
                self._emit(code, "web_ui", text)
        if warning.chat_event_id is None and self.pinned_uid is not None:
            warning.phone_uid = self.pinned_uid
            warning.chat_event_id = self._emit(code, "geochat", warning.text)

    def _ack_warning(self, code, warning):
        """Silence prompts without pretending to restore trust or read a chat."""
        warning.acknowledged = True
        warning.deadline = None
        self._events.pop((code, "audio"), None)

    def _ack_gesture(self, action_mono):
        for code, warning in self._warnings.items():
            if action_mono >= warning.raised_mono:
                self._ack_warning(code, warning)

    def _clear_warning(self, code, *, web=False):
        if self._warnings.pop(code, None) is None:
            return
        for kind in ("geochat", "audio", "web_ui"):
            self._events.pop((code, kind), None)
        if web:
            self._emit(code, "web_ui", "", active=False)

    def ack_event(self, event_id, now_mono, now_utc, *, message_id=None):
        """Ack actual chat send (with its unique ID), or durable audio/UI action.

        The first successful chat send starts the read timeout only if the
        warning is unacknowledged. Retries cannot postpone that timeout or
        clear newer events.
        The service owns globally unique message IDs and ordered receipt/send
        completion handling. Failed transports must leave events pending.
        """
        if type(event_id) is not int or event_id <= 0:
            raise ValueError("event_id must be a positive integer")
        event = next((e for e in self._events.values() if e.event_id == event_id), None)
        if event is None:
            return False
        if event.kind == "geochat":
            message_id = _text(message_id, "message_id")
            if any(w.message_id == message_id for w in self._warnings.values()):
                raise ValueError("message_id is already outstanding")
        mono, _ = self._now(now_mono, now_utc)
        if event.kind == "geochat":
            warning = self._warnings[event.code]
            warning.message_id = message_id
            warning.sent_mono = mono
            if not warning.acknowledged:
                warning.deadline = mono + self.warning_read_timeout_s
        del self._events[event.code, event.kind]
        return True

    def _message_rejection(self, message, phone_time, from_ethernet):
        if not from_ethernet:
            return "wrong_ingress"
        if self.pinned_uid is None or message.sender_uid != self.pinned_uid:
            return "uid_mismatch"
        if self.radio_uid is None or message.destination_uid != self.radio_uid:
            return "wrong_destination"
        if message.stale <= phone_time or (phone_time - message.time).total_seconds() >= self.receipt_max_age_s:
            return "stale"
        if max((message.time - phone_time).total_seconds(), (message.start - phone_time).total_seconds()) > self.future_tolerance_s:
            return "future"
        return None

    def feed_chat(self, chat, receipt_mono, receipt_utc, *, from_ethernet):
        """Return admitted PhoneChat for plain-text display, or a rejection string.

        No commands, alert acknowledgements, location evidence or SA refresh.
        The service owns a bounded display history/deduplication by sender and
        message ID; no history or output queue is allocated here.
        """
        if not isinstance(chat, PhoneChat) or type(from_ethernet) is not bool:
            raise ValueError("parsed PhoneChat and boolean ingress required")
        mono, _ = self._now(receipt_mono, receipt_utc)
        phone_time = self.phone_now(mono)
        rejection = self._message_rejection(chat, phone_time, from_ethernet)
        if rejection is not None:
            return rejection
        if chat.remarks_time is not None and (chat.remarks_time - phone_time).total_seconds() > self.future_tolerance_s:
            return "future"
        return chat

    def feed_receipt(self, receipt, receipt_mono, receipt_utc, *, from_ethernet):
        """Consume direct delivered/read status; neither pins nor refreshes SA."""
        if not isinstance(receipt, ChatReceipt) or type(from_ethernet) is not bool:
            raise ValueError("parsed ChatReceipt and boolean ingress required")
        mono, _ = self._now(receipt_mono, receipt_utc)
        phone_time = self.phone_now(mono)
        rejection = self._message_rejection(receipt, phone_time, from_ethernet)
        if rejection is not None:
            return rejection
        if receipt.status not in ("delivered", "read"):
            return "invalid_status"
        item = next(((code, w) for code, w in self._warnings.items()
                     if w.message_id == receipt.message_id), None)
        if item is None:
            return "unknown_message"
        code, warning = item
        if (self.phone_now(warning.sent_mono) - receipt.time).total_seconds() > self.future_tolerance_s:
            return "predates_message"
        if warning.read or (warning.delivered and receipt.status == "delivered"):
            return "duplicate"
        warning.delivered = True
        if receipt.status == "read":
            warning.read = True
            self._ack_warning(code, warning)
        return receipt.status

    def _remind(self, mono):
        for code, warning in self._warnings.items():
            if not warning.acknowledged and warning.deadline is not None and mono >= warning.deadline:
                if (code, "audio") not in self._events:
                    self._emit(code, "audio", warning.audio)
                warning.deadline = mono + self.warning_read_timeout_s

    def select(self, now_mono, now_utc, *, radio_gps=None):
        """Apply freshness, distrust, user conflict/confirmation and reminders.

        fault_epoch only revokes trust. A new mark within restore_margin_m of
        fresh, unjammed GPS restores POSITION trust, even if the raw monitor
        is still quarantined. An old held mark or clean ranging never does.
        A later fault invalidates this confirmation. A confirming Sent marker
        is queued for retirement before returning to GPS (backpressure defers
        confirmation); its geography remains the fallback landmark. gps_state
        stays raw; no time-service permission is produced here.
        """
        mono = _number(now_mono, "monotonic time")
        fresh = False
        state = "NO_FIX"
        gps_fix = None
        jammed = agrees = False
        fault = False
        if radio_gps is not None:
            if not isinstance(radio_gps, RadioGPS):
                raise ValueError("radio_gps must be RadioGPS or None")
            if (radio_gps.state not in _GPS_STATES or type(radio_gps.jammed) is not bool
                    or type(radio_gps.ranges_agree) is not bool):
                raise ValueError("invalid GPS state/jamming/agreement flag")
            if type(radio_gps.fault_epoch) is not int or radio_gps.fault_epoch < self._fault_epoch:
                raise ValueError("fault_epoch must be a nondecreasing nonnegative integer")
            observed = _number(radio_gps.observed_mono, "GPS observation time")
            gps_fix = radio_gps.fix
            if gps_fix is not None:
                if (not isinstance(gps_fix, Fix) or gps_fix.source != "gnss"
                        or gps_fix.phone_uid is not None):
                    raise ValueError("radio GPS must be an own-receiver GNSS Fix")
                _bounded(gps_fix.lat, "GPS latitude", -90, 90)
                _bounded(gps_fix.lon, "GPS longitude", -180, 180)
                fresh = 0 <= mono - observed < self.gps_max_age_s and _usable(gps_fix)
            state = radio_gps.state if fresh else "NO_FIX"
            jammed = radio_gps.jammed
            condition = (radio_gps.state, jammed)
            bad = radio_gps.state in (_GPS_STATES - _TRUSTED - {"NO_FIX"}) or jammed
            fault = (radio_gps.fault_epoch > self._fault_epoch
                     or (bad and condition != self._fault_condition))
            agrees = fresh and not jammed and state != "NO_FIX" and (
                radio_gps.ranges_agree or state == "RANGE_CONSISTENT")
        mono, _ = self._now(mono, now_utc)
        if radio_gps is not None:
            self._fault_epoch = radio_gps.fault_epoch
            self._fault_condition = condition
        if fault:
            self._distrusted = True
            self._distrust_since = mono
            self._position_confirmed = False
            self._clear_warning("confirm_position")
        distance = None
        if self._manual is not None and fresh and state != "NO_FIX":
            mark = self._manual
            distance = _distance_m((gps_fix.lat, gps_fix.lon), (mark.fix.lat, mark.fix.lon))
            epoch = (mark.clock_generation, mark.observation_time)
            if self._checked_manual_epoch != epoch:
                checked = True
                if distance > self.alert_threshold_m:
                    self._distrusted = self._overridden = True
                    self._distrust_since = mono
                    self._position_confirmed = False
                    # A newer conflict supersedes the previous unsent warning.
                    self._clear_warning("gps_override", web=True)
                    self._warn("gps_override", f"GPS location overridden by your mark; "
                               f"mark and radio GPS are {distance:.1f} m apart "
                               f"(alert threshold {self.alert_threshold_m:g} m). Radio GPS is suspect.",
                               "GPS location overridden", web=True, acknowledged=True)
                elif (distance <= self.restore_margin_m and not jammed
                      and (not self._distrusted or mark.observation_mono >= self._distrust_since)):
                    if self._retire_confirming_marker():
                        self._distrusted = self._overridden = False
                        self._position_confirmed = True
                    else:
                        checked = False  # retry confirmation once deletion capacity frees
                if checked:
                    self._checked_manual_epoch = epoch
        usable = fresh and state != "NO_FIX" and not jammed and (
            state in _TRUSTED or self._position_confirmed)
        own_selected = usable and not self._distrusted
        location = None if own_selected else self._manual
        if location is not None:
            age = max(0.0, mono - location.observation_mono)
            fix = replace(location.fix, remarks=location.fix.remarks + f"; location age {age:.1f} s")
            location = replace(location, fix=fix, age_s=age)
        chosen = gps_fix if own_selected else (None if location is None else location.fix)
        if not self._distrusted:
            self._clear_warning("gps_override", web=True)
            self._clear_warning("confirm_position")
        if (self._distrusted or chosen is None) and not self._overridden:
            self._warn("gps_untrusted", "Radio GPS is unavailable or untrusted. "
                       "If ATAK shows NO GPS, tap it to select your best known location "
                       "or drag your own dot; "
                       "otherwise Send a point to the radio contact. The newest choice wins.",
                       "Set your best known location in ATAK")
        else:
            self._clear_warning("gps_untrusted")
        if self._distrusted and agrees:
            self._warn("confirm_position", "Ranging and GPS agree again. Mark your best known "
                       "location in ATAK to confirm whether GPS is usable.",
                       "Mark your best known location to confirm GPS", initial_audio=False)
        self._remind(mono)
        return LocationSelection(location, tuple(self._retirements), own_selected,
                                 chosen, self._present(mono), state,
                                 self._overridden, distance, not self._distrusted,
                                 self.outbound_events)

    def get_fix(self, now_mono, now_utc, **kwargs):
        """Return the selected Fix; phone presence gates transport separately."""
        return self.select(now_mono, now_utc, **kwargs).fix

    def ack_retired(self, marker_uids):
        """Service has durably taken responsibility for these delete requests.

        This is not proof of removal in ATAK. The service owns bounded retry
        and delivery verification after acknowledging; unsent requests must
        remain pending. Unknown/already acknowledged UIDs are harmless.
        """
        if isinstance(marker_uids, str):
            raise ValueError("marker_uids must be a collection of UIDs")
        for uid in marker_uids:
            self._retirements.pop(uid, None)
