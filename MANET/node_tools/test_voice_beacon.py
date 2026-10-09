"""Beacon scheduling and interface binding without audio hardware."""
import importlib.util
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    "mesh_voice", Path(__file__).with_name("mesh-voice.py"))
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)


class Timers:
    Error = RuntimeError

    def __init__(self):
        self.sources = {}
        self.serial = 0
        self.MainLoop = Mock

    def timeout_add(self, delay, callback, *args):
        self.serial += 1
        self.sources[self.serial] = (delay, callback, args)
        return self.serial

    def timeout_add_seconds(self, delay, callback, *args):
        return self.timeout_add(delay * 1000, callback, *args)

    def source_remove(self, source):
        del self.sources[source]

    def fire(self, source):
        _, callback, args = self.sources[source]
        repeat = callback(*args)
        if not repeat:
            del self.sources[source]
        return repeat

    def matching(self, callback):
        return [s for s, (_, cb, _) in self.sources.items() if cb == callback]


class Element:
    def __init__(self, **props):
        self.props = props
        self.writes = []
        self.get_static_pad = Mock(return_value=Mock())
        self.connect = Mock()

    def set_property(self, name, value):
        self.props[name] = value
        self.writes.append((name, value))

    def get_property(self, name):
        return self.props.get(name)


class SendSocket(Element):
    def __init__(self, addr, device):
        super().__init__()
        self.addr = addr
        self.device = device

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def getsockname(self):
        return (self.addr, 40000)

    def getsockopt(self, *args):
        return self.device.encode() + b"\0"


class Pipeline:
    def __init__(self, desc):
        self.desc = desc
        self.elements = {n: Element() for n in re.findall(r"name=(\w+)", desc)}
        if "ptt" in self.elements:
            self.elements["ptt"].props["drop"] = True
        self.get_bus = Mock(return_value=Mock())

    def get_by_name(self, name):
        return self.elements.get(name)

    def set_state(self, state):
        sink = self.get_by_name("sink")
        if sink:
            sock = None
            if state == "PLAYING":
                addr = re.search(r"bind-address=([\d.]+)", self.desc)
                device = re.search(r"multicast-iface=(\w+)", self.desc)
                sock = SendSocket(addr[1] if addr else "0.0.0.0", device[1])
            sink.props["used-socket"] = sock
        return "SUCCESS"

    def get_state(self, timeout):
        return "SUCCESS", "PLAYING", None


