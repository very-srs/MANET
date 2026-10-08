"""Scenario assertions for an injected transport and simulated clock."""
from datetime import datetime, timedelta
import json
import subprocess
import xml.etree.ElementTree as ET


class CheckFailure(RuntimeError):
    pass


def date(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class Plan:
    """Assertions for a subclass that supplies transport, state and clock operations."""
    def __init__(self, packets):
        self.packets = packets
        self.offset, self.mode, self.point = 0, 'none', None
        self.silent, self.due = False, 0
        self.events = []
        self.passes = 0

    def phone_time(self):
        return self.utc() + timedelta(seconds=self.offset)

    def check(self, name, condition):
        print(('PASS ' if condition else 'FAIL ') + name, flush=True)
        if not condition:
            raise CheckFailure(name)
        self.passes += 1

    def until(self, name, predicate, timeout=15):
        end = self.mono() + timeout
        while self.mono() < end:
            self.wait(.5)
            try:
                result = predicate()
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
                # RuntimeDirectory/status can disappear briefly during startup.
                continue
            if result:
                self.check(name, True)
                return result
        self.check(name, False)

    def receive(self, port, data):
        root = ET.fromstring(data)
        if root.tag != 'event':
            raise CheckFailure('received non-CoT document')
        event = (self.mono(), self.utc(), port, root)
        self.events.append(event)
        self.record('rx', port=port, xml=data.decode())
        if port == 4349:
            self.packets.last_fix = dict(root.find('point').attrib)
        elif root.find('detail/contact') is not None:
            self.packets.radio_uid = root.get('uid')
        elif root.get('type') == 'b-t-f':
            # Delivery is deliberately distinct from read throughout this plan.
            try:
                self.send(self.packets.receipt(self.phone_time(), root.find('detail/__chat').get('messageId')))
            except OSError as exc:
                self.record('delivery_error', error=str(exc))

    def sa(self, mode=None, point=None):
        if mode is not None:
            self.mode, self.point = mode, point
        self.send(self.packets.sa(self.mode, self.phone_time(), self.point), tcp=False)
        self.due = self.mono() + 30

    def event(self, name, predicate, after=0):
        return self.until(name, lambda: next((e for e in self.events[after:] if predicate(e)), None))

    def stamped(self, event):
        _, received, _, root = event
        stamp = date(root.get('time'))
        return (abs((stamp - received).total_seconds() - self.offset) < 5
                and date(root.get('stale')) > stamp)

    def contact(self, after=0):
        event = self.event('contact received in phone time',
                           lambda e: e[3].find('detail/contact') is not None and self.stamped(e), after)
        point = event[3].find('point')
        self.check('contact uses ZERO geography', float(point.get('lat')) == float(point.get('lon')) == 0)

    def feed(self, point, after=0):
        return self.event('4349 manual coordinates and phone-domain stamps', lambda e:
                          e[2] == 4349 and self.stamped(e)
                          and abs(float(e[3].find('point').get('lat')) - point[0]) < 1e-7
                          and abs(float(e[3].find('point').get('lon')) - point[1]) < 1e-7
                          and e[3].find('detail/precisionlocation').get('geopointsrc') == 'MANET:manual', after)

    def manual(self, uid):
        state = self.status()
        self.check('held marker and monotonic age agree',
                   state.get('manual_marker_uid') == uid
                   and abs(state['manual_age_s'] - (state['written_mono'] - state['manual_observation_mono'])) < .01)
        return state

    def held(self, original):
        state = self.manual(original['manual_marker_uid'])
        self.check('held observation unchanged',
                   state['manual_observation_mono'] == original['manual_observation_mono']
                   and state['manual_observation_time'] == original['manual_observation_time']
                   and state['manual_age_s'] >= original['manual_age_s'])
        return state

    def disappear(self):
        self.silent = True
        print('Waiting 80 s for monotonic presence expiry...', flush=True)
        self.wait(80)
        self.check('silence >75 s expires phone presence', not self.status()['phone_present'])
        cursor = len(self.events)
        self.wait(2)
        self.check('no CoT while phone absent', len(self.events) == cursor)
        return cursor

    def persistence(self, before, reboot=False):
        state = self.status()
        for key in ('phone_uid', 'peer', 'manual_marker_uid', 'manual_observation_time', 'position_trusted'):
            self.check(('reboot' if reboot else 'restart') + ' retains ' + key, state[key] == before[key])
        self.check('persistence retains manual geography',
                   (state['selected']['lat'], state['selected']['lon'], state['selected']['source']) ==
                   (before['selected']['lat'], before['selected']['lon'], 'manual'))
        self.check('persistence never makes held location younger', state['manual_age_s'] >= before['manual_age_s'])
        if reboot:
            self.check('reboot reports age lower bound', state['manual_age_is_lower_bound'])
            self.check('reboot requires fresh SA', not state['phone_present'])
        else:
            self.check('restart preserves observation mono', state['manual_observation_mono'] == before['manual_observation_mono'])
        return state

    def run(self):
        self.sa()
        self.until('path ready and phone pinned', lambda: self.status().get('phone_present'))
        self.contact()
        warning = self.event('initial GeoChat received in phone time',
                             lambda e: e[3].get('type') == 'b-t-f' and self.stamped(e))
        message = warning[3].find('detail/__chat').get('messageId')
        state = self.status()
        self.check('no GPS means no position/feed', state['gps_state'] == 'NO_FIX'
                   and state['selected'] is None and not any(e[2] == 4349 for e in self.events))
        self.check('delivered receipt leaves prompt pending', state['audio_pending'] > 0)
        self.send(self.packets.chat(self.phone_time(), 'opened'))
        self.until('opened is plain chat', lambda: any(c['text'] == 'opened' for c in self.status()['chat']))
        self.check('plain chat does not cancel prompt', self.status()['audio_pending'] > 0)
        self.send(self.packets.receipt(self.phone_time(), message, True))
        self.until('read receipt cancels prompt', lambda: self.status()['audio_pending'] == 0)
        self.wait(65)  # exceeds the 60 s unread-warning deadline
        self.check('read prompt stays cancelled beyond deadline', self.status()['audio_pending'] == 0)

        cursor = len(self.events)
        self.sa('user', (0.25, -30.25))
        self.feed((0.25, -30.25), cursor)
        self.check('manual gesture leaves no prompt', self.status()['audio_pending'] == 0)
        far, near = (0.26, -30.25), (0.26001, -30.25)
        a, b = 'plan-A-' + self.token, 'plan-B-' + self.token
        for uid, point in ((a, far), (b, near)):
            cursor = len(self.events)
            self.send(self.packets.point(self.phone_time(), point, uid))
            self.feed(point, cursor)
            state = self.manual(uid)
            self.check('far/near without GPS stays manual; no GPS override',
                       state['gps_state'] == 'NO_FIX' and not state['gps_overridden']
                       and state['selected']['source'] == 'manual')
        self.event('replacement retires A', lambda e: e[3].get('type') == 't-x-d-d'
                   and e[3].find('detail/link').get('uid') == a, cursor)
        original = self.manual(b)
        self.wait(8)
        self.send(self.packets.resend(b, self.phone_time()))
        self.wait(1)
        self.held(original)
        self.wait(7)
        self.offset += 600
        self.sa('echo')
        self.until('forward step diagnosed', lambda: (self.status().get('clock_diagnostic') or {}).get('reason') == 'forward_step')
        self.wait(2)
        self.sa('user', (1.25, -31.25))
        self.wait(1)
        self.held(original)
        self.feed(near, len(self.events) - 1)
        self.wait(38)
        self.held(original)
        self.check('only A retired', all(e[3].find('detail/link').get('uid') == a
                   for e in self.events if e[3].get('type') == 't-x-d-d'))

        # An older timestamp is rejected while live; accept a fresh SA after expiry.
        rejected = self.status()['rejected']
        self.offset -= 1200
        self.silent = True
        self.sa('echo')
        self.until('live backward step rejected', lambda: self.status()['rejected'] > rejected)
        self.check('backward rejection reason', self.status()['last_rejection'] == 'replayed_or_out_of_order')
        cursor = self.disappear()
        self.silent = False
        self.sa('echo')
        self.contact(cursor)
        self.check('backward step accepted after expiry', self.status()['clock_diagnostic']['reason'] == 'backward_step')
        self.held(original)

        # Ascending offsets avoid unnecessary extra 80 s waits. Zero was checked above.
        for offset in (-86400, -600, -30, 30, 600, 86400):
            if offset < self.offset:
                self.disappear()
            self.offset, self.silent = offset, False
            cursor = len(self.events)
            self.sa('echo')
            self.feed(near, cursor)
            state = self.status()
            expected = self.phone_time().timestamp() - state['_radio_wall']
            self.check('phone offset %+.0f s: status agrees with observed UTC' % offset,
                       abs(state['phone_clock_offset_s'] - expected) < 5)
            self.held(original)

        # The monitor fixture supplies distrust without a GNSS fix.
        cursor = len(self.events)
        self.monitor_fault()
        self.until('monitor fault persists distrust with manual fallback', lambda:
                   not self.status()['position_trusted'] and self.status()['audio_pending'] > 0)
        warning = self.event('fault GeoChat received', lambda e: e[3].get('type') == 'b-t-f', cursor)
        message = warning[3].find('detail/__chat').get('messageId')
        before = self.manual(b)
        self.silent = True
        self.restart()
        self.persistence(before)
        self.check('unread prompt survives service restart', self.status()['audio_pending'] > 0)
        # Let the fixture expire before reboot so monitor loss is already accounted for.
        self.wait(6)
        before = self.status()
        self.capture_stop('before reboot')
        self.reboot(before['boot_id'])
        self.persistence(before, reboot=True)
        cursor = len(self.events)
        self.wait(2)
        self.check('no CoT after reboot before fresh SA', len(self.events) == cursor)
        self.capture_start()
        self.silent = False
        self.sa('echo')
        self.contact(cursor)
        self.feed(near, cursor)
        self.send(self.packets.receipt(self.phone_time(), message, True))
        self.until('pre-restart/reboot message ID still cancels prompt', lambda: self.status()['audio_pending'] == 0)
        self.check('receipt did not restore GPS trust', not self.status()['position_trusted'])
        self.wait(65)
        self.check('restored receipt suppresses later reminders', self.status()['audio_pending'] == 0)
        self.capture_stop('after reboot')
