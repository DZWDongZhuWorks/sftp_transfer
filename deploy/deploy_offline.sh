#!/usr/bin/env bash
#
# deploy_offline.sh — 船機的唯一一次人工安裝入口
# ---------------------------------------------------------------------------
# ⚠ 這支腳本的職責早已超出檔名。它最初只做一件事:用 deploy/wheelhouse/ 內預先下載的
#   wheel 為 sftp_transfer 建立專屬 venv(所以叫 offline —— 指的是**不從 PyPI 下載**,
#   不是「機器沒有網路」;船上是連得到 SFTP 的 61.56.200.137 的,OTA 整套機制就靠它)。
#   後來陸續被加上 timer、sudoers 白名單、心跳服務、存活巡檢(見 6fcbcad / 6192841 /
#   708c781),於是實際上變成「船機唯一的一次性人工安裝流程」。檔頭照實寫,避免誤導。
#
# 三個階段:
#
#   A. 一次性人工設定(**所有需要你輸入的東西都集中在這裡**)
#      1) 船舶身分檔 share/.env/vessel_basic_info.json(vsl_name / ipc)
#         —— 順便偵測殘留的接管旗標與舊格式 failover_state.json
#      2) install_autostart.sh   → nssms-boot.service + linger
#      3) 舊 clink_* 遷移        → 停用/移除三支 system unit + 加入 gpio 群組(需密碼)
#         **必須排在 6) 之前**:舊 clink_alarm_controller / clink_board_server 還活著時,
#         新的 nssms-alarm-controller / nssms-board-server 會撞 port 起不來。
#      4) install_timers.sh      → 7 支週期排程 timer
#      5) sudoers 白名單         → reboot / teamviewer 需要(這一步要輸入一次密碼)
#      6) install_services.sh    → 4 支常駐服務:heartbeat(雙向心跳/接管)、
#                                  alarm-controller / board-server / button(綁實體 IPC-1)
#      7) 詢問「之後要不要立即執行完整啟動流程」——**只問，執行在階段 C**
#      這一段結束後會印「以下不再需要任何輸入」,操作者可以離開終端機。
#
#   B. sftp_transfer 專屬 venv(離線、無人干預)
#      wheelhouse + MANIFEST.txt sha256 校驗 → virtualenv → pip --no-index → 匯入驗證
#      路徑預設 ~/venv/wanhai_nssms/share/sftp_transfer(與 radar / SHM 的慣例一致)
#
#   C. 完整啟動流程與驗證(無人干預)
#      reboot_launcher.sh:掛載資料碟 → update_booster(SFTP 拉最新程式碼)→ 依角色安裝
#      各專案環境並啟動服務 → 然後才跑 health_check 與 automation_health_check
#      (順序是刻意的:啟動要在 venv 之後——SFTP 下載要用它;要在巡檢之前——服務起來後
#       那份巡檢才第一次真的有意義)
#      可用 --no-launch 關閉;選 n 也不會壞,下次開機 nssms-boot 會跑同一支啟動器。
#
# 目標平台：Linux aarch64 / CPython 3.10 / glibc >= 2.34  (NVIDIA Tegra, mic-733ao)
#
# 用法：
#   ./deploy_offline.sh                 # 建立/更新專屬 venv，安裝執行期相依 + 測試堆疊（預設）
#   ./deploy_offline.sh --skip-tests    # 不安裝 pytest 測試堆疊，健康檢查也略過單元測試
#   ./deploy_offline.sh --with-tests    # （保留相容；現為預設，明確要求安裝測試堆疊）
#   ./deploy_offline.sh --recreate      # 砍掉重建 venv（乾淨安裝）
#   ./deploy_offline.sh --no-health-check # 部署後不自動執行能力／自動化健康檢查
#   ./deploy_offline.sh --no-launch      # 部署後不執行啟動流程（不下載程式碼、不啟動服務）
#   ./deploy_offline.sh --check-only    # 只驗證 wheel 完整性與環境，不安裝
#   ./deploy_offline.sh --venv /path/to/venv        # 自訂 venv 路徑
#   ./deploy_offline.sh --python /usr/bin/python3.10 # 指定建立 venv 用的直譯器
#
# 特性：
#   * venv 安裝全程 --no-index，永不連 PyPI（階段 C 的 SFTP 下載另當別論）。
#   * 以 python3.10 -m virtualenv 建立 venv（與 radar / SHM 一致），不再依賴系統的
#     python3-venv / ensurepip；若 python3.10 尚無 virtualenv，會用隨附的
#     install_virtualenv_offline.sh + virtualenv_wheels/ 先離線補齊。
#   * venv 與系統 site-packages 隔離。
#   * 安裝前以 MANIFEST.txt 校驗 wheel sha256（可用 --skip-verify 跳過）。
#   * 安裝後在 venv 內驗證關鍵套件可正常匯入。
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WHEELHOUSE="${SCRIPT_DIR}/wheelhouse"
MANIFEST="${SCRIPT_DIR}/MANIFEST.txt"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SHARE_DIR="$(dirname "$PROJECT_DIR")"
# 船舶基本資訊檔：供各設定檔的 {vsl_name}/{ipc} 佔位符替換使用（見 settings.py）。
VESSEL_INFO="${SHARE_DIR}/.env/vessel_basic_info.json"

DEFAULT_VENV="${HOME}/venv/wanhai_nssms/share/sftp_transfer"
VENV_DIR="${DEFAULT_VENV}"
PYTHON_BIN=""          # 空字串＝自動偵測（優先 python3.10，與下游 install_env.sh 一致）
# 預設安裝測試堆疊（pytest 等），讓部署後的 health_check 預設就會實際跑單元測試。
# 以 --skip-tests 關閉：不裝測試套件，且轉傳 --skip-tests 讓 health_check 略過。
INSTALL_TESTS=1
CHECK_ONLY=0
SKIP_VERIFY=0
RECREATE=0
RUN_HEALTH=1
# 部署完成後是否立即跑一次完整啟動流程(下載程式碼 + 裝環境 + 啟動服務)。
RUN_LAUNCH=1

# --- 顏色輸出 --------------------------------------------------------------
if [ -t 1 ]; then
  R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[36m'; N=$'\e[0m'
else
  R=""; G=""; Y=""; B=""; N=""
fi
info()  { printf "%s[INFO]%s %s\n"  "$B" "$N" "$*"; }
ok()    { printf "%s[ OK ]%s %s\n"  "$G" "$N" "$*"; }
warn()  { printf "%s[WARN]%s %s\n"  "$Y" "$N" "$*"; }
err()   { printf "%s[FAIL]%s %s\n"  "$R" "$N" "$*" >&2; }

usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# --- 解析參數 --------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --with-tests)  INSTALL_TESTS=1 ;;
    --skip-tests)  INSTALL_TESTS=0 ;;
    --check-only)  CHECK_ONLY=1 ;;
    --skip-verify) SKIP_VERIFY=1 ;;
    --recreate)    RECREATE=1 ;;
    --no-health-check) RUN_HEALTH=0 ;;
    --no-launch)   RUN_LAUNCH=0 ;;
    --venv)        VENV_DIR="${2:?--venv 需要一個路徑參數}"; shift ;;
    --python)      PYTHON_BIN="${2:?--python 需要一個路徑參數}"; shift ;;
    -h|--help)     usage ;;
    *) err "未知參數：$1"; echo "執行 --help 查看用法" >&2; exit 2 ;;
  esac
  shift
