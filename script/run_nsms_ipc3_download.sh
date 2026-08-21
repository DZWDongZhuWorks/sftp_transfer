#!/usr/bin/env bash
# 更新 IPC-3 的 nsms(gateway / warning_sign / smoke 三個模組)
#
# 與 run_nsms_download.sh 是**兩件不同的事**,刻意分成兩支:
#   run_nsms_download.sh       IPC2_nsms_download_settings.json → nsms/bridge_safety_system
#                              (影像 + 語音分析,IPC-2 專屬)
#   run_nsms_ipc3_download.sh  IPC3_nsms_download_settings.json → nsms/(IPC-3 的三個模組)
# 兩者的 remote_path 與 local_path 都不重疊,同一台機器上也不會互相覆蓋。
#
# 設定檔的 remote_path 同時涵蓋 STANDARD/nsms-IPC3 與 UNIQUE/{vsl_name}/nsms-IPC3,
# 兩者都落到同一個 local_path(../../nsms),所以三個模組一次下載全部涵蓋 ——
# scheduler 的 start_nsms_ipc3.sh update 相位只呼叫本腳本一次。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

# 切換到專案根目錄，讓設定檔中的相對路徑（如 ignore_file: config/xxx_ignore.txt）
# 無論從哪個目錄或排程 (cron) 執行都能正確解析。
cd "$BASE_DIR"

# 開發機 (CLINK) 守門：見 _dev_guard.sh。本下載是 overwrite，會覆蓋開發端工作區
# （nsms/ 底下的模組各有自己的 git），在 CLINK 上一律略過。
source "$SCRIPT_DIR/_dev_guard.sh"
dev_guard "$BASE_DIR"

config="$SCRIPT_DIR/../config/IPC3_nsms_download_settings.json"

if [[ ! -f "$config" ]]; then
    echo "找不到設定檔: $config" >&2
    exit 1
fi

# 使用 sftp_transfer 專屬 venv 的 Python 啟動（離線部署由 deploy/deploy_offline.sh 建立）
VENV_PY="${SFTP_TRANSFER_VENV:-$HOME/venv/wanhai_nssms/share/sftp_transfer}/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
    echo "找不到 sftp_transfer 專屬 venv 的 Python: $VENV_PY" >&2
    echo "請先執行 deploy/deploy_offline.sh 建立 venv。" >&2
    exit 1
fi

"$VENV_PY" "$BASE_DIR/main.py" --cli --config "$config"
