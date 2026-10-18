"""Private, node-local Wi-Fi credentials shared by built-in uplink radios."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re

from manet_config_io import atomic_write

SAVED_MISSING = 'Saved password is unavailable. Enter the password and try again.'
MAX_NETWORKS = 32


def network_key(value):
    """Use exact UTF-8 SSID bytes and auth, never a BSSID or normalized name."""
    ssid, security = value.get('ssid'), value.get('security')
    if (not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32
            or any(ord(c) < 32 or ord(c) == 127 for c in ssid)
            or security not in ('wpa2', 'sae')):
        raise ValueError('Choose a saved secured network')
    return ssid.encode(), security


class SavedNetworks:
    """Serialize migration, success, lookup and forget across local helpers.

    Entries run from least to most recently used. Completed attempt IDs prevent
    a watcher, reconnect or reboot from resurrecting a forgotten/evicted entry.
    Only explicit connection requests create a new attempt ID.
    """
    def __init__(self):
        self.builtin = Path(os.environ.get('MANET_WIFI_UPLINK_CONFIG',
                                          '/etc/manet/wifi-uplink.json'))
        self.path = Path(os.environ.get('MANET_WIFI_UPLINK_SAVED_CONFIG',
                                       str(self.builtin.with_name('wifi-uplink-saved.json'))))
        self.lock = self.path.with_suffix('.lock')

    def profile(self):
        """Read the private built-in client profile."""
        try:
            value = json.loads(self.builtin.read_text())
            return value if isinstance(value, dict) else {}
        except FileNotFoundError:
            return {}
        except (ValueError, UnicodeError) as error:
            raise ValueError('Could not read the local Wi-Fi profile') from error

    @contextmanager
    def transaction(self):
        """Keep private updates atomic and serialize local helpers."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'a') as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield self.load()

    def write(self, data):
        """Persist secrets in a same-directory 0600 replacement with fsync."""
        atomic_write(self.path, json.dumps(data, sort_keys=True) + '\n', mode=0o600)

    def load(self):
        """Migrate legacy profiles once, excluding all new unverified attempts."""
        try:
            data = json.loads(self.path.read_text())
            if (data.get('version') != 1 or not isinstance(data.get('entries'), list)
                    or not isinstance(data.get('completed'), dict)):
                raise ValueError()
            return data
        except FileNotFoundError:
            data = {'version': 1, 'entries': [], 'completed': {}}
            profile = self.profile()
            if profile and 'save_id' not in profile:
                self.put(data, profile)
            self.write(data)
            return data
        except (ValueError, AttributeError) as error:
            # Never reflect corrupt credential file contents to a caller.
            raise ValueError('Could not read saved Wi-Fi networks') from error

    def put(self, data, profile):
        """Insert validated credentials and evict the oldest unused entry."""
        try:
            key = network_key(profile)
        except ValueError:
            return
        password = profile.get('password')
        if (not isinstance(password, str) or not 8 <= len(password.encode()) <= 63
                or any(c in password for c in '\r\n\0')):
            return
        data['entries'] = [row for row in data['entries'] if network_key(row) != key]
        data['entries'].append({k: profile[k] for k in ('ssid', 'security', 'password')})
        if len(data['entries']) > MAX_NETWORKS:
            protected = {key}
            current = self.profile()
            if current.get('enabled'):
                try:
                    protected.add(network_key(current))
                except ValueError:
                    pass
            while len(data['entries']) > MAX_NETWORKS:
                oldest = next(i for i, row in enumerate(data['entries'])
                              if network_key(row) not in protected)
                data['entries'].pop(oldest)

    def public(self):
        """Return names and security only, even when internal records grow."""
        if not any(p.exists() for p in (self.path, self.builtin)):
            return []
        with self.transaction() as data:
            return [{k: row[k] for k in ('ssid', 'security')} for row in data['entries']]

    def annotate(self, rows):
        """Attach only a boolean to observations, including cached scans."""
        keys = {network_key(row) for row in self.public()}
        for row in rows:
            try:
                row['saved'] = network_key(row) in keys
            except ValueError:
                row['saved'] = False
        return rows

    def resolve(self, request):
        """Resolve an explicit saved selection before any radio mutations."""
        if not isinstance(request, dict) or type(request.get('use_saved', False)) is not bool:
            raise ValueError('Invalid saved Wi-Fi request')
        if not request.get('use_saved') or request.get('enabled', True) is False:
            return request
        key = network_key(request)
        if request.get('password'):
            raise ValueError('Choose the saved password or enter a different password')
        with self.transaction() as data:
            for row in data['entries']:
                if network_key(row) == key:
                    return dict(request, password=row['password'])
        raise ValueError(SAVED_MISSING)

    def observe(self, profile, status):
        """Save only COMPLETED authentication for the exact running attempt."""
        attempt = profile.get('save_id', '')
        if (not re.fullmatch('[0-9a-f]{32}', attempt)
                or status.get('wpa_state') != 'COMPLETED'
                or status.get('id_str') != attempt
                or (profile.get('bssid') and status.get('bssid') != profile['bssid'])):
            return
        with self.transaction() as data:
            current = self.profile()
            if (not current.get('enabled') or current.get('save_id') != attempt
                    or any(current.get(k) != profile.get(k) for k in ('ssid', 'security', 'password'))
                    or data['completed'].get('builtin') == attempt):
                return
            self.put(data, profile)
            data['completed']['builtin'] = attempt
            self.write(data)

    def forget(self, ssid, security):
        """Remove the saved copy without modifying the active client profile."""
        key = network_key({'ssid': ssid, 'security': security})
        with self.transaction() as data:
            data['entries'] = [row for row in data['entries'] if network_key(row) != key]
            # Forget also cancels an association whose success is not yet saved.
            profile = self.profile()
            try:
                matches = network_key(profile) == key
            except ValueError:
                matches = False
            attempt = profile.get('save_id', '')
            if matches and re.fullmatch('[0-9a-f]{32}', attempt):
                data['completed']['builtin'] = attempt
            self.write(data)
            return [{k: row[k] for k in ('ssid', 'security')} for row in data['entries']]