done

# 未指定 --python 時自動選直譯器：優先 python3.10（下游 install_env.sh 與 wheelhouse
# 的 cp310 wheel 皆以此為準），退而求其次才用 python3。
if [ -z "$PYTHON_BIN" ]; then
  if command -v python3.10 >/dev/null 2>&1; then
    PYTHON_BIN="python3.10"
  else
    PYTHON_BIN="python3"
  fi
fi

echo "==========================================================="
echo " sftp_transfer 離線部署 (offline deploy — 專屬 venv)"
echo "==========================================================="

# --- 前置檢查 --------------------------------------------------------------
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  err "找不到 Python 直譯器：$PYTHON_BIN"; exit 1
fi
PY_VER="$("$PYTHON_BIN" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))')"
PY_TAG="$("$PYTHON_BIN" -c 'import sys;print("cp%d%d"%sys.version_info[:2])')"
info "基底直譯器    : $PYTHON_BIN ($PY_VER, $PY_TAG)"
info "系統架構      : $(uname -m) ($(uname -s) $(uname -r))"
info "Wheelhouse    : $WHEELHOUSE"
info "專案目錄      : $PROJECT_DIR"
info "專屬 venv     : $VENV_DIR"
info "船舶資訊檔    : $VESSEL_INFO"

# --- 船舶基本資訊檔（vessel_basic_info.json）檢查 / 互動建立 ----------------
# 剛啟動就先確認它存在且內容正確（需含非空的 vsl_name / ipc）；
# 缺少或內容不正確時，以互動問答讓使用者輸入並建立該檔。
# 印出現有內容；有效回傳 0、檔案不存在回傳 3、內容不正確回傳 2、
# 「欄位有效但處於接管中」回傳 4。
#
# 為什麼需要區分 4:身分檔現在同時承載接管旗標(failover / failover_since)。若只檢查
# vsl_name/ipc 非空，一台帶著 failover=true 的機器重新部署會被判定「有效，沿用現有內容」
# ——**靜默保留接管狀態**。而重新部署幾乎總是意味著機器被重裝、搬移或換角色，那個旗標
# 幾乎確定是殘留;殘留下去會讓本機一直以 emer 角色啟動、與對方形成雙主。
vessel_info_show() {
  "$PYTHON_BIN" - "$VESSEL_INFO" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        info = json.load(f)
except FileNotFoundError:
    sys.exit(3)
except Exception as e:  # noqa
    print(f"內容無法解析：{e}")
    sys.exit(2)
if not isinstance(info, dict):
    print("內容不是 JSON 物件")
    sys.exit(2)
for k, v in info.items():
    print(f"{k} = {v}")
missing = [k for k in ("vsl_name", "ipc") if not str(info.get(k, "")).strip()]
if missing:
    print("缺少或為空的必要欄位：" + ", ".join(missing))
    sys.exit(2)
# 真假白名單與 scheduler/failover/role.py 的 is_failover_on() 一致。
if str(info.get("failover", "")).strip().lower() in {"true", "1", "yes"}:
    sys.exit(4)
sys.exit(0)
PY
}

# 清除身分檔的接管旗標（只移除那兩個欄位，不動 ipc / vsl_name）。
clear_failover_flag() {
  "$PYTHON_BIN" - "$VESSEL_INFO" <<'PY'
import json, os, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    info = json.load(f)
for key in ("failover", "failover_since", "failover_since_iso"):
    info.pop(key, None)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(info, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.replace(tmp, path)   # 原子替換，避免任何讀者看到半寫檔
PY
}

vessel_get() {  # $1=key → 印出現有值（去頭尾空白），讀取失敗則印空字串
  "$PYTHON_BIN" - "$VESSEL_INFO" "$1" <<'PY' 2>/dev/null || true
import json, sys
try:
    info = json.load(open(sys.argv[1], encoding="utf-8"))
    print(str(info.get(sys.argv[2], "")).strip())
except Exception:
    print("")
PY
}

prompt_field() {  # $1=提示文字 $2=key → 結果放進 REPLY_VAL（不可為空，有舊值則當預設）
  local cur val
  cur="$(vessel_get "$2")"
  while true; do
    if [ -n "$cur" ]; then
      read -r -p "  $1 [$cur]: " val || val=""
      val="${val:-$cur}"
    else
      read -r -p "  $1: " val || val=""
    fi
    val="$(printf '%s' "$val" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    if [ -n "$val" ]; then REPLY_VAL="$val"; return 0; fi
    warn "  不可為空，請重新輸入。"
  done
}

create_vessel_info() {
  if [ ! -t 0 ]; then
    err "非互動終端機，無法以問答建立船舶資訊檔。"
    err "請手動建立 $VESSEL_INFO ，內容範例：{\"vsl_name\": \"WH289\", \"ipc\": \"IPC-1\"}"
    exit 1
  fi
  # 本函式是整檔覆寫（只寫 vsl_name / ipc），所以會連帶清掉接管旗標。這在「內容不正確
  # 要重建」的路徑上正是想要的效果，但必須明說 —— 否則使用者不會知道自己剛剛結束了接管。
  # 註：A4 的「只碰兩個欄位、絕不新建」規則約束的是 heartbeat.py 與 failover_ctl.sh
  # 這兩個自動寫入者；deploy 是身分檔的產生者，整檔覆寫是刻意的例外。
  if [ -f "$VESSEL_INFO" ] && grep -q '"failover"' "$VESSEL_INFO" 2>/dev/null; then
    warn "注意：重建身分檔會一併清除接管旗標（failover / failover_since）。"
    warn "若本機正在替對方接管，重建後角色會回到正常值。"
  fi
  local vsl ipc ans
  while true; do
    echo ""
    info "請輸入船舶基本資訊："
    prompt_field "船名 vsl_name（例：WH289）" "vsl_name"; vsl="$REPLY_VAL"
    prompt_field "IPC 代號 ipc（例：IPC-1）"  "ipc";      ipc="$REPLY_VAL"
    echo ""
    echo "  即將寫入 $VESSEL_INFO ："
    echo "    vsl_name = $vsl"
    echo "    ipc      = $ipc"
    read -r -p "  確認無誤？[Y/n] " ans || ans=""
    case "$ans" in
      ""|Y|y) break ;;
      *) warn "重新輸入。" ;;
    esac
  done
  mkdir -p "$(dirname "$VESSEL_INFO")"
  VSL_NAME="$vsl" IPC="$ipc" "$PYTHON_BIN" - "$VESSEL_INFO" <<'PY'
