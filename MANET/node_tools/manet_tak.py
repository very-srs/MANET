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
import re
import struct
import xml.parsers.expat as expat
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


def _wire_varint(value):
    """Encode a bounded nonnegative field tag or length."""
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result + bytes([value]))


def _wire_edit(data, changes, budget):
    """Replace specified protobuf values while retaining other encoded bytes."""
    offset, result, seen = 0, bytearray(), set()
    while offset < len(data):
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError('too many rewrite fields')
        start = offset
        tag, offset = _tak_varint(data, offset)
        number, wire = tag >> 3, tag & 7
        tag_end = offset
        if not 1 <= number < 1 << 29:
            raise ValueError('invalid rewrite tag')
        if wire == 0:
            _, offset = _tak_varint(data, offset)
            value = data[tag_end:offset]
        else:
            if wire == 2:
                size, offset = _tak_varint(data, offset)
            elif wire in (1, 5):
                size = 8 if wire == 1 else 4
            else:
                raise ValueError('unsupported rewrite wire type')
            if size > len(data) - offset:
                raise ValueError('truncated rewrite value')
            value = data[offset:offset + size]
            offset += size
        if number not in changes:
            result.extend(data[start:offset])
            continue
        expected, replacement = changes[number]
        if number in seen or wire != expected:
            raise ValueError('ambiguous rewrite value')
        seen.add(number)
        value = replacement(value) if callable(replacement) else replacement
        result.extend(data[start:tag_end])
        result.extend((_wire_varint(len(value)) if wire == 2 else b'') + value)
    for number, (wire, replacement) in changes.items():
        if number not in seen:
            value = replacement(b'') if callable(replacement) else replacement
            result.extend(_wire_varint(number << 3 | wire))
            result.extend((_wire_varint(len(value)) if wire == 2 else b'') + value)
    return bytes(result)


def _xml_position(data, point, source, *, detail_only=False):
    """Edit only start-tag attribute values, preserving all other XML bytes."""
    data.decode('utf-8')
    declaration = re.match(rb'\s*<\?xml\b[^?]*\?>', data)
    if declaration:
        encoding = re.search(rb'encoding\s*=\s*["\x27]([^"\x27]+)', declaration[0])
        if encoding and encoding[1].lower() not in (b'utf-8', b'utf8', b'us-ascii'):
            raise ValueError('unsupported rewrite encoding')
    prefix = b'<detail>' if detail_only else b''
    document = prefix + data + (b'</detail>' if detail_only else b'')
    cot._parse_document(document, MAX_SA_BYTES + 17)
    parser = expat.ParserCreate()
    stack, edits = [], []
    precision_seen = False
    # Validated XML supplies uniqueness and rejects entities/DTDs. Restrict the
    # lexical editor to ordinary UTF-8 attribute names and quote-delimited tags.
    tag_pattern = re.compile(rb'<(?:[^<>"\x27\x00]|"[^"\x00]*"|\x27[^\x27\x00]*\x27)*>')
    attr_pattern = re.compile(rb'([^\s=<>"\x27/]+)(\s*=\s*)(["\x27])(.*?)\3', re.DOTALL)
    escaped = source.replace('&', '&amp;').replace('<', '&lt;').replace('"', '&quot;').replace("'", '&apos;').encode()

    def start(name, attrs):
        """Collect byte spans for the two direct location elements."""
        nonlocal precision_seen
        stack.append(name)
        path = tuple(stack)
        precision_path = ('detail', 'precisionlocation') if detail_only else ('event', 'detail', 'precisionlocation')
        values = point if not detail_only and path == ('event', 'point') else (
            {'geopointsrc': escaped, 'altsrc': escaped} if path == precision_path else None)
        if values is None:
            return
        precision_seen |= path == precision_path
        offset = parser.CurrentByteIndex
        match = tag_pattern.match(document, offset)
        if match is None:
            raise ValueError('unsupported XML start tag')
        tag = match[0]
        found = set()

        def attribute(item):
            """Keep each unrelated attribute and its original quoting."""
            key = item[1].decode('utf-8')
            if key not in values:
                return item[0]
            found.add(key)
            value = values[key]
            return item[1] + item[2] + item[3] + (value if isinstance(value, bytes) else str(value).encode()) + item[3]

        tag = attr_pattern.sub(attribute, tag)
        extra = b''.join(b' ' + key.encode() + b'="' + (value if isinstance(value, bytes) else str(value).encode()) + b'"'
                         for key, value in values.items() if key not in found)
        end = -2 if tag.endswith(b'/>') else -1
        edits.append((offset, match.end(), tag[:end] + extra + tag[end:]))

    def end(name):
        """Add missing precision metadata only to a full CoT event."""
        if not detail_only and tuple(stack) == ('event', 'detail') and not precision_seen:
            offset = parser.CurrentByteIndex
            edits.append((offset, offset, b'<precisionlocation geopointsrc="' + escaped + b'" altsrc="' + escaped + b'"/>'))
        stack.pop()

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.Parse(document, True)
    for start, end, value in sorted(edits, reverse=True):
        document = document[:start] + value + document[end:]
    return document[len(prefix):-9] if detail_only else document


def rewrite_mesh_sa(data, fix, source):
    """Replace only SA geography/precision, returning original bytes on doubt."""
    try:
        if not isinstance(decode_mesh_sa(data), cot.PhoneSA) or not cot._usable(fix):
            return data
        if fix.source == 'gnss' and fix.phone_uid is not None:
            return data
        cot._text(source, 'rewrite source')
        if not source.startswith(cot.EXTERNAL_SOURCE_PREFIX) or len(source.encode()) > MAX_TAK_TEXT:
            return data
        root = ET.Element('event')
        cot._point(root, fix)
        point = root.find('point').attrib
        if data.lstrip().startswith(b'<'):
            result = _xml_position(data, point, source)
        else:
            budget = [MAX_TAK_FIELDS]
            def precision(value):
                """Retain unknown precision fields."""
                return _wire_edit(value, {1: (2, source.encode()), 2: (2, source.encode())}, budget)
            def detail(value):
                """Update typed precision and any overriding XML precision."""
                fields = _tak_fields(value, {1: 2}, [MAX_TAK_FIELDS])
                changes = {4: (2, precision)}
                if 1 in fields:
                    changes[1] = (2, lambda raw: _xml_position(raw, point, source, detail_only=True))
                return _wire_edit(value, changes, budget)
            def event(value):
                """Rebuild location fields without touching the SA envelope."""
                changes = {key: (1, struct.pack('<d', float(point[name]))) for key, name in
                           ((10, 'lat'), (11, 'lon'), (12, 'hae'), (13, 'ce'), (14, 'le'))}
                changes[15] = (2, detail)
                return _wire_edit(value, changes, budget)
            result = data[:3] + _wire_edit(data[3:], {2: (2, event)}, budget)
        if len(result) > MAX_SA_BYTES or not isinstance(decode_mesh_sa(result), cot.PhoneSA):
            return data
        return result
    except (ValueError, ET.ParseError, expat.ExpatError, LookupError, OverflowError, struct.error):
        return data
