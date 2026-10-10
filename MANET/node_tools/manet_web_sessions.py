"""Local management sessions, cleared when the web process exits."""

import hashlib
import hmac
import math
import re
import secrets
import threading
import time


# Fixed lifetimes use a monotonic clock, unaffected by GPS/NTP clock steps.
SESSION_SECONDS = 48 * 60 * 60
MAX_SESSIONS = 64
# A 32-byte URL-safe token has 43 unpadded base64 characters.
TOKEN_PATTERN = re.compile(r'[A-Za-z0-9_-]{43}')
LOGIN_WINDOW = 60
LOGIN_CLIENT_FAILURES = 5
LOGIN_GLOBAL_FAILURES = 30


class LoginThrottled(ValueError):
    """Carry the retry delay when a login failure budget is exhausted."""
    def __init__(self, retry_after):
        """Round the retry delay up to at least one second."""
        self.retry_after = max(1, math.ceil(retry_after))
        super().__init__('Too many login attempts; try again shortly')


class SessionStore:
    """Keep expiring sessions and login failure budgets under one lock."""
    def __init__(self, password_reader, lifetime=SESSION_SECONDS,
                 limit=MAX_SESSIONS, clock=time.monotonic):
        """Set session limits and the clock used for fixed deadlines."""
        self.password_reader = password_reader
        self.lifetime = lifetime
        self.limit = limit
        self.clock = clock
        self._password_digest = None
        self._sessions = {}
        self._login_failures = []
        self._lock = threading.Lock()

    @staticmethod
    def _token_key(token):
        """Validate token syntax and hash it for in-memory lookup."""
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            return None
        return hashlib.sha256(token.encode('ascii')).digest()

    def _refresh(self):
        """Invalidate changed credentials and prune expiry under the lock."""
        # Read credentials inside the lock, so concurrent requests cannot
        # restore sessions invalidated by a newer password observation.
        password = self.password_reader()
        digest = hashlib.sha256(password.encode('utf-8')).digest()
        if digest != self._password_digest:
            self._sessions.clear()
            self._login_failures.clear()
            self._password_digest = digest
        now = self.clock()
        self._sessions = {key: deadline for key, deadline in self._sessions.items()
                          if deadline > now}
        return password, now

    def login(self, password, previous_token='', client='local'):
        """Check throttling and credentials, then issue a browser session."""
        with self._lock:
            expected, now = self._refresh()
            # All HTTP aliases share this budget. Bound memory by the global
            # failure limit so arbitrary client addresses cannot grow a map.
            self._login_failures = [(when, ip) for when, ip in self._login_failures
                                    if when + LOGIN_WINDOW > now]
            own = [when for when, ip in self._login_failures if ip == client]
            deadlines = []
            if len(own) >= LOGIN_CLIENT_FAILURES:
                deadlines.append(own[-LOGIN_CLIENT_FAILURES] + LOGIN_WINDOW)
            if len(self._login_failures) >= LOGIN_GLOBAL_FAILURES:
                deadlines.append(self._login_failures[-LOGIN_GLOBAL_FAILURES][0] + LOGIN_WINDOW)
            # Retry only when both the client and node budgets permit it.
            if deadlines:
                raise LoginThrottled(max(deadlines) - now)
            try:
                # An unset credential disables login, including empty input.
                matches = (bool(expected) and isinstance(password, str)
                           and hmac.compare_digest(password.encode('utf-8'), expected.encode('utf-8')))
            except UnicodeEncodeError:
                matches = False
            if not matches:
                self._login_failures.append((now, client))
                return None
            token = secrets.token_urlsafe(32)
            # A successful re-login replaces only this browser's session.
            self._sessions.pop(self._token_key(previous_token), None)
            # Dict insertion order evicts the oldest retained login at capacity.
            if len(self._sessions) >= self.limit:
                del self._sessions[next(iter(self._sessions))]
            self._sessions[self._token_key(token)] = now + self.lifetime
            return token

    def valid(self, token):
        """Check a token after refreshing credentials and session expiry."""
        with self._lock:
            password, _ = self._refresh()
            return bool(password) and self._token_key(token) in self._sessions

    def logout(self, token):
        """Remove only the session identified by this token."""
        with self._lock:
            self._sessions.pop(self._token_key(token), None)