import json, os, sys
data = {"vsl_name": os.environ["VSL_NAME"], "ipc": os.environ["IPC"]}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  ok "已建立/更新船舶基本資訊檔：$VESSEL_INFO"
}

# --check-only 的承諾是「只驗證，不安裝」（見檔頭用法）。以下這一整段「一次性設定」
# 會建立身分檔、清除接管旗標、刪除舊格式狀態檔、佈署 systemd unit 與 sudoers ——
# 全部都是變更。所以 --check-only 一律只回報現況、不執行任何動作。
#
# 這個 guard 是後補的：CHECK_ONLY 原本要到 venv 那一段才被檢查，於是 --check-only 會
# 一路把身分檔與 systemd 都改掉。加入「清除接管旗標」「刪除舊格式狀態檔」之後風險升級
# ——對一台真的正在接管中的船,那會直接讓它失去接管。
DRYRUN_NOTE=""
if [ "$CHECK_ONLY" -eq 1 ]; then
  DRYRUN_NOTE="（--check-only：只回報，不執行）"
  echo ""
  info "--check-only：以下一次性設定只回報現況，不做任何變更。"
fi

echo ""
info "檢查船舶基本資訊檔 ..."
set +e
VESSEL_OUT="$(vessel_info_show)"; VESSEL_RC=$?
set -e
[ -n "$VESSEL_OUT" ] && printf '%s\n' "$VESSEL_OUT" | sed 's/^/       /'
if [ "$CHECK_ONLY" -eq 1 ]; then
  case "$VESSEL_RC" in
    0) ok   "船舶基本資訊檔有效。" ;;
    4) warn "船舶基本資訊檔有效，但**帶著 failover 接管旗標** —— 本機會以 emer 角色啟動。$DRYRUN_NOTE" ;;
    3) warn "找不到船舶基本資訊檔；正式部署時會以互動問答建立。$DRYRUN_NOTE" ;;
    *) warn "船舶基本資訊檔內容不正確；正式部署時會重新建立。$DRYRUN_NOTE" ;;
  esac
elif [ "$VESSEL_RC" -eq 0 ]; then
  ok "船舶基本資訊檔有效，沿用現有內容。"
elif [ "$VESSEL_RC" -eq 4 ]; then
  # 欄位有效，但帶著接管旗標。重新部署幾乎總是意味著機器被重裝、搬移或換角色，
  # 所以預設清除;真的正在接管中(對方確實故障)才選 n。
  echo ""
  warn "══════════════════ 本機處於接管狀態 ══════════════════"
  warn "身分檔帶有 failover 旗標，本機會以 emer 角色啟動（等同對方的完整服務清單）。"
  warn "═══════════════════════════════════════════════════"
  # 不猜預設值,把證據給操作者。成本是不對稱的:
  #   誤清 → 若對方真的死了,船上立刻失去那些服務,而且沒有任何告警(安靜的嚴重故障)
  #   誤留 → 啟動器每次都印雙主警告、巡檢 24 小時後升 WARN、status 直接顯示(很吵,可回復)
  # 所以預設保留(Enter = N),並先跑一次唯讀的 status 讓操作者看對方到底活不活著。
  FAILOVER_CTL="${SHARE_DIR}/scheduler/failover/failover_ctl.sh"
  if [ -f "$FAILOVER_CTL" ]; then
    echo ""
    info "先確認對方是否還活著（failover_ctl.sh status，唯讀）："
    set +e
    bash "$FAILOVER_CTL" status 2>&1 | sed 's/^/       /'
    set -e
  fi
  echo ""
  warn "判讀:"
  warn "  * 上面顯示對方**有回應** → 這個旗標是殘留,應該清除(否則兩台同時跑同一批服務)"
  warn "  * 上面顯示對方**無回應** → 本機可能真的在替它接管,清除會讓船上失去那些服務"
  clear_ans=""
  if [ -t 0 ]; then
    read -r -p "  清除接管旗標？（不確定就按 Enter 保留，之後可用 failover_ctl.sh clear）[y/N] " \
      clear_ans || clear_ans=""
  else
    warn "非互動終端機：不擅自更動身分檔，保留現狀。"
    warn "如需清除請執行：bash $FAILOVER_CTL clear"
    clear_ans="n"
  fi
  case "$clear_ans" in
    Y|y)
      if clear_failover_flag; then
        ok "已清除接管旗標（vsl_name / ipc 未變更）。"
        info "角色要生效仍需執行：bash ${SHARE_DIR}/scheduler/reboot_launcher.sh --reconcile"
      else
        warn "清除失敗，保留現狀。請改用 failover_ctl.sh clear 處理。"
      fi
      ;;
    *) warn "保留接管旗標。本機將繼續以 emer 角色啟動。" ;;
  esac
elif [ "$VESSEL_RC" -eq 3 ]; then
  warn "找不到船舶基本資訊檔，將以互動問答建立。"
  create_vessel_info
else
  warn "船舶基本資訊檔內容不正確，將重新建立。"
  create_vessel_info
fi

# --- 舊格式的接管狀態檔（已廢除）------------------------------------------
# 若 .env/ 是從舊機複製過來的，這個檔會讓新機被 heartbeat 遷移成「接管中」——
# 首次部署的機器不該繼承別台的接管狀態。
# 【移除條件】全隊確認升級完成後，連同 scheduler/failover/role.py 的遷移碼一起刪掉。
LEGACY_FAILOVER="${SHARE_DIR}/.env/failover_state.json"
if [ -f "$LEGACY_FAILOVER" ]; then
  echo ""
  warn "偵測到舊格式的接管狀態檔：$LEGACY_FAILOVER"
  warn "它已廢除。若保留，heartbeat 啟動時會把它遷移成本機的接管狀態。"
  warn "判讀與上面同一個道理:若 .env/ 是從舊機複製過來的,這是殘留,該刪;"
  warn "若本機真的在替一台死掉的對端接管,刪掉就會失去接管。"
  warn "保留是可回復的（遷移後會出現在 failover_ctl.sh status 與巡檢報告裡）,所以預設保留。"
  legacy_ans=""
  if [ "$CHECK_ONLY" -eq 1 ]; then
    warn "$DRYRUN_NOTE 正式部署時會詢問是否刪除。"
    legacy_ans="n"
  elif [ -t 0 ]; then
    read -r -p "  刪除它？（不確定就按 Enter 保留）[y/N] " legacy_ans || legacy_ans=""
  else
    warn "非互動終端機：不擅自刪除，保留現狀。"
    legacy_ans="n"
  fi
  case "$legacy_ans" in
    Y|y) rm -f "$LEGACY_FAILOVER" && ok "已刪除 $LEGACY_FAILOVER" ;;
    *) warn "保留舊格式接管狀態檔。heartbeat 啟動時會把它遷移進身分檔;"
       warn "若確認是殘留,遷移後執行:bash ${SHARE_DIR}/scheduler/failover/failover_ctl.sh clear" ;;
  esac
fi

