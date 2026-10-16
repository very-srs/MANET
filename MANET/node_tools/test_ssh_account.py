"""Flash-time SSH account propagation, with no real users or disks modified."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent
PROVISIONING = TOOLS.parent / 'provisioning'
PWSH = shutil.which('pwsh')


def bash_function(name):
    source = (PROVISIONING / 'linux-flasher.sh').read_text()
    return re.search(r'^' + name + r'\(\) \{\n.*?^\}\n', source, re.M | re.S)[0]


class SshAccountTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='manet-ssh-account-')
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.conf = self.root / 'mesh.conf'
        self.env = dict(os.environ, MANET_TOOLS_DIR=str(TOOLS),
                        MANET_MESH_CONF=str(self.conf))

    def shell(self, script, **env):
        return subprocess.run(['bash', '-c', script], cwd=self.root,
                              env=dict(self.env, **env), text=True,
                              capture_output=True, timeout=15)

    def test_selected_account_uses_passwd_home_and_primary_group(self):
        self.conf.write_text('ssh_user=field_ops-2\n')
        result = self.shell('''
getent() { printf '%s:x:1001:1002::/srv/operator:/bin/bash\n' "$2"; }
. "$MANET_TOOLS_DIR/manet-common.sh"
manet_load_ssh_account || exit
printf '%s\n' "$MANET_SSH_USER" "$MANET_SSH_HOME" "$MANET_SSH_GROUP"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['field_ops-2', '/srv/operator', '1002'])

    def test_missing_setting_defaults_to_radio_but_invalid_setting_fails(self):
        for content, expected in [('', 0), ('ssh_user=root\n', 1),
                                  ('ssh_user=nobody\n', 1), ('ssh_user=\n', 1),
                                  ('ssh_user=bad/name\n', 1), ('ssh_user=$(id)\n', 1)]:
            with self.subTest(content=content):
                self.conf.write_text(content)
                result = self.shell('''
getent() { return 2; }
. "$MANET_TOOLS_DIR/manet-common.sh"
manet_load_ssh_account || exit
printf '%s\n' "$MANET_SSH_USER" "$MANET_SSH_HOME"
''')
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected == 0:
                    self.assertEqual(result.stdout.splitlines(), ['radio', '/home/radio'])

    def test_existing_system_account_is_refused(self):
        self.conf.write_text('ssh_user=daemon\n')
        result = self.shell('''
getent() { echo daemon:x:1:1::/usr/sbin:/usr/sbin/nologin; }
. "$MANET_TOOLS_DIR/manet-common.sh"
manet_load_ssh_account
''')
        self.assertNotEqual(result.returncode, 0)

    def test_linux_save_load_and_legacy_default(self):
        source = ''.join(bash_function(n) for n in (
            'valid_ssh_username', 'validate_ssh_username', 'save_config', 'load_config'))
        result = self.shell(source + '''
CONFIG_DIR=.
HALOW_REGULATORY_DOMAIN=US
RADIO_USER=field_ops-2
RADIO_PW=sample-password
save_config <<'ANSWERS'
y
custom
ANSWERS
RADIO_USER=wrong
load_config ./custom.conf
printf 'RESULT=%s\n' "$RADIO_USER"
printf 'RADIO_PW="old-password"\n' > legacy.conf
load_config ./legacy.conf
printf 'LEGACY=%s\n' "$RADIO_USER"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('RADIO_USER="field_ops-2"', (self.root / 'custom.conf').read_text())
        self.assertIn('RESULT=field_ops-2', result.stdout)
        self.assertIn('LEGACY=radio', result.stdout)

    def test_pi_first_boot_creates_selected_account_and_sets_password(self):
        source = (PROVISIONING / 'firstrun.sh.template').read_text()
        source = source[source.index('# Create the selected SSH user'):source.index('# Allow several client keys')]
        for name in ('radio', 'field_ops-2'):
            with self.subTest(name=name):
                script = source.replace('__RADIO_USER__', name).replace('__RADIO_PW__', 'sample-password')
                stubs = '''
id() { if [ "$1" = -u ]; then return 1; else echo "$2"; fi; }
useradd() { printf 'CREATE %s\n' "$*"; }
chpasswd() { while read -r line; do printf 'PASSWORD %s\n' "$line"; done; }
getent() { printf '%s:x:1000:1000::/home/%s:/bin/bash\n' "$2" "$2"; }
mkdir() { printf 'MKDIR %s\n' "$*"; }
chmod() { :; }
chown() { printf 'CHOWN %s\n' "$*"; }
'''
                result = self.shell(stubs + script)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'PASSWORD {name}:sample-password', result.stdout)
                self.assertIn(f'/home/{name}/.ssh', result.stdout)
                self.assertIn(f' {name}\n', result.stdout)
                self.assertNotIn('sample-password', result.stderr)

    def test_linux_rock3a_account_files_and_collision(self):
        etc = self.root / 'etc'
        (etc / 'sudoers.d').mkdir(parents=True)
        (etc / 'passwd').write_text('root:x:0:0:root:/root:/bin/bash\n')
        (etc / 'group').write_text('root:x:0:\nsudo:x:27:existing\n')
        (etc / 'shadow').write_text('root:!:19700:0:99999:7:::\n')
        source = ''.join(bash_function(n) for n in (
            'valid_ssh_username', 'validate_ssh_username', 'create_rock3a_ssh_account'))
        result = self.shell(source + '''
sudo() { "$@"; }
chown() { :; }
openssl() { echo test-hash; }
RADIO_USER=field_ops-2
RADIO_PW=sample-password
create_rock3a_ssh_account "$PWD" || exit
if create_rock3a_ssh_account "$PWD"; then exit 5; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('field_ops-2:x:1000:1000:field_ops-2:/home/field_ops-2:/bin/bash', (etc / 'passwd').read_text())
        self.assertIn('sudo:x:27:existing,field_ops-2', (etc / 'group').read_text())
        self.assertIn('field_ops-2:test-hash:', (etc / 'shadow').read_text())
        self.assertEqual((etc / 'passwd').read_text().count('field_ops-2:x:'), 1)
        self.assertTrue((self.root / 'home/field_ops-2').is_dir())

    @unittest.skipUnless(PWSH, 'PowerShell is required')
    def test_windows_config_and_receipt_use_selected_username(self):
        script = self.root / 'check.ps1'
        script.write_text(r'''
$ErrorActionPreference = 'Stop'
foreach ($file in @('windows.ps1', 'manet-flasher.ps1')) {
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $env:TEST_PROVISIONING $file), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ($errors | Out-String) }
    foreach ($name in @('Test-SshUsername', 'Save-Config', 'Load-Config', 'Build-MeshConf', 'Get-ReceiptText', 'Initialize-Rock3aSshAccount')) {
        $function = $ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true)
        if ($function) { Invoke-Expression $function.Extent.Text }
    }
}
foreach ($name in @('radio', 'field_ops-2', '_operator')) {
    if (-not (Test-SshUsername $name)) { throw "Rejected $name" }
}
foreach ($name in @('root', 'nobody', '', 'FieldOps', 'bad/name', '-bad', ('a' * 33))) {
    if (Test-SshUsername $name) { throw "Accepted $name" }
}
$CONFIG_DIR = $env:TEST_WORK
$Script:RADIO_USER = 'field_ops-2'
$Script:RADIO_PW = 'sample-password'
$Script:HALOW_REGULATORY_DOMAIN = 'US'
$Script:answers = [Collections.Generic.Queue[string]]::new()
$Script:answers.Enqueue('y'); $Script:answers.Enqueue('custom')
function Read-Host { param($Prompt); return $Script:answers.Dequeue() }
Save-Config
$Script:RADIO_USER = 'wrong'
Load-Config (Join-Path $CONFIG_DIR 'custom.conf')
if ($Script:RADIO_USER -cne 'field_ops-2') { throw 'Username was not restored' }
if ((Build-MeshConf) -notmatch '(?m)^ssh_user=field_ops-2$') { throw 'Missing node setting' }
$Script:HARDWARE_MODEL = 'cm4'
$Script:BoardLabels = @{ cm4 = 'CM4' }
if ((Get-ReceiptText) -notmatch 'SSH username\s+field_ops-2') { throw 'Wrong receipt username' }
$etc = Join-Path $CONFIG_DIR 'etc'
New-Item -ItemType Directory $etc | Out-Null
Set-Content (Join-Path $etc 'passwd') 'root:x:0:0:root:/root:/bin/bash'
Set-Content (Join-Path $etc 'group') "root:x:0:`nsudo:x:27:existing"
Set-Content (Join-Path $etc 'shadow') 'root:!:19700:0:99999:7:::'
Initialize-Rock3aSshAccount $CONFIG_DIR 'test-hash'
if ((Get-Content (Join-Path $etc 'passwd') -Raw) -notmatch 'field_ops-2:x:1000:1000:field_ops-2:/home/field_ops-2:/bin/bash') { throw 'Incorrect passwd entry' }
if ((Get-Content (Join-Path $etc 'group') -Raw) -notmatch 'sudo:x:27:existing,field_ops-2') { throw 'Incorrect sudo group' }
if ((Get-Content (Join-Path $etc 'shadow') -Raw) -notmatch 'field_ops-2:test-hash:') { throw 'Incorrect shadow entry' }
$collision = $false
try { Initialize-Rock3aSshAccount $CONFIG_DIR 'test-hash' } catch { $collision = $true }
if (-not $collision) { throw 'Account collision accepted' }
Set-Content (Join-Path $CONFIG_DIR 'legacy.conf') 'RADIO_PW="old-password"'
Load-Config (Join-Path $CONFIG_DIR 'legacy.conf')
if ($Script:RADIO_USER -cne 'radio') { throw 'Old config retained previous custom username' }
''')
        result = subprocess.run([PWSH, '-NoProfile', '-File', str(script)],
                                env=dict(self.env, TEST_PROVISIONING=str(PROVISIONING),
                                         TEST_WORK=str(self.root)),
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
