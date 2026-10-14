"""Decode only default mesh phone SA into manet_cot.PhoneSA.

Wire field numbers follow TAK-Product-Center/atak-civ/takproto, read 2026-10-13:
https://github.com/TAK-Product-Center/atak-civ/tree/main/takproto
README.txt defines the bf/version/bf mesh header; takmessage.proto,
cotevent.proto, detail.proto, contact.proto and precisionlocation.proto define
the subset below. This is an independent bounded reader, not generated code.
Unknown fields are skipped, protobuf groups and ambiguous singular fields are
rejected. No protocol negotiation, control, arbitrary event or extension decoder
is exposed. Contact phone numbers and unrelated detail never enter PhoneSA.
The caller must prove physical ingress, MAC/neighbor ownership and policy first.
"""

from datetime import datetime, timedelta, timezone
import struct
import xml.etree.ElementTree as ET

import manet_cot as cot

MESH_GROUP = '239.2.3.1'
MESH_PORT = 6969
MAX_SA_BYTES = 8192
MAX_DETAIL_BYTES = 4096
MAX_TAK_FIELDS = 256
MAX_TAK_TEXT = 512


def _tak_varint(data, offset):
    """Read a canonical unsigned 64-bit varint without unbounded shifts."""
    value = 0
    for index in range(10):
        if offset >= len(data):
            raise ValueError('truncated TAK varint')
        byte = data[offset]
        offset += 1
        if index == 9 and byte > 1:
            raise ValueError('TAK varint overflow')
        value |= (byte & 127) << (7 * index)
        if byte < 128:
            if index and not byte:
                raise ValueError('noncanonical TAK varint')
            return value, offset
    raise ValueError('TAK varint overflow')


def _tak_fields(data, schema, budget):
    """Skip unknown fields and retain only unique fields of the expected type."""
    offset, result = 0, {}
    while offset < len(data):
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError('too many TAK fields')
        tag, offset = _tak_varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not 1 <= number < 1 << 29:
            raise ValueError('invalid TAK field number')
        if number in schema and (wire != schema[number] or number in result):
            raise ValueError('ambiguous TAK field or wrong wire type')
        if wire == 0:
            value, offset = _tak_varint(data, offset)
        else:
            if wire == 2:
                size, offset = _tak_varint(data, offset)
            elif wire in (1, 5):
                size = 8 if wire == 1 else 4
            else:
                raise ValueError('unsupported TAK wire type')
            if size > len(data) - offset:
                raise ValueError('truncated TAK field')
            value = data[offset:offset + size] if number in schema else None
            offset += size
        if number in schema:
            result[number] = value
    return result


def _tak_text(value, limit=MAX_TAK_TEXT):
    """Decode bounded text without echoing untrusted bytes in error messages."""
    if len(value) > limit:
        raise ValueError('oversized TAK text')
    try:
        return cot._text(value.decode('utf-8'), 'TAK text', empty=True)
    except (UnicodeError, ValueError):
        raise ValueError('invalid TAK text') from None


def _tak_time(value):
    """Convert protobuf milliseconds with integer arithmetic and UTC bounds."""
    try:
        return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=value)).isoformat(
            timespec='milliseconds').replace('+00:00', 'Z')
    except (OverflowError, ValueError):
        raise ValueError('invalid TAK timestamp') from None


def _tak_detail(data, budget):
    """Project only SA detail fields; discard phone numbers and other metadata."""
    fields = _tak_fields(data, {1: 2, 2: 2, 4: 2, 8: 2}, budget)
    if 8 in fields:
        # Unknown extensions can replace precision/ancestry semantics.
        raise ValueError('unsupported TAK detail extension')
    root = ET.Element('detail')
    if fields.get(1):
        raw = _tak_text(fields[1], MAX_DETAIL_BYTES).encode('utf-8')
        detail = cot._parse_document(b'<detail>' + raw + b'</detail>', MAX_DETAIL_BYTES + 17)
        for child in detail:
            # Sent person markers are not presence, even if their type matches.
            if child.tag == 'creator':
                raise ValueError('creator-tagged event is not self SA')
            allowed = {'contact': ('endpoint', 'callsign'),
                       'precisionlocation': ('geopointsrc', 'altsrc'),
                       'link': ('uid', 'relation')}.get(child.tag)
            if allowed is not None:
                attrs = {key: _tak_text(child.attrib[key].encode('utf-8'))
                         for key in allowed if key in child.attrib}
                ET.SubElement(root, child.tag, attrs)
    for tag, number, names in (('contact', 2, {1: 'endpoint', 2: 'callsign'}),
                               ('precisionlocation', 4, {1: 'geopointsrc', 2: 'altsrc'})):
        if number in fields:
            values = _tak_fields(fields[number], {key: 2 for key in names}, budget)
            # The public protocol gives the whole XML element precedence.
            if root.find(tag) is None:
                ET.SubElement(root, tag, {name: _tak_text(values[key]) for key, name in names.items()
                                         if values.get(key)})
    return root


def decode_mesh_sa(data):
    """Return PhoneSA, None for control-only v1, or a payload-free rejection."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_SA_BYTES:
        return 'invalid or oversized mesh SA'
    try:
        if data.lstrip().startswith(b'<'):
            root = cot._parse_document(data, MAX_SA_BYTES)
            if root.find('detail/creator') is not None:
                raise ValueError('creator-tagged event is not self SA')
        else:
            if data[:3] != b'\xbf\x01\xbf':
                raise ValueError('unsupported TAK mesh magic/version')
            budget = [MAX_TAK_FIELDS]
            message = _tak_fields(data[3:], {1: 2, 2: 2}, budget)
            if 2 not in message:
                if 1 in message:
                    return None
                raise ValueError('TAK message has no event')
            schema = {1: 2, 5: 2, 6: 0, 7: 0, 8: 0, 9: 2,
                      10: 1, 11: 1, 12: 1, 13: 1, 14: 1, 15: 2}
            event = _tak_fields(message[2], schema, budget)
            attrs = {name: _tak_text(event.get(key, b''))
                     for key, name in ((1, 'type'), (5, 'uid'), (9, 'how'))}
            attrs.update({name: _tak_time(event.get(key, 0))
                          for key, name in ((6, 'time'), (7, 'start'), (8, 'stale'))})
            root = ET.Element('event', dict(attrs, version='2.0'))
            ET.SubElement(root, 'point', {
                name: str(struct.unpack('<d', event.get(key, b'\0' * 8))[0])
                for key, name in ((10, 'lat'), (11, 'lon'), (12, 'hae'), (13, 'ce'), (14, 'le'))})
            root.append(_tak_detail(event.get(15, b''), budget))
        return cot._phone_sa_from_root(root)
    except (ValueError, ET.ParseError, LookupError, OverflowError):
        # Never persist/log packet bytes, contact phone attributes or decoder
        # exceptions that might include untrusted values.
        return 'malformed or unsupported mesh SA'
