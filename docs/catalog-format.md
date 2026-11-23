# SourceDiff catalog format v1

A catalog is an ASCII byte stream. TAB separates fields and LF ends every row, including the trailer. There is no byte-order mark, padding, quoting, or comment syntax. Read catalogs as data.

```text
SOURCEDIFF<TAB>1
S<TAB>key<TAB>escaped-value
E<TAB>path<TAB>kind<TAB>digest<TAB>size<TAB>mtime_ns<TAB>precision_ns<TAB>target<TAB>verification
X<TAB>path<TAB>operation<TAB>message<TAB>scope
END<TAB>status<TAB>entry-count<TAB>error-count
```

The physical order is one version row, all settings, all entries, all errors, and one trailer. Entries and errors may be absent for an incomplete scan. Any payload after the trailer, including a blank line, is invalid.

## Escaping and ordering

In fields designated as escaped, keep bytes 0x20 through 0x7E literal except for percent (0x25). Represent all other bytes as a percent followed by two uppercase hexadecimal digits. For example, the bytes `sub/a` followed by percent, TAB, LF, CR, and 0xFF serialize as `sub/a%25%09%0A%0D%FF`. Escaping a byte that has a literal representation, writing lowercase hexadecimal escapes, and using malformed escapes are invalid. Catalog fields cannot contain a decoded NUL. Quotes, spaces, dollar signs, and shell-looking strings remain literal data.

Entries are strictly ascending and unique by decoded path bytes, using unsigned lexicographic order. The percent-escaped spelling does not preserve that order. An external sort may use a fixed two-digit hexadecimal representation of each decoded path byte as a temporary key, sorted in the C locale. A short prefix orders before its extensions. For example, the raw paths `!`, `.`, `a`, and the single byte 0xFF appear in that order even though 0xFF serializes as `%FF`.

All paths are escaped, relative to the scanned root, and use slash separators. The special path `.` names the root. Other paths cannot begin or end with a slash or contain empty, `.`, or `..` components. A complete catalog contains exactly one root entry of kind `directory`. The root can appear after other entries, as determined by byte ordering. An incomplete catalog can omit an unobserved root. Symlink destination text and setting values are independent of catalog path syntax.

## Settings rows

Each scalar key below is required exactly once. Readers accept any order among settings rows. Writers emit scalar keys in the order shown, then list values grouped by the order of their keys below. Settings keys are literal ASCII. Values are escaped.

| Key | Values |
| --- | --- |
| `hash` | `sha256` or `blake3` |
| `recursive` | `true` or `false` |
| `max-depth` | Nonnegative integer, or `-` for no explicit limit. The root has depth 0. |
| `symlinks` | `record`, `follow`, or `skip` |
| `directory-hashes` | `true` or `false` |
| `engine` | `python` or `bash` |
| `engine-version` | Nonempty informational runtime version |
| `tool-version` | Nonempty informational SourceDiff version |
| `complete` | `complete` or `incomplete`, matching the trailer |

The optional keys `exclude`, `exclude-dir`, and `exclude-ext` each use one row per nonempty value. Preserve the order of a given list's rows, including repeated values. Omit a list's rows when it is empty. Unknown keys and duplicate scalars are invalid. Runtime or comparison settings such as job counts, quick mode, and output paths are outside this header.

## Entry and error rows

| Entry field | Encoding and meaning |
| --- | --- |
| `path` | Escaped root-relative path |
| `kind` | `file`, `directory`, or `symlink` |
| `digest` | Exactly 64 lowercase hexadecimal digits for either algorithm, or `-` for an absent digest |
| `size` | Nonnegative byte count, or `-` when absent. Directory size is informational. |
| `mtime_ns` | Signed nanoseconds since the Unix epoch, or `-` when absent |
| `precision_ns` | Positive integer timestamp precision in nanoseconds, or `-` when absent. Requires an mtime when present. |
| `target` | Nonempty escaped destination bytes for a symlink. For all other kinds, a literal `-` means absent. A symlink can have the literal destination `-`. |
| `verification` | `verified` for a verified digest, `reused` for a file hash reused by a quick scan, or `unverified` for an absent or unverified digest |

