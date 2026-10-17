"""Exercise source preparation against disposable local Git repositories."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "prepare_sources", Path(__file__).with_name("prepare-sources.py"))
prepare_sources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare_sources)


class SourcePreparationTests(unittest.TestCase):
    """Pin revisions without discarding source edits or unrelated files."""

    def setUp(self):
        """Create a local upstream and an isolated build workspace."""
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.upstream = self.root / "upstream"
        self.upstream.mkdir()
        self.work = self.root / "work"
        self.git(self.upstream, "init", "--quiet")
        (self.upstream / "source.c").write_text("original\n")
        self.git(self.upstream, "add", "source.c")
        self.git(self.upstream, "-c", "user.name=Build Test",
                 "-c", "user.email=build@example.invalid", "commit",
                 "--quiet", "-m", "fixture")
        self.revision = self.git(self.upstream, "rev-parse", "HEAD").strip()
        self.entry = dict(url=str(self.upstream), commit=self.revision,
                          path="source")

    def git(self, path, *args):
        """Run real Git operations confined to the temporary fixture."""
        return subprocess.check_output(
            ["git", "-C", str(path), *args], text=True,
            stderr=subprocess.STDOUT)

    def test_fetches_exact_revision_and_reuses_it(self):
        """Fetch a pinned commit and preserve later developer edits."""
        prepare_sources.prepare(self.work, self.entry, False)
        target = self.work / "source"
        self.assertEqual(self.git(target, "rev-parse", "HEAD").strip(),
                         self.revision)
        (target / "source.c").write_text("developer edit\n")
        prepare_sources.prepare(self.work, self.entry, False)
        prepare_sources.prepare(self.work, self.entry, True)
        self.assertEqual((target / "source.c").read_text(), "developer edit\n")

    def test_wrong_revision_does_not_reset_tree(self):
        """A pin mismatch leaves the checkout and its files untouched."""
        prepare_sources.prepare(self.work, self.entry, False)
        target = self.work / "source"
        entry = dict(self.entry, commit="0" * 40)
        with self.assertRaisesRegex(ValueError, "Unexpected revision"):
            prepare_sources.prepare(self.work, entry, False)
        self.assertEqual(self.git(target, "rev-parse", "HEAD").strip(),
                         self.revision)
        self.assertEqual((target / "source.c").read_text(), "original\n")

    def test_nonempty_directory_is_preserved(self):
        """Refuse to initialize a repository over existing user files."""
        target = self.work / "source"
        target.mkdir(parents=True)
        (target / "notes").write_text("keep\n")
        with self.assertRaisesRegex(ValueError, "Refusing nonempty"):
            prepare_sources.prepare(self.work, self.entry, False)
        self.assertFalse((target / ".git").exists())
        self.assertEqual((target / "notes").read_text(), "keep\n")

    def test_check_missing_input_does_not_create_it(self):
        """Read-only verification must not initialize or fetch inputs."""
        with self.assertRaisesRegex(ValueError, "Missing"):
            prepare_sources.prepare(self.work, self.entry, True)
        self.assertFalse(self.work.exists())

    def test_patch_is_required_and_idempotent(self):
        """Apply the source patch once and reject a conflicting edit."""
        patch = self.root / "change.patch"
        patch.write_text(
            "diff --git a/source.c b/source.c\n"
            "--- a/source.c\n+++ b/source.c\n"
            "@@ -1 +1 @@\n-original\n+patched\n")
        entry = dict(self.entry, patch=str(patch))
        prepare_sources.prepare(self.work, entry, False)
        prepare_sources.prepare(self.work, entry, False)
        prepare_sources.prepare(self.work, entry, True)
        target = self.work / "source/source.c"
        self.assertEqual(target.read_text(), "patched\n")
        target.write_text("conflict\n")
        with self.assertRaisesRegex(ValueError, "patch missing or changed"):
            prepare_sources.prepare(self.work, entry, True)
        self.assertEqual(target.read_text(), "conflict\n")


if __name__ == "__main__":
    unittest.main()
