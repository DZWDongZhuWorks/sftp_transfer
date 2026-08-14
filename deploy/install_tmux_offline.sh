#!/usr/bin/env bash
# install_tmux_offline.sh — 依偵測到的 Ubuntu profile，以預載 deb 離線補齊 tmux。
#
# 用法：
#   ./install_tmux_offline.sh [--check-only]
#   ./install_tmux_offline.sh --profile-dir deploy/platforms/ubuntu-18.04-arm64
#
# 安全策略：
#   * 已有 tmux 能建立/查詢/刪除獨立 session 時保留現況，不強制升級。
#   * deb、architecture、package set、glibc 及解包後二進位 probe 全通過才執行 dpkg。
#   * 沒有 root/sudo 就停止，不以 dpkg-deb -x 模擬安裝，不呼叫 apt。
#
# 離開碼：0 已就緒；1 安裝失敗；2 參數錯誤；4 資產不完整；
#         5 check-only 下可安裝；6 平台或 ABI 不相容。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PLATFORMS_ROOT="${SCRIPT_DIR}/platforms"
PROFILE_DIR=""
CHECK_ONLY=0

if [ -t 1 ]; then
  R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[36m'; N=$'\e[0m'
else
  R=""; G=""; Y=""; B=""; N=""
fi
info() { printf '%s[INFO]%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '%s[ OK ]%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s[WARN]%s %s\n' "$Y" "$N" "$*"; }
err()  { printf '%s[FAIL]%s %s\n' "$R" "$N" "$*" >&2; }
usage() { sed -n '2,/^set -uo pipefail/p' "$0" | sed '$d;s/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --check-only) CHECK_ONLY=1 ;;
    --profile-dir) PROFILE_DIR="${2:?--profile-dir 需要路徑}"; shift ;;
    -h|--help) usage ;;
    *) err "未知參數：$1"; exit 2 ;;
  esac
  shift
done

# shellcheck source=deploy/lib/offline_common.sh
. "${SCRIPT_DIR}/lib/offline_common.sh"

tmux_usable() {
  local socket="nssms-probe-$$"
  command -v tmux >/dev/null 2>&1 || return 1
  tmux -V >/dev/null 2>&1 || return 1
  tmux -L "$socket" new-session -d -s nssms-probe 'sleep 30' >/dev/null 2>&1 || return 1
  if ! tmux -L "$socket" has-session -t nssms-probe >/dev/null 2>&1; then
    tmux -L "$socket" kill-server >/dev/null 2>&1 || true
    return 1
  fi
  tmux -L "$socket" kill-server >/dev/null 2>&1 || return 1
}

echo "==========================================================="
echo " tmux 雙平台離線安裝（dpkg only，不連網）"
echo "==========================================================="

if [ -z "$PROFILE_DIR" ]; then
  nssms_detect_profile "$PLATFORMS_ROOT" || exit $?
  PROFILE_DIR="$NSSMS_PROFILE_DIR"
else
  PROFILE_DIR="$(cd "$PROFILE_DIR" 2>/dev/null && pwd -P)" || {
    err "找不到 profile 目錄：$PROFILE_DIR"; exit 4;
  }
  if [ ! -r "$PROFILE_DIR/profile.env" ]; then
    err "找不到 profile.env：$PROFILE_DIR"
    exit 4
  fi
  # 仍先偵測真實平台，再要求 override 指向同一個 profile，避免手動指定 Jammy 給 Bionic。
  nssms_detect_profile "$PLATFORMS_ROOT" || exit $?
  if [ "$PROFILE_DIR" != "$NSSMS_PROFILE_DIR" ]; then
    err "指定 profile 與本機不符：$PROFILE_DIR（本機應為 $NSSMS_PROFILE_DIR）"
    exit 6
  fi
fi

# shellcheck disable=SC1090
. "$PROFILE_DIR/profile.env"
DEBS_DIR="$PROFILE_DIR/debs"
MANIFEST="$DEBS_DIR/MANIFEST.txt"
info "平台 profile：$PROFILE_ID"
info "deb 目錄：$DEBS_DIR"
nssms_verify_flat_manifest "$DEBS_DIR" "$MANIFEST" '*.deb' "tmux debs" || exit $?

declare -a DEBS=()
declare -a NEEDED=()
declare -a PACKAGE_NAMES=()
while IFS= read -r deb; do DEBS+=("$deb"); done < <(find "$DEBS_DIR" -maxdepth 1 -type f -name '*.deb' | sort)

for deb in "${DEBS[@]}"; do
  pkg="$(dpkg-deb -f "$deb" Package 2>/dev/null)" || { err "無法讀取 deb：$deb"; exit 4; }
  ver="$(dpkg-deb -f "$deb" Version 2>/dev/null)" || { err "無法讀取 deb：$deb"; exit 4; }
  arch="$(dpkg-deb -f "$deb" Architecture 2>/dev/null)" || { err "無法讀取 deb：$deb"; exit 4; }
  depends="$(dpkg-deb -f "$deb" Depends 2>/dev/null || true)"
  if [ "$arch" != "$PROFILE_ARCH" ]; then
    err "deb architecture 不符：$(basename "$deb") 是 $arch，需要 $PROFILE_ARCH"
    exit 6
  fi
  libc_min="$(printf '%s\n' "$depends" | sed -n 's/.*libc6 (>= \([^)]*\)).*/\1/p')"
  if [ -n "$libc_min" ] && ! nssms_version_ge "$NSSMS_GLIBC" "$libc_min"; then
    err "$(basename "$deb") 需要 glibc >= $libc_min，本機是 $NSSMS_GLIBC"
    exit 6
  fi
  PACKAGE_NAMES+=("$pkg")

  query="$(dpkg-query -W -f='${Status}|${Version}' "$pkg" 2>/dev/null || true)"
  case "$query" in
    "install ok installed|"*) cur="${query#*|}" ;;
    *) cur="" ;;
  esac
  if [ -z "$cur" ] || ! dpkg --compare-versions "$cur" ge "$ver"; then
    NEEDED+=("$deb")
    info "  $pkg：${cur:-未安裝} → $ver"
  fi
