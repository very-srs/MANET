"""OpenVLM audio card selection without audio hardware."""
import unittest
from unittest.mock import patch

import test_voice_beacon as base


voice = base.voice


class VoiceAudioTests(unittest.TestCase):
    # Reuse the beacon tests' GLib/Gst fakes without re-running their tests.
    setUp = base.VoiceBeaconTests.setUp
    patch = base.VoiceBeaconTests.patch
    state = base.VoiceBeaconTests.state

    def build_without_card(self):
        """Start on the ALSA default, as a node booted before its OpenVLM."""
        self.daemon.cfg.test_tone = False
        with patch.object(voice, "find_openvlm_card", return_value=None):
            self.daemon.build()
            self.daemon.start()
        self.assertIn("alsasrc device=default", self.daemon.tx.desc)

    def test_card_plugged_in_after_start_moves_audio_onto_it(self):
        self.build_without_card()

        # Example: OpenVLM enumerates as card 2 hours after the daemon started.
        with patch.object(voice, "find_openvlm_card", return_value=2):
            self.daemon._tick_health()

        self.assertIn("alsasrc device=plughw:2", self.daemon.tx.desc)
        self.assertIn("alsasink name=play device=plughw:2", self.daemon.rx.desc)

    def test_unchanged_card_does_not_rebuild(self):
        self.daemon.cfg.test_tone = False
        with patch.object(voice, "find_openvlm_card", return_value=2):
            self.daemon.build()
            self.daemon.start()
            tx = self.daemon.tx
            self.daemon._tick_health()
        self.assertIs(tx, self.daemon.tx)

    def test_explicit_devices_are_never_replaced(self):
        self.daemon.cfg.test_tone = False
        self.daemon.cfg.alsa_in = self.daemon.cfg.alsa_out = "hw:5"
        with patch.object(voice, "find_openvlm_card", return_value=None):
            self.daemon.build()
            self.daemon.start()
        tx = self.daemon.tx
        with patch.object(voice, "find_openvlm_card", return_value=2):
            self.daemon._tick_health()
        self.assertIs(tx, self.daemon.tx)

    def test_held_ptt_survives_the_move(self):
        self.build_without_card()
        self.daemon.on_ptt(True)
        self.assertTrue(self.daemon.transmitting)

        with patch.object(voice, "find_openvlm_card", return_value=2):
            self.daemon._tick_health()

        self.assertTrue(self.daemon.transmitting)
        self.assertFalse(self.daemon.valve.props["drop"])

    def test_replug_on_same_index_is_a_new_card(self):
        self.daemon.cfg.test_tone = False
        with patch.object(voice, "openvlm_card_id", return_value=(2, "001/004")):
            self.daemon.build()
            self.daemon.start()
        tx = self.daemon.tx
        with patch.object(voice, "openvlm_card_id", return_value=(2, "001/005")):
            self.daemon._tick_health()
        self.assertIsNot(tx, self.daemon.tx)


class Amixer:
    """Record amixer calls; fail for controls the card does not have."""

    def __init__(self, controls=("PCM",)):
        self.controls = controls
        self.calls = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv)
        ok = argv[0] == "amixer" and argv[5] in self.controls
        return base.SimpleNamespace(returncode=0 if ok else 1, stdout="",
                                    stderr="")


