"""Exercise service defect fixes through the real shell functions and blocks."""

import errno
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


TOOLS = Path(__file__).resolve().parent
PEER_EDITS = ('add_peer_to_config', 'share_default_folder_with_peer')


def functions(script):
    """Extract shell functions without running node setup or resident loops."""
    return '\n'.join(re.findall(
        r'^\w+\(\) \{\n.*?^\}', (TOOLS / script).read_text(), re.M | re.S))


class ShellFixture(unittest.TestCase):
    """Run real shell code with temporary paths and external command stubs."""

    def setUp(self):
        """Create an isolated command path and event log."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.events = self.root / 'events'
        self.events.touch()
        self.env = dict(os.environ, TEST_ROOT=str(self.root),
                        EVENTS=str(self.events), PATH=os.pathsep.join(
                            (str(self.bin), os.environ['PATH'])))

    def stub(self, name, body, interpreter='/bin/bash'):
        """Install a fake external command in the fixture's PATH."""
        path = self.bin / name
        path.write_text(f'#!{interpreter}\n' + body)
        path.chmod(0o755)

    def shell(self, source, **env):
        """Run a shell fragment without inherited shell error options."""
        return subprocess.run(
            ['bash', '-c', source], cwd=self.root,
            env=dict(self.env, **env), capture_output=True, text=True,
            timeout=10)


