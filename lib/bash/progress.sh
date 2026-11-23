# shellcheck shell=bash
# Only the coordinator writes operational events. Worker stdout is a protocol.
sd_verbose=false sd_log_arg='' sd_log_path='' sd_log_fd='' sd_log_scratch=''
sd_log_enabled=false sd_log_failed=false sd_log_owner=$BASHPID
sd_log_started=0 sd_log_last=0 sd_log_now=0 sd_log_phase=validate
sd_log_completed=0 sd_log_hashed=0 sd_log_reused=0 sd_log_active=0 sd_log_errors=0
sd_log_protected_paths=()

sd_log_event() {
    [[ $sd_log_enabled == true && $BASHPID == "$sd_log_owner" ]] || return 0
    [[ $sd_log_failed == false ]] || return 2
    local event=$1 timestamp line item key value encoded
    shift
    TZ=UTC printf -v timestamp '%(%Y-%m-%dT%H:%M:%SZ)T' -1
    line="$timestamp $event"
    for item; do
        key=${item%%=*}; value=${item#*=}
        sd_encode "$value" encoded
        line+=" $key=$encoded"
    done
    if [[ -n $sd_log_fd ]] && ! printf '%s\n' "$line" >&"$sd_log_fd"; then
        sd_log_failed=true
        return 2
    fi
    if [[ $sd_verbose == true ]] && ! printf '%s\n' "$line" >&2; then
        sd_log_failed=true
        return 2
    fi
}

sd_log_tick() {
    [[ $sd_log_enabled == true && $BASHPID == "$sd_log_owner" ]] || return 0
    # Linux uptime is monotonic and needs only a builtin read, including on
    # Bash before EPOCHREALTIME. One extra centisecond avoids rounding below 1s.
    local uptime_seconds uptime_fraction uptime_rest
    # shellcheck disable=SC2034 # Discard the idle-time remainder of /proc/uptime.
    IFS=' .' read -r uptime_seconds uptime_fraction uptime_rest </proc/uptime || return 2
    sd_log_now=$((uptime_seconds * 100 + 10#$uptime_fraction))
    if (( sd_log_started == 0 )); then sd_log_started=$sd_log_now; sd_log_last=$sd_log_now; fi
    if [[ ${1:-} != force ]] && (( sd_log_now - sd_log_last < 101 )); then return 0; fi
    sd_log_last=$sd_log_now
    sd_log_event PROGRESS "phase=$sd_log_phase" "completed=$sd_log_completed" "hashed=$sd_log_hashed" \
        "reused=$sd_log_reused" "active=$sd_log_active" "errors=$sd_log_errors" "elapsed=$(((sd_log_now-sd_log_started)/100))"
}

sd_log_boundary() {
    [[ $sd_log_enabled == true ]] || return 0
    sd_log_phase=$1
    sd_log_event PHASE "phase=$1" && sd_log_tick force
}

sd_log_error() {
    [[ $sd_log_enabled == true && $BASHPID == "$sd_log_owner" ]] || return 0
    ((sd_log_errors+=1))
    sd_log_event ERROR "path=$1" "operation=$2" "message=$3"
}

sd_log_file_done() {
    [[ $sd_log_enabled == true && $BASHPID == "$sd_log_owner" ]] || return 0
    ((sd_log_completed+=1))
    if [[ $2 == reused ]]; then
        ((sd_log_reused+=1)); sd_log_event REUSED "path=$1"
    else
        ((sd_log_hashed+=1)); sd_log_event HASHED "path=$1"
    fi
}

# Consume a completed worker's private record after its ordinary ACK. Keep the
# reader offset in the parent so logging never rereads an ever-growing spool.
sd_log_worker_record() {
    [[ $sd_log_enabled == true ]] || return 0
    local row path message fd=${sd_record_fds[$1]:-}
    local -a fields
    IFS= read -r -u "$fd" row || return 2
    sd_split_fields "$row" fields
    sd_decode "${fields[4]}" path || return 2
    if [[ ${fields[3]} == X ]]; then
        ((sd_log_completed+=1))
        sd_decode "${fields[6]}" message || return 2
        sd_log_error "$path" "${fields[5]}" "$message"
    else
        sd_log_file_done "$path" "${fields[11]}"
    fi
}

# These invocation variables are dynamically owned by sd_main.
# shellcheck disable=SC2154
sd_log_init() {
    local identity=$1
    shift
    if [[ -n $sd_log_arg ]]; then
        sd_exclusion_binding "$sd_log_arg" sd_log_path ||
            { sd_error "log must name a file in an existing directory: $sd_log_arg"; return; }
        sd_validate_destination "$destination" "$overwrite" "$identity" "$target" "$sd_log_path" "$@" || return 2
        sd_validate_destination "$sd_log_path" "$overwrite" "$identity" "$target" "$destination" "$@" || return 2
        sd_log_scratch=$(mktemp -d -- "${sd_log_path%/*}/.sourcediff-log-XXXXXXXX") || return 2
        exec {sd_log_fd}>"$sd_log_scratch/log" || return 2
        # shellcheck disable=SC2034 # Final publication reads these in main/compare.
        sd_log_protected_paths=("$sd_log_path" "/proc/$BASHPID/fd/$sd_log_fd")
        sd_publish "$sd_log_scratch/log" "$sd_log_path" "$overwrite" "$identity" "$target" "$destination" "$@" || return 2
        rm -rf -- "$sd_log_scratch" || return 2
        sd_log_scratch=''
    fi
    if [[ $sd_verbose == true || -n $sd_log_path ]]; then sd_log_enabled=true; fi
    sd_log_event START "command=$command_name" && sd_log_boundary validate
}

sd_log_finish() {
    local status=$1 outcome=completed
    if [[ $sd_log_failed == true ]]; then
        printf 'sourcediff: operational log write failed\n' >&2
        return 2
    fi
    (( status != 2 )) || outcome=incomplete
    sd_log_active=0
    sd_log_tick force && sd_log_event SUMMARY "outcome=$outcome" "status=$status" \
        "completed=$sd_log_completed" "hashed=$sd_log_hashed" "reused=$sd_log_reused" \
        "errors=$sd_log_errors" "elapsed=$(((sd_log_now-sd_log_started)/100))"
}
