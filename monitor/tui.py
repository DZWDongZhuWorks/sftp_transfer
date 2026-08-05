#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tui.py — log_monitor 的 curses 互動式終端機介面。

以 stdlib `curses` 提供接近 HTML 報告的控制能力：鍵盤展開/收合、上下選取、
搜尋、方向/狀態過濾、只看異常、看單一裝置明細、即時重載。

由 `log_monitor.py --tui` 於互動式終端機呼叫 `run_app(args)`；非 TTY 或無 curses
時，`log_monitor` 會自動退回靜態輸出。

設計：curses 只出現在最外層（draw / run 迴圈）；資料壓平與狀態轉移都是純函式，
可在無終端機環境下單元測試。資料層（collect/aggregate/build_tree）完全沿用 log_monitor。
"""
from __future__ import annotations

import curses
import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from monitor.log_monitor import (
    TS_FMT,
    _apply_filters,
    _SEVERITY,
    _counts_str,
    _detail_str,
    _humanize_age,
    _MODE_LABEL,
    aggregate_by_device,
    build_tree,
    collect_logs,
    device_detail_lines,
    group_is_problem,
    read_log_rows,
    sync_logs,
    write_html_report,
)

_MODE_CYCLE = ["", "download", "upload"]
_STATUS_CYCLE = ["all", "ok", "stale", "problem"]
_PAIR = {"success": 1, "stale": 2, "incomplete": 2, "partial": 3, "aborted": 3}
_SYNC_LINE_LIMIT = 20
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ENTER_KEYS = (10, 13, curses.KEY_ENTER)
_SORT_CYCLE = ["船隻名稱", "更新時間", "嚴重度", "裝置名稱"]
_MODE_ARROW = {"download": "↓", "upload": "↑"}
# 平坦模式的欄寬（顯示欄）：方向 船 IPC 元件 最後執行 檔案 成/略/失 距今 摘要
_FLAT_COLS = (1, 10, 6, 20, 11, 5, 9, 8)
_FLAT_ALIGN = ("left", "left", "left", "left", "left", "right", "right", "left")
_FLAT_GUTTER = "    "  # 對齊 _draw 的 depth-0 縮排("  ") + 狀態燈("●") + 空白


# --- 顯示寬度（CJK 全形字佔 2 欄）------------------------------------------
def _char_width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def disp_width(s: str) -> int:
    return sum(_char_width(c) for c in s)


def fit_display(s: str, cols: int) -> tuple[str, int]:
    """截斷 s 至顯示寬度不超過 cols，回傳 (截斷後字串, 實際顯示寬度)。"""
    if cols <= 0:
        return "", 0
    out, w = [], 0
    for c in s:
        cw = _char_width(c)
        if w + cw > cols:
            break
        out.append(c)
        w += cw
    return "".join(out), w


def pad_display(s: str, cols: int, align: str = "left") -> str:
    """依顯示寬度截斷並補空白到剛好 cols 欄（CJK 對齊用）。"""
    fs, w = fit_display(s, cols)
    fill = " " * (cols - w)
    return fill + fs if align == "right" else fs + fill


def slice_display(s: str, off: int, cols: int) -> str:
    """取 s 從「顯示欄」off 起、寬不超過 cols 欄的片段（水平捲動用）。

    不能用字元索引切片：off 落在全形字中間時該字只剩右半可見，改補一格空白佔位，
    整列才不會左移一欄。右緣沿用 fit_display 的規則，不會吐出半個全形字。
    """
    if cols <= 0:
        return ""
    out, w = [], 0
    for c in s:
        cw = _char_width(c)
        if w + cw <= off:      # 完全在可視範圍左側
            w += cw
            continue
        if w < off:            # 全形字被 off 切半 → 以空白補其右半
            out.append(" ")
            w += cw
            continue
        out.append(c)
        w += cw
    return fit_display("".join(out), cols)[0]


# ---------------------------------------------------------------------------
# 純資料 / 狀態（無 curses，可測）
# ---------------------------------------------------------------------------
@dataclass
class Row:
    kind: str  # 'mode' | 'vessel' | 'ipc' | 'device'
    depth: int
    key: tuple
    text: str
    status: str  # 用於著色：display_status 或群組 worst
    ref: object


@dataclass
class TuiState:
    expanded: set = field(default_factory=set)
    seen: set = field(default_factory=set)
    query: str = ""
    mode: str = ""       # '' | 'download' | 'upload'
    status: str = "all"  # 'all' | 'ok' | 'stale' | 'problem'
    only_problem: bool = False
    sel_key: tuple | None = None
    scroll: int = 0
    now: datetime | None = None
    flat: bool = False                 # True＝平坦模式（不分群，全船隊一張表）
    sort_key: str = _SORT_CYCLE[0]      # 見 _SORT_CYCLE
    sort_desc: bool = True             # 預設由 _SORT_CYCLE 首欄位降冪排序
    html_note: str = ""                # --html 每輪寫出的結果，顯示在第二行（""＝未啟用）


def _badge(s) -> str:
    parts = [f"裝置 {s.total}", f"正常 {s.ok}"]
    if s.stale:
        parts.append(f"過期 {s.stale}")
    if s.bad:
        parts.append(f"異常 {s.bad}")
    return "｜".join(parts)


def _searchtext(d) -> str:
    return " ".join(
        [d.device_name, d.component, d.vessel or "", d.ipc or "", _detail_str(d)]
    ).lower()


def _device_line(d, now: datetime | None) -> str:
    rec = d.latest
    comp = pad_display(d.component, 20)
    last = pad_display(rec.started_at.strftime(TS_FMT) if rec.started_at else "—", 19)
    files = pad_display("—" if rec.file_count is None else str(rec.file_count), 5, "right")
    counts = pad_display(_counts_str(rec), 9, "right")
    age = pad_display(_humanize_age(d.last_seen, now) if now else "—", 9)
    detail = fit_display(_detail_str(d), 60)[0]
    return f"{comp} {last} {files} {counts} {age} {detail}"


def _flat_cells(cells) -> str:
    """依 _FLAT_COLS 逐欄 pad 成固定顯示寬（CJK 安全）。

    表頭與內文共用這一個函式，欄位起點由 _FLAT_COLS 這個單一來源保證，
    不必兩邊各算一次（同 CSV 檢視用 _CSV_PINNED_W 保證對齊的手法）。
    """
    fixed = " ".join(
        pad_display(c, w, a) for c, w, a in zip(cells, _FLAT_COLS, _FLAT_ALIGN)
    )
    return f"{fixed} {cells[len(_FLAT_COLS)]}"  # 最後一欄（摘要）不設寬，由 _put 截斷


def _device_line_flat(item, now: datetime | None) -> str:
    """平坦模式的裝置列：沒有分群結構交代身分，故列本身要帶方向/船/IPC。

    方向必須顯示——同一台裝置的下載與上傳是兩筆 DeviceStatus，少了方向兩列會長得一樣。
    時間用 %m-%d %H:%M 而非 TS_FMT，省 8 欄留給摘要。
    """
    d = item.dev
    rec = d.latest
    return _flat_cells([
        _MODE_ARROW.get(rec.mode, "?"),
        item.vessel,
        item.ipc,
        d.component,
        rec.started_at.strftime("%m-%d %H:%M") if rec.started_at else "—",
        "—" if rec.file_count is None else str(rec.file_count),
        _counts_str(rec),
        _humanize_age(d.last_seen, now) if now else "—",
        _detail_str(d),
    ])


def flat_header_line() -> str:
    """平坦模式的欄名列（含 gutter，與內文同欄起點）。

    方向欄只有 1 欄寬，標籤得用半形寬的字：↕ 與資料的 ↓/↑ 同為東亞歧義字（寬 1），
    寫成全形的「向」會因為塞不進 1 欄而被 pad_display 整個丟掉。
    """
    return _FLAT_GUTTER + _flat_cells(
        ["↕", "船", "IPC", "元件", "最後執行", "檔案", "成/略/失", "距今", "摘要"]
    )


def sort_value(d, key: str):
    """單一欄位的排序值（純函式）。

    刻意不含 tiebreak：同鍵值的次序交給穩定排序保留「輸入順序」，也就是資料層
    build_tree 給的 船/IPC/元件 次序。例如同一船名的裝置會維持原有 IPC/元件次序。
    把 tiebreak 寫進 key 反而會被 reverse 一起翻轉，連預設畫面都會變。
    """
    if key == "更新時間":
        # 從未回報（last_seen=None）視為最舊，沿用 log_monitor 的 `or datetime.min` 慣例
        return d.last_seen or datetime.min
    if key == "裝置名稱":
        # casefold：元件名大小寫混雜（RADAR_UPLOADER vs ecdis），純 ASCII 序會把全大寫全推到最前
        return d.device_name.casefold()
    if key == "船隻名稱":
        return (d.vessel or "（未分類）").casefold()
    return _SEVERITY.get(d.display_status, 0)  # 未知狀態→0，沿用資料層 .get(x, 0) 慣例


def sort_devices(devices: list, key: str, desc: bool) -> list:
    """依欄位排序裝置（穩定，不就地改動來源）。"""
    return sorted(devices, key=lambda d: sort_value(d, key), reverse=desc)


def sort_vessels(vessels: list, key: str, desc: bool) -> list:
    """分群模式的船群次序：只有「船隻名稱」欄位會重排，其餘維持資料層順序。

    船名對整個 IPC 群組是常數（樹就是依 船→IPC 分的），套在群組內的裝置列上必然是
    no-op，所以這個欄位得作用在「船」這一層才有意義。其餘欄位是裝置屬性，群組層沒有
    單一值可比，維持 build_tree 的 (未分類置底, -嚴重度, 名稱) 次序。

    排序值與 sort_value("船隻名稱") 同為 name.casefold()，兩種檢視的船名次序才一致
    （含 （未分類） 這個 sentinel：升冪在最後、降冪在最前）。
    """
    if key != "船隻名稱":
        return vessels
    return sorted(vessels, key=lambda g: g.name.casefold(), reverse=desc)


def sort_label(key: str, desc: bool) -> str:
    return f"排序：{key}{'↓' if desc else '↑'}"


def _device_matches(d, state: TuiState) -> bool:
    if state.mode and d.latest.mode != state.mode:
        return False
    if state.only_problem and d.display_status == "success":
        return False
    st = state.status
    if st and st != "all":
        ds = d.display_status
        if st == "ok" and ds != "success":
            return False
        if st == "stale" and ds != "stale":
            return False
        if st == "problem" and ds in ("success", "stale"):
            return False
    if state.query and state.query.lower() not in _searchtext(d):
        return False
    return True


def flatten_tree(tree, state: TuiState, now: datetime | None) -> list[Row]:
    """依 expanded 集合與過濾條件把樹壓平成目前可見列（純函式）。

    收合的群組不展開子列；過濾後沒有任何可見裝置的群組整個略過。
    """
    rows: list[Row] = []
    for m in tree:
        m_has = any(
            _device_matches(d, state)
            for v in m.vessels
            for ip in v.ipcs
            for d in ip.devices
        )
        if not m_has:
            continue
        mkey = ("M", m.mode)
        rows.append(
            Row("mode", 0, mkey, f"{_MODE_LABEL.get(m.mode, m.mode)}  [{_badge(m.summary)}]",
                m.summary.worst, m)
        )
        if mkey not in state.expanded:
            continue
        for v in sort_vessels(m.vessels, state.sort_key, state.sort_desc):
            v_has = any(
                _device_matches(d, state) for ip in v.ipcs for d in ip.devices
            )
            if not v_has:
                continue
            vkey = ("V", m.mode, v.name)
            rows.append(
                Row("vessel", 1, vkey, f"{v.name}  [{_badge(v.summary)}]", v.summary.worst, v)
            )
            if vkey not in state.expanded:
                continue
            for ip in v.ipcs:
                dvs = [d for d in ip.devices if _device_matches(d, state)]
                if not dvs:
                    continue
                ikey = ("I", m.mode, v.name, ip.name)
                rows.append(
                    Row("ipc", 2, ikey, f"{ip.name}  [{_badge(ip.summary)}]", ip.summary.worst, ip)
                )
                if ikey not in state.expanded:
                    continue
                # 群組內也套使用者排序，o/O 在分群模式才不是死鍵；
                # 這只是 TUI 呈現，build_tree 給的順序不動（HTML/CLI 不受影響）
                for d in sort_devices(dvs, state.sort_key, state.sort_desc):
                    dkey = ("D", m.mode, v.name, ip.name, d.component)
                    rows.append(Row("device", 3, dkey, _device_line(d, now), d.display_status, d))
    return rows


@dataclass
class FlatItem:
    """平坦模式一列的來源：裝置＋它在樹裡的群組名。

    群組名取自樹而非 d.vessel/d.ipc——樹已把 None 正規化成 （未分類）/—，而分群模式的
    key 也是用樹的名字組出來的。取樹才能保證兩邊 key 完全一致，選取才不會在切換時掉。
    """

    mode: str
    vessel: str
    ipc: str
    dev: object


def tree_devices(tree) -> list[FlatItem]:
    """把樹走回扁平清單（純函式）。

    輸出順序＝資料層既有順序，正是 sort_devices 穩定排序所依賴的天然 tiebreak。
    走訪樹而不改 load_tree 的簽章：葉節點就是裝置，重走一次是 O(n)，
    而且只有樹上才有正規化過的群組名。
    """
    return [
        FlatItem(m.mode, v.name, ip.name, d)
        for m in tree for v in m.vessels for ip in v.ipcs for d in ip.devices
    ]


def flatten_flat(tree, state: TuiState, now: datetime | None) -> list[Row]:
    """平坦模式：忽略 方向/船/IPC 分群，全船隊裝置壓成單一表格（純函式）。

    全部 depth=0、kind='device'，key 與分群模式同一組 ("D", mode, vessel, ipc, component)。
    """
    items = [it for it in tree_devices(tree) if _device_matches(it.dev, state)]
    items = sorted(
        items, key=lambda it: sort_value(it.dev, state.sort_key), reverse=state.sort_desc
    )
    return [
        Row(
            "device",
            0,
            ("D", it.mode, it.vessel, it.ipc, it.dev.component),
            _device_line_flat(it, now),
            it.dev.display_status,
            it.dev,
        )
        for it in items
    ]


def visible_rows(tree, state: TuiState, now: datetime | None) -> list[Row]:
    """依 state.flat 選壓平方式；_main_loop 只呼叫這一個，分支不外流到 curses 層。"""
    return flatten_flat(tree, state, now) if state.flat else flatten_tree(tree, state, now)


def all_group_keys(tree) -> list[tuple]:
    keys: list[tuple] = []
    for m in tree:
        keys.append(("M", m.mode))
        for v in m.vessels:
            keys.append(("V", m.mode, v.name))
            for ip in v.ipcs:
                keys.append(("I", m.mode, v.name, ip.name))
    return keys


def seed_expanded(tree, state: TuiState) -> None:
    """首次見到的群組：依 group_is_problem 設預設展開；已見過的保留用戶操作。"""
    def consider(key, summary):
        if key not in state.seen:
            state.seen.add(key)
            if group_is_problem(summary):
                state.expanded.add(key)

    for m in tree:
        consider(("M", m.mode), m.summary)
        for v in m.vessels:
            consider(("V", m.mode, v.name), v.summary)
            for ip in v.ipcs:
                consider(("I", m.mode, v.name, ip.name), ip.summary)


def global_counts(tree) -> tuple[int, int, int, int]:
    t = o = s = b = 0
    for m in tree:
        t += m.summary.total
        o += m.summary.ok
        s += m.summary.stale
        b += m.summary.bad
    return t, o, s, b


# --- reducer（純狀態轉移）--------------------------------------------------
def toggle(state: TuiState, key: tuple) -> None:
    if key in state.expanded:
        state.expanded.discard(key)
    else:
        state.expanded.add(key)


def expand_all(state: TuiState, tree) -> None:
    state.expanded.update(all_group_keys(tree))


def collapse_all(state: TuiState) -> None:
    state.expanded.clear()


def cycle_mode(state: TuiState) -> None:
    i = _MODE_CYCLE.index(state.mode) if state.mode in _MODE_CYCLE else 0
    state.mode = _MODE_CYCLE[(i + 1) % len(_MODE_CYCLE)]


def cycle_status(state: TuiState) -> None:
    i = _STATUS_CYCLE.index(state.status) if state.status in _STATUS_CYCLE else 0
    state.status = _STATUS_CYCLE[(i + 1) % len(_STATUS_CYCLE)]


def toggle_flat(state: TuiState) -> None:
    """切換 平坦 ↔ 分群。

    離開平坦時先把選取裝置的祖先群組展開：兩邊 key 相同，但分群模式只列出「已展開」的
    裝置，祖先收合著就會被 clamp_selection 彈回第 0 列——游標無故從畫面中間飛到最上面。
    """
    state.flat = not state.flat
    if not state.flat:
        reveal(state, state.sel_key)
    state.scroll = 0


def cycle_sort(state: TuiState) -> None:
    i = _SORT_CYCLE.index(state.sort_key) if state.sort_key in _SORT_CYCLE else 0
    state.sort_key = _SORT_CYCLE[(i + 1) % len(_SORT_CYCLE)]


def toggle_sort_dir(state: TuiState) -> None:
    state.sort_desc = not state.sort_desc


def toggle_problem(state: TuiState) -> None:
    state.only_problem = not state.only_problem


def set_query(state: TuiState, q: str) -> None:
    state.query = q
    state.scroll = 0


def selected_index(rows: list[Row], state: TuiState) -> int:
    for i, r in enumerate(rows):
        if r.key == state.sel_key:
            return i
    return 0


def clamp_selection(rows: list[Row], state: TuiState) -> None:
    if not rows:
        state.sel_key = None
        return
    if state.sel_key is None or all(r.key != state.sel_key for r in rows):
        state.sel_key = rows[0].key


def move_selection(rows: list[Row], state: TuiState, delta: int) -> None:
    if not rows:
        state.sel_key = None
        return
    idx = selected_index(rows, state)
    state.sel_key = rows[max(0, min(len(rows) - 1, idx + delta))].key


def parent_key(key: tuple) -> tuple | None:
    if key[0] == "D":
        return ("I",) + key[1:4]
    if key[0] == "I":
        return ("V",) + key[1:3]
    if key[0] == "V":
        return ("M",) + key[1:2]
    return None


def reveal(state: TuiState, key: tuple | None) -> None:
    """展開 key 的所有祖先群組，讓它在分群模式必然可見。"""
    k = parent_key(key) if key else None
    while k:
        state.expanded.add(k)
        k = parent_key(k)


def collapse_or_parent(state: TuiState, row: Row | None) -> None:
    """←/h：先收合自己，否則跳回父群；平坦模式沒有父群故不跳。

    平坦模式若跳父群，sel_key 會被設成不存在的 ("I", …)，clamp_selection 只能彈回
    第 0 列——游標會無故從畫面中間飛到最上面。
    """
    if row is None:
        return
    if row.kind != "device" and row.key in state.expanded:
        state.expanded.discard(row.key)
        return
    if state.flat:
        return
    pk = parent_key(row.key)
    if pk:
        state.sel_key = pk


def key_action(ch: int) -> str | None:
    """把原始按鍵碼映射成動作標籤（純函式，可測；curses.KEY_* 為模組常數）。"""
    if ch == ord("q"):
        return "quit"
    if ch in (curses.KEY_UP, ord("k")):
        return "up"
    if ch in (curses.KEY_DOWN, ord("j")):
        return "down"
    if ch == curses.KEY_NPAGE:
        return "pgdn"
    if ch == curses.KEY_PPAGE:
        return "pgup"
    if ch == curses.KEY_HOME:
        return "home"
    if ch == curses.KEY_END:
        return "end"
    if ch in (curses.KEY_RIGHT, ord("l")):
        return "expand"
    if ch in (curses.KEY_LEFT, ord("h")):
        return "collapse"
    if ch in _ENTER_KEYS or ch == ord(" "):
        return "enter"
    if ch == ord("E"):
        return "expand_all"
    if ch == ord("C"):
        return "collapse_all"
    if ch == ord("p"):
        return "only_problem"
    if ch == ord("m"):
        return "cycle_mode"
    if ch == ord("s"):
        return "cycle_status"
    if ch == ord("f"):
        return "toggle_flat"
    if ch == ord("o"):  # 小寫循環欄位、大寫切換升降，與 CSV 檢視的 s/S 同慣例
        return "sort_field"
    if ch == ord("O"):
        return "sort_dir"
    if ch == ord("/"):
        return "search"
    if ch == ord("r"):
        return "reload"
    if ch == ord("?"):
        return "help"
    return None


# --- CSV 原始資料檢視的版面（純函式）--------------------------------------
_CSV_TS_COLS = 19
_CSV_LEVEL_COLS = 7
_CSV_PINNED_W = _CSV_TS_COLS + 1 + _CSV_LEVEL_COLS + 1  # 釘住欄佔用的顯示欄數
_CSV_HSTEP = 8  # ←/→ 一次水平捲動的顯示欄數

# 排序：時間在這份 log 天生單調遞增（單執行緒 logger），依時間排等於原序，故不列入循環。
_CSV_SORT_CYCLE = ["原序", "級別", "訊息"]
# 級別依嚴重度而非字母排（字母序會把 ERROR 排在 WARNING 前面純屬巧合）
_LEVEL_RANK = {"CRITICAL": 5, "ERROR": 4, "WARNING": 3, "INFO": 2, "DEBUG": 1}


@dataclass
class CsvRow:
    """CSV 一列的顯示用拆解；ts/level 為釘住欄，message 才隨 hoff 水平捲動。"""

    ts: str
    level: str
    message: str


def _flatten_cell(s: str) -> str:
    """欄位可能含換行/定位字元（例如例外 repr），攤平成單行免得打斷 curses 版面。"""
    return "".join(" " if ch < " " else ch for ch in s)


def csv_rows(rows: list[list[str]]) -> list[CsvRow]:
    """把原始 CSV 列拆成顯示用的 CsvRow。

    device_name / version_info 在同一份 log 的每列都相同（`_CSVFileHandler` 的實例屬性），
    放標題列就好、不佔格線欄位；格線只留 時間｜級別｜訊息。
    """
    out: list[CsvRow] = []
    for row in rows:
        ts, _dev, _ver, level, message = (list(row) + [""] * 5)[:5]
        out.append(
            CsvRow(_flatten_cell(ts), _flatten_cell(level), _flatten_cell(message))
        )
    return out


def csv_line(row: CsvRow, hoff: int, width: int) -> str:
    """組出一列：時間/級別釘住不動，只有訊息欄套用水平位移。

    釘住欄一律 pad 到 _CSV_PINNED_W，訊息因此永遠從同一欄開始——表頭與內文的對齊
    由這個常數保證，不必兩邊各算一次。
    """
    pinned = (
        f"{pad_display(row.ts, _CSV_TS_COLS)} "
        f"{pad_display(row.level, _CSV_LEVEL_COLS)} "
    )
    msg = slice_display(row.message, hoff, max(0, width - _CSV_PINNED_W))
    return fit_display(pinned + msg, width)[0]


def csv_header_line(hoff: int, width: int) -> str:
    """凍結表頭。訊息欄標籤刻意不隨 hoff 捲走，改用 ←N 標示已捲掉幾欄。"""
    label = "訊息" if hoff == 0 else f"訊息 ←{hoff}"
    return csv_line(CsvRow("時間", "級別", label), 0, width)


def csv_sort_rows(rows: list[CsvRow], key: str, desc: bool) -> list[CsvRow]:
    """依欄位排序（穩定）：同鍵值維持原始時間順序，log 的脈絡才不會被打散。"""
    if key == "級別":
        ranked = sorted(rows, key=lambda r: _LEVEL_RANK.get(r.level.strip().upper(), 0),
                        reverse=desc)
        return ranked
    if key == "訊息":
        return sorted(rows, key=lambda r: r.message, reverse=desc)
    return list(reversed(rows)) if desc else list(rows)  # 原序；反轉＝檔尾最新在前


def csv_sort_label(key: str, desc: bool) -> str:
    return f"排序：{key}{'↓' if desc else '↑'}"


@dataclass
class CsvView:
    """CSV 檢視的可變狀態（垂直/水平位移與排序），與繪製分離故可單獨測試。"""

    off: int = 0
    hoff: int = 0
    sort_key: str = _CSV_SORT_CYCLE[0]
    desc: bool = False


def csv_key_action(ch: int) -> str | None:
    """CSV 檢視的按鍵映射（純函式，可測；curses.KEY_* 為模組常數）。"""
    if ch in (ord("q"), ord("Q"), 27):  # 27=Esc：逐層往上回明細
        return "close"
    if ch in (curses.KEY_UP, ord("k")):
        return "up"
    if ch in (curses.KEY_DOWN, ord("j")):
        return "down"
    if ch == curses.KEY_PPAGE:
        return "pgup"
    if ch == curses.KEY_NPAGE:
        return "pgdn"
    if ch == ord("g"):
        return "top"
    if ch == ord("G"):
        return "bottom"
    if ch in (curses.KEY_LEFT, ord("h")):
        return "left"
    if ch in (curses.KEY_RIGHT, ord("l")):
        return "right"
    if ch in (ord("0"), curses.KEY_HOME):
        return "hreset"
    if ch == ord("s"):
        return "sort_col"
    if ch == ord("S"):
        return "sort_dir"
    return None


def csv_apply(view: CsvView, action: str, *, total: int, view_h: int) -> None:
    """就地套用動作（純狀態轉移）；夾回合法範圍交給繪製前的 clamp_* 統一處理。"""
    if action == "up":
        view.off -= 1
    elif action == "down":
        view.off += 1
    elif action == "pgup":
        view.off -= view_h
    elif action == "pgdn":
        view.off += view_h
    elif action == "top":
        view.off = 0
    elif action == "bottom":
        view.off = total
    elif action == "left":
        view.hoff -= _CSV_HSTEP
    elif action == "right":
        view.hoff += _CSV_HSTEP
    elif action == "hreset":
        view.hoff = 0
    elif action == "sort_col":
        i = _CSV_SORT_CYCLE.index(view.sort_key)
        view.sort_key = _CSV_SORT_CYCLE[(i + 1) % len(_CSV_SORT_CYCLE)]
        view.off = 0  # 換排序後原本的位置已無意義
    elif action == "sort_dir":
        view.desc = not view.desc
        view.off = 0


def clamp_scroll(total: int, view_h: int, off: int) -> int:
    """垂直位移夾在 [0, total - view_h]，view_h 至少 1 列。"""
    return max(0, min(off, max(0, total - max(1, view_h))))


def clamp_hscroll(max_width: int, view_w: int, hoff: int) -> int:
    """水平位移夾在 [0, 最寬列顯示寬 - 可視寬]，可視寬至少 1 欄。"""
    return max(0, min(hoff, max(0, max_width - max(1, view_w))))


# ---------------------------------------------------------------------------
# 資料載入
# ---------------------------------------------------------------------------
def load_tree(args, now: datetime, sync_handler=None):
    if getattr(args, "sync_config", None):
        if sync_handler is None:
            # 非 curses 呼叫仍不可讓 main.py 的輸出直接污染目前終端機。
            sync_logs(args.sync_config, quiet=True)
        else:
            sync_handler(args.sync_config)
    records = collect_logs(args.log_dir, mode=args.mode)
    devices = aggregate_by_device(records, now=now, stale_hours=args.stale_hours)
    devices = _apply_filters(devices, args.vessel, args.ipc, args.component, args.status)
    return build_tree(devices)


def write_html_snapshot(args, tree, now: datetime) -> str:
    """`--tui --html`：每輪分析後覆寫同一份報告，回傳要顯示在第二行的字（""＝沒下 --html）。

    TUI 會略過 log_monitor._run_once，HTML 因此得在共用的重載點自己寫一次，否則
    `--tui --html` 會靜默失敗。實際產出交給資料層的 write_html_report，與靜態、
    --watch 走同一份路徑與內容邏輯；這裡只多做 curses 層需要的兩件事：吞掉例外、湊訊息。

    裝置清單從樹走回來＝已套過 --vessel/--ipc/--component/--status，與靜態輸出相同；
    TUI 內的互動過濾（/、m、s、p）純屬畫面，不影響報告，報告永遠是同一份完整快照。
    """
    try:
        target = write_html_report(
            [it.dev for it in tree_devices(tree)],
            now,
            args.log_dir,
            args.stale_hours,
            getattr(args, "html", None),
        )
    except Exception as exc:  # 報告只是副產物，寫不出來不該讓整個 TUI 當掉
        return f"HTML 失敗：{type(exc).__name__}"
    return "" if target is None else f"HTML→{target.name}"


_HELP_LINES = [
    "移動      ↑/↓ 或 k/j、PgUp/PgDn、Home/End",
    "展開收合  Enter/Space 開合群組；→/l 展開、←/h 收合（裝置列 ← 跳父群）",
    "看明細    在裝置列按 Enter；明細再按 Enter 看該筆 CSV 原始資料",
    "CSV檢視   ↑↓/PgUp/PgDn/g/G 捲動、←→ 水平捲動、0 復位、s/S 排序、q/Esc 返回",
    "檢視      f 切換 平坦/分群（平坦＝全船隊一張表，忽略 方向/船/IPC 分群）",
    "排序      o 循環欄位（船隻名稱 / 更新時間 / 嚴重度 / 裝置名稱）、O 切換升降冪",
    "          預設 船隻名稱↓（分群模式下這欄排的是船群，其餘欄位排裝置列）",
    "          更新時間↑ 最久未更新在前（找失聯裝置）",
    "全部      E 全部展開、C 全部收合（僅分群模式看得到效果）",
    "過濾      / 搜尋（Esc 清除）、m 循環方向、s 循環狀態、p 只看異常",
    "其他      r 立即重載、? 說明、q 離開",
]


# ---------------------------------------------------------------------------
# curses 繪製 / 互動（最外層，不進單元測試）
# ---------------------------------------------------------------------------
def _addstr(win, y, x, text, attr=0):
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def _clean_sync_line(line: str) -> str:
    """移除會干擾 curses 游標位置的 ANSI 與控制字元。"""
    line = _ANSI_ESCAPE.sub("", line).replace("\t", "    ")
    return "".join(ch for ch in line if ch >= " ").strip()


def _draw_sync(stdscr, lines) -> None:
    """同步專用畫面：固定只顯示最近幾行，所有輸出皆經 curses 繪製。"""
    stdscr.erase()
    maxy, maxx = stdscr.getmaxyx()
    width = max(0, maxx - 1)
    title = f" 正在同步 fleet_logs…（僅顯示最近 {_SYNC_LINE_LIMIT} 行）"
    _addstr(stdscr, 0, 0, fit_display(title, width)[0], curses.A_BOLD)
    visible_count = min(_SYNC_LINE_LIMIT, max(0, maxy - 2))
    for y, line in enumerate(list(lines)[-visible_count:], start=1):
        _addstr(stdscr, y, 0, fit_display(line, width)[0])
    if maxy > 1:
        _addstr(
            stdscr,
            maxy - 1,
            0,
            pad_display(" 同步完成後自動返回監視畫面", width),
            curses.A_REVERSE,
        )
    stdscr.refresh()


def _sync_with_progress(stdscr, sync_config) -> bool:
    """攔截 main.py 輸出，清理後在 curses 內即時顯示最近幾行。"""
    recent = deque(maxlen=_SYNC_LINE_LIMIT)
    _draw_sync(stdscr, recent)

    def show(line):
        cleaned = _clean_sync_line(line)
        if cleaned:
            recent.append(cleaned)
            _draw_sync(stdscr, recent)

    return sync_logs(
        sync_config,
        quiet=True,
        output_callback=show,
    )


def _attr(status):
    if curses.has_colors():
        return curses.color_pair(_PAIR.get(status, 0))
    return 0


def _put(win, y, x, text, attr, limit):
    """在 (y,x) 寫入 text，截斷至剩餘顯示寬度 limit-x；回傳新的 x（顯示欄）。"""
    fs, w = fit_display(text, limit - x)
    if w > 0:
        _addstr(win, y, x, fs, attr)
    return x + w


def body_height(maxy: int, state: TuiState) -> int:
    """可視資料列數：2 行表頭 + 1 行底部提示，平坦模式再多 1 行凍結欄名。"""
    return max(1, maxy - (4 if state.flat else 3))


def footer_hint(state: TuiState) -> str:
    """底部提示（純函式）：平坦模式沒有群組，展開收合的提示換成排序。"""
    if state.flat:
        return (" ↑↓移動  Enter明細  f分群  o欄位/O升降  /搜尋  m方向  s狀態"
                "  p異常  r重載  ?說明  q離開")
    return (" ↑↓移動  Enter開合/明細  ←→收展  E/C全展收  f平坦  o/O排序"
            "  /搜尋  m方向  s狀態  p異常  r重載  ?說明  q離開")


def _draw(stdscr, state: TuiState, rows: list[Row], tree, watch: float) -> None:
    stdscr.erase()
    maxy, maxx = stdscr.getmaxyx()
    width = maxx - 1
    t, o, s, b = global_counts(tree)
    now_s = state.now.strftime(TS_FMT) if state.now else "—"
    head1 = f" SFTP Log 監視 {now_s}  裝置 {t}｜正常 {o}｜過期 {s}｜異常 {b}"
    filt = []
    if state.mode:
        filt.append("方向=" + _MODE_LABEL.get(state.mode, state.mode))
    if state.status != "all":
        filt.append("狀態=" + state.status)
    if state.only_problem:
        filt.append("只看異常")
    if state.query:
        filt.append(f"搜尋='{state.query}'")
    if watch:
        filt.append(f"每{int(watch)}s刷新")
    head2 = (f" 檢視: {'平坦' if state.flat else '分群'}｜{sort_label(state.sort_key, state.sort_desc)}"
             + (f"｜{state.html_note}" if state.html_note else "")
             + "  過濾: " + ("、".join(filt) if filt else "（無）"))
    _addstr(stdscr, 0, 0, fit_display(head1, width)[0], curses.A_BOLD)
    _addstr(stdscr, 1, 0, fit_display(head2, width)[0], curses.A_DIM)
    _addstr(stdscr, maxy - 1, 0, pad_display(footer_hint(state), width), curses.A_REVERSE)

    top, height = 2, body_height(maxy, state)
    if state.flat and maxy > 3:  # 平坦模式是表格，補一行凍結欄名
        _addstr(stdscr, 2, 0, fit_display(flat_header_line(), width)[0], curses.A_UNDERLINE)
        top = 3
    if height < 1:
        stdscr.refresh()
        return
    idx = selected_index(rows, state)
    if idx < state.scroll:
        state.scroll = idx
    elif idx >= state.scroll + height:
        state.scroll = idx - height + 1
    if state.scroll < 0:
        state.scroll = 0

    if not rows:
        _addstr(stdscr, top, 0, "（無符合資料）")
    for i in range(height):
        ridx = state.scroll + i
        if ridx >= len(rows):
            break
        r = rows[ridx]
        y = top + i
        indent = "  " * r.depth
        if r.kind == "device":
            prefix = indent + "  "
        else:
            prefix = indent + ("▼" if r.key in state.expanded else "▶") + " "
        sel = ridx == idx
        base = curses.A_REVERSE if sel else 0
        if sel:  # 先鋪整列反白底，選取列橫跨整行
            _addstr(stdscr, y, 0, " " * width, curses.A_REVERSE)
        # 依顯示寬度逐段寫入，避免 CJK 造成位移或溢出換行
        x = _put(stdscr, y, 0, prefix, base, width)
        x = _put(stdscr, y, x, "●", base if sel else _attr(r.status), width)
        _put(stdscr, y, x, " " + r.text, base, width)
    stdscr.refresh()


def _popup(stdscr, lines, title="明細", hint=" 任意鍵關閉 "):
    """置中彈窗；回傳關閉它的按鍵，讓呼叫端能據此再往下鑽一層。"""
    maxy, maxx = stdscr.getmaxyx()
    body = lines or ["（無明細）"]
    # hint 也要參與寬度計算：否則明細短、提示長時提示會被切掉
    w = min(
        maxx - 2,
        max([disp_width(title) + 6, disp_width(hint) + 4]
            + [disp_width(x) for x in body]) + 4,
    )
    h = min(maxy - 2, len(body) + 4)
    win = curses.newwin(h, w, max(0, (maxy - h) // 2), max(0, (maxx - w) // 2))
    win.box()
    _addstr(win, 0, 2, f" {title} ", curses.A_BOLD)
    for i, ln in enumerate(body[: h - 4]):
        _addstr(win, 2 + i, 2, fit_display(ln, w - 4)[0])
    _addstr(win, h - 1, 2, fit_display(hint, w - 4)[0], curses.A_DIM)
    win.refresh()
    return win.getch()


def _level_attr(level: str):
    """CSV 原始列著色：ERROR 紅、WARNING 黃，沿用主畫面既有色對。"""
    if not curses.has_colors():
        return 0
    if level == "ERROR":
        return curses.color_pair(3)
    if level == "WARNING":
        return curses.color_pair(2)
    return 0


def _csv_viewer(stdscr, rec, watch) -> None:
    """全畫面檢視該筆 log 的 CSV 原始資料（版型仿 STREAM_manager.py 的 _log_viewer）。

    畫在 stdscr 上而非 newwin：curses.wrapper 只對 stdscr 開 keypad(1)，
    子視窗收不到 curses.KEY_*，←/→、PgUp/PgDn 會失效。
    """
    raw, truncated = read_log_rows(rec.path)
    source = csv_rows(raw)
    if not source:
        source = [CsvRow("—", "", f"（無法讀取或無資料：{rec.path}）")]
    max_msg_w = max(disp_width(r.message) for r in source)
    dev_name = raw[0][1] if raw else rec.device_name
    version = raw[0][2] if raw else ""

    view = CsvView()
    rows = source
    stdscr.timeout(-1)  # 模態期間阻塞讀鍵，不被 --watch 的 1 秒輪詢打斷
    try:
        while True:
            maxy, maxx = stdscr.getmaxyx()  # 每幀重讀 → 改視窗大小自動重排
            width = max(1, maxx - 1)
            view_h = max(1, maxy - 3)
            msg_w = max(0, width - _CSV_PINNED_W)
            view.off = clamp_scroll(len(rows), view_h, view.off)
            view.hoff = clamp_hscroll(max_msg_w, msg_w, view.hoff)

            stdscr.erase()
            title = f" {rec.path.name}｜{dev_name}"
            if version:
                title += f"｜{version}"
            title += f"｜共 {len(rows)} 筆"
            if truncated:
                title += "（已截斷）"
            title += f"｜{csv_sort_label(view.sort_key, view.desc)}"
            _addstr(stdscr, 0, 0, pad_display(title, width),
                    curses.A_REVERSE | curses.A_BOLD)
            _addstr(stdscr, 1, 0, csv_header_line(view.hoff, width), curses.A_UNDERLINE)
            for i in range(view_h):
                idx = view.off + i
                if idx >= len(rows):
                    break
                row = rows[idx]
                _addstr(stdscr, 2 + i, 0, csv_line(row, view.hoff, width),
                        _level_attr(row.level))
            shown = f"{view.off + 1}-{min(view.off + view_h, len(rows))}/{len(rows)}"
            foot = (f" ↑↓捲動  ←→水平  PgUp/PgDn翻頁  g/G首末  0復位  s欄位/S升降"
                    f"  q/Esc返回明細   {shown}")
            _addstr(stdscr, maxy - 1, 0, pad_display(foot, width), curses.A_REVERSE)
            stdscr.refresh()

            act = csv_key_action(stdscr.getch())
            if act == "close":
                return
            if act:
                before = (view.sort_key, view.desc)
                csv_apply(view, act, total=len(rows), view_h=view_h)
                if (view.sort_key, view.desc) != before:
                    rows = csv_sort_rows(source, view.sort_key, view.desc)
    finally:
        stdscr.timeout(1000 if watch else -1)  # 還原主迴圈的讀鍵設定


def _device_drilldown(stdscr, dev, watch, repaint) -> None:
    """明細 ↔ CSV 原始資料：明細按 Enter 再往下鑽一層，其他鍵關閉回列表。

    每輪先 repaint()：CSV 檢視是畫滿 stdscr 的，較小的明細彈窗蓋不掉它，
    不先把主畫面重畫回來，第二次看明細就會疊在 CSV 殘影上。

    只認真正的 Enter（不含 Space）：Space 在主列表是「開合/開明細」，但在彈窗裡
    更像「關掉」的直覺，不該讓使用者想關卻反而更深入一層。
    """
    while True:
        repaint()
        ch = _popup(
            stdscr,
            device_detail_lines(dev),
            hint=" Enter 看 CSV 原始資料｜其他鍵關閉 ",
        )
        if ch not in _ENTER_KEYS:
            return
        _csv_viewer(stdscr, dev.latest, watch)


def _prompt_search(stdscr, state: TuiState):
    maxy, maxx = stdscr.getmaxyx()
    buf = list(state.query)
    curses.curs_set(1)
    while True:
        text = "".join(buf)
        _addstr(stdscr, maxy - 1, 0, (" 搜尋: " + text).ljust(maxx - 1)[: maxx - 1],
                curses.A_REVERSE)
        try:
            stdscr.move(maxy - 1, min(7 + len(text), maxx - 2))
        except curses.error:
            pass
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            break
        if ch == 27:  # Esc：清除搜尋
            buf = []
            break
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if buf:
                buf.pop()
        elif 32 <= ch <= 126:
            buf.append(chr(ch))
    curses.curs_set(0)
    set_query(state, "".join(buf))


def _main_loop(stdscr, args):
    import time

    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_GREEN, bg)
        curses.init_pair(2, curses.COLOR_YELLOW, bg)
        curses.init_pair(3, curses.COLOR_RED, bg)

    # --flat 既有語意就是「不分群的平面表格」，搭配 --tui 直接以平坦模式啟動
    state = TuiState(flat=bool(getattr(args, "flat", False)))
    watch = args.watch or 0

    def reload():
        now = datetime.now()
        tree = load_tree(
            args,
            now,
            sync_handler=lambda config: _sync_with_progress(stdscr, config),
        )
        seed_expanded(tree, state)
        state.now = now
        # reload 是唯一的重載點：首輪啟動、r 手動重載、--watch 自動刷新都經過這裡，
        # 掛在這上面 --html 才三種情況全涵蓋，與 --watch 儀表板的行為一致。
        state.html_note = write_html_snapshot(args, tree, now)
        return tree

    tree = reload()
    stdscr.timeout(1000 if watch else -1)
    last = time.monotonic()

    while True:
        rows = visible_rows(tree, state, state.now)
        clamp_selection(rows, state)
        _draw(stdscr, state, rows, tree, watch)
        ch = stdscr.getch()
        if ch == -1:
            if watch and (time.monotonic() - last) >= watch:
                tree = reload()
                last = time.monotonic()
            continue
        act = key_action(ch)
        if act == "quit":
            break
        elif act == "up":
            move_selection(rows, state, -1)
        elif act == "down":
            move_selection(rows, state, 1)
        elif act == "pgdn":
            move_selection(rows, state, body_height(stdscr.getmaxyx()[0], state))
        elif act == "pgup":
            move_selection(rows, state, -body_height(stdscr.getmaxyx()[0], state))
        elif act == "home" and rows:
            state.sel_key = rows[0].key
        elif act == "end" and rows:
            state.sel_key = rows[-1].key
        elif act in ("expand", "collapse", "enter"):
            idx = selected_index(rows, state)
            r = rows[idx] if rows else None
            if r is None:
                pass
            elif act == "expand":
                if r.kind != "device":
                    state.expanded.add(r.key)
            elif act == "collapse":
                collapse_or_parent(state, r)
            else:  # enter
                if r.kind == "device":
                    _device_drilldown(
                        stdscr, r.ref, watch,
                        repaint=lambda: _draw(stdscr, state, rows, tree, watch),
                    )
                else:
                    toggle(state, r.key)
        elif act == "expand_all":
            expand_all(state, tree)
        elif act == "collapse_all":
            collapse_all(state)
        elif act == "toggle_flat":
            toggle_flat(state)
        elif act == "sort_field":
            cycle_sort(state)
        elif act == "sort_dir":
            toggle_sort_dir(state)
        elif act == "only_problem":
            toggle_problem(state)
        elif act == "cycle_mode":
            cycle_mode(state)
        elif act == "cycle_status":
            cycle_status(state)
        elif act == "search":
            _prompt_search(stdscr, state)
        elif act == "reload":
            tree = reload()
            last = time.monotonic()
        elif act == "help":
            _popup(stdscr, _HELP_LINES, "說明")


def run_app(args) -> int:
    """由 log_monitor.main（--tui、TTY）呼叫；包在 curses.wrapper 內。"""
    try:
        curses.wrapper(_main_loop, args)
    except KeyboardInterrupt:
        pass
    return 0
