# SourceDiff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Alternatively, use superpowers:subagent-driven-development if the user explicitly chooses delegated execution. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Build a Linux/WSL catalog-and-compare CLI with interchangeable Python and Bash engines and portable relative-path catalogs.

**Architecture:** A Bash launcher dispatches to a Python engine by default or a GNU/Bash engine explicitly. Both implement one byte-safe versioned catalog protocol, bounded concurrent hashing, deterministic comparison, and atomic report publication. Build and test the Python reference behavior before bringing the independent Bash implementation to parity.

**Tech Stack:** Bash, GNU coreutils/findutils, Python 3 standard library, optional `b3sum`, optional ShellCheck. Integration tests use Python `unittest`; running the Bash engine itself must never require Python.

**Spec:** `docs/superpowers/specs/2026-11-19-sourcediff-design.md` (read alongside this plan).

## Global Constraints

- Linux and WSL; scans do not modify target contents.
- Python 3 by default; `--engine bash` uses only Bash and GNU tools. Dependency checks must report unsupported required features before scanning.
- SHA-256 is the default algorithm; `--hash blake3` requires `b3sum`.
- Default outputs in the invocation working directory: `catalog-YYYYMMDDHHMMSS.txt` and `difference-YYYYMMDDHHMMSS.txt`. Follow the current written spec spelling.
- Recurse by default; record symlinks by default; directory hashes disabled by default.
- Content-first full hashing is default; `--strict` and `--quick` are independent switches.
- Preserve arbitrary Linux path bytes; parse catalogs as data, never as shell commands.
- Continue on scan errors by default; provide `--fail-fast`; errors take precedence over differences.
- Exit codes: 0 successful/no counted discrepancies, 1 counted discrepancies, 2 error/incomplete.
- No overwrite without `--overwrite`; never overwrite the input catalog.
- No Git repository currently exists. Do not initialize it. If version control becomes enabled, commit each accepted task with a subject and a brief change-summary body; preserve unrelated edits.

## File and interface map

```text
sourcediff                         Bash executable: locate installation, select engine, exec
lib/sourcediff_py/__init__.py       package marker
lib/sourcediff_py/cli.py            arguments, settings merge, orchestration, exit status
lib/sourcediff_py/format.py         byte escaping, catalog/report parsing and serialization
lib/sourcediff_py/scan.py           traversal, bounded hashing, error records, subtree hashes
lib/sourcediff_py/compare.py        scoped sorted merge and change classification
lib/sourcediff_py/output.py         temporary workspace and atomic publication
lib/bash/main.sh                    Bash argument parsing and orchestration
lib/bash/format.sh                  identical protocol, escaping, strict parsing
lib/bash/scan.sh                    GNU traversal, bounded workers, subtree aggregation
lib/bash/compare.sh                 settings projection and sorted merge
lib/bash/output.sh                  temporary workspace, traps, atomic publication
tests/support.py                   temp trees, subprocess helpers, byte-path fixtures
tests/__init__.py                  test package marker for unittest discovery
tests/test_format.py                protocol golden vectors and invalid catalogs
tests/test_cli.py                   flags, help, output naming and safety
tests/test_scan.py                  catalog fixtures and errors
tests/test_compare.py               discrepancy and metadata semantics
tests/test_engines.py               engine/algorithm/job-count matrix
tests/test_failures.py              interruption, permission and changing-file cases
tests/smoke_performance.sh          informational throughput/RSS smoke run
docs/catalog-format.md             exact shared wire protocol
README.md                          dependencies, usage, limitations and verification
```

Python interfaces use dictionaries at protocol boundaries: settings map strings to strings or ordered lists; entries contain `path: bytes`, `kind: str`, `digest: str`, `size: int|None`, `mtime_ns: int|None`, `precision_ns: int|None`, `link_target: bytes|None`, and `verification: str`. Error records contain `path: bytes`, `operation: str`, `message: bytes`, and `scope: str` (`entry` or `subtree`). Keep these field names consistent across tasks.

Python functions: `encode_bytes(value: bytes) -> str`, `decode_bytes(value: str) -> bytes`, `read_catalog(path) -> (settings, entry_spool, errors, complete)`, `scan(root, settings, workspace, baseline=None) -> (entry_spool, error_spool, complete)`, `compare(baseline_spool, target_spool, settings, error_spool) -> (change_spool, totals, status)`, `publish(staged_path, destination, overwrite, forbidden_paths) -> None`, and `main(argv=None) -> int`. Spools are paths to sorted record files, not lists of all contents.

