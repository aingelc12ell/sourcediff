# shellcheck shell=bash

sd_workspace=
sd_worker_pids=()
sd_parent_child=

sd_error() {
    local diagnostic=$1
    if [[ ${sd_verbose:-false} == true ]]; then sd_encode "$diagnostic" diagnostic; fi
    printf 'sourcediff: %s\n' "$diagnostic" >&2
    if declare -F sd_log_error >/dev/null; then sd_log_error . invocation "$1" || true; fi
    return 2
}

# Resolve an existing parent through filesystem bindings, retaining the last name.
sd_exclusion_binding() {
    local _binding_path=$1 _binding_name _binding_parent _binding_actual
    _binding_name=${_binding_path##*/}
    case $_binding_name in
        ''|.|..) return 2 ;;
    esac
    if [[ $_binding_path == */* ]]; then
        _binding_parent=${_binding_path%/*}
        _binding_parent=${_binding_parent:-/}
    else
        _binding_parent=.
    fi
    [[ -d $_binding_parent ]] || return 2
    IFS= read -r -d '' _binding_actual < <(realpath -ez -- "$_binding_parent" 2>/dev/null) || return 2
    printf -v "$2" '%s/%s' "${_binding_actual%/}" "$_binding_name"
}

sd_identity() {
    stat -Lc '%d:%i' -- "$1" 2>/dev/null
}

# Arguments after the protected identity are paths whose aliases must not be replaced.
sd_validate_destination() {
    local dest=$1 overwrite=$2 protected_identity=$3
    shift 3
    local canonical actual path protected target_identity
    sd_exclusion_binding "$dest" dest || { sd_error "output must name a file in an existing directory: $dest"; return; }
    IFS= read -r -d '' canonical < <(realpath -mz -- "$dest") || return 2
    actual=$(sd_identity "$dest")
    if [[ -n $actual && $actual == "$protected_identity" ]]; then
        sd_error "output aliases the input catalog or scan target: $dest"
        return
    fi
    for path; do
        IFS= read -r -d '' protected < <(realpath -mz -- "$path") || return 2
        target_identity=$(sd_identity "$path")
        if [[ $canonical == "$protected" || ( -n $actual && $actual == "$target_identity" ) ]]; then
            sd_error "output aliases the input catalog or scan target: $dest"
            return
        fi
    done
    if [[ -e $dest || -L $dest ]]; then
        if [[ $overwrite == false ]]; then
            sd_error "output already exists: $dest; use --overwrite to replace it"
            return
        fi
        if [[ -d $dest ]]; then
            sd_error "output is a directory: $dest"
            return
        fi
    fi
    return 0
}

# Cleanup is owned by the invoking shell. Worker signal handlers stop their active
# subprocesses before the workspace is removed.
sd_cleanup() {
    local pid
    if [[ -n $sd_parent_child ]]; then
        kill -TERM -- "-$sd_parent_child" 2>/dev/null || kill -TERM "$sd_parent_child" 2>/dev/null || true
        wait "$sd_parent_child" 2>/dev/null || true
    fi
    for pid in "${sd_worker_pids[@]}"; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    for pid in "${sd_worker_pids[@]}"; do
        wait "$pid" 2>/dev/null || true
    done
    [[ -z ${sd_workspace:-} ]] || rm -rf -- "$sd_workspace"
    [[ -z ${sd_log_scratch:-} ]] || rm -rf -- "$sd_log_scratch"
    if [[ -n ${sd_log_fd:-} ]]; then exec {sd_log_fd}>&-; sd_log_fd=''; fi
}

sd_interrupt() {
    printf 'sourcediff: interrupted by SIG%s\n' "$1" >&2
    sd_log_error . invocation "interrupted by SIG$1" || true
    sd_log_finish 2 || true
    exit 2
}

sd_create_workspace() {
    local path=$1 overwrite=$2
    shift 2
    sd_validate_destination "$path" "$overwrite" '' "$@" || return 2
    trap sd_cleanup EXIT
    trap 'sd_interrupt HUP' HUP
    trap 'sd_interrupt INT' INT
    trap 'sd_interrupt TERM' TERM
    sd_workspace=$(mktemp -d -- "${path%/*}/.sourcediff-XXXXXXXX") ||
        sd_error "could not create the output workspace for: $path"
}

sd_publish() {
    local staged=$1 dest=$2 overwrite=$3
    shift 3
    sd_validate_destination "$dest" "$overwrite" "$@" || return 2
    if [[ $overwrite == true ]]; then
        mv -fT -- "$staged" "$dest"
    else
        ln -T -- "$staged" "$dest"
    fi || sd_error "cannot publish output: $dest"
}