done

actual_packages="$(printf '%s\n' "${PACKAGE_NAMES[@]}" | sort | paste -sd' ' -)"
expected_packages="$(printf '%s\n' $TMUX_EXPECTED_PACKAGES | sort | paste -sd' ' -)"
if [ "$actual_packages" != "$expected_packages" ]; then
  err "deb package set 不完整或含多餘套件"
  err "預期：$expected_packages"
  err "實際：$actual_packages"
  exit 4
fi

tmux_deb=""
for deb in "${DEBS[@]}"; do
  if [ "$(dpkg-deb -f "$deb" Package 2>/dev/null)" = "tmux" ]; then tmux_deb="$deb"; fi
done
[ -n "$tmux_deb" ] || { err "profile 中沒有 tmux deb"; exit 4; }
if [ "$(dpkg-deb -f "$tmux_deb" Version 2>/dev/null)" != "$TMUX_EXPECTED_VERSION" ]; then
  err "tmux 版本不符 profile：$(dpkg-deb -f "$tmux_deb" Version 2>/dev/null)"
  exit 6
fi

PROBE_DIR="$(mktemp -d /tmp/nssms-tmux-probe.XXXXXX)"
cleanup() {
  case "$PROBE_DIR" in /tmp/nssms-tmux-probe.*) rm -rf -- "$PROBE_DIR" ;; esac
}
trap cleanup EXIT
for deb in "${DEBS[@]}"; do
  dpkg-deb -x "$deb" "$PROBE_DIR" || { err "無法解包：$deb"; exit 4; }
done
PROBE_TMUX="$PROBE_DIR/usr/bin/tmux"
[ -x "$PROBE_TMUX" ] || { err "解包後找不到 tmux binary"; exit 4; }
LIBDIRS="$(find "$PROBE_DIR" -name 'lib*.so*' -printf '%h\n' | sort -u | paste -sd: -)"
PROBE_LD_LIBRARY_PATH="$LIBDIRS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
PROBE_SOCKET="nssms-deb-probe-$$"
if ! LD_LIBRARY_PATH="$PROBE_LD_LIBRARY_PATH" "$PROBE_TMUX" -V >/dev/null 2>&1; then
  err "解包後的 tmux 無法在本機 ABI 上執行："
  LD_LIBRARY_PATH="$PROBE_LD_LIBRARY_PATH" ldd "$PROBE_TMUX" >&2 || true
  exit 6
