#!/usr/bin/env bash
# 手動執行：遍歷 config/ 內所有「上傳」設定檔（*_upload_settings.json）並依序執行 SFTP 上傳。
#
# 供使用者手動一次跑完全部上傳用；自動排程（timer / reboot_launcher）不使用本腳本，
# 而是各自呼叫對應的單一 run_*.sh。實際遍歷與結果彙總由 run_all_uploads.py 負責。
#
# 用法：
#   script/run_all_uploads.sh                     # 跑 ./config 內全部上傳設定
#   script/run_all_uploads.sh --config-dir other  # 指定其他設定檔資料夾
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

# 切換到專案根目錄，讓設定檔中的相對路徑（如 ignore_file: config/xxx_ignore.txt）
# 無論從哪個目錄或排程 (cron) 執行都能正確解析（子行程會繼承此 CWD）。
cd "$BASE_DIR"

# 注意：上傳「刻意不套用」_dev_guard.sh 的 CLINK 守門。CLINK 是 STANDARD 的發佈源頭，
# 發佈動作正是要從這台往上傳；守門只用於下載（避免 STANDARD 覆蓋開發端未提交的修改）。
DRIVER="$BASE_DIR/run_all_uploads.py"
if [[ ! -f "$DRIVER" ]]; then
    echo "找不到彙總上傳腳本: $DRIVER" >&2
    exit 1
fi

# 使用 sftp_transfer 專屬 venv 的 Python 啟動（離線部署由 deploy/deploy_offline.sh 建立）
VENV_PY="${SFTP_TRANSFER_VENV:-$HOME/venv/wanhai_nssms/share/sftp_transfer}/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
    echo "找不到 sftp_transfer 專屬 venv 的 Python: $VENV_PY" >&2
    echo "請先執行 deploy/deploy_offline.sh 建立 venv。" >&2
    exit 1
fi

# 透傳所有參數（例如 --config-dir）給 driver。
"$VENV_PY" "$DRIVER" "$@"
