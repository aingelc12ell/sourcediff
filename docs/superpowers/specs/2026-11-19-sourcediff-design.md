# SourceDiff command-line tool design

Date: 2026-11-19
Status: Approved design captured for written-spec review

## Purpose and platform

Build a Bash command-line tool for Linux and WSL that catalogs a directory and compares another directory against that catalog using relative paths. The tool is read-only with respect to scanned contents.

## Architecture and dependencies

A Bash launcher exposes two interchangeable engines: Python 3 by default and a Python-free Bash/GNU engine selected with `--engine bash`. Both engines read and write the same versioned text catalog. Standard GNU tools provide traversal and hashing for the Bash engine. SHA-256 is the default algorithm; `--hash blake3` requires `b3sum`. Missing dependencies produce actionable errors rather than silently changing algorithms.

Keep argument handling, catalog serialization, scanning/hashing, comparison, and reporting as distinct responsibilities. Share format fixtures across engines to prevent behavioral drift.

## Commands and output paths

```bash
sourcediff catalog TARGET [--output PATH] [options]
sourcediff compare TARGET --catalog PATH [--output PATH] [options]
```

Without `--output`, write into the invocation working directory using local time:

- `catalog-YYYYMMDDHHMMSS.txt`
- `difference-YYYYMMDDHHMMSS.txt`

The spelling `difference` is intentional, as requested. Refuse existing output paths unless `--overwrite` is explicit, including timestamp collisions. Never overwrite the input catalog. Exclude the current output and input catalog from traversal when they are inside the scanned tree. Publish completed output atomically; an interrupted run must not leave an apparently complete catalog.

## Catalog format

Use a versioned, escaped, tab-separated text format, not executable shell syntax. A settings header carries the hash algorithm, recursion/depth settings, filters, symbolic-link policy, directory-hash setting, scan completeness, and informational engine/tool versions. Entry records carry relative path, entry type, content hash where applicable, byte size, and modification time at nanosecond precision where supported.

For cross-engine interoperability, encode percent, tab, newline, carriage return, and non-printable/non-ASCII path bytes as percent-encoded bytes. This preserves arbitrary Linux filename bytes; paths never include a leading slash or parent traversal. Reject malformed escapes, unsupported versions, duplicate paths, invalid settings, and incomplete catalogs as valid comparison baselines. Read settings through an allowlisted parser; never use `eval` or source a catalog. Serialization and ordering use bytewise C-locale rules and do not depend on filesystem enumeration order.

Record directories, including empty directories. Directory size and modification time are informational; directory content is represented by optional subtree hashes. Record unsupported special entries as scan errors rather than reading FIFOs, devices, or sockets.

## Scope and filters

Recurse by default. Provide `--no-recursive`, `--max-depth N`, repeatable `--exclude GLOB`, `--exclude-dir GLOB`, and `--exclude-ext EXT`. The root has depth zero; its direct children have depth one. Nonrecursive scanning includes direct entries only.

Match exclusion globs case-sensitively against relative paths; slash-free patterns also match basenames at any depth. Use documented shell-style glob semantics shared by both engines, independent of shell expansion. Directory exclusions prune traversal. Extension exclusions match the final filename suffix; accept an optional leading dot. Hidden entries are included unless excluded.

Comparison reuses saved scope settings by default. Explicit overrides replace the corresponding saved setting; provide explicit clear-filter options so saved exclusions can be removed. The report lists effective settings and overrides. Apply effective scope to both baseline entries and target traversal so excluded baseline entries do not become false missing entries. A broader scope can expose target entries absent from the filtered baseline; identify these as added within the effective scope and state that the original catalog did not cover them.

## Hashes, links, and directories

Support `--hash sha256|blake3` during catalog creation. Comparison always uses the catalog algorithm. Hash file bytes with configurable bounded parallelism through `--jobs N`; choose a conservative CPU-based default and document tuning for storage contention. Avoid launching one interpreter per file. Deterministic results must not depend on job count.

