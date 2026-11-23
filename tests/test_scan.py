"""Python catalog scanner coverage through the actual launcher and filesystem."""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT, run_cli

sys.path.insert(0, str(PROJECT_ROOT / "lib"))
from sourcediff_py.format import iter_entries, iter_errors, read_catalog


def sha(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux filesystem semantics")
class CatalogScanTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)
        self.source = self.work / "src"
        self.source.mkdir()
        self.sequence = 0

    def put(self, name, content=b""):
        path = self.source / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(content)

    def scan(self, *args, status=0, output=None):
        self.sequence += 1
        output = output or self.work / f"catalog-{self.sequence}.txt"
        result = run_cli(["catalog", self.source, "--output", output, *args], cwd=self.work)
        self.assertEqual(result.returncode, status, result.stderr)
        self.assertTrue(output.is_file(), result.stderr)
        settings, entries, errors, complete = read_catalog(output, self.work)
        self.assertEqual(complete, status == 0)
        self.assertEqual(settings["complete"], "complete" if complete else "incomplete")
        return settings, {entry["path"]: entry for entry in iter_entries(entries)}, list(iter_errors(errors))

    def test_byte_paths_file_hashes_and_link_text(self):
        self.put("a", b"abc")
        self.put("empty")
        self.put(".hidden")
        self.put("folder/nested", b"nested bytes")
        (self.source / "empty-directory").mkdir()
        root = os.fsencode(self.source)
        raw_name = b"odd%\t\n\r\xff"
        with open(root + b"/" + raw_name, "wb") as file:
            file.write(b"raw")
        link_target = b"missing\n\xff\n"
        os.symlink(link_target, root + b"/symbolic")
        os.utime(self.source / "a", ns=(900, 1700000000123456789))

        _, entries, errors = self.scan()
        expected = sorted((b".", b"a", b"empty", b".hidden", b"folder", b"folder/nested",
                           b"empty-directory", raw_name, b"symbolic"))
        self.assertEqual(list(entries), expected)
        self.assertEqual(errors, [])
        self.assertEqual(entries[b"a"]["digest"], sha(b"abc"))
        self.assertEqual(entries[b"a"]["size"], 3)
        self.assertEqual(entries[b"a"]["mtime_ns"], 1700000000123456789)
        self.assertEqual(entries[b"a"]["precision_ns"], 1)
        self.assertEqual(entries[b"empty"]["digest"], sha(b""))
        self.assertEqual(entries[b"folder/nested"]["digest"], sha(b"nested bytes"))
        self.assertEqual(entries[raw_name]["digest"], sha(b"raw"))
        self.assertEqual(entries[b"empty-directory"]["kind"], "directory")
        self.assertEqual(entries[b"empty-directory"]["digest"], "-")
        self.assertEqual(entries[b"symbolic"]["link_target"], link_target)
        self.assertEqual(entries[b"symbolic"]["digest"], sha(link_target))

    def test_exclusion_categories_use_case_sensitive_byte_globs_and_prune(self):
        for name in ("keep", "sub/keep", "sub/cache/deep/secret", "sub/drop1", "sub/drop12",
                     "sub/deep/a.tmp", "sub/deep/a.tmp.txt", "sub/deep/a.bak", "sub/deep/a.TMP",
                     "sub/deep/name", "sub/name", "sub/temp.tmp/retained", "other/leaf", "sub/toss/leaf",
                     "sub/dropx", "sub/drop2a", "sub/Drop2", "sub/.tmp"):
            self.put(name)
        self.put("sub/drop2")
        self.put("sub/drop3")
        os.mkfifo(self.source / "sub/cache/never-read")
        os.mkfifo(self.source / "sub/toss/never-read")
        os.link(self.source / "keep", self.source / "same-inode")
        with open(os.fsencode(self.source) + b"/\xff", "wb") as raw:
            raw.write(b"raw")

        settings, entries, errors = self.scan(
            "--exclude", "drop[1-2]", "--exclude", "sub/*name", "--exclude", b"\xff", "--exclude", "toss",
            "--exclude-dir", "ca?he", "--exclude-dir", "oth*", "--exclude-ext", ".tmp",
            "--exclude-ext", "bak",
        )
        self.assertEqual(set(entries), {
            b".", b"keep", b"sub", b"sub/keep", b"sub/drop12", b"sub/deep", b"sub/deep/a.tmp.txt",
            b"sub/deep/a.TMP", b"sub/temp.tmp", b"sub/temp.tmp/retained", b"sub/dropx",
            b"sub/drop2a", b"sub/Drop2", b"sub/drop3", b"sub/.tmp", b"same-inode",
        })
        self.assertEqual(errors, [])
        self.assertEqual(settings["exclude"], ["drop[1-2]", "sub/*name", os.fsdecode(b"\xff"), "toss"])
        self.assertEqual(settings["exclude-dir"], ["ca?he", "oth*"])
        self.assertEqual(settings["exclude-ext"], [".tmp", "bak"])

    def test_depth_limits_precede_descendant_errors(self):
        self.put("a")
        self.put("dir/sub/file")
        os.mkfifo(self.source / "dir" / "unsupported")
        for args, paths in (
            (("--max-depth", "0"), {b"."}),
            (("--max-depth", "1"), {b".", b"a", b"dir"}),
            (("--no-recursive",), {b".", b"a", b"dir"}),
            (("--max-depth", "4", "--no-recursive"), {b".", b"a", b"dir"}),
        ):
            with self.subTest(args=args):
                _, entries, errors = self.scan(*args)
                self.assertEqual(set(entries), paths)
                self.assertEqual(errors, [])

    def test_glob_bracket_negation_and_literal_backslash_prune_directories(self):
        for path in ("cachea/file", "cacheb/file", "backdir/file", "back\\dir/file"):
            self.put(path)
        os.mkfifo(self.source / "cacheb" / "unsupported")
        os.mkfifo(self.source / "back\\dir" / "unsupported")
        _, entries, errors = self.scan("--exclude-dir", "cache[!a]", "--exclude-dir", "back\\dir")
        self.assertEqual(set(entries), {b".", b"cachea", b"cachea/file", b"backdir", b"backdir/file"})
        self.assertEqual(errors, [])

    def test_follow_traverses_outside_and_retains_aliases_and_reports_cycles(self):
        self.put("local/file", b"inside")
        outside = self.work / "outside"
        outside.mkdir()
        (outside / "file").write_bytes(b"outside")
        (self.source / "alias").symlink_to("local")
        (self.source / "empty-link").symlink_to("missing")
        (self.source / "external").symlink_to(outside)
        (self.source / "local" / "loop").symlink_to("..")
        (self.source / "linked-file").symlink_to("local/file")
        (self.source / "self").symlink_to("self")

        _, entries, errors = self.scan("--symlinks", "follow", status=2)
        self.assertEqual(entries[b"local/file"]["digest"], sha(b"inside"))
        self.assertEqual(entries[b"alias/file"]["digest"], sha(b"inside"))
        self.assertEqual(entries[b"linked-file"]["digest"], sha(b"inside"))
        self.assertEqual(entries[b"external/file"]["digest"], sha(b"outside"))
        self.assertEqual({(error["path"], error["scope"]) for error in errors}, {
            (b"local/loop", "subtree"), (b"alias/loop", "subtree"),
            (b"empty-link", "subtree"), (b"self", "subtree"),
        })

        _, entries, errors = self.scan("--symlinks", "skip")
        self.assertEqual(set(entries), {b".", b"local", b"local/file"})
        self.assertEqual(errors, [])

        _, entries, errors = self.scan("--symlinks", "record")
        self.assertEqual(entries[b"local/loop"]["link_target"], b"..")
        self.assertEqual(entries[b"self"]["link_target"], b"self")
        self.assertEqual(errors, [])

    def test_excludes_current_output_and_workspace_through_root_alias(self):
        self.put("keep")
        output = self.source / "result.txt"
        output.write_bytes(b"old report")
        alias = self.work / "alias"
        alias.symlink_to(self.source)
        self.source = alias

        _, entries, errors = self.scan("--overwrite", output=output)
        self.assertEqual(set(entries), {b".", b"keep"})
        self.assertEqual(errors, [])
        self.assertEqual(sorted(path.name for path in self.source.iterdir()), ["keep", "result.txt"])

    def test_directory_hash_uses_sorted_canonical_frames_and_depth_limits(self):
        self.put("folder/file", b"x")
        self.put("folder/excluded", b"ignored")
        (self.source / "empty").mkdir()
        root = os.fsencode(self.source)
        os.symlink(b"file\n", root + b"/link")
        with open(root + b"/\xff", "wb") as raw:
            raw.write(b"raw")
        # Compose the expected wire frames independently of the scanner and codec.
        directory = sha(b"file\0file\0" + sha(b"x").encode() + b"\0")
        root_frames = (
            b"directory\0empty\0" + sha(b"").encode() + b"\0"
            b"directory\0folder\0" + directory.encode() + b"\0"
            b"symlink\0link\0" + sha(b"file\n").encode() + b"\0"
            b"file\0%FF\0" + sha(b"raw").encode() + b"\0"
        )
        settings, entries, _ = self.scan("--directory-hashes", "--exclude", "excluded", "--jobs", "3")
        self.assertEqual(settings["directory-hashes"], "true")
        self.assertEqual(entries[b"empty"]["digest"], sha(b""))
        self.assertEqual(entries[b"folder"]["digest"], directory)
        self.assertEqual(entries[b"."]["digest"], sha(root_frames))
        self.assertEqual(entries[b"."]["verification"], "verified")
        _, serial, _ = self.scan("--directory-hashes", "--exclude", "excluded", "--jobs", "1")
        self.assertEqual(entries, serial)

        settings, entries, _ = self.scan("--directory-hashes", "--max-depth", "1")
        self.assertEqual(settings["max-depth"], "1")
        self.assertEqual(entries[b"folder"]["digest"], sha(b""))
        self.assertEqual(entries[b"."]["digest"], sha(root_frames.replace(directory.encode(), sha(b"").encode())))

    def test_errors_are_incomplete_and_invalidate_only_affected_directory_hashes(self):
        self.put("good/file", b"good")
        self.put("bad/file", b"retained")
        os.mkfifo(self.source / "bad" / "pipe")
        _, entries, errors = self.scan("--directory-hashes", status=2)
        self.assertEqual([(error["path"], error["scope"]) for error in errors], [(b"bad/pipe", "entry")])
        self.assertEqual(entries[b"bad/file"]["digest"], sha(b"retained"))
        self.assertEqual(entries[b"good"]["verification"], "verified")
        self.assertEqual(entries[b"bad"]["digest"], "-")
        self.assertEqual(entries[b"."]["digest"], "-")

        _, _, errors = self.scan("--fail-fast", "--jobs", "1", status=2)
        self.assertIn((b".", "subtree"), {(error["path"], error["scope"]) for error in errors})

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "requires an unprivileged scanner")
    def test_reports_permission_errors_at_file_and_subtree_scopes(self):
        self.put("denied")
        self.put("closed/hidden")
        file = self.source / "denied"
        directory = self.source / "closed"
        self.addCleanup(directory.chmod, 0o700)
        file.chmod(0)
        directory.chmod(0)

        _, entries, errors = self.scan("--directory-hashes", status=2)
        self.assertEqual({(error["path"], error["scope"]) for error in errors}, {
            (b"denied", "entry"), (b"closed", "subtree"),
        })
        self.assertEqual(entries[b"closed"]["digest"], "-")
        self.assertEqual(entries[b"."]["digest"], "-")

    @unittest.skipUnless(shutil.which("b3sum"), "requires b3sum for hash verification")
    def test_blake3_hashing_of_file_link_and_empty_directory(self):
        with open(os.fsencode(self.source) + b"/strange\n\xff", "wb") as raw:
            raw.write(b"abc")
        (self.source / "link").symlink_to("abc")
        (self.source / "empty").mkdir()

        settings, entries, _ = self.scan("--hash", "blake3", "--directory-hashes")
        self.assertEqual(settings["hash"], "blake3")
        abc = "6437b3ac38465133ffb63b75273a8db548c558465d79db03fd359c6cd5bd9d85"
        self.assertEqual(entries[b"strange\n\xff"]["digest"], abc)
        self.assertEqual(entries[b"link"]["digest"], abc)
        self.assertEqual(entries[b"empty"]["digest"],
                         "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262")

    def scan_during_hash(self, change, *args):
        binaries = self.work / "bin"
        binaries.mkdir()
        hasher = binaries / "b3sum"
        hasher.write_text(
            '#!/bin/bash\n'
            ': > "$HASH_READY"\n'
            'while [[ ! -f "$HASH_RELEASE" ]]; do /bin/sleep 0.01; done\n'
            'exec "$REAL_B3SUM" "$@"\n',
            encoding="ascii",
        )
        hasher.chmod(0o700)
        ready = self.work / "hash-ready"
        release = self.work / "hash-release"
        output = self.work / "changed.txt"
        environment = {
            **os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "REAL_B3SUM": shutil.which("b3sum"),
            "HASH_READY": str(ready), "HASH_RELEASE": str(release),
        }
        command = ["bash", PROJECT_ROOT / "sourcediff", "catalog", self.source,
                   "--output", output, "--hash", "blake3", "--jobs", "1", *args]
        with subprocess.Popen(command, cwd=self.work, env=environment, stderr=subprocess.PIPE) as process:
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "the hashing subprocess did not start")
                change()
                release.touch()
                _, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 2, stderr)
            finally:
                release.touch()
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        _, entries, errors, complete = read_catalog(output, self.work)
        self.assertFalse(complete)
        return {entry["path"]: entry for entry in iter_entries(entries)}, list(iter_errors(errors))

    @unittest.skipUnless(shutil.which("b3sum"), "requires b3sum for synchronized hashing")
    def test_regular_file_replaced_during_hash_is_reported_as_incomplete(self):
        self.put("changing", b"x" * (2 * 1024 * 1024))
        original = self.source / "changing"
        replacement = self.work / "replacement"
        replacement.write_bytes(b"y" * original.stat().st_size)
        os.utime(replacement, ns=(0, original.stat().st_mtime_ns))

        entries, errors = self.scan_during_hash(lambda: os.replace(replacement, original))
        self.assertEqual(set(entries), {b"."})
        self.assertEqual([(error["path"], error["scope"]) for error in errors], [(b"changing", "entry")])

    @unittest.skipUnless(shutil.which("b3sum"), "requires b3sum for synchronized hashing")
    def test_ancestor_moved_during_hash_invalidates_the_detached_subtree(self):
        self.put("moved/inner/large", b"x" * (2 * 1024 * 1024))
        (self.source / "unaffected").mkdir()
        original = self.source / "moved"

        entries, errors = self.scan_during_hash(
            lambda: original.rename(self.work / "moved"), "--directory-hashes"
        )
        self.assertFalse(original.exists())
        self.assertEqual(set(entries), {b".", b"unaffected"})
        self.assertEqual(entries[b"."]["digest"], "-")
        self.assertEqual(entries[b"unaffected"]["verification"], "verified")
        self.assertEqual([(error["path"], error["scope"]) for error in errors], [(b"moved", "subtree")])

    @unittest.skipUnless(shutil.which("b3sum"), "requires b3sum for synchronized hashing")
    def test_root_replaced_during_hash_invalidates_the_root_scope(self):
        self.put("inner/large", b"x" * (2 * 1024 * 1024))
        replacement = self.work / "replacement"
        replacement.mkdir()

        def replace_root():
            self.source.rename(self.work / "old-root")
            replacement.rename(self.source)

        entries, errors = self.scan_during_hash(replace_root, "--directory-hashes")
        self.assertEqual(entries, {})
        self.assertEqual([(error["path"], error["scope"]) for error in errors], [(b".", "subtree")])

    def test_pending_hashes_across_deep_branches_stay_within_descriptor_limit(self):
        leaf_paths = [f"branch-{number}/" + "d/" * 25 + "leaf" for number in range(4)]
        for path in leaf_paths:
            self.put(path, b"content")
        output = self.work / "descriptor-budget.txt"
        # Hold file reads until traversal waits for workers. This forces outstanding reads across
        # sibling branches without relying on hashing speed or a timed delay to release them.
        program = """
import resource
import sys
from threading import Event

sys.path.insert(0, sys.argv[1])
from sourcediff_py import scan
from sourcediff_py.cli import main

started = Event()
release = Event()
real_hash = scan._hash_file
real_wait = scan.wait

def synchronized_hash(*args):
    started.set()
    if not release.wait(5):
        raise OSError("scanner did not wait for its pending hashes")
    return real_hash(*args)

def release_on_block(futures, *, timeout, return_when):
    if timeout is None:
        if not started.wait(5):
            raise RuntimeError("no hashing worker started")
        release.set()
    return real_wait(futures, timeout=timeout, return_when=return_when)

scan._hash_file = synchronized_hash
scan.wait = release_on_block
_, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (80, hard_limit))
sys.exit(main(["catalog", sys.argv[2], "--output", sys.argv[3], "--jobs", "4"]))
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", program, PROJECT_ROOT / "lib", self.source, output],
            cwd=self.work, capture_output=True, timeout=15,
        )
        self.assertTrue(output.is_file(), result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr + output.read_bytes()[-300:])
        _, entries, errors, complete = read_catalog(output, self.work)
        self.assertTrue(complete)
        self.assertEqual(list(iter_errors(errors)), [])
        verified = {entry["path"]: entry["digest"] for entry in iter_entries(entries) if entry["kind"] == "file"}
        self.assertEqual(verified, {path.encode(): sha(b"content") for path in leaf_paths})

    def test_missing_blake3_dependency_fails_without_creating_catalog(self):
        binaries = self.work / "bin"
        binaries.mkdir()
        for command in ("bash", "env", "readlink", "python3"):
            (binaries / command).symlink_to(shutil.which(command))
        output = self.work / "no-dependency.txt"
        result = run_cli(["catalog", self.source, "--hash", "blake3", "--output", output],
                         cwd=self.work, env={**os.environ, "PATH": str(binaries)})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(b"b3sum is required", result.stderr)
        self.assertFalse(output.exists())
