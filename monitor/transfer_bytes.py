# -*- coding: utf-8 -*-
"""TRANSFER_BYTES 的**唯一解析者**：本趟傳輸量那一行，以及傳輸 log 的檔名。

格式由 downloader.SFTPBase._log_transfer_bytes 寫出（README【Log 檔案格式】的事件表）：

    [TRANSFER_BYTES] 本趟傳輸量 1.2MB direction="download" bytes=1234567 files=3 elapsed_s=42.1 result="success"

【誰在用】scheduler 的 dashboard（sources/transfer_traffic.py）用 importlib 直接載入這一支，
依任務、日期加總每天的流量。格式只在這裡解析一次 —— 第二份 parser 會在格式第一次改版時
就開始說謊。log_monitor 的 device 切法（DEVICE）也從這裡取，同一個檔名不會被兩邊切成兩樣。

【為什麼是獨立的小模組，不放進 log_monitor.py】dashboard 用**系統 python3** 跑，Bionic 上是
3.6，而 log_monitor.py 用了 dataclasses（3.7；sftp_transfer 的 venv 靠 backport 輪子補，系統
python3 補不到）。所以這一支只用標準庫、不用任何 backport，列在 tests/test_offline_deploy.py
的 SYSTEM_PYTHON_FILES。也不 import 本專案的其他模組（downloader 會帶進 paramiko）。

【只讀檔尾】這一行寫在 run() 收尾、回傳 log 之前，後面只剩回傳 log 的幾行，所以永遠落在
檔尾。船上一台 IPC 30 天有上千份 CSV（船隊實測每台 615–1251 份、17–54 MB），整份剖析
是不必要的成本。

Python 3.6 相容。
"""
import csv
import json
import os
import re

EVENT = "TRANSFER_BYTES"
_TAG = "[%s]" % EVENT

#: 讀檔尾多少位元組找那一行。它後面只有回傳 log 的幾行（數百位元組），16 KB 是大量餘裕；
#: 前面的「失敗清單」再長也在它**之前**，不影響。
TAIL_BYTES = 16384

#: downloader.create_logger 的檔名：{D|U}_{device_name}_{YYYYmmdd_HHMMSS}.csv
LOG_NAME = re.compile(r"^(?P<prefix>[DU])_(?P<device>.+)_(?P<date>\d{8})_(?P<time>\d{6})\.csv$")

#: device_name 的慣例 {vsl_name}_{ipc}_{task}。vsl 必須容許底線（少數船用英文船名，如
#: WH_PORT_KELANG_EXPRESS），而欄位本身就用 _ 串接；非貪婪是刻意的 —— vsl 是第一個欄位，
#: 邊界取第一個 _IPC-N_，task 是自由格式、本身就可能含 IPC-N。船名不含 -，所以第一個
#: _IPC-N_ 必為真邊界。（原本住在 log_monitor._RE_DEVICE，現在那裡從這裡取。）
DEVICE = re.compile(r"^(?P<vsl>[A-Za-z0-9_]+?)_(?P<ipc>IPC-\d+)_(?P<comp>.+)$")

# 值是 JSON 表示法（downloader.diagnostic_message）：字串帶引號、可含跳脫；數字、null、true 不帶。
_FIELD = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|[^\s"]+)')
_DIRECTIONS = ("download", "upload")


def _count(value):
    """非負整數才算數；bool 是 int 的子類別，要先排除。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def parse_log_name(name):
    """傳輸 log 的檔名 → dict；不是這個形狀回 None。

    回傳 direction（download / upload）、device（檔名裡的 device_name，已被 create_logger
    換掉不安全字元）、task（去掉 {vsl}_{ipc}_ 的那段；不合慣例的靜態名整個當 task）、
    vsl / ipc（不合慣例時是 None）、date（YYYY-MM-DD）、time（HH:MM:SS）。
    日期與時間是**船機時鐘**寫的，這裡不驗證合不合理 —— 那是呼叫端的判斷。
    """
    m = LOG_NAME.match(name or "")
    if not m:
        return None
    device = m.group("device")
    dm = DEVICE.match(device)
    d, t = m.group("date"), m.group("time")
    return {
        "direction": "upload" if m.group("prefix") == "U" else "download",
        "device": device,
        "task": dm.group("comp") if dm else device,
        "vsl": dm.group("vsl") if dm else None,
        "ipc": dm.group("ipc") if dm else None,
        "date": "%s-%s-%s" % (d[:4], d[4:6], d[6:]),
        "time": "%s:%s:%s" % (t[:2], t[2:4], t[4:]),
    }


def parse_message(message):
    """CSV 的 message 欄 → dict；不是 TRANSFER_BYTES 或 bytes 不合法回 None。

    回傳 bytes、files、elapsed_s、direction、result。bytes 是唯一必要的欄位；其餘不合法
    時是 None，而不是讓整筆作廢 —— 少一個輔助欄位不該讓那一趟的流量變成未知。
    """
    if not isinstance(message, str) or not message.startswith(_TAG):
        return None
    fields = {}
    for key, raw in _FIELD.findall(message[len(_TAG):]):
        try:
            fields[key] = json.loads(raw)
        except ValueError:
            continue
    nbytes = _count(fields.get("bytes"))
    if nbytes is None:
        return None
    elapsed = fields.get("elapsed_s")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or elapsed < 0:
        elapsed = None
    direction = fields.get("direction")
    result = fields.get("result")
    return {
        "bytes": nbytes,
        "files": _count(fields.get("files")),
        "elapsed_s": elapsed,
        "direction": direction if direction in _DIRECTIONS else None,
        "result": result if isinstance(result, str) else None,
    }


def read_transfer_bytes(path, tail_bytes=TAIL_BYTES):
    """讀傳輸 log 的檔尾，回**最後一筆** TRANSFER_BYTES（parse_message 的 dict）或 None。

    None 的意思是「這份 log 沒有傳輸量紀錄」：v0.34.0 之前的版本、被 SIGKILL 的那一趟、
    或還在跑。**不是 0** —— 分辨哪一種、要不要當成未計，是呼叫端的事。
    讀檔的 OSError 照常往外拋（檔案在列舉與讀取之間被清理器刪掉是正常的，呼叫端決定怎麼算）。

    一份 log 一趟是 CLI 的契約；GUI 在同一個 logger 上連跑好幾趟時會有好幾行，這裡只取最後
    一筆，前幾趟會少算 —— GUI 只在岸端手動用，不進船上的統計。
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - int(tail_bytes))
        fh.seek(start)
        data = fh.read()
    text = data.decode("utf-8", "replace")
    if start > 0:
        # 從檔案中間開始讀，第一行多半是半截：丟掉。
        text = text.split("\n", 1)[1] if "\n" in text else ""
    found = None
    for line in text.splitlines():
        if _TAG not in line:
            continue
        # 這一行的 message 不含換行，所以單行交給 csv 剖析是安全的（引號跳脫由 csv 處理）。
        try:
            row = next(csv.reader([line]))
        except (csv.Error, StopIteration):
            continue
        if len(row) < 5:
            continue
        rec = parse_message(row[4])
        if rec is not None:
            found = rec
    return found
