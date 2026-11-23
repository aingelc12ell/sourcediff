"""Shared fixtures for SourceDiff's protocol and engine tests."""

import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_cli(args, cwd=None, engine="python", *, env=None):
    """Run the launcher with test arguments and capture its byte streams."""
    engine_args = [] if engine is None else ["--engine", engine]
    return subprocess.run(
        ["bash", PROJECT_ROOT / "sourcediff", *engine_args, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=15,
    )


GOLDEN_HEADER = (
    b"SOURCEDIFF\t1\n"
    b"S\thash\tsha256\n"
    b"S\trecursive\ttrue\n"
    b"S\tmax-depth\t-\n"
    b"S\tsymlinks\trecord\n"
    b"S\tdirectory-hashes\tfalse\n"
    b"S\tengine\tpython\n"
    b"S\tengine-version\t3.11\n"
    b"S\ttool-version\t1\n"
    b"S\tcomplete\tcomplete\n"
    b"S\texclude\tfirst%09%FF\n"
    b"S\texclude\tlast\n"
)

GOLDEN_ENTRIES = (
    b"E\t$(touch sentinel)\tfile\t"
    b"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad\t"
    b"3\t-2\t1\t-\tverified\n"
    b"E\t.\tdirectory\t-\t-\t-2\t1\t-\tunverified\n"
    b"E\tlink\tsymlink\t"
    b"3973e022e93220f9212c18d0d0c543ae7c309e46640da93a4a0314de999f5112\t"
    b"1\t-\t-\t-\tverified\n"
    b"E\tsub\tdirectory\t-\t0\t0\t-\t-\tunverified\n"
    b"E\tsub/a%25%09%0A%0D%FF\tfile\t"
    b"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\t"
    b"0\t0\t1000000000\t-\tverified\n"
    b"E\t%FF\tfile\t"
    b"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\t"
    b"0\t0\t1\t-\tverified\n"
)

GOLDEN_CATALOG = GOLDEN_HEADER + GOLDEN_ENTRIES + b"END\tcomplete\t6\t0\n"
