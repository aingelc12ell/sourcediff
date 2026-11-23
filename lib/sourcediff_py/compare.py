"""Project and compare catalog records with disk-backed indexes."""

import os
from contextlib import closing, contextmanager

from .format import REPORT_TOTALS, encode_change, iter_entries, iter_errors
from .scan import MTIME_PRECISION_NS, ScanScope, _HashCancellation, _parent, _Spool, exclusion_binding, sqlite3


def _ancestry(path):
    while path is not None:
        yield path
        path = _parent(path)


def _relative_exclusions(root, paths):
    resolved_root = os.path.realpath(os.fsencode(root))
    prefix = resolved_root.rstrip(b"/") + b"/"
    return {binding[len(prefix):] for binding in map(exclusion_binding, paths) if binding.startswith(prefix)}


def _scope_can_widen(saved, effective):
    return (
        saved.depth is not None and (effective.depth is None or effective.depth > saved.depth)
        or not set(saved.exclude).issubset(effective.exclude)
        or not set(saved.exclude_dir).issubset(effective.exclude_dir)
        or not saved.exclude_ext.issubset(effective.exclude_ext)
        or saved.symlinks == "skip" and effective.symlinks != "skip"
        or saved.symlinks == "record" and effective.symlinks == "follow"
    )


class _Baseline:
    def __init__(self, db, entries, saved, effective, root, exclusions):
        self.db = db
        self.algorithm = effective["hash"]
        self.saved_scope = ScanScope(saved)
        self.target_scope = ScanScope(effective)
        self.wider = _scope_can_widen(self.saved_scope, self.target_scope)
        self.changed_links = self.saved_scope.symlinks != self.target_scope.symlinks
        if effective["directory-hashes"] == "false":
            self.directory_scope = "disabled"
        elif self.wider or self.changed_links:
            self.directory_scope = "scope-changed"
        else:
            self.directory_scope = "compared"
        aggregate = self.directory_scope == "compared"
        self.rehash = aggregate and saved["directory-hashes"] == "false"
        self.spool = _Spool(db, aggregate)
        self.db.executescript("""
            CREATE TABLE target_links (path BLOB PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE failures (
                path BLOB NOT NULL, subtree INTEGER NOT NULL, PRIMARY KEY(path, subtree)
            ) WITHOUT ROWID;
        """)
        excluded = _relative_exclusions(root, exclusions)
        for entry in iter_entries(entries):
            path, kind = entry["path"], entry["kind"]
            if not self.target_scope.contains(path, kind) or (
                excluded and any(ancestor in excluded for ancestor in _ancestry(path))
            ):
                self.rehash |= aggregate
            elif kind == "directory":
                depth = 0 if path == b"." else path.count(b"/") + 1
                self.spool.add_directory(entry, depth)
            else:
                self.spool.add_entry(entry)

    def reuse_digest(self, path, info):
        entry = self.db.execute(
            "SELECT digest, size, mtime_ns, precision_ns FROM entries WHERE path = ? AND kind = 'file'",
            (path,),
        ).fetchone()
        if entry is None:
            return None
        digest, size, mtime, precision = entry
        if size == info.st_size and mtime == info.st_mtime_ns and precision == MTIME_PRECISION_NS:
            return digest
        return None

    def observe_scope(self, path, reason):
        if reason == "implicit-exclusion" or reason == "skipped-link" and self.saved_scope.symlinks == "follow":
            removed = self.spool.discard_subtree(path)
            self.rehash |= removed > 0 and self.directory_scope == "compared"
        elif reason == "recorded-link" and self.saved_scope.symlinks == "follow":
            # Its former followed descendants are out of scope even if the link read fails.
            self.db.execute("DELETE FROM entries WHERE path >= ? AND path < ?", (path + b"/", path + b"0"))
        elif reason == "filtered-link" and self.saved_scope.symlinks == "record":
            self.db.execute("DELETE FROM entries WHERE path = ? AND kind = 'symlink'", (path,))
        elif reason == "followed-link" and self.changed_links:
            self.db.execute("INSERT OR IGNORE INTO target_links VALUES (?)", (path,))

    def outside_saved_scope(self, path, kind):
        if not self.saved_scope.contains(path, kind):
            return True
        if self.changed_links and self.target_scope.symlinks == "follow":
            if self.saved_scope.symlinks == "skip":
                return any(self.db.execute("SELECT 1 FROM target_links WHERE path = ?", (ancestor,)).fetchone()
                           for ancestor in _ancestry(path))
            return any(self.db.execute(
                "SELECT 1 FROM entries WHERE path = ? AND kind = 'symlink'", (ancestor,)
            ).fetchone() for ancestor in _ancestry(path))
        return False

    def prepare(self, errors):
        if self.rehash:
            self.spool.hash_directories(self.algorithm, _HashCancellation())
            if self.spool.has_errors:
                raise OSError("cannot hash projected baseline directories")
        count = 0
        for error in iter_errors(errors):
            self.db.execute(
                "INSERT OR IGNORE INTO failures VALUES (?, ?)", (error["path"], error["scope"] == "subtree")
            )
            count += 1
        return count

    def obscured_by_error(self, path):
        return any(self.db.execute(
            "SELECT 1 FROM failures WHERE path = ? AND subtree >= ?", (ancestor, ancestor != path)
        ).fetchone() for ancestor in _ancestry(path))

    def report_context(self):
        return (
            ("baseline-coverage", ("potentially-broader" if self.wider else "within-saved-scope",)),
            ("directory-hash-scope", (self.directory_scope,)),
            ("symlink-policy", (self.saved_scope.symlinks, self.target_scope.symlinks)),
        )


