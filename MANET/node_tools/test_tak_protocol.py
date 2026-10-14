"""Synthetic mesh v1 fixtures and adversarial SA decoder coverage."""
from datetime import datetime, timedelta, timezone
import struct
import unittest
import xml.etree.ElementTree as ET

import manet_cot as cot
import manet_tak as tak

TAK_TIME = datetime(2026, 10, 7, tzinfo=timezone.utc)


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result + bytes([value]))


def field(number, value, wire=2):
    tag = varint(number << 3 | wire)
    if wire == 0:
        return tag + varint(value)
    if wire == 1:
        return tag + struct.pack('<d', value)
    if isinstance(value, str):
        value = value.encode()
    return tag + varint(len(value)) + value


def sa_fixture(*, address='192.0.2.2', uid='synthetic-phone', callsign='SYNTHETIC',
               point=(40., -105., 1600.), source=None, alt='???', how='h-e',
               stamp=TAK_TIME, xml_detail='', contact_xml=False, endpoint=None):
    """Build equivalent v0/v1 from public schema numbers, with no captured data."""
    endpoint = endpoint if endpoint is not None else address + ':4242:tcp'
    ms = int(stamp.timestamp() * 1000)
    event = b''.join((field(1, 'a-f-G-U-C'), field(5, uid), field(6, ms, 0),
                      field(7, ms, 0), field(8, ms + 75000, 0), field(9, how)))
    for number, value in zip(range(10, 15), (*point, cot.UNKNOWN, cot.UNKNOWN)):
        # Proto3 omits scalar zeros, including valid equator/prime-meridian fixes.
        if value:
            event += field(number, value, 1)
    root = ET.Element('event', version='2.0', uid=uid, type='a-f-G-U-C', how=how,
                      time=cot._stamp(stamp), start=cot._stamp(stamp),
                      stale=cot._stamp(stamp + timedelta(seconds=75)))
    ET.SubElement(root, 'point', {name: str(value) for name, value in zip(
        ('lat', 'lon', 'hae', 'ce', 'le'), (*point, cot.UNKNOWN, cot.UNKNOWN))})
    detail = ET.SubElement(root, 'detail')
    contact = ET.SubElement(detail, 'contact', endpoint=endpoint, callsign=callsign)
    precision = ET.SubElement(detail, 'precisionlocation', altsrc=alt)
    if source is not None:
        precision.set('geopointsrc', source)
    if contact_xml:
        # A fictional phone attribute forces the real whole-contact XML fallback.
        contact.set('phone', '+15550100000')
        xml_detail = ET.tostring(contact, encoding='unicode') + xml_detail
        binary_detail = b''
    else:
        binary_detail = field(2, field(1, endpoint) + field(2, callsign))
    binary_detail += field(4, (field(1, source) if source is not None else b'') + field(2, alt))
    if xml_detail:
        binary_detail += field(1, xml_detail)
        for child in ET.fromstring('<detail>' + xml_detail + '</detail>'):
            if child.tag != 'contact':
                detail.append(child)
    event += field(15, binary_detail)
    return b'\xbf\x01\xbf' + field(2, event), ET.tostring(root)


