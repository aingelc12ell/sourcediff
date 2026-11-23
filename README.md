# SourceDiff

Catalog a Linux/WSL directory, then compare another directory against its saved
relative paths. SourceDiff reads scanned contents without modifying them. It
reports changes; it does not copy, restore, synchronize or delete files.

## Installation and prerequisites

Copy `sourcediff` and the entire `lib/` directory together into one installation
directory. No SourceDiff package manager or Python packages are needed. In a
Linux or WSL shell, from that directory:

```bash
chmod +x sourcediff
./sourcediff --help
./sourcediff catalog --help
./sourcediff compare --help
```

Invoke the launcher by its path, or add its directory to `PATH`. Moving only the
launcher loses its engine modules. Use Linux/WSL paths, such as `/mnt/d/...` in
WSL, rather than Windows drive-letter paths.

Both engines need Bash and GNU `readlink` supporting `-f -z` to locate the
installation. The default engine needs `python3` with its standard-library
`sqlite3` module (not the separate SQLite CLI). The launcher rejects Python below
3.8; acceptance testing used Python 3.13.5, so the version guard is not evidence
that every older Python release has been tested. Install your distribution's
Python 3 with SQLite support, or select the independent Bash engine when Python
is unavailable.

`--engine bash` runs without Python. It uses Bash arrays, associative arrays and
namerefs, GNU coreutils/findutils/grep, and Linux `/proc` file descriptors.
Required GNU commands are `cat`, `cut`, `dd`, `find`, `grep`, `ln`, `mkfifo`,
`mktemp`, `mv`, `readlink`, `realpath`, `rm`, `sort`, `stat`, `wc`, plus
`sha256sum` for SHA-256 and `mkdir` for comparison. The engine checks GNU tool
identification and required features before scanning. The verified environment
used Bash 5.2.37, coreutils 9.7 and findutils 4.10.0 under Debian WSL2; no older
Bash/GNU compatibility floor has been established by this test run.

SHA-256 is the default. Either engine requires the external `b3sum` command on
`PATH` to create or compare a BLAKE3 catalog. Install `b3sum` separately through
your distribution or its upstream installation instructions. A missing command
is an error; SourceDiff never silently substitutes SHA-256. Neither engine
requires network access at runtime.

## Usage

```text
sourcediff catalog TARGET [--output PATH] [options]
sourcediff compare TARGET CATALOG [--output PATH] [options]
```

For compare, supply the directory to scan first and a complete baseline catalog
second. Place options before `--` when using it to pass a path with a leading
dash. For example, `sourcediff compare --output changes.txt -- -copy -baseline`.

For example, substitute your own source and copy directories:

```bash
./sourcediff catalog /data/source --exclude-dir .git --exclude-ext tmp
./sourcediff catalog /data/source --engine bash --hash blake3 --jobs 4 --output baseline.txt
./sourcediff compare /data/copy baseline.txt
./sourcediff compare /data/copy baseline.txt --strict --quick --output changes.txt
```

The first command writes a timestamp-named SHA-256 catalog. The second creates a
separate BLAKE3 baseline with the Bash engine; the last two commands use that
baseline's algorithm with the default Python engine. Catalogs work across engines.
Choose different output names or deliberately use `--overwrite` when rerunning.

This self-contained example uses only new temporary directories. Run it from the
installation directory after installing the prerequisites (including `b3sum`):

