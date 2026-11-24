#!/usr/bin/env bash
# install_ffmpeg_offline.sh — 以隨附的靜態 ffmpeg / ffprobe 離線安裝到 /usr/local/bin(全船所有帳號共用)。
#
# 用法：
#   ./install_ffmpeg_offline.sh [--check-only]
#
# 為什麼是靜態執行檔而不是 deb：
#   * 船上沒有對外網路，`apt install ffmpeg` 不成立；而 ffmpeg 的 deb 依賴閉包上百個套件，
#     一包「在別台機器上抓的」deb 正是 2026-07 弄斷 43 台 apt 依賴的那種東西(scheduler 的
#     docs/apt-health.md)。靜態執行檔不經 dpkg / apt、不碰任何系統套件，apt 斷掉的機器也裝得上。
#   * 以 musl 完全靜態連結：不依賴 glibc，Ubuntu 18.04(glibc 2.27)與 22.04 同一支就能跑。
#   * 內含 libx264(radar 長錄影唯一可用的 libx264rgb qp 0 無損編碼)。怎麼編的見
#     static/ffmpeg/build/README.md;壓縮檔內的 BUILDINFO.txt 記著原始碼版本與 sha256。
#
# 安裝結果：
#   /usr/local/bin/ffmpeg、/usr/local/bin/ffprobe        (root:root 0755)
#   /usr/local/share/nssms-ffmpeg/{BUILDINFO.txt,COPYING.GPLv2,SHA256SUMS}
#     —— 這個目錄是「這兩支歸本包管」的記號(寫執行檔之前就先建);SHA256SUMS 最後寫，
#        代表「這一版裝完整了」。沒有這個目錄而 /usr/local/bin 已有 ffmpeg/ffprobe 時，
#        視為別人放的，**不覆蓋**(exit 8)。/usr/bin/ffmpeg(apt 裝的)不受影響,只是 PATH 上
#        /usr/local/bin 在前,之後找到的是本包這支。
#
# 安全策略：
#   * 壓縮檔先過 MANIFEST.txt 的 sha256、解開後再過內附 SHA256SUMS，然後**在本機實測**：
#     ffmpeg 有 libx264rgb、真的編得出 2 幀 qp 0 的 mp4、ffprobe 跑得起來。全部通過才安裝。
#   * 先寫成同目錄的暫存名再 mv -f 換上(同一檔案系統上是原子操作;正在跑的 ffmpeg 不受影響)；
#     記號最後才寫 —— 中途失敗時記號不完整，下次會重裝。
#   * 已經是同一版(記號 + 兩支執行檔的 sha256 都相符)就什麼都不做。
#
# 離開碼：0 已就緒/已安裝；1 安裝失敗；2 參數錯誤；3 需要 sudo 但沒有預先授權(無終端機)；
#         4 離線包不完整；
#         5 check-only 下可安裝；6 平台不相容或解開後實測失敗；
#         8 /usr/local/bin 已有不是本包裝的 ffmpeg/ffprobe，不覆蓋。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BUNDLE_DIR="${SCRIPT_DIR}/static/ffmpeg"
PREFIX="/usr/local"
CHECK_ONLY=0

# 測試用 override(與 nssms_detect_profile 同一道閘門:只有 NSSMS_TEST_OVERRIDES=1 時才收)。
if [ -n "${NSSMS_FFMPEG_BUNDLE_DIR:-}${NSSMS_FFMPEG_PREFIX:-}${NSSMS_ARCH_OVERRIDE:-}" ]; then
  if [ "${NSSMS_TEST_OVERRIDES:-0}" != "1" ]; then
    printf '[FAIL] 測試 override 只允許測試使用(需 NSSMS_TEST_OVERRIDES=1)\n' >&2
    exit 2
  fi
  BUNDLE_DIR="${NSSMS_FFMPEG_BUNDLE_DIR:-$BUNDLE_DIR}"
  PREFIX="${NSSMS_FFMPEG_PREFIX:-$PREFIX}"
fi

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
    -h|--help) usage ;;
    *) err "未知參數：$1"; exit 2 ;;
  esac
  shift
done

# shellcheck source=deploy/lib/offline_common.sh
. "${SCRIPT_DIR}/lib/offline_common.sh"

BIN_DIR="$PREFIX/bin"
MARK_DIR="$PREFIX/share/nssms-ffmpeg"
TOOLS="ffmpeg ffprobe"

echo "==========================================================="
echo " ffmpeg 靜態執行檔離線安裝(不連網、不經 apt/dpkg)"
echo "==========================================================="

# ---- 平台：只有 arm64(船隊全是 Jetson)
arch="${NSSMS_ARCH_OVERRIDE:-$(uname -m)}"
case "$arch" in
  aarch64|arm64) ;;
  *) err "本包是 linux arm64 的執行檔，本機是 $arch"; exit 6 ;;
esac

