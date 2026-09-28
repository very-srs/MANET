"""Bound the work a local browser can ask of the radio."""

from http.server import HTTPServer
import re
from socketserver import ThreadingMixIn
import threading
import time


MAX_CONNECTIONS = 8
MAX_BODY_BYTES = 64 * 1024
READ_SECONDS = 10


class InputError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class RequestLimits:
    def parse_request(self):
        if not super().parse_request():
            return False
        self._body = None
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding'):
            raise InputError(400, 'Transfer-Encoding is not supported')
        if len(lengths) > 1 or (lengths and not re.fullmatch(r'[0-9]{1,10}', lengths[0])):
            raise InputError(400, 'Invalid Content-Length')
        self._body_length = int(lengths[0]) if lengths else 0
        if self._body_length > MAX_BODY_BYTES:
            raise InputError(413, 'Request exceeds 64 KiB')
        return True

    def read_body(self):
        if self._body is not None:
            return self._body
        deadline = time.monotonic() + READ_SECONDS
        remaining = self._body_length
        chunks = []
        try:
            while remaining:
                seconds = deadline - time.monotonic()
                if seconds <= 0:
                    raise TimeoutError()
                self.connection.settimeout(seconds)
                chunk = self.rfile.read1(min(remaining, 8192))
                if not chunk:
                    raise InputError(400, 'Incomplete request body')
                chunks.append(chunk)
                remaining -= len(chunk)
        except TimeoutError:
            raise InputError(408, 'Request body timed out') from None
        finally:
            self.connection.settimeout(READ_SECONDS)
        self._body = b''.join(chunks)
        return self._body

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except InputError as exc:
            self.close_connection = True
            self.send_error(exc.status, str(exc))


class BoundedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, *args, **kwargs):
        self.workers = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.workers.acquire(blocking=False):
            try:
                request.settimeout(0.1)
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\n'
                                b'Retry-After: 5\r\nConnection: close\r\n'
                                b'Cache-Control: no-store\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            request.settimeout(READ_SECONDS)
            super().process_request(request, client_address)
        except BaseException:
            self.workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.workers.release()


class Busy(Exception):
    pass


class StatusCache:
    """Share a small fixed set of status collections across browser tabs."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.entries = {}
        self.collectors = threading.BoundedSemaphore(2)
        self.generation = 0

    def invalidate(self):
        with self.lock:
            self.generation += 1
            for entry in self.entries.values():
                entry['until'] = 0

    def get(self, key, collect, ttl=5):
        with self.lock:
            entry = self.entries.setdefault(key, {'until': 0, 'lock': threading.Lock()})
        if not entry['lock'].acquire(timeout=0.2):
            raise Busy('Status collection is still running; retry shortly')
        try:
            with self.lock:
                if self.clock() < entry['until']:
                    return entry['value']
                generation = self.generation
            if not self.collectors.acquire(blocking=False):
                raise Busy('Radio is busy collecting status; retry shortly')
            try:
                value = collect()
            finally:
                self.collectors.release()
            with self.lock:
                if self.generation == generation:
                    entry.update(value=value, until=self.clock() + ttl)
            return value
        finally:
            entry['lock'].release()


STATUS_CACHE = StatusCache()
MANAGEMENT_WRITE = threading.Lock()
