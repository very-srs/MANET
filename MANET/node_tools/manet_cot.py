"""Build CoT XML and parse untrusted marker candidates.

Builders return UTF-8 bytes or None for suppressed geography. now is an
aware datetime in the recipient's clock domain. Use PhonePosition.outbound_time
for all tethered-phone builders. No function reads a clock. Fixes must be
current WGS84 with ellipsoid height in metres, not MSL. Only confidence 0.90
maps to ce/le. Other probabilities require an error model and stay unknown.
Wire references: TAK-Product-Center/atak-civ main, 2026-10-06:
* takkernel/shared/.../cot/event/CotPoint.java: UNKNOWN=9999999, ZERO,
  HAE metres, circular error radius and toGeoPoint ce/le preservation.
* takkernel/engine/.../maps/coords/GeoPoint.java: CE90/LE90, altitude bounds.
* atak/ATAK/app/.../contact/ContactListDetailHandler.java: friendly contacts,
  callsign, endpoint. chat/GeoChatService.java: hidden-location chat.
Phone classification uses precisionlocation. Ownership uses creator UID/time.
Transport is TAK protocol 0, CoT schema 2.0. parse_marker takes one complete
XML document. Listeners must bound buffers and frame split/coalesced reads.
Direct v0 TCP normally closes after one message. Streaming v0 concatenates
XML ending in </event>. Do not advertise TakControl/protobuf.
manet_phone applies local-EUD/controller/creator/epoch authorization. Other candidates
need explicit acceptance.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address
import math
import re
import xml.etree.ElementTree as ET


# CoT's sentinel for unavailable HAE and 90 percent error estimates.
UNKNOWN = 9999999
RADIO_TYPE = "a-f-G-E"
DEFAULT_MAX_BYTES = 64 * 1024
MAX_DEPTH = 32
CHAT_STALE_S = 86400
CONTACT_REFRESH_S = 30
CONTACT_STALE_S = 3600         # Avoid repeated new-contact alerts
EXTERNAL_POSITION_PORT = 4349
EXTERNAL_POSITION_STALE_S = 10
EXTERNAL_SOURCE_PREFIX = "MANET:"
RETIRE_STALE_S = 60
# Map the selected source to CoT's location-method code.
_HOW = {"gnss": "m-g", "ranged": "m-f", "manual": "h-e"}
# CoT type/how components are alphanumeric tokens separated by hyphens.
_TOKEN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\Z")
# Require UTC Z. Optional fractional seconds use one to six digits.
_UTC_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
                       r"[0-9]{2}(?:\.[0-9]{1,6})?Z\Z")


@dataclass(frozen=True)
class ErrorEstimate:
    """Horizontal radius or vertical absolute error in metres at confidence."""

    metres: float
    confidence: float


@dataclass(frozen=True)
class Fix:
    """Carry selected WGS84 geometry, uncertainty and source provenance."""
    lat: float
    lon: float
    source: str                    # gnss, ranged (including fused), or manual
    hae: float | None = None
    horizontal_error: ErrorEstimate | None = None
    vertical_error: ErrorEstimate | None = None
    # Ranged output requires both explicitly True. GNSS/manual positions do
    # not need a relative-frame solution, but explicit False suppresses any fix.
    orientation_resolved: bool | None = None
    mirror_resolved: bool | None = None
    remarks: str = ""
    # Dependent provenance, never independent position evidence. An accepted
    # user gesture may feed this phone's external position. An SA echo may not.
    phone_uid: str | None = None


@dataclass(frozen=True)
class MarkCandidate:
    """Untrusted observation until the location selector applies its policy.

    Unknown hae/ce/le are None. Times are aware UTC datetimes. ``time`` is
    message time, not necessarily observation time. start/stale and le are
    retained for service-side expiry checks and review.
    """

    uid: str
    type: str
    callsign: str | None
    lat: float
    lon: float
    hae: float | None
    ce: float | None
    time: datetime
    how: str
    remarks: str
    le: float | None
    start: datetime
    stale: datetime
    creator_uid: str | None = None
    creator_time: datetime | None = None

    @property
    def human_placed(self):
        """A placement hint, not authenticated intent or a fresh observation."""
        return self.how in ("h-e", "h-g-i-g-o")


@dataclass(frozen=True)
class PhoneSA:
    """Claimed phone self SA without Ethernet ingress proof.

    gps/manual describe the observed ATAK metadata, not GNSS authenticity.
    Preserve Endpoint verbatim, including tcpsrcreply:4242:srctcp. It is not
    a resolved address and must not authorize the sender.
    """

    uid: str
    callsign: str
    lat: float
    lon: float
    hae: float | None
    ce: float | None
    source: str
    geopointsrc: str | None
    altsrc: str | None
    time: datetime
    start: datetime
    stale: datetime
    endpoint: str | None
    how: str
    le: float | None
    parent_uids: tuple[str, ...] = ()  # p-s links: external-position ancestry


def _number(value, name):
    """Convert a finite number while rejecting booleans."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _bounded(value, name, low, high):
    """Convert a number and enforce inclusive bounds."""
    number = _number(value, name)
    if not low <= number <= high:
        raise ValueError(f"{name} out of range")
    return number


