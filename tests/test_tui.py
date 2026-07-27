# -*- coding: utf-8 -*-
"""monitor/tui.py 純邏輯測試（不觸及 curses 繪製）。

curses.KEY_* 為模組常數，import 後即可用，無需真實終端機。
"""
import csv
import curses
from datetime import datetime

from monitor.log_monitor import (
    aggregate_by_device,
    build_tree,
    collect_logs,
    device_detail_lines,
)
from monitor import tui

NOW = datetime(2026, 7, 27, 12, 0, 0)
RECENT = "2026-07-27 11:00:00"  # 1 小時前（未過期）
OLD = "2026-07-20 11:00:00"     # 7 天前（過期）


def _write(path, device_name, direction, when, success, skipped, failed):
    verb = "下載" if direction == "download" else "上傳"
    rows = [
        (when, "INFO", f"=== SFTP {verb}任務開始 ==="),
        (when, "INFO", f"=== {verb}任務結束：成功 {success}，略過 {skipped}，失敗 {failed} ==="),
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "device_name", "version_info", "level", "message"])
        for ts, level, msg in rows:
            w.writerow([ts, device_name, "", level, msg])


def _tree(tmp_path, specs, stale_hours=24):
    for i, (dev, direction, when, s, k, f) in enumerate(specs):
        prefix = "D_" if direction == "download" else "U_"
        _write(tmp_path / f"{prefix}{dev}_{i}.csv", dev, direction, when, s, k, f)
    devices = aggregate_by_device(collect_logs(tmp_path), now=NOW, stale_hours=stale_hours)
    return build_tree(devices)


# --- flatten：展開/收合 ----------------------------------------------------
def test_flatten_expand_collapse(tmp_path):
    tree = _tree(
        tmp_path,
        [
            ("CLINK_IPC-1_ecdis", "download", RECENT, 5, 0, 0),
            ("CLINK_IPC-1_radar", "download", RECENT, 3, 0, 0),
        ],
    )
    st = tui.TuiState()
    tui.seed_expanded(tree, st)  # 全健康 → 不預設展開

    default_rows = tui.flatten_tree(tree, st, NOW)
    assert [r.kind for r in default_rows] == ["mode"]  # 只有頂層

    tui.expand_all(st, tree)
    rows = tui.flatten_tree(tree, st, NOW)
    kinds = [r.kind for r in rows]
    assert kinds == ["mode", "vessel", "ipc", "device", "device"]
    assert rows[0].depth == 0 and rows[3].depth == 3

    tui.collapse_all(st)
    assert [r.kind for r in tui.flatten_tree(tree, st, NOW)] == ["mode"]


def test_seed_expands_problem_groups(tmp_path):
    tree = _tree(tmp_path, [("CLINK_IPC-1_radar", "download", RECENT, 0, 0, 3)])  # 失敗
    st = tui.TuiState()
    tui.seed_expanded(tree, st)
    rows = tui.flatten_tree(tree, st, NOW)
    assert [r.kind for r in rows] == ["mode", "vessel", "ipc", "device"]  # 問題群組自動展開


def test_seed_respects_user_collapse(tmp_path):
    # 問題群組首次預設展開；使用者收合後再次 seed（同一棵樹）不應被重新展開
    tree = _tree(tmp_path, [("CLINK_IPC-1_radar", "download", RECENT, 0, 0, 3)])
    st = tui.TuiState()
    tui.seed_expanded(tree, st)
    vkey = ("V", "download", "CLINK")
    assert vkey in st.expanded
    st.expanded.discard(vkey)          # 使用者收合
    tui.seed_expanded(tree, st)        # 模擬重載後再次 seed
    assert vkey not in st.expanded     # 已在 seen，不再自動展開


# --- flatten：過濾 ---------------------------------------------------------
def test_flatten_filters(tmp_path):
    tree = _tree(
        tmp_path,
        [
            ("CLINK_IPC-1_ecdis", "download", RECENT, 5, 0, 0),   # success
            ("CLINK_IPC-1_radar", "download", RECENT, 0, 0, 2),   # partial
            ("CLINK_IPC-1_share", "download", OLD, 5, 0, 0),      # stale
        ],
    )
    st = tui.TuiState()
    tui.expand_all(st, tree)

    def devs(state):
        return [r for r in tui.flatten_tree(tree, state, NOW) if r.kind == "device"]

    assert len(devs(st)) == 3

    st.only_problem = True  # 非 success（partial + stale）
    assert len(devs(st)) == 2

    st.only_problem = False
    st.status = "ok"
    assert [r.ref.component for r in devs(st)] == ["ecdis"]

    st.status = "problem"  # 非 success 且非 stale → 只有 partial
    assert [r.ref.component for r in devs(st)] == ["radar"]

    st.status = "all"
    st.query = "share"
    assert [r.ref.component for r in devs(st)] == ["share"]


