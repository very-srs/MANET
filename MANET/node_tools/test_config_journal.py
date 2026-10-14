"""Fault injection for durable config jobs and the real file applier."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch

from manet_admin import AdminTransport, Message, private_json_write
from manet_config_journal import ConfigJournal, config_application_status
import test_config_apply_acs as apply_fixture

TOOLS = apply_fixture.TOOLS

SPEC = importlib.util.spec_from_file_location(
    'journal_config_sync', TOOLS / 'mesh-config-sync.py')
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class JournalDeath(BaseException):
    """A process death must skip ordinary error handling."""


class ConfigJournalTests(unittest.TestCase):
    """Exercise disk recovery with real writes and isolated service stubs."""

    def setUp(self):
        apply_fixture.AcsApplyTests.setUp(self)
        apply_fixture.AcsApplyTests.manager_running(self, 'n')
        self.journal_dir = self.root / 'journal'
        self.roles = self.root / 'roles'
        self.roles.mkdir()
        (self.roles / 'mesh_if').write_text('wlan0 wlan1\n')
        (self.root / 'initial_time_synced').touch()
        with self.conf.open('a') as stream:
            stream.write('admin_password=old-password\nmtx=y\n')
        self.env.update(MANET_CONFIG_STATE_DIR=str(self.journal_dir),
                        MANET_IFACE_STATE_DIR=str(self.roles),
                        MANET_TIME_RUN_DIR=str(self.root),
                        MANET_ROLLBACK_DIR=str(self.root / 'legacy'))
        env = patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        self.admin = AdminTransport(self.conf, self.root / 'admin')
        self.package = {'kind': 'mesh_config', 'version': 'abc123',
                        'activate_at': 1, 'config': {'mesh_ssid': 'new'}}
        self.message = Message(self.package, time.time_ns(), 'a' * 32)
        self.boot = 'test-boot'
        self.now = 1000
        clock = patch.object(ConfigJournal, 'boot_clock',
                             side_effect=lambda: (self.boot, self.now))
        clock.start()
        self.addCleanup(clock.stop)
        peers = patch.object(ConfigJournal, 'peers', return_value=1)
        self.peer_query = peers.start()
        self.addCleanup(peers.stop)
        (self.bin / 'wpa_cli').write_text('#!/bin/sh\nprintf "PONG\\n"\n')
        (self.bin / 'wpa_cli').chmod(0o755)
        (self.bin / 'systemctl').write_text('''#!/bin/bash
printf '%s\\n' "systemctl $*" >> "$TEST_CALLS"
case "$1" in
    is-active)
        case "${@: -1}" in
            mesh-config-apply.service) exit 3 ;;
            mediamtx|mumble-server)
                [ -f "$TEST_ROOT/${@: -1}.active" ] && exit 0
                exit 3 ;;
            node-manager.service) [ -f "$TEST_ROOT/active" ] ;;
            *) [ ! -f "$TEST_ROOT/radio-stopped" ] ;;
        esac ;;
    stop) rm -f "$TEST_ROOT/$2.active" ;;
    restart)
        if [ "$2" = node-manager.service ]; then
            "$TEST_SELECT" --service-start >/dev/null
            touch "$TEST_ROOT/active"
        fi ;;
esac
''')
        self.launches = 0
        for key, value in {
            'JOURNAL_DIR': str(self.journal_dir), 'ADMIN': self.admin,
            'PENDING_FILE': str(self.run_dir / 'mesh_pending_config.json'),
            'ACK_VERSION_FILE': str(self.run_dir / 'mesh_config_ack_version'),
            'APPLIED_VERSION_FILE': str(self.run_dir /
                                        'mesh_applied_config_version'),
            'publish_ack': lambda *args: True,
            'latest_config_package': lambda *args: None,
            'run': self.launch,
        }.items():
            mock = patch.object(sync, key, value)
            mock.start()
            self.addCleanup(mock.stop)

    def journal(self):
        """Reload disk state into a fresh controller for every attempt."""
        return ConfigJournal()

    def launch(self, args, **kwargs):
        """Model the independent unit while running the real shell worker."""
        self.assertEqual(args, sync.APPLY_COMMAND)
        self.launches += 1
        with self.journal().locked() as journal:
            journal.work()
        return subprocess.CompletedProcess(args, 0, '', '')

    def stage_ready(self):
        """Deliver, ACK and consume one activation through real replay state."""
        with self.journal().locked() as journal:
            journal.stage(self.package, self.message, self.admin)
            journal.acknowledge()
            self.assertTrue(journal.authorize(self.admin))

    def current(self):
        """Return the persisted job without sharing mutable controller state."""
        with self.journal().locked() as journal:
            return copy.deepcopy(journal.job)

    def complete_trial(self):
        """Resolve the original trial after successful local verification."""
        self.now += 301
        sync.sync_once('')
        self.assertEqual(self.current()['phase'], 'committed')

    def test_consumed_job_resumes_without_ciphertext_clock_or_password(self):
        self.package['config']['admin_password'] = 'new-password'
        self.stage_ready()
        (self.root / 'initial_time_synced').unlink()
        for path in self.run_dir.iterdir():
            path.unlink()
        self.conf.write_text(self.conf.read_text().replace(
            'admin_password=old-password', 'admin_password=new-password'))
        self.assertFalse(self.admin.accept(70, self.message))
        self.assertEqual(sync.sync_once(''), 0)
        self.assertEqual(self.current()['phase'], 'verified')
        self.assertIn('mesh_ssid=new', self.conf.read_text())
        self.complete_trial()

    def test_future_activation_requires_initial_clock_and_due_time(self):
        self.package['activate_at'] = int(time.time()) + 600
        with self.journal().locked() as journal:
            journal.stage(self.package, self.message, self.admin)
            journal.acknowledge()
            self.assertFalse(journal.authorize(self.admin))
            journal.job['package']['activate_at'] = 1
            journal.save()
        (self.root / 'initial_time_synced').unlink()
        self.assertEqual(sync.sync_once(''), 0)
        self.assertEqual(self.launches, 0)
        self.assertEqual(self.current()['phase'], 'staged')

    def test_failed_second_radio_write_repairs_equal_mesh_conf_on_retry(self):
        self.package['config']['admin_password'] = 'new-password'
        self.stage_ready()
        # Reproduce the older writer order: mesh.conf and wlan0 have changed.
        self.conf.write_text(self.conf.read_text().replace(
            'mesh_ssid=old', 'mesh_ssid=new'))
        first = self.wpa / 'wpa_supplicant-wlan0.conf'
        second = self.wpa / 'wpa_supplicant-wlan1.conf'
        first.write_text('network={\nssid="new"\n}\n')
        second.write_text('network={\nssid="old"\n}\n')
        writer = self.bin / 'writer.py'
        writer.write_text(
            'import os, runpy, sys\nfrom pathlib import Path\n'
            'if "wlan1" in sys.argv[1]:\n    raise SystemExit(1)\n'
            f'sys.path.insert(0, {str(TOOLS)!r})\n'
            f'runpy.run_path({str(TOOLS / "mesh-config-write.py")!r}, '
            'run_name="__main__")\n')
        with patch.dict(os.environ, MANET_CONFIG_WRITER=str(writer)):
            with self.assertRaises(RuntimeError):
                sync.sync_once('')
        original = self.current()['backup']
        self.assertIsNotNone(original)
        self.assertIn('admin_password=new-password', self.conf.read_text())
        self.assertIn('ssid="old"', second.read_text())
        sync.sync_once('')
        self.assertEqual(self.current()['backup'], original)
        self.assertIn('ssid="new"', second.read_text())
        self.assertEqual(self.current()['phase'], 'verified')
        self.assertIn('supplicant restart', self.calls.read_text())

    def test_rollback_decision_is_terminal_even_after_partial_restore(self):
        old = self.conf.read_text()
        self.stage_ready()
        sync.sync_once('')
        self.now += 301
        self.peer_query.return_value = 0
        failed = self.bin / 'failed.py'
        failed.write_text('raise SystemExit(1)\n')
        with patch.dict(os.environ, MANET_SUPPLICANT_HELPER=str(failed)):
            with self.assertRaises(RuntimeError):
                sync.sync_once('')
        self.assertEqual(self.current()['phase'], 'rolling_back')
        self.peer_query.return_value = 1
        sync.sync_once('')
        self.assertEqual(self.current()['phase'], 'rolled_back')
        self.assertEqual(self.conf.read_text(), old)
        fresh_message = Message(self.package, time.time_ns(), 'b' * 32)
        with self.journal().locked() as journal:
            self.assertFalse(journal.stage(self.package, fresh_message,
                                           self.admin))
        marker = self.run_dir / 'mesh_applied_config_version'
        self.assertFalse(marker.exists())

    def test_boot_change_does_not_reset_original_trial(self):
        self.stage_ready()
        sync.sync_once('')
        backup = self.current()['backup']
        self.boot = 'next-boot'
        self.now = 1
        self.peer_query.return_value = 0
        sync.sync_once('')
        self.assertEqual(self.current()['phase'], 'rolled_back')
        self.assertEqual(self.current()['backup'], backup)

    def test_new_work_cannot_replace_active_backup_or_transport_order(self):
        self.stage_ready()
        sync.sync_once('')
        prior = self.current()
        newer = dict(self.package, version='def456')
        message = Message(newer, time.time_ns(), 'b' * 32)
        with self.journal().locked() as journal:
            self.assertFalse(journal.stage(newer, message, self.admin))
        self.assertEqual(self.current(), prior)
        history = json.loads((self.root / 'admin/received-70.json').read_text())
        self.assertEqual(history['order'], self.message.order)

    def test_failed_baseline_or_snapshot_does_not_write_live_settings(self):
        self.stage_ready()
        old = self.conf.read_text()
        for target in ('peers', 'capture'):
            with self.subTest(target=target):
                with patch.object(ConfigJournal, target,
                                  side_effect=OSError('failure')):
                    with self.assertRaises(OSError):
                        sync.sync_once('')
                self.assertEqual(self.current()['phase'], 'prepared')
                self.assertEqual(self.conf.read_text(), old)
                self.assertIsNone(self.current()['backup'])
        sync.sync_once('')
        self.assertEqual(self.current()['phase'], 'verified')

    def test_verified_trial_checks_active_service_and_actual_files(self):
        self.stage_ready()
        sync.sync_once('')
        self.calls.unlink()
        (self.wpa / 'wpa_supplicant-wlan1.conf').write_text(
            'network={\nssid="old"\n}\n')
        sync.sync_once('')
        self.assertIn('supplicant restart', self.calls.read_text())
        (self.root / 'radio-stopped').touch()
        with self.assertRaises(RuntimeError):
            sync.sync_once('')
        self.assertEqual(self.current()['phase'], 'applying')
        (self.root / 'radio-stopped').unlink()
        sync.sync_once('')
        self.assertEqual(self.current()['phase'], 'verified')

    def test_disabled_service_is_stopped_when_mesh_conf_already_matches(self):
        self.package['config'] = {'mtx': 'n'}
        self.conf.write_text(self.conf.read_text().replace('mtx=y', 'mtx=n'))
        (self.root / 'mediamtx.active').touch()
        self.stage_ready()
        sync.sync_once('')
        self.assertFalse((self.root / 'mediamtx.active').exists())
        self.assertEqual(self.current()['phase'], 'committed')

    def test_death_around_consumption_and_unit_launch(self):
        for boundary in ('complete', 'launch'):
            for after in (False, True):
                with self.subTest(boundary=boundary, after=after):
                    self.reset_job()
                    real = (self.admin.complete if boundary == 'complete'
                            else self.launch)
                    def die(*args, **kwargs):
                        if after:
                            real(*args, **kwargs)
                        raise JournalDeath()
                    owner = self.admin if boundary == 'complete' else sync
                    name = 'complete' if boundary == 'complete' else 'run'
                    with patch.object(owner, name, side_effect=die):
                        with self.assertRaises(JournalDeath):
                            self.stage_ready()
                            sync.sync_once('')
                    (self.root / 'initial_time_synced').unlink(missing_ok=True)
                    sync.sync_once('')
                    self.assertEqual(self.current()['phase'], 'verified')
                    self.assertFalse(self.admin.accept(70, self.message))
                    (self.root / 'initial_time_synced').touch()

    def reset_job(self):
        """Create an independent transaction for each fault boundary."""
        import shutil
        shutil.rmtree(self.journal_dir, ignore_errors=True)
        shutil.rmtree(self.root / 'admin', ignore_errors=True)
        for path in self.run_dir.glob('mesh_*'):
            path.unlink()
        self.conf.write_text('mesh_ssid=old\nacs=n\nmtx=y\n'
                             'admin_password=old-password\n')
        for path in self.wpa.glob('*.conf'):
            path.write_text('network={\nssid="old"\n}\n')
        self.now = 1000
        self.boot = 'test-boot'

    def test_death_before_and_after_every_journal_write(self):
        # Cover staging, ACK, authorization, consumption, backup, worker
        # input, verification, projection, terminal outcome and cleanup.
        observed = []
        real = private_json_write
        def record(path, value):
            observed.append(str(path))
            real(path, value)
        with patch('manet_config_journal.private_json_write', record):
            self.stage_ready()
            sync.sync_once('')
            self.complete_trial()
        for index in range(len(observed)):
            for after in (False, True):
                with self.subTest(write=index, after=after):
                    self.reset_job()
                    count = 0
                    def die(path, value):
                        nonlocal count
                        selected = count == index
                        count += 1
                        if selected and not after:
                            raise JournalDeath()
                        real(path, value)
                        if selected:
                            raise JournalDeath()
                    with patch('manet_config_journal.private_json_write', die):
                        with self.assertRaises(JournalDeath):
                            self.stage_ready()
                            sync.sync_once('')
                            self.complete_trial()
                    job = self.current()
                    if job is None or job['phase'] == 'staged':
                        # No activation existed yet. Redelivery remains valid.
                        self.stage_ready()
                    if self.current()['phase'] != 'committed':
                        sync.sync_once('')
                        if self.current()['phase'] == 'verified':
                            self.complete_trial()
                    self.assertEqual(self.current()['phase'], 'committed')
                    self.assertIn('mesh_ssid=new', self.conf.read_text())
                    self.assertEqual(self.journal_dir.stat().st_mode & 0o777,
                                     0o700)
                    path = self.journal_dir / 'journal.json'
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_death_between_safe_verification_and_commit_resumes(self):
        self.package['config'] = {'admin_password': 'new-password'}
        self.stage_ready()
        with patch.object(ConfigJournal, 'finish', side_effect=JournalDeath):
            with self.assertRaises(JournalDeath):
                sync.sync_once('')
        self.assertEqual(self.current()['phase'], 'verified')
        (self.root / 'initial_time_synced').unlink()
        sync.sync_once('')
        self.assertEqual(self.current()['phase'], 'committed')

    def test_failed_password_write_or_radio_restart_recovers(self):
        for operation in ('password', 'radio'):
            for after in (False, True):
                with self.subTest(operation=operation, after=after):
                    self.reset_job()
                    self.package['config']['admin_password'] = 'new-password'
                    self.stage_ready()
                    wrapper = self.bin / 'interrupt.py'
                    script = (TOOLS / 'mesh-config-write.py' if
                              operation == 'password' else
                              self.bin / 'supplicant')
                    trigger = ('"admin_password" in sys.argv' if
                               operation == 'password' else 'True')
                    code = ('import runpy, sys\n'
                            f'sys.path.insert(0, {str(TOOLS)!r})\n')
                    if not after:
                        code += f'if {trigger}: raise SystemExit(1)\n'
                    # The writer exits normally via SystemExit(0).
                    code += ('try:\n'
                             f'    runpy.run_path({str(script)!r}, '
                             'run_name="__main__")\n'
                             'except SystemExit as exc:\n'
                             '    if exc.code: raise\n')
                    if after:
                        code += f'if {trigger}: raise SystemExit(1)\n'
                    wrapper.write_text(code)
                    variable = ('MANET_CONFIG_WRITER' if
                                operation == 'password' else
                                'MANET_SUPPLICANT_HELPER')
                    with patch.dict(os.environ, {variable: str(wrapper)}):
                        with self.assertRaises(RuntimeError):
                            sync.sync_once('')
                    before = self.current()['backup']
                    sync.sync_once('')
                    self.assertEqual(self.current()['phase'], 'verified')
                    self.assertEqual(self.current()['backup'], before)
                    self.assertIn('admin_password=new-password',
                                  self.conf.read_text())

    def test_durable_file_and_directory_fsync_precede_consumption(self):
        events = []
        fsync = os.fsync
        complete = self.admin.complete
        def record_sync(fd):
            events.append(os.readlink(f'/proc/self/fd/{fd}'))
            fsync(fd)
        def record_complete(*args):
            job = json.loads((self.journal_dir / 'journal.json').read_text())
            self.assertEqual(job['job']['phase'], 'prepared')
            self.assertEqual(events[-1], str(self.journal_dir))
            self.assertIn(str(self.journal_dir), events[-2])
            complete(*args)
        with patch('os.fsync', record_sync), patch.object(
                self.admin, 'complete', record_complete):
            self.stage_ready()

    def test_legacy_consumed_pending_survives_rotation_without_clock(self):
        self.admin.accept(70, self.message)
        self.admin.complete(70, self.message)
        pending = self.run_dir / 'mesh_pending_config.json'
        private_json_write(pending, self.package)
        (self.run_dir / 'mesh_config_ack_version').write_text('abc123')
        (self.root / 'initial_time_synced').unlink()
        with self.journal().locked() as journal:
            journal.import_pending(sync.validate_package, self.admin)
        self.assertEqual(self.current()['phase'], 'prepared')
        pending.unlink()
        sync.sync_once('')
        self.assertEqual(self.current()['phase'], 'verified')

    def test_new_order_before_replacement_write_cannot_activate_old_stage(self):
        self.package['activate_at'] = 0
        with self.journal().locked() as journal:
            journal.stage(self.package, self.message, self.admin)
            journal.acknowledge()
            newer = Message(dict(self.package, version='def456'),
                            self.message.sent_ns + 1, 'b' * 32)
            self.admin.accept(70, newer)
            journal.job['package']['activate_at'] = 1
            journal.save()
        sync.sync_once('')
        self.assertEqual(self.current()['phase'], 'staged')
        self.assertEqual(self.launches, 0)

    def test_cidr_verification_waits_for_allocator_and_live_address(self):
        self.package['config'] = {'ipv4_network': '10.50.0.0/24'}
        self.package['no_rollback'] = True
        state = self.root / 'ipv4-state'
        live = [{'addr_info': [{'local': '10.50.0.8', 'prefixlen': 24}]}]
        state.write_text('PERSISTENT_NETWORK="10.49.0.0/24"\n'
                         'PERSISTENT_IPV4="10.49.0.8"\n')
        self.stage_ready()
        with patch.dict(os.environ, MANET_IPV4_STATE=str(state)):
            with self.journal().locked() as journal:
                with patch.object(journal, 'execute',
                                  return_value=json.dumps(live)):
                    self.assertFalse(journal.services_match())
                    state.write_text('PERSISTENT_NETWORK="10.50.0.0/24"\n'
                                     'PERSISTENT_IPV4="10.50.0.8"\n')
                    self.assertTrue(journal.ipv4_matches('10.50.0.0/24'))
                    live[0]['addr_info'][0]['prefixlen'] = 16
                with patch.object(journal, 'execute',
                                  return_value=json.dumps(live)):
                    self.assertFalse(journal.ipv4_matches('10.50.0.0/24'))

    def test_region_verification_ignores_acs_but_checks_country(self):
        self.package['config'] = {'regulatory_domain': 'US'}
        with self.conf.open('a') as stream:
            stream.write('regulatory_domain=US\n')
        self.stage_ready()
        region = self.root / 'region'
        radio = region / 'etc/wpa_supplicant/wpa_supplicant-wlan0.conf'
        radio.parent.mkdir(parents=True)
        radio.write_text('country=US\nfrequency=2412\n')
        with patch.dict(os.environ, MANET_REGION_ROOT=str(region)):
            with self.journal().locked() as journal:
                journal.persist_region_files()
                self.assertTrue(journal.files_match())
                radio.write_text('country=US\nfrequency=2462\n')
                self.assertTrue(journal.files_match())
                radio.write_text('country=GB\nfrequency=2462\n')
                self.assertFalse(journal.files_match())

    def test_status_distinguishes_staged_applied_and_rolled_back(self):
        self.package['config']['admin_password'] = 'new-password'
        self.stage_ready()
        status = config_application_status()
        self.assertEqual(status['application_status'], 'prepared')
        self.assertIsNone(status['applied_version'])
        sync.sync_once('')
        status = config_application_status()
        self.assertEqual(status['applied_version'], 'abc123')
        self.assertTrue(status['rollback']['armed'])
        self.assertEqual(status['rollback']['seconds_left'], 300)
        self.assertNotIn('password', json.dumps(status))
        self.now += 301
        self.peer_query.return_value = 0
        sync.sync_once('')
        status = config_application_status()
        self.assertEqual(status['application_status'], 'rolled_back')
        self.assertIsNone(status['applied_version'])
        self.assertFalse(status['rollback']['armed'])

    def test_existing_manager_resumes_without_alfred_records(self):
        self.stage_ready()
        integration = Path(os.environ.get('MANET_INTEGRATION_TOOLS', TOOLS))
        helper = self.bin / 'receiver'
        helper.write_text('#!/bin/sh\n'
                          'echo called >> "$TEST_ROOT/received"\n'
                          'cat >/dev/null\n')
        helper.chmod(0o755)
        source = integration / 'manet-common.sh'
        script = f'. "{source}"\n' + r"""
