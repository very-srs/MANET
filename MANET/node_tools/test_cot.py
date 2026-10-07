"""CoT wire contract and hostile XML tests; no sockets or device assumptions."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from manet_cot import (
    CHAT_STALE_S, CONTACT_REFRESH_S, CONTACT_STALE_S, DEFAULT_MAX_BYTES, MAX_DEPTH, UNKNOWN,
    CotStreamFramer, ErrorEstimate, Fix, MarkCandidate, PhoneSA,
    contact_refresh_due, geochat_report, parse_marker, parse_self_sa,
    hidden_contact, ChatReceipt, parse_chat_receipt, PhoneChat, parse_phone_chat,
    retire_marker, external_position, EXTERNAL_POSITION_PORT,
)


NOW = datetime(2026, 10, 6, 18, 23, 45, 123456, tzinfo=timezone.utc)
FIX = Fix(39.75, -104.99, "gnss", 1610.5,
          ErrorEstimate(8, 0.90), ErrorEstimate(12, 0.90))
SAMPLES = Path(__file__).resolve().parents[2] / "review-collab/atak-20261006/samples"


def marker():
    # Independent input fixture: production no longer builds radio map markers.
    return b'''<?xml version="1.0" encoding="utf-8"?>
<event version="2.0" uid="MANET-RADIO-3" type="a-f-G-E" how="m-g"
 time="2026-10-06T18:23:45.123Z" start="2026-10-06T18:23:45.123Z"
 stale="2026-10-06T18:24:45.123Z"><point lat="39.75" lon="-104.99"
 hae="1610.5" ce="8" le="12"/><detail><contact callsign="RADIO-3"/>
 <remarks>Test observation</remarks></detail></event>'''


def contact(endpoint_ip="10.42.0.5", port=4242, **kwargs):
    args = dict(uid="MANET-RADIO-3", callsign="RADIO-3", now=NOW,
                contact_endpoint=(endpoint_ip, port))
    args.update(kwargs)
    return hidden_contact(**args)


def edited(*, event=None, point=None):
    root = ET.fromstring(marker())
    root.attrib.update(event or {})
    root.find("point").attrib.update(point or {})
    return ET.tostring(root)


class BuilderTests(unittest.TestCase):
    def test_hidden_contact_without_any_position_or_trust_input(self):
        data = contact()
        root = ET.fromstring(data)
        self.assertTrue(data.startswith(b"<?xml "))
        self.assertEqual(root.get("type"), "a-f-G-E")
        self.assertEqual(root.get("uid"), "MANET-RADIO-3")
        self.assertEqual(root.find("point").attrib, {
            "lat": "0", "lon": "0", "hae": "9999999", "ce": "9999999", "le": "9999999"})
        self.assertEqual(root.find("detail/contact").attrib,
                         {"callsign": "RADIO-3", "endpoint": "10.42.0.5:4242:tcp"})
        self.assertIn("Send a point", root.findtext("detail/remarks"))
        self.assertIn("ZERO", parse_marker(data))
        self.assertIsNone(root.find("detail/TakControl"))
        for bad in (("10.0.0.1",), "10.0.0.1:4242", (12345, 4242), None):
            with self.assertRaises(ValueError):
                contact(contact_endpoint=bad)

    def test_truthful_external_how_and_unresolved_geography(self):
        for source, how in (("gnss", "m-g"), ("ranged", "m-f"), ("manual", "h-e")):
            fix = replace(FIX, source=source, orientation_resolved=True, mirror_resolved=True)
            self.assertEqual(ET.fromstring(external_position("radio", fix, NOW)).get("how"), how)
            for flag in ("orientation_resolved", "mirror_resolved"):
                self.assertIsNone(external_position("radio", replace(fix, **{flag: False}), NOW))
        for orientation, mirror in ((None, None), (True, None), (None, True)):
            fix = replace(FIX, source="ranged", orientation_resolved=orientation, mirror_resolved=mirror)
            self.assertIsNone(external_position("radio", fix, NOW))

    def test_unknown_values_and_confidence(self):
        candidate = parse_marker(external_position("radio", Fix(39, -105, "manual"), NOW))
        self.assertEqual((candidate.hae, candidate.ce, candidate.le), (None, None, None))
        for confidence in (0.50, 0.68, 0.95):
            fix = replace(FIX, horizontal_error=ErrorEstimate(4, confidence),
                          vertical_error=ErrorEstimate(5, confidence))
            candidate = parse_marker(external_position("radio", fix, NOW))
            self.assertEqual((candidate.ce, candidate.le), (None, None))
        candidate = parse_marker(external_position("radio", replace(FIX, hae=None), NOW))
        self.assertEqual((candidate.hae, candidate.ce, candidate.le), (None, 8, None))

    def test_contact_refresh_policy(self):
        self.assertEqual(CONTACT_REFRESH_S, 30)
        self.assertEqual(CONTACT_STALE_S, 3600)
        self.assertTrue(contact_refresh_due(0))
        self.assertFalse(contact_refresh_due(29.999, 0))
        self.assertTrue(contact_refresh_due(30, 0))
        self.assertTrue(contact_refresh_due(300, 0))
        self.assertTrue(contact_refresh_due(1, 0, changed=True))
        self.assertTrue(contact_refresh_due(1, 0, phone_reappeared=True))
        root = ET.fromstring(contact())
        self.assertEqual(root.get("stale"), "2026-10-06T19:23:45.123Z")
        for kwargs in ({"changed": "yes"}, {"phone_reappeared": 1}):
            with self.assertRaises(ValueError):
                contact_refresh_due(0, **kwargs)
        # A failed send does not change the caller's last-success timestamp.
        self.assertTrue(contact_refresh_due(31, 0))
        self.assertFalse(contact_refresh_due(31, 30))
        for now, last in ((0, 1), (float("nan"), None), (5, float("inf"))):
            with self.assertRaises(ValueError):
                contact_refresh_due(now, last)

    def test_retire_event_matches_atak_delete_importer(self):
        data = retire_marker('old<&"', "retire-task-1", NOW)
        root = ET.fromstring(data)
        self.assertEqual(root.get("type"), "t-x-d-d")
        self.assertEqual(root.get("uid"), "retire-task-1")
        self.assertEqual(root.get("how"), "m-g")
        self.assertEqual(root.get("time"), "2026-10-06T18:23:45.123Z")
        self.assertEqual(root.get("stale"), "2026-10-06T18:24:45.123Z")
        self.assertEqual(root.find("detail/link").attrib,
                         {"uid": 'old<&"', "relation": "none", "type": "none"})
        self.assertIsNotNone(root.find("detail/__forcedelete"))
        self.assertEqual(root.find("point").attrib,
                         {"lat": "0", "lon": "0", "hae": "9999999", "ce": "9999999", "le": "9999999"})
        self.assertIsInstance(parse_marker(data), str)
        framer = CotStreamFramer()
        self.assertEqual(framer.feed(data), [data])
        framer.finish()
        for target, task in (("", "task"), ("target", ""), ("same", "same")):
            with self.assertRaises(ValueError):
                retire_marker(target, task, NOW)

    def test_geochat_golden_hidden_location_and_routing(self):
        # Dots in UIDs must not break ATAK's legacy dotted-UID fallbacks:
        # explicit sourceID, messageId and conversation id carry the identity.
        text = 'RADIO-4: 35 m < 40 m & "clear"\nMeasured at 18:23:44Z.'
        data = geochat_report("radio.3", "RADIO-3", "phone.3", text, "msg.42", NOW)
        root = ET.fromstring(data)
        self.assertEqual(root.get("type"), "b-t-f")
        self.assertEqual(root.get("uid"), "GeoChat.radio.3.phone.3.msg.42")
        self.assertEqual(root.get("how"), "h-g-i-g-o")
        self.assertEqual(root.get("stale"), "2026-10-07T18:23:45.123Z")
        self.assertEqual(CHAT_STALE_S, 86400)
        self.assertEqual(root.find("point").attrib, {
            "lat": "0", "lon": "0", "hae": "9999999", "ce": "9999999", "le": "9999999",
        })
        self.assertEqual(root.find("detail/__chat").attrib, {
            "id": "phone.3", "messageId": "msg.42", "senderCallsign": "RADIO-3",
            "chatroom": "phone.3", "groupOwner": "false", "parent": "RootContactGroup",
        })
        self.assertEqual(root.find("detail/__chat/chatgrp").attrib,
                         {"id": "phone.3", "uid0": "radio.3", "uid1": "phone.3"})
        self.assertEqual(root.find("detail/link").attrib,
                         {"uid": "radio.3", "type": "a-f-G-E", "relation": "p-p"})
        self.assertEqual(root.find("detail/remarks").attrib, {
            "source": "BAO.F.ATAK.radio.3", "sourceID": "radio.3", "to": "phone.3",
            "time": "2026-10-06T18:23:45.123Z",
        })
        self.assertEqual(root.findtext("detail/remarks"), text)
        self.assertIsNone(root.find("detail/contact"))
        self.assertIsNone(root.find("detail/__serverdestination"))
        self.assertEqual(parse_marker(data), "event is not a point marker")
        self.assertEqual(data, geochat_report("radio.3", "RADIO-3", "phone.3", text, "msg.42", NOW))

    def test_timezones_milliseconds_and_expiry(self):
        local = NOW.astimezone(timezone(timedelta(hours=-6)))
        self.assertEqual(contact(now=local), contact())
        root = ET.fromstring(contact(now=NOW.replace(microsecond=999999), stale_s=0.001))
        self.assertEqual(root.get("start"), "2026-10-06T18:23:45.999Z")
        self.assertEqual(root.get("stale"), "2026-10-06T18:23:46.000Z")
        for invalid in (0, -1, 0.0001, float("nan"), float("inf"), 1e100, True):
            with self.subTest(stale_s=invalid), self.assertRaises(ValueError):
                contact(stale_s=invalid)
        with self.assertRaises(ValueError):
            contact(now=NOW.replace(tzinfo=None))

    def test_output_escaping_and_invalid_characters(self):
        callsign = 'RADIO <&> "é"'
        uid = 'uid<&"'
        root = ET.fromstring(contact(callsign=callsign, uid=uid))
        self.assertEqual((root.find("detail/contact").get("callsign"), root.get("uid")), (callsign, uid))
        for bad in ("", "  ", "bad\x00", "bad\ud800", "bad\ufffe"):
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError):
                contact(callsign=bad)
        with self.assertRaises(ValueError):
            geochat_report("r", "R", "p", "text\x01", "m", NOW)

    def test_invalid_fix_numbers_and_confidences(self):
        for name, values in {
            "lat": (91, -91, float("nan"), float("inf"), True),
            "lon": (181, -181, float("nan")),
            "hae": (-14001, 76001, UNKNOWN, float("inf")),
            "source": ("unknown",), "orientation_resolved": ("false", 1),
        }.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    external_position("radio", replace(FIX, **{name: value}), NOW)
        for error in (ErrorEstimate(-1, 0.9), ErrorEstimate(float("nan"), 0.9),
                      ErrorEstimate(UNKNOWN, 0.9), ErrorEstimate(3, 0),
                      ErrorEstimate(3, 90), ErrorEstimate(3, float("inf")), 3):
            with self.subTest(error=error), self.assertRaises(ValueError):
                external_position("radio", replace(FIX, horizontal_error=error), NOW)

    def test_invalid_endpoints(self):
        for value in ("224.0.0.1", "0.0.0.0", "127.0.0.1", "255.255.255.255",
                      "10.0.0.1:4242:tcp", "::1", "radio.example", "bad<&"):
            with self.subTest(ip=value), self.assertRaises(ValueError):
                contact(endpoint_ip=value)
        for value in (0, 65536, 42.5, "4242", True):
            with self.subTest(port=value), self.assertRaises(ValueError):
                contact(port=value)


class ExternalPositionTests(unittest.TestCase):
    def test_cot_contract_and_distinct_parent_uid(self):
        root = ET.fromstring(external_position("radio-1", FIX, NOW))
        self.assertEqual(EXTERNAL_POSITION_PORT, 4349)
        self.assertEqual(root.get("uid"), "radio-1.external-position")
        self.assertEqual(root.get("type"), "a-f-G-E")
        self.assertEqual(root.get("how"), "m-g")
        self.assertEqual(root.get("stale"), "2026-10-06T18:23:55.123Z")
        self.assertEqual({k: float(v) for k, v in root.find("point").attrib.items()},
                         {"lat": FIX.lat, "lon": FIX.lon, "hae": FIX.hae, "ce": 8, "le": 12})
        self.assertEqual(root.find("detail/precisionlocation").attrib,
                         {"geopointsrc": "MANET:gnss", "altsrc": "MANET:gnss"})
        self.assertEqual(root.findtext("detail/remarks"), "MANET radio GPS")
        self.assertEqual(root.find("detail/track").attrib, {})
        self.assertIsNone(root.find("detail/contact"))
        self.assertIsNone(root.find("detail/extendedGpsDetails"))  # no invented time/satellites

    def test_manual_unknown_accuracy_and_resolved_ranging(self):
        fix = Fix(39, -105, "manual", phone_uid="phone", remarks="location age 300.0 s")
        root = ET.fromstring(external_position("radio", fix, NOW))
        self.assertEqual(root.get("how"), "h-e")
        self.assertEqual(root.find("detail/precisionlocation").attrib,
                         {"geopointsrc": "MANET:manual", "altsrc": "???"})
        self.assertEqual(root.findtext("detail/remarks"), "MANET user location")
        for field in ("hae", "ce", "le"):
            self.assertEqual(float(root.find("point").get(field)), UNKNOWN)
        ranged = replace(FIX, source="ranged", orientation_resolved=True, mirror_resolved=True)
        root = ET.fromstring(external_position("radio", ranged, NOW))
        self.assertEqual(root.get("how"), "m-f")
        self.assertEqual(root.find("detail/precisionlocation").get("geopointsrc"), "MANET:ranged")
        root = ET.fromstring(external_position("radio", replace(FIX, horizontal_error=ErrorEstimate(3, .68)), NOW))
        self.assertEqual(float(root.find("point").get("ce")), UNKNOWN)

    def test_unusable_invalid_and_phone_gps_never_emit(self):
        self.assertIsNone(external_position("radio", None, NOW))
        self.assertIsNone(external_position("radio", replace(FIX, source="ranged"), NOW))
        self.assertIsNone(external_position("radio", replace(FIX, mirror_resolved=False), NOW))
        for fix in (replace(FIX, lat=0, lon=0), replace(FIX, lat=91),
                    replace(FIX, lon=float("nan")), replace(FIX, phone_uid="phone")):
            with self.assertRaises(ValueError):
                external_position("radio", fix, NOW)
        with self.assertRaises(ValueError):
            external_position("phone", Fix(39, -105, "manual", phone_uid="phone"), NOW)
        with self.assertRaises(ValueError):
            external_position("radio", FIX, NOW.replace(tzinfo=None))
        root = ET.fromstring(external_position('r<&"', FIX, NOW))
        self.assertEqual(root.get("uid"), 'r<&".external-position')

    def test_self_sa_echo_classification_and_presence_without_position(self):
        for how in ("m-g", "h-e"):
            root = ET.fromstring((SAMPLES / "self-sa-gps.xml").read_bytes())
            root.set("how", how)
            detail = root.find("detail")
            ET.SubElement(detail, "link", {"relation": "p-s", "uid": "radio.external-position", "type": "a-f-G-E"})
            parsed = parse_self_sa(ET.tostring(root))
            self.assertEqual(parsed.source, "gps")  # explicit source outranks a leftover parent
            self.assertEqual(parsed.parent_uids, ("radio.external-position",))
            detail.remove(detail.find("link"))
            detail.find("precisionlocation").set("geopointsrc", "MANET:manual")
            self.assertEqual(parse_self_sa(ET.tostring(root)).source, "external")
        for key in ("lat", "lon"):
            root.find("point").set(key, "0")
        for key in ("hae", "ce", "le"):
            root.find("point").set(key, str(UNKNOWN))
        self.assertIsInstance(parse_self_sa(ET.tostring(root)), PhoneSA)
        self.assertIsInstance(parse_marker(ET.tostring(root)), str)


def receipt_wire(status="r"):
    # Independent malformed-input fixture; real Pixel captures are tested below.
    return f'''<event version="2.0" uid="message.42" type="b-t-f-{status}" how="m-g"
 time="2026-10-06T18:23:45.123Z" start="2026-10-06T18:23:45.123Z"
 stale="2026-10-07T18:23:45.123Z"><point lat="0" lon="0" hae="9999999" ce="9999999" le="9999999"/>
 <detail><__chatreceipt id="radio.3" messageId="message.42" groupOwner="false">
 <chatgrp id="radio.3" uid0="phone.3" uid1="radio.3"/></__chatreceipt>
 <link uid="phone.3" type="a-f-G-U-C" relation="p-p"/>
 <__serverdestination destinations="10.42.0.20:4242:tcp:phone.3"/></detail></event>'''.encode()


class ChatReceiptTests(unittest.TestCase):
    def test_direct_delivered_read_and_tcp_connection_framing(self):
        for kind, status in (("d", "delivered"), ("r", "read")):
            data = receipt_wire(kind)
            result = parse_chat_receipt(data)
            self.assertIsInstance(result, ChatReceipt)
            self.assertEqual((result.message_id, result.sender_uid, result.destination_uid, result.status),
                             ("message.42", "phone.3", "radio.3", status))
            self.assertEqual(result.time, NOW.replace(microsecond=123000))
            self.assertEqual(result.stale - result.time, timedelta(days=1))
            # Device opens a separate TCP connection for each receipt.
            framer = CotStreamFramer()
            self.assertEqual(framer.feed(data[:-3]), [])
            self.assertEqual(framer.feed(data[-3:]), [data])
            framer.finish()
        framer = CotStreamFramer()
        self.assertEqual(framer.feed(receipt_wire("d") + receipt_wire("r")),
                         [receipt_wire("d"), receipt_wire("r")])

    def test_rejects_ambiguous_routing_ids_and_envelopes(self):
        for path, changes in ((".", {"uid": "wrong"}), (".", {"type": "b-t-f"}),
                              (".", {"time": "yesterday"}), (".", {"stale": "2020-01-01T00:00:00Z"}),
                              ("detail/link", {"uid": "impostor"}),
                              ("detail/__chatreceipt/chatgrp", {"uid2": "another"}),
                              ("detail/__chatreceipt/chatgrp", {"uid1": "phone.3"})):
            root = ET.fromstring(receipt_wire())
            root.find(path).attrib.update(changes)
            self.assertIsInstance(parse_chat_receipt(ET.tostring(root)), str)
        for path in ("point", "detail", "detail/link", "detail/__chatreceipt",
                     "detail/__chatreceipt/chatgrp"):
            root = ET.fromstring(receipt_wire())
            node = root.find(path)
            parent = root if "/" not in path else root.find(path.rsplit("/", 1)[0])
            parent.append(ET.fromstring(ET.tostring(node)))
            self.assertIsInstance(parse_chat_receipt(ET.tostring(root)), str)

    def test_safe_parser_limits_and_not_a_marker(self):
        data = receipt_wire()
        self.assertIsInstance(parse_marker(data), str)
        for bad in (data + data, data[:-1], data.decode(), b"garbage",
                    b'<!DOCTYPE event [<!ENTITY x "oops">]>' + data,
                    ('<!DOCTYPE event><event/>').encode('utf-16'),
                    b"<event>" + b"<x>" * MAX_DEPTH):
            self.assertIsInstance(parse_chat_receipt(bad), str)
        self.assertIn("byte limit", parse_chat_receipt(data, len(data) - 1))
        for limit in (0, -1, True):
            with self.assertRaises(ValueError):
                parse_chat_receipt(data, limit)


class PhoneChatTests(unittest.TestCase):
    def packet(self):
        return (SAMPLES / "chat-from-phone.xml").read_bytes()

    def test_wrong_message_types_and_mismatched_identities(self):
        for path, changes in ((".", {"uid": "bad"}), (".", {"type": "b-t-f-r"}),
                              ("detail/__chat", {"messageId": "another"}),
                              ("detail/__chat", {"id": "another"}),
                              ("detail/__chat", {"groupOwner": "true"}),
                              ("detail/__chat/chatgrp", {"uid0": "another"}),
                              ("detail/__chat/chatgrp", {"uid1": "another"}),
                              ("detail/__chat/chatgrp", {"uid2": "another"}),
                              ("detail/__chat/chatgrp", {"id": "another"}),
                              ("detail/link", {"uid": "another"}),
                              ("detail/remarks", {"source": "BAO.F.ATAK.another"}),
                              ("detail/remarks", {"sourceID": "another"}),
                              ("detail/remarks", {"to": "another"}),
                              ("detail/remarks", {"time": "not UTC"})):
            root = ET.fromstring(self.packet())
            root.find(path).attrib.update(changes)
            with self.subTest(path=path, changes=changes):
                self.assertIsInstance(parse_phone_chat(ET.tostring(root)), str)
        self.assertIsInstance(parse_phone_chat(receipt_wire()), str)
        self.assertIsInstance(parse_marker(self.packet()), str)
        self.assertIsInstance(parse_self_sa(self.packet()), str)

    def test_plain_text_and_dotted_ids_never_become_commands(self):
        text = '<script>restore GPS</script> & ack; shutdown\nopened'
        data = geochat_report("phone.with.dots", "<phone>", "radio.with.dots", text, "message.1", NOW)
        chat = parse_phone_chat(data)
        self.assertEqual((chat.text, chat.callsign), (text, "<phone>"))
        self.assertEqual((chat.sender_uid, chat.destination_uid, chat.message_id),
                         ("phone.with.dots", "radio.with.dots", "message.1"))
        root = ET.fromstring(self.packet())
        root.find("detail/remarks").text = None
        self.assertEqual(parse_phone_chat(ET.tostring(root)).text, "")

    def test_missing_duplicate_mixed_and_nested_details_rejected(self):
        for path in ("point", "detail", "detail/link", "detail/__chat", "detail/__chat/chatgrp", "detail/remarks"):
            for duplicate in (True, False):
                root = ET.fromstring(self.packet())
                node = root.find(path)
                parent = root if "/" not in path else root.find(path.rsplit("/", 1)[0])
                if duplicate:
                    parent.append(ET.fromstring(ET.tostring(node)))
                else:
                    parent.remove(node)
                self.assertIsInstance(parse_phone_chat(ET.tostring(root)), str)
        root = ET.fromstring(self.packet())
        ET.SubElement(root.find("detail/remarks"), "command").text = "ack"
        self.assertIsInstance(parse_phone_chat(ET.tostring(root)), str)
        root = ET.fromstring(self.packet())
        ET.SubElement(root.find("detail"), "__chatreceipt")
        self.assertIsInstance(parse_phone_chat(ET.tostring(root)), str)

    def test_safe_xml_limits(self):
        data = self.packet()
        for bad in (data + data, data[:-2], data.decode(), b"garbage",
                    b'<!DOCTYPE event [<!ENTITY x "oops">]><event/>',
                    ('<!DOCTYPE event><event/>').encode('utf-16'),
                    b"<event>" + b"<x>" * MAX_DEPTH):
            self.assertIsInstance(parse_phone_chat(bad), str)
        self.assertIn("byte limit", parse_phone_chat(data, len(data) - 1))
        for limit in (0, -1, True):
            with self.assertRaises(ValueError):
                parse_phone_chat(data, limit)


class ParserTests(unittest.TestCase):
    def reject(self, data, reason=None, **kwargs):
        result = parse_marker(data, **kwargs)
        self.assertIsInstance(result, str)
        self.assertTrue(result)
        if reason is not None:
            self.assertIn(reason, result)

    def test_byte_limit_and_wrong_inputs(self):
        data = marker()
        self.assertIsInstance(parse_marker(data, len(data)), MarkCandidate)
        self.reject(data, "byte limit", max_bytes=len(data) - 1)
        self.reject(b"x" * (DEFAULT_MAX_BYTES + 1), "byte limit")
        self.reject(data.decode(), "bytes")
        for limit in (0, -1, 3.5, True):
            with self.assertRaises(ValueError):
                parse_marker(data, limit)

    def test_malformed_binary_incomplete_and_multiple_documents(self):
        for data in (b"", b"<event", marker()[:-1], b"not XML", b"\xbf\x01\xbf\x00",
                     marker() + marker(), marker() + b"junk", b"<event><x></event>",
                     b'<?xml version="1.0" encoding="bogus"?><event/>'):
            with self.subTest(data=data[:60]):
                self.reject(data)

    def test_dtd_entities_are_blocked_before_expansion(self):
        # Small input, exponential expansion if a parser ever processes the DTD.
        entities = ['<!ENTITY e0 "abcdefghij">']
        for i in range(1, 10):
            entities.append(f'<!ENTITY e{i} "' + f'&e{i-1};' * 10 + '">')
        attacks = [
            '<!DOCTYPE event [' + "".join(entities) + ']><event>&e9;</event>',
            '<!DOCTYPE event [<!ENTITY steal SYSTEM "file:///etc/passwd">]>'
            '<event>&steal;</event>',
            '<!DOCTYPE event SYSTEM "http://127.0.0.1/external.dtd"><event/>',
            '<!DOCTYPE event [<!ENTITY % remote SYSTEM "http://127.0.0.1/x">'
            '%remote;]><event/>',
            '<!DOCTYPE event><event/>',
        ]
        for xml in attacks:
            for encoding in ("utf-8", "utf-16", "utf-16-le", "utf-16-be"):
                with self.subTest(attack=xml[:70], encoding=encoding):
                    self.reject(xml.encode(encoding), "DOCTYPE")
        self.reject(b"<event>&undeclared;</event>")

    def test_depth_limit_applies_inside_unknown_details(self):
        root = ET.fromstring(marker())
        node = root.find("detail")
        for _ in range(MAX_DEPTH - 2):
            node = ET.SubElement(node, "extension")
        self.assertIsInstance(parse_marker(ET.tostring(root)), MarkCandidate)
        ET.SubElement(node, "one-too-deep")
        self.reject(ET.tostring(root), "depth limit")
        self.reject(b"<event>" + b"<x>" * 2000 + b"</x>" * 2000 + b"</event>", "depth")

    def test_finite_numbers_and_ranges(self):
        for name in ("lat", "lon", "hae", "ce", "le"):
            for value in ("NaN", "inf", "-inf", "1e999", "", "bad"):
                with self.subTest(name=name, value=value):
                    self.reject(edited(point={name: value}), "finite number")
        for name, value in (("lat", "91"), ("lat", "-91"), ("lon", "181"),
                            ("lon", "-181"), ("hae", "-14001"), ("hae", "76001"),
                            ("ce", "-1"), ("le", "-1"), ("ce", "10000000")):
            self.reject(edited(point={name: value}), "out of range")
        for lat, lon in ((90, 180), (-90, -180), (0, 0)):
            self.assertIsInstance(parse_marker(edited(point={"lat": str(lat), "lon": str(lon)})),
                                  MarkCandidate)

    def test_sentinels_and_missing_optional_values(self):
        data = edited(point={name: "9999999" for name in ("hae", "ce", "le")})
        candidate = parse_marker(data)
        self.assertEqual((candidate.hae, candidate.ce, candidate.le), (None, None, None))
        # Six nines are NOT CotPoint.UNKNOWN: preserve as a real error bound.
        candidate = parse_marker(edited(point={"ce": "999999", "le": "999999"}))
        self.assertEqual((candidate.ce, candidate.le), (999999, 999999))
        root = ET.fromstring(marker())
        for name in ("hae", "ce", "le"):
            root.find("point").attrib.pop(name)
        root.remove(root.find("detail"))
        candidate = parse_marker(ET.tostring(root))
        self.assertEqual((candidate.hae, candidate.ce, candidate.le, candidate.callsign),
                         (None, None, None, None))
        self.assertEqual(candidate.remarks, "")
        root.find("point").set("lat", "0")
        root.find("point").set("lon", "0")
        self.reject(ET.tostring(root), "ZERO point")

    def test_required_fields_and_marker_types(self):
        for name in ("uid", "type", "how", "time", "start", "stale", "version"):
            root = ET.fromstring(marker())
            root.attrib.pop(name)
            with self.subTest(missing=name):
                self.reject(ET.tostring(root))
        for event_type in ("b-t-f", "t-x-c-t", "u-d-r", "u-d-f", "b-m-r", "bogus"):
            self.reject(edited(event={"type": event_type}), "not a point marker")
        for event_type in ("a-u-G", "a-h-G", "b-m-p-s-p-i", "b-m-p"):
            self.assertIsInstance(parse_marker(edited(event={"type": event_type})), MarkCandidate)
        for attrs in ({"uid": " "}, {"how": "bad value"}, {"type": "a-<bad"},
                      {"version": "0"}):
            self.reject(edited(event=attrs))
        self.reject(b'<wrapper><event version="2.0"/></wrapper>')

    def test_time_validation_but_no_clock_or_automatic_authorization(self):
        for name in ("time", "start", "stale"):
            for value in ("yesterday", "2026-02-30T12:00:00.000Z", "2026-10-06T12:00:00",
                          "2026-10-06T25:00:00.000Z", "2026-10-06T12:00:60.000Z"):
                self.reject(edited(event={name: value}), "invalid " + name)
        for expiry in ("2026-10-06T18:23:45.123Z", "2026-10-06T18:23:45.122Z"):
            self.reject(edited(event={"stale": expiry}), "after start")
        old = edited(event={"time": "2000-01-01T00:00:00Z", "start": "2000-01-01T00:00:00Z",
                            "stale": "2000-01-01T00:01:00Z", "how": "h-e"})
        candidate = parse_marker(old)
        self.assertIsInstance(candidate, MarkCandidate)
        self.assertEqual(candidate.stale.year, 2000)  # Caller must reject stale imports.
        self.assertFalse(hasattr(candidate, "accepted"))

    def test_duplicates_and_missing_point_rejected(self):
        for parent_path, tag in ((".", "point"), (".", "detail"),
                                 ("detail", "contact"), ("detail", "remarks")):
            root = ET.fromstring(marker())
            ET.SubElement(root.find(parent_path), tag)
            self.reject(ET.tostring(root), tag)
        root = ET.fromstring(marker())
        root.remove(root.find("point"))
        self.reject(ET.tostring(root), "point")

    def test_unknown_details_do_not_override_identity_or_point(self):
        root = ET.fromstring(marker())
        extension = ET.SubElement(root.find("detail"), "{urn:vendor}extension")
        ET.SubElement(extension, "point", {"lat": "91", "lon": "NaN"})
        ET.SubElement(extension, "contact", {"callsign": "fake"})
        candidate = parse_marker(ET.tostring(root))
        self.assertIsInstance(candidate, MarkCandidate)
        self.assertEqual((candidate.lat, candidate.lon, candidate.callsign), (39.75, -104.99, "RADIO-3"))


class DeviceSampleTests(unittest.TestCase):
    def test_captured_gps_self_sa(self):
        sa = parse_self_sa((SAMPLES / "self-sa-gps.xml").read_bytes())
        self.assertIsInstance(sa, PhoneSA)
        self.assertEqual((sa.uid, sa.callsign), ("ANDROID-0123456789abcdef", "USER1"))
        self.assertEqual((sa.lat, sa.lon, sa.hae, sa.ce, sa.le),
                         (39.7401234, -104.9912345, 1673.984, 4.6, None))
        self.assertEqual((sa.source, sa.geopointsrc, sa.altsrc), ("gps", "GPS", "GPS"))
        self.assertEqual(sa.endpoint, "tcpsrcreply:4242:srctcp")
        self.assertEqual(sa.time.isoformat(), "2026-10-06T23:31:56.125000+00:00")
        self.assertEqual(sa.stale - sa.start, timedelta(seconds=75))

    def test_captured_manual_self_sa(self):
        sa = parse_self_sa((SAMPLES / "self-sa-manual.xml").read_bytes())
        self.assertIsInstance(sa, PhoneSA)
        self.assertEqual((sa.source, sa.how, sa.geopointsrc, sa.altsrc),
                         ("manual", "h-e", None, "SRTM1"))
        self.assertEqual((sa.lat, sa.lon, sa.hae, sa.ce, sa.le),
                         (39.7401234, -104.9902345, 1661.03, None, None))

    def test_captured_cold_start_and_user_selection(self):
        empty = parse_self_sa((SAMPLES / "self-sa-no-location.xml").read_bytes())
        selected = parse_self_sa((SAMPLES / "self-sa-user-selected.xml").read_bytes())
        self.assertIsInstance(empty, PhoneSA)
        self.assertEqual((empty.source, empty.how, empty.geopointsrc), ("unknown", "h-g-i-g-o", None))
        self.assertEqual((empty.lat, empty.lon, empty.hae, empty.ce, empty.le), (0, 0, None, None, None))
        self.assertEqual(empty.uid, selected.uid)
        self.assertEqual((selected.source, selected.how, selected.geopointsrc, selected.altsrc),
                         ("manual", "m-g", "USER", "SRTM1"))
        self.assertEqual((selected.lat, selected.lon, selected.hae, selected.ce, selected.le),
                         (39.7421234, -105.0212345, 1606.18, None, None))
        self.assertEqual(selected.stale - selected.start, timedelta(seconds=75))
        for source in ("GPS", "USER", "MANET:manual"):
            root = ET.fromstring((SAMPLES / "self-sa-no-location.xml").read_bytes())
            ET.SubElement(root.find("detail"), "precisionlocation", {"geopointsrc": source})
            self.assertEqual(parse_self_sa(ET.tostring(root)).source, "unknown")

    def test_captured_delivered_and_read_receipts(self):
        for name, status, stamp in (("delivered", "delivered", "03:05:26.289000"),
                                    ("read", "read", "03:05:43.181000")):
            receipt = parse_chat_receipt((SAMPLES / f"chat-receipt-{name}.xml").read_bytes())
            self.assertIsInstance(receipt, ChatReceipt)
            self.assertEqual((receipt.message_id, receipt.sender_uid, receipt.destination_uid, receipt.status),
                             ("5edabef44ad046d8a1a6d08e64d56907", "ANDROID-0123456789abcdef",
                              "MANET-RADIO-cm4", status))
            self.assertEqual(receipt.time.isoformat(), f"2026-10-07T{stamp}+00:00")
            self.assertEqual(receipt.start, receipt.time)
            self.assertEqual(receipt.stale - receipt.time, timedelta(days=1))

    def test_captured_phone_chat(self):
        chat = parse_phone_chat((SAMPLES / "chat-from-phone.xml").read_bytes())
        self.assertIsInstance(chat, PhoneChat)
        self.assertEqual((chat.sender_uid, chat.destination_uid, chat.conversation_id, chat.callsign),
                         ("ANDROID-0123456789abcdef", "MANET-RADIO-cm4", "MANET-RADIO-cm4", "USER1"))
        self.assertEqual(chat.message_id, "a7280ceb-c48e-4644-8b18-ddd0bba742cf")
        self.assertEqual(chat.text, "opened")
        self.assertEqual(chat.time.isoformat(), "2026-10-07T03:05:49.822000+00:00")
        self.assertEqual(chat.remarks_time, chat.time)
        self.assertEqual(chat.stale - chat.start, timedelta(days=1))

    def test_all_nine_samples_frame_in_one_stream(self):
        names = ("self-sa-gps.xml", "self-sa-manual.xml", "sent-point.xml", "sent-rb-line.xml",
                 "chat-receipt-delivered.xml", "chat-receipt-read.xml", "chat-from-phone.xml",
                 "self-sa-no-location.xml", "self-sa-user-selected.xml")
        documents = [(SAMPLES / name).read_bytes().strip() for name in names]
        data = b"\n".join(documents)
        framer = CotStreamFramer()
        frames = []
        for offset in range(0, len(data), 37):
            frames.extend(framer.feed(data[offset:offset + 37]))
        framer.finish()
        self.assertEqual(frames, documents)

    def test_captured_sent_point_and_range_bearing_line(self):
        point = (SAMPLES / "sent-point.xml").read_bytes()
        mark = parse_marker(point)
        self.assertIsInstance(mark, MarkCandidate)
        self.assertEqual((mark.type, mark.how, mark.callsign), ("a-u-G", "h-g-i-g-o", "U.6.174044"))
        self.assertTrue(mark.human_placed)
        self.assertEqual(mark.creator_uid, "ANDROID-0123456789abcdef")
        self.assertEqual(mark.creator_time.isoformat(), "2026-10-06T23:40:44.538000+00:00")
        self.assertLess(mark.creator_time, mark.time)
        self.assertIsInstance(parse_self_sa(point), str)
        line = (SAMPLES / "sent-rb-line.xml").read_bytes()
        self.assertEqual(parse_marker(line), "event is not a point marker")
        self.assertIsInstance(parse_self_sa(line), str)

    def test_human_hint_and_creator_validation(self):
        for how, human in (("h-e", True), ("h-g-i-g-o", True), ("m-g", False), ("m-f", False)):
            mark = parse_marker(edited(event={"how": how}))
            self.assertEqual(mark.human_placed, human)
            self.assertIsNone(mark.creator_time)
            self.assertIsNone(mark.creator_uid)
        for attribute, value in (("time", "bad"), ("uid", "")):
            root = ET.fromstring((SAMPLES / "sent-point.xml").read_bytes())
            root.find("detail/creator").set(attribute, value)
            self.assertIsInstance(parse_marker(ET.tostring(root)), str)
        root = ET.fromstring((SAMPLES / "sent-point.xml").read_bytes())
        ET.SubElement(root.find("detail"), "creator")
        self.assertIn("creator", parse_marker(ET.tostring(root)))

    def test_classification_is_conservative(self):
        for how, gp, expected in (("m-g", "GPS", "gps"), ("h-e", None, "manual"),
                                  ("m-g", None, "unknown"), ("h-e", "GPS", "gps"),
                                  ("h-e", "MANET", "unknown"), ("m-f", "GPS", "gps"),
                                  ("h-g-i-g-o", None, "unknown"), ("m-g", "gps", "unknown"),
                                  ("m-g", "USER", "manual"), ("h-e", "USER", "manual"),
                                  ("h-g-i-g-o", "USER", "manual"), ("m-f", "USER", "manual"),
                                  ("h-e", "MANET:manual", "external")):
            root = ET.fromstring((SAMPLES / "self-sa-gps.xml").read_bytes())
            root.set("how", how)
            precision = root.find("detail/precisionlocation")
            precision.attrib.pop("geopointsrc")
            if gp is not None:
                precision.set("geopointsrc", gp)
            with self.subTest(how=how, gp=gp):
                self.assertEqual(parse_self_sa(ET.tostring(root)).source, expected)
        root = ET.fromstring((SAMPLES / "self-sa-gps.xml").read_bytes())
        ET.SubElement(root.find("detail"), "link", {"relation": "p-s", "uid": "radio"})
        self.assertEqual(parse_self_sa(ET.tostring(root)).source, "gps")
        root.find("detail/precisionlocation").set("geopointsrc", "USER")
        self.assertEqual(parse_self_sa(ET.tostring(root)).source, "manual")
        root.find("detail/precisionlocation").attrib.pop("geopointsrc")
        self.assertEqual(parse_self_sa(ET.tostring(root)).source, "external")

    def test_sa_reuses_safe_parser_and_rejects_ambiguous_source(self):
        self.assertIsInstance(parse_self_sa(marker()), str)  # Equipment, not phone self SA.
        for data in (b"<!DOCTYPE event><event/>", b"<event", b"\xbf\x01\xbf",
                     (SAMPLES / "self-sa-gps.xml").read_bytes().replace(b'ce="4.6"', b'ce="NaN"')):
            self.assertIsInstance(parse_self_sa(data), str)
        data = (SAMPLES / "self-sa-gps.xml").read_bytes()
        self.assertIn("byte limit", parse_self_sa(data, max_bytes=len(data)-1))
        for tag in ("contact", "precisionlocation"):
            root = ET.fromstring(data)
            ET.SubElement(root.find("detail"), tag)
            self.assertIsInstance(parse_self_sa(ET.tostring(root)), str)


class FramingTests(unittest.TestCase):
    def test_real_samples_at_every_split_boundary(self):
        for name in ("self-sa-gps.xml", "self-sa-manual.xml", "sent-point.xml", "sent-rb-line.xml"):
            data = (SAMPLES / name).read_bytes().strip()
            for split in range(len(data) + 1):
                framer = CotStreamFramer()
                frames = framer.feed(data[:split]) + framer.feed(data[split:])
                self.assertEqual(frames, [data], (name, split))
                self.assertEqual(framer.buffered_bytes, 0)
                framer.finish()

    def test_coalesced_reads_limit_each_document(self):
        data = marker()
        framer = CotStreamFramer(max_bytes=len(data))
        self.assertEqual(framer.feed(b" \n" + (data + b"\r\n") * 5), [data] * 5)
        framer.finish()

    def test_markup_inside_cdata_comments_attributes_and_pi(self):
        data = marker().replace(b"<detail>", b'<detail><!-- </event> --><?test </event> ?>'
                               b'<extension note="a > b"/><text><![CDATA[</event><event>]]></text>')
        framer = CotStreamFramer()
        frames = []
        for byte in data + marker():
            frames.extend(framer.feed(bytes((byte,))))
        self.assertEqual(frames, [data, marker()])
        self.assertIsInstance(parse_marker(frames[0]), MarkCandidate)
        framer.finish()

    def test_large_lexical_tokens_are_fed_complete(self):
        data = b'<event uid="' + b'x>' * 12000 + b'"><detail><![CDATA[' + b'x' * 12000 + b']]></detail></event>'
        framer = CotStreamFramer()
        frames = []
        for start in range(0, len(data), 113):
            frames.extend(framer.feed(data[start:start+113]))
        self.assertEqual(frames, [data])
        framer.finish()

    def test_oversize_incomplete_and_failed_connection(self):
        framer = CotStreamFramer(max_bytes=32)
        self.assertEqual(framer.feed(b'<event x="' + b"a" * 22), [])
        self.assertEqual(framer.buffered_bytes, 32)
        with self.assertRaisesRegex(ValueError, "byte limit"):
            framer.feed(b"a")
        self.assertEqual(framer.buffered_bytes, 0)
        with self.assertRaisesRegex(ValueError, "closed"):
            framer.feed(marker())
        framer = CotStreamFramer()
        framer.feed(b"<event>")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            framer.finish()
        framer = CotStreamFramer()
        framer.feed(b" \n")
        framer.finish()

    def test_hostile_streams(self):
        for data in (b'<!DOCTYPE event [<!ENTITY x "BOOM">]><event>&x;</event>',
                     b"<event>" + b"<x>" * MAX_DEPTH,
                     b"<event><x></event>", b"<not-event/>",
                     b'<?xml version="1.0" encoding="bogus"?><event/>',
                     "<event/>".encode("utf-16"), b"\xbf\x01\xbf<event/>"):
            with self.subTest(data=data[:50]), self.assertRaises(ValueError):
                framer = CotStreamFramer()
                framer.feed(data)
                framer.finish()


if __name__ == "__main__":
    unittest.main()
