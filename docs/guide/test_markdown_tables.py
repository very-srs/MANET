"""Check that every Markdown table in the repository renders as a table.

GitHub drops a table, or shifts its cells, when a row has a different number
of columns from the header. A row that starts with two pipes is the usual
cause: it gains an empty first cell.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DELIMITER = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


def cells(row):
    """Split a table row on unescaped pipes outside code spans."""
    row = row.strip()
    out, cell, code, i = [], "", False, 0
    while i < len(row):
        ch = row[i]
        if ch == "\\" and i + 1 < len(row):
            cell += row[i:i + 2]
            i += 2
            continue
        if ch == "`":
            code = not code
        if ch == "|" and not code:
            out.append(cell)
            cell = ""
        else:
            cell += ch
        i += 1
    out.append(cell)
    if row.startswith("|"):
        out = out[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        out = out[:-1]
    return out


def table_errors(text):
    """Return (line, message) for rows that do not match their header."""
    errors = []
    lines = text.splitlines()
    fenced = False
    i = 0
    while i < len(lines):
        if FENCE.match(lines[i]):
            fenced = not fenced
            i += 1
            continue
        header = lines[i]
        if (not fenced and "|" in header and i + 1 < len(lines)
                and DELIMITER.match(lines[i + 1].strip())):
            width = len(cells(header))
            if len(cells(lines[i + 1])) != width:
                errors.append((i + 2, "delimiter row does not match the header"))
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = lines[i].strip()
                if row.startswith("||"):
                    errors.append((i + 1, "row starts with an empty cell (||)"))
                elif len(cells(row)) != width:
                    errors.append((i + 1, "%d cells, header has %d"
                                   % (len(cells(row)), width)))
                i += 1
            continue
        i += 1
    return errors


def tracked_markdown():
    """List Markdown files tracked by Git."""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "*.md"],
                         capture_output=True, check=True).stdout
    return [ROOT / name for name in out.decode().split("\0") if name]


class MarkdownTableTests(unittest.TestCase):
    def test_short_row_is_reported(self):
        text = "| a | b |\n|---|---|\n| 1 |\n"
        self.assertEqual(table_errors(text), [(3, "1 cells, header has 2")])

    def test_double_pipe_row_is_reported(self):
        text = "| a | b |\n|---|---|\n|| 1 | 2 ||\n"
        self.assertEqual(table_errors(text),
                         [(3, "row starts with an empty cell (||)")])

    def test_pipes_in_code_and_escapes_are_cell_text(self):
        text = "| a | b |\n|---|---|\n| `x | y` | 1 \\| 2 |\n"
        self.assertEqual(table_errors(text), [])

    def test_fenced_tables_are_ignored(self):
        text = "```\n| a | b |\n|---|---|\n| 1 |\n```\n"
        self.assertEqual(table_errors(text), [])

    def test_repository_tables_render(self):
        problems = []
        for path in tracked_markdown():
            for line, message in table_errors(path.read_text(encoding="utf-8")):
                problems.append("%s:%d: %s" % (path.relative_to(ROOT), line, message))
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