class PlaybackLevelTests(unittest.TestCase):
    setUp = base.VoiceBeaconTests.setUp
    patch = base.VoiceBeaconTests.patch
    state = base.VoiceBeaconTests.state

    def build_on_card(self, amixer, card=(2, "001/004")):
        self.daemon.cfg.test_tone = False
        with patch.object(voice.subprocess, "run", side_effect=amixer), \
                patch.object(voice, "openvlm_card_id", return_value=card):
            self.daemon.build()
            self.daemon.start()
        self.daemon.write_state()

    def test_detected_card_is_set_to_full_volume_by_default(self):
        amixer = Amixer()
        self.build_on_card(amixer)
        self.assertIn(["amixer", "-q", "-c", "2", "sset", "PCM", "100%",
                       "unmute"], amixer.calls)
        self.assertEqual(self.state()["playback_level_pct"], 100)

    def test_level_is_set_after_the_device_opens(self):
        # Mixer writes just ahead of the PCM open made it fail as busy.
        order = []
        amixer = Amixer()
        self.daemon.cfg.test_tone = False
        real_play = self.daemon._play
        with patch.object(voice.subprocess, "run",
                          side_effect=lambda a, **k: (order.append("amixer"),
                                                      amixer(a, **k))[1]), \
                patch.object(self.daemon, "_play",
                             side_effect=lambda w: (order.append(w),
                                                    real_play(w))[1]), \
                patch.object(voice, "openvlm_card_id",
                             return_value=(2, "001/004")):
            self.daemon.build()
            self.assertEqual(order, [])
            self.daemon.start()
        self.assertEqual(order[:3], ["rx", "tx", "amixer"])

    def test_configured_level_is_used(self):
        self.daemon.cfg.playback_pct = 60
        amixer = Amixer()
        self.build_on_card(amixer)
        self.assertIn(["amixer", "-q", "-c", "2", "sset", "PCM", "60%",
                       "unmute"], amixer.calls)

    def test_generic_cm108_speaker_control_is_tried(self):
        amixer = Amixer(controls=("Speaker",))
        self.build_on_card(amixer)
        self.assertEqual([c[5] for c in amixer.calls if c[0] == "amixer"],
                         ["PCM", "Speaker"])
        self.assertEqual(self.state()["playback_level_pct"], 100)

    def test_no_usable_control_reports_unknown_level(self):
        amixer = Amixer(controls=())
        self.build_on_card(amixer)
        self.assertIsNone(self.state()["playback_level_pct"])

    def test_output_override_is_left_alone(self):
        self.daemon.cfg.alsa_out = "hw:5"
        amixer = Amixer()
        self.build_on_card(amixer)
        self.assertFalse([c for c in amixer.calls if c[0] == "amixer"])
        self.assertIsNone(self.state()["playback_level_pct"])

    def test_replug_restores_the_level(self):
        amixer = Amixer()
        self.build_on_card(amixer)
        amixer.calls.clear()
        with patch.object(voice.subprocess, "run", side_effect=amixer), \
                patch.object(voice, "openvlm_card_id",
                             return_value=(2, "001/005")):
            self.daemon._tick_health()
        self.assertIn(["amixer", "-q", "-c", "2", "sset", "PCM", "100%",
                       "unmute"], amixer.calls)


class Level:
    """A level element bus message: loudest channel RMS in dBFS."""

    def __init__(self, src, rms):
        self.type = "ELEMENT"
        self.src = base.SimpleNamespace(get_name=lambda: src)
        self._struct = base.SimpleNamespace(
            get_name=lambda: "level", get_value=lambda key: [rms])

    def get_structure(self):
        return self._struct


class ReceiveGainTests(unittest.TestCase):
    setUp = base.VoiceBeaconTests.setUp
    patch = base.VoiceBeaconTests.patch
    state = base.VoiceBeaconTests.state

    def branch_desc(self):
        self.daemon.build()
        bins = []
        self.gst.parse_bin_from_description = lambda desc, ghost: (
            bins.append(desc) or (_ for _ in ()).throw(base.voice.GLib.Error()))
        self.daemon._attach_branch(base.Mock(get_name=lambda: "recv_rtp_src_0"))
        return bins[0]

    def test_default_gain_is_applied_and_limited_per_talker(self):
        desc = self.branch_desc()
        gain = 10 ** (voice.RX_GAIN_DB_DEFAULT / 20.0)
        self.assertIn("audioamplify amplification=%.4f clipping-method=none"
                      % gain, desc)
        self.assertIn("audiodynamic mode=compressor characteristics=hard-knee",
                      desc)
        # Gain sits after the decoder and before the mixer's S16 caps.
        self.assertLess(desc.index("opusdec"), desc.index("audioamplify"))
        self.assertLess(desc.index("audiodynamic"), desc.index("S16LE"))

    def test_zero_gain_leaves_the_branch_untouched(self):
        self.daemon.cfg.rx_gain_db = 0.0
        desc = self.branch_desc()
        self.assertNotIn("audioamplify", desc)
        self.assertNotIn("audiodynamic", desc)

    def test_out_of_range_gain_falls_back_to_default(self):
        self.patch("read_kv_file", return_value={"voice_rx_gain_db": "40"})
        self.assertEqual(voice.Config().rx_gain_db, voice.RX_GAIN_DB_DEFAULT)

    def test_levels_report_loudest_window_then_reset(self):
        self.daemon.build()
        self.gst.MessageType = base.SimpleNamespace(ERROR="ERROR",
                                                    ELEMENT="ELEMENT")
        for src, rms in (("lvl", -31.0), ("lvl", -24.5), ("rxlvl", -19.25)):
            self.daemon._on_bus_message(None, Level(src, rms), "tx")
        self.daemon.write_state()
        self.assertEqual(self.state()["mic_db"], -24.5)
        self.assertEqual(self.state()["rx_db"], -19.2)
        self.daemon.write_state()
        self.assertIsNone(self.state()["mic_db"])
        self.assertIsNone(self.state()["rx_db"])

    def test_digital_silence_reports_the_floor(self):
        self.daemon.build()
        self.gst.MessageType = base.SimpleNamespace(ERROR="ERROR",
                                                    ELEMENT="ELEMENT")
        # The level element reports exact zeros as -700 dB.
        self.daemon._on_bus_message(None, Level("rxlvl", -700.0), "rx")
        self.daemon.write_state()
        self.assertEqual(self.state()["rx_db"], voice.LEVEL_FLOOR_DB)

    def test_playback_level_is_reasserted_on_the_peer_poll(self):
        amixer = Amixer()
        PlaybackLevelTests.build_on_card(self, amixer)
        amixer.calls.clear()
        with patch.object(voice.subprocess, "run", side_effect=amixer):
            self.daemon._tick_peers()
        self.assertIn(["amixer", "-q", "-c", "2", "sset", "PCM", "100%",
                       "unmute"], amixer.calls)