class VoiceBeaconTests(unittest.TestCase):
    def setUp(self):
        self.timers = Timers()
        self.gst = SimpleNamespace(
            parse_launch=Mock(side_effect=Pipeline),
            State=SimpleNamespace(PLAYING="PLAYING", NULL="NULL"),
            StateChangeReturn=SimpleNamespace(FAILURE="FAILURE", ASYNC="ASYNC"),
            PadProbeType=SimpleNamespace(BUFFER=1), SECOND=1000000000)
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.state_path = Path(self.scratch.name) / "voice.json"
        self.ip = self.patch("iface_ipv4", return_value="10.0.0.1")
        self.registry = self.patch("read_registry", return_value=[])
        self.logs = self.patch("log")
        self.patch("GLib", self.timers, create=True)
        self.patch("Gst", self.gst, create=True)
        self.patch("Gio", SimpleNamespace(
            Socket=SimpleNamespace(get_fd=lambda sock: sock)), create=True)
        self.patch("local_ipv4_addresses", return_value={"10.0.0.1"})
        self.patch("sd_notify")
        self.patch("STATE_FILE", str(self.state_path))
        self.patch("read_kv_file", return_value={
            "voice_codec": "opus", "voice_ptt": "off",
            "voice_test_tone": "y", "voice_iface": "br0",
            "voice_highpass_hz": "0"})
        fd_patch = patch.object(voice.socket, "fromfd",
                                side_effect=lambda *a: a[0])
        fd_patch.start()
        self.addCleanup(fd_patch.stop)
        self.daemon = voice.MeshVoice(voice.Config())

    def patch(self, name, *args, **kwargs):
        patcher = patch.object(voice, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def start(self):
        self.daemon.build()
        self.daemon.start()

    def state(self):
        return json.loads(self.state_path.read_text())

    def test_startup_and_peer_beacons_are_coalesced_one_shots(self):
        self.registry.return_value = [("10.0.0.2", "peer")]
        self.start()
        daemon = self.daemon
        pending = self.timers.matching(daemon._beacon_once)
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.timers.sources[pending[0]][0], 1500)
        self.assertIs(self.timers.fire(pending[0]), False)
        self.assertEqual(self.timers.matching(daemon._beacon_once), [])
        self.assertFalse(daemon.valve.props["drop"])
        self.assertIs(self.timers.fire(daemon._beacon_end), False)
        self.assertTrue(daemon.valve.props["drop"])

        for n in (3, 4, 5):
            self.registry.return_value.append(("10.0.0.%d" % n, "peer"))
            daemon.refresh_peers()
        pending = self.timers.matching(daemon._beacon_once)
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.timers.sources[pending[0]][0], 500)
        self.assertIs(self.timers.fire(pending[0]), False)
        self.assertIs(self.timers.fire(daemon._beacon_end), False)
        daemon.refresh_peers()
        self.assertEqual(self.timers.matching(daemon._beacon_once), [])
        periodic = self.timers.matching(daemon._tick_beacon)
        self.assertEqual(len(periodic), 1)
        self.assertIs(self.timers.fire(periodic[0]), True)
        end = daemon._beacon_end
        self.assertIs(self.timers.fire(periodic[0]), True)
        self.assertEqual(daemon._beacon_end, end)

    def test_skipped_one_shot_is_removed_and_beacons_can_be_disabled(self):
        self.start()
        self.daemon.on_ptt(True)
        self.assertIs(self.timers.fire(self.daemon._beacon_pending), False)
        self.assertIsNone(self.daemon._beacon_end)
        self.daemon.stop()
        self.daemon.cfg.beacon_sec = 0
        self.timers.sources.clear()
        self.registry.return_value = [("10.0.0.2", "peer")]
        self.start()
        self.assertEqual(self.timers.matching(self.daemon._beacon_once), [])
        self.assertEqual(self.timers.matching(self.daemon._tick_beacon), [])

    def test_unbound_blocks_ptt_beacons_and_all_clients_and_logs_once(self):
        self.ip.return_value = None
        self.daemon.cfg.unicast = True
        self.registry.return_value = [("10.0.0.2", "peer")]
        self.start()
        daemon = self.daemon
        self.assertIn('clients=""', daemon.tx.desc)
        self.assertEqual(daemon.sink.props["clients"], "")
        self.assertIs(self.timers.fire(daemon._beacon_pending), False)
        self.assertIsNone(daemon._beacon_end)
        for _ in range(3):
            daemon.on_ptt(True)
        self.assertFalse(daemon.transmitting)
        self.assertNotIn(("drop", False), daemon.valve.writes)
        self.assertFalse(self.state()["tx"])
        self.assertIn("br0", self.state()["tx_blocked"])
        refused = [c for c in self.logs.call_args_list
                   if c.args[0].startswith("TX: blocked:")]
        self.assertEqual(len(refused), 1)
        daemon.on_ptt(False)
        with patch.object(daemon, "_retune") as retune:
            self.assertIs(daemon._tick_peers(), True)
        retune.assert_not_called()
        self.assertIs(daemon._tick_beacon(), True)
        self.assertNotIn(("drop", False), daemon.valve.writes)

    def test_address_arrival_rebinds_and_resumes_held_ptt(self):
        self.ip.return_value = None
        self.daemon.cfg.ptt_mode = "always"
        self.start()
        daemon = self.daemon
        old_tx = daemon.tx
        self.ip.return_value = "10.0.0.1"
        self.assertIs(daemon._tick_peers(), True)
        self.assertIsNot(daemon.tx, old_tx)
        self.assertIn("bind-address=10.0.0.1", daemon.tx.desc)
        self.assertIn("multicast-iface=br0", daemon.tx.desc)
        self.assertTrue(daemon.transmitting)
        self.assertFalse(daemon.valve.props["drop"])
        self.assertIsNone(self.state()["tx_blocked"])
        self.assertEqual(self.state()["bind_address"], "10.0.0.1")
        self.assertEqual(len(self.timers.matching(daemon._tick_beacon)), 1)
        self.assertEqual(len(self.timers.matching(daemon._beacon_once)), 1)
        with patch.object(daemon, "_retune") as retune:
            daemon._tick_peers()
        retune.assert_not_called()

    def test_address_loss_closes_beacon_and_recovery_announces(self):
        self.start()
        daemon = self.daemon
        self.timers.fire(daemon._beacon_pending)
        self.ip.return_value = None
        daemon._tick_peers()
        self.assertTrue(daemon.valve.props["drop"])
        self.assertEqual(daemon.sink.props["clients"], "")
        self.assertIsNotNone(self.state()["tx_blocked"])
        self.ip.return_value = "10.0.0.9"
        daemon._tick_peers()
        self.assertIn("bind-address=10.0.0.9", daemon.tx.desc)
        self.assertIsNone(daemon._beacon_end)
        self.timers.fire(daemon._beacon_pending)
        self.assertFalse(daemon.valve.props["drop"])

    def test_socket_verification_fails_closed(self):
        self.start()
        daemon = self.daemon
        for sock in (None, SendSocket("0.0.0.0", "br0"),
                     SendSocket("10.0.0.1", "end0"),
                     SendSocket("10.0.0.1", "")):
            with self.subTest(socket=sock):
                daemon.sink.props["used-socket"] = sock
                daemon.on_ptt(True)
                daemon.on_ptt(False)
                daemon._tick_beacon()
                self.assertTrue(daemon.valve.props["drop"])
                self.assertFalse(daemon.transmitting)
                self.assertIsNotNone(daemon._tx_blocked)
        with patch.object(voice.socket, "fromfd",
                          side_effect=OSError("closed")):
            daemon.sink.props["used-socket"] = SendSocket("10.0.0.1", "br0")
            daemon.on_ptt(True)
        self.assertFalse(daemon.transmitting)
        self.assertTrue(daemon.valve.props["drop"])

    def test_recovery_respects_release_and_half_duplex(self):
        self.ip.return_value = None
        self.start()
        daemon = self.daemon
        daemon.on_ptt(True)
        daemon.on_ptt(False)
        self.ip.return_value = "10.0.0.1"
        daemon._tick_peers()
        self.assertFalse(daemon.transmitting)

        self.ip.return_value = None
        daemon._tick_peers()
        daemon.on_ptt(True)
        daemon.cfg.half_duplex = True
        self.ip.return_value = "10.0.0.1"
        with patch.object(daemon, "_remote_active", return_value=True):
            daemon._tick_peers()
        self.assertFalse(daemon.transmitting)
        self.assertTrue(daemon.valve.props["drop"])

    def test_retune_cancels_pending_and_active_beacon_timers(self):
        self.start()
        daemon = self.daemon
        pending = daemon._beacon_pending
        self.assertTrue(daemon._retune(2, voice.talk_group_port(2)))
        self.assertNotIn(pending, self.timers.sources)
        daemon._tick_beacon()
        ending = daemon._beacon_end
        self.assertTrue(daemon._retune(3, voice.talk_group_port(3)))
        self.assertNotIn(ending, self.timers.sources)
        self.assertIsNone(daemon._beacon_end)
        self.assertTrue(daemon.valve.props["drop"])

    def test_reload_preserves_ptt_with_verified_binding(self):
        self.start()
        daemon = self.daemon
        daemon.on_ptt(True)
        new = voice.Config()
        new.channel = 2
        new.port = voice.talk_group_port(2)
        with patch.object(voice, "Config", return_value=new):
            daemon.reload()
        self.assertTrue(daemon.transmitting)
        self.assertFalse(daemon.valve.props["drop"])

    def test_reload_and_restart_cannot_bypass_binding(self):
        self.start()
        daemon = self.daemon
        daemon.on_ptt(True)
        self.ip.return_value = None
        new = voice.Config()
        new.channel = 2
        new.port = voice.talk_group_port(2)
        with patch.object(voice, "Config", return_value=new):
            daemon.reload()
        self.assertFalse(daemon.transmitting)
        self.assertTrue(daemon.valve.props["drop"])
        self.assertNotIn(("drop", False), daemon.valve.writes)
        daemon.transmitting = True
        daemon._restart("tx")
        self.assertFalse(daemon.transmitting)
        self.assertTrue(daemon.valve.props["drop"])

    def test_rebind_failure_keeps_transmit_closed(self):
        self.start()
        daemon = self.daemon
        daemon.on_ptt(True)
        old_valve = daemon.valve
        self.ip.return_value = "10.0.0.9"
        self.gst.parse_launch.side_effect = RuntimeError("build failed")
        self.assertIs(daemon._tick_peers(), False)
        self.assertTrue(old_valve.props["drop"])
        self.assertFalse(daemon.transmitting)
        self.assertIsNone(daemon._bind_ip)
        self.assertEqual(daemon.exit_code, 1)


if __name__ == "__main__":
    unittest.main()
