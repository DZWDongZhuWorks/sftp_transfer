#!/usr/bin/env bash
#
# install_tmux_offline.sh — 以隨附的 .deb 離線補齊 tmux
# ---------------------------------------------------------------------------
# 為什麼需要:scheduler 的整個開機服務模型建立在 tmux 之上 ——
#   reboot_script/start_*.sh   以 `tmux new-session` 啟動每個 session 型專案
#   reboot_launcher.sh         以 `tmux has-session` 做差異對帳
#   automation_health_check.py 以「角色 → 預期 session」清單驗收
# 而船上沒有對外網路,`apt install tmux` 不成立。缺 tmux 時的症狀又特別難判讀:
# start_*.sh 各自 exit 2、啟動器「記錄並繼續」、每 30 分鐘重試同一批永遠補不起來
# —— 要交叉三份 log 才會發現原因是「缺一個指令」而不是「服務起不來」。
#
# 所以 tmux 用與 wheelhouse 完全同構的方式處理:預先蒐集好的 .deb 隨離線包派送,
# 安裝時 `dpkg -i`、全程不連網。這就是 `pip install --no-index --find-links` 的
# 系統套件版本,語意與同層的 install_virtualenv_offline.sh 一致。
#
# 呼叫者:sftp_transfer/deploy/deploy_offline.sh(部署 / 再部署時)。也可以手動跑。
# 冪等:tmux 已可用就什麼都不做。
#
# 兩條安裝路徑:
#   主路徑   sudo dpkg -i  —— 正式登錄進 dpkg 資料庫,之後 apt 也認得。
#   後備路徑 dpkg-deb -x   —— 解到 ~/.local,免 root。用於沒有 sudo 密碼可輸入的場合
#                            (非互動終端機)。systemd user manager 的預設 PATH 已含
#                            ~/.local/bin(systemd ≥ 248),所以 nssms-boot 找得到它,
#                            不需要改任何 unit 檔。
#
# ⚠ deb 與 wheel 一樣**平台綁定**:本包內的 deb 是 Ubuntu 22.04 (jammy) / arm64。
#   換架構或換發行版需重新蒐集,見 deploy/README.md「未來如何更新 / 重建 debs」。
#
# ⚠ 刻意**不**打包 libc6。tmux 只要求 libc6 >= 2.34,那是基底系統的東西;
#   在後備路徑上用 LD_LIBRARY_PATH 蓋掉 libc 是會把機器弄壞的操作。
#
# 用法:
#   ./install_tmux_offline.sh                # 需要時以 dpkg 離線安裝 tmux
#   ./install_tmux_offline.sh --check-only   # 只回報現況,不做任何變更
#   ./install_tmux_offline.sh --skip-verify  # 略過 deb 的 sha256 校驗
#   ./install_tmux_offline.sh --user-local   # 強制走免 root 路徑(解到 ~/.local)
#
# deb 來源優先序:TMUX_DEBS_DIR 指定 → 腳本同層 debs/ → 當前目錄 ./debs
#
# 離開碼:
#   0  已就緒(本來就有,或 dpkg 安裝成功)
#   1  安裝失敗(dpkg / 解壓失敗,或裝完 tmux -V 仍跑不起來)
#   2  參數錯誤
#   3  已以免 root 方式裝到 ~/.local/bin(可用,但不在 dpkg 資料庫裡)
#   4  離線包不完整(找不到 debs/ 或 sha256 校驗失敗)—— 未做任何變更
#   5  --check-only 下:尚未安裝,但離線包齊備、可安裝
# ---------------------------------------------------------------------------
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SELF="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"

CHECK_ONLY=0
SKIP_VERIFY=0
FORCE_USER_LOCAL=0

# 免 root 後備路徑的落點。刻意用 nssms- 前綴的獨立目錄:它不是套件管理員管的東西,
# 要能一眼看出來自哪裡、也能整個目錄刪掉還原。
USER_PREFIX="${HOME}/.local/lib/nssms-tmux"
USER_BIN="${HOME}/.local/bin"

