"""Private staging and atomic publication on the destination filesystem."""

import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from .interrupts import defer_interrupts


def destination_path(path):
    """Validate the raw pathname and resolve only its existing parent."""
    raw = os.fsdecode(os.fspath(path))
    parent, name = os.path.split(raw)
    if name in ("", ".", ".."):
        raise ValueError(f"output must name a file: {raw!r}")
    parent = parent or os.curdir
    # Check the raw parent first so a missing or nondirectory component cannot
    # disappear during normalization of a later dot-dot component.
    if not stat.S_ISDIR(os.stat(parent).st_mode):
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), parent)
    return Path(parent).resolve(strict=True) / name


def _aliases_protected(destination, forbidden_paths, forbidden_identities):
    canonical = os.path.realpath(destination)
    if any(canonical == os.path.realpath(path) for path in forbidden_paths):
        return True
    try:
        current = destination.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    if any(os.path.samestat(current, identity) for identity in forbidden_identities):
        return True
    for path in forbidden_paths:
        try:
            if os.path.samestat(current, os.stat(path)):
                return True
        except (FileNotFoundError, NotADirectoryError):
            continue
    return False


def validate_destination(destination, overwrite, forbidden_paths=(), *, forbidden_identities=()):
    """Validate output against protected paths and identities from open files."""
    destination = destination_path(destination)
    if _aliases_protected(destination, tuple(forbidden_paths), forbidden_identities):
        raise ValueError(f"output aliases the input catalog or scan target: {destination}")
    if os.path.lexists(destination):
        if not overwrite:
            raise FileExistsError(f"output already exists: {destination}; use --overwrite to replace it")
        if destination.is_dir():
            raise IsADirectoryError(f"output is a directory: {destination}")
    return destination


@contextmanager
def output_workspace(destination, overwrite, forbidden_paths=()):
    """Yield a private directory for artifact staging and all scan scratch files."""
    destination = validate_destination(destination, overwrite, forbidden_paths)
    with defer_interrupts() as resume:
        with TemporaryDirectory(prefix=".sourcediff-", dir=destination.parent) as directory:
            resume()
            yield Path(directory)


def publish(staged_path, destination, overwrite, forbidden_paths=(), *, forbidden_identities=()):
    """Atomically publish a closed staged artifact, rechecking protected inputs."""
    destination = validate_destination(
        destination, overwrite, forbidden_paths, forbidden_identities=forbidden_identities
    )
    if overwrite:
        os.replace(staged_path, destination)
    else:
        os.link(staged_path, destination)
        os.unlink(staged_path)
