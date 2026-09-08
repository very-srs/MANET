#!/usr/bin/env python3
"""Mesh push-to-talk voice over multicast RTP.

The audio path is entirely GStreamer — encode/decode, RTP framing, the jitter
buffer and the multicast/unicast fanout are all elements. This process only
supervises: it reads the PTT button, keeps the unicast peer list current,
adapts packing to measured loss, and publishes state for the web UI. Nothing
Python touches the audio thread, which is the whole reason this is not a
compiled daemon.

    TX  alsasrc -> level -> valve -> <enc> -> <pay> -> multiudpsink
    RX  udpsrc  -> rtpbin -+-> <depay> -> <dec> -\
                           +-> <depay> -> <dec> --+-> audiomixer -> alsasink
                                    (one branch per talker)

Receive is conference style: rtpbin demultiplexes by SSRC, gives every talker
their own jitter buffer, and audiomixer sums them, so simultaneous speakers are
mixed rather than corrupting one another. Transmit is still push-to-talk — the
valve is shut until the button is pressed, so nobody is hot-miked — and there
is no software lockout on talking over someone. That is etiquette, like any
conference bridge. voice_half_duplex=y restores the old refuse-to-key
behaviour for anyone who wants it.

A single rtpjitterbuffer cannot do this: two senders' sequence numbers
interleave in one buffer and the output is garbage. That limitation, not
policy, is what the old half-duplex lockout was really working around.

voice_codec picks the pair: lyra (the default -- libgstlyra.so plus model
weights) or opus (stock elements). Only the codec stages differ; the transport
around them was measured with Opus and is unchanged.

The two do not interoperate, and the failure is silence rather than bad audio.
Both derive the RTP payload type and clock rate from this one setting, and a
receiver builds every decode branch from its own configured codec rather than
from what arrived, so a node on the other codec hears nothing. That is why the
setting is staged mesh-wide over Alfred (radio_state, like a channel or key
change) instead of being a per-node choice, and why the fallback below matters:
a node whose lyra plugin or model weights are missing silently drops to opus,
which on a lyra mesh means it is deaf and mute. Check the log line.

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

A stalled pipeline is watched for separately from a failing one. GStreamer
posts a bus error when an element fails, and _note_pipeline_error backs those
off; it posts nothing at all when a pipeline stops moving audio while still
PLAYING, which is what a USB audio reset and a dropped multicast membership
both look like. See the stall watchdog constants below for the two flows that
are counted instead, and why tx_packets/rx_packets cannot do that job.

Reads /etc/mesh.conf, writes /run/mesh-voice.json.
"""

import errno
import glob
import json
import math
import os
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import zlib

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

# One payload type for both codecs. 111 is the conventional dynamic PT for
# Opus, and Lyra reuses it rather than claiming a second number, which is
# exactly why the two cannot interoperate: a node running opus accepts a
# PT=111 lyra packet as if it were opus. Nothing on the wire distinguishes
# them, so the codec is a fleet-wide setting staged through alfred rather
# than a per-radio one. See apply_voice_codec() in manet_radio.py.
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

# Decode branches are kept warm (see the rx pipeline), so the table is sized
# from the node registry rather than guessed. HARD is the ceiling: at roughly
# 5.3 MB per lyra branch, 64 is ~340 MB, which is the point where this stops
# being free on a 3.7 GB node. HEADROOM keeps a margin above the known node
# count so a node joining mid-operation is never the one that gets evicted.
VOICE_MAX_TALKERS_HARD = 64
VOICE_TALKER_HEADROOM = 2

# Presence beacon. A very short muted transmission whose only job is to make
# every receiver establish our RTP source before we say anything real.
BEACON_MS = 140

PTT_DEBOUNCE_MS = 150
HALF_DUPLEX_HOLD_MS = 500
RX_IDLE_MS = 500

# Pipeline restart backoff. A pipeline whose audio device is simply not there —
# a node provisioned with voice=y before its OpenVLM board is fitted — used to
# retry on a flat 5s timer for ever, logging the same two ALSA errors each
# time: ~73 journal lines a minute, ~105k a day. That was survivable while the
# journal lived in RAM, but it now persists to the card, so the noise wears the
# card and evicts the history that persistence exists to keep. Back off instead,
# and say it once.
PIPELINE_RETRY_BASE_SEC = 5
PIPELINE_RETRY_MAX_SEC = 300
# An error arriving this long after the previous one is a fresh fault, not a
# continuation, so it starts from the base delay again rather than inheriting a
# five-minute backoff from something that healed hours ago.
PIPELINE_RETRY_RESET_SEC = 900
REGISTRY_POLL_SEC = 30
STATE_WRITE_SEC = 2

# --- stall watchdog ----------------------------------------------------------
# A pipeline can stop moving audio without posting a single bus error. A USB
# audio device that resets under the CM108B comes back as a device that accepts
# state changes and delivers nothing, and a multicast membership dropped when
# br0 is rebuilt leaves udpsrc bound, PLAYING and deaf. _on_bus_message never
# hears about either, so both have to be watched from outside the pipeline.
#
# The counters that make that possible are NOT tx_packets and rx_packets. In a
# push-to-talk system those are flat almost all the time by design: the valve is
# shut until somebody keys up, and nothing is received until somebody else does.
# A watchdog driven off them would either restart a healthy quiet node or need a
# timeout so long it never fires. Two other flows do not stop while the
# pipelines are healthy, and those are what is counted:
#
#   tx  capture buffers arriving at the valve. They keep coming with the PTT
#       released, because the valve drops them downstream of the probe.
#   rx  buffers reaching the playback sink, fed continuously by the permanent
#       silent mixer input the pipeline already carries for its own reasons.
#
# Both run at roughly one buffer per 20 ms, so a few seconds of silence on
# either is unambiguous.
HEALTH_TICK_SEC = 5
# How long a flow may be stopped before it counts as a stall. Generous against
# a loaded CM4; a real stall does not recover on its own, so nothing is lost by
# waiting.
VOICE_STALL_SEC = 15
# Grace after a pipeline is told to play, before its flow is judged at all.
VOICE_STALL_GRACE_SEC = 20
# Restarting one pipeline fixes a wedged element. If that many restarts inside
# the window have not fixed it, the fault is not in one pipeline and the whole
# process is rebuilt instead: fresh ALSA handles, fresh sockets, a fresh
# multicast join and, with lyra, freshly loaded TFLite models.
VOICE_STALL_MAX_RESTARTS = 3
VOICE_STALL_WINDOW_SEC = 600
# The multicast membership is checked directly rather than inferred, because
# there is no dataflow to miss: a receiver that nobody is talking to looks
# exactly like one that has fallen out of the group.
IGMP_PROC = "/proc/net/igmp"

# Multicast loopback suppression. multiudpsink does not create its socket until
# it starts, and GStreamer state changes are asynchronous, so the socket can
# legitimately not exist yet at the moment we reach for it. Retry rather than
# warn once and give up: what an operator hears when this silently fails is
# their own voice back in the headset one jitter buffer late.
LOOPBACK_SUPPRESS_MS = 200
LOOPBACK_SUPPRESS_TRIES = 25          # 5 seconds' worth

# --- transmit high-pass ------------------------------------------------------
# Nothing a headset picks up below ~80 Hz is speech: it is mains hum, wind,
# handling rumble, and a boom mic rubbing on a face. Opus rolls some of it off
# inside the encoder, but removing it first is cleaner, and with lyra there is
# no equivalent to rely on. audiocheblimit ships in gstreamer1.0-plugins-good,
# which every voice node already installs, so this costs no new dependency.
#
# Both of these are fixed rather than configurable, because both were measured
# and only one setting works:
#
#   poles=4  8 poles is stable at 16 kHz (-66 dB at 20 Hz) and DEGENERATES at
#            48 kHz, where 80 Hz is a normalised frequency of 0.0017 and the
#            coefficients lose their precision: measured as a flat +4.9 dB of
#            gain at every frequency from 20 Hz to 3 kHz, no filtering at all.
#   type=1   Type 2 puts the ripple in the stopband and its cutoff means the
#            stopband edge, so at the same setting it does essentially nothing:
#            measured -0.3 dB at 60 Hz against type 1's -9.6 dB.
#
# Measured response of the shipping setting, identical at 16 and 48 kHz:
#
#   20 Hz -53.2 dB | 50 Hz -17.9 dB | 80 Hz  0.0 dB | 300 Hz +0.2 dB
#   40 Hz -27.2 dB | 60 Hz  -9.6 dB | 120 Hz 0.0 dB | 3 kHz   0.0 dB
#
# Cost is 0.03% of realtime at 16 kHz on a dev box, so under 1% of one CM4
# core even allowing 20x.
HIGHPASS_POLES = 4
HIGHPASS_TYPE = 1
HIGHPASS_MAX_HZ = 400

# Band-pass, when voice_lowpass_hz is also set. This is a different element
# (audiochebband, same plugin) and it wants a different pole count, which is
# why the two are not shared: audiochebband splits its poles between the two
# edges, so 4 gives a limp -3.5 dB at 60 Hz, and unlike audiocheblimit it is
# still stable at 8 at 48 kHz. Measured at 8 poles, 100-4000 Hz, near enough
# identical at 16 and 48 kHz:
#
#   40 Hz -36.9 dB | 100 Hz -0.2 dB | 1 kHz -0.1 dB | 5 kHz  -7.8 dB
#   60 Hz -20.6 dB | 300 Hz -0.0 dB | 4 kHz -0.2 dB | 6 kHz -17.2 dB
#
# Narrowing to a voice band is a taste decision, not a correctness one, so it
# is off by default and wants a field comparison across headsets.
BANDPASS_POLES = 8

