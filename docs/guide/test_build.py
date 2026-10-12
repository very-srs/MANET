"""Check that the catalog gate detects tool inventory changes."""

from pathlib import Path
import tempfile
import unittest

from build import check_tool_map


class ToolMapTests(unittest.TestCase):
    """Exercise additions, removals and ambiguous catalog ownership."""

    def setUp(self):
        """Make a small independent production/test directory."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.tools = root / "tools"
        self.tools.mkdir()
        for name in ("worker.py", "chronyc", "README.md", "test_worker.py"):
            (self.tools / name).touch()
        (self.tools / "fixtures").mkdir()
        (self.tools / "fixtures/example.json").touch()
        self.catalog = root / "map.md"
        self.rows = "".join(
            f"| [{name}](../MANET/node_tools/{name}) | Role |\n"
            for name in ("worker.py", "chronyc")
        )
        self.catalog.write_text(self.rows)

    def check(self):
        """Validate only the isolated fixture."""
        check_tool_map(self.tools, self.catalog)

    def test_complete_map_includes_binaries_and_excludes_tests(self):
        """A binary needs documentation; test assets do not."""
        self.check()

    def test_added_tool_requires_a_row(self):
        """Adding production code cannot silently bypass coverage."""
        (self.tools / "new-tool.sh").touch()
        with self.assertRaisesRegex(ValueError, "missing: new-tool.sh"):
            self.check()

    def test_removed_tool_leaves_a_stale_row(self):
        """The map must also follow source deletions."""
        (self.tools / "worker.py").unlink()
        with self.assertRaisesRegex(ValueError, "removed or excluded"):
            self.check()

    def test_duplicate_ownership_fails(self):
        """Every file has one primary responsibility row."""
        self.catalog.write_text(self.rows + self.rows)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.check()

    def test_generated_manager_is_rejected(self):
        """The runtime symlink must not become a source implementation."""
        (self.tools / "node-manager.sh").symlink_to("worker.py")
        with self.assertRaisesRegex(ValueError, "is generated"):
            self.check()

    def test_mislabelled_row_fails(self):
        """The displayed filename must identify the linked source."""
        self.catalog.write_text(self.rows.replace("[worker.py]", "[old.py]"))
        with self.assertRaisesRegex(ValueError, "label must match"):
            self.check()