Integer fields use canonical decimal spelling. Zero is `0`, positive values have no sign or leading zero, and negative values have one minus and no leading zero. The supported range is `0` through `9223372036854775807` for nonnegative fields and `-9223372036854775808` through `9223372036854775807` for mtime. Precision has a minimum of `1` when present.

A digest is required for every file in a complete catalog. When directory hashing is enabled it is also required for every directory in a complete catalog. When directory hashing is disabled, a directory digest is always absent. An absent digest requires `unverified`. Symlink rows are valid only under the `record` policy and always include a digest and destination. `reused` is reserved for regular files with a digest. Quick scan spools can retain unverified directory digests when verification depends on reused file hashes.

An error row has the escaped path, a nonempty escaped operation label, an escaped message (possibly empty), and the literal scope `entry` or `subtree`. The scope identifies whether the error affects only that entry or its descendants as well. Error rows may repeat paths. Their order is left to the producer, which should emit a deterministic order. A complete catalog cannot contain error rows.

The trailer status is `complete` or `incomplete` and both counts are nonnegative canonical integers. The counts must match the entry and error rows. An incomplete catalog can be parsed for diagnostics, but a comparison baseline must be complete.

## Hash bytes and directory frames

Hash regular file bytes directly. A recorded link's hash is taken over its raw destination bytes, including any trailing newlines. For a directory, visit included direct children in ascending decoded basename byte order. Append the following bytes for each child in order, with no other separators or delimiters.

```text
ASCII kind | NUL | ASCII escaped basename | NUL | ASCII lowercase digest | NUL
```

The kind and digest use the same spellings as the entry row. The basename is one decoded path component before escaping. Hash the concatenation using the selected catalog algorithm. An empty directory hashes the empty byte stream. A parent uses a child's subtree digest when the child is a directory. Excluded entries and metadata never contribute to the frames. If an included descendant failed to scan, omit the affected directory digests. Digests using quick-reused file hashes cannot be claimed fully verified.

## Python spool interface

- `encode_bytes(bytes) -> str` and `decode_bytes(str) -> bytes` implement the canonical codec. The standalone Python codec round-trips all 256 byte values. Catalog decoding rejects NUL because Linux paths and the Bash engine cannot represent it.
- `read_catalog(path, workspace=None, *, require_complete=False)` returns `(settings, entry_spool, error_spool, complete)`. Set `require_complete=True` for a comparison baseline. Format violations raise `FormatError`, a subclass of `ValueError`. I/O errors use ordinary Python I/O exceptions.
- `read_catalog_stream(source, workspace=None, *, require_complete=False)` uses the same parser and return values. It reads an already-open binary stream from its current position and leaves it open. This lets comparison validate and protect the identity of the same open file.
- Entry and error spools are `Path` objects referring to files containing only canonical binary E or X rows, respectively. Entry spools are sorted by decoded path. Both files are created even when empty. The reader removes temporary files on failed validation. On success, the caller owns the paths and should remove them or clean up the containing workspace. If no workspace is given, the platform temporary directory is used.
- Settings use strings or ordered lists of strings, with UTF-8 plus `surrogateescape` preserving raw bytes. The reader returns all three list keys, using empty lists where appropriate.
- `iter_entries(path)` yields dictionaries with `path: bytes`, `kind: str`, `digest: str` (including `-`), `size: int | None`, `mtime_ns: int | None`, `precision_ns: int | None`, `link_target: bytes | None`, and `verification: str`. `iter_errors(path)` yields dictionaries with `path: bytes`, `operation: str` (UTF-8 with `surrogateescape`), `message: bytes`, and `scope: str`.
- `encode_entry(entry)` and `encode_error(error)` produce LF-terminated binary spool rows for those dictionaries. `directory_child_frame(kind, basename, digest)` returns the exact bytes for one child frame.
- `write_catalog(staged_path, settings, entry_spool, error_spool, complete)` streams validated spools to a distinct staging file. It sets the `complete` header from the boolean and emits the trailer after all rows have validated. The caller owns staging cleanup and publication.
