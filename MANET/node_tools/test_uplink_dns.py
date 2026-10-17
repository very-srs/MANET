"""Check DNS qualification, bound probes and recoverable networkd migration."""
import contextlib
import importlib.util
import io
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

TOOLS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    'uplink_dns', TOOLS / 'manet-uplink-dns.py')
dns = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dns)


def wire_name(name):
    """Encode one uncompressed name for test DNS packets."""
    labels = (bytes([len(p)]) + p.encode() for p in name.split('.'))
    return b''.join(labels) + b'\0'


def response(ident=123, host='cp.cloudflare.com', truncated=False):
    """Build a valid response with a compressed A-record owner."""
    flags = 0x8380 if truncated else 0x8180
    header = struct.pack('!6H', ident, flags, 1, 0 if truncated else 1, 0, 0)
    question = wire_name(host) + b'\0\x01\0\x01'
    answer = b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 30, 4)
    return header + question + (b'' if truncated else
                               answer + socket.inet_aton('104.16.132.229'))


class UplinkDnsTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.command = Mock()
        self.write('run/upstream_iface', 'end0\n')
        self.add_lease('end0', 2)
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stderr(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def write(self, name, text):
        """Create state beneath the isolated node root."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def add_lease(self, iface, index, servers='192.168.69.1'):
        """Give a live candidate a DHCP address and resolver list."""
        self.write(f'sys/class/net/{iface}/ifindex', str(index))
        self.write(f'sys/class/net/{iface}/carrier', '1\n')
        return self.write(f'run/systemd/netif/leases/{index}',
                          'ADDRESS=192.168.69.100\nDNS=' + servers + '\n'
                          'DOMAINNAME=lan.example\n'
                          'DOMAIN_SEARCH_LIST=lab.example lan.example\n')

    def promote(self):
        """Publish the same gateway marker used by the dispatcher."""
        self.write('run/manet-uplink/mesh-gateway.state', '')

    def applied(self):
        """Read the runtime configuration visible to resolved."""
        path = self.root / 'run/systemd/resolved.conf.d/80-manet-uplink.conf'
        return path.read_text()

    def test_unqualified_lease_cannot_capture_dns(self):
        dns.reconcile_dns(self.root, self.command)
        self.assertNotIn('192.168.69.1', self.applied())
        self.assertIn('DNS=\nDomains=\n', self.applied())
        self.command.assert_called_once_with([
            'systemctl', 'reload-or-restart', 'systemd-resolved.service'])

    def test_promoted_private_dns_then_demotion_and_quiet_reconcile(self):
        self.promote()
        dns.reconcile_dns(self.root, self.command)
        self.assertIn('DNS=192.168.69.1\n', self.applied())
        self.assertNotIn('%', self.applied())
        self.assertIn('Domains=lab.example lan.example\n', self.applied())
        (self.root / 'run/manet-uplink/mesh-gateway.state').unlink()
        dns.reconcile_dns(self.root, self.command)
        self.assertNotIn('192.168.69.1', self.applied())
        dns.reconcile_dns(self.root, self.command)
        self.assertEqual(self.command.call_count, 2)
        self.assertEqual(len(self.output.getvalue().splitlines()), 2)

    def test_reconcile_replaces_previously_applied_scoped_dns(self):
        """Repair scoped DNS syntax even with unchanged gateway state."""
        self.promote()
        self.add_lease('end0', 2, '8.8.8.8 1.1.1.1')
        old = ('# Managed by manet-uplink-dns.py\n[Resolve]\nDNS=\nDomains=\n'
               'DNS=8.8.8.8%end0 1.1.1.1%end0\n'
               'Domains=lab.example lan.example\n')
        self.write('run/systemd/resolved.conf.d/80-manet-uplink.conf', old)
        self.write('run/manet-uplink-dns/applied', old)
        dns.reconcile_dns(self.root, self.command)
        self.assertIn('DNS=8.8.8.8 1.1.1.1\n', self.applied())
        self.assertNotIn('%', self.applied())
        self.command.assert_called_once_with([
            'systemctl', 'reload-or-restart', 'systemd-resolved.service'])
        dns.reconcile_dns(self.root, self.command)
        self.assertEqual(self.command.call_count, 1)

    def test_path_watches_qualification_and_leases_without_link_churn(self):
        """Avoid Python starts for networkd's routine link-state rewrites."""
        path = TOOLS.parent / 'systemd/manet-uplink-dns.path'
        lines = path.read_text().splitlines()
        watched = {line.partition('=')[2] for line in lines
                   if line.startswith('PathChanged=')}
        self.assertEqual(watched, {'/run/manet-uplink/mesh-gateway.state',
                                   '/run/upstream_iface',
                                   '/run/systemd/netif/leases'})

    def test_renewal_and_switch_do_not_reload_networkd(self):
        self.promote()
        dns.reconcile_dns(self.root, self.command)
        self.add_lease('end0', 2, '192.168.69.2')
        dns.reconcile_dns(self.root, self.command)
        self.assertIn('DNS=192.168.69.2\n', self.applied())
        self.add_lease('usb0', 3, '192.168.42.1')
        self.write('run/upstream_iface', 'usb0\n')
        dns.reconcile_dns(self.root, self.command)
        self.assertIn('DNS=192.168.42.1\n', self.applied())
        self.assertNotIn('192.168.69.', self.applied())
        self.assertTrue(all(c.args[0][0] == 'systemctl'
                            for c in self.command.call_args_list))

    def test_missing_lease_carrier_bridge_and_invalid_iface_withdraw_dns(self):
        self.promote()
        for reason in ('lease', 'carrier', 'bridge', 'name'):
            with self.subTest(reason=reason):
                path = self.add_lease('end0', 2)
                self.write('run/upstream_iface', 'end0\n')
                if reason == 'lease':
                    path.unlink()
                elif reason == 'carrier':
                    self.write('sys/class/net/end0/carrier', '0\n')
                elif reason == 'bridge':
                    self.write('sys/class/net/end0/master', '')
                else:
                    self.write('run/upstream_iface', '../../etc/secret\n')
                wanted, iface = dns.resolver_config(self.root)
                self.assertNotIn('192.168.69.1', wanted)
                self.assertEqual(iface, '')
                master = self.root / 'sys/class/net/end0/master'
                master.unlink(missing_ok=True)

    def test_untrusted_lease_fields_are_filtered(self):
        self.promote()
        self.write('run/systemd/netif/leases/2',
                   'ADDRESS=192.168.69.100\n'
                   'DNS=127.0.0.1 ::1 0.0.0.0 224.0.0.1 192.168.69.1 '
                   '192.168.69.1 8.8.8.8%end1\n'
                   'DOMAINNAME=~.\nDOMAIN_SEARCH_LIST=good.example [Resolve]\n')
        wanted, _ = dns.resolver_config(self.root)
        self.assertIn('DNS=192.168.69.1\n', wanted)
        self.assertIn('Domains=good.example\n', wanted)
        self.assertNotIn('~.', wanted)
        self.assertEqual(wanted.count('[Resolve]'), 1)

    def test_admin_down_sysfs_error_withdraws_dns(self):
        """Carrier reads can fail instead of returning zero on a down link."""
        self.promote()
        original = dns.read_dns_text

        def unavailable_carrier(path):
            """Emulate the sysfs carrier attribute on an admin-down device."""
            if path.name == 'carrier':
                raise OSError(22, 'Invalid argument')
            return original(path)

        with patch.object(dns, 'read_dns_text', unavailable_carrier):
            wanted, iface = dns.resolver_config(self.root)
        self.assertEqual(iface, '')
        self.assertNotIn('192.168.69.1', wanted)

    def test_failed_resolved_reload_is_retried_without_file_changes(self):
        self.command.side_effect = subprocess.CalledProcessError(1, 'systemctl')
        with self.assertRaises(subprocess.CalledProcessError):
            dns.reconcile_dns(self.root, self.command)
        self.command.side_effect = None
        dns.reconcile_dns(self.root, self.command)
        self.assertEqual(self.command.call_count, 2)

    def test_migration_preserves_identity_backups_and_unrelated_files(self):
        original = ('[Match]\nMACAddress=aa:bb:cc:dd:ee:ff\n\n[Network]\n'
                    'DHCP=yes\nIPv6AcceptRA=no\n\n[DHCPv4]\nUseDomains=true\n')
        names = ('10-end0.network', '10-end0.network.dhcp-default',
                 '20-usb0.network', '20-wlan2-usb-wifi-uplink.network',
                 '20-end0-gateway.network.off')
        for name in names:
            self.write('etc/systemd/network/' + name, original)
        unrelated = self.write('etc/systemd/network/99-custom.network',
                               original)
        bridge = self.write('etc/systemd/network/20-end0-eud.network.off',
                            '[Network]\nBridge=br0\n')
        dns.install(self.root, self.command)
        self.assertEqual(self.command.call_args_list[0].args[0],
                         ['networkctl', 'reload'])
        for name in names:
            text = (self.root / 'etc/systemd/network' / name).read_text()
            self.assertTrue(text.startswith(original))
            self.assertTrue(text.endswith(dns.POLICY))
        self.assertEqual(unrelated.read_text(), original)
        self.assertEqual(bridge.read_text(), '[Network]\nBridge=br0\n')
        self.command.reset_mock()
        dns.install(self.root, self.command)
        self.command.assert_not_called()

    def test_interrupted_migration_retries_reload_before_clearing_pending(self):
        self.write('etc/systemd/network/10-end0.network',
                   '[Network]\nDHCP=yes\n')
        self.command.side_effect = subprocess.CalledProcessError(
            1, 'networkctl')
        with self.assertRaises(subprocess.CalledProcessError):
            dns.install(self.root, self.command)
        pending = self.root / 'var/lib/manet-uplink-dns/network-reload-needed'
        self.assertTrue(pending.exists())
        self.command.reset_mock(side_effect=True)
        dns.install(self.root, self.command)
        self.assertEqual(self.command.call_args_list[0].args[0],
                         ['networkctl', 'reload'])
        self.assertFalse(pending.exists())

    def test_network_generators_embed_same_policy(self):
        for relative in ('node_tools/manet-uplink-dispatch.sh',
                         'node_tools/usb-ethernet-watch.sh',
                         'node_tools/usb-wifi-uplink.sh',
                         'systemd-network/20-end0-gateway.network.off',
                         'provisioning/firstrun.sh.template',
                         'provisioning/rock3a-provision.sh.template'):
            with self.subTest(relative=relative):
                text = (TOOLS.parent / relative).read_text()
                text = '\n'.join(line.lstrip('\t')
                                 for line in text.splitlines())
                self.assertIn(dns.POLICY.rstrip(), text)
                self.assertNotIn('UseDNS=yes', text)
                self.assertNotIn('UseDomains=true', text)

    def test_valid_dns_cname_and_rejection_of_unrelated_answer(self):
        host = 'cp.cloudflare.com'
        packet = response()
        self.assertEqual(dns.dns_reply(packet, 123, host), ['104.16.132.229'])
        for ident, question in ((124, host), (123, 'unrelated.example')):
            with self.assertRaises(ValueError):
                dns.dns_reply(packet, ident, question)
        alias = wire_name('alias.example')
        question = wire_name(host) + b'\0\x01\0\x01'
        cname = b'\xc0\x0c' + struct.pack('!HHIH', 5, 1, 60, len(alias)) + alias
        record = alias + struct.pack('!HHIH', 1, 1, 60, 4)
        packet = (struct.pack('!6H', 123, 0x8180, 1, 2, 0, 0) + question +
                  cname + record + socket.inet_aton('104.16.132.229'))
        self.assertEqual(dns.dns_reply(packet, 123, host), ['104.16.132.229'])
        with self.assertRaises(ValueError):
            dns.dns_name(b'\xc0\0', 0)
        with self.assertRaises((ValueError, struct.error)):
            dns.dns_reply(packet[:-1], 123, host)

    def test_lone_candidate_bound_lookup_does_not_need_global_dns(self):
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        sock.recv.return_value = response()
        with patch.object(dns.socket, 'socket', return_value=sock) as factory, \
                patch.object(dns.secrets, 'randbits', return_value=123):
            self.assertEqual(
                dns.resolve(self.root, 'end0', 'cp.cloudflare.com'),
                ['104.16.132.229'])
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt.assert_called_once_with(
            socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b'end0\0')
        sock.connect.assert_called_once_with(('192.168.69.1', 53))
        self.assertIn(wire_name('cp.cloudflare.com'),
                      sock.send.call_args.args[0])
        self.assertFalse((self.root / 'run/manet-uplink/mesh-gateway.state').exists())
        self.assertFalse((self.root / 'run/systemd/resolved.conf.d').exists())

    def test_truncated_dns_retries_tcp_on_same_device(self):
        udp, tcp = Mock(), Mock()
        for sock in (udp, tcp):
            sock.__enter__ = Mock(return_value=sock)
            sock.__exit__ = Mock(return_value=False)
        udp.recv.return_value = response(truncated=True)
        packet = response()
        tcp.recv.side_effect = [struct.pack('!H', len(packet)), packet]
        with patch.object(dns.socket, 'socket', side_effect=[udp, tcp]), \
                patch.object(dns.secrets, 'randbits', return_value=123):
            self.assertEqual(
                dns.resolve(self.root, 'end0', 'cp.cloudflare.com'),
                ['104.16.132.229'])
        tcp.setsockopt.assert_called_once_with(
            socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b'end0\0')
        tcp.connect.assert_called_once_with(('192.168.69.1', 53))

    def test_dns_failure_does_not_fall_back_to_an_unbound_lookup(self):
        with patch.object(dns.socket, 'socket', side_effect=OSError('blocked')):
            self.assertEqual(
                dns.resolve(self.root, 'end0', 'cp.cloudflare.com'), [])

    def test_tcp_partial_reads_cannot_extend_query_budget(self):
        """Do not give each fragment a fresh four-second timeout."""
        sock = Mock()
        sock.recv.return_value = b'a'
        with patch.object(dns.time, 'monotonic', side_effect=[2.0, 3.0, 4.0]):
            with self.assertRaises(TimeoutError):
                dns.receive(sock, 3, 4.0)
        sock.settimeout.assert_called_once_with(2.0)
        self.assertEqual(sock.recv.call_count, 1)


if __name__ == '__main__':
    unittest.main()