Bash functions mirror those boundaries using filenames and status codes: `sd_encode`, `sd_decode`, `sd_read_catalog`, `sd_scan`, `sd_compare`, `sd_publish`, and `sd_main`. Pass raw paths through NUL-delimited streams or quoted arguments, never line splitting; use encoded paths as associative keys. `sd_decode` writes through a named output variable so trailing newlines survive command substitution.

## Task 1: Specify and test the shared catalog protocol

**Files:** Create `docs/catalog-format.md`, `lib/sourcediff_py/{__init__,format}.py`, `tests/{__init__,support,test_format}.py`. Add the project `lib` directory to the test import path in `tests/__init__.py` so unittest commands work without installation.

**Consumes:** Approved design. **Produces:** Encoding functions and strict protocol reader/writer used by all later tasks.

- [x] Write golden-vector tests, including this byte-preserving case:

```python
def test_path_encoding(self):
    raw = b"sub/a%\t\n\r\xff"
    self.assertEqual(encode_bytes(raw), "sub/a%25%09%0A%0D%FF")
    self.assertEqual(decode_bytes(encode_bytes(raw)), raw)
```

- [x] Run `python3 -m unittest tests.test_format -v`; confirm missing implementation fails.
- [x] Define protocol v1 in the format document: `SOURCEDIFF<TAB>1`, repeated `S<TAB>key<TAB>encoded-value` settings, `E` entry rows, `X` error rows, and terminal `END<TAB>complete|incomplete<TAB>entry-count<TAB>error-count`. Define exact field order from the entry contract above; represent absent numeric/hash values as `-`. Escape all free-text byte fields. Repeat list settings in order; reject duplicate scalar settings. Sort entries by decoded path bytes.
- [x] Define hashing frames exactly: each directory child contributes ASCII kind, NUL, encoded child basename, NUL, lowercase child digest, NUL; hash the concatenation in decoded basename order. Empty directories hash empty bytes. Recorded links hash raw destination bytes and also retain that text. Root is a directory entry with path `.`; reject other `.` components, `..` components, leading slashes, decoded NUL, noncanonical escapes and duplicate decoded paths.
- [x] Implement byte codecs and strict streaming parser: validate required settings, finite enums, integer ranges, digest lengths, row shapes, entry ordering, completion trailer/counts, no trailing data and unknown record types. Incomplete catalog parsing remains available for diagnostics but baseline validation rejects it.

```python
def encode_bytes(value):
    return ''.join(chr(b) if 32 <= b <= 126 and b != 37
                   else f'%{b:02X}' for b in value)
```

- [x] Add rejection tests for truncated input, duplicate paths/scalars, malformed `%`, invalid digest, unsupported version and shell-looking data. Verify a field containing `$(touch sentinel)` never creates a file.
- [x] Run the format suite and record results. If Git is enabled, commit with summary body: `Define byte-safe versioned catalogs and validate malformed input.`

## Task 2: Deliver launcher, CLI settings and safe output lifecycle

**Files:** Create `sourcediff`, `lib/sourcediff_py/{cli,output}.py`, `tests/test_cli.py`; update `tests/support.py`.

**Consumes:** Protocol/settings contract. **Produces:** Public CLI, `main`, `publish`, test subprocess helper `run_cli(args, cwd=None, engine='python') -> CompletedProcess`.

- [x] Add failing tests for help, unknown flags, absent target, default engine, explicit engine, invalid jobs/depth/hash, output collision, input/output alias, and default timestamp names. Example:

```python
def test_catalog_name(self):
    result = run_cli(['catalog', str(self.source)], cwd=self.work)
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(len(list(self.work.glob('catalog-??????????????.txt'))), 1)
```

