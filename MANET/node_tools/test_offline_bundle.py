"""Check the offline bundle's inputs, its release checks, and its manifest record."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest

from manet_release import OFFLINE_BUNDLES, PACKAGES, ReleaseError, validate_manifest

ROOT = Path(__file__).resolve().parents[2]
PROVISIONING = ROOT / 'MANET/provisioning'
IMAGE = ('https://downloads.raspberrypi.com/raspios_lite_arm64/images/'
         'raspios_lite_arm64-2025-10-02/2025-10-01-raspios-trixie-arm64-lite.img.xz')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = load('build_offline_bundle', ROOT / 'MANET/packaging/build-offline-bundle.py')
PUBLISH = load('publish', ROOT / 'MANET/releases/publish.py')


def manifest(**extra):
    value = {'schema': 1, 'version': '0.551', 'tag': 'v0.551', 'commit': 'a' * 40,
             'assets': {name: {'size': 1, 'sha256': 'b' * 64} for name in PACKAGES}}
    value.update(extra)
    return value


class OfflineBundleInputTests(unittest.TestCase):
    def test_bundle_covers_every_provisioning_install(self):
        """The bundle downloads what first boot and radio-setup install, in order."""
        steps = BUILDER.install_steps()
        self.assertEqual([recommends for recommends, _ in steps], [False, True, False, False])
        first, mumble, avahi, gps = (names for _, names in steps)
        for name in ('chrony', 'hostapd', 'syncthing', 'gstreamer1.0-plugins-good', 'gpsd'):
            self.assertIn(name, first)
        self.assertEqual(mumble, ['mumble-server'])
        self.assertEqual(avahi[0], 'avahi-daemon')
        self.assertIn('python3-cryptography', avahi)
        self.assertEqual(gps, ['gpsd', 'gpsd-tools'])
        for names in (first, mumble, avahi, gps):
            self.assertFalse([n for n in names if n.startswith(('-', '$', '"'))])
        # A further install site must be added to the bundle, or the build stops.
        firstrun = BUILDER.shell_lines((PROVISIONING / 'firstrun.sh.template').read_text())
        sites = [line for line in firstrun
                 if re.match(r'\s*(?:DEBIAN_FRONTEND=\S+ )?apt(?:-get)? install\s', line)]
        self.assertEqual(len(sites), 2)

    def test_builder_reads_the_flashers_image_and_template_pins(self):
        self.assertEqual(BUILDER.image_url(), IMAGE)
        template = (PROVISIONING / 'firstrun.sh.template').read_text()
        for key, value in BUILDER.pins().items():
            self.assertIn(f"{key}='{value}'", template)

    def test_apt_names_skip_options(self):
        self.assertEqual(BUILDER.apt_names(['-y', '-o', 'Dpkg::Options::=--force-confold', 'a', 'b']),
                         (True, ['a', 'b']))
        self.assertEqual(BUILDER.apt_names(['--no-install-recommends', 'c', '"$@"']), (False, ['c', '"$@"']))


class OfflineBundleReleaseTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='manet-offline-')
        self.addCleanup(scratch.cleanup)
        self.work = Path(scratch.name)
        self.yq = b'yq binary'
        self.mediamtx = b'mediamtx archive'
        self.version = b'0.551\n11/2026\n'
        template = ("YQ_VERSION='v1'\nYQ_SHA256='%s'\nMEDIAMTX_VERSION='v2'\nMEDIAMTX_SHA256='%s'\n"
                    % (hashlib.sha256(self.yq).hexdigest(), hashlib.sha256(self.mediamtx).hexdigest()))
        sources = {'HEAD:MANET/provisioning/linux-flasher.sh': f'PI_OS_IMAGE_URL="{IMAGE}"\n'.encode(),
                   'HEAD:MANET/provisioning/firstrun.sh.template': template.encode()}
        original = PUBLISH.git
        PUBLISH.git = lambda *args: sources[args[1]]
        self.addCleanup(setattr, PUBLISH, 'git', original)

    def bundle(self, **override):
        members = {'VERSION': self.version, 'debs/Packages': b'Package: x\n', 'debs/x.deb': b'deb',
                   'yq_linux_arm64': self.yq, 'mediamtx_v2_linux_arm64.tar.gz': self.mediamtx,
                   'os-image.json': json.dumps({'url': IMAGE, 'sha256': 'c' * 64, 'size': 5}).encode()}
        members.update(override)
        path = self.work / 'cm4-offline.tar'
        with tarfile.open(path, 'w') as archive:
            for name, data in members.items():
                if data is None:
                    continue
                info = tarfile.TarInfo('./' + name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        path.with_name(path.name + '.sha256').write_text(
            f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n')
        return path

    def test_a_matching_bundle_supplies_the_image_record(self):
        record = PUBLISH.check_offline_bundle(self.bundle(), self.version)
        self.assertEqual(record, {'url': IMAGE, 'sha256': 'c' * 64, 'size': 5})

    def test_mismatched_bundles_are_refused(self):
        cases = {
            'Wrong version': dict(VERSION=b'0.550\n11/2026\n'),
            'different Raspberry Pi OS image': {'os-image.json': json.dumps(
                {'url': IMAGE.replace('2025-10-01', '2025-12-04'), 'sha256': 'c' * 64, 'size': 5}).encode()},
            'yq_linux_arm64': dict(yq_linux_arm64=b'other yq'),
            'mediamtx_v2': {'mediamtx_v2_linux_arm64.tar.gz': b'other'},
            'no package repository': {'debs/Packages': None},
        }
        for message, override in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    PUBLISH.check_offline_bundle(self.bundle(**override), self.version)
        path = self.bundle()
        path.with_name(path.name + '.sha256').write_text('0' * 64 + '  cm4-offline.tar\n')
        with self.assertRaisesRegex(ValueError, 'Checksum sidecar'):
            PUBLISH.check_offline_bundle(path, self.version)


class FlasherToolTests(unittest.TestCase):
    def test_committed_pins_cover_every_kit_program(self):
        PUBLISH.check_flasher_tools((PROVISIONING / 'flasher-tools.json').read_text())

    def test_bad_pins_are_refused(self):
        pins = json.loads((PROVISIONING / 'flasher-tools.json').read_text())
        cases = {
            'other host': dict(url='https://example.com/download/v1/' + pins['files'][0]['name']),
            'name mismatch': dict(name='other.AppImage'),
            'digest': dict(sha256='x' * 64),
            'size': dict(size=0),
        }
        for label, change in cases.items():
            with self.subTest(label):
                broken = json.loads(json.dumps(pins))
                broken['files'][0].update(change)
                with self.assertRaisesRegex(ValueError, 'Invalid program'):
                    PUBLISH.check_flasher_tools(json.dumps(broken))
        missing = dict(pins, files=pins['files'][1:])
        with self.assertRaisesRegex(ValueError, 'exactly the programs'):
            PUBLISH.check_flasher_tools(json.dumps(missing))

    def test_flasher_zip_carries_the_pins(self):
        self.assertIn('flasher-tools.json', PUBLISH.PROVISIONING)


class OfflineManifestTests(unittest.TestCase):
    image = {'url': IMAGE, 'sha256': 'c' * 64, 'size': 5}

    def test_older_releases_without_a_bundle_still_validate(self):
        validate_manifest(manifest())

    def test_a_bundle_needs_a_valid_image_record(self):
        offline = {name: {'size': 1, 'sha256': 'd' * 64} for name in OFFLINE_BUNDLES}
        value = manifest()
        value['assets'].update(offline)
        with self.assertRaises(ReleaseError):
            validate_manifest(value)
        value['os_image'] = dict(self.image)
        validate_manifest(value)
        for bad in ({'url': 'https://example.com/x.img.xz'}, {'sha256': 'x'}, {'size': 0}):
            with self.subTest(bad=bad):
                broken = dict(value, os_image=dict(self.image, **bad))
                with self.assertRaises(ReleaseError):
                    validate_manifest(broken)

    def test_prepare_release_reports_the_offline_bundle(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'manet-release.json'
            for offline in (False, True):
                with self.subTest(offline=offline):
                    value = manifest()
                    if offline:
                        value['assets']['cm4-offline.tar'] = {'size': 1, 'sha256': 'd' * 64}
                        value['os_image'] = self.image
                    path.write_text(json.dumps(value))
                    lines = subprocess.run([sys.executable, str(PROVISIONING / 'prepare-release.py'), str(path), 'cm4'],
                                           capture_output=True, text=True, check=True).stdout.split('\n')
                    self.assertEqual(lines[0], '0.551')
                    if offline:
                        self.assertEqual(lines[3], 'https://github.com/very-srs/MANET/releases/download/v0.551/cm4-offline.tar')
                        self.assertEqual(lines[4], 'd' * 64)
                    else:
                        self.assertEqual(lines[3:5], ['', ''])


if __name__ == '__main__':
    unittest.main()