timeout() { [ "$FAIL" != yes ] || return 1; printf '%s' "$RAW"; }
RAW=''; sync_alfred_command "$TEST_ROOT/bin/receiver" 70
FAIL=yes; sync_alfred_command "$TEST_ROOT/bin/receiver" 70
"""
        result = subprocess.run(['bash', '-c', script], capture_output=True,
                                text=True, timeout=10)
        received = self.root / 'received'
        calls = received.read_text().splitlines() if received.exists() else []
        self.assertEqual(calls, ['called', 'called'], result.stderr)

    def test_existing_status_reads_the_journal_trial(self):
        self.stage_ready()
        sync.sync_once('')
        integration = Path(os.environ.get('MANET_INTEGRATION_TOOLS', TOOLS))
        spec = importlib.util.spec_from_file_location(
            'journal_status_integration', integration / 'mesh-status.py')
        status = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(status)
        state = status.read_rollback_state()
        self.assertTrue(state['armed'])
        self.assertEqual(state['version'], self.package['version'])

    def test_receiver_defaults_follow_environment_after_module_import(self):
        """Unpatched defaults cannot fall back to the live node paths."""
        with patch.multiple(sync, JOURNAL_DIR=None, PENDING_FILE=None,
                            ACK_VERSION_FILE=None, APPLIED_VERSION_FILE=None):
            journal = sync.local_config_journal()
        self.assertEqual(journal.root, self.journal_dir)
        self.assertEqual(journal.pending,
                         self.run_dir / 'mesh_pending_config.json')
        self.assertEqual(journal.ack,
                         self.run_dir / 'mesh_config_ack_version')
        self.assertEqual(journal.applied,
                         self.run_dir / 'mesh_applied_config_version')
        self.assertEqual(journal.legacy, self.root / 'legacy')

    def test_newer_cancel_wins_before_first_scheduled_activation(self):
        with self.journal().locked() as journal:
            journal.stage(self.package, self.message, self.admin)
            journal.acknowledge()
        cancel = Message({'kind': 'mesh_config_cancel', 'version': 'abc123'},
                         self.message.sent_ns + 1, 'b' * 32)
        with patch.object(sync, 'latest_config_package', return_value=cancel):
            sync.sync_once('')
        self.assertEqual(self.current()['phase'], 'cancelled')
        self.assertEqual(self.launches, 0)

    def test_unknown_schema_and_corrupt_journal_fail_closed(self):
        self.stage_ready()
        for contents in ('{', '{"schema": 2}'):
            with self.subTest(contents=contents):
                (self.journal_dir / 'journal.json').write_text(contents)
                with self.assertRaises(ValueError):
                    sync.sync_once('')
        self.assertEqual(self.launches, 0)

    def test_lock_blocks_concurrent_delivery_and_worker(self):
        self.stage_ready()
        with self.journal().locked():
            self.assertEqual(sync.sync_once(''), 0)
            with self.assertRaises(BlockingIOError):
                with self.journal().locked():
                    self.fail('second worker acquired the lock')
        self.assertEqual(self.launches, 0)

    def test_legacy_pending_and_backup_migrate_before_volatile_loss(self):
        legacy = self.root / 'legacy'
        (legacy / 'wpa').mkdir(parents=True)
        old = self.conf.read_text()
        (legacy / 'mesh.conf').write_text(old)
        (legacy / 'wpa/wpa_supplicant-wlan0.conf').write_text(
            'network={\nssid="old"\n}\n')
        (legacy / 'state').write_text(
            "VERSION='abc123'\nPEERS_BEFORE=1\nBOOT_ID='test-boot'\n"
            'DEADLINE=1300\nREARMED=0\n')
        pending = self.run_dir / 'mesh_pending_config.json'
        private_json_write(pending, self.package)
        with self.journal().locked() as journal:
            journal.import_pending(sync.validate_package)
            self.assertEqual(journal.job['backup']['deadline'], 1300)
        self.assertFalse((legacy / 'state').exists())
        pending.unlink()
        (self.root / 'initial_time_synced').unlink()
        sync.sync_once('')
        self.assertEqual(self.current()['phase'], 'verified')
        self.now = 1301
        self.peer_query.return_value = 0
        sync.sync_once('')
        self.assertEqual(self.conf.read_text(), old)
        self.assertEqual(self.current()['phase'], 'rolled_back')


if __name__ == '__main__':
    unittest.main()
