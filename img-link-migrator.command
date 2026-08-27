#!/bin/zsh

emulate -LR zsh
setopt PIPE_FAIL
setopt EXTENDED_GLOB

readonly APP_NAME="IMG Link Migrator Standalone"
readonly APP_VERSION="0.6.0"
readonly USER_AGENT="IMG-Link-Migrator-Standalone/${APP_VERSION}"
readonly MAX_IMAGE_BYTES=33554432
readonly DEFAULT_HOST="xhscdn.com"
readonly MAX_ATTEMPTS=4
readonly DEFAULT_PARALLEL_TRANSFERS=3
readonly UPLOADS_PER_MINUTE=50
readonly DEFAULT_UPLOAD_INTERVAL_SECONDS=1.21
readonly UPLOAD_RATE_COOLDOWN_SECONDS=60
readonly CHEVERETO_URL="https://www.picgo.net"

typeset -a target_files
typeset -a urls
typeset -a source_hosts
typeset -A fingerprints
typeset -A url_file_indexes
typeset -i reference_count=0

api_key=""
target_path=""
all_hosts=false
provider="chevereto"
provider_name="PicGo.net"
cache_namespace="chevereto:${CHEVERETO_URL}"
parallel_transfers=$DEFAULT_PARALLEL_TRANSFERS
upload_interval_seconds=$DEFAULT_UPLOAD_INTERVAL_SECONDS
downloaded_mime="application/octet-stream"
downloaded_filename="image.bin"
state_dir="${HOME}/Library/Application Support/${APP_NAME}"
cache_file="${state_dir}/url-map.tsv"
content_cache_file="${state_dir}/content-map.tsv"
work_dir=""
migration_stop_requested=false
migration_stop_announced=false
test_transfer_mode=false
test_transfer_delay=0
transfer_label=""

cleanup() {
  if [[ -n "$work_dir" && -d "$work_dir" ]]; then
    /bin/rm -rf -- "$work_dir"
  fi
}

trap cleanup EXIT
trap 'exit 130' INT TERM

fail() {
  print -u2 -r -- "Error: $1"
  return 1
}

request_migration_stop() {
  migration_stop_requested=true
  if [[ "$migration_stop_announced" == false ]]; then
    migration_stop_announced=true
    print -u2 -r -- "Stop requested. Finishing active transfers and their file updates..."
  fi
}

log_transfer_error() {
  print -u2 -r -- "${transfer_label:+${transfer_label} }$1"
}

pause_before_exit() {
  if [[ -t 0 ]]; then
    print
    read -r "?Press Return to close..."
  fi
}

file_hash() {
  LC_ALL=C /usr/bin/shasum -a 256 -- "$1" | /usr/bin/awk '{print $1}'
}

host_is_destination() {
  local host="${1:l}"

  if [[ "$provider" == "imgbb" ]]; then
    [[ "$host" == "ibb.co" || "$host" == *."ibb.co" ]]
  else
    [[ "$host" == "picgo.net" || "$host" == *."picgo.net" ]]
  fi
}

host_is_selected() {
  local host="${1:l}"
  local rule

  host_is_destination "$host" && return 1
  [[ "$all_hosts" == true ]] && return 0

  for rule in "${source_hosts[@]}"; do
    if [[ "$host" == "$rule" || "$host" == *."$rule" ]]; then
      return 0
    fi
  done
  return 1
}

url_is_selected() {
  local url="$1"
  local host

  if [[ "$url" =~ '^https?://([^/:?#]+)' ]]; then
    host="${match[1]:l}"
    host_is_selected "$host"
    return $?
  fi
  return 1
}

trim_url_suffix() {
  local value="$1"

  while [[ "$value" == *')' || "$value" == *']' || "$value" == *'}' || \
    "$value" == *',' || "$value" == *';' || "$value" == *'.' ]]; do
    value="${value[1,-2]}"
  done
  print -r -- "$value"
}

collect_target_files() {
  local candidate
  local extension
  target_files=()

  if [[ -f "$target_path" ]]; then
    extension="${target_path:e:l}"
    if [[ "$extension" != "txt" && "$extension" != "md" && "$extension" != "markdown" ]]; then
      fail "Select a .txt, .md, or .markdown file."
      return 1
    fi
    target_files+=("${target_path:A}")
    return 0
  fi

  if [[ ! -d "$target_path" ]]; then
    fail "The selected target does not exist."
    return 1
  fi

  while IFS= read -r -d '' candidate; do
    target_files+=("${candidate:A}")
  done < <(
    /usr/bin/find "$target_path" \
      -type d -name '.*' -prune -o \
      -type f ! -name '.*' \
      \( -iname '*.txt' -o -iname '*.md' -o -iname '*.markdown' \) \
      -print0
  )
}

