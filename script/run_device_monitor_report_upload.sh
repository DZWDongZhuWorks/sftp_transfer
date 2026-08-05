#!/usr/bin/env bash
# 手動補傳 device_monitor 健康度報表。
#
# 正常情況下不需要執行這支：report.py 產完報表就會自己呼叫同一份設定上傳。
# 這支是給「上傳失敗後想立刻重試、不想等下一個 timer 週期」時用的。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

cd "$BASE_DIR"

config="$SCRIPT_DIR/../config/device_monitor_report_upload_settings.json"

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