# --- 顏色輸出(與 install_docker_group.sh / deploy_offline.sh 一致)---------
if [ -t 1 ]; then
  R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[36m'; N=$'\e[0m'
else
  R=""; G=""; Y=""; B=""; N=""
fi
info()  { printf "%s[INFO]%s %s\n"  "$B" "$N" "$*"; }
ok()    { printf "%s[ OK ]%s %s\n"  "$G" "$N" "$*"; }
warn()  { printf "%s[WARN]%s %s\n"  "$Y" "$N" "$*"; }
err()   { printf "%s[FAIL]%s %s\n"  "$R" "$N" "$*" >&2; }

# --help 只印檔頭那一段(第 2 行到第一個非註解行為止)。與 deploy_offline.sh 同一個
# 做法:`grep '^#'` 會把全檔的 column-0 實作註解一起倒出來,而這支腳本的實作註解不少。
usage() { awk 'NR == 1 { next } !/^#/ { exit } { sub(/^# ?/, ""); print }' "$0"; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --check-only) CHECK_ONLY=1 ;;
    --skip-verify) SKIP_VERIFY=1 ;;
    --user-local) FORCE_USER_LOCAL=1 ;;
    -h|--help)    usage ;;
    *) err "未知參數:$1"; echo "執行 --help 查看用法" >&2; exit 2 ;;
  esac
  shift
done

echo "==========================================================="
echo " tmux 離線安裝 (offline install — 隨附 .deb，不連網)"
echo "==========================================================="

# --- 1. 現況:tmux 到底能不能用 --------------------------------------------
# 判準刻意是「跑得起來」而不是「dpkg 說裝了」:兩者會不一致(套件在資料庫裡但二進位
# 被刪、或 so 缺失),而我們在意的是 start_*.sh 呼叫 tmux 會不會成功。
tmux_usable() { command -v tmux >/dev/null 2>&1 && tmux -V >/dev/null 2>&1; }

if tmux_usable; then
  ok "tmux 已可用:$(tmux -V)  ($(command -v tmux))"
  ok "無需安裝。"
  echo "==========================================================="
  exit 0
fi

if command -v tmux >/dev/null 2>&1; then
  warn "找到 tmux($(command -v tmux))但執行失敗 —— 二進位或共享程式庫有問題,將重新安裝。"
else
  warn "系統沒有 tmux —— 所有 session 型專案(shm / radar / wave / ecdis / flag)都起不來。"
fi

# --- 2. 定位 deb 目錄 ------------------------------------------------------
# 來源優先序與 install_virtualenv_offline.sh 的 WHEELS_DIR 一致。
DEBS_DIR="${TMUX_DEBS_DIR:-$SCRIPT_DIR/debs}"
if [ ! -d "$DEBS_DIR" ] && [ -d "./debs" ]; then
  DEBS_DIR="./debs"
fi
info "deb 目錄      : $DEBS_DIR"

incomplete() {  # 離線包不完整:一律不做任何變更,回 4
  err "$*"
  echo "-----------------------------------------------------------"
  err "離線包不完整,未做任何變更。"
  err "取得 deb 的方式見 $SCRIPT_DIR/README.md「未來如何更新 / 重建 debs」。"
  echo "==========================================================="
  exit 4
}

[ -d "$DEBS_DIR" ] || incomplete "找不到 deb 目錄:$DEBS_DIR"

# shellcheck disable=SC2207
DEBS=($(find "$DEBS_DIR" -maxdepth 1 -name '*.deb' | sort))
[ "${#DEBS[@]}" -gt 0 ] || incomplete "$DEBS_DIR 內沒有任何 .deb 檔案"
ok "找到 ${#DEBS[@]} 個 deb 檔案"

# --- 3. sha256 校驗 --------------------------------------------------------
# 與 deploy_offline.sh 的 wheel 校驗同一個做法:只取 64 位 hex 開頭的行,讓 MANIFEST
# 可以帶檔頭註解。校驗基準目錄是 debs/ 自己(所以 MANIFEST 放在 debs/ 裡面)。
DEBS_MANIFEST="${DEBS_DIR}/MANIFEST.txt"
if [ "$SKIP_VERIFY" -eq 1 ]; then
  warn "--skip-verify:略過 deb sha256 校驗"
elif [ ! -f "$DEBS_MANIFEST" ]; then
  warn "找不到 $DEBS_MANIFEST,略過 deb sha256 校驗"
else
  info "以 MANIFEST.txt 校驗 deb sha256 ..."
  if ( cd "$DEBS_DIR" && grep -E '^[0-9a-f]{64}  ' MANIFEST.txt | sha256sum -c --quiet ) 2>/dev/null; then
    ok "所有 deb 檔案 sha256 校驗通過"
  else
    incomplete "deb 校驗失敗,檔案可能損毀或被竄改。可用 --skip-verify 強制略過。"
  fi
fi