collect_urls() {
  local file
  local url
  local indexed_files
  local -i file_index=0

  urls=()
  fingerprints=()
  url_file_indexes=()
  reference_count=0

  for file in "${target_files[@]}"; do
    (( file_index += 1 ))
    fingerprints[$file]="$(file_hash "$file")"
    while IFS= read -r url; do
      url="$(trim_url_suffix "$url")"
      if url_is_selected "$url"; then
        (( reference_count += 1 ))
        indexed_files="${url_file_indexes[$url]-}"
        if [[ " $indexed_files " != *" $file_index "* ]]; then
          url_file_indexes[$url]="${indexed_files:+${indexed_files} }${file_index}"
        fi
        if (( ${urls[(Ie)$url]} == 0 )); then
          urls+=("$url")
        fi
      fi
    done < <(
      LC_ALL=C /usr/bin/grep -aEo "https?://[^[:space:]<>\"']+" "$file" || true
    )
  done
}

lookup_cache() {
  local wanted="$1"
  local namespace
  local original
  local migrated
  local third

  [[ -f "$cache_file" ]] || return 1
  while IFS=$'\t' read -r namespace original third; do
    if [[ -n "$third" ]]; then
      migrated="$third"
    else
      migrated="$original"
      original="$namespace"
      namespace="imgbb"
    fi
    if [[ "$namespace" == "$cache_namespace" && "$original" == "$wanted" && -n "$migrated" ]]; then
      print -r -- "$migrated"
      return 0
    fi
  done < "$cache_file"
  return 1
}

record_cache() {
  local original="$1"
  local migrated="$2"

  /bin/mkdir -p -- "$state_dir" || return 1
  /usr/bin/touch "$cache_file" || return 1
  /bin/chmod 600 "$cache_file" || return 1
  print -r -- "${cache_namespace}"$'\t'"${original}"$'\t'"${migrated}" >> "$cache_file"
}

lookup_content_cache() {
  local wanted_digest="$1"
  local namespace
  local digest
  local migrated

  [[ -f "$content_cache_file" ]] || return 1
  while IFS=$'\t' read -r namespace digest migrated; do
    if [[ "$namespace" == "$cache_namespace" && \
      "$digest" == "$wanted_digest" && -n "$migrated" ]]; then
      print -r -- "$migrated"
      return 0
    fi
  done < "$content_cache_file"
  return 1
}

acquire_content_lock() {
  local digest="$1"
  local lock_dir="${work_dir}/content-${digest}.lock"

  while ! /bin/mkdir -- "$lock_dir" 2>/dev/null; do
    /bin/sleep 0.02
  done
}

release_content_lock() {
  local digest="$1"
  /bin/rmdir -- "${work_dir}/content-${digest}.lock" 2>/dev/null || true
}

acquire_content_cache_write_lock() {
  local lock_dir="${work_dir}/content-cache-write.lock"

  while ! /bin/mkdir -- "$lock_dir" 2>/dev/null; do
    /bin/sleep 0.02
  done
}

release_content_cache_write_lock() {
  /bin/rmdir -- "${work_dir}/content-cache-write.lock" 2>/dev/null || true
}

record_content_cache() {
  local digest="$1"
  local migrated="$2"
  local write_status=0

  /bin/mkdir -p -- "$state_dir" || return 1
  acquire_content_cache_write_lock || return 1
  /usr/bin/touch "$content_cache_file" || write_status=1
  if (( write_status == 0 )); then
    /bin/chmod 600 "$content_cache_file" || write_status=1
  fi
  if (( write_status == 0 )) && \
    ! lookup_content_cache "$digest" >/dev/null; then
    print -r -- "${cache_namespace}"$'\t'"${digest}"$'\t'"${migrated}" \
      >> "$content_cache_file" || write_status=1
  fi
  release_content_cache_write_lock
  return "$write_status"
}

sleep_before_retry() {
  local attempt="$1"
  local delay=$(( 1 << (attempt - 1) ))
  log_transfer_error "Retrying in ${delay} second(s)..."
  /bin/sleep "$delay"
}

current_time_seconds() {
  LC_ALL=C /usr/bin/perl -MTime::HiRes=time -e 'printf "%.6f\n", time'
}

acquire_upload_rate_lock() {
  local lock_dir="${work_dir}/upload-rate.lock"

  while ! /bin/mkdir -- "$lock_dir" 2>/dev/null; do
    /bin/sleep 0.02
  done
}

release_upload_rate_lock() {
  /bin/rmdir -- "${work_dir}/upload-rate.lock" 2>/dev/null || true
}

write_next_upload_time() {
  local value="$1"
  local state_file="${work_dir}/next-upload-time"
  local temp_file="${state_file}.tmp"

  print -r -- "$value" > "$temp_file" || return 1
  /bin/mv -f -- "$temp_file" "$state_file"
}