class MumbleDatabaseTests(ShellFixture):
    """Use SQLite databases while injecting backup and filesystem failures."""

    def setUp(self):
        """Install a SQLite CLI adapter and harmless node command stubs."""
        super().setUp()
        self.source = self.root / 'source.sqlite'
        self.dest = self.root / 'dest.sqlite'
        self.database(self.source, 'shared')
        self.database(self.dest, 'local')
        self.code = functions('mumble-election.sh') + '''
log() { printf '%s\n' "$*" >&2; }
WORKING_DB="$TEST_ROOT/dest.sqlite"
SHARED_DB_DIR="$TEST_ROOT/shared"
SHARED_DB="$TEST_ROOT/source.sqlite"
BACKUP_DIR="$SHARED_DB_DIR/backups"
MUMBLE_SERVICE_NAME=mumble-server.service
MUMBLE_USER=radio
'''
        # The host has no sqlite3 CLI. Use the real Python SQLite engine for
        # its backup/integrity/checkpoint operations and inject CLI failures.
        self.stub('sqlite3', '''
import os
from pathlib import Path
import shlex
import sqlite3
import sys

path, sql = sys.argv[1:]
with open(os.environ['EVENTS'], 'a') as log:
    log.write(sql + '\\n')
if sql.startswith('.backup'):
    target = shlex.split(sql)[1]
    if os.environ.get('BACKUP_MODE') == 'fail':
        sys.exit(1)
    if os.environ.get('BACKUP_MODE') == 'corrupt':
        Path(target).write_text('invalid database')
        sys.exit(0)
    with sqlite3.connect(path) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
else:
    try:
        with sqlite3.connect(path) as db:
            for row in db.execute(sql):
                print('|'.join(map(str, row)))
    except sqlite3.DatabaseError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
''', interpreter=sys.executable)
        self.stub('logger', 'cat >/dev/null\nexit 0\n')
        self.stub('sleep', ':\n')
        self.stub('sudo', '''
[ "${1:-}" = -u ] && shift 2
"$@"
''')
        self.stub('chown', ':\n')
        self.stub('mv', '''
if [ "${FAIL_MOVE:-}" = "$1" ]; then exit 1; fi
exec /bin/mv "$@"
''')
        self.stub('systemctl', '''
printf '%s\n' "$*" >> "$EVENTS"
case "$1" in
    is-active) [ "${SERVICE_ACTIVE:-0}" = 1 ] ;;
    *) exit 0 ;;
esac
''')

    def database(self, path, value):
        """Create a database with a recognizable payload."""
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE payload (value TEXT)')
            db.execute('INSERT INTO payload VALUES (?)', (value,))

    def payload(self, path):
        """Read the database payload after a copy or failed replacement."""
        with sqlite3.connect(path) as db:
            return db.execute('SELECT value FROM payload').fetchone()[0]

    def copy(self, **env):
        """Invoke the production copy helper on the fixture databases."""
        return self.shell(self.code +
                          '\nsafe_copy_database "$SHARED_DB" "$WORKING_DB"',
                          **env)

    def test_backup_failure_reaches_checkpoint_copy(self):
        """A successful logger must not hide a failed SQLite backup."""
        result = self.copy(BACKUP_MODE='fail')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('wal_checkpoint', self.events.read_text())
        self.assertEqual(self.payload(self.dest), 'shared')

    def test_corrupt_backup_reaches_checkpoint_copy(self):
        """An invalid backup also leaves the checkpoint fallback available."""
        result = self.copy(BACKUP_MODE='corrupt')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('wal_checkpoint', self.events.read_text())
        self.assertEqual(self.payload(self.dest), 'shared')

    def test_failed_backup_replacement_returns_failure(self):
        """A failed rename must retain the old DB and report failure."""
        result = self.copy(FAIL_MOVE=str(self.dest) + '.tmp')
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.payload(self.dest), 'local')
        self.assertNotIn('copied successfully', result.stderr)

    def test_failed_fallback_replacement_returns_failure(self):
        """Check the fallback rename independently of backup status handling."""
        # Force the old pipeline down its fallback too, isolating this defect.
        self.stub('logger', 'cat >/dev/null\nexit 1\n')
        result = self.copy(BACKUP_MODE='fail',
                           FAIL_MOVE=str(self.dest) + '.tmp')
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.payload(self.dest), 'local')

    def test_failed_sidecar_replacement_returns_failure(self):
        """The fallback must not hide failure to move a WAL/SHM sidecar."""
        self.stub('logger', 'cat >/dev/null\nexit 1\n')
        for suffix in ('-wal', '-shm'):
            with self.subTest(suffix=suffix):
                # Isolate publication from SQLite consuming its sidecars.
                code = self.code + '''
checkpoint_database() { :; }
check_database_integrity() { return 0; }
safe_copy_database "$SHARED_DB" "$WORKING_DB"
'''
                Path(str(self.source) + suffix).write_text('sidecar')
                result = self.shell(
                    code, BACKUP_MODE='fail',
                    FAIL_MOVE=str(self.dest) + '.tmp' + suffix)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                Path(str(self.source) + suffix).unlink()

    def test_failed_fallback_copy_rejects_stale_temporary_database(self):
        """A leftover valid temporary DB cannot hide a failed file copy."""
        self.database(Path(str(self.dest) + '.tmp'), 'stale')
        self.stub('logger', 'cat >/dev/null\nexit 1\n')
        self.stub('cp', 'exit 1\n')
        result = self.copy(BACKUP_MODE='fail')
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.payload(self.dest), 'local')

    def test_failed_sidecar_copy_returns_failure(self):
        """A missing sidecar must not turn a partial fallback into success."""
        self.stub('logger', 'cat >/dev/null\nexit 1\n')
        self.stub('cp', '''
case "$1" in *-wal|*-shm) exit 1 ;; esac
exec /bin/cp "$@"
''')
        for suffix in ('-wal', '-shm'):
            with self.subTest(suffix=suffix):
                Path(str(self.source) + suffix).write_text('sidecar')
                code = self.code + '''
checkpoint_database() { :; }
check_database_integrity() { return 0; }
safe_copy_database "$SHARED_DB" "$WORKING_DB"
'''
                result = self.shell(code, BACKUP_MODE='fail')
                self.assertNotEqual(result.returncode, 0, result.stderr)
                Path(str(self.source) + suffix).unlink()

    def test_failed_shared_publication_returns_failure(self):
        """The outer publishing rename must propagate a failure as well."""
        result = self.shell(self.code + '\nsync_database_to_shared',
                            FAIL_MOVE=str(self.source) + '.uploading')
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.payload(self.source), 'shared')
        self.assertNotIn('Database synced to Syncthing', result.stderr)

    def test_missing_backup_directory_is_created(self):
        """An existing shared directory still needs its backup subdirectory."""
        (self.root / 'shared').mkdir()
        result = self.shell(self.code + '\nensure_shared_directory')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'shared/backups').is_dir())

    def takeover(self, **env):
        """Run the real takeover block with a failed shared database sync."""
        source = (TOOLS / 'mumble-election.sh').read_text()
        start = source.index('    # Sync database and start service if needed')
        end = source.index('\n    touch /var/run/mesh-mumble.state', start)
        code = self.code + '''
WAS_ALREADY_LEADER=false
sync_database_from_shared() {
    printf '%s\n' sync >> "$EVENTS"
    return 1
}
''' + source[start:end]
        return self.shell(code, **env)

    def test_sync_failure_logs_degraded_local_start(self):
        """A valid local DB keeps voice available when shared sync fails."""
        result = self.takeover()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Shared database sync failed', result.stderr)
        self.assertIn('start mumble-server.service', self.events.read_text())
        self.assertEqual(self.payload(self.dest), 'local')

    def test_sync_failure_does_not_start_corrupt_local_database(self):
        """A known bad local DB cannot serve as the degraded fallback."""
        self.dest.write_text('corrupt')
        result = self.takeover()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('start mumble-server.service', self.events.read_text())

    def test_running_voice_restarts_bound_to_new_vip_on_takeover(self):
        """A live server must reload host= after acquiring a changed VIP."""
        self.running_takeover()

    def test_running_voice_rebinds_even_when_shared_database_is_corrupt(self):
        """An early sync failure still needs to reload the changed binding."""
        self.source.write_text('corrupt')
        self.running_takeover(sync_fails=True)

    def running_takeover(self, sync_fails=False):
        """Exercise VIP assignment and real DB sync with a stateful service."""
        config = self.root / 'mumble.ini'
        config.write_text('host=10.0.0.1\n')
        bound = self.root / 'bound'
        bound.write_text('10.0.0.1\n')
        (self.root / 'active').touch()
        self.stub('ip', 'exit 0\n')  # Neither VIP is assigned yet.
        self.stub('arping', 'exit 0\n')
        self.stub('systemctl', '''
printf '%s\n' "$*" >> "$EVENTS"
case "$1" in
    is-active) [ -f "$TEST_ROOT/active" ] ;;
    stop) rm -f "$TEST_ROOT/active" ;;
    start)
        if [ ! -f "$TEST_ROOT/active" ]; then
            sed -n 's/^host=//p' "$TEST_ROOT/mumble.ini" > "$TEST_ROOT/bound"
            touch "$TEST_ROOT/active"
        fi ;;
esac
''')
        source = (TOOLS / 'mumble-election.sh').read_text()
        start = source.index('    # Check whether VIPs are assigned locally.')
        end = source.index('\n    touch /var/run/mesh-mumble.state', start)
        result = self.shell(self.code + '''
MUMBLE_CONFIG="$TEST_ROOT/mumble.ini"
MUMBLE_IPV4_VIP=10.1.0.1
IPV4_VIP_WITH_MASK=10.1.0.1/24
MUMBLE_IPV6_VIP=fd00:1::1
MUMBLE_IPV6_VIP_WITH_MASK=fd00:1::1/64
CONTROL_IFACE=br0
ensure_shared_directory
''' + source[start:end])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_text(), 'host=10.1.0.1\n')
        self.assertEqual(bound.read_text(), '10.1.0.1\n')
        self.assertTrue((self.root / 'active').exists())
        events = self.events.read_text().splitlines()
        self.assertLess(events.index('stop mumble-server.service'),
                        events.index('start mumble-server.service'))
        self.assertEqual(self.payload(self.dest), 'local' if sync_fails else 'shared')
        if sync_fails:
            self.assertIn('Shared database sync failed', result.stderr)

    def test_sync_failure_allows_first_local_start(self):
        """With no local DB, Mumble can create its default on first start."""
        self.dest.unlink()
        result = self.takeover()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('start mumble-server.service', self.events.read_text())


