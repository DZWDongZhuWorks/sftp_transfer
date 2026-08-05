#!/usr/bin/env bash
# 上傳 share（alarm/board/flag/controller → STANDARD/share/，各自依 basename 展開）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

# 切換到專案根目錄，讓設定檔中的相對路徑（如 ignore_file: config/xxx_ignore.txt）
# 無論從哪個目錄或排程 (cron) 執行都能正確解析。
cd "$BASE_DIR"

# 注意：上傳「刻意不套用」_dev_guard.sh 的 CLINK 守門。CLINK 是 STANDARD 的發佈源頭，
# 發佈動作正是要從這台往上傳；守門只用於下載（避免 STANDARD 覆蓋開發端未提交的修改）。
config="$SCRIPT_DIR/../config/IPC1_share_upload_settings.json"

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

"$VENV_PY" "$BASE_DIR/main.py" --cli --mode upload --config "$config"
