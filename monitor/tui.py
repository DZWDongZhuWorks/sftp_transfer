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
from dataclasses import dataclass, field
from datetime import datetime

from monitor.log_monitor import (
    TS_FMT,
    _apply_filters,
    _counts_str,
    _detail_str,
    _humanize_age,
    _MODE_LABEL,
    aggregate_by_device,
    build_tree,
    collect_logs,
    device_detail_lines,
    group_is_problem,
    sync_logs,
)

_MODE_CYCLE = ["", "download", "upload"]
_STATUS_CYCLE = ["all", "ok", "stale", "problem"]
_PAIR = {"success": 1, "stale": 2, "incomplete": 2, "partial": 3, "aborted": 3}


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
    return "{comp:<20} {last:<17} {files:>5} {counts:>9} {age:<9} {detail}".format(
        comp=d.component[:20],
        last=rec.started_at.strftime(TS_FMT) if rec.started_at else "—",
        files=("—" if rec.file_count is None else str(rec.file_count)),
        counts=_counts_str(rec),
        age=_humanize_age(d.last_seen, now) if now else "—",
        detail=_detail_str(d)[:40],
    )


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
        for v in m.vessels:
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
                for d in dvs:
                    dkey = ("D", m.mode, v.name, ip.name, d.component)
                    rows.append(Row("device", 3, dkey, _device_line(d, now), d.display_status, d))
    return rows


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
    if ch in (10, 13, curses.KEY_ENTER, ord(" ")):
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
    if ch == ord("/"):
        return "search"
    if ch == ord("r"):
        return "reload"
    if ch == ord("?"):
        return "help"
    return None


# ---------------------------------------------------------------------------
# 資料載入
# ---------------------------------------------------------------------------
def load_tree(args, now: datetime):
    if getattr(args, "sync_config", None):
        sync_logs(args.sync_config)
    records = collect_logs(args.log_dir, mode=args.mode)
    devices = aggregate_by_device(records, now=now, stale_hours=args.stale_hours)
    devices = _apply_filters(devices, args.vessel, args.ipc, args.component, args.status)
    return build_tree(devices)


_HELP_LINES = [
    "移動      ↑/↓ 或 k/j、PgUp/PgDn、Home/End",
    "展開收合  Enter/Space 開合群組；→/l 展開、←/h 收合（裝置列 ← 跳父群）",
    "看明細    在裝置列按 Enter",
    "全部      E 全部展開、C 全部收合",
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


def _attr(status):
    if curses.has_colors():
        return curses.color_pair(_PAIR.get(status, 0))
    return 0


def _draw(stdscr, state: TuiState, rows: list[Row], tree, watch: float) -> None:
    stdscr.erase()
    maxy, maxx = stdscr.getmaxyx()
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
    head2 = " 過濾: " + ("、".join(filt) if filt else "（無）")
    _addstr(stdscr, 0, 0, head1[: maxx - 1], curses.A_BOLD)
    _addstr(stdscr, 1, 0, head2[: maxx - 1], curses.A_DIM)
    foot = (" ↑↓移動  Enter開合/明細  ←→收展  E/C全展收  /搜尋  m方向  s狀態"
            "  p異常  r重載  ?說明  q離開")
    _addstr(stdscr, maxy - 1, 0, foot[: maxx - 1].ljust(maxx - 1), curses.A_REVERSE)

    top, height = 2, maxy - 3
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
        line = (prefix + "● " + r.text)[: maxx - 1]
        sel = ridx == idx
        _addstr(stdscr, y, 0, line.ljust(maxx - 1)[: maxx - 1],
                curses.A_REVERSE if sel else 0)
        if not sel:
            _addstr(stdscr, y, len(prefix), "●", _attr(r.status))
    stdscr.refresh()


def _popup(stdscr, lines, title="明細"):
    maxy, maxx = stdscr.getmaxyx()
    body = lines or ["（無明細）"]
    w = min(maxx - 2, max([len(title) + 6] + [len(x) for x in body]) + 4)
    h = min(maxy - 2, len(body) + 4)
    win = curses.newwin(h, w, max(0, (maxy - h) // 2), max(0, (maxx - w) // 2))
    win.box()
    _addstr(win, 0, 2, f" {title} ", curses.A_BOLD)
    for i, ln in enumerate(body[: h - 4]):
        _addstr(win, 2 + i, 2, ln[: w - 4])
    _addstr(win, h - 1, 2, " 任意鍵關閉 ", curses.A_DIM)
    win.refresh()
    win.getch()


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

    state = TuiState()
    watch = args.watch or 0

    def reload():
        now = datetime.now()
        tree = load_tree(args, now)
        seed_expanded(tree, state)
        state.now = now
        return tree

    tree = reload()
    stdscr.timeout(1000 if watch else -1)
    last = time.monotonic()

    while True:
        rows = flatten_tree(tree, state, state.now)
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
            move_selection(rows, state, max(1, stdscr.getmaxyx()[0] - 4))
        elif act == "pgup":
            move_selection(rows, state, -max(1, stdscr.getmaxyx()[0] - 4))
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
                if r.kind != "device" and r.key in state.expanded:
                    state.expanded.discard(r.key)
                else:
                    pk = parent_key(r.key)
                    if pk:
                        state.sel_key = pk
            else:  # enter
                if r.kind == "device":
                    _popup(stdscr, device_detail_lines(r.ref))
                else:
                    toggle(state, r.key)
        elif act == "expand_all":
            expand_all(state, tree)
        elif act == "collapse_all":
            collapse_all(state)
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
