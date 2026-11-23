#!/usr/bin/env bash
# Informational Linux/WSL smoke run. Run with bash tests/smoke_performance.sh.
set -euo pipefail
export LC_ALL=C

project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
gnu_time=${GNU_TIME:-$(type -P time || true)}
if [[ -z $gnu_time || $("$gnu_time" --version 2>/dev/null) != *GNU* ]]; then
    printf 'GNU time is required. Add it to PATH or set GNU_TIME.\n' >&2
    exit 2
fi

work=$(mktemp -d)
tree=$work/tree
output_dir=$work/output
sample_done=$work/sample-done
sampler=

cleanup() {
    if [[ -n $sampler ]]; then
        : > "$sample_done"
        wait "$sampler" || true
    fi
    rm -rf -- "$work"
}
trap cleanup EXIT
trap 'exit 2' HUP INT TERM

mkdir -- "$tree" "$output_dir"
group_count=8
small_per_group=16
large_count=3
large_mib=8
file_count=$((group_count * small_per_group + large_count))
entry_count=$((file_count + group_count + 1))
for ((group=0; group<group_count; group++)); do
    mkdir -- "$tree/group-$group"
    for ((file=0; file<small_per_group; file++)); do
        printf 'group=%d file=%d\n' "$group" "$file" > "$tree/group-$group/file-$file"
    done
done
for ((file=0; file<large_count; file++)); do
    dd if=/dev/zero of="$tree/large-$file" bs=1M count="$large_mib" status=none
done

sample_workspace() {
    local workspace blocks inodes peak_blocks=0 peak_inodes=0
    while [[ ! -e $sample_done ]]; do
        for workspace in "$output_dir"/.sourcediff-*; do
            [[ -d $workspace ]] || continue
            blocks=$(du -sk -- "$workspace" 2>/dev/null) || continue
            inodes=$(du --inodes -s -- "$workspace" 2>/dev/null) || continue
            blocks=${blocks%%$'\t'*}
            inodes=${inodes%%$'\t'*}
            if (( blocks > peak_blocks )); then peak_blocks=$blocks; fi
            if (( inodes > peak_inodes )); then peak_inodes=$inodes; fi
        done
        sleep .05
    done
    printf '%s\t%s\n' "$peak_blocks" "$peak_inodes"
}

measure() {
    local label=$1 engine=$2 jobs=$3 expected=$4 status=0
    local elapsed rss temp_kib temp_inodes
    shift 4
    rm -f -- "$sample_done"
    sample_workspace > "$work/temp-cost" &
    sampler=$!
    "$gnu_time" -q -f $'%e\t%M' -o "$work/time" \
        bash "$project/sourcediff" --engine "$engine" "$@" --jobs "$jobs" \
        > "$work/stdout" || status=$?
    : > "$sample_done"
    wait "$sampler"
    sampler=
    if (( status != expected )); then
        printf '%s %s jobs=%s returned %d, expected %d\n' \
            "$label" "$engine" "$jobs" "$status" "$expected" >&2
        cat -- "$work/stdout" >&2
        exit 1
    fi
    if compgen -G "$output_dir/.sourcediff-*" >/dev/null; then
        printf '%s %s left a staging workspace\n' "$label" "$engine" >&2
        exit 1
    fi
    IFS=$'\t' read -r elapsed rss < "$work/time"
    IFS=$'\t' read -r temp_kib temp_inodes < "$work/temp-cost"
    printf '%-13s %-7s %4s %6s %8s %10s %12s %6s\n' \
        "$label" "$engine" "$jobs" "$elapsed" "$rss" "$temp_kib" "$temp_inodes" "$status"
}

report_rows() {
    awk -F '\t' '$1 !~ /^(SOURCEDIFF-REPORT|I|S|O)$/' "$1"
}

printf 'Fixture: %d small files, %d files of %d MiB, %d directories including root\n' \
    "$((group_count * small_per_group))" "$large_count" "$large_mib" "$((group_count + 1))"
printf 'Temporary allocation and inode counts are sampled; RSS is GNU time maximum resident size.\n'
printf '%-13s %-7s %4s %6s %8s %10s %12s %6s\n' \
    operation engine jobs sec RSS_KiB temp_KiB temp_inodes status

for engine in python bash; do
    for jobs in 1 4; do
        catalog=$output_dir/$engine-catalog-$jobs
        measure catalog "$engine" "$jobs" 0 catalog "$tree" --directory-hashes --output "$catalog"
        awk -F '\t' '$1 ~ /^(E|X|END)$/' "$catalog" > "$work/$engine-catalog-$jobs.rows"
        grep -Fxq "$(printf 'END\tcomplete\t%d\t0' "$entry_count")" "$work/$engine-catalog-$jobs.rows"
        cmp -- "$work/python-catalog-1.rows" "$work/$engine-catalog-$jobs.rows"
    done
done

baseline=$output_dir/python-catalog-1
printf 'changed payload with a different length\n' > "$tree/group-0/file-0"
rm -- "$tree/group-1/file-0"
printf 'added\n' > "$tree/added"
for engine in python bash; do
    for jobs in 1 4; do
        report=$output_dir/$engine-compare-$jobs
        measure compare "$engine" "$jobs" 1 compare "$tree" "$baseline" --output "$report"
        report_rows "$report" > "$work/$engine-compare-$jobs.rows"
        cmp -- "$work/python-compare-1.rows" "$work/$engine-compare-$jobs.rows"
        grep -Fxq "$(printf 'T\tfiles-hashed\t%d' "$file_count")" "$report"
        grep -Fxq $'T\tunknown\t0' "$report"
        grep -Fxq $'T\terrors\t0' "$report"
    done
    report=$output_dir/$engine-quick
    measure compare-quick "$engine" 4 1 compare "$tree" "$baseline" --quick --output "$report"
    report_rows "$report" > "$work/$engine-quick.rows"
    grep -Fxq "$(printf 'T\tfiles-reused\t%d' "$((file_count - 2))")" "$report"
    grep -Fxq $'T\tfiles-hashed\t2' "$report"
    cmp -- "$work/python-quick.rows" "$work/$engine-quick.rows"
done

reference=$work/python-compare-1.rows
grep -Fq $'ADDED\tadded\tcounted\t' "$reference"
grep -Fq $'MISSING\tgroup-1/file-0\tcounted\t' "$reference"
grep -Fq $'CONTENT_CHANGED\tgroup-0/file-0\tcounted\t' "$reference"
grep -Fxq $'END\tcomplete\t1' "$reference"
printf 'Matched %d catalog entries and all full comparison rows across engines and job counts.\n' "$entry_count"
printf 'Quick comparisons matched across engines and reused %d file digests per run.\n' "$((file_count - 2))"
