"""RTP admission, object lifetime, and optional real GStreamer receive tests."""
import array
from collections import defaultdict
import struct
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_voice_beacon as beacon

voice = beacon.voice


def packet(ssrc, sequence=0, timestamp=0, first=0x80, payload=111):
    return struct.pack('!BBHII', first, payload, sequence, timestamp, ssrc) + b'\xf8\xff\xfe'


class Buffer:
    def __init__(self, data, ip='10.0.0.2'):
        self.data = data
        self.ip = ip

    def get_size(self):
        return len(self.data)

    def extract_dup(self, offset, size):
        return self.data[offset:offset + size]


class Address:
    def __init__(self, ip):
        self.ip = ip

    def get_address(self):
        return self

    def to_string(self):
        return self.ip

    def get_port(self):
        return 5000


class Pad:
    def __init__(self, name='pad'):
        self.name = name
        self.probes = {}
        self.peer = None
        self.serial = 0

    def get_name(self):
        return self.name

    def add_probe(self, kind, callback, *args):
        self.serial += 1
        self.probes[self.serial] = (callback, args)
        return self.serial

    def remove_probe(self, probe):
        del self.probes[probe]

    def link(self, peer):
        self.peer = peer
        peer.peer = self
        return 'OK'

    def unlink(self, peer):
        if self.peer == peer:
            self.peer = None
            peer.peer = None


class Branch:
    def __init__(self):
        self.pads = {name: Pad(name) for name in ('src', 'sink')}
        self.state = 'NULL'

    def get_static_pad(self, name):
        return self.pads[name]

    def set_property(self, *args):
        pass

    def sync_state_with_parent(self):
        self.state = 'PLAYING'

    def set_state(self, state):
        self.state = state