wait_for_upload_slot() {
  local state_file="${work_dir}/next-upload-time"
  local next_time=0
  local now
  local delay

  acquire_upload_rate_lock || return 1
  if [[ -f "$state_file" ]]; then
    IFS= read -r next_time < "$state_file"
  fi
  now="$(current_time_seconds)" || {
    release_upload_rate_lock
    return 1
  }
  delay="$(
    LC_ALL=C /usr/bin/perl -e '
      my ($next, $now) = @ARGV;
      my $delay = $next - $now;
      printf "%.6f\n", $delay > 0 ? $delay : 0;
    ' "$next_time" "$now"
  )" || {
    release_upload_rate_lock
    return 1
  }
  if [[ "$delay" != "0.000000" ]]; then
    /bin/sleep "$delay"
  fi
  now="$(current_time_seconds)" || {
    release_upload_rate_lock
    return 1
  }
  next_time="$(
    LC_ALL=C /usr/bin/perl -e 'printf "%.6f\n", $ARGV[0] + $ARGV[1]' \
      "$now" "$upload_interval_seconds"
  )" || {
    release_upload_rate_lock
    return 1
  }
  write_next_upload_time "$next_time"
  local write_status=$?
  release_upload_rate_lock
  return "$write_status"
}

defer_uploads() {
  local seconds="$1"
  local state_file="${work_dir}/next-upload-time"
  local next_time=0
  local now
  local deferred_time

  acquire_upload_rate_lock || return 1
  if [[ -f "$state_file" ]]; then
    IFS= read -r next_time < "$state_file"
  fi
  now="$(current_time_seconds)" || {
    release_upload_rate_lock
    return 1
  }
  deferred_time="$(
    LC_ALL=C /usr/bin/perl -e '
      my ($current, $now, $seconds) = @ARGV;
      my $candidate = $now + $seconds;
      printf "%.6f\n", $current > $candidate ? $current : $candidate;
    ' "$next_time" "$now" "$seconds"
  )" || {
    release_upload_rate_lock
    return 1
  }
  write_next_upload_time "$deferred_time"
  local write_status=$?
  release_upload_rate_lock
  return "$write_status"
}

is_upload_rate_error() {
  local message="${1:l}"
  [[ "$message" == *"flood"* || "$message" == *"rate limit"* || \
    "$message" == *"too many requests"* ]]
}

is_duplicate_upload_error() {
  local message="${1:l}"
  [[ "$message" == *"duplicated upload"* || \
    "$message" == *"duplicate upload"* ]]
}