class PttBeepTests(unittest.TestCase):
    setUp = base.VoiceBeaconTests.setUp
    patch = base.VoiceBeaconTests.patch

    def start(self):
        self.daemon.build()
        self.daemon.start()
        return self.daemon.rx.get_by_name("beep")

    def play_out(self, beep):
        """Fire beep timers to the end; return the (freq, volume) steps heard."""
        heard = []
        while self.timers.matching(self.daemon._beep_next):
            heard.append((beep.props.get("freq"), beep.props.get("volume")))
            self.timers.fire(self.timers.matching(self.daemon._beep_next)[0])
        self.assertEqual(beep.props["volume"], 0.0)
        return [h for h in heard if h[1]]

    def test_beep_source_is_local_and_silent_at_rest(self):
        beep = self.start()
        self.assertIsNotNone(beep)
        self.assertIn("name=beep", self.daemon.rx.desc)
        self.assertNotIn("name=beep", self.daemon.tx.desc)
        self.assertEqual(beep.props["volume"], 0.0)

    def test_key_and_roger(self):
        beep = self.start()
        level = self.daemon.cfg.ptt_beep_pct / 100.0

        self.daemon._on_ptt_edge(True)
        self.assertTrue(self.daemon.transmitting)
        self.assertEqual(self.play_out(beep),
                         [(hz, level) for hz, _ in voice.PTT_BEEP_KEY if hz])

        self.daemon._on_ptt_edge(False)
        self.assertEqual(self.play_out(beep),
                         [(hz, level) for hz, _ in voice.PTT_BEEP_ROGER if hz])

    def test_refused_press_sounds_blocked_tone(self):
        beep = self.start()
        self.daemon.cfg.half_duplex = True
        self.daemon.rx_active = True
        self.daemon.last_rx_ms = voice.now_ms()

        self.daemon._on_ptt_edge(True)

        self.assertFalse(self.daemon.transmitting)
        self.assertEqual([hz for hz, _ in self.play_out(beep)],
                         [hz for hz, _ in voice.PTT_BEEP_BLOCKED if hz])

        # Releasing a press that never transmitted is not a roger.
        self.daemon._on_ptt_edge(False)
        self.assertEqual(self.play_out(beep), [])

    def test_zero_level_disables_the_beep(self):
        self.daemon.cfg.ptt_beep_pct = 0
        self.assertIsNone(self.start())
        self.daemon._on_ptt_edge(True)
        self.daemon._on_ptt_edge(False)
        self.assertFalse(self.timers.matching(self.daemon._beep_next))

    def test_retune_while_held_is_silent_and_cancels_a_beep(self):
        self.start()
        self.daemon._on_ptt_edge(True)
        self.assertTrue(self.timers.matching(self.daemon._beep_next))

        self.assertTrue(self.daemon._retune(2, voice.talk_group_port(2)))
        self.daemon._set_tx(True)

        self.assertFalse(self.timers.matching(self.daemon._beep_next))
        self.assertEqual(self.daemon.rx.get_by_name("beep").props["volume"],
                         0.0)


if __name__ == "__main__":
    unittest.main()
