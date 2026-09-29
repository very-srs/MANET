"""Release selection, publication boundaries, and prerelease retention."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import manet_release as release

SPEC = importlib.util.spec_from_file_location('manet_publisher', Path(__file__).resolve().parents[1] / 'releases/publish.py')
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


def manifest(version='0.551'):
    return {'schema': 1, 'version': version, 'tag': 'v' + version, 'commit': 'a' * 40,
            'assets': {name: {'size': 1, 'sha256': 'b' * 64} for name in release.PACKAGES}}


def metadata(number, prerelease=True, draft=False):
    return {'id': number, 'tag_name': f'v0.{number}', 'prerelease': prerelease, 'draft': draft,
            'published_at': f'2026-09-{number:02d}T00:00:00Z',
            'assets': [{'name': 'manet-release.json'}]}


class ReleaseSelectionTests(unittest.TestCase):
    def setUp(self):
        self.urls = []

    def fetch(self, responses):
        def download(url, destination, limit):
            self.urls.append(url)
            destination.write_text(json.dumps(responses[url]))
        return download

    def test_stable_uses_latest_manifest_without_prerelease_list(self):
        value = release.select_release(fetch=self.fetch({release.STABLE_MANIFEST: manifest()}))
        self.assertEqual(value['version'], '0.551')
        self.assertEqual(self.urls, [release.STABLE_MANIFEST])

    def test_development_selects_publication_date_including_stable_and_skips_drafts(self):
        rows = [metadata(4), metadata(9, draft=True), metadata(7, prerelease=False)]
        result = release.select_release(True, fetch=self.fetch({
            release.API + '/releases?per_page=100&page=1': rows,
            release.DOWNLOADS + '/v0.7/manet-release.json': manifest('0.7'),
        }))
        self.assertEqual(result['tag'], 'v0.7')

    def test_development_paginates_and_does_not_use_tag_version_order(self):
        old = metadata(1)
        newest = metadata(2)
        newest['tag_name'] = 'v0.001'
        fetch = Mock(side_effect=[[old] * 100, [newest]])
        self.assertEqual(release.newest_release(release.release_list(fetch))['tag_name'], 'v0.001')
        self.assertEqual(fetch.call_count, 2)

    def test_missing_stable_does_not_fall_back_to_development(self):
        fetch = Mock(side_effect=release.ReleaseError('404'))
        with self.assertRaises(release.ReleaseError):
            release.select_release(fetch=fetch)
        self.assertEqual(fetch.call_count, 1)

    def test_manifest_rejects_wrong_tag_missing_board_and_malformed_digest(self):
        for mutate in (lambda m: m.update(tag='v0.999'),
                       lambda m: m['assets'].pop('rpi5-install.tar.gz'),
                       lambda m: m['assets']['cm4-tools.tar.gz'].update(sha256='bad')):
            value = manifest()
            mutate(value)
            with self.assertRaises(release.ReleaseError):
                release.validate_manifest(value)
        with self.assertRaises(release.ReleaseError):
            release.validate_manifest(manifest(), 'v0.552')

    def test_asset_is_pinned_and_checksum_required(self):
        value = manifest()
        self.assertEqual(release.asset_url(value, 'cm4-tools.tar.gz'),
                         release.DOWNLOADS + '/v0.551/cm4-tools.tar.gz')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test'
            path.write_bytes(b'payload')
            expected = {'size': 7, 'sha256': hashlib.sha256(b'payload').hexdigest()}
            release.verify_asset(path, expected)
            path.write_bytes(b'changed')
            with self.assertRaises(release.ReleaseError):
                release.verify_asset(path, expected)


class PublicationTests(unittest.TestCase):
    def test_cleanup_preserves_stable_drafts_and_three_newest_prereleases(self):
        rows = [metadata(i) for i in range(1, 8)]
        rows += [metadata(20, prerelease=False), metadata(21, draft=True)]
        self.assertEqual([r['id'] for r in publisher.cleanup_candidates(rows)], [4, 3, 2, 1])

    def test_cleanup_preserves_unrelated_releases(self):
        unrelated = metadata(1)
        unrelated['assets'] = []
        self.assertEqual(publisher.cleanup_candidates([unrelated] + [metadata(i) for i in range(2, 5)]), [])

    def test_cleanup_rechecks_promotion_before_deleting(self):
        client = Mock()
        client.releases.return_value = [metadata(i) for i in range(1, 5)]
        client.request.return_value = metadata(1, prerelease=False)
        publisher.cleanup(client, apply=True)
        client.request.assert_called_once_with('/releases/1')

    def test_cleanup_dry_run_never_deletes(self):
        client = Mock()
        client.releases.return_value = [metadata(i) for i in range(1, 5)]
        publisher.cleanup(client)
        client.request.assert_not_called()

    def test_published_tag_cannot_be_overwritten(self):
        client = Mock()
        client.releases.return_value = [{'tag_name': 'v0.551', 'draft': False}]
        with self.assertRaisesRegex(ValueError, 'already published'):
            publisher.publish(client, manifest(), {}, 'notes')
        client.request.assert_not_called()

    def test_failed_upload_keeps_release_as_draft(self):
        client = Mock()
        client.releases.return_value = []
        client.request.side_effect = [
            {'id': 1, 'upload_url': 'https://uploads.github.com/test{?name}', 'assets': []},
            {'size': 7, 'digest': 'sha256:' + '0' * 64},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'package'
            path.write_bytes(b'payload')
            with self.assertRaisesRegex(ValueError, 'verification failed'):
                publisher.publish(client, manifest(), {'package': path}, 'notes')
        self.assertFalse(any(len(c.args) > 1 and c.args[1] == 'PATCH' for c in client.request.call_args_list))
        creation = client.request.call_args_list[0].args[2]
        self.assertTrue(creation['draft'])
        self.assertTrue(creation['prerelease'])
        self.assertEqual(creation['make_latest'], 'false')

    def test_promotion_rejects_incomplete_release(self):
        client = Mock()
        client.request.return_value = {'id': 1, 'draft': False, 'assets': []}
        with self.assertRaisesRegex(ValueError, 'missing'):
            publisher.promote(client, 'v0.551')
        self.assertEqual(client.request.call_count, 1)

    def test_promotion_preserves_assets_and_sets_stable_latest(self):
        value = manifest()
        for name in ('manet-flasher.zip', 'flash-a-radio.sh', 'Flash-a-Radio.cmd'):
            value['assets'][name] = {'size': 1, 'sha256': 'b' * 64}
        body = json.dumps(value).encode()
        assets = [{'name': name, 'size': a['size'], 'digest': 'sha256:' + a['sha256']}
                  for name, a in value['assets'].items()]
        assets += [{'name': name + '.sha256'} for name in release.PACKAGES]
        assets.append({'name': 'manet-release.json', 'size': len(body),
                       'digest': 'sha256:' + hashlib.sha256(body).hexdigest()})
        client = Mock()
        client.request.side_effect = [{'id': 1, 'draft': False, 'assets': assets}, {}]
        fetch = lambda url, path, limit: path.write_bytes(body)
        publisher.promote(client, 'v0.551', fetch)
        self.assertEqual(client.request.call_count, 2)
        patch = client.request.call_args.args
        self.assertEqual(patch[1], 'PATCH')
        self.assertFalse(patch[2]['prerelease'])
        self.assertEqual(patch[2]['make_latest'], 'true')

    def test_publish_defaults_to_prerelease_and_only_publishes_after_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'package'
            path.write_bytes(b'payload')
            asset = {'name': 'package', 'size': 7, 'digest': 'sha256:' + publisher.digest(path)}
            client = Mock()
            client.releases.return_value = []
            client.request.side_effect = [
                {'id': 1, 'upload_url': 'https://uploads.github.com/test{?name}', 'assets': []},
                asset, {'assets': [asset]}, {'html_url': 'https://github.com/example/release', 'tag_name': 'v0.551', 'draft': False, 'prerelease': True},
            ]
            publisher.publish(client, manifest(), {'package': path}, 'notes')
        calls = client.request.call_args_list
        self.assertEqual(calls[2].args, ('/releases/1',))
        self.assertEqual(calls[3].args[2]['tag_name'], 'v0.551')
        self.assertTrue(calls[3].args[2]['prerelease'])
        self.assertFalse(calls[3].args[2]['draft'])
        self.assertEqual(calls[3].args[2]['make_latest'], 'false')

    def test_upload_verification_rejects_a_renamed_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Flash-a-Radio.cmd'
            path.write_bytes(b'launcher')
            asset = {'name': 'Flash.a.Radio.cmd', 'size': 8,
                     'digest': 'sha256:' + publisher.digest(path)}
            with self.assertRaisesRegex(ValueError, 'verification failed'):
                publisher.verify_upload(asset, path)

    def test_replace_draft_updates_source_and_clears_only_unpublished_assets(self):
        client = Mock()
        draft = {'id': 1, 'tag_name': 'v0.551', 'draft': True, 'target_commitish': 'c' * 40,
                 'upload_url': 'https://uploads.github.com/test{?name}', 'assets': [{'id': 7}]}
        client.releases.side_effect = [[draft], []]
        refreshed = dict(draft, target_commitish='a' * 40, assets=[])
        client.request.side_effect = [draft, None, refreshed, refreshed,
                                      {'html_url': 'https://github.com/example/release', 'tag_name': 'v0.551', 'draft': False, 'prerelease': True}]
        publisher.publish(client, manifest(), {}, 'notes', replace_draft=True)
        calls = client.request.call_args_list
        self.assertEqual(calls[1].args, ('/releases/assets/7', 'DELETE'))
        self.assertEqual(calls[2].args[2]['target_commitish'], 'a' * 40)
        self.assertEqual(calls[2].args[2]['tag_name'], 'v0.551')

    def test_replace_draft_stops_if_someone_published_it(self):
        client = Mock()
        client.releases.return_value = [{'id': 1, 'tag_name': 'v0.551', 'draft': True}]
        client.request.return_value = {'draft': False}
        with self.assertRaisesRegex(ValueError, 'already been published'):
            publisher.publish(client, manifest(), {}, 'notes', replace_draft=True)
        self.assertEqual(client.request.call_count, 1)

    def test_unexpected_published_tag_is_reported_as_failure(self):
        client = Mock()
        client.releases.return_value = []
        client.request.side_effect = [
            {'id': 1, 'upload_url': 'https://uploads.github.com/test{?name}', 'assets': []},
            {'assets': []},
            {'tag_name': 'untagged-example', 'draft': False, 'prerelease': True},
        ]
        with self.assertRaisesRegex(ValueError, 'unexpected release metadata'):
            publisher.publish(client, manifest(), {}, 'notes')
        self.assertEqual(client.releases.call_count, 1)


if __name__ == '__main__':
    unittest.main()