fi
if ! LD_LIBRARY_PATH="$PROBE_LD_LIBRARY_PATH" \
     "$PROBE_TMUX" -L "$PROBE_SOCKET" new-session -d -s nssms-deb-probe 'sleep 30' \
     >/dev/null 2>&1; then
  LD_LIBRARY_PATH="$PROBE_LD_LIBRARY_PATH" \
    "$PROBE_TMUX" -L "$PROBE_SOCKET" kill-server >/dev/null 2>&1 || true
  err "解包後的 tmux 可載入，但無法建立 session。"
  exit 6
fi
if ! LD_LIBRARY_PATH="$PROBE_LD_LIBRARY_PATH" \
     "$PROBE_TMUX" -L "$PROBE_SOCKET" has-session -t nssms-deb-probe >/dev/null 2>&1; then
  LD_LIBRARY_PATH="$PROBE_LD_LIBRARY_PATH" \
    "$PROBE_TMUX" -L "$PROBE_SOCKET" kill-server >/dev/null 2>&1 || true
  err "解包後的 tmux 建立 session 後無法查詢。"
  exit 6
fi
LD_LIBRARY_PATH="$PROBE_LD_LIBRARY_PATH" \
  "$PROBE_TMUX" -L "$PROBE_SOCKET" kill-server >/dev/null 2>&1 || {
    err "解包後的 tmux 無法乾淨結束 probe server。"; exit 6;
  }
probe_version="$(LD_LIBRARY_PATH="$PROBE_LD_LIBRARY_PATH" "$PROBE_TMUX" -V 2>/dev/null)"
ok "tmux 解包 session/ABI probe 通過：$probe_version"

if tmux_usable; then
  ok "既有 tmux 已通過 session 能力測試，保留現況：$(tmux -V) ($(command -v tmux))"
  exit 0
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  ok "--check-only：profile 與 deb 均可安裝；未做任何變更。"
  exit 5
fi

if [ "$(id -u)" -eq 0 ]; then
  DPKG_CMD=(dpkg -i)
elif ! command -v sudo >/dev/null 2>&1; then
  err "缺少 root/sudo，拒絕以 dpkg-deb -x 模擬安裝。請取得 sudo 後重跑。"
  exit 1
elif sudo -n true 2>/dev/null; then
  DPKG_CMD=(sudo dpkg -i)
elif [ -t 0 ]; then
  info "接下來需要輸入一次 sudo 密碼。"
  DPKG_CMD=(sudo dpkg -i)
else
  err "非互動終端機且 sudo 需要密碼，無法安全安裝 tmux。"
  err "請在互動終端執行，或預先授權 sudo dpkg。"
  exit 1
fi

if [ "${#NEEDED[@]}" -eq 0 ]; then
  # package database 全顯示已安裝、但能力 probe 失敗才會到這裡。重裝整個小型 closure，
  # 同時修復遺失/損壞的 libevent、tinfo 或 utempter，不只修 tmux 檔案本身。
  NEEDED=("${DEBS[@]}")
fi
info "以 ${DPKG_CMD[*]} 安裝 ${#NEEDED[@]} 個離線 deb ..."
"${DPKG_CMD[@]}" "${NEEDED[@]}"
dpkg_rc=$?
if [ "$dpkg_rc" -ne 0 ]; then
  err "dpkg -i 失敗（exit=$dpkg_rc）。未呼叫 apt；請檢查 dpkg --audit。"
  exit 1
fi
if ! tmux_usable; then
  err "dpkg 完成後 tmux 仍未通過 session 能力測試。"
  case "$(command -v tmux 2>/dev/null || true)" in
    /usr/bin/tmux|/bin/tmux) ;;
    "") ;;
    *) err "PATH 上另有 tmux 覆蓋系統版本：$(command -v tmux)；請人工檢查後重跑。" ;;
  esac
  exit 1
fi
ok "tmux 已安裝並通過能力測試：$(tmux -V)"