# --- 4. 算出「真正缺的」套件 ------------------------------------------------
# 不要無腦 `dpkg -i debs/*.deb`。libtinfo6 這種核心程式庫在機上幾乎一定已經是同版本,
# 對一台正在運行的機器做無謂的 reinstall 是白白製造風險 —— 只裝真的缺(或版本不足)的。
#
# 例外:tmux 本身。若它在 dpkg 資料庫裡卻跑不起來(第 1 段已判定),就必須重裝它,
# 否則這支腳本會得出「沒有東西要裝」然後結束,而 tmux 依然不能用。
NEEDED=()
SKIPPED=()
for deb in "${DEBS[@]}"; do
  pkg="$(dpkg-deb -f "$deb" Package 2>/dev/null)"
  ver="$(dpkg-deb -f "$deb" Version 2>/dev/null)"
  if [ -z "$pkg" ] || [ -z "$ver" ]; then
    incomplete "無法讀取 deb 的中控資訊(檔案可能損毀):$deb"
  fi
  cur=""
  if [ "$(dpkg-query -W -f='${db:Status-Status}' "$pkg" 2>/dev/null)" = "installed" ]; then
    cur="$(dpkg-query -W -f='${Version}' "$pkg" 2>/dev/null)"
  fi
  if [ "$pkg" = "tmux" ]; then
    NEEDED+=("$deb")
    [ -n "$cur" ] \
      && info "  ${pkg}:已安裝 $cur 但無法執行 → 重新安裝 $ver" \
      || info "  ${pkg}:未安裝 → 安裝 $ver"
  elif [ -z "$cur" ]; then
    NEEDED+=("$deb")
    info "  ${pkg}:未安裝 → 安裝 $ver"
  elif dpkg --compare-versions "$cur" ge "$ver"; then
    SKIPPED+=("$pkg")
  else
    NEEDED+=("$deb")
    info "  ${pkg}:已安裝 $cur(低於 $ver)→ 升級"
  fi
done
[ "${#SKIPPED[@]}" -gt 0 ] && ok "已滿足、不動的依賴:${SKIPPED[*]}"

# --- 5. --check-only:只回報 ----------------------------------------------
if [ "$CHECK_ONLY" -eq 1 ]; then
  echo ""
  warn "tmux 尚未可用,但離線包齊備 —— 共 ${#NEEDED[@]} 個 deb 待安裝。"
  warn "執行以下指令安裝(需輸入一次密碼):"
  warn "  bash $SELF"
  ok "--check-only 完成:未做任何變更。"
  echo "==========================================================="
  exit 5
fi

# --- 6. 主路徑:dpkg -i ----------------------------------------------------
# 一次把所有 deb 交給 dpkg,讓它自己處理相依順序 —— 分次裝反而可能中途卡在
# 「依賴還沒到」。全部離線,絕不觸發 apt。
verify_and_report() {  # $1=離開碼(成功時);失敗一律 1
  if ! tmux_usable; then
    err "安裝後 tmux 仍無法執行。"
    err "以 ldd 檢查缺哪個共享程式庫:  ldd \$(command -v tmux)"
    echo "==========================================================="
    exit 1
  fi
  echo "-----------------------------------------------------------"
  ok "tmux 已就緒:$(tmux -V)  ($(command -v tmux))"
}

install_via_dpkg() {
  local dpkg_cmd
  if [ "$(id -u)" -eq 0 ]; then
    dpkg_cmd=(dpkg -i)
  else
    dpkg_cmd=(sudo dpkg -i)
  fi
  info "以 ${dpkg_cmd[*]} 安裝 ${#NEEDED[@]} 個 deb ..."
  if ! "${dpkg_cmd[@]}" "${NEEDED[@]}"; then
    rc=$?
    err "dpkg -i 失敗(exit=$rc)。"
    err "常見原因是離線包缺了某個依賴 —— **不要**跑 apt-get -f install(船上不通網路)。"
    err "改以 ldd 找出缺什麼,再從有網路的同平台機器蒐集對應的 deb:"
    err "  dpkg -I ${NEEDED[0]} | grep Depends"
    echo "==========================================================="
    exit 1
  fi
  verify_and_report
  echo "==========================================================="
  exit 0
}

