#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""以 curses 掃描 config/，讓操作者勾選本次要執行的 SFTP 專案。

預設同時列出 ``*_download_settings.json`` 與
``*_upload_settings.json``；真正傳輸時仍沿用 main.py 的 CLI 流程。
方向採雙向守門：CLINK 發佈端只可上傳，其餘部署端只可下載。
"""
from __future__ import annotations

import argparse
import curses
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from run_all_downloads import is_dev_machine


BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
MAIN_SCRIPT = BASE_DIR / "main.py"
MODE_LABEL = {"download": "下載", "upload": "上傳"}
MODE_ORDER = {"download": 0, "upload": 1}
FILTER_CYCLE = ("all", "download", "upload")


@dataclass(frozen=True)
class TransferItem:
    path: Path
    mode: str
    project: str


@dataclass
class SelectionState:
    selected: set[Path] = field(default_factory=set)
    index: int = 0
    scroll: int = 0
    mode_filter: str = "all"
    message: str = ""


def project_from_name(name: str, mode: str) -> str:
    suffix = f"_{mode}_settings.json"
    return name[: -len(suffix)] if name.endswith(suffix) else name


def scan_setting_files(config_dir: Path, mode: str = "all") -> list[TransferItem]:
    """依 run_all_* 的命名規則掃描設定檔，不讀取其中的連線密碼。"""
    modes = ("download", "upload") if mode == "all" else (mode,)
    items = [
        TransferItem(path, item_mode, project_from_name(path.name, item_mode))
        for item_mode in modes
        for path in config_dir.glob(f"*_{item_mode}_settings.json")
        if path.is_file()
    ]
    return sorted(items, key=lambda item: (item.project.casefold(), MODE_ORDER[item.mode], item.path.name))


def visible_items(items: list[TransferItem], mode_filter: str) -> list[TransferItem]:
    if mode_filter == "all":
        return items
    return [item for item in items if item.mode == mode_filter]


def locked_mode_for_role(dev_machine: bool) -> str:
    """CLINK 鎖下載；其餘（含無法辨識角色）鎖上傳，失效方向安全。"""
    return "download" if dev_machine else "upload"


def policy_message(locked_mode: str) -> str:
    if locked_mode == "download":
        return "CLINK 發佈端禁止下載，只允許上傳。"
    return "部署端禁止上傳，只允許下載，避免舊程式回灌 OTA。"


def is_selectable(item: TransferItem, locked_mode: str) -> bool:
    return item.mode != locked_mode


def toggle_item(state: SelectionState, item: TransferItem, locked_mode: str) -> None:
    if not is_selectable(item, locked_mode):
        state.message = policy_message(locked_mode)
        return
    if item.path in state.selected:
        state.selected.remove(item.path)
    else:
        state.selected.add(item.path)
    state.message = ""


def toggle_all_visible(
    state: SelectionState,
    items: list[TransferItem],
    locked_mode: str,
) -> None:
    paths = {item.path for item in items if is_selectable(item, locked_mode)}
    if paths and paths <= state.selected:
        state.selected.difference_update(paths)
    else:
        state.selected.update(paths)
    state.message = ""


def clamp_state(state: SelectionState, rows: list[TransferItem]) -> None:
    if not rows:
        state.index = 0
        state.scroll = 0
        return
    state.index = max(0, min(state.index, len(rows) - 1))
    state.scroll = max(0, min(state.scroll, state.index))


def key_action(ch: int) -> str | None:
    if ch in (ord("q"), 27):
        return "quit"
    if ch in (curses.KEY_UP, ord("k")):
        return "up"
    if ch in (curses.KEY_DOWN, ord("j")):
        return "down"
    if ch == curses.KEY_PPAGE:
        return "pgup"
    if ch == curses.KEY_NPAGE:
        return "pgdn"
    if ch == curses.KEY_HOME:
        return "home"
    if ch == curses.KEY_END:
        return "end"
    if ch == ord(" "):
        return "toggle"
    if ch == ord("a"):
        return "all"
    if ch == ord("x"):
        return "clear"
    if ch == ord("m"):
        return "mode"
    if ch == ord("r"):
        return "reload"
    if ch in (10, 13, curses.KEY_ENTER):
        return "run"
    return None


def _char_width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def fit_display(text: str, cols: int) -> str:
    if cols <= 0:
        return ""
    out: list[str] = []
    width = 0
    for char in text:
        char_width = _char_width(char)
        if width + char_width > cols:
            break
        out.append(char)
        width += char_width
    return "".join(out)


def _addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def _counts(items: list[TransferItem], selected: set[Path]) -> tuple[int, int]:
    downloads = sum(item.mode == "download" and item.path in selected for item in items)
    uploads = sum(item.mode == "upload" and item.path in selected for item in items)
    return downloads, uploads


def _draw(
    stdscr,
    state: SelectionState,
    all_items: list[TransferItem],
    rows: list[TransferItem],
    locked_mode: str,
) -> None:
    stdscr.erase()
    maxy, maxx = stdscr.getmaxyx()
    width = max(0, maxx - 1)
    if maxy < 8 or maxx < 48:
        _addstr(stdscr, 0, 0, fit_display("終端機太小，請放大到至少 48x8；q 離開。", width), curses.A_BOLD)
        stdscr.refresh()
        return

    selected_downloads, selected_uploads = _counts(all_items, state.selected)
    filter_label = {"all": "全部", **MODE_LABEL}[state.mode_filter]
    head = (
        f" SFTP 專案選擇｜掃描 {len(all_items)} 份｜"
        f"已選 下載 {selected_downloads}、上傳 {selected_uploads}｜顯示 {filter_label}"
    )
    _addstr(stdscr, 0, 0, fit_display(head, width), curses.A_BOLD)
    warning = " " + policy_message(locked_mode)
    warning_attr = curses.A_BOLD
    if curses.has_colors():
        warning_attr |= curses.color_pair(3)
    _addstr(stdscr, 1, 0, fit_display(warning, width), warning_attr)

    body_top = 3
    body_height = maxy - body_top - 2
    if state.index < state.scroll:
        state.scroll = state.index
    elif state.index >= state.scroll + body_height:
        state.scroll = state.index - body_height + 1

    if not rows:
        _addstr(stdscr, body_top, 1, "（目前篩選沒有設定檔）", curses.A_DIM)
    for line_index in range(body_height):
        row_index = state.scroll + line_index
        if row_index >= len(rows):
            break
        item = rows[row_index]
        locked = not is_selectable(item, locked_mode)
        checked = item.path in state.selected
        mark = "[-]" if locked else ("[x]" if checked else "[ ]")
        mode = f"[{MODE_LABEL[item.mode]}]"
        line = f" {mark} {mode:<4} {item.project:<30} {item.path.name}"
        selected_row = row_index == state.index
        attr = curses.A_REVERSE if selected_row else 0
        if locked and not selected_row:
            attr |= curses.A_DIM
        elif checked and not selected_row and curses.has_colors():
            attr |= curses.color_pair(1)
        _addstr(stdscr, body_top + line_index, 0, fit_display(line, width), attr)

    message = state.message or "↑↓/j/k 移動  Space 勾選  a 全選目前  x 清除  m 篩選  r 重掃  Enter 執行  q 離開"
    _addstr(stdscr, maxy - 1, 0, fit_display((" " + message).ljust(width), width), curses.A_REVERSE)
    stdscr.refresh()


def _confirm(stdscr, selected: list[TransferItem]) -> bool:
    downloads, uploads = _counts(selected, {item.path for item in selected})
    while True:
        stdscr.erase()
        maxy, maxx = stdscr.getmaxyx()
        width = max(0, maxx - 1)
        _addstr(
            stdscr,
            0,
            0,
            fit_display(f" 即將依序執行 {len(selected)} 個專案（下載 {downloads}、上傳 {uploads}）", width),
            curses.A_BOLD,
        )
        available = max(0, maxy - 4)
        for index, item in enumerate(selected[:available], 1):
            _addstr(
                stdscr,
                index,
                1,
                fit_display(f"{index:>2}. [{MODE_LABEL[item.mode]}] {item.project}", max(0, width - 1)),
            )
        if len(selected) > available and maxy > 3:
            _addstr(stdscr, maxy - 3, 1, f"……另有 {len(selected) - available} 項")
        _addstr(stdscr, maxy - 1, 0, fit_display(" 確定開始？y 執行；n/Esc 返回選單", width), curses.A_REVERSE)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), 27, ord("q")):
            return False


def _main_loop(stdscr, config_dir: Path, scan_mode: str, locked_mode: str):
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_GREEN, background)
        curses.init_pair(3, curses.COLOR_RED, background)

    state = SelectionState(mode_filter=scan_mode)
    items = scan_setting_files(config_dir, scan_mode)
    while True:
        rows = visible_items(items, state.mode_filter)
        clamp_state(state, rows)
        _draw(stdscr, state, items, rows, locked_mode)
        action = key_action(stdscr.getch())
        page = max(1, stdscr.getmaxyx()[0] - 5)
        if action == "quit":
            return None
        if action == "up":
            state.index -= 1
        elif action == "down":
            state.index += 1
        elif action == "pgup":
            state.index -= page
        elif action == "pgdn":
            state.index += page
        elif action == "home":
            state.index = 0
        elif action == "end":
            state.index = max(0, len(rows) - 1)
        elif action == "toggle" and rows:
            toggle_item(state, rows[state.index], locked_mode)
        elif action == "all":
            toggle_all_visible(state, rows, locked_mode)
        elif action == "clear":
            state.selected.clear()
            state.message = "已清除全部勾選。"
        elif action == "mode" and scan_mode == "all":
            current = FILTER_CYCLE.index(state.mode_filter)
            state.mode_filter = FILTER_CYCLE[(current + 1) % len(FILTER_CYCLE)]
            state.index = 0
            state.scroll = 0
        elif action == "reload":
            items = scan_setting_files(config_dir, scan_mode)
            existing = {item.path for item in items}
            state.selected.intersection_update(existing)
            state.message = f"已重新掃描，共 {len(items)} 份設定檔。"
        elif action == "run":
            selected = [item for item in items if item.path in state.selected]
            if not selected:
                state.message = "尚未選擇任何專案。"
            elif _confirm(stdscr, selected):
                return selected
        clamp_state(state, rows)


def execute_selected(items: list[TransferItem], locked_mode: str) -> int:
    forbidden = [item for item in items if not is_selectable(item, locked_mode)]
    if forbidden:
        print(f"錯誤：{policy_message(locked_mode)}", file=sys.stderr)
        for item in forbidden:
            print(f"  [已阻擋] [{MODE_LABEL[item.mode]}] {item.path.name}", file=sys.stderr)
        return 3

    results: list[tuple[TransferItem, int]] = []
    total = len(items)
    for index, item in enumerate(items, 1):
        print(f"\n===== [{index}/{total}] 開始{MODE_LABEL[item.mode]}：{item.path.name} =====", flush=True)
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(MAIN_SCRIPT),
                    "--cli",
                    "--mode",
                    item.mode,
                    "--config",
                    str(item.path),
                ],
                cwd=str(BASE_DIR),
            )
            returncode = proc.returncode
        except KeyboardInterrupt:
            print("\n使用者中止，後續專案不再執行。", file=sys.stderr)
            return 130
        results.append((item, returncode))
        status = "完成" if returncode == 0 else f"失敗 (rc={returncode})"
        print(f"===== [{index}/{total}] {item.path.name} {status} =====", flush=True)

    print("\n========== 本次傳輸結果彙總 ==========")
    for item, returncode in results:
        status = "成功" if returncode == 0 else f"失敗 rc={returncode}"
        print(f"  [{'成功' if returncode == 0 else '失敗'}] [{MODE_LABEL[item.mode]}] {item.project} ({status})")
    failed = sum(returncode != 0 for _, returncode in results)
    print(f"共 {total} 個專案，成功 {total - failed}，失敗 {failed}")
    return 0 if failed == 0 else 1


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="互動勾選本次要下載／上傳的 SFTP 專案")
    parser.add_argument("--config-dir", default=str(CONFIG_DIR), help="設定檔資料夾（預設 ./config）")
    parser.add_argument(
        "--mode",
        choices=FILTER_CYCLE,
        default="all",
        help="只掃描指定方向（預設 all）",
    )
    parser.add_argument("--list", action="store_true", help="只列出掃描結果，不啟動 curses 或傳輸")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_dir = Path(args.config_dir).expanduser()
    items = scan_setting_files(config_dir, args.mode)
    if not items:
        print(f"錯誤：{config_dir} 內找不到符合方向的 *_settings.json", file=sys.stderr)
        return 1
    locked_mode = locked_mode_for_role(is_dev_machine())
    if args.list:
        print(f"規則：{policy_message(locked_mode)}")
        for item in items:
            status = "locked" if item.mode == locked_mode else "enabled"
            print(f"{item.mode:<8} {status:<7} {item.project:<30} {item.path.name}")
        return 0
    if not MAIN_SCRIPT.is_file():
        print(f"錯誤：找不到傳輸入口 {MAIN_SCRIPT}", file=sys.stderr)
        return 1
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("錯誤：互動選單需要 TTY；可先用 --list 檢查掃描結果。", file=sys.stderr)
        return 2

    try:
        selected = curses.wrapper(_main_loop, config_dir, args.mode, locked_mode)
    except KeyboardInterrupt:
        selected = None
    if selected is None:
        print("已取消，沒有執行任何傳輸。")
        return 0
    return execute_selected(selected, locked_mode)


if __name__ == "__main__":
    sys.exit(main())