```bash
tool="$PWD/sourcediff"
demo=$(mktemp -d)
mkdir -p "$demo/source/.git" "$demo/copy"
printf 'hello\n' > "$demo/source/message.txt"
printf 'scratch\n' > "$demo/source/cache.tmp"
printf 'internal\n' > "$demo/source/.git/state"
cp -a "$demo/source/." "$demo/copy/"
(
  cd "$demo"
  "$tool" catalog "$demo/source" --exclude-dir .git --exclude-ext tmp
  "$tool" catalog "$demo/source" --engine bash --hash blake3 --jobs 4 --output baseline.txt
  "$tool" compare "$demo/copy" baseline.txt
  "$tool" compare "$demo/copy" baseline.txt --strict --quick --output changes.txt
  "$tool" compare "$demo/copy" baseline.txt --engine bash --output bash-changes.txt
)
# All five commands above return 0 for this preserved copy.
printf 'changed\n' > "$demo/copy/message.txt"
"$tool" compare "$demo/copy" "$demo/baseline.txt" --differences-only --output "$demo/changed.txt"
status=$?
printf 'Changed comparison status: %s (expected 1)\n' "$status"
printf 'Artifacts: %s\n' "$demo"
```

The example assumes a shell without `set -e`, because a comparison that finds
differences intentionally returns 1. Inspect the artifacts in the printed
directory; remove that temporary directory yourself when finished.

## Options

| Option | Meaning |
| --- | --- |
| `-h`, `--help` | Show top-level or command help. |
| `--version` | Show SourceDiff and selected engine runtime versions. |
| `--engine python\|bash` | Select engine; Python is the default, independent of the baseline's producer. |
| `--output PATH` | Set output path; relative paths use the invocation working directory. |
| `--overwrite` | Permit replacing an existing output or log file, subject to input protection and the stable-name requirement below. |
| `-v`, `--verbose` | Write timestamped operational events and progress to stderr. |
| `--log-file PATH` | Write persistent operational events and progress; works independently of verbose mode. |
| `--jobs N` | Positive maximum hashing worker count; an explicit value overrides the default. Python defaults to `max(1, min(4, CPU count))`, using 1 when unknown. Bash uses the available CPU count from optional `nproc`, capped at 4; it falls back to 1 if the command is absent, fails, or returns anything other than a canonical positive decimal count. |
| `--fail-fast` | Stop scheduling new work after the first scan error; safely finish or cancel active work. |
| `--recursive`, `--no-recursive` | Recurse (catalog default), or include root and immediate children only. |
| `--max-depth N` | Nonnegative depth limit; root is depth 0, direct children depth 1. Both recursion and depth limits apply. |
| `--symlinks record\|follow\|skip` | Record destination text (catalog default), traverse targets, or omit links. |
| `--directory-hashes`, `--no-directory-hashes` | Enable or disable included-subtree digests; disabled by default for catalog. |
| `--exclude GLOB`, `--clear-exclude` | Repeatable general exclusions, or clear this category. |
| `--exclude-dir GLOB [GLOB ...]`, `--clear-exclude-dir` | Directory exclusion lists that prune traversal, or clear this category. |
| `--exclude-ext EXT [EXT ...]`, `--clear-exclude-ext` | Regular-file suffix exclusion lists, or clear this category. |
| `--hash sha256\|blake3` | Catalog only. SHA-256 by default; comparison always uses the baseline algorithm. |
| `--strict` | Compare only: count nondirectory metadata changes as discrepancies. |
| `--quick` | Compare only: reuse eligible file hashes; independent of `--strict`. |
| `--differences-only` | Compare only: write counted changes, scan uncertainty and errors, full comparison totals, and exit status. |

Both commands and engines accept whitespace lists for `--exclude-dir` and
`--exclude-ext`. Put **TARGET and, for compare, CATALOG before whitespace-list
options**. Each list consumes one or more values up to the next option,
including any bare path in that span. An attached, shell-quoted pipe group such
as `--exclude-dir='.git|node_modules|vendor'` consumes only that argument and can
appear before, between, or after the positional paths.

```bash
./sourcediff catalog /data/source --exclude-dir .git node_modules vendor --exclude-ext log tmp bak
./sourcediff catalog --exclude-dir='.git|node_modules|vendor' /data/source --exclude-ext='log|tmp|bak'
./sourcediff catalog /data/source --exclude-dir .git --exclude-dir node_modules --exclude-ext tmp
./sourcediff compare /data/copy baseline.txt --exclude-dir old cache --clear-exclude-dir --exclude-dir vendor build
```

