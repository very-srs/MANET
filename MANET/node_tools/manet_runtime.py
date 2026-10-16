"""On-demand helper work hosted by mesh-channel-agreement, with no timer.

The FIFO worker sleeps in select until a manager/dispatcher requests work.
ACS keeps its independent decision loop. Service observations
allow a bounded client-lock wait, while other busy callers fall back without
queuing. Files are private and operations and arguments are allowlisted.
No shell text or Python received through this channel is evaluated.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import select
import ssl
import sys
import threading
import time

from manet_ip_runtime import main as reconcile_ip, module
from manet_registry_builder import atomic_text


class Helpers:
    """Dispatch allowlisted runtime work and cache the Syncthing public ID."""
    def __init__(self):
        """Start with no cached Syncthing certificate identity."""
        self.cert_cache = None

    def syncthing_id(self):
        """Derive the device ID, reusing it only while cert metadata matches."""
        from manet_ids import bytes_to_syncthing_id
        # Syncthing prefers an existing legacy config, else XDG_STATE_HOME.
        # Match the account selected at flash time, including its actual home.
        if 'MANET_SYNCTHING_CERT' in os.environ:
            paths = [Path(os.environ['MANET_SYNCTHING_CERT'])]
        else:
            username = 'radio'
            try:
                for line in Path(os.environ.get('MANET_MESH_CONF', '/etc/mesh.conf')).read_text().splitlines():
                    if line.startswith('ssh_user='):
                        username = line.partition('=')[2]
                        break
            except FileNotFoundError:
                pass
            if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', username) or username in ('root', 'nobody'):
                raise ValueError('Invalid SSH username in mesh.conf')
            try:
                account = pwd.getpwnam(username)
            except KeyError:
                return ''  # First boot has not created the account yet.
            if account.pw_uid < 1000 or account.pw_uid == 65534:
                raise ValueError('Refusing a system account for Syncthing')
            home = Path(account.pw_dir)
            paths = [home / '.config/syncthing/cert.pem',
                     home / '.local/state/syncthing/cert.pem']
        for path in paths:
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            key = (str(path), info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
            if self.cert_cache is None or self.cert_cache[0] != key:
                # https://docs.syncthing.net/dev/device-ids.html : SHA256(DER),
                # base32 and Luhn check digits. No private key or PAM session.
                der = ssl.PEM_cert_to_DER_cert(path.read_text())
                self.cert_cache = (key, bytes_to_syncthing_id(hashlib.sha256(der).digest()))
            return self.cert_cache[1]
        self.cert_cache = None
        return ''  # First boot: retry on the next identity publication.

    def dispatch(self, action, argument='-'):
        """Run one allowed operation and return its status code and output."""
        if action == 'ip' and argument == '-':
            return reconcile_ip(), ''
        if action == 'ipv4' and argument == '-':
            from manet_node_ipv4 import current_ipv4
            return 0, current_ipv4('br0')
        if action == 'syncthing' and argument == '-':
            return 0, self.syncthing_id()
        if action == 'interfaces' and argument == '-':
            from manet_interfaces import collect
            return 0, json.dumps(collect(), separators=(',', ':'))
        # MCS accepts only a bounded interface name, never shell syntax.
        if action == 'mcs' and re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', argument):
            helper = module('halow-mcs-summary')
            return 0, helper.shell_output(helper.collect(argument))
        if action == 'ap-mesh' and argument == '-':
            from manet_ap_mesh import Transition
            return 0, json.dumps(Transition().to_mesh())
        if argument in ('mediamtx', 'mumble'):
            from manet_election_runtime import check, hosting, service_ipv6
            if action == 'election':
                return 0, check(argument)
            if action == 'hosting':
                return 0, 'true' if hosting(argument) else 'false'
            if action == 'service-vip':
                return 0, service_ipv6(argument) + '/128'
        raise ValueError('unsupported runtime operation')


def serve(directory, helpers, stop=None):
    """Run one blocking request loop with the supplied handler."""
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    # Only this account may access request, reply and result files.
    if info.st_uid != os.geteuid() or info.st_mode & 0o077 or directory.is_symlink():
        raise PermissionError('runtime directory must be private and owned by this process')
    ready = directory / 'ready'
    ready.unlink(missing_ok=True)
    descriptors = []
    try:
        for name in ('request', 'reply'):
            path = directory / name
            path.unlink(missing_ok=True)
            os.mkfifo(path, 0o600)
            # Opening both ends avoids FIFO open blocking without a client.
            descriptors.append(os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC))
        request, reply = descriptors
        # Clients bind requests to this worker generation, not just its PID.
        generation = f'{os.getpid()} {time.monotonic_ns()}\n'
        atomic_text(ready, generation, 0o600)
        pending = b''
        while stop is None or not stop.is_set():
            # Wake every 100 ms with a stop event, else wait for a request.
            if not select.select([request], [], [], .1 if stop else None)[0]:
                continue
            pending += os.read(request, 4096)
            # Discard oversized buffered framing above 8 KiB.
            if len(pending) > 8192:
                pending = b''
                continue
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                # Reply tokens allow at most 80 digits or underscores.
                match = re.fullmatch(rb'([0-9_]{1,80}) (.*)', line)
                if not match:
                    continue
                token = match[1].decode()
                # Bound action/argument fields before dispatching any operation.
                request_fields = re.fullmatch(rb'([a-z][a-z0-9-]{0,31}) ([A-Za-z0-9_.:-]{1,32})', match[2])
                # Withdraw availability while working, including if the client
                # times out. No new request is submitted until it finishes.
                ready.unlink(missing_ok=True)
                # A valid reply token always gets an answer, even for malformed
                # requests. In particular, ipv4 is an operation, not bad framing.
                rc, output = 2, ''
                if request_fields:
                    action, argument = (s.decode() for s in request_fields.groups())
                    try:
                        rc, output = helpers.dispatch(action, argument)
                    except Exception as error:
                        # RuntimeError text is written for operators. Other
                        # exceptions can quote commands, so report only the type.
                        reason = type(error).__name__
                        if type(error) is RuntimeError:
                            reason = ' '.join(str(error).split())[:200] or reason
                        print(f'RUNTIME: {action} failed: {reason}', file=sys.stderr, flush=True)
                        rc, output = 1, reason
                # Expired clients must not leave unbounded files or block ACS.
                for old in directory.glob('result.*'):
                    old.unlink(missing_ok=True)
                atomic_text(directory / ('result.' + token), output, 0o600)
                # Publish availability before releasing a waiting client.
                # Its client lock serializes the next observer's request.
                atomic_text(ready, generation, 0o600)
                try:
                    os.write(reply, f'{token} {rc}\n'.encode())
                except BlockingIOError:
                    pass
    finally:
        ready.unlink(missing_ok=True)
        for descriptor in descriptors:
            os.close(descriptor)


def start():
    """Start the daemon FIFO thread with its own persistent helper cache."""
    thread = threading.Thread(target=serve, name='runtime-requests', daemon=True,
                              args=(os.environ.get('MANET_RUNTIME_DIR', '/run/manet-runtime'), Helpers()))
    thread.start()
    return thread