class VoiceSourceTests(unittest.TestCase):
    patch = beacon.VoiceBeaconTests.patch

    def setUp(self):
        beacon.VoiceBeaconTests.setUp(self)
        self.gst.PadProbeReturn = SimpleNamespace(OK='OK', DROP='DROP')
        self.gst.PadLinkReturn = SimpleNamespace(OK='OK')
        self.gst.parse_bin_from_description = lambda *a: Branch()
        self.gst.ElementFactory = SimpleNamespace(make=lambda *a: Branch())
        self.patch('GstNet', SimpleNamespace(buffer_get_net_address_meta=lambda b:
                   SimpleNamespace(addr=Address(b.ip)) if b.ip else None), create=True)
        voice.Gio.InetSocketAddress = Address
        self.elements = set()
        self.mixpads = set()
        self.native = {}
        self.pads = {}
        self.daemon.rx = SimpleNamespace(add=self.elements.add, remove=self.elements.remove)
        self.daemon.mixer = SimpleNamespace(request_pad_simple=self.request_pad,
                                           release_request_pad=self.mixpads.remove)
        self.daemon.rtpbin = SimpleNamespace(emit=self.clear_stream)
        self.daemon.rtp_session = SimpleNamespace(emit=lambda _, s: self.native.get(s))
        self.daemon._rx_depay_desc = 'depay'
        self.daemon._rx_dec_desc = 'decode'
        self.daemon._rx_raw_rate = 48000
        self.now = self.daemon.sources.updated

    def request_pad(self, name):
        pad = Pad(name)
        self.mixpads.add(pad)
        return pad

    def clear_stream(self, action, session, ssrc):
        self.assertEqual(action, 'clear-ssrc')
        pad = self.pads.pop(ssrc, None)
        if pad:
            self.daemon._on_rtp_pad_removed(self.daemon.rtpbin, pad)

    def receive(self, ssrc, ip='10.0.0.2', data=None):
        buf = Buffer(packet(ssrc) if data is None else data, ip)
        with patch.object(voice.time, 'monotonic', return_value=self.now):
            result = self.daemon._on_rx_buffer(None, SimpleNamespace(get_buffer=lambda: buf))
        if result == 'OK' and ssrc not in self.native:
            self.native[ssrc] = object()
            jb = object()
            self.daemon._on_new_jitterbuffer(self.daemon.rtpbin, jb, 0, ssrc)
            pad = Pad('recv_rtp_src_0_%d_111' % ssrc)
            self.pads[ssrc] = pad
            self.daemon._on_rtp_pad_added(self.daemon.rtpbin, pad)
        return result

    def expire(self, ssrc):
        self.daemon._on_source_timeout(self.daemon.rtpbin, 0, ssrc)
        self.clear_stream('clear-ssrc', 0, ssrc)
        del self.native[ssrc]

    def test_rotating_sender_does_not_allocate_rejected_identities(self):
        for ssrc in range(2000):
            self.receive(ssrc)
        self.assertEqual(len(self.native), voice.VOICE_IP_BURST)
        self.assertEqual(len(self.daemon.sources.senders), 1)
        self.assertEqual(len(self.daemon.jitterbuffers), voice.VOICE_IP_BURST)
        self.assertEqual(self.daemon.sources.dropped, 1998)
        self.assertEqual(self.timers.sources, {})
        # The old reproducer also must not retain unsolicited callbacks.
        for ssrc in range(2000, 4000):
            self.daemon._on_new_jitterbuffer(self.daemon.rtpbin, object(), 0, ssrc)
        self.assertEqual(len(self.daemon.jitterbuffers), voice.VOICE_IP_BURST)

    def test_global_rate_cap_parked_cap_and_native_slots_survive_clear(self):
        for ssrc in range(2000):
            self.receive(ssrc, '10.1.%d.%d' % divmod(ssrc, 256))
        self.assertEqual(len(self.native), voice.VOICE_ADMISSION_BURST)
        for ssrc in range(2000, 4000):
            self.now += 1
            self.receive(ssrc, '10.2.%d.%d' % divmod(ssrc - 2000, 256))
            self.assertLessEqual(len(self.daemon.rx_branches), self.daemon.cfg.max_talkers)
            self.assertLessEqual(len(self.daemon.rx_parked), voice.VOICE_MAX_PARKED)
            self.assertLessEqual(len(self.daemon.jitterbuffers), voice.VOICE_MAX_SOURCES)
        self.daemon._reap_sources()
        self.assertEqual(len(self.native), voice.VOICE_MAX_SOURCES)
        self.assertEqual(len(self.daemon.sources.sources), voice.VOICE_MAX_SOURCES)
        self.assertEqual(len(self.daemon.rx_parked), voice.VOICE_MAX_PARKED)
        self.assertEqual(len(self.elements), self.daemon.cfg.max_talkers + voice.VOICE_MAX_PARKED)
        self.assertEqual(len(self.mixpads), self.daemon.cfg.max_talkers)
        self.assertEqual(self.daemon._discarded_pads, {})
        # Clearing demux state must not allow unlimited hidden session records.
        self.assertEqual(self.receive(5000, '10.3.0.1'), 'DROP')

    def test_native_expiry_releases_every_object_and_pending_revival(self):
        self.daemon.cfg.max_talkers = 1
        self.receive(1)
        self.receive(2)
        parked_name = next(iter(self.daemon.rx_parked))
        self.daemon._on_parked_buffer(None, None, parked_name)
        self.daemon._on_parked_buffer(None, None, parked_name)
        self.assertEqual(len(self.timers.sources), 1)
        pads = list(self.pads.values())
        branches = list(self.elements)
        # A timeout signal precedes RTPSession's hash-table removal.
        self.daemon._on_source_timeout(self.daemon.rtpbin, 0, 1)
        self.daemon._reap_sources()
        self.assertIsNotNone(self.daemon.sources.get(1))
        self.assertEqual(self.receive(1), 'DROP')
        for ssrc in (1, 2):
            self.expire(ssrc)
        self.daemon._reap_sources()
        for mapping in (self.daemon.sources.sources, self.daemon.sources.senders,
                        self.daemon.rx_branches, self.daemon.rx_parked,
                        self.daemon.jitterbuffers, self.daemon.branch_seen,
                        self.daemon._reviving, self.daemon._discarded_pads,
                        self.timers.sources, self.elements, self.mixpads):
            self.assertFalse(mapping)
        for pad in pads:
            self.assertFalse(pad.probes)
            self.assertIsNone(pad.peer)
        for branch in branches:
            self.assertEqual(branch.state, 'NULL')
            self.assertFalse(branch.get_static_pad('src').probes)
        self.assertEqual(self.receive(3), 'OK')

    def test_normal_restart_and_new_talker_pass_first_packet_during_churn(self):
        self.assertEqual(self.receive(10), 'OK')
        self.assertEqual(self.receive(11), 'OK')
        for ssrc in range(12, 1012):
            self.assertEqual(self.receive(ssrc), 'DROP')
        self.assertEqual(self.receive(10), 'OK')
        self.assertEqual(self.receive(11), 'OK')
        self.assertEqual(self.receive(1012, '10.0.0.3'), 'OK')
        self.now += voice.VOICE_IP_ADMISSION_SEC
        self.assertEqual(self.receive(12), 'OK')
        self.now += voice.VOICE_IP_ADMISSION_SEC
        self.assertEqual(self.receive(13), 'OK')
        self.now += voice.VOICE_IP_ADMISSION_SEC
        self.assertEqual(self.receive(14), 'DROP')
        self.daemon.talker_names = {'10.0.0.2': 'radio-two'}
        self.assertEqual(self.daemon._talker_name(11), 'radio-two')

    def test_parallel_parking_reserves_capacity_before_creating_gst_objects(self):
        entered = threading.Event()
        release = threading.Event()

        def make(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return Branch()

        self.daemon.sources.admit(1, '10.0.0.2', self.now)
        self.daemon.sources.admit(2, '10.0.0.3', self.now)
        first = Pad('recv_rtp_src_0_1_111')
        second = Pad('recv_rtp_src_0_2_111')
        with patch.object(voice, 'VOICE_MAX_PARKED', 1), \
                patch.object(self.gst.ElementFactory, 'make', side_effect=make):
            worker = threading.Thread(target=self.daemon._park_branch,
                                      args=(first.name, first))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                self.daemon._park_branch(second.name, second)
                self.assertTrue(self.daemon.sources.get(2)['retired'])
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.daemon.rx_parked), 1)
        self.assertFalse(self.daemon._parking)

    def test_reject_malformed_csrc_self_and_address_collisions_before_rtpbin(self):
        self.daemon.my_ssrc = 1
        self.assertEqual(self.receive(1), 'DROP')
        self.assertEqual(self.daemon.rx_loopback, 1)
        for data in (b'', packet(2, first=0x40), packet(2, first=0x81),
                     packet(2, payload=112), packet(2, first=0x90),
                     packet(2, first=0xa0)[:-1] + b'\x00'):
            self.assertEqual(self.receive(2, data=data), 'DROP')
        self.assertEqual(self.receive(2, ip=None), 'DROP')
        self.assertEqual(self.daemon.sources.sources, {})
        self.assertEqual(self.receive(2), 'OK')
        self.assertEqual(self.receive(2, ip='10.0.0.3'), 'DROP')

    def test_ssrc_uses_full_random_word(self):
        with patch.object(voice.random, 'getrandbits', return_value=0xfedcba98) as bits:
            self.assertEqual(voice.node_ssrc('10.0.0.2'), 0xfedcba98)
        bits.assert_called_once_with(32)
        self.assertIsNone(voice.node_ssrc(None))
        with patch.object(voice.random, 'getrandbits', side_effect=[0xffffffff, 42]):
            self.assertEqual(voice.node_ssrc('10.0.0.2'), 42)

    def test_existing_ssrc_cannot_rotate_udp_ports(self):
        sources = self.daemon.sources
        self.assertTrue(sources.admit(10, '10.0.0.2', self.now, 5000))
        for port in range(5001, 6002):
            self.assertFalse(sources.admit(10, '10.0.0.2', self.now, port))
        self.assertTrue(sources.admit(10, '10.0.0.2', self.now, 5000))
        self.assertEqual(len(sources.sources), 1)

    def test_non_receive_pad_removal_keeps_ssrc_zero(self):
        self.receive(0)
        self.daemon._on_rtp_pad_removed(self.daemon.rtpbin, Pad('recv_rtp_sink_0'))
        self.assertIn(0, self.daemon.jitterbuffers)
        self.assertEqual(len(self.daemon.rx_branches), 1)
        self.expire(0)
        self.daemon._reap_sources()
        self.assertFalse(self.daemon.jitterbuffers)


