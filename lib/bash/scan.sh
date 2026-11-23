# shellcheck shell=bash

# GNU stat exposes epoch seconds and their corresponding fractional fields.
# Keep the complete string for before/after comparisons (including ctime).
sd_stat() {
    local _stat_out _stat_path=$1
    local -a _stat_options=()
    [[ $2 != true ]] || _stat_options=(-L)
    _stat_out=$(stat "${_stat_options[@]}" -c '%d:%i|%f|%s|%Y|%y|%Z|%z' -- "$_stat_path" 2>"$4") || return 2
    printf -v "$3" '%s' "$_stat_out"
}

sd_stat_kind() {
    local _kind_bits=${1#*|} _kind_name
    _kind_bits=${_kind_bits%%|*}
    case $(( (16#$_kind_bits & 0170000) >> 12 )) in
        4) _kind_name=directory ;;
        8) _kind_name='file' ;;
        10) _kind_name=symlink ;;
        *) _kind_name=special ;;
    esac
    printf -v "$2" '%s' "$_kind_name"
}

sd_stat_identity() {
    local _identity_remainder=${1#*|} _identity_type
    _identity_type=${_identity_remainder%%|*}
    printf -v "$2" '%s:%s' "${1%%|*}" "$(( 16#$_identity_type & 0170000 ))"
}

sd_metadata() {
    local _metadata_rest=${1#*|} _metadata_size _metadata_sec _metadata_nsec _metadata_time=-
    _metadata_rest=${_metadata_rest#*|}
    _metadata_size=${_metadata_rest%%|*}
    _metadata_rest=${_metadata_rest#*|}
    _metadata_sec=${_metadata_rest%%|*}
    _metadata_rest=${_metadata_rest#*|}
    _metadata_nsec=${_metadata_rest#*.}
    _metadata_nsec=${_metadata_nsec%% *}
    sd_integer "$_metadata_size" 0 no || _metadata_size=-
    if [[ $_metadata_nsec =~ ^[0-9]{9}$ ]] && sd_integer "$_metadata_sec" signed no; then
        _metadata_nsec=$((10#$_metadata_nsec))
        if (( (_metadata_sec > -9223372037 && _metadata_sec < 9223372036) ||
              (_metadata_sec == -9223372037 && _metadata_nsec >= 145224192) ||
              (_metadata_sec == 9223372036 && _metadata_nsec <= 854775807) )); then
            _metadata_time=$((_metadata_sec * 1000000000 + _metadata_nsec))
        fi
    fi
    printf -v "$2" '%s' "$_metadata_size"
    printf -v "$3" '%s' "$_metadata_time"
    if [[ $_metadata_time == - ]]; then
        printf -v "$4" '%s' -
    else
        printf -v "$4" '%s' 1
    fi
}

sd_regex_literal() {
    local _literal_char=$1
    case $_literal_char in
        \\|'('|')'|'['|']'|'{'|'}'|'.'|'*'|'?'|'+'|'|'|'^'|'$')
            _literal_char=\\$_literal_char ;;
    esac
    printf -v "$2" '%s' "$_literal_char"
}

# POSIX classes and backslash escapes have no special meaning in fnmatchcase.
# Generate explicit regex alternatives for each class to avoid shell glob rules.
sd_glob_class() {
    local _class_spec=$1 _class_i=0 _class_first _class_last _class_byte _class_escape
    local _class_result='' _class_negated=false _class_ordinal
    local -A _class_members=()
    if [[ $_class_spec == '!'* ]]; then
        _class_spec=${_class_spec:1}
        _class_negated=true
    fi
    while (( _class_i < ${#_class_spec} )); do
        printf -v _class_first '%d' "'${_class_spec:_class_i:1}"
        _class_last=$_class_first
        if (( _class_i+2 < ${#_class_spec} )) && [[ ${_class_spec:_class_i+1:1} == - ]]; then
            printf -v _class_last '%d' "'${_class_spec:_class_i+2:1}"
            ((_class_i+=2))
        fi
        for ((_class_ordinal=_class_first; _class_ordinal<=_class_last; _class_ordinal++)); do
            _class_members[$_class_ordinal]=true
        done
        ((_class_i+=1))
    done
    for ((_class_ordinal=1; _class_ordinal<=255; _class_ordinal++)); do
        [[ ${_class_members[$_class_ordinal]:-false} != "$_class_negated" ]] || continue
        printf -v _class_escape '\\x%02X' "$_class_ordinal"
        printf -v _class_byte '%b' "$_class_escape"
        sd_regex_literal "$_class_byte" _class_escape
        _class_result+="${_class_result:+|}$_class_escape"
    done
    printf -v "$2" '(%s)' "${_class_result:-a^}"
}

sd_glob_regex() {
    local _glob_value=$1 _glob_i _glob_j _glob_start _glob_char _glob_token _glob_result=^
    for ((_glob_i=0; _glob_i<${#_glob_value}; _glob_i++)); do
        _glob_char=${_glob_value:_glob_i:1}
        case $_glob_char in
            '*') _glob_result+='.*' ;;
            '?') _glob_result+='.' ;;
            '[')
                _glob_start=$((_glob_i+1))
                _glob_j=$_glob_start
                [[ ${_glob_value:_glob_j:1} != '!' ]] || ((_glob_j+=1))
                [[ ${_glob_value:_glob_j:1} != ']' ]] || ((_glob_j+=1))
                while (( _glob_j < ${#_glob_value} )) && [[ ${_glob_value:_glob_j:1} != ']' ]]; do
                    ((_glob_j+=1))
                done
                if (( _glob_j == ${#_glob_value} )); then
                    _glob_result+='\['
                else
                    sd_glob_class "${_glob_value:_glob_start:_glob_j-_glob_start}" _glob_token
                    _glob_result+=$_glob_token
                    _glob_i=$_glob_j
                fi
                ;;
            *)
                sd_regex_literal "$_glob_char" _glob_token
                _glob_result+=$_glob_token
                ;;
        esac
    done
    printf -v "$2" '%s$' "$_glob_result"
}

sd_prepare_scope() {
    local -n _scope_settings=$1
    local key item raw regex
    sd_depth=${_scope_settings[max-depth]}
    [[ $sd_depth != - ]] || sd_depth=$SD_MAX_INT
    if [[ ${_scope_settings[recursive]} == false ]] && (( sd_depth > 1 )); then sd_depth=1; fi
    for key in "${SD_FILTER_KEYS[@]}"; do
        while IFS= read -r item && [[ -n $item ]]; do
            sd_decode "$item" raw || return 2
            if [[ $key == exclude-ext ]]; then
                sd_extensions+=("${raw#.}")
            else
                sd_glob_regex "$raw" regex
                sd_filter_categories+=("$key")
                sd_filter_regexes+=("$regex")
                if [[ $raw == */* ]]; then sd_filter_basename+=(false); else sd_filter_basename+=(true); fi
            fi
        done <<< "${_scope_settings[$key]}"
    done
}

sd_matches_glob() {
    local path=$1 category=$2 i name=${1##*/}
    for i in "${!sd_filter_categories[@]}"; do
        [[ ${sd_filter_categories[i]} == "$category" ]] || continue
        if [[ $path =~ ${sd_filter_regexes[i]} ||
              ( ${sd_filter_basename[i]} == true && $name =~ ${sd_filter_regexes[i]} ) ]]; then
            return 0
        fi
    done
    return 1
}

sd_excludes_kind() {
    local path=$1 kind=$2 name=${1##*/} suffix prefix extension
    case $kind in
        directory) sd_matches_glob "$path" exclude-dir; return ;;
        file)
            [[ $name == *.* ]] || return 1
            prefix=${name%.*}
            [[ $prefix == *[!.]* ]] || return 1
            suffix=${name##*.}
            for extension in "${sd_extensions[@]}"; do
                [[ $suffix != "$extension" ]] || return 0
            done
            ;;
    esac
    return 1
}

# Used by baseline projection after sd_prepare_scope. The caller owns the
# dynamically scoped depth/filter variables described by sd_scan.
sd_scope_contains() {
    local path=$1 kind=$2 directories
    [[ $path != . ]] || return 0
    directories=${path//[^\/]/}
    (( ${#directories}+1 <= sd_depth )) || return 1
    [[ $kind != symlink || $sd_symlinks != skip ]] || return 1
    sd_matches_glob "$path" exclude && return 1
    sd_excludes_kind "$path" "$kind" && return 1
    while [[ $path == */* ]]; do
        path=${path%/*}
        sd_matches_glob "$path" exclude && return 1
        sd_matches_glob "$path" exclude-dir && return 1
    done
    return 0
}

sd_normalize_hash() {
    local _hash_result _hash_digest _hash_suffix="  -$SD_LF"
    # A zero delimiter reports failure at EOF while preserving all LF bytes.
    if IFS= read -r -d '' _hash_result <"$1"; then return 2; fi
    _hash_digest=${_hash_result%"$_hash_suffix"}
    [[ $_hash_result == "$_hash_digest$_hash_suffix" ]] && sd_digest_field "$_hash_digest" || return 2
    printf -v "$2" '%s' "$_hash_digest"
}

sd_hash_stream() {
    local hash_response=$sd_scan_dir/parent.hash result=0 monitor=false
    if [[ $- == *m* ]]; then monitor=true; fi
    set -m
    "$sd_hasher" - <&0 >"$hash_response" 2>/dev/null &
    sd_parent_child=$!
    wait "$sd_parent_child" 2>/dev/null || result=2
    sd_parent_child=
    if [[ $monitor == false ]]; then set +m; fi
    (( result == 0 )) || return 2
    sd_normalize_hash "$hash_response" "$1"
}

# Prefixes group subtrees in one external sort, even for a sibling such as
# 'a!' whose bytewise path ordering otherwise interleaves with 'a/child'.
sd_tree_key() {
    local _tree_path=$1 _tree_result='' _tree_component
    if [[ $_tree_path != . ]]; then
        while [[ $_tree_path == */* ]]; do
            sd_hex_key "${_tree_path%%/*}" _tree_component
            _tree_result+="$_tree_component!"
            _tree_path=${_tree_path#*/}
        done
        sd_hex_key "$_tree_path" _tree_component
        _tree_result+="$_tree_component!"
    fi
    printf -v "$2" '%s' "$_tree_result"
}

sd_entry_record() {
    local path=$1 kind=$2 info=$3 digest=$4 target_value=$5 verification=$6
    local wire key order size mtime precision
    sd_tree_key "$path" key
    sd_hex_key "$path" order
    sd_encode "$path" wire
    sd_metadata "$info" size mtime precision
    printf '%s\t1\t%s\tE\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$key" "$order" "$wire" "$kind" "$digest" "$size" "$mtime" "$precision" "$target_value" "$verification" \
        >>"$sd_records" || return 2
    if [[ $kind == file ]]; then sd_log_file_done "$path" "$verification"; fi
}

sd_scan_error() {
    local path=$1 operation=$2 message=$3 scope=$4 wire text key order
    sd_tree_key "$path" key
    sd_hex_key "$path" order
    sd_encode "$path" wire
    sd_encode "$message" text
    printf '%s\t2\t%s\tX\t%s\t%s\t%s\t%s\n' "$key" "$order" "$wire" "$operation" "$text" "$scope" \
        >>"$sd_records" || return 2
    : >"$sd_scan_dir/scan-error" || return 2
    sd_log_error "$path" "$operation" "$message"
}

sd_diagnostic() {
    local _diagnostic_value
    _diagnostic_value=$(<"$1")
    printf -v "$3" '%s' "${_diagnostic_value##*: }"
    [[ -n $_diagnostic_value ]] || printf -v "$3" '%s' "$2"
}

sd_stop_child() {
    local -n _stop_pid=$1
    if [[ -n $_stop_pid ]]; then
        kill -TERM -- "-$_stop_pid" 2>/dev/null || kill -TERM "$_stop_pid" 2>/dev/null || true
        wait "$_stop_pid" 2>/dev/null || true
        _stop_pid=
    fi
}

# GNU dd opens the input without blocking, then pauses on the output FIFO.
# Reopen its input through /proc and validate it while dd cannot consume bytes.
sd_open_checked() {
    local pathname=$1 expected=$2 follow=$3 kind=$4 fifo=$5 error_log=$6
    local -n _open_fd=$7 _open_pid=$8
    local observed='' probe_kind source_proc probe_attempt=0
    local flags=nonblock
    [[ $follow == true ]] || flags+=,nofollow
    [[ $kind != directory ]] || flags+=,directory
    # Redirect before forking so the probe cannot see the inherited control FIFO.
    {
        dd if="$pathname" of="$fifo" iflag="$flags" status=none 2>"$error_log" &
        _open_pid=$!
    } </dev/null
    source_proc=/proc/$_open_pid/fd/0
    while kill -0 "$_open_pid" 2>/dev/null; do
        if sd_stat "$source_proc" true observed /dev/null && [[ $observed != "$sd_dev_null" ]]; then
            sd_stat_kind "$observed" probe_kind
            if [[ $kind == directory ]]; then sd_stat_identity "$observed" observed; fi
            if [[ $probe_kind == "$kind" && $observed == "$expected" ]] &&
                { exec {_open_fd}<"$source_proc"; } 2>/dev/null; then
                sd_stop_child "$8"
                if sd_stat "/proc/$BASHPID/fd/$_open_fd" true observed /dev/null; then
                    if [[ $kind == directory ]]; then sd_stat_identity "$observed" observed; fi
                    [[ $observed == "$expected" ]] && return 0
                fi
                exec {_open_fd}<&-
            else
                sd_stop_child "$8"
            fi
            return 2
        fi
        # A wedged open must not leave a worker spinning or scanning forever.
        ((probe_attempt+=1))
        if (( probe_attempt >= 1000 )); then
            sd_stop_child "$8"
            return 2
        fi
    done
    sd_stop_child "$8"
    return 2
}

sd_worker_hash() {
    local path=$1 absolute=$2 expected=$3 follow=$4 fd observed digest message
    if ! sd_open_checked "$absolute" "$expected" "$follow" file "$sd_open_gate" "$sd_input_error" fd sd_active_child; then
        sd_diagnostic "$sd_input_error" 'file changed before hashing' message
        sd_scan_error "$path" hash-file "$message" entry
        return
    fi
    "$sd_hasher" - <&"$fd" >"$sd_hash_output" 2>/dev/null &
    sd_active_child=$!
    if wait "$sd_active_child" 2>/dev/null; then
        sd_normalize_hash "$sd_hash_output" digest || digest=
    else
        digest=
    fi
    sd_active_child=
    if [[ -z $digest ]]; then
        exec {fd}<&-
        sd_scan_error "$path" hash-file "$sd_hasher did not return a valid digest" entry
        return
    fi
    if ! sd_stat "/proc/$BASHPID/fd/$fd" true observed /dev/null || [[ $observed != "$expected" ]]; then
        exec {fd}<&-
        sd_scan_error "$path" hash-file 'file changed while hashing' entry
        return
    fi
    exec {fd}<&-
    if ! sd_stat "$absolute" "$follow" observed /dev/null || [[ $observed != "$expected" ]]; then
        sd_scan_error "$path" hash-file 'file changed while hashing' entry
        return
    fi
    sd_entry_record "$path" file "$expected" "$digest" - verified
}

sd_worker() {
    local index=$1 input=$2 path absolute snapshot follow i
    local sd_records=$sd_scan_dir/worker-$index.records sd_hash_output=$sd_scan_dir/worker-$index.hash
    local sd_open_gate=$sd_scan_dir/worker-$index.open sd_input_error=$sd_scan_dir/worker-$index.stderr
    local sd_active_child=
    trap - EXIT
    trap 'sd_stop_child sd_active_child; exit 2' HUP INT TERM
    set -m
    for i in "${sd_control_fds[@]}"; do exec {i}>&-; done
    for i in "${sd_traversal_fds[@]}" "${sd_job_fds[@]}"; do exec {i}<&-; done
    for i in "${sd_record_fds[@]}"; do exec {i}<&-; done
    if [[ -n $sd_log_fd ]]; then exec {sd_log_fd}>&-; fi
    while IFS= read -r -d '' path &&
          IFS= read -r -d '' absolute &&
          IFS= read -r -d '' snapshot &&
          IFS= read -r -d '' follow; do
        sd_worker_hash "$path" "$absolute" "$snapshot" "$follow" || exit 2
        printf '%s\n' "$index" >&"$sd_response_fd" || exit 2
    done <"$input"
}

sd_start_worker() {
    local index=$1 input=$sd_scan_dir/worker-$1.commands control record_fd
    mkfifo -- "$input" "$sd_scan_dir/worker-$index.open" || return 2
    : >"$sd_scan_dir/worker-$index.records" || return 2
    if [[ ${sd_log_enabled:-false} == true ]]; then
        exec {record_fd}<"$sd_scan_dir/worker-$index.records" || return 2
        sd_record_fds[index]=$record_fd
    fi
    sd_worker "$index" "$input" &
    sd_worker_pids+=("$!")
    exec {control}>"$input" || return 2
    sd_control_fds[index]=$control
    sd_worker_records+=("$sd_scan_dir/worker-$index.records")
}

sd_reap_worker() {
    local id pid parent_copy timeout=1
    if [[ ${sd_log_enabled:-false} == true ]]; then timeout=0.2; fi
    while ! IFS= read -r -t "$timeout" -u "$sd_response_fd" id; do
        sd_log_tick || return 2
        for pid in "${sd_worker_pids[@]}"; do
            kill -0 "$pid" 2>/dev/null || return 2
        done
    done
    [[ $id =~ ^[0-9]+$ && ${sd_busy_workers[$id]:-false} == true ]] || return 2
    parent_copy=${sd_job_fds[id]}
    exec {parent_copy}<&-
    unset 'sd_job_fds[id]'
    sd_busy_workers[id]=false
    ((sd_pending-=1))
    # shellcheck disable=SC2034 # Shared with the coordinator's progress module.
    sd_log_active=$sd_pending
    sd_log_worker_record "$id" || return 2
    return 0
}

sd_stop_scheduling() {
    if [[ $sd_fail_fast == true && -e $sd_scan_dir/scan-error ]]; then
        sd_aborted=true
        return 0
    fi
    return 1
}

sd_schedule_hash() {
    local relative=$1 parent_fd=$2 name=$3 snapshot=$4 follow=$5
    local index output_fd digest='' size mtime precision parent_copy
    sd_stop_scheduling && return 0
    if [[ -n $sd_reuse_observer ]]; then
        sd_metadata "$snapshot" size mtime precision
        if "$sd_reuse_observer" "$relative" "$size" "$mtime" "$precision" digest && sd_digest_field "$digest"; then
            sd_entry_record "$relative" file "$snapshot" "$digest" - reused
            return
        fi
    fi
    if (( sd_pending == sd_worker_limit )); then
        sd_reap_worker || return 2
    fi
    sd_stop_scheduling && return 0
    for ((index=0; index<sd_worker_limit; index++)); do
        [[ ${sd_busy_workers[index]:-false} != true ]] || continue
        if [[ -z ${sd_control_fds[index]+set} ]]; then
            sd_start_worker "$index" || return 2
        fi
        output_fd=${sd_control_fds[index]}
        # Keep the directory anchor valid after traversal leaves this branch.
        exec {parent_copy}<&"$parent_fd" || return 2
        sd_job_fds[index]=$parent_copy
        printf '%s\0' "$relative" "/proc/$sd_scan_pid/fd/$parent_copy/$name" "$snapshot" "$follow" \
            >&"$output_fd" || return 2
        sd_busy_workers[index]=true
        ((sd_pending+=1))
        # shellcheck disable=SC2034 # Shared with the coordinator's progress module.
        sd_log_active=$sd_pending
        sd_log_event HASH "path=$relative" || return 2
        return 0
    done
    return 2
}

sd_scope_event() {
    if [[ $2 == *link* ]]; then sd_log_event LINK "path=$1" "action=$2" || return 2; fi
    if [[ -n $sd_scope_observer ]]; then
        "$sd_scope_observer" "$1" "$2"
    fi
}

sd_record_link() {
    local path=$1 absolute=$2 info=$3 target_value wire observed digest message
    if ! IFS= read -r -d '' target_value < <(readlink -z -- "$absolute" 2>"$sd_error_log"); then
        sd_diagnostic "$sd_error_log" 'could not read symbolic link' message
        sd_scan_error "$path" readlink "$message" entry
        return
    fi
    if ! sd_stat "$absolute" false observed /dev/null || [[ $observed != "$info" ]]; then
        sd_scan_error "$path" readlink 'symbolic link changed while reading' entry
        return
    fi
    printf '%s' "$target_value" >"$sd_scan_dir/link-input" || return 2
    if ! sd_hash_stream digest <"$sd_scan_dir/link-input"; then
        sd_scan_error "$path" readlink "$sd_hasher did not return a valid digest" entry
        return
    fi
    sd_encode "$target_value" wire
    sd_entry_record "$path" symlink "$info" "$digest" "$wire" verified
}

sd_visit() {
    sd_log_tick || return 2
    local relative=$1 parent_fd=$2 name=$3 physical=$4 depth=$5
    local absolute=/proc/$sd_scan_pid/fd/$parent_fd/$name
    local info kind follow=false message
    if [[ -n ${sd_excluded["$physical"]+set} ]]; then
        sd_scope_event "$relative" implicit-exclusion
        return
    fi
    sd_matches_glob "$relative" exclude && return 0
    if ! sd_stat "$absolute" false info "$sd_error_log"; then
        sd_diagnostic "$sd_error_log" 'could not stat entry' message
        sd_scan_error "$relative" lstat "$message" subtree
        return
    fi
    sd_stat_kind "$info" kind
    if [[ $kind == symlink && $sd_symlinks == follow ]]; then
        follow=true
        if IFS= read -r -d '' physical < <(realpath -mz -- "$physical" 2>/dev/null) &&
            [[ -n ${sd_excluded["$physical"]+set} ]]; then
            sd_scope_event "$relative" implicit-exclusion
            return
        fi
        if ! sd_stat "$absolute" true info "$sd_error_log"; then
            sd_diagnostic "$sd_error_log" 'could not follow link' message
            sd_scan_error "$relative" follow-link "$message" subtree
            return
        fi
        sd_stat_kind "$info" kind
    fi
    if sd_excludes_kind "$relative" "$kind"; then
        if [[ $follow == true ]]; then sd_scope_event "$relative" filtered-link; fi
        return
    fi
    if [[ $follow == true && ( $kind == file || $kind == directory ) ]]; then
        sd_scope_event "$relative" followed-link || return 2
    fi
    case $kind in
        directory) sd_walk "$relative" "$absolute" "$physical" "$depth" "$info" "$follow" ;;
        file) sd_schedule_hash "$relative" "$parent_fd" "$name" "$info" "$follow" ;;
        symlink)
            if [[ $sd_symlinks == record ]]; then
                sd_scope_event "$relative" recorded-link || return 2
                sd_record_link "$relative" "$absolute" "$info"
            else
                sd_scope_event "$relative" skipped-link
            fi
            ;;
        special) sd_scan_error "$relative" entry-type 'unsupported filesystem entry type' entry ;;
    esac
}

sd_mark_partial_directory() {
    local tree
    sd_tree_key "$1" tree
    printf '%s\t3\t\tinvalid\n' "$tree" >>"$sd_records"
}

sd_open_scan_directory() {
    sd_open_checked "$1" "$2" "$3" directory "$sd_scan_dir/directory-open" "$sd_error_log" "$4" sd_parent_child
}

sd_walk() {
    local relative=$1 absolute=$2 physical=$3 depth=$4 info=$5 follow=$6
    local identity wire message child path directory_fd children_fd traversed=false
    local children=$sd_scan_dir/children-$depth
    sd_stat_identity "$info" identity
    if [[ -n ${sd_ancestors[$identity]+set} ]]; then
        sd_scan_error "$relative" follow-link 'directory resolves to an ancestor' subtree
        return
    fi
    sd_entry_record "$relative" directory "$info" - - unverified || return 2
    if (( depth < sd_depth )); then
        if sd_open_scan_directory "$absolute" "$identity" "$follow" directory_fd; then
            traversed=true
        else
            sd_diagnostic "$sd_error_log" 'directory binding changed while scanning' message
            sd_scan_error "$relative" scandir "$message" subtree || return 2
        fi
    fi
    sd_encode "$relative" wire
    printf '%s\t%s\t%s\t%s\t%s\n' "$depth" "$wire" "$identity" "$follow" "$traversed" \
        >>"$sd_scan_dir/bindings" || return 2
    [[ $traversed == true ]] || return 0

    sd_traversal_fds[directory_fd]=$directory_fd
    if ! find -H "/proc/$sd_scan_pid/fd/$directory_fd" -mindepth 1 -maxdepth 1 -printf '%f\0' \
        >"$children" 2>"$sd_error_log"; then
        sd_diagnostic "$sd_error_log" 'could not enumerate directory' message
        sd_scan_error "$relative" scandir "$message" subtree || return 2
    fi
    exec {children_fd}<"$children" || return 2
    sd_traversal_fds[children_fd]=$children_fd
    sd_ancestors[$identity]=true
    while IFS= read -r -d '' -u "$children_fd" child; do
        sd_stop_scheduling && break
        path=$child
        [[ $relative == . ]] || path=$relative/$child
        sd_visit "$path" "$directory_fd" "$child" "${physical%/}/$child" "$((depth+1))" || return 2
        [[ $sd_aborted == false ]] || break
    done
    if [[ $sd_aborted == true ]]; then sd_mark_partial_directory "$relative" || return 2; fi
    unset 'sd_ancestors[$identity]' 'sd_traversal_fds[$directory_fd]' 'sd_traversal_fds[$children_fd]'
    exec {children_fd}<&- {directory_fd}<&-
}

sd_binding_matches() {
    local info identity
    sd_stat "$1" "$3" info /dev/null || return 1
    sd_stat_identity "$info" identity
    [[ $identity == "$2" ]]
}

sd_discard_binding() {
    local key order
    sd_tree_key "$1" key
    sd_hex_key "$1" order
    printf '%s\t0\t%s\tdrop\n' "$key" "$order" >>"$sd_records" || return 2
    sd_scan_error "$1" directory-binding 'directory binding changed while scanning' subtree
}

sd_close_binding() {
    local slot=$1 descriptor=${binding_fds[$1]}
    if ! sd_binding_matches "${binding_anchors[slot]}" "${binding_identities[slot]}" "${binding_follows[slot]}"; then
        sd_discard_binding "${binding_paths[slot]}" || return 2
    fi
    exec {descriptor}<&-
    unset 'binding_fds[slot]' 'binding_paths[slot]' 'binding_anchors[slot]' \
        'binding_identities[slot]' 'binding_follows[slot]'
}

sd_verify_bindings() {
    local depth wire identity follow traversed path anchor descriptor bad_depth=-1
    local -a binding_fds=() binding_paths=() binding_anchors=() binding_identities=() binding_follows=()
    while IFS="$SD_TAB" read -r depth wire identity follow traversed; do
        while (( ${#binding_fds[@]} > depth )); do
            sd_close_binding "$((${#binding_fds[@]}-1))" || return 2
        done
        (( bad_depth == -1 || depth <= bad_depth )) || continue
        bad_depth=-1
        sd_decode "$wire" path || return 2
        anchor=$sd_scan_root
        if (( depth > 0 )); then anchor=/proc/$sd_scan_pid/fd/${binding_fds[depth-1]}/${path##*/}; fi
        if ! sd_binding_matches "$anchor" "$identity" "$follow"; then
            sd_discard_binding "$path" || return 2
            bad_depth=$depth
        elif [[ $traversed == true ]]; then
            if sd_open_scan_directory "$anchor" "$identity" "$follow" descriptor; then
                binding_fds[depth]=$descriptor
                binding_paths[depth]=$path
                binding_anchors[depth]=$anchor
                binding_identities[depth]=$identity
                binding_follows[depth]=$follow
            else
                sd_discard_binding "$path" || return 2
                bad_depth=$depth
            fi
        fi
    done <"$sd_scan_dir/bindings"
    while (( ${#binding_fds[@]} )); do
        sd_close_binding "$((${#binding_fds[@]}-1))" || return 2
    done
}

sd_aggregate_frame() {
    local slot=$1 kind=$2 escaped_name=$3 digest=$4 verification=$5 tree=${6%!} parent_tree=''
    [[ $sd_directory_hashes == true && $slot -ge 0 ]] || return 0
    # Imported catalogs can omit intermediate records. A frame contains only
    # direct children, even when the next present directory is a higher ancestor.
    if [[ $tree == *!* ]]; then parent_tree=${tree%!*}!; fi
    [[ ${frame_prefix[slot]} == "$parent_tree" ]] || return 0
    if [[ $digest == - ]]; then
        frame_valid[slot]=false
    elif [[ ${frame_valid[slot]} == true ]]; then
        printf '%s\0%s\0%s\0' "$kind" "$escaped_name" "$digest" >>"$sd_scan_dir/frame-$slot" || return 2
        if [[ $verification != verified ]]; then frame_verification[slot]=unverified; fi
    fi
}

sd_aggregate_close() {
    local slot=$1 digest=- verification=unverified name=${frame_name[$1]} message log_path
    local -a fields
    sd_split_fields "${frame_record[slot]}" fields
    if [[ $sd_directory_hashes == true && ${frame_valid[slot]} == true ]]; then
        sd_decode "${fields[1]}" log_path || return 2
        sd_log_event HASH_DIRECTORY "path=$log_path" || return 2
        if sd_hash_stream digest <"$sd_scan_dir/frame-$slot"; then
            verification=${frame_verification[slot]}
            sd_log_event DIRECTORY_HASHED "path=$log_path" || return 2
        else
            sd_encode "$sd_hasher did not return a valid digest" message
            printf '%s\tX\t%s\thash-directory\t%s\tentry\n' \
                "${frame_order[slot]}" "${fields[1]}" "$message" >>"$sd_scan_dir/keyed-errors" || return 2
            sd_log_error "$log_path" hash-directory "$sd_hasher did not return a valid digest" || return 2
        fi
    fi
    printf '%s\tE\t%s\tdirectory\t%s\t%s\t%s\t%s\t-\t%s\n' \
        "${frame_order[slot]}" "${fields[1]}" "$digest" "${fields[4]}" "${fields[5]}" "${fields[6]}" "$verification" \
        >>"$sd_scan_dir/keyed-entries" || return 2
    sd_aggregate_frame "$((slot-1))" directory "$name" "$digest" "$verification" "${frame_prefix[slot]}" || return 2
    unset 'frame_prefix[slot]' 'frame_record[slot]' 'frame_order[slot]' 'frame_name[slot]' \
        'frame_valid[slot]' 'frame_verification[slot]'
}

sd_aggregate() {
    local sorted=$1 line tree order rank row escaped_name dropping=false dropped_tree='' slot
    local -a fields
    local -a frame_prefix=() frame_record=() frame_order=() frame_name=() frame_valid=() frame_verification=()
    : >"$sd_scan_dir/keyed-entries" && : >"$sd_scan_dir/keyed-errors" || return 2
    while IFS= read -r line; do
        sd_log_tick || return 2
        tree=${line%%"$SD_TAB"*}
        line=${line#*"$SD_TAB"}
        rank=${line%%"$SD_TAB"*}
        line=${line#*"$SD_TAB"}
        order=${line%%"$SD_TAB"*}
        row=${line#*"$SD_TAB"}
        while (( ${#frame_prefix[@]} )); do
            slot=$((${#frame_prefix[@]}-1))
            [[ $tree != "${frame_prefix[slot]}"* ]] || break
            sd_aggregate_close "$slot" || return 2
        done
        slot=$((${#frame_prefix[@]}-1))
        if [[ $dropping == true && $tree != "$dropped_tree"* ]]; then dropping=false; fi
        case $rank in
            0)
                if [[ $dropping == false ]]; then dropped_tree=$tree; dropping=true; fi
                ;;
            1)
                [[ $dropping == false ]] || continue
                sd_split_fields "$row" fields
                escaped_name=${fields[1]##*/}
                if [[ ${fields[2]} == directory ]]; then
                    ((slot+=1))
                    frame_prefix[slot]=$tree frame_record[slot]=$row frame_order[slot]=$order
                    frame_name[slot]=$escaped_name frame_valid[slot]=true frame_verification[slot]=verified
                    if [[ $sd_directory_hashes == true ]]; then
                        : >"$sd_scan_dir/frame-$slot" || return 2
                    fi
                else
                    printf '%s\t%s\n' "$order" "$row" >>"$sd_scan_dir/keyed-entries" || return 2
                    sd_aggregate_frame "$slot" "${fields[2]}" "$escaped_name" "${fields[3]}" "${fields[8]}" "$tree" ||
                        return 2
                fi
                ;;
            2)
                printf '%s\t%s\n' "$order" "$row" >>"$sd_scan_dir/keyed-errors" || return 2
                if (( slot >= 0 )) && [[ $sd_directory_hashes == true ]]; then
                    frame_valid[slot]=false
                fi
                ;;
            3)
                if (( slot >= 0 )) && [[ ${frame_prefix[slot]} == "$tree" ]]; then
                    frame_valid[slot]=false
                fi
                ;;
        esac
    done <"$sorted"
    for ((slot=${#frame_prefix[@]}-1; slot>=0; slot--)); do
        sd_aggregate_close "$slot" || return 2
    done
}

# Canonical entries and errors are sorted by decoded bytes. Error ties use
# canonical encoded fields in byte order, as required by comparison reports.
sd_scan_sorted_spool() {
    local input=$1 result=$2
    ( set -o pipefail
      sort -S 32M -T "$sd_scan_dir" -t "$SD_TAB" -k1,1 -- "$input" | cut -f2- >"$result" )
}

# ROOT SETTINGS_MAP WORKSPACE JOBS FAIL_FAST REUSE_FN SCOPE_FN [EXCLUSIONS...]
# Returns zero after producing entries/errors, including for an incomplete
# observation. Technical failure returns 2 and produces no publishable spool.
sd_scan() {
    local sd_scan_root=$1 sd_scan_dir=$3 sd_worker_limit=$4 sd_fail_fast=$5
    local sd_scan_pid=$BASHPID
    local sd_reuse_observer=$6 sd_scope_observer=$7
    local settings_ref=$2
    local -n _scan_settings=$settings_ref
    shift 7
    local sd_symlinks=${_scan_settings[symlinks]} sd_directory_hashes=${_scan_settings[directory-hashes]}
    local sd_hasher=sha256sum sd_depth sd_dev_null sd_response_fd
    local sd_error_log=$sd_scan_dir/scan.stderr sd_records=$sd_scan_dir/scan.records
    local root_physical root_info root_kind path binding pid control status sd_pending=0 sd_aborted=false
    local -a sd_filter_categories=() sd_filter_regexes=() sd_filter_basename=() sd_extensions=()
    local -a sd_control_fds=() sd_busy_workers=() sd_worker_records=() sd_job_fds=() sd_traversal_fds=() sd_record_fds=()
    local -A sd_excluded=() sd_ancestors=()
    [[ ${_scan_settings[hash]} != blake3 ]] || sd_hasher=b3sum
    sd_prepare_scope "$settings_ref" || return 2
    for path in "$sd_scan_dir" "$@"; do
        if sd_exclusion_binding "$path" binding; then sd_excluded["$binding"]=true; fi
    done
    : >"$sd_records" && : >"$sd_scan_dir/bindings" || return 2
    rm -f -- "$sd_scan_dir/scan-error" || return 2
    mkfifo -- "$sd_scan_dir/worker-results" "$sd_scan_dir/directory-open" || return 2
    exec {sd_response_fd}<>"$sd_scan_dir/worker-results" || return 2
    sd_stat /dev/null true sd_dev_null /dev/null || return 2
    if ! sd_stat "$sd_scan_root" true root_info "$sd_error_log" ||
        ! IFS= read -r -d '' root_physical < <(realpath -ez -- "$sd_scan_root" 2>/dev/null); then
        sd_diagnostic "$sd_error_log" 'could not stat target' path
        sd_scan_error . stat "$path" subtree || return 2
    else
        sd_stat_kind "$root_info" root_kind
        if [[ $root_kind == directory ]]; then
            sd_walk . "$sd_scan_root" "$root_physical" 0 "$root_info" true || return 2
        else
            sd_scan_error . entry-type 'target is not a directory' subtree || return 2
        fi
    fi
    if [[ $sd_aborted == true ]]; then
        sd_scan_error . aborted 'scan stopped after an error; remaining scope is unknown' subtree || return 2
    fi
    while (( sd_pending )); do sd_reap_worker || return 2; done
    for control in "${sd_control_fds[@]}"; do exec {control}>&-; done
    exec {sd_response_fd}>&-
    status=0
    for pid in "${sd_worker_pids[@]}"; do wait "$pid" || status=2; done
    sd_worker_pids=()
    for control in "${sd_record_fds[@]}"; do exec {control}<&-; done
    (( status == 0 )) || return 2
    sd_verify_bindings || return 2
    sort -S 32M -T "$sd_scan_dir" -t "$SD_TAB" -k1,1 -k2,2 -- \
        "$sd_records" "${sd_worker_records[@]}" >"$sd_scan_dir/tree-sorted" || return 2
    if [[ $sd_directory_hashes == true ]]; then sd_log_boundary hash-directory || return 2; fi
    sd_aggregate "$sd_scan_dir/tree-sorted" || return 2
    sd_scan_sorted_spool "$sd_scan_dir/keyed-entries" "$sd_scan_dir/entries" &&
        sd_scan_sorted_spool "$sd_scan_dir/keyed-errors" "$sd_scan_dir/errors" || return 2
    # The CLI and comparison orchestrator read the completion status.
    # shellcheck disable=SC2034
    sd_catalog_status=complete
    if [[ -s $sd_scan_dir/errors ]]; then
        # shellcheck disable=SC2034
        sd_catalog_status=incomplete
    fi
}
