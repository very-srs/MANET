# Push to talk

## Marine

![Crayon drawing of a hand pressing a wired push-to-talk button on one radio while sound travels wirelessly to a headset plugged into another radio.](../assets/voice-crayon.png)

### Same group. Press. Talk.

1. **Voice enabled.** Use a supported audio/PTT setup plugged into the radio.
2. **Same talk group.** Everyone who needs to hear you chooses that group.
3. **Hold the button.** Speak into the headset microphone.
4. **Release.** Your microphone stops transmitting.

You can hear other speakers while your button is released. The web page does not use your phone's microphone.

## User

### Voice comes from the radio's headset

Mesh PTT uses an audio device and physical push-to-talk button attached to the radio. Enable mesh voice during setup. The web interface shows status and lets you choose a talk group; it is not a browser microphone.

The supported USB path uses a CM108-family audio device, including OpenVLM hardware. Headset wiring and microphone level matter: a military dynamic microphone needs suitable amplification before the CM108B input. Check the [audio hardware reference](../../../MANET/node_tools/README.md#audio-hardware) before connecting a headset.

### Select the same talk group

Open the management UI's Voice tab. Radios start on group 1 and can select groups 1 through 32. Set each participating radio to the group you want; changing your group does not move everybody else.

Hold the physical PTT button to transmit. Release it to stop transmitting. Receivers can mix simultaneous speakers, so two people talking at once are both heard.

### When there is no audio

Confirm voice is enabled, the mesh has a working path, the talk groups match, and the Voice tab reports a healthy audio path. Then check the audio hardware and microphone wiring.

The default codec is **Lyra**. Lyra and Opus do not interoperate. A radio that missed a mesh-wide codec change while powered off must be brought into the same codec setting; reissue the change with it online.

## Developer

### Pipeline and transport

`mesh-voice.py` owns GStreamer transmit and receive pipelines. The physical PTT controls the transmit valve. OpenVLM/CM108B input is read through USB HID, not an SBC GPIO. The web interface exposes status and configuration, not browser capture.

Voice defaults to Lyra using the packaged GStreamer elements and model weights. Missing Lyra artifacts can trigger an Opus fallback, which cannot communicate with a Lyra mesh. Treat codec and payload framing as compatibility requirements, not quality preferences.

Talk groups share multicast address `239.192.41.1`; group *n* uses RTP port `38801 + (n - 1) * 2`, with RTCP at the next port. The receive path demultiplexes SSRCs, gives talkers their own decode/jitter paths, and mixes them. Optional half-duplex behavior refuses local PTT while a remote source is transmitting.

### Multicast, scheduling and packet size

BATMAN's multicast optimization already fans out to unicast where applicable. Receivers must join the multicast group; additional userspace unicast redundancy is off by default. Voice uses DSCP CS6 (48) to reach the desired queue through BATMAN's priority mapping.

Lyra adapts frames per packet while keeping bitrate fixed. Under loss it reduces packet duration, subject to the low-throughput floor, to limit the speech lost with each packet. Buffer sizing and branch lifetime matter independently of codec speed. Do not describe estimated pipeline delay as measured mouth-to-ear latency.

### Configuration and failure handling

Talk-group changes are local: write `voice_channel` and reload through the checked retune path. Codec changes require mesh-wide staging and ACKs because encoder, payload and clock rate change together. An offline node does not automatically reconcile a missed codec change.

Transmit remains blocked until the socket is bound to the configured voice interface and its IPv4 address. A held PTT resumes only when the binding and duplex conditions permit it. A watchdog monitors actual audio flow so a pipeline reporting PLAYING cannot hide a stalled USB path indefinitely. `/run/mesh-voice.json` supplies the status readout.

- [Voice settings and operational behavior](../../../MANET/node_tools/README.md#push-to-talk-voice)
- [Pipeline, multicast, buffering and QoS internals](../../../docs/node-tools-internals.md#voice)
- [Voice daemon](../../../MANET/node_tools/mesh-voice.py)
