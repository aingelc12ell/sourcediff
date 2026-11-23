"""Scan directory trees and emit byte-sorted catalog spools."""

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from threading import Event, Lock

from .format import directory_child_frame, encode_entry, encode_error
from .interrupts import defer_interrupts

try:
    import sqlite3
except ImportError:
    sqlite3 = None


_ROOT = b"."
_CHUNK_SIZE = 1024 * 1024
_INT64_MAX = (1 << 63) - 1
_BLAKE3_OUTPUT = re.compile(rb"([0-9a-f]{64})  -\n")
MTIME_PRECISION_NS = 1


class ScanScope:
    """Match effective catalog settings against raw relative paths."""

    def __init__(self, settings):
        limit = settings["max-depth"]
        self.depth = None if limit == "-" else int(limit)
        if settings["recursive"] == "false":
            self.depth = 1 if self.depth is None else min(1, self.depth)
        self.symlinks = settings["symlinks"]
        self.exclude = self._patterns(settings, "exclude")
        self.exclude_dir = self._patterns(settings, "exclude-dir")
        self.exclude_ext = {
            b"." + (pattern[1:] if pattern.startswith(b".") else pattern)
            for pattern in self._patterns(settings, "exclude-ext")
        }

    @staticmethod
    def _patterns(settings, name):
        return [value.encode("utf-8", "surrogateescape") for value in settings.get(name, ())]

    @staticmethod
    def _matches(path, patterns):
        basename = path.rsplit(b"/", 1)[-1]
        return any(
            fnmatchcase(path, pattern) or (b"/" not in pattern and fnmatchcase(basename, pattern))
            for pattern in patterns
        )

    def can_descend(self, depth):
        return self.depth is None or depth < self.depth

    def excludes_path(self, path):
        return self._matches(path, self.exclude)

    def excludes_kind(self, path, kind):
        if kind == "directory":
            return self._matches(path, self.exclude_dir)
        if kind == "file":
            return os.path.splitext(path)[1] in self.exclude_ext
        return kind == "symlink" and self.symlinks == "skip"

    def contains(self, path, kind):
        """Apply scope to a baseline entry, including filters on directory ancestors."""
        if path == _ROOT:
            return True
        if self.depth is not None and path.count(b"/") >= self.depth:
            return False
        if self.excludes_path(path) or self.excludes_kind(path, kind):
            return False
        parent = _parent(path)
        while parent != _ROOT:
            if self.excludes_path(parent) or self.excludes_kind(parent, "directory"):
                return False
            parent = _parent(parent)
        return True


def _parent(path):
    if path == _ROOT:
        return None
    return path.rpartition(b"/")[0] or _ROOT


def _identity(info):
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _snapshot(info):
    return _identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _entry(path, kind, info, digest="-", target=None, verification="unverified"):
    mtime = info.st_mtime_ns
    if not -_INT64_MAX - 1 <= mtime <= _INT64_MAX:
        mtime = None
    size = info.st_size
    return {
        "path": path, "kind": kind, "digest": digest,
        "size": size if 0 <= size <= _INT64_MAX else None,
        "mtime_ns": mtime, "precision_ns": MTIME_PRECISION_NS if mtime is not None else None,
        "link_target": target, "verification": verification,
    }


def exclusion_binding(path):
    """Resolve an artifact's parent using filesystem semantics, preserving its name."""
    parent, name = os.path.split(os.fsencode(path))
    return os.path.join(os.path.realpath(parent or b"."), name)