Only attached `=` values on these two options are pipe-split. For a literal pipe,
use a separate quoted value: `--exclude-dir 'name|part'`. General `--exclude`
remains single-valued, with literal pipes. Empty values and empty pipe components
(`'|a'`, `'a|'`, `'a||b'`) are rejected; spaces are preserved without trimming.
Use attached values for leading dashes, for example `--exclude-dir='-old|-cache'`.
Quote spaces and globs in either syntax.

Quote globs, for example `--exclude '*.tmp'`, so the invoking shell does not
expand them. Matching is case-sensitive against root-relative path bytes.
Slash-free patterns additionally match a basename at any depth. Supported
shell-style patterns are `*`, `?`, `[abc]`, `[a-z]`, and `[!abc]`; `*` and `?`
can match slashes in the whole relative path. `**` has no special recursive
meaning. Leading dots are ordinary bytes, backslashes are literal, and POSIX
character classes such as `[[:alpha:]]` have no special class meaning. Hidden
entries are included unless excluded. General exclusions also prune matching
directories. The root itself is always included.

Extension matching uses the final suffix of a regular file's basename, with
one optional leading dot on `EXT`: `tmp` and `.tmp` match `a.tmp`, not `a.tmp.bak`.
Leading-dot-only names such as `.tmp` have no extension; matching is case-sensitive.
Recorded symlinks are not regular files for this filter.

Comparison inherits recursion, depth, all three filter lists, symlink policy and
directory-hash policy from the baseline. Explicit scalar options replace their
saved value; omitted settings remain inherited. There is no CLI option to reset
a saved finite maximum depth to unlimited; `--recursive` alone does not clear it.
The first explicit filter in a category replaces that category's saved list;
subsequent occurrences append. A `--clear-*` option empties that category at its
position on the command line, and later filters append to the empty list. For
example, `--clear-exclude --exclude '*.bak'` leaves only `*.bak`. Other filter
categories remain inherited.

Effective scope applies to both baseline and target, avoiding false missing
entries when narrowing scope. Widening scope can reveal added target entries
that the old catalog did not cover; these still count as discrepancies. The
default report identifies potentially broader coverage and explicit overrides. A
follow-mode catalog cannot recover which paths originally came through links;
changing symlink policy can therefore change types and coverage by policy alone.

## Progress and operational logs

Both commands and engines support logging. Normal invocations remain quiet
except for existing diagnostics. Use `-v` or `--verbose` for stderr, or
`--log-file PATH` for a persistent log without progress on stderr. With both
options, operational event lines are copied to both destinations. Ordinary
fatal diagnostics still go to stderr. For example:

```bash
./sourcediff catalog /data/source --output baseline.txt --verbose --log-file catalog.log
./sourcediff compare /data/copy baseline.txt --output changes.txt --log-file compare.log
```

Logs are plain newline-delimited ASCII with UTC timestamps, an event name, and
`key=value` fields. Paths and messages use the catalog's canonical `%HH` byte
escaping, including percent signs and control bytes. They contain no terminal
animation or ANSI sequences, so redirected stderr remains readable. Phase
boundaries cover validation, indexing, scanning, directory hashing, comparison,
writing and publication as applicable. File scheduling/completion, quick reuse,
link decisions, discrepancies and errors have individual events.

Periodic `PROGRESS` lines appear at most once per second, with additional phase
and final updates. They show completed regular-file work, successfully hashed
files, reused files, active worker assignments, errors and elapsed seconds.
These are observed work counts, including work later invalidated by a changed
directory binding; no preliminary counting pass, percentage or ETA is used.
The coordinator remains responsive while file hash workers run. Comparison
`TOTALS` events use report totals. `SUMMARY` distinguishes completed statuses
0 and 1 from incomplete/error status 2; interruption never reports success.
Log write failures return 2.

