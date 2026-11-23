"""List filter syntax at the public CLI and direct Python parser boundaries."""

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sourcediff_py.cli import create_parser, effective_settings
from sourcediff_py.format import decode_bytes, iter_entries, read_catalog
from tests.support import run_cli


class DirectFilterListTests(unittest.TestCase):
    def test_whitespace_and_attached_groups_produce_ordered_flat_lists(self):
        args = create_parser().parse_args([
            "catalog", "tree", "--exclude-dir", "a", "b|literal", "c",
            "--exclude-dir=d|e", "--exclude-ext", "log", ".tmp", "--exclude-ext=bak|old",
        ])
        settings, _ = effective_settings(args)
        self.assertEqual(settings["exclude-dir"], ["a", "b|literal", "c", "d", "e"])
        self.assertEqual(settings["exclude-ext"], ["log", ".tmp", "bak", "old"])

    def test_attached_group_before_target_stops_at_its_argument(self):
        args = create_parser().parse_args(["catalog", "--exclude-dir=a|b", "tree"])
        self.assertEqual(args.target, "tree")
        self.assertEqual(args.exclude_dir, ["a", "b"])

    def test_clear_and_repeated_parse_do_not_reuse_values(self):
        parser = create_parser()
        args = parser.parse_args([
            "catalog", "tree", "--exclude-dir=a|b", "--clear-exclude-dir",
            "--exclude-dir", "c", "d", "--exclude-dir=e|f",
        ])
        self.assertEqual(args.exclude_dir, ["c", "d", "e", "f"])
        self.assertIsNone(parser.parse_args(["catalog", "tree"]).exclude_dir)

    def test_scalar_values_and_double_dash_are_not_preprocessed(self):
        args = create_parser().parse_args([
            "catalog", "--output=out|--exclude-dir=a|b", "--log-file=log|--exclude-ext=tmp|bak",
            "--exclude", "name|part", "--exclude-dir", "a", "b", "--", "--exclude-dir=x|y",
        ])
        self.assertEqual(args.target, "--exclude-dir=x|y")
        self.assertEqual(args.output, "out|--exclude-dir=a|b")
        self.assertEqual(args.log_file, "log|--exclude-ext=tmp|bak")
        self.assertEqual(args.exclude, ["name|part"])
        self.assertEqual(args.exclude_dir, ["a", "b"])

    def test_empty_pipe_parts_and_missing_whitespace_values_are_usage_errors(self):
        for flag in ("--exclude-dir", "--exclude-ext"):
            for suffix in ("=", "=|a", "=a|", "=a||b", None):
                with self.subTest(flag=flag, suffix=suffix), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as failure:
                        create_parser().parse_args(["catalog", "tree", flag + suffix if suffix is not None else flag])
                    self.assertEqual(failure.exception.code, 2)