class _HashCancellation(Event):
    """Stop external hashers so a blocked pipe cannot keep a worker alive."""

    def __init__(self):
        super().__init__()
        self._children = set()
        self._lock = Lock()

    def cancel(self):
        self.set()
        with self._lock:
            for child in self._children:
                child.kill()

    @contextmanager
    def start(self):
        with defer_interrupts() as resume:
            with subprocess.Popen(
                ["b3sum", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ) as child, self.track(child):
                resume()
                yield child

    @contextmanager
    def track(self, child):
        try:
            with self._lock:
                if self.is_set():
                    raise OSError("scan interrupted")
                self._children.add(child)
            yield
        finally:
            with self._lock:
                child.kill()
                self._children.discard(child)
            # Closing buffered input can write again. Stop the reader first so
            # an interrupted write cannot block during cleanup.
            with suppress(BrokenPipeError):
                child.stdin.close()


def _hash_chunks(chunks, algorithm, cancelled):
    if algorithm == "sha256":
        digest = hashlib.sha256()
        for chunk in chunks:
            if cancelled.is_set():
                raise OSError("scan interrupted")
            digest.update(chunk)
        return digest.hexdigest()

    with cancelled.start() as child:
        for chunk in chunks:
            if cancelled.is_set():
                raise OSError("scan interrupted")
            child.stdin.write(chunk)
        child.stdin.close()
        output = child.stdout.read(128)
        match = _BLAKE3_OUTPUT.fullmatch(output)
        if match is None or child.wait() != 0:
            raise OSError("b3sum did not return a valid BLAKE3 digest")
        return match[1].decode("ascii")


def _hash_file(parent_fd, name, info, follow, algorithm, cancelled):
    try:
        if cancelled.is_set():
            raise OSError("scan interrupted")
        expected = _snapshot(info)
        flags = os.O_RDONLY | os.O_NONBLOCK
        if not follow:
            flags |= os.O_NOFOLLOW
        with os.fdopen(os.open(name, flags, dir_fd=parent_fd), "rb", buffering=0) as source:
            if _snapshot(os.fstat(source.fileno())) != expected:
                raise OSError("file changed before hashing")
            blocks = iter(lambda: source.read(_CHUNK_SIZE), b"")
            digest = _hash_chunks(blocks, algorithm, cancelled)
            if _snapshot(os.fstat(source.fileno())) != expected:
                raise OSError("file changed while hashing")
        if _snapshot(os.stat(name, dir_fd=parent_fd, follow_symlinks=follow)) != expected:
            raise OSError("file changed while hashing")
        return digest
    finally:
        os.close(parent_fd)


def _open_directory(name, parent_fd, identity, follow):
    flags = os.O_RDONLY | os.O_DIRECTORY
    if not follow:
        flags |= os.O_NOFOLLOW
    fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        if _identity(os.fstat(fd)) != identity:
            raise OSError("directory binding changed while scanning")
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def _directory(name, parent_fd, identity, follow):
    fd = _open_directory(name, parent_fd, identity, follow)
    try:
        with os.scandir(fd) as children:
            yield fd, children
    finally:
        os.close(fd)


class _Spool:
    """Keep the sortable index and subtree state on disk."""

    def __init__(self, connection, directory_hashes, reporter=None):
        self.db = connection
        self.directory_hashes = directory_hashes
        self.has_errors = False
        self.reporter = reporter
        self.db.executescript("""
            PRAGMA journal_mode = OFF;
            PRAGMA temp_store = FILE;
            CREATE TABLE entries (
                path BLOB PRIMARY KEY, parent BLOB, kind TEXT, digest TEXT, size INTEGER,
                mtime_ns INTEGER, precision_ns INTEGER, link_target BLOB, verification TEXT
            ) WITHOUT ROWID;
            CREATE TABLE errors (path BLOB, row BLOB);
            CREATE INDEX ordered_errors ON errors (path, row);
            CREATE TABLE bindings (
                sequence INTEGER PRIMARY KEY, path BLOB, depth INTEGER, device TEXT, inode TEXT, follow INTEGER
            );
        """)
        if directory_hashes:
            self.db.executescript("""
                CREATE INDEX children ON entries (parent, path);
                CREATE TABLE directories (
                    path BLOB PRIMARY KEY, depth INTEGER, invalid INTEGER DEFAULT 0
                ) WITHOUT ROWID;
                CREATE INDEX bottom_up ON directories (depth DESC, path);
            """)

    def add_entry(self, entry):
        self.db.execute(
            """INSERT INTO entries VALUES (
                :path, :parent, :kind, :digest, :size, :mtime_ns, :precision_ns, :link_target, :verification
            )""",
            {**entry, "parent": _parent(entry["path"])},
        )

    def add_directory(self, entry, depth):
        self.add_entry(entry)
        if self.directory_hashes:
            self.db.execute("INSERT INTO directories (path, depth) VALUES (?, ?)", (entry["path"], depth))

    def add_binding(self, path, depth, identity, follow):
        device, inode, _ = identity
        self.db.execute(
            "INSERT INTO bindings (path, depth, device, inode, follow) VALUES (?, ?, ?, ?, ?)",
            (path, depth, str(device), str(inode), follow),
        )

    def iter_bindings(self):
        # Insertion order is depth first, keeping a directory and its descendants contiguous.
        rows = self.db.execute("SELECT path, depth, device, inode, follow FROM bindings ORDER BY sequence")
        for path, depth, device, inode, follow in rows:
            yield path, depth, (int(device), int(inode), stat.S_IFDIR), bool(follow)

    def invalidate(self, paths):
        if self.directory_hashes:
            self.db.executemany("UPDATE directories SET invalid = 1 WHERE path = ?", ((path,) for path in paths))

    def discard_subtree(self, path):
        tables = ("entries", "directories") if self.directory_hashes else ("entries",)
        for table in tables:
            if path == _ROOT:
                result = self.db.execute(f"DELETE FROM {table}")
            else:
                # Slash and '0' are adjacent byte values, so this range contains only descendants.
                result = self.db.execute(
                    f"DELETE FROM {table} WHERE path = ? OR (path >= ? AND path < ?)",
                    (path, path + b"/", path + b"0"),
                )
            if table == "entries":
                removed = result.rowcount
        return removed

    def add_error(self, path, operation, error, scope):
        self.has_errors = True
        message = os.fsencode(getattr(error, "strerror", None) or str(error)).replace(b"\0", b"?")
        if self.reporter is not None:
            self.reporter.error(path, operation, message)
        row = encode_error({"path": path, "operation": operation, "message": message, "scope": scope})
        self.db.execute("INSERT INTO errors VALUES (?, ?)", (path, row))
        self.invalidate((_parent(path), path) if scope == "subtree" else (_parent(path),))

    def hash_directories(self, algorithm, cancelled):
        if not self.directory_hashes:
            return
        directories = self.db.execute("SELECT path FROM directories WHERE NOT invalid ORDER BY depth DESC, path")
        for (path,) in directories:
            if self.reporter is not None:
                self.reporter.progress()
            children = self.db.execute(
                "SELECT 1 FROM entries WHERE parent = ? AND digest = '-' LIMIT 1", (path,)
            )
            if children.fetchone():
                continue
            children = self.db.execute(
                "SELECT path, kind, digest, verification FROM entries WHERE parent = ? ORDER BY path", (path,)
            )
            verification = "verified"

            def frames():
                nonlocal verification
                for child, kind, digest, child_verification in children:
                    if child_verification != "verified":
                        verification = "unverified"
                    yield directory_child_frame(kind, child.rsplit(b"/", 1)[-1], digest)

            try:
                if self.reporter is not None:
                    self.reporter.event("HASH_DIRECTORY", path=path)
                digest = _hash_chunks(frames(), algorithm, cancelled)
            except OSError as error:
                self.add_error(path, "hash-directory", error, "entry")
                continue
            self.db.execute(
                "UPDATE entries SET digest = ?, verification = ? WHERE path = ?", (digest, verification, path)
            )
            if self.reporter is not None:
                self.reporter.event("DIRECTORY_HASHED", path=path)

    def iter_entries(self):
        rows = self.db.execute("""
            SELECT path, kind, digest, size, mtime_ns, precision_ns, link_target, verification
            FROM entries ORDER BY path
        """)
        for row in rows:
            yield dict(row)

    def write(self, workspace):
        paths = []
        try:
            errors = self.db.execute("SELECT row FROM errors ORDER BY path, row")
            for name, rows in (
                ("entries", (encode_entry(entry) for entry in self.iter_entries())),
                ("errors", (row for (row,) in errors)),
            ):
                with tempfile.NamedTemporaryFile(dir=workspace, prefix=f"scan-{name}-", delete=False) as spool:
                    paths.append(Path(spool.name))
                    for row in rows:
                        spool.write(row)
        except BaseException:
            for path in paths:
                path.unlink(missing_ok=True)
            raise
        return paths[0], paths[1], not self.has_errors


@dataclass
class _DirectoryFrame:
    context: object
    children: object
    fd: int
    path: bytes
    physical: bytes
    depth: int
    identity: tuple


@dataclass
class _BindingFrame:
    fd: int
    name: bytes
    path: bytes
    identity: tuple
    follow: bool


class _Scanner:
    def __init__(self, settings, spool, baseline, jobs, fail_fast, exclude_paths, on_scope_event, reporter=None):
        self.scope = ScanScope(settings)
        self.algorithm = settings["hash"]
        self.spool = spool
        self.baseline = baseline
        self.jobs = jobs
        self.fail_fast = fail_fast
        self.on_scope_event = on_scope_event
        self.reporter = reporter
        self.cancelled = _HashCancellation()
        self.pending = {}
        self.stack = []
        self.ancestors = set()
        self.excluded = {exclusion_binding(path) for path in exclude_paths}

    def _scope_event(self, path, reason):
        if self.reporter is not None and "link" in reason:
            self.reporter.event("LINK", path=path, action=reason)
        if self.on_scope_event is not None:
            self.on_scope_event(path, reason)

    def _collect(self, block=False):
        if not self.pending:
            return
        while True:
            done, _ = wait(self.pending, timeout=(0.2 if self.reporter is not None else None) if block else 0,
                           return_when=FIRST_COMPLETED)
            if self.reporter is not None:
                self.reporter.active = min(len(self.pending), self.jobs)
                self.reporter.progress()
            if done or not block:
                break
        for future in done:
            path, info = self.pending.pop(future)
            try:
                digest = future.result()
            except OSError as error:
                self.spool.add_error(path, "hash-file", error, "entry")
            else:
                self.spool.add_entry(_entry(path, "file", info, digest, verification="verified"))
                if self.reporter is not None:
                    self.reporter.hashed += 1
                    self.reporter.event("HASHED", path=path)
            if self.reporter is not None:
                self.reporter.completed += 1
                self.reporter.active = min(len(self.pending), self.jobs)

    def _submit(self, workers, fd, name, path, info, follow):
        if self.baseline is not None:
            digest = self.baseline(path, info)
            if digest is not None:
                self.spool.add_entry(_entry(path, "file", info, digest, verification="reused"))
                if self.reporter is not None:
                    self.reporter.completed += 1
                    self.reporter.reused += 1
                    self.reporter.event("REUSED", path=path)
                return
        duplicate = os.dup(fd)
        try:
            future = workers.submit(_hash_file, duplicate, name, info, follow, self.algorithm, self.cancelled)
        except BaseException:
            os.close(duplicate)
            raise
        self.pending[future] = path, info
        if self.reporter is not None:
            self.reporter.active = min(len(self.pending), self.jobs)
            self.reporter.event("HASH", path=path)

    def _push_directory(self, parent_fd, name, path, physical, depth, info, follow):
        identity = _identity(info)
        if identity in self.ancestors:
            self.spool.add_error(path, "follow-link", "directory resolves to an ancestor", "subtree")
            return
        self.spool.add_directory(_entry(path, "directory", info), depth)
        if not self.scope.can_descend(depth):
            return
        opened = _directory(name, parent_fd, identity, follow)
        try:
            fd, children = opened.__enter__()
        except OSError as error:
            self.spool.add_error(path, "scandir", error, "subtree")
            return
        self.ancestors.add(identity)
        self.stack.append(_DirectoryFrame(opened, children, fd, path, physical, depth, identity))
        self.spool.add_binding(path, depth, identity, follow)

    def _pop_directory(self):
        frame = self.stack.pop()
        self.ancestors.remove(frame.identity)
        frame.context.__exit__(None, None, None)

    def _invalid_binding(self, path, error):
        self.spool.discard_subtree(path)
        self.spool.add_error(path, "directory-binding", error, "subtree")

    def _close_binding(self, stack):
        frame = stack.pop()
        try:
            parent_fd = stack[-1].fd if stack else None
            current = os.stat(frame.name, dir_fd=parent_fd, follow_symlinks=frame.follow)
            if _identity(current) != frame.identity:
                raise OSError("directory binding changed while scanning")
        except OSError as error:
            self._invalid_binding(frame.path, error)
        finally:
            os.close(frame.fd)

    def _verify_bindings(self, root):
        # Reopen the recorded names from the requested root once all file reads have finished.
        # Following the recorded depth-first order keeps only one validation chain open.
        stack = []
        skipped_depth = None
        try:
            for path, depth, identity, follow in self.spool.iter_bindings():
                while len(stack) > depth:
                    self._close_binding(stack)
                if skipped_depth is not None and depth > skipped_depth:
                    continue
                skipped_depth = None
                name = root if path == _ROOT else path.rsplit(b"/", 1)[-1]
                parent_fd = stack[-1].fd if stack else None
                try:
                    fd = _open_directory(name, parent_fd, identity, follow)
                except OSError as error:
                    self._invalid_binding(path, error)
                    skipped_depth = depth
                else:
                    stack.append(_BindingFrame(fd, name, path, identity, follow))
            while stack:
                self._close_binding(stack)
        finally:
            for frame in stack:
                os.close(frame.fd)

    def _visit(self, workers, frame, child):
        name = os.fsencode(child.name)
        relative = name if frame.path == _ROOT else frame.path + b"/" + name
        physical = os.path.join(frame.physical, name)
        if physical in self.excluded:
            self._scope_event(relative, "implicit-exclusion")
            return
        if self.scope.excludes_path(relative):
            return
        try:
            info = child.stat(follow_symlinks=False)
        except OSError as error:
            self.spool.add_error(relative, "lstat", error, "subtree")
            return
        linked = stat.S_ISLNK(info.st_mode)
        follow = linked and self.scope.symlinks == "follow"
        if follow:
            physical = os.path.realpath(physical)
            if physical in self.excluded:
                self._scope_event(relative, "implicit-exclusion")
                return
            try:
                info = child.stat()
            except OSError as error:
                self.spool.add_error(relative, "follow-link", error, "subtree")
                return

        kind = None
        if stat.S_ISDIR(info.st_mode):
            kind = "directory"
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        if kind is not None:
            if self.scope.excludes_kind(relative, kind):
                if follow:
                    self._scope_event(relative, "filtered-link")
                return
            if follow:
                self._scope_event(relative, "followed-link")

        if kind == "directory":
            self._push_directory(frame.fd, name, relative, physical, frame.depth + 1, info, follow)
        elif kind == "file":
            try:
                self._submit(workers, frame.fd, name, relative, info, follow)
            except OSError as error:
                self.spool.add_error(relative, "hash-file", error, "entry")
        elif linked and not follow:
            if self.scope.symlinks == "record":
                self._scope_event(relative, "recorded-link")
                self._record_link(frame.fd, name, relative, info)
            else:
                self._scope_event(relative, "skipped-link")
        else:
            self.spool.add_error(relative, "entry-type", "unsupported filesystem entry type", "entry")

    def _record_link(self, fd, name, path, info):
        try:
            target = os.readlink(name, dir_fd=fd)
            if _snapshot(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _snapshot(info):
                raise OSError("symbolic link changed while reading")
            digest = _hash_chunks((target,), self.algorithm, self.cancelled)
        except OSError as error:
            self.spool.add_error(path, "readlink", error, "entry")
        else:
            self.spool.add_entry(_entry(path, "symlink", info, digest, target, "verified"))

    def _walk(self, root, workers):
        try:
            info = os.stat(root)
        except OSError as error:
            self.spool.add_error(_ROOT, "stat", error, "subtree")
            return
        if not stat.S_ISDIR(info.st_mode):
            self.spool.add_error(_ROOT, "entry-type", "target is not a directory", "subtree")
            return
        self._push_directory(None, root, _ROOT, os.path.realpath(root), 0, info, True)
        while self.stack:
            if self.reporter is not None:
                self.reporter.progress()
            self._collect(block=len(self.pending) >= 2 * self.jobs)
            if self.fail_fast and self.spool.has_errors:
                self.spool.invalidate(frame.path for frame in self.stack)
                self.spool.add_error(_ROOT, "aborted", "scan stopped after an error; remaining scope is unknown",
                                     "subtree")
                break
            frame = self.stack[-1]
            try:
                child = next(frame.children)
            except StopIteration:
                self._pop_directory()
            except OSError as error:
                self.spool.add_error(frame.path, "scandir", error, "subtree")
                self._pop_directory()
            else:
                self._visit(workers, frame, child)
        while self.stack:
            self._pop_directory()
        while self.pending:
            self._collect(block=True)

    def run(self, root):
        with ThreadPoolExecutor(max_workers=self.jobs) as workers:
            try:
                self._walk(root, workers)
            except BaseException:
                self.cancelled.cancel()
                raise
            finally:
                while self.stack:
                    self._pop_directory()
        self._verify_bindings(root)
        if self.reporter is not None and self.spool.directory_hashes:
            self.reporter.phase("hash-directory")
        self.spool.hash_directories(self.algorithm, self.cancelled)


def scan(root, settings, workspace, baseline=None, *, jobs=None, fail_fast=False, exclude_paths=(),
         on_scope_event=None, reporter=None):
    """Return sorted entry and error spools and a completeness flag.

    An optional baseline callback receives a regular file's byte path and stat
    result. It returns a reusable digest or None and runs on the scanning thread.
    An optional scope callback receives visited relative paths at implicit
    exclusions and symlink decisions so comparison can project the baseline.
    """
    if sqlite3 is None:
        raise OSError("the Python sqlite3 module is required to index catalog records")
    if jobs is None:
        jobs = max(1, min(4, os.cpu_count() or 1))
    with tempfile.TemporaryDirectory(dir=workspace, prefix="scan-index-") as index:
        try:
            with closing(sqlite3.connect(Path(index) / "records.sqlite3")) as connection:
                connection.row_factory = sqlite3.Row
                reporter = reporter if reporter is not None and reporter.enabled else None
                spool = _Spool(connection, settings["directory-hashes"] == "true", reporter)
                scanner = _Scanner(settings, spool, baseline, jobs, fail_fast, (*exclude_paths, workspace),
                                   on_scope_event, reporter)
                scanner.run(os.fsencode(root))
                return spool.write(workspace)
        except sqlite3.Error as error:
            raise OSError(f"cannot index catalog records: {error}") from error