# --- 開機自動執行設定（scheduler/install_autostart.sh） --------------------
# 與船舶資訊檔一樣，是需要使用者留意的一次性設定：詢問是否設定開機自動啟動
# （systemd user service + linger）。install_autostart.sh 具冪等性，可重複執行。
#
# 以 --require-linger 呼叫，讓 install_autostart.sh 用離開碼區分結果，deploy 才能
# 「掌握」實際成功狀態(而非只知道有沒有崩)。deploy 端僅據以警告、不中斷部署。
#   0 = 完全成功(service enabled + linger on)
#   3 = user service 已裝，但 linger 未開啟(開機免登入自動執行需要它)
#   4 = 設定失敗(找不到腳本 / 無法寫入 unit / user manager 不可用)
#   2 = install_autostart.sh 參數錯誤
# AUTOSTART_STATUS 供最後的部署總結顯示；先給預設值(set -u 下需先定義)。
AUTOSTART_INSTALLER="${SHARE_DIR}/scheduler/install_autostart.sh"
AUTOSTART_STATUS="未執行"
echo ""
info "檢查開機自動執行設定 ..."
if [ ! -f "$AUTOSTART_INSTALLER" ]; then
  warn "找不到 $AUTOSTART_INSTALLER ，略過開機自動執行設定。"
  AUTOSTART_STATUS="略過（找不到安裝腳本）"
elif [ "$CHECK_ONLY" -eq 1 ]; then
  # --check-only 不佈署 unit；改用安裝器自己的 --check-only 回報現況
  #（它會一併檢查啟動器的必要檔案是否齊全,缺就回 4）。
  set +e
  bash "$AUTOSTART_INSTALLER" --check-only
  AUTOSTART_RC=$?
  set -e
  [ "$AUTOSTART_RC" -eq 0 ] \
    && AUTOSTART_STATUS="現況正常$DRYRUN_NOTE" \
    || AUTOSTART_STATUS="現況有問題（rc=$AUTOSTART_RC）$DRYRUN_NOTE"
elif [ ! -t 0 ]; then
  # 非互動終端機：不擅自更動 systemd / linger，僅提示手動指令。
  warn "非互動終端機，略過開機自動執行設定。"
  warn "如需設定，請手動執行：bash $AUTOSTART_INSTALLER"
  AUTOSTART_STATUS="略過（非互動終端機）"
else
  autostart_ans=""
  read -r -p "  是否設定開機自動啟動 scheduler（reboot_launcher.sh）？[Y/n] " autostart_ans || autostart_ans=""
  case "$autostart_ans" in
    ""|Y|y)
      # 捕捉離開碼判讀結果；install_autostart.sh 於非互動/無權限時不會中斷，
      # 這裡即使回非 0 也只警告，不影響 sftp_transfer 的部署結果。
      set +e
      bash "$AUTOSTART_INSTALLER" --require-linger
      AUTOSTART_RC=$?
      set -e
      case "$AUTOSTART_RC" in
        0) ok   "開機自動執行：已設定並啟用（service enabled + linger on）"
           AUTOSTART_STATUS="已啟用" ;;
        3) warn "開機自動執行：user service 已安裝，但 linger 未開啟；請手動執行 sudo loginctl enable-linger $(id -un)"
           AUTOSTART_STATUS="部分完成（linger 未開啟）" ;;
        4) warn "開機自動執行：設定失敗（rc=4）。可能原因:"
           warn "  * 缺少啟動器必要檔案(reboot_launcher.sh / reboot_script/roles.conf /"
           warn "    failover/effective_role.sh)—— 離線包不完整,見上方 install_autostart 的明細"
           warn "  * 找不到腳本 / 無法寫入 unit / systemd user manager 不可用"
           AUTOSTART_STATUS="設定失敗（rc=4）" ;;
        2) warn "開機自動執行：install_autostart.sh 參數錯誤（rc=2）"
           AUTOSTART_STATUS="設定失敗（參數錯誤）" ;;
        *) warn "開機自動執行：未預期的結果（rc=$AUTOSTART_RC），請檢視上方訊息"
           AUTOSTART_STATUS="未知（rc=$AUTOSTART_RC）" ;;
      esac
      ;;
    *)
      info "略過開機自動執行設定。日後可執行：bash $AUTOSTART_INSTALLER"
      AUTOSTART_STATUS="使用者略過"
      ;;
  esac
fi

# --- 一次性遷移：舊的 clink_* 系統服務 → nssms 常駐服務 --------------------
# alarm / board / button 原本是 /etc/systemd/system/ 下的三支 system unit（clink_*），
# 現已收編為 scheduler/services/ 的 user unit。這一步把舊的停掉並移除。
#
# **必須排在 install_services.sh 之前**：舊 unit 還活著時，新的 alarm/board 會撞 port
# （`OSError: [Errno 98] Address already in use`）。
#
# 順帶把使用者加進 gpio 群組：nssms-button 跑的 btn 是用 libgpiod 開 /dev/gpiochip*，
# 那些節點是 root:gpio 660，所以「gpio 群組成員」就足夠 —— 不需要保留一支 root unit、
# 不需要 sudoers 白名單。注意群組變更只對**新** session 生效，所以 button 可能要到
# 重登入 / 重開機才會起來（那是預期行為，不是失敗）。
#
# 冪等：舊 unit 不存在、使用者已在群組時，整段安靜跳過。
LEGACY_UNITS=(clink_alarm_controller clink_board_server clink_button)
MIGRATE_STATUS="未執行"

legacy_present() {  # 回傳 0 = 至少還有一支舊 unit 存在
  local u
  for u in "${LEGACY_UNITS[@]}"; do
    [ -f "/etc/systemd/system/${u}.service" ] && return 0
  done
  return 1
}
gpio_needed() {  # 回傳 0 = 需要加入 gpio 群組
  getent group gpio >/dev/null 2>&1 || return 1   # 沒有 gpio 群組就不用加
  id -nG "$(id -un)" | tr ' ' '\n' | grep -qx gpio && return 1
  return 0
}

echo ""
info "檢查舊 clink_* 系統服務的遷移狀態 ..."
if ! legacy_present && ! gpio_needed; then
  ok "無需遷移（舊 clink_* 不存在，且已在 gpio 群組）。"
  MIGRATE_STATUS="無需遷移"
elif [ "$CHECK_ONLY" -eq 1 ]; then
  legacy_present && warn "仍存在舊 clink_* 系統服務（需遷移）：$(
    for u in "${LEGACY_UNITS[@]}"; do
      [ -f "/etc/systemd/system/${u}.service" ] && printf '%s ' "$u"
    done)"
  gpio_needed && warn "使用者 $(id -un) 尚未加入 gpio 群組（nssms-button 需要）。"
  MIGRATE_STATUS="待遷移$DRYRUN_NOTE"
elif [ ! -t 0 ]; then
  warn "非互動終端機，略過 clink_* 遷移（需 sudo）。"
  warn "如需遷移，請手動執行："
  warn "  sudo systemctl disable --now ${LEGACY_UNITS[*]}"
  warn "  sudo rm -f /etc/systemd/system/clink_{alarm_controller,board_server,button}.service"
  warn "  sudo usermod -aG gpio $(id -un)   # 之後需重登入或重開機"
  MIGRATE_STATUS="略過（非互動終端機）"