# ---- 離線包完整性
info "離線包目錄：$BUNDLE_DIR"
nssms_verify_flat_manifest "$BUNDLE_DIR" "$BUNDLE_DIR/MANIFEST.txt" '*.tar.xz' "ffmpeg 離線包" || exit $?
mapfile -t TARBALLS < <(find "$BUNDLE_DIR" -maxdepth 1 -type f -name '*.tar.xz' | sort)
if [ "${#TARBALLS[@]}" -ne 1 ]; then
  err "ffmpeg 離線包應該剛好一個 .tar.xz，實際 ${#TARBALLS[@]} 個"
  exit 4
fi
TARBALL="${TARBALLS[0]}"
info "壓縮檔：$(basename "$TARBALL")"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/nssms-ffmpeg.XXXXXX")" || { err "建不了暫存目錄"; exit 1; }
# shellcheck disable=SC2329  # 由下一行的 trap 呼叫
cleanup() {
  case "$WORK" in */nssms-ffmpeg.*) rm -rf -- "$WORK" ;; esac
}
trap cleanup EXIT

if ! tar -xJf "$TARBALL" -C "$WORK" 2>"$WORK/tar.err"; then
  err "解不開壓縮檔：$(tail -n 3 "$WORK/tar.err")"
  exit 4
fi
mapfile -t TOPS < <(find "$WORK" -mindepth 1 -maxdepth 1 -type d)
if [ "${#TOPS[@]}" -ne 1 ]; then
  err "壓縮檔應該只有一個頂層目錄"
  exit 4
fi
X="${TOPS[0]}"
for f in bin/ffmpeg bin/ffprobe BUILDINFO.txt COPYING.GPLv2 SHA256SUMS; do
  [ -f "$X/$f" ] || { err "壓縮檔內缺 $f"; exit 4; }
done
if ! (cd "$X" && sha256sum -c --quiet SHA256SUMS); then
  err "壓縮檔內的 SHA256SUMS 校驗失敗"
  exit 4
fi
for t in $TOOLS; do
  grep -q "  bin/$t\$" "$X/SHA256SUMS" || { err "SHA256SUMS 沒有列 bin/$t"; exit 4; }
done
ok "壓縮檔內容校驗通過：$(sed -n 's/^version: //p' "$X/BUILDINFO.txt" | head -n 1)"

# ---- 在本機實測解開的執行檔(不通過就不裝)
probe_ffmpeg() {  # $1=ffmpeg 路徑 $2=暫存目錄 → rc 0 = 有 libx264rgb 而且編得出 2 幀
  local ff="$1" dir="$2" encoders
  "$ff" -hide_banner -version >/dev/null 2>&1 || { err "$ff 執行不起來"; return 1; }
  # 先收進變數再用 [[ ]] 比對,不接 pipe:`ffmpeg -encoders | grep -q` 在 pipefail 下會誤判 ——
  # grep 一找到就結束,ffmpeg 還在寫剩下的清單,吃 SIGPIPE,整條 pipeline 變成失敗(真離線包實際踩過)。
  encoders="$("$ff" -hide_banner -encoders 2>/dev/null)"
  if [[ "$encoders" != *" libx264rgb "* ]]; then
    err "$ff 沒有 libx264rgb 編碼器"
    return 1
  fi
  # 2 幀 16x16 bgr24,與 radar 長錄影同一組關鍵參數(rawvideo bgr24 → libx264rgb qp 0)
  if ! head -c 1536 /dev/zero | "$ff" -hide_banner -loglevel error -y \
        -f rawvideo -pix_fmt bgr24 -s 16x16 -framerate 1 -i pipe:0 \
        -c:v libx264rgb -qp 0 -pix_fmt bgr24 "$dir/probe.mp4" 2>"$dir/probe.err"; then
    err "$ff 試編失敗：$(tail -n 3 "$dir/probe.err")"
    return 1
  fi
  [ -s "$dir/probe.mp4" ] || { err "$ff 試編沒有產出檔案"; return 1; }
}
probe_ffprobe() {  # $1=ffprobe 路徑 $2=probe.mp4
  local fp="$1" mp4="$2" n
  "$fp" -hide_banner -version >/dev/null 2>&1 || { err "$fp 執行不起來"; return 1; }
  n="$("$fp" -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames \
        -of csv=p=0 "$mp4" 2>/dev/null)"
  [ "$n" = "2" ] || { err "$fp 讀試編檔的幀數不對(${n:-空})"; return 1; }
}
if ! probe_ffmpeg "$X/bin/ffmpeg" "$WORK" || ! probe_ffprobe "$X/bin/ffprobe" "$WORK/probe.mp4"; then
  err "解開後的執行檔在本機實測失敗，不安裝。"
  exit 6
fi
ok "解開後實測通過(libx264rgb、2 幀 qp 0 試編、ffprobe 讀回 2 幀)"

