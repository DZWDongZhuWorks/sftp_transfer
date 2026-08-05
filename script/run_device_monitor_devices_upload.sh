#!/usr/bin/env bash
# 發佈各船設備清單到 UNIQUE/（只在開發機 CLINK 執行）
#
# 前置：先跑 device_monitor/tool/parse_ip_table.py 產出 data/vessels/ 樹狀結構，
#       並核對 data/vessels/_parse_report.json 的跳過清單與撞名記錄。
# 效果：vessels/WH325/device_monitor/config/devices.csv
#       → UNIQUE/WH325/device_monitor/config/devices.csv
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

cd "$BASE_DIR"

config="$SCRIPT_DIR/../config/device_monitor_devices_upload_settings.json"
vessels_dir="$BASE_DIR/../device_monitor/data/vessels"

if [[ ! -f "$config" ]]; then
    echo "找不到設定檔: $config" >&2
    exit 1
fi

if [[ ! -d "$vessels_dir" ]]; then
    echo "找不到設備清單目錄: $vessels_dir" >&2
    echo "請先執行 device_monitor/tool/parse_ip_table.py 產出各船清單。" >&2
    exit 1
fi

VENV_PY="${SFTP_TRANSFER_VENV:-$HOME/venv/wanhai_nssms/share/sftp_transfer}/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
    echo "找不到 sftp_transfer 專屬 venv 的 Python: $VENV_PY" >&2
    echo "請先執行 deploy/deploy_offline.sh 建立 venv。" >&2
    exit 1
fi

echo "即將把 $(find "$vessels_dir" -mindepth 1 -maxdepth 1 -type d | wc -l) 艘船的設備清單推到 UNIQUE/"
"$VENV_PY" "$BASE_DIR/main.py" --cli --mode upload --config "$config"