def test_flatten_mode_filter(tmp_path):
    tree = _tree(
        tmp_path,
        [
            ("CLINK_IPC-1_ecdis", "download", RECENT, 5, 0, 0),
            ("CLINK_IPC-1_ecdis", "upload", RECENT, 3, 0, 0),
        ],
    )
    st = tui.TuiState()
    tui.expand_all(st, tree)
    modes = {r.text.split()[0] for r in tui.flatten_tree(tree, st, NOW) if r.kind == "mode"}
    assert modes == {"↓", "↑"}  # _MODE_LABEL 前綴

    st.mode = "download"
    kept = {r.ref.latest.mode for r in tui.flatten_tree(tree, st, NOW) if r.kind == "device"}
    assert kept == {"download"}


def test_flatten_empty_when_all_filtered(tmp_path):
    tree = _tree(tmp_path, [("CLINK_IPC-1_ecdis", "download", RECENT, 5, 0, 0)])
    st = tui.TuiState()
    tui.expand_all(st, tree)
    st.query = "no-such-device"
    assert tui.flatten_tree(tree, st, NOW) == []


# --- reducer ---------------------------------------------------------------
def test_toggle_and_cycles():
    st = tui.TuiState()
    tui.toggle(st, ("M", "download"))
    assert ("M", "download") in st.expanded
    tui.toggle(st, ("M", "download"))
    assert ("M", "download") not in st.expanded

    assert st.mode == ""
    tui.cycle_mode(st); assert st.mode == "download"
    tui.cycle_mode(st); assert st.mode == "upload"
    tui.cycle_mode(st); assert st.mode == ""

    assert st.status == "all"
    tui.cycle_status(st); assert st.status == "ok"
    for _ in range(3):
        tui.cycle_status(st)
    assert st.status == "all"

    assert st.only_problem is False
    tui.toggle_problem(st); assert st.only_problem is True


def test_move_selection_clamps(tmp_path):
    tree = _tree(
        tmp_path,
        [
            ("CLINK_IPC-1_ecdis", "download", RECENT, 5, 0, 0),
            ("CLINK_IPC-1_radar", "download", RECENT, 3, 0, 0),
        ],
    )
    st = tui.TuiState()
    tui.expand_all(st, tree)
    rows = tui.flatten_tree(tree, st, NOW)
    tui.clamp_selection(rows, st)
    assert st.sel_key == rows[0].key

    tui.move_selection(rows, st, -5)  # 夾在頂端
    assert st.sel_key == rows[0].key
    tui.move_selection(rows, st, 999)  # 夾在底端
    assert st.sel_key == rows[-1].key

    # 空列表：選取清空、不崩潰
    tui.move_selection([], st, 1)
    assert st.sel_key is None


def test_clamp_selection_recovers_missing_key(tmp_path):
    tree = _tree(tmp_path, [("CLINK_IPC-1_ecdis", "download", RECENT, 5, 0, 0)])
    st = tui.TuiState()
    tui.expand_all(st, tree)
    rows = tui.flatten_tree(tree, st, NOW)
    st.sel_key = ("D", "download", "GONE", "IPC-9", "x")  # 不存在
    tui.clamp_selection(rows, st)
    assert st.sel_key == rows[0].key


def test_parent_key():
    assert tui.parent_key(("D", "download", "CLINK", "IPC-1", "ecdis")) == (
        "I", "download", "CLINK", "IPC-1"
    )
    assert tui.parent_key(("I", "download", "CLINK", "IPC-1")) == ("V", "download", "CLINK")
    assert tui.parent_key(("V", "download", "CLINK")) == ("M", "download")
    assert tui.parent_key(("M", "download")) is None


def test_key_action():
    assert tui.key_action(ord("q")) == "quit"
    assert tui.key_action(curses.KEY_UP) == "up"
    assert tui.key_action(ord("j")) == "down"
    assert tui.key_action(ord(" ")) == "enter"
    assert tui.key_action(curses.KEY_ENTER) == "enter"
    assert tui.key_action(curses.KEY_RIGHT) == "expand"
    assert tui.key_action(ord("/")) == "search"
    assert tui.key_action(ord("E")) == "expand_all"
    assert tui.key_action(ord("z")) is None


# --- 明細 ------------------------------------------------------------------
def test_device_detail_lines(tmp_path):
    p = tmp_path / "D_CLINK_IPC-1_radar_0.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "device_name", "version_info", "level", "message"])
        w.writerow([RECENT, "CLINK_IPC-1_radar", "", "INFO", "=== SFTP 下載任務開始 ==="])
        w.writerow([RECENT, "CLINK_IPC-1_radar", "", "ERROR", "檔案 a/x.bin 下載失敗，放棄重試"])
        w.writerow([RECENT, "CLINK_IPC-1_radar", "", "INFO", "=== 下載任務結束：成功 0，略過 0，失敗 1 ==="])
        w.writerow([RECENT, "CLINK_IPC-1_radar", "", "INFO", "失敗清單：a/x.bin"])
    devices = aggregate_by_device(collect_logs(tmp_path), now=NOW, stale_hours=24)
    lines = device_detail_lines(devices[0])
    text = "\n".join(lines)
    assert "CLINK_IPC-1_radar" in text
    assert "失敗清單：a/x.bin" in text
    assert any("ERROR" in ln for ln in lines)