else
  legacy_present && warn "偵測到舊的 clink_* 系統服務，它們與新的 nssms 常駐服務會撞 port。"
  gpio_needed && info "另外需把 $(id -un) 加進 gpio 群組（nssms-button 讀 GPIO 用）。"
  migrate_ans=""
  read -r -p "  現在執行遷移（停用並移除舊 clink_*、加入 gpio 群組）？（需輸入一次密碼）[Y/n] " migrate_ans || migrate_ans=""
  case "$migrate_ans" in
    ""|Y|y)
      MIGRATE_RC=0
      if legacy_present; then
        set +e
        sudo systemctl disable --now "${LEGACY_UNITS[@]}" 2>/dev/null
        for u in "${LEGACY_UNITS[@]}"; do
          sudo rm -f "/etc/systemd/system/${u}.service" || MIGRATE_RC=1
        done
        sudo systemctl daemon-reload
        set -e
        [ "$MIGRATE_RC" -eq 0 ] && ok "已停用並移除舊 clink_* 系統服務。" \
                                || warn "舊 clink_* 移除時有項目失敗，請檢視上方訊息。"
      fi
      if gpio_needed; then
        set +e
        sudo usermod -aG gpio "$(id -un)"
        GPIO_RC=$?
        set -e
        if [ "$GPIO_RC" -eq 0 ]; then
          ok "已把 $(id -un) 加進 gpio 群組。"
          warn "群組變更只對新 session 生效 —— nssms-button 要到重登入/重開機才會起來。"
        else
          warn "加入 gpio 群組失敗（exit=$GPIO_RC），nssms-button 將無法讀取 GPIO。"
          MIGRATE_RC=1
        fi
      fi
      [ "$MIGRATE_RC" -eq 0 ] && MIGRATE_STATUS="已遷移" \
                              || MIGRATE_STATUS="部分完成"
      ;;
    *)
      warn "略過遷移。**新的 alarm / board 常駐服務會因 port 被舊 clink_* 佔用而起不來。**"
      MIGRATE_STATUS="使用者略過（新服務會撞 port）"
      ;;
  esac
fi

# --- 週期排程設定（scheduler/install_timers.sh + sudoers 白名單） ----------
# 與開機自動執行同屬「需使用者留意的一次性設定」：
#   1) install_timers.sh 佈署/啟用 systemd user timer（純 user 層，免 root）。
#   2) reboot / teamviewer 這兩支 timer 需 root，改由極窄的 /etc/sudoers.d 白名單
#      放行；安裝白名單需一次性輸入密碼（sudo）——趁部署互動時一併完成。
# 兩步皆冪等；非互動終端機時不擅自更動，僅印出手動指令。
TIMERS_INSTALLER="${SHARE_DIR}/scheduler/install_timers.sh"
SERVICES_INSTALLER="${SHARE_DIR}/scheduler/install_services.sh"
SUDOERS_SRC="${SHARE_DIR}/scheduler/etc/nssms-scheduler.sudoers"
SUDOERS_DST="/etc/sudoers.d/nssms-scheduler"
SCHED_STATUS="未執行"
SUDOERS_STATUS="未執行"
SERVICES_STATUS="未執行"
echo ""
info "檢查週期排程設定 ..."
if [ ! -f "$TIMERS_INSTALLER" ]; then
  warn "找不到 $TIMERS_INSTALLER ，略過週期排程設定。"
  SCHED_STATUS="略過（找不到安裝腳本）"
elif [ "$CHECK_ONLY" -eq 1 ]; then
  # --check-only 不佈署 timer、不裝 sudoers、不重啟 heartbeat;只回報現況。
  set +e
  bash "$TIMERS_INSTALLER" --check-only
  set -e
  SCHED_STATUS="僅回報現況$DRYRUN_NOTE"
  [ -f "$SUDOERS_DST" ] && SUDOERS_STATUS="已存在" || SUDOERS_STATUS="未安裝$DRYRUN_NOTE"
  if [ -f "$SERVICES_INSTALLER" ]; then
    set +e
    bash "$SERVICES_INSTALLER" --check-only
    set -e
    SERVICES_STATUS="僅回報現況$DRYRUN_NOTE"
  else
    SERVICES_STATUS="略過（找不到安裝腳本）"
  fi
elif [ ! -t 0 ]; then
  warn "非互動終端機，略過週期排程設定。"
  warn "如需設定，請手動執行：bash $TIMERS_INSTALLER"
  warn "reboot / teamviewer 需 sudo 白名單，見 $SUDOERS_SRC 檔頭安裝說明。"
  SCHED_STATUS="略過（非互動終端機）"
else
  sched_ans=""
  read -r -p "  是否設定週期排程與 ipc 接管（timer + 心跳/接管服務 + sudo 白名單）？[Y/n] " sched_ans || sched_ans=""
  case "$sched_ans" in
    ""|Y|y)
      # (1) 佈署 / 啟用 timer（user 層，免 root；失敗只警告不中斷部署）
      set +e
      bash "$TIMERS_INSTALLER"
      TIMERS_RC=$?
      set -e
      if [ "$TIMERS_RC" -eq 0 ]; then
        ok "週期排程 timer 已佈署並啟用。"
        SCHED_STATUS="已啟用"
      else
        warn "週期排程 timer 設定有項目失敗（exit=$TIMERS_RC），請檢視上方訊息。"
        SCHED_STATUS="部分完成（exit=$TIMERS_RC）"
      fi

      # (2) sudo 白名單（reboot / teamviewer 需要；此步需輸入密碼一次）
      echo ""
      if [ ! -f "$SUDOERS_SRC" ]; then
        warn "找不到 $SUDOERS_SRC ，略過 sudo 白名單安裝。"
        warn "未安裝白名單時，reboot / teamviewer 兩支 timer 會因 sudo 需密碼而失敗。"
        SUDOERS_STATUS="略過（找不到來源檔）"
      elif [ -f "$SUDOERS_DST" ]; then
        ok "sudo 白名單已存在（$SUDOERS_DST），沿用現有設定。"
        SUDOERS_STATUS="已存在"
      else
        sudoers_ans=""
        read -r -p "  reboot / teamviewer 需 sudo 白名單，現在安裝？（需輸入一次密碼）[Y/n] " sudoers_ans || sudoers_ans=""
        case "$sudoers_ans" in
          ""|Y|y)
            # 以目前使用者名稱套用（來源檔預設 mic-733ao；換人也正確）。
            CUR_USER="$(id -un)"
            TMP_SUDOERS="$(mktemp)"
            sed "s/^mic-733ao /${CUR_USER} /" "$SUDOERS_SRC" > "$TMP_SUDOERS"
            # 先驗證語法（絕不安裝壞掉的 sudoers，以免打壞整個 sudo）。
            if sudo visudo -c -f "$TMP_SUDOERS" >/dev/null 2>&1; then
              if sudo install -m 0440 -o root -g root "$TMP_SUDOERS" "$SUDOERS_DST"; then
                ok "已安裝 sudo 白名單：$SUDOERS_DST"
                SUDOERS_STATUS="已安裝"
              else
                warn "sudo 白名單安裝失敗（install 失敗）。"
                SUDOERS_STATUS="安裝失敗"
              fi
            else
              warn "sudo 白名單語法驗證未通過，未安裝（避免打壞 sudo）。"
              SUDOERS_STATUS="驗證失敗（未安裝）"
            fi
            rm -f "$TMP_SUDOERS"
            ;;
          *)
            info "略過 sudo 白名單安裝。日後可依 $SUDOERS_SRC 檔頭說明手動安裝。"
            SUDOERS_STATUS="使用者略過"
            ;;
        esac
      fi

      # (3) 常駐服務（user 層,免 root）：
      #     nssms-heartbeat（兩台都裝,角色自動分派）
      #     nssms-alarm-controller / nssms-board-server / nssms-button
      #       （硬體實體綁 IPC-1,unit 內有 ExecCondition 自行判定,兩台裝同一份即可）
      #
      #     **必須排在下面 (4) 的舊 clink_* 停用之後嗎？不是 —— 反過來。**
      #     (4) 已經在本區塊之前執行完（見上方一次性遷移段），因為舊的 system unit 還活著
      #     時新 unit 會撞 port。這裡只負責裝。
      echo ""
      if [ ! -f "$SERVICES_INSTALLER" ]; then
        warn "找不到 $SERVICES_INSTALLER ，略過常駐服務安裝。"
        SERVICES_STATUS="略過（找不到安裝腳本）"
      else
        set +e
        bash "$SERVICES_INSTALLER"
        SV_RC=$?
        set -e
        if [ "$SV_RC" -eq 0 ]; then
          ok "常駐服務已佈署並啟用（heartbeat / alarm / board / button）。"
          SERVICES_STATUS="已啟用"
        else
          warn "常駐服務安裝有項目失敗（exit=$SV_RC），請檢視上方訊息。"
          SERVICES_STATUS="部分完成（exit=$SV_RC）"
        fi
      fi
      ;;
    *)
      info "略過週期排程設定。日後可執行：bash $TIMERS_INSTALLER"
      SCHED_STATUS="使用者略過"
      ;;
  esac