def _text(value, name, *, empty=False):
    """Require text containing only characters allowed in XML."""
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{name} must be text" + ("" if empty else " and nonempty"))
    # ElementTree escapes markup, but does not reject invalid XML characters.
    if any(not (c in "\t\n\r" or "\x20" <= c <= "\ud7ff"
                or "\ue000" <= c <= "\ufffd" or "\U00010000" <= c <= "\U0010ffff")
           for c in value):
        raise ValueError(f"{name} contains invalid XML characters")
    return value


def _utc(value):
    """Normalize an aware datetime to UTC."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("now must be an aware datetime")
    return value.astimezone(timezone.utc)


def _stamp(value):
    """Format a timestamp with millisecond precision and a UTC Z suffix."""
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _event(uid, event_type, how, now, stale_s):
    """Create a CoT 2.0 envelope with start and stale times in UTC."""
    now = _utc(now)
    stale_s = _number(stale_s, "stale_s")
    # Millisecond serialization must leave stale later than start.
    if stale_s < 0.001:
        raise ValueError("stale_s must be at least one millisecond")
    try:
        stale = now + timedelta(seconds=stale_s)
    except OverflowError:
        raise ValueError("stale_s exceeds datetime range") from None
    return ET.Element("event", {
        "version": "2.0", "uid": _text(uid, "uid"), "type": event_type,
        "time": _stamp(now), "start": _stamp(now), "stale": _stamp(stale),
        "how": how, "access": "Undefined",
    })


def _usable(fix):
    """Check fix source and require resolved ranged geometry."""
    if fix is None:
        return False
    if not isinstance(fix, Fix):
        raise ValueError("fix must be a Fix or None")
    if fix.source not in _HOW:
        raise ValueError("unknown fix source")
    for flag in (fix.orientation_resolved, fix.mirror_resolved):
        if flag is not None and type(flag) is not bool:
            raise ValueError("geometry flags must be bool or None")
    if fix.orientation_resolved is False or fix.mirror_resolved is False:
        return False
    return fix.source != "ranged" or (fix.orientation_resolved is True
                                     and fix.mirror_resolved is True)


def _error(estimate, name):
    """Encode CE90/LE90 only for a 90 percent confidence estimate."""
    if estimate is None:
        return UNKNOWN, f"{name} error unknown"
    if not isinstance(estimate, ErrorEstimate):
        raise ValueError(f"{name} error must be an ErrorEstimate or None")
    metres = _bounded(estimate.metres, name + " error", 0, UNKNOWN)
    confidence = _bounded(estimate.confidence, name + " confidence", 0, 1)
    if confidence == 0 or metres == UNKNOWN:
        raise ValueError("use None for unknown error; confidence must be positive")
    note = f"{name} error {metres:g} m at {confidence * 100:g}% confidence"
    if confidence != 0.90:
        return UNKNOWN, note + " (CoT 90% error unknown)"
    return metres, note


def _point(event, fix):
    """Append validated geometry and return uncertainty/provenance remarks."""
    lat = _bounded(fix.lat, "lat", -90, 90)
    lon = _bounded(fix.lon, "lon", -180, 180)
    # GeoPoint.isAltitudeValid's actual constants, not its stale Javadoc.
    hae = UNKNOWN if fix.hae is None else _bounded(fix.hae, "hae", -14000, 76000)
    ce, h_note = _error(fix.horizontal_error, "horizontal")
    le, v_note = _error(fix.vertical_error, "vertical")
    if fix.hae is None:
        # An altitude error cannot describe a missing altitude.
        le = UNKNOWN
        v_note += "; altitude unknown"
    ET.SubElement(event, "point", {
        "lat": str(lat), "lon": str(lon), "hae": str(hae),
        "ce": str(ce), "le": str(le),
    })
    remarks = f"MANET radio; source={fix.source}; {h_note}; {v_note}"
    if fix.remarks:
        remarks += "; " + _text(fix.remarks, "fix remarks")
    if fix.phone_uid is not None:
        remarks += "; from tethered phone " + _text(fix.phone_uid, "phone_uid")
    return remarks


def _serialize(event):
    """Serialize an event as UTF-8 XML with a declaration."""
    return ET.tostring(event, encoding="utf-8", xml_declaration=True)


def hidden_contact(uid, callsign, contact_endpoint, now, stale_s=CONTACT_STALE_S):
    """Advertise the radio's reachable contact without asserting a location.

    Pixel 6a/ATAK 5.6 accepts CotPoint.ZERO for contacts and Send targets.
    Any icon at 0,0 does not represent the radio's geographic position.
    No selected fix, spoof state or warning label belongs in this builder.
    """
    event = _event(uid, RADIO_TYPE, "m-g", now, stale_s)
    ET.SubElement(event, "point", {
        "lat": "0", "lon": "0", "hae": str(UNKNOWN),
        "ce": str(UNKNOWN), "le": str(UNKNOWN),
    })
    detail = ET.SubElement(event, "detail")
    ET.SubElement(detail, "contact", {
        "callsign": _text(callsign, "callsign"),
        "endpoint": _contact_endpoint(contact_endpoint),
    })
    ET.SubElement(detail, "remarks").text = (
        "Send a point to this contact to set your radio's location.")
    return _serialize(event)


def external_position(radio_uid, fix, now, stale_s=EXTERNAL_POSITION_STALE_S):
    """Build the selected own-location CoT for unicast UDP 4349.

    Only pass the selector's Fix. This builder cannot establish GPS freshness
    or trust. Reject phone GPS/SA-derived GNSS, but allow accepted user marks.
    ExternalGPSInput hides items with this UID, so use a separate stable UID.
    It uses point/precisionlocation/remarks, not how/stale, for location expiry.
    Refresh about once a second while selected, then stop. Never send ZERO for
    no fix. Source tags identify self-SA echoes without parent links. Hand-set
    self SA is eligible only after the feed stops. Empty track clears unknown
    motion. Contacts and GeoChat use normal CoT.
    """
    if not _usable(fix):
        return None
    if fix.phone_uid is not None and fix.source != "manual":
        raise ValueError("phone-derived GPS/ranging cannot feed external position")
    radio_uid = _text(radio_uid, "radio_uid")
    if radio_uid == fix.phone_uid:
        raise ValueError("radio UID must differ from phone UID")
    event = _event(radio_uid + ".external-position", RADIO_TYPE, _HOW[fix.source], now, stale_s)
    _point(event, fix)  # Validate coordinates/HAE/errors and convert CE90/LE90.
    if float(fix.lat) == 0 and float(fix.lon) == 0:
        raise ValueError("ATAK external position rejects latitude/longitude 0,0")
    source = EXTERNAL_SOURCE_PREFIX + fix.source
    detail = ET.SubElement(event, "detail")
    ET.SubElement(detail, "precisionlocation", {
        "geopointsrc": source, "altsrc": source if fix.hae is not None else "???",
    })
    ET.SubElement(detail, "remarks").text = {
        "gnss": "MANET radio GPS", "manual": "MANET user location", "ranged": "MANET ranged location",
    }[fix.source]
    ET.SubElement(detail, "track")  # Absent speed/course resets to NaN.
    return _serialize(event)


def contact_refresh_due(now_mono, last_sent_mono=None, *, changed=False, phone_reappeared=False):
    """Send immediately, then every 30 s after a successful send.

    Use CONTACT_STALE_S (3600 s) for each hidden contact event, including
    when no position is available.
    Record last_sent_mono only after successful transport, so failures remain
    due. The service applies retry backoff. Changes/reappearance send at once.
    """
    now_mono = _number(now_mono, "now_mono")
    if type(changed) is not bool or type(phone_reappeared) is not bool:
        raise ValueError("changed and phone_reappeared must be bool")
    if last_sent_mono is None:
        return True
    last_sent_mono = _number(last_sent_mono, "last_sent_mono")
    if now_mono < last_sent_mono:
        raise ValueError("monotonic time moved backwards")
    return changed or phone_reappeared or now_mono - last_sent_mono >= CONTACT_REFRESH_S


def retire_marker(marker_uid, task_uid, now):
    """Remove a replaced or GPS-confirming marker, unicast to its own phone.

    ATAK CotDeleteImporter.importData reads t-x-d-d, detail/link uid with
    nonempty relation/type, and __forcedelete. Its documented example uses
    relation=none/type=none. Without force it only requests staleness.
    remoteDelete=false can refuse even forced deletion. A task is not a receipt.
    Only target UIDs returned by the selector's retirement list, never
    arbitrary candidates, phone self SA, or the radio's contact UID.
    """
    marker_uid = _text(marker_uid, "marker_uid")
    if marker_uid == task_uid:
        raise ValueError("delete task UID must differ from its target")
    event = _event(task_uid, "t-x-d-d", "m-g", now, RETIRE_STALE_S)
    ET.SubElement(event, "point", {
        "lat": "0", "lon": "0", "hae": str(UNKNOWN),
        "ce": str(UNKNOWN), "le": str(UNKNOWN),
    })
    detail = ET.SubElement(event, "detail")
    ET.SubElement(detail, "link", {"uid": marker_uid, "relation": "none", "type": "none"})
    ET.SubElement(detail, "__forcedelete")
    return _serialize(event)


def _contact_endpoint(endpoint):
    """Validate a unicast IPv4/port pair and format a TCP contact endpoint."""
    if not isinstance(endpoint, (tuple, list)) or len(endpoint) != 2:
        raise ValueError("contact_endpoint must be (IPv4, port)")
    endpoint_ip, port = endpoint
    if not isinstance(endpoint_ip, str):
        raise ValueError("endpoint_ip must be a literal unicast IPv4 address")
    try:
        address = IPv4Address(endpoint_ip)
    except (ValueError, TypeError):
        raise ValueError("endpoint_ip must be a literal unicast IPv4 address") from None
    if (address.is_unspecified or address.is_multicast or address.is_loopback
            or int(address) == 0xffffffff):
        raise ValueError("endpoint_ip must be a reachable unicast IPv4 address")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 to 65535")
    return f"{address}:{port}:tcp"


def geochat_report(sender_uid, sender_callsign, dest_uid, text, msg_id, now):
    """Build a direct b-t-f text report with ATAK's hidden-location envelope.

    CotPoint.ZERO is structural here, never a radio position. The recipient
    must be the phone's ATAK UID. The caller supplies a unique message ID,
    dates the measurements in text, and limits report frequency. No endpoint
    or reply capability is invented when only these arguments are known.
    """
    sender_uid = _text(sender_uid, "sender_uid")
    dest_uid = _text(dest_uid, "dest_uid")
    msg_id = _text(msg_id, "msg_id")
    event = _event(f"GeoChat.{sender_uid}.{dest_uid}.{msg_id}", "b-t-f",
                   "h-g-i-g-o", now, CHAT_STALE_S)
    ET.SubElement(event, "point", {
        "lat": "0", "lon": "0", "hae": str(UNKNOWN),
        "ce": str(UNKNOWN), "le": str(UNKNOWN),
    })
    detail = ET.SubElement(event, "detail")
    chat = ET.SubElement(detail, "__chat", {
        "id": dest_uid, "messageId": msg_id,
        "senderCallsign": _text(sender_callsign, "sender_callsign"),
        "chatroom": dest_uid, "groupOwner": "false", "parent": "RootContactGroup",
    })
    ET.SubElement(chat, "chatgrp", {"id": dest_uid, "uid0": sender_uid, "uid1": dest_uid})
    ET.SubElement(detail, "link", {"uid": sender_uid, "type": RADIO_TYPE, "relation": "p-p"})
    ET.SubElement(detail, "remarks", {
        "source": "BAO.F.ATAK." + sender_uid, "sourceID": sender_uid,
        "to": dest_uid, "time": event.get("time"),
    }).text = _text(text, "text", empty=True)
    return _serialize(event)


class _SafeTreeBuilder(ET.TreeBuilder):
    """Abort during parsing, before DTD declarations or excessive depth."""

    def __init__(self):
        """Initialize the parser nesting counter."""
        super().__init__()
        self.depth = 0

    def doctype(self, name, pubid, system):
        """Reject every DTD declaration reported by the XML parser."""
        # Parser callback, not a byte search: UTF-16 cannot bypass this check.
        raise ValueError("DOCTYPE is forbidden")

    def start(self, tag, attrs):
        """Reject excessive nesting before constructing the next element."""
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ValueError("XML nesting exceeds depth limit")
        return super().start(tag, attrs)

    def end(self, tag):
        """Close an element and decrement the nesting counter."""
        result = super().end(tag)
        self.depth -= 1
        return result


def _child(parent, name, *, required=False):
    """Select one direct child, rejecting missing required or duplicate tags."""
    children = [] if parent is None else parent.findall(name)
    if len(children) > 1 or (required and not children):
        raise ValueError(f"expected {'one' if required else 'at most one'} {name}")
    return children[0] if children else None


def _parse_time(value, name):
    """Parse a Z-suffixed ISO UTC timestamp with optional fractional seconds."""
    if not isinstance(value, str) or not _UTC_TIME.fullmatch(value):
        raise ValueError(f"invalid {name}: expected ISO 8601 UTC")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError(f"invalid {name}: expected ISO 8601 UTC") from None


def _optional_number(point, name):
    """Decode bounded HAE/error values, mapping absent or unknown to None."""
    value = point.get(name)
    if value is None:
        return None
    number = _number(value, name)
    if number == UNKNOWN:
        return None
    if name == "hae":
        return _bounded(number, name, -14000, 76000)
    return _bounded(number, name, 0, UNKNOWN)


def parse_marker(data: bytes, max_bytes=DEFAULT_MAX_BYTES):
    """Return an untrusted MarkCandidate or a rejection-reason string.

    Accept atom (a-*) and point-marker (b-m-p*) event families. Chat, control,
    routes and shapes are not point observations. Unknown detail extensions
    are ignored, while duplicate known fields are rejected as ambiguous.
    Built-in XML escapes are decoded. DTDs/custom/external entities are
    forbidden. Byte and depth limits apply before a complete tree is built.
    An invalid max_bytes is a caller error (ValueError), not a wire rejection.
    """
    _byte_limit(max_bytes)
    try:
        return _marker_from_root(_parse_document(data, max_bytes))
    except (ET.ParseError, LookupError):
        return "malformed or unsupported XML"
    except ValueError as exc:
        return str(exc)


def _byte_limit(max_bytes):
    """Validate the caller-supplied XML byte limit."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")


