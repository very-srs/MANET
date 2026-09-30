"""Boot-local, cross-process limits on unauthenticated scrypt work."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

BURST = 8
REFILL_PER_SECOND = 1.0
BAD_MESSAGE_SECONDS = 60
MAX_BAD_MESSAGES = 256
MAX_AUTHENTICATED_SALTS = 256


class ReceiveBusy(ValueError):
    pass


class ReceiveLimits:
    def __init__(self, directory, clock=time.monotonic, lock_timeout=5):
        self.directory = Path(directory)
        self.clock = clock
        self.lock_timeout = lock_timeout

    @contextmanager
    def locked(self, name='.state-lock', timeout=0.5):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.directory / name, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'w') as lock:
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
        try:
            data = json.loads((self.directory / name).read_text())
            if not isinstance(data, dict):
                raise ValueError('invalid receive-limit state')
            return data
        except FileNotFoundError:
            return {}

    def write(self, name, data):
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
                    tokens = min(BURST, float(state.get('tokens', BURST))
                                 + max(0, now - previous) * REFILL_PER_SECOND)
                    if tokens < 1:
                        raise ReceiveBusy('Admin key-derivation limit reached; retry later')
                    self.write('budget.json', {'when': now, 'tokens': tokens - 1})
            yield

    def authenticate(self, salt_identity):
        with self.locked():
            now = self.clock()
            entries = self.read('authenticated.json')
            if salt_identity in entries and now - entries[salt_identity] < 60:
                return
            entries[salt_identity] = now
            entries = dict(sorted(entries.items(), key=lambda pair: pair[1])[-MAX_AUTHENTICATED_SALTS:])
            self.write('authenticated.json', entries)

    def rejected(self, fingerprint):
        with self.locked():
            return self.read('bad.json').get(fingerprint, 0) > self.clock()

    def reject(self, fingerprint):
        try:
            with self.locked():
                now = self.clock()
                entries = {k: v for k, v in self.read('bad.json').items() if v > now}
                entries[fingerprint] = now + BAD_MESSAGE_SECONDS
                entries = dict(sorted(entries.items(), key=lambda pair: pair[1])[-MAX_BAD_MESSAGES:])
                self.write('bad.json', entries)
        except ReceiveBusy:
            # Budgeting is still enforced if another receiver holds the lock.
            pass
