"""Boot-local, cross-process limits on unauthenticated scrypt work."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

# Unknown salts share eight initial derivations and regain one per second.
BURST = 8
REFILL_PER_SECOND = 1.0
BAD_MESSAGE_SECONDS = 60
MAX_BAD_MESSAGES = 256
MAX_AUTHENTICATED_SALTS = 256


class ReceiveBusy(ValueError):
    """Signal contention or exhaustion of the receive work budget."""
    pass


class ReceiveLimits:
    """Share derivation budgets and rejection caches across receivers."""
    def __init__(self, directory, clock=time.monotonic, lock_timeout=5):
        """Set the state directory, budget clock and derivation wait limit."""
        self.directory = Path(directory)
        self.clock = clock
        self.lock_timeout = lock_timeout

    @contextmanager
    def locked(self, name='.state-lock', timeout=0.5):
        """Hold a named file lock, failing after the timeout in seconds."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.directory / name, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'w') as lock:
            # Poll every 10 ms without tying the wait to wall-clock changes.
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as error:
                    if time.monotonic() >= deadline:
                        raise ReceiveBusy('Admin authentication is busy; retry later') from error
                    time.sleep(0.01)
            yield

    def read(self, name):
        """Read a state object, treating a missing file as empty."""
        try:
            data = json.loads((self.directory / name).read_text())
            if not isinstance(data, dict):
                raise ValueError('invalid receive-limit state')
            return data
        except FileNotFoundError:
            return {}

    def write(self, name, data):
        """Replace one receive-limit state file atomically."""
        # /run is tmpfs: these counters never write to the node's SD card.
        fd, temporary = tempfile.mkstemp(prefix='.limits-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as target:
                json.dump(data, target, separators=(',', ':'), allow_nan=False)
            os.replace(temporary, self.directory / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @contextmanager
    def derivation(self, salt_identity=''):
        """Reserve a serialized derivation and charge unknown salts."""
        # Serialize actual cache-miss KDFs across receivers as well as rate
        # limiting them: attacker-controlled salts cannot multiply peak RAM.
        with self.locked('.derivation-lock', self.lock_timeout):
            with self.locked():
                # Previously authenticated salts are bounded independently.
                # Short-lived receivers must not spend the untrusted-salt
                # budget repeatedly on healthy nodes. Keys remain memory-only.
                known = salt_identity and salt_identity in self.read('authenticated.json')
                if not known:
                    now = self.clock()
                    state = self.read('budget.json')
                    previous = float(state.get('when', now))
                    # Cap accumulated idle credit at the initial burst.
                    tokens = min(BURST, float(state.get('tokens', BURST))
                                 + max(0, now - previous) * REFILL_PER_SECOND)
                    if tokens < 1:
                        raise ReceiveBusy('Admin key-derivation limit reached; retry later')
                    self.write('budget.json', {'when': now, 'tokens': tokens - 1})
            yield

    def authenticate(self, salt_identity):
        """Remember a salt that passed authenticated payload validation."""
        with self.locked():
            now = self.clock()
            entries = self.read('authenticated.json')
            # Refresh known salts at most once a minute to limit state writes.
            if salt_identity in entries and now - entries[salt_identity] < 60:
                return
            entries[salt_identity] = now
            # Keep salt identities with the newest authentication timestamps.
            entries = dict(sorted(entries.items(), key=lambda pair: pair[1])[-MAX_AUTHENTICATED_SALTS:])
            self.write('authenticated.json', entries)

    def rejected(self, fingerprint):
        """Check whether an exact rejected envelope is still cached."""
        with self.locked():
            return self.read('bad.json').get(fingerprint, 0) > self.clock()

    def reject(self, fingerprint):
        """Cache a rejected envelope with a bounded expiry and entry count."""
        try:
            with self.locked():
                now = self.clock()
                entries = {k: v for k, v in self.read('bad.json').items() if v > now}
                entries[fingerprint] = now + BAD_MESSAGE_SECONDS
                # Keep the latest expiries when the rejection cache is full.
                entries = dict(sorted(entries.items(), key=lambda pair: pair[1])[-MAX_BAD_MESSAGES:])
                self.write('bad.json', entries)
        except ReceiveBusy:
            # Budgeting is still enforced if another receiver holds the lock.
            pass