def _parse_document(data, max_bytes):
    """Parse bounded bytes with DTD and nesting protections."""
    if not isinstance(data, bytes):
        raise ValueError("data must be bytes")
    if len(data) > max_bytes:
        raise ValueError("XML exceeds byte limit")
    return ET.fromstring(data, parser=ET.XMLParser(target=_SafeTreeBuilder()))


def _marker_from_root(root, *, allow_zero=False):
    """Validate a point event and extract unauthenticated marker claims."""
    if root.tag != "event" or root.get("version") != "2.0":
        raise ValueError("expected a CoT 2.0 event in protocol-0 XML")
    uid = _text(root.get("uid"), "uid")
    event_type = _text(root.get("type"), "type")
    how = _text(root.get("how"), "how")
    if not _TOKEN.fullmatch(event_type) or not _TOKEN.fullmatch(how):
        raise ValueError("invalid type or how")
    if not (event_type.startswith("a-") or event_type == "b-m-p"
            or event_type.startswith("b-m-p-")):
        raise ValueError("event is not a point marker")
    time = _parse_time(root.get("time"), "time")
    start = _parse_time(root.get("start"), "start")
    stale = _parse_time(root.get("stale"), "stale")
    if stale <= start:
        raise ValueError("stale must be after start")
    point = _child(root, "point", required=True)
    if len(point):
        raise ValueError("point must not have child elements")
    lat = _bounded(point.get("lat"), "lat", -90, 90)
    lon = _bounded(point.get("lon"), "lon", -180, 180)
    hae, ce, le = (_optional_number(point, name) for name in ("hae", "ce", "le"))
    if not allow_zero and lat == 0 and lon == 0 and hae is None and ce is None and le is None:
        raise ValueError("hidden/unknown ZERO point is not a mark")
    detail = _child(root, "detail")
    contact = _child(detail, "contact")
    remarks = _child(detail, "remarks")
    creator = _child(detail, "creator")
    creator_uid = None if creator is None else creator.get("uid")
    creator_time = None if creator is None else creator.get("time")
    if creator_uid is not None:
        _text(creator_uid, "creator uid")
    if creator_time is not None:
        creator_time = _parse_time(creator_time, "creator time")
    callsign = None if contact is None else contact.get("callsign")
    if callsign is not None:
        _text(callsign, "callsign")
    return MarkCandidate(uid, event_type, callsign, lat, lon, hae, ce,
                         time, how, "" if remarks is None else "".join(remarks.itertext()),
                         le, start, stale, creator_uid, creator_time)