- [x] Run `python3 -m unittest tests.test_cli -v`; confirm failures before implementing each behavior. Initially test successful argument parsing through help/validation; enable successful catalog cases after Task 3.
- [x] Implement launcher resolution relative to its own installation directory, preserving caller working directory and argument bytes. Dispatch Python with the bundled library path; Bash dispatch must not probe/invoke Python. Missing engines/dependencies exit 2 with a clear message.
- [x] Implement options: `--engine`, `--output`, `--catalog`, `--hash`, `--jobs`, `--recursive`, `--no-recursive`, `--max-depth`, `--exclude`, `--exclude-dir`, `--exclude-ext`, corresponding `--clear-exclude*` flags, `--symlinks`, `--directory-hashes`, `--no-directory-hashes`, `--strict`, `--quick`, `--fail-fast`, `--overwrite`, `--help`, `--version`. Reject catalog-only/comparison-only option misuse. Use an explicit “unspecified” state when merging catalog settings; repeated override filters replace saved lists for that category.
- [x] Implement staging on the destination filesystem, cleanup traps/finally blocks, source/output alias checks using canonical identities, and exclusive no-clobber publication. Use atomic link-based publication when overwrite is false and atomic replacement when true; do not rely on a preflight existence check alone. Exclude staging/workspace paths from traversal too.

```python
if overwrite:
    os.replace(staged_path, destination)
else:
    os.link(staged_path, destination)
    os.unlink(staged_path)
```

- [x] Test existing files, aliases via symlink/hard link, concurrent destination creation, and cleanup after interruption. Run CLI tests and `bash -n sourcediff`. Conditional commit summary: `Add engine dispatch, explicit settings and atomic no-clobber output.`

## Task 3: Build the Python catalog scanner

**Files:** Create `lib/sourcediff_py/scan.py`, `tests/test_scan.py`; modify `cli.py`, `support.py`.

**Consumes:** Settings, codec, workspace/publisher. **Produces:** `scan` and functioning Python `catalog`.

- [x] Add a catalog fixture test with regular/empty files, a hidden file, empty folder, nested file, newline/invalid-UTF8 names and recorded link. Test expected relative paths and SHA-256 directly:

```python
def test_file_digest(self):
    (self.source / 'a').write_bytes(b'abc')
    catalog = self.catalog(engine='python')
    self.assertEqual(catalog.entries[b'a']['digest'],
                     hashlib.sha256(b'abc').hexdigest())
```

- [x] Run `python3 -m unittest tests.test_scan -v` and confirm failing scanner cases.
- [x] Implement byte-path `os.scandir` traversal, lstat/stat policy, pruning, depth limits and filters. Define glob matching as case-sensitive `*`, `?`, bracket expressions with no shell expansion; wildcards match `/` in relative-path patterns, while slash-free patterns also match basenames. Check ancestors when applying directory filters to baseline paths later. Track ancestor device/inode identities for follow-link cycles.
- [x] Hash regular files in streaming chunks through a bounded executor; default jobs `max(1,min(4,cpu_count))`, with a queue at most twice jobs. Use SHA-256 in-process and `b3sum` via stdin for BLAKE3 so unusual filenames cannot corrupt tool output. Compare identity/size/mtime before and after reading; reject detected changes. Batch/external-sort records rather than retaining all contents/tasks.
- [x] Record unsupported special entries and traversal/hash errors with scope. On fail-fast stop submitting work and mark remaining scope unknown. Catalog errors produce an incomplete trailer and exit 2.
- [x] Add bottom-up subtree aggregation using the canonical frames from Task 1. At configured depth boundaries hash only the included scope and retain depth settings. Mark ancestor hashes invalid after descendant errors. Record directory metadata as informational.
- [x] Test all filter categories, saved settings, recursive/depth variants, symlink skip/follow/record, dangling links, loops, outside-root links, shared inode paths, output/workspace exclusion, directory hashes, BLAKE3 present/missing. Run format/CLI/scan suites. Conditional commit summary: `Catalog scoped trees with bounded hashing and explicit scan errors.`

## Task 4: Build Python comparison and discrepancy reports

**Files:** Create `lib/sourcediff_py/compare.py`, `tests/test_compare.py`; modify `cli.py`, `format.py`, `scan.py`.

**Consumes:** Valid baseline, effective settings, scan spools. **Produces:** `compare`, default/strict/quick modes, complete reports.

- [x] Write a fixture creating baseline `keep`, `change`, `remove`; then mutate content, delete `remove`, add `new`, and compare. Assert categories rather than timestamp-dependent whole-file strings:

```python
self.assertEqual(result.returncode, 1)
self.assertEqual(changes['MISSING'], {b'remove'})
self.assertEqual(changes['ADDED'], {b'new'})
self.assertEqual(changes['CONTENT_CHANGED'], {b'change'})
```

