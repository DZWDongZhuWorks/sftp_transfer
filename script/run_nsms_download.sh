#!/usr/bin/env bash
# 更新 nsms(bridge_safety_system:video + audio 兩個分析模組)
#
# 設定檔 config/IPC2_nsms_download_settings.json 是 nsms 的**共用**下載設定:
# remote_path 同時包含 STANDARD 的 bridge_safety_system 全包,以及 UNIQUE/{vsl_name} 底下
# 該船專屬的 video setting_file，兩者都落到同一個 local_path。因此 video / audio 不各自
# 下載，一次下載涵蓋兩個模組 —— start_nsms.sh 的 update 相位只呼叫本腳本一次。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

# 切換到專案根目錄，讓設定檔中的相對路徑（如 ignore_file: config/xxx_ignore.txt）
# 無論從哪個目錄或排程 (cron) 執行都能正確解析。
cd "$BASE_DIR"

# 開發機 (CLINK) 守門：見 _dev_guard.sh。nsms 下載是 overwrite，會覆蓋開發端工作區
# （bridge_video_analysis_module 底下還有自己的 .git），在 CLINK 上一律略過。
source "$SCRIPT_DIR/_dev_guard.sh"
dev_guard "$BASE_DIR"

config="$SCRIPT_DIR/../config/IPC2_nsms_download_settings.json"

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