def parse_self_sa(data: bytes, max_bytes=DEFAULT_MAX_BYTES):
    """Return PhoneSA or a rejection string, using the captured Pixel profile.

    Require a-f-G-U-C and a contact callsign, not a sent map point or radio
    equipment marker. geopointsrc determines GPS, USER (manual), or MANET:
    (external), regardless of how. With no source, h-e is a dragged manual
    point unless a p-s link identifies an echo. ZERO is always presence only.
    Unknown providers remain unknown. Parent links are a fallback echo guard.
    This is classification of claims, never authentication or ingress proof.
    """
    _byte_limit(max_bytes)
    try:
        root = _parse_document(data, max_bytes)
        mark = _marker_from_root(root, allow_zero=True)  # Presence, no fix.
        if mark.type != "a-f-G-U-C":
            raise ValueError("event is not phone self SA (expected a-f-G-U-C)")
        detail = _child(root, "detail")
        contact = _child(detail, "contact", required=True)
        callsign = _text(mark.callsign, "callsign")
        precision = _child(detail, "precisionlocation")
        geopointsrc = None if precision is None else precision.get("geopointsrc")
        altsrc = None if precision is None else precision.get("altsrc")
        parents = tuple(_text(link.get("uid"), "parent uid") for link in detail.findall("link")
                        if link.get("relation") == "p-s")
        source = "unknown"
        if mark.lat == 0 and mark.lon == 0:
            pass  # ATAK cold start sends SA without a usable location.
        elif (geopointsrc or "").startswith(EXTERNAL_SOURCE_PREFIX):
            source = "external"
        elif geopointsrc == "GPS":
            source = "gps"
        elif geopointsrc == "USER":
            source = "manual"
        elif parents:
            source = "external"
        elif not geopointsrc and mark.how == "h-e":
            source = "manual"
        return PhoneSA(mark.uid, callsign, mark.lat, mark.lon, mark.hae, mark.ce,
                       source, geopointsrc, altsrc, mark.time, mark.start, mark.stale,
                       contact.get("endpoint"), mark.how, mark.le, parents)
    except (ET.ParseError, LookupError):
        return "malformed or unsupported XML"
    except ValueError as exc:
        return str(exc)