chevereto_response_url() {
  local response_file="$1"
  local status_code
  local migrated_url
  local error_message

  status_code="$(
    /usr/bin/plutil -extract status_code raw -o - "$response_file" 2>/dev/null
  )"
  migrated_url="$(
    /usr/bin/plutil -extract image.url raw -o - "$response_file" 2>/dev/null
  )"
  error_message="$(
    /usr/bin/plutil -extract error.message raw -o - "$response_file" 2>/dev/null
  )"
  [[ "$migrated_url" == http://* || "$migrated_url" == https://* ]] || return 1
  if [[ "$status_code" == "200" ]] || is_duplicate_upload_error "$error_message"; then
    print -r -- "$migrated_url"
    return 0
  fi
  return 1
}

handle_upload_rate_error() {
  local message="$1"

  if is_upload_rate_error "$message"; then
    log_transfer_error \
      "Upload limit reached. Pausing all uploads for ${UPLOAD_RATE_COOLDOWN_SECONDS} seconds."
    defer_uploads "$UPLOAD_RATE_COOLDOWN_SECONDS"
    return 0
  fi
  return 1
}

download_image() {
  local url="$1"
  local output_file="$2"
  local host=""
  local attempt
  local size
  local mime
  local extension
  local -a curl_args

  if [[ "$url" =~ '^https?://([^/:?#]+)' ]]; then
    host="${match[1]:l}"
  fi

  for (( attempt = 1; attempt <= MAX_ATTEMPTS; attempt++ )); do
    curl_args=(
      --location
      --fail
      --silent
      --show-error
      --connect-timeout 15
      --max-time 90
      --max-filesize "$MAX_IMAGE_BYTES"
      --user-agent "$USER_AGENT"
      --header 'Accept: image/*,*/*;q=0.8'
      --output "$output_file"
    )
    if [[ "$host" == "xhscdn.com" || "$host" == *."xhscdn.com" ]]; then
      curl_args+=(--referer 'https://www.xiaohongshu.com/')
    fi

    : > "$output_file"
    if /usr/bin/curl "${curl_args[@]}" -- "$url"; then
      size="$(/usr/bin/stat -f '%z' "$output_file" 2>/dev/null || print 0)"
      mime="$(/usr/bin/file -b --mime-type "$output_file" 2>/dev/null)"
      if (( size > 0 && size <= MAX_IMAGE_BYTES )) && [[ "$mime" == image/* ]]; then
        downloaded_mime="$mime"
        extension="${mime#image/}"
        extension="${extension%%+*}"
        [[ "$extension" == "jpeg" ]] && extension="jpg"
        [[ "$extension" =~ '^[A-Za-z0-9]+$' ]] || extension="img"
        downloaded_filename="image.${extension}"
        return 0
      fi
      log_transfer_error "Downloaded content is not a valid image or exceeds 32 MB."
    else
      log_transfer_error "Download attempt ${attempt} failed."
    fi

    (( attempt < MAX_ATTEMPTS )) && sleep_before_retry "$attempt"
  done
  return 1
}

upload_imgbb() {
  local image_file="$1"
  local response_file="$2"
  local attempt
  local curl_status
  local success
  local migrated_url
  local error_message
  local rate_limited

  for (( attempt = 1; attempt <= MAX_ATTEMPTS; attempt++ )); do
    rate_limited=false
    wait_for_upload_slot || return 1
    {
      print -r -- 'silent'
      print -r -- 'show-error'
      print -r -- 'connect-timeout = 15'
      print -r -- 'max-time = 90'
      print -r -- 'request = "POST"'
      print -r -- "url = \"https://api.imgbb.com/1/upload?key=${api_key}\""
    } | /usr/bin/curl \
      --config - \
      --form "image=@${image_file};filename=${downloaded_filename};type=${downloaded_mime}" \
      --output "$response_file"
    curl_status=$?

    if (( curl_status == 0 )); then
      success="$(
        /usr/bin/plutil -extract success raw -o - "$response_file" 2>/dev/null
      )"
      if [[ "$success" == "true" ]]; then
        migrated_url="$(
          /usr/bin/plutil -extract data.url raw -o - "$response_file" 2>/dev/null
        )"
        if [[ "$migrated_url" == https://* ]]; then
          print -r -- "$migrated_url"
          return 0
        fi
      fi

      error_message="$(
        /usr/bin/plutil -extract error.message raw -o - "$response_file" 2>/dev/null
      )"
      if [[ -n "$error_message" ]]; then
        log_transfer_error "ImgBB: $error_message"
        if handle_upload_rate_error "$error_message"; then
          rate_limited=true
        fi
      fi
    else
      log_transfer_error "Upload attempt ${attempt} failed."
    fi

    if (( attempt < MAX_ATTEMPTS )) && [[ "$rate_limited" == false ]]; then
      sleep_before_retry "$attempt"
    fi
  done
  return 1
}

upload_chevereto() {
  local image_file="$1"
  local response_file="$2"
  local attempt
  local curl_status
  local migrated_url
  local error_message
  local rate_limited

  for (( attempt = 1; attempt <= MAX_ATTEMPTS; attempt++ )); do
    rate_limited=false
    wait_for_upload_slot || return 1
    {
      print -r -- 'silent'
      print -r -- 'show-error'
      print -r -- 'connect-timeout = 15'
      print -r -- 'max-time = 90'
      print -r -- 'request = "POST"'
      print -r -- 'header = "Accept: application/json"'
      print -r -- "header = \"X-API-Key: ${api_key}\""
      print -r -- "url = \"${CHEVERETO_URL}/api/1/upload\""
    } | /usr/bin/curl \
      --config - \
      --form "source=@${image_file};filename=${downloaded_filename};type=${downloaded_mime}" \
      --form 'format=json' \
      --output "$response_file"
    curl_status=$?

    if (( curl_status == 0 )); then
      migrated_url="$(chevereto_response_url "$response_file")"
      error_message="$(
        /usr/bin/plutil -extract error.message raw -o - "$response_file" 2>/dev/null
      )"
      if [[ -n "$migrated_url" ]]; then
        if is_duplicate_upload_error "$error_message"; then
          log_transfer_error \
            "PicGo.net: duplicate content found; reusing the existing image URL."
        fi
        print -r -- "$migrated_url"
        return 0
      fi
      if [[ -n "$error_message" ]]; then
        log_transfer_error "PicGo.net: $error_message"
        if handle_upload_rate_error "$error_message"; then
          rate_limited=true
        fi
      fi
    else
      log_transfer_error "Upload attempt ${attempt} failed."
    fi

    if (( attempt < MAX_ATTEMPTS )) && [[ "$rate_limited" == false ]]; then
      sleep_before_retry "$attempt"
    fi
  done
  return 1
}

upload_image() {
  if [[ "$provider" == "imgbb" ]]; then
    upload_imgbb "$@"
  else
    upload_chevereto "$@"
  fi
}

write_transfer_result() {
  local result_file="$1"
  local transfer_status="$2"
  local migrated_url="${3:-}"
  local temp_result="${result_file}.tmp"

  print -r -- "${transfer_status}"$'\t'"${migrated_url}" > "$temp_result" || return 1
  /bin/mv -f -- "$temp_result" "$result_file"
}

transfer_url_worker() {
  local url="$1"
  local item_index="$2"
  local total="$3"
  local result_file="$4"
  local image_file="${work_dir}/transfer-${item_index}.image"
  local response_file="${work_dir}/transfer-${item_index}.json"
  local digest
  local duplicate_message
  local migrated_url
  local transfer_status="uploaded"

  transfer_label="[${item_index}/${total}]"

  if [[ "$test_transfer_mode" == true ]]; then
    /bin/sleep "$test_transfer_delay"
    print -r -- "[${item_index}/${total}] Uploading: $url"
    write_transfer_result \
      "$result_file" "uploaded" "https://test.invalid/image-${item_index}"
    return $?
  fi

  if ! download_image "$url" "$image_file"; then
    write_transfer_result "$result_file" "download_failed"
    return 1
  fi

  digest="$(file_hash "$image_file")" || {
    write_transfer_result "$result_file" "download_failed"
    return 1
  }
  acquire_content_lock "$digest" || {
    write_transfer_result "$result_file" "worker_failed"
    return 1
  }
  migrated_url="$(lookup_content_cache "$digest")"
  if [[ -n "$migrated_url" ]]; then
    print -r -- "[${item_index}/${total}] Reusing identical content: $migrated_url"
    release_content_lock "$digest"
    write_transfer_result "$result_file" "reused" "$migrated_url"
    return $?
  fi

  print -r -- "[${item_index}/${total}] Uploading: $url"
  migrated_url="$(upload_image "$image_file" "$response_file")"
  if [[ -z "$migrated_url" ]]; then
    release_content_lock "$digest"
    write_transfer_result "$result_file" "upload_failed"
    return 1
  fi

  if [[ "$provider" == "chevereto" ]]; then
    duplicate_message="$(
      /usr/bin/plutil -extract error.message raw -o - "$response_file" 2>/dev/null
    )"
    if is_duplicate_upload_error "$duplicate_message"; then
      transfer_status="reused"
    fi
  fi
  if ! record_content_cache "$digest" "$migrated_url"; then
    log_transfer_error "Warning: could not record the content cache."
  fi
  release_content_lock "$digest"
  write_transfer_result "$result_file" "$transfer_status" "$migrated_url"
}

replace_url_in_file() {
  local file="$1"
  local original_url="$2"
  local migrated_url="$3"
  local current_hash
  local temp_file
  local mode

  [[ -n "${fingerprints[$file]-}" ]] || return 0
  /usr/bin/grep -aFq -- "$original_url" "$file" || return 0

  current_hash="$(file_hash "$file")"
  if [[ "$current_hash" != "${fingerprints[$file]}" ]]; then
    print -u2 -r -- "  Warning: file changed after scanning; skipped: $file"
    return 1
  fi

  temp_file="$(/usr/bin/mktemp "${file}.img-link-migrator.XXXXXX")" || return 1
  if ! LC_ALL=C OLD_URL="$original_url" NEW_URL="$migrated_url" \
    /usr/bin/perl -0777 -pe \
      's/\Q$ENV{OLD_URL}\E/$ENV{NEW_URL}/g' -- "$file" > "$temp_file"; then
    /bin/rm -f -- "$temp_file"
    return 1
  fi

  mode="$(/usr/bin/stat -f '%Lp' "$file")"
  /bin/chmod "$mode" "$temp_file" || {
    /bin/rm -f -- "$temp_file"
    return 1
  }
  /bin/mv -f -- "$temp_file" "$file" || {
    /bin/rm -f -- "$temp_file"
    return 1
  }

  fingerprints[$file]="$(file_hash "$file")"
  print -r -- "  Updated: $file"
}

replace_url_in_indexed_files() {
  local original_url="$1"
  local migrated_url="$2"
  local file
  local file_index
  local -a matching_file_indexes
  local write_failed=false

  matching_file_indexes=(${=url_file_indexes[$original_url]})
  for file_index in "${matching_file_indexes[@]}"; do
    file="${target_files[$file_index]}"
    if ! replace_url_in_file "$file" "$original_url" "$migrated_url"; then
      write_failed=true
    fi
  done
  [[ "$write_failed" == false ]]
}

prompt_settings() {
  local service_input
  local target_input
  local source_input
  local host

  print -r -- "${APP_NAME} ${APP_VERSION}"
  print -r -- "Migrates image URLs in .txt, .md, and .markdown files."
  print

  print -r -- "Choose the upload service:"
  print -r -- "  Press Return or type 1 for PicGo.net (Chevereto API v1)."
  print -r -- "  Type 2 for ImgBB."
  read -r "service_input?Your choice [1]: "
  service_input="${service_input:l}"
  case "$service_input" in
    ""|1|picgo|picgo.net|chevereto)
      provider="chevereto"
      provider_name="PicGo.net"
      cache_namespace="chevereto:${CHEVERETO_URL}"
      ;;
    2|imgbb)
      provider="imgbb"
      provider_name="ImgBB"
      cache_namespace="imgbb"
      ;;
    *)
      fail "Choose 1 for PicGo.net or 2 for ImgBB."
      return 1
      ;;
  esac

  read -rs "api_key?${provider_name} API key: "
  print
  [[ -n "$api_key" ]] || {
    fail "${provider_name} API key is required."
    return 1
  }
  if [[ ! "$api_key" =~ '^[A-Za-z0-9_-]+$' ]]; then
    fail "The API key contains unsupported characters."
    return 1
  fi

  read -r "target_input?Drag one supported file or directory here, then press Return: "
  [[ -n "$target_input" ]] || {
    fail "A target is required."
    return 1
  }
  target_path="${(Q)target_input}"

  print
  print -r -- "Choose where the original image links come from:"
  print -r -- "  Press Return to use xhscdn.com and its subdomains."
  print -r -- "  Or type domains separated by commas: xhscdn.com,example.com"
  print -r -- "  Or type * to check every domain; non-image URLs are skipped."
  read -r "source_input?Your choice [xhscdn.com]: "
  source_input="${source_input:l}"
  source_input="${source_input//[[:space:]]/}"

  if [[ -z "$source_input" ]]; then
    source_hosts=("$DEFAULT_HOST")
  elif [[ "$source_input" == "*" ]]; then
    all_hosts=true
    source_hosts=()
  else
    source_hosts=("${(@s:,:)source_input}")
    for host in "${source_hosts[@]}"; do
      if [[ ! "$host" =~ '^[A-Za-z0-9.-]+$' ]]; then
        fail "Invalid source host: $host"
        return 1
      fi
    done
  fi

}

show_scan_summary() {
  local source_description

  source_description="${(j:, :)source_hosts}"
  [[ "$all_hosts" == true ]] && source_description="every domain"

  print
  print -r -- "Target: ${target_path:A}"
  print -r -- "Supported files: ${#target_files}"
  print -r -- "Matching URL references: ${reference_count}"
  print -r -- "Unique matching URLs: ${#urls}"
  print -r -- "Upload service: $provider_name"
  print -r -- "Upload request limit: ${UPLOADS_PER_MINUTE} per minute"
  print -r -- "Selected domains: $source_description"
  print

  if (( ${#urls} == 0 )); then
    print -r -- "No matching URLs were found. Nothing was changed."
    return 1
  fi

  print -r -- "Starting migration..."
}

run_migration() {
  local next_index=1
  local item_index
  local total=${#urls}
  local uploaded=0
  local reused=0
  local failed=0
  local run_status=0
  local url
  local migrated_url
  local result_file
  local result_status
  local job_id
  local job_pid
  local completed_any
  local -a active_job_ids
  local -A job_pids
  local -A job_urls
  local -A job_results

  /bin/mkdir -p -- "$state_dir" || return 1
  /bin/chmod 700 "$state_dir"

  migration_stop_requested=false
  migration_stop_announced=false
  trap request_migration_stop INT TERM

  while (( next_index <= total || ${#active_job_ids} > 0 )); do
    while [[ "$migration_stop_requested" == false ]] && \
      (( next_index <= total && ${#active_job_ids} < parallel_transfers )); do
      item_index=$next_index
      url="${urls[$item_index]}"
      (( next_index += 1 ))

      migrated_url="$(lookup_cache "$url")"
      if [[ -n "$migrated_url" ]]; then
        print -r -- "[${item_index}/${total}] Cached: $url"
        print -r -- "  Reused cached URL: $migrated_url"
        (( reused += 1 ))
        if ! replace_url_in_indexed_files "$url" "$migrated_url"; then
          (( failed += 1 ))
        fi
        continue
      fi

      print -r -- "[${item_index}/${total}] Downloading: $url"
      result_file="${work_dir}/transfer-${item_index}.result"
      (
        trap '' INT TERM
        transfer_url_worker "$url" "$item_index" "$total" "$result_file"
        job_status=$?
        if [[ ! -f "$result_file" ]]; then
          write_transfer_result "$result_file" "worker_failed"
        fi
        exit "$job_status"
      ) &
      job_pid=$!
      active_job_ids+=("$item_index")
      job_pids[$item_index]="$job_pid"
      job_urls[$item_index]="$url"
      job_results[$item_index]="$result_file"
    done

    if (( ${#active_job_ids} == 0 )); then
      break
    fi

    completed_any=false
    for job_id in "${active_job_ids[@]}"; do
      result_file="${job_results[$job_id]}"
      [[ -f "$result_file" ]] || continue

      wait "${job_pids[$job_id]}" 2>/dev/null
      IFS=$'\t' read -r result_status migrated_url < "$result_file"
      url="${job_urls[$job_id]}"

      if [[ ( "$result_status" == "uploaded" || "$result_status" == "reused" ) && \
        -n "$migrated_url" ]]; then
        if ! record_cache "$url" "$migrated_url"; then
          print -u2 -r -- "  Warning: could not record the migration cache."
        fi
        if [[ "$result_status" == "reused" ]]; then
          (( reused += 1 ))
          print -r -- "[${job_id}/${total}] Reused: $migrated_url"
        else
          (( uploaded += 1 ))
          print -r -- "[${job_id}/${total}] Uploaded: $migrated_url"
        fi
        if ! replace_url_in_indexed_files "$url" "$migrated_url"; then
          (( failed += 1 ))
        fi
      else
        (( failed += 1 ))
        case "$result_status" in
          download_failed)
            print -u2 -r -- "[${job_id}/${total}] Failed: download"
            ;;
          upload_failed)
            print -u2 -r -- "[${job_id}/${total}] Failed: upload"
            ;;
          *)
            print -u2 -r -- "[${job_id}/${total}] Failed: transfer worker"
            ;;
        esac
      fi

      active_job_ids=("${(@)active_job_ids:#${job_id}}")
      unset "job_pids[$job_id]" "job_urls[$job_id]" "job_results[$job_id]"
      completed_any=true
    done

    if [[ "$completed_any" == false ]]; then
      /bin/sleep 0.1
    fi
  done

  print
  if [[ "$migration_stop_requested" == true ]]; then
    print -r -- "Stopped after active transfers completed: uploaded ${uploaded}, reused ${reused}, failed ${failed}."
    run_status=130
  else
    print -r -- "Finished: uploaded ${uploaded}, reused ${reused}, failed ${failed}."
    (( failed == 0 )) || run_status=1
  fi

  trap 'exit 130' INT TERM
  return "$run_status"
}

self_test() {
  local test_dir
  local text_file
  local markdown_file
  local long_markdown_file
  local duplicate_response_file
  local content_digest
  local file
  local file_index
  local url
  local migrated_url
  local migration_status=0
  local signal_pid
  local rate_pid
  local rate_start
  local rate_end
  local -i cached_count=0
  local -i remaining_count=0
  local -a indexed_files
  local -a rate_pids
  local old_url='https://cdn.xhscdn.com/path/image'
  local second_url='http://sns-webpic-qc.xhscdn.com/path/image!variant'
  local third_url='https://media.xhscdn.com/path/third-image'
  local fourth_url='https://media.xhscdn.com/path/fourth-image'

  (( parallel_transfers == 3 )) || return 1

  test_dir="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/img-link-migrator-self-test.XXXXXX")" || return 1
  work_dir="$test_dir"
  upload_interval_seconds=0.05
  rate_pids=()
  for file_index in 1 2 3; do
    (
      wait_for_upload_slot || exit 1
      current_time_seconds > "${test_dir}/rate-slot-${file_index}.time"
    ) &
    rate_pids+=("$!")
  done
  for rate_pid in "${rate_pids[@]}"; do
    wait "$rate_pid" || return 1
  done
  /usr/bin/sort -n "${test_dir}"/rate-slot-*.time \
    > "${test_dir}/rate-slots.time" || return 1
  LC_ALL=C /usr/bin/perl -e '
    open my $input, "<", $ARGV[0] or exit 1;
    my @times = <$input>;
    exit 1 unless @times == 3;
    for my $index (1 .. $#times) {
      exit 1 if $times[$index] - $times[$index - 1] < 0.04;
    }
  ' "${test_dir}/rate-slots.time" || return 1
  rate_start="$(current_time_seconds)" || return 1
  defer_uploads 0.05 || return 1
  wait_for_upload_slot || return 1
  rate_end="$(current_time_seconds)" || return 1
  LC_ALL=C /usr/bin/perl -e 'exit !(($ARGV[1] - $ARGV[0]) >= 0.04)' \
    "$rate_start" "$rate_end" || return 1
  is_upload_rate_error \
    "Flooding detected. You can only upload 50 images per minute" || return 1
  ! is_upload_rate_error "Duplicated upload" || return 1
  /bin/rm -f -- "${work_dir}"/rate-slot-*.time \
    "${work_dir}/rate-slots.time" "${work_dir}/next-upload-time"
  upload_interval_seconds=$DEFAULT_UPLOAD_INTERVAL_SECONDS
  duplicate_response_file="${test_dir}/duplicate-response.json"
  print -r -- \
    '{"status_code":400,"error":{"message":"Duplicated upload"},"image":{"url":"https://origin.picgo.net/existing.webp"}}' \
    > "$duplicate_response_file"
  [[ "$(chevereto_response_url "$duplicate_response_file")" == \
    "https://origin.picgo.net/existing.webp" ]] || return 1
  text_file="${test_dir}/images.txt"
  markdown_file="${test_dir}/note.md"
  long_markdown_file="${test_dir}/note.markdown"
  /usr/bin/printf '\3771. %s\n2. %s\n' "$old_url" "$second_url" > "$text_file"
  {
    print -r -- "![image]($old_url)"
    print -r -- "3. $third_url"
    print -r -- '4. https://example.com/not-selected.jpg'
  } > "$markdown_file"
  {
    print -r -- "5. $old_url"
    print -r -- "6. $fourth_url"
    print -r -- '5. https://i.ibb.co/already/migrated.webp'
  } > "$long_markdown_file"

  target_path="$test_dir"
  source_hosts=("xhscdn.com")
  all_hosts=false
  collect_target_files || return 1
  collect_urls || return 1
  (( ${#target_files} == 3 && reference_count == 6 && ${#urls} == 4 )) || return 1
  indexed_files=(${=url_file_indexes[$old_url]})
  (( ${#indexed_files} == 3 )) || return 1
  indexed_files=(${=url_file_indexes[$second_url]})
  (( ${#indexed_files} == 1 )) || return 1

  state_dir="${test_dir}/state"
  cache_file="${state_dir}/url-map.tsv"
  content_cache_file="${state_dir}/content-map.tsv"
  content_digest="$(file_hash "$text_file")" || return 1
  record_content_cache "$content_digest" \
    "https://origin.picgo.net/content-cache.webp" || return 1
  [[ "$(lookup_content_cache "$content_digest")" == \
    "https://origin.picgo.net/content-cache.webp" ]] || return 1
  cache_namespace="imgbb"
  ! lookup_content_cache "$content_digest" >/dev/null || return 1
  cache_namespace="chevereto:${CHEVERETO_URL}"
  test_transfer_mode=true
  test_transfer_delay=0.2
  (
    /bin/sleep 0.05
    /bin/kill -INT $$
  ) &
  signal_pid=$!
  run_migration
  migration_status=$?
  wait "$signal_pid" 2>/dev/null
  test_transfer_mode=false
  test_transfer_delay=0
  (( migration_status == 130 )) || return 1

  for url in "${urls[@]}"; do
    migrated_url="$(lookup_cache "$url")"
    indexed_files=(${=url_file_indexes[$url]})
    if [[ -n "$migrated_url" ]]; then
      (( cached_count += 1 ))
      for file_index in "${indexed_files[@]}"; do
        file="${target_files[$file_index]}"
        ! /usr/bin/grep -aFq -- "$url" "$file" || return 1
      done
    else
      (( remaining_count += 1 ))
      for file_index in "${indexed_files[@]}"; do
        file="${target_files[$file_index]}"
        if /usr/bin/grep -aFq -- "$url" "$file"; then
          break
        fi
      done
      /usr/bin/grep -aFq -- "$url" "$file" || return 1
    fi
  done
  (( cached_count == 3 && remaining_count == 1 )) || return 1

  test_transfer_mode=true
  run_migration || return 1
  test_transfer_mode=false
  for url in "${urls[@]}"; do
    migrated_url="$(lookup_cache "$url")"
    [[ -n "$migrated_url" ]] || return 1
    indexed_files=(${=url_file_indexes[$url]})
    for file_index in "${indexed_files[@]}"; do
      file="${target_files[$file_index]}"
      ! /usr/bin/grep -aFq -- "$url" "$file" || return 1
    done
  done

  provider="chevereto"
  host_is_destination "cdn.picgo.net" || return 1
  ! host_is_destination "i.ibb.co" || return 1
  provider="imgbb"
  /bin/rm -rf -- "$test_dir"
  work_dir=""
  print -r -- "Self-test passed."
}

main() {
  local exit_code=0

  if [[ "${1:-}" == "--self-test" ]]; then
    self_test
    return $?
  fi

  umask 077
  work_dir="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/img-link-migrator.XXXXXX")" || return 1

  prompt_settings || return 2
  collect_target_files || return 2
  collect_urls || return 2
  if ! show_scan_summary; then
    return 0
  fi
  run_migration
  exit_code=$?
  if (( exit_code != 0 && exit_code != 130 )); then
    exit_code=2
  fi
  return "$exit_code"
}

main "$@"
exit_code=$?
(( exit_code == 130 )) || pause_before_exit
exit "$exit_code"