# equalizer-3bands: fixed centres at 100 Hz, 1100 Hz and 11 kHz. Gains are in
# dB, -24 to +12, and every one of them is live-settable.
#
# 11 kHz is above Nyquist at lyra's 16 kHz, and the band does not politely do
# nothing there, it degenerates into broadband gain: measured at 16 kHz,
# band2=+6 lifted a 1 kHz tone by 2.6 dB and clipped 4608 samples, and
# band2=-12 cut the same tone by 6 dB. So the high band is forced to 0
# whenever the raw rate cannot carry it.
EQ_HIGH_MIN_RATE = 24000              # 11 kHz band needs headroom above 22 kHz
EQ_BAND_HZ = (100, 1100, 11000)
EQ_MIN_DB = -24.0
EQ_MAX_DB = 12.0
# The shaping settings a reload re-reads, as opposed to the codec and device
# settings that stay start-up only.
SHAPE_KEYS = ("highpass_hz", "lowpass_hz", "eq", "eq_low", "eq_mid", "eq_high")


def log(msg):
    print("[%s] - VOICE: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg),
          flush=True)


def now_ms():
    return int(time.monotonic() * 1000)


def sd_notify(msg):
    """Send one systemd notification, if there is a systemd to send it to.

    Written out by hand rather than pulled in from python3-systemd: it is one
    datagram on a unix socket, and the package is not installed on a node.
    Silently does nothing when NOTIFY_SOCKET is unset, which is the case
    whenever the daemon is run from a shell.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]        # abstract namespace
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.sendto(msg.encode(), addr)
        finally:
            sock.close()
    except OSError:
        pass


def group_hex_forms(addr):
    """Both byte orders of a dotted-quad, as upper-case hex.

    /proc/net/igmp prints the group as a native-endian u32, so 239.192.41.1
    reads 0129C0EF on a little-endian node and EFC02901 on a big-endian one.
    Matching against both is cheaper than caring which we are on.
    """
    packed = socket.inet_aton(addr)
    return {packed.hex().upper(), packed[::-1].hex().upper()}


TALK_GROUP_HEX = group_hex_forms(TALK_GROUP_ADDR)


def igmp_groups(iface):
    """Multicast groups joined on an interface, or None if that is unknowable.

    None means the file could not be read or the interface was not in it, and
    callers must treat that as "unknown" rather than "not joined". The check
    exists to catch a membership that went away, not to invent a fault out of a
    proc file that moved or an interface that has not appeared yet.
    """
    groups = None
    current = None
    try:
        with open(IGMP_PROC, "r") as fh:
            for line in fh:
                if not line.startswith((" ", "\t")):
                    # Device header: "2\tbr0       :     3      V3"
                    fields = line.split()
                    current = fields[1].rstrip(":") if len(fields) > 1 else None
                    if current == iface:
                        groups = set()
                    continue
                if current != iface:
                    continue
                fields = line.split()
                if fields:
                    groups.add(fields[0].upper())
    except (OSError, AttributeError):
        return None
    return groups


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


# Config values that get interpolated into a Gst.parse_launch() description
# or a command line, and the characters each is allowed to use.
#
# parse_launch takes a pipeline *description*, not an argument list. A space,
# a "!" or an "=" inside one of these does not fail cleanly: the parser reads
# the rest of the description as further elements and properties, and the error
# names an element nobody wrote. voice_iface was additionally interpolated into
# a shell command by the old iface_ipv4().
#
# None of these can arrive from another node. voice_codec is the only voice key
# staged over Alfred, and mesh-radio-state checks it against a two-item list
# before writing it, so what is being guarded here is a hand-edited
# /etc/mesh.conf rather than a remote input. It is still worth doing: the
# daemon runs as root, and a typo should cost a log line rather than a pipeline
# that fails in a way nothing explains.
IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
# plughw:1,0 / default / hw:CARD=Device,DEV=0
ALSA_DEV_RE = re.compile(r"^[A-Za-z0-9_.,:=-]{1,64}$")
MODEL_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]{1,255}$")


def conf_str(conf, key, default, pattern):
    """A config string that is safe to interpolate, or the default instead."""
    val = conf.get(key, "").strip()
    if not val:
        return default
    if not pattern.match(val):
        log("%s=%r rejected (unsupported characters), using %r"
            % (key, val, default))
        return default
    return val


def conf_float(conf, key, default, low=None, high=None):
    try:
        val = float(conf.get(key, "").strip())
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
        self.iface = conf_str(conf, "voice_iface", "br0", IFACE_RE)
        self.channel = conf_int(conf, "voice_channel", 1, 1, TALK_GROUP_MAX)
        # 48 = CS6, not 46/EF — see the module docstring for why.
        self.dscp = conf_int(conf, "voice_dscp", 48, 0, 63)
        # "opus" or "lyra". Opus stays the default because it is in the stock
        # gst-plugins-base every node already has, whereas lyra needs
        # libgstlyra.so and the model weights installed. Asking for lyra on a
        # node without them falls back rather than failing to come up.
        self.codec = conf.get("voice_codec", "lyra").strip().lower()
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
        self.lyra_model = conf_str(conf, "voice_lyra_model",
                                   "/usr/local/share/lyra/model_coeffs",
                                   MODEL_PATH_RE)
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
        # Off by default: the receiver mixes talkers now, so talking over
        # someone degrades gracefully instead of corrupting the stream.
        # Etiquette is left to the operators, like any conference bridge.
        self.half_duplex = conf_bool(conf, "voice_half_duplex", False)
        # Decode branches kept warm. ~5.3 MB each with lyra (measured on a
        # CM4), so 8 is ~40 MB. Raise it if a talk group routinely has more
        # than 8 active speakers and you want them all warm.
        self.max_talkers = conf_int(conf, "voice_max_talkers", 8, 1,
                                   VOICE_MAX_TALKERS_HARD)
        # Safety net only. Beacons are normally event driven -- at start-up and
        # whenever a node appears in the registry -- because a receiver never
        # forgets a source (autoremove=false), so there is nothing to refresh.
        # This interval only covers a peer whose arrival we somehow missed.
        # 0 disables the periodic one entirely. See _tick_beacon.
        self.beacon_sec = conf_int(conf, "voice_beacon_sec", 600, 0, 3600)
        # Stall watchdog. On by default, because everything it catches is
        # silent: the node stays up, the unit stays active, the web UI still
        # says running, and the first anyone knows is an operator keying up
        # into a radio that is not there. n disables the stall judgement only;
        # the systemd keep-alive is independent of it.
        # Transmit high-pass corner. 0 disables the filter entirely, which
        # gives back a byte-identical transmit pipeline to the one before it
        # existed. 80 is the usual voice value: it keeps a male fundamental
        # (~85 Hz and up) and takes out everything below. Raise it towards 120
        # if 60 Hz mains hum is the actual complaint, since 60 Hz only sees
        # -9.6 dB at a corner of 80.
        self.highpass_hz = conf_int(conf, "voice_highpass_hz", 80, 0,
                                    HIGHPASS_MAX_HZ)
        # Upper edge. 0 leaves the path high-pass only, which is the default:
        # narrowing to a walkie-talkie band makes speech pop on some headsets
        # and sound thin on others, so it is Mike's field call, not a default.
        # Setting this turns the stage into a band-pass, which also changes the
        # element and the pole count. Try 4000 with highpass 100.
        self.lowpass_hz = conf_int(conf, "voice_lowpass_hz", 0, 0, 8000)
        # Three-band EQ for a formant/presence lift. Off by default: this is a
        # taste control and an audio stage nobody has listened to yet has no
        # business in every node's transmit path. Turn it on to A/B it; the
        # gains then retune live on SIGHUP with no gap in the audio.
        self.eq = conf_bool(conf, "voice_eq", False)
        self.eq_low = conf_float(conf, "voice_eq_low", 0.0,
                                 EQ_MIN_DB, EQ_MAX_DB)
        self.eq_mid = conf_float(conf, "voice_eq_mid", 0.0,
                                 EQ_MIN_DB, EQ_MAX_DB)
        self.eq_high = conf_float(conf, "voice_eq_high", 0.0,
                                  EQ_MIN_DB, EQ_MAX_DB)
        self.watchdog = conf_bool(conf, "voice_watchdog", True)
        self.watchdog_sec = conf_int(conf, "voice_watchdog_sec",
                                     VOICE_STALL_SEC, 5, 300)
        self.ptt_mode = conf.get("voice_ptt", "openvlm").strip().lower()
        # Empty means autodetect the OpenVLM card.
        self.alsa_in = conf_str(conf, "voice_alsa_in", "", ALSA_DEV_RE)
        self.alsa_out = conf_str(conf, "voice_alsa_out", "", ALSA_DEV_RE)
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
    # argv, not a shell string: the interface name comes from mesh.conf, and
    # os.popen ran it through /bin/sh. A timeout as well, because this is called
    # from build(), build() is called from a SIGHUP retune, and a retune runs on
    # the GLib main loop that everything else in this daemon depends on.
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", name],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
        if match:
            # Constrained to digits and dots by the pattern above, which is
            # what makes it safe as multiudpsink's bind-address.
            return match.group(1)
    return None


def ssrc_prefix_for_ip(addr):
    """Top 24 bits of the SSRC a node with this address will use, or None.

    The SSRC is split: 24 bits identifying the node, 8 bits identifying this
    particular run of the daemon. The high half is a hash of the mesh address,
    so any receiver can build address -> prefix for every node in the registry
    and name a talker without a back channel.
    """
    if not addr:
        return None
    try:
        parts = [int(p) for p in addr.split(".")]
    except ValueError:
        return None
    if len(parts) != 4 or any(p < 0 or p > 255 for p in parts):
        return None
    packed = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
    return (zlib.crc32(b"manet-voice:%d" % packed) & 0xFFFFFF)


def node_ssrc(addr):
    """Our SSRC: node prefix in the high 24 bits, run generation in the low 8.

    The generation is what stops a restart from being silent, and it is not
    cosmetic. With autoremove=false a receiver keeps our source for the life of
    its daemon. If we restarted and reused the same SSRC, our payloader's fresh
    sequence-number base would not match the source the receiver is still
    holding, the jitter buffer would resync, and every word would be discarded
    -- measured, a three second transmission arrived as nothing at all, and no
    beacon or jitter-buffer tuning rescued it.

    A new generation makes the restarted node a new source, which is clean.
    The abandoned source is silent from then on, so the LRU in
    _prune_branches() evicts it ahead of anything live.
    """
    prefix = ssrc_prefix_for_ip(addr)
    if prefix is None:
        return None
    return (prefix << 8) | random.randint(0, 255)


def local_ipv4_addresses():
    """Our own addresses, so we never unicast a copy back to ourselves."""
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            addrs.add(info[4][0])
    except Exception:
        pass
    # argv and a timeout, for the same reasons as iface_ipv4 above.
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
            if match:
                addrs.add(match.group(1))
    except (OSError, subprocess.SubprocessError):
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
        self.payloader = None
        self.mixer = None
        self.rtpbin = None
        # One decode branch and one jitter buffer per talker, keyed by
        # rtpbin pad name and SSRC respectively.
        self.rx_branches = {}
        self.jitterbuffers = {}
        self.branch_seen = {}
        # Talkers evicted by the cap: pad name -> (rtpbin pad, fakesink, probe).
        # See _park_branch.
        self.rx_parked = {}
        self._reviving = set()
        self.volume = None
        self.shaper = None
        self.equalizer = None
        self.txhead = None
        # Transmit level, trimmed below 1.0 only to make room for an eq boost.
        # Everything that opens the mic uses this rather than a literal 1.0.
        self._tx_gain = 1.0
        self._shape_sig = None
        self._raw_rate = SAMPLE_RATE
        self._eq_high_warned = None
        self.my_ssrc = None
        # ssrc prefix -> peer name, rebuilt from the registry each poll.
        self.talker_names = {}
        self._known_peer_ips = set()

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
        # Own packets dropped on receive. Non-zero means multicast loopback was
        # not suppressed on the send socket; the audio is still correct, but it
        # is worth seeing in the state file rather than only in the journal.
        self.rx_loopback = 0
        # Per-pipeline restart state, keyed "tx"/"rx". See _note_pipeline_error.
        self._pipeline_fault = {}
        self.peers = []
        self.local_ips = local_ipv4_addresses()
        self.ptt = None
        self.started_at = time.time()

        # Stall watchdog state, keyed "tx"/"rx". _flow_count and _flow_ts are
        # written from the streaming threads by the two pad probes; everything
        # else is only touched on the main loop. _ever_flowed gates the whole
        # thing: a pipeline that has never produced a buffer has not regressed,
        # it was never working, and restarting the process on a node whose
        # OpenVLM is simply not fitted would turn a quiet backed-off fault into
        # a ten-second restart loop.
        self._flow_count = {"tx": 0, "rx": 0}
        self._flow_ts = {"tx": 0.0, "rx": 0.0}
        self._ever_flowed = {"tx": False, "rx": False}
        self._playing_since = {"tx": 0.0, "rx": 0.0}
        self._stalls = {"tx": 0, "rx": 0}
        self._stall_history = {"tx": [], "rx": []}
        self._igmp_ok = None
        self._igmp_lost_since = 0.0
        # Set by _panic. main() returns it, and a non-zero exit is what asks
        # systemd to rebuild the whole voice stack.
        self.exit_code = 0
        self._fault = None
        # name -> (bus, handler id), so a retune can take down what it replaces.
        self._bus_watch = {}

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
                # Packing is link state, not configuration. build() runs again
                # on every SIGHUP retune, so taking this from cfg would put an
                # adapted node silently back to the configured default while
                # self.packing -- and the web UI reading it -- still reported
                # the adapted value, and _set_packing's equality short-circuit
                # would stop the controller correcting the disagreement until
                # loss happened to move it somewhere else again. On a lossy
                # link that resets 1 frame/packet to 2 and doubles the length
                # of every loss, at exactly the moment it was measured
                # audible. The talk group moved; the radio link did not.
                fpp = self.packing
                packet_ms = fpp * 20
                log("codec: lyra %d bps, %d frame(s)/packet (%d ms)"
                    % (self.cfg.lyra_bitrate, fpp, packet_ms))
                return (
                    "lyraenc name=enc bitrate=%d model-path=%s"
                    % (self.cfg.lyra_bitrate, self.cfg.lyra_model),
                    "rtplyrapay name=pay pt=%d frames-per-packet=%d"
                    % (RTP_PAYLOAD_TYPE, fpp),
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

    def _eq_gains(self, raw_rate):
        """The three EQ gains, with the high band forced off when unusable."""
        high = self.cfg.eq_high
        if high and raw_rate < EQ_HIGH_MIN_RATE:
            if self._eq_high_warned != (high, raw_rate):
                self._eq_high_warned = (high, raw_rate)
                log("voice_eq_high=%+.1f ignored: the %d Hz band needs a raw "
                    "rate of at least %d Hz and this codec runs at %d, where "
                    "the band degenerates into broadband gain"
                    % (high, EQ_BAND_HZ[2], EQ_HIGH_MIN_RATE, raw_rate))
            high = 0.0
        return self.cfg.eq_low, self.cfg.eq_mid, high

    def _shape_signature(self, raw_rate):
        """What the shaping chain is *made of*, as opposed to how it is tuned.

        Two configurations with the same signature differ only in element
        properties, and every one of those is controllable, so a reload can
        apply them to the running pipeline. A different signature needs the
        pipeline rebuilding.
        """
        if not self.cfg.highpass_hz and not self.cfg.lowpass_hz:
            filt = None
        elif self.cfg.lowpass_hz and self.cfg.highpass_hz:
            filt = "band"
        else:
            filt = "limit"
        return (filt, bool(self.cfg.eq), raw_rate)

    def _shaping_stage(self, raw_rate):
        """Transmit shaping, as a pipeline fragment ending in a bang.

        Returns "" when nothing is enabled, so the transmit pipeline is then
        byte for byte the one that existed before any of this, which is the
        point of building it as a self-contained block: if it misbehaves in the
        field, voice_highpass_hz=0 with voice_eq=n gets the old pipeline back
        without a code change.

        The conversions on either side are not avoidable. These elements take
        F32 and F64 only and the encoders are fed S16LE, so the block converts
        up and back rather than moving the whole pipeline to float, which would
        change what reaches lyraenc.
        """
        filt, eq, _ = self._shape_signature(raw_rate)
        parts = []

        if filt == "band":
            if Gst.ElementFactory.find("audiochebband") is None:
                log("voice_lowpass_hz set but audiochebband is not registered; "
                    "no band-pass. Install gstreamer1.0-plugins-good.")
            else:
                parts.append("audiochebband name=shape mode=band-pass "
                             "lower-frequency=%d upper-frequency=%d poles=%d "
                             "type=%d"
                             % (self.cfg.highpass_hz, self.cfg.lowpass_hz,
                                BANDPASS_POLES, HIGHPASS_TYPE))
                log("transmit band-pass: %d-%d Hz, %d-pole chebyshev type %d"
                    % (self.cfg.highpass_hz, self.cfg.lowpass_hz,
                       BANDPASS_POLES, HIGHPASS_TYPE))
        elif filt == "limit":
            if Gst.ElementFactory.find("audiocheblimit") is None:
                log("voice_highpass_hz=%d but audiocheblimit is not registered, "
                    "so no transmit high-pass. Install "
                    "gstreamer1.0-plugins-good." % self.cfg.highpass_hz)
            else:
                # Only one edge is ever set here: a low-pass with no high-pass
                # is not a thing anyone has asked for, but it costs nothing to
                # honour it if it is the only edge configured.
                mode, cut = (("high-pass", self.cfg.highpass_hz)
                             if self.cfg.highpass_hz
                             else ("low-pass", self.cfg.lowpass_hz))
                parts.append("audiocheblimit name=shape mode=%s cutoff=%d "
                             "poles=%d type=%d"
                             % (mode, cut, HIGHPASS_POLES, HIGHPASS_TYPE))
                log("transmit %s: %d Hz, %d-pole chebyshev type %d"
                    % (mode, cut, HIGHPASS_POLES, HIGHPASS_TYPE))

        if eq:
            if Gst.ElementFactory.find("equalizer-3bands") is None:
                log("voice_eq=y but equalizer-3bands is not registered. "
                    "Install gstreamer1.0-plugins-good.")
            else:
                low, mid, high = self._eq_gains(raw_rate)
                # Headroom goes FIRST, ahead of everything that can boost.
                # The block converts back to S16LE at its end, so a boost
                # clips there and no amount of attenuation downstream can undo
                # it: trim first, boost second, and the peak comes out where it
                # went in. Its own element rather than the existing `vol`,
                # which lives past that conversion and is owned by the PTT and
                # beacon paths.
                parts.insert(0, "volume name=txhead volume=1.0")
                parts.append("equalizer-3bands name=eq band0=%.2f band1=%.2f "
                             "band2=%.2f" % (low, mid, high))
                log("transmit eq: %+.1f/%+.1f/%+.1f dB at %d/%d/%d Hz"
                    % (low, mid, high, *EQ_BAND_HZ))

        if not parts:
            return ""
        return ("audioconvert ! " + " ! ".join(parts)
                + " ! audioconvert ! audio/x-raw,format=S16LE ! ")

    def _apply_shaping(self, raw_rate):
        """Push the configured cutoffs and gains onto the running elements.

        Every property these elements expose is marked controllable, so this
        needs no rebuild and makes no gap in the audio, which is the whole
        point: choosing a voice band and a formant lift is a listening test
        across headsets, and a listening test is useless if every change costs
        a restart and a TFLite model reload.
        """
        changed = False
        if self.shaper is not None:
            if self.cfg.lowpass_hz and self.cfg.highpass_hz:
                want = (("lower-frequency", float(self.cfg.highpass_hz)),
                        ("upper-frequency", float(self.cfg.lowpass_hz)))
            else:
                want = (("cutoff",
                         float(self.cfg.highpass_hz or self.cfg.lowpass_hz)),)
            for prop, value in want:
                if abs(self.shaper.get_property(prop) - value) > 0.01:
                    self.shaper.set_property(prop, value)
                    changed = True
        if self.equalizer is not None:
            for band, value in enumerate(self._eq_gains(raw_rate)):
                prop = "band%d" % band
                if abs(self.equalizer.get_property(prop) - value) > 0.01:
                    self.equalizer.set_property(prop, value)
                    changed = True
        if changed:
            # Applied even mid-transmission. It steps the level, which is the
            # lesser evil: the alternative is transmitting the rest of the word
            # clipped.
            self._apply_tx_gain(raw_rate)
        return changed

    def _apply_tx_gain(self, raw_rate):
        """Trim the transmit level by whatever the EQ is boosting.

        An EQ boost raises peaks, and the block converts back to S16LE, so a
        boost on an already hot mic clips rather than sounding louder: measured
        at 48 kHz, band1=+3 on a 0.8 full-scale tone clipped 12157 samples in a
        second. Taking the same amount back out on the volume element makes a
        boost a change of tone rather than a change of level, which is what it
        is for. Costs level, not headroom, and playback has 20 dB spare.
        """
        boost = max([0.0] + [g for g in self._eq_gains(raw_rate) if g > 0])
        self._tx_gain = 10.0 ** (-boost / 20.0)
        if self.txhead is not None:
            self.txhead.set_property("volume", self._tx_gain)
        if boost:
            log("transmit headroom: %.3f (-%.1f dB) ahead of the eq boost"
                % (self._tx_gain, boost))

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

        # What we actually built with, which is not always what was asked for:
        # a missing lyra plugin or model dir falls back to opus. The UI paints
        # its codec picker from this, so reporting the *configured* value here
        # would show a node as Lyra while it was really transmitting Opus --
        # and since the two do not interoperate, that is precisely the node
        # that is silently deaf to the rest of the mesh.
        self.effective_codec = encoding.lower()

        # sync=false on the sink: alsasrc is the clock for a live capture, and
        # making the sink wait on running time would only add latency.
        # Shaping goes ahead of `level`, not after it, so the mic dB the web UI
        # shows is the level of what is actually transmitted. With it
        # downstream, rumble and hum would drive the meter on a node that was
        # sending none of it, which is the opposite of what the meter is for.
        self._shape_sig = self._shape_signature(raw_rate)
        self._raw_rate = raw_rate
        tx_desc = (
            "{src} ! "
            "audioconvert ! audioresample ! "
            "audio/x-raw,rate={rate},channels=1,format=S16LE ! "
            "{hpf}"
            "level name=lvl interval=200000000 ! "
            "volume name=vol ! "
            "valve name=ptt drop=true ! "
            "{enc} ! {pay} ! "
            "multiudpsink name=sink clients={group}:{port} "
            "  qos-dscp={dscp} ttl-mc={ttl} loop=false auto-multicast=false "
            "  {bind} sync=false async=false"
        ).format(src=src_desc, rate=raw_rate, hpf=self._shaping_stage(raw_rate),
                 enc=enc_desc, pay=pay_desc,
                 group=TALK_GROUP_ADDR, port=self.cfg.port, dscp=self.cfg.dscp,
                 ttl=self.cfg.ttl,
                 bind=("bind-address=%s" % bind_ip) if bind_ip else "")

        # Conference receive: rtpbin demultiplexes by SSRC and gives each talker
        # its own jitter buffer, and audiomixer sums them. A single
        # rtpjitterbuffer cannot do this — two senders' sequence numbers
        # interleave in one buffer and the output is garbage, which is why the
        # old pipeline needed a half-duplex lockout to be usable at all.
        #
        # The silent source is not decoration. audiomixer only produces output
        # while it has an input, so with nobody talking the sink would be
        # starved, and every transmission would start with the DAC spinning up.
        # A permanent silent input keeps the mixer and alsasink running so
        # speech starts cleanly.
        #
        # autoremove=false is deliberate and load-bearing (it is also rtpbin's
        # own default; an earlier revision set it true and that was the bug).
        # Reaping an idle talker means the next thing they say rebuilds the
        # branch, and a rebuilt branch loses the head of the transmission.
        # Measured on a CM4 with lyra, 3.00 s bursts:
        #
        #     branch rebuilt on demand   2.82 s arrived  (180 ms lost)
        #     branch pre-built, attached 2.92 s arrived  ( 80 ms lost)
        #     talker already established 3.04 s arrived  (nothing lost)
        #
        # Only a source rtpbin has seen before costs nothing, so the branches
        # stay. Blocking the pad, lowering probation and raising the mixer's
        # min-upstream-latency were all tried and none of them helped; the
        # residual is rtpbin establishing a new source, not our linking.
        #
        # ignore-inactive-pads is then required rather than optional: branches
        # that are kept are silent between transmissions, and without it the
        # mixer would sit waiting on them.
        self.rx_branches = {}
        self.jitterbuffers = {}
        self.branch_seen = {}
        self.rx_parked = {}
        self._reviving = set()
        # Loss is read as a delta against the previous window, and a rebuild
        # destroys every jitter buffer, so the cumulative counters restart at
        # zero. Without this the first window after a retune is a large
        # negative delta and gets discarded. _pk_clean_since is deliberately
        # left alone: it tracks how long the link has been clean, and the link
        # is not what changed.
        self._pk_last_pushed = 0
        self._pk_last_lost = 0
        self.my_ssrc = None
        # ssrc -> peer name, for the UI. Filled from the registry.
        self.talker_names = {}
        self._known_peer_ips = set()
        rx_desc = (
            "rtpbin name=rtpbin latency={jitter} do-lost=true autoremove=false "
            "udpsrc name=src address={group} port={port} "
            "  multicast-iface={iface} auto-multicast=true buffer-size=1048576 "
            "  caps=\"application/x-rtp,media=(string)audio,"
            "clock-rate=(int){rate},encoding-name=(string){encoding},"
            "payload=(int){pt}\" ! rtpbin.recv_rtp_sink_0 "
            "audiomixer name=mix ignore-inactive-pads=true ! "
            "  audioconvert ! audioresample ! {play} "
            "audiotestsrc name=silence wave=silence is-live=true ! "
            "  audio/x-raw,rate={arate},channels=1,format=S16LE ! mix. "
        ).format(group=TALK_GROUP_ADDR, port=self.cfg.port,
                 iface=self.cfg.iface, rate=clock_rate, encoding=encoding,
                 pt=RTP_PAYLOAD_TYPE, arate=raw_rate,
                 jitter=max(self.cfg.jitter_ms, packet_ms * 2),
                 play=playback_desc)

        self.tx = Gst.parse_launch(tx_desc)
        self.rx = Gst.parse_launch(rx_desc)
        self.valve = self.tx.get_by_name("ptt")
        self.sink = self.tx.get_by_name("sink")
        self.payloader = self.tx.get_by_name("pay")
        self.volume = self.tx.get_by_name("vol")
        # May be None: either stage can be absent or its element missing.
        self.shaper = self.tx.get_by_name("shape")
        self.equalizer = self.tx.get_by_name("eq")
        self.txhead = self.tx.get_by_name("txhead")
        # Now that txhead exists, set the headroom the configured boost needs.
        self._apply_tx_gain(raw_rate)

        # SSRC = 24-bit hash of our mesh address, plus an 8-bit generation for
        # this run. The prefix lets any receiver name us from the registry; the
        # generation stops a restart from colliding with the source a receiver
        # is still holding. See node_ssrc().
        ssrc = node_ssrc(iface_ipv4(self.cfg.iface))
        if ssrc is not None and self.payloader is not None:
            self.payloader.set_property("ssrc", ssrc)
            self.my_ssrc = ssrc
        self.mixer = self.rx.get_by_name("mix")
        self.rtpbin = self.rx.get_by_name("rtpbin")

        # Branches appear and disappear as people key up and drop. Remember the
        # codec stages so the handler can build one per talker.
        self._rx_depay_desc = depay_desc
        self._rx_dec_desc = dec_desc
        self._rx_raw_rate = raw_rate
        self.rtpbin.connect("pad-added", self._on_rtp_pad_added)
        self.rtpbin.connect("pad-removed", self._on_rtp_pad_removed)
        # Loss is measured per talker now, so collect the jitter buffers as
        # rtpbin creates them; _tick_packing sums across them.
        self.rtpbin.connect("new-jitterbuffer", self._on_new_jitterbuffer)

        # Keep the bus and the handler id. A retune replaces both pipelines,
        # and a watch that is never removed keeps its bus alive for the life of
        # the process; see _remove_bus_watches for what that costs. Taking the
        # old ones down here as well as in _release_pipelines means build()
        # cannot orphan a watch by being called twice.
        self._remove_bus_watches()
        for pipeline, name in ((self.tx, "tx"), (self.rx, "rx")):
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            self._bus_watch[name] = (bus, bus.connect("message",
                                                      self._on_bus_message,
                                                      name))

        # Count inbound packets and drive the half-duplex gate straight off the
        # socket, before the jitter buffer adds its own delay.
        src = self.rx.get_by_name("src")
        src.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER,
                                            self._on_rx_buffer)
        # Counting at the sink pad proves packets really left, which is the
        # difference between "the valve is open" and "audio is on the air".
        self.sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER,
                                                   self._on_tx_buffer)

        # Stall watchdog. The valve's SINK pad, not its source: buffers arrive
        # there and are dropped inside the valve, so this counts capture even
        # with the PTT released, which is exactly the point. Playback is
        # counted at the sink because the mixer's permanent silent input keeps
        # it fed whether or not anyone is talking. See the constants block.
        self.valve.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, self._on_capture_buffer)
        play = self.rx.get_by_name("play")
        if play is not None:
            play.get_static_pad("sink").add_probe(
                Gst.PadProbeType.BUFFER, self._on_playback_buffer)

    def start(self):
        self._play("rx")
        self._play("tx")
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
        if self.cfg.beacon_sec:
            # Announce this run immediately: any receiver still holding our
            # previous generation needs to see the new SSRC, and anyone already
            # listening should have us warm before the first press of the PTT.
            GLib.timeout_add(1500, self._tick_beacon)
            GLib.timeout_add_seconds(self.cfg.beacon_sec, self._tick_beacon)
            log("beacon: ssrc 0x%08x, on start-up and on new peers "
                "(safety net every %ds)"
                % (self.my_ssrc or 0, self.cfg.beacon_sec))
        GLib.timeout_add_seconds(STATE_WRITE_SEC, self._tick_state)
        GLib.timeout_add(100, self._tick_rx_decay)
        GLib.timeout_add_seconds(HEALTH_TICK_SEC, self._tick_health)
        if self.cfg.watchdog:
            log("watchdog: stall after %ds with no audio flowing; whole-stack "
                "restart after %d in-process restarts inside %ds"
                % (self.cfg.watchdog_sec, VOICE_STALL_MAX_RESTARTS,
                   VOICE_STALL_WINDOW_SEC))
        else:
            log("watchdog: stall detection disabled by voice_watchdog=n")
        # Type is simple, but WatchdogSec= in the unit is enough for systemd to
        # pass NOTIFY_SOCKET, and READY=1 is harmless either way.
        sd_notify("READY=1")

    def _suppress_multicast_loopback(self, attempt=0, sink=None):
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

        And it is retried, because multiudpsink has no socket until it has
        started and a GStreamer state change is asynchronous: asking too early
        gets None back, and the original code took that as its answer and never
        asked again. The pipeline then ran for its whole life with loopback on,
        which is not a degraded mode, it is the operator being fed their own
        transmission at headset volume one jitter buffer late.

        Failing here still does not fail a retune. It does not need to: the
        own-SSRC drop in _on_rx_buffer is the actual guarantee, and this is the
        tidier of the two, keeping our packets out of rtpbin rather than
        throwing them away after they arrive.
        """
        if sink is not None and sink is not self.sink:
            return False              # a retune replaced the pipeline under us
        sink = self.sink
        if sink is None:
            return False
        try:
            sock = sink.get_property("used-socket")
        except Exception as exc:
            log("warning: could not read the send socket: %s" % exc)
            return False
        if sock is None:
            if attempt < LOOPBACK_SUPPRESS_TRIES:
                GLib.timeout_add(LOOPBACK_SUPPRESS_MS,
                                 self._suppress_multicast_loopback,
                                 attempt + 1, sink)
                return False
            log("warning: multiudpsink never produced a send socket after "
                "%.1fs, so multicast loopback could not be suppressed; the "
                "own-SSRC drop on receive is what is stopping the operator "
                "hearing themselves"
                % (LOOPBACK_SUPPRESS_TRIES * LOOPBACK_SUPPRESS_MS / 1000.0))
            return False
        try:
            sock.set_property("multicast-loopback", False)
            still_on = sock.get_property("multicast-loopback")
        except Exception as exc:
            log("warning: could not disable multicast loopback: %s" % exc)
            return False
        if still_on:
            log("warning: multicast loopback still enabled after clearing it")
        elif attempt:
            log("multicast loopback suppressed on attempt %d" % (attempt + 1))
        return False

    def stop(self, keep_state=False):
        if self.ptt:
            self.ptt.stop()
        self._release_pipelines()
        # _panic keeps the state file so the fault it just wrote survives the
        # ten seconds until systemd has us back. A clean shutdown removes it,
        # because a node with voice stopped should not look like one whose
        # daemon merely went quiet.
        if not keep_state:
            try:
                os.unlink(STATE_FILE)
            except OSError:
                pass
        self.loop.quit()

    # -- events --

    def _on_bus_message(self, _bus, message, which):
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            # An audio device can vanish on USB reset; keep the process alive so
            # systemd's restart backoff is not the recovery path for a replug.
            self._note_pipeline_error(which, err.message, debug)
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

        A retune tears both pipelines down before it builds anything, so there
        is a window where a failure leaves no working audio path at all. The
        ladder out of that is: rebuild on the new group, and if that fails,
        rebuild on the old one, and if that fails too, hand the whole process
        to systemd. Limping on is not an option here, because every way this
        can fail leaves the daemon alive and reporting itself healthy.
        """
        new = Config()
        was_transmitting = self.transmitting
        old_channel = self.cfg.channel
        raw_rate = self._raw_rate

        # Shaping is copied across before anything is compared, because the
        # signature is computed from self.cfg. Keep the old values so a failed
        # rebuild can put them back with the talk group.
        shape_was = {k: getattr(self.cfg, k) for k in SHAPE_KEYS}
        for key in SHAPE_KEYS:
            setattr(self.cfg, key, getattr(new, key))
        restructure = (self._shape_sig is not None
                       and self._shape_signature(raw_rate) != self._shape_sig)

        if new.channel == self.cfg.channel and not restructure:
            # Cutoffs and gains only, which every one of these elements accepts
            # while PLAYING. No rebuild, no gap, no model reload.
            if self._apply_shaping(raw_rate):
                log("reload: transmit shaping retuned in place")
            else:
                log("reload: nothing changed (talk group %d)" % old_channel)
            self.write_state()
            return False

        if restructure:
            log("reload: transmit shaping changed shape, rebuilding")

        if self._retune(new.channel, new.port):
            # The rebuilt valve defaults to closed. If the operator was holding
            # PTT across the change, honour it rather than silently dropping
            # their transmission.
            if was_transmitting and self.valve:
                self.valve.set_property("drop", False)

            # Loss history describes the old group's traffic; keep the packing
            # level but restart the measurement rather than judge the new group
            # on the old one's numbers.
            self._pk_last_pushed = 0
            self._pk_last_lost = 0
            self._pk_clean_since = time.time()
            self.rx_loss_pct = 0.0

            if new.channel != old_channel:
                log("reload: talk group %d -> %d (port %d)"
                    % (old_channel, new.channel, new.port))
            else:
                log("reload: rebuilt on talk group %d" % old_channel)
            self.write_state()
            return False

        # Do not leave the node deaf because a retune failed. Go back to the
        # group and the shaping that were working.
        log("reload: rebuild on talk group %d failed, reverting to the "
            "previous settings" % new.channel)
        for key, value in shape_was.items():
            setattr(self.cfg, key, value)
        if self._retune(old_channel, talk_group_port(old_channel)):
            log("reload: back on talk group %d" % old_channel)
            self.write_state()
            return False

        # Both builds failed, so this is not something about the new group and
        # there is nothing left in process to fall back to. mesh.conf already
        # holds the operator's choice, so the restarted daemon comes up on the
        # group they asked for.
        self._panic("reload to talk group %d failed and reverting to %d "
                    "failed too" % (new.channel, old_channel))
        return False

    def _retune(self, channel, port):
        """Rebuild both pipelines on one talk group. True only if both play.

        Every step is guarded, and not only against GLib.Error. build() reaches
        for elements by name and hangs pad probes off them, so a pipeline that
        parsed but came back missing an element raises AttributeError, and
        PyGObject prints an exception raised inside a signal handler and then
        swallows it. SIGHUP arrives through a signal handler, so that
        combination used to leave the daemon alive with self.tx pointing at the
        new pipeline, self.rx at the old one it had already set to NULL, and
        self.valve at elements of neither: no audio, no error in the journal,
        and a state file still saying "running".
        """
        self.cfg.channel = channel
        self.cfg.port = port
        self._release_pipelines()
        # build() re-creates both pipelines, their bus watches and pad probes
        # against the new port. It deliberately does not touch the PTT reader
        # or the GLib timers, which are installed by start() and must not be
        # duplicated.
        try:
            self.build()
        except Exception as exc:
            log("retune: build on talk group %d failed: %s" % (channel, exc))
            return False
        if not self._play("rx") or not self._play("tx"):
            return False
        self._suppress_multicast_loopback()
        # These pipelines are new objects, so a fault recorded against the ones
        # they replaced is not theirs. Leaving it would keep the watchdog off
        # the rx pipeline indefinitely, since the entry is only cleared by a
        # buffer arriving and on a quiet talk group none ever does.
        self._pipeline_fault.clear()
        self._igmp_lost_since = 0.0
        return True

    def _remove_bus_watches(self):
        """Drop the bus watches, which is the part that is easy to miss.

        gst_bus_add_signal_watch() attaches a GSource holding a reference to
        the bus, so a bus whose watch is never removed is never finalised, and
        every GstBus carries a GstPoll control pipe. A retune builds two new
        pipelines, so a talk group change that only set the old ones to NULL
        leaked four file descriptors and two live GSources every time.
        Measured on the bench: 4 fds per retune, dead flat once the watch is
        removed. An operator clicking a rotary switch through the groups would
        reach the default 1024-descriptor limit inside a single operation, and
        the daemon would then fail to open the very sockets and ALSA devices
        that voice needs.
        """
        for name, (bus, handler) in self._bus_watch.items():
            try:
                if handler:
                    bus.disconnect(handler)
                bus.remove_signal_watch()
            except Exception as exc:
                log("teardown: %s bus watch: %s" % (name, exc))
        self._bus_watch = {}

    def _release_pipelines(self):
        """Take both pipelines to NULL and drop everything holding them up.

        The element handles go too. build() reassigns all of them, but it does
        so one at a time and can raise part way through, and a stale self.valve
        pointing into a pipeline that has been torn down is exactly the
        half-built state _retune exists to prevent.
        """
        self._remove_bus_watches()
        for pipeline in (self.tx, self.rx):
            if pipeline:
                pipeline.set_state(Gst.State.NULL)
        self.tx = self.rx = None
        self.valve = self.sink = self.payloader = None
        self.volume = self.mixer = self.rtpbin = None
        self.shaper = self.equalizer = self.txhead = None
        self.rx_branches = {}
        self.jitterbuffers = {}
        self.branch_seen = {}
        self.rx_parked = {}
        self._reviving = set()

    def _play(self, which):
        """Take one pipeline to PLAYING, and prove that it got there.

        set_state() not returning FAILURE is not proof. A port already bound or
        an ALSA device held by something else fails during the state change,
        not during parse_launch(), and the old retune path treated a successful
        parse as a working pipeline. get_state() is what actually waits for the
        answer.

        ASYNC after the timeout is reported but not treated as a failure: a
        live pipeline can still be settling, the bus watch already owns real
        errors, and turning a slow start into a teardown would be a worse bug
        than the one being fixed here.
        """
        pipeline = self.tx if which == "tx" else self.rx
        if pipeline is None:
            log("%s pipeline missing" % which)
            return False
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            log("%s pipeline refused to start" % which)
            return False
        ret, state, _pending = pipeline.get_state(5 * Gst.SECOND)
        if ret == Gst.StateChangeReturn.FAILURE:
            log("%s pipeline failed to reach PLAYING" % which)
            return False
        if ret == Gst.StateChangeReturn.ASYNC:
            log("%s pipeline still settling after 5s (%s)"
                % (which, Gst.Element.state_get_name(state)))
        self._mark_playing(which)
        return True

    def _mark_playing(self, which):
        """Restart the watchdog's clocks for a pipeline that was just started.

        Both clocks, and both matter. _playing_since gives the pipeline its
        grace period; _flow_ts stops the first tick after a restart judging the
        new pipeline on how long the old one had been silent.
        """
        now = time.monotonic()
        self._playing_since[which] = now
        self._flow_ts[which] = now

    def _panic(self, reason):
        """Give up in process and let systemd rebuild the whole voice stack.

        Exiting IS the restart. `systemctl restart mesh-voice` from inside
        mesh-voice is the obvious alternative and it is worse: the job blocks
        on the very unit issuing it, and the --no-block form races the process
        it is killing. Restart=on-failure with RestartSec=10 already does this
        properly, and a fresh process is the whole point: new ALSA handles, new
        sockets, a new multicast join, mesh.conf read again and, with lyra,
        freshly loaded TFLite models.
        """
        log("voice stack restart: %s" % reason)
        self._fault = reason
        self.exit_code = 1
        try:
            self.write_state()
        except Exception:
            pass
        self.stop(keep_state=True)

    def _note_pipeline_error(self, which, text, debug):
        """Log a pipeline error and schedule a restart, backing off if it repeats.

        The first occurrence of a given error is logged in full, along with when
        the retry will happen. Identical errors after that are counted, not
        logged — a missing sound card produces the same two lines for ever, and
        printing them every few seconds buries everything else in the journal.
        The tally is emitted once, on recovery or when the error changes.
        """
        state = self._pipeline_fault.get(which)
        now = time.time()

        if state is None or now - state["last_ts"] > PIPELINE_RETRY_RESET_SEC:
            state = {"delay": PIPELINE_RETRY_BASE_SEC, "text": None, "repeats": 0,
                     "last_ts": now, "capped": False}
            self._pipeline_fault[which] = state
        state["last_ts"] = now

        if text == state["text"]:
            state["repeats"] += 1
            # Say something once when the backoff tops out, so a permanently
            # broken pipeline is still visible in the log rather than going
            # completely silent, then stay quiet.
            if state["delay"] >= PIPELINE_RETRY_MAX_SEC and not state["capped"]:
                state["capped"] = True
                log("%s pipeline: still failing after %d attempt(s) — retrying "
                    "every %ds, further identical errors suppressed"
                    % (which, state["repeats"], PIPELINE_RETRY_MAX_SEC))
        else:
            self._flush_pipeline_repeats(which, state)
            state["text"] = text
            state["repeats"] = 0
            state["capped"] = False
            log("%s pipeline error: %s (%s) — retrying in %ds"
                % (which, text, debug, state["delay"]))

        GLib.timeout_add_seconds(state["delay"], self._restart, which)
        state["delay"] = min(state["delay"] * 2, PIPELINE_RETRY_MAX_SEC)

    def _flush_pipeline_repeats(self, which, state):
        """Emit the suppressed-repeat tally, if there is one."""
        if state and state["repeats"]:
            log("%s pipeline: previous error repeated %d more time(s)"
                % (which, state["repeats"]))
            state["repeats"] = 0

    def _note_pipeline_ok(self, which):
        """A buffer flowed, so whatever was wrong with this pipeline is over."""
        state = self._pipeline_fault.pop(which, None)
        if state and state["text"] is not None:
            self._flush_pipeline_repeats(which, state)
            log("%s pipeline recovered" % which)

    def _restart(self, which):
        pipeline = self.tx if which == "tx" else self.rx
        # Only announce the restart while the error is still being logged;
        # during a suppressed streak this would just be more of the same noise.
        state = self._pipeline_fault.get(which)
        if not state or not state["repeats"]:
            log("%s pipeline restarting" % which)
        pipeline.set_state(Gst.State.NULL)
        pipeline.set_state(Gst.State.PLAYING)
        # The watchdog measures flow from the moment a pipeline was last asked
        # to play, so a restart has to move that mark. Without it the next tick
        # judges the new pipeline on the old one's silence and restarts it
        # again immediately, burning the escalation budget in fifteen seconds.
        self._mark_playing(which)
        return False  # one-shot

    def _on_tx_buffer(self, _pad, _info):
        self.tx_packets += 1
        # Data flowing is the only honest evidence a pipeline came back. The
        # dict is empty in the normal case, so this costs a truth test per
        # buffer and nothing else.
        if self._pipeline_fault:
            self._note_pipeline_ok("tx")
        return Gst.PadProbeReturn.OK

    def _on_rx_buffer(self, _pad, info):
        # Our own transmission, if the socket-level suppression did not take.
        # This is the guarantee, not the optimisation. _suppress_multicast_
        # loopback reaches for a socket that may not exist yet and swallows
        # every failure, so it can silently leave loopback on, and what that
        # sounds like is the operator's own voice in their headset one jitter
        # buffer late. Dropping here also keeps rx_active honest: without it a
        # half-duplex node reads its own audio as a remote talker and refuses
        # to key.
        #
        # Safe to key off the SSRC because it is a hash of our own mesh address
        # plus a per-run generation byte (see node_ssrc), so no peer can
        # collide with it without already sharing our IP.
        if self.my_ssrc is not None:
            buf = info.get_buffer()
            # RTP fixed header is 12 bytes, SSRC a big-endian u32 at offset 8.
            if buf is not None and buf.get_size() >= 12:
                if int.from_bytes(buf.extract_dup(8, 4), "big") == self.my_ssrc:
                    self.rx_loopback += 1
                    return Gst.PadProbeReturn.DROP

        self.rx_packets += 1
        self.last_rx_ms = now_ms()
        if self._pipeline_fault:
            self._note_pipeline_ok("rx")
        if not self.rx_active:
            self.rx_active = True
        return Gst.PadProbeReturn.OK

    def _on_capture_buffer(self, _pad, _info):
        """Capture reached the valve. Runs whether or not the PTT is pressed."""
        self._flow_count["tx"] += 1
        self._flow_ts["tx"] = time.monotonic()
        self._ever_flowed["tx"] = True
        return Gst.PadProbeReturn.OK

    def _on_playback_buffer(self, _pad, _info):
        """Audio reached the speaker, if only the mixer's silence."""
        self._flow_count["rx"] += 1
        self._flow_ts["rx"] = time.monotonic()
        self._ever_flowed["rx"] = True
        return Gst.PadProbeReturn.OK

    # -- stall watchdog --

    def _tick_health(self):
        """Watch for a pipeline that stopped moving audio without saying so.

        Deliberately separate from the bus-error path, and it defers to it: a
        pipeline with a live entry in _pipeline_fault is skipped entirely,
        because _note_pipeline_error's backoff already owns it. That includes
        the node provisioned with voice=y before its OpenVLM board was fitted,
        which would otherwise be restarted every fifteen seconds for ever. This
        one only judges silence.
        """
        # Unconditional, and independent of voice_watchdog: its only job is to
        # prove the GLib main loop is still turning, which is the one failure
        # everything below is structurally unable to catch, since everything
        # below runs on that loop.
        sd_notify("WATCHDOG=1")
        if not self.cfg.watchdog:
            return True

        now = time.monotonic()
        for which in ("tx", "rx"):
            if which in self._pipeline_fault:
                continue            # the error path owns this one
            if not self._ever_flowed[which]:
                continue            # never worked, so nothing has regressed
            since = self._playing_since[which]
            if not since or now - since < VOICE_STALL_GRACE_SEC:
                continue
            idle = now - self._flow_ts[which]
            if idle < self.cfg.watchdog_sec:
                continue
            self._on_stall(which, "no %s for %.0fs"
                           % ("capture buffers" if which == "tx"
                              else "playback buffers", idle))

        self._check_igmp(now)
        return True

    def _check_igmp(self, now):
        """Catch a receiver that is bound, PLAYING and no longer in the group.

        udpsrc joins the multicast group once, at start. If the membership goes
        away underneath it (br0 rebuilt, or the mesh interface dropped from the
        bridge and re-added) nothing is torn down and nothing is logged: the
        socket stays bound, the pipeline stays PLAYING, and it simply never
        receives again. No dataflow check can see that, because a receiver
        nobody is talking to looks exactly the same.

        Losing the join also stops us being heard, not merely from hearing:
        batman-adv drops multicast to a group with no listeners, so on a
        two-node mesh a dropped membership takes out both directions.
        """
        if "rx" in self._pipeline_fault:
            return
        joined = igmp_groups(self.cfg.iface)
        if joined is None:
            self._igmp_ok = None            # unknown is not a fault
            return
        self._igmp_ok = bool(joined & TALK_GROUP_HEX)
        if self._igmp_ok:
            self._igmp_lost_since = 0.0
            return
        if not self._igmp_lost_since:
            self._igmp_lost_since = now     # one miss could be a rejoin in flight
            return
        if now - self._igmp_lost_since >= self.cfg.watchdog_sec:
            self._igmp_lost_since = 0.0
            self._on_stall("rx", "multicast membership for %s dropped on %s"
                           % (TALK_GROUP_ADDR, self.cfg.iface))

    def _on_stall(self, which, why):
        """One pipeline stalled silently: restart it, or the process.

        Restarting the pipeline is nearly free and fixes a wedged element. When
        it does not, restarting it again will not either, so the escalation is
        deliberately shallow: three goes inside ten minutes and the whole stack
        is handed to systemd.
        """
        self._stalls[which] += 1
        now = time.monotonic()
        history = [t for t in self._stall_history[which]
                   if now - t < VOICE_STALL_WINDOW_SEC]
        history.append(now)
        self._stall_history[which] = history
        log("%s pipeline stalled: %s (%d since start, %d inside %ds)"
            % (which, why, self._stalls[which], len(history),
               VOICE_STALL_WINDOW_SEC))
        if len(history) >= VOICE_STALL_MAX_RESTARTS:
            self._panic("%s pipeline stalled %d times in %ds and restarting it "
                        "did not fix it"
                        % (which, len(history), VOICE_STALL_WINDOW_SEC))
            return
        self._restart(which)

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

    # -- conference receive --

    def _on_new_jitterbuffer(self, _rtpbin, jitterbuffer, session, ssrc):
        """rtpbin made a jitter buffer for a new talker; keep it for stats.

        Loss is per talker now. The packing controller sums across these, which
        is the right aggregate: it is asking "how lossy is this radio link",
        not "how lossy is any one speaker".
        """
        self.jitterbuffers[ssrc] = jitterbuffer
        # SSRC is the sender's mesh address, so this names the talker outright
        # rather than printing an opaque 32-bit number.
        who = self.talker_names.get(ssrc >> 8) or ("ssrc 0x%08x" % ssrc)
        log("rx: new talker %s (ssrc 0x%08x)" % (who, ssrc))

    def _on_rtp_pad_added(self, _rtpbin, pad):
        """rtpbin made a receive pad for a talker. Give it a decode branch.

        Fires once per source for the life of the daemon, because
        autoremove=false means rtpbin never lets a source go. That is the whole
        reason _park_branch exists: an evicted talker will never get a second
        pad-added to rebuild them.
        """
        name = pad.get_name()
        if not name.startswith("recv_rtp_src_"):
            return
        if name in self.rx_branches or name in self.rx_parked:
            return
        # Park on failure rather than walk away: an unlinked rtpbin pad is the
        # bug _park_branch exists to prevent, and a branch that could not be
        # built is no different from one that was evicted.
        if not self._attach_branch(pad):
            self._park_branch(name, pad)
            return
        self._prune_branches()

    def _attach_branch(self, pad):
        """Build one talker's decode branch and feed it into the mixer."""
        name = pad.get_name()
        try:
            # An explicit capsfilter, not bare caps: as the last item in a bin
            # description the parser reads "audio/x-raw,..." as an element name
            # and fails with 'no element "audio"'. Every talker must arrive at
            # the mixer in the same format, so this cannot simply be dropped.
            depay = Gst.parse_bin_from_description(
                "%s ! %s ! audioconvert ! audioresample ! capsfilter "
                "caps=\"audio/x-raw, rate=(int)%d, channels=(int)1, "
                "format=(string)S16LE\""
                % (self._rx_depay_desc, self._rx_dec_desc, self._rx_raw_rate),
                True)
        except GLib.Error as exc:
            log("rx: cannot build branch for %s: %s" % (name, exc))
            return False

        self.rx.add(depay)
        mixpad = self.mixer.request_pad_simple("sink_%u")
        if mixpad is None:
            log("rx: audiomixer refused a pad for %s" % name)
            self.rx.remove(depay)
            return False

        if (pad.link(depay.get_static_pad("sink")) != Gst.PadLinkReturn.OK
                or depay.get_static_pad("src").link(mixpad)
                != Gst.PadLinkReturn.OK):
            log("rx: link failed for %s" % name)
            self.mixer.release_request_pad(mixpad)
            self.rx.remove(depay)
            return False

        depay.sync_state_with_parent()
        # The rtpbin pad is kept, not just the elements hanging off it. Eviction
        # has to be able to find this pad again to park it, and revival has to
        # be able to re-link it.
        self.rx_branches[name] = (pad, depay, mixpad)
        self.branch_seen[name] = time.time()
        # Touch on every decoded buffer so the LRU below evicts the talker who
        # has been quiet longest, not whoever happened to arrive first.
        depay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER,
            lambda _p, _i, n=name: (self.branch_seen.__setitem__(n, time.time()),
                                    Gst.PadProbeReturn.OK)[1])
        log("rx: talker branch up (%d active)" % len(self.rx_branches))
        return True

    def _tick_beacon(self):
        """Announce ourselves so receivers establish our source before we talk.

        This is the only thing that actually pre-establishes a talker, and the
        alternative was measured and rejected. Synthesising a source locally
        from a peer's registry address does create the slot -- but our invented
        sequence numbers and timestamps become the source's base, the real
        sender's do not match, and the jitter buffer resyncs and discards the
        transmission. Measured end to end: the peer talked for three seconds
        and the output was digital silence, peak amplitude zero. A receive
        source can only be established by the node that owns the SSRC.

        So we do it from the sending side: mute, open the valve for one packet
        or two, close, unmute. That is real RTP from the real payloader with
        the real sequence numbers, which is exactly what makes the next
        transmission arrive whole.

        It costs about 4 packets a minute at the default interval. batman-adv
        drops multicast that nobody has joined, so beacons only occupy air when
        somebody is actually listening.
        """
        if self.transmitting or self.ptt_pressed:
            return True                      # never interrupt a transmission
        if not self.valve or not self.volume:
            return True
        self.volume.set_property("volume", 0.0)
        self.valve.set_property("drop", False)
        GLib.timeout_add(BEACON_MS, self._end_beacon)
        return True

    def _end_beacon(self):
        # _set_tx already restores volume if PTT beat us to it, so only close
        # the valve when we are not actually transmitting.
        if not self.transmitting and self.valve:
            self.valve.set_property("drop", True)
        if self.volume:
            self.volume.set_property("volume", 1.0)
        return False                          # one-shot

    def _size_talker_table(self, peer_count):
        """Keep the branch table comfortably above the number of known nodes.

        Every node on the talk group is a potential talker, and an evicted
        talker pays the first-contact penalty again next time they speak. So
        the table tracks the registry rather than a guess: known nodes plus
        headroom, never below the configured value, never above the hard cap.
        """
        want = min(peer_count + VOICE_TALKER_HEADROOM, VOICE_MAX_TALKERS_HARD)
        if want > self.cfg.max_talkers:
            log("rx: %d nodes known — raising warm talker table %d -> %d "
                "(~%d MB with lyra)"
                % (peer_count, self.cfg.max_talkers, want, want * 5))
            self.cfg.max_talkers = want
        elif peer_count + VOICE_TALKER_HEADROOM > VOICE_MAX_TALKERS_HARD:
            log("rx: %d nodes known but the warm talker table is capped at %d "
                "— the least recently heard will be evicted and pay a "
                "first-contact delay when they next speak"
                % (peer_count, VOICE_MAX_TALKERS_HARD))

    def _prune_branches(self):
        """Keep at most VOICE_MAX_TALKERS branches, dropping the quietest.

        Branches are never reaped on idle (see the pipeline comment), so the
        only bound is this one. Each costs about 5.3 MB with lyra, measured, so
        the default cap is roughly 40 MB against 3.4 GB free — the cap exists to
        stop unbounded growth when nodes churn SSRCs across restarts and
        retunes, not because the memory is scarce.

        Evicting a talker only costs them the head of their next transmission,
        and only if they were the least recently heard. That is true only
        because the pad is parked rather than abandoned; see _park_branch.
        """
        while len(self.rx_branches) > self.cfg.max_talkers:
            oldest = min(self.branch_seen, key=self.branch_seen.get)
            entry = self.rx_branches.pop(oldest, None)
            self.branch_seen.pop(oldest, None)
            if entry is None:
                continue
            pad, branch, mixpad = entry
            branch.set_state(Gst.State.NULL)
            self.rx.remove(branch)
            self.mixer.release_request_pad(mixpad)
            self._park_branch(oldest, pad)
            log("rx: evicted least-recent talker branch (cap %d)"
                % self.cfg.max_talkers)

    def _park_branch(self, name, pad):
        """Hold an evicted talker's rtpbin pad on a fakesink, and watch it.

        Tearing the decode branch down leaves rtpbin's receive pad unlinked,
        and rtpbin never takes that pad back: autoremove=false keeps the source
        for the life of the daemon, so no second pad-added will ever arrive to
        rebuild the branch. An evicted talker was therefore inaudible for good,
        which is not what "least recently heard" is supposed to cost, and it is
        silent on both ends: they hear the acknowledgement, we simply never
        hear them again.

        Parking fixes both halves of that. The fakesink keeps the pad linked,
        so pushing to it returns OK instead of NOT_LINKED, and the buffer probe
        is what tells us they have started talking again, which is the signal
        pad-added would have given us if rtpbin still emitted one.
        """
        sink = Gst.ElementFactory.make("fakesink", None)
        if sink is None:
            log("rx: no fakesink to park %s on; that talker is lost until "
                "they restart" % name)
            return
        sink.set_property("sync", False)
        sink.set_property("async", False)
        self.rx.add(sink)
        if pad.link(sink.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            log("rx: could not park %s" % name)
            self.rx.remove(sink)
            return
        sink.sync_state_with_parent()
        probe = pad.add_probe(Gst.PadProbeType.BUFFER, self._on_parked_buffer,
                              name)
        self.rx_parked[name] = (pad, sink, probe)

    def _on_parked_buffer(self, _pad, _info, name):
        """A parked talker started speaking again. Rebuild them.

        Runs on a streaming thread, so it only schedules: relinking pads and
        adding elements is main-loop work. The buffer that triggered this goes
        to the fakesink, which is the measured 180 ms head loss that eviction
        has always been documented to cost.
        """
        if name not in self._reviving:
            self._reviving.add(name)
            GLib.idle_add(self._revive_branch, name)
        return Gst.PadProbeReturn.OK

    def _revive_branch(self, name):
        """Take a talker off the fakesink and give them a decoder again."""
        self._reviving.discard(name)
        entry = self.rx_parked.pop(name, None)
        if entry is None:
            return False
        pad, sink, probe = entry
        if probe:
            pad.remove_probe(probe)
        pad.unlink(sink.get_static_pad("sink"))
        sink.set_state(Gst.State.NULL)
        self.rx.remove(sink)
        who = self.talker_names.get(self._ssrc_of(name, 0) >> 8) or name
        log("rx: parked talker %s is back, rebuilding their branch" % who)
        if not self._attach_branch(pad):
            self._park_branch(name, pad)      # keep the pad linked and watched
            return False
        self._prune_branches()
        return False

    @staticmethod
    def _ssrc_of(pad_name, default=0):
        """SSRC out of an rtpbin pad name: recv_rtp_src_<session>_<ssrc>_<pt>."""
        try:
            return int(pad_name.split("_")[4])
        except (IndexError, ValueError):
            return default

    def _on_rtp_pad_removed(self, _rtpbin, pad):
        """Tear the branch down when rtpbin times the talker out.

        Without this the pipeline accumulates a decoder per talker per session
        for the life of the daemon — on a busy net that is a slow leak of both
        memory and CPU, and with lyra each one holds a TFLite interpreter.
        """
        name = pad.get_name()
        self._reviving.discard(name)
        parked = self.rx_parked.pop(name, None)
        if parked is not None:
            _pad, sink, probe = parked
            if probe:
                pad.remove_probe(probe)
            sink.set_state(Gst.State.NULL)
            self.rx.remove(sink)
        entry = self.rx_branches.pop(name, None)
        self.branch_seen.pop(name, None)
        if entry is None:
            return
        _pad, branch, mixpad = entry
        branch.set_state(Gst.State.NULL)
        self.rx.remove(branch)
        self.mixer.release_request_pad(mixpad)
        # Drop jitter buffers whose element has left the pipeline, so the stats
        # dict does not grow without bound either.
        for ssrc in [s for s, jb in self.jitterbuffers.items()
                     if jb.get_parent() is None]:
            del self.jitterbuffers[ssrc]
        log("rx: talker branch down (%d active)" % len(self.rx_branches))

    def _rx_loss_stats(self):
        """(pushed, lost) summed over every current talker's jitter buffer."""
        pushed = lost = 0
        for jb in list(self.jitterbuffers.values()):
            try:
                stats = jb.get_property("stats")
                pushed += stats.get_value("num-pushed") or 0
                lost += stats.get_value("num-lost") or 0
            except Exception:
                continue
        return pushed, lost

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
        multicast RTP has no back channel. It is a decent proxy because it
        measures the same radio link in the other direction, but it is only a
        proxy, and an asymmetric link will fool it. Summed across talkers,
        since the question is how lossy the link is, not who is speaking.
        """
        if self.cfg.codec != "lyra":
            return False        # nothing to adapt; stop the timer
        if self.transmitting:
            # Our own transmission crowds the air and we are not listening to
            # it, so a window that overlaps TX says nothing useful about the
            # link. Skipping is not about duplex policy -- it is about not
            # measuring during the one period we cannot measure.
            return True

        pushed, lost = self._rx_loss_stats()

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
        if on and self.volume:
            # A beacon may have muted us moments ago; never key up silent.
            self.volume.set_property("volume", 1.0)
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

        # The registry is read unconditionally. It drives naming, warm-branch
        # sizing and the new-node beacon, none of which have anything to do
        # with unicast -- an earlier revision read it only when
        # voice_unicast=y, which meant that on the default configuration
        # talkers showed as raw SSRC hex, the talker table never grew past its
        # floor, and a node joining mid-operation was never announced to.
        registry = read_registry(self.local_ips)

        peers = []
        if self.cfg.unicast:
            peers = registry
            if self.cfg.max_peers and len(peers) > self.cfg.max_peers:
                peers = peers[:self.cfg.max_peers]
            clients += ["%s:%d" % (ip, self.cfg.port) for ip, _ in peers]

        # Every known node is a potential talker, so the warm-branch table and
        # the ssrc->name map are both driven from the registry.
        self._size_talker_table(len(registry))
        self.talker_names = {}
        for ip, host in registry:
            prefix = ssrc_prefix_for_ip(ip)
            if prefix is not None:
                self.talker_names[prefix] = host or ip

        # A node we have not seen before cannot have heard us either, so
        # announce ourselves rather than waiting for the periodic beacon.
        new_ips = {ip for ip, _ in registry} - self._known_peer_ips
        if new_ips and self.cfg.beacon_sec:
            log("beacon: %d new node(s) in registry — announcing" % len(new_ips))
            GLib.timeout_add(500, self._tick_beacon)
        self._known_peer_ips = {ip for ip, _ in registry}

        if peers != self.peers:
            log("peers: %d unicast target(s)%s" % (
                len(peers),
                (" — " + ", ".join(h or ip for ip, h in peers)) if peers else ""))
        self.peers = peers
        if self.sink:
            self.sink.set_property("clients", ",".join(clients))

    # -- state --

    def _idle(self, which):
        """Seconds since this flow last produced, or None if it never has."""
        if not self._ever_flowed[which]:
            return None
        return round(max(0.0, time.monotonic() - self._flow_ts[which]), 1)

    def _tick_state(self):
        self.write_state()
        return True

    def write_state(self):
        # Totals across every talker's jitter buffer. Late and duplicate are
        # summed the same way; per-talker detail is not worth the UI space.
        stats = {}
        late = dups = 0
        for jb in list(self.jitterbuffers.values()):
            try:
                s = jb.get_property("stats")
                late += s.get_value("num-late") or 0
                dups += s.get_value("num-duplicates") or 0
            except Exception:
                continue
        pushed, lost = self._rx_loss_stats()
        stats = {"num-lost": lost, "num-late": late, "num-duplicates": dups,
                 "num-pushed": pushed}

        state = {
            # "restarting" only ever appears in the ten seconds between _panic
            # writing this and systemd having the daemon back, and it exists so
            # the VOICE tab can say why rather than going blank.
            "service": "restarting" if self._fault else "running",
            "fault": self._fault,
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
            "codec": getattr(self, "effective_codec", self.cfg.codec),
            "codec_configured": self.cfg.codec,
            "codec_fallback": (getattr(self, "effective_codec", self.cfg.codec)
                               != self.cfg.codec),
            "bitrate": (self.cfg.lyra_bitrate if self.cfg.codec == "lyra"
                        else self.cfg.bitrate),
            "frame_ms": (self.packing * 20 if self.cfg.codec == "lyra"
                         else self.cfg.frame_ms),
            "frames_per_packet": (self.packing if self.cfg.codec == "lyra"
                                  else None),
            "rx_loss_pct": round(self.rx_loss_pct, 1),
            "unicast": self.cfg.unicast,
            "talkers": len(self.rx_branches),
            "talkers_parked": len(self.rx_parked),
            "talker_names": sorted(
                self.talker_names.get(sr >> 8) or ("0x%08x" % sr)
                for sr in self.jitterbuffers),
            "max_talkers": self.cfg.max_talkers,
            "peers": [{"ip": ip, "hostname": host} for ip, host in self.peers],
            "tx_packets": self.tx_packets,
            "rx_packets": self.rx_packets,
            "rx_lost": stats.get("num-lost", 0),
            "rx_late": stats.get("num-late", 0),
            "rx_duplicates": stats.get("num-duplicates", 0),
            "rx_loopback": self.rx_loopback,
            # Watchdog view. capture/playback are the flows the stall detector
            # actually judges; tx_packets and rx_packets above are traffic, and
            # are flat on a quiet talk group by design.
            "highpass_hz": self.cfg.highpass_hz,
            "lowpass_hz": self.cfg.lowpass_hz,
            "eq": ([round(g, 2) for g in self._eq_gains(self._raw_rate)]
                   if self.cfg.eq else None),
            "tx_gain_db": round(20.0 * math.log10(self._tx_gain), 1),
            "watchdog": self.cfg.watchdog,
            "watchdog_sec": self.cfg.watchdog_sec,
            "capture_buffers": self._flow_count["tx"],
            "playback_buffers": self._flow_count["rx"],
            # null, not a very large number, when the flow has never run at
            # all: a node whose OpenVLM was never fitted has no audio device,
            # which is a different thing from one whose audio device stopped,
            # and the watchdog treats them differently too.
            "capture_idle": self._idle("tx"),
            "playback_idle": self._idle("rx"),
            "stalls": self._stalls["tx"] + self._stalls["rx"],
            "igmp_joined": self._igmp_ok,
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
    # Non-zero when _panic decided the stack needed rebuilding, which is what
    # turns Restart=on-failure into the recovery path.
    return voice.exit_code


if __name__ == "__main__":
    sys.exit(main())
