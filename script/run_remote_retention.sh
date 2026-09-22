#!/usr/bin/env bash
# 岸端 SFTP log 的保留政策：刪掉遠端 sftp_logs 底下超過 21 天的傳輸紀錄。
# 由 scheduler 的 nssms-remote-log-retention.timer 每天觸發一次。
#
# 預設**只預覽**（remote_retention.py 不給 --apply 就不刪任何東西）。要真的刪，
# 設 REMOTE_RETENTION_APPLY=1。設計論證見 remote_retention.py 的檔頭。
#
# 為什麼是 21 天而不是 30：本地鏡像 fleet_logs 的清掃是 30 天（scheduler 的
# cleanup_rules.json 規則 sftp-fleet-reports），而遠端窗刻意短 9 天 —— 那個差額就是
# 逐檔「本地確實有同一份」這道檢查能成立的全部理由。兩邊相等時它會失效，因為兩者用
# 同一個時間戳、同一天到期、誰先跑誰贏。
#
# 這支刻意**不呼叫 _dev_guard.sh**：守門擋的是「在 CLINK 發佈源頭跑下載會覆蓋未發佈的
# 修改」，而本程式不下載任何東西，且岸端監控機就是 CLINK —— 加守門等於讓它永遠不能跑。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

cd "$BASE_DIR"

config="$BASE_DIR/config/log_monitor_sync.json"

# 【彙整端閘門】unit 本身用 `# NSSMS-BaseIPC=ipc1` 限定實體 IPC，但船隊每一艘的 IPC-1
# 都符合那個條件，而只有岸端那一台在做船隊 log 彙整。判準用**設定檔在不在**：
# config/log_monitor_sync.json 依慣例不納入版控（見 monitor/README.md），所以它天然只
# 存在於彙整端。
#
# 這裡 exit 0 而不是 exit 2 是刻意的：這是「本機不是這個工作的對象」，不是故障。回非零
# 會讓船隊每一艘 IPC-1 每天多一個 failed unit，而那會遮蔽真正的故障
# （同 install_timers.sh 的 OPTIONAL_UNITS 註解所記的教訓）。
if [[ ! -f "$config" ]]; then
    echo "本機沒有 $config —— 不是岸端 log 彙整端，略過。"
    exit 0
fi

VENV_PY="${SFTP_TRANSFER_VENV:-$HOME/venv/wanhai_nssms/share/sftp_transfer}/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
    echo "找不到 sftp_transfer 專屬 venv 的 Python: $VENV_PY" >&2
    echo "請先執行 deploy/deploy_offline.sh 建立 venv。" >&2
    exit 2
fi

args=(--config "$config" --retention-days "${REMOTE_RETENTION_DAYS:-21}")
if [[ "${REMOTE_RETENTION_APPLY:-0}" == "1" ]]; then
    args+=(--apply)
fi

exec "$VENV_PY" "$BASE_DIR/remote_retention.py" "${args[@]}" "$@"
