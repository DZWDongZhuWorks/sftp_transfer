#!/usr/bin/env bash
# 下載 device_monitor：STANDARD 的程式碼 + UNIQUE 的本船設備清單，合併到同一個本地目錄。
#
# 兩個來源在同一份設定的 remote_path 陣列中，local_path 為單一且不帶尾斜線 ＝ 合併模式，
# 所以 UNIQUE/{vsl_name}/device_monitor/config/devices.csv 會落到
# share/device_monitor/config/devices.csv，與 STANDARD 來的程式碼並存。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

cd "$BASE_DIR"

# 開發機 (CLINK) 守門：在發佈源頭上執行下載會把 STANDARD 覆蓋回本機、
# 清掉尚未發佈的開發修改，因此一律略過。
source "$SCRIPT_DIR/_dev_guard.sh"
dev_guard "$BASE_DIR"

config="$SCRIPT_DIR/../config/device_monitor_download_settings.json"

if [[ ! -f "$config" ]]; then
    echo "找不到設定檔: $config" >&2
    exit 1
fi

VENV_PY="${SFTP_TRANSFER_VENV:-$HOME/venv/wanhai_nssms/share/sftp_transfer}/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
    echo "找不到 sftp_transfer 專屬 venv 的 Python: $VENV_PY" >&2
    echo "請先執行 deploy/deploy_offline.sh 建立 venv。" >&2
    exit 1
fi

"$VENV_PY" "$BASE_DIR/main.py" --cli --mode download --config "$config"
