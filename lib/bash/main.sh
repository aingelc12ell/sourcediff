#!/usr/bin/env bash
# shellcheck shell=bash
if (( BASH_VERSINFO[0] < 4 || ( BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 3 ) )); then
    printf 'sourcediff: Bash 4.3 or later is required\n' >&2
    exit 2
fi

sd_lib_dir=${BASH_SOURCE[0]%/*}
# shellcheck source=lib/bash/format.sh
source "$sd_lib_dir/format.sh" || exit 2
# shellcheck source=lib/bash/output.sh
source "$sd_lib_dir/output.sh" || exit 2
# shellcheck source=lib/bash/progress.sh
source "$sd_lib_dir/progress.sh" || exit 2
# shellcheck source=lib/bash/scan.sh
source "$sd_lib_dir/scan.sh" || exit 2

sd_help() {
    local command_name=$1
    case $command_name in
        catalog) printf 'Usage: sourcediff catalog TARGET [options]\n' ;;
        compare) printf 'Usage: sourcediff compare TARGET CATALOG [options]\n' ;;
        *)
            printf '%s\n' \
                'Usage: sourcediff [--engine python|bash] {catalog,compare} ...' \
                '  catalog     create a directory catalog' \
                '  compare     compare a directory with a complete catalog' \
                '  --help      display this help' \
                '  --version   display the tool and engine versions'
            return
            ;;
    esac
    printf '%s\n' \
        '  --engine python|bash        choose the execution engine' \
        '  --version                   display the tool and engine versions' \
        '  --output PATH               set the output path' \
        '  --overwrite                 replace an existing output file' \
        '  -v, --verbose               log operations and progress to stderr' \
        '  --log-file PATH             write persistent operational logs' \
        '  --jobs N                    set the maximum hashing worker count' \
        '  --fail-fast                 stop scheduling after a scan error' \
        '  --recursive / --no-recursive' \
        '  --max-depth N               limit depth (root is zero)' \
        '  --symlinks record|follow|skip (follow can scan outside TARGET)' \
        '  --directory-hashes / --no-directory-hashes' \
        '  --exclude GLOB / --clear-exclude' \
        '  --exclude-dir GLOB [GLOB ...] / --clear-exclude-dir' \
        '  --exclude-ext EXT [EXT ...] / --clear-exclude-ext' \
        '  Put all positional paths first for whitespace lists; each list stops at the next option.' \
        "  Or use --exclude-dir='a|b' / --exclude-ext='log|tmp' (shell-quoted)." \
        '  Attached groups consume only that argument and may precede positional paths.' \
        "  Repeat filters to append; separate 'a|b' is literal; clear resets at its position."
    if [[ $command_name == catalog ]]; then
        printf '%s\n' '  --hash sha256|blake3          choose the hash algorithm'
    else
        printf '%s\n' '  CATALOG                     path to a complete baseline catalog' \
            '  --strict                    count metadata changes' \
            '  --quick                     allow reuse of size- and mtime-matched file digests' \
            '  --differences-only          write counted changes, scan uncertainty and errors, and comparison totals'
    fi
}

sd_decimal_arg() {
    local _decimal_arg=$1 _decimal_min=$2
    [[ $_decimal_arg =~ ^[0-9]+$ ]] || return 2
    _decimal_arg=${_decimal_arg#"${_decimal_arg%%[!0]*}"}
    _decimal_arg=${_decimal_arg:-0}
    sd_integer "$_decimal_arg" "$_decimal_min" no || return 2
    printf -v "$3" '%s' "$_decimal_arg"
}

sd_positional_token() {
    [[ $1 != -* || $1 == - || $1 =~ ^-([0-9]+|[0-9]*\.[0-9]+)$ ]]
}

# Fill the command variables and scope override map owned by sd_main.
sd_parse_args() {
    local only_positional=false option value key encoded rest
    local -a values
    for option in "$@"; do
        case $option in
            -h|--help) sd_help ''; return 3 ;;
            --version) printf 'SourceDiff 1 (bash %s)\n' "$BASH_VERSION"; return 3 ;;
            catalog|compare) command_name=$option; shift; break ;;
            --) shift; break ;;
            *) sd_error "expected catalog or compare, got: $option"; return ;;
        esac
    done
    if [[ -z $command_name ]]; then
        case ${1:-} in
            catalog|compare) command_name=$1; shift ;;
            *) sd_error 'expected catalog or compare'; return ;;
        esac
    fi

    while (( $# )); do
        option=$1
        shift
        if [[ $only_positional == true ]] || sd_positional_token "$option"; then
            if [[ -z $target ]]; then
                [[ -n $option ]] || { sd_error 'TARGET must not be empty'; return; }
                target=$option
            elif [[ $command_name == compare && -z $baseline_path ]]; then
                [[ -n $option ]] || { sd_error 'CATALOG must not be empty'; return; }
                baseline_path=$option
            else
                sd_error "unexpected argument: $option"
                return
            fi
            continue
        fi
        case $option in
            --) only_positional=true; continue ;;
            -h|--help) sd_help "$command_name"; return 3 ;;
            --version) printf 'SourceDiff 1 (bash %s)\n' "$BASH_VERSION"; return 3 ;;
            --overwrite) overwrite=true; continue ;;
            -v|--verbose) sd_verbose=true; continue ;;
            --fail-fast) fail_fast=true; continue ;;
            --quick|--strict|--differences-only)
                [[ $command_name == compare ]] || { sd_error "unrecognized argument: $option"; return; }
                case $option in
                    --quick) quick=true ;;
                    --strict) strict=true ;;
                    --differences-only) differences_only=true ;;
                esac
                continue
                ;;
            --recursive|--no-recursive|--directory-hashes|--no-directory-hashes)
                key=${option#--}
                if [[ $key == no-* ]]; then
                    overrides[${key#no-}]=false
                else
                    overrides[$key]=true
                fi
                continue
                ;;
            --clear-exclude|--clear-exclude-dir|--clear-exclude-ext)
                overrides[${option#--clear-}]=
                continue
                ;;
        esac
        key=${option%%=*}
        case $key in
            --output|--hash|--jobs|--max-depth|--symlinks|--exclude|--exclude-dir|--exclude-ext|--log-file|--engine) ;;
            *) sd_error "unrecognized argument: $option"; return ;;
        esac
        if [[ $key == --exclude-dir || $key == --exclude-ext ]]; then
            values=()
            if [[ $option == *=* ]]; then
                rest=${option#*=}
                while [[ $rest == *'|'* ]]; do
                    values+=("${rest%%|*}")
                    rest=${rest#*|}
                done
                values+=("$rest")
            else
                while (( $# )) && sd_positional_token "$1"; do
                    values+=("$1")
                    shift
                done
            fi
            (( ${#values[@]} )) || { sd_error "$key needs a value"; return; }
            for value in "${values[@]}"; do
                [[ -n $value ]] || { sd_error "$key needs nonempty values (no empty pipe components)"; return; }
                sd_encode "$value" encoded
                overrides[${key#--}]+="$encoded$SD_LF"
            done
            continue
        fi
        if [[ $option == *=* ]]; then
            value=${option#*=}
        else
            if (( ! $# )) || ! sd_positional_token "$1"; then
                sd_error "$key needs a value"
                return
            fi
            value=$1
            shift
        fi
        [[ -n $value ]] || { sd_error "$key needs a nonempty value"; return; }
        case $key in
            --engine)
                # The launcher retains selectors that delimit a filter chain.
                [[ $value == python || $value == bash ]] || { sd_error '--engine expects python or bash'; return; }
                ;;
            --output) destination_arg=$value ;;
            --log-file) sd_log_arg=$value ;;
            --hash)
                [[ $command_name == catalog ]] || { sd_error "unrecognized argument: $key"; return; }
                sd_setting_value hash "$value" || { sd_error "$key expects sha256 or blake3"; return; }
                requested_hash=$value
                ;;
            --jobs|--max-depth)
                if [[ $key == --jobs ]]; then
                    sd_decimal_arg "$value" 1 jobs || { sd_error "invalid $key: $value"; return; }
                else
                    sd_decimal_arg "$value" 0 value || { sd_error "invalid $key: $value"; return; }
                    overrides[max-depth]=$value
                fi
                ;;
            --symlinks)
                sd_setting_value symlinks "$value" || { sd_error "$key expects record, follow, or skip"; return; }
                overrides[symlinks]=$value
                ;;
            --exclude)
                sd_encode "$value" encoded
                overrides[${key#--}]+="$encoded$SD_LF"
                ;;
        esac
    done
    [[ -n $target ]] || { sd_error 'TARGET is required'; return; }
    if [[ $command_name == compare && -z $baseline_path ]]; then
        sd_error 'CATALOG is required'
        return
    fi
    return 0
}

sd_effective_settings() {
    local -n _effective=$1 _overrides=$2
    local key
    _effective=([hash]="${requested_hash:-sha256}" [recursive]=true [max-depth]=- [symlinks]=record
                [directory-hashes]=false [exclude]='' [exclude-dir]='' [exclude-ext]='')
    if [[ -n ${3:-} ]]; then
        local -n _saved=$3
        for key in "${!_effective[@]}"; do
            # shellcheck disable=SC2004
            _effective[$key]=${_saved[$key]}
        done
    fi
    for key in "${!_overrides[@]}"; do
        # shellcheck disable=SC2004
        _effective[$key]=${_overrides[$key]}
    done
    # shellcheck disable=SC2154
    _effective[engine]=bash
    _effective[engine-version]=$BASH_VERSION
    _effective[tool-version]=1
}

sd_probe_tools() (
    local hash_algorithm=$1 command_mode=${2:-} target anchor link_target data fifo resolved observed stamp
    local probe
    probe=$(mktemp -d -- "${TMPDIR:-/tmp}/.sourcediff-probe-XXXXXXXX") ||
        { sd_error 'GNU mktemp -d must create a private probe directory'; return; }
    trap 'rm -rf -- "$probe"' EXIT

    if ! target=$(mktemp -d -- "$probe/target-XXXXXXXX") || ! data=$(mktemp -- "$target/record-XXXXXXXX"); then
        sd_error 'GNU mktemp must support directory and file templates'
        return
    fi
    anchor=$probe/anchor
    link_target=${target##*/}
    ln -s -- "$link_target" "$anchor" || { sd_error 'GNU ln must support symbolic links'; return; }
    printf '%s\n' 'sourcediff probe' >"$data" || return 2

    if ! find -H "$anchor" -mindepth 1 -maxdepth 1 -printf '%f\0' >"$probe/found" 2>/dev/null ||
        ! IFS= read -r -d '' observed <"$probe/found" || [[ $observed != "${data##*/}" ]]; then
        sd_error 'GNU find must support -H, -mindepth, -maxdepth, and NUL-terminated -printf output'
        return
    fi
    if ! IFS= read -r -d '' observed < <(readlink -z -- "$anchor" 2>/dev/null) || [[ $observed != "$link_target" ]]; then
        sd_error 'GNU readlink must support NUL-terminated -z output'
        return
    fi
    if ! IFS= read -r -d '' resolved < <(realpath -ez -- "$anchor" 2>/dev/null) || [[ ! $resolved -ef $target ]] ||
        ! IFS= read -r -d '' observed < <(realpath -mz -- "$anchor/missing" 2>/dev/null) ||
        [[ $observed != "$resolved/missing" ]]; then
        sd_error 'GNU realpath must support -ez and -mz for physical, NUL-terminated paths'
        return
    fi
    stamp='^[0-9]+:[0-9]+\|[0-9a-f]+\|[0-9]+\|-?[0-9]+\|[^|]+\.[0-9]{9} [^|]+\|-?[0-9]+\|[^|]+\.[0-9]{9} [^|]+$'
    if ! sd_stat "$anchor" true observed /dev/null || [[ ! $observed =~ $stamp ]]; then
        sd_error 'GNU stat -Lc must provide device, inode, mode, size, and nanosecond mtime and ctime'
        return
    fi
    if ! dd if="$target" of=/dev/null count=0 iflag=nonblock,nofollow,directory status=none 2>/dev/null ||
        ! dd if="$data" of=/dev/null count=0 iflag=nonblock,nofollow status=none 2>/dev/null; then
        sd_error 'GNU dd must support status=none and nonblock, nofollow, and directory input flags'
        return
    fi
    fifo=$probe/fifo
    if ! mkfifo -- "$fifo" || [[ ! -p $fifo ]]; then
        sd_error 'GNU mkfifo must create a named pipe'
        return
    fi
    if ! ln -T -- "$data" "$probe/link" || [[ ! $data -ef $probe/link ]]; then
        sd_error 'GNU ln must support -T for exclusive output publication'
        return
    fi
    if ! mv -fT -- "$probe/link" "$probe/output" || [[ ! $data -ef $probe/output ]]; then
        sd_error 'GNU mv must support -fT for atomic output replacement'
        return
    fi
    if [[ $command_mode == compare ]] && { ! mkdir -- "$probe/index" || [[ ! -d $probe/index ]]; }; then
        sd_error 'GNU mkdir must support -- for comparison indexes'
        return
    fi
    printf 'B\tsecond\nA\tfirst\n' >"$probe/keys" || return 2
    if ! sort -S 32M -T "$probe" -t "$SD_TAB" -k1,1 -k2,2 -- "$probe/keys" >"$probe/sorted" ||
        [[ $(<"$probe/sorted") != $'A\tfirst\nB\tsecond' ]]; then
        sd_error 'GNU sort must support -S, -T, -t, and -k for disk-backed record sorting'
        return
    fi
    if [[ $(cut -f2- -- "$probe/sorted") != $'first\nsecond' ]]; then
        sd_error 'GNU cut must support -f2- for reading sorted spools'
        return
    fi
    if ! grep -aq 'sourcediff probe' -- "$data"; then
        sd_error 'GNU grep must support -aq for validating catalog bytes'
        return
    fi
    if [[ $(cat -- "$data") != 'sourcediff probe' || $(wc -l <"$data") != 1 ]]; then
        sd_error 'GNU cat and wc -l must support catalog spool I/O'
        return
    fi
    if [[ $hash_algorithm == sha256 ]] &&
        { ! sha256sum - </dev/null >"$probe/hash" || ! sd_normalize_hash "$probe/hash" observed; }; then
        sd_error 'GNU sha256sum must return a SHA-256 digest from stdin'
        return
    fi
    if ! rm -rf -- "$target" || [[ -e $target ]]; then
        sd_error 'GNU rm must support -rf for private workspace cleanup'
        return
    fi
)

