#!/usr/bin/env bash
# shellcheck shell=bash
#
# 離線部署共用的唯讀檢查。呼叫者須先定義 err/info/ok（未定義時使用本檔的保底輸出）。

if ! declare -F err >/dev/null 2>&1; then
  err() { printf '[FAIL] %s\n' "$*" >&2; }
fi
if ! declare -F info >/dev/null 2>&1; then
  info() { printf '[INFO] %s\n' "$*"; }
fi
if ! declare -F ok >/dev/null 2>&1; then
  ok() { printf '[ OK ] %s\n' "$*"; }
fi

nssms_version_ge() {
  dpkg --compare-versions "$1" ge "$2"
}

nssms_detect_profile() {
  local platforms_root="$1"
  local os_release_file="/etc/os-release"
  local detected_id detected_version detected_arch detected_glibc
  local required_profile_var

  case "${NSSMS_TEST_OVERRIDES:-0}" in
    0)
      if [ -n "${NSSMS_OS_RELEASE_FILE:-}${NSSMS_ARCH_OVERRIDE:-}${NSSMS_GLIBC_OVERRIDE:-}" ]; then
        err "偵測 override 只允許測試使用（需 NSSMS_TEST_OVERRIDES=1）"
        return 6
      fi
      ;;
    1) os_release_file="${NSSMS_OS_RELEASE_FILE:-/etc/os-release}" ;;
    *) err "NSSMS_TEST_OVERRIDES 只能是 0 或 1"; return 6 ;;
  esac

  if [ ! -r "$os_release_file" ]; then
    err "讀不到作業系統資訊：$os_release_file"
    return 6
  fi

  detected_id="$(
    unset ID VERSION_ID
    # shellcheck disable=SC1090
    . "$os_release_file"
    printf '%s' "${ID:-}"
  )"
  detected_version="$(
    unset ID VERSION_ID
    # shellcheck disable=SC1090
    . "$os_release_file"
    printf '%s' "${VERSION_ID:-}"
  )"
  if [ "${NSSMS_TEST_OVERRIDES:-0}" -eq 1 ]; then
    detected_arch="${NSSMS_ARCH_OVERRIDE:-$(dpkg --print-architecture 2>/dev/null || true)}"
    detected_glibc="${NSSMS_GLIBC_OVERRIDE:-$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')}"
  else
    detected_arch="$(dpkg --print-architecture 2>/dev/null || true)"
    detected_glibc="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')"
  fi

  case "${detected_id}:${detected_version}:${detected_arch}" in
    ubuntu:18.04:arm64) NSSMS_PROFILE_ID="ubuntu-18.04-arm64" ;;
    ubuntu:22.04:arm64) NSSMS_PROFILE_ID="ubuntu-22.04-arm64" ;;
    *)
      err "不支援的平台：${detected_id:-unknown} ${detected_version:-unknown} ${detected_arch:-unknown}"
      err "目前只支援 Ubuntu 18.04 / 22.04 ARM64；不會猜測或套用其他平台資產。"
      return 6
      ;;
  esac

  NSSMS_PROFILE_DIR="${platforms_root}/${NSSMS_PROFILE_ID}"
  NSSMS_PROFILE_FILE="${NSSMS_PROFILE_DIR}/profile.env"
  if [ ! -r "$NSSMS_PROFILE_FILE" ]; then
    err "找不到平台 profile：$NSSMS_PROFILE_FILE"
    return 4
  fi

  # profile.env 是本 repo 版控的常數，不接受外部產生或網路取得的檔案。先清掉同名
  # 環境變數，避免呼叫端的殘值蓋過 profile 缺漏。
  unset PROFILE_ID PROFILE_OS_ID PROFILE_OS_VERSION PROFILE_ARCH
  unset PROFILE_GLIBC_MIN
  unset TMUX_EXPECTED_PACKAGES TMUX_EXPECTED_VERSION
  # shellcheck disable=SC1090
  . "$NSSMS_PROFILE_FILE"
  for required_profile_var in \
    PROFILE_ID PROFILE_OS_ID PROFILE_OS_VERSION PROFILE_ARCH PROFILE_GLIBC_MIN \
    TMUX_EXPECTED_PACKAGES TMUX_EXPECTED_VERSION; do
    if [ -z "${!required_profile_var:-}" ]; then
      err "profile 缺少必要欄位：$required_profile_var（$NSSMS_PROFILE_FILE）"
      return 6
    fi
  done
  if [ "${PROFILE_ID:-}" != "$NSSMS_PROFILE_ID" ]; then
    err "profile 身分不一致：目錄=$NSSMS_PROFILE_ID，內容=${PROFILE_ID:-missing}"
    return 6
  fi
  if [ "${PROFILE_OS_ID:-}" != "$detected_id" ] || \
     [ "${PROFILE_OS_VERSION:-}" != "$detected_version" ] || \
     [ "${PROFILE_ARCH:-}" != "$detected_arch" ]; then
    err "profile 與本機不相容：$NSSMS_PROFILE_FILE"
    return 6
  fi
  if [ -z "$detected_glibc" ] || ! nssms_version_ge "$detected_glibc" "$PROFILE_GLIBC_MIN"; then
    err "glibc 不相容：本機 ${detected_glibc:-unknown}，profile 至少需要 $PROFILE_GLIBC_MIN"
    return 6
  fi
  NSSMS_OS_ID="$detected_id"
  NSSMS_OS_VERSION="$detected_version"
  NSSMS_ARCH="$detected_arch"
  NSSMS_GLIBC="$detected_glibc"
  export NSSMS_PROFILE_ID NSSMS_PROFILE_DIR NSSMS_PROFILE_FILE
  export NSSMS_OS_ID NSSMS_OS_VERSION NSSMS_ARCH NSSMS_GLIBC
}

