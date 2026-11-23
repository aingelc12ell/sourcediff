import os
import re
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sourcediff_py.format import iter_entries, iter_errors, read_catalog
from tests.support import GOLDEN_CATALOG, PROJECT_ROOT, run_cli


class EngineFixture(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.source = self.work / "tree"
        self.source.mkdir()

    def put(self, name, data=b""):
        full = os.fsencode(self.source) + b"/" + name
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as target:
            target.write(data)

    def catalog(self, engine, name, *options, status=0, env=None):
        path = self.work / name
        result = run_cli(["catalog", str(self.source), "--output", path, *options],
                         cwd=self.work, engine=engine, env=env)
        self.assertEqual(result.returncode, status, result.stderr)
        settings, entries, errors, complete = read_catalog(path, workspace=self.work)
        return path, settings, list(iter_entries(entries)), list(iter_errors(errors)), complete


@unittest.skipUnless(sys.platform.startswith("linux"), "requires GNU tools and Linux paths")
class BashCatalogTests(EngineFixture):
    def configured_default_jobs(self, output=None, status=0):
        binaries = self.work / "cpu-bin"
        binaries.mkdir(exist_ok=True)
        detector = binaries / "nproc"
        if output is None:
            detector.unlink(missing_ok=True)
        else:
            detector.write_text(
                '#!/bin/bash\nprintf "%s" "$CPU_OUTPUT"\nexit "$CPU_STATUS"\n',
                encoding="ascii",
            )
            detector.chmod(0o700)
        # Observe sd_main's selected configuration before argument processing or
        # filesystem work; expectations below come from the approved CPU cap.
        result = subprocess.run([
            shutil.which("bash"), "-c",
            'source "$1"; sd_parse_args() { printf "%s\\n" "$jobs"; return 3; }; sd_main',
            "bash", PROJECT_ROOT / "lib/bash/main.sh",
        ], cwd=self.work, env={**os.environ, "PATH": str(binaries),
                              "CPU_OUTPUT": output or "", "CPU_STATUS": str(status)},
            capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        return result.stdout

    def test_default_jobs_uses_cpu_count_capped_at_four(self):
        for detected, expected in (("1\n", b"1\n"), ("2\n", b"2\n"), ("3\n", b"3\n"),
                                   ("4\n", b"4\n"), ("12\n", b"4\n"),
                                   ("999999999999999999999999999999999999\n", b"4\n")):
            with self.subTest(detected=detected):
                self.assertEqual(self.configured_default_jobs(detected), expected)

    def test_default_jobs_falls_back_to_one_for_unknown_or_malformed_cpu_count(self):
        for detected, status in ((None, 0), ("", 0), ("12\n", 1), ("0\n", 0),
                                 ("-1\n", 0), ("unknown\n", 0), ("2\n3\n", 0),
                                 (" 2\n", 0), ("02\n", 0), ("1+1\n", 0), ("$(>sentinel)\n", 0)):
            with self.subTest(detected=detected, status=status):
                self.assertEqual(self.configured_default_jobs(detected, status), b"1\n")
                self.assertFalse((self.work / "sentinel").exists())

    def test_public_cli_jobs_override_keeps_requested_worker_count(self):
        binaries = self.work / "bin"
        binaries.mkdir()
        detector = binaries / "nproc"
        detector.write_text('#!/bin/bash\nprintf "1\\n"\n', encoding="ascii")
        detector.chmod(0o700)
        hook = self.work / "observe-jobs.sh"
        hook.write_text(
            'if [[ $0 == */lib/bash/main.sh ]]; then\n'
            '    set -T\n'
            '    trap \'if [[ $BASH_COMMAND == sd_scan\\ * ]]; then\n'
            '        printf "%s\\n" "$jobs" > "$JOBS_OBSERVED"\n'
            '    fi\' DEBUG\n'
            'fi\n', encoding="ascii",
        )
        observed = self.work / "jobs-observed"
        environment = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}",
                       "BASH_ENV": str(hook), "JOBS_OBSERVED": str(observed)}
        self.put(b"file", b"content")
        for options, expected in (((), b"1\n"), (("--jobs", "7"), b"7\n")):
            with self.subTest(options=options):
                _, _, entries, errors, complete = self.catalog(
                    "bash", "observed-output", *options, "--overwrite", env=environment)
                self.assertEqual(observed.read_bytes(), expected)
                self.assertTrue(complete)
                self.assertEqual(errors, [])
                self.assertEqual([row["path"] for row in entries], [b".", b"file"])

    def validate_with_bash(self, path):
        return subprocess.run([
            "bash", "-c",
            'source "$1"; declare -A saved; sd_read_catalog "$2" "$3" "$4" saved',
            "bash", PROJECT_ROOT / "lib/bash/format.sh", path, self.work / "entries", self.work / "errors",
        ], cwd=self.work, capture_output=True, timeout=10)

    def test_catalog_cross_readers_preserve_paths_links_and_directory_frames(self):
        for name in (b"!", b"-lead", b"a/inside\n", b"a!", b"b/%\t\xff\n", "ña☃".encode(), b"\xff\n"):
            self.put(name, name)
        os.symlink(b"-lead\xff\n\n", os.fsencode(self.source) + b"/link\n")
        # Include a pre-epoch fractional timestamp.
        os.utime(self.source / "-lead", ns=(-12, -12))
        algorithms = ["sha256"] + (["blake3"] if shutil.which("b3sum") else [])
        for algorithm in algorithms:
            with self.subTest(algorithm=algorithm):
                options = ("--directory-hashes", "--hash", algorithm)
                baseline, old_settings, old, _, _ = self.catalog("python", "reference", *options, "--overwrite")
                validation = self.validate_with_bash(baseline)
                self.assertEqual(validation.returncode, 0, validation.stderr)
                for jobs in ("1", "3"):
                    _, settings, entries, errors, complete = self.catalog(
                        "bash", "bash-output", *options, "--jobs", jobs, "--overwrite"
                    )
                    self.assertTrue(complete)
                    self.assertEqual(errors, [])
                    self.assertEqual(entries, old)
                    self.assertEqual(settings["engine"], "bash")
                    self.assertEqual(settings["hash"], old_settings["hash"])

    def test_bash_reader_checks_catalog_as_data_and_rejects_invalid_rows(self):
        path = self.work / "fixture"
        path.write_bytes(GOLDEN_CATALOG)
        result = self.validate_with_bash(path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.work / "sentinel").exists())
        for data in (
            GOLDEN_CATALOG.replace(b"a%25", b"a%00"),
            GOLDEN_CATALOG.replace(b"a%25", b"a\0%25"),
            GOLDEN_CATALOG.replace(b"S\thash\tsha256", b"S\thash\tsha256\t"),
            GOLDEN_CATALOG.replace(b"\t-2\t1\t", b"\t-9223372036854775809\t1\t"),
            GOLDEN_CATALOG.replace(b"E\tlink", b"E\t!link"),
            GOLDEN_CATALOG.replace(b"E\tlink", b"E\t../link"),
            GOLDEN_CATALOG[:-1],
            GOLDEN_CATALOG + b"\n",
        ):
            with self.subTest(data=data):
                path.write_bytes(data)
                self.assertNotEqual(self.validate_with_bash(path).returncode, 0)

    def test_filter_and_depth_parity(self):
        for name in (b"a/one", b"b/two", b"keep/two", b"^/lit", b"name\\part/sub",
                     b"nameXpart/sub", b"suffix.tmp", b".tmp", b"deep/nest/leaf", b"[foo]/one"):
            self.put(name)
        collection = (
            ("--exclude-dir", "[!a]", "--exclude-dir", r"name\part", "--exclude-ext", ".tmp"),
            ("--exclude-dir", "[^a]", "--exclude", "*/nest*", "--exclude", "[[:alpha:]]"),
            ("--exclude", "deep", "--clear-exclude", "--exclude", "[z-a]", "--max-depth", "02"),
            ("--no-recursive", "--max-depth", "0"),
        )
        for options in collection:
            with self.subTest(options=options):
                args = (*options, "--directory-hashes", "--overwrite")
                _, _, old, _, _ = self.catalog("python", "reference", *args)
                _, _, new, _, _ = self.catalog("bash", "bash-output", *args)
                self.assertEqual(new, old)

    def test_follow_modes_special_files_and_directory_hash_errors(self):
        self.put(b"okay/one", b"content")
        (self.source / "tree-link").symlink_to("okay")
        (self.source / "cycle").symlink_to(".")
        os.mkfifo(self.source / "unhashable")
        for links in ("record", "skip", "follow"):
            with self.subTest(links=links):
                options = ("--directory-hashes", "--symlinks", links, "--overwrite")
                _, _, expected, old_errors, _ = self.catalog("python", "reference", *options, status=2)
                _, _, actual, errors, complete = self.catalog("bash", "bash-output", *options, status=2)
                self.assertFalse(complete)
                self.assertEqual(actual, expected)
                error_scope = lambda rows: {(row["path"], row["operation"], row["scope"]) for row in rows}
                self.assertEqual(error_scope(errors), error_scope(old_errors))

    def test_catalog_with_only_bash_and_gnu_tools_on_path(self):
        binaries = self.work / "bin"
        binaries.mkdir()
        for tool in (
            "bash", "cat", "cut", "dd", "find", "grep", "ln", "mkfifo", "mktemp",
            "mv", "readlink", "realpath", "rm", "sha256sum", "sort", "stat", "wc",
        ):
            (binaries / tool).symlink_to(shutil.which(tool))
        self.put(b"file", b"content")
        environment = {**os.environ, "PATH": str(binaries)}
        self.assertIsNone(shutil.which("python3", path=environment["PATH"]))
        _, _, entries, _, complete = self.catalog("bash", "output", env=environment)
        self.assertTrue(complete)
        self.assertEqual([row["path"] for row in entries], [b".", b"file"])

    def test_excludes_output_through_physical_symlink_dotdot_binding(self):
        self.put(b"report", b"still in scope")
        self.put(b"nested/report", b"omit the actual output")
        (self.source / "nested" / "child").mkdir()
        (self.source / "alias").symlink_to("nested/child")
        output = self.source / "alias" / ".." / "report"
        result = run_cli(["catalog", self.source, "--output", output, "--overwrite"], cwd=self.work, engine="bash")
        self.assertEqual(result.returncode, 0, result.stderr)
        _, entries, _, _ = read_catalog(output, self.work)
        paths = {row["path"] for row in iter_entries(entries)}
        self.assertIn(b"report", paths)
        self.assertNotIn(b"nested/report", paths)
        self.assertFalse(any(b".sourcediff-" in path for path in paths))

    def changing_scan(self, change=None):
        binaries = self.work / "bin"
        binaries.mkdir(exist_ok=True)
        hasher = binaries / "b3sum"
        hasher.write_text(
            '#!/bin/bash\n'
            ': > "$HASH_READY"\n'
            'while [[ ! -f "$HASH_RELEASE" ]]; do /bin/sleep 0.01; done\n'
            'exec "$REAL_B3SUM" "$@"\n',
            encoding="ascii",
        )
        hasher.chmod(0o700)
        output = self.work / "output"
        ready = self.work / "ready"
        release = self.work / "release"
        environment = {
            **os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "REAL_B3SUM": shutil.which("b3sum"),
            "HASH_READY": str(ready), "HASH_RELEASE": str(release),
        }
        command = ["bash", PROJECT_ROOT / "sourcediff", "--engine", "bash", "catalog", self.source,
                   "--output", output, "--hash", "blake3", "--jobs", "1", "--directory-hashes"]
        if change is None:
            output.write_bytes(b"prior artifact")
            command.append("--overwrite")
        with subprocess.Popen(command, cwd=self.work, env=environment, stderr=subprocess.PIPE) as process:
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "the hash did not start")
                if change is None:
                    process.send_signal(signal.SIGTERM)
                else:
                    change()
                    release.touch()
                _, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 2, stderr)
            finally:
                release.touch()
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        if change is None:
            self.assertIn(b"SIGTERM", stderr)
            self.assertEqual(output.read_bytes(), b"prior artifact")
            self.assertEqual(list(self.work.glob(".sourcediff-*")), [])
            return
        _, entries, errors, complete = read_catalog(output, self.work)
        self.assertFalse(complete)
        return {entry["path"]: entry for entry in iter_entries(entries)}, list(iter_errors(errors))

    @unittest.skipUnless(shutil.which("b3sum"), "requires b3sum for synchronized hashing")
    def test_detached_directory_is_discarded_after_workers_finish(self):
        self.put(b"moved/inner/file", b"old")
        (self.source / "unaffected").mkdir()
        entries, errors = self.changing_scan(lambda: (self.source / "moved").rename(self.work / "moved"))
        self.assertEqual(set(entries), {b".", b"unaffected"})
        self.assertEqual(entries[b"."]["digest"], "-")
        self.assertEqual(entries[b"unaffected"]["verification"], "verified")
        self.assertEqual([(row["path"], row["operation"], row["scope"]) for row in errors],
                         [(b"moved", "directory-binding", "subtree")])

    @unittest.skipUnless(shutil.which("b3sum"), "requires b3sum for synchronized hashing")
    def test_root_rebinding_discards_the_observed_tree(self):
        self.put(b"inner/file", b"old")

        def replace():
            self.source.rename(self.work / "detached")
            self.source.mkdir()

        entries, errors = self.changing_scan(replace)
        self.assertEqual(entries, {})
        self.assertEqual([(row["path"], row["operation"], row["scope"]) for row in errors],
                         [(b".", "directory-binding", "subtree")])

    @unittest.skipUnless(shutil.which("b3sum"), "requires b3sum for synchronized hashing")
    def test_file_replaced_while_hashing_is_not_published_as_verified(self):
        self.put(b"file", b"old")
        original = self.source / "file"
        replacement = self.work / "replacement"
        replacement.write_bytes(b"new")
        os.utime(replacement, ns=(0, original.stat().st_mtime_ns))
        entries, errors = self.changing_scan(lambda: os.replace(replacement, original))
        self.assertEqual(set(entries), {b"."})
        self.assertEqual([(row["path"], row["operation"], row["scope"]) for row in errors],
                         [(b"file", "hash-file", "entry")])

    @unittest.skipUnless(shutil.which("b3sum"), "requires b3sum for synchronized hashing")
    def test_interrupting_worker_hash_cleans_up_and_preserves_output(self):
        self.put(b"file", b"old")
        self.changing_scan()

    @unittest.skipUnless(shutil.which("b3sum"), "requires b3sum for synchronized hashing")
    def test_interrupting_parent_hash_cleans_up_and_preserves_output(self):
        (self.source / "link").symlink_to("file")
        self.changing_scan()

    def test_catalog_accepts_a_writable_relative_tmpdir(self):
        scratch = self.work / "scratch space"
        scratch.mkdir()
        self.put(b"file", b"content")
        environment = {**os.environ, "TMPDIR": scratch.name}
        for engine in ("python", "bash"):
            with self.subTest(engine=engine):
                _, _, entries, errors, complete = self.catalog(engine, f"{engine}-relative-tmp", env=environment)
                self.assertTrue(complete)
                self.assertEqual(errors, [])
                self.assertEqual([row["path"] for row in entries], [b".", b"file"])
                self.assertEqual(list(scratch.iterdir()), [])

    def test_unsupported_find_printf_fails_before_staging(self):
        binaries = self.work / "bin"
        binaries.mkdir()
        find = binaries / "find"
        find.write_text(
            '#!/bin/bash\n'
            'if [[ $1 == --version ]]; then exec "$REAL_FIND" "$@"; fi\n'
            'printf "this build of find has no -printf\\n" >&2\n'
            'exit 1\n',
            encoding="ascii",
        )
        find.chmod(0o700)
        environment = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "REAL_FIND": shutil.which("find")}
        result = run_cli(["catalog", self.source, "--output", "absent"], cwd=self.work, engine="bash", env=environment)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(b"find", result.stderr)
        self.assertIn(b"-printf", result.stderr)
        self.assertFalse((self.work / "absent").exists())
        self.assertEqual(list(self.work.glob(".sourcediff-*")), [])

    def test_failed_find_during_traversal_still_publishes_an_incomplete_catalog(self):
        binaries = self.work / "bin"
        binaries.mkdir()
        find = binaries / "find"
        find.write_text(
            '#!/bin/bash\n'
            'directory=${2:-}\n'
            'if [[ $(/usr/bin/readlink -f -- "$directory") == "$TARGET_DIR" ]]; then\n'
            '    printf "find: target traversal failed\\n" >&2\n'
            '    exit 1\n'
            'fi\n'
            'exec "$REAL_FIND" "$@"\n',
            encoding="ascii",
        )
        find.chmod(0o700)
        environment = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}",
                       "REAL_FIND": shutil.which("find"), "TARGET_DIR": str(self.source)}
        _, _, _, errors, complete = self.catalog("bash", "runtime-error", status=2, env=environment)
        self.assertFalse(complete)
        self.assertEqual([(row["path"], row["operation"]) for row in errors], [(b".", "scandir")])

    def test_relative_paths_longer_than_linux_path_max_are_catalogued(self):
        native = TemporaryDirectory(dir="/tmp")
        self.addCleanup(native.cleanup)
        self.source = Path(native.name) / "tree"
        self.source.mkdir()
        components = [b"d" * 250] * 17
        fd = os.open(self.source, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in components:
                os.mkdir(component, dir_fd=fd)
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
            file_fd = os.open(b"name\n", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd)
            with os.fdopen(file_fd, "wb") as stream:
                stream.write(b"deep content")
            os.symlink(b"name\n", b"link\n", dir_fd=fd)
        finally:
            os.close(fd)
        for policy in ("record", "follow"):
            with self.subTest(policy=policy):
                options = ("--directory-hashes", "--symlinks", policy, "--overwrite")
                _, _, reference, _, _ = self.catalog("python", "deep-reference", *options)
                _, _, entries, errors, complete = self.catalog("bash", "deep-bash", *options)
                self.assertTrue(complete)
                self.assertEqual(errors, [])
                self.assertEqual(entries, reference)
                self.assertEqual(len(entries), 20)
                self.assertIn(b"/".join([*components, b"link\n"]), {row["path"] for row in entries})

    def fail_fast_scan(self, nested):
        self.put(b"a-fail", b"fails")
        if nested:
            self.put(b"z-open/one", b"unobserved")
            self.put(b"z-open/two", b"unobserved")
        else:
            self.put(b"z-final", b"unobserved")
        binaries = self.work / "bin"
        binaries.mkdir()
        scripts = {
            "b3sum": (
                'if /bin/mkdir "$HASH_ENTERED" 2>/dev/null; then\n'
                '    while [[ ! -e $RELEASE_HASH ]]; do /bin/sleep .01; done\n'
                '    exit 1\n'
                'fi\n'
                'exec "$REAL_B3SUM" "$@"\n'
            ),
            "find": (
                'if [[ $1 == --version ]]; then exec "$REAL_FIND" "$@"; fi\n'
                'if [[ $(/usr/bin/readlink -f -- "$2") == "$PAUSE_DIR" ]]; then\n'
                '    : > "$PARENT_ENTERED"\n'
                '    while [[ ! -e $RELEASE_PARENT ]]; do /bin/sleep .01; done\n'
                'fi\n'
                'set -o pipefail\n'
                '"$REAL_FIND" "$@" | /usr/bin/sort -z\n'
            ),
            "stat": (
                'if [[ $PAUSE_FILE == true && ${!#} == */z-final ]]; then\n'
                '    : > "$PARENT_ENTERED"\n'
                '    while [[ ! -e $RELEASE_PARENT ]]; do /bin/sleep .01; done\n'
                'fi\n'
                'exec "$REAL_STAT" "$@"\n'
            ),
        }
        for name, script in scripts.items():
            executable = binaries / name
            executable.write_text("#!/bin/bash\n" + script, encoding="ascii")
            executable.chmod(0o700)
        release_parent, release_hash = self.work / "release-parent", self.work / "release-hash"
        parent_entered, hash_entered = self.work / "parent-entered", self.work / "hash-entered"
        environment = {
            **os.environ, "PATH": f"{binaries}:{os.environ['PATH']}",
            "REAL_B3SUM": shutil.which("b3sum"), "REAL_FIND": shutil.which("find"), "REAL_STAT": shutil.which("stat"),
            "PAUSE_FILE": "false" if nested else "true",
            "PAUSE_DIR": str(self.source / "z-open") if nested else "",
            "RELEASE_PARENT": str(release_parent), "RELEASE_HASH": str(release_hash),
            "PARENT_ENTERED": str(parent_entered), "HASH_ENTERED": str(hash_entered),
        }
        if not nested:
            # Signal when the final file has made it past the first scheduler check.
            # The failing worker stays blocked until the parent tries to reap it.
            scheduler_waiting = self.work / "scheduler-waiting"
            trace = self.work / "trace-scheduler.sh"
            trace.write_text(
                'if [[ $0 == */lib/bash/main.sh ]]; then\n'
                '    set -T\n'
                '    trap \'if [[ ${FUNCNAME[0]:-} == sd_schedule_hash && $BASH_COMMAND == sd_reap_worker ]]; '
                'then : > "$SCHEDULER_WAITING"; fi\' DEBUG\n'
                'fi\n',
                encoding="ascii",
            )
            environment.update(BASH_ENV=str(trace), SCHEDULER_WAITING=str(scheduler_waiting))
        output = self.work / "fail-fast-catalog"
        with subprocess.Popen(
            ["bash", PROJECT_ROOT / "sourcediff", "--engine", "bash", "catalog", self.source,
             "--output", output, "--hash", "blake3", "--jobs", "1", "--directory-hashes", "--fail-fast"],
            cwd=self.work, env=environment, stderr=subprocess.PIPE,
        ) as process:
            try:
                deadline = time.monotonic() + 10
                while not (hash_entered.exists() and parent_entered.exists()) and process.poll() is None:
                    self.assertLess(time.monotonic(), deadline, "scanner did not reach synchronized work")
                    time.sleep(.01)
                self.assertIsNone(process.poll(), "scanner exited before the controlled failure")
                if not nested:
                    release_parent.touch()
                    while not scheduler_waiting.exists() and process.poll() is None:
                        self.assertLess(time.monotonic(), deadline, "scheduler did not wait for the failing worker")
                        time.sleep(.01)
                release_hash.touch()
                while not list(self.work.glob(".sourcediff-*/scan-error")) and process.poll() is None:
                    self.assertLess(time.monotonic(), deadline, "failing worker did not record the error")
                    time.sleep(.01)
                release_parent.touch()
                _, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 2, stderr)
            finally:
                release_hash.touch()
                release_parent.touch()
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        _, entries, errors, complete = read_catalog(output, self.work)
        self.assertFalse(complete)
        return {entry["path"]: entry for entry in iter_entries(entries)}, list(iter_errors(errors))

    @unittest.skipUnless(shutil.which("b3sum"), "requires b3sum for synchronized hashing")
    def test_fail_fast_final_unscheduled_file_has_unknown_scope(self):
        entries, errors = self.fail_fast_scan(nested=False)
        self.assertEqual(set(entries), {b"."})
        self.assertEqual([(row["path"], row["operation"], row["scope"]) for row in errors],
                         [(b".", "aborted", "subtree"), (b"a-fail", "hash-file", "entry")])

    @unittest.skipUnless(shutil.which("b3sum"), "requires b3sum for synchronized hashing")
    def test_fail_fast_active_directory_has_no_partial_digest(self):
        entries, errors = self.fail_fast_scan(nested=True)
        self.assertEqual(set(entries), {b".", b"z-open"})
        self.assertEqual(entries[b"z-open"]["digest"], "-")
        self.assertEqual(entries[b"z-open"]["verification"], "unverified")
        self.assertTrue(any(row["path"] == b"." and row["operation"] == "aborted" for row in errors))


