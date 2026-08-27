#!/bin/zsh

emulate -LR zsh
setopt PIPE_FAIL
setopt EXTENDED_GLOB

readonly APP_NAME="IMG Link Migrator Standalone"
readonly APP_VERSION="0.2.0"
readonly USER_AGENT="IMG-Link-Migrator-Standalone/${APP_VERSION}"
readonly MAX_IMAGE_BYTES=33554432
readonly DEFAULT_HOST="xhscdn.com"
readonly MAX_ATTEMPTS=4
readonly CHEVERETO_URL="https://www.picgo.net"

typeset -a target_files
typeset -a urls
typeset -a source_hosts
typeset -A fingerprints
typeset -i reference_count=0

api_key=""
target_path=""
all_hosts=false
provider="imgbb"
provider_name="ImgBB"
cache_namespace="imgbb"
downloaded_mime="application/octet-stream"
downloaded_filename="image.bin"
state_dir="${HOME}/Library/Application Support/${APP_NAME}"
cache_file="${state_dir}/url-map.tsv"
work_dir=""

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

  urls=()
  fingerprints=()
  reference_count=0

  for file in "${target_files[@]}"; do
    fingerprints[$file]="$(file_hash "$file")"
    while IFS= read -r url; do
      url="$(trim_url_suffix "$url")"
      if url_is_selected "$url"; then
        (( reference_count += 1 ))
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

sleep_before_retry() {
  local attempt="$1"
  local delay=$(( 1 << (attempt - 1) ))
  print -u2 -r -- "  Retrying in ${delay} second(s)..."
  /bin/sleep "$delay"
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
      print -u2 -r -- "  Downloaded content is not a valid image or exceeds 32 MB."
    else
      print -u2 -r -- "  Download attempt ${attempt} failed."
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

  for (( attempt = 1; attempt <= MAX_ATTEMPTS; attempt++ )); do
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
      [[ -n "$error_message" ]] && print -u2 -r -- "  ImgBB: $error_message"
    else
      print -u2 -r -- "  Upload attempt ${attempt} failed."
    fi

    (( attempt < MAX_ATTEMPTS )) && sleep_before_retry "$attempt"
  done
  return 1
}

