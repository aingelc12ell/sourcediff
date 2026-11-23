"""SourceDiff argument parsing, catalog settings, and invocation lifecycle."""

import argparse
import os
import platform
import re
import shutil
import signal
import stat
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import current_thread, main_thread

from .format import encode_bytes, read_catalog_stream, write_catalog, write_report
from .output import destination_path, output_workspace, publish, validate_destination
from .progress import Reporter


TOOL_VERSION = "1"
_DEFAULT_SCOPE = {
    "recursive": "true",
    "max-depth": "-",
    "symlinks": "record",
    "directory-hashes": "false",
}
_FILTER_HELP = {
    "exclude": ("GLOB", "exclude entries matching GLOB"),
    "exclude-dir": ("GLOB", "exclude and prune directories matching GLOB"),
    "exclude-ext": ("EXT", "exclude files by final suffix, with or without a leading dot"),
}
_MAX_INT = (1 << 63) - 1
_LIST_OPTIONS = {"--exclude-dir", "--exclude-ext"}
_VALUE_OPTIONS = {
    "--engine", "--output", "--hash", "--jobs", "--max-depth",
    "--exclude", "--symlinks", "--log-file",
}


def _positional_token(value):
    # Match argparse's ordinary negative-number values as well as bare '-'.
    return not value.startswith("-") or value == "-" or re.fullmatch(r"-([0-9]+|[0-9]*\.[0-9]+)", value) is not None


class _FilterListParser(argparse.ArgumentParser):
    def parse_known_args(self, args=None, namespace=None):
        """Expand CLI groups once, before ordinary public argparse processing."""
        args = list(sys.argv[1:] if args is None else args)
        expanded = []
        index = 0
        while index < len(args):
            option = args[index]
            index += 1
            if option == "--":
                expanded.extend([option, *args[index:]])
                break
            key, separator, attached = option.partition("=")
            if key in _LIST_OPTIONS:
                if separator:
                    values = attached.split("|")
                else:
                    values = []
                    while index < len(args) and _positional_token(args[index]):
                        values.append(args[index])
                        index += 1
                if not values or any(value == "" for value in values):
                    self.error(f"{key} needs one or more nonempty values (no empty pipe components)")
                expanded.extend(f"{key}={value}" for value in values)
            else:
                expanded.append(option)
                # A scalar option owns exactly its next token, even if that
                # token resembles a filter option. Let argparse validate it.
                if not separator and key in _VALUE_OPTIONS and index < len(args):
                    expanded.append(args[index])
                    index += 1
        return super().parse_known_args(expanded, namespace)


def _decimal(value, minimum=0):
    digits = value.lstrip("0") or "0"
    if value.isascii() and value.isdecimal() and len(digits) <= 19:
        number = int(digits)
        if minimum <= number <= _MAX_INT:
            return number
    raise argparse.ArgumentTypeError(f"expected a decimal integer from {minimum} to {_MAX_INT}")


def _positive(value):
    return _decimal(value, minimum=1)


def _nonempty(value):
    if not value:
        raise argparse.ArgumentTypeError("value must not be empty")
    return value


def _engine_options(parser):
    parser.add_argument(
        "--engine", choices=("python", "bash"), default=argparse.SUPPRESS,
        help="select the execution engine (python by default)",
    )
    parser.add_argument(
        "--version", action="version", version=f"SourceDiff {TOOL_VERSION} (python {platform.python_version()})",
    )


def _scope_options(parser):
    parser.add_argument("--recursive", dest="recursive", action="store_const", const="true",
                        help="recurse into subdirectories (default for catalog)")
    parser.add_argument("--no-recursive", dest="recursive", action="store_const", const="false",
                        help="scan only the root and its immediate children")
    parser.add_argument("--max-depth", type=_decimal, metavar="N", help="limit scan depth (the root has depth zero)")
    parser.add_argument("--symlinks", choices=("record", "follow", "skip"),
                        help="record link text by default; follow may scan outside TARGET")
    parser.add_argument("--directory-hashes", dest="directory_hashes", action="store_const", const="true",
                        help="compute directory subtree hashes")
    parser.add_argument("--no-directory-hashes", dest="directory_hashes", action="store_const", const="false",
                        help="omit directory subtree hashes (default for catalog)")
    for key, (metavar, help_text) in _FILTER_HELP.items():
        destination = key.replace("-", "_")
        syntax = ("put all positional paths first for whitespace lists; or use a shell-quoted attached "
                  "group like --%s='a|b'; repeat to append; separate 'a|b' is literal" % key
                  if f"--{key}" in _LIST_OPTIONS else "repeat to specify multiple values")
        parser.add_argument(f"--{key}", action="append", type=_nonempty, dest=destination, metavar=metavar,
                            help=f"{help_text}; {syntax}")
        parser.add_argument(f"--clear-{key}", action="store_const", const=[], dest=destination,
                            help=f"clear --{key} values")