fi

# --- 是否於部署完成後立即執行完整啟動流程（只收集決定，執行在最後面） ------
# 到目前為止只做完「一次性設定」:身分、systemd 骨架、sudoers。各專案的**程式碼、環境安裝
# 與服務啟動**全部在啟動流程裡:
#     reboot_launcher.sh → update_booster.sh(SFTP 拉最新程式碼)→ 依角色套用 update+env+run
# 少了它,部署跑完機器上一個 tmux session 都沒有,而總結卻是一排「已啟用」。
#
# **決定在這裡收集,執行放到最後面。** 理由:接下來的 venv 建置是一長段無人干預的流程,
# 若把詢問放在它之後,操作者就得守在機器前等它跑完才能回答那一題 —— 所有需要人輸入的東西
# 都該集中在最前面。(而且啟動流程本身可能要數分鐘,問完就能一路跑到底。)
#
# 選 n 也不會壞:只要前面的開機 unit 裝成功了,下次開機 nssms-boot 就會跑同一支啟動器。
LAUNCHER="${SHARE_DIR}/scheduler/reboot_launcher.sh"
LAUNCH_STATUS="未執行"
LAUNCH_DECISION="skip"
echo ""
info "檢查是否於部署完成後立即執行完整啟動流程 ..."
if [ "$CHECK_ONLY" -eq 1 ]; then
  warn "--check-only:不執行啟動流程。$DRYRUN_NOTE"
  LAUNCH_STATUS="略過（--check-only）"
elif [ "$RUN_LAUNCH" -eq 0 ]; then
  info "--no-launch:略過啟動流程。"
  LAUNCH_STATUS="略過（--no-launch）"
elif [ ! -f "$LAUNCHER" ]; then
  warn "找不到 $LAUNCHER ，略過啟動流程。"
  LAUNCH_STATUS="略過（找不到啟動器）"
elif [ ! -t 0 ]; then
  warn "非互動終端機:不擅自啟動服務。"
  warn "如需啟動請於部署後執行:bash $LAUNCHER"
  LAUNCH_STATUS="略過（非互動終端機）"
else
  info "它會:掛載資料碟 → SFTP 拉最新程式碼 → 安裝各專案環境 → 啟動服務。"
  info "首次部署沒有 launcher_state.json,所以是全相位套用,可能需要數分鐘。"
  info "會在本腳本的最後、健康檢查之前執行(這之後不再需要你輸入任何東西)。"
  if [ "${DEPLOY_VSL_UPPER:-}" = "CLINK" ]; then
    warn "本機 vsl_name=CLINK(開發機):update_booster 會刻意略過整個 OTA,"
    warn "所以**不會**下載程式碼,只會用機上現有版本啟動。"
  fi
  launch_ans=""
  read -r -p "  部署完成後立即執行?（選 n 則下次開機由 nssms-boot 自動跑）[Y/n] " \
    launch_ans || launch_ans=""
  case "$launch_ans" in
    ""|Y|y) LAUNCH_DECISION="run"; ok "已排入:部署完成後會執行一次完整啟動流程。" ;;
    *) LAUNCH_STATUS="使用者略過"
       info "略過。下次開機 nssms-boot 會自動執行,或手動:bash $LAUNCHER" ;;
  esac
fi

echo ""
info "以下不再需要任何輸入,可以離開終端機。"

if [ ! -d "$WHEELHOUSE" ]; then
  err "wheelhouse 目錄不存在：$WHEELHOUSE"; exit 1
