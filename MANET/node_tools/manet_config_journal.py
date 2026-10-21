"""Durable local mesh configuration jobs, independent of Alfred delivery."""

from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

from manet_admin import Message, _jumped, clock_ready, private_json_write
import manet_clock
from manet_config_io import atomic_write, load_kv_file, rewrite_keys
from mesh_config import DANGEROUS_KEYS
from manet_supplicant import configured, service_name

TERMINAL = {'committed', 'rolled_back', 'cancelled'}
TOOLS = Path(__file__).resolve().parent


def config_digest(value):
    """Identify literal authenticated content without retaining ciphertext."""
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode()).hexdigest()


def config_application_status(root=None):
    """Read a credential-free projection for the existing admin status API."""
    journal = ConfigJournal(root)
    path = journal.root / 'journal.json'
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if data.get('schema') != 1:
        raise ValueError('Unknown config journal schema')
    job = data['job']
    if not job:
        return None
    phase = job['phase']
    version = job['package']['version']
    rollback = {'armed': False}
    backup = job['backup']
    if backup and phase not in TERMINAL:
        boot, now = journal.boot_clock()
        left = backup['deadline'] - now if backup['boot'] == boot else 0
        rollback = {'armed': True, 'version': version,
                    'seconds_left': max(0, int(left)),
                    'peers_before': str(backup['peers'])}
    return {'version': version, 'application_status': phase,
            'applied_version': version if phase in
            {'verified', 'committed'} else None,
            'last_error': job['last_error'], 'rollback': rollback}


