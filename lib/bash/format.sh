# shellcheck shell=bash
export LC_ALL=C
shopt -u nocasematch

SD_TAB=$'\t'
SD_LF=$'\n'
SD_SCALAR_KEYS=(hash recursive max-depth symlinks directory-hashes engine engine-version tool-version complete)
SD_FILTER_KEYS=(exclude exclude-dir exclude-ext)
SD_MAX_INT=9223372036854775807

sd_encode() {
    local _enc_input=$1 _enc_result='' _enc_byte _enc_escape _enc_i
    for ((_enc_i=0; _enc_i<${#_enc_input}; _enc_i++)); do
        _enc_byte=${_enc_input:_enc_i:1}
        case $_enc_byte in
            %) _enc_result+=%25 ;;
            [' '-'~']) _enc_result+=$_enc_byte ;;
            *)
                printf -v _enc_escape '%%%02X' "'$_enc_byte"
                _enc_result+=$_enc_escape
                ;;
        esac
    done
    printf -v "$2" '%s' "$_enc_result"
}

sd_decode() {
    local _dec_input=$1 _dec_result='' _dec_prefix _dec_hex _dec_byte
    while [[ $_dec_input == *%* ]]; do
        _dec_prefix=${_dec_input%%\%*}
        [[ $_dec_prefix != *[^' '-'~']* ]] || return 2
        _dec_input=${_dec_input#*%}
        _dec_hex=${_dec_input:0:2}
        [[ $_dec_hex =~ ^[0-9A-F]{2}$ && $_dec_hex != 00 ]] || return 2
        if (( 16#$_dec_hex >= 32 && 16#$_dec_hex <= 126 )) && [[ $_dec_hex != 25 ]]; then
            return 2
        fi
        printf -v _dec_byte '%b' "\\x$_dec_hex"
        _dec_result+=$_dec_prefix$_dec_byte
        _dec_input=${_dec_input:2}
    done
    [[ $_dec_input != *[^' '-'~']* ]] || return 2
    printf -v "$2" '%s' "$_dec_result$_dec_input"
}

sd_hex_key() {
    local _hex_input=$1 _hex_result='' _hex_pair _hex_byte _hex_i
    for ((_hex_i=0; _hex_i<${#_hex_input}; _hex_i++)); do
        _hex_byte=${_hex_input:_hex_i:1}
        printf -v _hex_pair '%02X' "'$_hex_byte"
        _hex_result+=$_hex_pair
    done
    printf -v "$2" '%s' "$_hex_result"
}

sd_path_field() {
    local _path_decoded
    sd_decode "$1" _path_decoded || return
    case /$_path_decoded/ in
        /./) ;;
        *//*|*/./*|*/../*) return 2 ;;
    esac
    printf -v "$2" '%s' "$_path_decoded"
}

sd_integer() {
    local number=$1 minimum=$2 optional=$3 magnitude negative_limit=9223372036854775808
    [[ $optional == yes && $number == - ]] && return 0
    [[ $number =~ ^(0|-?[1-9][0-9]{0,18})$ ]] || return 2
    if [[ $number == -* ]]; then
        [[ $minimum == signed ]] || return 2
        magnitude=${number#-}
        # The minimum signed integer has a magnitude too large for shell arithmetic.
        # shellcheck disable=SC2071
        [[ ${#magnitude} -lt 19 || $magnitude < $negative_limit || $magnitude == "$negative_limit" ]]
    else
        # Compare validated, equal-width decimal strings to prevent overflow.
        # shellcheck disable=SC2071
        [[ ${#number} -lt 19 || $number < $SD_MAX_INT || $number == "$SD_MAX_INT" ]] || return 2
        [[ $minimum != 1 || $number != 0 ]]
    fi
}

sd_digest_field() {
    [[ $1 =~ ^[0-9a-f]{64}$ ]]
}

sd_split_fields() {
    local _split_line=$1
    local -n _split_fields=$2
    _split_fields=()
    while [[ $_split_line == *"$SD_TAB"* ]]; do
        _split_fields+=("${_split_line%%"$SD_TAB"*}")
        _split_line=${_split_line#*"$SD_TAB"}
    done
    _split_fields+=("$_split_line")
}

sd_setting_value() {
    [[ -n $2 ]] || return 2
    case $1 in
        hash) [[ $2 == sha256 || $2 == blake3 ]] ;;
        recursive|directory-hashes) [[ $2 == true || $2 == false ]] ;;
        max-depth) sd_integer "$2" 0 yes ;;
        symlinks) [[ $2 == record || $2 == follow || $2 == skip ]] ;;
        engine) [[ $2 == python || $2 == bash ]] ;;
        complete) [[ $2 == complete || $2 == incomplete ]] ;;
        engine-version|tool-version|exclude|exclude-dir|exclude-ext) return 0 ;;
        *) return 2 ;;
    esac
}

sd_catalog_format_error() {
    printf 'sourcediff: invalid catalog: %s\n' "$1" >&2
    return 2
}

# List slots store canonical escaped values, each terminated by LF.
# Scalar slots store raw bytes.
sd_parse_catalog_rows() {
    local -n _parsed_settings=$1
    local _parsed_entries=$2 _parsed_errors=$3 _require_complete=$4
    local row tag key value phase=S previous='' path kind digest verification target root=0
    local entry_count=0 error_count=0
    local -a fields
    _parsed_settings=([exclude]='' [exclude-dir]='' [exclude-ext]='')
    if ! IFS= read -r row || [[ $row != "SOURCEDIFF${SD_TAB}1" ]]; then
        sd_catalog_format_error 'missing or unsupported version'
        return
    fi
    while IFS= read -r row; do
        sd_split_fields "$row" fields
        tag=${fields[0]}
        if [[ $phase == S && $tag != S ]]; then
            for key in "${SD_SCALAR_KEYS[@]}"; do
                [[ -n ${_parsed_settings[$key]+set} ]] ||
                    { sd_catalog_format_error "missing setting $key"; return; }
            done
            if [[ $_require_complete == yes && ${_parsed_settings[complete]} != complete ]]; then
                sd_catalog_format_error 'comparison baseline is incomplete'
                return
            fi
            phase=E
        fi
        case $tag in
            S)
                [[ $phase == S && ${#fields[@]} -eq 3 ]] || return 2
                key=${fields[1]}
                sd_decode "${fields[2]}" value && sd_setting_value "$key" "$value" || return 2
                case $key in
                    exclude|exclude-dir|exclude-ext)
                        # shellcheck disable=SC2004
                        _parsed_settings[$key]+="${fields[2]}$SD_LF"
                        ;;
                    *)
                        [[ -z ${_parsed_settings[$key]+set} ]] || return 2
                        # shellcheck disable=SC2004
                        _parsed_settings[$key]=$value
                        ;;
                esac
                ;;
            E)
                [[ $phase == E && ${#fields[@]} -eq 9 ]] || return 2
                sd_path_field "${fields[1]}" path || return 2
                [[ -z $previous || $previous < $path ]] || return 2
                previous=$path
                kind=${fields[2]} digest=${fields[3]} verification=${fields[8]}
                if [[ $path == . ]]; then
                    [[ $kind == directory ]] || return 2
                    root=1
                fi
                case $kind in
                    file|directory) [[ ${fields[7]} == - ]] || return 2 ;;
                    symlink)
                        [[ ${_parsed_settings[symlinks]} == record && $digest != - ]] || return 2
                        sd_decode "${fields[7]}" target && [[ -n $target ]] || return 2
                        ;;
                    *) return 2 ;;
                esac
                if [[ $digest == - ]]; then
                    [[ $verification == unverified ]] || return 2
                    if [[ ${_parsed_settings[complete]} == complete &&
                          ( $kind == file || ${_parsed_settings[directory-hashes]} == true ) ]]; then
                        return 2
                    fi
                else
                    sd_digest_field "$digest" || return 2
                    [[ $kind != directory || ${_parsed_settings[directory-hashes]} == true ]] || return 2
                fi
                case $verification in
                    verified|unverified) ;;
                    reused) [[ $kind == file ]] || return 2 ;;
                    *) return 2 ;;
                esac
                sd_integer "${fields[4]}" 0 yes && sd_integer "${fields[5]}" signed yes &&
                    sd_integer "${fields[6]}" 1 yes || return 2
                [[ ${fields[5]} != - || ${fields[6]} == - ]] || return 2
                printf '%s\n' "$row" >&"$_parsed_entries" || return 2
                ((entry_count+=1))
                ;;
            X)
                [[ $phase != END && ${#fields[@]} -eq 5 ]] || return 2
                sd_path_field "${fields[1]}" path && sd_decode "${fields[2]}" value &&
                    [[ -n $value ]] && sd_decode "${fields[3]}" value || return 2
                [[ ${fields[4]} == entry || ${fields[4]} == subtree ]] || return 2
                phase=X
                printf '%s\n' "$row" >&"$_parsed_errors" || return 2
                ((error_count+=1))
                ;;
            END)
                [[ $phase != END && ${#fields[@]} -eq 4 &&
                    ${fields[1]} == "${_parsed_settings[complete]}" &&
                    ${fields[2]} == "$entry_count" && ${fields[3]} == "$error_count" ]] || return 2
                if [[ ${_parsed_settings[complete]} == complete ]]; then
                    (( root && !error_count )) || return 2
                fi
                phase=END
                ;;
            *) return 2 ;;
        esac
    done
    [[ -z $row && $phase == END ]]
}

# Read an open descriptor at its current position and leave it open. GNU grep
# checks the complete raw stream before Bash read, which otherwise ignores NUL.
sd_read_catalog_stream() {
    local fd=$1 entries=$2 errors=$3 settings_ref=$4 require_complete=${5:-no}
    local raw status=0 spool_parent=.
    [[ $entries != */* ]] || spool_parent=${entries%/*}
    raw=$(mktemp -- "${spool_parent:-/}/.catalog-input-XXXXXXXX") || return 2
    if cat <&"$fd" >"$raw"; then
        if grep -aq "[^${SD_TAB} -~]" -- "$raw"; then
            status=2
        elif [[ $? != 1 ]]; then
            status=2
        else
            sd_parse_catalog_rows "$settings_ref" 3 4 "$require_complete" <"$raw" 3>"$entries" 4>"$errors" ||
                status=2
        fi
    else
        status=2
    fi
    rm -f -- "$raw"
    if (( status )); then
        rm -f -- "$entries" "$errors"
        sd_catalog_format_error 'malformed record, byte field, sequence, or trailer'
    fi
}

sd_read_catalog() {
    local _source_fd _status=0
    exec {_source_fd}<"$1" || return 2
    sd_read_catalog_stream "$_source_fd" "$2" "$3" "$4" "${5:-no}" || _status=$?
    exec {_source_fd}<&-
    return "$_status"
}

# Write catalog-order settings to stdout for either artifact.
sd_write_settings() {
    local -n _write_settings=$1
    local completeness=$2 key value
    for key in "${SD_SCALAR_KEYS[@]}"; do
        if [[ $key == complete ]]; then
            value=$completeness
        else
            sd_encode "${_write_settings[$key]}" value
        fi
        printf 'S\t%s\t%s\n' "$key" "$value" || return 2
    done
    for key in "${SD_FILTER_KEYS[@]}"; do
        while IFS= read -r value && [[ -n $value ]]; do
            printf 'S\t%s\t%s\n' "$key" "$value" || return 2
        done <<< "${_write_settings[$key]}"
    done
}

sd_write_catalog() {
    local staged=$1 entries=$2 errors=$3 completeness=$4 entry_count error_count
    entry_count=$(wc -l <"$entries") && error_count=$(wc -l <"$errors") || return 2
    {
        printf 'SOURCEDIFF\t1\n' || return 2
        sd_write_settings "$5" "$completeness" || return 2
        cat -- "$entries" "$errors" || return 2
        printf 'END\t%s\t%s\t%s\n' "$completeness" "$entry_count" "$error_count"
    } >"$staged"
}
