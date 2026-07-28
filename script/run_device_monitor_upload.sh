#!/usr/bin/env bash
# 上傳 device_monitor 程式碼到 STANDARD（開發機發佈用）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

# 切換到專案根目錄，讓設定檔中的相對路徑無論從哪個目錄或排程執行都能正確解析。
cd "$BASE_DIR"

# 注意：上傳「刻意不套用」_dev_guard.sh 的 CLINK 守門。CLINK 是 STANDARD 的發佈源頭，
# 發佈動作正是要從這台往上傳；守門只用於下載。
config="$SCRIPT_DIR/../config/device_monitor_upload_settings.json"

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

"$VENV_PY" "$BASE_DIR/main.py" --cli --mode upload --config "$config"
