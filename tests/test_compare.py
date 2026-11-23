"""End-to-end Python comparison fixtures."""

import hashlib
import os
import re
import sys
import unittest
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.support import PROJECT_ROOT, run_cli

sys.path.insert(0, str(PROJECT_ROOT / "lib"))
from sourcediff_py.cli import main as python_main


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.tree = self.work / "tree"
        self.tree.mkdir()
        self.catalog = self.work / "baseline"
        self.report = self.work / "difference"

    def save(self, *options):
        result = run_cli(["catalog", self.tree, "--output", self.catalog, *options], cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stderr)

    def compare(self, *options, status=0):
        result = run_cli(
            ["compare", self.tree, self.catalog, "--output", self.report, "--overwrite", *options],
            cwd=self.work,
        )
        self.assertEqual(result.returncode, status, result.stderr)
        self.assertTrue(self.report.is_file(), result.stderr)
        return self.report_rows()

    def report_rows(self):
        report = self.report.read_bytes()
        self.assertTrue(report.startswith(b"SOURCEDIFF-REPORT\t1\n"))
        rows = defaultdict(list)
        for row in report.split(b"\n")[:-1]:
            category, *fields = row.split(b"\t")
            rows[category].append(fields)
        return rows

    def test_content_added_missing_type_and_byte_paths(self):
        (self.tree / "keep").write_bytes(b"same")
        (self.tree / "change").write_bytes(b"old")
        (self.tree / "remove").mkdir()
        (self.tree / "remove" / "leaf").touch()
        (self.tree / "type").touch()
        with open(os.fsencode(self.tree) + b"/raw%\t\n\xff", "wb") as file:
            file.write(b"old")
        self.save()
        (self.tree / "change").write_bytes(b"new")
        (self.tree / "remove" / "leaf").unlink()
        (self.tree / "remove").rmdir()
        (self.tree / "type").unlink()
        (self.tree / "type").mkdir()
        (self.tree / "new").touch()
        with open(os.fsencode(self.tree) + b"/raw%\t\n\xff", "wb") as file:
            file.write(b"new")
        rows = self.compare(status=1)
        self.assertEqual([row[0] for row in rows[b"MISSING"]], [b"remove", b"remove/leaf"])
        self.assertEqual([row[0] for row in rows[b"ADDED"]], [b"new"])
        self.assertEqual([row[0] for row in rows[b"CONTENT_CHANGED"]], [b"change", b"raw%25%09%0A%FF"])
        self.assertEqual([row[0] for row in rows[b"TYPE_CHANGED"]], [b"type"])
        self.assertIn(hashlib.sha256(b"old").hexdigest().encode(), rows[b"CONTENT_CHANGED"][0])
        self.assertIn(hashlib.sha256(b"new").hexdigest().encode(), rows[b"CONTENT_CHANGED"][0])

    def test_identical_tree_produces_zero_discrepancies_and_default_output(self):
        (self.tree / "file").touch()
        self.save()
        result = run_cli(["compare", self.tree, self.catalog], cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = list(self.work.glob("difference-*.txt"))
        self.assertEqual(len(outputs), 1)
        self.assertRegex(outputs[0].name, r"^difference-\d{14}\.txt$")
        self.assertIn(b"T\tcounted\t0\n", outputs[0].read_bytes())

    def test_metadata_is_independently_counted_strictly_for_files(self):
        file = self.tree / "file"
        file.write_bytes(b"same")
        self.save("--directory-hashes")
        for changed in (file, self.tree):
            info = changed.stat()
            os.utime(changed, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
        rows = self.compare()
        self.assertEqual([(row[0], row[1]) for row in rows[b"METADATA_CHANGED"]],
                         [(b".", b"informational"), (b"file", b"informational")])
        self.assertEqual(rows[b"CONTENT_CHANGED"], [])
        rows = self.compare("--strict", "--quick", status=1)
        self.assertEqual([(row[0], row[1]) for row in rows[b"METADATA_CHANGED"]],
                         [(b".", b"informational"), (b"file", b"counted")])
        self.assertIn([b"files-hashed", b"1"], rows[b"T"])

    def test_quick_reuse_can_miss_same_size_restored_mtime_changes(self):
        file = self.tree / "file"
        file.write_bytes(b"old")
        self.save("--directory-hashes")
        info = file.stat()
        file.write_bytes(b"new")
        os.utime(file, ns=(info.st_atime_ns, info.st_mtime_ns))
        rows = self.compare(status=1)
        self.assertEqual([row[0] for row in rows[b"CONTENT_CHANGED"]], [b".", b"file"])
        rows = self.compare("--quick", "--strict")
        self.assertEqual(rows[b"CONTENT_CHANGED"], [])
        self.assertIn([b"files-reused", b"1"], rows[b"T"])
        self.assertIn([b"files-hashed", b"0"], rows[b"T"])
        self.assertIn([b"directory-digests-unverified", b"1"], rows[b"T"])

    def test_quick_hashes_with_unavailable_or_different_saved_precision(self):
        file = self.tree / "file"
        file.write_bytes(b"old")
        self.save()
        original = self.catalog.read_bytes()
        info = file.stat()
        file.write_bytes(b"new")
        os.utime(file, ns=(info.st_atime_ns, info.st_mtime_ns))
        for precision in (b"-", b"1000000000"):
            with self.subTest(precision=precision):
                self.catalog.write_bytes(re.sub(rb"(E\tfile\tfile\t[^\t]+\t3\t-?\d+\t)1\t",
                                                rb"\g<1>" + precision + b"\t", original))
                rows = self.compare("--quick", status=1)
                self.assertEqual([row[0] for row in rows[b"CONTENT_CHANGED"]], [b"file"])
                self.assertIn([b"files-hashed", b"1"], rows[b"T"])

    def test_narrow_scope_projects_old_and_new_and_recomputes_directory_hashes(self):
        (self.tree / "trim").mkdir()
        (self.tree / "trim" / "file").touch()
        (self.tree / "excluded.log").touch()
        self.save("--directory-hashes")
        rows = self.compare("--exclude-dir", "trim", "--exclude-ext", ".log")
        self.assertEqual(rows[b"MISSING"], [])
        self.assertEqual(rows[b"CONTENT_CHANGED"], [])
        self.assertIn([b"exclude-dir", b"trim"], rows[b"O"])
        self.assertIn([b"exclude-ext", b".log"], rows[b"S"])

    def test_wider_scope_marks_entries_as_added_without_prior_coverage(self):
        (self.tree / "excluded").touch()
        (self.tree / "folder").mkdir()
        (self.tree / "folder" / "file").touch()
        self.save("--no-recursive", "--exclude", "excluded", "--directory-hashes")
        rows = self.compare("--recursive", "--clear-exclude", status=1)
        self.assertEqual([(row[0], row[1]) for row in rows[b"ADDED"]],
                         [(b"excluded", b"outside-baseline-scope"), (b"folder/file", b"outside-baseline-scope")])
        self.assertIn([b"exclude"], rows[b"O"])
        self.assertIn([b"directory-hash-scope", b"scope-changed"], rows[b"I"])
        self.assertEqual(rows[b"CONTENT_CHANGED"], [])

    def test_symlink_content_is_destination_bytes(self):
        link = os.fsencode(self.tree) + b"/link"
        os.symlink(b"left\n", link)
        self.save()
        os.unlink(link)
        os.symlink(b"right\t", link)
        rows = self.compare(status=1)
        self.assertEqual([row[0] for row in rows[b"CONTENT_CHANGED"]], [b"link"])
        self.assertIn(b"left%0A", rows[b"CONTENT_CHANGED"][0])
        self.assertIn(b"right%09", rows[b"CONTENT_CHANGED"][0])

    def test_symlink_override_does_not_mark_skipped_followed_descendants_missing(self):
        (self.tree / "real").mkdir()
        (self.tree / "real" / "leaf").touch()
        (self.tree / "link").symlink_to("real")
        self.save("--symlinks", "follow", "--directory-hashes")
        rows = self.compare("--symlinks", "skip")
        self.assertEqual(rows[b"MISSING"], [])
        self.assertIn([b"symlink-policy", b"follow", b"skip"], rows[b"I"])

    def test_followed_links_filtered_by_resolved_kind_do_not_become_missing(self):
        directory = self.work / "directory"
        directory.mkdir()
        file = self.work / "file"
        file.touch()
        (self.tree / "directory-link").symlink_to(directory)
        (self.tree / "file-link.log").symlink_to(file)
        self.save("--exclude-dir", "directory-link", "--exclude-ext", "log")

        rows = self.compare("--symlinks", "follow")
        self.assertEqual(rows[b"MISSING"], [])
        self.assertEqual(rows[b"ERROR"], [])

        directory.rmdir()
        rows = self.compare("--symlinks", "follow", status=2)
        self.assertEqual(rows[b"MISSING"], [])
        self.assertEqual([row[:2] for row in rows[b"ERROR"]], [[b"directory-link", b"follow-link"]])
        self.assertEqual([row[0] for row in rows[b"UNKNOWN"]], [b"directory-link"])

    def test_unreadable_recorded_link_excludes_old_followed_descendants(self):
        directory = self.work / "directory"
        directory.mkdir()
        (directory / "leaf").touch()
        (self.tree / "link").symlink_to(directory)
        self.save("--symlinks", "follow")
        readlink = os.readlink

        def deny_link(path, *, dir_fd=None):
            if dir_fd is not None and path == b"link":
                raise PermissionError("fixture rejected link destination read")
            return readlink(path, dir_fd=dir_fd)

        with patch("sourcediff_py.scan.os.readlink", side_effect=deny_link):
            status = python_main([
                "compare", str(self.tree), str(self.catalog), "--output", str(self.report),
                "--symlinks", "record",
            ])
        self.assertEqual(status, 2)
        rows = self.report_rows()
        self.assertEqual(rows[b"MISSING"], [])
        self.assertEqual([row[:2] for row in rows[b"ERROR"]], [[b"link", b"readlink"]])
        self.assertEqual([row[0] for row in rows[b"UNKNOWN"]], [b"link"])
        self.assertEqual(rows[b"END"], [[b"incomplete", b"2"]])

    def test_catalog_path_through_symlink_and_dotdot_keeps_unrelated_file_in_scope(self):
        nested = self.tree / "nested"
        (nested / "sub").mkdir(parents=True)
        (self.tree / "alias").symlink_to("nested/sub")
        other = self.tree / "catalog"
        other.write_bytes(b"old")
        (nested / "catalog").touch()
        self.save("--directory-hashes")
        self.catalog.replace(nested / "catalog")
        self.catalog = self.tree / "alias" / ".." / "catalog"
        other.write_bytes(b"new")

        rows = self.compare(status=1)
        self.assertEqual([row[0] for row in rows[b"CONTENT_CHANGED"]], [b".", b"catalog"])
        self.assertEqual(rows[b"MISSING"], [])

    def test_report_input_and_output_inside_target_are_excluded_on_both_sides(self):
        input_path = self.tree / "input"
        output_path = self.tree / "output"
        input_path.write_bytes(b"prior input path occupant")
        output_path.write_bytes(b"prior output path occupant")
        self.save("--directory-hashes")
        self.catalog.replace(input_path)
        self.catalog = input_path
        self.report = output_path
        rows = self.compare()
        self.assertEqual(rows[b"MISSING"], [])
        self.assertEqual(rows[b"ADDED"], [])
        self.assertEqual(rows[b"CONTENT_CHANGED"], [])
        self.assertTrue(self.catalog.read_bytes().startswith(b"SOURCEDIFF\t1\n"))
        self.assertEqual(sorted(path.name for path in self.tree.iterdir()), ["input", "output"])

    def test_entry_and_subtree_errors_preserve_unknowns_and_take_precedence(self):
        (self.tree / "unread").mkdir()
        (self.tree / "unread" / "leaf").touch()
        (self.tree / "bad").touch()
        (self.tree / "change").write_bytes(b"old")
        self.save("--symlinks", "follow", "--directory-hashes")
        (self.tree / "unread" / "leaf").unlink()
        (self.tree / "unread").rmdir()
        (self.tree / "unread").symlink_to("absent")
        (self.tree / "bad").unlink()
        os.mkfifo(self.tree / "bad")
        (self.tree / "change").write_bytes(b"new")
        rows = self.compare(status=2)
        self.assertEqual(rows[b"MISSING"], [])
        self.assertEqual([row[0] for row in rows[b"ERROR"]], [b"bad", b"unread"])
        self.assertEqual([row[0] for row in rows[b"UNKNOWN"]], [b".", b"bad", b"unread", b"unread/leaf"])
        self.assertEqual([row[0] for row in rows[b"CONTENT_CHANGED"]], [b"change"])
        self.assertEqual(rows[b"END"], [[b"incomplete", b"2"]])

    def test_fail_fast_uses_unknown_for_unvisited_scope(self):
        (self.tree / "bad").touch()
        (self.tree / "old").touch()
        self.save()
        (self.tree / "bad").unlink()
        os.mkfifo(self.tree / "bad")
        (self.tree / "old").unlink()
        rows = self.compare("--fail-fast", "--jobs", "1", status=2)
        self.assertEqual(rows[b"MISSING"], [])
        self.assertIn(b"old", [row[0] for row in rows[b"UNKNOWN"]])
        self.assertIn([b".", b"aborted"], [row[:2] for row in rows[b"ERROR"]])

    def test_incomplete_catalog_is_rejected_without_report(self):
        os.mkfifo(self.tree / "pipe")
        result = run_cli(["catalog", self.tree, "--output", self.catalog], cwd=self.work)
        self.assertEqual(result.returncode, 2, result.stderr)
        result = run_cli(["compare", self.tree, self.catalog, "--output", self.report], cwd=self.work)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(b"incomplete", result.stderr)
        self.assertFalse(self.report.exists())


if __name__ == "__main__":
    unittest.main()
