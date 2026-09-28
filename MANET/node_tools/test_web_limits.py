"""Request limits, shared status work and overload recovery."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import io
import threading
import unittest
from unittest.mock import Mock, patch

import manet_web_limits as limits
from test_web_sessions import MemoryConnection, status
import test_web_sessions as sessions


class RequestLimitTests(unittest.TestCase):
    def request(self, headers, body=b''):
        raw = ('POST /api/perf-auth HTTP/1.0\r\nHost: localhost\r\n' + headers + '\r\n').encode() + body
        connection = MemoryConnection(raw)
        with patch.object(status, 'is_allowed_ip', return_value=True), \
                patch.object(status.WEB_SESSIONS, 'login', return_value=None):
            status.MeshHandler(connection, ('127.0.0.1', 1), Mock())
        response = http.client.HTTPResponse(MemoryConnection(bytes(connection.output)))
        response.begin()
        return response.status

    def test_invalid_lengths_and_transfer_encoding(self):
        for value in ('-1', 'lots', '1.0', '99999999999'):
            self.assertEqual(self.request(f'Content-Length: {value}\r\n'), 400)
        self.assertEqual(self.request('Content-Length: 2\r\nContent-Length: 2\r\n'), 400)
        self.assertEqual(self.request('Transfer-Encoding: chunked\r\n'), 400)

    def test_oversized_and_truncated_requests_never_reach_login(self):
        self.assertEqual(self.request('Content-Length: 65537\r\n'), 413)
        self.assertEqual(self.request('Content-Length: 10\r\n', b'{}'), 400)
        self.assertEqual(self.request('Content-Length: 2\r\n', b'{}'), 401)

    def test_slow_body_has_a_total_deadline(self):
        handler = object.__new__(status.MeshHandler)
        handler._body = None
        handler._body_length = 2
        handler.connection = Mock()
        handler.rfile = Mock()
        handler.rfile.read1.return_value = b'x'
        with patch.object(limits.time, 'monotonic', side_effect=[0, 1, 11]):
            with self.assertRaises(limits.InputError) as caught:
                handler.read_body()
        self.assertEqual(caught.exception.status, 408)
        handler.rfile.read1.assert_called_once()

    def test_connection_cap_releases_slots_after_handlers_fail(self):
        server = object.__new__(limits.BoundedHTTPServer)
        server.workers = threading.BoundedSemaphore(limits.MAX_CONNECTIONS)
        release = threading.Event()
        completed = threading.Event()
        counts = [0]
        lock = threading.Lock()
        def finish(*args):
            release.wait(2)
            raise RuntimeError('disconnected')
        def shutdown(*args):
            with lock:
                counts[0] += 1
                if counts[0] == limits.MAX_CONNECTIONS + 1:
                    completed.set()
        server.finish_request = finish
        server.shutdown_request = shutdown
        server.handle_error = Mock()
        for _ in range(limits.MAX_CONNECTIONS):
            server.process_request(Mock(), ('127.0.0.1', 1))
        extra = Mock()
        server.process_request(extra, ('127.0.0.1', 2))
        self.assertIn(b'503', extra.sendall.call_args.args[0])
        release.set()
        self.assertTrue(completed.wait(3))
        # All worker threads have left their finally blocks after joining.
        server.server_close = lambda: None
        for _ in range(limits.MAX_CONNECTIONS):
            self.assertTrue(server.workers.acquire(timeout=1))


class CacheTests(unittest.TestCase):
    def test_shared_result_expires_and_mutations_invalidate(self):
        clock = [1]
        cache = limits.StatusCache(clock=lambda: clock[0])
        collect = Mock(side_effect=[{'n': 1}, {'n': 2}, {'n': 3}])
        self.assertEqual(cache.get('status', collect), {'n': 1})
        self.assertEqual(cache.get('status', collect), {'n': 1})
        clock[0] = 6
        self.assertEqual(cache.get('status', collect), {'n': 2})
        cache.invalidate()
        self.assertEqual(cache.get('status', collect), {'n': 3})

    def test_concurrent_tabs_do_not_duplicate_collection(self):
        cache = limits.StatusCache()
        entered, release = threading.Event(), threading.Event()
        def collect():
            entered.set()
            release.wait(2)
            return {'ok': True}
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(cache.get, 'status', collect)
            self.assertTrue(entered.wait(1))
            with self.assertRaises(limits.Busy):
                cache.get('status', Mock(side_effect=AssertionError('duplicate')))
            release.set()
            self.assertEqual(first.result(), {'ok': True})

    def test_global_collection_limit_and_error_release(self):
        cache = limits.StatusCache()
        cache.collectors.acquire()
        cache.collectors.acquire()
        with self.assertRaises(limits.Busy):
            cache.get('status', Mock())
        cache.collectors.release()
        with self.assertRaises(ValueError):
            cache.get('status', Mock(side_effect=ValueError()))
        self.assertEqual(cache.get('status', lambda: 'recovered'), 'recovered')

    def test_invalidation_during_collection_does_not_cache_old_data(self):
        cache = limits.StatusCache()
        def old():
            cache.invalidate()
            return 'old'
        self.assertEqual(cache.get('status', old), 'old')
        self.assertEqual(cache.get('status', lambda: 'new'), 'new')


class ManagementLoadTests(unittest.TestCase):
    def setUp(self):
        self.client = sessions.WebSessionTests()
        self.client.setUp()
        self.addCleanup(self.client.doCleanups)
        _, self.token = self.client.login()

    def test_busy_change_returns_retry_and_status_remains_available(self):
        with limits.MANAGEMENT_WRITE:
            response = self.client.request('POST', '/manage/api/voice/channel', b'{}', self.token)
            self.assertEqual(response.status, 503)
            self.assertEqual(response.getheader('Retry-After'), '5')
            self.assertEqual(self.client.request('GET', '/manage/api/measure/status', cookie=self.token).status, 200)
        with patch('manet_manage.set_voice_channel', return_value={'ok': True}):
            response = self.client.request('POST', '/manage/api/voice/channel', b'{"channel":2}', self.token)
        self.assertEqual(response.status, 200)

    def test_measurement_limits_are_checked_before_starting_a_worker(self):
        import json
        base = {'label': 'audit', 'pairs': [{}], 'tests': ['tcp_1stream'], 'duration': 30}
        with patch('manet_manage._measure_status', {'running': False}), \
                patch('manet_manage.threading.Thread') as worker:
            for extra in ({'duration': 0}, {'duration': 301}, {'pairs': [{}] * 65},
                          {'tests': ['unknown']}, {'pairs': [{}] * 64, 'duration': 300}):
                body = json.dumps(dict(base, **extra)).encode()
                response = self.client.request('POST', '/manage/api/measure/start', body, self.token)
                self.assertEqual(response.status, 400)
            worker.assert_not_called()
            response = self.client.request('POST', '/manage/api/measure/start', json.dumps(base).encode(), self.token)
            self.assertEqual(response.status, 200)
            worker.return_value.start.assert_called_once()


if __name__ == '__main__':
    unittest.main()