@unittest.skipIf(voice.GST_IMPORT_ERROR is not None, 'GI/GStreamer unavailable in this Python')
class VoiceGStreamerTests(unittest.TestCase):
    """No sockets or audio device required, using real RTP, Opus and mixer bins."""

    def setUp(self):
        self.Gst = voice.Gst
        self.Gst.init(None)
        for name in ('appsrc', 'appsink', 'rtpbin', 'opusenc', 'opusdec', 'audiomixer'):
            if not self.Gst.ElementFactory.find(name):
                self.skipTest('missing GStreamer element ' + name)
        with patch.object(voice, 'read_kv_file', return_value={'voice_codec': 'opus'}), \
                patch.object(voice, 'local_ipv4_addresses', return_value=set()):
            self.daemon = voice.MeshVoice(voice.Config())
        self.d = self.daemon
        self.d.rx = self.Gst.parse_launch(
            'appsrc name=src is-live=true format=time do-timestamp=true '
            'caps="application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload=111" '
            '! rtpbin.recv_rtp_sink_0 '
            'rtpbin name=rtpbin autoremove=true max-streams=128 latency=40 do-lost=true '
            'audiomixer name=mix ignore-inactive-pads=true ! fakesink sync=false '
            'audiotestsrc is-live=true wave=silence ! '
            'audio/x-raw,rate=48000,channels=1,format=S16LE ! mix.')
        self.addCleanup(self.d._release_pipelines)
        self.d.mixer = self.d.rx.get_by_name('mix')
        self.d.rtpbin = self.d.rx.get_by_name('rtpbin')
        self.d._configure_rtp_session()
        self.d._rx_depay_desc = 'rtpopusdepay'
        self.d._rx_dec_desc = 'opusdec'
        self.d._rx_raw_rate = 48000
        for signal, callback in (('pad-added', self.d._on_rtp_pad_added),
                                 ('pad-removed', self.d._on_rtp_pad_removed),
                                 ('new-jitterbuffer', self.d._on_new_jitterbuffer),
                                 ('on-timeout', self.d._on_source_timeout)):
            self.d.rtpbin.connect(signal, callback)
        self.src = self.d.rx.get_by_name('src')
        self.src.get_static_pad('src').add_probe(self.Gst.PadProbeType.BUFFER,
                                               self.d._on_rx_buffer)
        self.samples = defaultdict(int)
        original = self.d._on_decoded_buffer

        def decoded(pad, info, name):
            buf = info.get_buffer()
            data = array.array('h', buf.extract_dup(0, buf.get_size()))
            if data and max(data) > 100:
                self.samples[self.d._ssrc_of(name)] += len(data)
            return original(pad, info, name)

        self.d._on_decoded_buffer = decoded
        self.d.rx.set_state(self.Gst.State.PLAYING)

    def spin(self, seconds):
        end = time.monotonic() + seconds
        context = voice.GLib.MainContext.default()
        while time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            self.d._reap_sources()
            time.sleep(0.01)
        msg = self.d.rx.get_bus().pop_filtered(self.Gst.MessageType.ERROR)
        self.assertIsNone(msg, msg.parse_error() if msg else '')

    def push(self, buf, ip):
        buf.pts = buf.dts = self.Gst.CLOCK_TIME_NONE
        addr = voice.Gio.InetSocketAddress.new_from_string(ip, 5000)
        voice.GstNet.buffer_add_net_address_meta(buf, addr)
        self.assertEqual(self.src.emit('push-buffer', buf), self.Gst.FlowReturn.OK)

    def speak(self, ssrc):
        tx = self.Gst.parse_launch(
            'audiotestsrc is-live=true num-buffers=50 samplesperbuffer=960 ! '
            'audio/x-raw,rate=48000,channels=1 ! opusenc ! '
            'rtpopuspay pt=111 ssrc=%d seqnum-offset=0 timestamp-offset=0 ! '
            'appsink name=out sync=false' % ssrc)
        tx.set_state(self.Gst.State.PLAYING)
        try:
            out = tx.get_by_name('out')
            while True:
                sample = out.emit('try-pull-sample', 2 * self.Gst.SECOND)
                if sample is None:
                    break
                self.push(sample.get_buffer().copy_deep(), '10.0.0.2')
        finally:
            tx.set_state(self.Gst.State.NULL)
        self.spin(0.2)

    def test_real_opus_restart_is_audible_on_first_transmission(self):
        self.speak(0xabcdef01)
        self.speak(0x12345678)
        for ssrc in (0xabcdef01, 0x12345678):
            self.assertGreater(self.samples[ssrc], 24000, 'less than 0.5s audible on first key-up')
        self.assertEqual(len(self.d.jitterbuffers), 2)

    def test_real_native_churn_and_idle_removal(self):
        # Accelerated native timeout. RTPSession still uses a five-second
        # fallback sender interval, hence up to roughly 30 seconds to expire.
        self.d.rtp_session.set_property('rtcp-min-interval', self.Gst.SECOND)
        # Rate behavior is tested separately. Fill the entire admission table
        # here to exercise real parked overflow and native clear-ssrc as well.
        burst = patch.object(voice, 'VOICE_ADMISSION_BURST', voice.VOICE_MAX_SOURCES)
        burst.start()
        self.addCleanup(burst.stop)
        self.d.sources.tokens = voice.VOICE_MAX_SOURCES
        for ssrc in range(1, 1002):
            # Include a source that never completes native probation.
            for seq in range(1 if ssrc == voice.VOICE_MAX_SOURCES else 3):
                data = packet(ssrc, seq, seq * 960)
                buf = self.Gst.Buffer.new_wrapped(data)
                self.push(buf, '10.1.%d.%d' % divmod(ssrc, 256))
        self.spin(1)
        self.assertEqual(len(self.d.sources.sources), voice.VOICE_MAX_SOURCES)
        self.assertLessEqual(self.d.rtp_session.get_property('num-sources'),
                             voice.VOICE_MAX_SOURCES + 1)
        self.assertLessEqual(len(self.d.jitterbuffers), voice.VOICE_MAX_SOURCES)
        self.assertLessEqual(len(self.d.rx_parked), voice.VOICE_MAX_PARKED)
        deadline = time.monotonic() + 35
        while self.d.sources.sources and time.monotonic() < deadline:
            self.spin(0.2)
        self.assertFalse(self.d.sources.sources)
        self.assertFalse(self.d.sources.senders)
        self.assertFalse(self.d.jitterbuffers)
        self.assertFalse(self.d.rx_branches)
        self.assertFalse(self.d.rx_parked)
        self.assertFalse(self.d.branch_seen)
        self.assertFalse(self.d._reviving)
        self.assertFalse(self.d._discarded_pads)
        # RTPSession keeps its one local RTCP identity, not a remote source.
        self.assertLessEqual(self.d.rtp_session.get_property('num-sources'), 1)
        self.assertEqual(len(self.d.mixer.sinkpads), 1)  # permanent silent input
        self.assertFalse([p for p in self.d.rtpbin.srcpads
                          if p.get_name().startswith('recv_rtp_src_')])
        children = self.d.rx.iterate_recurse()
        while True:
            result, element = children.next()
            if result == self.Gst.IteratorResult.DONE:
                break
            self.assertEqual(result, self.Gst.IteratorResult.OK)
            factory = element.get_factory()
            if factory:
                self.assertNotIn(factory.get_name(), ('rtpjitterbuffer', 'opusdec'))


if __name__ == '__main__':
    unittest.main()