@unittest.skipUnless(sys.platform.startswith("linux"), "requires the Linux launcher and GNU tools")
class PublicFilterListTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.tree = self.work / "tree"
        self.tree.mkdir()
        for name in (b"a/leaf", b"b/leaf", b"c/leaf", b"name|part/leaf", b"keep.txt",
                     b"skip.log", b"skip.tmp", b"skip.bak"):
            self.put(name)
        self.serial = 0

    def put(self, name):
        path = os.fsencode(self.tree) + b"/" + name
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as output:
            output.write(b"fixture")

    def invoke(self, engine, options, *, command="catalog", baseline=None, before=(), target=None, status=0):
        self.serial += 1
        output = self.work / ("artifact-%d" % self.serial)
        args = [command, *before, str(self.tree) if target is None else target]
        if baseline is not None:
            args.append(baseline)
        result = run_cli([*args, "--output", output, "--jobs", "1", *options], engine=engine, cwd=self.work)
        self.assertEqual(result.returncode, status, result.stderr)
        if status == 2:
            self.assertFalse(output.exists())
        return output

    def catalog(self, engine, options, **kwargs):
        output = self.invoke(engine, options, **kwargs)
        settings, entries, errors, complete = read_catalog(output, workspace=self.work)
        self.assertTrue(complete)
        return output, settings, {entry["path"] for entry in iter_entries(entries)}

    def filter_rows(self, output, category, key):
        prefix = category + b"\t" + key.encode() + b"\t"
        return [os.fsdecode(decode_bytes(row[len(prefix):].decode("ascii")))
                for row in output.read_bytes().splitlines() if row.startswith(prefix)]

    def test_whitespace_attached_and_repeated_forms_filter_real_entries(self):
        for engine in ("python", "bash"):
            for options in (
                ["--exclude-dir", "a", "b", "--exclude-dir", "c", "--exclude-ext", "log", "tmp", "bak"],
                ["--exclude-dir=a|b|c", "--exclude-ext=log|tmp|bak"],
                ["--exclude-dir", "a", "--exclude-dir", "b", "--exclude-dir", "c",
                 "--exclude-ext", "log", "--exclude-ext", "tmp", "--exclude-ext", "bak"],
            ):
                with self.subTest(engine=engine, options=options):
                    _, settings, entries = self.catalog(engine, options)
                    self.assertEqual(settings["exclude-dir"], ["a", "b", "c"])
                    self.assertEqual(settings["exclude-ext"], ["log", "tmp", "bak"])
                    self.assertEqual(entries, {b".", b"keep.txt", b"name|part", b"name|part/leaf"})

    def test_attached_groups_before_target_and_literal_pipe_separate_values(self):
        for engine in ("python", "bash"):
            with self.subTest(engine=engine):
                _, settings, entries = self.catalog(engine, ["--exclude-dir", "name|part"],
                                                    before=["--exclude-dir=a|b"])
                self.assertEqual(settings["exclude-dir"], ["a", "b", "name|part"])
                self.assertNotIn(b"name|part", entries)
                self.assertIn(b"c/leaf", entries)

    def test_values_preserve_spaces_globs_backslashes_and_non_utf8_bytes(self):
        values = [b" space ", b"back\\dir", b"wild?", b"raw\xff\n", b" "]
        for name in (b" space /leaf", b"back\\dir/leaf", b"wildX/leaf", b"raw\xff\n/leaf", b" /leaf"):
            self.put(name)
        for engine in ("python", "bash"):
            for options in (["--exclude-dir", *values], [b"--exclude-dir=" + b"|".join(values)]):
                with self.subTest(engine=engine, options=options):
                    _, settings, entries = self.catalog(engine, options)
                    self.assertEqual(settings["exclude-dir"], [os.fsdecode(value) for value in values])
                    for name in (b" space ", b"back\\dir", b"wildX", b"raw\xff\n", b" "):
                        self.assertNotIn(name, entries)

    def test_cross_engine_inheritance_override_and_positional_clears(self):
        for writer, reader in (("python", "bash"), ("bash", "python")):
            baseline, _, _ = self.catalog(writer, ["--exclude-dir", "name|part", "a",
                                                  "--exclude-ext=log|tmp", "--exclude", "unused|literal"])
            for options, expected_dirs, expected_exts, overridden in (
                ([], ["name|part", "a"], ["log", "tmp"], False),
                (["--exclude-dir", "b", "c", "--exclude-ext=bak|old"], ["b", "c"], ["bak", "old"], True),
                (["--exclude-dir=a|b", "--clear-exclude-dir", "--exclude-dir", "c", "name|part",
                  "--exclude-ext", "bak", "tmp", "--clear-exclude-ext", "--exclude-ext=log|old"],
                 ["c", "name|part"], ["log", "old"], True),
            ):
                with self.subTest(writer=writer, reader=reader, options=options):
                    output = self.invoke(reader, options, command="compare", baseline=baseline,
                                         status=1 if overridden else 0)
                    self.assertEqual(self.filter_rows(output, b"S", "exclude-dir"), expected_dirs)
                    self.assertEqual(self.filter_rows(output, b"S", "exclude-ext"), expected_exts)
                    self.assertEqual(self.filter_rows(output, b"S", "exclude"), ["unused|literal"])
                    self.assertEqual(self.filter_rows(output, b"O", "exclude-dir"), expected_dirs if overridden else [])
                    self.assertEqual(self.filter_rows(output, b"O", "exclude-ext"), expected_exts if overridden else [])

    def test_empty_groups_are_rejected_by_both_commands_and_engines(self):
        baseline, _, _ = self.catalog("python", [])
        for engine in ("python", "bash"):
            for command in ("catalog", "compare"):
                for flag in ("--exclude-dir", "--exclude-ext"):
                    for values in ([""], ["a", ""], None, ["--verbose"]):
                        with self.subTest(engine=engine, command=command, flag=flag, values=values):
                            self.invoke(engine, [flag, *(values or [])], command=command,
                                        baseline=baseline if command == "compare" else None, status=2)
                    for value in ("", "|a", "a|", "a||b"):
                        with self.subTest(engine=engine, command=command, flag=flag, value=value):
                            self.invoke(engine, [flag + "=" + value], command=command,
                                        baseline=baseline if command == "compare" else None, status=2)

    def test_other_options_stop_chains_and_engine_removal_keeps_boundary(self):
        for engine in ("python", "bash"):
            for boundary in (["--verbose"], ["-v"], ["--clear-exclude-ext"], ["--jobs", "1"],
                             ["--unknown"], ["--engine", engine], ["--engine=" + engine], ["--"]):
                with self.subTest(engine=engine, boundary=boundary):
                    self.invoke(engine, ["--exclude-dir", "a", *boundary, "extra"], status=2)
            _, settings, _ = self.catalog(engine, ["--exclude-dir", "a", "b", "--engine", engine,
                                                   "--exclude-ext=tmp|bak"])
            self.assertEqual(settings["exclude-dir"], ["a", "b"])
        _, settings, _ = self.catalog("bash", ["--exclude-dir", "a", "--engine", "bash",
                                               "--jobs", "1", "--engine", "python"])
        self.assertEqual(settings["engine"], "python")

    def test_non_list_options_and_option_like_paths_remain_literal(self):
        for engine in ("python", "bash"):
            for log_name in ("log|--exclude-dir=a|b", "--engine=bash"):
                log_options = ["--log-file", self.work / log_name] if not log_name.startswith("--") else ["--log-file=" + log_name]
                _, settings, _ = self.catalog(engine, ["--exclude-dir", "a", "b", *log_options,
                                                        "--exclude", "unused|literal"])
                self.assertEqual(settings["exclude"], ["unused|literal"])
                self.assertEqual(settings["exclude-dir"], ["a", "b"])
                self.assertTrue((self.work / log_name).exists())
                (self.work / log_name).unlink()
            self.invoke(engine, ["--exclude", "a", "b"], status=2)
            self.invoke(engine, ["--exclude-dir", "a", "--log-file", "--engine=bash"], status=2)
            self.invoke(engine, ["--exclude-dir=a|b", "extra"], status=2)
            self.invoke(engine, ["--exclude-dir", "-dash"], status=2)
            _, settings, _ = self.catalog(engine, ["--exclude-dir=-dash|-other", "--exclude-ext", "-1", "-.5"])
            self.assertEqual(settings["exclude-dir"], ["-dash", "-other"])
            self.assertEqual(settings["exclude-ext"], ["-1", "-.5"])

    def test_double_dash_preserves_filter_like_target(self):
        target = self.work / "--exclude-dir=a|b"
        target.mkdir()
        for engine in ("python", "bash"):
            self.serial += 1
            output = self.work / ("literal-%d" % self.serial)
            result = run_cli(["catalog", "--output", output, "--exclude-dir", "a", "b",
                              "--", target.name], engine=engine, cwd=self.work)
            self.assertEqual(result.returncode, 0, result.stderr)
            settings, _, _, _ = read_catalog(output, workspace=self.work)
            self.assertEqual(settings["exclude-dir"], ["a", "b"])

    def test_output_and_catalog_paths_with_pipes_and_filter_substrings(self):
        for engine in ("python", "bash"):
            baseline = self.work / (engine + "|--exclude-dir=a|b")
            result = run_cli(["catalog", self.tree, "--exclude-dir", "a", "b",
                              "--output", baseline], engine=engine, cwd=self.work)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = self.work / (engine + "|--exclude-ext=log|tmp")
            result = run_cli(["compare", self.tree, baseline,
                              "--output", report], engine=engine, cwd=self.work)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.filter_rows(report, b"S", "exclude-dir"), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