@dataclass(frozen=True)
class ChatReceipt:
    """Direct GeoChat status, with identities still requiring ingress proof."""
    message_id: str
    status: str                 # delivered or read
    sender_uid: str
    destination_uid: str
    time: datetime
    start: datetime
    stale: datetime


def _chat_envelope(root):
    """Validate shared CoT chat geometry, timing and required detail."""
    if root.tag != "event" or root.get("version") != "2.0":
        raise ValueError("expected a CoT 2.0 event in protocol-0 XML")
    uid = _text(root.get("uid"), "event UID")
    time = _parse_time(root.get("time"), "time")
    start = _parse_time(root.get("start"), "start")
    stale = _parse_time(root.get("stale"), "stale")
    if stale <= max(start, time):
        raise ValueError("stale must be after start and time")
    point = _child(root, "point", required=True)
    if len(point):
        raise ValueError("point must not have child elements")
    _bounded(point.get("lat"), "lat", -90, 90)
    _bounded(point.get("lon"), "lon", -180, 180)
    for name in ("hae", "ce", "le"):
        _optional_number(point, name)
    return uid, time, start, stale, _child(root, "detail", required=True)


def _direct_chat(detail, tag):
    """Extract two-party chat identities and require a matching sender link."""
    chat = _child(detail, tag, required=True)
    other = "__chat" if tag == "__chatreceipt" else "__chatreceipt"
    if _child(detail, other) is not None:
        raise ValueError("ambiguous chat and receipt details")
    message_id = _text(chat.get("messageId"), "message ID")
    group = _child(chat, "chatgrp", required=True)
    sender = _text(group.get("uid0"), "chat sender")
    destination = _text(group.get("uid1"), "chat destination")
    # Direct chats use uid0/uid1 and must not claim group ownership.
    if (sender == destination or chat.get("groupOwner", "false") != "false"
            or any(k.startswith("uid") and k not in ("uid0", "uid1") for k in group.keys())):
        raise ValueError("chat must be direct")
    link = _child(detail, "link", required=True)
    if link.get("relation") != "p-p" or link.get("uid") != sender:
        raise ValueError("chat sender link mismatch")
    return chat, group, message_id, sender, destination


