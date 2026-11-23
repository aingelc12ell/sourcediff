import errno
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from tests.support import GOLDEN_CATALOG, PROJECT_ROOT, run_cli


@unittest.skipUnless(sys.platform.startswith("linux"), "requires the Linux launcher")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.source = self.work / "src"
        self.source.mkdir()
        self.baseline = self.work / "baseline"
        self.baseline.write_bytes(GOLDEN_CATALOG)

    def assert_cli_error(self, args, fragment, *, engine=None):
        result = run_cli(args, cwd=self.work, engine=engine)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(fragment, result.stderr)

    def test_help_documents_commands_and_follow_risk(self):
        result = run_cli(["--help"], cwd=self.work, engine=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"catalog", result.stdout)
        self.assertIn(b"compare", result.stdout)
        result = run_cli(["catalog", "--help"], cwd=self.work, engine=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"--directory-hashes", result.stdout)
        self.assertIn(b"outside", result.stdout)

    def test_default_and_explicit_python_engine_from_another_cwd(self):
        for args in (
            ["--version"],
            ["--engine", "python", "--version"],
            ["catalog", "--engine=python", "--version"],
        ):
            with self.subTest(args=args):
                result = run_cli(args, cwd=self.work, engine=None)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(b"python", result.stdout.lower())

    def test_symlinked_launcher_preserves_cwd_and_non_utf8_target(self):
        alias = self.work / "source diff"
        alias.symlink_to(PROJECT_ROOT / "sourcediff")
        os.mkdir(os.fsencode(self.work) + b"/-src\xff\n")
        (self.work / "occupied").write_bytes(b"original")
        result = subprocess.run(
            [alias, "catalog", "--output", "occupied", "--", b"-src\xff\n"],
            cwd=self.work,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(b"already exists", result.stderr)

    def test_explicit_bash_dispatch_does_not_require_python(self):
        executables = self.work / "bin"
        executables.mkdir()
        for name in ("bash", "readlink"):
            (executables / name).symlink_to(shutil.which(name))
        installed = self.work / "installation with spaces"
        installed.mkdir()
        launcher = installed / "sourcediff"
        shutil.copy2(PROJECT_ROOT / "sourcediff", launcher)
        bash_engine = installed / "lib" / "bash" / "main.sh"
        bash_engine.parent.mkdir(parents=True)
        bash_engine.write_text('printf "%s\\0" "$PWD" "$@"\n')
        result = subprocess.run(
            [launcher, "catalog", "--engine=bash", b"\xff\n"],
            cwd=self.work,
            env={**os.environ, "PATH": str(executables)},
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(b"\0"), [os.fsencode(self.work), b"catalog", b"\xff\n", b""])
        result = subprocess.run(
            [launcher, "--version"],
            cwd=self.work,
            env={**os.environ, "PATH": str(executables)},
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(b"python3", result.stderr)
        bash_engine.unlink()
        result = subprocess.run(
            [launcher, "--engine", "bash", "--version"],
            cwd=self.work,
            env={**os.environ, "PATH": str(executables)},
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(b"Bash engine", result.stderr)

    def test_partial_python_installation_reports_dependency_error(self):
        installation = self.work / "installation"
        installation.mkdir()
        launcher = installation / "sourcediff"
        shutil.copy2(PROJECT_ROOT / "sourcediff", launcher)
        package = installation / "lib" / "sourcediff_py"
        package.mkdir(parents=True)
        for filename in ("__init__.py", "cli.py", "format.py"):
            shutil.copy2(PROJECT_ROOT / "lib" / "sourcediff_py" / filename, package)
        result = subprocess.run([launcher, "--version"], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(b"installation", result.stderr)
        self.assertIn(b"sourcediff_py.output", result.stderr)

    def test_unknown_flags_and_invalid_engine_are_usage_errors(self):
        for args, fragment in (
            (["catalog", "src", "--unknown"], b"--unknown"),
            (["catalog", "src", "--hashes"], b"--hashes"),
            (["catalog", "src", "--engine", "unknown"], b"--engine"),
            (["catalog", "src", "--engine"], b"--engine"),
        ):
            with self.subTest(args=args):
                self.assert_cli_error(args, fragment)

    def test_target_is_required_and_must_be_a_directory(self):
        for args, fragment in (
            (["catalog"], b"TARGET"),
            (["catalog", "absent"], b"absent"),
            (["catalog", "baseline"], b"directory"),
            (["catalog", "src", "--output", "src", "--overwrite"], b"scan target"),
        ):
            with self.subTest(args=args):
                self.assert_cli_error(args, fragment)

    def test_rejects_invalid_jobs_depth_and_hash(self):
        for args in (
            ["--jobs", "0"],
            ["--jobs", "-1"],
            ["--jobs", "1.5"],
            ["--max-depth", "-1"],
            ["--max-depth", "9223372036854775808"],
            ["--hash", "md5"],
        ):
            with self.subTest(args=args):
                self.assert_cli_error(["catalog", "src", *args], os.fsencode(args[0]))

    def test_blake3_dependency_is_required_before_scanning(self):
        executables = self.work / "bin"
        executables.mkdir()
        for name in ("bash", "readlink", "python3"):
            (executables / name).symlink_to(shutil.which(name))
        result = subprocess.run(
            [PROJECT_ROOT / "sourcediff", "catalog", "src", "--hash=blake3"],
            cwd=self.work,
            env={**os.environ, "PATH": str(executables)},
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(b"b3sum", result.stderr)
        self.assertEqual(list(self.work.glob("catalog-*.txt")), [])

    def test_rejects_command_specific_options_in_the_wrong_command(self):
        for command, option in (
            ("compare", ["--hash", "sha256"]),
            ("catalog", ["--strict"]),
            ("catalog", ["--quick"]),
            ("catalog", ["--catalog", "baseline"]),
        ):
            with self.subTest(command=command, option=option):
                baseline_args = ["baseline"] if command == "compare" else []
                self.assert_cli_error([command, "src", *baseline_args, *option], os.fsencode(option[0]))
        self.assert_cli_error(["compare", "src"], b"CATALOG")

    def test_catalog_validates_output_collision_before_scanning(self):
        occupied = self.work / "occupied"
        occupied.write_bytes(b"original")
        self.assert_cli_error(["catalog", "src", "--output", "occupied"], b"already exists")
        self.assertEqual(occupied.read_bytes(), b"original")

    def test_invalid_output_spellings_reject_before_scanning(self):
        output = self.work / "output"
        output.write_bytes(b"original")
        for raw_path, fragment in (
            ("output/", b"output"),
            ("output/.", b"output"),
            ("missing/../output", b"missing"),
            ("output/../output", b"output"),
        ):
            with self.subTest(raw_path=raw_path):
                self.assert_cli_error(["catalog", "src", "--output", raw_path, "--overwrite"], fragment)
                self.assertEqual(output.read_bytes(), b"original")

    def test_compare_never_overwrites_input_catalog_aliases(self):
        symlink = self.work / "symbolic"
        symlink.symlink_to(self.baseline)
        hardlink = self.work / "hardlink"
        os.link(self.baseline, hardlink)
        alias_dir = self.work / "parent"
        alias_dir.symlink_to(self.work, target_is_directory=True)
        for alias in ("baseline", "symbolic", "hardlink", "parent/baseline"):
            with self.subTest(alias=alias):
                self.assert_cli_error(
                    ["compare", "src", "baseline", "--output", alias, "--overwrite"],
                    b"input catalog",
                )
        self.assertEqual(self.baseline.read_bytes(), GOLDEN_CATALOG)
        self.assertTrue(symlink.is_symlink())
        self.assertTrue(hardlink.samefile(self.baseline))

    def test_compare_rejects_incomplete_baseline_and_cleans_staging(self):
        self.baseline.write_bytes(
            GOLDEN_CATALOG.replace(b"S\tcomplete\tcomplete", b"S\tcomplete\tincomplete")
            .replace(b"END\tcomplete", b"END\tincomplete")
        )
        self.assert_cli_error(
            ["compare", "src", "baseline", "--output", "report"],
            b"incomplete",
        )
        self.assertFalse((self.work / "report").exists())
        self.assertEqual(list(self.work.glob(".sourcediff-*")), [])

    def test_compare_protects_catalog_opened_before_rename(self):
        self.baseline.unlink()
        os.mkfifo(self.baseline)
        report = self.work / "report"
        process = subprocess.Popen(
            [PROJECT_ROOT / "sourcediff", "compare", "src", "baseline",
             "--output", "report", "--overwrite"],
            cwd=self.work,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    writer = os.open(self.baseline, os.O_WRONLY | os.O_NONBLOCK)
                    break
                except OSError as error:
                    if error.errno != errno.ENXIO:
                        raise
                    if process.poll() is not None:
                        self.fail(process.communicate()[1])
                    self.assertLess(time.monotonic(), deadline, "catalog was never opened")
                    time.sleep(0.01)
            with os.fdopen(writer, "wb") as catalog_stream:
                self.baseline.rename(report)
                catalog_stream.write(GOLDEN_CATALOG)
            _, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 2, stderr)
            self.assertIn(b"input catalog", stderr)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
        self.assertTrue(stat.S_ISFIFO(report.stat().st_mode))
        self.assertEqual(list(self.work.glob(".sourcediff-*")), [])

    def test_signal_during_baseline_read_cleans_staging(self):
        # A FIFO keeps the catalog read blocked without depending on scan speed.
        self.baseline.unlink()
        os.mkfifo(self.baseline)
        report = self.work / "report"
        report.write_bytes(b"original")
        process = subprocess.Popen(
            [PROJECT_ROOT / "sourcediff", "compare", "src", "baseline",
             "--output", "report", "--overwrite"],
            cwd=self.work,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            while not list(self.work.glob(".sourcediff-*")):
                if process.poll() is not None:
                    self.fail(process.communicate()[1])
                self.assertLess(time.monotonic(), deadline, "staging was never created")
                time.sleep(0.01)
            process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 2, stderr)
            self.assertIn(b"SIGTERM", stderr)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
        self.assertEqual(report.read_bytes(), b"original")
        self.assertEqual(list(self.work.glob(".sourcediff-*")), [])

    def test_signal_at_staging_mkdtemp_return_cleans_only_owned_directory(self):
        report = self.work / "report"
        report.write_bytes(b"original")
        unrelated = self.work / ".sourcediff-unrelated"
        unrelated.mkdir()
        sentinel = unrelated / "sentinel"
        sentinel.write_bytes(b"unrelated data")
        created_path = self.work / "created-path"
        program = """
import os
import signal
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from sourcediff_py.cli import main

constructor = tempfile.mkdtemp.__code__

def interrupt_at_return(frame, event, value):
    if event == "return" and frame.f_code is constructor and frame.f_locals["prefix"] == ".sourcediff-":
        sys.setprofile(None)
        Path(sys.argv[2]).write_text(value, encoding="utf-8")
        os.kill(os.getpid(), signal.SIGTERM)

sys.setprofile(interrupt_at_return)
sys.exit(main(["compare", "src", "baseline", "--output", "report", "--overwrite"]))
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", program, PROJECT_ROOT / "lib", created_path],
            cwd=self.work, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(b"SIGTERM", result.stderr)
        self.assertTrue(created_path.exists(), "the real staging allocation was not reached")
        self.assertFalse(Path(created_path.read_text()).exists(), "the allocated staging directory leaked")
        self.assertEqual(report.read_bytes(), b"original")
        self.assertEqual(sentinel.read_bytes(), b"unrelated data")
        self.assertEqual(list(self.work.glob(".sourcediff-*")), [unrelated])

    def test_bash_signal_at_staging_mktemp_return_cleans_only_owned_directory(self):
        binaries = self.work / "bin"
        binaries.mkdir()
        created_path = self.work / "created-path"
        shim = binaries / "mktemp"
        shim.write_text(
            '#!/bin/bash\n'
            'directory=$(/usr/bin/mktemp "$@") || exit $?\n'
            'printf "%s\\n" "$directory"\n'
            'case "$directory" in\n'
            '    "$STAGING_PARENT"/.sourcediff-*)\n'
            '        printf "%s" "$directory" > "$CREATED_PATH"\n'
            '        kill -TERM "$PPID"\n'
            '        ;;\n'
            'esac\n', encoding="ascii",
        )
        shim.chmod(0o700)
        report = self.work / "report"
        report.write_bytes(b"original")
        unrelated = self.work / ".sourcediff-unrelated"
        unrelated.mkdir()
        sentinel = unrelated / "sentinel"
        sentinel.write_bytes(b"unrelated data")
        result = run_cli(
            ["catalog", "src", "--output", "report", "--overwrite"],
            cwd=self.work, engine="bash",
            env={**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}",
                 "CREATED_PATH": str(created_path), "STAGING_PARENT": str(self.work)},
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(b"SIGTERM", result.stderr)
        self.assertTrue(created_path.exists(), "the real staging allocation was not reached")
        self.assertFalse(Path(created_path.read_text()).exists(), "the allocated staging directory leaked")
        self.assertEqual(report.read_bytes(), b"original")
        self.assertEqual(sentinel.read_bytes(), b"unrelated data")
        self.assertEqual(list(self.work.glob(".sourcediff-*")), [unrelated])

    def test_default_catalog_output_is_in_the_invocation_directory(self):
        result = run_cli(["catalog", "src"], cwd=self.work, engine=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list(self.work.glob("catalog-??????????????.txt"))), 1)


class SettingsTests(unittest.TestCase):
    def test_default_timestamp_names_have_the_documented_prefixes(self):
        from sourcediff_py.cli import default_output

        for command, prefix in (("catalog", "catalog"), ("compare", "difference")):
            with self.subTest(command=command):
                output = default_output(command)
                self.assertEqual(output.parent, Path.cwd())
                self.assertRegex(output.name, rf"^{prefix}-[0-9]{{14}}\.txt$")

    def test_explicit_scope_replaces_saved_values_and_runtime_is_separate(self):
        from sourcediff_py.cli import create_parser, effective_settings
        from sourcediff_py.format import read_catalog

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "baseline"
            path.write_bytes(GOLDEN_CATALOG.replace(
                b"S\texclude\tlast\n", b"S\texclude\tlast\nS\texclude-dir\tsaved\n"
            ))
            saved, _, _, _ = read_catalog(path, workspace=temporary, require_complete=True)
        args = create_parser().parse_args([
            "compare", "src", "baseline", "--no-recursive", "--max-depth=0",
            "--exclude", "new", "--exclude", "newer", "--clear-exclude-dir",
            "--directory-hashes", "--strict", "--quick", "--fail-fast", "--jobs", "2",
        ])
        settings, overrides = effective_settings(args, saved)
        self.assertEqual(overrides, {
            "recursive": "false", "max-depth": "0", "exclude": ["new", "newer"],
            "exclude-dir": [], "directory-hashes": "true",
        })
        self.assertEqual(settings["hash"], "sha256")
        self.assertEqual(settings["symlinks"], "record")
        self.assertEqual(settings["exclude"], ["new", "newer"])
        self.assertEqual(settings["exclude-dir"], [])
        self.assertEqual(saved["exclude-dir"], ["saved"])
        self.assertEqual(saved["exclude"], ["first\t\udcff", "last"])
        self.assertTrue(args.strict and args.quick and args.fail_fast)
        self.assertEqual(args.jobs, 2)
        self.assertTrue({"strict", "quick", "fail-fast", "jobs", "output"}.isdisjoint(settings))

    def test_catalog_settings_use_defaults_and_comparison_inherits_saved_scope(self):
        from sourcediff_py.cli import create_parser, effective_settings

        parser = create_parser()
        fresh, _ = effective_settings(parser.parse_args(["catalog", "src"]))
        self.assertEqual(fresh["hash"], "sha256")
        self.assertEqual(fresh["recursive"], "true")
        self.assertEqual(fresh["max-depth"], "-")
        self.assertEqual(fresh["symlinks"], "record")
        self.assertEqual(fresh["directory-hashes"], "false")
        self.assertEqual(fresh["exclude-ext"], [])
        saved = {**fresh, "hash": "blake3", "recursive": "false", "exclude-ext": [".txt"]}
        effective, overrides = effective_settings(
            parser.parse_args(["compare", "src", "baseline"]), saved
        )
        self.assertEqual(effective, saved)
        self.assertEqual(overrides, {})


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux output publication")
class PublicationTests(unittest.TestCase):
    def test_publish_rejects_invalid_raw_destination(self):
        from sourcediff_py.output import output_workspace, publish

        with TemporaryDirectory() as temporary:
            destination = Path(temporary) / "output"
            destination.write_bytes(b"original")
            for suffix in ("output/", "output/.", "missing/../output", "output/../output"):
                with self.subTest(suffix=suffix):
                    with output_workspace(destination, overwrite=True) as workspace:
                        staged = workspace / "result"
                        staged.write_bytes(b"replacement")
                        with self.assertRaises((OSError, ValueError)):
                            publish(staged, temporary + "/" + suffix, overwrite=True)
                    self.assertEqual(destination.read_bytes(), b"original")

    def test_publish_accepts_a_valid_symlinked_parent(self):
        from sourcediff_py.output import output_workspace, publish

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "actual"
            parent.mkdir()
            alias = root / "alias"
            alias.symlink_to(parent, target_is_directory=True)
            destination = alias / "output"
            with output_workspace(destination, overwrite=False) as workspace:
                self.assertEqual(workspace.parent, parent)
                staged = workspace / "result"
                staged.write_bytes(b"result")
                publish(staged, destination, overwrite=False)
            self.assertEqual((parent / "output").read_bytes(), b"result")

    def test_publish_rejects_catalog_moved_to_destination_after_preflight(self):
        from sourcediff_py.output import output_workspace, publish

        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            destination = directory / "report"
            baseline = directory / "baseline"
            baseline.write_bytes(b"protected catalog")
            with output_workspace(destination, overwrite=True, forbidden_paths=(baseline,)) as workspace:
                with baseline.open("rb") as catalog_stream:
                    identity = os.fstat(catalog_stream.fileno())
                    baseline.rename(destination)
                    staged = workspace / "result"
                    staged.write_bytes(b"replacement")
                    with self.assertRaisesRegex(ValueError, "input catalog"):
                        publish(staged, destination, overwrite=True, forbidden_paths=(baseline,),
                                forbidden_identities=(identity,))
            self.assertEqual(destination.read_bytes(), b"protected catalog")

    def test_output_created_after_preflight_is_never_clobbered(self):
        from sourcediff_py.output import output_workspace, publish

        with TemporaryDirectory() as temporary:
            destination = Path(temporary) / "output"
            with output_workspace(destination, overwrite=False) as workspace:
                self.assertEqual(workspace.parent, destination.parent)
                staged = workspace / "result"
                staged.write_bytes(b"ours")
                destination.write_bytes(b"racer")
                with self.assertRaises(FileExistsError):
                    publish(staged, destination, overwrite=False)
                self.assertEqual(destination.read_bytes(), b"racer")
            self.assertFalse(workspace.exists())

    def test_concurrent_publish_has_one_winner_and_never_overwrites(self):
        from sourcediff_py.output import output_workspace, publish

        with TemporaryDirectory() as temporary:
            destination = Path(temporary) / "output"
            barrier = Barrier(2, timeout=5)

            def try_publish(payload):
                with output_workspace(destination, overwrite=False) as workspace:
                    staged = workspace / "result"
                    staged.write_bytes(payload)
                    barrier.wait()
                    try:
                        publish(staged, destination, overwrite=False)
                    except FileExistsError:
                        return None
                    return payload

            with ThreadPoolExecutor(max_workers=2) as executor:
                winners = [result for result in executor.map(try_publish, [b"one", b"two"]) if result]
            self.assertEqual(len(winners), 1)
            self.assertEqual(destination.read_bytes(), winners[0])
            self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_publish_rechecks_aliases_and_overwrite_replaces_complete_bytes(self):
        from sourcediff_py.output import output_workspace, publish

        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            destination = directory / "report"
            baseline = directory / "baseline"
            baseline.write_bytes(b"protected")
            with output_workspace(destination, overwrite=True, forbidden_paths=(baseline,)) as workspace:
                staged = workspace / "result"
                staged.write_bytes(b"replacement")
                destination.symlink_to(baseline)
                with self.assertRaisesRegex(ValueError, "input catalog"):
                    publish(staged, destination, overwrite=True, forbidden_paths=(baseline,))
                destination.unlink()
                destination.write_bytes(b"old result")
                publish(staged, destination, overwrite=True, forbidden_paths=(baseline,))
                self.assertFalse(staged.exists())
            self.assertEqual(baseline.read_bytes(), b"protected")
            self.assertEqual(destination.read_bytes(), b"replacement")

    def test_dangling_symlink_counts_as_an_existing_output(self):
        from sourcediff_py.output import output_workspace

        with TemporaryDirectory() as temporary:
            destination = Path(temporary) / "out"
            destination.symlink_to("absent")
            with self.assertRaises(FileExistsError):
                with output_workspace(destination, overwrite=False):
                    self.fail("preflight accepted a dangling symlink")


if __name__ == "__main__":
    unittest.main()
