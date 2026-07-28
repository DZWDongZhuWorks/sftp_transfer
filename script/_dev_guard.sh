#!/usr/bin/env bash
# 開發機 (CLINK) 守門共用函式，供各 run_*_download.sh source 使用。
#
# 為什麼需要：所有下載設定檔都是 duplicate_mode=overwrite，且 local_path 指向開發端
# 工作區（/home/.../wanhai_nssms/ 底下，含 radar、SHM-stream-manager 等 git repo）。
# 這台 (vsl_name=CLINK) 是 STANDARD 的發佈源頭，在其上執行任何下載都會把 STANDARD
# 的內容覆蓋回本機、清掉尚未發佈的開發修改，因此在 CLINK 上一律略過下載。
#
# 失效方向安全：讀不到船舶資訊檔／解析失敗／非 CLINK，一律當一般船照常下載，
# 船隊永遠不會因為守門而被誤關。上傳（dev → STANDARD 發佈）不受此守門影響。
#
# 用法（各腳本 cd 到 BASE_DIR 後）：
#   source "$SCRIPT_DIR/_dev_guard.sh"
#   dev_guard "$BASE_DIR"

dev_guard() {
    local base="$1"
    # 船舶資訊檔路徑與 settings.py 一致（可用 VESSEL_INFO_PATH 覆蓋，預設 share/.env/）。
    local vinfo="${VESSEL_INFO_PATH:-$base/../.env/vessel_basic_info.json}"
    local vsl
    vsl="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("vsl_name",""))' "$vinfo" 2>/dev/null || true)"
    if [[ "${vsl^^}" == "CLINK" ]]; then
        echo "偵測到開發機 (vsl_name=CLINK)，略過下載，避免覆蓋未提交的修改。" >&2
        exit 0
    fi
}