upload_chevereto() {
  local image_file="$1"
  local response_file="$2"
  local attempt
  local curl_status
  local status_code
  local migrated_url
  local error_message

  for (( attempt = 1; attempt <= MAX_ATTEMPTS; attempt++ )); do
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
      status_code="$(
        /usr/bin/plutil -extract status_code raw -o - "$response_file" 2>/dev/null
      )"
      if [[ "$status_code" == "200" ]]; then
        migrated_url="$(
          /usr/bin/plutil -extract image.url raw -o - "$response_file" 2>/dev/null
        )"
        if [[ "$migrated_url" == http://* || "$migrated_url" == https://* ]]; then
          print -r -- "$migrated_url"
          return 0
        fi
      fi
      error_message="$(
        /usr/bin/plutil -extract error.message raw -o - "$response_file" 2>/dev/null
      )"
      [[ -n "$error_message" ]] && print -u2 -r -- "  PicGo.net: $error_message"
    else
      print -u2 -r -- "  Upload attempt ${attempt} failed."
    fi

    (( attempt < MAX_ATTEMPTS )) && sleep_before_retry "$attempt"
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

prompt_settings() {
  local service_input
  local target_input
  local source_input
  local host

  print -r -- "${APP_NAME} ${APP_VERSION}"
  print -r -- "Migrates image URLs in .txt, .md, and .markdown files."
  print

  print -r -- "Choose the upload service:"
  print -r -- "  Press Return or type 1 for ImgBB."
  print -r -- "  Type 2 for PicGo.net (Chevereto API v1)."
  read -r "service_input?Your choice [1]: "
  service_input="${service_input:l}"
  case "$service_input" in
    ""|1|imgbb)
      provider="imgbb"
      provider_name="ImgBB"
      cache_namespace="imgbb"
      ;;
    2|picgo|picgo.net|chevereto)
      provider="chevereto"
      provider_name="PicGo.net"
      cache_namespace="chevereto:${CHEVERETO_URL}"
      ;;
    *)
      fail "Choose 1 for ImgBB or 2 for PicGo.net."
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
  print -r -- "Selected domains: $source_description"
  print

  if (( ${#urls} == 0 )); then
    print -r -- "No matching URLs were found. Nothing was changed."
    return 1
  fi

  print -r -- "Starting migration..."
}

run_migration() {
  local index=0
  local uploaded=0
  local reused=0
  local failed=0
  local url
  local migrated_url
  local image_file="${work_dir}/image.bin"
  local response_file="${work_dir}/response.json"
  local file
  local write_failed

  /bin/mkdir -p -- "$state_dir" || return 1
  /bin/chmod 700 "$state_dir"

  for url in "${urls[@]}"; do
    (( index += 1 ))
    print
    print -r -- "[${index}/${#urls}] $url"

    migrated_url="$(lookup_cache "$url")"
    if [[ -n "$migrated_url" ]]; then
      (( reused += 1 ))
      print -r -- "  Reused cached URL: $migrated_url"
    else
      if ! download_image "$url" "$image_file"; then
        (( failed += 1 ))
        print -u2 -r -- "  Failed: download"
        continue
      fi

      migrated_url="$(upload_image "$image_file" "$response_file")"
      if [[ -z "$migrated_url" ]]; then
        (( failed += 1 ))
        print -u2 -r -- "  Failed: upload"
        continue
      fi

      if ! record_cache "$url" "$migrated_url"; then
        print -u2 -r -- "  Warning: could not record the migration cache."
      fi
      (( uploaded += 1 ))
      print -r -- "  Uploaded: $migrated_url"
    fi

    write_failed=false
    for file in "${target_files[@]}"; do
      if ! replace_url_in_file "$file" "$url" "$migrated_url"; then
        write_failed=true
      fi
    done
    [[ "$write_failed" == true ]] && (( failed += 1 ))
  done

  print
  print -r -- "Finished: uploaded ${uploaded}, reused ${reused}, failed ${failed}."
  (( failed == 0 ))
}

self_test() {
  local test_dir
  local text_file
  local markdown_file
  local long_markdown_file
  local file
  local old_url='https://cdn.xhscdn.com/path/image'
  local second_url='http://sns-webpic-qc.xhscdn.com/path/image!variant'
  local new_url='https://i.ibb.co/test/image.webp'

  test_dir="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/img-link-migrator-self-test.XXXXXX")" || return 1
  work_dir="$test_dir"
  text_file="${test_dir}/images.txt"
  markdown_file="${test_dir}/note.md"
  long_markdown_file="${test_dir}/note.markdown"
  /usr/bin/printf '\3771. %s\n2. %s\n' "$old_url" "$second_url" > "$text_file"
  {
    print -r -- "![image]($old_url)"
    print -r -- '4. https://example.com/not-selected.jpg'
  } > "$markdown_file"
  {
    print -r -- "5. $old_url"
    print -r -- '5. https://i.ibb.co/already/migrated.webp'
  } > "$long_markdown_file"

  target_path="$test_dir"
  source_hosts=("xhscdn.com")
  all_hosts=false
  collect_target_files || return 1
  collect_urls || return 1
  (( ${#target_files} == 3 && reference_count == 4 && ${#urls} == 2 )) || return 1
  for file in "${target_files[@]}"; do
    replace_url_in_file "$file" "$old_url" "$new_url" || return 1
    ! /usr/bin/grep -aFq -- "$old_url" "$file" || return 1
  done
  /usr/bin/grep -aFq -- "$new_url" "$text_file" || return 1
  /usr/bin/grep -aFq -- "$second_url" "$text_file" || return 1
  /usr/bin/grep -aFq -- "$new_url" "$markdown_file" || return 1
  /usr/bin/grep -aFq -- "$new_url" "$long_markdown_file" || return 1
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
  run_migration || exit_code=2
  return "$exit_code"
}

main "$@"
exit_code=$?
pause_before_exit
exit "$exit_code"
