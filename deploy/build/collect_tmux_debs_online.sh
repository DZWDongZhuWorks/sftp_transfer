#!/usr/bin/env bash
# 在有外網、且 OS/architecture 與目標相同的建置機執行；船機禁止執行本腳本。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd -P)"

info() { printf '[INFO] %s\n' "$*"; }
ok()   { printf '[ OK ] %s\n' "$*"; }
err()  { printf '[FAIL] %s\n' "$*" >&2; }

if [ "${1:-}" != "--allow-network-build" ] || [ "$#" -ne 1 ]; then
  err "這是有外網建置機專用腳本；用法：$0 --allow-network-build"
  exit 2
fi

# shellcheck source=deploy/lib/offline_common.sh
. "${DEPLOY_DIR}/lib/offline_common.sh"
nssms_detect_profile "${DEPLOY_DIR}/platforms" || exit $?
# shellcheck disable=SC1090
. "$NSSMS_PROFILE_FILE"

if ! command -v apt-get >/dev/null 2>&1; then
  err "建置機缺少 apt-get"
  exit 1
fi

STAGING="$(mktemp -d /tmp/nssms-tmux-debs.XXXXXX)"
cleanup() { case "$STAGING" in /tmp/nssms-tmux-debs.*) rm -rf -- "$STAGING" ;; esac; }
trap cleanup EXIT

info "從建置機目前設定的 Ubuntu repository 下載 $PROFILE_ID 套件 ..."
(cd "$STAGING" && apt-get download $TMUX_EXPECTED_PACKAGES)

count="$(find "$STAGING" -maxdepth 1 -type f -name '*.deb' | wc -l | tr -d ' ')"
expected_count="$(printf '%s\n' $TMUX_EXPECTED_PACKAGES | wc -l | tr -d ' ')"
if [ "$count" -ne "$expected_count" ]; then
  err "下載數量不符：預期 $expected_count，實際 $count"
  exit 1
fi

declare -a DOWNLOADED_PACKAGES=()
tmux_version=""
for deb in "$STAGING"/*.deb; do
  pkg="$(dpkg-deb -f "$deb" Package)"
  ver="$(dpkg-deb -f "$deb" Version)"
  [ "$(dpkg-deb -f "$deb" Architecture)" = "$PROFILE_ARCH" ] || {
    err "套件架構不符：$deb"; exit 1;
  }
  DOWNLOADED_PACKAGES+=("$pkg")
  [ "$pkg" != tmux ] || tmux_version="$ver"
done

actual_packages="$(printf '%s\n' "${DOWNLOADED_PACKAGES[@]}" | sort | paste -sd' ' -)"
expected_packages="$(printf '%s\n' $TMUX_EXPECTED_PACKAGES | sort | paste -sd' ' -)"
if [ "$actual_packages" != "$expected_packages" ]; then
  err "下載的 package set 不符：預期 $expected_packages；實際 $actual_packages"
  exit 1
fi
if [ "$tmux_version" != "$TMUX_EXPECTED_VERSION" ]; then
  err "repository 的 tmux 版本是 ${tmux_version:-missing}，profile 鎖定 $TMUX_EXPECTED_VERSION"
  err "請先審核並更新 profile.env，再重新組包。"
  exit 1
fi

DEST="$NSSMS_PROFILE_DIR/debs"
find "$DEST" -maxdepth 1 -type f -name '*.deb' -delete
cp "$STAGING"/*.deb "$DEST/"
{
  printf '# %s tmux 離線套件；由 collect_tmux_debs_online.sh 產生。\n' "$PROFILE_ID"
  (cd "$DEST" && sha256sum ./*.deb | sed 's#  \./#  #')
} > "$DEST/MANIFEST.txt"
ok "已更新 $DEST；請將 deb 與 MANIFEST.txt 一併提交/派送。"