sd_require_tools() {
    local tool version
    local -a tools=(cat cut dd find grep ln mkfifo mktemp mv readlink realpath rm sort stat wc)
    if [[ ${1:-} == sha256 ]]; then tools+=(sha256sum); fi
    if [[ ${2:-} == compare ]]; then tools+=(mkdir); fi
    for tool in "${tools[@]}"; do
        version=$("$tool" --version 2>/dev/null) || { sd_error "GNU $tool is required"; return; }
        [[ $version == *GNU* ]] || { sd_error "GNU $tool is required"; return; }
    done
    [[ -d /proc/$BASHPID/fd ]] || { sd_error 'Linux /proc file descriptors are required'; return; }
    sd_probe_tools "${1:-}" "${2:-}"
}

sd_require_hash() {
    case $1 in
        sha256) command -v sha256sum >/dev/null || sd_error 'GNU sha256sum is required' ;;
        blake3) command -v b3sum >/dev/null || sd_error 'b3sum is required for BLAKE3; install b3sum and retry' ;;
    esac
}

sd_default_jobs() {
    local cpu_count
    if ! cpu_count=$(nproc 2>/dev/null) || [[ ! $cpu_count =~ ^[1-9][0-9]*$ ]]; then
        printf '1\n'
        return
    fi
    # Match small counts as strings: arbitrarily large detector output must
    # never enter shell arithmetic or overflow before the cap is applied.
    case $cpu_count in
        1|2|3) printf '%s\n' "$cpu_count" ;;
        *) printf '4\n' ;;
    esac
}

