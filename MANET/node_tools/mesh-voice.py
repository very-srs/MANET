#!/usr/bin/env python3
"""Mesh push-to-talk voice over multicast RTP.

The audio path is entirely GStreamer — encode/decode, RTP framing, the jitter
buffer and the multicast/unicast fanout are all elements. This process only
supervises: it reads the PTT button, keeps the unicast peer list current,
adapts packing to measured loss, and publishes state for the web UI. Nothing
Python touches the audio thread, which is the whole reason this is not a
compiled daemon.

    TX  alsasrc -> level -> valve -> <enc> -> <pay>   -> multiudpsink
    RX  udpsrc  -> rtpjitterbuffer -> <depay> -> <dec> -> alsasink

voice_codec picks the pair: opus (stock elements, the default) or lyra
(libgstlyra.so plus model weights, and a fallback to opus if either is
missing). Only the codec stages differ; the transport around them was measured
with Opus and is unchanged.

With lyra, frames-per-packet adapts to receive loss — see _tick_packing. The
short version: at these bitrates the headers dominate, so packing is a much
bigger airtime lever than codec bitrate, and the loss response is to packetise
*smaller*, spending airtime to keep each loss short enough to conceal.

Addressing follows OpenMANET's scheme: every talk group shares one multicast
group and differs only by port, so switching channels never causes an IGMP
leave/join. Channel n uses port 38801 + (n-1)*2 — the stride is 2 because
port+1 is that channel's RTCP, per the RTP port-pairing convention.

Two things about the transport that are easy to get wrong:

  * Multicast TTL defaults to 1, which silently black-holes voice the moment a
    peer is more than one hop away. `ttl-mc` is set explicitly.
  * DSCP 48 (CS6), not the obvious 46 (EF), is what gets voice into WMM AC_VO
    on a batman-adv mesh. Linux 6.12+ added an RFC 8325 mapping to
    cfg80211_classify8021d() that sends EF to UP 6 (AC_VO) — but batman-adv
    never lets it run. batadv_skb_set_priority() (net/batman-adv/main.c),
    called from batadv_interface_tx() for every packet entering bat0, stamps
    skb->priority with 256 + (TOS >> 5) using the OLD naive rule, and
    mac80211 takes that 802.1d passthrough value before it ever looks at the
    DSCP. So EF gives 256+5 = UP 5 = AC_VI, and CS6 gives 256+6 = UP 6 =
    AC_VO, on every kernel version. Confirm on-air before trusting it.

Unicast redundancy is OFF by default, and the reason is measured, not assumed.
batman-adv already converts multicast to unicast for us: with
multicast_forceflood disabled (our configuration) and listeners at or below
multicast_fanout (default 16), batadv_mcast_forw_mode_by_count() returns
BATADV_FORW_UCASTS and emits one unicast frame per listener. Verified on the
bench: 200 multicast packets produced exactly 200 unicast frames addressed to
the peer's MAC on wlan2, with no broadcast frames above baseline. Those frames
get 802.11 ACKs and retries already, so adding a userspace unicast copy per
peer would double airtime for zero extra reliability.

The option remains because it stops being redundant above multicast_fanout
listeners, where batman-adv falls back to BATADV_FORW_BCAST, and if
multicast_forceflood is ever turned on. Peers come from the Alfred-built node
registry rather than from learning senders, because in a PTT system the node
that has never transmitted is exactly the one that needs to hear you.

One consequence of the same optimisation, and it is not optional: multicast to
a group nobody has joined is DROPPED at the sender -- batadv_mcast_forw_mode()
returns BATADV_FORW_NONE when the listener count is zero. Receivers joining the
group is therefore what makes transmission work at all, not merely what makes
it arrive. udpsrc does the IGMP join (auto-multicast=true); expect a brief
window after start-up where nothing flows until joins propagate.

Reads /etc/mesh.conf, writes /run/mesh-voice.json.
"""

import errno
import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

# GStreamer is a hard requirement for voice but must not be one for the unit to
# exist. The install tarball enables mesh-voice.service on every node, including
# nodes provisioned before python3-gi and the gstreamer packages were added to
# firstrun.sh — on those, importing gi raises and, with Restart=on-failure and
# RestartSec=10, systemd would retry every ten seconds forever without ever
# tripping the start limit. Exit 0 instead: a node without the runtime is simply
# a node with no voice, which is also what voice=n gives.
try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst  # noqa: E402
    GST_IMPORT_ERROR = None
except (ImportError, ValueError) as _exc:      # ValueError: gi typelib missing
    GST_IMPORT_ERROR = _exc

MESH_CONF = os.environ.get("MANET_MESH_CONF", "/etc/mesh.conf")
REGISTRY_FILE = os.environ.get("MESH_REGISTRY_FILE", "/var/run/mesh_node_registry")
STATE_FILE = os.environ.get("MESH_VOICE_STATE", "/run/mesh-voice.json")

# One multicast group for every talk group; the port selects the channel.
TALK_GROUP_ADDR = "239.192.41.1"
TALK_GROUP_BASE_PORT = 38801
TALK_GROUP_PORT_STRIDE = 2
TALK_GROUP_MAX = 32

# Opus/RTP. 111 is the conventional dynamic payload type for Opus.
RTP_PAYLOAD_TYPE = 111
SAMPLE_RATE = 48000
# Opus pins its RTP clock at 48 kHz whatever the input rate; Lyra is a 16 kHz
# codec and its elements accept nothing else.
OPUS_CLOCK_RATE = 48000
LYRA_RATE = 16000
FRAME_MS = 20

