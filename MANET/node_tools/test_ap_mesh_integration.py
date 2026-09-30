"""Shell entry points must use active roles throughout AP/mesh transitions."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import manet_eud_ap
import test_acs

TOOLS = Path(__file__).resolve().parent


class EntryPointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='manet-ap-role-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('mesh_if', 'mesh_24_if', 'mesh_5_if', 'no_mesh_if', 'halow_if'):
            (self.root / name).write_text('')

    def shell(self, source, **env):
        return subprocess.run(['bash', '-c', source], capture_output=True, text=True,
                              env=dict(os.environ, **env), timeout=10)

    def test_setup_records_original_band_and_resets_previous_selection(self):
        source = (TOOLS / 'radio-setup.sh').read_text().split(
            '# === AP INTERFACE SELECTION', 1)[1].split(
            '# === CLEANUP STALE PER-INTERFACE SERVICES', 1)[0]
        source = source[source.index('AP_INTERFACE=""'):].replace('/var/lib', str(self.root))
        for band in ('2.4', '5', ''):
            with self.subTest(band=band):
                for name in ('mesh_if', 'mesh_24_if', 'mesh_5_if', 'no_mesh_if'):
                    (self.root / name).write_text('')
                if band:
                    (self.root / 'mesh_if').write_text('candidate\n')
                    role = 'mesh_24_if' if band == '2.4' else 'mesh_5_if'
                    (self.root / role).write_text('candidate\n')
                else:
                    (self.root / 'no_mesh_if').write_text('candidate\n')
                result = self.shell('eud=auto\niw() { echo "wiphy 0 5180"; }\n' + source)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.root / 'ap_interface').read_text().strip(), 'candidate')
                self.assertEqual((self.root / 'ap_mesh_band').read_text().strip(), band)
                self.assertEqual((self.root / 'mesh_if').read_text(), '')
        self.assertEqual(self.shell('eud=wired\n' + source).returncode, 0)
        self.assertEqual((self.root / 'ap_interface').read_text(), '')
        self.assertEqual((self.root / 'ap_mesh_band').read_text(), '')

    def test_supplicant_guard_requires_mesh_membership_and_released_hostapd(self):
        (self.root / 'ap_interface').write_text('wlan1\n')
        source = (TOOLS / 'manet-ap-guard.sh').read_text()
        for membership, running, allowed in [('', False, False), ('wlan1\n', False, True),
                                               ('wlan1\n', True, False)]:
            with self.subTest(membership=membership, running=running):
                (self.root / 'mesh_if').write_text(membership)
                body = f'systemctl() {{ return {0 if running else 3}; }}\nset -- wlan1\n' + source
                result = self.shell(body, MANET_IFACE_STATE_DIR=str(self.root))
                self.assertEqual(result.returncode == 0, allowed)

    def test_batman_classifies_returned_candidate_by_active_role(self):
        source = (TOOLS / 'batman-if-setup.sh').read_text()
        function = re.search(r'^refresh_interfaces\(\) \{.*?^\}', source, re.M | re.S)[0]
        function = function.replace('/var/lib', str(self.root)).replace('/sys/class/net', str(self.root))
        (self.root / 'ap_interface').write_text('wlan1\n')
        (self.root / 'wlan1').mkdir()
        for members in ('wlan1\n', ''):
            (self.root / 'mesh_if').write_text(members)
            result = self.shell('iw() { exit 99; }\n' + function +
                                '\nrefresh_interfaces\necho "mesh=$STANDARD_MESH_INTERFACES"')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines()[-1].strip(), 'mesh=' + members.strip())

    def test_degraded_end0_with_carrier_does_not_trigger_unplug_cleanup(self):
        for directory in ('var/run', 'sys/class/net/end0'):
            (self.root / directory).mkdir(parents=True)
        (self.root / 'sys/class/net/end0/carrier').write_text('1\n')
        source = (TOOLS.parent / 'networkd-dispatcher/off').read_text()
        source = source.replace('/var/run/', str(self.root / 'var/run') + '/')
        source = source.replace('/sys/class/net/', str(self.root / 'sys/class/net') + '/')
        result = self.shell('ip() { exit 99; }; systemctl() { exit 99; }\n' + source, IFACE='end0')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_detector_can_explicitly_demote_a_no_internet_link_with_carrier(self):
        for directory in ('var/run', 'sys/class/net/end0', 'etc/systemd/network', 'var/lib'):
            (self.root / directory).mkdir(parents=True)
        (self.root / 'sys/class/net/end0/carrier').write_text('1\n')
        (self.root / 'etc/mesh.conf').write_text('eud=wired\n')
        state = self.root / 'var/run/ethernet_detection_state'
        state.write_text('ETH_MODE=GATEWAY\n')
        source = (TOOLS.parent / 'networkd-dispatcher/off').read_text()
        source = re.sub(r'/(?:etc|run|var|sys|usr/local/bin)/',
                        lambda m: str(self.root) + m[0], source)
        stubs = '''
ip() { echo "ip $*"; }
systemctl() { :; }
networkctl() { :; }
nft() { :; }
batctl() { :; }
systemd-cat() { cat >/dev/null; }
'''
        result = self.shell(stubs + source, IFACE='end0', MANET_ETH_FORCE_CLEANUP='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ip addr flush dev end0', result.stdout)
        self.assertFalse(state.exists())

    def test_editing_saved_ap_credentials_does_not_start_inactive_hostapd(self):
        with patch.dict(os.environ, MANET_AP_ROLE_LOCK=str(self.root / 'role.lock')), \
                patch.object(manet_eud_ap.subprocess, 'run', return_value=
                          subprocess.CompletedProcess([], 3)) as run:
            manet_eud_ap.restart_hostapd()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ['systemctl', 'is-active', '--quiet', 'hostapd.service'])

    def test_deferred_radio_transition_still_finishes_wired_eud_setup(self):
        source = (TOOLS / 'ethernet-autodetect.sh').read_text()
        body = source.split('elif [ "$DETECTED_MODE" == "wired-eud" ]; then', 1)[1]
        body = body.split('\nelse\n    log "ERROR: Unknown mode:', 1)[0]
        body = body.replace('/var/run/', str(self.root) + '/')
        body = body.replace('/etc/', str(self.root) + '/')
        manager = self.root / 'mesh-ip-manager.sh'
        manager.write_text('#!/bin/sh\necho configured-addresses\n'); manager.chmod(0o755)
        body = body.replace('/usr/local/bin/mesh-ip-manager.sh', str(manager))
        result = self.shell('''
log() { :; }
ip() { echo "master br0"; }
python3() { return 1; }
systemctl() { :; }
cp() { :; }
nft() { echo cleared-nat; }
batctl() { echo "batctl $*"; }
EUD_MODE=auto
AP_INTERFACE=wlan1
ETH_IFACE=end0
ACTIVE_CONFIG=""
''' + body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ETH_MODE=WIRED_EUD', (self.root / 'ethernet_detection_state').read_text())
        self.assertIn('batctl gw_mode client', result.stdout)
        self.assertIn('cleared-nat', result.stdout)
        self.assertIn('configured-addresses', result.stdout)


class AcsOwnershipTests(test_acs.AcsHarness):
    def test_withdrawn_band_is_neither_written_nor_restarted_from_cached_roles(self):
        self.configure(2437, 5200)
        result = self.shell(self.definitions('node-manager-acs.sh') + '''
: > "$MANET_IFACE_STATE_DIR/mesh_5_if"
acs_write_channels 2462 5220 data all
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('frequency=5200', (self.wpa / 'wpa_supplicant-wlan1.conf').read_text())
        self.assertNotIn('wpa_supplicant@wlan1', (self.root / 'commands').read_text())

    def test_activation_holds_channel_lock(self):
        self.command('systemctl', '''
import fcntl
with open(os.environ['MANET_ACS_LOCK_FILE'], 'a') as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('protected restart')
    else:
        sys.exit(99)
''')
        result = self.shell(self.definitions('node-manager-acs.sh') +
                            '\nacs_write_channels 2437 5200 data all\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('protected restart'), 2)


if __name__ == '__main__':
    unittest.main()
