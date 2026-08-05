#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""以 curses 掃描 config/，讓操作者勾選本次要執行的 SFTP 專案。

預設同時列出 ``*_download_settings.json`` 與
``*_upload_settings.json``；真正傳輸時仍沿用 main.py 的 CLI 流程。
方向採雙向守門：CLINK 發佈端只可上傳，其餘部署端只可下載。
設定檔可用 ``trans_type: telemetry`` 宣告自己是船到岸的資料回傳流，
不受上述方向鎖管制（見 ``read_trans_type``）。
"""
from __future__ import annotations

import argparse
import curses
import json
import re
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
TRANSFER_SUMMARY_RE = re.compile(r"任務結束(?:（\d+ 組）)?：成功 (\d+)，略過 (\d+)，失敗 (\d+)")

# 流類別：deploy＝程式／設定發佈流（岸→船），telemetry＝資料回傳流（船→岸）。
DEPLOY = "deploy"
TELEMETRY = "telemetry"
# OTA 發佈樹的根。上傳到這些路徑底下的內容會被船隻拉走，是「舊程式回灌」的唯一途徑。
PUBLISH_ROOTS = ("/fleet/wanhai_nssms_deploy/STANDARD", "/fleet/wanhai_nssms_deploy/UNIQUE")


@dataclass(frozen=True)
class TransferItem:
    path: Path
    mode: str
    project: str
    # 預設值不可省略：既有呼叫端與測試以位置參數建構 TransferItem。
    trans_type: str = DEPLOY


@dataclass
class SelectionState:
    selected: set[Path] = field(default_factory=set)
    index: int = 0
    scroll: int = 0
    mode_filter: str = "all"
    message: str = ""


@dataclass(frozen=True)
class TransferResult:
    item: TransferItem
    returncode: int
    success: int
    skipped: int
    failed: int


def _run_transfer(item: TransferItem) -> tuple[int, tuple[int, int, int] | None]:
    """執行單項傳輸、即時轉送輸出，並擷取核心程式印出的檔案統計。"""
    proc = subprocess.Popen(
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
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    counts = None
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        match = TRANSFER_SUMMARY_RE.search(line)
        if match:
            counts = tuple(map(int, match.groups()))
    return proc.wait(), counts


def project_from_name(name: str, mode: str) -> str:
    suffix = f"_{mode}_settings.json"
    return name[: -len(suffix)] if name.endswith(suffix) else name


def _targets_publish_tree(remote_path) -> bool:
    """remote_path（字串或字串陣列）是否有任何一條落在 OTA 發佈樹底下。

    比對未展開佔位符的原始字串即可：PUBLISH_ROOTS 的前綴都在任何 {} 之前。
    """
    paths = remote_path if isinstance(remote_path, list) else [remote_path]
    return any(
        isinstance(p, str) and p.startswith(PUBLISH_ROOTS)
        for p in paths
    )


def read_trans_type(path: Path, mode: str) -> str:
    """讀出設定檔宣告的流類別；任何不確定的情況一律回 DEPLOY（fail-closed）。

    刻意不用 settings.load_settings()：它會做佔位符解析，在沒有 vessel_basic_info.json
    的機器上會對含 {vsl_name} 的設定檔拋 PlaceholderError，讓整個選單開不起來。
    這裡只要兩個欄位的字面值，直接讀原始 JSON 即可，也不會碰到連線憑證。

    標籤只能讓守門更嚴、不能更鬆：宣告 telemetry 的上傳若指向 OTA 發佈樹，
    視為標錯而降回 deploy——設定檔名彼此相似，複製貼上標錯的代價不該是打開回灌的門。

    下載側刻意不做等價驗證：上傳誤放行會污染發佈樹、影響整個船隊且不可回復；
    下載誤放行只影響本機工作區，git 可還原。要在下載側做等價檢查得逐檔跑
    git check-ignore，成本與收益不成比例。這是權衡後的取捨，不是漏掉。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        declared = data.get("trans_type")
    except (OSError, json.JSONDecodeError, AttributeError):
        return DEPLOY
    if declared != TELEMETRY:
        return DEPLOY
    if mode == "upload" and _targets_publish_tree(data.get("remote_path", "")):
        return DEPLOY
    return TELEMETRY


def scan_setting_files(config_dir: Path, mode: str = "all") -> list[TransferItem]:
    """依 run_all_* 的命名規則掃描設定檔。

    只讀取 trans_type 與 remote_path 兩個欄位判斷流類別，不觸碰其中的連線憑證。
    """
    modes = ("download", "upload") if mode == "all" else (mode,)
    items = [
        TransferItem(
            path,
            item_mode,
            project_from_name(path.name, item_mode),
            read_trans_type(path, item_mode),
        )
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
        return "CLINK 發佈端禁止下載，只允許上傳。（回傳類專案不受此限）"
    return "部署端禁止上傳，只允許下載，避免舊程式回灌 OTA。（回傳類專案不受此限）"


def is_selectable(item: TransferItem, locked_mode: str) -> bool:
    """回傳流兩個方向都碰不到方向鎖要保護的東西（開發工作區、OTA 發佈樹），故不受管制。"""
    if item.trans_type == TELEMETRY:
        return True
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
        # 標出回傳類，讓操作者看得出某筆為何在鎖定方向仍可勾選；空白版與 [回傳] 顯示等寬。
        tag = "[回傳]" if item.trans_type == TELEMETRY else "      "
        line = f" {mark} {mode} {tag} {item.project:<30} {item.path.name}"
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

    results: list[TransferResult] = []
    total = len(items)
    for index, item in enumerate(items, 1):
        print(f"\n===== [{index}/{total}] 開始{MODE_LABEL[item.mode]}：{item.path.name} =====", flush=True)
        try:
            returncode, counts = _run_transfer(item)
        except KeyboardInterrupt:
            print("\n使用者中止，後續專案不再執行。", file=sys.stderr)
            return 130
        # 設定或連線若在核心統計產生前失敗，仍以一筆執行失敗呈現在數字總結中。
        success, skipped, failed = counts or (0, 0, 1 if returncode else 0)
        results.append(TransferResult(item, returncode, success, skipped, failed))
        status = "完成" if returncode == 0 else f"失敗 (rc={returncode})"
        print(f"===== [{index}/{total}] {item.path.name} {status} =====", flush=True)

    print("\n========== 本次傳輸結果彙總 ==========")
    for result in results:
        status = "成功" if result.returncode == 0 else f"失敗 rc={result.returncode}"
        print(
            f"  [{'成功' if result.returncode == 0 else '失敗'}] "
            f"[{MODE_LABEL[result.item.mode]}] {result.item.project} ({status})｜"
            f"成功 {result.success}，略過 {result.skipped}，失敗 {result.failed}"
        )
    failed_projects = sum(result.returncode != 0 for result in results)
    print(f"共 {total} 個專案，成功 {total - failed_projects}，失敗 {failed_projects}")
    return 0 if failed_projects == 0 else 1


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
            status = "enabled" if is_selectable(item, locked_mode) else "locked"
            print(
                f"{item.mode:<8} {status:<7} {item.trans_type:<9} "
                f"{item.project:<30} {item.path.name}"
            )
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