Provide `--symlinks record|follow|skip`, defaulting to `record`. Record mode stores and compares the link destination text, without accessing its target. Skip mode omits links. Follow mode traverses linked contents under the link's relative path, detects ancestor cycles, and reports broken links or cycles as errors. Follow mode may traverse outside the target root; document that consequence in help. It must not silently drop distinct paths that resolve to the same inode.

`--directory-hashes` is optional and disabled by default. Compute subtree hashes bottom-up from a canonical ordered sequence of included child names, types, and content/subtree hashes, with unambiguous field framing. Excluded entries and metadata do not affect content subtree hashes. Empty directories have a defined canonical hash. A directory hash is valid only when all included descendants were successfully scanned. Quick comparisons cannot claim fully verified directory hashes when file hashes were reused.

## Comparison semantics

Default content-first mode always hashes regular files. Report added, missing, content-changed, type-changed, and metadata-changed entries separately. Size and modification-time differences remain visible, but metadata-only differences do not cause exit code 1 in default mode. Symlink destination changes count as content changes. Folder additions/removals are reported alongside their affected entries; do not infer moves or renames.

`--strict` treats metadata differences as discrepancies as well. `--quick` skips hashing a regular file when size and modification time match the baseline; clearly label those entries as unverified/reused in summary counts. Quick mode can miss changes preserving both attributes. It is independent of strictness and can be combined with `--strict`. Missing metadata precision prevents the quick shortcut unless values can be compared at the same supported precision.

Compare content and metadata per entry without treating matching directory hashes as permission to skip descendant checks. Keep path matching case-sensitive on Linux/WSL, even when a mounted filesystem has different behavior.

## Reports and error handling

Write a readable escaped text report with version, source catalog, target, effective settings, comparison mode, overrides, completeness, verification counts, discrepancy records, error records, and totals. Paths use the same reversible escaping as catalogs. A report with no discrepancies still contains the summary. Include old/new hash, size, or modification-time values where relevant.

Continue on scan errors by default; `--fail-fast` stops scheduling new work on the first error and safely finishes or cancels active workers. Check file identity, size, and modification time before and after hashing to detect observed changes; this is not a filesystem snapshot guarantee. Report unreadable files, traversal failures, vanished entries, and detected changes explicitly. Never classify an unreadable or unvisited subtree as confirmed missing; mark affected comparisons unknown/incomplete.

Incomplete scans produce explicitly incomplete artifacts and exit code 2. Fail-fast artifacts, when emitted, must also identify their partial scope. Error status takes precedence over discrepancy status.

- `0`: successful catalog, or complete comparison with no counted discrepancies.
- `1`: complete comparison with counted discrepancies.
- `2`: usage/dependency errors, malformed/incomplete baseline, scan errors, or incomplete results.

## Performance and practical limits

Prune excluded directories before traversal, bound worker counts and queues, and stream records where possible. Use external sorting/merge comparison where necessary to avoid memory proportional to all file contents or an unbounded task queue. Temporary disk use can grow with entry count. Hashing all bytes remains the correctness default; speed depends on storage, file distribution, algorithm, and concurrency. No claim of universally fastest performance is made.

## Verification and deliverables

Deliver the Bash command, focused engine modules/helpers, usage documentation with examples, and an automated fixture-based integration suite runnable on Linux/WSL. Verify Bash syntax and use ShellCheck when available; run Python checks where appropriate.

Exercise both engines and cross-engine catalogs for hashes, filtering, recursion/depth, arbitrary filenames, empty folders, symlink modes and cycles, optional directory hashes, metadata-only changes, quick/strict behavior, added/missing/type changes, output exclusion and collisions, malformed catalogs, and scan failures. Compare results across job counts. Exercise BLAKE3 when installed and test its missing-dependency error otherwise. Use realistic multi-file smoke measurements to check parallelism without imposing machine-specific speed thresholds.

## Scope exclusions

No synchronization, deletion, copying, restoration, remote transport, watch daemon, permissions/ownership comparison, hard-link topology comparison, or rename inference. Do not initialize Git unless requested. If version control is enabled later, include a brief change summary on every commit as instructed by the user.
