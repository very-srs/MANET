"""Password-authenticated, encrypted Alfred control messages.

Status/telemetry remains public. Control, cancellation and ACK messages use
AES-256-GCM with a scrypt key derived from the shared *admin* password. No mesh
SAE/AP key fallback is allowed. Persistent receive state prevents an applied
or cancelled command from becoming new again after reboot or rollback.
"""

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time


MESH_CONF = '/etc/mesh.conf'
STATE_DIR = '/var/lib/manet-admin'
CONFIG_ACK_TYPE = 73
KINDS = {70: {'mesh_config', 'mesh_config_cancel'},
         71: {'radio_state', 'radio_cancel'},
         72: {'radio_ack'}, 73: {'config_ack'},
         74: {'acs_state'}, 75: {'acs_helper'}}
CHALLENGE_KINDS = {76: {'acs_probe'}, 77: {'acs_probe_reply'}}
MAX_MESSAGE_BYTES = 16384
MAX_AGE_SECONDS = 900
FUTURE_SKEW_SECONDS = 60
ENVELOPE_KIND = 'manet_admin_v1'


class AdminError(ValueError):
    pass


def clock_ready():
    directory = os.environ.get('MANET_TIME_RUN_DIR', os.environ.get('MANET_ACS_RUN_DIR', '/run'))
    return (Path(directory) / 'initial_time_synced').is_file()


def require_clock():
    if not clock_ready():
        raise AdminError('Waiting for initial GPS/NTP synchronization before timed mesh control')


def password_from_conf(path=MESH_CONF):
    password = ''
    with open(path) as source:
        for line in source:
            key, sep, value = line.partition('=')
            if sep and key.strip() == 'admin_password':
                password = value.strip().strip('\"\'')
    if not password:
        raise AdminError('No admin_password configured; admin control is disabled')
    return password