@unittest.skipUnless(sys.platform.startswith("linux"), "requires GNU tools and Linux paths")
class EngineComparisonTests(EngineFixture):
    def compare(self, engine, baseline, target, *options, status=0, env=None, output=None):
        report = output if output is not None else self.work / "difference"
        result = run_cli(["compare", target, baseline, "--output", report, "--overwrite", *options],
                         cwd=self.work, engine=engine, env=env)
        self.assertEqual(result.returncode, status, result.stderr)
        self.assertTrue(report.is_file(), result.stderr)
        return report.read_bytes()

    @staticmethod
    def normalize(report, *, messages=False):
        rows = report.splitlines()
        rows = [row for row in rows if not row.startswith((b"S\tengine\t", b"S\tengine-version\t"))]
        if messages:
            rows = [b"\t".join((*row.split(b"\t")[:3], row.split(b"\t")[-1]))
                    if row.startswith(b"ERROR\t") else row for row in rows]
        return rows

    def assert_parity(self, baseline, *options, status=0, messages=False):
        expected = self.compare("python", baseline, self.source, *options, status=status)
        actual = self.compare("bash", baseline, self.source, *options, status=status)
        self.assertEqual(self.normalize(actual, messages=messages), self.normalize(expected, messages=messages))
        self.assertTrue(actual.endswith({0: b"END\tcomplete\t0\n", 1: b"END\tcomplete\t1\n",
                                         2: b"END\tincomplete\t2\n"}[status]))
        return actual

    def test_report_and_catalog_engine_matrix(self):
        for name in (b"!", b"a/leaf", b"a!", b"changed\t%\xff\n", b"remove", b"type"):
            self.put(name, name)
        os.symlink(b"!\n", os.fsencode(self.source) + b"/link\n")
        target = self.work / "target"
        shutil.copytree(self.source, target, symlinks=True)
        with open(os.fsencode(target) + b"/changed\t%\xff\n", "wb") as stream:
            stream.write(b"new bytes")
        (target / "remove").unlink()
        (target / "type").unlink()
        (target / "type").mkdir()
        (target / "added").touch()
        (target / "link\n").unlink()
        (target / "link\n").symlink_to(b"a/leaf\n\n")
        algorithms = ("sha256", "blake3") if shutil.which("b3sum") else ("sha256",)
        for algorithm in algorithms:
            prior_entries = prior_report = None
            for jobs in ("1", "2", "4"):
                options = ("--hash", algorithm, "--directory-hashes", "--jobs", jobs, "--overwrite")
                for writer in ("python", "bash"):
                    with self.subTest(algorithm=algorithm, jobs=jobs, writer=writer):
                        baseline, _, entries, errors, complete = self.catalog(writer, "matrix", *options)
                        self.assertTrue(complete)
                        self.assertEqual(errors, [])
                        if prior_entries is None:
                            prior_entries = entries
                        self.assertEqual(entries, prior_entries)
                        for reader in ("python", "bash"):
                            with self.subTest(reader=reader):
                                report = self.compare(reader, baseline, target, "--jobs", jobs, status=1)
                                rows = self.normalize(report)
                                if prior_report is None:
                                    prior_report = rows
                                self.assertEqual(rows, prior_report)
                                self.assertIn(b"T\tcounted\t6", rows)
                                self.assertTrue(report.endswith(b"END\tcomplete\t1\n"))
                                self.assertIn(b"CONTENT_CHANGED\tchanged%09%25%FF%0A\tcounted\t", report)

    def test_strict_and_quick_are_independent(self):
        self.put(b"unchanged", b"same")
        self.put(b"restored\n", b"old")
        self.put(b"metadata", b"same bytes")
        baseline, *_ = self.catalog("python", "baseline", "--directory-hashes")
        content = self.source / "restored\n"
        old = content.stat()
        content.write_bytes(b"new")
        os.utime(content, ns=(old.st_atime_ns, old.st_mtime_ns))
        metadata = self.source / "metadata"
        old = metadata.stat()
        os.utime(metadata, ns=(old.st_atime_ns, old.st_mtime_ns + 2_000_000_000))
        for strict in (False, True):
            for quick in (False, True):
                with self.subTest(strict=strict, quick=quick):
                    options = [name for name, enabled in (("--strict", strict), ("--quick", quick)) if enabled]
                    report = self.assert_parity(baseline, *options, status=int(strict or not quick))
                    detail = b"counted" if strict else b"informational"
                    self.assertIn(b"METADATA_CHANGED\tmetadata\t" + detail + b"\t", report)
                    self.assertEqual(b"CONTENT_CHANGED\trestored%0A\t" in report, not quick)
                    self.assertIn(b"T\tfiles-reused\t" + (b"2" if quick else b"0") + b"\n", report)
                    self.assertIn(b"T\tdirectory-digests-unverified\t" + (b"1" if quick else b"0") + b"\n", report)

    def test_quick_rehashes_when_saved_precision_is_unavailable_or_different(self):
        self.put(b"file", b"old")
        baseline, *_ = self.catalog("bash", "baseline")
        original = baseline.read_bytes()
        path = self.source / "file"
        stamp = path.stat()
        path.write_bytes(b"new")
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        for precision in (b"-", b"1000000000"):
            with self.subTest(precision=precision):
                edited, count = re.subn(rb"(E\tfile\tfile\t[^\t]+\t3\t-?\d+\t)1\t",
                                        rb"\g<1>" + precision + b"\t", original)
                self.assertEqual(count, 1)
                baseline.write_bytes(edited)
                report = self.assert_parity(baseline, "--quick", status=1)
                self.assertIn(b"CONTENT_CHANGED\tfile\tcounted\t", report)
                self.assertIn(b"T\tfiles-hashed\t1\nT\tfiles-reused\t0\n", report)

    def test_depth_and_filters_project_or_widen_scope(self):
        for name in (b"folder/deeper/file", b"trim/file", b"unseen/data", b"left.log", b"keep"):
            self.put(name)
        baseline, *_ = self.catalog("bash", "narrow", "--exclude", "unseen", "--exclude-ext", "log",
                                    "--directory-hashes", "--max-depth", "2")
        report = self.assert_parity(baseline, "--exclude-dir", "trim", "--exclude-ext", ".log", "--max-depth", "1")
        self.assertIn(b"I\tdirectory-hash-scope\tcompared\n", report)
        report = self.assert_parity(baseline, "--clear-exclude", "--clear-exclude-ext", "--max-depth", "3", status=1)
        self.assertIn(b"I\tdirectory-hash-scope\tscope-changed\n", report)
        self.assertIn(b"O\texclude\nO\texclude-ext\n", report)
        for path in (b"folder/deeper/file", b"left.log", b"unseen", b"unseen/data"):
            self.assertIn(b"ADDED\t" + path + b"\toutside-baseline-scope\t", report)
        plain, *_ = self.catalog("python", "plain")
        report = self.assert_parity(plain, "--directory-hashes", "--no-recursive", "--quick")
        self.assertIn(b"I\tdirectory-hash-scope\tcompared\n", report)

    def test_projected_directory_frames_include_only_immediate_children(self):
        self.put(b"parent/child")
        baseline, *_ = self.catalog("python", "baseline")
        rows = [row for row in baseline.read_bytes().splitlines() if not row.startswith(b"E\tparent\t")]
        self.assertEqual(rows[-1], b"END\tcomplete\t3\t0")
        rows[-1] = b"END\tcomplete\t2\t0"
        baseline.write_bytes(b"\n".join(rows) + b"\n")
        report = self.assert_parity(baseline, "--directory-hashes", status=1)
        self.assertIn(b"CONTENT_CHANGED\t.\tcounted\tdirectory\t"
                      b"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\t", report)

    def test_link_policy_projection_and_scope_tags(self):
        outside = self.work / "outside"
        outside.mkdir()
        (outside / "child").touch()
        (self.source / "link").symlink_to(outside)
        (self.source / "filtered").symlink_to(outside)
        follow, *_ = self.catalog("bash", "follow", "--symlinks", "follow", "--directory-hashes")
        skip, *_ = self.catalog("python", "skip", "--symlinks", "skip")
        record, *_ = self.catalog("bash", "record", "--exclude-dir", "filtered")
        report = self.assert_parity(follow, "--symlinks", "skip")
        self.assertNotIn(b"MISSING\t", report)
        report = self.assert_parity(follow, "--symlinks", "record", status=1)
        self.assertIn(b"TYPE_CHANGED\tlink\tcounted\t", report)
        self.assertNotIn(b"MISSING\t", report)
        report = self.assert_parity(skip, "--symlinks", "follow", status=1)
        self.assertIn(b"ADDED\tlink\toutside-baseline-scope\t", report)
        self.assertIn(b"ADDED\tlink/child\toutside-baseline-scope\t", report)
        report = self.assert_parity(record, "--symlinks", "follow", status=1)
        self.assertIn(b"ADDED\tlink/child\toutside-baseline-scope\t", report)
        self.assertNotIn(b"MISSING\tfiltered\t", report)
        (outside / "child").unlink()
        outside.rmdir()
        report = self.assert_parity(record, "--symlinks", "follow", status=2, messages=True)
        self.assertIn(b"UNKNOWN\tfiltered\ttarget-scan-error\t", report)
        self.assertIn(b"ERROR\tfiltered\tfollow-link\t", report)

    def test_readlink_failure_excludes_old_followed_descendants(self):
        self.put(b"real/leaf")
        (self.source / "link").symlink_to("real")
        baseline, *_ = self.catalog("bash", "baseline", "--symlinks", "follow")
        binaries = self.work / "bin"
        binaries.mkdir()
        readlink = binaries / "readlink"
        readlink.write_text('#!/bin/bash\n'
                            'if [[ ${!#} == /proc/*/fd/*/link ]]; then\n'
                            '    printf "fixture could not read link\\n" >&2; exit 1\n'
                            'fi\n'
                            'exec "$REAL_READLINK" "$@"\n', encoding="ascii")
        readlink.chmod(0o700)
        environment = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}",
                       "REAL_READLINK": shutil.which("readlink")}
        report = self.compare("bash", baseline, self.source, "--symlinks", "record", status=2, env=environment)
        self.assertIn(b"UNKNOWN\tlink\ttarget-scan-error\t", report)
        self.assertIn(b"ERROR\tlink\treadlink\tfixture could not read link\tentry\n", report)
        self.assertNotIn(b"MISSING\t", report)
        self.assertNotIn(b"\tlink/leaf\t", report)

    def test_errors_obscure_unread_scope_and_take_precedence(self):
        self.put(b"change", b"old")
        self.put(b"bad")
        self.put(b"unread/leaf")
        baseline, *_ = self.catalog("bash", "baseline", "--symlinks", "follow", "--directory-hashes")
        (self.source / "change").write_bytes(b"new")
        (self.source / "bad").unlink()
        os.mkfifo(self.source / "bad")
        shutil.rmtree(self.source / "unread")
        (self.source / "unread").symlink_to("missing")
        report = self.assert_parity(baseline, status=2, messages=True)
        for path in (b".", b"bad", b"unread", b"unread/leaf"):
            self.assertIn(b"UNKNOWN\t" + path + b"\ttarget-scan-error\t", report)
        self.assertIn(b"CONTENT_CHANGED\tchange\tcounted\t", report)
        self.assertIn(b"T\tcounted\t1\n", report)
        self.assertNotIn(b"MISSING\t", report)
        report = self.compare("bash", baseline, self.source, "--fail-fast", "--jobs", "1", status=2)
        self.assertIn(b"ERROR\t.\taborted\t", report)
        self.assertIn(b"UNKNOWN\tunread/leaf\ttarget-scan-error\t", report)
        self.assertNotIn(b"MISSING\t", report)

    def test_artifact_projection_uses_physical_parent_and_resolved_baseline(self):
        self.put(b"baseline", b"unrelated")
        self.put(b"output", b"unrelated")
        self.put(b"nested/data", b"prior input")
        self.put(b"nested/output", b"prior output")
        (self.source / "nested" / "child").mkdir()
        (self.source / "alias").symlink_to("nested/child")
        (self.source / "nested" / "baseline").symlink_to("data")
        baseline, *_ = self.catalog("bash", "saved", "--directory-hashes")
        baseline.replace(self.source / "nested" / "data")
        baseline = self.source / "alias" / ".." / "baseline"
        output = self.source / "alias" / ".." / "output"
        (self.source / "baseline").write_bytes(b"changed")
        (self.source / "output").write_bytes(b"changed")
        for engine in ("python", "bash"):
            with self.subTest(engine=engine):
                report = self.compare(engine, baseline, self.source, output=output, status=1)
                changes = [row.split(b"\t")[:3] for row in report.splitlines() if row.startswith(b"CONTENT_CHANGED\t")]
                self.assertEqual(changes, [[b"CONTENT_CHANGED", path, b"counted"] for path in (b".", b"baseline", b"output")])
                self.assertNotIn(b"MISSING\t", report)
                self.assertNotIn(b"ADDED\t", report)
                self.assertTrue(baseline.read_bytes().startswith(b"SOURCEDIFF\t1\n"))

    def test_relative_invocation_paths_use_the_physical_working_directory(self):
        self.put(b"file")
        self.catalog("python", "baseline")
        alias = self.work.parent / (self.work.name + "-alias")
        alias.symlink_to(self.work)
        self.addCleanup(alias.unlink)
        environment = {**os.environ, "PWD": str(alias)}
        for engine in ("python", "bash"):
            with self.subTest(engine=engine):
                result = run_cli(["compare", ".//tree/.", "./baseline", "--output", "report",
                                  "--overwrite"], cwd=alias, engine=engine, env=environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = (self.work / "report").read_bytes()
                self.assertIn(b"I\tsource-catalog\t" + os.fsencode(self.work / "baseline") + b"\n", report)
                self.assertIn(b"I\ttarget\t" + os.fsencode(self.source) + b"\n", report)

    def test_final_publication_protects_the_open_baseline_identity(self):
        self.put(b"file", b"content")
        baseline, *_ = self.catalog("python", "baseline")
        original = baseline.read_bytes()
        output = self.work / "report"
        moved = self.work / "moved"
        entered, release = self.work / "entered", self.work / "release"
        hook = self.work / "pause-publication.sh"
        hook.write_text(
            'if [[ $0 == */lib/bash/main.sh ]]; then\n'
            '    set -T\n'
            '    trap \'if [[ $BASH_COMMAND == sd_publish\\ * ]]; then\n'
            '        : > "$PUBLICATION_ENTERED"\n'
            '        while [[ ! -f $PUBLICATION_RELEASE ]]; do /bin/sleep .01; done\n'
            '    fi\' DEBUG\n'
            'fi\n', encoding="ascii",
        )
        environment = {**os.environ, "BASH_ENV": str(hook), "PUBLICATION_ENTERED": str(entered),
                       "PUBLICATION_RELEASE": str(release)}
        with subprocess.Popen(
            ["bash", PROJECT_ROOT / "sourcediff", "--engine", "bash", "compare", self.source,
             baseline, "--output", output, "--overwrite"],
            cwd=self.work, env=environment, stderr=subprocess.PIPE,
        ) as process:
            try:
                deadline = time.monotonic() + 10
                while not entered.exists() and process.poll() is None:
                    self.assertLess(time.monotonic(), deadline, "comparison did not reach publication")
                    time.sleep(.01)
                self.assertTrue(entered.exists(), "comparison exited before publication")
                baseline.rename(moved)
                baseline.write_bytes(original)
                os.link(moved, output)
                release.touch()
                _, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 2, stderr)
                self.assertIn(b"aliases the input catalog", stderr)
            finally:
                release.touch()
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertTrue(os.path.samefile(moved, output))
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(list(self.work.glob(".sourcediff-*")), [])

    def test_invalid_and_incomplete_baselines_leave_the_output_intact(self):
        baseline, *_ = self.catalog("python", "baseline")
        valid = baseline.read_bytes()
        output = self.work / "report"
        output.write_bytes(b"original output")
        for data in (valid + b"\n", valid.replace(b"\tcomplete\n", b"\tincomplete\n", 1)
                     .replace(b"END\tcomplete\t", b"END\tincomplete\t")):
            with self.subTest(data=data):
                baseline.write_bytes(data)
                result = run_cli(["compare", self.source, baseline, "--output", output, "--overwrite"],
                                 cwd=self.work, engine="bash")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(b"catalog", result.stderr)
                self.assertEqual(output.read_bytes(), b"original output")
                self.assertEqual(list(self.work.glob(".sourcediff-*")), [])

    def test_quick_compares_paths_longer_than_linux_path_max(self):
        native = TemporaryDirectory(dir="/tmp")
        self.addCleanup(native.cleanup)
        self.source = Path(native.name) / "deep"
        self.source.mkdir()
        components = [b"d" * 250] * 17
        descriptor = os.open(self.source, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in components:
                os.mkdir(component, dir_fd=descriptor)
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            file = os.open(b"raw%\t\xff\n", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=descriptor)
            with os.fdopen(file, "wb") as stream:
                stream.write(b"deep content")
            baseline, *_ = self.catalog("python", "deep")
            report = self.assert_parity(baseline, "--quick")
            self.assertIn(b"T\tfiles-reused\t1\n", report)
            file = os.open(b"raw%\t\xff\n", os.O_WRONLY | os.O_TRUNC, dir_fd=descriptor)
            with os.fdopen(file, "wb") as stream:
                stream.write(b"new deep content")
            report = self.assert_parity(baseline, "--quick", status=1)
            wire = b"/".join([*components, b"raw%25%09%FF%0A"])
            self.assertIn(b"CONTENT_CHANGED\t" + wire + b"\tcounted\t", report)
        finally:
            os.close(descriptor)

    def test_compare_with_only_bash_and_gnu_tools_on_path(self):
        binaries = self.work / "bin"
        binaries.mkdir()
        for tool in (
            "bash", "cat", "cut", "dd", "find", "grep", "ln", "mkdir", "mkfifo", "mktemp",
            "mv", "readlink", "realpath", "rm", "sha256sum", "sort", "stat", "wc",
        ):
            (binaries / tool).symlink_to(shutil.which(tool))
        self.put(b"file", b"content")
        baseline, *_ = self.catalog("bash", "baseline", "--directory-hashes")
        environment = {**os.environ, "PATH": str(binaries)}
        output = self.work / "python-free-report"
        result = subprocess.run(
            [binaries / "bash", "-c", 'if command -v python || command -v python3; then exit 99; fi; exec bash "$@"',
             "bash", PROJECT_ROOT / "sourcediff", "--engine", "bash", "compare", self.source,
             baseline, "--output", output, "--quick"],
            cwd=self.work, env=environment, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"T\tfiles-reused\t1\n", output.read_bytes())


if __name__ == "__main__":
    unittest.main()