The log parent directory must already exist. Existing log files are refused
unless `--overwrite` explicitly permits replacement; logs are replaced, never
appended. A log cannot alias the scan root, baseline input, or catalog/report
output, including through symlinks or hardlinks. Logs are initialized through
private staging and retain an open file handle. The same stable-namespace
overwrite caveat described below applies. The current log binding and active
scratch are implicitly excluded from scanning and baseline projection, so an
in-tree log does not contribute to directory hashes. Unrelated old logs remain
ordinary entries. Logging options are runtime settings and never enter saved
catalog/report headers or become inherited comparison settings.

## Outputs and comparison results

Default filenames use local time to the second in the invocation working
directory: `catalog-YYYYMMDDHHMMSS.txt` and
`difference-YYYYMMDDHHMMSS.txt`. Timestamp collisions are refused just like any
existing output unless `--overwrite` is supplied. A report is written even when
there are no differences. Existing outputs are protected by exclusive creation
by default; artifacts are staged and atomically published on the output
filesystem. The destination parent directory must exist and be writable.

Never use the input catalog as output. SourceDiff also checks aliases to the
opened baseline. With `--overwrite`, keep the destination parent path and final
directory entry stable through publication: another process can rebind a name
between the final baseline identity check and atomic replacement. Coordinate
concurrent namespace changes yourself. This is not an atomic inode-checked
replacement guarantee. The current output, input catalog, log, and active private
workspace are excluded if encountered within the scanned tree; unrelated old
artifacts in the tree remain ordinary scan entries.

Catalog paths are relative to the scanned root (`.` is the root), so source and
copy can live at different absolute locations. Paths are case-sensitive even
when the underlying mounted filesystem behaves differently. Arbitrary Linux
filename bytes survive reversible `%HH` escaping: percent, TAB, LF, CR,
nonprintable and non-ASCII bytes are escaped with uppercase hexadecimal. Records
are sorted by decoded byte paths, not escaped spelling. Catalogs and reports are
data, never shell scripts. See the exact [catalog format](docs/catalog-format.md)
and [report format](docs/report-format.md).

Size and modification time are recorded when available. Times are signed
nanoseconds since the Unix epoch, accompanied by precision; current engines
report precision `1` (nanoseconds), which does not guarantee that the underlying
filesystem clock has nanosecond resolution. Directory size and mtime are always
informational. Permissions, ownership, access time, creation time, hard-link
topology, and moves/renames are not compared.

By default, every regular file is hashed and differences are content-first:
added, missing, content-changed and type-changed entries count. Recorded symlink
destination changes count as content changes. Metadata-only changes remain
visible but return 0 if there are no counted differences. `--strict` additionally
counts known size/mtime changes for nondirectories. Folder additions/removals
are reported alongside affected descendants; totals count report rows, not a
deduplicated number of changed files.

Use `--differences-only` for a compact report. It starts with
`SOURCEDIFF-DIFFERENCES<TAB>1` and omits invocation, settings, overrides, and
informational metadata rows. It retains counted changes, all `UNKNOWN` and
`ERROR` rows, the full comparison totals, and the completeness and exit-status
trailer. The `metadata-changed` total can therefore exceed the number of metadata
rows shown. An unchanged comparison writes the marker, totals, and trailer.
The flag applies to the current report only. Comparison scope, exit status,
and operational logs use the same rules in both report modes.

`--quick` reuses a baseline file digest only at the same projected path with
matching known size, mtime and supported precision. Missing or differing
precision forces hashing. A same-size content change with preserved mtime can
be missed, including with `--strict --quick`. Reports separately count
`files-hashed`, `files-reused`, and `files-unverified`; the last includes reused
files and is not an additional disjoint group. Directory digests depending on
reused files are unverified.