class TakProtocolTests(unittest.TestCase):
    def test_real_shaped_sa_uses_the_same_xml_classifier_without_phone_data(self):
        for source, how in ((None, 'h-e'), ('GPS', 'm-g'), ('USER', 'h-e'),
                            ('MANET:radio-1', 'h-e'), ('unrecognized', 'h-e')):
            for contact_xml in (False, True):
                with self.subTest(source=source, contact_xml=contact_xml):
                    data, xml = sa_fixture(source=source, how=how, contact_xml=contact_xml,
                        xml_detail='<uid Droid="SYNTHETIC"/><__group name="Blue" role="Team Member"/>'
                                   '<status battery="50"/><takv version="synthetic"/>')
                    result = tak.decode_mesh_sa(data)
                    self.assertIsInstance(result, cot.PhoneSA)
                    self.assertEqual(result, cot.parse_self_sa(xml))
                    self.assertNotIn('+15550100000', repr(result))
                    self.assertEqual(tak.decode_mesh_sa(xml), result)

    def test_zero_scalar_defaults_and_parent_echo_are_preserved(self):
        for point in ((0., 0., 0.), (0., -105., 0.), (40., 0., 0.)):
            data, xml = sa_fixture(point=point, xml_detail='<link relation="p-s" uid="MANET:radio"/>')
            self.assertEqual(tak.decode_mesh_sa(data), cot.parse_self_sa(xml))

    def test_xml_detail_precision_precedes_typed_precision(self):
        data, _ = sa_fixture(source='GPS', xml_detail='<precisionlocation geopointsrc="USER" altsrc="USER"/>')
        result = tak.decode_mesh_sa(data)
        self.assertEqual((result.source, result.geopointsrc, result.altsrc), ('manual', 'USER', 'USER'))

    def test_unknown_fields_are_skipped_but_control_only_is_not_presence(self):
        data, xml = sa_fixture()
        self.assertEqual(tak.decode_mesh_sa(data + field(100, b'opaque')), cot.parse_self_sa(xml))
        self.assertIsNone(tak.decode_mesh_sa(b'\xbf\x01\xbf' + field(1, b'')))

    def test_unknown_magic_versions_oversize_and_every_truncation(self):
        data, _ = sa_fixture()
        for bad in (b'', b'\xbf\x00\xbf', b'\xbf\x02\xbf' + data[3:],
                    b'\xbf\x81\x00\xbf' + data[3:], b'\xbe\x01\xbf' + data[3:],
                    b'\xbf\x01\xbe' + data[3:], b'x' * (tak.MAX_SA_BYTES + 1)):
            self.assertIsInstance(tak.decode_mesh_sa(bad), str)
        for size in range(len(data)):
            self.assertIsInstance(tak.decode_mesh_sa(data[:size]), str, size)

    def test_protobuf_overflow_groups_duplicates_types_and_work_limit(self):
        data, _ = sa_fixture()
        bad_payloads = (b'\0', b'\x80' * 11, b'\x80' * 9 + b'\x02', b'\x08\x80',
                        b'\x13', field(2, 3, 0), data[3:] * 2,
                        field(100, b'') * (tak.MAX_TAK_FIELDS + 1),
                        b'\x12\xff\xff\xff\xff\x0f')
        for payload in bad_payloads:
            self.assertIsInstance(tak.decode_mesh_sa(b'\xbf\x01\xbf' + payload), str)

    def test_bad_geometry_time_and_text_reject_without_payload_in_reason(self):
        for options in ({'point': (float('nan'), -105., 0.)}, {'point': (91., -105., 0.)},
                        {'callsign': 'x' * 513}, {'uid': ''},
                        {'xml_detail': '<remarks>' + 'x' * 4096 + '</remarks>'},
                        {'xml_detail': '<creator uid="someone"/>'}):
            data, _ = sa_fixture(**options)
            result = tak.decode_mesh_sa(data)
            self.assertIsInstance(result, str)
            self.assertEqual(result, 'malformed or unsupported mesh SA')
        for event in (field(6, (1 << 64) - 1, 0), field(1, b'\xff')):
            self.assertIsInstance(tak.decode_mesh_sa(b'\xbf\x01\xbf' + field(2, event)), str)

    def test_xml_security_bounds_and_sa_only(self):
        for data in (b'<!DOCTYPE event [<!ENTITY x "sensitive">]><event>&x;</event>',
                     b'<event>' * 40 + b'</event>' * 40, b'<event', b'<other/>'):
            self.assertIsInstance(tak.decode_mesh_sa(data), str)
        data, xml = sa_fixture(xml_detail='<creator uid="creator"/>', contact_xml=True)
        for payload in (data, xml):
            self.assertIsInstance(tak.decode_mesh_sa(payload), str)


if __name__ == '__main__':
    unittest.main()