# Lyra frame size on the wire, by bitrate. Fixed by the codec, and the reason
# the receiver can recover the packet geometry without any signalling.
LYRA_FRAME_BYTES = {3200: 8, 6000: 15, 9200: 23}

# --- adaptive packing --------------------------------------------------------
# Thresholds for the frames-per-packet controller; see _tick_packing.
BATCTL = "/usr/sbin/batctl"
PACKING_MIN = 1                  # 20 ms packets: most robust, most airtime
PACKING_MAX = 3                  # 60 ms packets: cheapest, audibly bursty
PACKING_DEFAULT = 2              # 40 ms: the measured knee
PACKING_TICK_SEC = 2
PACKING_LOSS_HIGH_PCT = 5.0      # above this, shrink packets (fast)
PACKING_LOSS_LOW_PCT = 1.0       # below this, reclaim airtime (slow)
PACKING_UP_HOLD_SEC = 30         # how long clean before stepping back up
PACKING_MIN_SAMPLE = 25          # packets per window needed to judge at all
PACKING_LINK_FLOOR_MBPS = 2.0    # below this, treat loss as congestion

# OpenVLM is a C-Media CM108B. The PTT switch lands on the codec's GPIO3 and is
# read from USB HID input reports — there is no SBC GPIO involved.
OPENVLM_VID = 0x0D8C
OPENVLM_PID = 0x0012
HID_REPORT_LEN = 5
HID_GPIO3_MASK = 0x04  # IR1 bit 2 — PTT
HID_GPIO1_MASK = 0x01  # IR1 bit 0 — OpenVLM identity strap
# CM108B datasheet 7.4: IR1[3:0] only reflects live GPIO when IR0[7:6] == 0.
HID_IR0_VALID_MASK = 0xC0

PTT_DEBOUNCE_MS = 150
HALF_DUPLEX_HOLD_MS = 500
RX_IDLE_MS = 500
REGISTRY_POLL_SEC = 30
STATE_WRITE_SEC = 2


def log(msg):
    print("[%s] - VOICE: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg),
          flush=True)


def now_ms():
    return int(time.monotonic() * 1000)


# --- configuration -----------------------------------------------------------

def read_kv_file(path):
    """Parse a key=value file, tolerating comments, blanks and quoting."""
    out = {}
    try:
        with open(path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                out[key.strip()] = val.strip().strip("'\"")
    except OSError:
        pass
    return out


def conf_int(conf, key, default, low=None, high=None):
    try:
        val = int(conf.get(key, "").strip())
    except (ValueError, AttributeError):
        return default
    if low is not None and val < low:
        return default
    if high is not None and val > high:
        return default
    return val


def conf_bool(conf, key, default):
    val = conf.get(key, "").strip().lower()
    if val in ("y", "yes", "true", "1", "on"):
        return True
    if val in ("n", "no", "false", "0", "off"):
        return False
    return default


def talk_group_port(channel):
    if channel < 1 or channel > TALK_GROUP_MAX:
        channel = 1
    return TALK_GROUP_BASE_PORT + (channel - 1) * TALK_GROUP_PORT_STRIDE


class Config:
    def __init__(self):
        conf = read_kv_file(MESH_CONF)
        self.enabled = conf_bool(conf, "voice", False)
        self.iface = conf.get("voice_iface", "br0")
        self.channel = conf_int(conf, "voice_channel", 1, 1, TALK_GROUP_MAX)
        # 48 = CS6, not 46/EF — see the module docstring for why.
        self.dscp = conf_int(conf, "voice_dscp", 48, 0, 63)
        # "opus" or "lyra". Opus stays the default because it is in the stock
        # gst-plugins-base every node already has, whereas lyra needs
        # libgstlyra.so and the model weights installed. Asking for lyra on a
        # node without them falls back rather than failing to come up.
        self.codec = conf.get("voice_codec", "opus").strip().lower()
        if self.codec not in ("opus", "lyra"):
            self.codec = "opus"
        self.bitrate = conf_int(conf, "voice_bitrate", 32000, 6000, 128000)
        # Lyra is not free-rate: 3200, 6000 and 9200 are the only trained
        # operating points, and each maps to a fixed frame size (8/15/23 B).
        self.lyra_bitrate = conf_int(conf, "voice_lyra_bitrate", 6000)
        if self.lyra_bitrate not in (3200, 6000, 9200):
            self.lyra_bitrate = 6000
        # Frames per RTP packet. 2 (40 ms) measured as the knee: at 10 % loss,
        # 20 ms losses are inaudible, 40 ms barely audible, 60 ms clearly
        # audible, 80 ms unpleasant. Going from 1 to 2 takes 43 % off the wire.
        self.lyra_fpp = conf_int(conf, "voice_lyra_frames_per_packet", 2, 1, 6)
        self.lyra_model = conf.get("voice_lyra_model",
                                   "/usr/local/share/lyra/model_coeffs").strip()
        # Packet headers dominate the on-air cost at these bitrates: 12 B RTP +
        # 8 UDP + 20 IP + 14 Ethernet is 42-54 B per packet against a 32-132 B
        # payload. Fewer, larger frames is therefore a bigger lever than a
        # lower bitrate — measured, 16 kbps at 60 ms costs less on the wire
        # than 6 kbps at 20 ms. Costs latency and makes each lost packet take
        # a longer chunk of audio with it. Opus permits 2.5/5/10/20/40/60.
        self.frame_ms = conf_int(conf, "voice_frame_ms", FRAME_MS)
        if self.frame_ms not in (10, 20, 40, 60):
            self.frame_ms = FRAME_MS
        self.ttl = conf_int(conf, "voice_ttl", 32, 1, 255)
        self.jitter_ms = conf_int(conf, "voice_jitter_ms", 100, 20, 500)
        self.loss_pct = conf_int(conf, "voice_loss_pct", 20, 0, 100)
        # Off by default: batman-adv already fans multicast out as unicast.
        self.unicast = conf_bool(conf, "voice_unicast", False)
        self.max_peers = conf_int(conf, "voice_unicast_max_peers", 16, 0, 128)
        self.half_duplex = conf_bool(conf, "voice_half_duplex", True)
        self.ptt_mode = conf.get("voice_ptt", "openvlm").strip().lower()
        # Empty means autodetect the OpenVLM card.
        self.alsa_in = conf.get("voice_alsa_in", "").strip()
        self.alsa_out = conf.get("voice_alsa_out", "").strip()
        # Bench mode: a 440 Hz tone in place of the mic and a null sink in
        # place of the speaker, so the transport can be proven on a node that
        # has no audio hardware fitted yet.
        self.test_tone = conf_bool(conf, "voice_test_tone", False)
        self.port = talk_group_port(self.channel)


# --- OpenVLM PTT -------------------------------------------------------------

def hidraw_candidates():
    """Every /dev/hidraw* whose parent USB device is a CM108-family chip.

    Returns (path, is_openvlm) with the GPIO1 strap probed where possible, so a
    genuine OpenVLM is preferred over a generic dongle plugged in alongside.
    """
    want = "%04X:%04X" % (OPENVLM_VID, OPENVLM_PID)
    found = []
    for dev in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(dev, "device/uevent")) as fh:
                uevent = fh.read()
        except OSError:
            continue
        # HID_ID=0003:00000D8C:00000012
        match = re.search(r"HID_ID=[0-9A-Fa-f]+:0*([0-9A-Fa-f]{4}):0*([0-9A-Fa-f]{4})",
                          uevent)
        if not match:
            continue
        if ("%s:%s" % (match.group(1), match.group(2))).upper() != want:
            continue
        path = "/dev/" + os.path.basename(dev)
        found.append((path, probe_openvlm_strap(path)))
    # Strapped devices first.
    found.sort(key=lambda item: not item[1])
    return found