# Ubuntu 18.04 隨附的 mawk 1.3.3 不支援 ERE interval（例如 {64}），所以下方
# 一律用 length + 字元集合解析 SHA256。
# 驗證扁平目錄：manifest 必須存在、有至少一筆 hash、所有 payload 都列在 manifest，且
# manifest 不能引用子目錄或目錄外檔案。$3 是 find 的 -name pattern（例如 '*.whl'）。
nssms_verify_flat_manifest() {
  local asset_dir="$1"
  local manifest="$2"
  local payload_pattern="$3"
  local label="$4"
  local manifest_count payload_count filename invalid_count duplicate manifest_dir

  if [ ! -d "$asset_dir" ]; then
    err "$label 目錄不存在：$asset_dir"
    return 4
  fi
  asset_dir="$(cd "$asset_dir" && pwd -P)"
  case "$manifest" in
    /*) ;;
    *) manifest="$(pwd -P)/$manifest" ;;
  esac
  if [ ! -f "$manifest" ]; then
    err "$label manifest 不存在：$manifest"
    return 4
  fi
  manifest_dir="$(cd "$(dirname "$manifest")" && pwd -P)"
  if [ "$manifest_dir" != "$asset_dir" ]; then
    err "$label manifest 必須與 payload 位於同一目錄：$manifest"
    return 4
  fi

  invalid_count="$(awk '
    /^[ \t]*$/ { next }
    /^[ \t]*#/ { next }
    !(NF == 2 && length($1) == 64 && $1 ~ /^[0-9a-f]+$/) { n++ }
    END { print n+0 }
  ' "$manifest")"
  if [ "$invalid_count" -ne 0 ]; then
    err "$label manifest 含 $invalid_count 筆格式錯誤的非註解內容：$manifest"
    return 4
  fi

  manifest_count="$(awk 'NF == 2 && length($1) == 64 && $1 ~ /^[0-9a-f]+$/ {n++} END {print n+0}' "$manifest")"
  payload_count="$(find "$asset_dir" -maxdepth 1 -type f -name "$payload_pattern" | wc -l | tr -d ' ')"
  if [ "$manifest_count" -eq 0 ]; then
    err "$label manifest 沒有任何 sha256 項目：$manifest"
    return 4
  fi
  if [ "$manifest_count" -ne "$payload_count" ]; then
    err "$label 數量不一致：manifest=$manifest_count，目錄內=$payload_count"
    return 4
  fi

  duplicate="$(awk 'NF == 2 && length($1) == 64 && $1 ~ /^[0-9a-f]+$/ {print $2}' "$manifest" | sort | uniq -d | head -1)"
  if [ -n "$duplicate" ]; then
    err "$label manifest 重複列出檔案：$duplicate"
    return 4
  fi

  while read -r filename; do
    case "$filename" in
      ""|.|..|*/*)
        err "$label manifest 含不安全路徑：$filename"
        return 4
        ;;
    esac
    case "$filename" in
      $payload_pattern) ;;
      *)
        err "$label manifest 含非預期檔案：$filename"
        return 4
        ;;
    esac
  done < <(awk 'NF == 2 && length($1) == 64 && $1 ~ /^[0-9a-f]+$/ {print $2}' "$manifest")

  if ! (cd "$asset_dir" && awk 'NF == 2 && length($1) == 64 && $1 ~ /^[0-9a-f]+$/' "$manifest" | sha256sum -c --quiet); then
    err "$label sha256 校驗失敗"
    return 4
  fi
  ok "$label sha256 校驗通過（$payload_count 個檔案）"
}
