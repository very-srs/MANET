# Push to talk

## Marine

![Crayon drawing of a hand pressing a wired push-to-talk button on one radio while sound travels wirelessly to a headset plugged into another radio.](../assets/voice-crayon.png)

### Same group. Press. Talk.

1. **Voice enabled.** Use a supported audio/PTT setup plugged into the radio.
2. **Same talk group.** Everyone who needs to hear you chooses that group.
3. **Hold the button.** Speak into the headset microphone.
4. **Release.** Your microphone stops transmitting.

You can hear other speakers while your button is released. Speaking louder does not make the signal go farther.

**Too quiet or too loud?** Move the sliders on the Voice tab's GAIN card.

## User

### Voice comes from the radio's headset

Mesh PTT uses an audio device and physical push-to-talk button attached to the radio. Enable mesh voice during setup. The web interface shows status, lets you choose a talk group, and sets this radio's listening level. It is not a browser microphone.

The supported USB path uses a CM108-family audio device and is intended for OpenVLM hardware. Headset wiring and microphone level matter: a military dynamic microphone needs suitable amplification before the CM108B input. Check the [audio hardware reference](../../../MANET/node_tools/README.md#audio-hardware) before connecting a headset. An electret mic needs no amplification on a bare OpenVLM.

### Select the same talk group

Open the management UI's Voice tab. Radios start on group 1 and can select groups 1 through 32. Set each participating radio to the group you want. Changing your radio's group does not move anybody else.

Hold the physical PTT button to transmit. Release it to stop transmitting. Receivers can mix simultaneous speakers, so two people talking at once are both heard.

### When there is no audio

Confirm voice is enabled, the mesh is connected, the talk groups match, and the Voice tab reports a healthy audio path. Then check the audio hardware and microphone wiring.

The default codec is **Lyra**. It transfers good-quality audio with less bandwidth than Opus, but lacks compatibility with other tools that would expect Opus. Lyra and Opus do not interoperate. A radio that missed a mesh-wide codec change while powered off must be set to the same codec. Reissue the change with it online.

A receiver remembers at most 128 talkers, and each radio can add only a few new ones in a short time. If one radio's voice service restarts many times in quick succession, other radios may refuse its new voice for a while, until its old entries expire after about 15 minutes without traffic.

### Set the listening level

The GAIN card on the Voice tab sets how loud other speakers sound on this radio. It changes only this radio, and a change takes effect within a moment, even during a conversation.

- Receive gain makes every incoming voice louder, from 0 to 24 dB. Each 10 dB sounds about twice as loud. At 0 the voices are played exactly as received. Above 0 a limiter keeps a loud speaker from distorting. A voice that is already loud gains less than a quiet one.
- Playback level is the headset volume control on the radio's audio device, from 0 to 100%. Until you move it, the audio device keeps its own volume and the GAIN card shows "card default". Once you set it, the radio applies it whenever voice starts and again if the audio device is reconnected.
- Input peak shows how loud your own microphone is, refreshed every 2 seconds. It works with the button released, so you can check it without transmitting. Yellow means loud. Red means the sound is close to the loudest the radio can capture, so speak more softly or move the microphone away from your mouth.

If speakers are too quiet, raise playback level first. If they are still too quiet at 100%, raise receive gain a few dB at a time.

## Developer

### Pipeline and transport

`mesh-voice.py` owns GStreamer transmit and receive pipelines. The physical PTT controls the transmit valve. OpenVLM/CM108B input is read through USB HID, not an SBC GPIO. The web interface exposes status and configuration, not browser capture.

Voice defaults to Lyra using the packaged GStreamer elements and model weights. Missing Lyra artifacts can trigger an Opus fallback, which cannot communicate with a Lyra mesh. Treat codec and payload framing as compatibility requirements, not quality preferences.

Talk groups share multicast address `239.192.41.1`. Group *n* uses RTP port `38801 + (n - 1) * 2`. The next port is reserved for RTCP, but no RTCP is sent or received. Each packet passes source admission before it reaches the RTP session. The receive path demultiplexes SSRCs, gives talkers their own decode/jitter paths, and mixes them. Optional half-duplex behavior refuses local PTT while a remote source is transmitting.

The transmit path captures audio through ALSA, measures its level, gates it
with the PTT valve, encodes it, and places the encoded frames into RTP
packets. On receive, RTP identifies the sender's stream, a jitter buffer
reorders and schedules packets, and the matching decoder produces PCM audio
for the mixer. GStreamer handles the audio path. The Python daemon supervises
state and devices rather than processing each audio sample.

Each sender has its own RTP sequence numbers and timing. Feeding several
senders into one jitter buffer would mix unrelated sequence spaces and
discard or misorder valid packets. Separate receive branches preserve those
timelines, then the mixer combines their decoded audio. A permanent silent
input keeps the playback device running while nobody speaks, avoiding a
device startup at every PTT press.

### Establishing a speaker before speech starts

The RTP synchronization-source identifier, or SSRC, identifies one sender
instance. Each run picks a fully random 32-bit SSRC, and receivers name a
talker from its sender address, not from the SSRC. Reusing the same source
identity after a restart could pair new sequence numbers with a receiver's
old state. A fresh SSRC lets receivers establish the restarted stream
separately. Receivers admit at most 128 sources, with a small per-address
budget for new ones. An admitted source is released only when the RTP
session's inactivity timer removes it, about 15 minutes after its last packet.

Receivers keep established sources through silence so the next transmission
does not pay the full branch-creation cost. A sender also emits a short
muted presence transmission at startup and when newly observed peers need
to learn it. The beacon uses the sender's actual RTP sequence/timestamp
space, as a receiver inventing packets on another sender's behalf would create
the wrong state. These choices reduce clipped beginnings without continuous
audio transmission. The [voice internals](../../../docs/node-tools-internals.md#voice)
describe branch limits, cleanup, and the fallback beacon interval.

Local multicast loopback is disabled on the transmitting socket, and the
receive path also discards packets carrying its own SSRC. Otherwise a radio
could play its microphone back a jitter-buffer delay later and, in optional
half-duplex mode, mistake itself for a remote speaker.

### Receive level

Two controls set what an operator hears. `voice_playback_pct` is the card's
own playback mixer. It is unset by default, and the daemon then leaves the
card at its power-up level. Once an operator sets it, the daemon writes the
level with `amixer` after both pipelines have started, because no ALSA
restore covers a USB card that enumerates after boot. It writes it again
after a receive pipeline restart and after every pipeline rebuild, because a
replugged card comes back at its power-up level. Writing the mixer just
before the playback device opens made that open fail as busy in one of
three CM4 restarts, which is why the write follows the state change. The
daemon skips this step when `voice_alsa_out` overrides the device.

`voice_rx_gain_db` is software gain inside each talker's decode branch,
after the decoder and before the mixer. The branch converts to 32-bit float,
applies `audioamplify` without clipping, and then a hard-knee
`audiodynamic` limiter with its threshold at -6 dBFS. The limiter keeps 1/20
of the excess above that threshold. Per-talker placement means one loud
speaker is limited without pulling the others down, and the sum of several
talkers is then clipped only by the mixer. At 0 dB the limiter runs at a
ratio of 1, so the default leaves decoded audio unchanged. `audiodynamic`
multiplies the excess by the ratio, so a ratio above 1 expands rather than
limits.

Both elements are named in every branch, including at 0 dB. A SIGHUP from
the GAIN card updates them on the main loop by walking a copy of
`rx_branches`, taken under the sources lock as the
[threads and reload](../../../docs/node-tools-internals.md#threads-and-reload)
rules require. Parked talkers have no decoder and pick up the current gain
when their branch is rebuilt. A reload that also rebuilds the pipelines
builds every branch at the new gain.

The input meter reads the peak from the transmit pipeline's existing
`level` element, which runs ahead of the PTT valve and therefore moves
while the button is released. `/run/mesh-voice.json` reports `rx_gain_db`,
`playback_level` (`null` when the mixer was not set), `mic_rms_db`, and
`mic_peak_db`. The [receive level internals](../../../docs/node-tools-internals.md#receive-level)
hold the measured limiter response. See the
[gain tests](../../../MANET/node_tools/test_voice_gain.py) for the reload paths.

### Multicast, scheduling, and packet size

BATMAN's multicast optimization already fans out to unicast where applicable. Receivers must join the multicast group. Additional userspace unicast redundancy is off by default. Voice uses DSCP CS6 (48) to reach the desired queue through BATMAN's priority mapping.

Group membership is functional, not merely a receiver preference. BATMAN
can suppress a multicast transmission when it knows of no listeners. With
eligible listener counts, it forwards separate radio unicast frames to the
listeners, obtaining the underlying link's ACK/retry behavior. Adding another
application-level unicast copy would then spend airtime on the same audio
twice. That is why multicast reception and membership propagation should be
checked before interpreting silence as an encoder fault.

Lyra adapts frames per packet while keeping bitrate fixed. Under loss it reduces packet duration, subject to the low-throughput floor, to limit the speech lost with each packet. Buffer sizing and branch lifetime matter independently of codec speed. Do not describe estimated pipeline delay as measured mouth-to-ear latency.

### Configuration and failure handling

Talk-group changes are local: write `voice_channel` and reload through the checked retune path. Codec changes require mesh-wide staging and ACKs because encoder, payload, and clock rate change together. An offline node does not automatically reconcile a missed codec change.

Transmit remains blocked until the socket is bound to the configured voice interface and its IPv4 address. A held PTT resumes only when the binding and duplex conditions permit it. A watchdog monitors actual audio flow so a pipeline reporting PLAYING cannot hide a stalled USB path indefinitely. `/run/mesh-voice.json` supplies the status readout.

- [Voice settings and operational behavior](../../../MANET/node_tools/README.md#push-to-talk-voice)
- [Pipeline, multicast, buffering, and QoS internals](../../../docs/node-tools-internals.md#voice)
- [Voice daemon](../../../MANET/node_tools/mesh-voice.py)