def probe_openvlm_strap(path):
    """True when GPIO1 reads high, which is how an OpenVLM identifies itself."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        data = os.read(fd, HID_REPORT_LEN)
    except OSError:
        return False
    finally:
        os.close(fd)
    report = parse_hid_report(data)
    if report is None:
        return False
    _ir0, ir1 = report
    return bool(ir1 & HID_GPIO1_MASK)


def parse_hid_report(data):
    """Return (IR0, IR1) from a CM108B input report, or None if unusable."""
    if not data or len(data) < 3:
        return None
    # The kernel prepends a report ID on full-length reports.
    start = 1 if len(data) >= HID_REPORT_LEN else 0
    if len(data) < start + 2:
        return None
    ir0 = data[start]
    ir1 = data[start + 1]
    if ir0 & HID_IR0_VALID_MASK:
        return None
    return ir0, ir1


class OpenVLMPTT(threading.Thread):
    """Blocking hidraw reader. Calls on_change(pressed) on the GLib main loop.

    Handles hot-plug by reopening: the board may enumerate after we start, and
    a USB reset must not take voice down permanently.
    """

    daemon = True

    def __init__(self, on_change, on_presence):
        super().__init__(name="openvlm-ptt")
        self._on_change = on_change
        self._on_presence = on_presence
        self._stop = threading.Event()
        self.connected = False
        self.device = None

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            devices = hidraw_candidates()
            if not devices:
                self._set_presence(False, None)
                self._stop.wait(2.0)
                continue
            path, strapped = devices[0]
            try:
                fd = os.open(path, os.O_RDONLY)
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.EACCES, errno.EBUSY):
                    log("PTT: open %s: %s" % (path, exc))
                self._set_presence(False, None)
                self._stop.wait(2.0)
                continue
            log("PTT: OpenVLM on %s (identity strap %s)"
                % (path, "present" if strapped else "absent"))
            self._set_presence(True, path)
            try:
                self._read_loop(fd)
            finally:
                os.close(fd)
                self._set_presence(False, None)
                # A release must never be lost with the device.
                GLib.idle_add(self._on_change, False)
                log("PTT: OpenVLM disconnected, waiting for reconnect")

    def _read_loop(self, fd):
        pressed = False
        last_edge = 0
        while not self._stop.is_set():
            try:
                data = os.read(fd, HID_REPORT_LEN)
            except OSError:
                return
            if not data:
                return
            report = parse_hid_report(data)
            if report is None:
                continue
            _ir0, ir1 = report
            state = bool(ir1 & HID_GPIO3_MASK)
            if state == pressed:
                continue
            stamp = now_ms()
            if stamp - last_edge < PTT_DEBOUNCE_MS:
                continue
            last_edge = stamp
            pressed = state
            GLib.idle_add(self._on_change, pressed)

    def _set_presence(self, connected, path):
        if connected == self.connected and path == self.device:
            return
        self.connected = connected
        self.device = path
        GLib.idle_add(self._on_presence, connected)


# --- peers -------------------------------------------------------------------

REGISTRY_LINE = re.compile(
    r"^NODE_([0-9A-Fa-f]+)_(IPV4_ADDRESS|HOSTNAME|NODE_STATE)='(.*)'$")


def read_registry(exclude_ips):
    """Active peers from the Alfred-built registry, as [(ip, hostname), ...].

    The registry is a shell-sourceable file of NODE_<id>_<KEY>='value' lines;
    it is parsed rather than sourced. Only ACTIVE nodes are returned — a STALE
    node has stopped publishing telemetry and unicasting to it is wasted air.
    """
    nodes = {}
    try:
        with open(REGISTRY_FILE, "r") as fh:
            for line in fh:
                match = REGISTRY_LINE.match(line.strip())
                if not match:
                    continue
                node_id, key, value = match.groups()
                nodes.setdefault(node_id, {})[key] = value
    except OSError:
        return []

    peers = []
    for fields in nodes.values():
        ip = fields.get("IPV4_ADDRESS", "").strip()
        if not ip or ip in exclude_ips:
            continue
        if fields.get("NODE_STATE", "ACTIVE") != "ACTIVE":
            continue
        peers.append((ip, fields.get("HOSTNAME", "").strip()))
    peers.sort()
    return peers


def iface_ipv4(name):
    """First IPv4 address on an interface, or None if it has none yet.

    Used as multiudpsink's bind-address: binding the send socket to the
    interface address is what guarantees outbound multicast egresses the mesh
    interface rather than whatever the route table would otherwise pick. br0
    carries two addresses (the node's mesh address and the EUD DHCP gateway);
    either is on the right interface, so the first is fine.
    """
    try:
        with os.popen("ip -4 -o addr show dev %s 2>/dev/null" % name) as fh:
            for line in fh:
                match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
                if match:
                    return match.group(1)
    except OSError:
        pass
    return None


def local_ipv4_addresses():
    """Our own addresses, so we never unicast a copy back to ourselves."""
    addrs = set()
    try:
        import socket
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            addrs.add(info[4][0])
    except Exception:
        pass
    try:
        with os.popen("ip -4 -o addr show 2>/dev/null") as fh:
            for line in fh:
                match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
                if match:
                    addrs.add(match.group(1))
    except OSError:
        pass
    return addrs


# --- ALSA --------------------------------------------------------------------

def find_openvlm_card():
    """ALSA card index for the CM108B, via /proc/asound/card*/usbid."""
    want = "%04x:%04x" % (OPENVLM_VID, OPENVLM_PID)
    for path in sorted(glob.glob("/proc/asound/card*/usbid")):
        try:
            with open(path) as fh:
                if fh.read().strip().lower() != want:
                    continue
        except OSError:
            continue
        match = re.search(r"card(\d+)", path)
        if match:
            return int(match.group(1))
    return None


def alsa_device(explicit, card):
    if explicit:
        return explicit
    if card is not None:
        # plughw rather than hw: let ALSA convert if the card disagrees about
        # format, instead of failing the pipeline outright.
        return "plughw:%d" % card
    return "default"


# --- the daemon --------------------------------------------------------------

class MeshVoice:
    def __init__(self, cfg):
        self.cfg = cfg
        self.loop = GLib.MainLoop()
        self.tx = None
        self.rx = None
        self.valve = None
        self.sink = None
        self.jitter = None
        self.payloader = None

        # Adaptive packing state. `packing` mirrors the payloader property so
        # the state file and the web UI can report it without querying GStreamer
        # from the wrong thread.
        self.packing = cfg.lyra_fpp
        self.rx_loss_pct = 0.0
        self._pk_last_pushed = 0
        self._pk_last_lost = 0
        self._pk_clean_since = time.time()

        self.ptt_pressed = False
        self.ptt_connected = False
        self.transmitting = False
        self.last_rx_ms = 0
        self.rx_active = False
        self.rx_packets = 0
        self.tx_packets = 0
        self.peers = []
        self.local_ips = local_ipv4_addresses()
        self.ptt = None
        self.started_at = time.time()

    # -- pipelines --

    def _codec_stages(self):
        """Encoder/payloader/depayloader/decoder for the configured codec.

        Returns (enc, pay, depay, dec, encoding_name, packet_ms, raw_rate,
        clock_rate). Everything around these stages is identical either way,
        which is the point: the transport was measured with Opus and only the
        codec swaps out.

        The rates cannot be shared, though. Opus always runs its RTP clock at
        48 kHz regardless of input; Lyra is a 16 kHz codec and its elements
        accept nothing else, so both the raw caps ahead of the encoder and the
        RTP clock-rate have to follow the codec.
        """
        if self.cfg.codec == "lyra":
            missing = [n for n in ("lyraenc", "lyradec", "rtplyrapay",
                                   "rtplyradepay")
                       if Gst.ElementFactory.find(n) is None]
            if missing:
                log("voice_codec=lyra but %s not registered — falling back to "
                    "opus. Install libgstlyra.so and %s."
                    % (", ".join(missing), self.cfg.lyra_model))
            elif not os.path.isdir(self.cfg.lyra_model):
                log("voice_codec=lyra but model dir %s is missing — falling "
                    "back to opus" % self.cfg.lyra_model)
            else:
                packet_ms = self.cfg.lyra_fpp * 20
                log("codec: lyra %d bps, %d frame(s)/packet (%d ms)"
                    % (self.cfg.lyra_bitrate, self.cfg.lyra_fpp, packet_ms))
                return (
                    "lyraenc name=enc bitrate=%d model-path=%s"
                    % (self.cfg.lyra_bitrate, self.cfg.lyra_model),
                    "rtplyrapay name=pay pt=%d frames-per-packet=%d"
                    % (RTP_PAYLOAD_TYPE, self.cfg.lyra_fpp),
                    "rtplyradepay",
                    "lyradec model-path=%s" % self.cfg.lyra_model,
                    "LYRA", packet_ms, LYRA_RATE, LYRA_RATE)

        log("codec: opus %d bps, %d ms frames"
            % (self.cfg.bitrate, self.cfg.frame_ms))
        return (
            "opusenc name=enc bitrate=%d frame-size=%d inband-fec=true "
            "packet-loss-percentage=%d audio-type=voice"
            % (self.cfg.bitrate, self.cfg.frame_ms, self.cfg.loss_pct),
            "rtpopuspay name=pay pt=%d" % RTP_PAYLOAD_TYPE,
            "rtpopusdepay",
            "opusdec plc=true use-inband-fec=true",
            "OPUS", self.cfg.frame_ms, SAMPLE_RATE, OPUS_CLOCK_RATE)

    def build(self):
        if self.cfg.test_tone:
            src_desc = ("audiotestsrc name=cap is-live=true wave=sine freq=440")
            playback_desc = "fakesink name=play sync=false"
            log("audio: BENCH MODE — 440 Hz tone in, null sink out")
        else:
            card = find_openvlm_card()
            dev_in = alsa_device(self.cfg.alsa_in, card)
            dev_out = alsa_device(self.cfg.alsa_out, card)
            src_desc = "alsasrc device=%s name=cap" % dev_in
            playback_desc = "alsasink name=play device=%s sync=false" % dev_out
            log("audio: capture=%s playback=%s (openvlm card %s)"
                % (dev_in, dev_out, card if card is not None else "not found"))

        # auto-multicast=false: this socket only ever sends. Letting it join the
        # group collides with our own udpsrc, which already holds it.
        bind_ip = iface_ipv4(self.cfg.iface)
        if not bind_ip:
            log("warning: %s has no IPv4 address yet — multicast egress will "
                "follow the route table" % self.cfg.iface)

        (enc_desc, pay_desc, depay_desc, dec_desc, encoding, packet_ms,
         raw_rate, clock_rate) = self._codec_stages()

        # sync=false on the sink: alsasrc is the clock for a live capture, and
        # making the sink wait on running time would only add latency.
        tx_desc = (
            "{src} ! "
            "audioconvert ! audioresample ! "
            "audio/x-raw,rate={rate},channels=1,format=S16LE ! "
            "level name=lvl interval=200000000 ! "
            "valve name=ptt drop=true ! "
            "{enc} ! {pay} ! "
            "multiudpsink name=sink clients={group}:{port} "
            "  qos-dscp={dscp} ttl-mc={ttl} loop=false auto-multicast=false "
            "  {bind} sync=false async=false"
        ).format(src=src_desc, rate=raw_rate, enc=enc_desc, pay=pay_desc,
                 group=TALK_GROUP_ADDR, port=self.cfg.port, dscp=self.cfg.dscp,
                 ttl=self.cfg.ttl,
                 bind=("bind-address=%s" % bind_ip) if bind_ip else "")

        rx_desc = (
            "udpsrc name=src address={group} port={port} "
            "  multicast-iface={iface} auto-multicast=true buffer-size=1048576 "
            "  caps=\"application/x-rtp,media=(string)audio,"
            "clock-rate=(int){rate},encoding-name=(string){encoding},"
            "payload=(int){pt}\" ! "
            "rtpjitterbuffer name=jb latency={jitter} do-lost=true ! "
            "{depay} ! {dec} ! "
            "audioconvert ! audioresample ! {play}"
        ).format(group=TALK_GROUP_ADDR, port=self.cfg.port,
                 iface=self.cfg.iface, rate=clock_rate, encoding=encoding,
                 pt=RTP_PAYLOAD_TYPE, depay=depay_desc, dec=dec_desc,
                 jitter=max(self.cfg.jitter_ms, packet_ms * 2),
                 play=playback_desc)

        self.tx = Gst.parse_launch(tx_desc)
        self.rx = Gst.parse_launch(rx_desc)
        self.valve = self.tx.get_by_name("ptt")
        self.sink = self.tx.get_by_name("sink")
        self.jitter = self.rx.get_by_name("jb")
        self.payloader = self.tx.get_by_name("pay")

        for pipeline, name in ((self.tx, "tx"), (self.rx, "rx")):
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self._on_bus_message, name)

        # Count inbound packets and drive the half-duplex gate straight off the
        # socket, before the jitter buffer adds its own delay.
        src = self.rx.get_by_name("src")
        src.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER,
                                            self._on_rx_buffer)
        # Counting at the sink pad proves packets really left, which is the
        # difference between "the valve is open" and "audio is on the air".
        self.sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER,
                                                   self._on_tx_buffer)

    def start(self):
        self.rx.set_state(Gst.State.PLAYING)
        self.tx.set_state(Gst.State.PLAYING)
        self._suppress_multicast_loopback()
        log("listening on %s:%d (channel %d) via %s"
            % (TALK_GROUP_ADDR, self.cfg.port, self.cfg.channel, self.cfg.iface))

        if self.cfg.ptt_mode == "openvlm":
            self.ptt = OpenVLMPTT(self.on_ptt, self.on_ptt_presence)
            self.ptt.start()
        elif self.cfg.ptt_mode == "always":
            log("PTT: always-on (open mic)")
            self.ptt_connected = True
            self.on_ptt(True)
        else:
            log("PTT: mode %r — receive only" % self.cfg.ptt_mode)

        self.refresh_peers()
        GLib.timeout_add_seconds(REGISTRY_POLL_SEC, self._tick_peers)
        if self.cfg.codec == "lyra":
            GLib.timeout_add_seconds(PACKING_TICK_SEC, self._tick_packing)
        GLib.timeout_add_seconds(STATE_WRITE_SEC, self._tick_state)
        GLib.timeout_add(100, self._tick_rx_decay)

    def _suppress_multicast_loopback(self):
        """Stop our own multicast coming straight back into our receiver.

        multiudpsink's `loop` property is only applied on the code path that
        also joins the group, so with auto-multicast=false it is silently
        ignored (`ttl-mc` and `qos-dscp` are applied regardless — both verified
        on the wire). Without this the operator hears themselves through the
        headset one jitter-buffer late, and the UI shows RX during every
        transmission.

        The socket is reached through `used-socket`. PyGObject hands it back as
        an untyped GSocket without the Gio.Socket methods bound, so this goes
        through the GObject property rather than set_multicast_loopback().
        """
        if not self.sink:
            return
        try:
            sock = self.sink.get_property("used-socket")
            if sock is None:
                log("warning: no send socket yet — multicast loopback not "
                    "suppressed")
                return
            sock.set_property("multicast-loopback", False)
            if sock.get_property("multicast-loopback"):
                log("warning: multicast loopback still enabled")
        except Exception as exc:
            log("warning: could not disable multicast loopback: %s" % exc)

    def stop(self):
        if self.ptt:
            self.ptt.stop()
        for pipeline in (self.tx, self.rx):
            if pipeline:
                pipeline.set_state(Gst.State.NULL)
        try:
            os.unlink(STATE_FILE)
        except OSError:
            pass
        self.loop.quit()

    # -- events --

    def _on_bus_message(self, _bus, message, which):
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log("%s pipeline error: %s (%s)" % (which, err.message, debug))
            # An audio device can vanish on USB reset; keep the process alive so
            # systemd's restart backoff is not the recovery path for a replug.
            GLib.timeout_add_seconds(5, self._restart, which)
        elif message.type == Gst.MessageType.ELEMENT:
            struct = message.get_structure()
            if struct and struct.get_name() == "level":
                self._mic_db = (struct.get_value("rms") or [-90.0])[0]

    def reload(self):
        """Re-read mesh.conf and retune if the talk group moved. SIGHUP entry.

        Talk group is a per-radio setting, like the channel knob on a handheld:
        the operator changes it from the web UI (and, later, the rotary switch
        on the enclosure). Both write voice_channel to mesh.conf and send
        SIGHUP, so there is one mechanism rather than one per input.

        Retuning in-process rather than restarting the unit matters for the
        rotary switch, where clicking through groups would otherwise mean a
        systemd restart per detent — several seconds each, and with lyra a
        TFLite model reload on top.

        Only the channel is applied. Codec, bitrate and device settings are
        read once at start-up on purpose: changing those means rebuilding the
        audio path, and doing it under an operator's thumb on the PTT is a good
        way to lose a transmission mid-word.
        """
        new = Config()
        if new.channel == self.cfg.channel:
            log("reload: talk group unchanged (%d)" % self.cfg.channel)
            return False

        was_transmitting = self.transmitting
        old_channel = self.cfg.channel
        self.cfg.channel = new.channel
        self.cfg.port = new.port

        for pipeline in (self.tx, self.rx):
            if pipeline:
                pipeline.set_state(Gst.State.NULL)
        # build() re-creates both pipelines, their bus watches and pad probes
        # against the new port. It deliberately does not touch the PTT reader
        # or the GLib timers, which are installed by start() and must not be
        # duplicated.
        try:
            self.build()
            self.rx.set_state(Gst.State.PLAYING)
            self.tx.set_state(Gst.State.PLAYING)
            self._suppress_multicast_loopback()
        except GLib.Error as exc:
            # Do not leave the node deaf because a retune failed. Fall back to
            # the group that was working; if that fails too the bus watch will
            # restart the pipelines.
            log("reload: rebuild on channel %d failed (%s) — reverting to %d"
                % (new.channel, exc, old_channel))
            self.cfg.channel = old_channel
            self.cfg.port = talk_group_port(old_channel)
            try:
                self.build()
                self.rx.set_state(Gst.State.PLAYING)
                self.tx.set_state(Gst.State.PLAYING)
                self._suppress_multicast_loopback()
            except GLib.Error as exc2:
                log("reload: revert also failed: %s" % exc2)
            return False

        # The rebuilt valve defaults to closed. If the operator was holding PTT
        # across the change, honour it rather than silently dropping their
        # transmission.
        if was_transmitting and self.valve:
            self.valve.set_property("drop", False)

        # Loss history describes the old group's traffic; keep the packing
        # level but restart the measurement rather than judge the new group on
        # the old one's numbers.
        self._pk_last_pushed = 0
        self._pk_last_lost = 0
        self._pk_clean_since = time.time()
        self.rx_loss_pct = 0.0

        log("reload: talk group %d -> %d (port %d)"
            % (old_channel, new.channel, new.port))
        self.write_state()
        return False

    def _restart(self, which):
        pipeline = self.tx if which == "tx" else self.rx
        log("%s pipeline restarting" % which)
        pipeline.set_state(Gst.State.NULL)
        pipeline.set_state(Gst.State.PLAYING)
        return False  # one-shot

    def _on_tx_buffer(self, _pad, _info):
        self.tx_packets += 1
        return Gst.PadProbeReturn.OK

    def _on_rx_buffer(self, _pad, _info):
        self.rx_packets += 1
        self.last_rx_ms = now_ms()
        if not self.rx_active:
            self.rx_active = True
        return Gst.PadProbeReturn.OK

    def _tick_rx_decay(self):
        if self.rx_active and now_ms() - self.last_rx_ms > RX_IDLE_MS:
            self.rx_active = False
        return True

    def on_ptt_presence(self, connected):
        self.ptt_connected = connected
        if not connected and self.transmitting:
            self._set_tx(False)
        return False  # idle_add one-shot

    def on_ptt(self, pressed):
        self.ptt_pressed = pressed
        if pressed:
            if self.cfg.half_duplex and self._remote_active():
                log("TX: blocked — half duplex, remote active")
                return False
            self._set_tx(True)
        else:
            self._set_tx(False)
        return False

    def _remote_active(self):
        return self.rx_active and now_ms() - self.last_rx_ms < HALF_DUPLEX_HOLD_MS

    # -- adaptive packing --

    def _link_mbps(self):
        """batman-adv's throughput estimate for the worst neighbour, in Mbit/s.

        Returns None when it cannot be read (batctl absent, not privileged,
        mesh down). Callers treat None as "unknown", not as "bad".
        """
        try:
            out = subprocess.run([BATCTL, "meshif", "bat0", "n"],
                                 capture_output=True, text=True, timeout=3)
            if out.returncode != 0:
                return None
        except (OSError, subprocess.SubprocessError):
            return None
        rates = []
        for line in out.stdout.splitlines():
            # "0c:bf:74:00:2b:f1    0.108s (       43.2) [     wlan2]"
            m = re.search(r"\(\s*([0-9]+\.?[0-9]*)\s*\)", line)
            if m:
                try:
                    rates.append(float(m.group(1)))
                except ValueError:
                    pass
        return min(rates) if rates else None

    def _set_packing(self, fpp, why):
        if fpp == self.packing or self.payloader is None:
            return
        self.packing = fpp
        try:
            self.payloader.set_property("frames-per-packet", fpp)
        except Exception as exc:
            log("packing: could not set frames-per-packet=%d: %s" % (fpp, exc))
            return
        log("packing: %d frame(s)/packet (%d ms, ~%.1f kbps on air) — %s"
            % (fpp, fpp * 20, 0.4 * (86.0 / fpp + LYRA_FRAME_BYTES.get(
                self.cfg.lyra_bitrate, 15)), why))

    def _tick_packing(self):
        """Adapt frames-per-packet to measured receive loss.

        Direction is deliberately the opposite of the usual codec-downgrade
        reflex, and the measurements are why. On this link one 20 ms frame
        costs 101 bytes on air, 86 of which is header, so packing frames is a
        far bigger lever than codec bitrate: 1 -> 2 frames/packet takes 43 %
        off the wire, while 6000 -> 3200 bps takes 12 % off and is plainly
        audible. So bitrate stays fixed and packing moves.

        Under loss we packetise SMALLER. A lost packet takes frames_per_packet
        frames with it, and a listening test at 10 % loss put the audibility
        knee exactly in this range: 20 ms losses inaudible, 40 ms barely
        audible, 60 ms clearly audible, 80 ms unpleasant. Spending airtime to
        keep each loss short enough for Lyra's concealment to hide is the whole
        trade.

        That is only safe while loss means fades rather than congestion: on a
        saturated link, offering more packets makes it worse. Hence the floor
        from batman-adv's throughput estimate.

        The signal is our own receive loss, not the far end's -- plain
        multicast RTP has no back channel. Half duplex makes that a fair proxy:
        we measure the same radio link in the other direction, moments before
        we key up. It is a proxy, though, and an asymmetric link will fool it.
        """
        if self.cfg.codec != "lyra" or self.jitter is None:
            return False        # nothing to adapt; stop the timer
        if self.transmitting:
            return True         # half duplex: not receiving, so no signal

        try:
            stats = self.jitter.get_property("stats")
            pushed = stats.get_value("num-pushed") or 0
            lost = stats.get_value("num-lost") or 0
        except Exception:
            return True

        d_pushed = pushed - self._pk_last_pushed
        d_lost = lost - self._pk_last_lost
        self._pk_last_pushed, self._pk_last_lost = pushed, lost
        total = d_pushed + d_lost
        if total < PACKING_MIN_SAMPLE:
            # Too little traffic in this window to judge. Hold, and do not let
            # silence count as "clean" toward reclaiming airtime.
            return True

        loss_pct = 100.0 * d_lost / total
        self.rx_loss_pct = loss_pct

        floor = PACKING_MIN
        mbps = self._link_mbps()
        if mbps is not None and mbps < PACKING_LINK_FLOOR_MBPS:
            # The link is already struggling for capacity. Do not answer loss
            # by offering more packets.
            floor = max(floor, PACKING_DEFAULT)

        now = time.time()
        if loss_pct > PACKING_LOSS_HIGH_PCT:
            self._pk_clean_since = now
            if self.packing > floor:
                self._set_packing(self.packing - 1,
                                  "loss %.1f%% > %.0f%%" % (loss_pct,
                                                            PACKING_LOSS_HIGH_PCT))
        elif loss_pct < PACKING_LOSS_LOW_PCT:
            if now - self._pk_clean_since >= PACKING_UP_HOLD_SEC:
                if self.packing < PACKING_MAX:
                    self._set_packing(self.packing + 1,
                                      "clean %.0fs at %.1f%% loss"
                                      % (now - self._pk_clean_since, loss_pct))
                self._pk_clean_since = now
        else:
            # Between the thresholds: hold position and restart the clean
            # timer, so recovery needs a genuinely quiet stretch rather than an
            # average that happens to land in the middle.
            self._pk_clean_since = now
        return True

    def _set_tx(self, on):
        if on == self.transmitting:
            return
        self.transmitting = on
        if self.valve:
            self.valve.set_property("drop", not on)
        log("TX: %s" % ("start" if on else "stop"))

    # -- peers --

    def _tick_peers(self):
        self.refresh_peers()
        return True

    def refresh_peers(self):
        """Point multiudpsink at the multicast group plus every active peer.

        `clients` is writable while the pipeline is PLAYING, so the destination
        set follows the mesh without restarting anything.
        """
        clients = ["%s:%d" % (TALK_GROUP_ADDR, self.cfg.port)]
        peers = []
        if self.cfg.unicast:
            peers = read_registry(self.local_ips)
            if self.cfg.max_peers and len(peers) > self.cfg.max_peers:
                peers = peers[:self.cfg.max_peers]
            clients += ["%s:%d" % (ip, self.cfg.port) for ip, _ in peers]

        if peers != self.peers:
            log("peers: %d unicast target(s)%s" % (
                len(peers),
                (" — " + ", ".join(h or ip for ip, h in peers)) if peers else ""))
        self.peers = peers
        if self.sink:
            self.sink.set_property("clients", ",".join(clients))

    # -- state --

    def _tick_state(self):
        self.write_state()
        return True

    def write_state(self):
        stats = {}
        if self.jitter:
            try:
                stats = self.jitter.get_property("stats") or {}
                stats = {k: stats[k] for k in stats.keys()}
            except Exception:
                stats = {}

        state = {
            "service": "running",
            "uptime": int(time.time() - self.started_at),
            "ptt_mode": self.cfg.ptt_mode,
            "ptt_connected": self.ptt_connected,
            "ptt_active": self.ptt_pressed,
            "ptt_device": self.ptt.device if self.ptt else None,
            "tx": self.transmitting,
            "rx": self.rx_active,
            "channel": self.cfg.channel,
            "group": TALK_GROUP_ADDR,
            "port": self.cfg.port,
            "interface": self.cfg.iface,
            "dscp": self.cfg.dscp,
            "codec": self.cfg.codec,
            "bitrate": (self.cfg.lyra_bitrate if self.cfg.codec == "lyra"
                        else self.cfg.bitrate),
            "frame_ms": (self.packing * 20 if self.cfg.codec == "lyra"
                         else self.cfg.frame_ms),
            "frames_per_packet": (self.packing if self.cfg.codec == "lyra"
                                  else None),
            "rx_loss_pct": round(self.rx_loss_pct, 1),
            "unicast": self.cfg.unicast,
            "peers": [{"ip": ip, "hostname": host} for ip, host in self.peers],
            "tx_packets": self.tx_packets,
            "rx_packets": self.rx_packets,
            "rx_lost": stats.get("num-lost", 0),
            "rx_late": stats.get("num-late", 0),
            "rx_duplicates": stats.get("num-duplicates", 0),
            "updated": int(time.time()),
        }
        tmp = STATE_FILE + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(state, fh)
            os.replace(tmp, STATE_FILE)
        except OSError as exc:
            log("state write failed: %s" % exc)


def main():
    if GST_IMPORT_ERROR is not None:
        log("GStreamer Python bindings unavailable (%s) — voice disabled. "
            "Install python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base "
            "gstreamer1.0-plugins-good gstreamer1.0-alsa." % GST_IMPORT_ERROR)
        return 0

    cfg = Config()
    if not cfg.enabled and "--force" not in sys.argv:
        log("voice=n in %s — nothing to do" % MESH_CONF)
        return 0

    Gst.init(None)

    voice = MeshVoice(cfg)
    try:
        voice.build()
    except GLib.Error as exc:
        log("pipeline build failed: %s" % exc)
        return 1

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig,
                             lambda *_: (voice.stop(), False)[1])
    # SIGHUP = re-read mesh.conf and change talk group. Returns True so the
    # handler stays installed for the next one; the web UI and, later, the
    # enclosure rotary switch both drive channel changes through this.
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGHUP,
                         lambda *_: (voice.reload(), True)[1])

    voice.start()
    try:
        voice.loop.run()
    except KeyboardInterrupt:
        voice.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
