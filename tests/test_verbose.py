"""Public operational logging contracts, independent of catalog/report records."""
import os
import re
import resource
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT, run_cli


class VerboseTests(unittest.TestCase):
    def test_verbose_fatal_diagnostic_escapes_control_bytes(self):
        for engine in ("python", "bash"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                result = run_cli(["catalog", str(Path(tmp) / "missing\n\x1b"), "-v"], engine=engine)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn(b"\x1b", result.stderr)
                self.assertIn(b"%0A%1B", result.stderr)

    def test_logging_sinks_and_identical_artifacts(self):
        for engine in ("python", "bash"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); tree = root / "tree"; tree.mkdir()
                (tree / "a\n%\t").write_bytes(b"hello")
                (tree / "link").symlink_to("a\n%\t")
                quiet = root / "quiet"; logged = root / "logged"; log = root / "events"
                args = ["catalog", str(tree), "--output"]
                result = run_cli([*args, str(quiet)], engine=engine)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b"")
                result = run_cli([*args, str(logged), "-v", "--log-file", str(log)], engine=engine)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(quiet.read_bytes(), logged.read_bytes())
                self.assertEqual(result.stderr, log.read_bytes())
                self.assertIn(b"phase=scan", result.stderr)
                self.assertIn(b"HASHED", result.stderr)
                self.assertIn(b"LINK", result.stderr)
                self.assertIn(b"a%0A%25%09", result.stderr)
                self.assertIn(b"status=0", result.stderr)
                self.assertIn(b"completed", result.stderr)
                for line in result.stderr.splitlines():
                    self.assertRegex(line, rb"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ")
                    self.assertTrue(all(32 <= byte < 127 for byte in line), line)
                fileonly = root / "fileonly"
                result = run_cli([*args, str(logged), "--overwrite", "--log-file", str(fileonly)], engine=engine)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b"")
                self.assertIn(b"SUMMARY", fileonly.read_bytes())

    def test_comparison_quick_discrepancies_and_errors(self):
        for engine in ("python", "bash"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); tree = root / "tree"; tree.mkdir(); (tree / "a").write_text("a")
                baseline = root / "baseline"; report = root / "report"
                self.assertEqual(run_cli(["catalog", str(tree), "--output", str(baseline)], engine=engine).returncode, 0)
                args = ["compare", str(tree), str(baseline), "--output", str(report), "--quick"]
                result = run_cli([*args, "--verbose"], engine=engine)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(b"REUSED", result.stderr)
                self.assertIn(b"reused=1", result.stderr)
                reference = report.read_bytes()
                result = run_cli([*args, "--overwrite"], engine=engine)
                self.assertEqual(reference, report.read_bytes())
                (tree / "b").write_text("b")
                result = run_cli([*args, "--overwrite", "-v"], engine=engine)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(b"ADDED", result.stderr)
                self.assertIn(b"status=1", result.stderr)
                self.assertIn(b"completed", result.stderr)
                os.mkfifo(tree / "fifo")
                result = run_cli([*args, "--overwrite", "-v"], engine=engine)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(b"ERROR", result.stderr)
                self.assertIn(b"errors=1", result.stderr)
                self.assertIn(b"incomplete", result.stderr)

    def test_log_exclusion_directory_hashes_and_policy(self):
        for engine in ("python", "bash"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); tree = root / "tree"; tree.mkdir(); (tree / "a").write_text("a")
                baseline = root / "baseline"; report = root / "report"; log = tree / "events"
                args = ["catalog", str(tree), "--output", str(baseline), "--directory-hashes"]
                self.assertEqual(run_cli([*args, "--log-file", str(log)], engine=engine).returncode, 0)
                self.assertNotIn(b"E\tevents\t", baseline.read_bytes())
                result = run_cli(["compare", str(tree), str(baseline), "--output", str(report),
                                  "--log-file", str(log), "--overwrite"], engine=engine)
                self.assertEqual(result.returncode, 0, result.stderr)
                before = log.read_bytes()
                result = run_cli(["catalog", str(tree), "--output", str(root / "new"),
                                  "--log-file", str(log)], engine=engine)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(log.read_bytes(), before)

    def test_baseline_projection_excludes_new_log_binding(self):
        for engine in ("python", "bash"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); tree = root / "tree"; tree.mkdir()
                (tree / "a").write_text("a"); (tree / "events").write_text("old content")
                baseline = root / "baseline"; report = root / "report"
                self.assertEqual(run_cli(["catalog", str(tree), "--directory-hashes", "--output", str(baseline)],
                                         engine=engine).returncode, 0)
                result = run_cli(["compare", str(tree), str(baseline), "--output", str(report),
                                  "--overwrite", "--log-file", str(tree / "events")], engine=engine)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(b"MISSING\tevents\t", report.read_bytes())
                self.assertNotIn(b"CONTENT_CHANGED", report.read_bytes())

    def test_log_write_failure_is_not_success(self):
        def limit_log():
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE, (128, 128))

        for engine in ("python", "bash"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); tree = root / "tree"; tree.mkdir(); (tree / "a").write_text("a")
                output = root / "output"; log = root / "events"
                result = subprocess.run(["bash", PROJECT_ROOT / "sourcediff", "--engine", engine, "catalog",
                                         str(tree), "--output", str(output), "--log-file", str(log)],
                                        preexec_fn=limit_log, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(b"sourcediff:", result.stderr)
                self.assertNotIn(b"outcome=completed", log.read_bytes())
                self.assertFalse(output.exists())
                self.assertFalse(list(root.glob(".sourcediff-*")))

    def test_directory_hash_failure_is_logged_once(self):
        for engine in ("python", "bash"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); tree = root / "tree"; tree.mkdir(); (tree / "a").write_text("hello")
                tools = root / "bin"; tools.mkdir(); wrapper = tools / "b3sum"
                wrapper.write_text('#!/bin/bash\ninput=$(mktemp "$HASH_TMP/hash.XXXXXX")\n'
                                   'trap \'rm -f "$input"\' EXIT\ncat >"$input"\n'
                                   'if grep -aq "^file" "$input"; then exit 1; fi\n'
                                   f'"{shutil.which("b3sum")}" - <"$input"\n')
                wrapper.chmod(0o755)
                env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "HASH_TMP": str(root)}
                result = run_cli(["catalog", str(tree), "--hash", "blake3", "--directory-hashes", "-v",
                                  "--output", str(root / "output")], engine=engine, env=env)
                self.assertEqual(result.returncode, 2, result.stderr)
                errors = [line for line in result.stderr.splitlines() if b" ERROR " in line]
                self.assertEqual(len(errors), 1, result.stderr)
                self.assertIn(b"operation=hash-directory", errors[0])
                self.assertIn(b"errors=1", result.stderr)

    def test_failed_file_worker_logs_once_and_completes_work_count(self):
        for engine in ("python", "bash"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); tree = root / "tree"; tree.mkdir(); (tree / "a").write_text("hello")
                tools = root / "bin"; tools.mkdir(); wrapper = tools / "b3sum"; log = root / "log"
                wrapper.write_text('#!/bin/bash\ninput=$(mktemp "$HASH_TMP/hash.XXXXXX")\n'
                                   'trap \'rm -f "$input"\' EXIT\ncat >"$input"\n'
                                   '[[ ! -s $input ]] || exit 1\n'
                                   f'"{shutil.which("b3sum")}" - <"$input"\n')
                wrapper.chmod(0o755)
                env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "HASH_TMP": str(root)}
                result = run_cli(["catalog", str(tree), "--hash", "blake3", "-v", "--log-file", str(log),
                                  "--output", str(root / "output")], engine=engine, env=env)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stderr, log.read_bytes())
                errors = [line for line in result.stderr.splitlines() if b" ERROR " in line]
                self.assertEqual(len(errors), 1, result.stderr)
                self.assertIn(b"path=a operation=hash-file", errors[0])
                self.assertIn(b"status=2 completed=1 hashed=0 reused=0 errors=1", result.stderr)

    def test_alias_protection_and_raw_path_validation(self):
        for engine in ("python", "bash"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); tree = root / "tree"; tree.mkdir(); (tree / "a").write_text("a")
                baseline = root / "baseline"; output = root / "output"
                self.assertEqual(run_cli(["catalog", str(tree), "--output", str(baseline)], engine=engine).returncode, 0)
                before = baseline.read_bytes()
                hard = root / "hard"; os.link(baseline, hard)
                symlink = root / "sym"; symlink.symlink_to(baseline)
                tree_alias = root / "tree-alias"; tree_alias.symlink_to(tree)
                for log in (baseline, hard, symlink, output, tree, tree_alias, root / "missing/../bad"):
                    result = run_cli(["compare", str(tree), str(baseline), "--output", str(output),
                                      "--log-file", str(log), "--overwrite"], engine=engine)
                    self.assertEqual(result.returncode, 2, (engine, log, result.stderr))
                    self.assertEqual(baseline.read_bytes(), before)
                    self.assertFalse(output.exists())
                output.write_bytes(b"preserve output")
                out_alias = root / "output-alias"; os.link(output, out_alias)
                result = run_cli(["catalog", str(tree), "--output", str(output), "--overwrite",
                                  "--log-file", str(out_alias)], engine=engine)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(output.read_bytes(), b"preserve output")
                output.unlink()
                result = run_cli(["catalog", str(tree), "--output", str(output), "--log-file", "--engine=bash"],
                                 cwd=root, engine=engine)
                self.assertEqual(result.returncode, 2)

    def test_heartbeat_and_interruption_during_real_hash(self):
        for engine in ("python", "bash"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); tree = root / "tree"; tree.mkdir(); (tree / "a").write_bytes(b"a" * 2000000)
                tools = root / "bin"; tools.mkdir(); gate = root / "gate"; started = root / "started"
                real = shutil.which("b3sum")
                self.assertIsNotNone(real)
                wrapper = tools / "b3sum"
                # Probe calls have empty input. The real hasher starts before its
                # output is gated, so this exercises coordinator wait loops.
                wrapper.write_text('#!/bin/bash\ninput=$(mktemp "$HASH_TMP/hash.XXXXXX")\ncat >"$input"\n'
                                   f'"{real}" - <"$input" >"$input.out"\n'
                                   'if [[ -s $input ]]; then touch "$STARTED"; while [[ ! -e $GATE ]]; do sleep .1; done; fi\n'
                                   'cat "$input.out"\nrm -f "$input" "$input.out"\n')
                wrapper.chmod(0o755)
                env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "GATE": str(gate),
                       "STARTED": str(started), "HASH_TMP": str(root)}
                output = root / "output"; log = root / "events"
                command = ["bash", str(PROJECT_ROOT / "sourcediff"), "--engine", engine, "catalog", str(tree),
                           "--hash", "blake3", "--output", str(output), "--log-file", str(log), "-v"]
                process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                try:
                    deadline = time.monotonic() + 10
                    while not started.exists() and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertTrue(started.exists())
                    time.sleep(2.7)
                    data = log.read_bytes()
                    ticks = [line for line in data.splitlines() if b" PROGRESS " in line and b"active=1" in line]
                    self.assertGreaterEqual(len(ticks), 2, data)
                    process.send_signal(signal.SIGTERM)
                    _, stderr = process.communicate(timeout=8)
                    self.assertEqual(process.returncode, 2, stderr)
                    self.assertIn(b"status=2", log.read_bytes())
                    self.assertIn(b"incomplete", log.read_bytes())
                    self.assertFalse(output.exists())
                    self.assertFalse(list(root.glob(".sourcediff-*")))
                finally:
                    gate.touch()
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()

    def test_output_rechecks_active_log_alias_before_publication(self):
        for engine in ("python", "bash"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); tree = root / "tree"; tree.mkdir(); (tree / "a").write_text("hello")
                tools = root / "bin"; tools.mkdir(); wrapper = tools / "b3sum"
                gate = root / "gate"; started = root / "started"; output = root / "output"; log = root / "log"
                wrapper.write_text('#!/bin/bash\ninput=$(mktemp "$HASH_TMP/hash.XXXXXX")\n'
                                   'trap \'rm -f "$input" "$input.out"\' EXIT\ncat >"$input"\n'
                                   f'"{shutil.which("b3sum")}" - <"$input" >"$input.out"\n'
                                   'if [[ -s $input ]]; then touch "$STARTED"; while [[ ! -e $GATE ]]; do sleep .05; done; fi\n'
                                   'cat "$input.out"\n')
                wrapper.chmod(0o755)
                env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "HASH_TMP": str(root),
                       "GATE": str(gate), "STARTED": str(started)}
                process = subprocess.Popen(["bash", PROJECT_ROOT / "sourcediff", "--engine", engine, "catalog",
                                            str(tree), "--hash", "blake3", "--output", str(output), "--overwrite",
                                            "--log-file", str(log)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           start_new_session=True)
                try:
                    deadline = time.monotonic() + 10
                    while not started.exists() and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertTrue(started.exists())
                    # Move the opened log before rebinding the output, exercising
                    # identity protection even after the requested log name disappears.
                    moved = root / "moved-log"; log.rename(moved); os.link(moved, output)
                    gate.touch()
                    _, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 2, stderr)
                    self.assertTrue(output.samefile(moved))
                    self.assertIn(b"status=2", moved.read_bytes())
                    self.assertNotIn(b"SOURCEDIFF\t", output.read_bytes())
                finally:
                    gate.touch()
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
