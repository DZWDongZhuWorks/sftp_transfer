#!/usr/bin/env bash
# 岸端 SFTP log 的保留政策：刪掉遠端 sftp_logs 底下超過 30 天的傳輸紀錄。
#
# 預設**只預覽**（remote_retention.py 不給 --apply 就不刪任何東西）。要真的刪，
# 設 REMOTE_RETENTION_APPLY=1 或直接加參數。設計論證見 remote_retention.py 的檔頭。
#
# 這支刻意**不呼叫 _dev_guard.sh**：守門擋的是「在 CLINK 發佈源頭跑下載會覆蓋未發佈的
# 修改」，而本程式不下載任何東西，且岸端監控機就是 CLINK —— 加守門等於讓它永遠不能跑。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

cd "$BASE_DIR"

config="$BASE_DIR/config/log_monitor_sync.json"

if [[ ! -f "$config" ]]; then
    echo "找不到設定檔: $config" >&2
    exit 2
fi

VENV_PY="${SFTP_TRANSFER_VENV:-$HOME/venv/wanhai_nssms/share/sftp_transfer}/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
    echo "找不到 sftp_transfer 專屬 venv 的 Python: $VENV_PY" >&2
    echo "請先執行 deploy/deploy_offline.sh 建立 venv。" >&2
    exit 2
fi

args=(--config "$config" --retention-days "${REMOTE_RETENTION_DAYS:-30}")
if [[ "${REMOTE_RETENTION_APPLY:-0}" == "1" ]]; then
    args+=(--apply)
fi

exec "$VENV_PY" "$BASE_DIR/remote_retention.py" "${args[@]}" "$@"
