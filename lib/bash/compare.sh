# shellcheck shell=bash
# Invocation options and scan status are dynamically scoped by main.sh and scan.sh.
# shellcheck disable=SC2154

SD_REPORT_TOTALS=(added missing content-changed type-changed metadata-changed unknown errors counted
                  files-hashed files-reused files-unverified directory-digests-verified directory-digests-unverified)
SD_ABSENT_VALUES=$'-\t-\t-\t-\t-\t-\t-'

sd_comparison_depth() {
    local -n _depth_settings=$1
    local _compare_limit=${_depth_settings[max-depth]}
    if [[ ${_depth_settings[recursive]} == false ]] &&
        { [[ $_compare_limit == - ]] || (( _compare_limit > 1 )); }; then
        _compare_limit=1
    fi
    printf -v "$2" '%s' "$_compare_limit"
}

sd_normalized_extensions() {
    local item _normalized_ext=''
    while IFS= read -r item && [[ -n $item ]]; do
        _normalized_ext+=${item#.}$SD_LF
    done <<< "$1"
    printf -v "$2" '%s' "$_normalized_ext"
}

sd_compare_scope_can_widen() {
    local -n _old_scope=$1 _new_scope=$2
    local old_depth new_depth key item old_values new_values
    sd_comparison_depth "$1" old_depth
    sd_comparison_depth "$2" new_depth
    if [[ $old_depth != - ]] && { [[ $new_depth == - ]] || (( new_depth > old_depth )); }; then
        return 0
    fi
    case ${_old_scope[symlinks]}:${_new_scope[symlinks]} in
        skip:record|skip:follow|record:follow) return 0 ;;
    esac
    for key in "${SD_FILTER_KEYS[@]}"; do
        old_values=${_old_scope[$key]} new_values=${_new_scope[$key]}
        if [[ $key == exclude-ext ]]; then
            sd_normalized_extensions "$old_values" old_values
            sd_normalized_extensions "$new_values" new_values
        fi
        while IFS= read -r item; do
            [[ $SD_LF$new_values == *"$SD_LF$item$SD_LF"* ]] || return 0
        done < <(printf '%s' "$old_values")
    done
    return 1
}

# Hex keys are reversible, contain no slashes and stay case-distinct on mounts
# with case-insensitive names. Chunk long keys through directory descriptors so
# neither the index filename nor the syscall path can exceed Linux limits.
# Each indexed fact consumes one file; branch directories are shared prefixes.
sd_compare_index() {
    local action=$1 key=$2 lane=$3 payload=${4:-}
    local anchor=$sd_compare_index_dir fd='' next_fd branch record result=0 record_value
    while (( ${#key} > 240 )); do
        branch=$anchor/d${key:0:240}
        if [[ ! -d $branch ]]; then
            if [[ $action != put ]]; then result=1; break; fi
            mkdir -- "$branch" || { result=2; break; }
        fi
        exec {next_fd}<"$branch" || { result=2; break; }
        if [[ -n $fd ]]; then exec {fd}<&-; fi
        fd=$next_fd
        anchor=/proc/$BASHPID/fd/$fd
        key=${key:240}
    done
    if (( result == 0 )); then
        record=$anchor/$lane-$key
        case $action in
            put) printf '%s\n' "$payload" >"$record" || result=2 ;;
            test) [[ -f $record ]] || result=1 ;;
            get)
                if [[ ! -f $record ]]; then
                    result=1
                elif IFS= read -r record_value <"$record"; then
                    printf -v "$payload" '%s' "$record_value"
                else
                    result=2
                fi
                ;;
        esac
    fi
    if [[ -n $fd ]]; then exec {fd}<&-; fi
    return "$result"
}

