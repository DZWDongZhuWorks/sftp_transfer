#!/usr/bin/env bash
# 更新 scheduler
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

# 切換到專案根目錄，讓設定檔中的相對路徑（如 ignore_file: config/xxx_ignore.txt）
# 無論從哪個目錄或排程 (cron) 執行都能正確解析。
cd "$BASE_DIR"

# --- 開發機 (CLINK) 守門 ---------------------------------------------------
# scheduler 下載會以 SFTP 遠端內容覆蓋本地 share/scheduler（duplicate_mode=overwrite），
# 在開發機上會連同尚未提交的修改一併還原。守門放在本腳本內，讓它無論被 reboot_tmux.sh /
# update_booster.sh、timer 或人工直接呼叫都能自我保護，不再單靠外部呼叫端把關。
# 船舶資訊檔路徑與 settings.py 一致（可用 VESSEL_INFO_PATH 覆蓋，預設 share/.env/）。
vessel_info="${VESSEL_INFO_PATH:-$BASE_DIR/../.env/vessel_basic_info.json}"
vsl="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("vsl_name",""))' "$vessel_info" 2>/dev/null || true)"
if [[ "${vsl^^}" == "CLINK" ]]; then
    echo "偵測到開發機 (vsl_name=CLINK)，略過 scheduler 下載，避免覆蓋未提交的修改。" >&2
    exit 0
fi

config="$SCRIPT_DIR/../config/scheduler_download_settings.json"

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