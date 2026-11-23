"""Controlled runtime failures through the public CLI and scanner hash seam."""

import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
from contextlib import contextmanager, suppress
from pathlib import Path

from sourcediff_py.format import iter_entries, iter_errors, read_catalog
from tests import test_engines
from tests.support import PROJECT_ROOT, run_cli


ENGINES = ("python", "bash")
BLAKE3 = shutil.which("b3sum")


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux process and filesystem behavior")
class FailureTests(test_engines.EngineFixture):
    def report(self, engine, baseline, *options):
        output = self.work / "report"
        result = run_cli(["compare", self.source, baseline, "--output", output,
                          "--overwrite", *options], cwd=self.work, engine=engine)
        self.assertEqual(result.returncode, 2, result.stderr)
        report = output.read_bytes()
        self.assertTrue(report.endswith(b"END\tincomplete\t2\n"))
        self.assertFalse(list(self.work.glob(".sourcediff-*")))
        return report

    def test_real_access_errors_are_unknown_without_hiding_readable_changes(self):
        if os.geteuid() == 0:
            self.skipTest("permission-denied coverage requires an unprivileged Linux user")
        self.put(b"locked/child", b"old")
        self.put(b"unreadable", b"old")
        self.put(b"readable", b"old")
        baseline, *_ = self.catalog("python", "baseline", "--directory-hashes")
        self.put(b"readable", b"changed")
        locked, unreadable = self.source / "locked", self.source / "unreadable"
        for path, mode in ((locked, 0o700), (unreadable, 0o600)):
            self.addCleanup(path.chmod, mode)
            path.chmod(0)
        # Prove the fixture is actually denied by the filesystem and current user.
        with self.assertRaises(PermissionError):
            list(locked.iterdir())
        with self.assertRaises(PermissionError):
            unreadable.read_bytes()
        for engine in ENGINES:
            with self.subTest(engine=engine):
                report = self.report(engine, baseline)
                for path in (b".", b"locked", b"locked/child", b"unreadable"):
                    self.assertIn(b"UNKNOWN\t" + path + b"\ttarget-scan-error\t", report)
                self.assertIn(b"CONTENT_CHANGED\treadable\tcounted\t", report)
                self.assertNotIn(b"MISSING\t", report)
                error_rows = [row.split(b"\t") for row in report.splitlines() if row.startswith(b"ERROR\t")]
                self.assertEqual({(row[1], row[-1]) for row in error_rows},
                                 {(b"locked", b"subtree"), (b"unreadable", b"entry")})

    def test_partial_catalog_without_trailer_is_rejected_before_publication(self):
        self.put(b"file", b"data")
        baseline, *_ = self.catalog("python", "baseline")
        partial = baseline.read_bytes().rsplit(b"END\t", 1)[0]
        baseline.write_bytes(partial)
        output = self.work / "report"
        output.write_bytes(b"existing report")
        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = run_cli(["compare", self.source, baseline, "--output", output,
                                  "--overwrite"], cwd=self.work, engine=engine)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(b"trailer", result.stderr)
                self.assertEqual(output.read_bytes(), b"existing report")
                self.assertEqual(baseline.read_bytes(), partial)
                self.assertFalse(list(self.work.glob(".sourcediff-*")))

    def test_public_commands_protect_input_identity_and_exclude_workspaces(self):
        self.put(b"file", b"data")
        baseline, *_ = self.catalog("python", "baseline", "--directory-hashes")
        baseline.rename(self.source / "baseline")
        baseline = self.source / "baseline"
        alias = self.work / "alias"
        os.link(baseline, alias)
        original = baseline.read_bytes()
        output = self.source / "report"
        for engine in ENGINES:
            with self.subTest(engine=engine):
                for forbidden in (baseline, alias):
                    result = run_cli(["compare", self.source, baseline, "--output", forbidden,
                                      "--overwrite"], cwd=self.work, engine=engine)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertTrue(os.path.samefile(baseline, alias))
                    self.assertEqual(baseline.read_bytes(), original)
                result = run_cli(["compare", self.source, baseline, "--output", output,
                                  "--overwrite"], cwd=self.work, engine=engine)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = output.read_bytes()
                self.assertNotIn(b".sourcediff-", report)
                self.assertTrue(report.endswith(b"END\tcomplete\t0\n"))
                self.assertFalse(list(self.source.glob(".sourcediff-*")))

    def test_fifo_replaces_a_regular_file_without_blocking_or_reporting_missing(self):
        self.put(b"unsupported", b"old")
        baseline, *_ = self.catalog("python", "baseline")
        child = self.source / "unsupported"
        child.unlink()
        os.mkfifo(child)
        for engine in ENGINES:
            with self.subTest(engine=engine):
                report = self.report(engine, baseline)
                self.assertIn(b"UNKNOWN\tunsupported\ttarget-scan-error\t", report)
                self.assertIn(b"ERROR\tunsupported\tentry-type\t", report)
                self.assertNotIn(b"MISSING\tunsupported\t", report)

    def assert_observation_error(self, output):
        _, entries, errors, complete = read_catalog(output, self.work)
        self.assertFalse(complete)
        rows = list(iter_entries(entries))
        self.assertEqual([(row["path"], row["digest"], row["verification"]) for row in rows],
                         [(b".", "-", "unverified")])
        self.assertEqual([(row["path"], row["operation"], row["scope"]) for row in iter_errors(errors)],
                         [(b"file", "hash-file", "entry")])

    def test_python_in_place_change_and_vanished_entry_during_sha256_hash(self):
        program = """
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from sourcediff_py import scan
from sourcediff_py.cli import main

root, action, output = sys.argv[2:]
file = Path(root) / "file"
hash_chunks = scan._hash_chunks

def change_after_read(chunks, algorithm, cancelled):
    digest = hash_chunks(chunks, algorithm, cancelled)
    if action == "change":
        file.write_bytes(b"new length")
    else:
        file.unlink()
    return digest

scan._hash_chunks = change_after_read
sys.exit(main(["catalog", root, "--directory-hashes", "--jobs", "1", "--output", output]))
"""
        for action in ("change", "vanish"):
            with self.subTest(action=action):
                self.put(b"file", b"old")
                output = self.work / action
                result = subprocess.run([sys.executable, "-B", "-c", program, PROJECT_ROOT / "lib",
                                         self.source, action, output],
                                        cwd=self.work, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assert_observation_error(output)

    def test_python_fail_fast_stops_refilling_queue_and_invalidates_open_directory(self):
        self.put(b"a-vanishes", b"old")
        for name in (b"first", b"second", b"last"):
            self.put(b"z-open/" + name, b"old")
        baseline, *_ = self.catalog("python", "baseline", "--directory-hashes")
        output = self.work / "fail-fast-report"
        # Arrange traversal explicitly because directory iteration order varies by filesystem.
        program = """
import sys
from contextlib import contextmanager
from pathlib import Path
from threading import Event

sys.path.insert(0, sys.argv[1])
from sourcediff_py import scan
from sourcediff_py.cli import main

root, baseline, output = sys.argv[2:]
directory, hash_file, wait = scan._directory, scan._hash_file, scan.wait
release = Event()

@contextmanager
def ordered_directory(*args):
    with directory(*args) as (fd, children):
        yield fd, iter(sorted(children, key=lambda child: child.name))

def synchronized_hash(fd, name, *args):
    if name == b"a-vanishes":
        if not release.wait(5):
            raise RuntimeError("the scheduler did not wait for outstanding hashes")
        (Path(root) / "a-vanishes").unlink()
    return hash_file(fd, name, *args)

def release_on_block(futures, *, timeout, return_when):
    if timeout is None:
        release.set()
        if len(futures) > 2:
            raise RuntimeError("a single worker queued more than one additional task")
    return wait(futures, timeout=timeout, return_when=return_when)

scan._directory = ordered_directory
scan._hash_file = synchronized_hash
scan.wait = release_on_block
sys.exit(main(["compare", root, baseline, "--output", output,
               "--jobs", "1", "--fail-fast"]))
"""
        result = subprocess.run([sys.executable, "-B", "-c", program, PROJECT_ROOT / "lib",
                                 self.source, baseline, output],
                                cwd=self.work, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 2, result.stderr)
        report = output.read_bytes()
        self.assertTrue(report.endswith(b"END\tincomplete\t2\n"))
        self.assertIn(b"ERROR\t.\taborted\t", report)
        self.assertIn(b"ERROR\ta-vanishes\thash-file\t", report)
        for path in (b"z-open", b"z-open/last", b"z-open/second"):
            self.assertIn(b"UNKNOWN\t" + path + b"\ttarget-scan-error\t", report)
        self.assertIn(b"T\tfiles-hashed\t1\n", report)
        self.assertIn(b"T\tdirectory-digests-verified\t0\n", report)
        self.assertNotIn(b"MISSING\t", report)

    def controlled_hasher(self, body, **environment):
        binaries = self.work / "bin"
        binaries.mkdir(exist_ok=True)
        hasher = binaries / "b3sum"
        hasher.write_text("#!/bin/bash\n" + body, encoding="ascii")
        hasher.chmod(0o700)
        return {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", **environment}

    @unittest.skipUnless(BLAKE3, "requires b3sum for a real digest after the controlled mutation")
    def test_bash_in_place_change_and_vanished_entry_during_blake3_hash(self):
        for action, change in (("change", 'printf "new length" > "$HASH_INPUT"\n'),
                               ("vanish", 'rm -- "$HASH_INPUT"\n')):
            with self.subTest(action=action):
                self.put(b"file", b"old")
                environment = self.controlled_hasher(
                    change + 'exec "$REAL_B3SUM" "$@"\n',
                    HASH_INPUT=str(self.source / "file"), REAL_B3SUM=BLAKE3,
                )
                output = self.work / action
                result = run_cli(["catalog", self.source, "--directory-hashes", "--hash", "blake3",
                                  "--jobs", "1", "--output", output],
                                 cwd=self.work, engine="bash", env=environment)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assert_observation_error(output)

    @contextmanager
    def paused_hash(self, engine, args, body, *, before="", **environment):
        ready = self.work / "hash-pid"
        ready.unlink(missing_ok=True)
        env = self.controlled_hasher(
            before +
            'printf "%s\\n" "$$" > "$HASH_READY.tmp"\n'
            'mv -- "$HASH_READY.tmp" "$HASH_READY"\n' + body,
            HASH_READY=str(ready), **environment,
        )
        command = ["bash", PROJECT_ROOT / "sourcediff", "--engine", engine, *args, "--jobs", "1"]
        with subprocess.Popen(command, cwd=self.work, env=env, stderr=subprocess.PIPE,
                              start_new_session=True) as process:
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists(), "the controlled hasher did not start")
                yield process, int(ready.read_bytes())
            finally:
                if "HASH_RELEASE" in environment:
                    Path(environment["HASH_RELEASE"]).touch()
                if process.poll() is None:
                    if ready.exists():
                        with suppress(ProcessLookupError):
                            os.kill(int(ready.read_bytes()), signal.SIGKILL)
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()

    @unittest.skipUnless(BLAKE3, "requires b3sum to complete the hash before the publication race")
    def test_output_created_after_real_scan_starts_is_never_clobbered(self):
        self.put(b"file", b"data")
        output = self.work / "output"
        release = self.work / "release"
        for engine in ENGINES:
            with self.subTest(engine=engine):
                output.unlink(missing_ok=True)
                release.unlink(missing_ok=True)
                with self.paused_hash(
                    engine, ["catalog", self.source, "--output", output, "--hash", "blake3"],
                    'while [[ ! -e $HASH_RELEASE ]]; do /bin/sleep .01; done\n'
                    'exec "$REAL_B3SUM" "$@"\n',
                    HASH_RELEASE=str(release), REAL_B3SUM=BLAKE3,
                ) as (process, _):
                    with output.open("xb") as competing_writer:
                        competing_writer.write(b"competing output")
                    release.touch()
                    _, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 2, stderr)
                self.assertEqual(output.read_bytes(), b"competing output")
                self.assertFalse(list(self.work.glob(".sourcediff-*")))

    def test_interrupting_a_blocked_external_hash_reaps_worker_and_cleans_staging(self):
        cases = (
            ("digest-read", 1, ""),
            ("input-write", 1024 * 1024, ""),
            ("process-wait", 1, "printf '%064d  -\\n' 0\nexec 1>&-\n"),
        )
        for engine in ENGINES:
            for wait_state, size, before_sleep in cases:
                # One byte fits in the pipe, leaving Python blocked on the digest.
                # A MiB blocks the write. Closing stdout after a digest blocks the process wait.
                with self.subTest(engine=engine, wait_state=wait_state):
                    self.put(b"file", b"x" * size)
                    output = self.work / f"{engine}-output"
                    args = ["catalog", self.source, "--output", output, "--hash", "blake3"]
                    with self.paused_hash(engine, args, before_sleep + "exec /bin/sleep 60\n") as (process, worker):
                        self.assertTrue(list(self.work.glob(".sourcediff-*")))
                        process_name = Path(f"/proc/{worker}/comm")
                        deadline = time.monotonic() + 5
                        while process_name.read_bytes().strip() != b"sleep":
                            self.assertLess(time.monotonic(), deadline, "the blocking hasher did not exec")
                            time.sleep(.01)
                        process.send_signal(signal.SIGTERM)
                        try:
                            _, stderr = process.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            self.fail("the CLI did not interrupt and reap the blocked hash")
                        self.assertEqual(process.returncode, 2, stderr)
                        self.assertIn(b"SIGTERM", stderr)
                        self.assertFalse(process_name.exists(), "hash subprocess was not reaped")
                    self.assertFalse(output.exists())
                    self.assertFalse(list(self.work.glob(".sourcediff-*")))

    def test_python_sigterm_at_synchronous_hasher_constructor_return_reaps_owned_child(self):
        output = self.work / "startup-output"
        child_pid = self.work / "startup-child"
        environment = self.controlled_hasher("exec /bin/sleep 60\n")
        program = """
import os
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from sourcediff_py.cli import main

root, output, pid_path = sys.argv[2:]
constructor = subprocess.Popen.__init__.__code__

def interrupt_at_return(frame, event, arg):
    if event == "return" and frame.f_code is constructor:
        child = frame.f_locals["self"]
        if child.args == ["b3sum", "-"]:
            sys.setprofile(None)
            Path(pid_path).write_text(str(child.pid), encoding="ascii")
            os.kill(os.getpid(), signal.SIGTERM)

sys.setprofile(interrupt_at_return)
sys.exit(main(["catalog", root, "--hash", "blake3", "--directory-hashes", "--output", output]))
"""
        with subprocess.Popen(["/bin/sleep", "60"]) as unrelated:
            try:
                result = subprocess.run(
                    [sys.executable, "-B", "-c", program, PROJECT_ROOT / "lib",
                     self.source, output, child_pid], cwd=self.work, env=environment,
                    capture_output=True, timeout=10,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(b"SIGTERM", result.stderr)
                self.assertTrue(child_pid.exists(), "the real Popen constructor was not reached")
                self.assertIsNone(unrelated.poll(), "cancellation stopped an unrelated process")
                self.assertFalse(Path(f"/proc/{int(child_pid.read_text())}").exists(),
                                 "the hasher spawned before registration was not reaped")
                self.assertFalse(output.exists())
                self.assertFalse(list(self.work.glob(".sourcediff-*")))
            finally:
                unrelated.terminate()
                if child_pid.exists():
                    with suppress(ProcessLookupError):
                        os.kill(int(child_pid.read_text()), signal.SIGKILL)

    def test_python_spawned_hasher_keeps_default_sigterm_behavior(self):
        environment = self.controlled_hasher('kill -TERM "$$"\nprintf \'%064d  -\\n\' 0\n')
        output = self.work / "terminated-hasher-output"
        result = run_cli(
            ["catalog", self.source, "--hash", "blake3", "--directory-hashes", "--output", output],
            cwd=self.work, engine="python", env=environment,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        _, entries, errors, complete = read_catalog(output, self.work)
        self.assertFalse(complete)
        self.assertEqual([(row["path"], row["digest"]) for row in iter_entries(entries)], [(b".", "-")])
        self.assertEqual([(row["path"], row["operation"]) for row in iter_errors(errors)],
                         [(b".", "hash-directory")])

    @unittest.skipUnless(BLAKE3, "requires b3sum for the baseline and target directory digests")
    def test_python_interrupt_during_projected_directory_input_reaps_synchronous_hasher(self):
        # Long child frames fill the pipe while the projected root digest is written on the main thread.
        for index in range(400):
            self.put(f"{index:03}-".encode() + b"x" * 224)
        baseline, *_ = self.catalog("python", "baseline", "--hash", "blake3")
        empty_target = self.work / "empty-target"
        empty_target.mkdir()
        output = self.work / "projected-report"
        args = ["compare", empty_target, baseline, "--output", output, "--directory-hashes"]
        with self.paused_hash(
            "python", args, "exec /bin/sleep 60\n",
            before='if [[ ! -e $FIRST_HASH ]]; then\n'
                   '    : > "$FIRST_HASH"\n'
                   '    exec "$REAL_B3SUM" "$@"\n'
                   'fi\n',
            FIRST_HASH=str(self.work / "first-hash"), REAL_B3SUM=BLAKE3,
        ) as (process, worker):
            channel = Path(f"/proc/{process.pid}/wchan")
            deadline = time.monotonic() + 5
            while b"pipe_write" not in channel.read_bytes():
                self.assertLess(time.monotonic(), deadline, "projected directory input did not fill the pipe")
                time.sleep(.01)
            process.send_signal(signal.SIGTERM)
            try:
                _, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.fail("the CLI blocked while closing the interrupted hasher's input")
            self.assertEqual(process.returncode, 2, stderr)
            self.assertIn(b"SIGTERM", stderr)
            self.assertFalse(Path(f"/proc/{worker}").exists())
        self.assertFalse(output.exists())
        self.assertFalse(list(self.work.glob(".sourcediff-*")))