def create_parser():
    """Build the shared Python parser, leaving scope arguments unset by default."""
    parser = _FilterListParser(
        prog="sourcediff", allow_abbrev=False,
        description="Catalog a directory or compare it against a saved catalog.",
    )
    _engine_options(parser)
    commands = parser.add_subparsers(dest="command", metavar="{catalog,compare}", required=True,
                                    parser_class=argparse.ArgumentParser)
    catalog = commands.add_parser("catalog", allow_abbrev=False, help="create a directory catalog")
    comparison = commands.add_parser("compare", allow_abbrev=False, help="compare a directory with a catalog")
    for command in (catalog, comparison):
        _engine_options(command)
        command.add_argument("target", type=_nonempty, metavar="TARGET", help="directory to scan")
        command.add_argument("--output", type=_nonempty, metavar="PATH", help="write to PATH")
        command.add_argument("--overwrite", action="store_true", help="allow replacement of existing output")
        command.add_argument("-v", "--verbose", action="store_true", help="write operational logs and progress to stderr")
        command.add_argument("--log-file", type=_nonempty, metavar="PATH", help="write operational logs to PATH")
        command.add_argument("--jobs", type=_positive, default=max(1, min(4, os.cpu_count() or 1)), metavar="N",
                             help="set the maximum number of hashing workers")
        command.add_argument("--fail-fast", action="store_true", help="stop scheduling work after the first scan error")
        _scope_options(command)
    catalog.add_argument("--hash", choices=("sha256", "blake3"), help="hash algorithm (sha256 by default)")
    comparison.add_argument("catalog", type=_nonempty, metavar="CATALOG", help="complete baseline catalog")
    comparison.add_argument("--strict", action="store_true", help="count metadata changes as discrepancies")
    comparison.add_argument("--quick", action="store_true",
                            help="reuse matching-size and matching-mtime file hashes; may miss content changes")
    comparison.add_argument("--differences-only", action="store_true",
                            help="write counted changes, scan uncertainty and errors, and comparison totals")
    return parser


def effective_settings(args, saved=None):
    """Return wire-format scan settings and the explicitly supplied scope overrides."""
    overrides = {}
    settings = {
        "hash": (args.hash or "sha256") if saved is None else saved["hash"],
        "engine": "python",
        "engine-version": platform.python_version(),
        "tool-version": TOOL_VERSION,
    }
    for key, default in _DEFAULT_SCOPE.items():
        value = getattr(args, key.replace("-", "_"))
        if value is not None:
            overrides[key] = str(value)
        settings[key] = overrides.get(key, default if saved is None else saved[key])
    for key in _FILTER_HELP:
        values = getattr(args, key.replace("-", "_"))
        if values is not None:
            overrides[key] = [os.fsencode(value).decode("utf-8", "surrogateescape") for value in values]
        inherited = [] if saved is None else saved.get(key, [])
        settings[key] = list(overrides.get(key, inherited))
    return settings, overrides


def default_output(command):
    """Name an output in the invocation working directory using local time."""
    prefix = "catalog" if command == "catalog" else "difference"
    return Path.cwd() / f"{prefix}-{datetime.now():%Y%m%d%H%M%S}.txt"


def _check_hash_dependency(settings):
    if settings["hash"] == "blake3" and shutil.which("b3sum") is None:
        raise ValueError("b3sum is required for BLAKE3; install b3sum and retry")


@contextmanager
def _interruptible():
    """Turn catchable termination signals into unwinding through workspace cleanup."""
    handlers = {}

    def interrupt(signum, _frame):
        raise KeyboardInterrupt(f"interrupted by {signal.Signals(signum).name}")

    try:
        if current_thread() is main_thread():
            for name in ("SIGHUP", "SIGINT", "SIGTERM"):
                signum = getattr(signal, name, None)
                if signum is not None:
                    handlers[signum] = signal.signal(signum, interrupt)
        yield
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)


def _start_reporter(args, reporter, target, destination, identities=()):
    protected = (target, destination)
    if args.command == "compare":
        protected += (Path(args.catalog).absolute(),)
    if args.log_file:
        log = destination_path(args.log_file)
        # Validate both directions before initializing the live log.
        validate_destination(destination, args.overwrite, (*protected[:1], log, *protected[2:]),
                             forbidden_identities=identities)
        reporter.open_log(log, args.overwrite, protected, identities)
        exclusions = (log,)
    else:
        exclusions = ()
    reporter.event("START", command=args.command)
    reporter.phase("validate")
    return exclusions


