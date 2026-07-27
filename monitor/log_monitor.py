#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""log_monitor.py — SFTP 傳輸 Log 監視分析工具（獨立、唯讀、僅依賴 stdlib）。

解析一個本地 log 目錄（由 sftp_transfer 本體先把遠端 log_remote_dir
`/fleet/wanhai_nssms_deploy/sftp_logs/` 下載下來），彙整成「每裝置最新更新狀態」，
同時輸出終端機彩色列表與自包含 HTML 報告，供用戶全覽與排查各設備的更新狀況。

用法：
  # A) 只分析（下載已由本體/排程完成）
    python monitor/log_monitor.py --log-dir logs --html

  # B) 一鍵即時監視（工具自行觸發本體下載遠端 sftp_logs 再分析）
    python monitor/log_monitor.py --sync-config config/xxx_download_settings.json \\
      --log-dir <該 config 的 local_path> --watch 60 --html
    
    python monitor/log_monitor.py --sync-config config/log_monitor_sync.json --log-dir fleet_logs --watch 60 --html

離開碼：一切正常 -> 0；有任何裝置處於異常（partial/aborted/incomplete）或過期 -> 1。
"""
from __future__ import annotations

import argparse
import csv
import html
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

MAIN_SCRIPT = PROJECT_DIR / "main.py"
DEFAULT_LOG_DIR = PROJECT_DIR / "logs"
TS_FMT = "%Y-%m-%d %H:%M:%S"

# --- 解析錨點（對應 downloader.py / uploader.py 的訊息字串）------------------
_RE_START = re.compile(r"SFTP (下載|上傳)任務開始")
_RE_FILE_COUNT = re.compile(r"發現 (\d+) 個檔案")
_RE_SUMMARY = re.compile(
    r"(下載|上傳)任務結束(?:（\d+ 組）)?：成功 (\d+)，略過 (\d+)，失敗 (\d+)"
)
_RE_ABORT = re.compile(r"任務中止：(.+?)\s*={0,3}\s*$")
_RE_FAILED_LIST = re.compile(r"失敗清單：(.+)")
_RE_DEVICE = re.compile(r"^(?P<vsl>[A-Za-z0-9]+)_(?P<ipc>IPC-\d+)_(?P<comp>.+)$")

# 狀態嚴重度（數字越大越該優先呈現）
_SEVERITY = {"aborted": 4, "incomplete": 3, "partial": 3, "stale": 2, "success": 0}


# ---------------------------------------------------------------------------
# 資料模型
# ---------------------------------------------------------------------------
@dataclass
class RunRecord:
    """單一 log 檔＝一次傳輸執行的彙整結果。"""

    path: Path
    device_name: str
    mode: str  # 'download' | 'upload'
    started_at: datetime | None = None
    ended_at: datetime | None = None
    file_count: int | None = None
    success: int | None = None
    skipped: int | None = None
    failed: int | None = None
    status: str = "incomplete"  # success | partial | aborted | incomplete
    abort_reason: str = ""
    failed_list: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class DeviceStatus:
    """依 device_name 彙整後的單一裝置狀態。"""

    device_name: str
    vessel: str | None
    ipc: str | None
    component: str
    latest: RunRecord
    run_count: int
    last_seen: datetime | None
    is_stale: bool
    history: list[RunRecord] = field(default_factory=list)

    @property
    def display_status(self) -> str:
        """對外呈現用狀態：latest 正常但過期時回報 stale。"""
        if self.latest.status == "success" and self.is_stale:
            return "stale"
        return self.latest.status


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_device_name(name: str) -> tuple[str | None, str | None, str]:
    """把 device_name 拆成 (vessel, ipc, component)，best-effort。

    主樣式 `{vsl}_{ipc}_{component}`（如 CLINK_IPC-1_ecdis）；不符者
    vessel/ipc=None、component=整串（涵蓋 RADAR_UPLOADER、舊命名）。
    """
    m = _RE_DEVICE.match(name or "")
    if m:
        return m.group("vsl"), m.group("ipc"), m.group("comp")
    return None, None, (name or "unknown")


def _detect_mode(filename: str, start_direction: str | None) -> str:
    """判定傳輸方向：優先看起始行內容，其次看檔名前綴。"""
    if start_direction == "上傳":
        return "upload"
    if start_direction == "下載":
        return "download"
    if filename.startswith("U_"):
        return "upload"
    return "download"


def parse_log_file(path) -> RunRecord | None:
    """解析單一 CSV log 檔為 RunRecord；檔案損壞/非本工具格式回傳 None。"""
    path = Path(path)
    device_name = ""
    started_at = None
    ended_at = None
    file_count = None
    success = skipped = failed = None
    abort_reason = ""
    failed_list: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []
    start_direction = None
    summary_direction = None
    saw_rows = False

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if not header or header[:2] != ["timestamp", "device_name"]:
                return None  # 非本工具的 CSV log
            for row in reader:
                if len(row) < 5:
                    continue
                ts_raw, dev, _version, level, message = row[0], row[1], row[2], row[3], row[4]
                saw_rows = True
                if dev and not device_name:
                    device_name = dev
                ts = _parse_ts(ts_raw)
                if ts is not None:
                    if started_at is None:
                        started_at = ts
                    ended_at = ts

                m = _RE_START.search(message)
                if m:
                    start_direction = m.group(1)
                m = _RE_FILE_COUNT.search(message)
                if m:
                    file_count = int(m.group(1))
                m = _RE_SUMMARY.search(message)
                if m:
                    summary_direction = m.group(1)
                    success, skipped, failed = int(m.group(2)), int(m.group(3)), int(m.group(4))
                m = _RE_ABORT.search(message)
                if m:
                    abort_reason = m.group(1)
                m = _RE_FAILED_LIST.search(message)
                if m:
                    failed_list = [p.strip() for p in m.group(1).split(",") if p.strip()]

                if level == "WARNING":
                    warnings.append(message)
                elif level == "ERROR":
                    errors.append(message)
    except (OSError, csv.Error, UnicodeError):
        return None

    if not saw_rows:
        return None

    if abort_reason:
        status = "aborted"
    elif success is not None:
        status = "partial" if failed else "success"
    else:
        status = "incomplete"

    if not device_name:
        # 退而求其次：由檔名還原（去掉 D_/U_ 前綴與尾端時間戳記）
        device_name = _device_from_filename(path.name)

    direction = summary_direction or start_direction
    return RunRecord(
        path=path,
        device_name=device_name,
        mode=_detect_mode(path.name, direction),
        started_at=started_at,
        ended_at=ended_at,
        file_count=file_count,
        success=success,
        skipped=skipped,
        failed=failed,
        status=status,
        abort_reason=abort_reason,
        failed_list=failed_list,
        warnings=warnings,
        errors=errors,
    )


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), TS_FMT)
    except (ValueError, AttributeError):
        return None


_RE_FILENAME = re.compile(r"^(?:[DU]_)?(?P<dev>.+?)_\d{8}_\d{6}$")


def _device_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    m = _RE_FILENAME.match(stem)
    if m:
        return m.group("dev")
    return stem or "unknown"


def collect_logs(log_dir, mode: str = "all") -> list[RunRecord]:
    """遞迴掃描 log_dir 下所有 *.csv，解析成 RunRecord 清單並依 mode 過濾。

    用 rglob 是為了涵蓋遠端 sftp_logs 下載後的巢狀結構
    （download/{vsl}/{ipc}/{component}/*.csv）；平面目錄（如既有 logs/）同樣適用。
    """
    log_dir = Path(log_dir)
    records: list[RunRecord] = []
    for path in sorted(log_dir.rglob("*.csv")):
        rec = parse_log_file(path)
        if rec is None:
            continue
        if mode != "all" and rec.mode != mode:
            continue
        records.append(rec)
    return records


def aggregate_by_device(
    records: list[RunRecord], now: datetime, stale_hours: float
) -> list[DeviceStatus]:
    """依 device_name 分組，取最新一次執行為代表並計算過期與排序。"""
    groups: dict[str, list[RunRecord]] = {}
    for rec in records:
        groups.setdefault(rec.device_name, []).append(rec)

    stale_delta = timedelta(hours=stale_hours)
    devices: list[DeviceStatus] = []
    for device_name, recs in groups.items():
        recs_sorted = sorted(
            recs, key=lambda r: r.started_at or datetime.min, reverse=True
        )
        latest = recs_sorted[0]
        last_seen = latest.started_at
        is_stale = last_seen is None or (now - last_seen) > stale_delta
        vessel, ipc, component = parse_device_name(device_name)
        devices.append(
            DeviceStatus(
                device_name=device_name,
                vessel=vessel,
                ipc=ipc,
                component=component,
                latest=latest,
                run_count=len(recs),
                last_seen=last_seen,
                is_stale=is_stale,
                history=recs_sorted,
            )
        )

    devices.sort(
        key=lambda d: (
            -_SEVERITY.get(d.display_status, 0),
            d.last_seen or datetime.min,
            d.device_name,
        )
    )
    return devices


# ---------------------------------------------------------------------------
# 選配：觸發本體下載遠端 log
# ---------------------------------------------------------------------------
def sync_logs(sync_config, timeout: float | None = 600) -> bool:
    """以子行程呼叫 sftp_transfer 本體下載遠端 log（沿用 run_all_downloads.py 模式）。

    失敗僅回傳 False、不拋例外，讓分析仍能沿用既有本地資料。
    """
    sync_config = Path(sync_config)
    if not sync_config.exists():
        print(f"[WARN] 同步設定檔不存在，略過下載：{sync_config}", file=sys.stderr)
        return False
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(MAIN_SCRIPT),
                "--cli",
                "--mode",
                "download",
                "--config",
                str(sync_config),
            ],
            cwd=str(PROJECT_DIR),
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[WARN] 觸發下載失敗，改用既有本地 log：{exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(f"[WARN] 本體下載回傳非零（{proc.returncode}），改用既有本地 log", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# 呈現：CLI
# ---------------------------------------------------------------------------
_STATUS_LABEL = {
    "success": "正常",
    "partial": "部分失敗",
    "aborted": "中止",
    "incomplete": "未完成",
    "stale": "過期",
}
_STATUS_COLOR = {  # ANSI code
    "success": "32",
    "partial": "31",
    "aborted": "31",
    "incomplete": "33",
    "stale": "33",
}


def _color(code: str, text: str, use_color: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if use_color else text


def _humanize_age(last_seen: datetime | None, now: datetime) -> str:
    if last_seen is None:
        return "—"
    secs = max(0, int((now - last_seen).total_seconds()))
    if secs < 3600:
        return f"{secs // 60}分鐘前"
    if secs < 86400:
        return f"{secs // 3600}小時前"
    return f"{secs // 86400}天前"


def _counts_str(rec: RunRecord) -> str:
    if rec.success is None:
        return "—"
    return f"{rec.success}/{rec.skipped}/{rec.failed}"


def _detail_str(dev: DeviceStatus) -> str:
    rec = dev.latest
    if rec.abort_reason:
        return f"中止：{rec.abort_reason}"
    if rec.errors:
        return rec.errors[-1]
    if rec.status == "success" and dev.is_stale:
        return "逾期未回報"
    if rec.warnings:
        return rec.warnings[-1]
    return ""


def render_cli(devices: list[DeviceStatus], now: datetime, use_color: bool = True) -> str:
    """組出終端機彩色列表（回傳字串，方便測試）。"""
    lines: list[str] = []
    total = len(devices)
    ok = sum(1 for d in devices if d.display_status == "success")
    stale = sum(1 for d in devices if d.display_status == "stale")
    bad = total - ok - stale

    header = (
        f"SFTP Log 監視 — 產生於 {now.strftime(TS_FMT)}｜"
        f"裝置 {total}｜"
        + _color("32", f"正常 {ok}", use_color)
        + "｜"
        + _color("31", f"異常 {bad}", use_color)
        + "｜"
        + _color("33", f"過期 {stale}", use_color)
    )
    lines.append(header)
    lines.append("-" * 96)

    if not devices:
        lines.append("（無資料：找不到任何可解析的 log）")
        return "\n".join(lines)

    row_fmt = "{light}  {dev:<28} {mode:<4} {last:<18} {files:>6} {counts:>10}  {age:<10} {detail}"
    lines.append(
        "  狀態  {dev:<28} {mode:<4} {last:<18} {files:>6} {counts:>10}  {age:<10} 摘要".format(
            dev="裝置", mode="方向", last="最後執行", files="檔案", counts="成功/略/失", age="距今"
        )
    )
    for d in devices:
        st = d.display_status
        light = _color(_STATUS_COLOR.get(st, "0"), "●", use_color)
        rec = d.latest
        lines.append(
            row_fmt.format(
                light=light,
                dev=(d.device_name[:28]),
                mode="↓" if rec.mode == "download" else "↑",
                last=rec.started_at.strftime(TS_FMT) if rec.started_at else "—",
                files=("—" if rec.file_count is None else str(rec.file_count)),
                counts=_counts_str(rec),
                age=_humanize_age(d.last_seen, now),
                detail=_detail_str(d)[:60],
            )
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 呈現：HTML（單一自包含檔，內嵌 CSS/JS，不引外部資源）
# ---------------------------------------------------------------------------
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SFTP Log 監視</title>
<style>
:root{{--bg:#f7f8fa;--fg:#1c1e21;--muted:#6b7280;--card:#fff;--border:#e3e6ea;
--ok:#188038;--warn:#b8860b;--bad:#d93025;--chiptext:#fff;--hover:#eef1f5;}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181c;--fg:#e6e8eb;--muted:#9aa0a6;
--card:#1f2226;--border:#2c3036;--hover:#262a30;}}}}
*{{box-sizing:border-box;}}
body{{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font-family:-apple-system,"Noto Sans TC","Segoe UI",Roboto,sans-serif;}}
h1{{font-size:20px;margin:0 0 4px;}}
.meta{{color:var(--muted);font-size:13px;margin-bottom:16px;}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px;}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;
padding:12px 18px;min-width:96px;}}
.card .n{{font-size:24px;font-weight:700;}}
.card .l{{font-size:12px;color:var(--muted);}}
.controls{{margin-bottom:12px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;}}
input,select{{padding:7px 10px;border:1px solid var(--border);border-radius:8px;
background:var(--card);color:var(--fg);font-size:14px;}}
.tablewrap{{overflow-x:auto;background:var(--card);border:1px solid var(--border);
border-radius:10px;}}
table{{border-collapse:collapse;width:100%;font-size:14px;}}
th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid var(--border);
white-space:nowrap;}}
th{{cursor:pointer;user-select:none;position:sticky;top:0;background:var(--card);}}
th:hover{{color:var(--muted);}}
tbody tr.main{{cursor:pointer;}}
tbody tr.main:hover{{background:var(--hover);}}
.chip{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
color:var(--chiptext);}}
.s-success{{background:var(--ok);}} .s-partial,.s-aborted{{background:var(--bad);}}
.s-incomplete,.s-stale{{background:var(--warn);}}
.detail{{display:none;}} .detail td{{white-space:normal;color:var(--muted);
font-size:13px;background:var(--bg);}}
.detail.open{{display:table-row;}}
.mono{{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;}}
.err{{color:var(--bad);}} .warn{{color:var(--warn);}}
.muted{{color:var(--muted);}}
</style>
</head>
<body>
<h1>SFTP 傳輸 Log 監視</h1>
<div class="meta">產生於 {generated_at}｜來源目錄 <span class="mono">{log_dir}</span>｜過期門檻 {stale_hours} 小時</div>
<div class="cards">
  <div class="card"><div class="n">{total}</div><div class="l">裝置</div></div>
  <div class="card"><div class="n" style="color:var(--ok)">{ok}</div><div class="l">正常</div></div>
  <div class="card"><div class="n" style="color:var(--bad)">{bad}</div><div class="l">異常</div></div>
  <div class="card"><div class="n" style="color:var(--warn)">{stale}</div><div class="l">過期</div></div>
</div>
<div class="controls">
  <input id="q" placeholder="搜尋裝置 / 摘要…" oninput="flt()">
  <select id="st" onchange="flt()">
    <option value="">全部狀態</option>
    <option value="success">正常</option>
    <option value="stale">過期</option>
    <option value="partial">部分失敗</option>
    <option value="aborted">中止</option>
    <option value="incomplete">未完成</option>
  </select>
  <span class="muted" style="font-size:13px">點欄位標題可排序、點列可展開明細</span>
</div>
<div class="tablewrap">
<table id="t">
<thead><tr>
<th onclick="srt(0)">狀態</th><th onclick="srt(1)">裝置</th><th onclick="srt(2)">船/IPC</th>
<th onclick="srt(3)">元件</th><th onclick="srt(4)">方向</th><th onclick="srt(5)">最後執行</th>
<th onclick="srt(6)">檔案</th><th onclick="srt(7)">成功/略/失</th><th onclick="srt(8)">距今</th>
<th onclick="srt(9)">摘要</th>
</tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
<script>
function flt(){{
  var q=document.getElementById('q').value.toLowerCase();
  var st=document.getElementById('st').value;
  document.querySelectorAll('#t tbody tr.main').forEach(function(tr){{
    var okS=!st||tr.dataset.status===st;
    var okQ=!q||tr.textContent.toLowerCase().indexOf(q)>=0;
    var show=okS&&okQ;
    tr.style.display=show?'':'none';
    var d=tr.nextElementSibling;
    if(d&&d.classList.contains('detail')&&!show){{d.classList.remove('open');}}
  }});
}}
function srt(col){{
  var tb=document.querySelector('#t tbody');
  var rows=Array.prototype.slice.call(tb.querySelectorAll('tr.main'));
  var asc=tb.dataset.col==col&&tb.dataset.dir!='asc'?true:false;
  rows.sort(function(a,b){{
    var x=a.children[col].dataset.sort||a.children[col].textContent;
    var y=b.children[col].dataset.sort||b.children[col].textContent;
    var nx=parseFloat(x),ny=parseFloat(y);
    if(!isNaN(nx)&&!isNaN(ny)){{x=nx;y=ny;}}
    return (x>y?1:x<y?-1:0)*(asc?1:-1);
  }});
  tb.dataset.col=col;tb.dataset.dir=asc?'asc':'desc';
  rows.forEach(function(r){{var d=r.nextElementSibling;tb.appendChild(r);
    if(d&&d.classList.contains('detail'))tb.appendChild(d);}});
}}
document.querySelectorAll('#t tbody tr.main').forEach(function(tr){{
  tr.addEventListener('click',function(){{
    var d=tr.nextElementSibling;
    if(d&&d.classList.contains('detail'))d.classList.toggle('open');
  }});
}});
</script>
</body>
</html>
"""


def _esc(s) -> str:
    return html.escape("" if s is None else str(s))


def _detail_html(dev: DeviceStatus) -> str:
    rec = dev.latest
    parts: list[str] = []
    if rec.abort_reason:
        parts.append(f'<div class="err">中止原因：{_esc(rec.abort_reason)}</div>')
    if rec.failed_list:
        parts.append(
            '<div class="err">失敗清單：<span class="mono">'
            + _esc(", ".join(rec.failed_list))
            + "</span></div>"
        )
    for e in rec.errors[-5:]:
        parts.append(f'<div class="err mono">ERROR：{_esc(e)}</div>')
    for w in rec.warnings[-5:]:
        parts.append(f'<div class="warn mono">WARNING：{_esc(w)}</div>')
    parts.append(
        f'<div class="muted">來源檔：<span class="mono">{_esc(rec.path.name)}</span>'
        f"｜歷史執行 {dev.run_count} 次</div>"
    )
    if len(dev.history) > 1:
        hist = "、".join(
            (r.started_at.strftime("%m-%d %H:%M") if r.started_at else "—")
            + f"（{_STATUS_LABEL.get(r.status, r.status)}）"
            for r in dev.history[:6]
        )
        parts.append(f'<div class="muted">近期：{_esc(hist)}</div>')
    return "".join(parts) or '<div class="muted">無額外明細</div>'


def render_html(
    devices: list[DeviceStatus],
    out_path,
    generated_at: datetime,
    log_dir: str = "",
    stale_hours: float = 24,
) -> Path:
    """產生單一自包含 HTML 報告，回傳寫出的路徑。"""
    out_path = Path(out_path)
    total = len(devices)
    ok = sum(1 for d in devices if d.display_status == "success")
    stale = sum(1 for d in devices if d.display_status == "stale")
    bad = total - ok - stale

    rows: list[str] = []
    for d in devices:
        rec = d.latest
        st = d.display_status
        age_secs = (
            int((generated_at - d.last_seen).total_seconds()) if d.last_seen else -1
        )
        sort_ts = rec.started_at.strftime("%Y%m%d%H%M%S") if rec.started_at else "0"
        counts = _counts_str(rec)
        rows.append(
            "<tr class='main' data-status='{st}'>"
            "<td data-sort='{sev}'><span class='chip s-{st}'>{label}</span></td>"
            "<td>{dev}</td><td>{vi}</td><td>{comp}</td><td>{mode}</td>"
            "<td data-sort='{sort_ts}'>{last}</td>"
            "<td data-sort='{files_sort}'>{files}</td>"
            "<td>{counts}</td>"
            "<td data-sort='{age}'>{age_h}</td><td>{detail}</td>"
            "</tr>".format(
                st=st,
                sev=_SEVERITY.get(st, 0),
                label=_STATUS_LABEL.get(st, st),
                dev=_esc(d.device_name),
                vi=_esc("/".join(x for x in (d.vessel, d.ipc) if x) or "—"),
                comp=_esc(d.component),
                mode="↓ 下載" if rec.mode == "download" else "↑ 上傳",
                sort_ts=sort_ts,
                last=_esc(rec.started_at.strftime(TS_FMT) if rec.started_at else "—"),
                files_sort=(rec.file_count if rec.file_count is not None else -1),
                files=("—" if rec.file_count is None else rec.file_count),
                counts=_esc(counts),
                age=age_secs,
                age_h=_esc(_humanize_age(d.last_seen, generated_at)),
                detail=_esc(_detail_str(d)),
            )
        )
        rows.append(
            f"<tr class='detail'><td colspan='10'>{_detail_html(d)}</td></tr>"
        )

    doc = _HTML_TEMPLATE.format(
        generated_at=_esc(generated_at.strftime(TS_FMT)),
        log_dir=_esc(log_dir),
        stale_hours=_esc(stale_hours),
        total=total,
        ok=ok,
        bad=bad,
        stale=stale,
        rows="\n".join(rows) if rows else "<tr><td colspan='10'>（無資料）</td></tr>",
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="SFTP 傳輸 Log 監視分析工具（解析本地 log 目錄，輸出 CLI 列表與 HTML 報告）"
    )
    p.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR), help="要分析的本地 log 目錄（預設 ./logs）")
    p.add_argument("--mode", choices=["download", "upload", "all"], default="all", help="只看某方向")
    p.add_argument("--stale-hours", type=float, default=24, help="逾期告警門檻（小時，預設 24）")
    p.add_argument(
        "--html",
        nargs="?",
        const="__auto__",
        default=None,
        help="另存 HTML 報告；可接路徑，不接則存到 logs/log_monitor_<時間>.html",
    )
    p.add_argument("--sync-config", default=None, help="分析前先用此 download 設定檔觸發本體下載遠端 log")
    p.add_argument("--watch", type=float, default=None, help="每 N 秒刷新（搭配 --sync-config 才會重新下載）")
    p.add_argument("--vessel", default=None, help="只顯示指定船名（vessel）")
    p.add_argument("--component", default=None, help="只顯示指定元件（component）")
    p.add_argument("--no-color", action="store_true", help="停用 ANSI 顏色")
    return p


def _apply_filters(devices, vessel, component):
    if vessel:
        devices = [d for d in devices if d.vessel == vessel]
    if component:
        devices = [d for d in devices if d.component == component]
    return devices


def _run_once(args, use_color: bool) -> tuple[str, list[DeviceStatus]]:
    if args.sync_config:
        sync_logs(args.sync_config)
    now = datetime.now()
    records = collect_logs(args.log_dir, mode=args.mode)
    devices = aggregate_by_device(records, now=now, stale_hours=args.stale_hours)
    devices = _apply_filters(devices, args.vessel, args.component)
    out = render_cli(devices, now=now, use_color=use_color)

    if args.html is not None:
        if args.html == "__auto__":
            html_path = Path(args.log_dir) / f"log_monitor_{now.strftime('%Y%m%d_%H%M%S')}.html"
        else:
            html_path = Path(args.html)
        render_html(
            devices,
            html_path,
            generated_at=now,
            log_dir=str(args.log_dir),
            stale_hours=args.stale_hours,
        )
        out += f"\n\nHTML 報告已寫出：{html_path}"
    return out, devices


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    use_color = (not args.no_color) and sys.stdout.isatty()

    if args.watch:
        try:
            while True:
                out, _ = _run_once(args, use_color)
                os.system("cls" if os.name == "nt" else "clear")
                print(out, flush=True)
                _sleep(args.watch)
        except KeyboardInterrupt:
            print("\n已停止監視。")
            return 0

    out, devices = _run_once(args, use_color)
    print(out)
    bad = sum(1 for d in devices if d.display_status not in ("success",))
    return 1 if bad else 0


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


if __name__ == "__main__":
    sys.exit(main())