# Test the exact path in one lane and its proper ancestors in another. The
# root's special name has no slash-prefixed descendants in the ordering key.
sd_compare_index_covers() {
    local path=$1 exact_lane=$2 ancestor_lane=$3 key result
    sd_hex_key "$path" key
    if [[ -n $exact_lane ]]; then
        sd_compare_index test "$key" "$exact_lane"
        result=$?
        (( result == 1 )) || return "$result"
    fi
    [[ -n $ancestor_lane ]] || return 1
    while [[ $path != . ]]; do
        if [[ $path == */* ]]; then
            path=${path%/*}
            key=${key:0:2*${#path}}
        else
            path=. key=2E
        fi
        sd_compare_index test "$key" "$ancestor_lane"
        result=$?
        (( result == 1 )) || return "$result"
    done
    return 1
}

sd_compare_reuse() {
    local key cached cached_size cached_mtime cached_precision cached_digest result
    [[ $2 != - && $3 != - && $4 == 1 ]] || return 1
    sd_hex_key "$1" key
    sd_compare_index get "$key" file cached
    result=$?
    if (( result == 2 )); then sd_compare_index_failed=true; fi
    (( result == 0 )) || return 1
    IFS=$SD_TAB read -r cached_size cached_mtime cached_precision cached_digest <<< "$cached"
    [[ $2 == "$cached_size" && $3 == "$cached_mtime" && $4 == "$cached_precision" ]] || return 1
    printf -v "$5" '%s' "$cached_digest"
}

sd_compare_observe_scope() {
    local reason=$2 lane key
    case $reason:$sd_saved_symlinks in
        implicit-exclusion:*|skipped-link:follow) lane=omit ;;
        recorded-link:follow) lane=omit-children ;;
        filtered-link:record) lane=omit-recorded ;;
        followed-link:skip) lane=covered-link ;;
        *) return 0 ;;
    esac
    sd_hex_key "$1" key
    sd_compare_index put "$key" "$lane" || return 2
    if [[ $lane != covered-link ]]; then sd_compare_scope_events=true; fi
}

sd_compare_artifact_excluded() {
    local _artifact_path
    for _artifact_path in "${sd_compare_artifacts[@]}"; do
        if [[ $1 == "$_artifact_path" || $1 == "$_artifact_path/"* ]]; then return 0; fi
    done
    return 1
}

sd_compare_prepare_baseline() {
    local input=$1 output=$2 row path key
    local sd_depth sd_symlinks=${settings[symlinks]}
    local -a fields sd_filter_categories=() sd_filter_regexes=() sd_filter_basename=() sd_extensions=()
    sd_prepare_scope settings || return 2
    while IFS= read -r row; do
        sd_split_fields "$row" fields
        sd_decode "${fields[1]}" path || return 2
        if ! sd_scope_contains "$path" "${fields[2]}" || sd_compare_artifact_excluded "$path"; then
            sd_compare_projected=true
            continue
        fi
        if [[ $quick == true && ${fields[2]} == file && ${fields[4]} != - && ${fields[6]} == 1 ]]; then
            sd_hex_key "$path" key
            sd_compare_index put "$key" file \
                "${fields[4]}$SD_TAB${fields[5]}$SD_TAB${fields[6]}$SD_TAB${fields[3]}" || return 2
        fi
        printf '%s\n' "$row" || return 2
    done <"$input" >"$output"
}

sd_compare_observation_excludes() {
    local path=$1 kind=$2 result
    sd_compare_index_covers "$path" omit omit
    result=$?
    (( result == 1 )) || return "$result"
    if [[ $sd_saved_symlinks == follow && ${settings[symlinks]} == record ]]; then
        sd_compare_index_covers "$path" '' omit-children
        return
    fi
    if [[ $sd_saved_symlinks == record && $kind == symlink ]]; then
        sd_compare_index_covers "$path" omit-recorded ''
        return
    fi
    return 1
}

sd_compare_observed_baseline() {
    local row path key result
    local -a fields
    while IFS= read -r row; do
        sd_split_fields "$row" fields
        sd_decode "${fields[1]}" path || return 2
        if [[ $sd_compare_scope_events == true ]]; then
            sd_compare_observation_excludes "$path" "${fields[2]}"
            result=$?
            if (( result == 0 )); then
                sd_compare_projected=true
                continue
            fi
            (( result == 1 )) || return 2
        fi
        if [[ ${settings[symlinks]} == follow && $sd_saved_symlinks == record && ${fields[2]} == symlink ]]; then
            sd_hex_key "$path" key
            sd_compare_index put "$key" covered-link || return 2
        fi
        printf '%s\n' "$row" || return 2
    done <"$1" >"$2"
}

sd_compare_hash_baseline() {
    local input=$1 output=$2 sd_scan_dir=$sd_workspace/projected-digests
    # sd_aggregate and sd_hash_stream use the scanner's dynamically scoped settings.
    # shellcheck disable=SC2034
    local sd_directory_hashes=true sd_hasher=sha256sum row body path tree order
    if [[ ${settings[hash]} == blake3 ]]; then
        # shellcheck disable=SC2034
        sd_hasher=b3sum
    fi
    mkdir -- "$sd_scan_dir" || return 2
    while IFS= read -r row; do
        body=${row#*"$SD_TAB"}
        sd_decode "${body%%"$SD_TAB"*}" path || return 2
        sd_tree_key "$path" tree
        sd_hex_key "$path" order
        printf '%s\t1\t%s\t%s\n' "$tree" "$order" "$row" || return 2
    done <"$input" >"$sd_scan_dir/records"
    sort -S 32M -T "$sd_scan_dir" -t "$SD_TAB" -k1,1 -k2,2 -- "$sd_scan_dir/records" \
        >"$sd_scan_dir/tree-sorted" || return 2
    sd_aggregate "$sd_scan_dir/tree-sorted" || return 2
    [[ ! -s $sd_scan_dir/keyed-errors ]] || { sd_error 'cannot hash projected baseline directories'; return; }
    sd_scan_sorted_spool "$sd_scan_dir/keyed-entries" "$output"
}

sd_compare_index_errors() {
    local row path key
    local -a fields
    while IFS= read -r row; do
        sd_split_fields "$row" fields
        sd_decode "${fields[1]}" path || return 2
        sd_hex_key "$path" key
        sd_compare_index put "$key" "error-${fields[4]}" || return 2
        ((sd_report_totals[errors]+=1))
    done <"$1"
}

sd_compare_outside_saved() {
    if ! sd_scope_contains "$1" "$2"; then return 0; fi
    if [[ ${settings[symlinks]} == follow && $sd_saved_symlinks != follow ]]; then
        sd_compare_index_covers "$1" covered-link covered-link
        return
    fi
    return 1
}

sd_compare_verification_totals() {
    case $1:$2:$3 in
        file:*:verified) ((sd_report_totals[files-hashed]+=1)) ;;
        file:*:reused)
            ((sd_report_totals[files-reused]+=1))
            ((sd_report_totals[files-unverified]+=1))
            ;;
        file:*) ((sd_report_totals[files-unverified]+=1)) ;;
        directory:-:*) ;;
        directory:*:verified) ((sd_report_totals[directory-digests-verified]+=1)) ;;
        directory:*) ((sd_report_totals[directory-digests-unverified]+=1)) ;;
    esac
}

sd_compare_change() {
    local category=$1 detail=$2 old_row=$3 new_row=$4
    local key=${category,,} body wire old_values=$SD_ABSENT_VALUES new_values=$SD_ABSENT_VALUES
    if [[ -n $old_row ]]; then
        body=${old_row#*"$SD_TAB"}
        wire=${body%%"$SD_TAB"*}
        old_values=${body#*"$SD_TAB"}
    fi
    if [[ -n $new_row ]]; then
        body=${new_row#*"$SD_TAB"}
        wire=${body%%"$SD_TAB"*}
        new_values=${body#*"$SD_TAB"}
    fi
    key=${key//_/-}
    ((sd_report_totals[$key]+=1))
    case $detail in counted|outside-baseline-scope) ((sd_report_totals[counted]+=1)) ;; esac
    local log_path
    sd_decode "$wire" log_path || return 2
    sd_log_event CHANGE "path=$log_path" "category=$category" "detail=$detail" || return 2
    printf '%s\t%s\t%s\t%s\t%s\n' "$category" "$wire" "$detail" "$old_values" "$new_values"
}

sd_compare_matching() {
    local old_row=$1 new_row=$2 detail=informational
    local -n _left=$3 _right=$4
    local kind=${_right[2]}
    if [[ ${_left[2]} != "$kind" ]]; then
        sd_compare_change TYPE_CHANGED counted "$old_row" "$new_row"
        return
    fi
    if [[ $kind == symlink ]]; then
        if [[ ${_left[7]} != "${_right[7]}" ]]; then
            sd_compare_change CONTENT_CHANGED counted "$old_row" "$new_row" || return 2
        fi
    elif [[ $kind == file || $sd_directory_hash_scope == compared ]]; then
        if [[ ${_left[3]} == - || ${_right[3]} == - ]]; then
            sd_compare_change UNKNOWN target-scan-error "$old_row" "$new_row" || return 2
        elif [[ ${_left[3]} != "${_right[3]}" ]]; then
            sd_compare_change CONTENT_CHANGED counted "$old_row" "$new_row" || return 2
        fi
    fi
    if [[ ( ${_left[4]} != - && ${_right[4]} != - && ${_left[4]} != "${_right[4]}" ) ||
          ( ${_left[5]} != - && ${_right[5]} != - && ${_left[5]} != "${_right[5]}" ) ]]; then
        if [[ $strict == true && $kind != directory ]]; then detail=counted; fi
        sd_compare_change METADATA_CHANGED "$detail" "$old_row" "$new_row" || return 2
    fi
}

sd_compare_read_row() {
    local -n _merge_row=$2 _merge_path=$3
    local body
    if ! IFS= read -r _merge_row <&"$1"; then
        [[ -z $_merge_row ]] || return 2
        _merge_path=''
        return 0
    fi
    body=${_merge_row#*"$SD_TAB"}
    sd_decode "${body%%"$SD_TAB"*}" "$3"
}

sd_compare_missing() {
    local path=$1 row=$2 lane result
    if (( sd_report_totals[errors] )); then
        for lane in error-entry error-subtree; do
            if [[ $lane == error-entry ]]; then
                sd_compare_index_covers "$path" "$lane" ''
            else
                sd_compare_index_covers "$path" "$lane" "$lane"
            fi
            result=$?
            if (( result == 0 )); then
                sd_compare_change UNKNOWN target-scan-error "$row" ''
                return
            fi
            (( result == 1 )) || return 2
        done
    fi
    sd_compare_change MISSING counted "$row" ''
}

# BASELINE_ENTRIES TARGET_ENTRIES TARGET_ERRORS CHANGES
# Hold one row per reader during the byte-ordered merge.
sd_compare() {
    local old_row new_row old_path new_path detail result
    # Scope helpers and the matching comparator use these variables by name.
    # shellcheck disable=SC2034
    local sd_depth sd_symlinks=$sd_saved_symlinks
    # shellcheck disable=SC2034
    local -a old_fields new_fields sd_filter_categories=() sd_filter_regexes=() sd_filter_basename=() sd_extensions=()
    sd_compare_index_errors "$3" || return 2
    sd_prepare_scope saved || return 2
    sd_compare_read_row 3 old_row old_path && sd_compare_read_row 4 new_row new_path || return 2
    while [[ -n $old_row || -n $new_row ]]; do
        sd_log_tick || return 2
        if [[ -z $new_row || ( -n $old_row && $old_path < $new_path ) ]]; then
            sd_compare_missing "$old_path" "$old_row" && sd_compare_read_row 3 old_row old_path || return 2
            continue
        fi
        sd_split_fields "$new_row" new_fields
        sd_compare_verification_totals "${new_fields[2]}" "${new_fields[3]}" "${new_fields[8]}"
        if [[ -z $old_row || $new_path < $old_path ]]; then
            detail=counted
            sd_compare_outside_saved "$new_path" "${new_fields[2]}"
            result=$?
            case $result in
                0) detail=outside-baseline-scope ;;
                1) ;;
                *) return 2 ;;
            esac
            sd_compare_change ADDED "$detail" '' "$new_row" || return 2
        else
            sd_split_fields "$old_row" old_fields
            sd_compare_matching "$old_row" "$new_row" old_fields new_fields &&
                sd_compare_read_row 3 old_row old_path || return 2
        fi
        sd_compare_read_row 4 new_row new_path || return 2
    done
} 3<"$1" 4<"$2" >"$4"

sd_write_report_overrides() {
    local key value
    for key in "${SD_SCALAR_KEYS[@]}"; do
        [[ -n ${overrides[$key]+set} ]] || continue
        sd_encode "${overrides[$key]}" value
        printf 'O\t%s\t%s\n' "$key" "$value" || return 2
    done
    for key in "${SD_FILTER_KEYS[@]}"; do
        [[ -n ${overrides[$key]+set} ]] || continue
        if [[ -z ${overrides[$key]} ]]; then
            printf 'O\t%s\n' "$key" || return 2
        else
            while IFS= read -r value && [[ -n $value ]]; do
                printf 'O\t%s\t%s\n' "$key" "$value" || return 2
            done <<< "${overrides[$key]}"
        fi
    done
}

# Normalize lexical "." and slash spellings for the invocation fields. Keep
# ".." components because the filesystem resolves them after symlink targets.
sd_report_absolute_path() {
    local rest=${1#/} prefix=/ component _report_path
    if [[ $1 == //* && $1 != ///* ]]; then prefix=//; rest=${rest#/}; fi
    _report_path=$prefix
    while [[ -n $rest ]]; do
        component=${rest%%/*}
        if [[ $rest == */* ]]; then rest=${rest#*/}; else rest=''; fi
        [[ -n $component && $component != . ]] || continue
        if [[ $_report_path != "$prefix" ]]; then _report_path+=/; fi
        _report_path+=$component
    done
    sd_encode "$_report_path" "$2"
}

sd_write_comparison_report() {
    local staged=$1 changes=$2 errors=$3 report_status=$4 completeness=complete source_field target_field key row fields
    if (( report_status == 2 )); then completeness=incomplete; fi
    {
        if [[ $differences_only == true ]]; then
            printf 'SOURCEDIFF-DIFFERENCES\t1\n' || return 2
            while IFS= read -r row; do
                if [[ $row == "METADATA_CHANGED$SD_TAB"* ]]; then
                    fields=${row#*"$SD_TAB"}
                    fields=${fields#*"$SD_TAB"}
                    [[ $fields != "informational$SD_TAB"* ]] || continue
                fi
                printf '%s\n' "$row" || return 2
            done <"$changes" || return 2
        else
            sd_report_absolute_path "$baseline_path" source_field
            sd_report_absolute_path "$target" target_field
            printf 'SOURCEDIFF-REPORT\t1\nI\tsource-catalog\t%s\nI\ttarget\t%s\n' "$source_field" "$target_field" &&
                printf 'I\tstrict\t%s\nI\tquick\t%s\nI\tfail-fast\t%s\n' "$strict" "$quick" "$fail_fast" &&
                printf 'I\tbaseline-complete\tcomplete\nI\tbaseline-coverage\t%s\nI\tdirectory-hash-scope\t%s\n' \
                    "$sd_baseline_coverage" "$sd_directory_hash_scope" &&
                printf 'I\tsymlink-policy\t%s\t%s\n' "$sd_saved_symlinks" "${settings[symlinks]}" &&
                sd_write_settings settings "$completeness" && sd_write_report_overrides &&
                cat -- "$changes" || return 2
        fi
        while IFS= read -r row; do
            printf 'ERROR%s\n' "${row#X}" || return 2
        done <"$errors"
        for key in "${SD_REPORT_TOTALS[@]}"; do
            printf 'T\t%s\t%s\n' "$key" "${sd_report_totals[$key]}" || return 2
        done
        printf 'END\t%s\t%s\n' "$completeness" "$report_status"
    } >"$staged"
}

sd_compare_from_catalog() {
    local baseline_fd=$1 baseline_descriptor=/proc/$BASHPID/fd/$1
    local identity resolved_baseline root binding path key baseline_rows reuse_fn='' report_status=0
    local sd_compare_index_dir sd_saved_symlinks sd_directory_hash_scope=disabled sd_baseline_coverage=within-saved-scope
    local sd_compare_projected=false sd_compare_scope_events=false sd_compare_index_failed=false
    local -a exclusions sd_compare_artifacts=()
    local -A saved sd_report_totals
    identity=$(sd_identity "$baseline_descriptor") ||
        { sd_error 'cannot identify the open baseline catalog'; return; }
    IFS= read -r -d '' resolved_baseline < <(readlink -z -- "$baseline_descriptor") ||
        { sd_error 'cannot resolve the open baseline catalog'; return; }
    sd_create_workspace "$destination" "$overwrite" "$target" "$baseline_path" || return 2
    sd_log_init "$identity" "$baseline_path" "$resolved_baseline" || return 2
    sd_log_boundary index || return 2
    sd_read_catalog_stream "$baseline_fd" "$sd_workspace/baseline" "$sd_workspace/baseline-errors" saved yes || return 2
    sd_validate_destination "$destination" "$overwrite" "$identity" "$target" "$baseline_path" || return 2
    sd_effective_settings settings overrides saved
    sd_require_hash "${settings[hash]}" || return 2
    if [[ ${settings[hash]} == sha256 ]]; then
        local hash_version
        if ! hash_version=$(sha256sum --version 2>/dev/null) || [[ $hash_version != *GNU* ]]; then
            sd_error 'GNU sha256sum is required'
            return
        fi
    fi
    local hash_program=b3sum hash_probe
    if [[ ${settings[hash]} == sha256 ]]; then hash_program=sha256sum; fi
    if ! "$hash_program" - </dev/null >"$sd_workspace/hash-probe" ||
        ! sd_normalize_hash "$sd_workspace/hash-probe" hash_probe || [[ -z $hash_probe ]]; then
        sd_error "$hash_program must return a ${settings[hash]} digest from stdin"
        return
    fi
    sd_saved_symlinks=${saved[symlinks]}
    if sd_compare_scope_can_widen saved settings; then sd_baseline_coverage=potentially-broader; fi
    if [[ ${settings[directory-hashes]} == true ]]; then
        if [[ $sd_baseline_coverage == potentially-broader || $sd_saved_symlinks != "${settings[symlinks]}" ]]; then
            sd_directory_hash_scope=scope-changed
        else
            sd_directory_hash_scope=compared
        fi
    fi
    exclusions=("$destination" "$baseline_path" "$resolved_baseline")
    [[ -z $sd_log_path ]] || exclusions+=("$sd_log_path")
    if IFS= read -r -d '' root < <(realpath -ez -- "$target" 2>/dev/null); then
        root=${root%/}/
        for path in "${exclusions[@]}" "$sd_workspace"; do
            if sd_exclusion_binding "$path" binding && [[ $binding == "$root"* ]]; then
                sd_compare_artifacts+=("${binding#"$root"}")
            fi
        done
    fi
    sd_compare_index_dir=$sd_workspace/index
    mkdir -- "$sd_compare_index_dir" || return 2
    baseline_rows=$sd_workspace/initial-projection
    sd_compare_prepare_baseline "$sd_workspace/baseline" "$baseline_rows" ||
        { sd_error 'could not project the baseline catalog'; return; }
    if [[ $quick == true ]]; then reuse_fn=sd_compare_reuse; fi
    sd_log_boundary scan || return 2
    sd_scan "$target" settings "$sd_workspace" "$jobs" "$fail_fast" "$reuse_fn" sd_compare_observe_scope "${exclusions[@]}" ||
        { sd_error 'could not complete the comparison scan'; return; }
    [[ $sd_compare_index_failed != true ]] || { sd_error 'could not read the quick baseline index'; return; }

    if [[ $sd_compare_scope_events == true || ( $sd_saved_symlinks == record && ${settings[symlinks]} == follow ) ]]; then
        sd_compare_observed_baseline "$baseline_rows" "$sd_workspace/observed-projection" ||
            { sd_error 'could not project observed comparison scope'; return; }
        baseline_rows=$sd_workspace/observed-projection
    fi
    if [[ $sd_directory_hash_scope == compared &&
        ( $sd_compare_projected == true || ${saved[directory-hashes]} == false ) ]]; then
        sd_compare_hash_baseline "$baseline_rows" "$sd_workspace/rehashed-projection" ||
            { sd_error 'could not compute projected directory hashes'; return; }
        baseline_rows=$sd_workspace/rehashed-projection
    fi
    for key in "${SD_REPORT_TOTALS[@]}"; do sd_report_totals[$key]=0; done
    sd_log_boundary compare || return 2
    if ! sd_compare "$baseline_rows" "$sd_workspace/entries" "$sd_workspace/errors" "$sd_workspace/changes"; then
        sd_error 'could not compare catalog records'
        return
    fi
    if [[ $sd_catalog_status != complete ]] || (( sd_report_totals[errors] || sd_report_totals[unknown] )); then
        report_status=2
    elif (( sd_report_totals[counted] )); then
        report_status=1
    fi
    if [[ $sd_log_enabled == true ]]; then
        local -a log_totals=()
        for key in "${SD_REPORT_TOTALS[@]}"; do log_totals+=("$key=${sd_report_totals[$key]}"); done
        sd_log_event TOTALS "${log_totals[@]}" || return 2
    fi
    sd_log_boundary write || return 2
    sd_write_comparison_report "$sd_workspace/output" "$sd_workspace/changes" "$sd_workspace/errors" "$report_status" ||
        { sd_error 'could not write the comparison report'; return; }
    sd_log_boundary publish || return 2
    sd_publish "$sd_workspace/output" "$destination" "$overwrite" "$identity" "$target" "$baseline_path" \
        "${sd_log_protected_paths[@]}" || return 2
    return "$report_status"
}

sd_compare_main() {
    local baseline_fd result=0
    exec {baseline_fd}<"$baseline_path" || { sd_error "cannot open baseline catalog: $baseline_path"; return; }
    sd_compare_from_catalog "$baseline_fd" || result=$?
    exec {baseline_fd}<&-
    return "$result"
}
