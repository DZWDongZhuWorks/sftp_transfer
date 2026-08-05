#!/usr/bin/env bash
# 手動互動執行：掃描 config/ 後，以 curses 選擇本次要下載／上傳的專案。
#
# 用法：
#   script/run_selected_transfers.sh
#   script/run_selected_transfers.sh --mode download
#   script/run_selected_transfers.sh --mode upload --config-dir other_config
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
cd "$BASE_DIR"

DRIVER="$BASE_DIR/run_selected_transfers.py"
if [[ ! -f "$DRIVER" ]]; then
    echo "找不到互動選擇腳本: $DRIVER" >&2
    exit 1
fi

VENV_PY="${SFTP_TRANSFER_VENV:-$HOME/venv/wanhai_nssms/share/sftp_transfer}/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
    echo "找不到 sftp_transfer 專屬 venv 的 Python: $VENV_PY" >&2
    echo "請先執行 deploy/deploy_offline.sh 建立 venv。" >&2
    exit 1
fi

exec "$VENV_PY" "$DRIVER" "$@"