sd_main() {
    local command_name='' target='' requested_hash='' baseline_path='' destination_arg='' destination
    local overwrite=false fail_fast=false jobs status prefix current_dir
    local strict=false quick=false differences_only=false
    local -A settings overrides
    jobs=$(sd_default_jobs)
    sd_parse_args "$@"
    status=$?
    (( status != 3 )) || return 0
    (( status == 0 )) || return 2
    sd_log_enabled=$sd_verbose
    if [[ $target != /* || ( -n $baseline_path && $baseline_path != /* ) ]]; then
        # The sentinel preserves a final LF in the working directory's name.
        current_dir=$(pwd -P && printf '.') || { sd_error 'cannot resolve the working directory'; return; }
        current_dir=${current_dir%$'\n.'}
        if [[ $target != /* ]]; then target=${current_dir%/}/$target; fi
        if [[ -n $baseline_path && $baseline_path != /* ]]; then baseline_path=${current_dir%/}/$baseline_path; fi
    fi
    [[ -d $target ]] || { sd_error "target is not a directory: $target"; return; }
    sd_effective_settings settings overrides
    if [[ $command_name == catalog ]]; then
        sd_require_hash "${settings[hash]}" || return 2
        sd_require_tools "${settings[hash]}" || return 2
    fi
    if [[ -z $destination_arg ]]; then
        prefix=catalog
        if [[ $command_name == compare ]]; then prefix=difference; fi
        printf -v destination_arg '%s-%(%Y%m%d%H%M%S)T.txt' "$prefix" -1
    fi
    sd_exclusion_binding "$destination_arg" destination ||
        { sd_error "output must name a file in an existing directory: $destination_arg"; return; }

    if [[ $command_name == compare ]]; then
        # shellcheck source=lib/bash/compare.sh
        source "$sd_lib_dir/compare.sh" || return 2
        sd_require_tools '' compare || return 2
        sd_compare_main
        return
    fi

    sd_create_workspace "$destination" "$overwrite" "$target" || return 2
    sd_log_init '' || return 2
    sd_log_boundary scan || return 2
    local -a exclusions=("$destination")
    [[ -z $sd_log_path ]] || exclusions+=("$sd_log_path")
    sd_scan "$target" settings "$sd_workspace" "$jobs" "$fail_fast" '' '' "${exclusions[@]}" ||
        { sd_error 'could not complete the catalog scan'; return; }
    sd_log_boundary write || return 2
    sd_write_catalog "$sd_workspace/output" "$sd_workspace/entries" "$sd_workspace/errors" \
        "$sd_catalog_status" settings || { sd_error 'could not write the catalog'; return; }
    sd_log_boundary publish || return 2
    sd_publish "$sd_workspace/output" "$destination" "$overwrite" '' "$target" "${sd_log_protected_paths[@]}" || return 2
    [[ $sd_catalog_status == complete ]] && return 0
    return 2
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    sd_main "$@"
    sd_status=$?
    sd_log_finish "$sd_status" || sd_status=2
    exit "$sd_status"
fi