class SyncthingConfigTests(ShellFixture):
    """Check configuration content and metadata after real shell edits."""

    def setUp(self):
        """Create a private config with a folder and an existing device."""
        super().setUp()
        self.config = self.root / 'config.xml'
        self.config.write_text('''<configuration version="37">
    <folder id="default" path="/home/radio/Sync">
    </folder>
    <device id="EXISTING"><address>dynamic</address></device>
</configuration>
''')
        self.config.chmod(0o600)

    def edit_peer(self, function):
        """Invoke a peer-manager function against the isolated config."""
        return self.shell(functions('syncthing-peer-manager.sh') +
                          '\nSYNCTHING_CONFIG_FILE="$TEST_ROOT/config.xml"\n' +
                          f'{function} NEW-PEER\n')

    def test_peer_edits_preserve_private_mode(self):
        """Both root replacements must retain the radio config's mode."""
        for function in PEER_EDITS:
            with self.subTest(function=function):
                self.config.chmod(0o600)
                result = self.edit_peer(function)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(stat.S_IMODE(self.config.stat().st_mode),
                                 0o600)
                tree = ET.parse(self.config)
                prefix = '.' if function == 'add_peer_to_config' else './folder'
                self.assertIsNotNone(
                    tree.find(prefix + '/device[@id="NEW-PEER"]'))

    def test_peer_edits_preserve_owner_and_group(self):
        """Use a nondefault group to detect lost ownership without root."""
        groups = [group for group in os.getgroups() if group != os.getegid()]
        if not groups:
            self.skipTest('requires a supplementary group for chgrp')
        try:
            os.chown(self.config, -1, groups[0])
            self.config.chmod(0o600)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.EPERM, errno.EACCES):
                raise
            self.skipTest(f'ownership/mode changes unavailable: {exc}')
        for function in PEER_EDITS:
            with self.subTest(function=function):
                os.chown(self.config, -1, groups[0])
                before = self.config.stat()
                result = self.edit_peer(function)
                self.assertEqual(result.returncode, 0, result.stderr)
                after = self.config.stat()
                self.assertEqual((after.st_uid, after.st_gid),
                                 (before.st_uid, before.st_gid))

    def test_metadata_copy_failure_keeps_original_config(self):
        """Failed metadata preservation must prevent publishing a root file."""
        self.stub('cp', 'exit 1\n')
        for function in PEER_EDITS:
            with self.subTest(function=function):
                before = self.config.read_bytes()
                result = self.edit_peer(function)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.config.read_bytes(), before)

    def harden_gui(self, gui):
        """Run the exact provisioning sed edit on generated-style XML."""
        self.config.write_text(
            '<configuration>\n' + gui + '\n'
            '<device id="OTHER"><address>dynamic</address></device>\n'
            '</configuration>\n')
        source = (TOOLS / 'radio-setup.sh').read_text()
        start = source.index('    echo " >> Hardening Syncthing')
        end = source.index('    echo " -- CONFIGURED --', start)
        result = self.shell('SYNCTHING_CONFIG="$TEST_ROOT/config.xml"\n' +
                            source[start:end])
        self.assertEqual(result.returncode, 0, result.stderr)
        tree = ET.parse(self.config)
        self.assertEqual(tree.findtext('gui/address'), '127.0.0.1:8384')
        self.assertEqual(tree.findtext('gui/apikey'), 'keep-this-key')
        self.assertEqual(tree.findtext('device/address'), 'dynamic')
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)

    def test_gui_default_multiline_layout(self):
        """The documented default has GUI and address on separate lines."""
        # https://docs.syncthing.net/users/config.html#config-file-format
        self.harden_gui('''    <gui enabled="true" tls="false">
        <address>0.0.0.0:8384</address>
        <apikey>keep-this-key</apikey>
        <theme>default</theme>
    </gui>''')

    def test_gui_reordered_attributes(self):
        """Additional or reordered GUI attributes must not defeat binding."""
        self.harden_gui('''    <gui debugging="false" tls="true" enabled="true">
        <address>[::]:8384</address>
        <apikey>keep-this-key</apikey>
    </gui>''')
        self.assertEqual(ET.parse(self.config).find('gui').get('tls'), 'true')

    def test_gui_compact_layout_preserves_other_fields(self):
        """A compact GUI must retain its API key and adjacent device address."""
        self.harden_gui('<gui enabled="true" tls="false" debugging="false">'
                        '<address>0.0.0.0:8384</address>'
                        '<apikey>keep-this-key</apikey></gui>')