fi
WHL_COUNT=$(find "$WHEELHOUSE" -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ')
if [ "$WHL_COUNT" -eq 0 ]; then
  err "wheelhouse 內沒有任何 .whl 檔案"; exit 1
fi
ok "找到 $WHL_COUNT 個 wheel 檔案"

# 建立 venv 改用 python3.10 -m virtualenv（與 radar / SHM 一致，不再依賴系統
# python3-venv / ensurepip）。若目標直譯器尚未安裝 virtualenv，先以隨附的離線
# 安裝腳本補齊（install_virtualenv_offline.sh + virtualenv_wheels/）。
VENV_INSTALLER="${SCRIPT_DIR}/install_virtualenv_offline.sh"
if "$PYTHON_BIN" -m virtualenv --version >/dev/null 2>&1; then
  ok "virtualenv 可用：$("$PYTHON_BIN" -m virtualenv --version 2>&1 | awk '{print $2}')"
elif [ "$CHECK_ONLY" -eq 1 ]; then
  # --check-only 只驗證、不安裝；僅回報缺 virtualenv，實際部署時才會離線補齊。
  warn "$PYTHON_BIN 尚未安裝 virtualenv（--check-only 不進行安裝）。"
  warn "實際部署時將以 $VENV_INSTALLER 離線補齊。"
else
  warn "$PYTHON_BIN 尚未安裝 virtualenv，將以隨附腳本離線安裝 ..."
  if [ ! -f "$VENV_INSTALLER" ]; then
    err "找不到離線安裝腳本：$VENV_INSTALLER"; exit 1
  fi
  bash "$VENV_INSTALLER"
  if ! "$PYTHON_BIN" -m virtualenv --version >/dev/null 2>&1; then
    err "virtualenv 離線安裝後，$PYTHON_BIN 仍無法使用（可能裝到了其他解譯器）。"
    err "請確認 $PYTHON_BIN 與 install_virtualenv_offline.sh 選用的解譯器一致。"
    exit 1
  fi
  ok "virtualenv 離線安裝完成並可用：$("$PYTHON_BIN" -m virtualenv --version 2>&1 | awk '{print $2}')"
fi

# --- 校驗 wheel 完整性 ------------------------------------------------------
if [ "$SKIP_VERIFY" -eq 0 ] && [ -f "$MANIFEST" ]; then
  info "以 MANIFEST.txt 校驗 wheel sha256 ..."
  if ( cd "$WHEELHOUSE" && grep -E '^[0-9a-f]{64}  ' "$MANIFEST" | sha256sum -c --quiet ) 2>/dev/null; then
    ok "所有 wheel 檔案 sha256 校驗通過"
  else
    err "wheel 校驗失敗，檔案可能損毀或被竄改。可用 --skip-verify 強制略過。"; exit 1
  fi
else
  warn "略過 wheel sha256 校驗"
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  ok "--check-only 完成：環境與 wheel 皆就緒，未執行安裝。"
  exit 0
fi

# --- 建立 / 沿用 venv ------------------------------------------------------
VENV_PY="${VENV_DIR}/bin/python"
if [ "$RECREATE" -eq 1 ] && [ -d "$VENV_DIR" ]; then
  warn "--recreate：移除既有 venv $VENV_DIR"
  rm -rf "$VENV_DIR"
fi

if [ -x "$VENV_PY" ]; then
  ok "沿用既有 venv：$VENV_DIR"
else
  info "建立專屬 venv（$PYTHON_BIN -m virtualenv，離線，含 pip）..."
  mkdir -p "$(dirname "$VENV_DIR")"
  "$PYTHON_BIN" -m virtualenv "$VENV_DIR"
  if [ ! -x "$VENV_PY" ]; then
    err "venv 建立失敗：找不到 $VENV_PY"; exit 1
  fi
  ok "venv 建立完成"
fi
info "venv pip 版本 : $("$VENV_PY" -m pip --version 2>/dev/null | awk '{print $2}')"

# --- 執行離線安裝 ----------------------------------------------------------
RUNTIME_PKGS=(paramiko bcrypt cryptography pynacl cffi pycparser invoke typing-extensions)
TEST_PKGS=(pytest pytest-cov coverage pluggy iniconfig packaging pygments tomli exceptiongroup)

PKGS=("${RUNTIME_PKGS[@]}")
if [ "$INSTALL_TESTS" -eq 1 ]; then
  PKGS+=("${TEST_PKGS[@]}")
  info "安裝範圍      : 執行期相依 + 測試堆疊 (pytest；預設)"
else
  info "安裝範圍      : 執行期相依 (paramiko 堆疊；--skip-tests)"
fi

info "開始離線安裝到 venv（--no-index，不連外網）..."
set +e
"$VENV_PY" -m pip install \
  --no-index \
  --find-links "$WHEELHOUSE" \
  --upgrade \
  "${PKGS[@]}"
PIP_RC=$?
set -e
if [ "$PIP_RC" -ne 0 ]; then
  err "pip 安裝失敗（exit=$PIP_RC）。"; exit "$PIP_RC"
fi
ok "套件安裝完成"

# --- 安裝後驗證 ------------------------------------------------------------
info "在 venv 內驗證關鍵套件可正常匯入 ..."
"$VENV_PY" - <<'PY'
import importlib, sys
mods = ["paramiko", "cryptography", "nacl", "bcrypt", "cffi"]
fail = False
for m in mods:
    try:
        mod = importlib.import_module(m)
        v = getattr(mod, "__version__", "?")
        print(f"  [ OK ] {m:<14} {v}")
    except Exception as e:  # noqa
        print(f"  [FAIL] {m:<14} {e}")
        fail = True
sys.exit(1 if fail else 0)
PY
ok "匯入驗證通過"

echo "-----------------------------------------------------------"
ok "離線部署完成！專屬 venv：$VENV_DIR"

# --- 執行完整啟動流程（決定已在前面收集，這裡只執行） ----------------------
# 刻意放在 venv 之後:update_booster 的 SFTP 下載要用 sftp_transfer 的 venv。
# 也刻意放在健康檢查**之前**:服務起來之後,那份巡檢才第一次真的有意義
#(否則 tmux 段永遠是「預期 session 不存在」,報告等於白給)。
# 這裡不再詢問任何事 —— 所有互動都集中在前面,這之後全程無人干預。
if [ "$LAUNCH_DECISION" = "run" ]; then
  echo ""
  echo "── 執行完整啟動流程（reboot_launcher.sh）──"
  set +e
  bash "$LAUNCHER"
  LAUNCH_RC=$?
  set -e
  if [ "$LAUNCH_RC" -eq 0 ]; then
    ok "啟動流程完成（所有項目成功）。"
    LAUNCH_STATUS="已完成"
  else
    # 啟動器對個別項目失敗是「記錄並繼續」,所以非 0 代表有項目失敗而非整體中止。
    warn "啟動流程有項目失敗（exit=$LAUNCH_RC）。詳見上方總結與"
    warn "  ${SHARE_DIR}/scheduler/logs/launcher.log"
    LAUNCH_STATUS="有項目失敗（exit=$LAUNCH_RC）"
  fi
fi

# --- 部署後自動健康檢查 ----------------------------------------------------
HEALTH_RC=0
if [ "$RUN_HEALTH" -eq 1 ]; then
  if [ -f "$SCRIPT_DIR/health_check.py" ]; then
    echo ""
    info "自動執行健康檢查（能力測試 + SFTP 連線 + 健康報告）..."
    echo "==========================================================="
    # 未安裝測試堆疊（--skip-tests）時，轉傳 --skip-tests 讓 health_check 直接略過
    # 單元測試那一項（記 INFO 而非 WARN）。預設有裝 pytest 時則實際跑測試。
    HEALTH_ARGS=()
    [ "$INSTALL_TESTS" -eq 0 ] && HEALTH_ARGS+=(--skip-tests)
    set +e
    "$VENV_PY" "$SCRIPT_DIR/health_check.py" "${HEALTH_ARGS[@]}"
    HEALTH_RC=$?
    set -e
    echo "==========================================================="
    if [ "$HEALTH_RC" -eq 0 ]; then
      ok "健康檢查結果：HEALTHY"
    else
      warn "健康檢查發現問題（exit=$HEALTH_RC），請檢視上方報告。"
    fi
  else
    warn "找不到 health_check.py，略過自動健康檢查。"
  fi
else
  info "已指定 --no-health-check，略過能力健康檢查。"
fi

# --- 隱性自動化存活巡檢 ----------------------------------------------------
# 放在部署流程最後，以實際 systemd 狀態驗證：user manager / linger / unit /
# timer / sudoers / heartbeat / tmux。使用 --compact --fail-on-warn，部署畫面
# 只顯示 WARN/FAIL 與總結；完整明細仍寫入 logs/automation_health_report_<時間>.md。
# wave 尚未提供時由巡檢器列為 SKIP，不影響整體健康。
AUTOMATION_CHECKER="${SCRIPT_DIR}/automation_health_check.py"
AUTOMATION_RC=0
AUTOMATION_STATUS="未執行"
if [ "$RUN_HEALTH" -eq 1 ]; then
  echo ""
  info "執行隱性自動化存活巡檢（compact）..."
  if [ -f "$AUTOMATION_CHECKER" ]; then
    set +e
    "$PYTHON_BIN" "$AUTOMATION_CHECKER" --compact --fail-on-warn
    AUTOMATION_RC=$?
    set -e
    case "$AUTOMATION_RC" in
      0)
        ok "自動化存活巡檢：HEALTHY"
        AUTOMATION_STATUS="HEALTHY"
        ;;
      1)
        warn "自動化存活巡檢：UNHEALTHY（有 FAIL；請查看上方與 Markdown 報告）"
        AUTOMATION_STATUS="UNHEALTHY（exit=1）"
        ;;
      2)
        warn "自動化存活巡檢：DEGRADED（有 WARN；請查看上方與 Markdown 報告）"
        AUTOMATION_STATUS="DEGRADED（exit=2）"
        ;;
      *)
        warn "自動化存活巡檢未預期結束（exit=$AUTOMATION_RC）"
        AUTOMATION_STATUS="執行異常（exit=$AUTOMATION_RC）"
        ;;
    esac
  else
    warn "找不到 $AUTOMATION_CHECKER，略過自動化存活巡檢。"
    AUTOMATION_RC=127
    AUTOMATION_STATUS="略過（找不到巡檢腳本）"
  fi