# --- 7. 後備路徑:dpkg-deb -x 到 ~/.local(免 root)------------------------
install_user_local() {
  info "以免 root 方式安裝到 $USER_PREFIX ..."
  mkdir -p "$USER_PREFIX" "$USER_BIN" || { err "無法建立 $USER_PREFIX"; exit 1; }
  local deb
  for deb in "${NEEDED[@]}"; do
    if ! dpkg-deb -x "$deb" "$USER_PREFIX"; then
      err "解壓失敗:$deb"
      echo "==========================================================="
      exit 1
    fi
  done

  local real_tmux="$USER_PREFIX/usr/bin/tmux"
  [ -x "$real_tmux" ] || { err "解壓後找不到 $real_tmux"; exit 1; }

  # 只有真的解出了共享程式庫才需要 wrapper。這個區分是刻意的:LD_LIBRARY_PATH 會被
  # tmux 開出來的**每一個** shell 與子行程繼承,而 session 裡跑的是各專案的 python /
  # 啟動腳本 —— 沒必要時不該讓它們的動態連結器行為被我們改掉。
  # 常見情形(機上只缺 tmux、依賴都在)就落在這裡:單純一條 symlink。
  local libdirs
  libdirs="$(find "$USER_PREFIX" -name 'lib*.so*' -printf '%h\n' 2>/dev/null | sort -u | paste -sd: -)"
  rm -f "$USER_BIN/tmux"
  if [ -z "$libdirs" ]; then
    ln -s "$real_tmux" "$USER_BIN/tmux" || { err "無法建立 $USER_BIN/tmux"; exit 1; }
    ok "已建立 symlink:$USER_BIN/tmux → $real_tmux"
  else
    cat > "$USER_BIN/tmux" <<EOF
#!/bin/sh
# 由 install_tmux_offline.sh 自動產生 —— 免 root 安裝的 tmux。
# 隨附的共享程式庫也一起解到了 $USER_PREFIX,所以要帶著 LD_LIBRARY_PATH 才找得到。
# 刻意只在這支 wrapper 的範圍內設定;整份安裝可用 rm -rf 兩個路徑還原:
#   rm -rf "$USER_PREFIX" "$USER_BIN/tmux"
LD_LIBRARY_PATH="$libdirs\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}" \\
  exec "$real_tmux" "\$@"
EOF
    chmod +x "$USER_BIN/tmux" || { err "無法設定 $USER_BIN/tmux 執行權限"; exit 1; }
    ok "已建立 wrapper:$USER_BIN/tmux(LD_LIBRARY_PATH=$libdirs)"
  fi

  # 本 shell 的 PATH 可能還沒有 ~/.local/bin,但驗證要看得到剛裝的那一支。
  # 先留一份原始 PATH:最後那句「你的 shell 找不到它」要對**加之前**的 PATH 判斷,
  # 否則永遠不會成立(我們自己剛把它加進去了)。
  local orig_path="$PATH"
  PATH="$USER_BIN:$PATH"
  verify_and_report
  echo ""
  warn "這是免 root 安裝:tmux **不在** dpkg 資料庫裡,apt 不知道它存在。"
  warn "日後若以 apt 正式安裝 tmux,請先移除它以免蓋住系統版本:"
  warn "  rm -rf \"$USER_PREFIX\" \"$USER_BIN/tmux\""
  echo ""
  info "systemd user manager 的預設 PATH 已含 ~/.local/bin,所以 nssms-boot 找得到它。"
  info "確認:  systemctl --user show-environment | grep ^PATH"
  if ! printf '%s' ":${orig_path}:" | grep -q ":${USER_BIN}:"; then
    warn "但**你這個互動 shell** 的 PATH 沒有 $USER_BIN —— 重登入後才會有。"
  fi
  echo "==========================================================="
  exit 3
}

# --- 8. 選路徑 -------------------------------------------------------------
# 主路徑需要 root。判斷順序刻意與 install_docker_group.sh 一致,但結論不同:那支腳本
# 在「非互動 + 需要密碼」時只能放棄,這支還有免 root 的後備,所以是退路而不是失敗。
if [ "$FORCE_USER_LOCAL" -eq 1 ]; then
  info "--user-local:直接走免 root 路徑。"
  install_user_local
elif [ "$(id -u)" -eq 0 ]; then
  install_via_dpkg
elif ! command -v sudo >/dev/null 2>&1; then
  warn "既不是 root、也找不到 sudo —— 改走免 root 路徑。"
  install_user_local
elif sudo -n true 2>/dev/null; then
  install_via_dpkg
elif [ -t 0 ]; then
  info "接下來需要輸入一次 sudo 密碼(dpkg -i 需要 root)。"
  install_via_dpkg
else
  warn "需要 sudo 密碼,但這不是互動終端機 —— 改走免 root 路徑。"
  warn "若要正式登錄進 dpkg 資料庫,之後請手動執行:  bash $SELF"
  install_user_local
fi
