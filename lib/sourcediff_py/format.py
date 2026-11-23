"""Canonical catalog records and disk-backed spools for protocol v1."""

import re
from itertools import chain
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote_from_bytes, unquote_to_bytes


class FormatError(ValueError):
    """A record or catalog violates the SourceDiff wire protocol."""


_VERSION = b"SOURCEDIFF\t1\n"
_LITERAL_BYTES = bytes(value for value in range(0x20, 0x7F) if value != 0x25)
_MAX_INT = (1 << 63) - 1
_MIN_INT = -(1 << 63)
_INTEGER = re.compile(r"0|-?[1-9][0-9]{0,18}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_KINDS = {"file", "directory", "symlink"}
_VERIFICATION = {"verified", "reused", "unverified"}
_SCOPE = {"entry", "subtree"}
_COMPLETENESS = {"complete", "incomplete"}
_BOOLEANS = {"true", "false"}
_SCALAR_SETTINGS = {
    "hash": {"sha256", "blake3"},
    "recursive": _BOOLEANS,
    "max-depth": None,
    "symlinks": {"record", "follow", "skip"},
    "directory-hashes": _BOOLEANS,
    "engine": {"python", "bash"},
    "engine-version": None,
    "tool-version": None,
    "complete": _COMPLETENESS,
}
_LIST_SETTINGS = ("exclude", "exclude-dir", "exclude-ext")


def encode_bytes(value):
    """Return the canonical ASCII escape sequence for bytes."""
    return quote_from_bytes(value, safe=_LITERAL_BYTES)


def decode_bytes(value):
    """Decode canonical ASCII byte escapes, rejecting alternate spellings."""
    if not value.isascii():
        raise FormatError("encoded byte fields must be ASCII")
    decoded = unquote_to_bytes(value)
    if encode_bytes(decoded) != value:
        raise FormatError("malformed or noncanonical byte escape")
    return decoded


def _byte_field(value):
    decoded = decode_bytes(value)
    if b"\0" in decoded:
        raise FormatError("NUL is not allowed in a catalog field")
    return decoded


def _text_field(value):
    return _byte_field(value).decode("utf-8", "surrogateescape")


def _encode_text(value):
    return encode_bytes(value.encode("utf-8", "surrogateescape"))


def _path_field(value):
    path = _byte_field(value)
    if path != b"." and any(part in (b"", b".", b"..") for part in path.split(b"/")):
        raise FormatError("entry and error paths must be canonical relative paths")
    return path


def _integer(value, minimum, label, *, optional=False):
    if optional and value == "-":
        return None
    if _INTEGER.fullmatch(value):
        number = int(value)
        if minimum <= number <= _MAX_INT:
            return number
    raise FormatError(f"invalid {label}")


def _fields(line):
    if not line.endswith(b"\n"):
        raise FormatError("unterminated catalog record")
    try:
        return line[:-1].decode("ascii").split("\t")
    except UnicodeDecodeError as exc:
        raise FormatError("catalog records must be ASCII") from exc


def _record(*fields):
    return ("\t".join(fields) + "\n").encode("ascii")


def _optional_integer(number):
    return "-" if number is None else str(number)


def _entry_values(entry):
    target = entry["link_target"]
    if (entry["kind"] == "symlink") != (target is not None):
        raise FormatError("a link target is required only for recorded symlinks")
    return (
        entry["kind"],
        entry["digest"],
        _optional_integer(entry["size"]),
        _optional_integer(entry["mtime_ns"]),
        _optional_integer(entry["precision_ns"]),
        "-" if target is None else encode_bytes(target),
        entry["verification"],
    )


def encode_entry(entry):
    """Serialize an entry dictionary as one LF-terminated binary spool row."""
    return _record("E", encode_bytes(entry["path"]), *_entry_values(entry))


def encode_change(category, path, detail, old, new):
    """Serialize a comparison record with both entry values."""
    missing = ("-",) * 7
    return _record(
        category, encode_bytes(path), detail,
        *(_entry_values(old) if old is not None else missing),
        *(_entry_values(new) if new is not None else missing),
    )


def _parse_entry(fields):
    if len(fields) != 9 or fields[0] != "E":
        raise FormatError("invalid entry record")
    _, path_field, kind, digest, size, mtime, precision, target, verification = fields
    path = _path_field(path_field)
    if kind not in _KINDS or (path == b"." and kind != "directory"):
        raise FormatError("invalid entry kind")
    if digest != "-" and not _DIGEST.fullmatch(digest):
        raise FormatError("invalid entry digest")
    if (
        verification not in _VERIFICATION
        or (verification == "reused" and kind != "file")
        or (digest == "-" and verification != "unverified")
    ):
        raise FormatError("invalid entry verification")
    size = _integer(size, 0, "entry size", optional=True)
    mtime = _integer(mtime, _MIN_INT, "entry mtime", optional=True)
    precision = _integer(precision, 1, "entry time precision", optional=True)
    if mtime is None and precision is not None:
        raise FormatError("entry time precision requires an mtime")
    if kind == "symlink":
        target = _byte_field(target)
        if not target or digest == "-":
            raise FormatError("recorded symlinks require a target and digest")
    elif target != "-":
        raise FormatError("only recorded symlinks have targets")
    else:
        target = None
    return {
        "path": path,
        "kind": kind,
        "digest": digest,
        "size": size,
        "mtime_ns": mtime,
        "precision_ns": precision,
        "link_target": target,
        "verification": verification,
    }


def encode_error(error):
    """Serialize an error dictionary as one LF-terminated binary spool row."""
    return _record(
        "X", encode_bytes(error["path"]), _encode_text(error["operation"]),
        encode_bytes(error["message"]), error["scope"],
    )


def _parse_error(fields):
    if len(fields) != 5 or fields[0] != "X":
        raise FormatError("invalid error record")
    _, path, operation, message, scope = fields
    operation = _text_field(operation)
    if not operation or scope not in _SCOPE:
        raise FormatError("invalid error operation or scope")
    return {
        "path": _path_field(path),
        "operation": operation,
        "message": _byte_field(message),
        "scope": scope,
    }


def iter_entries(path):
    """Yield entry dictionaries from an entry spool in its stored order."""
    with open(path, "rb") as spool:
        for line in spool:
            yield _parse_entry(_fields(line))


def iter_errors(path):
    """Yield error dictionaries from an error spool in its stored order."""
    with open(path, "rb") as spool:
        for line in spool:
            yield _parse_error(_fields(line))


def directory_child_frame(kind, basename, digest):
    """Return the bytes hashed for one direct child of a directory."""
    if kind not in _KINDS or not _DIGEST.fullmatch(digest):
        raise FormatError("a directory child frame requires a kind and digest")
    if basename in (b"", b".", b"..") or b"/" in basename or b"\0" in basename:
        raise FormatError("a directory child frame requires a basename")
    return f"{kind}\0{encode_bytes(basename)}\0{digest}\0".encode("ascii")


def _validate_setting(key, value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise FormatError(f"invalid value for setting {key}")
    if key in _LIST_SETTINGS:
        return
    choices = _SCALAR_SETTINGS[key]
    if choices is not None and value not in choices:
        raise FormatError(f"invalid value for setting {key}")
    if key == "max-depth":
        _integer(value, 0, "max-depth setting", optional=True)


def _check_required_settings(settings):
    for key in _SCALAR_SETTINGS:
        if key not in settings:
            raise FormatError(f"missing required setting {key}")


def _read_settings(source):
    settings = {key: [] for key in _LIST_SETTINGS}
    for line in source:
        fields = _fields(line)
        if fields[0] != "S":
            _check_required_settings(settings)
            return settings, chain((fields,), map(_fields, source))
        if len(fields) != 3:
            raise FormatError("invalid setting record")
        _, key, encoded = fields
        if key not in _SCALAR_SETTINGS and key not in _LIST_SETTINGS:
            raise FormatError(f"unknown setting {key!r}")
        value = _text_field(encoded)
        _validate_setting(key, value)
        if key in _LIST_SETTINGS:
            settings[key].append(value)
        elif key in settings:
            raise FormatError(f"duplicate scalar setting {key}")
        else:
            settings[key] = value
    raise FormatError("missing catalog trailer")


class _EntrySequence:
    """Validate entry invariants using constant state as the spool streams."""

    def __init__(self, settings):
        self.complete = settings["complete"] == "complete"
        self.directory_hashes = settings["directory-hashes"] == "true"
        self.record_links = settings["symlinks"] == "record"
        self.previous = None
        self.has_root = False
        self.count = 0

    def accept(self, fields):
        entry = _parse_entry(fields)
        path = entry["path"]
        if self.previous is not None and path <= self.previous:
            raise FormatError("entries must have unique paths in decoded byte order")
        self.previous = path
        self.has_root |= path == b"."

        kind = entry["kind"]
        has_digest = entry["digest"] != "-"
        if kind == "symlink" and not self.record_links:
            raise FormatError("symlink entry requires the record policy")
        if kind == "directory" and has_digest and not self.directory_hashes:
            raise FormatError("directory digest supplied with directory hashes disabled")
        if self.complete and not has_digest and (kind == "file" or self.directory_hashes):
            raise FormatError("missing digest in complete catalog")
        self.count += 1

    def finish(self, error_count):
        if self.complete and (not self.has_root or error_count):
            raise FormatError("a complete catalog requires a directory root and no errors")


def _check_trailer(fields, settings, entry_count, error_count):
    if len(fields) != 4 or fields[1] != settings["complete"]:
        raise FormatError("invalid or inconsistent catalog trailer")
    entries = _integer(fields[2], 0, "entry count")
    errors = _integer(fields[3], 0, "error count")
    if (entries, errors) != (entry_count, error_count):
        raise FormatError("catalog counts do not match its trailer")


def _read_catalog(source, entries, errors):
    if source.readline(len(_VERSION)) != _VERSION:
        raise FormatError("expected a SourceDiff version 1 catalog")
    settings, rows = _read_settings(source)
    sequence = _EntrySequence(settings)
    error_count = 0
    for fields in rows:
        if fields[0] == "E" and not error_count:
            sequence.accept(fields)
            entries.write(_record(*fields))
        elif fields[0] == "X":
            _parse_error(fields)
            errors.write(_record(*fields))
            error_count += 1
        elif fields[0] == "END":
            _check_trailer(fields, settings, sequence.count, error_count)
            sequence.finish(error_count)
            if source.read(1):
                raise FormatError("unexpected data after catalog trailer")
            return settings, sequence.complete
        else:
            raise FormatError("unknown or misplaced catalog record")
    raise FormatError("missing catalog trailer")


def read_catalog(path, workspace=None, *, require_complete=False):
    """Validate a catalog into caller-owned entry and error spool paths."""
    with open(path, "rb") as source:
        return read_catalog_stream(source, workspace, require_complete=require_complete)


def read_catalog_stream(source, workspace=None, *, require_complete=False):
    """Validate an open binary stream at its current position without closing it."""
    created = []
    try:
        with NamedTemporaryFile(
            mode="wb", dir=workspace, prefix="sourcediff-entries-", delete=False
        ) as entries:
            created.append(Path(entries.name))
            with NamedTemporaryFile(
                mode="wb", dir=workspace, prefix="sourcediff-errors-", delete=False
            ) as errors:
                created.append(Path(errors.name))
                settings, complete = _read_catalog(source, entries, errors)
                if require_complete and not complete:
                    raise FormatError("incomplete catalog cannot be used as a baseline")
    except BaseException:
        for spool in created:
            spool.unlink(missing_ok=True)
        raise
    return settings, created[0], created[1], complete


def _write_settings(destination, settings):
    for key in settings:
        if key not in _SCALAR_SETTINGS and key not in _LIST_SETTINGS:
            raise FormatError(f"unknown setting {key!r}")
    _check_required_settings(settings)
    for key in _SCALAR_SETTINGS:
        value = settings[key]
        _validate_setting(key, value)
        destination.write(_record("S", key, _encode_text(value)))
    for key in _LIST_SETTINGS:
        values = settings.get(key, [])
        if not isinstance(values, list):
            raise FormatError(f"setting {key} requires a list")
        for value in values:
            _validate_setting(key, value)
            destination.write(_record("S", key, _encode_text(value)))


def write_catalog(path, settings, entry_spool, error_spool, complete):
    """Write a staged catalog from canonical spools, validating every row."""
    status = "complete" if complete else "incomplete"
    settings = dict(settings, complete=status)
    with open(entry_spool, "rb") as entries, open(error_spool, "rb") as errors, open(
        path, "wb"
    ) as destination:
        destination.write(_VERSION)
        _write_settings(destination, settings)
        sequence = _EntrySequence(settings)
        for line in entries:
            sequence.accept(_fields(line))
            destination.write(line)
        error_count = 0
        for line in errors:
            _parse_error(_fields(line))
            destination.write(line)
            error_count += 1
        sequence.finish(error_count)
        destination.write(_record("END", status, str(sequence.count), str(error_count)))


REPORT_TOTALS = (
    "added", "missing", "content-changed", "type-changed", "metadata-changed",
    "unknown", "errors", "counted", "files-hashed", "files-reused",
    "files-unverified", "directory-digests-verified", "directory-digests-unverified",
)


def write_report(path, source, target, settings, overrides, context, changes, errors, totals, status,
                 *, differences_only=False):
    """Stream a comparison report to a staged file."""
    completeness = "incomplete" if status == 2 else "complete"
    with open(path, "wb") as destination:
        if differences_only:
            destination.write(b"SOURCEDIFF-DIFFERENCES\t1\n")
        else:
            destination.write(b"SOURCEDIFF-REPORT\t1\n")
            destination.write(_record("I", "source-catalog", encode_bytes(source)))
            destination.write(_record("I", "target", encode_bytes(target)))
            for key, values in context:
                destination.write(_record("I", key, *(_encode_text(value) for value in values)))
            _write_settings(destination, {**settings, "complete": completeness})
            for key in chain(_SCALAR_SETTINGS, _LIST_SETTINGS):
                if key not in overrides:
                    continue
                value = overrides[key]
                if isinstance(value, list):
                    for item in value:
                        destination.write(_record("O", key, _encode_text(item)))
                    if not value:
                        destination.write(_record("O", key))
                else:
                    destination.write(_record("O", key, _encode_text(value)))
        with open(changes, "rb") as rows:
            if differences_only:
                for row in rows:
                    category, _, detail, _ = row.split(b"\t", 3)
                    if category != b"METADATA_CHANGED" or detail != b"informational":
                        destination.write(row)
            else:
                destination.writelines(rows)
        for error in iter_errors(errors):
            destination.write(b"ERROR" + encode_error(error)[1:])
        for key in REPORT_TOTALS:
            destination.write(_record("T", key, str(totals[key])))
        destination.write(_record("END", completeness, str(status)))
