#!/usr/bin/env bash
# 只抓 web OTA 的解包密鑰 gpg_prefix.txt(一個檔)。
#
# 【為什麼是獨立一支,而不是同步整個 STANDARD/web_ota/】OTA 工具本身自 2026-09 起由
# share/scheduler 維護(tool/web-ota/),隨 scheduler 自更新散佈。把整個 web_ota/ 拉下來
# 會多出第二份工具,兩份各自漂移 —— 而症狀是「查問題的人跑到舊的那一份」。
# 這裡只取密鑰:程式碼歸 scheduler、密鑰歸 web,各自走各自的路。
#
# 【為什麼 remote_path 可以指到檔案】downloader.py 會先 stat 遠端路徑,不是目錄就
# 只抓那一個檔(見 _collect_remote_files)。不必靠 ignore 規則去湊。
#
# 【為什麼 duplicate_mode=overwrite 而不是鏡像】overwrite 沒有刪除語意,所以某次抓取
# 失敗時舊密鑰留在原地。若用會刪除的鏡像,密鑰在岸端短暫缺席就會讓全船隊下一輪
# 一起變 needs_manual。
# 【設定檔不在版控裡,所以內容寫在這】config/ 整個被 .gitignore 擋掉(裡面有明碼帳密),
# 而它的散佈路徑是岸端同步:sftp_download_settings.json 是
# STANDARD/share/sftp_transfer → "." recursive + overwrite,而 config/ **不在**
# sftp_download_ignore.txt 裡 —— 所以放上 STANDARD 的 config/ 會覆蓋每一條船。
# 要讓這支腳本能用,config/web_ota_gpg_prefix_download_settings.json 必須長這樣:
#
#   {
#     "mode": "download",
#     "host": "61.56.200.137",
#     "port": 22,
#     "device_name": "{vsl_name}_{ipc}_web_ota_key",
#     "username": "aiuser",
#     "password": "<與其他 config 同一組>",
#     "remote_path": "/fleet/wanhai_nssms_deploy/STANDARD/web_ota/gpg_prefix.txt",
#     "local_path": "{nvme}/wanhai_nssms/web/site/ota",
#     "recursive": false,
#     "resume": true,
#     "wait_for_network": true,
#     "retry_count": 0,
#     "duplicate_mode": "overwrite"
#   }
#
# 【local_path 一定要用 {nvme}】不要寫死。資料碟沒掛載時 _resolve_nvme() 會中止該任務
# 而不是 fallback 到主碟 —— 那正是要的:密鑰落在主碟上,資料碟船永遠讀不到。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
cd "$BASE_DIR"

# shellcheck source=script/_dev_guard.sh
source "$SCRIPT_DIR/_dev_guard.sh"
dev_guard "$BASE_DIR"

config="$SCRIPT_DIR/../config/web_ota_gpg_prefix_download_settings.json"
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

"$VENV_PY" "$BASE_DIR/main.py" --cli --config "$config"