- [x] Run `python3 -m unittest tests.test_compare -v` to demonstrate failures.
- [x] Implement catalog validation before target traversal, effective-scope projection on both streams, and sorted merge by decoded path. Emit independent metadata records and counted content/type/add/remove records. Do not count directory size/mtime even in strict mode: the spec makes these informational. Compare link destination bytes as content. Include descendant additions/removals without rename inference.
- [x] Implement quick lookup using a disk-backed sorted/indexed baseline, avoiding an unbounded in-memory task list. Reuse only same-type file hashes with equal size and precision-compatible mtime. Count reused versus hashed files; never label reused subtree hashes fully verified. Strictness remains independent.
- [x] Implement deterministic report rows `ADDED`, `MISSING`, `CONTENT_CHANGED`, `TYPE_CHANGED`, `METADATA_CHANGED`, `ERROR`, `UNKNOWN`, plus settings, overrides, verification totals, completeness and final status. Use old/new values and byte escaping. Protect error scopes from false missing classifications; an aborted traversal makes unvisited scope unknown.
- [x] Test mtime-only differences return 0/default and 1/strict; same-size modified content with restored mtime is caught by full mode and explicitly unverified in quick mode. Test type changes, no differences, overrides narrowing/broadening scope, metadata precision, incomplete input rejection, and error precedence 2.
- [x] Run all Python-engine suites. Conditional commit summary: `Compare catalogs with explicit content, metadata and incomplete-scope results.`

## Task 5: Implement Python-free Bash protocol and catalog engine

**Files:** Create `lib/bash/{main,format,scan,output}.sh`, `tests/test_engines.py`; modify launcher dispatch.

**Consumes:** Frozen protocol, CLI and scanner fixtures. **Produces:** `--engine bash catalog` compatible with Python catalogs.

- [x] Add cross-reader tests: Python reads a Bash catalog and Bash validates a Python catalog. Include invalid UTF-8, trailing-newline paths and link text, leading dash, `%`, tab and Unicode filenames.

```python
for writer in ('python', 'bash'):
    baseline = self.catalog(engine=writer)
    for reader in ('python', 'bash'):
        result = run_cli(['compare', str(self.source), '--catalog', str(baseline.path)],
                         cwd=self.work, engine=reader)
        self.assertEqual(result.returncode, 0, result.stderr)
```

- [x] Run `python3 -m unittest tests.test_engines -v`; expect missing Bash engine failures (comparison pair completes in Task 6).
- [x] Implement `LC_ALL=C` byte codecs and strict parsing without `eval`. Decode into named variables using Bash builtins, not command substitution; preserve trailing newlines. Implement all CLI option and dependency checks with the same saved-settings merge rules.
- [x] Implement NUL-safe GNU traversal with pruning, GNU `stat` timestamps and identity checks, `readlink -z`, and explicit cycle handling for follow mode. Never pipe a FIFO/device into a hasher. Hash via stdin; normalize GNU checksum output. Use a bounded persistent Bash worker pool with spool files and parent-coordinated fail-fast status, avoiding Python or per-file shell interpreter launches.
- [x] Implement external sort and bottom-up directory aggregation using the identical framing; use encoded records for sort/join only with the documented decoded-byte sort key. Implement safe temp handling and atomic publication parity.
- [x] Run Bash catalog fixtures with Python validation; run `bash -n sourcediff lib/bash/*.sh` (loop over files so each is checked) and `shellcheck sourcediff lib/bash/*.sh` when installed. Conditional commit summary: `Add GNU/Bash catalog engine with byte-safe protocol parity.`

## Task 6: Implement Bash comparison and prove engine interchangeability

**Files:** Create `lib/bash/compare.sh`; modify Bash main/scan/format modules and `tests/test_engines.py`.

**Consumes:** Bash catalogs and reference comparison behavior. **Produces:** Full Python-free engine.

- [x] Run the shared comparison scenarios for all four writer/reader engine combinations. Add assertions for report status and normalized change rows; ignore informational engine names and timestamps only.
- [x] Confirm tests fail for unimplemented Bash comparison; implement scope projection, disk-based quick baseline lookup, sorted merge, error-scope suppression and report generation using the exact Task 4 categories.
- [x] Test Bash-only execution with a controlled executable PATH containing required Bash/GNU tools but no Python. Run the command directly from a shell fixture; the test runner may use Python, the child engine may not.
- [x] Add SHA-256/BLAKE3 matrix when BLAKE3 is installed, jobs 1/2/4, and link/depth/filter combinations. Compare canonical catalog entry rows and normalized reports across engines.
- [x] Run `python3 -m unittest discover -s tests -t . -v`, syntax checks and available ShellCheck; correct parity failures before advancing. Conditional commit summary: `Complete Python-free comparison and cross-engine interoperability.`