def parse_chat_receipt(data: bytes, max_bytes=DEFAULT_MAX_BYTES):
    """Parse ATAK b-t-f-d/r, or return a rejection reason.

    Verified Pixel receipts use messageId as event UID, __chatreceipt/chatgrp
    uid0 as sender, uid1 as recipient, and a p-p sender link. Receipt geometry
    is never location evidence. Group/ambiguous receipts are rejected. Safe
    XML limits are shared with the marker and chat parsers.
    """
    _byte_limit(max_bytes)
    try:
        root = _parse_document(data, max_bytes)
        uid, time, start, stale, detail = _chat_envelope(root)
        status = {"b-t-f-d": "delivered", "b-t-f-r": "read"}.get(root.get("type"))
        if status is None:
            raise ValueError("event is not a chat receipt")
        _, _, message_id, sender, destination = _direct_chat(detail, "__chatreceipt")
        if message_id != uid:
            raise ValueError("receipt message IDs differ")
        return ChatReceipt(uid, status, sender, destination, time, start, stale)
    except (ET.ParseError, LookupError):
        return "malformed or unsupported XML"
    except ValueError as exc:
        return str(exc)


@dataclass(frozen=True)
class PhoneChat:
    """Untrusted chat claims requiring PhonePosition.feed_chat authorization.

    Text/callsign are plain display data, not HTML, commands or an alert ack.
    No endpoint or geography from this packet grants authority.
    """
    uid: str
    message_id: str
    sender_uid: str
    destination_uid: str
    conversation_id: str
    callsign: str
    text: str
    time: datetime
    start: datetime
    stale: datetime
    remarks_time: datetime | None


