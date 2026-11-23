# SourceDiff comparison report v1

Reports are ASCII with TAB-separated fields and LF-terminated rows. Escaped fields use the canonical byte codec and relative path rules from [catalog format v1](catalog-format.md). Display the data as text or parse it as records.

```text
SOURCEDIFF-REPORT<TAB>1
I<TAB>key<TAB>value...
S<TAB>key<TAB>escaped-value
O<TAB>key[<TAB>escaped-value]
category<TAB>path<TAB>detail<TAB>old-values<TAB>new-values
ERROR<TAB>path<TAB>operation<TAB>message<TAB>scope
T<TAB>key<TAB>count
END<TAB>completeness<TAB>exit-status
```

The default report orders the version, all `I`, `S` and `O` rows, all path comparison rows, all errors, every total and the trailer. Comparison and error rows can be absent. The trailer is the final record.

## Differences-only report v1

With `--differences-only`, the report starts with `SOURCEDIFF-DIFFERENCES<TAB>1`. It omits the `I`, `S` and `O` rows and informational `METADATA_CHANGED` rows. It includes both `ADDED` details, `MISSING`, `CONTENT_CHANGED`, `TYPE_CHANGED`, strict-counted `METADATA_CHANGED`, and all `UNKNOWN` and `ERROR` rows. Row encoding, old and new value groups, and decoded path order are shared with the default report.

Every `T` row and the `END` trailer follow in the same order and carry the full comparison's counts, completeness and status. The `metadata-changed` total includes informational rows omitted from the compact report. A complete comparison with zero displayed changes contains only the version marker, totals and trailer. The flag is local to the current invocation. Operational `CHANGE` and `TOTALS` events reflect the full comparison in both modes.

## Invocation and scope

Write `I` keys in this order. Escape values as catalog byte fields.

| Key | Values |
| --- | --- |
| `source-catalog` | Absolute path of the supplied input catalog |
| `target` | Absolute path of the supplied target root |
| `strict` | `true` or `false` |
| `quick` | `true` or `false` |
| `fail-fast` | `true` or `false` |
| `baseline-complete` | `complete`, required for comparison |
| `baseline-coverage` | `within-saved-scope` or `potentially-broader` |
| `directory-hash-scope` | `compared`, `disabled` or `scope-changed` |
| `symlink-policy` | Two fields with the saved policy followed by the effective policy |

Use the catalog settings order and encoding for the effective `S` rows, including the comparison engine and runtime version. The `complete` row describes comparison completeness and agrees with the trailer. The number of worker jobs is independent of the comparison settings and omitted from the artifact.

Emit `O` rows for explicitly supplied overrides in settings-key order, even when they repeat a saved value. A scalar uses one value row. A nonempty list uses one row per value, preserving order and repeats. An explicitly cleared list uses a single two-field row with no value field. The inherited settings row is absent for that empty list.

Depth and filters apply to both sides. `potentially-broader` means the effective depth can exceed the saved depth, a saved exclusion is absent from its effective category, or the symlink policy can expand coverage (`skip` to `record` or `follow`, or `record` to `follow`). Compare glob exclusions as sets of exact raw patterns and extension exclusions after normalizing one optional leading dot. This conservative check uses exact pattern membership.

Follow catalogs do not preserve symbolic-link origins. When policies differ, linked paths may change type or coverage because of traversal policy alone. A currently observed skipped symlink excludes its old followed path and descendants. Changing to `record` excludes old followed descendants as soon as the current entry is identified as a symlink. An unsuccessful destination read leaves an `ERROR` and the matching prior entry becomes `UNKNOWN`. When a recorded link is followed and its resolved file or directory kind is excluded by an effective filter, exclude the old recorded link as well. A failed resolution remains a scan error.

Use `compared` when directory hashing is effective, effective settings do not potentially widen containment, and the saved and effective symlink policies match. If projection removes old entries, rebuild the baseline directory digests from its remaining entries using the catalog algorithm and child frames. This also supports enabling effective directory hashes against a complete catalog saved without them. Use `scope-changed` when directory hashing is enabled and a broader scope or differing symlink policy can change directory coverage. Omit directory content classifications in that case and continue comparing descendants. Use `disabled` when effective directory hashing is off. Current input and output path exclusions also apply to the old entry projection.

