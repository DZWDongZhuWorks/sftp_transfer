"""共用設定檔（settings.json）讀取/開啟工具，CLI 與 GUI 皆透過此模組載入預設參數。"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"

# 船舶基本資訊檔（各船部署時放置），內容如 {"vsl_name": "WH289", "ipc": "IPC-1"}。
# 設定檔字串值中的 {vsl_name}、{ipc} 等佔位符會以此檔案的對應值替換。
# 可用環境變數 VESSEL_INFO_PATH 覆蓋路徑（測試或特殊部署用）。
VESSEL_INFO_PATH = Path(__file__).resolve().parent.parent / ".env" / "vessel_basic_info.json"

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")

# 本地端路徑欄位。這些值由本機的檔案系統解讀，**相對路徑相對於 CWD**，而所有
# script/run_*.sh 都會先 `cd "$BASE_DIR"`（= share/sftp_transfer），所以寫相對路徑
# 就是機器無關的。remote_path 刻意不在此列：那是 SFTP 伺服器上的路徑,不能用本機
# 的家目錄去解讀它。
_LOCAL_PATH_FIELDS = ("local_path", "ignore_file", "log_dir", "key_file")

# 看起來像 shell 變數或家目錄縮寫的值。本模組**不做**任何展開（見 ConfigPathError）。
_SHELLISH_PATH = re.compile(r"^~|\$\{|\$[A-Za-z_]")


class PlaceholderError(ValueError):
    """設定檔中的佔位符無法解析（vessel 資訊檔不存在、壞掉或缺少對應 key）。"""


class ConfigPathError(PlaceholderError):
    """本地端路徑欄位寫了 shell 才看得懂的東西（~ 或 $VAR）。

    為什麼要明確報錯，而不是展開、也不是默默接受
    ----------------------------------------------
    本模組只做 {name} 佔位符替換，從不呼叫 expanduser/expandvars。而 `~` 與
    `$HOME` 都**不是**絕對路徑，於是會被當成相對路徑，相對於 CWD（= BASE_DIR）
    解析成：

        share/sftp_transfer/~/y
        share/sftp_transfer/$HOME/Documents/x

    也就是真的建出名字叫 `~` 或 `$HOME` 的目錄，然後把檔案下載進去。不會有任何
    錯誤訊息，只是東西全放錯位置 —— 比直接失敗難查得多。所以這裡選擇當場拒絕。

    不改成「幫忙展開」是刻意的：config/ 是**集中管理、由 SFTP OTA 發佈到全船隊**
    的（見 .sftp_upload_manifest.json 與 sftp_download_ignore.txt —— config/ 不在
    排除清單、duplicate_mode=overwrite）。一份共用的設定檔裡不該有任何需要「依這台
    機器的環境變數才知道指到哪」的值；正確做法是寫相對路徑。

    刻意繼承 PlaceholderError:main.py / gui.py / pack_upload.py 已經有「設定檔的值
    不可用 → 印訊息並中止」的處理路徑,讓它們不必逐一改就能給出一樣的使用者體驗。
    """


def _check_local_paths(settings):
    """本地端路徑欄位不得含 shell 語法。原地檢查，不修改值。"""
    def check(field, value):
        if isinstance(value, str) and _SHELLISH_PATH.search(value):
            raise ConfigPathError(
                f"設定檔欄位 {field} 的值 {value!r} 含 shell 語法（~ 或 $VAR），"
                f"本工具不做展開，會被當成相對路徑而把檔案放到錯誤位置。"
                f"請改用相對路徑（相對於 share/sftp_transfer，例如 local_path: \".\"、"
                f"ignore_file: \"config/xxx_ignore.txt\"、log_dir: \"logs\"）"
                f"或絕對路徑。"
            )

    for field in _LOCAL_PATH_FIELDS:
        if field not in settings:
            continue
        value = settings[field]
        if isinstance(value, list):
            for item in value:
                check(field, item)
        else:
            check(field, value)

SETTINGS_TEMPLATE = {
    "mode": "download",
    # 流類別，與 mode（方向）正交，僅供 run_selected_transfers 的守門判斷，main.py 不讀取。
    # deploy＝程式／設定發佈流（岸→船），受方向鎖管制；telemetry＝資料回傳流（船→岸），不受管制。
    "trans_type": "deploy",
    "host": "",
    "port": 22,
    "device_name": "",
    "version_info": "",
    "username": "",
    "password": "",
    "key_file": "",
    "remote_path": "",
    "local_path": "",
    "auto_reconnect": True,
    "resume": True,
    "wait_for_network": True,
    "recursive": True,
    "ignore_file": "",
    "retry_count": 0,
    "retry_delay": 10,
    "upload_log": False,
    "log_remote_dir": "",
    "log_dir": "logs",
    "duplicate_mode": "overwrite",
    "duplicate_suffix": "copy",
}


def _load_vessel_info():
    path = Path(os.environ.get("VESSEL_INFO_PATH") or VESSEL_INFO_PATH)
    if not path.exists():
        raise PlaceholderError(f"設定檔使用了佔位符，但找不到船舶資訊檔：{path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            info = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise PlaceholderError(f"船舶資訊檔 {path} 讀取失敗：{e}")
    if not isinstance(info, dict):
        raise PlaceholderError(f"船舶資訊檔 {path} 內容必須是 JSON 物件")
    return {key: str(value) for key, value in info.items()}


def resolve_placeholders(settings):
    """把設定值字串中的 {vsl_name}、{ipc} 等佔位符換成 vessel_basic_info.json 的對應值。

    - 處理字串值與字串陣列（如 remote_path 的路徑陣列）內的每個元素，其他型別原樣保留。
    - 完全沒有佔位符時不會去讀船舶資訊檔（該檔可以不存在）。
    - 佔位符無法解析（檔案不存在／缺少 key）時拋出 PlaceholderError，
      避免把 "{vsl_name}" 這種字面文字當成路徑上傳到伺服器。
    """
    vessel_info = None

    def resolve_text(field, value):
        nonlocal vessel_info
        for name in _PLACEHOLDER.findall(value):
            if vessel_info is None:
                vessel_info = _load_vessel_info()
            if name not in vessel_info:
                raise PlaceholderError(
                    f"設定檔欄位 {field} 的佔位符 {{{name}}} 在船舶資訊檔中找不到對應值"
                    f"（可用的 key：{', '.join(sorted(vessel_info)) or '（無）'}）"
                )
        if vessel_info:
            value = _PLACEHOLDER.sub(lambda m: vessel_info[m.group(1)], value)
        return value

    resolved = {}
    for field, value in settings.items():
        if isinstance(value, str):
            value = resolve_text(field, value)
        elif isinstance(value, list):
            value = [resolve_text(field, item) if isinstance(item, str) else item for item in value]
        resolved[field] = value
    return resolved


def load_settings(path=SETTINGS_PATH):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"警告：設定檔 {path} 讀取失敗，將忽略此檔案：{e}", file=sys.stderr)
        return {}
    resolved = resolve_placeholders(data)
    # 佔位符替換**之後**才檢查:{home} 之類的替換結果也要納入判斷。
    _check_local_paths(resolved)
    return resolved


def save_settings(path, data):
    """把設定內容寫成 JSON 檔（覆蓋既有內容），回傳檔案路徑。"""
    path = Path(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def ensure_settings_file(path=SETTINGS_PATH, seed=None):
    """若設定檔不存在則建立一份（可用目前畫面上的值當作起始內容），回傳檔案路徑。"""
    path = Path(path)
    if not path.exists():
        data = dict(SETTINGS_TEMPLATE)
        if seed:
            data.update({k: v for k, v in seed.items() if v not in (None, "")})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def open_in_default_app(path):
    path = str(path)
    if sys.platform.startswith("win"):
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", path])
    else:
        subprocess.run(["xdg-open", path])