def parse_phone_chat(data: bytes, max_bytes=DEFAULT_MAX_BYTES):
    """Parse direct b-t-f text/identities/times, or return a rejection reason.

    This uses the captured Pixel profile, not the dotted-UID fallback. All
    explicit sender/recipient/message identifiers must agree. A parsed chat
    still needs proven end0 ingress and the pinned phone identity before it
    can be displayed. It never changes a position, acknowledges an alert or
    supplies a command. The UI must render text and callsign as plain text.
    """
    _byte_limit(max_bytes)
    try:
        root = _parse_document(data, max_bytes)
        uid, time, start, stale, detail = _chat_envelope(root)
        if root.get("type") != "b-t-f":
            raise ValueError("event is not phone chat")
        chat, group, message_id, sender, destination = _direct_chat(detail, "__chat")
        conversation = _text(chat.get("id"), "conversation ID")
        if conversation != destination or group.get("id") != conversation:
            raise ValueError("chat conversation mismatch")
        if uid != f"GeoChat.{sender}.{conversation}.{message_id}":
            raise ValueError("chat event UID mismatch")
        callsign = _text(chat.get("senderCallsign"), "sender callsign")
        remarks = _child(detail, "remarks", required=True)
        if len(remarks):
            raise ValueError("chat remarks must be plain text")
        if remarks.get("to") != destination:
            raise ValueError("chat text recipient mismatch")
        if (remarks.get("sourceID", sender) != sender
                or remarks.get("source", "BAO.F.ATAK." + sender) != "BAO.F.ATAK." + sender):
            raise ValueError("chat text sender mismatch")
        remarks_time = remarks.get("time")
        if remarks_time is not None:
            remarks_time = _parse_time(remarks_time, "remarks time")
        return PhoneChat(uid, message_id, sender, destination, conversation, callsign,
                         remarks.text or "", time, start, stale, remarks_time)
    except (ET.ParseError, LookupError):
        return "malformed or unsupported XML"
    except ValueError as exc:
        return str(exc)


class _FrameTarget(_SafeTreeBuilder):
    """Track completion of a single event root with safe XML parsing."""
    def __init__(self):
        """Start with no completed event root."""
        super().__init__()
        self.complete = False

    def start(self, tag, attrs):
        """Require event as the document root before tracking nesting."""
        if self.depth == 0 and tag != "event":
            raise ValueError("expected event root")
        return super().start(tag, attrs)

    def end(self, tag):
        """Mark completion when the outer event element closes."""
        result = super().end(tag)
        self.complete = self.depth == 0
        return result