Resolve artifact parent paths according to filesystem semantics and preserve the final name. Exclude the input catalog's resolved location as well when the supplied name is a symlink. A `..` after a symlink is evaluated relative to the symlink target during resolution.

## Comparison and error records

Write changes in ascending decoded path byte order. Per path, write a content or unknown row before any metadata row. A type change is one row with both entry values. Each old or new value group is seven fields in catalog order.

```text
kind<TAB>digest<TAB>size<TAB>mtime_ns<TAB>precision_ns<TAB>target<TAB>verification
```

Use a literal `-` in all seven fields when the corresponding entry is absent. Encode a present entry exactly as in the catalog, omitting its initial `E` and path fields. When baseline directory hashes were recomputed after projection, the old values show the resulting effective hashes.

| Category | Detail | Interpretation |
| --- | --- | --- |
| `ADDED` | `counted` or `outside-baseline-scope` | Target-only entry in effective scope. The latter detail also indicates that the original catalog did not cover the entry under its saved depth, filters, or identifiable link scope. |
| `MISSING` | `counted` | Absent target entry in effective scope where the scan could establish absence. |
| `CONTENT_CHANGED` | `counted` | Digests differ, or recorded symlink destination bytes differ. |
| `TYPE_CHANGED` | `counted` | Entry kinds differ. |
| `METADATA_CHANGED` | `counted` or `informational` | At least one known size or mtime value differs. Corresponding values must be present on both sides. Precision is carried in the entries. Only strict comparisons of nondirectories use `counted`. |
| `UNKNOWN` | `target-scan-error` | A missing target row is covered by an exact entry error or an equal/ancestor subtree error, or an otherwise comparable directory digest is unavailable because the scan is incomplete. |

Both `ADDED` details count. Content, type, and metadata changes are classified per path. Matching directory digests never bypass the comparison of descendants. A root subtree error from fail-fast protects all unobserved paths. Paths with valid target observations remain comparable where their available values permit it.

For each scan error, use the catalog's X field order and encodings with `ERROR` as the row name. Order by decoded path bytes, then bytewise by the encoded error row fields. Keep repeated errors.

Quick reuse requires a regular file at the same projected path, a known matching byte size and mtime, and equal supported timestamp precision. Engines reporting nanoseconds use precision `1`. Unknown or differing precision forces a fresh hash. Record reused target files as `reused`. A target directory digest containing a reused file hash is `unverified`.

## Totals and trailer

Use nonnegative canonical decimal counts from the full comparison and emit all of these totals in order, including zeros.

| Key | Counts |
| --- | --- |
| `added` | `ADDED` rows |
| `missing` | `MISSING` rows |
| `content-changed` | `CONTENT_CHANGED` rows |
| `type-changed` | `TYPE_CHANGED` rows |
| `metadata-changed` | All `METADATA_CHANGED` rows |
| `unknown` | `UNKNOWN` rows |
| `errors` | `ERROR` rows |
| `counted` | Rows with detail `counted` or `outside-baseline-scope` |
| `files-hashed` | Target file rows with verified digests |
| `files-reused` | Target file rows with reused digests |
| `files-unverified` | Target file rows without a verified digest, including reused digests |
| `directory-digests-verified` | Target directory rows with present, verified digests |
| `directory-digests-unverified` | Target directory rows with present, unverified digests |

The trailer is `END<TAB>complete<TAB>0` for a complete report with no counted discrepancies and `END<TAB>complete<TAB>1` for a complete report with counted discrepancies. Use `END<TAB>incomplete<TAB>2` for an incomplete scan or comparison, even when it has counted discrepancies. Invalid catalogs and invocation or publication failures return exit 2 without publishing a report for that invocation.

## Publication

`--overwrite` requires a stable destination namespace through final publication, including the destination's parent path and directory entry. The publisher checks the destination against the identity of the open baseline immediately before atomic replacement. An external process can rebind the destination between that identity check and replacement. Coordinate concurrent changes to the destination namespace when using `--overwrite`. Default publication uses an exclusive link to prevent clobbering an existing destination.