@contextmanager
def baseline_index(workspace, entries, saved, effective, root, exclusions):
    """Index the projected baseline and keep it available throughout the live scan."""
    if sqlite3 is None:
        raise OSError("the Python sqlite3 module is required to index catalog records")
    try:
        with closing(sqlite3.connect(workspace / "baseline.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            yield _Baseline(db, entries, saved, effective, root, exclusions)
    except sqlite3.Error as error:
        raise OSError(f"cannot index comparison records: {error}") from error


def _merge(baseline, target):
    """Pair sorted entries by raw relative path in one streaming pass."""
    old, new = next(baseline, None), next(target, None)
    while old is not None or new is not None:
        if new is None or old is not None and old["path"] < new["path"]:
            yield old, None
            old = next(baseline, None)
        elif old is None or new["path"] < old["path"]:
            yield None, new
            new = next(target, None)
        else:
            yield old, new
            old, new = next(baseline, None), next(target, None)


def _metadata_changed(old, new):
    return any(old[field] is not None and new[field] is not None and old[field] != new[field]
               for field in ("size", "mtime_ns"))


def _matching_changes(old, new, strict, compare_directories):
    kind = new["kind"]
    if old["kind"] != kind:
        yield "TYPE_CHANGED", "counted"
        return
    if kind == "symlink":
        if old["link_target"] != new["link_target"]:
            yield "CONTENT_CHANGED", "counted"
    elif kind == "file" or compare_directories:
        if "-" in (old["digest"], new["digest"]):
            yield "UNKNOWN", "target-scan-error"
        elif old["digest"] != new["digest"]:
            yield "CONTENT_CHANGED", "counted"
    if _metadata_changed(old, new):
        yield "METADATA_CHANGED", "counted" if strict and kind != "directory" else "informational"


def _path_changes(baseline, old, new, strict, errors):
    if old is None:
        detail = "outside-baseline-scope" if baseline.outside_saved_scope(new["path"], new["kind"]) else "counted"
        yield "ADDED", detail
    elif new is None:
        if errors and baseline.obscured_by_error(old["path"]):
            yield "UNKNOWN", "target-scan-error"
        else:
            yield "MISSING", "counted"
    else:
        yield from _matching_changes(old, new, strict, baseline.directory_scope == "compared")


def _verification_totals(entry, totals):
    verification = entry["verification"]
    if entry["kind"] == "file":
        if verification == "verified":
            totals["files-hashed"] += 1
        else:
            totals["files-unverified"] += 1
            totals["files-reused"] += verification == "reused"
    elif entry["kind"] == "directory" and entry["digest"] != "-":
        key = "directory-digests-verified" if verification == "verified" else "directory-digests-unverified"
        totals[key] += 1


def compare(baseline, target_spool, error_spool, workspace, *, strict=False, complete=True, reporter=None):
    """Write a sorted change spool and return its path, report totals, and exit status."""
    totals = dict.fromkeys(REPORT_TOTALS, 0)
    totals["errors"] = baseline.prepare(error_spool)
    changes = workspace / "changes"
    with changes.open("wb") as destination:
        for old, new in _merge(baseline.spool.iter_entries(), iter_entries(target_spool)):
            if reporter is not None and reporter.enabled:
                reporter.progress()
            path = (new if new is not None else old)["path"]
            if new is not None:
                _verification_totals(new, totals)
            for category, detail in _path_changes(baseline, old, new, strict, totals["errors"]):
                totals[category.lower().replace("_", "-")] += 1
                totals["counted"] += detail in {"counted", "outside-baseline-scope"}
                destination.write(encode_change(category, path, detail, old, new))
                if reporter is not None and reporter.enabled:
                    reporter.event("CHANGE", path=path, category=category, detail=detail)
    if not complete or totals["errors"] or totals["unknown"]:
        status = 2
    else:
        status = int(totals["counted"] > 0)
    return changes, totals, status
