"""Receive gain, limiter, and playback level without audio hardware."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import manet_manage as manage
import test_voice_beacon as beacon

voice = beacon.voice


class Bin:
    """Talker decode bin holding the named gain and limiter elements."""

    def __init__(self):
        self.elements = {"rxgain": beacon.Element(),
                         "rxlimit": beacon.Element()}

    def get_by_name(self, name):
        return self.elements.get(name)


class ReceiveGainTests(beacon.VoiceBeaconTests):
    """Reuse the beacon fixture; its own tests are not rerun here."""

    def setUp(self):
        super().setUp()
        self.conf = dict(voice.read_kv_file.return_value)
        voice.read_kv_file.return_value = self.conf
        self.patch("find_openvlm_card", return_value=1)
        self.mixer = self.patch("set_playback_level", return_value="PCM")
        self.daemon = voice.MeshVoice(voice.Config())
        self.start()

    def attach(self):
        branch = Bin()
        self.daemon.rx_branches["talker"] = (Mock(), branch, Mock(), 1)
        return branch

    def test_zero_db_passes_through_and_boost_limits(self):
        self.assertEqual(self.daemon._rx_gain_params(), (1.0, 1.0))
        self.assertIn("ratio=1.00", self.daemon._rx_gain_stage())
        self.daemon.cfg.rx_gain_db = 6.0
        amp, ratio = self.daemon._rx_gain_params()
        self.assertAlmostEqual(amp, 1.9953, places=4)
        # audiodynamic expands above threshold when ratio > 1.
        self.assertLess(ratio, 1.0)

    def test_live_branches_follow_gain_and_parked_ones_are_untouched(self):
        branch = self.attach()
        parked = Mock(spec=[])
        self.daemon.rx_parked["quiet"] = (Mock(), parked, 2)
        self.daemon.cfg.rx_gain_db = 12.0
        self.daemon._update_rx_gain()
        self.assertAlmostEqual(
            branch.elements["rxgain"].props["amplification"], 3.981, places=3)
        self.assertEqual(branch.elements["rxlimit"].props["ratio"],
                         voice.RX_LIMIT_RATIO)

    def test_in_place_reload_applies_both_levels(self):
        branch = self.attach()
        self.mixer.reset_mock()
        self.conf.update(voice_rx_gain_db="18", voice_playback_pct="60")
        self.daemon.reload()
        self.assertAlmostEqual(
            branch.elements["rxgain"].props["amplification"], 7.943, places=3)
        self.mixer.assert_called_once_with(1, 60)
        self.assertEqual(self.state()["rx_gain_db"], 18.0)
        self.assertEqual(self.state()["playback_level"], 60)

    def test_talk_group_reload_keeps_level_changes(self):
        self.mixer.reset_mock()
        self.conf.update(voice_channel="3", voice_rx_gain_db="6",
                         voice_playback_pct="40")
        self.daemon.reload()
        self.assertEqual(self.daemon.cfg.channel, 3)
        self.assertEqual(self.daemon.cfg.rx_gain_db, 6.0)
        self.assertIn(call(1, 40), self.mixer.call_args_list)

    def test_rebuild_writes_mixer_after_playback_reopens(self):
        # A mixer write just before the playback open can fail it as busy.
        order = []
        play = self.daemon._play

        def record_play(which):
            order.append("play " + which)
            return play(which)

        self.mixer.side_effect = lambda card, pct: order.append("mixer") or "PCM"
        self.conf.update(voice_channel="3", voice_playback_pct="40")
        with patch.object(self.daemon, "_play", side_effect=record_play):
            self.daemon.reload()
        self.assertEqual(order, ["play rx", "play tx", "mixer"])

    def test_unconfigured_level_leaves_the_card_alone(self):
        # setUp started the daemon with no voice_playback_pct in mesh.conf.
        self.assertIsNone(self.daemon.cfg.playback_pct)
        self.mixer.assert_not_called()
        self.conf.update(voice_rx_gain_db="6")
        self.daemon.reload()
        self.mixer.assert_not_called()
        self.assertIsNone(self.state()["playback_level"])

    def test_override_device_and_mixer_failure_leave_level_unset(self):
        self.daemon.cfg.playback_pct = 50
        self.mixer.return_value = None
        self.daemon._set_playback_level()
        self.assertIsNone(self.daemon.playback_level)
        self.mixer.return_value = "PCM"
        self.daemon.cfg.alsa_out = "hw:2"
        self.mixer.reset_mock()
        self.daemon._set_playback_level()
        self.mixer.assert_not_called()
        self.assertIsNone(self.daemon.playback_level)


# The fixture's tests belong to test_voice_beacon.
for _name in dir(beacon.VoiceBeaconTests):
    if _name.startswith("test_"):
        setattr(ReceiveGainTests, _name, None)


class SetVoiceAudioTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.conf = Path(scratch.name) / "mesh.conf"
        self.conf.write_text("mesh_key=secret\nvoice_rx_gain_db=3\n"
                             "voice_rx_gain_db=9\nvoice_channel=2")
        conf = patch.object(manage, "MESH_CONF_FILE", str(self.conf))
        conf.start()
        self.addCleanup(conf.stop)
        run = patch.object(manage.subprocess, "run",
                           return_value=SimpleNamespace(returncode=0))
        self.run = run.start()
        self.addCleanup(run.stop)

    def test_rewrites_only_level_keys_and_reloads(self):
        result = manage.set_voice_audio({"rx_gain_db": 12,
                                         "playback_pct": 80})
        self.assertEqual(result, {"ok": True, "reloaded": True})
        self.assertEqual(self.conf.read_text(),
                         "mesh_key=secret\nvoice_rx_gain_db=12.0\n"
                         "voice_channel=2\nvoice_playback_pct=80\n")
        self.run.assert_called_once()

    def test_rejects_bad_values_without_writing(self):
        before = self.conf.read_text()
        for req in ({"rx_gain_db": 25}, {"rx_gain_db": -1},
                    {"rx_gain_db": "nan"}, {"rx_gain_db": None},
                    {"playback_pct": 101}, {"playback_pct": "x"}, {}):
            with self.subTest(req=req):
                self.assertFalse(manage.set_voice_audio(req)["ok"])
        self.assertEqual(self.conf.read_text(), before)
        self.run.assert_not_called()

    def test_failed_reload_still_saves(self):
        self.run.return_value = SimpleNamespace(returncode=1)
        result = manage.set_voice_audio({"playback_pct": 50})
        self.assertEqual(result, {"ok": True, "reloaded": False})
        self.assertIn("voice_playback_pct=50\n", self.conf.read_text())


@unittest.skipIf(voice.GST_IMPORT_ERROR is not None,
                 'GI/GStreamer unavailable in this Python')
class LimiterGStreamerTests(unittest.TestCase):
    """Measure the real gain fragment on a 1 kHz tone at 16 kHz."""

    def setUp(self):
        self.Gst = voice.Gst
        self.Gst.init(None)
        for name in ("audiotestsrc", "audioamplify", "audiodynamic", "level"):
            if not self.Gst.ElementFactory.find(name):
                self.skipTest("missing GStreamer element " + name)
        with patch.object(voice, "read_kv_file", return_value={}):
            self.daemon = voice.MeshVoice(voice.Config())

    def peak(self, in_dbfs, gain_db):
        """Return the peak dBFS after the fragment at gain_db."""
        self.daemon.cfg.rx_gain_db = gain_db
        pipeline = self.Gst.parse_launch(
            "audiotestsrc wave=sine freq=1000 volume=%f num-buffers=40 ! "
            "audio/x-raw,format=S16LE,rate=16000,channels=1 ! %s"
            "audioconvert ! audio/x-raw,format=S16LE ! "
            "level interval=100000000 ! fakesink"
            % (10 ** (in_dbfs / 20.0), self.daemon._rx_gain_stage()))
        bus = pipeline.get_bus()
        pipeline.set_state(self.Gst.State.PLAYING)
        best = -200.0
        try:
            while True:
                msg = bus.timed_pop_filtered(
                    5 * self.Gst.SECOND,
                    self.Gst.MessageType.ELEMENT | self.Gst.MessageType.EOS)
                if msg is None or msg.type == self.Gst.MessageType.EOS:
                    break
                best = max(best, msg.get_structure().get_value("peak")[0])
        finally:
            pipeline.set_state(self.Gst.State.NULL)
        return best

    def test_measured_table(self):
        # Matches the table above RX_GAIN_DB_DEFAULT in mesh-voice.py.
        self.assertAlmostEqual(self.peak(-3, 0), -3.0, delta=0.2)
        self.assertAlmostEqual(self.peak(-30, 12), -18.0, delta=0.2)
        self.assertAlmostEqual(self.peak(-3, 12), -4.2, delta=0.2)
        self.assertAlmostEqual(self.peak(-3, 18), -2.4, delta=0.2)


if __name__ == "__main__":
    unittest.main()