# ---- 現況：已是同一版 / 別人放的 / 需要安裝
want_sha() { awk -v p="bin/$1" '$2 == p {print $1}' "$X/SHA256SUMS"; }
file_sha() { sha256sum "$1" 2>/dev/null | awk '{print $1}'; }

state="install"
if [ -f "$MARK_DIR/SHA256SUMS" ] && cmp -s "$MARK_DIR/SHA256SUMS" "$X/SHA256SUMS"; then
  state="ready"
  for t in $TOOLS; do
    [ "$(file_sha "$BIN_DIR/$t")" = "$(want_sha "$t")" ] || state="install"
  done
fi
# 「歸不歸本包管」看記號目錄在不在(安裝時寫執行檔之前就先建它),不看 SHA256SUMS ——
# 後者每次換版前會先刪掉,中途中斷時只剩目錄;那種情況下要能重裝,不能誤判成別人放的。
if [ "$state" = "install" ] && [ ! -d "$MARK_DIR" ]; then
  for t in $TOOLS; do
    if [ -e "$BIN_DIR/$t" ] || [ -L "$BIN_DIR/$t" ]; then
      state="foreign"
      warn "$BIN_DIR/$t 已存在，但不是本包裝的(沒有 $MARK_DIR)。"
    fi
  done
fi

case "$state" in
  ready)
    ok "已是本包這一版，保留現況：$("$BIN_DIR/ffmpeg" -hide_banner -version 2>/dev/null | head -n 1)"
    exit 0 ;;
  foreign)
    warn "不覆蓋別人放的執行檔。要改用本包，請人工確認後移走 $BIN_DIR/ffmpeg 與 ffprobe 再重跑。"
    exit 8 ;;
esac

if [ "$CHECK_ONLY" -eq 1 ]; then
  ok "--check-only：可安裝到 $BIN_DIR；未做任何變更。"
  exit 5
fi

# ---- 取得權限(比照 install_tmux_offline.sh)
OWN=(-o root -g root)
if [ "$(id -u)" -eq 0 ]; then
  SUDO=()
elif [ "$PREFIX" != "/usr/local" ] && [ -w "$PREFIX" ]; then
  SUDO=(); OWN=()          # 只有測試會走到:自己可寫的暫存 prefix
elif ! command -v sudo >/dev/null 2>&1; then
  err "缺少 root/sudo，無法寫入 $BIN_DIR。"
  exit 1
elif sudo -n true 2>/dev/null; then
  SUDO=(sudo)
elif [ -t 0 ]; then
  info "接下來需要輸入一次 sudo 密碼。"
  SUDO=(sudo)
else
  err "非互動終端機且 sudo 需要密碼，無法安裝 ffmpeg。"
  exit 3
fi

info "安裝到 $BIN_DIR ..."
"${SUDO[@]}" mkdir -p "$BIN_DIR" "$MARK_DIR" || { err "建不了 $BIN_DIR / $MARK_DIR"; exit 1; }
# 換上新版之前先讓記號失效:中途失敗時下次會重裝,而不是誤判成「已是同一版」。
"${SUDO[@]}" rm -f "$MARK_DIR/SHA256SUMS" || { err "移不掉舊記號"; exit 1; }
for t in $TOOLS; do
  if ! "${SUDO[@]}" install -m 0755 "${OWN[@]}" "$X/bin/$t" "$BIN_DIR/.$t.nssms-new" \
     || ! "${SUDO[@]}" mv -f "$BIN_DIR/.$t.nssms-new" "$BIN_DIR/$t"; then
    "${SUDO[@]}" rm -f "$BIN_DIR/.$t.nssms-new" 2>/dev/null
    err "寫入 $BIN_DIR/$t 失敗"
    exit 1
  fi
done
for f in BUILDINFO.txt COPYING.GPLv2 SHA256SUMS; do   # SHA256SUMS 最後寫 = 安裝完成
  "${SUDO[@]}" install -m 0644 "${OWN[@]}" "$X/$f" "$MARK_DIR/$f" || { err "寫入 $MARK_DIR/$f 失敗"; exit 1; }
done

# ---- 裝好之後用裝上去的那兩支再測一次
mkdir -p "$WORK/post"
if ! probe_ffmpeg "$BIN_DIR/ffmpeg" "$WORK/post" || ! probe_ffprobe "$BIN_DIR/ffprobe" "$WORK/post/probe.mp4"; then
  err "安裝後實測失敗。"
  exit 1
fi
on_path="$(command -v ffmpeg 2>/dev/null || true)"
if [ "$PREFIX" = "/usr/local" ] && [ "$on_path" != "$BIN_DIR/ffmpeg" ]; then
  warn "PATH 上第一個 ffmpeg 是 ${on_path:-(找不到)},不是 $BIN_DIR/ffmpeg —— 用到的程式請檢查 PATH。"
fi
ok "ffmpeg 已安裝：$("$BIN_DIR/ffmpeg" -hide_banner -version 2>/dev/null | head -n 1)"
exit 0
