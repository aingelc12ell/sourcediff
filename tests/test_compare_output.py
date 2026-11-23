"""Public CLI and report contracts shared by both SourceDiff engines."""

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.support import run_cli


ENGINES = ("python", "bash")
COMPACT = b"SOURCEDIFF-DIFFERENCES\t1"
FULL = b"SOURCEDIFF-REPORT\t1"


@unittest.skipUnless(sys.platform.startswith("linux"), "requires the Linux launcher and GNU tools")
class CompareOutputTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.target = self.work / "target"
        self.target.mkdir()
        self.baseline = self.work / "baseline"
        self.report = self.work / "report"

    def save(self, *options, engine="python"):
        result = run_cli(["catalog", self.target, "--output", self.baseline, *options], engine=engine, cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stderr)

    def compare(self, engine, status, *options):
        result = run_cli(["compare", self.target, self.baseline, "--output", self.report,
                          "--overwrite", *options], engine=engine, cwd=self.work)
        self.assertEqual(result.returncode, status, result.stderr)
        self.assertTrue(self.report.exists(), result.stderr)
        return self.report.read_bytes().splitlines()

    def check_compact(self, compact, full, changes, totals, status):
        self.assertEqual(full[0], FULL)
        self.assertEqual(compact[0], COMPACT)
        rows = [row.split(b"\t") for row in compact[1:] if not row.startswith((b"T\t", b"END\t"))]
        self.assertEqual([tuple(row[:3]) for row in rows], changes)
        self.assertTrue(all(row in full for row in compact[1:]))
        total_rows = [row for row in compact if row.startswith(b"T\t")]
        self.assertEqual(total_rows, [row for row in full if row.startswith(b"T\t")])
        self.assertEqual([int(row.rsplit(b"\t", 1)[1]) for row in total_rows], totals)
        self.assertEqual(compact[-1], status)

    def test_requires_two_nonempty_positionals_and_rejects_legacy_flag(self):
        self.save()
        for engine in ENGINES:
            for case, (command, args, diagnostic) in enumerate((
                ("compare", [], b"TARGET"),
                ("compare", [self.target], b"CATALOG"),
                ("compare", ["", self.baseline], b"empty"),
                ("compare", [self.target, ""], b"empty"),
                ("compare", [self.target, self.baseline, "unexpected"], b"unexpected"),
                ("compare", [self.target, "--catalog", self.baseline], b"--catalog"),
                ("compare", [self.target, self.baseline, "--catalog", self.baseline], b"--catalog"),
                ("compare", [self.target, self.baseline, f"--catalog={self.baseline}"], b"--catalog"),
                ("catalog", [self.target, "--catalog", self.baseline], b"--catalog"),
                ("catalog", [self.target, f"--catalog={self.baseline}"], b"--catalog"),
                ("catalog", [self.target, "--differences-only"], b"--differences-only"),
            )):
                with self.subTest(engine=engine, command=command, args=args):
                    output = self.work / f"invalid-{engine}-{case}"
                    result = run_cli([command, "--output", output, *args], engine=engine, cwd=self.work)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(diagnostic, result.stderr)
                    self.assertFalse(output.exists())

    def test_double_dash_preserves_raw_positional_paths(self):
        target = b"-target\t\xff\n"
        baseline = b"--engine=bash\t\xfe\n"
        os.mkdir(os.fsencode(self.work) + b"/" + target)
        result = run_cli(["catalog", "--output", b"./" + baseline, "--", target], cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stderr)
        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = run_cli(["compare", "--output", self.report, "--overwrite", "--",
                                  target, baseline], engine=engine, cwd=self.work)
                self.assertEqual(result.returncode, 0, result.stderr)
                rows = self.report.read_bytes().splitlines()
                self.assertTrue(rows[1].endswith(b"/--engine=bash%09%FE%0A"), rows[1])
                self.assertTrue(rows[2].endswith(b"/-target%09%FF%0A"), rows[2])

    def test_attached_groups_can_surround_positionals_and_engine_delimits_whitespace(self):
        for name in ("one", "two", "three"):
            (self.target / name).mkdir()
        for name in ("file.tmp", "file.bak"):
            (self.target / name).touch()
        self.save("--exclude-dir", "one", "two", "three", "--exclude-ext", "tmp", "bak")
        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = run_cli([
                    "compare", "--exclude-dir=one|two", self.target, "--exclude-dir", "three",
                    "--engine", engine, self.baseline, "--output", self.report, "--overwrite",
                ], engine=None, cwd=self.work)
                self.assertEqual(result.returncode, 0, result.stderr)
                rows = self.report.read_bytes().splitlines()
                self.assertIn(b"S\texclude-ext\ttmp", rows)
                self.assertIn(b"S\texclude-ext\tbak", rows)
                self.assertEqual([row for row in rows if row.startswith(b"O\texclude-dir\t")],
                                 [b"O\texclude-dir\tone", b"O\texclude-dir\ttwo", b"O\texclude-dir\tthree"])
                result = run_cli([
                    "compare", self.target, "--exclude-dir=one|two|three", self.baseline,
                    "--exclude-ext=tmp|bak", "--differences-only", "--output", self.report, "--overwrite",
                ], engine=engine, cwd=self.work)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.report.read_bytes().splitlines()[0], COMPACT)

    def test_no_change_report_has_only_totals_and_trailer(self):
        (self.target / "same").write_bytes(b"old")
        self.save()
        for engine in ENGINES:
            with self.subTest(engine=engine):
                full = self.compare(engine, 0)
                compact = self.compare(engine, 0, "--differences-only")
                self.check_compact(compact, full, [], [0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0],
                                   b"END\tcomplete\t0")
                self.assertIn(b"I\tstrict\tfalse", full)
                self.assertIn(b"S\tcomplete\tcomplete", full)

    def test_metadata_visibility_logs_and_quick_verification(self):
        edited = self.target / "edited"
        edited.write_bytes(b"old")
        (self.target / "same").touch()
        directory = self.target / "directory"
        directory.mkdir()
        self.save(engine="bash")
        for path in (edited, directory):
            stamp = path.stat()
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2_000_000_000))
        for engine in ENGINES:
            with self.subTest(engine=engine):
                log = self.work / f"{engine}.log"
                full = self.compare(engine, 0)
                compact = self.compare(engine, 0, "--differences-only", "--log-file", log)
                self.check_compact(compact, full, [], [0, 0, 0, 0, 2, 0, 0, 0, 2, 0, 0, 0, 0],
                                   b"END\tcomplete\t0")
                self.assertIn(b"category=METADATA_CHANGED detail=informational", log.read_bytes())
                self.assertIn(b"metadata-changed=2", log.read_bytes())
                strict = ("--strict", "--quick")
                full = self.compare(engine, 1, *strict)
                compact = self.compare(engine, 1, *strict, "--differences-only")
                self.check_compact(compact, full, [(b"METADATA_CHANGED", b"edited", b"counted")],
                                   [0, 0, 0, 0, 2, 0, 0, 1, 1, 1, 1, 0, 0], b"END\tcomplete\t1")

    def test_counted_changes_and_outside_scope_keep_order_and_entry_groups(self):
        for name in ("content", "missing", "type"):
            (self.target / name).write_bytes(b"old")
        (self.target / "outside.tmp").touch()
        self.save("--exclude-ext", "tmp", "bak")
        changed = self.target / "content"
        stamp = changed.stat()
        changed.write_bytes(b"new")
        os.utime(changed, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2_000_000_000))
        (self.target / "missing").unlink()
        (self.target / "type").unlink()
        (self.target / "type").symlink_to("content")
        with open(os.fsencode(self.target) + b"/added\xff\t", "wb") as added:
            added.write(b"new")
        root_stamp = self.target.stat()
        os.utime(self.target, ns=(root_stamp.st_atime_ns, root_stamp.st_mtime_ns + 2_000_000_000))

        expected = [
            (b"ADDED", b"added%FF%09", b"counted"),
            (b"CONTENT_CHANGED", b"content", b"counted"),
            (b"MISSING", b"missing", b"counted"),
            (b"ADDED", b"outside.tmp", b"outside-baseline-scope"),
            (b"TYPE_CHANGED", b"type", b"counted"),
        ]
        first = None
        for engine in ENGINES:
            with self.subTest(engine=engine):
                full = self.compare(engine, 1, "--clear-exclude-ext")
                compact = self.compare(engine, 1, "--clear-exclude-ext", "--differences-only")
                self.check_compact(compact, full, expected, [2, 1, 1, 1, 2, 0, 0, 5, 3, 0, 0, 0, 0],
                                   b"END\tcomplete\t1")
                self.assertIn(b"O\texclude-ext", full)
                change = compact[2].split(b"\t")
                self.assertEqual(len(change), 17)
                self.assertEqual((change[3], change[10]), (b"file", b"file"))
                self.assertNotEqual(change[4], change[11])
                if first is not None:
                    self.assertEqual(compact, first)
                first = compact

    def test_unknown_and_error_remain_visible_in_incomplete_report(self):
        external = self.work / "external"
        external.write_bytes(b"old")
        (self.target / "link").symlink_to(external)
        self.save("--symlinks", "follow")
        external.unlink()
        for engine in ENGINES:
            with self.subTest(engine=engine):
                full = self.compare(engine, 2)
                compact = self.compare(engine, 2, "--differences-only")
                self.check_compact(compact, full, [(b"UNKNOWN", b"link", b"target-scan-error"),
                                                   (b"ERROR", b"link", b"follow-link")],
                                   [0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0], b"END\tincomplete\t2")

    def test_compact_does_not_publish_for_invalid_baseline_or_overwrite_input(self):
        self.baseline.write_bytes(b"invalid baseline\n")
        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = run_cli(["compare", self.target, self.baseline, "--output", self.report,
                                  "--differences-only"], engine=engine, cwd=self.work)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.report.exists())

        self.baseline.unlink()
        self.save()
        original = self.baseline.read_bytes()
        alias = self.work / "alias"
        os.link(self.baseline, alias)
        for engine in ENGINES:
            for output in ("--output", "--log-file"):
                with self.subTest(engine=engine, output=output):
                    result = run_cli(["compare", self.target, self.baseline, output, alias,
                                      "--overwrite", "--differences-only"], engine=engine, cwd=self.work)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(self.baseline.read_bytes(), original)
                    self.assertEqual(alias.stat().st_ino, self.baseline.stat().st_ino)
                    self.assertEqual(list(self.work.glob("difference-*.txt")), [])
