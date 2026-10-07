"""Real manager identity publication through the FIFO transport and encoder."""
import base64
from contextlib import ExitStack
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import NodeInfo_pb2
from manet_ids import bytes_to_syncthing_id, int_to_ipv4
import manet_runtime as runtime

TOOLS = Path(__file__).resolve().parent
ADDRESS = '10.30.2.146'
DEVICE_ID = bytes_to_syncthing_id(bytes(range(32)))
MANAGERS = ('node-manager-static.sh', 'node-manager-acs.sh')


class WorkerRestart(BaseException):
    """Simulate process death inside a request; serve's finally closes its FIFOs."""


class IdentityRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='manet-identity-runtime-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'runtime'
        self.run = self.root / 'run'
        self.run.mkdir()
        (self.run / 'my_ipv4_chunk').write_text('28\n')
        (self.run / 'my_ipv4_chunk_size').write_text('5\n')
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls = []
        self.threads = []
        self.stop_events = []
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.addCleanup(self.finish)
        self.patches.enter_context(patch.object(runtime, 'reconcile_ip', return_value=0))
        self.patches.enter_context(patch('manet_node_ipv4.current_ipv4', return_value=ADDRESS))
        self.patches.enter_context(patch.object(runtime.Helpers, 'syncthing_id', return_value=DEVICE_ID))

    def finish(self):
        self.release.set()
        for stop in self.stop_events:
            stop.set()
        for thread in self.threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def wait_ready(self):
        deadline = time.monotonic() + 2
        while not (self.directory / 'ready').exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.005)

    def start(self, mode='healthy'):
        parent = self
        class Helpers(runtime.Helpers):
            def dispatch(self, action, argument='-'):
                parent.calls.append(action)
                if action == 'ipv4':
                    if mode == 'slow':
                        parent.entered.set()
                        if not parent.release.wait(2):
                            raise RuntimeError('test did not release the slow request')
                    if mode == 'restart':
                        raise WorkerRestart()
                    if mode == 'empty':
                        return 0, ''
                if action == 'syncthing' and mode == 'syncthing-error':
                    return 1, ''
                return super().dispatch(action, argument)
        stop = threading.Event()
        self.stop_events.append(stop)
        def serve():
            try:
                runtime.serve(self.directory, Helpers(), stop)
            except WorkerRestart:
                pass
        thread = threading.Thread(target=serve)
        self.threads.append(thread)
        thread.start()
        self.wait_ready()
        return thread

    def publish(self, manager, *, ip_first=False, fallback=ADDRESS, chunk='28',
                last_publish='100', last_allocation='previous'):
        marker = self.run / 'my_ipv4_chunk'
        if chunk is None:
            marker.unlink(missing_ok=True)
        else:
            marker.write_text(chunk + '\n')
        payloads = self.root / 'payloads'
        payloads.unlink(missing_ok=True)
        source = (TOOLS / manager).read_text().split('# === PUBLISH IDENTITY (Alfred type 67) ===', 1)[1]
        boundary = ('# === CHECK STATE: LOBBY OR DATA ===' if manager.endswith('-acs.sh')
                    else '# === PUBLISH TELEMETRY (Alfred type 68) ===')
        source = source.split(boundary, 1)[0]
        source = source.replace('/var/run/', str(self.run) + '/')
        source = source.replace('/sys/class/net/', str(self.root / 'net') + '/')
        body = f'. "{TOOLS}/manet-common.sh"\n' + r'''
log() { printf '%s\n' "$*" >&2; }
hostname() { printf 'mesh-5cc2\n'; }
python3() {
    case "$1" in
        */manet_node_ipv4.py)
            printf 'ipv4\n' >> "$TEST_ROOT/fallbacks"
            printf '%s\n' "$TEST_ADDRESS" ;;
        */manet-syncthing-id.py)
            printf 'syncthing\n' >> "$TEST_ROOT/fallbacks"
            printf '%s\n' "$TEST_DEVICE_ID" ;;
        *) return 99 ;;
    esac
}
alfred() {
    [ "$1 $2" = '-s 67' ] || return 99
    local payload
    IFS= read -r payload || true
    printf '%s\n' "$payload" >> "$TEST_ROOT/payloads"
}
'''
        if ip_first:
            body += 'manet_runtime_call ip || exit $?\n'
        body += source + '\nprintf "%s\\n%s\\n" "$LAST_IDENTITY_PUBLISH" "$LAST_IDENTITY_ALLOCATION" > "$TEST_ROOT/timers"\n'
        env = dict(os.environ, TEST_ROOT=str(self.root), TEST_ADDRESS=fallback,
                   TEST_DEVICE_ID=DEVICE_ID, MANET_RUNTIME_DIR=str(self.directory),
                   MANET_RUNTIME_WAIT='.05', MANET_TOOLS_DIR=str(TOOLS),
                   ENCODER_PATH=str(TOOLS / 'encoder.py'), MY_MAC='02:00:00:00:00:01',
                   CONTROL_IFACE='br0', ALFRED_IDENTITY_TYPE='67', MONO='5000',
                   IDENTITY_PUBLISH_INTERVAL='270', LAST_IDENTITY_PUBLISH=last_publish,
                   LAST_IDENTITY_ALLOCATION=last_allocation,
                   PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
        result = subprocess.run(['bash', '-c', body], capture_output=True, text=True, env=env, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr)
        timers = (self.root / 'timers').read_text().splitlines()
        records = []
        for payload in payloads.read_text().splitlines() if payloads.exists() else []:
            identity = NodeInfo_pb2.NodeIdentity()
            identity.ParseFromString(base64.b64decode(payload))
            records.append(identity)
        return records, timers, result.stderr

    def assert_published(self, result, chunk=28):
        records, timers, _ = result
        self.assertEqual(len(records), 1, result[2])
        self.assertEqual(int_to_ipv4(records[0].ipv4_address), ADDRESS)
        self.assertEqual(records[0].ipv4_chunk, chunk)
        self.assertEqual(records[0].ipv4_chunk_size, 5)
        self.assertEqual(records[0].hostname, 'mesh-5cc2')
        self.assertEqual(records[0].syncthing_id, bytes(range(32)))
        self.assertEqual(timers, ['5000', f'{chunk}:{ADDRESS}'])

    def assert_deferred(self, result):
        records, timers, error = result
        self.assertEqual(records, [])
        self.assertEqual(timers, ['100', 'previous'])
        self.assertIn('identity publish deferred', error)

    def test_ip_preflight_then_identity_uses_worker_without_any_fallback(self):
        self.start()
        for manager in MANAGERS:
            with self.subTest(manager=manager):
                self.assert_published(self.publish(manager, ip_first=True))
        self.assertEqual(self.calls, ['ip', 'ipv4', 'syncthing'] * 2)
        self.assertFalse((self.root / 'fallbacks').exists())

    def test_busy_worker_falls_back_and_publishes_all_identity_fields(self):
        self.start('slow')
        # Another request is executing; ready is withdrawn and its client owns
        # the slot. Identity uses the standalone selectors without queuing.
        command = f'. "{TOOLS}/manet-runtime-client.sh"; manet_runtime_call ipv4'
        with subprocess.Popen(['bash', '-c', command], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, MANET_RUNTIME_DIR=str(self.directory),
                                       MANET_RUNTIME_WAIT='2')) as busy:
            self.assertTrue(self.entered.wait(2))
            try:
                for manager in MANAGERS:
                    with self.subTest(manager=manager):
                        self.assert_published(self.publish(manager))
            finally:
                self.release.set()
                busy.communicate(timeout=3)
        self.assertEqual(self.calls, ['ipv4'])

    def test_client_slot_contention_also_falls_back_before_submission(self):
        self.start()
        with (self.directory / 'client.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for manager in MANAGERS:
                with self.subTest(manager=manager):
                    self.assert_published(self.publish(manager))
        self.assertEqual(self.calls, [])

    def test_absent_worker_falls_back_and_chunk_zero_remains_allocated(self):
        for manager in MANAGERS:
            for chunk in (0, 28):
                with self.subTest(manager=manager, chunk=chunk):
                    self.assert_published(self.publish(manager, chunk=str(chunk)), chunk=chunk)

    def test_slow_worker_defers_without_losing_retry_then_publishes(self):
        self.start('slow')
        for manager in MANAGERS:
            with self.subTest(manager=manager):
                self.release.clear()
                self.assert_deferred(self.publish(manager))
                self.assertTrue(self.entered.is_set())
                self.assertFalse((self.root / 'fallbacks').exists())
                self.release.set()
                self.wait_ready()
                self.assert_published(self.publish(manager))

    def test_worker_dying_during_reply_defers_then_restart_publishes(self):
        for manager in MANAGERS:
            with self.subTest(manager=manager):
                dying = self.start('restart')
                self.assert_deferred(self.publish(manager))
                dying.join(2)
                self.assertFalse(dying.is_alive())
                self.assertFalse((self.root / 'fallbacks').exists())
                healthy = self.start()
                self.assert_published(self.publish(manager))
                self.stop_events[-1].set()
                healthy.join(2)

    def test_empty_success_or_empty_standalone_address_never_publishes_a_claim(self):
        worker = self.start('empty')
        for manager in MANAGERS:
            with self.subTest(manager=manager, path='worker'):
                self.assert_deferred(self.publish(manager))
        self.stop_events[-1].set()
        worker.join(2)
        for manager in MANAGERS:
            with self.subTest(manager=manager, path='standalone'):
                self.assert_deferred(self.publish(manager, fallback=''))

    def test_syncthing_transport_failure_defers_instead_of_erasing_identity(self):
        self.start('syncthing-error')
        for manager in MANAGERS:
            with self.subTest(manager=manager):
                self.assert_deferred(self.publish(manager))

    def test_discovery_without_a_chunk_still_advertises_before_ipv4_exists(self):
        for manager in MANAGERS:
            with self.subTest(manager=manager):
                records, timers, _ = self.publish(manager, chunk=None, fallback='')
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].ipv4_address, 0)
                self.assertEqual(timers, ['5000', ':'])
        self.assertNotIn('ipv4', (self.root / 'fallbacks').read_text())


if __name__ == '__main__':
    unittest.main()
