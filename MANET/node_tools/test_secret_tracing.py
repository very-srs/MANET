#!/usr/bin/env python3
"""Provisioning scripts trace every command (set -x) into logs that persist,
one of them on the FAT boot partition. Keys and passwords must never be on a
traced line."""

from pathlib import Path
import re
import subprocess
import tempfile
import unittest

MANET = Path(__file__).resolve().parents[1]
SCRIPTS = [MANET / 'node_tools/radio-setup.sh',
           MANET / 'provisioning/firstrun.sh.template',
           MANET / 'provisioning/rock3a-provision.sh.template']
SECRET = re.compile(r'__(MESH_SAE_KEY|LAN_AP_KEY|ADMIN_PW|RADIO_PW)__|\$\{?(mesh_key|KEY|LAN_AP_KEY|'
                    r'new_root_password|new_user_password|radio_password|value)\b|'
                    r'\b(KEY|LAN_AP_KEY)="?\$')
HEREDOC = re.compile(r"<<-?\s*'?\"?(\w+)")


def traced_secret_lines(text, offset=0):
    """Lines that use a secret while xtrace is on. A heredoc body is not
    traced as part of its own script, only the command that opens it, but it
    may be a script itself (firstrun writes provision-mesh.sh), so it is
    scanned as one, starting untraced."""
    tracing, heredoc_end, body, found = False, None, [], []
    for number, line in enumerate(text.splitlines(), 1):
        if heredoc_end:
            if line.strip() == heredoc_end:
                found += traced_secret_lines('\n'.join(body), offset + number - len(body) - 1)
                heredoc_end, body = None, []
            else:
                body.append(line)
            continue
        code = line.split('#', 1)[0] if not line.lstrip().startswith('#') else ''
        if re.search(r'\bset -x\b', code):
            tracing = True
        elif re.search(r'\bset \+x\b', code):
            tracing = False
        elif tracing and SECRET.search(code):
            found.append(f'{offset + number}: {line.strip()}')
        match = HEREDOC.search(code)
        if match:
            heredoc_end = match[1]
    return found


class SecretTracingTests(unittest.TestCase):
    def test_no_secret_is_used_while_tracing(self):
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                self.assertEqual(traced_secret_lines(script.read_text()), [])

    def test_scanner_catches_a_traced_secret(self):
        self.assertTrue(traced_secret_lines('set -x\necho "root:$new_root_password" | chpasswd\n'))
        self.assertFalse(traced_secret_lines('set -x\ncat > f <<EOF\nsae_password="$KEY"\nEOF\n'))
        embedded = "cat > p.sh << 'X'\nset -x\necho \"mesh_key=__MESH_SAE_KEY__\"\nX\n"
        self.assertEqual(traced_secret_lines(embedded), ['3: echo "mesh_key=__MESH_SAE_KEY__"'])

    def test_config_loading_trace_omits_values(self):
        # Run radio-setup.sh's own mesh.conf loop under bash -x.
        source = (MANET / 'node_tools/radio-setup.sh').read_text()
        start = source.index('# This loop reads the stored setup variables')
        end = source.index('done < <(cat /etc/mesh.conf)\nset -x\n') + len('done < <(cat /etc/mesh.conf)\nset -x\n')
        with tempfile.TemporaryDirectory() as scratch:
            conf = Path(scratch) / 'mesh.conf'
            conf.write_text('mesh_key=SECRETMESHKEY\nadmin_password=SECRETADMIN\nmesh_ssid=MESH_T\n')
            snippet = 'set -x\n' + source[start:end].replace('/etc/mesh.conf', str(conf)) + \
                'echo "loaded ${mesh_ssid}"\n'
            result = subprocess.run(['bash', '-c', snippet], capture_output=True, text=True, timeout=10)
        self.assertIn('loaded MESH_T', result.stdout)
        self.assertIn('+ echo', result.stderr)
        self.assertNotIn('SECRET', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