## Task 7: Verify failure handling, races and scaling limits

**Files:** Create `tests/test_failures.py`, `tests/smoke_performance.sh`; modify affected engine modules only for demonstrated failures.

**Consumes:** Both complete engines. **Produces:** Evidence for incomplete results, safe outputs and bounded work.

- [x] Add controlled permission-denied tests under an unprivileged Linux user; if run as root, explicitly skip with reason and rerun unprivileged before claiming permission coverage. Assert no false missing records:

```python
self.assertEqual(result.returncode, 2)
self.assertIn(b'UNKNOWN', result.stdout + report_bytes)
self.assertNotIn(b'MISSING\tlocked/child', report_bytes)
```

- [x] Add deterministic changing-file tests by wrapping the hash operation at its module boundary (Python) or supplying a controlled hasher executable (Bash), altering the file between before/after observations. Do not rely on timing alone. Add vanished entries, directory errors, FIFO, interrupted workers, partial catalog rejection and fail-fast queue cancellation.
- [x] Verify competing output creation cannot clobber an existing file; terminate a scan and assert no complete output or abandoned workers remain. Verify output/input aliases and temporary paths are excluded with both engines.
- [x] Create a smoke script that generates many small files and a few large files in a temporary directory, runs catalog/compare at jobs 1 and 4 with `/usr/bin/time`, prints elapsed time/RSS/counts, and traps cleanup. Assert equal results; impose no host-specific speed threshold. Check queued tasks and per-worker buffering remain bounded.
- [x] Run failure tests, full suite, syntax checks and optional ShellCheck. Record platform/tools, skips and limitations. Conditional commit summary: `Verify incomplete scans, atomic outputs and bounded concurrent work.`

## Task 8: Publish usage documentation and final acceptance evidence

**Files:** Create `README.md`; update `docs/catalog-format.md` for any clarified protocol rules and this plan's checkboxes.

**Consumes:** Verified CLI. **Produces:** User-ready examples and reproducible validation commands.

- [x] Document installation without a package manager, executable permissions, dependencies, both engines, BLAKE3 installation requirement, all flags, glob semantics, filter replacement/clearing, output collisions and status codes. Include:

```bash
./sourcediff catalog /data/source --exclude-dir .git --exclude-ext tmp
./sourcediff catalog /data/source --engine bash --hash blake3 --jobs 4 --output baseline.txt
./sourcediff compare /data/copy --catalog baseline.txt
./sourcediff compare /data/copy --catalog baseline.txt --strict --quick --output changes.txt
```

- [x] Explain metadata-only exit semantics, quick-mode blind spots, directory hash scope, follow-link traversal outside root, arbitrary filename escaping, incomplete-scan unknowns and non-snapshot guarantees. Describe resource tuning without promising universally fastest performance.
- [x] Execute documented examples against temporary fixtures, validating expected statuses and generated filenames. Run `python3 -m unittest discover -s tests -t . -v`, `python3 -m compileall -q lib/sourcediff_py tests`, Bash syntax checks, available ShellCheck and smoke script once after final code changes. Do not claim Linux/WSL tests passed from Windows-only checks; if WSL is unavailable, report that execution requirement explicitly.
- [x] Review every spec section against Tasks 1–8; record actual commands/results and remaining limitations in the handoff. If Git is enabled, commit with summary body: `Document installation, CLI options, verification and operational limits.`

## Plan self-review and execution order

Execute Tasks 1–8 sequentially; each carries its own failing-test → implementation → passing-test cycle. No independent agent work is required. Task 1 owns the wire contract; Task 2 owns CLI/output behavior; Task 3 owns catalog semantics; Task 4 owns comparisons; Tasks 5–6 prove engine parity; Task 7 proves failure/scaling behavior; Task 8 covers documentation and final evidence. The task interfaces use consistent names and spool-based boundaries. No implementation or Git initialization is authorized merely by creating this plan.