else
  info "已指定 --no-health-check，略過自動化存活巡檢。"
  AUTOMATION_STATUS="略過（--no-health-check）"
fi

echo ""
echo "── 部署總結 ──"
# 首次部署最容易搞錯的就是「這台裝成 IPC-1 還是 IPC-2」,而它決定了會啟動哪些服務。
# 直接印出啟動器實際會採用的有效角色,一眼可見。
EFFECTIVE_ROLE_SH="${SHARE_DIR}/scheduler/failover/effective_role.sh"
if [ -f "$EFFECTIVE_ROLE_SH" ]; then
  DEPLOY_ROLE="$(bash "$EFFECTIVE_ROLE_SH" --quiet 2>/dev/null || echo "（判定失敗）")"
else
  DEPLOY_ROLE="（找不到 effective_role.sh）"
fi
# 開發機(CLINK)的 OTA 守門會讓「第一次開機自動下載程式碼」這件事不成立,後面要據此提醒。
DEPLOY_VSL_UPPER="$(printf '%s' "$(vessel_get vsl_name)" | tr '[:lower:]' '[:upper:]')"
printf "  本機有效角色    ：%s\n" "$DEPLOY_ROLE"
printf "  開機自動執行設定：%s\n" "$AUTOSTART_STATUS"
printf "  週期排程 timer   ：%s\n" "$SCHED_STATUS"
printf "  常駐服務        ：%s\n" "$SERVICES_STATUS"
printf "  clink_* 遷移    ：%s\n" "$MIGRATE_STATUS"
printf "  sudo 白名單      ：%s\n" "$SUDOERS_STATUS"
[ "$RUN_HEALTH" -eq 1 ] && printf "  健康檢查：%s\n" \
  "$( [ "$HEALTH_RC" -eq 0 ] && echo HEALTHY || echo "有問題（exit=$HEALTH_RC）" )"
printf "  完整啟動流程    ：%s\n" "$LAUNCH_STATUS"
printf "  自動化存活巡檢  ：%s\n" "$AUTOMATION_STATUS"

echo ""
echo "── ⚠ 尚未完成:服務還沒有啟動 ──"
# 本腳本刻意只做「一次性人工設定」:身分、systemd 骨架、sudoers、sftp_transfer 的 venv。
# 它不做 SFTP 下載、不跑 update_booster、也不 start nssms-boot(只 enable)。
# 各專案的程式碼、環境安裝與服務啟動,全部由第一次開機的
#   nssms-boot → reboot_launcher.sh → update_booster.sh(OTA)→ 依角色全相位套用
# 完成。不講清楚的話,操作者看到上面一排「已啟用」會以為部署完成了。
echo "  deploy_offline 只做一次性設定(身分 / systemd / sudoers / sftp_transfer venv)。"
echo "  各專案的程式碼、環境與服務由第一次開機流程完成:"
echo "    nssms-boot → reboot_launcher.sh → update_booster.sh(SFTP 拉最新程式碼)"
echo "                                    → 依角色 $DEPLOY_ROLE 套用 update+env+run"
echo ""
echo "  所以接下來請二選一:"
echo "    sudo reboot                                # 建議:完整走一次真實開機流程"
echo "    systemctl --user start nssms-boot          # 或立即手動觸發一次(不重開機)"
echo ""
echo "  想先確認會做什麼(不執行任何動作):"
echo "    bash ${SHARE_DIR}/scheduler/reboot_launcher.sh --dry-run"
echo ""
echo "  啟動後的驗證:"
echo "    tmux ls                                     # 應列出本角色該有的 session"
echo "    tail -f ${SHARE_DIR}/scheduler/logs/launcher.log       # 開機做了什麼"
echo "    tail -f ${SHARE_DIR}/scheduler/failover/logs/heartbeat.log"
echo "    ${PYTHON_BIN} ${AUTOMATION_CHECKER}"
if [ "${DEPLOY_VSL_UPPER:-}" = "CLINK" ]; then
  echo ""
  warn "  本機 vsl_name=CLINK(開發機):update_booster 會刻意略過整個 OTA,"
  warn "  所以程式碼**不會**自動下載,需人工放置或 rsync。"
fi
echo ""
echo "啟用 venv："
echo "  source \"$VENV_DIR/bin/activate\""
echo ""
echo "以此 venv 執行工具（不啟用也可以直接用絕對路徑）："
echo "  \"$VENV_PY\" \"$PROJECT_DIR/main.py\" --cli"
echo ""
echo "如需單獨再跑一次健康檢查："
echo "  \"$VENV_PY\" \"$SCRIPT_DIR/health_check.py\""
echo ""
echo "如需單獨再跑一次自動化存活巡檢："
echo "  \"$PYTHON_BIN\" \"$AUTOMATION_CHECKER\""
echo "==========================================================="

# 部署本身成功即回傳 0；健康檢查結果另以訊息呈現，不影響部署離開碼。
exit 0