class CotStreamFramer:
    """Frame protocol-0 XML with one bounded instance per TCP connection.

    feed(bytes) returns a list of documents, each <= max_bytes. Accept UTF-8
    or ASCII-compatible XML, not UTF-16. Comments/CDATA with </event> cannot
    split a frame. DTDs, excessive depth, invalid/oversize XML and incomplete
    frames at finish() raise ValueError and poison the instance. Discard the
    whole feed batch on error and close without resynchronizing. Discard ASCII
    whitespace between documents. Trailing comments require an event before
    EOF. No clock/socket I/O.
    """

    def __init__(self, max_bytes=DEFAULT_MAX_BYTES):
        """Validate the frame limit and initialize an open stream parser."""
        _byte_limit(max_bytes)
        self.max_bytes = max_bytes
        self._closed = False
        self._reset()

    def _reset(self):
        """Discard frame state and prepare to parse the next document."""
        self._buffer = bytearray()
        self._target = _FrameTarget()
        self._parser = ET.XMLParser(target=self._target)
        self._mode = "text"
        self._quote = None
        self._token_start = 0
        self._parsed = 0

    @property
    def buffered_bytes(self):
        """Return the size of the incomplete document buffer."""
        return len(self._buffer)

    def feed(self, data):
        """Return complete documents and permanently close on invalid input."""
        if self._closed:
            raise ValueError("framer is closed")
        if not isinstance(data, bytes):
            raise ValueError("data must be bytes")
        documents = []
        try:
            # Scan once, feeding only complete lexical units to Expat. This
            # avoids both quadratic reparsing of incomplete attributes and
            # delayed end callbacks with newer Expat's reparse deferral.
            for byte in data:
                if not self._buffer and byte in b" \t\r\n":
                    continue
                if byte == 0:
                    raise ValueError("NUL/UTF-16 is not supported by the stream framer")
                if len(self._buffer) >= self.max_bytes:
                    raise ValueError("XML exceeds byte limit")
                self._buffer.append(byte)
                if not self._lexical_end(byte):
                    continue
                self._parser.feed(bytes(self._buffer[self._parsed:]))
                self._parsed = len(self._buffer)
                if self._target.complete:
                    self._parser.close()
                    documents.append(bytes(self._buffer))
                    self._reset()
        except (ET.ParseError, LookupError, ValueError) as exc:
            self._closed = True
            self._reset()
            raise ValueError(f"invalid CoT stream: {exc}") from None
        return documents

    def _lexical_end(self, byte):
        """Find complete tags, declarations, comments and CDATA sections."""
        if self._mode == "text":
            if byte == ord("<"):
                self._mode = "tag"
                self._token_start = len(self._buffer) - 1
            return False
        if self._mode == "tag":
            if len(self._buffer) - self._token_start == 2:
                if byte == ord("!"):
                    self._mode = "declaration"
                    return False
                if byte == ord("?"):
                    self._mode = "pi"
                    return False
            # A greater-than sign inside an attribute does not end the tag.
            if self._quote is not None:
                if byte == self._quote:
                    self._quote = None
            elif byte in (ord("'"), ord('"')):
                self._quote = byte
            elif byte == ord(">"):
                self._mode = "text"
                return True
            return False
        if self._mode == "declaration":
            prefix = bytes(self._buffer[self._token_start:])
            if prefix == b"<!--":
                self._mode = "comment"
            elif prefix == b"<![CDATA[":
                self._mode = "cdata"
            elif not any(p.startswith(prefix) for p in (b"<!--", b"<![CDATA[")):
                raise ValueError("DOCTYPE and other declarations are forbidden")
            return False
        # Embedded closing-event text stays inside its current lexical unit.
        ending = {"pi": b"?>", "comment": b"-->", "cdata": b"]]>"}[self._mode]
        if self._buffer.endswith(ending):
            self._mode = "text"
            return True
        return False

    def finish(self):
        """Close the framer, rejecting any incomplete document at EOF."""
        if self._closed:
            raise ValueError("framer is closed")
        self._closed = True
        incomplete = bool(self._buffer)
        self._reset()
        if incomplete:
            raise ValueError("incomplete XML at EOF")
