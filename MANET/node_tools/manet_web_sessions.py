"""Local management sessions, cleared when the web process exits."""

import hashlib
import hmac
import re
import secrets
import threading
import time


SESSION_SECONDS = 48 * 60 * 60
MAX_SESSIONS = 64
TOKEN_PATTERN = re.compile(r'[A-Za-z0-9_-]{43}')


class SessionStore:
    def __init__(self, password_reader, lifetime=SESSION_SECONDS,
                 limit=MAX_SESSIONS, clock=time.monotonic):
        self.password_reader = password_reader
        self.lifetime = lifetime
        self.limit = limit
        self.clock = clock
        self._password_digest = None
        self._sessions = {}
        self._lock = threading.Lock()

    @staticmethod
    def _token_key(token):
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            return None
        return hashlib.sha256(token.encode('ascii')).digest()

    def _refresh(self):
        # Read credentials inside the lock, so concurrent requests cannot
        # restore sessions invalidated by a newer password observation.
        password = self.password_reader()
        digest = hashlib.sha256(password.encode('utf-8')).digest()
        if digest != self._password_digest:
            self._sessions.clear()
            self._password_digest = digest
        now = self.clock()
        self._sessions = {key: deadline for key, deadline in self._sessions.items()
                          if deadline > now}
        return password, now

    def login(self, password, previous_token=''):
        with self._lock:
            expected, now = self._refresh()
            if not expected or not isinstance(password, str):
                return None
            try:
                supplied = password.encode('utf-8')
            except UnicodeEncodeError:
                return None
            if not hmac.compare_digest(supplied, expected.encode('utf-8')):
                return None
            token = secrets.token_urlsafe(32)
            # A successful re-login replaces only this browser's session.
            self._sessions.pop(self._token_key(previous_token), None)
            if len(self._sessions) >= self.limit:
                del self._sessions[next(iter(self._sessions))]
            self._sessions[self._token_key(token)] = now + self.lifetime
            return token

    def valid(self, token):
        with self._lock:
            password, _ = self._refresh()
            return bool(password) and self._token_key(token) in self._sessions

    def logout(self, token):
        with self._lock:
            self._sessions.pop(self._token_key(token), None)