class ConfigJournal:
    """Serialize one local job and retain terminal transaction tombstones."""

    def __init__(self, root=None, pending=None, ack=None, applied=None):
        """Use installed paths unless a caller supplies an isolated layout."""
        self.root = Path(root or os.environ.get(
            'MANET_CONFIG_STATE_DIR', '/var/lib/manet-config'))
        runtime = Path(os.environ.get('MANET_RUN_DIR', '/var/run'))
        self.pending = Path(pending or runtime / 'mesh_pending_config.json')
        self.ack = Path(ack or runtime / 'mesh_config_ack_version')
        self.applied = Path(applied or runtime / 'mesh_applied_config_version')
        self.conf = Path(os.environ.get('MANET_MESH_CONF', '/etc/mesh.conf'))
        self.wpa = Path(os.environ.get(
            'MANET_WPA_DIR', '/etc/wpa_supplicant'))
        self.legacy = Path(os.environ.get(
            'MANET_ROLLBACK_DIR', '/var/lib/manet-config-rollback'))
        self.data = {'schema': 1, 'job': None, 'finished': {}}

    @contextmanager
    def locked(self):
        """Exclude sync, migration and workers, without waiting on a worker."""
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        directory = os.open(self.root.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        fd = os.open(self.root / 'lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = self.root / 'journal.json'
            if path.exists():
                self.data = json.loads(path.read_text())
                if self.data.get('schema') != 1:
                    raise ValueError('Unknown config journal schema')
            yield self
        finally:
            os.close(fd)

    @property
    def job(self):
        """Return the current durable job, including its terminal outcome."""
        return self.data['job']

    def save(self):
        """Persist a transition before its externally visible side effects."""
        private_json_write(self.root / 'journal.json', self.data)

    def busy(self):
        """An activated job owns configuration until commit or rollback."""
        return self.job and self.job['phase'] not in TERMINAL | {'staged'}

    def stage(self, package, message, admin):
        """Accept a transport order and durably stage its literal settings."""
        version = package['version']
        digest = config_digest(package['config'])
        if self.busy() or version in self.data['finished']:
            return False
        if (self.job and self.job['package']['version'] == version
                and self.job['desired_digest'] != digest):
            raise ValueError('Transaction version reused with other settings')
        if not admin.accept(70, message):
            return False
        acknowledged = bool(self.job and self.job.get('acknowledged')
                            and self.job['package'] == package)
        self.data['job'] = {
            'package': package, 'order': message.order,
            'payload_digest': config_digest(message.payload),
            'desired_digest': digest, 'phase': 'staged',
            'acknowledged': acknowledged, 'consumed': False,
            'backup': None, 'last_error': None,
            'clock': manet_clock.epoch_id(),
        }
        self.save()
        self.project()
        return True

    def cancel(self, message, admin):
        """Cancel staging only, preserving any active trial or restoration."""
        if self.busy() or not admin.accept(70, message):
            return
        if self.job and self.job['phase'] == 'staged':
            self.finish('cancelled')
        admin.complete(70, message)
        self.project()

    def acknowledge(self):
        """Remember successful staged ACK publication before activation."""
        if not self.job['acknowledged']:
            self.job['acknowledged'] = True
            self.save()

    def authorize(self, admin):
        """Make due work recoverable before consuming its transport message."""
        if not self.job:
            return False
        job = self.job
        if job['phase'] == 'staged':
            if _jumped(job.get('clock'), manet_clock.epoch_id()):
                # A clock jump voids a staged change before its activation.
                self.finish('cancelled')
                self.project()
                return False
            # A newer transport order may have been accepted just before a
            # crash saving its replacement stage. Never activate that older
            # stage while replay history already points to its replacement.
            if not job.get('legacy'):
                order = job['order']
                message = Message(job['package'], order[0], order[1])
                if not admin.accept(70, message):
                    return False
            due = job['package'].get('activate_at', 0)
            if not (job['acknowledged'] and due and clock_ready()
                    and time.time() >= due):
                return False
            job['phase'] = 'prepared'
            self.save()
        if job['phase'] in TERMINAL:
            return False
        if not job['consumed']:
            order = job['order']
            message = Message(job['package'], order[0], order[1])
            admin.complete(70, message)
            job['consumed'] = True
            self.save()
        return True

    def wpa_paths(self):
        """Select exactly the mesh supplicant files owned by config apply."""
        return sorted(path for path in
                      self.wpa.glob('wpa_supplicant-wlan*.conf')
                      if not path.name.endswith('-uplink.conf'))

    def files_match(self):
        """Compare every requested assignment, including supplicant copies."""
        config = self.job['package']['config']
        contents = self.conf.read_text()
        if rewrite_keys(contents, config) != contents:
            return False
        values = {dst: config[src] for src, dst in
                  [('mesh_ssid', 'ssid'), ('mesh_key', 'sae_password')]
                  if src in config}
        for path in self.wpa_paths():
            contents = path.read_text()
            if rewrite_keys(contents, values, values) != contents:
                return False
        for name, digest in self.job.get('region_files', {}).items():
            if self.region_fingerprint(Path(name).read_text()) != digest:
                return False
        return True

    def region_fingerprint(self, contents):
        """Track region assignments without treating ACS channels as drift."""
        prefixes = ('regulatory_domain=', 'halow_regulatory_domain=',
                    'country=', 'country_code=', 'REGDOMAIN=',
                    'options cfg80211 ', 'options morse ')
        return config_digest([line.strip() for line in contents.splitlines()
                              if line.strip().startswith(prefixes)])

    def persist_region_files(self):
        """Fsync deferred region output before verified completion."""
        if 'regulatory_domain' not in self.job['package']['config']:
            return
        root = Path(os.environ.get('MANET_REGION_ROOT', '/'))
        paths = [root / 'etc' / name for name in (
            'mesh.conf', 'modprobe.d/cfg80211.conf', 'modprobe.d/morse.conf',
            'default/crda', 'hostapd/hostapd.conf')]
        paths.extend((root / 'etc/wpa_supplicant').glob(
            'wpa_supplicant-*.conf'))
        digests = {}
        for path in paths:
            if path.exists():
                with path.open() as source:
                    digest = self.region_fingerprint(source.read())
                    digests[str(path)] = digest
                    os.fsync(source.fileno())
                fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        self.job['region_files'] = digests

    def ipv4_matches(self, network):
        """Wait for the existing IP manager to publish and install its CIDR."""
        state = load_kv_file(Path(os.environ.get(
            'MANET_IPV4_STATE', '/etc/mesh_ipv4_state')))
        if state.get('PERSISTENT_NETWORK') != network:
            return False
        address = state.get('PERSISTENT_IPV4', '')
        desired = ipaddress.IPv4Network(network)
        if not address or ipaddress.IPv4Address(address) not in desired:
            return False
        rows = json.loads(self.execute(['ip', '-j', '-4', 'addr', 'show',
                                        'dev', 'br0'], timeout=3))
        return any(entry.get('local') == address and
                   entry.get('prefixlen') == desired.prefixlen
                   for row in rows for entry in row.get('addr_info', []))

    def services_match(self, config=None):
        """Probe live requested services, respecting elections and radio-off."""
        if config is None:
            config = self.job['package']['config']
        if ('ipv4_network' in config
                and not self.ipv4_matches(config['ipv4_network'])):
            return False
        for key, unit in [('mtx', 'mediamtx'), ('mumble', 'mumble-server')]:
            if config.get(key) == 'n':
                result = subprocess.run(
                    ['systemctl', 'is-active', '--quiet', unit],
                    capture_output=True, timeout=3)
                if result.returncode not in {3, 4}:
                    return False
        if 'acs' in config:
            self.execute(['systemctl', 'is-active', '--quiet',
                          'node-manager.service'], timeout=3)
            runtime = Path(os.environ.get('MANET_RUN_DIR', '/var/run'))
            running = (runtime / 'node-manager.running').read_text().strip()
            expected = ('node-manager-acs.sh' if config['acs'] == 'y'
                        else 'node-manager-static.sh')
            link = Path(os.environ.get('MANET_BIN_DIR', str(TOOLS)))
            selected = Path(os.readlink(link / 'node-manager.sh')).name
            if running != expected or selected != expected:
                return False
        if any(key in config for key in DANGEROUS_KEYS):
            roles = Path(os.environ.get('MANET_IFACE_STATE_DIR', '/var/lib'))
            state_path = roles / 'mesh_radio_state.json'
            desired = (json.loads(state_path.read_text()).get('desired', {})
                       if state_path.exists() else {})
            for iface, halow in configured(roles).items():
                if desired.get(iface) == 'down':
                    continue
                self.execute(['systemctl', 'is-active', '--quiet',
                              service_name(iface, halow)], timeout=3)
                control = ('/var/run/wpa_supplicant_s1g' if halow
                           else '/var/run/wpa_supplicant')
                cli = 'wpa_cli_s1g' if halow else 'wpa_cli'
                pong = self.execute([cli, '-p', control, '-i', iface, 'ping'],
                                    timeout=3)
                if 'PONG' not in pong.splitlines():
                    return False
        return True

    def dangerous(self):
        """Include divergent radio files when deciding to protect a change."""
        config = self.job['package']['config']
        current = load_kv_file(self.conf)
        if any(key in config and config[key] != current.get(key)
               for key in DANGEROUS_KEYS):
            return True
        values = {dst: config[src] for src, dst in
                  [('mesh_ssid', 'ssid'), ('mesh_key', 'sae_password')]
                  if src in config}
        return any(rewrite_keys(path.read_text(), values, values)
                   != path.read_text() for path in self.wpa_paths())

    def execute(self, argv, **kwargs):
        """Bound a subprocess and keep credentials out of failure messages."""
        result = subprocess.run(argv, capture_output=True, text=True,
                                timeout=kwargs.pop('timeout', 30), **kwargs)
        if result.returncode:
            raise RuntimeError(f'{Path(argv[0]).name} failed')
        return result.stdout.strip()

    def boot_clock(self):
        """Return a boot identity and suspend-inclusive uptime."""
        boot = Path(os.environ.get('MANET_BOOT_ID_FILE',
                    '/proc/sys/kernel/random/boot_id')).read_text().strip()
        return boot, time.clock_gettime(time.CLOCK_BOOTTIME)

    def peers(self):
        """Count distinct peers using the existing bounded BATMAN helper."""
        value = self.execute([str(TOOLS / 'mesh-peer-count.sh'), '--batctl',
                              os.environ.get('BATCTL', '/usr/sbin/batctl')])
        if not value.isdecimal():
            raise ValueError('Invalid peer baseline')
        return int(value)

    def capture(self, source, destination):
        """Keep original bytes and metadata inside the fsynced journal."""
        st = source.stat()
        return {'path': str(destination), 'contents': source.read_text(),
                'mode': st.st_mode, 'uid': st.st_uid, 'gid': st.st_gid}

    def prepare_backup(self):
        """Save the first baseline and deadline before any live file write."""
        if self.job['phase'] != 'prepared':
            return
        if self.dangerous() and not self.job['package'].get('no_rollback'):
            peers = self.peers()
            files = [self.capture(path, path) for path in
                     [self.conf, *self.wpa_paths()]]
            boot, now = self.boot_clock()
            grace = int(os.environ.get('MANET_ROLLBACK_GRACE', '300'))
            if grace < 0:
                raise ValueError('Invalid rollback grace')
            self.job['backup'] = {
                'files': files, 'peers': peers, 'boot': boot,
                'deadline': now + grace,
            }
        self.job['phase'] = 'applying'
        self.save()

    def expired(self):
        """Never extend a trial, including when its boot clock is lost."""
        backup = self.job['backup']
        boot, now = self.boot_clock()
        return boot != backup['boot'] or now >= backup['deadline']

    def finish(self, phase):
        """Persist terminal intent and its tombstone in the same write."""
        self.job['phase'] = phase
        self.job['last_error'] = None
        version = self.job['package']['version']
        self.data['finished'][version] = {
            'phase': phase, 'desired_digest': self.job['desired_digest'],
        }
        self.save()

    def project(self):
        """Rebuild volatile UI hints from durable application state."""
        if not self.job:
            return
        job = self.job
        phase = job['phase']
        if phase in TERMINAL:
            self.pending.unlink(missing_ok=True)
            self.ack.unlink(missing_ok=True)
        else:
            private_json_write(self.pending, dict(job['package'],
                               application_status=phase,
                               last_error=job['last_error']))
            if job['acknowledged']:
                atomic_write(self.ack, job['package']['version'] + '\n')
        if phase in {'verified', 'committed'}:
            atomic_write(self.applied, job['package']['version'] + '\n')
        elif phase == 'rolled_back':
            self.applied.unlink(missing_ok=True)

    def restore(self):
        """Finish a durable rollback decision even if peers return midway."""
        for entry in self.job['backup']['files']:
            metadata = SimpleNamespace(st_mode=entry['mode'],
                                       st_uid=entry['uid'], st_gid=entry['gid'])
            atomic_write(entry['path'], entry['contents'], metadata)
        self.execute([sys.executable, os.environ.get(
            'MANET_SUPPLICANT_HELPER', str(TOOLS / 'manet_supplicant.py')),
            'restart'], timeout=150)
        self.execute(['systemctl', 'restart', 'batman-enslave.service'])
        if 'acs' in self.job['package']['config']:
            self.execute([os.environ.get('NODE_MANAGER_SELECT',
                          str(TOOLS / 'node-manager-select.sh'))])
            self.execute(['systemctl', 'restart', 'node-manager.service'])
        current = load_kv_file(self.conf)
        restored = {key: current[key] for key in self.job['package']['config']
                    if key in current}
        for key, unit in [('mtx', 'mediamtx'), ('mumble', 'mumble-server')]:
            if restored.get(key) == 'n':
                subprocess.run(['systemctl', 'stop', unit],
                               capture_output=True, timeout=30)
        if not self.services_match(restored):
            raise RuntimeError('Waiting for restored services or allocation')
        self.finish('rolled_back')

    def work(self):
        """Reconcile one attempt, or resolve the original rollback trial."""
        job = self.job
        if not job or job['phase'] in TERMINAL | {'staged'}:
            self.project()
            return
        if not job['consumed']:
            raise RuntimeError('Receiver must finish transport consumption')
        try:
            self.prepare_backup()
            backup = job['backup']
            if backup and self.expired() and job['phase'] != 'rolling_back':
                recovered = False
                if job['phase'] == 'verified':
                    try:
                        if not (self.files_match() and self.services_match()):
                            raise RuntimeError('Verification failed')
                        recovered = backup['peers'] == 0 or self.peers() > 0
                    except (OSError, ValueError, RuntimeError,
                            subprocess.SubprocessError):
                        pass
                if recovered:
                    self.finish('committed')
                else:
                    job['phase'] = 'rolling_back'
                    self.save()
            if job['phase'] == 'verified':
                try:
                    healthy = self.files_match() and self.services_match()
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    healthy = False
                if not healthy:
                    job['phase'] = 'applying'
                    self.save()
            if job['phase'] == 'rolling_back':
                self.restore()
            elif job['phase'] == 'applying':
                # A crash after a restart but before verification must repeat
                # reconciliation, even when every file already matches.
                private_json_write(self.root / 'desired.json', job['package'])
                env = dict(os.environ, MANET_CONFIG_WORKER='1',
                           MANET_CONFIG_PENDING=str(self.root / 'desired.json'))
                self.execute(['bash', str(TOOLS / 'mesh-config-apply.sh')],
                             env=env, timeout=170)
                self.persist_region_files()
                if not (self.files_match() and self.services_match()):
                    raise RuntimeError('Configuration verification failed')
                job['phase'] = 'verified'
                job['last_error'] = None
                self.save()
                if not backup:
                    self.finish('committed')
            if job['phase'] == 'verified' and not backup:
                self.finish('committed')
            self.project()
        except Exception as exc:
            # Subprocess stderr may contain credentials. Store only its type.
            job['last_error'] = type(exc).__name__
            self.save()
            raise

    def import_pending(self, validate, admin=None, strict=False):
        """Adopt root-private legacy staging before /run can be discarded."""
        if self.job:
            return
        legacy_only = (not self.pending.exists()
                       and (self.legacy / 'state').exists())
        if not self.pending.exists() and not legacy_only:
            return
        if not legacy_only:
            st = self.pending.stat()
            if st.st_uid != os.geteuid() or st.st_mode & 0o077:
                if strict:
                    raise ValueError('Legacy pending config is not private')
                return
        active = subprocess.run(
            ['systemctl', 'is-active', '--quiet', 'mesh-config-apply.service'],
            capture_output=True, timeout=3)
        if active.returncode == 0:
            raise RuntimeError('Wait for the legacy config worker to stop')
        if active.returncode not in {3, 4}:
            raise RuntimeError('Cannot verify the legacy worker is stopped')
        if legacy_only:
            old = load_kv_file(self.legacy / 'state')
            current = load_kv_file(self.conf)
            package = {'version': old['VERSION'], 'activate_at': 1,
                       'config': {key: current[key] for key in DANGEROUS_KEYS
                                  if key in current}}
        else:
            package = json.loads(self.pending.read_text())
        valid, reason = validate(package)
        if not valid:
            raise ValueError(reason)
        # Legacy staging was already authorized locally. An in-flight old
        # worker must be stopped before migration, as documented for updates.
        self.data['job'] = {
            'package': package, 'order': [0, 'legacy'],
            'payload_digest': config_digest(package),
            'desired_digest': config_digest(package['config']),
            'phase': 'staged', 'acknowledged': False, 'consumed': True,
            'backup': None, 'last_error': None,
        }
        if admin is not None and package.get('activate_at', 0) > 0:
            history_path = admin.state_dir / 'received-70.json'
            if history_path.exists() and self.ack.exists():
                history = json.loads(history_path.read_text())
                if (history.get('done') is True and
                        self.ack.read_text().strip() == package['version']):
                    self.job['order'] = history['order']
                    self.job['phase'] = 'prepared'
                    self.job['acknowledged'] = True
        self.job['legacy'] = True
        state = self.legacy / 'state'
        if state.exists():
            old = load_kv_file(state)
            if old.get('VERSION') != package['version']:
                raise ValueError('Conflicting legacy rollback trial')
            files = [self.capture(self.legacy / 'mesh.conf', self.conf)]
            for path in sorted((self.legacy / 'wpa').glob('*.conf')):
                if not path.name.endswith('-uplink.conf'):
                    files.append(self.capture(path, self.wpa / path.name))
            self.job['backup'] = {
                'files': files, 'peers': int(old['PEERS_BEFORE']),
                'deadline': int(old['DEADLINE']), 'boot': old['BOOT_ID'],
            }
            self.job['phase'] = ('rolling_back' if
                                (self.legacy / 'restoring').exists()
                                else 'verified' if legacy_only
                                else 'applying')
            self.job['acknowledged'] = True
        self.save()
        self.retire_legacy()
        self.project()

    def retire_legacy(self):
        """Disable the old checker only after its backup is durable here."""
        if self.job and self.job.get('legacy'):
            state = self.legacy / 'state'
            if state.exists() and self.job['backup']:
                os.replace(state, self.legacy / 'journal-owned')
                fd = os.open(self.legacy, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)


def config_journal_main():
    """Run the worker inside the existing independent transient unit."""
    journal = ConfigJournal()
    try:
        with journal.locked():
            journal.retire_legacy()
            journal.work()
        return 0
    except BlockingIOError:
        return 0
    except Exception as exc:
        print(f'Config apply journal: {type(exc).__name__}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(config_journal_main())