def private_json_write(path, value):
    """Atomic, owner-only staging and security state; never log the contents."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as target:
            json.dump(value, target, separators=(',', ':'), allow_nan=False)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path):
    try:
        with open(path) as source:
            value = json.load(source)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise AdminError('Invalid persistent admin state')
    return value


def _b64(data):
    return base64.b64encode(data).decode('ascii')


def _unb64(text, length=None):
    if not isinstance(text, str) or len(text) > MAX_MESSAGE_BYTES * 2:
        raise AdminError('Invalid encrypted message field')
    data = base64.b64decode(text, validate=True)
    if length is not None and len(data) != length:
        raise AdminError('Invalid encrypted message field length')
    return data


@lru_cache(maxsize=64)
def _derive_key(password, salt):
    # 32 MiB; salts are random per publishing node, reused with fresh nonces.
    # Cache only in process memory. Receivers never persist derived keys.
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=b'MANET admin v1\0' + salt, length=32,
                  n=2**15, r=8, p=1).derive(password.encode('utf-8'))


@dataclass(frozen=True)
class Message:
    payload: dict
    sent_ns: int
    message_id: str

    @property
    def order(self):
        return [self.sent_ns, self.message_id]


class AdminTransport:
    def __init__(self, conf=MESH_CONF, state_dir=STATE_DIR):
        self.conf = conf
        self.state_dir = Path(state_dir)

    @contextmanager
    def _locked(self):
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        fd = os.open(self.state_dir / '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def seal(self, type_id, payload):
        require_clock()
        if payload.get('kind') not in KINDS.get(type_id, set()):
            raise AdminError('Wrong admin message kind for Alfred type')
        password = password_from_conf(self.conf)
        with self._locked():
            path = self.state_dir / 'sender.json'
            sender = _read_json(path)
            salt = _unb64(sender['salt'], 16) if sender else os.urandom(16)
            received = _read_json(self.state_dir / f'received-{type_id}.json')
            now = time.time_ns()
            sent_ns = max(now, int(sender.get('sent_ns', 0)) + 1,
                          int(received.get('order', [0])[0]) + 1)
            if sent_ns > now + FUTURE_SKEW_SECONDS * 10**9:
                raise AdminError('Clock behind admin history; synchronize time before changing settings')
            private_json_write(path, {'salt': _b64(salt), 'sent_ns': sent_ns})
        return self._encrypt(type_id, payload, password, salt, sent_ns)

    def seal_challenge(self, type_id, payload):
        """Clock-independent ACS discovery only; the caller must bind a nonce.

        This has separate salt state and never advances timed sender/replay
        history. No administrative command can use this transport.
        """
        if payload.get('kind') not in CHALLENGE_KINDS.get(type_id, set()):
            raise AdminError('Wrong bootstrap message kind for Alfred type')
        password = password_from_conf(self.conf)
        with self._locked():
            path = self.state_dir / 'bootstrap-sender.json'
            sender = _read_json(path)
            salt = _unb64(sender['salt'], 16) if sender else os.urandom(16)
            if not sender:
                private_json_write(path, {'salt': _b64(salt)})
        return self._encrypt(type_id, payload, password, salt, 0)

    @staticmethod
    def _encrypt(type_id, payload, password, salt, sent_ns):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        body = json.dumps({'payload': payload, 'sent_ns': sent_ns,
                           'message_id': secrets.token_hex(16)},
                          separators=(',', ':'), allow_nan=False).encode()
        if len(body) > MAX_MESSAGE_BYTES:
            raise AdminError('Admin message too large')
        nonce = os.urandom(12)
        ciphertext = AESGCM(_derive_key(password, salt)).encrypt(
            nonce, body, f'MANET admin v1 Alfred {type_id}'.encode())
        return {'kind': ENVELOPE_KIND, 'salt': _b64(salt),
                'nonce': _b64(nonce), 'ciphertext': _b64(ciphertext)}

    def open(self, type_id, envelope):
        require_clock()
        message = self._decrypt(type_id, envelope, KINDS)
        age = (time.time_ns() - message.sent_ns) / 10**9
        if not -FUTURE_SKEW_SECONDS <= age <= MAX_AGE_SECONDS:
            raise AdminError('Expired admin message or clocks out of sync')
        return message

    def open_challenge(self, type_id, envelope):
        """Authenticate type 76/77 only. Freshness requires the caller's nonce."""
        message = self._decrypt(type_id, envelope, CHALLENGE_KINDS)
        if message.sent_ns != 0:
            raise AdminError('Invalid bootstrap timestamp')
        return message

    def _decrypt(self, type_id, envelope, kinds):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if not isinstance(envelope, dict) or envelope.get('kind') != ENVELOPE_KIND:
            raise AdminError('Unsigned admin message')
        if type_id not in kinds:
            raise AdminError('Unknown admin channel')
        salt = _unb64(envelope.get('salt'), 16)
        nonce = _unb64(envelope.get('nonce'), 12)
        ciphertext = _unb64(envelope.get('ciphertext'))
        if not 16 <= len(ciphertext) <= MAX_MESSAGE_BYTES + 16:
            raise AdminError('Invalid admin ciphertext length')
        plaintext = AESGCM(_derive_key(password_from_conf(self.conf), salt)).decrypt(
            nonce, ciphertext, f'MANET admin v1 Alfred {type_id}'.encode())
        body = json.loads(plaintext)
        payload, sent_ns, message_id = body['payload'], body['sent_ns'], body['message_id']
        if (not isinstance(payload, dict) or payload.get('kind') not in kinds[type_id]
                or type(sent_ns) is not int or not isinstance(message_id, str)
                or not re.fullmatch('[0-9a-f]{32}', message_id)):
            raise AdminError('Invalid authenticated message')
        return Message(payload, sent_ns, message_id)

    def messages(self, type_id, raw):
        """Authenticate before comparing versions/timestamps or inspecting actions."""
        from cryptography.exceptions import InvalidTag
        messages = []
        # Our wire envelope is printable ASCII. Alfred escapes quotes and
        # backslashes; decode only its quoted payload, not network text as code.
        for match in re.finditer(r'\{\s*"[0-9a-fA-F:]{17}"\s*,\s*("(?:\\.|[^"\\])*")\s*\}', raw):
            try:
                envelope = json.loads(json.loads(match.group(1)))
                messages.append(self.open(type_id, envelope))
            except (ValueError, KeyError, TypeError, InvalidTag):
                continue
        return sorted(messages, key=lambda message: message.order)

    def accept(self, type_id, message):
        """Persist the newest control message before staging or applying it.

        Repeated delivery of the current pending message is normal in Alfred.
        An older message or a consumed one cannot be staged again. Receivers
        consume activations before applying, since applying may stop them.
        """
        if type_id not in (70, 71):
            raise AdminError('Replay tracking is only for control channels')
        with self._locked():
            path = self.state_dir / f'received-{type_id}.json'
            state = _read_json(path)
            previous = state.get('order', [0, ''])
            if message.order < previous or (message.order == previous and state.get('done')):
                return False
            if message.order != previous:
                private_json_write(path, {'order': message.order, 'done': False})
        return True

    def complete(self, type_id, message):
        with self._locked():
            path = self.state_dir / f'received-{type_id}.json'
            state = _read_json(path)
            if state.get('order') == message.order:
                state['done'] = True
                private_json_write(path, state)


def new_version():
    # A fresh transaction ID prevents old ACKs authorizing an identical edit.
    return secrets.token_hex(16)