Optional directory hashes summarize only included children recursively, ordered
by basename bytes and framed with kind, escaped name and digest. Empty folders
hash the empty byte stream. Exclusions and metadata do not contribute; a failed
included descendant prevents a valid enclosing digest. Matching subtree hashes
never skip descendant comparison. Narrowed baseline scope can be rehashed from
saved entries; broader scope or a different link policy marks directory hash
scope as changed and suppresses directory content classifications while still
comparing descendants.

Symlinks default to `record`: compare destination bytes without reading the
target. `skip` omits links. `follow` accesses resolved contents under the link's
relative path and **can traverse outside TARGET**. Broken links and ancestor
cycles are errors. Distinct relative paths resolving to one inode remain
distinct entries. FIFOs, devices, sockets and other unsupported special entries
are scan errors; SourceDiff does not hash their contents.

## Errors and exit status

| Status | Meaning |
| --- | --- |
| `0` | Successful complete catalog, or complete comparison with no counted discrepancies. |
| `1` | Complete comparison with counted discrepancies. |
| `2` | Usage/dependency/format/publication error, scan error, or incomplete result; takes precedence over differences. |

Scanning continues after errors by default. Unreadable, vanished, changed-during-
hashing and unvisited entries are identified explicitly. `--fail-fast` stops new
work rather than promising an instantaneous stop of existing workers. Affected
baseline paths become `UNKNOWN` when target errors prevent establishing their
state; unreadable subtrees are not confirmed missing. Incomplete catalog/report
artifacts are labeled incomplete and return 2, including partial fail-fast
artifacts. An incomplete catalog is unusable as a comparison baseline. Invalid
baselines, invocation errors and publication failures do not publish a report
for that invocation; an existing artifact can remain from a previous run.

The scan checks observed file identity, size and mtime around hashing and checks
directory bindings. It is not a filesystem snapshot: concurrent changes can
escape observation, and entries are read at different times. Scan a quiescent
tree or a separately created filesystem snapshot when consistency is required.
Catchable interruption triggers owned-worker and temporary-workspace cleanup;
uncatchable termination or a system crash cannot guarantee that cleanup.

## Resources and verification

Hashing all file bytes is the correctness default. Tune `--jobs` for the storage:
extra workers can help independent reads and can worsen contention. Python uses
a bounded worker queue, chunked reads and SQLite disk indexes. Bash bounds worker
slots and uses GNU sorting/spools; quick comparison creates a small index file
per reusable path. Temporary disk blocks and inodes can grow with entry count,
especially for Bash quick comparisons. Private workspaces live beside the output,
so provide free space there. Neither engine loads all file contents into memory.

The informational smoke fixture uses 128 small files, three 8 MiB files and nine
directories including the root. It compares engines at one and four jobs, then
checks quick results. Prior measurements on WSL tmpfs showed that four workers
helped Bash for this fixture while Python was slightly slower; these results do
not predict throughput on other storage. GNU time RSS is process-accounting
data, and sampled temporary blocks/inodes are lower bounds that may miss short-
lived files. No universally fastest engine or concurrency setting is promised.

From the project directory in Linux/WSL with Python 3, both engine dependencies,
`b3sum`, ShellCheck and GNU time available:

```bash
python3 -B -m unittest discover -s tests -t . -v
python3 -m compileall -q lib/sourcediff_py tests
for file in sourcediff lib/bash/*.sh tests/smoke_performance.sh; do bash -n "$file" || exit; done
shellcheck -x sourcediff lib/bash/*.sh tests/smoke_performance.sh
bash tests/smoke_performance.sh
```

Run tests as an ordinary user: permission-denial fixtures skip under root.
BLAKE3 matrix cases need `b3sum`; missing-dependency handling is tested too.
ShellCheck and GNU time are validation tools, not runtime requirements. Acceptance
was exercised under WSL2, not on a separate native Linux machine. The test suite
covers byte filenames, cross-engine catalogs, filters, links, quick/strict modes,
parallelism, publication safety, malformed input, failures and cancellation;
it does not establish snapshot consistency or deployed-storage performance.
