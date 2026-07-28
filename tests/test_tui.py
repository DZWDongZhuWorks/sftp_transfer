# -*- coding: utf-8 -*-
"""monitor/tui.py 純邏輯測試（不觸及 curses 繪製）。

curses.KEY_* 為模組常數，import 後即可用，無需真實終端機。
"""
import csv
import curses
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

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


def test_load_tree_quiets_sync_output(tmp_path):
    args = SimpleNamespace(
        sync_config="sync.json",
        log_dir=tmp_path,
        mode="all",
        stale_hours=24,
        vessel=None,
        ipc=None,
        component=None,
        status="all",
    )
    with mock.patch.object(tui, "sync_logs", return_value=True) as sync:
        assert tui.load_tree(args, NOW) == []
    sync.assert_called_once_with("sync.json", quiet=True)


def test_sync_progress_keeps_recent_lines_clean():
    # 高度足夠顯示滿 _SYNC_LINE_LIMIT 行
    class FakeScreen:
        def __init__(self):
            self.rows = {}

        def erase(self):
            self.rows.clear()

        def getmaxyx(self):
            return tui._SYNC_LINE_LIMIT + 4, 80

        def addstr(self, y, x, text, attr=0):
            self.rows[y] = text

        def refresh(self):
            pass

    limit = tui._SYNC_LINE_LIMIT
    total = limit + 5  # 餵超過上限，驗證只保留最近 limit 行

    def fake_sync(config, quiet, output_callback):
        assert config == "sync.json"
        assert quiet is True
        for number in range(1, total + 1):
            output_callback(f"\x1b[31mline {number}\x1b[0m\r")
        return True

    screen = FakeScreen()
    with mock.patch.object(tui, "sync_logs", side_effect=fake_sync):
        assert tui._sync_with_progress(screen, "sync.json") is True

    values = set(screen.rows.values())
    # 只保留最近 limit 行（line 6..25）；較早的 line 1..5 被丟棄
    for n in range(total - limit + 1, total + 1):
        assert f"line {n}" in values
    for n in range(1, total - limit + 1):
        assert f"line {n}" not in values
    # ANSI / 控制字元已清除
    assert all("\x1b" not in v for v in values)


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
def test_display_width_helpers():
    assert tui.disp_width("abc") == 3
    assert tui.disp_width("裝置") == 4          # CJK 全形各佔 2 欄
    assert tui.disp_width("6天前") == 5          # 1 半形 + 2 全形
    # pad_display 依顯示寬度補滿，不論 CJK
    assert tui.disp_width(tui.pad_display("裝置", 10)) == 10
    assert tui.disp_width(tui.pad_display("abc", 10)) == 10
    assert tui.pad_display("x", 5, "right") == "    x"
    # fit_display 不超過欄寬、且不會切半個全形字
    s, w = tui.fit_display("遠端路徑不存在", 5)
    assert w <= 5 and tui.disp_width(s) == w


def test_device_line_columns_align():
    # 不同 age 文字（天/小時/分鐘前）下，detail 前的固定欄位顯示寬度一致
    import types
    from datetime import datetime as _dt

    def fake(comp, age_secs):
        rec = types.SimpleNamespace(
            mode="download",
            started_at=_dt(2026, 7, 27, 1, 0, 0),
            file_count=5,
            success=5, skipped=0, failed=0,
            status="success", abort_reason="", errors=[], warnings=[],
        )
        d = types.SimpleNamespace(
            component=comp, latest=rec, is_stale=False, status="success",
            display_status="success", device_name="x", vessel="V", ipc="IPC-1",
            last_seen=_dt(2026, 7, 27, 1, 0, 0),
        )
        return d

    now = _dt(2026, 7, 27, 1, 30, 0)      # 30 分鐘前
    later = _dt(2026, 7, 30, 1, 0, 0)     # 3 天前
    fixed = 20 + 1 + 19 + 1 + 5 + 1 + 9 + 1 + 9
    w1 = tui.disp_width(tui.fit_display(tui._device_line(fake("ecdis", 0), now), fixed)[0])
    w2 = tui.disp_width(tui.fit_display(tui._device_line(fake("radar", 0), later), fixed)[0])
    assert w1 == w2 == fixed


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
