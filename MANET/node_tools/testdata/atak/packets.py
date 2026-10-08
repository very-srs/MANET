"""Pure packet builders using synthetic CoT samples beside this module."""
from datetime import timedelta, timezone
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET

SAMPLES = Path(__file__).with_name('samples')
MODES = {'gps': 'self-sa-gps.xml', 'user': 'self-sa-user-selected.xml',
         'drag': 'self-sa-manual.xml', 'none': 'self-sa-no-location.xml',
         'echo': 'self-sa-gps.xml'}


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def load(name, samples=SAMPLES):
    return ET.fromstring((samples / name).read_bytes())


def envelope(root, now, lifetime):
    root.set('time', stamp(now))
    root.set('start', stamp(now))
    root.set('stale', stamp(now + timedelta(seconds=lifetime)))
    remarks = root.find('detail/remarks')
    if remarks is not None and remarks.get('time') is not None:
        remarks.set('time', stamp(now))
    return root


class Packets:
    def __init__(self, samples=SAMPLES):
        self.samples = samples
        self.uid = load('self-sa-gps.xml', samples).get('uid')
        self.radio_uid = None
        self.last_fix = None
        self.points = {}

    def sa(self, mode, now, point=None):
        root = envelope(load(MODES[mode], self.samples), now, 75)
        root.set('uid', self.uid)
        if point is not None and mode != 'none':
            root.find('point').set('lat', str(point[0]))
            root.find('point').set('lon', str(point[1]))
        if mode == 'echo':
            precision = root.find('detail/precisionlocation')
            precision.set('geopointsrc', 'MANET:manual')
            if self.last_fix is not None:
                root.find('point').attrib.update(self.last_fix)
        return ET.tostring(root)

    def point(self, now, point=None, uid=None):
        root = envelope(load('sent-point.xml', self.samples), now, 86400)
        root.set('uid', uid or str(uuid.uuid4()))
        creator = root.find('detail/creator')
        creator.set('uid', self.uid)
        creator.set('time', stamp(now))
        if point is not None:
            root.find('point').set('lat', str(point[0]))
            root.find('point').set('lon', str(point[1]))
        self.points[root.get('uid')] = ET.tostring(root)
        return self.points[root.get('uid')]

    def resend(self, uid, now):
        root = envelope(ET.fromstring(self.points[uid]), now, 86400)
        return ET.tostring(root)

    def routed(self, name, now, message_id, receipt=False, text=None):
        if not self.radio_uid:
            raise ValueError('set radio_uid before building routed packets')
        root = envelope(load(name, self.samples), now, 86400)
        root.set('uid', message_id if receipt else f'GeoChat.{self.uid}.{self.radio_uid}.{message_id}')
        detail = root.find('detail')
        chat = detail.find('__chatreceipt' if receipt else '__chat')
        chat.set('id', self.radio_uid)
        chat.set('messageId', message_id)
        chat.set('chatroom', self.radio_uid)
        group = chat.find('chatgrp')
        group.set('id', self.radio_uid)
        group.set('uid0', self.uid)
        group.set('uid1', self.radio_uid)
        for link in detail.findall('link'):
            if link.get('relation') == 'p-p':
                link.set('uid', self.uid)
        remarks = detail.find('remarks')
        if remarks is not None:
            remarks.set('to', self.radio_uid)
            remarks.set('source', 'BAO.F.ATAK.' + self.uid)
            if 'sourceID' in remarks.attrib:
                remarks.set('sourceID', self.uid)
            if text is not None:
                remarks.text = text
        return ET.tostring(root)

    def receipt(self, now, message_id, read=False):
        return self.routed('chat-receipt-read.xml' if read else 'chat-receipt-delivered.xml',
                           now, message_id, receipt=True)

    def chat(self, now, text):
        return self.routed('chat-from-phone.xml', now, str(uuid.uuid4()), text=text)
