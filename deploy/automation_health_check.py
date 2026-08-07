#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NSSMS 隱性自動化設定的一鍵存活巡檢。

本工具只讀取狀態，不會 start/stop/restart/enable/disable 任何服務，也不會修改
systemd、sudoers、tmux 或 failover 狀態。

預設把 wave 排程視為可選功能：timer 仍會檢查，但 wave 腳本缺少或最近執行失敗
只記為 SKIP，不影響整體結果。需要嚴格檢查 wave 時加 --strict-wave。

離開碼：
  0  沒有 FAIL（允許 INFO/SKIP/WARN）
  1  至少一項 FAIL
  2  指定 --fail-on-warn 且至少一項 WARN（但沒有 FAIL）
"""
from __future__ import annotations

import argparse
import getpass
import grp
import json
import os
import pwd
import shlex
import shutil
import socket
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
SHARE_DIR = PROJECT_DIR.parent
SCHEDULER_DIR = SHARE_DIR / "scheduler"
TIMERS_DIR = SCHEDULER_DIR / "timers"
# 常駐服務的母體目錄。nssms-heartbeat.service 原本在 failover/,隨「timer / service 架構
# 拆分」搬到這裡,與 alarm-controller / board-server / button 同列(由 install_services.sh
# 安裝)。這一份清單必須跟著 services/ 走 —— 忘了改的話,unit-sync 會回報「找不到 repo
# 母體」而讓整份巡檢變 UNHEALTHY(實際發生過)。
SERVICES_DIR = SCHEDULER_DIR / "services"
FAILOVER_DIR = SCHEDULER_DIR / "failover"
# 身分檔。身分與接管狀態都在這一個檔裡(ipc + failover + failover_since)。
# 可用環境變數覆蓋（測試專用），與 sftp_transfer/settings.py 同一慣例。
VESSEL_INFO = Path(
    os.environ.get("VESSEL_INFO_PATH") or SHARE_DIR / ".env" / "vessel_basic_info.json"
)
# 舊格式的獨立接管狀態檔,已廢除;heartbeat 啟動時會遷移。這裡只是讓它可見。
# 【移除條件】全隊確認升級完成後,連同 scheduler/failover/role.py 的遷移碼一起刪掉。
LEGACY_FAILOVER_STATE = Path(
    os.environ.get("FAILOVER_STATE_PATH") or SHARE_DIR / ".env" / "failover_state.json"
)
USER_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
SUDOERS_FILE = Path("/etc/sudoers.d/nssms-scheduler")
REPORT_DIR = PROJECT_DIR / "logs"

# 接管持續超過這麼久就從 INFO 升為 WARN。真實接管撐過一天代表 ipc1 一直沒修好，
# 該有人知道；為了測試而手動建立、事後忘了清除的殘留狀態檔也會從這裡浮出來。
TAKEOVER_WARN_HOURS = 24

# nssms-boot 還在跑中（activating/start）撐超過這麼久，就不再當作「還沒收工」而是真的
# 卡住。為什麼這條線得由巡檢器自己畫：nssms-boot 是 Type=oneshot，而 systemd 對 oneshot
# 的 TimeoutStartSec 預設是 infinity —— 它自己永遠不會喊卡。ExecStartPre 的 sleep 20 加上
# reboot_launcher 的 OTA（SFTP 拉碼）與全相位起服務，船上慢網也該在十幾分鐘內收工。
BOOT_ACTIVATING_FAIL_SECONDS = 900

CORE_SERVICES = ("nssms-boot.service", "nssms-heartbeat.service")
TIMERS = (
    "nssms-device-monitor-probe",
    "nssms-device-monitor-report",
    "nssms-reboot",
    "nssms-teamviewer",
    "nssms-warm-env",
    "nssms-wave-send",
    "nssms-wave-update",
)
OPTIONAL_WAVE = {"nssms-wave-send", "nssms-wave-update"}

# 角色 → 預期的 tmux session。**由 scheduler/reboot_script/roles.conf 推導**,不再硬編碼:
# 那份表是啟動器的唯一真相,在這裡放第二份清單會漂移,而且漂移不會有任何執行期錯誤
# ——正是這支巡檢器要抓的那種安靜失效。讀不到時 record FAIL 並退回下面的保底值,
# 絕不拋例外(deploy_offline.sh 把本程式當作首次部署唯一的驗證關卡)。
ROLES_CONF = SCHEDULER_DIR / "reboot_script" / "roles.conf"
FALLBACK_EXPECTED_TMUX = {
    "ipc1": {"shm", "radar", "wave", "ecdis", "flag"},
    "ipc1emer": {"shm", "radar", "wave", "ecdis", "flag"},
    "ipc2": {"shm"},
    "ipc2emer": {"shm", "radar", "wave", "ecdis", "flag"},
}

_TTY = sys.stdout.isatty()
_COLORS = {
    "PASS": "\033[32m",
    "WARN": "\033[33m",
    "FAIL": "\033[31m",
    "INFO": "\033[36m",
    "SKIP": "\033[90m",
}
_RESET = "\033[0m"
_COMPACT = False


@dataclass
class Check:
    section: str
    name: str
    status: str
    detail: str


RESULTS: list[Check] = []


def color(status: str) -> str:
    if not _TTY:
        return status
    return f"{_COLORS.get(status, '')}{status}{_RESET}"


def record(section: str, name: str, status: str, detail: str) -> None:
    RESULTS.append(Check(section, name, status, detail))
    if not _COMPACT or status in {"WARN", "FAIL"}:
        print(f"  [{color(status):>4}] {name}: {detail}")


def heading(title: str) -> None:
    if not _COMPACT:
        print(f"\n=== {title} ===")


def _takeover_age_hours(since) -> float | None:
    """接管已持續幾小時。since 不是合法時間就回傳 None。

    負值（since 位於未來）同樣視為不合法——heartbeat.sanitize_since() 會把它
    clamp 成當下，這裡跟著回報為異常而非算出一個負的時數。
    """
    try:
        age = (datetime.now().timestamp() - float(since)) / 3600
    except (TypeError, ValueError):
        return None
    return age if age >= 0 else None


def _monotonic_age_seconds(value: str) -> float | None:
    """systemd 的 *TimestampMonotonic（開機以來的微秒）→ 距今幾秒。

    刻意用 monotonic 而不是人類可讀的 *Timestamp：後者長成
    "Tue 2026-08-05 10:00:00 CST"，格式隨 locale 與時區浮動，船上機器兩者都不保證，
    解析失敗就等於量不到時間。monotonic 對上 /proc/uptime 只是兩個數字相減。

    量不到（欄位空白、單位從未離開 inactive、讀不到 uptime）一律回傳 None，
    讓呼叫端自己決定要怎麼降級 —— 這支程式絕不因為量不到時間就拋例外。
    """
    try:
        started = float(value) / 1_000_000
    except (TypeError, ValueError):
        return None
    if started <= 0:
        return None
    try:
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None
    age = uptime - started
    return age if age >= 0 else None


def run(cmd: list[str], timeout: float = 15) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def systemctl_show(unit: str, *properties: str) -> dict[str, str]:
    cmd = ["systemctl", "--user", "show", unit, "--no-pager"]
    for prop in properties:
        cmd.extend(["-p", prop])
    proc = run(cmd)
    values: dict[str, str] = {"_returncode": str(proc.returncode)}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if proc.stderr.strip():
        values["_stderr"] = proc.stderr.strip()
    return values


def normalize_ipc(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON 根節點不是物件")
    return data


def _failover_on(value) -> bool:
    """身分檔的 failover 欄位是否為真。

    規則必須與 scheduler/failover/role.py 的 is_failover_on()、effective_role.sh 的
    nssms_failover_on() 及 device_monitor/_common.py 的 _failover_on() 逐字相同;
    四份實作由 scheduler/tests/test_role_contract.py 保證不漂移。
    """
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    return False


def check_identity() -> tuple[str, str]:
    heading("身分與路徑")
    role = "unknown"
    vessel = "unknown"
    info: dict = {}
    try:
        info = read_json(VESSEL_INFO)
        vessel = str(info.get("vsl_name", "")).strip() or "unknown"
        ipc = normalize_ipc(str(info.get("ipc", "")))
        if ipc in {"ipc1", "ipc2"}:
            role = ipc
            record("identity", "船舶／IPC 身分", "PASS", f"{vessel} / {ipc}")
        else:
            record("identity", "船舶／IPC 身分", "FAIL", f"無法辨識 ipc={ipc!r}")
    except Exception as exc:  # noqa: BLE001
        record("identity", "船舶／IPC 身分", "FAIL", f"{VESSEL_INFO}: {exc}")

    # 接管狀態 = 身分檔的 failover 旗標(兩個方向都可能)。
    if role in {"ipc1", "ipc2"} and _failover_on(info.get("failover")):
        peer = "ipc2" if role == "ipc1" else "ipc1"
        role += "emer"
        raw_since = info.get("failover_since")
        since_desc = info.get("failover_since_iso", raw_since)
        age_h = _takeover_age_hours(raw_since)
        if age_h is None:
            # since 壞掉 → heartbeat 會 clamp 成現在（見 role.sanitize_since），
            # 接管不會中斷，但最短停留的計時會被重設，值得提出來。
            record(
                "identity", "接管狀態", "WARN",
                f"{role}，但 failover_since 不是合法時間（{raw_since!r}）；"
                "heartbeat 會以當下時間重新起算最短停留",
            )
        elif age_h >= TAKEOVER_WARN_HOURS:
            # 真實接管撐過一天代表對方一直沒修好，該有人知道；
            # 測試留下的殘留旗標也會從這裡浮出來。
            record(
                "identity", "接管狀態", "WARN",
                f"{role} 已持續 {age_h:.1f} 小時（≥ {TAKEOVER_WARN_HOURS}）"
                f"，since={since_desc}；"
                f"請確認 {peer} 是否真的故障，若為測試殘留請執行 "
                "scheduler/failover/failover_ctl.sh clear",
            )
        else:
            record("identity", "接管狀態", "INFO",
                   f"{role}（接管 {peer}），已持續 {age_h:.1f} 小時，since={since_desc}")
    elif info.get("failover") is not None:
        # 有欄位但不是真:白名單外的值一律視為未接管並告警(見 _failover_on)。
        record("identity", "接管狀態", "WARN",
               f"身分檔有 failover={info.get('failover')!r}，非明確的 true → 視為未接管")
    else:
        record("identity", "接管狀態", "INFO", "NORMAL（身分檔無 failover 旗標）")

    # 舊格式的獨立狀態檔:heartbeat 啟動時會遷移,但在那之前它仍會影響角色判定。
    if LEGACY_FAILOVER_STATE.exists():
        record(
            "identity", "舊格式接管狀態檔", "WARN",
            f"{LEGACY_FAILOVER_STATE} 仍存在（已廢除）。heartbeat 啟動時會併進身分檔的 "
            "failover 欄位並刪除；要立即完成請執行 "
            "systemctl --user restart nssms-heartbeat",
        )

    if vessel.upper() == "CLINK":
        record("identity", "OTA 守門", "INFO", "CLINK 開發機會刻意略過開機 SFTP OTA")
    else:
        record("identity", "OTA 守門", "PASS", "非 CLINK，開機 OTA 應正常執行")
    return vessel, role


def check_user_manager() -> None:
    heading("systemd user manager")
    proc = run(["systemctl", "--user", "show-environment"])
    if proc.returncode == 0:
        record("systemd", "user manager", "PASS", "systemctl --user 可正常溝通")
    else:
        record(
            "systemd",
            "user manager",
            "FAIL",
            (proc.stderr or proc.stdout).strip() or f"exit={proc.returncode}",
        )
        return

    user = getpass.getuser()
    linger = run(["loginctl", "show-user", user, "-p", "Linger", "--value"])
    if linger.returncode == 0 and linger.stdout.strip() == "yes":
        record("systemd", "linger", "PASS", f"{user}: Linger=yes（免登入可於開機運作）")
    else:
        detail = linger.stdout.strip() or linger.stderr.strip() or "Linger!=yes"
        record("systemd", "linger", "FAIL", detail)

    timezone = run(["timedatectl", "show", "-p", "Timezone", "--value"])
    zone = timezone.stdout.strip() if timezone.returncode == 0 else "unknown"
    record("systemd", "排程時區", "INFO", f"{zone}（OnCalendar 依此時區解讀）")


def evaluate_service(
    unit: str,
    *,
    expected_active: str,
    expected_substates: set[str],
    optional: bool = False,
    strict_optional: bool = False,
) -> dict[str, str]:
    props = systemctl_show(
        unit,
        "LoadState",
        "UnitFileState",
        "ActiveState",
        "SubState",
        "Result",
        "ExecMainStatus",
        "ExecMainStartTimestamp",
        "ExecMainExitTimestamp",
        "FragmentPath",
        "MainPID",
    )
    skip = optional and not strict_optional
    bad_status = "SKIP" if skip else "FAIL"

    if props.get("LoadState") != "loaded":
        record("units", unit, bad_status, f"LoadState={props.get('LoadState', 'unknown')}")
        return props

    state = props.get("ActiveState", "")
    substate = props.get("SubState", "")
    result = props.get("Result", "")
    exit_status = props.get("ExecMainStatus", "")
    healthy = state == expected_active and substate in expected_substates
    if expected_active == "inactive":
        healthy = state in {"inactive", "active", "activating"} and result not in {
            "exit-code",
            "signal",
            "timeout",
            "core-dump",
        }

    if healthy:
        detail = f"{state}/{substate}, result={result or 'n/a'}"
        if exit_status:
            detail += f", exit={exit_status}"
        record("units", unit, "PASS", detail)
    else:
        detail = f"{state}/{substate}, result={result or 'n/a'}, exit={exit_status or 'n/a'}"
        record("units", unit, bad_status, detail)
    return props


def check_core_services() -> None:
    heading("核心 services")
    boot = systemctl_show(
        "nssms-boot.service",
        "LoadState",
        "UnitFileState",
        "ActiveState",
        "SubState",
        "Result",
        "ExecMainStatus",
        "FragmentPath",
        "InactiveExitTimestampMonotonic",
    )
    boot_ok = (
        boot.get("LoadState") == "loaded"
        and boot.get("UnitFileState") == "enabled"
        and boot.get("ActiveState") == "active"
        and boot.get("SubState") == "exited"
        and boot.get("Result") == "success"
        and boot.get("ExecMainStatus") in {"", "0"}
    )
    boot_detail = (
        f"{boot.get('ActiveState')}/{boot.get('SubState')}, "
        f"enabled={boot.get('UnitFileState')}, result={boot.get('Result')}, "
        f"exit={boot.get('ExecMainStatus') or 'n/a'}"
    )
    boot_status = "PASS" if boot_ok else "FAIL"
    if not boot_ok:
        # 「這一輪還在跑」不等於「壞了」。activating/start + result=success 是 oneshot
        # 正在執行中的正常樣貌,而 nssms-boot 的跑中時間本來就長:ExecStartPre 的
        # sleep 20 之後還有 reboot_launcher 的 OTA 與全相位起服務。
        #
        # 為什麼一定要跟 FAIL 分開:deploy_offline 若在機器剛開機、或有人另開視窗手動
        # `systemctl --user start nssms-boot` 的當下跑到這裡,就會撞見這個中間狀態。
        # 報成 FAIL 的話,船上工程師會以為開機入口壞了而去 disable/重裝 unit ——
        # 動到的正是全船唯一的開機入口,比原本那個「等一下就好」的狀況嚴重得多。
        age = _monotonic_age_seconds(boot.get("InactiveExitTimestampMonotonic", ""))
        starting = (
            boot.get("LoadState") == "loaded"
            and boot.get("UnitFileState") == "enabled"
            and boot.get("ActiveState") == "activating"
            and boot.get("Result") in {"", "success"}
        )
        if starting and (age is None or age <= BOOT_ACTIVATING_FAIL_SECONDS):
            boot_status = "WARN"
            elapsed = f"已 {int(age)} 秒" if age is not None else "起始時間量不到"
            boot_detail += (
                f"；仍在啟動中（{elapsed}）,不是失敗。等它收工後重跑本巡檢,"
                "應為 active/exited"
            )
        elif starting:
            boot_detail += (
                f"；卡在啟動中已 {int(age)} 秒（超過 {BOOT_ACTIVATING_FAIL_SECONDS} 秒）,"
                f"看 {SCHEDULER_DIR / 'logs' / 'launcher.log'} 最後一行卡在哪"
            )
    record("units", "nssms-boot.service", boot_status, boot_detail)

    heartbeat = systemctl_show(
        "nssms-heartbeat.service",
        "LoadState",
        "UnitFileState",
        "ActiveState",
        "SubState",
        "Result",
        "MainPID",
        "FragmentPath",
    )
    heartbeat_ok = (
        heartbeat.get("LoadState") == "loaded"
        and heartbeat.get("UnitFileState") == "enabled"
        and heartbeat.get("ActiveState") == "active"
        and heartbeat.get("SubState") == "running"
        and heartbeat.get("Result") == "success"
        and int(heartbeat.get("MainPID", "0") or "0") > 0
    )
    record(
        "units",
        "nssms-heartbeat.service",
        "PASS" if heartbeat_ok else "FAIL",
        (
            f"{heartbeat.get('ActiveState')}/{heartbeat.get('SubState')}, "
            f"enabled={heartbeat.get('UnitFileState')}, "
            f"pid={heartbeat.get('MainPID') or 'n/a'}"
        ),
    )


def unit_source_path(unit: str) -> Path | None:
    """unit 檔名 → repo 母體路徑。

    刻意不再對 nssms-heartbeat.service 做特例:改成依序在 services/ 與 timers/ 找。
    這樣日後 services/ 新增常駐服務時,本檔不必跟著改一個硬編碼的名字。
    """
    for directory in (SERVICES_DIR, TIMERS_DIR):
        candidate = directory / unit
        if candidate.exists():
            return candidate
    return None


def check_unit_sources() -> None:
    heading("unit 母體與實際安裝版本")
    # 常駐服務由 services/ 目錄列舉,不硬編碼名字 —— 新增一支就自動納入比對。
    units = sorted(p.name for p in SERVICES_DIR.glob("*.service")) if SERVICES_DIR.is_dir() else []
    if not units:
        record("unit-sync", "services/", "FAIL", f"找不到常駐服務母體目錄：{SERVICES_DIR}")
    for stem in TIMERS:
        units.extend((f"{stem}.timer", f"{stem}.service"))

    for unit in units:
        source = unit_source_path(unit)
        installed = USER_UNIT_DIR / unit
        if source is None or not source.exists():
            status = "SKIP" if unit.startswith(("nssms-wave-send", "nssms-wave-update")) else "FAIL"
            record("unit-sync", unit, status, "找不到 repo 母體")
            continue
        if not installed.exists():
            record("unit-sync", unit, "FAIL", f"尚未安裝至 {installed}")
            continue
        try:
            same = source.read_bytes() == installed.read_bytes()
        except OSError as exc:
            record("unit-sync", unit, "FAIL", str(exc))
            continue
        record(
            "unit-sync",
            unit,
            "PASS" if same else "WARN",
            "母體與已安裝版本一致" if same else "內容不同；需重跑對應安裝器",
        )


def parse_execstart_targets(service_file: Path) -> list[Path]:
    targets: list[Path] = []
    for raw in service_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("ExecStart="):
            continue
        value = line.split("=", 1)[1].replace("%h", str(Path.home()))
        try:
            tokens = shlex.split(value)
        except ValueError:
            continue
        for token in tokens:
            if token.startswith("/") and token not in {"/bin/bash", "/usr/bin/sudo"}:
                targets.append(Path(token))
    return targets


def check_timers(strict_wave: bool) -> None:
    heading("timers 與最近執行結果")
    for stem in TIMERS:
        optional = stem in OPTIONAL_WAVE
        timer = f"{stem}.timer"
        service = f"{stem}.service"
        timer_props = systemctl_show(
            timer,
            "LoadState",
            "UnitFileState",
            "ActiveState",
            "SubState",
            "Result",
            "NextElapseUSecRealtime",
            "LastTriggerUSec",
        )
        timer_ok = (
            timer_props.get("LoadState") == "loaded"
            and timer_props.get("UnitFileState") == "enabled"
            and timer_props.get("ActiveState") == "active"
            and timer_props.get("SubState") == "waiting"
        )
        record(
            "timers",
            timer,
            "PASS" if timer_ok else "FAIL",
            (
                f"{timer_props.get('ActiveState')}/{timer_props.get('SubState')}, "
                f"enabled={timer_props.get('UnitFileState')}, "
                f"next={timer_props.get('NextElapseUSecRealtime') or 'n/a'}, "
                f"last={timer_props.get('LastTriggerUSec') or '尚無'}"
            ),
        )

        evaluate_service(
            service,
            expected_active="inactive",
            expected_substates={"dead"},
            optional=optional,
            strict_optional=strict_wave,
        )

        source = TIMERS_DIR / service
        if not source.exists():
            status = "SKIP" if optional and not strict_wave else "FAIL"
            record("targets", f"{service} ExecStart", status, f"找不到 unit 母體：{source}")
            continue
        targets = parse_execstart_targets(source)
        if not targets:
            record("targets", f"{service} ExecStart", "WARN", "未解析到外部目標")
            continue
        missing = [str(path) for path in targets if not path.exists()]
        if missing:
            status = "SKIP" if optional and not strict_wave else "FAIL"
            record("targets", f"{service} ExecStart", status, "缺少：" + ", ".join(missing))
        else:
            record(
                "targets",
                f"{service} ExecStart",
                "PASS",
                "目標存在：" + ", ".join(str(path) for path in targets),
            )


def check_failed_units(strict_wave: bool) -> None:
    heading("systemd failed units")
    proc = run(
        [
            "systemctl",
            "--user",
            "--failed",
            "--no-legend",
            "--plain",
            "--no-pager",
        ]
    )
    if proc.returncode not in {0, 1}:
        record("failed-units", "failed unit 清單", "FAIL", proc.stderr.strip())
        return
    failed = []
    for line in proc.stdout.splitlines():
        unit = line.split(maxsplit=1)[0] if line.strip() else ""
        if unit.startswith("nssms-"):
            failed.append(unit)
    if not failed:
        record("failed-units", "nssms failed units", "PASS", "無")
        return
    for unit in failed:
        optional = unit.startswith(("nssms-wave-send.", "nssms-wave-update."))
        status = "SKIP" if optional and not strict_wave else "FAIL"
        detail = "wave 尚未提供，忽略既有失敗狀態" if status == "SKIP" else "unit 處於 failed"
        record("failed-units", unit, status, detail)


def check_sudoers() -> None:
    heading("sudoers 最小權限")
    if not SUDOERS_FILE.exists():
        record("sudoers", str(SUDOERS_FILE), "FAIL", "檔案不存在")
        return
    try:
        info = SUDOERS_FILE.stat()
        mode = stat.S_IMODE(info.st_mode)
        owner = pwd.getpwuid(info.st_uid).pw_name
        group = grp.getgrgid(info.st_gid).gr_name
        metadata_ok = mode == 0o440 and owner == "root" and group == "root"
        record(
            "sudoers",
            "檔案權限",
            "PASS" if metadata_ok else "FAIL",
            f"mode={mode:04o}, owner={owner}:{group}",
        )
    except OSError as exc:
        record("sudoers", "檔案權限", "FAIL", str(exc))

    proc = run(["sudo", "-n", "-l"])
    text = f"{proc.stdout}\n{proc.stderr}"
    expected = (
        "/usr/bin/systemctl reboot",
        "/usr/bin/teamviewer daemon restart",
    )
    if proc.returncode == 0 and all(item in text for item in expected):
        record("sudoers", "NOPASSWD 白名單", "PASS", "reboot 與 teamviewer 精確命令皆存在")
    else:
        missing = [item for item in expected if item not in text]
        record(
            "sudoers",
            "NOPASSWD 白名單",
            "FAIL",
            "缺少：" + ", ".join(missing) if missing else f"sudo -n -l exit={proc.returncode}",
        )


DEFAULT_PEERS = {"ipc1": "192.168.8.115", "ipc2": "192.168.8.220"}


def resolve_peer_addr(config: dict, peer: str) -> str:
    """對方的位址。規則與 heartbeat.resolve_peer() 相同(含舊 peer_ip 鍵的相容)。"""
    peers = config.get("peers") or {}
    # 舊設定檔的 peer_ip 指的一律是 ipc1(改版前只有 ipc2→ipc1 這個方向)。
    if peer == "ipc1" and str(config.get("peer_ip") or "").strip():
        return str(config["peer_ip"]).strip()
    return str(peers.get(peer) or "").strip() or DEFAULT_PEERS.get(peer, "")


def heartbeat_probe(role: str) -> None:
    heading("heartbeat 實際探針")
    config_path = FAILOVER_DIR / "config.json"
    try:
        config = read_json(config_path)
        port = int(config.get("port", 6100))
        timeout = min(float(config.get("timeout", 3)), 5.0)
    except Exception as exc:  # noqa: BLE001
        record("heartbeat", "設定檔", "FAIL", f"{config_path}: {exc}")
        return

    base = role[:-4] if role.endswith("emer") else role
    if base not in {"ipc1", "ipc2"}:
        record("heartbeat", "角色探針", "FAIL", f"未知角色：{role}")
        return

    # 心跳現在是雙向的:兩台都同時跑 responder 與 monitor,所以**每個角色**都要驗兩件事。
    # 1) responder 自檢:本機的心跳埠有沒有在聽(對方就是靠這個判斷本機活著)。
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            reply = sock.recv(128).decode("utf-8", errors="replace").strip()
        status = "PASS" if reply.startswith("alive ") else "FAIL"
        record("heartbeat", "responder 自檢", status, f"127.0.0.1:{port} → {reply!r}")
    except OSError as exc:
        record("heartbeat", "responder 自檢", "FAIL",
               f"127.0.0.1:{port}: {exc}；對方會把本機視為失聯")

    # 2) monitor 方向:探測對方(ipc1 探 ipc2、ipc2 探 ipc1)。
    peer = "ipc2" if base == "ipc1" else "ipc1"
    peer_ip = resolve_peer_addr(config, peer)
    if not peer_ip:
        record("heartbeat", f"{peer} peer", "FAIL",
               f"設定檔沒有 {peer} 的位址（peers.{peer}）")
        return
    try:
        with socket.create_connection((peer_ip, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            reply = sock.recv(128).decode("utf-8", errors="replace").strip()
        record("heartbeat", f"{peer} peer", "PASS", f"{peer_ip}:{port} → {reply!r}")
    except OSError as exc:
        record(
            "heartbeat", f"{peer} peer", "WARN",
            f"{peer_ip}:{port} 暫時無回應：{exc}；接管仍依 takeover_after 判定",
        )


def load_expected_tmux() -> dict[str, set[str]]:
    """從 scheduler/reboot_script/roles.conf 推導「角色 → 預期的 tmux session」。

    取 kind=session 且相位含 run 的專案。@inherit <role> from <parent> 會展開。
    讀不到或解不開時 record FAIL 並退回保底值 —— 這支程式是 deploy_offline.sh 眼中
    首次部署唯一的驗證關卡,絕不能因為讀一個設定檔就拋例外、讓部署最後一步變成
    「執行異常」。
    """
    try:
        text = ROLES_CONF.read_text(encoding="utf-8")
    except OSError as exc:
        record("tmux", "角色宣告表", "FAIL",
               f"{ROLES_CONF}: {exc}；改用內建保底清單比對")
        return dict(FALLBACK_EXPECTED_TMUX)

    expected: dict[str, set[str]] = {}
    inherits: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] == "@inherit":
            if len(parts) >= 4 and parts[2] == "from":
                inherits.append((parts[1], parts[3]))
            continue
        if len(parts) < 4:
            continue
        role_name, project, kind, phases = parts[0], parts[1], parts[2], parts[3]
        expected.setdefault(role_name, set())
        if kind == "session" and "run" in phases.split(","):
            expected[role_name].add(project)
    for child, parent in inherits:
        if parent in expected:
            expected[child] = set(expected[parent]) | expected.get(child, set())

    missing = [r for r in FALLBACK_EXPECTED_TMUX if r not in expected]
    if missing:
        record("tmux", "角色宣告表", "FAIL",
               f"{ROLES_CONF} 缺少角色 {', '.join(sorted(missing))}；改用內建保底清單比對")
        return dict(FALLBACK_EXPECTED_TMUX)
    record("tmux", "角色宣告表", "PASS",
           f"由 {ROLES_CONF.name} 推導 {len(expected)} 個角色的預期 session")
    return expected


def check_tmux(role: str) -> None:
    heading("tmux 工作負載")
    expected_tmux = load_expected_tmux()
    # 「沒有 tmux 這個指令」與「有 tmux 但沒有 session」是兩種完全不同的故障,原本都落進
    # 下面那個 WARN（run() 把 FileNotFoundError 收成 rc=127），於是報告只寫
    # 「No such file or directory: 'tmux'」—— 讀起來像是某個路徑設定錯了。
    #
    # 缺指令記 FAIL 而不是 WARN 是刻意的:它是確定性的全面失效（每一支 start_*.sh 都會
    # exit 2，而啟動器每 30 分鐘重試、永遠補不起來）。WARN 在 --fail-on-warn 下只是
    # exit 2 DEGRADED，很容易被讀成「服務還在起」。
    if shutil.which("tmux") is None:
        record(
            "tmux",
            "tmux 指令",
            "FAIL",
            "系統未安裝 tmux —— 所有 session 型專案都起不來；"
            "以 sftp_transfer/deploy/install_tmux_offline.sh 離線補齊",
        )
        return
    proc = run(["tmux", "list-sessions", "-F", "#{session_name}"])
    if proc.returncode != 0:
        record(
            "tmux",
            "session 清單",
            "WARN",
            proc.stderr.strip() or "沒有 tmux session",
        )
        return
    actual = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    expected = expected_tmux.get(role, set())
    for name in sorted(expected):
        if name == "wave" and name not in actual:
            record("tmux", name, "SKIP", "wave 可選功能尚未啟動")
        else:
            record(
                "tmux",
                name,
                "PASS" if name in actual else "WARN",
                "session 存在" if name in actual else "預期 session 不存在",
            )
    extra = sorted(actual - expected)
    if extra:
        record("tmux", "其他 sessions", "INFO", ", ".join(extra))


def write_report(vessel: str, role: str, overall: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"automation_health_report_{timestamp}.md"
    counts = {key: sum(item.status == key for item in RESULTS) for key in _COLORS}
    lines = [
        "# NSSMS 自動化存活巡檢報告",
        "",
        f"- 產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 主機：{socket.gethostname()}",
        f"- 使用者：{getpass.getuser()}",
        f"- 身分：{vessel} / {role}",
        f"- 整體結果：{overall}",
        (
            "- 統計："
            + "　".join(f"{key}={counts[key]}" for key in ("PASS", "WARN", "FAIL", "INFO", "SKIP"))
        ),
        "",
        "| 分類 | 狀態 | 項目 | 詳細 |",
        "|---|---|---|---|",
    ]
    for item in RESULTS:
        detail = item.detail.replace("|", "\\|").replace("\n", "<br>")
        name = item.name.replace("|", "\\|")
        lines.append(f"| {item.section} | {item.status} | {name} | {detail} |")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    global _COMPACT
    parser = argparse.ArgumentParser(description="NSSMS systemd/timer/heartbeat 自動化存活巡檢")
    parser.add_argument(
        "--strict-wave",
        action="store_true",
        help="將 wave 腳本缺少或 service 失敗視為 FAIL（預設為 SKIP）",
    )
    parser.add_argument(
        "--fail-on-warn",
        action="store_true",
        help="沒有 FAIL 但存在 WARN 時以離開碼 2 結束",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="只輸出終端結果，不產生 Markdown 報告",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="只顯示 WARN/FAIL 與總結；完整明細仍寫入報告",
    )
    args = parser.parse_args()
    _COMPACT = args.compact

    if args.compact:
        print(
            "── NSSMS 自動化存活巡檢"
            f"（唯讀；wave={'嚴格' if args.strict_wave else '可空置'}）──"
        )
    else:
        print("=" * 68)
        print(" NSSMS 隱性自動化設定存活巡檢（唯讀）")
        print("=" * 68)
        print(f"檢查時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"專案根目錄：{SHARE_DIR}")
        print(f"wave 模式：{'嚴格' if args.strict_wave else '可選／空置允許'}")

    vessel, role = check_identity()
    check_user_manager()
    check_core_services()
    check_unit_sources()
    check_timers(args.strict_wave)
    check_failed_units(args.strict_wave)
    check_sudoers()
    heartbeat_probe(role)
    check_tmux(role)

    counts = {key: sum(item.status == key for item in RESULTS) for key in _COLORS}
    if counts["FAIL"]:
        overall = "UNHEALTHY"
    elif counts["WARN"]:
        overall = "DEGRADED"
    else:
        overall = "HEALTHY"

    if args.compact:
        print("── 巡檢總結 ──")
    else:
        heading("總結")
    print(
        "  "
        + "  ".join(
            f"{key}={counts[key]}" for key in ("PASS", "WARN", "FAIL", "INFO", "SKIP")
        )
    )
    print(f"  整體結果：{overall}")

    if not args.no_report:
        try:
            report = write_report(vessel, role, overall)
            print(f"  報告：{report}")
        except OSError as exc:
            record("report", "Markdown 報告", "WARN", f"寫入失敗：{exc}")

    if not args.compact:
        print("=" * 68)
    if counts["FAIL"]:
        return 1
    if args.fail_on_warn and counts["WARN"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