def _run(args, reporter):
    if getattr(args, "engine", "python") != "python":
        raise ValueError("use the sourcediff launcher to select the Bash engine")
    target = Path(args.target).absolute()
    if not stat.S_ISDIR(target.stat().st_mode):
        raise ValueError(f"target is not a directory: {target}")
    destination = destination_path(args.output or default_output(args.command))
    if args.command == "catalog":
        from .scan import scan

        settings, _ = effective_settings(args)
        _check_hash_dependency(settings)
        extra_exclusions = _start_reporter(args, reporter, target, destination)
        with output_workspace(destination, args.overwrite, (target, *extra_exclusions)) as workspace:
            reporter.phase("scan")
            entries, errors, complete = scan(
                target, settings, workspace, jobs=args.jobs, fail_fast=args.fail_fast,
                exclude_paths=(destination, *extra_exclusions), reporter=reporter,
            )
            staged = workspace / "output"
            reporter.phase("write")
            write_catalog(staged, settings, entries, errors, complete)
            reporter.phase("publish")
            publish(staged, destination, args.overwrite, (target, *extra_exclusions),
                    forbidden_identities=reporter.identities())
        return 0 if complete else 2

    baseline = Path(args.catalog).absolute()
    protected_paths = (target, baseline)
    with output_workspace(destination, args.overwrite, protected_paths) as workspace, baseline.open("rb") as source:
        protected_identities = (os.fstat(source.fileno()),)
        extra_exclusions = _start_reporter(args, reporter, target, destination, protected_identities)
        protected_paths += extra_exclusions
        protected_identities += reporter.identities()
        reporter.phase("index")
        saved, baseline_entries, _, _ = read_catalog_stream(source, workspace, require_complete=True)
        validate_destination(
            destination, args.overwrite, protected_paths, forbidden_identities=protected_identities
        )
        settings, overrides = effective_settings(args, saved)
        _check_hash_dependency(settings)

        from .compare import baseline_index, compare
        from .scan import scan

        exclusions = (destination, baseline, os.path.realpath(baseline), *extra_exclusions)
        with baseline_index(workspace, baseline_entries, saved, settings, target, (*exclusions, workspace)) as index:
            reporter.phase("scan")
            entries, errors, complete = scan(
                target, settings, workspace, baseline=index.reuse_digest if args.quick else None,
                jobs=args.jobs, fail_fast=args.fail_fast, exclude_paths=exclusions,
                on_scope_event=index.observe_scope, reporter=reporter,
            )
            reporter.phase("compare")
            changes, totals, status = compare(index, entries, errors, workspace, strict=args.strict, complete=complete,
                                               reporter=reporter)
            reporter.event("TOTALS", **totals)
            context = (
                ("strict", ("true" if args.strict else "false",)),
                ("quick", ("true" if args.quick else "false",)),
                ("fail-fast", ("true" if args.fail_fast else "false",)),
                ("baseline-complete", ("complete",)),
                *index.report_context(),
            )
        staged = workspace / "output"
        reporter.phase("write")
        write_report(staged, os.fsencode(baseline), os.fsencode(target), settings, overrides,
                     context, changes, errors, totals, status, differences_only=args.differences_only)
        reporter.phase("publish")
        publish(staged, destination, args.overwrite, protected_paths, forbidden_identities=protected_identities)
        return status


def main(argv=None):
    """Validate and run an invocation, returning a SourceDiff exit status."""
    try:
        args = create_parser().parse_args(argv)
    except SystemExit as error:
        return error.code
    reporter = Reporter(args.verbose, bool(args.log_file))
    status = 2
    try:
        with _interruptible():
            status = _run(args, reporter)
            reporter.finish(status)
    except (OSError, ValueError, KeyboardInterrupt) as error:
        diagnostic = str(error) or "interrupted"
        print(f"sourcediff: {encode_bytes(os.fsencode(diagnostic)) if args.verbose else diagnostic}", file=sys.stderr)
        status = 2
        try:
            if not reporter.failed:
                reporter.error(os.fsencode(getattr(error, "filename", None) or "."), "invocation", diagnostic)
                reporter.finish(status)
        except OSError:
            pass
    finally:
        try:
            reporter.close()
        except OSError as error:
            diagnostic = str(error)
            print(f"sourcediff: {encode_bytes(os.fsencode(diagnostic)) if args.verbose else diagnostic}", file=sys.stderr)
            status = 2
    return status


if __name__ == "__main__":
    sys.exit(main())