class BridgeMembershipTests(ShellFixture):
    """Exercise the real bridge-membership checks with observed memberships."""

    def membership(self, bridged, meshed):
        """Run only the diagnostic's bridge section, using real parsers."""
        self.stub('ip', 'exit 0\n')
        self.stub('bridge', 'printf "1: bat0: master br0\\n"\n' +
                  ('printf "2: wlan1: master br0\\n"\n' if bridged else ''))
        self.stub('batctl', 'printf "wlan0: active\\n"\n' +
                  ('printf "wlan1: active\\n"\n' if meshed else ''))
        source = (TOOLS / 'verify-bridge.sh').read_text()
        start = source.index('# --- 1. Check Bridge Membership ---')
        end = source.index('# --- 2. Check bat0 Membership ---', start)
        return self.shell(functions('verify-bridge.sh') + '''
ERRORS=0
WARNINGS=0
AP_INTERFACE=wlan1
''' + source[start:end] + '\nexit "$ERRORS"\n')

    def test_candidate_returned_to_mesh_is_not_a_bridge_failure(self):
        """A provisioned candidate can legitimately be only in bat0."""
        result = self.membership(bridged=False, meshed=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('BOTH br0 and bat0', result.stderr)

    def test_simultaneous_membership_is_a_failure(self):
        """An actually bridged AP must not also be a BATMAN hard interface."""
        result = self.membership(bridged=True, meshed=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('BOTH br0 and bat0', result.stderr)

    def test_ap_only_and_idle_candidate_are_not_failures(self):
        """Normal AP membership and an idle candidate must not fail."""
        for bridged in (True, False):
            with self.subTest(bridged=bridged):
                result = self.membership(bridged=bridged, meshed=False)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
